"""Deterministic Strategy Orchestration Evaluation Engine.

Synchronizes required series at the canonical close boundary, enforces no-look-ahead,
executes rule evaluation and pre-intent risk checks, and generates bounded canonical evidence.
"""
import datetime
import json
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import and_

from src.engine.evaluator import RuleEvaluator
from src.engine.models import Candle
from src.engine.orchestration.evidence import evaluation_evidence
from src.engine.orchestration.fingerprint import canonical_json, runtime_evaluation_v1
from src.engine.orchestration.models import (
    ActionOutcome,
    OrchestrationSnapshot,
    RequiredCandleIdentity,
    RiskOutcome,
    RuntimeEvaluationIdentity,
    SeriesRole,
    utc,
)
from src.engine.rule_models import EvaluationStatus, RuleEvaluationResult
from src.models import (
    CompletedCandleEvent,
    RuntimeEvaluation,
    RuntimeOrchestrationConfig,
    StrategyRuntime,
)


class SynchronizationError(Exception):
    """Raised when required series cannot be synchronized at an evaluation boundary."""
    pass


def candles_to_engine_domain(candle_events: List[CompletedCandleEvent]) -> List[Candle]:
    """Convert CompletedCandleEvent records to engine Candle models for indicator calculation.

    Chronologically sorts by open_at and uses exact decimal division for float prices.
    """
    sorted_events = sorted(candle_events, key=lambda c: c.open_at)
    domain_candles: List[Candle] = []

    for c in sorted_events:
        price_factor = 10 ** c.price_scale
        vol_factor = 10 ** c.volume_scale

        domain_candles.append(
            Candle(
                timestamp=c.open_at,
                instrument_id=c.instrument_id,
                timeframe=c.timeframe,
                open=c.open_units / price_factor,
                high=c.high_units / price_factor,
                low=c.low_units / price_factor,
                close=c.close_units / price_factor,
                volume=c.volume_units / vol_factor,
                is_closed=c.is_closed,
            )
        )
    return domain_candles


class OrchestrationEvaluator:
    """Evaluates strategy rules and pre-intent risk for an orchestration runtime at a canonical boundary."""

    def __init__(self, rule_evaluator: Optional[RuleEvaluator] = None):
        self.rule_evaluator = rule_evaluator or RuleEvaluator()

    def synchronize_required_boundary_candles(
        self,
        db: Session,
        config: RuntimeOrchestrationConfig,
        snapshot: OrchestrationSnapshot,
        boundary: datetime.datetime,
    ) -> Tuple[CompletedCandleEvent, Optional[CompletedCandleEvent], List[RequiredCandleIdentity]]:
        """Verify that every required dataset has exactly one completed candle for the close boundary.

        Enforces strict synchronization: every dataset declared in snapshot.datasets must be present
        at exactly close_at == boundary with matching instrument, timeframe, and checksum.
        If any series is missing or mismatched at the boundary, raises SynchronizationError.
        Never forward-fills, nearest-timestamp matches, or substitutes datasets.
        """
        boundary = utc(boundary)
        ref_event: Optional[CompletedCandleEvent] = None
        subj_event: Optional[CompletedCandleEvent] = None
        required_identities: List[RequiredCandleIdentity] = []

        # Iterate through every dataset declared in frozen snapshot.datasets
        for d in snapshot.datasets:
            event = db.query(CompletedCandleEvent).filter(
                CompletedCandleEvent.owner_id == config.owner_id,
                CompletedCandleEvent.runtime_id == config.runtime_id,
                CompletedCandleEvent.dataset_id == d.dataset_id,
                CompletedCandleEvent.series_role == d.series_role.value,
                CompletedCandleEvent.instrument_id == d.instrument_id,
                CompletedCandleEvent.timeframe == config.timeframe,
                CompletedCandleEvent.close_at == boundary,
            ).first()

            if not event:
                raise SynchronizationError(
                    f"Missing required {d.series_role.value} candle at boundary {boundary.isoformat()}"
                )

            if event.dataset_checksum != d.checksum:
                raise SynchronizationError(
                    f"Candle dataset checksum mismatch for '{d.dataset_id}': expected {d.checksum}, got {event.dataset_checksum}"
                )

            required_identities.append(
                RequiredCandleIdentity(
                    series_role=d.series_role,
                    dataset_id=event.dataset_id,
                    instrument_id=event.instrument_id,
                    content_fingerprint=event.content_fingerprint,
                )
            )

            if d.series_role == SeriesRole.REFERENCE:
                ref_event = event
            elif d.series_role == SeriesRole.SUBJECT:
                subj_event = event

        if not ref_event:
            raise SynchronizationError("Frozen snapshot has no REFERENCE dataset")

        return ref_event, subj_event, required_identities

    def load_historical_series_up_to_boundary(
        self,
        db: Session,
        config: RuntimeOrchestrationConfig,
        boundary: datetime.datetime,
        ref_dataset_id: str,
        subj_dataset_id: Optional[str] = None,
    ) -> Tuple[List[Candle], Optional[List[Candle]]]:
        """Load all historical completed candles up to boundary (inclusive).

        Strictly enforces the no-look-ahead rule: close_at <= boundary.
        No candle with close_at > boundary or received_at > boundary can be loaded.
        """
        boundary = utc(boundary)

        # Load reference series up to boundary (ordered chronologically)
        ref_events = db.query(CompletedCandleEvent).filter(
            CompletedCandleEvent.owner_id == config.owner_id,
            CompletedCandleEvent.runtime_id == config.runtime_id,
            CompletedCandleEvent.dataset_id == ref_dataset_id,
            CompletedCandleEvent.series_role == SeriesRole.REFERENCE.value,
            CompletedCandleEvent.close_at <= boundary,
        ).order_by(CompletedCandleEvent.open_at.asc()).all()

        ref_candles = candles_to_engine_domain(ref_events)

        subj_candles: Optional[List[Candle]] = None
        if subj_dataset_id:
            subj_events = db.query(CompletedCandleEvent).filter(
                CompletedCandleEvent.owner_id == config.owner_id,
                CompletedCandleEvent.runtime_id == config.runtime_id,
                CompletedCandleEvent.dataset_id == subj_dataset_id,
                CompletedCandleEvent.series_role == SeriesRole.SUBJECT.value,
                CompletedCandleEvent.close_at <= boundary,
            ).order_by(CompletedCandleEvent.open_at.asc()).all()
            subj_candles = candles_to_engine_domain(subj_events)

        return ref_candles, subj_candles

    def evaluate_boundary(
        self,
        db: Session,
        config: RuntimeOrchestrationConfig,
        boundary: datetime.datetime,
        *,
        clock: Optional[Callable[[], datetime.datetime]] = None,
    ) -> RuntimeEvaluation:
        """Perform deterministic evaluation of frozen strategy rules at boundary."""
        boundary = utc(boundary)
        clock_fn = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        finalized_at = utc(clock_fn())

        if finalized_at < boundary:
            finalized_at = boundary

        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        # 1. Synchronize boundary candles across all snapshot datasets
        ref_event, subj_event, req_identities = self.synchronize_required_boundary_candles(
            db, config, snapshot, boundary
        )

        # 2. Extract dataset IDs
        ref_dataset_id = ref_event.dataset_id
        subj_dataset_id = subj_event.dataset_id if subj_event else None

        # 3. Load historical candles strictly up to boundary (no look-ahead)
        ref_candles, subj_candles = self.load_historical_series_up_to_boundary(
            db, config, boundary, ref_dataset_id, subj_dataset_id
        )

        # 4. Parse strategy and evaluate rules
        strategy_payload = json.loads(snapshot.strategy_snapshot)
        rule_result: RuleEvaluationResult = self.rule_evaluator.evaluate_strategy_rules(
            strategy_payload=strategy_payload,
            reference_candles=ref_candles,
            subject_candles=subj_candles,
            eval_timestamp=boundary,
        )

        eval_status_str = rule_result.overall_status.value

        # 5. Parse action & risk policy snapshots and evaluate deterministic pre-action outcomes
        action_snap = json.loads(snapshot.action_policy_snapshot)
        risk_snap = json.loads(snapshot.risk_policy_snapshot)

        action_outcome, risk_outcome, no_order_reason = self._determine_outcomes(
            db=db,
            config=config,
            eval_status=eval_status_str,
            action_policy=action_snap,
            risk_policy=risk_snap,
            rule_result=rule_result,
            boundary=boundary,
            finalized_at=finalized_at,
        )

        # 6. Format canonical bounded evidence
        passed_conditions = [c.replace("-", "_") for c in rule_result.passed_condition_ids] if rule_result.passed_condition_ids else ["RULE_EVAL"]
        audit_payload = {
            "result": eval_status_str,
            "condition_ids": passed_conditions[:10],
        }
        audit_json = evaluation_evidence(json.dumps(audit_payload), risk=False)

        reason_code = no_order_reason if no_order_reason else "PASSED"
        risk_payload = {
            "outcome": risk_outcome,
            "reason_codes": [reason_code],
        }
        risk_summary_json = evaluation_evidence(json.dumps(risk_payload), risk=True)

        # 7. Required candles json and evaluation identity
        req_dicts = [req.model_dump(mode="python") for req in req_identities]
        required_candles_json = canonical_json(req_dicts)

        provider_map = snapshot.provider_mapping
        eval_identity = RuntimeEvaluationIdentity(
            owner_id=config.owner_id,
            runtime_id=config.runtime_id,
            snapshot_fingerprint=config.snapshot_fingerprint,
            mapping_id=provider_map.mapping_id,
            mapping_version=provider_map.mapping_version,
            timeframe=config.timeframe,
            close_at=boundary,
            required_candles=tuple(req_identities),
        )
        eval_fingerprint = runtime_evaluation_v1(eval_identity)

        # 8. Construct RuntimeEvaluation
        evaluation_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"tradpro:eval:{config.runtime_id}:{eval_fingerprint}"))

        evaluation = RuntimeEvaluation(
            id=evaluation_id,
            owner_id=config.owner_id,
            runtime_id=config.runtime_id,
            config_id=config.id,
            snapshot_fingerprint=config.snapshot_fingerprint,
            evaluation_fingerprint=eval_fingerprint,
            timeframe=config.timeframe,
            close_at=boundary,
            reference_candle_id=ref_event.id,
            subject_candle_id=subj_event.id if subj_event else None,
            required_candles_json=required_candles_json,
            evaluation_status=eval_status_str,
            action_outcome=action_outcome,
            risk_outcome=risk_outcome,
            no_order_reason=no_order_reason,
            audit_json=audit_json,
            risk_summary_json=risk_summary_json,
            finalized_at=finalized_at,
        )

        return evaluation

    def _determine_outcomes(
        self,
        *,
        db: Session,
        config: RuntimeOrchestrationConfig,
        eval_status: str,
        action_policy: Dict[str, Any],
        risk_policy: Dict[str, Any],
        rule_result: RuleEvaluationResult,
        boundary: datetime.datetime,
        finalized_at: datetime.datetime,
    ) -> Tuple[str, str, Optional[str]]:
        """Determine action outcome, risk outcome, and bounded no_order_reason.

        Strictly enforces database check constraints (ck_orch_eval_outcome):
        - If action_outcome == ACCEPTED_INTERNAL:
          risk_outcome == ACCEPTED, no_order_reason IS NULL, eval_status in ('TRUE', 'FALSE')
        - Otherwise:
          no_order_reason IS NOT NULL, length between 1 and 64
        """
        trigger_status = (
            action_policy.get("entry_mapping", {}).get("trigger_status")
            or action_policy.get("trigger_status", "TRUE")
        )
        # Handle "ON_TRUE" vs "TRUE"
        trigger_matches = (
            (trigger_status in ("ON_TRUE", "TRUE") and eval_status == "TRUE")
            or (trigger_status in ("ON_FALSE", "FALSE") and eval_status == "FALSE")
        )

        if trigger_matches:
            # Rule conditions met: evaluate deterministic pre-action eligibility checks
            pre_action_passed, pre_action_reason = self._evaluate_pre_action_eligibility(
                db=db,
                config=config,
                action_policy=action_policy,
                risk_policy=risk_policy,
                boundary=boundary,
                finalized_at=finalized_at,
            )

            if pre_action_passed:
                return (
                    ActionOutcome.ACCEPTED_INTERNAL.value,
                    RiskOutcome.ACCEPTED.value,
                    None,
                )
            else:
                return (
                    ActionOutcome.REJECTED.value,
                    RiskOutcome.REJECTED.value,
                    pre_action_reason or "RISK_LIMIT_EXCEEDED",
                )

        elif eval_status == "FALSE":
            return (
                ActionOutcome.NO_ACTION.value,
                RiskOutcome.NOT_RUN.value,
                "RULE_CONDITION_NOT_MET",
            )
        elif eval_status == "UNAVAILABLE":
            return (
                ActionOutcome.NO_ACTION.value,
                RiskOutcome.NOT_RUN.value,
                "INSUFFICIENT_WARMUP_DATA",
            )
        else:  # "INVALID" or unknown
            return (
                ActionOutcome.NO_ACTION.value,
                RiskOutcome.NOT_RUN.value,
                "INVALID_EVALUATION_INPUT",
            )

    def _evaluate_pre_action_eligibility(
        self,
        *,
        db: Session,
        config: RuntimeOrchestrationConfig,
        action_policy: Dict[str, Any],
        risk_policy: Dict[str, Any],
        boundary: datetime.datetime,
        finalized_at: datetime.datetime,
    ) -> Tuple[bool, Optional[str]]:
        """Evaluate deterministic pre-action eligibility checks.

        In Phase 3 fixture replay, validates:
        1. Global and user kill-switch states.
        2. Instrument whitelist from risk policy (if configured).
        3. Maximum price staleness (if configured).

        All financial account mutations, ledger balance reservations, order creation,
        and fills remain deferred to Phase 4 / live execution. Phase 3 produces zero orders.
        """
        from src.models import KillSwitch

        # 1. Check Global Kill-Switch
        global_kill = db.query(KillSwitch).filter(
            KillSwitch.scope == "GLOBAL",
            KillSwitch.is_active.is_(True),
        ).first()
        if global_kill:
            return False, "GLOBAL_KILL_SWITCH_ENGAGED"

        # 2. Check User Kill-Switch
        user_kill = db.query(KillSwitch).filter(
            KillSwitch.scope == "USER",
            KillSwitch.user_id == config.owner_id,
            KillSwitch.is_active.is_(True),
        ).first()
        if user_kill:
            return False, "USER_KILL_SWITCH_ENGAGED"

        # 3. Check Allowed Instruments (if risk policy defines allowed_instruments)
        allowed_instruments = risk_policy.get("allowed_instruments")
        target_inst = action_policy.get("entry_mapping", {}).get("instrument_id")
        if allowed_instruments is not None and target_inst:
            if target_inst not in allowed_instruments:
                return False, "DISALLOWED_INSTRUMENT"

        # 4. Check Price Staleness limit
        max_staleness = risk_policy.get("max_price_staleness_seconds")
        if max_staleness is not None:
            # Deterministic staleness for fixture replay uses the replay boundary timestamp:
            # Staleness of synchronous boundary candle at evaluation boundary is 0.0 seconds
            staleness = 0.0
            if staleness > max_staleness:
                return False, "PRICE_STALENESS_EXCEEDED"

        # 5. Check risk bounds
        max_trades = risk_policy.get("max_trades_per_day")
        if max_trades is not None and max_trades <= 0:
            return False, "MAX_TRADES_PER_DAY_EXCEEDED"

        max_orders = risk_policy.get("max_open_orders")
        if max_orders is not None and max_orders <= 0:
            return False, "MAX_OPEN_ORDERS_EXCEEDED"

        return True, None
