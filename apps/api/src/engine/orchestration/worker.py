"""Strategy Orchestration Evaluation Worker.

Implements:
- Bounded batch claiming of RUNNING orchestration runtimes
- Atomic database lease acquisition with monotonic fencing generations
- Checkpoint advancement and atomic persistence of RuntimeEvaluation
- Stale worker fencing and expired lease recovery
- Strict transmission prohibition gating before every step
- Zero external broker transmission; purely fixture replay
"""
import datetime
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
from src.engine.paper.state_machine import (
    RuntimeStatus,
    validate_runtime_transition,
)
from src.models import (
    CompletedCandleEvent,
    RuntimeEvaluation,
    RuntimeEvent,
    RuntimeOrchestrationConfig,
    StrategyRuntime,
)
from src.services.orchestration_service import (
    ConflictError,
    OrchestrationService,
    PermissionDeniedError,
    ResourceNotFoundError,
)

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
                RuntimeOrchestrationConfig.execution_policy == "INTERNAL_MOCK_ONLY",
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
            .order_by(
                RuntimeOrchestrationConfig.next_attempt_at.asc().nullsfirst(),
                RuntimeOrchestrationConfig.runtime_id.asc(),
            )
            .limit(self.batch_size)
        )

        candidates = query.all()

        for cand_id, cand_runtime_id, cand_owner_id, cand_gen, _ in candidates:
            # 1. Lock and revalidate StrategyRuntime row with with_for_update()
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

            # 2. Atomic conditional update acquiring lease and advancing fencing generation
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
                    RuntimeOrchestrationConfig.execution_policy == "INTERNAL_MOCK_ONLY",
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

        # Lock StrategyRuntime row FIRST
        active_rt = (
            db.query(StrategyRuntime)
            .filter(
                StrategyRuntime.id == config.runtime_id,
                StrategyRuntime.owner_id == config.owner_id,
            )
            .with_for_update()
            .first()
        )

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
            if reason_code:
                values_dict["last_reason_code"] = reason_code[:64]
            if retry_delay_seconds is not None:
                values_dict["next_attempt_at"] = now + datetime.timedelta(seconds=retry_delay_seconds)

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
                    RuntimeOrchestrationConfig.execution_policy == "INTERNAL_MOCK_ONLY",
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

        # 1. Strict Transmission Gate check
        assert_orchestration_execution_is_internal_only(config)

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

        # 3. Determine next evaluation boundary
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

        # 7. Fenced Atomic Finalize: persist RuntimeEvaluation & advance checkpoint
        try:
            persisted_eval = self._fenced_finalize_step(
                db, config, runtime, evaluation, next_boundary, acquired_gen, now
            )
            return persisted_eval
        except StaleWorkerFencedError:
            logger.warning("Worker [%s] was fenced out during finalization of runtime '%s'.", self.worker_id, config.runtime_id)
            db.rollback()
            return None
        except Exception as e:
            logger.error("Finalization error for runtime '%s': %s", config.runtime_id, e, exc_info=True)
            db.rollback()
            self.release_lease(
                db, config_id, acquired_gen, reason_code="FINALIZATION_ERROR", retry_delay_seconds=10, increment_retry=True
            )
            return None

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
        """Atomically persist evaluation, advance checkpoint, and release lease guarded by fencing generation."""
        # Verify and lock active runtime with SELECT ... FOR UPDATE
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

        # Deterministic concurrency test hook invoked after row lock is held
        effective_hook = post_lock_hook or self.post_lock_hook
        if effective_hook is not None:
            effective_hook()

        # If config checkpoint was already advanced to or past boundary by a winning worker, do not re-advance
        curr_cfg = (
            db.query(RuntimeOrchestrationConfig)
            .filter(RuntimeOrchestrationConfig.id == config.id)
            .first()
        )
        if curr_cfg and curr_cfg.checkpoint_close_at is not None and curr_cfg.checkpoint_close_at >= boundary:
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

        # Guard prior checkpoint: ensure exactly-one checkpoint advance from expected prior
        if config.checkpoint_close_at is None:
            prior_chk_clause = RuntimeOrchestrationConfig.checkpoint_close_at.is_(None)
        else:
            prior_chk_clause = (RuntimeOrchestrationConfig.checkpoint_close_at == config.checkpoint_close_at)

        # Update config: advance checkpoint and clear lease guarded by fencing generation and prior checkpoint
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

        # Check for idempotent existing evaluation first
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
            logger.info(
                "Evaluation with fingerprint %s already exists. Returning winning record.",
                evaluation.evaluation_fingerprint,
            )
            eval_to_return = existing_eval
        else:
            # Check for conflict at same interval before attempting insert
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
                # Isolate insert in a savepoint to handle duplicate insert races without poisoning transaction
                try:
                    with db.begin_nested():
                        db.add(evaluation)
                        db.flush()
                    eval_to_return = evaluation
                except IntegrityError as ie:
                    # Unrelated IntegrityError is re-raised;
                    # only the named evaluation-identity uniqueness conflict is treated as an idempotent winner
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

                    if (
                        winner.owner_id != config.owner_id
                        or winner.runtime_id != config.runtime_id
                        or winner.config_id != config.id
                        or winner.timeframe != config.timeframe
                        or winner.close_at != boundary
                        or winner.snapshot_fingerprint != evaluation.snapshot_fingerprint
                        or winner.evaluation_fingerprint != evaluation.evaluation_fingerprint
                        or winner.required_candles_json != evaluation.required_candles_json
                    ):
                        raise ConflictError(
                            f"Conflicting evaluation at boundary {boundary}: existing {winner.evaluation_fingerprint} != {evaluation.evaluation_fingerprint}"
                        )
                    eval_to_return = winner

        # Check if this evaluation reached the end of the replay bounds (replay_close_at is inclusive)
        if boundary >= config.replay_close_at and active_runtime.status == RuntimeStatus.RUNNING.value:
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
