"""Strategy Orchestration Evaluation Worker.

Implements:
- Bounded batch claiming of RUNNING orchestration runtimes
- Authoritative target-boundary candidate ordering across distinct timeframes
- Atomic database lease acquisition with monotonic fencing generations
- Checkpoint advancement and atomic persistence of RuntimeEvaluation
- Stale worker fencing and expired lease recovery
- Strict transmission prohibition gating before every step
- Zero external broker transmission; purely fixture replay
- Universal lock hierarchy: StrategyRuntime -> RuntimeOrchestrationConfig -> PaperAccount -> PaperPosition -> Order
- Authoritative PaperAccount execution barrier across participating members (RUNNING and PAUSED)
- Historical fill eligibility on subsequent candles (T_eval_close <= T_candle_open)
- Multi-action deterministic evaluation with provisional cash/position budget
- Accounting conservation with dual-balance ledger tracking
"""
import datetime
import json
import logging
import os
import signal
import sys
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import and_, func, or_, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from src.database import SessionLocal, is_sqlite_locked_error
from src.engine.orchestration.evaluator import OrchestrationEvaluator, SynchronizationError
from src.engine.orchestration.ingestion import (
    IngestionError,
    ingest_all_required_fixture_candles,
)
from src.engine.orchestration.models import (
    OrchestrationSnapshot,
    TIMEFRAME_SECONDS,
    utc,
)
from src.engine.orchestration.transmission_gate import (
    TransmissionProhibitedError,
    assert_orchestration_execution_is_internal_only,
)
from src.engine.paper.fill_model import DeterministicFillEngine, FillResult
from src.engine.paper.models import (
    InstrumentSpec,
    LedgerEntryType,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    get_instrument_spec,
)
from src.engine.paper.state_machine import (
    RuntimeStatus,
    validate_order_transition,
    validate_runtime_transition,
)
from src.models import (
    AccountLedgerEntry,
    ActionDecision,
    CompletedCandleEvent,
    Fill,
    Order,
    OrderEvent,
    OrderIntent,
    PaperAccount,
    PaperPosition,
    RiskDecision,
    RuntimeEvaluation,
    RuntimeEvent,
    RuntimeOrchestrationConfig,
    StrategyRuntime,
)
from src.services.orchestration_service import (
    AccountBarrierBlockedError,
    AccountUnderReplayOwnershipError,
    ConflictError,
    OrchestrationService,
    PermissionDeniedError,
    ResourceNotFoundError,
    RuntimeReplayBehindAccountError,
)
from src.services.paper_service import PaperService

logger = logging.getLogger("tradepro.evaluation_worker")


MAX_EVALUATION_RETRIES = 5


class StaleWorkerFencedError(Exception):
    """Raised when a worker's lease has expired or a newer fencing generation has superseded it."""
    pass


class StrategyEvaluationWorker:
    """Bounded evaluation worker for RUNNING strategy orchestration runtimes."""

    def __init__(
        self,
        worker_id: Optional[str] = None,
        batch_size: int = 10,
        lease_duration_seconds: int = 30,
        poll_interval_seconds: float = 1.0,
        evaluator: Optional[OrchestrationEvaluator] = None,
        clock: Optional[Callable[[], datetime.datetime]] = None,
        post_lock_hook: Optional[Callable[[], None]] = None,
    ):
        self.worker_id = worker_id or f"eval-worker-{uuid.uuid4().hex[:8]}"
        self.batch_size = max(1, min(batch_size, 50))
        self.lease_duration_seconds = max(5, min(lease_duration_seconds, 300))
        self.poll_interval_seconds = max(0.1, poll_interval_seconds)
        self.evaluator = evaluator or OrchestrationEvaluator()
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.post_lock_hook = post_lock_hook
        self._running = False
        self._stop_requested = False

    def request_stop(self, *args) -> None:
        """Signal handler callback for graceful shutdown."""
        logger.info("Evaluation worker [%s] received stop request. Shutting down gracefully...", self.worker_id)
        self._stop_requested = True

    def claim_next_candidate(self, db: Session) -> Optional[Tuple[str, int]]:
        """Claim a single eligible RUNNING orchestration configuration atomically.

        Orders candidate discovery by each member's target next evaluation close boundary,
        incorporating its own frozen timeframe duration.
        Returns (config_id, acquired_fencing_generation) if claimed, None otherwise.
        """
        now = utc(self.clock())

        # Query candidates whose runtime is RUNNING, retry_count < MAX_RETRIES, and lease is available
        query = (
            db.query(
                RuntimeOrchestrationConfig.id,
                RuntimeOrchestrationConfig.runtime_id,
                RuntimeOrchestrationConfig.owner_id,
                RuntimeOrchestrationConfig.fencing_generation,
                RuntimeOrchestrationConfig.lease_expires_at,
                RuntimeOrchestrationConfig.checkpoint_close_at,
                RuntimeOrchestrationConfig.replay_open_at,
                RuntimeOrchestrationConfig.timeframe,
                RuntimeOrchestrationConfig.alignment_offset_seconds,
            )
            .join(
                StrategyRuntime,
                and_(
                    StrategyRuntime.id == RuntimeOrchestrationConfig.runtime_id,
                    StrategyRuntime.owner_id == RuntimeOrchestrationConfig.owner_id,
                ),
            )
            .filter(
                StrategyRuntime.status == RuntimeStatus.RUNNING.value,
                RuntimeOrchestrationConfig.source_type == "FIXTURE_REPLAY",
                RuntimeOrchestrationConfig.execution_policy.in_(["INTERNAL_MOCK_ONLY", "INTERNAL_PAPER"]),
                RuntimeOrchestrationConfig.retry_count < MAX_EVALUATION_RETRIES,
                or_(
                    RuntimeOrchestrationConfig.lease_owner.is_(None),
                    RuntimeOrchestrationConfig.lease_expires_at <= now,
                ),
                or_(
                    RuntimeOrchestrationConfig.next_attempt_at.is_(None),
                    RuntimeOrchestrationConfig.next_attempt_at <= now,
                ),
                or_(
                    RuntimeOrchestrationConfig.checkpoint_close_at.is_(None),
                    RuntimeOrchestrationConfig.checkpoint_close_at < RuntimeOrchestrationConfig.replay_close_at,
                ),
            )
            .limit(self.batch_size * 5)
        )

        candidates = query.all()

        # Sort candidate runtimes by their target next boundary close using each member's frozen timeframe
        def cand_sort_key(c):
            c_id, c_rt_id, c_own, c_gen, c_exp, c_chk, c_open, c_tf, c_offset = c
            tf_sec = TIMEFRAME_SECONDS.get(c_tf, 900)
            next_b = (c_chk + datetime.timedelta(seconds=tf_sec)) if c_chk else (c_open + datetime.timedelta(seconds=tf_sec))
            return (next_b, c_rt_id)

        sorted_candidates = sorted(candidates, key=cand_sort_key)

        for cand_id, cand_runtime_id, cand_owner_id, cand_gen, _, _, _, _, _ in sorted_candidates:
            # 1. Lock and revalidate StrategyRuntime row with with_for_update() (Level 1)
            active_rt = (
                db.query(StrategyRuntime)
                .filter(
                    StrategyRuntime.id == cand_runtime_id,
                    StrategyRuntime.owner_id == cand_owner_id,
                    StrategyRuntime.status == RuntimeStatus.RUNNING.value,
                )
                .with_for_update()
                .first()
            )
            if not active_rt:
                # Runtime is no longer RUNNING (paused, stopped, or missing)
                continue

            # 2. Atomic conditional update acquiring lease and advancing fencing generation (Level 2)
            new_gen = cand_gen + 1
            lease_until = now + datetime.timedelta(seconds=self.lease_duration_seconds)

            stmt = (
                update(RuntimeOrchestrationConfig)
                .where(
                    RuntimeOrchestrationConfig.id == cand_id,
                    RuntimeOrchestrationConfig.owner_id == cand_owner_id,
                    RuntimeOrchestrationConfig.runtime_id == cand_runtime_id,
                    RuntimeOrchestrationConfig.fencing_generation == cand_gen,
                    RuntimeOrchestrationConfig.source_type == "FIXTURE_REPLAY",
                    RuntimeOrchestrationConfig.execution_policy.in_(["INTERNAL_MOCK_ONLY", "INTERNAL_PAPER"]),
                    RuntimeOrchestrationConfig.retry_count < MAX_EVALUATION_RETRIES,
                    or_(
                        RuntimeOrchestrationConfig.checkpoint_close_at.is_(None),
                        RuntimeOrchestrationConfig.checkpoint_close_at < RuntimeOrchestrationConfig.replay_close_at,
                    ),
                    or_(
                        RuntimeOrchestrationConfig.next_attempt_at.is_(None),
                        RuntimeOrchestrationConfig.next_attempt_at <= now,
                    ),
                    or_(
                        RuntimeOrchestrationConfig.lease_owner.is_(None),
                        RuntimeOrchestrationConfig.lease_expires_at <= now,
                    ),
                )
                .values(
                    lease_owner=self.worker_id,
                    lease_expires_at=lease_until,
                    fencing_generation=new_gen,
                    updated_at=now,
                )
            )
            res = db.execute(stmt)
            if res.rowcount == 1:
                db.commit()
                return cand_id, new_gen
            else:
                db.rollback()

        return None

    def release_lease(
        self,
        db: Session,
        config_id: str,
        acquired_gen: int,
        *,
        reason_code: Optional[str] = None,
        retry_delay_seconds: Optional[int] = None,
        increment_retry: bool = False,
    ) -> None:
        """Release lease on error, pause, or completion with atomic poison-work quarantine handling."""
        now = utc(self.clock())
        config = db.query(RuntimeOrchestrationConfig).filter(
            RuntimeOrchestrationConfig.id == config_id,
            RuntimeOrchestrationConfig.fencing_generation == acquired_gen,
            RuntimeOrchestrationConfig.lease_owner == self.worker_id,
        ).first()

        if not config:
            return  # Worker fenced out or lease already cleared

        # Lock StrategyRuntime row FIRST (Level 1)
        active_rt = (
            db.query(StrategyRuntime)
            .filter(
                StrategyRuntime.id == config.runtime_id,
                StrategyRuntime.owner_id == config.owner_id,
            )
            .with_for_update()
            .first()
        )

        # Lock PaperAccount (Level 3) if runtime has an account
        if active_rt and active_rt.account_id and config.execution_policy == "INTERNAL_PAPER":
            db.query(PaperAccount).filter(
                PaperAccount.id == active_rt.account_id,
                PaperAccount.owner_id == config.owner_id,
            ).with_for_update().first()

        current_retries = config.retry_count
        values_dict: Dict[str, Any] = {
            "lease_owner": None,
            "lease_expires_at": None,
            "updated_at": now,
        }

        if increment_retry:
            new_retries = current_retries + 1
            if new_retries >= MAX_EVALUATION_RETRIES:
                # Atomic poison-work quarantine transition
                values_dict["retry_count"] = MAX_EVALUATION_RETRIES
                values_dict["next_attempt_at"] = None
                values_dict["last_reason_code"] = f"QUARANTINED_{(reason_code or 'RETRY_EXHAUSTED')[:50]}"

                if active_rt and active_rt.status == RuntimeStatus.RUNNING.value:
                    validate_runtime_transition(
                        RuntimeStatus.RUNNING,
                        RuntimeStatus.PAUSED,
                        actor=self.worker_id,
                        reason_code="EVALUATION_QUARANTINED",
                    )
                    active_rt.status = RuntimeStatus.PAUSED.value
                    active_rt.version = active_rt.version + 1
                    active_rt.updated_at = now

                    seq = (
                        db.query(func.coalesce(func.max(RuntimeEvent.sequence_number), 0))
                        .filter(RuntimeEvent.runtime_id == active_rt.id)
                        .scalar()
                        + 1
                    )
                    event = RuntimeEvent(
                        runtime_id=active_rt.id,
                        sequence_number=seq,
                        previous_status=RuntimeStatus.RUNNING.value,
                        new_status=RuntimeStatus.PAUSED.value,
                        actor=self.worker_id,
                        reason_code="EVALUATION_QUARANTINED",
                        metadata_json={
                            "quarantine_reason": reason_code or "RETRY_EXHAUSTED",
                            "exhausted_retries": MAX_EVALUATION_RETRIES,
                        },
                        created_at=now,
                    )
                    db.add(event)
            else:
                values_dict["retry_count"] = new_retries
                delay = retry_delay_seconds if retry_delay_seconds is not None else min(60, 5 * (2 ** (new_retries - 1)))
                values_dict["next_attempt_at"] = now + datetime.timedelta(seconds=delay)
                if reason_code:
                    values_dict["last_reason_code"] = reason_code[:64]
        else:
            # Ordinary deferral without consuming retry attempts (e.g. AccountBarrierBlockedError)
            if reason_code:
                values_dict["last_reason_code"] = reason_code[:64]
            if retry_delay_seconds is not None:
                values_dict["next_attempt_at"] = now + datetime.timedelta(seconds=retry_delay_seconds)
            elif reason_code == "ACCOUNT_BARRIER_DEFERRED":
                values_dict["next_attempt_at"] = now + datetime.timedelta(seconds=1)
            else:
                values_dict["next_attempt_at"] = None

        stmt = (
            update(RuntimeOrchestrationConfig)
            .where(
                RuntimeOrchestrationConfig.id == config_id,
                RuntimeOrchestrationConfig.owner_id == config.owner_id,
                RuntimeOrchestrationConfig.runtime_id == config.runtime_id,
                RuntimeOrchestrationConfig.fencing_generation == acquired_gen,
                RuntimeOrchestrationConfig.lease_owner == self.worker_id,
            )
            .values(**values_dict)
        )
        db.execute(stmt)
        db.commit()

    def evaluate_candidate(
        self,
        db: Session,
        config_id: str,
    ) -> Optional[RuntimeEvaluation]:
        """Claim and process an evaluation step for candidate config preserving global lock ordering."""
        config = db.query(RuntimeOrchestrationConfig).filter(RuntimeOrchestrationConfig.id == config_id).first()
        if not config:
            return None

        # Lock StrategyRuntime row FIRST
        active_rt = (
            db.query(StrategyRuntime)
            .filter(
                StrategyRuntime.id == config.runtime_id,
                StrategyRuntime.owner_id == config.owner_id,
                StrategyRuntime.status == RuntimeStatus.RUNNING.value,
            )
            .with_for_update()
            .first()
        )
        if not active_rt:
            return None

        if config.lease_owner != self.worker_id:
            now = utc(self.clock())
            expires = now + datetime.timedelta(seconds=self.lease_duration_seconds)
            new_gen = config.fencing_generation + 1
            stmt = (
                update(RuntimeOrchestrationConfig)
                .where(
                    RuntimeOrchestrationConfig.id == config.id,
                    RuntimeOrchestrationConfig.owner_id == config.owner_id,
                    RuntimeOrchestrationConfig.runtime_id == config.runtime_id,
                    RuntimeOrchestrationConfig.fencing_generation == config.fencing_generation,
                    RuntimeOrchestrationConfig.source_type == "FIXTURE_REPLAY",
                    RuntimeOrchestrationConfig.execution_policy.in_(["INTERNAL_MOCK_ONLY", "INTERNAL_PAPER"]),
                    RuntimeOrchestrationConfig.retry_count < MAX_EVALUATION_RETRIES,
                    or_(
                        RuntimeOrchestrationConfig.lease_owner.is_(None),
                        RuntimeOrchestrationConfig.lease_expires_at <= now,
                    ),
                    or_(
                        RuntimeOrchestrationConfig.checkpoint_close_at.is_(None),
                        RuntimeOrchestrationConfig.checkpoint_close_at < RuntimeOrchestrationConfig.replay_close_at,
                    ),
                )
                .values(
                    lease_owner=self.worker_id,
                    lease_expires_at=expires,
                    fencing_generation=new_gen,
                    updated_at=now,
                )
            )
            res = db.execute(stmt)
            if res.rowcount == 0:
                db.rollback()
                return None
            db.commit()
            return self.process_runtime_step(db, config.id, new_gen)
        return self.process_runtime_step(db, config.id, config.fencing_generation)

    def process_runtime_step(
        self,
        db: Session,
        config_id: str,
        acquired_gen: int,
    ) -> Optional[RuntimeEvaluation]:
        """Process exactly one evaluation step for the claimed runtime configuration."""
        now = utc(self.clock())

        config = db.query(RuntimeOrchestrationConfig).filter(
            RuntimeOrchestrationConfig.id == config_id
        ).first()

        if not config:
            return None

        # 1. Strict Transmission Gate check (zero external broker requests)
        assert_orchestration_execution_is_internal_only(config)

        if config.lease_owner is None:
            stmt = (
                update(RuntimeOrchestrationConfig)
                .where(
                    RuntimeOrchestrationConfig.id == config_id,
                    RuntimeOrchestrationConfig.fencing_generation == acquired_gen,
                    RuntimeOrchestrationConfig.lease_owner.is_(None),
                )
                .values(
                    lease_owner=self.worker_id,
                    lease_expires_at=now + datetime.timedelta(seconds=self.lease_duration_seconds),
                    updated_at=now,
                )
            )
            db.execute(stmt)
            db.commit()
            config = db.query(RuntimeOrchestrationConfig).filter(
                RuntimeOrchestrationConfig.id == config_id
            ).first()

        # 2. Re-verify runtime status and readiness
        runtime = db.query(StrategyRuntime).filter(
            StrategyRuntime.id == config.runtime_id,
            StrategyRuntime.owner_id == config.owner_id,
        ).first()

        if not runtime or runtime.status != RuntimeStatus.RUNNING.value:
            # Runtime is no longer RUNNING (paused, stopped, or missing)
            logger.info("Runtime '%s' is not in RUNNING state (%s). Releasing lease.", config.runtime_id, getattr(runtime, "status", None))
            self.release_lease(db, config_id, acquired_gen, reason_code=f"RUNTIME_STATUS_{getattr(runtime, 'status', 'MISSING')}")
            return None

        # Readiness gate check (kill switch, active owner, etc.)
        readiness = OrchestrationService.evaluate_activation_readiness(
            db, config.runtime_id, config.owner_id, now=now, target_action="EXECUTE"
        )
        if not readiness["ready"]:
            reasons_summary = "; ".join(readiness["reasons"])
            logger.warning("Runtime '%s' failed eligibility gate: %s", config.runtime_id, reasons_summary)
            self.release_lease(
                db,
                config_id,
                acquired_gen,
                reason_code="ELIGIBILITY_GATE_FAILED",
                retry_delay_seconds=30,
                increment_retry=True,
            )
            return None

        # 3. Determine next evaluation boundary using this member's timeframe
        tf_seconds = TIMEFRAME_SECONDS.get(config.timeframe, 900)

        # Verify replay bounds alignment to timeframe
        replay_duration = (config.replay_close_at - config.replay_open_at).total_seconds()
        if replay_duration <= 0 or replay_duration % tf_seconds != 0:
            logger.error("Replay bounds not aligned to timeframe '%s' for runtime '%s'", config.timeframe, config.runtime_id)
            self.release_lease(
                db, config_id, acquired_gen, reason_code="REPLAY_BOUNDS_MISALIGNED", retry_delay_seconds=60, increment_retry=True
            )
            return None

        if config.checkpoint_close_at is None:
            next_boundary = config.replay_open_at + datetime.timedelta(seconds=tf_seconds)
        else:
            next_boundary = config.checkpoint_close_at + datetime.timedelta(seconds=tf_seconds)

        next_boundary = utc(next_boundary)

        # 4. Check if replay boundary has reached replay_close_at
        if next_boundary > config.replay_close_at:
            if config.checkpoint_close_at is not None and config.checkpoint_close_at >= config.replay_close_at:
                logger.info("Runtime '%s' reached end of replay window (%s). Marking COMPLETED.", config.runtime_id, config.replay_close_at)
                self._complete_runtime(db, runtime, config, acquired_gen, now)
                return None
            else:
                logger.error(
                    "Runtime '%s' checkpoint (%s) has not reached replay_close_at (%s) but next_boundary (%s) exceeds it.",
                    config.runtime_id,
                    config.checkpoint_close_at,
                    config.replay_close_at,
                    next_boundary,
                )
                self.release_lease(
                    db, config_id, acquired_gen, reason_code="REPLAY_BOUNDS_MISALIGNED", retry_delay_seconds=60, increment_retry=True
                )
                return None

        # 5. Ingest required fixture candles up to next_boundary
        try:
            ingest_all_required_fixture_candles(
                db, config, up_to_close_at=next_boundary, clock=self.clock
            )
            db.commit()
        except IngestionError as e:
            logger.error("Candle ingestion error for runtime '%s': %s", config.runtime_id, e)
            db.rollback()
            self.release_lease(
                db, config_id, acquired_gen, reason_code="CANDLE_INGESTION_ERROR", retry_delay_seconds=10, increment_retry=True
            )
            return None

        # 6. Evaluate boundary
        try:
            evaluation = self.evaluator.evaluate_boundary(
                db, config, next_boundary, clock=self.clock
            )
        except SynchronizationError as e:
            # Missing candle at boundary: cannot evaluate yet
            logger.warning("Series synchronization not ready for runtime '%s' at boundary %s: %s", config.runtime_id, next_boundary, e)
            self.release_lease(
                db, config_id, acquired_gen, reason_code="SERIES_UNSYNCHRONIZED", retry_delay_seconds=5
            )
            return None
        except Exception as e:
            logger.error("Evaluation computation error for runtime '%s': %s", config.runtime_id, e, exc_info=True)
            self.release_lease(
                db, config_id, acquired_gen, reason_code="EVALUATION_ERROR", retry_delay_seconds=10, increment_retry=True
            )
            return None

        # 7. Fenced Atomic Finalize: persist RuntimeEvaluation, process fills, budget actions, advance checkpoint
        try:
            persisted_eval = self._fenced_finalize_step(
                db, config, runtime, evaluation, next_boundary, acquired_gen, now, post_lock_hook=self.post_lock_hook
            )
            return persisted_eval
        except AccountBarrierBlockedError as e:
            logger.info("Runtime '%s' deferred by account barrier: %s", config.runtime_id, e)
            db.rollback()
            self.release_lease(
                db,
                config_id,
                acquired_gen,
                reason_code="ACCOUNT_BARRIER_DEFERRED",
                retry_delay_seconds=1,
                increment_retry=False,
            )
            return None
        except RuntimeReplayBehindAccountError as e:
            logger.error("Runtime '%s' is behind account watermark: %s", config.runtime_id, e)
            db.rollback()
            self.release_lease(
                db,
                config_id,
                acquired_gen,
                reason_code="RUNTIME_BEHIND_WATERMARK",
                retry_delay_seconds=30,
                increment_retry=True,
            )
            return None
        except StaleWorkerFencedError:
            logger.warning("Worker [%s] was fenced out during finalization of runtime '%s'.", self.worker_id, config.runtime_id)
            db.rollback()
            return None
        except Exception as e:
            import traceback
            traceback.print_exc()
            logger.error("Finalization error for runtime '%s': %s", config.runtime_id, e, exc_info=True)
            db.rollback()
            self.release_lease(
                db, config_id, acquired_gen, reason_code="FINALIZATION_ERROR", retry_delay_seconds=10, increment_retry=True
            )
            return None

    @staticmethod
    def _resolve_instrument_spec(
        inst_spec_dict: Dict[str, Any],
        snapshot: OrchestrationSnapshot,
        fallback_instrument_id: Optional[str] = None,
    ) -> InstrumentSpec:
        inst_id = inst_spec_dict.get("instrument_id") or fallback_instrument_id or "NSE_INDEX|Nifty 50"
        ref_ds = snapshot.datasets[0].dataset_id if snapshot.datasets else "synthetic_underlying_nifty_15m"
        subj_ds = snapshot.datasets[1].dataset_id if len(snapshot.datasets) > 1 else ref_ds
        spec_fields: Dict[str, Any] = {
            "instrument_id": inst_id,
            "symbol": inst_spec_dict.get("symbol") or "NIFTY",
            "reference_dataset_id": ref_ds,
            "execution_dataset_id": subj_ds,
            "dataset_id": subj_ds,
            "lot_size_units": inst_spec_dict.get("lot_size_units", 1),
            "tick_size_units": inst_spec_dict.get("tick_size_units", 5),
            "price_scale": inst_spec_dict.get("price_scale", 2),
            "min_quantity_units": inst_spec_dict.get("min_quantity_units", 1),
            "max_quantity_units": inst_spec_dict.get("max_quantity_units", 500000),
        }
        for k, v in inst_spec_dict.items():
            if k in InstrumentSpec.model_fields and v is not None:
                spec_fields[k] = v
        return InstrumentSpec(**spec_fields)

    def _fenced_finalize_step(
        self,
        db: Session,
        config: RuntimeOrchestrationConfig,
        runtime: StrategyRuntime,
        evaluation: RuntimeEvaluation,
        boundary: datetime.datetime,
        acquired_gen: int,
        now: datetime.datetime,
        *,
        post_lock_hook: Optional[Callable[[], None]] = None,
    ) -> RuntimeEvaluation:
        """Atomically persist evaluation, fills, provisional budget orders, advance checkpoint, and update watermark."""
        # 1. Level 1: Verify and lock active runtime with SELECT ... FOR UPDATE
        active_runtime = (
            db.query(StrategyRuntime)
            .filter(
                StrategyRuntime.id == config.runtime_id,
                StrategyRuntime.owner_id == config.owner_id,
            )
            .with_for_update()
            .first()
        )
        if not active_runtime:
            raise StaleWorkerFencedError(
                f"Runtime '{config.runtime_id}' not found; aborting finalization."
            )
        if active_runtime.status != RuntimeStatus.RUNNING.value:
            raise StaleWorkerFencedError(
                f"Runtime '{config.runtime_id}' is in status '{active_runtime.status}' (expected RUNNING); aborting finalization."
            )

        # Deterministic concurrency test hook invoked after Level 1 lock is held
        effective_hook = post_lock_hook or self.post_lock_hook
        if effective_hook is not None:
            effective_hook()

        db.refresh(active_runtime)
        if active_runtime.status != RuntimeStatus.RUNNING.value:
            raise StaleWorkerFencedError(
                f"Runtime '{config.runtime_id}' is in status '{active_runtime.status}' (expected RUNNING); aborting finalization."
            )

        # 2. Level 2: Lock RuntimeOrchestrationConfig with FOR UPDATE and revalidate all Phase 3 finalization guards
        curr_cfg = (
            db.query(RuntimeOrchestrationConfig)
            .filter(
                RuntimeOrchestrationConfig.id == config.id,
                RuntimeOrchestrationConfig.owner_id == config.owner_id,
                RuntimeOrchestrationConfig.runtime_id == config.runtime_id,
            )
            .with_for_update()
            .first()
        )
        if not curr_cfg:
            raise StaleWorkerFencedError(f"Configuration '{config.id}' not found; aborting finalization.")

        if curr_cfg.owner_id != active_runtime.owner_id or curr_cfg.runtime_id != active_runtime.id:
            raise StaleWorkerFencedError("Owner/runtime/configuration consistency check failed; aborting finalization.")

        if curr_cfg.checkpoint_close_at is not None and curr_cfg.checkpoint_close_at >= boundary:
            # Winning worker already advanced checkpoint; reload winning evaluation
            winner = (
                db.query(RuntimeEvaluation)
                .filter(
                    RuntimeEvaluation.owner_id == config.owner_id,
                    RuntimeEvaluation.runtime_id == config.runtime_id,
                    RuntimeEvaluation.timeframe == config.timeframe,
                    RuntimeEvaluation.close_at == boundary,
                )
                .first()
            )
            if curr_cfg.lease_owner == self.worker_id and curr_cfg.fencing_generation == acquired_gen:
                curr_cfg.lease_owner = None
                curr_cfg.lease_expires_at = None
                curr_cfg.updated_at = now
            db.commit()
            if winner:
                db.refresh(winner)
                if winner.evaluation_fingerprint != evaluation.evaluation_fingerprint:
                    raise ConflictError(
                        f"Conflicting evaluation at boundary {boundary}: existing {winner.evaluation_fingerprint} != {evaluation.evaluation_fingerprint}"
                    )
                return winner
            return evaluation

        if curr_cfg.lease_owner != self.worker_id:
            raise StaleWorkerFencedError(f"Worker '{self.worker_id}' lost lease to '{curr_cfg.lease_owner}'; aborting finalization.")
        if curr_cfg.lease_expires_at is None or curr_cfg.lease_expires_at <= now:
            raise StaleWorkerFencedError(f"Lease for worker '{self.worker_id}' has expired; aborting finalization.")
        if curr_cfg.fencing_generation != acquired_gen:
            raise StaleWorkerFencedError(f"Fencing generation mismatch (expected {acquired_gen}, got {curr_cfg.fencing_generation}); aborting finalization.")
        if curr_cfg.checkpoint_close_at != config.checkpoint_close_at:
            raise StaleWorkerFencedError(f"Checkpoint mismatch on config '{config.id}'; aborting finalization.")

        tf_seconds = TIMEFRAME_SECONDS.get(config.timeframe, 900)
        expected_next = (curr_cfg.checkpoint_close_at + datetime.timedelta(seconds=tf_seconds)) if curr_cfg.checkpoint_close_at else (curr_cfg.replay_open_at + datetime.timedelta(seconds=tf_seconds))
        if boundary != expected_next:
            raise ValueError(f"Target boundary {boundary} does not match expected next boundary {expected_next}.")
        if boundary > curr_cfg.replay_close_at:
            raise ValueError(f"Target boundary {boundary} exceeds replay_close_at {curr_cfg.replay_close_at}.")

        assert_orchestration_execution_is_internal_only(config)

        # 3. Level 3: Lock PaperAccount and enforce Account Barrier & Watermark (if INTERNAL_PAPER)
        account: Optional[PaperAccount] = None
        tf_seconds = TIMEFRAME_SECONDS.get(config.timeframe, 900)

        if config.execution_policy == "INTERNAL_PAPER":
            account = (
                db.query(PaperAccount)
                .filter(
                    PaperAccount.id == active_runtime.account_id,
                    PaperAccount.owner_id == config.owner_id,
                )
                .with_for_update()
                .first()
            )
            if not account:
                raise ResourceNotFoundError(f"Linked paper account '{active_runtime.account_id}' not found.")
            if not account.is_active:
                raise ValueError("Linked paper account is inactive.")

            # 3a. Account Replay Barrier Check across all participating members (RUNNING and PAUSED)
            # Membership survives pause/quarantine while the runtime can resume.
            participating_members = (
                db.query(
                    StrategyRuntime.id,
                    RuntimeOrchestrationConfig.checkpoint_close_at,
                    RuntimeOrchestrationConfig.replay_open_at,
                    RuntimeOrchestrationConfig.replay_close_at,
                    RuntimeOrchestrationConfig.timeframe,
                )
                .join(RuntimeOrchestrationConfig, StrategyRuntime.id == RuntimeOrchestrationConfig.runtime_id)
                .filter(
                    StrategyRuntime.account_id == account.id,
                    StrategyRuntime.owner_id == config.owner_id,
                    StrategyRuntime.status.in_([RuntimeStatus.RUNNING.value, RuntimeStatus.PAUSED.value]),
                    RuntimeOrchestrationConfig.execution_policy == "INTERNAL_PAPER",
                )
                .all()
            )
            for m_id, m_chk, m_open, m_close, m_tf in participating_members:
                if m_id == config.runtime_id:
                    continue
                m_tf_sec = TIMEFRAME_SECONDS.get(m_tf, 900)
                m_next = (m_chk + datetime.timedelta(seconds=m_tf_sec)) if m_chk else (m_open + datetime.timedelta(seconds=m_tf_sec))
                if m_next <= m_close:
                    if m_next < boundary:
                        raise AccountBarrierBlockedError(
                            f"Account {account.id} barrier blocked: member runtime '{m_id}' has earlier target boundary {m_next} < {boundary}."
                        )
                    elif m_next == boundary and m_id < config.runtime_id:
                        raise AccountBarrierBlockedError(
                            f"Account {account.id} barrier blocked: member runtime '{m_id}' has same target boundary {m_next} with tie priority."
                        )

            # 3b. Account Watermark Check
            if account.committed_replay_watermark is not None:
                if boundary < account.committed_replay_watermark:
                    raise RuntimeReplayBehindAccountError(
                        f"Runtime '{config.runtime_id}' target boundary {boundary} is behind account committed replay watermark {account.committed_replay_watermark}."
                    )
                elif boundary == account.committed_replay_watermark:
                    later_committed = (
                        db.query(RuntimeEvaluation.runtime_id)
                        .join(StrategyRuntime, RuntimeEvaluation.runtime_id == StrategyRuntime.id)
                        .join(RuntimeOrchestrationConfig, RuntimeEvaluation.config_id == RuntimeOrchestrationConfig.id)
                        .filter(
                            StrategyRuntime.account_id == account.id,
                            StrategyRuntime.owner_id == account.owner_id,
                            RuntimeEvaluation.owner_id == account.owner_id,
                            RuntimeEvaluation.close_at == boundary,
                            RuntimeEvaluation.runtime_id >= config.runtime_id,
                            RuntimeOrchestrationConfig.execution_policy == "INTERNAL_PAPER",
                        )
                        .first()
                    )
                    if later_committed:
                        raise RuntimeReplayBehindAccountError(
                            f"Runtime '{config.runtime_id}' target boundary {boundary} is blocked: member '{later_committed[0]}' already committed at or after this tie priority."
                        )

        # Deterministic concurrency test hook invoked after Level 1-3 row locks are held
        effective_hook = post_lock_hook or self.post_lock_hook
        if effective_hook is not None:
            effective_hook()

        # Parse snapshot for instruments and policy definitions
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)
        action_snap = json.loads(snapshot.action_policy_snapshot) if snapshot.action_policy_snapshot else {}
        risk_snap = json.loads(snapshot.risk_policy_snapshot) if snapshot.risk_policy_snapshot else {}
        inst_spec_dict = json.loads(snapshot.instrument_specification) if snapshot.instrument_specification else {}

        # 4. Historical Fill Processing (if INTERNAL_PAPER)
        if config.execution_policy == "INTERNAL_PAPER" and account is not None:
            candle_open_at = boundary - datetime.timedelta(seconds=tf_seconds)

            # Query open orders eligible for fills at this boundary:
            # Order generated at evaluation close T can only be filled on subsequent candle opening at T (T_eval_close <= T_candle_open)
            eligible_orders_query = (
                db.query(Order)
                .join(OrderIntent, and_(Order.intent_id == OrderIntent.id, Order.owner_id == OrderIntent.owner_id, Order.runtime_id == OrderIntent.runtime_id))
                .join(RuntimeEvaluation, and_(OrderIntent.evaluation_id == RuntimeEvaluation.id, OrderIntent.owner_id == RuntimeEvaluation.owner_id, OrderIntent.runtime_id == RuntimeEvaluation.runtime_id))
                .join(RuntimeOrchestrationConfig, and_(RuntimeEvaluation.config_id == RuntimeOrchestrationConfig.id, RuntimeEvaluation.owner_id == RuntimeOrchestrationConfig.owner_id, RuntimeEvaluation.runtime_id == RuntimeOrchestrationConfig.runtime_id))
                .filter(
                    Order.owner_id == config.owner_id,
                    Order.runtime_id == config.runtime_id,
                    Order.account_id == account.id,
                    Order.status.in_([OrderStatus.CREATED.value, OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value]),
                    RuntimeOrchestrationConfig.execution_policy == "INTERNAL_PAPER",
                    RuntimeEvaluation.close_at <= candle_open_at,
                )
                .order_by(Order.order_sequence_number.asc(), Order.id.asc())
            )
            if db.bind and db.bind.dialect.name == "postgresql":
                eligible_orders_query = eligible_orders_query.with_for_update(of=Order)
            else:
                eligible_orders_query = eligible_orders_query.with_for_update()

            open_orders = eligible_orders_query.all()

            if open_orders:
                exec_candle_event = db.query(CompletedCandleEvent).filter(
                    CompletedCandleEvent.id == (evaluation.subject_candle_id or evaluation.reference_candle_id)
                ).first()
                if exec_candle_event:
                    inst_spec = self._resolve_instrument_spec(inst_spec_dict, snapshot, open_orders[0].instrument_id)
                    orders_payload = [
                        {
                            "id": o.id,
                            "side": o.side,
                            "order_type": o.order_type,
                            "quantity_units": o.quantity_units,
                            "filled_quantity_units": o.filled_quantity_units,
                            "limit_price_units": o.limit_price_units,
                            "accepted_at": o.created_at.isoformat(),
                            "order_sequence_number": o.order_sequence_number,
                            "intent_trigger_key": f"{o.runtime_id}:{o.order_sequence_number}",
                        }
                        for o in open_orders
                    ]
                    exec_summary = DeterministicFillEngine.calculate_order_fills(
                        open_orders=orders_payload,
                        instrument_spec=inst_spec,
                        candle_open_units=exec_candle_event.open_units,
                        candle_high_units=exec_candle_event.high_units,
                        candle_low_units=exec_candle_event.low_units,
                        candle_close_units=exec_candle_event.close_units,
                        candle_volume_units=exec_candle_event.volume_units,
                        candle_timestamp=exec_candle_event.close_at,
                    )
                    for fill in exec_summary.fills:
                        PaperService._apply_fill(db, active_runtime, fill, inst_spec)

        # 5. Approve each validated contract action under the account lock,
        # before inserting the immutable evaluation or any intent/order.
        evaluated_actions: List[Dict[str, Any]] = []
        inst_spec_for_actions = self._resolve_instrument_spec(inst_spec_dict, snapshot)
        if config.execution_policy == "INTERNAL_PAPER" and account is not None:
            from .paper_actions import plan_actions
            from .evidence import evaluation_evidence
            exec_c = db.query(CompletedCandleEvent).filter(
                CompletedCandleEvent.id == (evaluation.subject_candle_id or evaluation.reference_candle_id)
            ).one_or_none()
            evaluated_actions = plan_actions(db, active_runtime, account, evaluation,
                inst_spec_for_actions, action_snap, risk_snap, exec_c,
                json.loads(evaluation.audit_json).get("rule_results", {}))
            accepted = any(action["accepted"] for action in evaluated_actions)
            risk_rejected = any(action["risk_outcome"] == "REJECTED" for action in evaluated_actions)
            evaluation.action_outcome = "ACCEPTED_INTERNAL" if accepted else ("REJECTED" if risk_rejected else "NO_ACTION")
            evaluation.risk_outcome = "ACCEPTED" if accepted else ("REJECTED" if risk_rejected else "NOT_RUN")
            if accepted:
                evaluation.no_order_reason = None
            elif risk_rejected:
                first_rejected = next(action for action in evaluated_actions if action["risk_outcome"] == "REJECTED")
                evaluation.no_order_reason = first_rejected["reason"]
            else:
                evaluation.no_order_reason = evaluated_actions[0]["reason"] if evaluated_actions else "NO_ACTION"
            evaluation.risk_summary_json = evaluation_evidence(json.dumps({
                "outcome": evaluation.risk_outcome,
                "reason_codes": list(dict.fromkeys(action["reason"] for action in evaluated_actions)),
                "actions": [{"mapping_id": action["mapping_id"], "accepted": action["accepted"],
                    "risk_outcome": action["risk_outcome"], "reason_code": action["reason"]}
                    for action in evaluated_actions],
            }), risk=True)

        # 6. Check for idempotent existing evaluation first or conflict
        existing_eval = (
            db.query(RuntimeEvaluation)
            .filter(
                RuntimeEvaluation.owner_id == config.owner_id,
                RuntimeEvaluation.runtime_id == config.runtime_id,
                RuntimeEvaluation.evaluation_fingerprint == evaluation.evaluation_fingerprint,
            )
            .first()
        )

        if existing_eval:
            eval_to_return = existing_eval
        else:
            interval_conflict = (
                db.query(RuntimeEvaluation)
                .filter(
                    RuntimeEvaluation.owner_id == config.owner_id,
                    RuntimeEvaluation.runtime_id == config.runtime_id,
                    RuntimeEvaluation.timeframe == config.timeframe,
                    RuntimeEvaluation.close_at == boundary,
                )
                .first()
            )
            if interval_conflict:
                if interval_conflict.evaluation_fingerprint != evaluation.evaluation_fingerprint:
                    raise ConflictError(
                        f"Conflicting evaluation at boundary {boundary}: existing {interval_conflict.evaluation_fingerprint} != {evaluation.evaluation_fingerprint}"
                    )
                eval_to_return = interval_conflict
            else:
                try:
                    with db.begin_nested():
                        db.add(evaluation)
                        db.flush()
                    eval_to_return = evaluation
                except IntegrityError as ie:
                    orig_msg = str(ie.orig).lower() if ie.orig else str(ie).lower()
                    is_eval_conflict = (
                        "uq_orch_eval_fingerprint" in orig_msg
                        or "uq_orch_eval_interval" in orig_msg
                        or "runtime_evaluations.evaluation_fingerprint" in orig_msg
                        or "runtime_evaluations.close_at" in orig_msg
                        or "unique constraint failed: runtime_evaluations" in orig_msg
                    )
                    if not is_eval_conflict:
                        raise

                    winner = (
                        db.query(RuntimeEvaluation)
                        .filter(
                            RuntimeEvaluation.owner_id == config.owner_id,
                            RuntimeEvaluation.runtime_id == config.runtime_id,
                            RuntimeEvaluation.timeframe == config.timeframe,
                            RuntimeEvaluation.close_at == boundary,
                        )
                        .first()
                    )
                    if not winner:
                        raise
                    eval_to_return = winner

        # 8. Persist child entities referencing evaluation.id (ActionDecision, OrderIntent, RiskDecision, Order, Ledger)
        if config.execution_policy == "INTERNAL_PAPER" and account is not None:
            for act in evaluated_actions:
                mapping_id = act["mapping_id"]
                if act["accepted"]:
                    db.add(ActionDecision(
                        id=str(uuid.uuid4()),
                        owner_id=config.owner_id,
                        runtime_id=config.runtime_id,
                        evaluation_id=eval_to_return.id,
                        candle_timestamp=boundary,
                        action_mapping_id=mapping_id,
                        decision="EXECUTED",
                        reason_code="RULE_CONDITIONS_MET",
                        created_at=now,
                    ))
                    intent_id = str(uuid.uuid4())
                    intent_key = f"{config.runtime_id}:{boundary.isoformat()}:{mapping_id}"
                    intent = OrderIntent(
                        id=intent_id,
                        owner_id=config.owner_id,
                        runtime_id=config.runtime_id,
                        evaluation_id=eval_to_return.id,
                        action_mapping_id=mapping_id,
                        requested_instrument_id=inst_spec_for_actions.instrument_id,
                        resolved_instrument_id=inst_spec_for_actions.instrument_id,
                        intent_type=act["intent_type"],
                        reduce_only=act["intent_type"] in ("EXIT", "REDUCE"),
                        side=act["side"],
                        quantity_units=act["qty"],
                        order_type=act["order_type"],
                        limit_price_units=act["limit_price"],
                        time_in_force=act["time_in_force"],
                        source_candle_timestamp=boundary,
                        source_evaluation_fingerprint=eval_to_return.evaluation_fingerprint,
                        trigger_event_key=intent_key,
                        created_at=now,
                    )
                    db.add(intent)
                    db.flush()

                    db.add(RiskDecision(
                        id=str(uuid.uuid4()),
                        owner_id=config.owner_id,
                        intent_id=intent.id,
                        passed=True,
                        reason_code="RISK_OK",
                        message=act["risk_result"].message,
                        metrics_json=act["risk_result"].metrics,
                        created_at=now,
                    ))

                    seq = db.query(func.coalesce(func.max(Order.order_sequence_number), 0)).filter(Order.runtime_id == config.runtime_id).scalar() + 1
                    order = Order(
                        id=str(uuid.uuid4()),
                        owner_id=config.owner_id,
                        runtime_id=config.runtime_id,
                        intent_id=intent.id,
                        account_id=account.id,
                        order_sequence_number=seq,
                        instrument_id=inst_spec_for_actions.instrument_id,
                        side=act["side"],
                        order_type=act["order_type"],
                        quantity_units=act["qty"],
                        limit_price_units=act["limit_price"],
                        filled_quantity_units=0,
                        status=OrderStatus.ACCEPTED.value,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(order)
                    db.flush()

                    db.add(OrderEvent(
                        id=str(uuid.uuid4()),
                        order_id=order.id,
                        sequence_number=1,
                        previous_status="NONE",
                        new_status=OrderStatus.ACCEPTED.value,
                        actor=self.worker_id,
                        reason_code="ORCHESTRATION_ORDER_ACCEPTED",
                        metadata_json={"provenance": "INTERNAL_PAPER"},
                        created_at=now,
                    ))

                    if act["side"] == "BUY":
                        cost = act["cost"]
                        account.reserved_cash_units += cost
                        l_seq = db.query(func.coalesce(func.max(AccountLedgerEntry.sequence_number), 0)).filter(AccountLedgerEntry.account_id == account.id).scalar() + 1
                        db.add(AccountLedgerEntry(
                            id=str(uuid.uuid4()),
                            account_id=account.id,
                            owner_id=config.owner_id,
                            sequence_number=l_seq,
                            entry_type=LedgerEntryType.CASH_RESERVATION.value,
                            amount_units=cost,
                            balance_after_units=account.total_cash_units,
                            settled_cash_delta_units=0,
                            reserved_cash_delta_units=cost,
                            settled_cash_after_units=account.total_cash_units,
                            reserved_cash_after_units=account.reserved_cash_units,
                            order_id=order.id,
                            reason_code="BUY_ORDER_RESERVED",
                            idempotency_key=f"reserve:{order.id}:{l_seq}",
                            created_at=now,
                        ))
                        db.flush()
                else:
                    db.add(ActionDecision(
                        id=str(uuid.uuid4()),
                        owner_id=config.owner_id,
                        runtime_id=config.runtime_id,
                        evaluation_id=eval_to_return.id,
                        candle_timestamp=boundary,
                        action_mapping_id=mapping_id,
                        decision="IGNORED",
                        reason_code=act["reason"],
                        created_at=now,
                    ))

        # 8. Advance config checkpoint and release lease guarded by fencing generation and prior checkpoint
        if config.checkpoint_close_at is None:
            prior_chk_clause = RuntimeOrchestrationConfig.checkpoint_close_at.is_(None)
        else:
            prior_chk_clause = (RuntimeOrchestrationConfig.checkpoint_close_at == config.checkpoint_close_at)

        stmt = (
            update(RuntimeOrchestrationConfig)
            .where(
                RuntimeOrchestrationConfig.id == config.id,
                RuntimeOrchestrationConfig.owner_id == config.owner_id,
                RuntimeOrchestrationConfig.runtime_id == config.runtime_id,
                RuntimeOrchestrationConfig.fencing_generation == acquired_gen,
                RuntimeOrchestrationConfig.lease_owner == self.worker_id,
                prior_chk_clause,
            )
            .values(
                checkpoint_close_at=boundary,
                lease_owner=None,
                lease_expires_at=None,
                retry_count=0,
                next_attempt_at=now,
                last_reason_code="EVALUATION_FINALIZED",
                updated_at=now,
            )
        )
        res = db.execute(stmt)
        if res.rowcount == 0:
            db.rollback()
            raise StaleWorkerFencedError(f"Worker {self.worker_id} fenced out on config {config.id}")

        # 9. Update Account Watermark (if INTERNAL_PAPER)
        # Watermark is authoritatively derived from committed RuntimeEvaluation in database.

        # 10. Check if this evaluation reached end of replay bounds (replay_close_at is inclusive)
        if boundary >= config.replay_close_at and active_runtime.status == RuntimeStatus.RUNNING.value:
            if account is not None:
                # Expire any remaining open orders and release reserved cash
                remaining_open = db.query(Order).filter(
                    Order.runtime_id == config.runtime_id,
                    Order.owner_id == config.owner_id,
                    Order.status.in_([OrderStatus.CREATED.value, OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value]),
                ).all()
                for ro in remaining_open:
                    curr_st = OrderStatus(ro.status)
                    validate_order_transition(curr_st, OrderStatus.EXPIRED, actor=self.worker_id, reason_code="REPLAY_COMPLETED")
                    ro.status = OrderStatus.EXPIRED.value
                    if ro.side == "BUY":
                        rel_amt = PaperService._orchestration_order_reservation(db, ro)
                        if rel_amt is None:
                            raise ValueError("Paper execution order is missing evaluation provenance")
                        account.reserved_cash_units = max(0, account.reserved_cash_units - rel_amt)
                        l_seq = db.query(func.coalesce(func.max(AccountLedgerEntry.sequence_number), 0)).filter(AccountLedgerEntry.account_id == account.id).scalar() + 1
                        db.add(AccountLedgerEntry(
                            id=str(uuid.uuid4()),
                            account_id=account.id,
                            owner_id=config.owner_id,
                            sequence_number=l_seq,
                            entry_type=LedgerEntryType.RESERVATION_RELEASE.value,
                            amount_units=rel_amt,
                            balance_after_units=account.total_cash_units,
                            settled_cash_delta_units=0,
                            reserved_cash_delta_units=-rel_amt,
                            settled_cash_after_units=account.total_cash_units,
                            reserved_cash_after_units=account.reserved_cash_units,
                            order_id=ro.id,
                            reason_code="REPLAY_COMPLETED:ORDER_EXPIRED",
                            idempotency_key=f"expire_release:{ro.id}:{l_seq}",
                            created_at=now,
                        ))
                        db.flush()

            validate_runtime_transition(
                RuntimeStatus(active_runtime.status), RuntimeStatus.COMPLETED, actor=self.worker_id, reason_code="REPLAY_COMPLETED"
            )
            active_runtime.status = RuntimeStatus.COMPLETED.value
            active_runtime.version = active_runtime.version + 1
            active_runtime.updated_at = now

            seq = (
                db.query(func.coalesce(func.max(RuntimeEvent.sequence_number), 0))
                .filter(RuntimeEvent.runtime_id == active_runtime.id)
                .scalar()
                + 1
            )
            event = RuntimeEvent(
                runtime_id=active_runtime.id,
                sequence_number=seq,
                previous_status=RuntimeStatus.RUNNING.value,
                new_status=RuntimeStatus.COMPLETED.value,
                actor=self.worker_id,
                reason_code="REPLAY_COMPLETED",
                metadata_json={"final_checkpoint": boundary.isoformat()},
                created_at=now,
            )
            db.add(event)

        db.commit()
        db.refresh(eval_to_return)
        return eval_to_return

    def _complete_runtime(
        self,
        db: Session,
        runtime: StrategyRuntime,
        config: RuntimeOrchestrationConfig,
        acquired_gen: int,
        now: datetime.datetime,
    ) -> None:
        """Mark runtime as COMPLETED when checkpoint has reached replay_close_at."""
        # Level 1: Lock StrategyRuntime
        active_runtime = (
            db.query(StrategyRuntime)
            .filter(
                StrategyRuntime.id == config.runtime_id,
                StrategyRuntime.owner_id == config.owner_id,
            )
            .with_for_update()
            .first()
        )
        if not active_runtime or active_runtime.status != RuntimeStatus.RUNNING.value:
            self.release_lease(db, config.id, acquired_gen, reason_code="RUNTIME_ALREADY_NON_RUNNING")
            return

        # Level 3: Lock PaperAccount if INTERNAL_PAPER
        if active_runtime.account_id and config.execution_policy == "INTERNAL_PAPER":
            account = (
                db.query(PaperAccount)
                .filter(
                    PaperAccount.id == active_runtime.account_id,
                    PaperAccount.owner_id == config.owner_id,
                )
                .with_for_update()
                .first()
            )
            if account:
                remaining_open = db.query(Order).filter(
                    Order.runtime_id == config.runtime_id,
                    Order.owner_id == config.owner_id,
                    Order.status.in_([OrderStatus.CREATED.value, OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value]),
                ).all()
                for ro in remaining_open:
                    curr_st = OrderStatus(ro.status)
                    validate_order_transition(curr_st, OrderStatus.EXPIRED, actor=self.worker_id, reason_code="REPLAY_COMPLETED")
                    ro.status = OrderStatus.EXPIRED.value
                    if ro.side == "BUY":
                        rem_qty = ro.quantity_units - ro.filled_quantity_units
                        px = ro.limit_price_units or 0
                        fee = (rem_qty * px * 5) // 10000 + 2000
                        rel_amt = min((rem_qty * px) + fee, account.reserved_cash_units)
                        account.reserved_cash_units = max(0, account.reserved_cash_units - rel_amt)
                        l_seq = db.query(func.coalesce(func.max(AccountLedgerEntry.sequence_number), 0)).filter(AccountLedgerEntry.account_id == account.id).scalar() + 1
                        db.add(AccountLedgerEntry(
                            id=str(uuid.uuid4()),
                            account_id=account.id,
                            owner_id=config.owner_id,
                            sequence_number=l_seq,
                            entry_type=LedgerEntryType.RESERVATION_RELEASE.value,
                            amount_units=rel_amt,
                            balance_after_units=account.total_cash_units,
                            settled_cash_delta_units=0,
                            reserved_cash_delta_units=-rel_amt,
                            settled_cash_after_units=account.total_cash_units,
                            reserved_cash_after_units=account.reserved_cash_units,
                            order_id=ro.id,
                            reason_code="REPLAY_COMPLETED:ORDER_EXPIRED",
                            idempotency_key=f"expire_release:{ro.id}:{l_seq}",
                            created_at=now,
                        ))
                        db.flush()

        validate_runtime_transition(
            RuntimeStatus(active_runtime.status), RuntimeStatus.COMPLETED, actor=self.worker_id, reason_code="REPLAY_COMPLETED"
        )
        active_runtime.status = RuntimeStatus.COMPLETED.value
        active_runtime.version = active_runtime.version + 1
        active_runtime.updated_at = now

        seq = (
            db.query(func.coalesce(func.max(RuntimeEvent.sequence_number), 0))
            .filter(RuntimeEvent.runtime_id == active_runtime.id)
            .scalar()
            + 1
        )

        event = RuntimeEvent(
            runtime_id=active_runtime.id,
            sequence_number=seq,
            previous_status=RuntimeStatus.RUNNING.value,
            new_status=RuntimeStatus.COMPLETED.value,
            actor=self.worker_id,
            reason_code="REPLAY_COMPLETED",
            metadata_json={"replay_close_at": config.replay_close_at.isoformat()},
            created_at=now,
        )
        db.add(event)

        stmt = (
            update(RuntimeOrchestrationConfig)
            .where(
                RuntimeOrchestrationConfig.id == config.id,
                RuntimeOrchestrationConfig.fencing_generation == acquired_gen,
                RuntimeOrchestrationConfig.lease_owner == self.worker_id,
            )
            .values(
                lease_owner=None,
                lease_expires_at=None,
                last_reason_code="REPLAY_COMPLETED",
                updated_at=now,
            )
        )
        db.execute(stmt)
        db.commit()

    def run_once(self) -> int:
        """Execute a single processing pass across claimed runtimes. Returns number of evaluations processed."""
        processed_count = 0
        db = SessionLocal()
        try:
            while not self._stop_requested:
                claim = self.claim_next_candidate(db)
                if not claim:
                    break
                config_id, acquired_gen = claim
                eval_res = self.process_runtime_step(db, config_id, acquired_gen)
                if eval_res:
                    processed_count += 1
                if processed_count >= self.batch_size:
                    break
        finally:
            db.close()
        return processed_count

    def run(self, max_runs: Optional[int] = None) -> None:
        """Main operational worker execution loop."""
        self._running = True
        self._stop_requested = False

        # Set up signal handlers for graceful termination
        try:
            signal.signal(signal.SIGINT, self.request_stop)
            signal.signal(signal.SIGTERM, self.request_stop)
        except (ValueError, AttributeError):
            pass

        logger.info("Starting Strategy Evaluation Worker [%s] (batch_size=%d, lease_duration=%ds)",
                    self.worker_id, self.batch_size, self.lease_duration_seconds)

        runs = 0
        while self._running and not self._stop_requested:
            try:
                processed = self.run_once()
                runs += 1

                if max_runs is not None and runs >= max_runs:
                    logger.info("Worker reached max_runs (%d). Exiting loop.", max_runs)
                    break

                if processed == 0:
                    time.sleep(self.poll_interval_seconds)

            except Exception as e:
                logger.error("Unexpected worker loop exception: %s", e, exc_info=True)
                time.sleep(self.poll_interval_seconds)

        self._running = False
        logger.info("Strategy Evaluation Worker [%s] has stopped.", self.worker_id)
