"""Guarded Provider Evaluation Worker (Phase 6).

Coordinates:
- Discovery of RUNNING provider runtimes with monotonic lease fencing
- Zero database locks held during provider HTTP requests
- Market data acquisition through Phase 5 adapter/normalizer
- Validated provenance binding and durable evaluation
- Optional handoff to SandboxOutboxWorker
- Strict prohibition of live broker transmission
"""
import datetime
import logging
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from src.database import SessionLocal
from src.engine.market_data.adapter import UpstoxMarketDataAdapter
from src.engine.market_data.contracts import (
    MarketDataCandle,
    MarketDataDisabledError,
    MarketDataError,
    TIMEFRAME_TO_SECONDS,
)
from src.engine.market_data.provenance import compute_market_data_fingerprint
from src.engine.orchestration.models import OrchestrationSnapshot, utc
from src.engine.provider_execution.contracts import (
    ProviderEvaluationResult,
    ProviderExecutionError,
    RuntimeLifecycleFencedError,
)
from src.engine.provider_execution.engine import ProviderExecutionEngine
from src.engine.sandbox.outbox_worker import SandboxOutboxWorker
from src.models import (
    ProviderInstrumentMapping,
    RuntimeOrchestrationConfig,
    StrategyRuntime,
)
from src.services.market_data_service import MarketDataService

logger = logging.getLogger("tradepro.provider_evaluation_worker")


class StaleWorkerFencedError(Exception):
    """Raised when an evaluation worker lease has expired or been superseded."""
    pass


class ProviderEvaluationWorker:
    """
    Application worker entry point for provider-driven runtime evaluation.
    Discovers eligible RUNNING runtimes, acquires candles through Phase 5 normalizer,
    performs durable evaluation, and hands work to the sandbox outbox worker.
    """

    def __init__(
        self,
        worker_id: Optional[str] = None,
        batch_size: int = 10,
        lease_duration_seconds: int = 30,
        poll_interval_seconds: float = 1.0,
        engine: Optional[ProviderExecutionEngine] = None,
        market_data_adapter: Optional[Any] = None,
        sandbox_outbox_worker: Optional[SandboxOutboxWorker] = None,
        clock: Optional[Callable[[], datetime.datetime]] = None,
        session_factory: Optional[Callable[[], Session]] = None,
    ):
        self.worker_id = worker_id or f"prov-eval-{uuid.uuid4().hex[:8]}"
        self.batch_size = max(1, min(batch_size, 50))
        self.lease_duration_seconds = max(5, min(lease_duration_seconds, 300))
        self.poll_interval_seconds = max(0.1, poll_interval_seconds)
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.engine = engine or ProviderExecutionEngine(clock=self.clock)
        self.market_data_adapter = market_data_adapter
        self.sandbox_outbox_worker = sandbox_outbox_worker
        self.session_factory = session_factory
        self._stop_requested = False

    def request_stop(self, *args) -> None:
        self._stop_requested = True

    def claim_runtime_lease(
        self, db: Session, runtime_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Atomically claims the evaluation lease on a single RUNNING provider runtime.
        Returns claim metadata dictionary if successfully acquired, None otherwise.
        """
        now = utc(self.clock())

        rec = (
            db.query(StrategyRuntime, RuntimeOrchestrationConfig)
            .join(
                RuntimeOrchestrationConfig,
                and_(
                    RuntimeOrchestrationConfig.runtime_id == StrategyRuntime.id,
                    RuntimeOrchestrationConfig.owner_id == StrategyRuntime.owner_id,
                ),
            )
            .filter(
                StrategyRuntime.id == runtime_id,
                StrategyRuntime.status == "RUNNING",
                StrategyRuntime.trading_mode == "BROKER_SANDBOX",
                RuntimeOrchestrationConfig.execution_policy == "EXTERNAL_SANDBOX_DISPATCH",
                RuntimeOrchestrationConfig.source_type.in_(["PROVIDER_SANDBOX", "PROVIDER_UPSTOX_V3"]),
                or_(
                    RuntimeOrchestrationConfig.lease_owner.is_(None),
                    RuntimeOrchestrationConfig.lease_expires_at <= now,
                ),
            )
            .with_for_update()
            .first()
        )

        if not rec:
            return None

        runtime, orch_cfg = rec

        import json
        snap_dict = json.loads(orch_cfg.snapshot_json)
        snap_model = OrchestrationSnapshot(**snap_dict)
        frozen_mapping = snap_model.provider_mapping
        if not frozen_mapping or not frozen_mapping.mapping_id:
            logger.warning("Runtime %s snapshot lacks provider mapping.", runtime.id)
            return None

        mapping = db.get(ProviderInstrumentMapping, frozen_mapping.mapping_id)
        if not mapping or mapping.verification_status != "VERIFIED":
            logger.warning("Runtime %s mapping missing or unverified.", runtime.id)
            return None

        new_gen = (orch_cfg.fencing_generation or 0) + 1
        lease_until = now + datetime.timedelta(seconds=self.lease_duration_seconds)

        orch_cfg.lease_owner = self.worker_id
        orch_cfg.lease_expires_at = lease_until
        orch_cfg.fencing_generation = new_gen
        cfg_created = utc(orch_cfg.created_at) if orch_cfg.created_at else now
        orch_cfg.updated_at = max(now, cfg_created)
        db.flush()

        claim = {
            "runtime_id": runtime.id,
            "owner_id": runtime.owner_id,
            "config_id": orch_cfg.id,
            "fencing_generation": new_gen,
            "timeframe": runtime.timeframe,
            "instrument_token": mapping.provider_instrument_token,
            "mapping_id": mapping.id,
            "last_processed_ts": runtime.last_processed_candle_timestamp,
        }
        return claim

    def release_runtime_lease(
        self, db: Session, claim: Dict[str, Any]
    ) -> None:
        """Releases the worker lease unconditionally."""
        now = utc(self.clock())
        orch_cfg = db.get(RuntimeOrchestrationConfig, claim["config_id"])
        if orch_cfg and orch_cfg.lease_owner == self.worker_id:
            orch_cfg.lease_owner = None
            orch_cfg.lease_expires_at = None
            cfg_created = utc(orch_cfg.created_at) if orch_cfg.created_at else now
            orch_cfg.updated_at = max(now, cfg_created)
            db.flush()

    def acquire_provider_candles(
        self, claim: Dict[str, Any]
    ) -> Tuple[MarketDataCandle, List[MarketDataCandle]]:
        """
        Acquires completed provider candles through Phase 5 adapter/normalizer.
        IMPORTANT: Executed with ZERO database locks or transactions held.
        """
        resp = MarketDataService.fetch_and_normalize_candles(
            user_id=claim["owner_id"],
            instrument_key=claim["instrument_token"],
            timeframe=claim["timeframe"],
            mode="intraday",
            adapter=self.market_data_adapter,
            clock=self.clock,
        )

        now = utc(self.clock())
        interval_seconds = TIMEFRAME_TO_SECONDS.get(claim["timeframe"], 300)
        interval_delta = datetime.timedelta(seconds=interval_seconds)

        # 1. Preserve and validate full acquisition series provenance if provided
        if resp.provenance is not None and resp.candles:
            full_fp = compute_market_data_fingerprint(resp.candles)
            if full_fp.lower() != resp.provenance.content_fingerprint.lower():
                raise ProviderExecutionError(
                    f"Full provider acquisition series fingerprint mismatch: "
                    f"computed '{full_fp}' does not match supplied '{resp.provenance.content_fingerprint}'."
                )

        # 2. Filter to completed closed candles not in the future, sorted by timestamp ascending
        closed_candles = sorted(
            [c for c in resp.candles if c.is_closed and (c.timestamp + interval_delta) <= now],
            key=lambda c: c.timestamp,
        )

        if not closed_candles:
            raise ProviderExecutionError(
                f"No closed completed candles available for {claim['instrument_token']} in timeframe {claim['timeframe']}."
            )

        # 3. Determine next target candle: treat last_processed_candle_timestamp as previous CLOSE boundary.
        # The next eligible candle must OPEN exactly at that timestamp.
        last_processed = claim.get("last_processed_ts")
        if last_processed is not None:
            expected_open = utc(last_processed)
            matching_candle = next((c for c in closed_candles if c.timestamp == expected_open), None)

            if matching_candle is not None:
                target_candle = matching_candle
            else:
                # If a candle beyond expected_open exists in closed series, a required interval is absent -> fail closed!
                if any(c.timestamp > expected_open for c in closed_candles):
                    raise ProviderExecutionError(
                        f"Required candle at '{expected_open.isoformat()}' is missing from provider series; gap detected."
                    )
                # If all closed candles are prior to expected_open, no new completed candle has arrived yet.
                raise ProviderExecutionError(
                    f"No new closed completed candles available for {claim['instrument_token']} beyond '{expected_open.isoformat()}'."
                )
        else:
            # Initial backlog: start from earliest closed completed candle in order
            target_candle = closed_candles[0]

        # 4. History candles are all closed candles strictly before target candle
        history_candles = [c for c in closed_candles if c.timestamp < target_candle.timestamp]

        # 5. Bind provenance to the exact acquisition series used for evaluation
        prefix_series = list(history_candles) + [target_candle]
        if resp.provenance is not None:
            if len(prefix_series) == len(resp.candles):
                scoped_prov = resp.provenance
            else:
                prefix_fp = compute_market_data_fingerprint(prefix_series)
                scoped_prov = resp.provenance.model_copy(
                    update={
                        "candle_count": len(prefix_series),
                        "content_fingerprint": prefix_fp,
                    }
                )
            candle_with_prov = target_candle.model_copy(update={"provenance": scoped_prov})
        else:
            candle_with_prov = target_candle

        return candle_with_prov, history_candles

    def process_runtime(
        self, db: Session, runtime_id: str
    ) -> Optional[ProviderEvaluationResult]:
        """
        Executes a complete guarded provider evaluation cycle for a specific runtime.
        1. Claims lease under brief transaction.
        2. Acquires provider candles outside database transaction (zero locks held).
        3. Invokes durable engine evaluation under transaction.
        4. Releases lease and commits.
        5. Optionally dispatches sandbox outbox.
        """
        now = utc(self.clock())

        # Step 1: Claim lease
        claim = self.claim_runtime_lease(db, runtime_id)
        if not claim:
            return None
        db.commit()

        # Step 2: Acquire provider candles (Zero DB locks held!)
        try:
            target_candle, history = self.acquire_provider_candles(claim)
        except Exception as exc:
            logger.error("Failed to acquire provider candles for runtime %s: %s", runtime_id, exc)
            self.release_runtime_lease(db, claim)
            db.commit()
            raise

        # Step 3: Re-verify lease & execute durable evaluation
        try:
            orch_cfg = db.get(RuntimeOrchestrationConfig, claim["config_id"])
            if not orch_cfg or orch_cfg.fencing_generation != claim["fencing_generation"]:
                raise StaleWorkerFencedError(
                    f"Worker {self.worker_id} superseded on runtime {runtime_id}."
                )

            eval_res = self.engine.evaluate_runtime_candle(
                db=db,
                runtime_id=claim["runtime_id"],
                owner_id=claim["owner_id"],
                candle=target_candle,
                candle_history=history,
            )

            # Step 4: Release lease and commit
            self.release_runtime_lease(db, claim)
            db.commit()

            # Step 5: Optional outbox dispatch hand-off
            if self.sandbox_outbox_worker and eval_res.outbox_ids:
                try:
                    self.sandbox_outbox_worker.process_batch(db)
                    db.commit()
                except Exception as outbox_exc:
                    logger.warning("Sandbox outbox worker handoff failed: %s", outbox_exc)

            return eval_res

        except Exception:
            db.rollback()
            try:
                self.release_runtime_lease(db, claim)
                db.commit()
            except Exception:
                db.rollback()
            raise

    def run_cycle(self, db: Session) -> List[ProviderEvaluationResult]:
        """
        Discovers up to batch_size eligible activated provider runtimes and processes them.
        """
        now = utc(self.clock())
        candidates = (
            db.query(StrategyRuntime.id)
            .join(
                RuntimeOrchestrationConfig,
                and_(
                    RuntimeOrchestrationConfig.runtime_id == StrategyRuntime.id,
                    RuntimeOrchestrationConfig.owner_id == StrategyRuntime.owner_id,
                ),
            )
            .filter(
                StrategyRuntime.status == "RUNNING",
                StrategyRuntime.trading_mode == "BROKER_SANDBOX",
                RuntimeOrchestrationConfig.execution_policy == "EXTERNAL_SANDBOX_DISPATCH",
                RuntimeOrchestrationConfig.source_type.in_(["PROVIDER_SANDBOX", "PROVIDER_UPSTOX_V3"]),
                or_(
                    RuntimeOrchestrationConfig.lease_owner.is_(None),
                    RuntimeOrchestrationConfig.lease_expires_at <= now,
                ),
            )
            .limit(self.batch_size)
            .all()
        )

        results = []
        for (cand_id,) in candidates:
            if self._stop_requested:
                break
            try:
                res = self.process_runtime(db, cand_id)
                if res:
                    results.append(res)
            except Exception as e:
                logger.error("Error processing candidate %s: %s", cand_id, e)

        return results

    def run(self, max_runs: Optional[int] = None) -> int:
        """
        Main worker execution loop with disposable sessions, polling, and graceful shutdown.
        Runs bounded iterations (if max_runs provided) or continuously.
        Returns total number of processed evaluation results.
        """
        import signal
        import time

        self._stop_requested = False
        runs = 0
        total_evaluations = 0

        # Register signal handlers if in main thread
        try:
            signal.signal(signal.SIGINT, self.request_stop)
            signal.signal(signal.SIGTERM, self.request_stop)
        except (ValueError, AttributeError):
            pass

        logger.info(
            "Starting Provider Evaluation Worker [%s] (batch_size=%d, lease_duration=%ds, poll_interval=%.2fs)",
            self.worker_id,
            self.batch_size,
            self.lease_duration_seconds,
            self.poll_interval_seconds,
        )

        session_maker = self.session_factory or SessionLocal

        while not self._stop_requested:
            db = session_maker()
            try:
                results = self.run_cycle(db)
                total_evaluations += len(results)
            except Exception as e:
                logger.error(
                    "Unexpected error in provider evaluation worker cycle [%s]: %s",
                    self.worker_id,
                    e,
                    exc_info=True,
                )
            finally:
                db.close()

            runs += 1
            if max_runs is not None and runs >= max_runs:
                logger.info("Provider evaluation worker reached max_runs (%d). Exiting loop.", max_runs)
                break

            if not self._stop_requested:
                time.sleep(self.poll_interval_seconds)

        logger.info("Provider Evaluation Worker [%s] stopped.", self.worker_id)
        return total_evaluations
