import copy
import datetime
import logging
import os
import signal
import sys
import time
import uuid
from typing import Any, Callable, Dict, List, Optional
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import func, or_, and_, update
from sqlalchemy.exc import IntegrityError, OperationalError

from src.database import SessionLocal
from src.models import (
    AccountLedgerEntry,
    ExternalOrderLink,
    Order,
    OrderEvent,
    PaperAccount,
    ProviderConnection,
    ReconciliationRecord,
    SubmissionOutbox,
    WorkerHeartbeat,
    RuntimeOrchestrationConfig,
    StrategyRuntime,
)
from src.engine.orchestration.transmission_gate import (
    assert_orchestration_execution_is_internal_only,
    TransmissionProhibitedError,
)
from src.engine.paper.models import (
    LedgerEntryType,
    OrderSide,
    OrderStatus,
)
from src.engine.paper.state_machine import validate_order_transition
from src.engine.sandbox.upstox_adapter import (
    UpstoxSandboxAdapter,
    UpstoxPlaceResult,
    UpstoxCancelResult,
    UpstoxClientError,
    UpstoxRetryable429,
    UpstoxAmbiguousError,
)
from src.services.sandbox_gate_service import SandboxGateService

logger = logging.getLogger("tradepro.outbox_worker")

# Bounded SQLite lock retry constants
SQLITE_LOCK_MAX_RETRIES = 5
SQLITE_LOCK_BACKOFF_MS = [50, 100, 200, 400, 800]


def _is_sqlite_locked_error(exc: Exception) -> bool:
    """Return True only for transient SQLite busy/locked OperationalError."""
    if not isinstance(exc, OperationalError):
        return False
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


class SandboxOutboxWorker:
    """
    Dedicated delivery worker for the Upstox Sandbox Submission Outbox.
    Implements:
    - Bounded batch processing
    - Priority for CANCEL over PLACE
    - Database leasing and expired lease recovery
    - Durable pre-transmission marker (transmission_started_at)
    - Fail-closed expired-lease recovery
    - Double network-gate checking before transmission
    - Persistent WorkerHeartbeat tracking
    - Graceful shutdown
    - Conservative 429 and ambiguity handling
    - Bounded SQLite lock retry for reconciliation persistence
    """

    def __init__(
        self,
        worker_id: Optional[str] = None,
        batch_size: int = 10,
        lease_duration_seconds: int = 30,
        poll_interval_seconds: float = 1.0,
        adapter: Optional[UpstoxSandboxAdapter] = None,
        clock: Optional[Callable[[], datetime.datetime]] = None,
    ):
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.batch_size = max(1, min(batch_size, 100))
        self.lease_duration_seconds = lease_duration_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.adapter = adapter or UpstoxSandboxAdapter()
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self._running = False
        self._stop_requested = False

    def request_stop(self, *args) -> None:
        logger.info("Worker [%s] received stop request. Shutting down gracefully...", self.worker_id)
        self._stop_requested = True

    def run(self, max_runs: Optional[int] = None) -> None:
        """
        Main worker execution loop.
        Can run bounded iterations (for testing) or continuously.
        """
        self._running = True
        self._stop_requested = False

        # Register signal handlers if in main thread
        try:
            signal.signal(signal.SIGINT, self.request_stop)
            signal.signal(signal.SIGTERM, self.request_stop)
        except (ValueError, AttributeError):
            pass

        logger.info("Sandbox outbox worker [%s] started.", self.worker_id)
        runs = 0

        while self._running and not self._stop_requested:
            db = SessionLocal()
            try:
                self.record_heartbeat(db, status="HEALTHY")
                processed = self.process_batch(db)
                db.commit()
            except Exception as e:
                db.rollback()
                logger.error("Error in outbox worker loop: %s", str(e), exc_info=True)
                try:
                    self.record_heartbeat(db, status="ERROR")
                    db.commit()
                except Exception:
                    pass
            finally:
                db.close()

            runs += 1
            if max_runs is not None and runs >= max_runs:
                break

            if not self._stop_requested:
                time.sleep(self.poll_interval_seconds)

        # Worker shutdown
        db = SessionLocal()
        try:
            self.record_heartbeat(db, status="STOPPED")
            db.commit()
        except Exception:
            pass
        finally:
            db.close()
        logger.info("Sandbox outbox worker [%s] terminated cleanly.", self.worker_id)

    def record_heartbeat(self, db: Session, status: str = "HEALTHY", processed_delta: int = 0) -> None:
        now = self.clock()
        cfg_owner = os.environ.get("UPSTOX_SANDBOX_OWNER_ID")
        hb = db.query(WorkerHeartbeat).filter(WorkerHeartbeat.worker_id == self.worker_id).first()
        if not hb:
            hb = WorkerHeartbeat(
                worker_id=self.worker_id,
                owner_id=cfg_owner,
                provider_name="UPSTOX",
                status=status,
                last_heartbeat_at=now,
                batch_count=1,
                processed_count=processed_delta,
            )
            db.add(hb)
        else:
            hb.status = status
            hb.last_heartbeat_at = now
            hb.batch_count += 1
            hb.processed_count += processed_delta
            if cfg_owner:
                hb.owner_id = cfg_owner

    def process_batch(self, db: Session) -> int:
        now = self.clock()
        # 1. Recover expired leases (fail-closed based on transmission_started_at)
        self.recover_expired_leases(db, now)

        # 2. Claim pending records: Priority for CANCEL over PLACE
        claimed = self.claim_records(db, now)
        if not claimed:
            return 0

        # 3. Process each claimed record
        for outbox in claimed:
            self.process_record(db, outbox, now)

        return len(claimed)

    def recover_expired_leases(self, db: Session, now: datetime.datetime) -> None:
        """
        Fail-closed expired-lease recovery.

        If transmission_started_at is NULL: the external call never began.
          -> Safe to requeue (RETRY_SCHEDULED).
        If transmission_started_at is NOT NULL: the external call may have occurred.
          -> NEVER retransmit. Transition to RECONCILIATION_REQUIRED.
          -> Create exactly one OPEN reconciliation record.
          -> Zero adapter/network calls.
        """
        expired = db.query(SubmissionOutbox).filter(
            SubmissionOutbox.status == "CLAIMED",
            SubmissionOutbox.claim_lease_until < now,
        ).all()
        for rec in expired:
            if rec.transmission_started_at is None:
                # Safe: external call definitely did not begin
                rec.status = "RETRY_SCHEDULED"
                rec.claimed_by = None
                rec.claim_lease_until = None
                rec.last_error_code = "LEASE_EXPIRED"
                rec.last_error_message = "Worker lease expired before transmission; returned to queue."
            else:
                # UNSAFE: external call may have occurred. Fail closed.
                logger.warning(
                    "Expired lease for outbox %s with transmission_started_at=%s. "
                    "Failing closed to RECONCILIATION_REQUIRED.",
                    rec.id, rec.transmission_started_at,
                )
                order = db.query(Order).filter(
                    Order.id == rec.order_id,
                    Order.owner_id == rec.owner_id,
                ).first()

                if order:
                    curr_status = OrderStatus(order.status)
                    if curr_status != OrderStatus.RECONCILIATION_REQUIRED:
                        try:
                            validate_order_transition(
                                curr_status, OrderStatus.RECONCILIATION_REQUIRED,
                                actor="UPSTOX_WORKER", reason_code="LEASE_EXPIRED_AFTER_TRANSMISSION",
                            )
                            order.status = OrderStatus.RECONCILIATION_REQUIRED.value
                            seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(
                                OrderEvent.order_id == order.id
                            ).scalar() + 1
                            db.add(OrderEvent(
                                order_id=order.id,
                                sequence_number=seq,
                                previous_status=curr_status.value,
                                new_status=OrderStatus.RECONCILIATION_REQUIRED.value,
                                actor="UPSTOX_WORKER",
                                reason_code="LEASE_EXPIRED_AFTER_TRANSMISSION",
                                metadata_json={
                                    "error_message": "Lease expired after transmission may have begun",
                                    "transmission_started_at": rec.transmission_started_at.isoformat(),
                                },
                            ))
                        except Exception as ex:
                            logger.warning(
                                "Could not transition order %s during lease recovery: %s",
                                rec.order_id, str(ex),
                            )

                rec.status = "RECONCILIATION_REQUIRED"
                rec.claimed_by = None
                rec.claim_lease_until = None
                rec.last_error_code = "LEASE_EXPIRED_AFTER_TRANSMISSION"
                rec.last_error_message = "Lease expired after transmission may have begun; requires manual reconciliation."

                # Create exactly one OPEN reconciliation record (idempotent)
                try:
                    with db.begin_nested():
                        recon = ReconciliationRecord(
                            owner_id=rec.owner_id,
                            order_id=rec.order_id,
                            outbox_id=rec.id,
                            status="OPEN",
                        )
                        db.add(recon)
                        db.flush()
                except IntegrityError:
                    # Already exists — reload it
                    pass

    def claim_records(self, db: Session, now: datetime.datetime) -> List[SubmissionOutbox]:
        lease_until = now + datetime.timedelta(seconds=self.lease_duration_seconds)

        # Deterministic ordering:
        # 1. Priority ascending (CANCEL = 0, PLACE = 10)
        # 2. next_attempt_at ascending
        # 3. Creation sequence/timestamp ascending
        # 4. Stable ID ascending
        records = db.query(SubmissionOutbox).filter(
            SubmissionOutbox.status.in_(["PENDING", "RETRY_SCHEDULED"]),
            SubmissionOutbox.next_attempt_at <= now,
        ).order_by(
            SubmissionOutbox.priority.asc(),
            SubmissionOutbox.next_attempt_at.asc(),
            SubmissionOutbox.created_at.asc(),
            SubmissionOutbox.id.asc(),
        ).with_for_update().limit(self.batch_size).all()

        claimed = []
        for r in records:
            r.status = "CLAIMED"
            r.claimed_by = self.worker_id
            r.claim_lease_until = lease_until
            r.attempts += 1
            claimed.append(r)

        db.flush()
        return claimed

    def _commit_transmission_marker(self, db: Session, outbox: SubmissionOutbox, now: datetime.datetime) -> bool:
        """
        Persist the durable pre-transmission marker and commit.
        Returns True if the marker was successfully committed.
        Returns False if commit fails (caller must NOT proceed to external call).
        """
        outbox.transmission_started_at = now
        try:
            db.flush()
            db.commit()
            return True
        except Exception as exc:
            logger.error(
                "Failed to commit transmission marker for outbox %s: %s",
                outbox.id, str(exc),
            )
            try:
                db.rollback()
            except Exception:
                pass
            return False

    def process_record(self, db: Session, outbox: SubmissionOutbox, now: datetime.datetime) -> None:
        order = db.query(Order).filter(
            Order.id == outbox.order_id,
            Order.owner_id == outbox.owner_id,
        ).with_for_update().first()

        if not order:
            outbox.status = "DEAD_LETTER"
            outbox.last_error_code = "ORDER_NOT_FOUND"
            outbox.last_error_message = f"Referenced order {outbox.order_id} does not exist."
            return

        # Double check that the order does not belong to an orchestration fixture runtime
        if order.runtime_id:
            runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == order.runtime_id).first()
            is_orch_fixture = runtime and runtime.trading_mode == "BROKER_SANDBOX_RECORDED_FIXTURE"
            orch_cfg = db.query(RuntimeOrchestrationConfig).filter(RuntimeOrchestrationConfig.runtime_id == order.runtime_id).first()

            if orch_cfg or is_orch_fixture:
                if orch_cfg:
                    assert_orchestration_execution_is_internal_only(orch_cfg)

                # Safe Design B: Atomic conditional update via separate fresh session bound to the same engine.
                # Caller session `db` is NEVER committed or rolled back from this nested helper.
                engine = db.bind
                FreshSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
                with FreshSession() as fresh_db:
                    stmt = (
                        update(SubmissionOutbox)
                        .where(SubmissionOutbox.id == outbox.id)
                        .values(
                            status="DEAD_LETTER",
                            claimed_by=None,
                            claim_lease_until=None,
                            last_error_code="TRANSMISSION_PROHIBITED",
                            last_error_message="External transmission is prohibited for orchestration fixture runtime.",
                        )
                    )
                    fresh_db.execute(stmt)
                    fresh_db.commit()

                # Expire and reload the caller-owned row in-memory so it reflects the new state
                db.expire(outbox)
                db.refresh(outbox)

                raise TransmissionProhibitedError(
                    f"External transmission attempted on orchestration fixture runtime '{order.runtime_id}' for order '{order.id}'."
                )

        # Double check all network transmission gates immediately before transmission
        allowed, gate_reasons = SandboxGateService.check_outbox_transmission_gates(
            db=db,
            owner_id=outbox.owner_id,
            action_type=outbox.action_type,
            target_instrument_id=order.instrument_id,
            now=now,
        )

        if not allowed:
            # Gates failed: do not transmit! Reschedule or mark DEAD_LETTER if max attempts reached
            if outbox.attempts >= outbox.max_attempts:
                outbox.status = "DEAD_LETTER"
                outbox.claimed_by = None
                outbox.claim_lease_until = None
                outbox.last_error_code = "GATE_BLOCKED_EXHAUSTED"
                outbox.last_error_message = f"Max attempts exhausted before transmission: {'; '.join(gate_reasons)}"
                if outbox.action_type == "PLACE":
                    curr_status = OrderStatus(order.status)
                    if curr_status == OrderStatus.PENDING_SUBMISSION:
                        validate_order_transition(curr_status, OrderStatus.PROVIDER_REJECTED, actor="UPSTOX_WORKER", reason_code="GATE_BLOCKED_DEAD_LETTER")
                        order.status = OrderStatus.PROVIDER_REJECTED.value
                        seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
                        db.add(OrderEvent(
                            order_id=order.id,
                            sequence_number=seq,
                            previous_status=curr_status.value,
                            new_status=OrderStatus.PROVIDER_REJECTED.value,
                            actor="UPSTOX_WORKER",
                            reason_code="GATE_BLOCKED_DEAD_LETTER",
                            metadata_json={"error_message": outbox.last_error_message},
                        ))
                        self._release_reserved_cash(db, order, reason="GATE_BLOCKED_DEAD_LETTER")
                elif outbox.action_type == "CANCEL":
                    curr_status = OrderStatus(order.status)
                    if curr_status == OrderStatus.CANCEL_PENDING:
                        validate_order_transition(curr_status, OrderStatus.ACKNOWLEDGED, actor="UPSTOX_WORKER", reason_code="CANCEL_DEAD_LETTER_REVERT")
                        order.status = OrderStatus.ACKNOWLEDGED.value
                        seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
                        db.add(OrderEvent(
                            order_id=order.id,
                            sequence_number=seq,
                            previous_status=curr_status.value,
                            new_status=OrderStatus.ACKNOWLEDGED.value,
                            actor="UPSTOX_WORKER",
                            reason_code="CANCEL_DEAD_LETTER_REVERT",
                            metadata_json={"error_message": outbox.last_error_message},
                        ))
                        # Reservation remains unchanged. Never leave order stuck in CANCEL_PENDING.
                return

            outbox.status = "RETRY_SCHEDULED"
            outbox.claimed_by = None
            outbox.claim_lease_until = None
            outbox.next_attempt_at = now + datetime.timedelta(seconds=10)
            outbox.last_error_code = "GATE_BLOCKED"
            outbox.last_error_message = "; ".join(gate_reasons)
            return

        # --- TRANSACTION BOUNDARY: Commit the durable pre-transmission marker ---
        # This is a separate committed transaction BEFORE any external call.
        # If this commit fails, we do NOT contact the broker.
        if not self._commit_transmission_marker(db, outbox, now):
            # Marker commit failed — do NOT proceed to external call.
            # The outbox still has status=CLAIMED with no marker.
            # On lease expiry, recover_expired_leases will safely requeue it.
            logger.error("Aborting transmission for outbox %s: marker commit failed.", outbox.id)
            return

        # Re-query order since we committed (session state may be stale)
        order = db.query(Order).filter(
            Order.id == outbox.order_id,
            Order.owner_id == outbox.owner_id,
        ).first()
        outbox = db.query(SubmissionOutbox).filter(
            SubmissionOutbox.id == outbox.id,
        ).first()

        if not order or not outbox:
            return

        token = os.environ.get("UPSTOX_SANDBOX_ACCESS_TOKEN", "")

        # --- EXTERNAL CALL: No DB write transaction held during HTTP request ---
        if outbox.action_type == "PLACE":
            self._handle_place_action(db, outbox, order, token, now)
        elif outbox.action_type == "CANCEL":
            self._handle_cancel_action(db, outbox, order, token, now)
        else:
            outbox.status = "DEAD_LETTER"
            outbox.last_error_code = "UNKNOWN_ACTION"
            outbox.last_error_message = f"Unsupported action type '{outbox.action_type}'"

        db.commit()

    def _handle_place_action(
        self,
        db: Session,
        outbox: SubmissionOutbox,
        order: Order,
        token: str,
        now: datetime.datetime,
    ) -> None:
        curr_status = OrderStatus(order.status)
        if curr_status != OrderStatus.PENDING_SUBMISSION:
            logger.warning("Order %s status is %s; skipping place action", order.id, curr_status.value)
            outbox.status = "DEAD_LETTER"
            outbox.last_error_code = "INVALID_ORDER_STATE"
            outbox.last_error_message = f"Order status is {curr_status.value}, expected PENDING_SUBMISSION"
            return

        payload = copy.deepcopy(outbox.payload_json)
        # Release caller transaction so zero locks/transactions are held during external HTTP call
        try:
            db.rollback()
        except Exception:
            pass

        try:
            res = self.adapter.place_order(payload, token)
            # Success!
            provider_order_id = res.provider_order_id

            # Create external order link
            ext_link = ExternalOrderLink(
                owner_id=order.owner_id,
                order_id=order.id,
                provider_name="UPSTOX",
                provider_order_id=provider_order_id,
                submitted_at=now,
            )
            db.add(ext_link)

            # Order transitions to ACKNOWLEDGED
            validate_order_transition(curr_status, OrderStatus.ACKNOWLEDGED, actor="UPSTOX_SANDBOX", reason_code="ORDER_PLACED_V3")
            order.status = OrderStatus.ACKNOWLEDGED.value

            seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
            db.add(OrderEvent(
                order_id=order.id,
                sequence_number=seq,
                previous_status=curr_status.value,
                new_status=OrderStatus.ACKNOWLEDGED.value,
                actor="UPSTOX_SANDBOX",
                reason_code="ORDER_PLACED_V3",
                metadata_json={"provider_order_id": provider_order_id},
            ))

            outbox.status = "DELIVERED"
            outbox.claimed_by = None
            outbox.claim_lease_until = None
            outbox.last_error_code = None
            outbox.last_error_message = None

            # Update last_successful_transmission_at only after unambiguous success
            conn = db.query(ProviderConnection).filter(
                ProviderConnection.owner_id == order.owner_id,
                ProviderConnection.provider_name == "UPSTOX",
            ).first()
            if conn:
                conn.last_successful_transmission_at = now

        except UpstoxRetryable429 as e:
            # Under sandbox rules, 429 cannot rule out acceptance; never auto-retry
            self._transition_to_reconciliation(db, outbox, order, "HTTP_429_AMBIGUOUS", e.message)

        except UpstoxClientError as e:
            # Documented client rejection before order creation -> safe to mark PROVIDER_REJECTED
            curr_status = OrderStatus(order.status)
            validate_order_transition(curr_status, OrderStatus.PROVIDER_REJECTED, actor="UPSTOX_SANDBOX", reason_code=e.error_code)
            order.status = OrderStatus.PROVIDER_REJECTED.value

            seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
            db.add(OrderEvent(
                order_id=order.id,
                sequence_number=seq,
                previous_status=curr_status.value,
                new_status=OrderStatus.PROVIDER_REJECTED.value,
                actor="UPSTOX_SANDBOX",
                reason_code=e.error_code,
                metadata_json={"error_message": e.message, "status_code": e.status_code},
            ))

            # Release reserved cash!
            self._release_reserved_cash(db, order, reason=f"REJECTED:{e.error_code}")

            outbox.status = "DEAD_LETTER"
            outbox.claimed_by = None
            outbox.claim_lease_until = None
            outbox.last_error_code = e.error_code
            outbox.last_error_message = e.message

        except UpstoxAmbiguousError as e:
            # Ambiguous: 5xx, timeout, or ambiguous 429
            self._transition_to_reconciliation(db, outbox, order, "AMBIGUOUS_ERROR", str(e))

    def _handle_cancel_action(
        self,
        db: Session,
        outbox: SubmissionOutbox,
        order: Order,
        token: str,
        now: datetime.datetime,
    ) -> None:
        # Check if provider_order_id exists in ExternalOrderLink
        ext_link = db.query(ExternalOrderLink).filter(
            ExternalOrderLink.owner_id == order.owner_id,
            ExternalOrderLink.order_id == order.id,
        ).first()

        if not ext_link:
            # Order was never submitted externally (e.g. cancelled while in PENDING_SUBMISSION)
            # Safe to cancel locally!
            curr_status = OrderStatus(order.status)
            validate_order_transition(curr_status, OrderStatus.CANCELLED, actor="SYSTEM_OMS", reason_code="LOCAL_CANCEL_PRE_SUBMISSION")
            order.status = OrderStatus.CANCELLED.value

            seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
            db.add(OrderEvent(
                order_id=order.id,
                sequence_number=seq,
                previous_status=curr_status.value,
                new_status=OrderStatus.CANCELLED.value,
                actor="SYSTEM_OMS",
                reason_code="LOCAL_CANCEL_PRE_SUBMISSION",
            ))

            self._release_reserved_cash(db, order, reason="LOCAL_CANCEL")
            outbox.status = "DELIVERED"
            outbox.claimed_by = None
            outbox.claim_lease_until = None
            return

        provider_order_id = ext_link.provider_order_id
        # Release caller transaction so zero locks/transactions are held during external HTTP call
        try:
            db.rollback()
        except Exception:
            pass

        # Order has external link: transmit DELETE /v3/order/cancel
        try:
            res = self.adapter.cancel_order(provider_order_id, token)
            # Mandatory Correction 8: Confirm cancellation response explicitly confirms cancellation
            if res.cancelled:
                curr_status = OrderStatus(order.status)
                validate_order_transition(curr_status, OrderStatus.CANCELLED, actor="UPSTOX_SANDBOX", reason_code="CANCEL_ORDER_V3")
                order.status = OrderStatus.CANCELLED.value

                seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
                db.add(OrderEvent(
                    order_id=order.id,
                    sequence_number=seq,
                    previous_status=curr_status.value,
                    new_status=OrderStatus.CANCELLED.value,
                    actor="UPSTOX_SANDBOX",
                    reason_code="CANCEL_ORDER_V3",
                    metadata_json={"provider_order_id": res.provider_order_id},
                ))

                self._release_reserved_cash(db, order, reason="UPSTOX_CANCELLED")
                outbox.status = "DELIVERED"
                outbox.claimed_by = None
                outbox.claim_lease_until = None

                # Update last_successful_transmission_at only after unambiguous success
                conn = db.query(ProviderConnection).filter(
                    ProviderConnection.owner_id == order.owner_id,
                    ProviderConnection.provider_name == "UPSTOX",
                ).first()
                if conn:
                    conn.last_successful_transmission_at = now
            else:
                self._transition_to_reconciliation(db, outbox, order, "AMBIGUOUS_CANCEL_STATUS", "Provider did not confirm cancellation")

        except UpstoxRetryable429 as e:
            self._transition_to_reconciliation(db, outbox, order, "HTTP_429_AMBIGUOUS_CANCEL", e.message)

        except (UpstoxClientError, UpstoxAmbiguousError) as e:
            self._transition_to_reconciliation(db, outbox, order, "CANCEL_AMBIGUOUS_OR_CLIENT_ERROR", str(e))

    def _transition_to_reconciliation(
        self,
        db: Session,
        outbox: SubmissionOutbox,
        order: Order,
        error_code: str,
        error_message: str,
    ) -> None:
        """
        Transition outbox + order to RECONCILIATION_REQUIRED with bounded SQLite lock retry.
        On retry exhaustion, fail loudly but preserve the transmission_started_at marker
        so lease recovery will eventually force reconciliation.

        Session Ownership Architecture:
        - Design: ATTEMPT 0 USES CALLER SESSION; RETRIES USE FRESH SESSIONS.
        - Attempt 0 uses the caller's session directly so that nested transaction savepoints,
          caller session tracking, and caller test mock patches operate directly.
        - If transient SQLite lock contention occurs on Attempt 0, the caller's session
          undergoes `db.rollback()` to clear the failed attempt-0 nested transaction.
          This rollback is safe because previous outbox items are committed individually,
          the durable marker was committed in a prior transaction, and no unrelated pending
          writes exist in the caller session.
        - The caller session is NEVER closed by this helper (session lifecycle remains with caller).
        - Subsequent retries (Attempts 1..N) use fresh sessions derived from the engine to avoid
          reusing a tainted transaction state.
        - When a fresh retry session commits, `db.expire(outbox)` and `db.expire(order)` are
          invoked on the caller session so stale dirty ORM state cannot overwrite or flush
          old statuses.
        - On retry exhaustion, the last SQLite OperationalError is re-raised; the previously
          committed `transmission_started_at` marker remains intact in the DB.
        """
        outbox_id = outbox.id
        order_id = order.id
        bound_engine = db.get_bind()
        dialect_name = bound_engine.dialect.name if bound_engine is not None else ""
        is_sqlite = "sqlite" in dialect_name

        last_exc: Optional[OperationalError] = None

        # Attempt 0: Use caller's session directly
        try:
            self._do_reconciliation_transition(db, outbox, order, error_code, error_message)
            return  # Success
        except OperationalError as oe:
            if not (is_sqlite and _is_sqlite_locked_error(oe)):
                raise  # Not a transient SQLite lock error — propagate immediately
            last_exc = oe
            logger.warning(
                "SQLite lock contention on reconciliation transition: outbox_id=%s attempt=1/%d error_category=SQLITE_LOCKED",
                outbox_id,
                SQLITE_LOCK_MAX_RETRIES,
            )
            try:
                db.rollback()
            except Exception:
                pass

        # Subsequent attempts: Use fresh sessions from the engine
        from sqlalchemy.orm import sessionmaker as _sessionmaker
        FreshSessionFactory = _sessionmaker(
            bind=bound_engine, autocommit=False, autoflush=False
        )

        for attempt in range(1, SQLITE_LOCK_MAX_RETRIES):
            backoff_ms = SQLITE_LOCK_BACKOFF_MS[
                min(attempt - 1, len(SQLITE_LOCK_BACKOFF_MS) - 1)
            ]
            time.sleep(backoff_ms / 1000.0)

            fresh_db = FreshSessionFactory()
            try:
                fresh_order = (
                    fresh_db.query(Order).filter(Order.id == order_id).first()
                )
                fresh_outbox = (
                    fresh_db.query(SubmissionOutbox)
                    .filter(SubmissionOutbox.id == outbox_id)
                    .first()
                )
                if not fresh_order or not fresh_outbox:
                    raise RuntimeError(
                        f"outbox/order records missing during reconciliation retry (outbox_id={outbox_id})"
                    )

                self._do_reconciliation_transition(
                    fresh_db, fresh_outbox, fresh_order, error_code, error_message
                )
                fresh_db.commit()

                # Refresh/expire caller's ORM objects so they see the committed state
                try:
                    db.expire(outbox)
                    db.expire(order)
                except Exception:
                    pass
                return

            except OperationalError as oe:
                if not (is_sqlite and _is_sqlite_locked_error(oe)):
                    try:
                        fresh_db.rollback()
                    except Exception:
                        pass
                    raise

                last_exc = oe
                logger.warning(
                    "SQLite lock contention on reconciliation transition: outbox_id=%s attempt=%d/%d error_category=SQLITE_LOCKED",
                    outbox_id,
                    attempt + 1,
                    SQLITE_LOCK_MAX_RETRIES,
                )
                try:
                    fresh_db.rollback()
                except Exception:
                    pass

            except Exception:
                try:
                    fresh_db.rollback()
                except Exception:
                    pass
                raise

            finally:
                try:
                    fresh_db.close()
                except Exception:
                    pass

        # Retry exhaustion: fail loudly, preserve marker
        logger.error(
            "CRITICAL: SQLite lock retry exhausted: outbox_id=%s attempts=%d. "
            "transmission_started_at marker remains committed. "
            "Lease recovery will force reconciliation on next cycle.",
            outbox_id,
            SQLITE_LOCK_MAX_RETRIES,
        )
        if last_exc is not None:
            raise last_exc

    def _do_reconciliation_transition(
        self,
        db: Session,
        outbox: SubmissionOutbox,
        order: Order,
        error_code: str,
        error_message: str,
    ) -> None:
        """Inner reconciliation transition logic (may raise OperationalError on SQLite lock)."""
        curr_status = OrderStatus(order.status)
        if curr_status != OrderStatus.RECONCILIATION_REQUIRED:
            try:
                with db.begin_nested():
                    validate_order_transition(curr_status, OrderStatus.RECONCILIATION_REQUIRED, actor="UPSTOX_WORKER", reason_code=error_code)
                    order.status = OrderStatus.RECONCILIATION_REQUIRED.value
                    seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
                    db.add(OrderEvent(
                        order_id=order.id,
                        sequence_number=seq,
                        previous_status=curr_status.value,
                        new_status=OrderStatus.RECONCILIATION_REQUIRED.value,
                        actor="UPSTOX_WORKER",
                        reason_code=error_code,
                        metadata_json={"error_message": error_message[:500]},
                    ))
                    db.flush()
            except OperationalError:
                raise  # Let caller handle SQLite lock retry
            except Exception as ex:
                logger.warning("Could not transition order %s to RECONCILIATION_REQUIRED (concurrent race): %s", order.id, str(ex))
                db.refresh(order)

        outbox.last_error_code = error_code
        outbox.last_error_message = error_message[:500]
        # Update outbox status to RECONCILIATION_REQUIRED and release lease
        outbox.status = "RECONCILIATION_REQUIRED"
        outbox.claimed_by = None
        outbox.claim_lease_until = None

        # Concurrency-safe creation of exactly one OPEN reconciliation record:
        # Nested transaction/savepoint catches only the expected (owner_id, outbox_id) unique race,
        # preserves the outer order/outbox updates, reloads the winning OPEN record, and re-raises unrelated errors.
        try:
            with db.begin_nested():
                rec = ReconciliationRecord(
                    owner_id=outbox.owner_id,
                    order_id=order.id,
                    outbox_id=outbox.id,
                    status="OPEN",
                )
                db.add(rec)
                db.flush()
        except IntegrityError as exc:
            err_msg = str(exc).lower()
            is_expected_unique = (
                "uq_reconciliation_records_owner_outbox" in err_msg
                or ("unique constraint" in err_msg and "outbox_id" in err_msg)
                or ("reconciliation_records.owner_id" in err_msg and "reconciliation_records.outbox_id" in err_msg)
            )
            if not is_expected_unique:
                raise

            # Savepoint was already rolled back by with block; reload winning OPEN record
            winning_rec = db.query(ReconciliationRecord).filter(
                ReconciliationRecord.owner_id == outbox.owner_id,
                ReconciliationRecord.outbox_id == outbox.id,
            ).first()
            if not winning_rec:
                raise

    def _release_reserved_cash(self, db: Session, order: Order, reason: str) -> None:
        if order.side != OrderSide.BUY.value:
            return

        acct = db.query(PaperAccount).filter(PaperAccount.id == order.account_id).with_for_update().first()
        if not acct:
            return

        # Check if already released idempotently
        idem_key = f"order_release:{order.id}"
        existing_release = db.query(AccountLedgerEntry).filter(
            AccountLedgerEntry.owner_id == order.owner_id,
            AccountLedgerEntry.idempotency_key == idem_key,
        ).first()
        if existing_release:
            return

        remaining_qty = order.quantity_units - order.filled_quantity_units

        res_entry = db.query(AccountLedgerEntry).filter(
            AccountLedgerEntry.account_id == acct.id,
            AccountLedgerEntry.order_id == order.id,
            AccountLedgerEntry.entry_type == LedgerEntryType.CASH_RESERVATION.value,
        ).first()

        if res_entry:
            total_reserved = res_entry.amount_units
            already_released = db.query(func.coalesce(func.sum(AccountLedgerEntry.amount_units), 0)).filter(
                AccountLedgerEntry.account_id == acct.id,
                AccountLedgerEntry.order_id == order.id,
                AccountLedgerEntry.entry_type == LedgerEntryType.RESERVATION_RELEASE.value,
            ).scalar()
            unreleased = max(0, total_reserved - already_released)
            if order.quantity_units > 0 and remaining_qty < order.quantity_units:
                release_amount = (unreleased * remaining_qty) // order.quantity_units
            else:
                release_amount = unreleased
        else:
            price = order.limit_price_units or 0
            fee = (remaining_qty * price * 5) // 10000
            release_amount = (remaining_qty * price) + fee

        release_amount = min(release_amount, acct.reserved_cash_units)

        if release_amount <= 0:
            return

        acct.reserved_cash_units = max(0, acct.reserved_cash_units - release_amount)

        l_seq = db.query(func.coalesce(func.max(AccountLedgerEntry.sequence_number), 0)).filter(AccountLedgerEntry.account_id == acct.id).scalar() + 1
        ledger = AccountLedgerEntry(
            account_id=acct.id,
            owner_id=order.owner_id,
            sequence_number=l_seq,
            entry_type=LedgerEntryType.RESERVATION_RELEASE.value,
            amount_units=release_amount,
            balance_after_units=acct.total_cash_units,
            settled_cash_delta_units=0,
            reserved_cash_delta_units=-release_amount,
            settled_cash_after_units=acct.total_cash_units,
            reserved_cash_after_units=acct.reserved_cash_units,
            order_id=order.id,
            reason_code=f"OUTBOX_RELEASE:{reason}",
            idempotency_key=idem_key,
        )
        db.add(ledger)
        db.flush()
