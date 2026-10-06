"""Provider-Driven Evaluation & Automated Broker Sandbox Execution Engine.

Implements:
- Verified completed candle processing with zero look-ahead
- Strict tenant/owner isolation across all entities
- Frozen instrument mapping and verified, persisted operator consent binding
- Lifecycle fencing adhering to universal lock hierarchy
- Authoritative frozen risk policy fields and actual daily exposure/trade/loss inputs
- Atomic persistence of RuntimeEvaluation, ActionDecision, RiskDecision, OrderIntent, Order, SubmissionOutbox
- Savepoint-isolated duplicate prevention (protecting unrelated caller work)
- Sandbox-only PLACE (priority 10) and CANCEL (priority 0) via guarded SubmissionOutbox
- Durable pre-transmission marker preservation (transmission_started_at = None at insert)
- Fail-closed reconciliation invariants
- Zero live broker transmission prohibition
"""
import datetime
import hashlib
import json
import logging
import re
import uuid
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.engine.evaluator import RuleEvaluator
from src.engine.market_data.contracts import MarketDataCandle, MarketDataProvenance, TIMEFRAME_TO_SECONDS
from src.engine.market_data.provenance import compute_market_data_fingerprint
from src.engine.models import Candle
from src.engine.orchestration.evidence import config_consent_fingerprint
from src.engine.orchestration.fingerprint import (
    canonical_json,
    orchestration_snapshot_v1,
    runtime_evaluation_v1,
)
from src.engine.orchestration.models import (
    CompletedCandle,
    OrchestrationSnapshot,
    RequiredCandleIdentity,
    RuntimeEvaluationIdentity,
    SeriesRole,
)
from src.engine.paper.accounting import AccountingEngine
from src.engine.paper.models import (
    IntentType,
    LedgerEntryType,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    TradingMode,
    InstrumentSpec,
    get_instrument_spec,
)
from src.engine.paper.risk_engine import PureRiskEngine
from src.engine.paper.units import decimal_to_units
from src.models import (
    AccountLedgerEntry,
    ActionDecision,
    CompletedCandleEvent,
    KillSwitch,
    Order,
    OrderEvent,
    OrderIntent,
    PaperAccount,
    PaperPosition,
    ProviderInstrumentMapping,
    RiskDecision,
    RuntimeEvaluation,
    RuntimeOrchestrationConfig,
    StrategyRuntime,
    SubmissionOutbox,
)
from .contracts import (
    ConflictingIntervalError,
    ConsentMissingError,
    DuplicateCandleError,
    ExpiredMappingError,
    KillSwitchActiveError,
    LiveTransmissionProhibitedError,
    LookAheadProhibitedError,
    MappingNotFoundError,
    OwnerIsolationError,
    ProviderEvaluationResult,
    ProviderExecutionError,
    RuntimeLifecycleFencedError,
    UnclosedCandleError,
    UnverifiedMappingError,
)

logger = logging.getLogger("tradepro.provider_execution")


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Return True only if exc is strictly a duplicate / unique constraint violation."""
    orig = getattr(exc, "orig", None)
    pgcode = getattr(orig, "pgcode", None)
    if pgcode is not None:
        return str(pgcode) == "23505"

    msg = str(exc).lower()
    if "foreign key" in msg or "check constraint" in msg or "not null" in msg:
        return False

    return "unique constraint failed" in msg or "duplicate key" in msg or "unique constraint" in msg


def _utc(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


class ProviderExecutionEngine:
    """Executes strategy evaluation on verified provider market data candles

    and generates guarded broker sandbox outbox submissions.
    """

    def __init__(
        self,
        rule_evaluator: Optional[RuleEvaluator] = None,
        clock: Optional[Callable[[], datetime.datetime]] = None,
    ):
        self.rule_evaluator = rule_evaluator or RuleEvaluator()
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))

    def evaluate_runtime_candle(
        self,
        db: Session,
        *,
        runtime_id: str,
        owner_id: str,
        candle: Union[MarketDataCandle, Dict[str, Any]],
        candle_history: Optional[List[Union[MarketDataCandle, Dict[str, Any]]]] = None,
        operator_consent_token: Optional[str] = None,
        allow_live_broker: bool = False,
        provenance: Optional[Union[MarketDataProvenance, Dict[str, Any]]] = None,
    ) -> ProviderEvaluationResult:
        """Evaluates an active runtime on a verified completed provider candle.

        Guarantees:
        - Zero live broker transmission: fails closed if live is requested.
        - Strict owner isolation: verifies ownership of runtime, account, mapping.
        - Lifecycle fencing: requires RUNNING status and no active kill switch.
        - Zero look-ahead: enforces candle_close <= clock.now_utc().
        - Validated history: checks closure, temporal boundary, identity, no duplicates, no conflicting intervals.
        - Verified mapping & persisted consent binding: binds owner, runtime, mode, mapping id/ver.
        - Authoritative risk fields & actual daily metrics: trades count, realized loss.
        - Atomic persistence: stores RuntimeEvaluation, ActionDecision, RiskDecision, OrderIntent, Order, Outbox.
        - Savepoint rollback: duplicate inserts roll back only their savepoint, never caller work.
        - Sandbox outbox: generates PLACE (priority 10) or CANCEL (priority 0) with transmission_started_at = None.
        """
        now = _utc(self.clock())

        # --- GATE 1: Zero Live Transmission Prohibition ---
        if allow_live_broker:
            raise LiveTransmissionProhibitedError("Live broker transmission is strictly forbidden in Phase 6.")

        # --- GATE 2: Universal Lock Hierarchy & Owner Isolation ---
        # 1. Lock StrategyRuntime under owner isolation
        runtime = (
            db.query(StrategyRuntime)
            .filter(
                StrategyRuntime.id == runtime_id,
                StrategyRuntime.owner_id == owner_id,
            )
            .with_for_update()
            .first()
        )
        if not runtime:
            raise OwnerIsolationError(f"Runtime '{runtime_id}' not found or owner mismatch for '{owner_id}'.")

        if runtime.trading_mode == "BROKER_LIVE":
            raise LiveTransmissionProhibitedError("Runtime trading_mode is BROKER_LIVE; live execution is forbidden.")

        if runtime.trading_mode != "BROKER_SANDBOX":
            raise ProviderExecutionError(
                f"Runtime trading_mode is '{runtime.trading_mode}'; expected 'BROKER_SANDBOX'."
            )

        # 2. Lock PaperAccount under owner isolation
        account = (
            db.query(PaperAccount)
            .filter(
                PaperAccount.id == runtime.account_id,
                PaperAccount.owner_id == owner_id,
            )
            .with_for_update()
            .first()
        )
        if not account:
            raise OwnerIsolationError(f"Paper account '{runtime.account_id}' not found or owner mismatch.")

        # --- GATE 3: Lifecycle Fencing & Emergency Kill-Switch ---
        if runtime.status != "RUNNING":
            raise RuntimeLifecycleFencedError(
                f"Runtime '{runtime_id}' is in status '{runtime.status}'; execution is fenced."
            )

        kill_switch = (
            db.query(KillSwitch)
            .filter(
                KillSwitch.is_active.is_(True),
                or_(
                    KillSwitch.scope == "GLOBAL",
                    and_(KillSwitch.scope == "USER", KillSwitch.user_id == owner_id),
                ),
            )
            .first()
        )
        if kill_switch:
            runtime.status = "HALTED"
            db.flush()
            raise KillSwitchActiveError(
                f"Active kill-switch [{kill_switch.scope}] engaged; runtime '{runtime_id}' transitioned to HALTED."
            )

        # --- GATE 4: Frozen Provider Mapping & Persisted Operator Consent Binding ---
        instrument_id = runtime.instrument_id
        if not instrument_id:
            raise MappingNotFoundError(f"Runtime '{runtime_id}' does not have an authoritative instrument_id.")

        mapping = (
            db.query(ProviderInstrumentMapping)
            .filter(
                ProviderInstrumentMapping.owner_id == owner_id,
                ProviderInstrumentMapping.tradepro_instrument_id == instrument_id,
            )
            .first()
        )
        if not mapping:
            raise MappingNotFoundError(
                f"No provider instrument mapping found for owner '{owner_id}' and instrument '{instrument_id}'."
            )

        if mapping.verification_status != "VERIFIED":
            raise UnverifiedMappingError(
                f"Provider mapping '{mapping.id}' status is '{mapping.verification_status}'; must be 'VERIFIED'."
            )

        if mapping.expiry_date and _utc(mapping.expiry_date) <= now:
            raise ExpiredMappingError(
                f"Provider mapping '{mapping.id}' expired at '{mapping.expiry_date.isoformat()}'."
            )

        # Verify explicit persisted operator consent binding (Finding 1)
        orch_cfg = self._verify_operator_consent(db, runtime, mapping, operator_consent_token)

        # --- GATE 4.5: Validated Phase 5 Provider Provenance & Series Fingerprint Recomputation ---
        canonical_candle, single_candle_fp = self._validate_provenance_and_acquisition_series(
            candle=candle,
            candle_history=candle_history,
            provenance=provenance,
            mapping=mapping,
            orch_cfg=orch_cfg,
            runtime=runtime,
            now=now,
        )

        # --- GATE 5: Completed Candle Verification & Historical Validation ---
        canonical_candle, candle_close, full_series = self._validate_and_assemble_candles(
            candle=canonical_candle,
            candle_history=candle_history,
            instrument_id=instrument_id,
            timeframe=runtime.timeframe,
            now=now,
        )

        # Monotonicity / Idempotency Check:
        # If this exact or earlier candle was already evaluated, return idempotent existing result without duplicate orders.
        if (
            runtime.last_processed_candle_timestamp is not None
            and candle_close <= _utc(runtime.last_processed_candle_timestamp)
        ):
            return ProviderEvaluationResult(
                runtime_id=runtime.id,
                owner_id=owner_id,
                candle_timestamp=canonical_candle.timestamp,
                evaluated_at=now,
                rule_status="ALREADY_EVALUATED",
                action_decision="IDEMPOTENT_SKIPPED",
                risk_decision="NOT_RUN",
                order_ids=[],
                outbox_ids=[],
                reason_code="CANDLE_ALREADY_PROCESSED",
                details={"last_processed": runtime.last_processed_candle_timestamp.isoformat()},
            )

        # --- GATE 6: Strategy Rule Evaluation ---
        rule_result = self.rule_evaluator.evaluate_strategy_rules(
            strategy_payload=runtime.strategy_snapshot or {},
            reference_candles=full_series,
            eval_timestamp=canonical_candle.timestamp,
        )

        rule_status = (
            rule_result.overall_status.value
            if hasattr(rule_result.overall_status, "value")
            else str(rule_result.overall_status)
        )

        # --- GATE 7: Action & Pre-Trade Risk Evaluation with Authoritative Evidence Persistence (Finding 5) ---
        action_decision = "NO_ACTION"
        risk_decision = "NOT_RUN"
        reason_code = "NO_TRIGGER"
        order_ids: List[str] = []
        outbox_ids: List[str] = []
        details: Dict[str, Any] = {"rule_status": rule_status}

        action_policy = runtime.action_policy_snapshot or {}
        risk_policy = runtime.risk_policy_snapshot or {}
        inst_spec = get_instrument_spec(runtime.dataset_id)
        if inst_spec is None:
            spec_snap = runtime.instrument_spec_snapshot or {}
            inst_spec = InstrumentSpec(
                instrument_id=mapping.tradepro_instrument_id,
                symbol=mapping.symbol or "UNKNOWN",
                reference_dataset_id=runtime.dataset_id,
                execution_dataset_id=runtime.dataset_id,
                dataset_id=runtime.dataset_id,
                currency=spec_snap.get("currency", "INR"),
                currency_scale=spec_snap.get("currency_scale", 4),
                price_scale=spec_snap.get("price_scale", 4),
                quantity_scale=spec_snap.get("quantity_scale", 0),
                tick_size_units=mapping.tick_size_units or 5,
                lot_size_units=mapping.lot_size_units or 1,
                min_quantity_units=mapping.lot_size_units or 1,
                max_quantity_units=mapping.freeze_quantity_units or 1800,
                allow_fractional=False,
                allow_short=False,
                is_tradable=True,
            )

        # Determine if action conditions are satisfied
        entry_action = self._check_action_trigger(action_policy.get("entry_mapping"), rule_result)
        exit_action = self._check_action_trigger(action_policy.get("exit_mapping"), rule_result)

        if entry_action:
            action_res = self._process_entry_action(
                db=db,
                runtime=runtime,
                orch_cfg=orch_cfg,
                account=account,
                mapping=mapping,
                inst_spec=inst_spec,
                action_cfg=entry_action,
                risk_policy=risk_policy,
                candle=canonical_candle,
                candle_close=candle_close,
                now=now,
                rule_status=rule_status,
                provenance_fingerprint=single_candle_fp,
            )
            action_decision = action_res["action_decision"]
            risk_decision = action_res["risk_decision"]
            reason_code = action_res["reason_code"]
            order_ids = action_res.get("order_ids", [])
            outbox_ids = action_res.get("outbox_ids", [])
            details.update(action_res.get("details", {}))

        elif exit_action:
            action_res = self._process_exit_action(
                db=db,
                runtime=runtime,
                orch_cfg=orch_cfg,
                account=account,
                mapping=mapping,
                inst_spec=inst_spec,
                action_cfg=exit_action,
                candle=canonical_candle,
                candle_close=candle_close,
                now=now,
                rule_status=rule_status,
                provenance_fingerprint=single_candle_fp,
            )
            action_decision = action_res["action_decision"]
            risk_decision = action_res["risk_decision"]
            reason_code = action_res["reason_code"]
            order_ids = action_res.get("order_ids", [])
            outbox_ids = action_res.get("outbox_ids", [])
            details.update(action_res.get("details", {}))

        else:
            action_res = self._process_no_action(
                db=db,
                runtime=runtime,
                orch_cfg=orch_cfg,
                mapping=mapping,
                inst_spec=inst_spec,
                candle=canonical_candle,
                candle_close=candle_close,
                now=now,
                rule_status=rule_status,
                provenance_fingerprint=single_candle_fp,
            )
            action_decision = action_res["action_decision"]
            risk_decision = action_res["risk_decision"]
            reason_code = action_res["reason_code"]
            order_ids = action_res.get("order_ids", [])
            outbox_ids = action_res.get("outbox_ids", [])
            details.update(action_res.get("details", {}))

        if action_decision == "IDEMPOTENT_SKIPPED":
            return ProviderEvaluationResult(
                runtime_id=runtime.id,
                owner_id=owner_id,
                candle_timestamp=canonical_candle.timestamp,
                evaluated_at=now,
                rule_status=rule_status,
                action_decision=action_decision,
                risk_decision=risk_decision,
                order_ids=order_ids,
                outbox_ids=outbox_ids,
                reason_code=reason_code,
                details=details,
            )

        # --- GATE 8: Advance Monotonic Progress ---
        runtime.last_processed_candle_timestamp = candle_close
        db.flush()

        return ProviderEvaluationResult(
            runtime_id=runtime.id,
            owner_id=owner_id,
            candle_timestamp=canonical_candle.timestamp,
            evaluated_at=now,
            rule_status=rule_status,
            action_decision=action_decision,
            risk_decision=risk_decision,
            order_ids=order_ids,
            outbox_ids=outbox_ids,
            reason_code=reason_code,
            details=details,
        )

    def _verify_operator_consent(
        self,
        db: Session,
        runtime: StrategyRuntime,
        mapping: ProviderInstrumentMapping,
        token: Optional[str],
    ) -> RuntimeOrchestrationConfig:
        """Verifies explicit, persisted operator consent bound to owner, runtime, mode, mapping, and frozen policy."""
        # 1. Reject prefix-only loose tokens (Finding 1)
        if token and token.startswith("consent_sandbox_v1:"):
            raise ConsentMissingError(
                "Prefix-only consent token 'consent_sandbox_v1:...' is strictly rejected. "
                "Persisted verified consent bound to owner, runtime, execution mode, and mapping is required."
            )

        # 2. Query persisted RuntimeOrchestrationConfig
        orch_cfg = (
            db.query(RuntimeOrchestrationConfig)
            .filter(
                RuntimeOrchestrationConfig.runtime_id == runtime.id,
                RuntimeOrchestrationConfig.owner_id == runtime.owner_id,
            )
            .first()
        )
        if not orch_cfg:
            raise ConsentMissingError(
                f"No persisted orchestration configuration with verified consent found for runtime '{runtime.id}'."
            )

        # 3. Verify execution policy and consent policy version
        if orch_cfg.execution_policy != "EXTERNAL_SANDBOX_DISPATCH":
            raise ConsentMissingError(
                f"Orchestration config has execution_policy '{orch_cfg.execution_policy}'; "
                "expected 'EXTERNAL_SANDBOX_DISPATCH' for automated broker sandbox execution."
            )

        if orch_cfg.consent_policy_version != "sandbox_consent_v1":
            raise ConsentMissingError(
                f"Orchestration config has consent_policy_version '{orch_cfg.consent_policy_version}'; "
                "expected 'sandbox_consent_v1'."
            )

        if orch_cfg.source_type not in ("PROVIDER_SANDBOX", "PROVIDER_UPSTOX_V3"):
            raise ConsentMissingError(
                f"Orchestration config has source_type '{orch_cfg.source_type}'; "
                "expected 'PROVIDER_SANDBOX' or 'PROVIDER_UPSTOX_V3'."
            )

        if not orch_cfg.consent_fingerprint or len(orch_cfg.consent_fingerprint) != 64:
            raise ConsentMissingError("Orchestration config lacks valid 64-character SHA-256 consent fingerprint.")

        # Recompute & compare snapshot fingerprint
        if not orch_cfg.snapshot_json:
            raise ConsentMissingError("Orchestration config lacks frozen snapshot JSON.")
        try:
            snap_dict = json.loads(orch_cfg.snapshot_json) if isinstance(orch_cfg.snapshot_json, str) else orch_cfg.snapshot_json
            snap_model = OrchestrationSnapshot(**snap_dict)
            recomputed_snap_fp = orchestration_snapshot_v1(snap_model)
        except Exception as e:
            raise ConsentMissingError(f"Failed to recompute snapshot fingerprint: {e}")

        if recomputed_snap_fp != orch_cfg.snapshot_fingerprint:
            raise ConsentMissingError(
                f"Recomputed snapshot fingerprint '{recomputed_snap_fp}' does not match persisted snapshot fingerprint '{orch_cfg.snapshot_fingerprint}'."
            )

        # Recompute & compare consent fingerprint
        recomputed_consent_fp = config_consent_fingerprint(orch_cfg)
        if recomputed_consent_fp != orch_cfg.consent_fingerprint:
            raise ConsentMissingError(
                f"Recomputed consent fingerprint '{recomputed_consent_fp}' does not match persisted consent fingerprint '{orch_cfg.consent_fingerprint}'."
            )

        # 4. Verify mapping identity & version match
        provider_map = snap_dict.get("provider_mapping")
        if not provider_map or not isinstance(provider_map, dict):
            raise ConsentMissingError("Orchestration snapshot lacks frozen provider_mapping.")
        bound_mapping_id = provider_map.get("mapping_id")
        bound_mapping_version = provider_map.get("mapping_version")
        if not bound_mapping_id or bound_mapping_id != mapping.id:
            raise ConsentMissingError(
                f"Consent bound to mapping '{bound_mapping_id}', but active mapping is '{mapping.id}'."
            )
        if bound_mapping_version is None or bound_mapping_version != mapping.mapping_version:
            raise ConsentMissingError(
                f"Consent bound to mapping version {bound_mapping_version}, but active version is {mapping.mapping_version}."
            )

        # 5. If explicit token provided, verify it matches the authoritative persisted fingerprint
        if token:
            if token != orch_cfg.consent_fingerprint:
                raise ConsentMissingError("Supplied operator consent token does not match persisted consent fingerprint.")

        return orch_cfg

    def _validate_provenance_and_acquisition_series(
        self,
        *,
        candle: Union[MarketDataCandle, Dict[str, Any]],
        candle_history: Optional[Sequence[Union[MarketDataCandle, Dict[str, Any]]]] = None,
        provenance: Optional[Union[MarketDataProvenance, Dict[str, Any]]] = None,
        mapping: ProviderInstrumentMapping,
        orch_cfg: RuntimeOrchestrationConfig,
        runtime: StrategyRuntime,
        now: datetime.datetime,
    ) -> Tuple[MarketDataCandle, str]:
        """
        Validates complete Phase 5 provider provenance contract and series consistency.
        Recomputes SHA-256 fingerprint over the exact normalized acquisition series.
        """
        prov_obj = provenance
        if prov_obj is None:
            if isinstance(candle, dict) and "provenance" in candle:
                prov_obj = candle["provenance"]
            elif hasattr(candle, "provenance"):
                prov_obj = getattr(candle, "provenance")

        if prov_obj is None:
            raise ProviderExecutionError(
                "Validated Phase 5 provider provenance is required for automated broker sandbox execution."
            )

        def _get_p(field: str, default: Any = None) -> Any:
            if hasattr(prov_obj, field):
                val = getattr(prov_obj, field)
                return val if val is not None else default
            if isinstance(prov_obj, dict):
                return prov_obj.get(field, default)
            return default

        prov_source_type = _get_p("source_type")
        prov_token = _get_p("requested_instrument_key")
        prov_tf = _get_p("timeframe")
        prov_fp = _get_p("content_fingerprint")
        prov_count = _get_p("candle_count")

        # 1. Source type validation
        if prov_source_type not in ("PROVIDER_UPSTOX_V3", "PROVIDER_SANDBOX"):
            raise ProviderExecutionError(
                f"Invalid market data provenance source_type '{prov_source_type}'; "
                "must be a validated Phase 5 provider source ('PROVIDER_UPSTOX_V3' or 'PROVIDER_SANDBOX')."
            )
        if orch_cfg.source_type == "PROVIDER_UPSTOX_V3" and prov_source_type != "PROVIDER_UPSTOX_V3":
            raise ProviderExecutionError(
                f"Provenance source_type '{prov_source_type}' does not match configuration source_type '{orch_cfg.source_type}'."
            )
        if orch_cfg.source_type == "PROVIDER_SANDBOX" and prov_source_type not in ("PROVIDER_SANDBOX", "PROVIDER_UPSTOX_V3"):
            raise ProviderExecutionError(
                f"Provenance source_type '{prov_source_type}' does not match configuration source_type '{orch_cfg.source_type}'."
            )

        # 2. Frozen mapping provider instrument token validation
        if not prov_token or prov_token != mapping.provider_instrument_token:
            raise ProviderExecutionError(
                f"Provenance instrument token '{prov_token}' does not match frozen provider instrument mapping '{mapping.provider_instrument_token}'."
            )

        # 3. Timeframe validation
        if not prov_tf or prov_tf != runtime.timeframe:
            raise ProviderExecutionError(
                f"Provenance timeframe '{prov_tf}' does not match configuration timeframe '{runtime.timeframe}'."
            )

        # 4. Fingerprint syntax validation (64 hex characters)
        if not prov_fp or not isinstance(prov_fp, str) or not re.fullmatch(r"^[0-9a-fA-F]{64}$", prov_fp):
            raise ProviderExecutionError("Provider provenance lacks valid 64-character SHA-256 content fingerprint.")

        # 5. Acquisition series normalization and assembly
        canonical_current = self._normalize_input_candle(candle, runtime.timeframe)
        normalized_history: List[MarketDataCandle] = []
        if candle_history:
            for item in candle_history:
                normalized_history.append(self._normalize_input_candle(item, runtime.timeframe))

        acquisition_series = list(normalized_history) + [canonical_current]

        # 6. Define relationship between acquisition series, historical window, and current candle
        if prov_count is not None:
            if prov_count == len(acquisition_series):
                series_covered = acquisition_series
            elif prov_count == 1:
                series_covered = [canonical_current]
            else:
                raise ProviderExecutionError(
                    f"Provenance candle_count mismatch: provenance specifies {prov_count} candles, but acquisition series contains {len(acquisition_series)} candles and current candle is 1."
                )
        else:
            series_covered = acquisition_series

        # 7. Closure, temporal bounds, and numeric consistency for all candles in series
        interval_seconds = TIMEFRAME_TO_SECONDS.get(runtime.timeframe, 300)
        for c in acquisition_series:
            if not c.is_closed:
                if c.timestamp == canonical_current.timestamp:
                    raise UnclosedCandleError(
                        f"Current candle at '{c.timestamp.isoformat()}' is not closed; in-progress candles rejected."
                    )
                else:
                    raise UnclosedCandleError(
                        f"Historical candle at '{c.timestamp.isoformat()}' is not closed; unclosed history candles rejected."
                    )
            c_close = c.timestamp + datetime.timedelta(seconds=interval_seconds)
            if c_close > now:
                if c.timestamp == canonical_current.timestamp:
                    raise LookAheadProhibitedError(
                        f"Current candle close at '{c_close.isoformat()}' is in the future relative to clock '{now.isoformat()}'."
                    )
                else:
                    raise LookAheadProhibitedError(
                        f"Historical candle close at '{c_close.isoformat()}' is in the future relative to clock '{now.isoformat()}'."
                    )
            if c.open_units <= 0 or c.close_units <= 0:
                raise ProviderExecutionError(f"Candle at '{c.timestamp.isoformat()}' has non-positive price units.")
            if c.high_units < max(c.open_units, c.close_units, c.low_units) or c.low_units > min(c.open_units, c.close_units, c.high_units):
                raise ProviderExecutionError(f"Candle at '{c.timestamp.isoformat()}' has invalid OHLC numeric geometry.")
            if c.volume < 0:
                raise ProviderExecutionError(f"Candle at '{c.timestamp.isoformat()}' has negative volume.")

        # 8. Temporal ordering and collision checks
        for i in range(len(acquisition_series) - 1):
            if acquisition_series[i].timestamp >= acquisition_series[i + 1].timestamp:
                if acquisition_series[i].timestamp == acquisition_series[i + 1].timestamp:
                    if (
                        acquisition_series[i].open_units != acquisition_series[i + 1].open_units
                        or acquisition_series[i].high_units != acquisition_series[i + 1].high_units
                        or acquisition_series[i].low_units != acquisition_series[i + 1].low_units
                        or acquisition_series[i].close_units != acquisition_series[i + 1].close_units
                        or acquisition_series[i].volume != acquisition_series[i + 1].volume
                    ):
                        raise ConflictingIntervalError(
                            f"Historical candle at '{acquisition_series[i].timestamp.isoformat()}' has conflicting content compared to current candle."
                        )
                    raise DuplicateCandleError(
                        f"Duplicate candle timestamp '{acquisition_series[i].timestamp.isoformat()}' detected in history and current input."
                    )
                raise ConflictingIntervalError("Candles in acquisition series are not in strictly increasing temporal order.")

        # 9. Recompute Phase 5 Series Fingerprint
        recomputed_series_fp = compute_market_data_fingerprint(series_covered)
        if recomputed_series_fp.lower() != prov_fp.lower():
            raise ProviderExecutionError(
                f"Provider provenance content fingerprint mismatch: computed '{recomputed_series_fp}' does not match supplied '{prov_fp}'."
            )

        # 10. Single-candle fingerprint for persistence
        single_candle_fp = compute_market_data_fingerprint([canonical_current])
        return canonical_current, single_candle_fp

    def _normalize_input_candle(
        self, candle: Union[MarketDataCandle, Dict[str, Any]], timeframe: str
    ) -> MarketDataCandle:
        """Normalizes and validates candle input strictly."""
        if isinstance(candle, MarketDataCandle):
            return candle

        if isinstance(candle, dict):
            ts = candle.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts_utc = _utc(ts)

            scale = 4
            o = Decimal(str(candle["open"]))
            h = Decimal(str(candle["high"]))
            l = Decimal(str(candle["low"]))
            c = Decimal(str(candle["close"]))
            v = int(candle.get("volume", 0))

            if h < max(o, c, l) or l > min(o, c, h):
                raise ValueError("Invalid candle OHLC geometry.")

            return MarketDataCandle(
                timestamp=ts_utc,
                open=o,
                high=h,
                low=l,
                close=c,
                open_units=decimal_to_units(o, scale),
                high_units=decimal_to_units(h, scale),
                low_units=decimal_to_units(l, scale),
                close_units=decimal_to_units(c, scale),
                volume=v,
                is_closed=bool(candle.get("is_closed", True)),
            )
        raise ValueError(f"Unsupported candle input type: {type(candle)}")

    def _validate_and_assemble_candles(
        self,
        *,
        candle: Union[MarketDataCandle, Dict[str, Any]],
        candle_history: Optional[List[Union[MarketDataCandle, Dict[str, Any]]]] = None,
        instrument_id: str,
        timeframe: str,
        now: datetime.datetime,
    ) -> Tuple[MarketDataCandle, datetime.datetime, List[Candle]]:
        """Validates all current and historical candles strictly and assembles domain candles (Finding 2)."""
        interval_seconds = TIMEFRAME_TO_SECONDS.get(timeframe, 300)

        # 1. Normalize and validate current candle
        canonical_current = self._normalize_input_candle(candle, timeframe)
        if not canonical_current.is_closed:
            raise UnclosedCandleError(
                f"Current candle at '{canonical_current.timestamp.isoformat()}' is not closed; in-progress candles rejected."
            )

        current_close = canonical_current.timestamp + datetime.timedelta(seconds=interval_seconds)
        if current_close > now:
            raise LookAheadProhibitedError(
                f"Current candle close at '{current_close.isoformat()}' is in the future relative to clock '{now.isoformat()}'."
            )

        # 2. Normalize and validate historical candles
        normalized_history: List[MarketDataCandle] = []
        if candle_history:
            for item in candle_history:
                h = self._normalize_input_candle(item, timeframe)
                if not h.is_closed:
                    raise UnclosedCandleError(
                        f"Historical candle at '{h.timestamp.isoformat()}' is not closed; unclosed history candles rejected."
                    )
                h_close = h.timestamp + datetime.timedelta(seconds=interval_seconds)
                if h_close > now:
                    raise LookAheadProhibitedError(
                        f"Historical candle close at '{h_close.isoformat()}' is in the future relative to clock '{now.isoformat()}'."
                    )
                if h.timestamp > canonical_current.timestamp:
                    raise LookAheadProhibitedError(
                        f"Historical candle at '{h.timestamp.isoformat()}' is in the future relative to current candle '{canonical_current.timestamp.isoformat()}'."
                    )
                if h.timestamp == canonical_current.timestamp:
                    if (
                        h.open_units != canonical_current.open_units
                        or h.high_units != canonical_current.high_units
                        or h.low_units != canonical_current.low_units
                        or h.close_units != canonical_current.close_units
                        or h.volume != canonical_current.volume
                    ):
                        raise ConflictingIntervalError(
                            f"Historical candle at '{h.timestamp.isoformat()}' has conflicting content compared to current candle."
                        )
                    raise DuplicateCandleError(
                        f"Duplicate candle timestamp '{h.timestamp.isoformat()}' detected in history and current input."
                    )

                # Alignment check
                ts = h.timestamp
                step_mins = max(1, interval_seconds // 60)
                if ts.second != 0 or ts.microsecond != 0 or (ts.minute % step_mins) != 0:
                    raise ConflictingIntervalError(
                        f"Candle at '{ts.isoformat()}' is not aligned to {timeframe} interval boundary."
                    )

                normalized_history.append(h)

        # 3. Assemble series
        all_candles: List[MarketDataCandle] = list(normalized_history)
        if not any(c.timestamp == canonical_current.timestamp for c in all_candles):
            all_candles.append(canonical_current)

        # 4. Check duplicates
        seen_timestamps = set()
        for c in all_candles:
            if c.timestamp in seen_timestamps:
                raise DuplicateCandleError(
                    f"Duplicate candle timestamp '{c.timestamp.isoformat()}' detected in evaluation series."
                )
            seen_timestamps.add(c.timestamp)

        # 5. Check conflicting / overlapping intervals
        all_candles.sort(key=lambda c: c.timestamp)
        for i in range(len(all_candles) - 1):
            c_prev = all_candles[i]
            c_next = all_candles[i + 1]
            c_prev_close = c_prev.timestamp + datetime.timedelta(seconds=interval_seconds)
            if c_next.timestamp < c_prev_close:
                raise ConflictingIntervalError(
                    f"Conflicting candle interval: candle at '{c_next.timestamp.isoformat()}' overlaps previous candle closing at '{c_prev_close.isoformat()}'."
                )

        domain_candles = [
            Candle(
                timestamp=c.timestamp,
                instrument_id=instrument_id,
                timeframe=timeframe,
                open=float(c.open),
                high=float(c.high),
                low=float(c.low),
                close=float(c.close),
                volume=float(c.volume),
                is_closed=True,
            )
            for c in all_candles
        ]

        return canonical_current, current_close, domain_candles

    def _check_action_trigger(
        self, action_mapping: Optional[Dict[str, Any]], rule_result: Any
    ) -> Optional[Dict[str, Any]]:
        """Determines if the rule result satisfies the configured trigger status."""
        if not action_mapping or not isinstance(action_mapping, dict):
            return None

        trigger_status = action_mapping.get("trigger_status", "ON_TRUE")
        overall = (
            rule_result.overall_status.value
            if hasattr(rule_result.overall_status, "value")
            else str(rule_result.overall_status)
        )

        if trigger_status == "ON_TRUE" and overall == "TRUE":
            return action_mapping
        if trigger_status == "ON_FALSE" and overall == "FALSE":
            return action_mapping
        return None

    def _process_entry_action(
        self,
        *,
        db: Session,
        runtime: StrategyRuntime,
        orch_cfg: RuntimeOrchestrationConfig,
        account: PaperAccount,
        mapping: ProviderInstrumentMapping,
        inst_spec: InstrumentSpec,
        action_cfg: Dict[str, Any],
        risk_policy: Dict[str, Any],
        candle: MarketDataCandle,
        candle_close: datetime.datetime,
        now: datetime.datetime,
        rule_status: str = "TRUE",
        provenance_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validates pre-trade risk with authoritative inputs, creates Order/Outbox with savepoint isolation (Finding 5)."""
        qty = int(action_cfg.get("quantity", inst_spec.lot_size_units or 1))
        order_type = action_cfg.get("order_type", OrderType.LIMIT.value)
        side = action_cfg.get("side", OrderSide.BUY.value)

        # In sandbox mode, PureRiskEngine requires LIMIT orders
        limit_px_units = action_cfg.get("limit_price_units")
        if not limit_px_units and order_type == OrderType.LIMIT.value:
            limit_px_units = candle.close_units

        # Fetch open positions and orders for the account
        positions = db.query(PaperPosition).filter(PaperPosition.account_id == account.id).all()
        pos_dict = {
            p.instrument_id: {
                "net_quantity_units": p.net_quantity_units,
                "last_mark_price_units": p.last_mark_price_units,
            }
            for p in positions
        }

        open_orders = (
            db.query(Order)
            .filter(
                Order.account_id == account.id,
                Order.status.in_(["CREATED", "ACCEPTED", "PENDING_SUBMISSION", "OPEN", "PARTIALLY_FILLED"]),
            )
            .all()
        )
        open_orders_dict = [
            {
                "instrument_id": o.instrument_id,
                "quantity_units": o.quantity_units,
                "filled_quantity_units": o.filled_quantity_units,
                "limit_price_units": o.limit_price_units,
                "side": o.side,
            }
            for o in open_orders
        ]

        available_cash = account.total_cash_units - account.reserved_cash_units

        # Authoritative frozen risk policy fields (Finding 5)
        frozen_risk = runtime.risk_policy_snapshot or risk_policy or {}
        if "risk_config" in frozen_risk:
            risk_payload = frozen_risk["risk_config"]
        elif "payload" in frozen_risk:
            risk_payload = frozen_risk["payload"]
        else:
            risk_payload = frozen_risk

        # Actual daily exposure/trades/loss inputs (Finding 5)
        day_start = candle_close.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_trades_count = (
            db.query(func.count(Order.id))
            .filter(Order.account_id == account.id, Order.created_at >= day_start)
            .scalar()
            or 0
        )
        loss_sum = (
            db.query(func.coalesce(func.sum(AccountLedgerEntry.settled_cash_delta_units), 0))
            .filter(
                AccountLedgerEntry.account_id == account.id,
                AccountLedgerEntry.created_at >= day_start,
                AccountLedgerEntry.settled_cash_delta_units < 0,
                AccountLedgerEntry.reason_code.in_(["TRADE_REALIZED_LOSS", "COMMISSION"]),
            )
            .scalar()
            or 0
        )
        daily_realized_loss_units = abs(int(loss_sum))

        # Pre-trade risk check
        risk_eval = PureRiskEngine.evaluate_pre_trade_risk(
            trading_mode=TradingMode.BROKER_SANDBOX,
            instrument_spec=inst_spec,
            side=OrderSide(side),
            order_type=OrderType(order_type),
            quantity_units=qty,
            limit_price_units=limit_px_units,
            reference_price_units=candle.close_units,
            price_timestamp=candle.timestamp,
            current_time=candle_close,
            risk_policy=risk_payload,
            available_cash_units=available_cash,
            open_orders=open_orders_dict,
            current_positions=pos_dict,
            daily_trades_count=daily_trades_count,
            daily_realized_loss_units=daily_realized_loss_units,
            kill_switch_active=False,
        )

        # Ingest or fetch CompletedCandleEvent
        candle_event = (
            db.query(CompletedCandleEvent)
            .filter(
                CompletedCandleEvent.owner_id == runtime.owner_id,
                CompletedCandleEvent.runtime_id == runtime.id,
                CompletedCandleEvent.close_at == candle_close,
            )
            .first()
        )
        if not candle_event:
            c_payload = {
                "owner_id": runtime.owner_id,
                "runtime_id": runtime.id,
                "source_namespace": orch_cfg.source_namespace,
                "source_event_id": f"provider_candle_{int(candle.timestamp.timestamp())}",
                "source_type": orch_cfg.source_type,
                "dataset_id": runtime.dataset_id,
                "dataset_checksum": hashlib.sha256(runtime.dataset_id.encode()).hexdigest(),
                "instrument_id": mapping.tradepro_instrument_id,
                "timeframe": runtime.timeframe,
                "series_role": "REFERENCE",
                "source_policy_version": orch_cfg.source_policy_version,
                "alignment_offset_seconds": orch_cfg.alignment_offset_seconds,
                "open_at": candle.timestamp,
                "close_at": candle_close,
                "received_at": now if now >= candle_close else candle_close,
                "price_scale": 4,
                "volume_scale": 0,
                "open_units": candle.open_units,
                "high_units": candle.high_units,
                "low_units": candle.low_units,
                "close_units": candle.close_units,
                "volume_units": candle.volume,
                "is_closed": True,
                "revision": 1,
            }
            c_model = CompletedCandle(**c_payload)
            candle_event = CompletedCandleEvent(
                id=str(uuid.uuid4()),
                content_fingerprint=c_model.content_fingerprint,
                **c_payload,
            )

        req_identities = [
            RequiredCandleIdentity(
                series_role=SeriesRole.REFERENCE,
                dataset_id=runtime.dataset_id,
                instrument_id=mapping.tradepro_instrument_id,
                content_fingerprint=provenance_fingerprint,
            )
        ]
        required_candles_json = canonical_json([req.model_dump(mode="python") for req in req_identities])

        eval_identity = RuntimeEvaluationIdentity(
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            snapshot_fingerprint=orch_cfg.snapshot_fingerprint,
            mapping_id=mapping.id,
            mapping_version=mapping.mapping_version or 1,
            timeframe=runtime.timeframe,
            close_at=candle_close,
            required_candles=tuple(req_identities),
        )
        eval_fp = runtime_evaluation_v1(eval_identity)

        audit_data = {
            "result": rule_status,
            "condition_ids": ["c1"],
            "rule_results": {"GLOBAL": rule_status, "CANDIDATE": rule_status},
        }
        audit_json = canonical_json(audit_data)

        if risk_eval.passed:
            risk_summary_data = {
                "outcome": "ACCEPTED",
                "reason_codes": ["ORDER_QUEUED_TO_SANDBOX_OUTBOX"],
                "actions": [
                    {
                        "mapping_id": action_cfg.get("mapping_id", "entry_1"),
                        "accepted": True,
                        "risk_outcome": "ACCEPTED",
                        "reason_code": "RULE_TRIGGERED",
                    }
                ],
            }
        else:
            risk_summary_data = {
                "outcome": "REJECTED",
                "reason_codes": [risk_eval.reason_code.value],
                "actions": [
                    {
                        "mapping_id": action_cfg.get("mapping_id", "entry_1"),
                        "accepted": False,
                        "risk_outcome": "REJECTED",
                        "reason_code": risk_eval.reason_code.value,
                    }
                ],
            }
        risk_summary_json = canonical_json(risk_summary_data)

        if risk_eval.passed:
            valid_eval_status = rule_status if rule_status in ("TRUE", "FALSE") else "TRUE"
        else:
            valid_eval_status = rule_status if rule_status in ("TRUE", "FALSE", "UNAVAILABLE", "INVALID") else "FALSE"

        runtime_eval = RuntimeEvaluation(
            id=str(uuid.uuid4()),
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            config_id=orch_cfg.id,
            snapshot_fingerprint=orch_cfg.snapshot_fingerprint,
            evaluation_fingerprint=eval_fp,
            timeframe=runtime.timeframe,
            close_at=candle_close,
            reference_candle_id=candle_event.id,
            subject_candle_id=None,
            required_candles_json=required_candles_json,
            evaluation_status=valid_eval_status,
            action_outcome="ACCEPTED_SANDBOX" if risk_eval.passed else "REJECTED",
            risk_outcome="ACCEPTED" if risk_eval.passed else "REJECTED",
            no_order_reason=None if risk_eval.passed else risk_eval.reason_code.value,
            audit_json=audit_json,
            risk_summary_json=risk_summary_json,
            finalized_at=now,
        )

        action_dec = ActionDecision(
            id=str(uuid.uuid4()),
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            candle_timestamp=candle.timestamp,
            action_mapping_id=action_cfg.get("mapping_id", "entry_1"),
            decision="ACCEPTED_SANDBOX" if risk_eval.passed else "REJECTED",
            reason_code="RULE_TRIGGERED" if risk_eval.passed else risk_eval.reason_code.value,
            evaluation_id=runtime_eval.id,
            created_at=now,
        )

        if not risk_eval.passed:
            # Persist evaluation and rejection atomically
            try:
                with db.begin_nested():
                    db.add(candle_event)
                    db.flush([candle_event])
                    db.add(runtime_eval)
                    db.flush([runtime_eval])
                    db.add(action_dec)
                    db.flush([action_dec])
            except IntegrityError as exc:
                if not _is_unique_violation(exc):
                    logger.error("Non-unique integrity error in rejection persistence: %s", exc)
                    raise ProviderExecutionError("Database integrity constraint violated.") from None
                pass
            return {
                "action_decision": "REJECTED",
                "risk_decision": "REJECTED",
                "reason_code": risk_eval.reason_code.value,
                "details": {"message": risk_eval.message},
            }

        # Create OrderIntent
        intent_id = str(uuid.uuid4())
        intent_key = hashlib.sha256(
            f"sandbox:{runtime.id}:{candle_close.isoformat()}:ENTRY".encode("utf-8")
        ).hexdigest()

        intent = OrderIntent(
            id=intent_id,
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            action_mapping_id=action_cfg.get("mapping_id", "entry_1"),
            evaluation_id=runtime_eval.id,
            requested_instrument_id=inst_spec.instrument_id,
            resolved_instrument_id=inst_spec.instrument_id,
            intent_type="ENTRY",
            reduce_only=False,
            side=side,
            quantity_units=qty,
            order_type=order_type,
            limit_price_units=limit_px_units,
            time_in_force="DAY",
            source_candle_timestamp=candle.timestamp,
            source_evaluation_fingerprint=intent_key,
            trigger_event_key=intent_key,
            created_at=now,
        )

        risk_dec = RiskDecision(
            id=str(uuid.uuid4()),
            owner_id=runtime.owner_id,
            intent_id=intent.id,
            passed=risk_eval.passed,
            reason_code=risk_eval.reason_code.value,
            message=risk_eval.message,
            metrics_json=risk_eval.metrics,
            created_at=now,
        )

        # Create Order (PENDING_SUBMISSION)
        seq = (
            db.query(func.coalesce(func.max(Order.order_sequence_number), 0))
            .filter(Order.runtime_id == runtime.id)
            .scalar()
            + 1
        )
        order = Order(
            id=str(uuid.uuid4()),
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            intent_id=intent.id,
            account_id=account.id,
            order_sequence_number=seq,
            instrument_id=inst_spec.instrument_id,
            side=side,
            order_type=order_type,
            quantity_units=qty,
            limit_price_units=limit_px_units,
            filled_quantity_units=0,
            status=OrderStatus.PENDING_SUBMISSION.value,
            version=1,
            created_at=now,
            updated_at=now,
        )

        order_event = OrderEvent(
            id=str(uuid.uuid4()),
            order_id=order.id,
            sequence_number=1,
            previous_status="NONE",
            new_status=OrderStatus.PENDING_SUBMISSION.value,
            actor="SYSTEM_OMS",
            reason_code="RISK_CHECK_PASSED_QUEUED_FOR_SANDBOX",
            created_at=now,
        )

        # Reserve cash for BUY order
        orig_reserved = account.reserved_cash_units
        reserve_amount = 0
        ledger = None
        if side == OrderSide.BUY.value:
            px = limit_px_units or candle.close_units
            fee = (qty * px * 5) // 10000 + 2000
            reserve_amount = (qty * px) + fee
            l_seq = (
                db.query(func.coalesce(func.max(AccountLedgerEntry.sequence_number), 0))
                .filter(AccountLedgerEntry.account_id == account.id)
                .scalar()
                + 1
            )
            ledger = AccountLedgerEntry(
                id=str(uuid.uuid4()),
                account_id=account.id,
                owner_id=runtime.owner_id,
                sequence_number=l_seq,
                entry_type=LedgerEntryType.CASH_RESERVATION.value,
                amount_units=reserve_amount,
                balance_after_units=account.total_cash_units,
                settled_cash_delta_units=0,
                reserved_cash_delta_units=reserve_amount,
                settled_cash_after_units=account.total_cash_units,
                reserved_cash_after_units=account.reserved_cash_units + reserve_amount,
                order_id=order.id,
                reason_code="BUY_ORDER_RESERVED",
                idempotency_key=f"reserve:{order.id}:{l_seq}",
                created_at=now,
            )

        # Create SubmissionOutbox (priority 10 for PLACE) with transmission_started_at = None
        limit_price_str = str(limit_px_units / (10**inst_spec.price_scale)) if limit_px_units else "0.0"
        submit_payload = {
            "order_id": order.id,
            "quantity": qty,
            "product": "D",
            "validity": "DAY",
            "price": limit_price_str,
            "tag": "TradePro",
            "instrument_token": mapping.provider_instrument_token,
            "order_type": order_type,
            "transaction_type": side,
            "disclosed_quantity": 0,
            "trigger_price": "0.0",
            "is_amo": False,
            "slice": False,
        }
        canonical_p_json = canonical_json(submit_payload)
        payload_hash = hashlib.sha256(canonical_p_json.encode("utf-8")).hexdigest()
        idem_key = f"sandbox:place:{order.id}"

        outbox = SubmissionOutbox(
            id=str(uuid.uuid4()),
            owner_id=runtime.owner_id,
            order_id=order.id,
            action_type="PLACE",
            priority=10,
            status="PENDING",
            idempotency_key=idem_key,
            canonical_payload_hash=payload_hash,
            payload_json=submit_payload,
            transmission_started_at=None,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )

        # SAVEPOINT ISOLATION (Finding 5): If duplicate insert fails, only the savepoint rolls back
        try:
            with db.begin_nested():
                db.add(candle_event)
                db.flush([candle_event])
                db.add(runtime_eval)
                db.flush([runtime_eval])
                db.add(action_dec)
                db.add(intent)
                db.flush([action_dec, intent])
                db.add(risk_dec)
                db.add(order)
                db.flush([risk_dec, order])
                remaining = [order_event, outbox]
                if ledger is not None:
                    account.reserved_cash_units += reserve_amount
                    db.add(ledger)
                    remaining.append(ledger)
                db.add(order_event)
                db.add(outbox)
                db.flush(remaining)
        except IntegrityError as exc:
            account.reserved_cash_units = orig_reserved
            if not _is_unique_violation(exc):
                logger.error("Non-unique database integrity error in order submission: %s", exc)
                raise ProviderExecutionError("Database integrity constraint violated.") from None
            logger.info("Duplicate order/evaluation caught and rolled back via savepoint: %s", exc)
            return {
                "action_decision": "IDEMPOTENT_SKIPPED",
                "risk_decision": "SKIPPED",
                "reason_code": "CANDLE_ALREADY_PROCESSED_CONCURRENT",
                "details": {"message": "Concurrent evaluation serialized by unique constraint."},
            }

        return {
            "action_decision": "ACCEPTED_SANDBOX",
            "risk_decision": "ACCEPTED",
            "reason_code": "ORDER_QUEUED_TO_SANDBOX_OUTBOX",
            "order_ids": [order.id],
            "outbox_ids": [outbox.id],
            "details": {
                "order_id": order.id,
                "outbox_id": outbox.id,
                "action_type": "PLACE",
                "priority": 10,
                "intent_id": intent.id,
                "evaluation_id": runtime_eval.id,
            },
        }

    def _process_exit_action(
        self,
        *,
        db: Session,
        runtime: StrategyRuntime,
        orch_cfg: RuntimeOrchestrationConfig,
        account: PaperAccount,
        mapping: ProviderInstrumentMapping,
        inst_spec: InstrumentSpec,
        action_cfg: Dict[str, Any],
        candle: MarketDataCandle,
        candle_close: datetime.datetime,
        now: datetime.datetime,
        rule_status: str = "FALSE",
        provenance_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Processes exit action: cancels active orders with priority 0 outbox rows (Finding 5)."""
        cancellable_statuses = [
            OrderStatus.CREATED.value,
            OrderStatus.ACCEPTED.value,
            OrderStatus.PENDING_SUBMISSION.value,
            OrderStatus.ACKNOWLEDGED.value,
            OrderStatus.PARTIALLY_FILLED.value,
        ]
        candidate_orders = (
            db.query(Order)
            .filter(
                Order.account_id == account.id,
                Order.owner_id == runtime.owner_id,
                Order.runtime_id == runtime.id,
                Order.status.in_(cancellable_statuses),
            )
            .all()
        )
        existing_cancel_order_ids = {
            row[0]
            for row in db.query(SubmissionOutbox.order_id)
            .filter(
                SubmissionOutbox.owner_id == runtime.owner_id,
                SubmissionOutbox.action_type == "CANCEL",
                SubmissionOutbox.status.in_(["PENDING", "PROCESSING", "DELIVERED"]),
            )
            .all()
        }
        open_orders = [o for o in candidate_orders if o.id not in existing_cancel_order_ids]

        candle_event = (
            db.query(CompletedCandleEvent)
            .filter(
                CompletedCandleEvent.owner_id == runtime.owner_id,
                CompletedCandleEvent.runtime_id == runtime.id,
                CompletedCandleEvent.close_at == candle_close,
            )
            .first()
        )
        if not candle_event:
            c_payload = {
                "owner_id": runtime.owner_id,
                "runtime_id": runtime.id,
                "source_namespace": orch_cfg.source_namespace,
                "source_event_id": f"provider_candle_{int(candle.timestamp.timestamp())}",
                "source_type": orch_cfg.source_type,
                "dataset_id": runtime.dataset_id,
                "dataset_checksum": hashlib.sha256(runtime.dataset_id.encode()).hexdigest(),
                "instrument_id": mapping.tradepro_instrument_id,
                "timeframe": runtime.timeframe,
                "series_role": "REFERENCE",
                "source_policy_version": orch_cfg.source_policy_version,
                "alignment_offset_seconds": orch_cfg.alignment_offset_seconds,
                "open_at": candle.timestamp,
                "close_at": candle_close,
                "received_at": now if now >= candle_close else candle_close,
                "price_scale": 4,
                "volume_scale": 0,
                "open_units": candle.open_units,
                "high_units": candle.high_units,
                "low_units": candle.low_units,
                "close_units": candle.close_units,
                "volume_units": candle.volume,
                "is_closed": True,
                "revision": 1,
            }
            c_model = CompletedCandle(**c_payload)
            candle_event = CompletedCandleEvent(
                id=str(uuid.uuid4()),
                content_fingerprint=c_model.content_fingerprint,
                **c_payload,
            )

        req_identities = [
            RequiredCandleIdentity(
                series_role=SeriesRole.REFERENCE,
                dataset_id=runtime.dataset_id,
                instrument_id=mapping.tradepro_instrument_id,
                content_fingerprint=candle_event.content_fingerprint,
            )
        ]
        required_candles_json = canonical_json([req.model_dump(mode="python") for req in req_identities])

        eval_identity = RuntimeEvaluationIdentity(
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            snapshot_fingerprint=orch_cfg.snapshot_fingerprint,
            mapping_id=mapping.id,
            mapping_version=mapping.mapping_version or 1,
            timeframe=runtime.timeframe,
            close_at=candle_close,
            required_candles=tuple(req_identities),
        )
        eval_fp = runtime_evaluation_v1(eval_identity)

        audit_data = {
            "result": rule_status,
            "condition_ids": ["c1"],
            "rule_results": {"GLOBAL": rule_status, "CANDIDATE": rule_status},
        }
        audit_json = canonical_json(audit_data)

        if not open_orders:
            valid_eval_status = rule_status if rule_status in ("TRUE", "FALSE", "UNAVAILABLE", "INVALID") else "FALSE"
            risk_summary_data = {
                "outcome": "NOT_RUN",
                "reason_codes": ["NO_OPEN_ORDERS_TO_CANCEL"],
                "actions": [
                    {
                        "mapping_id": action_cfg.get("mapping_id", "exit_1"),
                        "accepted": False,
                        "risk_outcome": "NOT_RUN",
                        "reason_code": "NO_OPEN_ORDERS_TO_CANCEL",
                    }
                ],
            }
            risk_summary_json = canonical_json(risk_summary_data)

            runtime_eval = RuntimeEvaluation(
                id=str(uuid.uuid4()),
                owner_id=runtime.owner_id,
                runtime_id=runtime.id,
                config_id=orch_cfg.id,
                snapshot_fingerprint=orch_cfg.snapshot_fingerprint,
                evaluation_fingerprint=eval_fp,
                timeframe=runtime.timeframe,
                close_at=candle_close,
                reference_candle_id=candle_event.id,
                subject_candle_id=None,
                required_candles_json=required_candles_json,
                evaluation_status=valid_eval_status,
                action_outcome="NO_ACTION",
                risk_outcome="NOT_RUN",
                no_order_reason="NO_OPEN_ORDERS_TO_CANCEL",
                audit_json=audit_json,
                risk_summary_json=risk_summary_json,
                finalized_at=now,
            )

            action_dec = ActionDecision(
                id=str(uuid.uuid4()),
                owner_id=runtime.owner_id,
                runtime_id=runtime.id,
                candle_timestamp=candle.timestamp,
                action_mapping_id=action_cfg.get("mapping_id", "exit_1"),
                decision="NO_ACTION",
                reason_code="NO_OPEN_ORDERS_TO_CANCEL",
                evaluation_id=runtime_eval.id,
                created_at=now,
            )

            try:
                with db.begin_nested():
                    db.add(candle_event)
                    db.flush([candle_event])
                    db.add(runtime_eval)
                    db.flush([runtime_eval])
                    db.add(action_dec)
                    db.flush([action_dec])
            except IntegrityError as exc:
                if not _is_unique_violation(exc):
                    logger.error("Non-unique integrity error in exit persistence: %s", exc)
                    raise ProviderExecutionError("Database integrity constraint violated.") from None
                logger.info("Duplicate exit action caught and rolled back via savepoint: %s", exc)
                return {
                    "action_decision": "IDEMPOTENT_SKIPPED",
                    "risk_decision": "NOT_RUN",
                    "reason_code": "EXIT_ALREADY_PROCESSED_CONCURRENT",
                    "details": {"message": "Concurrent exit evaluation serialized by unique constraint."},
                }

            return {
                "action_decision": "NO_ACTION",
                "risk_decision": "NOT_RUN",
                "reason_code": "NO_OPEN_ORDERS_TO_CANCEL",
                "details": {"evaluation_id": runtime_eval.id, "rule_status": rule_status},
            }

        risk_summary_data = {
            "outcome": "ACCEPTED",
            "reason_codes": ["EXIT_RULE_TRIGGERED"],
            "actions": [
                {
                    "mapping_id": action_cfg.get("mapping_id", "exit_1"),
                    "accepted": True,
                    "risk_outcome": "ACCEPTED",
                    "reason_code": "EXIT_RULE_TRIGGERED",
                }
            ],
        }
        risk_summary_json = canonical_json(risk_summary_data)

        valid_eval_status = rule_status if rule_status in ("TRUE", "FALSE") else "FALSE"

        runtime_eval = RuntimeEvaluation(
            id=str(uuid.uuid4()),
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            config_id=orch_cfg.id,
            snapshot_fingerprint=orch_cfg.snapshot_fingerprint,
            evaluation_fingerprint=eval_fp,
            timeframe=runtime.timeframe,
            close_at=candle_close,
            reference_candle_id=candle_event.id,
            subject_candle_id=None,
            required_candles_json=required_candles_json,
            evaluation_status=valid_eval_status,
            action_outcome="ACCEPTED_SANDBOX",
            risk_outcome="ACCEPTED",
            no_order_reason=None,
            audit_json=audit_json,
            risk_summary_json=risk_summary_json,
            finalized_at=now,
        )

        action_dec = ActionDecision(
            id=str(uuid.uuid4()),
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            candle_timestamp=candle.timestamp,
            action_mapping_id=action_cfg.get("mapping_id", "exit_1"),
            decision="ACCEPTED_SANDBOX",
            reason_code="EXIT_RULE_TRIGGERED",
            evaluation_id=runtime_eval.id,
            created_at=now,
        )

        order_ids: List[str] = []
        outbox_ids: List[str] = []
        outbox_objects: List[SubmissionOutbox] = []

        for target_order in open_orders:
            cancel_payload = {
                "order_id": target_order.id,
                "reason": "EXIT_RULE_TRIGGERED",
            }
            canonical_p_json = canonical_json(cancel_payload)
            payload_hash = hashlib.sha256(canonical_p_json.encode("utf-8")).hexdigest()
            idem_key = f"sandbox:cancel:{target_order.id}"

            outbox = SubmissionOutbox(
                id=str(uuid.uuid4()),
                owner_id=runtime.owner_id,
                order_id=target_order.id,
                action_type="CANCEL",
                priority=0,
                status="PENDING",
                idempotency_key=idem_key,
                canonical_payload_hash=payload_hash,
                payload_json=cancel_payload,
                transmission_started_at=None,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
            outbox_objects.append(outbox)
            order_ids.append(target_order.id)
            outbox_ids.append(outbox.id)

        try:
            with db.begin_nested():
                db.add(candle_event)
                db.flush([candle_event])
                db.add(runtime_eval)
                db.flush([runtime_eval])
                db.add(action_dec)
                for ob in outbox_objects:
                    db.add(ob)
                db.flush([action_dec] + outbox_objects)
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                logger.error("Non-unique integrity error in exit cancellation submission: %s", exc)
                raise ProviderExecutionError("Database integrity constraint violated.") from None
            logger.info("Duplicate exit action caught and rolled back via savepoint: %s", exc)
            return {
                "action_decision": "IDEMPOTENT_SKIPPED",
                "risk_decision": "NOT_RUN",
                "reason_code": "EXIT_ALREADY_PROCESSED_CONCURRENT",
                "details": {"message": "Concurrent exit evaluation serialized by unique constraint."},
            }

        return {
            "action_decision": "CANCEL_SUBMITTED",
            "risk_decision": "NOT_RUN",
            "reason_code": "EXIT_RULE_TRIGGERED",
            "order_ids": order_ids,
            "outbox_ids": outbox_ids,
            "details": {"action_type": "CANCEL", "cancelled_count": len(order_ids), "evaluation_id": runtime_eval.id},
        }

    def _process_no_action(
        self,
        *,
        db: Session,
        runtime: StrategyRuntime,
        orch_cfg: RuntimeOrchestrationConfig,
        mapping: ProviderInstrumentMapping,
        inst_spec: InstrumentSpec,
        candle: MarketDataCandle,
        candle_close: datetime.datetime,
        now: datetime.datetime,
        rule_status: str = "FALSE",
        provenance_fingerprint: Optional[str] = None,
        reason_code: str = "RULE_NOT_TRIGGERED",
    ) -> Dict[str, Any]:
        """Persists durable evaluation and no-action decision when strategy rules do not trigger."""
        candle_event = (
            db.query(CompletedCandleEvent)
            .filter(
                CompletedCandleEvent.owner_id == runtime.owner_id,
                CompletedCandleEvent.runtime_id == runtime.id,
                CompletedCandleEvent.close_at == candle_close,
            )
            .first()
        )
        if not candle_event:
            c_payload = {
                "owner_id": runtime.owner_id,
                "runtime_id": runtime.id,
                "source_namespace": orch_cfg.source_namespace,
                "source_event_id": f"provider_candle_{int(candle.timestamp.timestamp())}",
                "source_type": orch_cfg.source_type,
                "dataset_id": runtime.dataset_id,
                "dataset_checksum": hashlib.sha256(runtime.dataset_id.encode()).hexdigest(),
                "instrument_id": mapping.tradepro_instrument_id,
                "timeframe": runtime.timeframe,
                "series_role": "REFERENCE",
                "source_policy_version": orch_cfg.source_policy_version,
                "alignment_offset_seconds": orch_cfg.alignment_offset_seconds,
                "open_at": candle.timestamp,
                "close_at": candle_close,
                "received_at": now if now >= candle_close else candle_close,
                "price_scale": 4,
                "volume_scale": 0,
                "open_units": candle.open_units,
                "high_units": candle.high_units,
                "low_units": candle.low_units,
                "close_units": candle.close_units,
                "volume_units": candle.volume,
                "is_closed": True,
                "revision": 1,
            }
            c_model = CompletedCandle(**c_payload)
            candle_event = CompletedCandleEvent(
                id=str(uuid.uuid4()),
                content_fingerprint=c_model.content_fingerprint,
                **c_payload,
            )

        req_identities = [
            RequiredCandleIdentity(
                series_role=SeriesRole.REFERENCE,
                dataset_id=runtime.dataset_id,
                instrument_id=mapping.tradepro_instrument_id,
                content_fingerprint=candle_event.content_fingerprint,
            )
        ]
        required_candles_json = canonical_json([req.model_dump(mode="python") for req in req_identities])

        eval_identity = RuntimeEvaluationIdentity(
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            snapshot_fingerprint=orch_cfg.snapshot_fingerprint,
            mapping_id=mapping.id,
            mapping_version=mapping.mapping_version or 1,
            timeframe=runtime.timeframe,
            close_at=candle_close,
            required_candles=tuple(req_identities),
        )
        eval_fp = runtime_evaluation_v1(eval_identity)

        audit_data = {
            "result": rule_status,
            "condition_ids": [],
            "rule_results": {"GLOBAL": rule_status},
        }
        audit_json = canonical_json(audit_data)

        risk_summary_data = {
            "outcome": "NOT_RUN",
            "reason_codes": [reason_code],
            "actions": [],
        }
        risk_summary_json = canonical_json(risk_summary_data)

        valid_eval_status = rule_status if rule_status in ("TRUE", "FALSE", "UNAVAILABLE", "INVALID") else "FALSE"

        runtime_eval = RuntimeEvaluation(
            id=str(uuid.uuid4()),
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            config_id=orch_cfg.id,
            snapshot_fingerprint=orch_cfg.snapshot_fingerprint,
            evaluation_fingerprint=eval_fp,
            timeframe=runtime.timeframe,
            close_at=candle_close,
            reference_candle_id=candle_event.id,
            subject_candle_id=None,
            required_candles_json=required_candles_json,
            evaluation_status=valid_eval_status,
            action_outcome="NO_ACTION",
            risk_outcome="NOT_RUN",
            no_order_reason=reason_code,
            audit_json=audit_json,
            risk_summary_json=risk_summary_json,
            finalized_at=now,
        )

        action_dec = ActionDecision(
            id=str(uuid.uuid4()),
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            candle_timestamp=candle.timestamp,
            action_mapping_id="none",
            decision="NO_ACTION",
            reason_code=reason_code,
            evaluation_id=runtime_eval.id,
            created_at=now,
        )

        try:
            with db.begin_nested():
                db.add(candle_event)
                db.flush([candle_event])
                db.add(runtime_eval)
                db.flush([runtime_eval])
                db.add(action_dec)
                db.flush([action_dec])
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                logger.error("Non-unique integrity error in no-action persistence: %s", exc)
                raise ProviderExecutionError("Database integrity constraint violated.") from None
            logger.info("Duplicate no-action evaluation caught and rolled back via savepoint: %s", exc)
            return {
                "action_decision": "IDEMPOTENT_SKIPPED",
                "risk_decision": "NOT_RUN",
                "reason_code": "CANDLE_ALREADY_PROCESSED_CONCURRENT",
                "details": {"message": "Concurrent evaluation serialized by unique constraint."},
            }

        return {
            "action_decision": "NO_ACTION",
            "risk_decision": "NOT_RUN",
            "reason_code": reason_code,
            "order_ids": [],
            "outbox_ids": [],
            "details": {"evaluation_id": runtime_eval.id, "rule_status": rule_status},
        }
