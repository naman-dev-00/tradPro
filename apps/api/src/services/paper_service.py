import uuid
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models import (
    User,
    Strategy,
    PaperAccount,
    AccountLedgerEntry,
    StrategyActionPolicy,
    RiskPolicy,
    StrategyRuntime,
    OrderIntent,
    Order,
    OrderEvent,
    Fill,
    PaperPosition,
    KillSwitch,
    RuntimeEvent,
    ActionDecision,
    RiskDecision,
    ApiIdempotencyRecord,
)
from src.engine.paper.models import (
    TradingMode,
    IntentType,
    OrderSide,
    OrderType,
    TimeInForce,
    OrderStatus,
    RuntimeStatus,
    KillSwitchScope,
    LedgerEntryType,
    ActionTriggerCondition,
    PositionExistsBehavior,
    RiskReasonCode,
    ActionIgnoredReasonCode,
    InstrumentSpec,
    get_instrument_spec,
    PACKAGED_INSTRUMENT_SPECS,
)
from src.engine.paper.units import decimal_to_units, units_to_decimal, quantize_decimal
from src.engine.paper.state_machine import validate_order_transition, validate_runtime_transition
from src.engine.paper.risk_engine import PureRiskEngine, RiskCheckResult
from src.engine.paper.fill_model import DeterministicFillEngine, CandleExecutionSummary, FillResult
from src.engine.paper.accounting import AccountingEngine, PositionUpdateResult
from src.engine.paper.runtime import canonicalize_json, compute_order_intent_identity, compute_trigger_event_key, compute_evaluation_fingerprint
from src.engine.manifest import load_dataset_candles, get_dataset_entry, get_manifest_checksums_snapshot, MANIFEST_VERSION
from src.engine.evaluator import RuleEvaluator
from src.engine.rule_models import EvaluationStatus

logger = logging.getLogger("tradepro.paper_service")

class ResourceNotFoundError(ValueError):
    """Raised when a referenced resource does not exist or is not owned by the caller."""
    pass

class PaperService:
    # --- Account Operations ---

    @staticmethod
    def create_account(
        db: Session,
        owner_id: str,
        name: str,
        initial_balance: Decimal,
        currency: str = "INR"
    ) -> PaperAccount:
        currency_scale = 2
        initial_units = decimal_to_units(initial_balance, currency_scale)

        account = PaperAccount(
            owner_id=owner_id,
            name=name,
            currency=currency,
            total_cash_units=initial_units,
            reserved_cash_units=0,
            is_active=True,
            version=1,
        )
        db.add(account)
        db.flush()

        # Insert initial deposit ledger entry with explicit deltas
        ledger = AccountLedgerEntry(
            account_id=account.id,
            owner_id=owner_id,
            sequence_number=1,
            entry_type=LedgerEntryType.INITIAL_DEPOSIT.value,
            settled_cash_delta_units=initial_units,
            reserved_cash_delta_units=0,
            settled_cash_after_units=initial_units,
            reserved_cash_after_units=0,
            amount_units=initial_units,
            balance_after_units=initial_units,
            reason_code="INITIAL_DEPOSIT",
            idempotency_key=f"init:{account.id}",
        )
        db.add(ledger)
        db.commit()
        db.refresh(account)
        return account

    @staticmethod
    def list_accounts(db: Session, owner_id: str) -> List[PaperAccount]:
        return db.query(PaperAccount).filter(PaperAccount.owner_id == owner_id).order_by(PaperAccount.created_at.desc()).all()

    @staticmethod
    def get_account(db: Session, account_id: str, owner_id: str) -> Optional[PaperAccount]:
        return db.query(PaperAccount).filter(PaperAccount.id == account_id, PaperAccount.owner_id == owner_id).first()

    @staticmethod
    def get_ledger_entries(db: Session, account_id: str, owner_id: str, limit: int = 50, offset: int = 0) -> List[AccountLedgerEntry]:
        # Verify ownership
        account = PaperService.get_account(db, account_id, owner_id)
        if not account:
            return []
        return (
            db.query(AccountLedgerEntry)
            .filter(AccountLedgerEntry.account_id == account_id)
            .order_by(AccountLedgerEntry.sequence_number.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

    # --- Policy Operations ---

    @staticmethod
    def create_action_policy(
        db: Session,
        owner_id: str,
        strategy_id: str,
        name: str,
        payload: Dict[str, Any]
    ) -> StrategyActionPolicy:
        # Check strategy ownership
        strat = db.query(Strategy).filter(Strategy.id == strategy_id, Strategy.owner_id == owner_id).first()
        if not strat:
            raise ResourceNotFoundError(f"Strategy '{strategy_id}' not found.")

        # Determine version
        latest = (
            db.query(StrategyActionPolicy)
            .filter(StrategyActionPolicy.strategy_id == strategy_id)
            .order_by(StrategyActionPolicy.version.desc())
            .first()
        )
        version = (latest.version + 1) if latest else 1

        policy = StrategyActionPolicy(
            owner_id=owner_id,
            strategy_id=strategy_id,
            name=name,
            version=version,
            payload=payload,
            is_active=True,
        )
        db.add(policy)
        db.commit()
        db.refresh(policy)
        return policy

    @staticmethod
    def get_action_policy(db: Session, policy_id: str, owner_id: str) -> Optional[StrategyActionPolicy]:
        return db.query(StrategyActionPolicy).filter(StrategyActionPolicy.id == policy_id, StrategyActionPolicy.owner_id == owner_id).first()

    @staticmethod
    def list_action_policies(db: Session, owner_id: str) -> List[StrategyActionPolicy]:
        return db.query(StrategyActionPolicy).filter(StrategyActionPolicy.owner_id == owner_id).order_by(StrategyActionPolicy.created_at.desc()).all()

    @staticmethod
    def create_risk_policy(
        db: Session,
        owner_id: str,
        name: str,
        payload: Dict[str, Any],
        is_default: bool = False
    ) -> RiskPolicy:
        latest = (
            db.query(RiskPolicy)
            .filter(RiskPolicy.owner_id == owner_id, RiskPolicy.name == name)
            .order_by(RiskPolicy.version.desc())
            .first()
        )
        version = (latest.version + 1) if latest else 1

        policy = RiskPolicy(
            owner_id=owner_id,
            name=name,
            version=version,
            payload=payload,
            is_default=is_default,
        )
        db.add(policy)
        db.commit()
        db.refresh(policy)
        return policy

    @staticmethod
    def get_risk_policy(db: Session, policy_id: str, owner_id: str) -> Optional[RiskPolicy]:
        return db.query(RiskPolicy).filter(RiskPolicy.id == policy_id, RiskPolicy.owner_id == owner_id).first()

    @staticmethod
    def list_risk_policies(db: Session, owner_id: str) -> List[RiskPolicy]:
        return db.query(RiskPolicy).filter(RiskPolicy.owner_id == owner_id).order_by(RiskPolicy.created_at.desc()).all()

    # --- Runtime Orchestration ---

    @staticmethod
    def instantiate_runtime(
        db: Session,
        owner_id: str,
        strategy_id: str,
        account_id: str,
        dataset_id: str,
        action_policy_id: Optional[str] = None,
        risk_policy_id: Optional[str] = None,
        timeframe: str = "15m"
    ) -> StrategyRuntime:
        # Check strategy & account ownership
        strat = db.query(Strategy).filter(Strategy.id == strategy_id, Strategy.owner_id == owner_id).first()
        if not strat:
            raise ResourceNotFoundError(f"Strategy '{strategy_id}' not found.")

        acct = db.query(PaperAccount).filter(PaperAccount.id == account_id, PaperAccount.owner_id == owner_id).first()
        if not acct:
            raise ResourceNotFoundError(f"Paper account '{account_id}' not found.")

        # Check action policy ownership if supplied
        if action_policy_id:
            action_policy = db.query(StrategyActionPolicy).filter(
                StrategyActionPolicy.id == action_policy_id,
                StrategyActionPolicy.owner_id == owner_id
            ).first()
            if not action_policy:
                raise ResourceNotFoundError(f"Action policy '{action_policy_id}' not found.")
        else:
            action_policy = PaperService._synthesize_default_action_policy(db, owner_id, strat)
            action_policy_id = action_policy.id

        # Check risk policy ownership if supplied
        if risk_policy_id:
            risk_policy = db.query(RiskPolicy).filter(
                RiskPolicy.id == risk_policy_id,
                RiskPolicy.owner_id == owner_id
            ).first()
            if not risk_policy:
                raise ResourceNotFoundError(f"Risk policy '{risk_policy_id}' not found.")
        else:
            risk_policy = PaperService._synthesize_default_risk_policy(db, owner_id, strat)
            risk_policy_id = risk_policy.id

        # Verify dataset in manifest and category
        manifest_entry = get_dataset_entry(dataset_id)
        if not manifest_entry:
            raise ValueError(f"Dataset '{dataset_id}' not found in canonical dataset manifest.")
        if manifest_entry.category.value != "SUBJECT":
            raise ValueError(f"Dataset '{dataset_id}' is a {manifest_entry.category.value} dataset. Only SUBJECT datasets are orderable/tradable.")

        entry_map = action_policy.payload.get("entry_mapping", {})
        inst_id = entry_map.get("instrument_id") or "synthetic_candidate_option_pe_23000_15m"
        inst_spec = get_instrument_spec(inst_id)
        if not inst_spec:
            raise ValueError(f"Instrument '{inst_id}' has no registered InstrumentSpec in catalog.")
        if not inst_spec.is_tradable:
            raise ValueError(f"Instrument '{inst_id}' is a non-tradable reference instrument and cannot be used for runtime execution.")
        if dataset_id != inst_spec.execution_dataset_id:
            raise ValueError(f"Dataset mismatch: runtime execution dataset '{dataset_id}' does not match instrument execution dataset '{inst_spec.execution_dataset_id}'.")

        # Transactionally freeze all snapshots at runtime creation time (Item 3)
        strategy_snapshot = dict(strat.payload)
        action_policy_snapshot = dict(action_policy.payload)
        risk_policy_snapshot = dict(risk_policy.payload)
        instrument_spec_snapshot = inst_spec.model_dump(mode="json")
        fee_model_snapshot = {"fee_basis_points": 5, "flat_fee_units": 2000}
        slippage_model_snapshot = {"model": "FIXED_BPS", "basis_points": 5}

        runtime = StrategyRuntime(
            owner_id=owner_id,
            strategy_id=strategy_id,
            action_policy_id=action_policy_id,
            risk_policy_id=risk_policy_id,
            account_id=account_id,
            status=RuntimeStatus.DRAFT.value,
            trading_mode=TradingMode.PAPER.value,
            dataset_id=dataset_id,
            timeframe=timeframe,
            strategy_snapshot=strategy_snapshot,
            action_policy_snapshot=action_policy_snapshot,
            risk_policy_snapshot=risk_policy_snapshot,
            instrument_spec_snapshot=instrument_spec_snapshot,
            fee_model_snapshot=fee_model_snapshot,
            slippage_model_snapshot=slippage_model_snapshot,
            dataset_checksum=manifest_entry.dataset_checksum,
            manifest_version=MANIFEST_VERSION,
            engine_version="1.0.0",
            runtime_schema_version="1.0.0",
            consecutive_errors=0,
            version=1,
        )
        db.add(runtime)
        db.flush()

        # Record initial DRAFT event
        event = RuntimeEvent(
            runtime_id=runtime.id,
            sequence_number=1,
            previous_status="NONE",
            new_status=RuntimeStatus.DRAFT.value,
            actor=owner_id,
            reason_code="RUNTIME_INSTANTIATED",
        )
        db.add(event)
        db.commit()
        db.refresh(runtime)
        return runtime

    @staticmethod
    def _synthesize_default_action_policy(db: Session, owner_id: str, strategy: Strategy) -> StrategyActionPolicy:
        # Look for existing policy
        existing = db.query(StrategyActionPolicy).filter(StrategyActionPolicy.strategy_id == strategy.id).first()
        if existing:
            return existing

        default_inst = "synthetic_candidate_option_pe_23000_15m"
        payload = {
            "entry_mapping": {
                "mapping_id": "auto_entry_1",
                "rule_target": "GLOBAL",
                "trigger_status": "ON_TRUE",
                "instrument_id": default_inst,
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": 50,
                "time_in_force": "DAY",
                "cooldown_bars": 1,
                "intent_type": "ENTRY",
            },
            "position_exists_behavior": "IGNORE",
            "max_entries_per_day": 5,
        }
        policy = StrategyActionPolicy(
            owner_id=owner_id,
            strategy_id=strategy.id,
            name=f"{strategy.name} Auto Action Policy",
            version=1,
            payload=payload,
            is_active=True,
        )
        db.add(policy)
        db.flush()
        return policy

    @staticmethod
    def _synthesize_default_risk_policy(db: Session, owner_id: str, strategy: Strategy) -> RiskPolicy:
        existing = db.query(RiskPolicy).filter(RiskPolicy.owner_id == owner_id, RiskPolicy.name == "Default Risk Policy").first()
        if existing:
            return existing

        payload = {
            "max_quantity_per_order_units": 1000,
            "max_notional_per_order_units": 50000000,  # 500,000 INR in paise
            "max_open_orders": 10,
            "max_open_positions": 5,
            "max_instrument_exposure_units": 100000000,
            "max_total_exposure_units": 200000000,
            "max_trades_per_day": 20,
            "max_daily_realized_loss_units": 5000000,
            "allowed_instruments": list(PACKAGED_INSTRUMENT_SPECS.keys()),
            "max_price_staleness_seconds": 3600,
            "fee_basis_points": 5,
            "flat_fee_units": 2000,
        }
        policy = RiskPolicy(
            owner_id=owner_id,
            name="Default Risk Policy",
            version=1,
            payload=payload,
            is_default=True,
        )
        db.add(policy)
        db.flush()
        return policy

    @staticmethod
    def validate_runtime(db: Session, runtime_id: str, owner_id: str) -> Dict[str, Any]:
        runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == runtime_id, StrategyRuntime.owner_id == owner_id).first()
        if not runtime:
            raise ResourceNotFoundError(f"Runtime '{runtime_id}' not found.")

        errors = []
        # Validate based on frozen snapshots
        action_snap = runtime.action_policy_snapshot or {}
        entry_map = action_snap.get("entry_mapping")
        if not entry_map:
            errors.append("Action policy snapshot missing entry_mapping.")
        else:
            inst_id = entry_map.get("instrument_id")
            if not get_instrument_spec(inst_id):
                errors.append(f"Instrument '{inst_id}' has no registered InstrumentSpec.")

        if not runtime.risk_policy_snapshot:
            errors.append("Linked risk policy snapshot missing.")

        acct = db.query(PaperAccount).filter(PaperAccount.id == runtime.account_id, PaperAccount.owner_id == owner_id).first()
        if not acct:
            raise ResourceNotFoundError(f"Linked paper account '{runtime.account_id}' not found.")
        elif acct.total_cash_units <= 0:
            errors.append("Linked paper account has zero balance.")

        if errors:
            return {"valid": False, "errors": errors}

        # Transition to READY
        validate_runtime_transition(RuntimeStatus(runtime.status), RuntimeStatus.READY, actor=owner_id, reason_code="VALIDATION_PASSED")
        runtime.status = RuntimeStatus.READY.value

        seq = db.query(func.coalesce(func.max(RuntimeEvent.sequence_number), 0)).filter(RuntimeEvent.runtime_id == runtime.id).scalar() + 1
        event = RuntimeEvent(
            runtime_id=runtime.id,
            sequence_number=seq,
            previous_status=RuntimeStatus.DRAFT.value,
            new_status=RuntimeStatus.READY.value,
            actor=owner_id,
            reason_code="VALIDATION_PASSED",
        )
        db.add(event)
        db.commit()
        db.refresh(runtime)
        return {"valid": True, "status": runtime.status}

    @staticmethod
    def start_runtime(db: Session, runtime_id: str, owner_id: str) -> StrategyRuntime:
        runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == runtime_id, StrategyRuntime.owner_id == owner_id).first()
        if not runtime:
            raise ResourceNotFoundError(f"Runtime '{runtime_id}' not found.")

        # Check kill switch
        global_ks = db.query(KillSwitch).filter(KillSwitch.scope == "GLOBAL", KillSwitch.is_active.is_(True)).first()
        user_ks = db.query(KillSwitch).filter(KillSwitch.scope == "USER", KillSwitch.user_id == owner_id, KillSwitch.is_active.is_(True)).first()
        if global_ks or user_ks:
            raise ValueError("Cannot start runtime while kill switch is active.")

        curr_status = RuntimeStatus(runtime.status)
        validate_runtime_transition(curr_status, RuntimeStatus.RUNNING, actor=owner_id, reason_code="RUNTIME_STARTED")

        # Snapshots remain strictly frozen from instantiate_runtime
        runtime.status = RuntimeStatus.RUNNING.value

        seq = db.query(func.coalesce(func.max(RuntimeEvent.sequence_number), 0)).filter(RuntimeEvent.runtime_id == runtime.id).scalar() + 1
        event = RuntimeEvent(
            runtime_id=runtime.id,
            sequence_number=seq,
            previous_status=curr_status.value,
            new_status=RuntimeStatus.RUNNING.value,
            actor=owner_id,
            reason_code="RUNTIME_STARTED",
        )
        db.add(event)
        db.commit()
        db.refresh(runtime)
        return runtime

    @staticmethod
    def pause_runtime(db: Session, runtime_id: str, owner_id: str) -> StrategyRuntime:
        runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == runtime_id, StrategyRuntime.owner_id == owner_id).first()
        if not runtime:
            raise ResourceNotFoundError(f"Runtime '{runtime_id}' not found.")

        curr_status = RuntimeStatus(runtime.status)
        validate_runtime_transition(curr_status, RuntimeStatus.PAUSED, actor=owner_id, reason_code="USER_PAUSED")
        runtime.status = RuntimeStatus.PAUSED.value

        seq = db.query(func.coalesce(func.max(RuntimeEvent.sequence_number), 0)).filter(RuntimeEvent.runtime_id == runtime.id).scalar() + 1
        event = RuntimeEvent(
            runtime_id=runtime.id,
            sequence_number=seq,
            previous_status=curr_status.value,
            new_status=RuntimeStatus.PAUSED.value,
            actor=owner_id,
            reason_code="USER_PAUSED",
        )
        db.add(event)
        db.commit()
        db.refresh(runtime)
        return runtime

    @staticmethod
    def resume_runtime(db: Session, runtime_id: str, owner_id: str) -> StrategyRuntime:
        return PaperService.start_runtime(db, runtime_id, owner_id)

    @staticmethod
    def stop_runtime(db: Session, runtime_id: str, owner_id: str) -> StrategyRuntime:
        runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == runtime_id, StrategyRuntime.owner_id == owner_id).first()
        if not runtime:
            raise ResourceNotFoundError(f"Runtime '{runtime_id}' not found.")

        curr_status = RuntimeStatus(runtime.status)
        validate_runtime_transition(curr_status, RuntimeStatus.STOPPED, actor=owner_id, reason_code="USER_STOPPED")
        runtime.status = RuntimeStatus.STOPPED.value

        # Cancel all open orders for this runtime
        open_orders = db.query(Order).filter(
            Order.runtime_id == runtime.id,
            Order.status.in_([OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value])
        ).all()

        for o in open_orders:
            PaperService._cancel_order_internal(db, o, actor=owner_id, reason="RUNTIME_STOPPED")

        seq = db.query(func.coalesce(func.max(RuntimeEvent.sequence_number), 0)).filter(RuntimeEvent.runtime_id == runtime.id).scalar() + 1
        event = RuntimeEvent(
            runtime_id=runtime.id,
            sequence_number=seq,
            previous_status=curr_status.value,
            new_status=RuntimeStatus.STOPPED.value,
            actor=owner_id,
            reason_code="USER_STOPPED",
        )
        db.add(event)
        db.commit()
        db.refresh(runtime)
        return runtime

    @staticmethod
    def cancel_order(db: Session, order_id: str, owner_id: str, reason: str = "USER_CANCELLED") -> Order:
        order = db.query(Order).filter(Order.id == order_id, Order.owner_id == owner_id).first()
        if not order:
            raise ResourceNotFoundError(f"Order '{order_id}' not found.")
        return PaperService._cancel_order_internal(db, order, actor=owner_id, reason=reason)

    @staticmethod
    def _cancel_order_internal(db: Session, order: Order, actor: str, reason: str) -> Order:
        curr_status = OrderStatus(order.status)
        validate_order_transition(curr_status, OrderStatus.CANCEL_PENDING, actor=actor, reason_code="CANCEL_REQUESTED")
        order.status = OrderStatus.CANCEL_PENDING.value

        # Sequence number for order event
        seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
        evt1 = OrderEvent(
            order_id=order.id,
            sequence_number=seq,
            previous_status=curr_status.value,
            new_status=OrderStatus.CANCEL_PENDING.value,
            actor=actor,
            reason_code="CANCEL_REQUESTED",
        )
        db.add(evt1)
        db.flush()

        # Immediate paper broker confirmation
        validate_order_transition(OrderStatus.CANCEL_PENDING, OrderStatus.CANCELLED, actor="PAPER_BROKER", reason_code=reason)
        order.status = OrderStatus.CANCELLED.value

        evt2 = OrderEvent(
            order_id=order.id,
            sequence_number=seq + 1,
            previous_status=OrderStatus.CANCEL_PENDING.value,
            new_status=OrderStatus.CANCELLED.value,
            actor="PAPER_BROKER",
            reason_code=reason,
        )
        db.add(evt2)

        # Release cash reservation if BUY order
        if order.side == OrderSide.BUY.value:
            acct = db.query(PaperAccount).filter(PaperAccount.id == order.account_id).with_for_update().first()
            if acct:
                # Calculate remaining reserved cash to release
                remaining_qty = order.quantity_units - order.filled_quantity_units
                price = order.limit_price_units or 0
                fee = (remaining_qty * price * 5) // 10000
                release_amount = (remaining_qty * price) + fee
                release_amount = min(release_amount, acct.reserved_cash_units)

                acct.reserved_cash_units = max(0, acct.reserved_cash_units - release_amount)

                # Ledger entry with explicit bucket deltas
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
                    reason_code=f"ORDER_CANCELLED:{reason}",
                    idempotency_key=f"cancel_release:{order.id}:{l_seq}",
                )
                db.add(ledger)

        db.commit()
        db.refresh(order)
        return order

    # --- Candle Stepping & Simulation Execution ---

    @staticmethod
    def step_runtime(db: Session, runtime_id: str, owner_id: str, step_count: int = 1) -> Dict[str, Any]:
        runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == runtime_id, StrategyRuntime.owner_id == owner_id).first()
        if not runtime:
            raise ResourceNotFoundError(f"Runtime '{runtime_id}' not found.")
        if runtime.status != RuntimeStatus.RUNNING.value:
            raise ValueError(f"Cannot step runtime in status '{runtime.status}'. Must be RUNNING.")

        # Execute strictly from frozen snapshots (Item 3)
        action_policy_snapshot = runtime.action_policy_snapshot or {}
        risk_policy_snapshot = runtime.risk_policy_snapshot or {}
        strategy_snapshot = runtime.strategy_snapshot or {}
        inst_spec_snapshot = runtime.instrument_spec_snapshot or {}

        if inst_spec_snapshot:
            inst_spec = InstrumentSpec(**inst_spec_snapshot)
        else:
            inst_id = action_policy_snapshot.get("entry_mapping", {}).get("instrument_id", "synthetic_candidate_option_pe_23000_15m")
            inst_spec = get_instrument_spec(inst_id)
            if not inst_spec:
                raise ValueError(f"Unknown instrument '{inst_id}'")

        # Explicit separation of reference dataset (rules/signals) vs execution dataset (fills/volume) (Item 2)
        ref_dataset_id = getattr(inst_spec, "reference_dataset_id", "synthetic_underlying_nifty_15m")
        exec_dataset_id = getattr(inst_spec, "execution_dataset_id", inst_spec.dataset_id)

        # Load reference candles (for strategy rules and indicators)
        ref_candles = load_dataset_candles(ref_dataset_id)
        if not ref_candles:
            raise ValueError(f"No candle data found for reference dataset '{ref_dataset_id}'.")

        # Load execution candles (for order fills and volume)
        exec_candles = load_dataset_candles(exec_dataset_id)
        if not exec_candles:
            raise ValueError(f"No candle data found for execution dataset '{exec_dataset_id}'.")

        exec_candles_by_ts = {c.timestamp: c for c in exec_candles}

        # Find starting candle index based on last_processed_candle_timestamp on reference series
        start_idx = 0
        if runtime.last_processed_candle_timestamp:
            for i, c in enumerate(ref_candles):
                if c.timestamp > runtime.last_processed_candle_timestamp:
                    start_idx = i
                    break
            else:
                # End of dataset reached: transition to COMPLETED (Item 10)
                curr_status = RuntimeStatus(runtime.status)
                validate_runtime_transition(curr_status, RuntimeStatus.COMPLETED, actor="SYSTEM_CLOCK", reason_code="END_OF_DATASET")
                runtime.status = RuntimeStatus.COMPLETED.value
                seq = db.query(func.coalesce(func.max(RuntimeEvent.sequence_number), 0)).filter(RuntimeEvent.runtime_id == runtime.id).scalar() + 1
                event = RuntimeEvent(
                    runtime_id=runtime.id,
                    sequence_number=seq,
                    previous_status=curr_status.value,
                    new_status=RuntimeStatus.COMPLETED.value,
                    actor="SYSTEM_CLOCK",
                    reason_code="END_OF_DATASET",
                )
                db.add(event)
                db.commit()
                db.refresh(runtime)
                return {"status": "COMPLETED", "message": "End of dataset reached.", "steps_executed": 0}

        steps_executed = 0
        fills_executed = 0
        intents_created = 0

        try:
            for i in range(start_idx, min(start_idx + step_count, len(ref_candles))):
                ref_candle = ref_candles[i]
                exec_candle = exec_candles_by_ts.get(ref_candle.timestamp)
                if not exec_candle:
                    raise ValueError(f"Execution candle missing for instrument '{inst_spec.instrument_id}' at timestamp {ref_candle.timestamp}")

                # Convert execution candle prices & volume to scaled units for order fills
                c_open_units = decimal_to_units(str(exec_candle.open), inst_spec.price_scale)
                c_high_units = decimal_to_units(str(exec_candle.high), inst_spec.price_scale)
                c_low_units = decimal_to_units(str(exec_candle.low), inst_spec.price_scale)
                c_close_units = decimal_to_units(str(exec_candle.close), inst_spec.price_scale)
                c_vol_units = int(exec_candle.volume)

                # 1. First, process open orders against current execution candle open/high/low/close
                open_orders = db.query(Order).filter(
                    Order.runtime_id == runtime.id,
                    Order.status.in_([OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value])
                ).all()

                if open_orders:
                    intent_ids = [o.intent_id for o in open_orders if o.intent_id]
                    intent_keys = dict(
                        db.query(OrderIntent.id, OrderIntent.trigger_event_key)
                        .filter(OrderIntent.id.in_(intent_ids))
                        .all()
                    ) if intent_ids else {}

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
                            "intent_trigger_key": intent_keys.get(o.intent_id, str(o.order_sequence_number)),
                        }
                        for o in open_orders
                    ]

                    exec_summary = DeterministicFillEngine.calculate_order_fills(
                        open_orders=orders_payload,
                        instrument_spec=inst_spec,
                        candle_open_units=c_open_units,
                        candle_high_units=c_high_units,
                        candle_low_units=c_low_units,
                        candle_close_units=c_close_units,
                        candle_volume_units=c_vol_units,
                        candle_timestamp=exec_candle.timestamp,
                    )

                    for fill in exec_summary.fills:
                        fills_executed += 1
                        PaperService._apply_fill(db, runtime, fill, inst_spec)

                # 2. Check DAY orders expiration if end of day session
                PaperService._check_order_expirations(db, runtime, exec_candle.timestamp)

                # 3. Evaluate Strategy rules on closed reference candle using frozen strategy snapshot
                evaluator = RuleEvaluator()
                rule_result = evaluator.evaluate_strategy_rules(
                    strategy_payload=strategy_snapshot,
                    reference_candles=ref_candles[:i+1],
                    eval_timestamp=ref_candle.timestamp,
                )

                # 4. Generate OrderIntent if action mapping triggered
                entry_map = action_policy_snapshot.get("entry_mapping")
                if entry_map and rule_result.overall_status.value == entry_map.get("trigger_status", "ON_TRUE").replace("ON_", ""):
                    # Triggered!
                    intent = PaperService._process_action_trigger(
                        db,
                        runtime=runtime,
                        action_mapping=entry_map,
                        inst_spec=inst_spec,
                        candle_timestamp=exec_candle.timestamp,
                        eval_close_units=c_close_units,
                        strat_payload=strategy_snapshot,
                        risk_policy_payload=risk_policy_snapshot,
                    )
                    if intent:
                        intents_created += 1

                # 5. Mark positions to market using execution candle close
                PaperService._mark_positions_to_market(db, runtime.account_id, inst_spec.instrument_id, c_close_units)

                runtime.last_processed_candle_timestamp = ref_candle.timestamp
                steps_executed += 1

            db.commit()
            db.refresh(runtime)
        except Exception:
            db.rollback()
            raise

        return {
            "runtime_id": runtime.id,
            "status": runtime.status,
            "last_candle_timestamp": runtime.last_processed_candle_timestamp,
            "steps_executed": steps_executed,
            "fills_executed": fills_executed,
            "intents_created": intents_created,
        }

    @staticmethod
    def _apply_fill(db: Session, runtime: StrategyRuntime, fill: FillResult, inst_spec: InstrumentSpec) -> None:
        order = db.query(Order).filter(Order.id == fill.order_id).with_for_update().first()
        acct = db.query(PaperAccount).filter(PaperAccount.id == order.account_id).with_for_update().first()
        pos = db.query(PaperPosition).filter(
            PaperPosition.account_id == acct.id,
            PaperPosition.instrument_id == inst_spec.instrument_id
        ).with_for_update().first()

        if not pos:
            pos = PaperPosition(
                owner_id=runtime.owner_id,
                account_id=acct.id,
                instrument_id=inst_spec.instrument_id,
                net_quantity_units=0,
                average_entry_price_units=0,
                cost_basis_units=0,
                gross_realized_pnl_units=0,
                total_fees_units=0,
                net_realized_pnl_units=0,
                last_mark_price_units=fill.fill_price_units,
                unrealized_pnl_units=0,
            )
            db.add(pos)
            db.flush()

        # 1. First validate position accounting & short prevention check BEFORE mutating anything (Item 6 & Item 4)
        pos_res = AccountingEngine.apply_fill_to_position(
            current_net_qty_units=pos.net_quantity_units,
            current_avg_price_units=pos.average_entry_price_units,
            fill_side=OrderSide(order.side),
            fill_qty_units=fill.fill_quantity_units,
            fill_price_units=fill.fill_price_units,
            fill_fee_units=fill.fee_units,
            allow_short=inst_spec.allow_short,
        )

        # 2. Update order status & filled quantity
        order.filled_quantity_units += fill.fill_quantity_units
        new_status = OrderStatus.FILLED.value if fill.is_full_fill else OrderStatus.PARTIALLY_FILLED.value
        curr_status = OrderStatus(order.status)
        validate_order_transition(curr_status, OrderStatus(new_status), actor="PAPER_BROKER", reason_code="FILL_EXECUTED")
        order.status = new_status

        # 3. Insert fill record with explicit UUID and owner consistency (Item 13)
        fill_id = str(uuid.uuid4())
        db_fill = Fill(
            id=fill_id,
            owner_id=runtime.owner_id,
            order_id=order.id,
            account_id=acct.id,
            instrument_id=inst_spec.instrument_id,
            side=order.side,
            quantity_units=fill.fill_quantity_units,
            price_units=fill.fill_price_units,
            fee_units=fill.fee_units,
            candle_timestamp=fill.candle_timestamp,
            fill_idempotency_key=fill.fill_idempotency_key,
        )
        db.add(db_fill)
        db.flush()

        # 4. Update order event
        seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
        evt = OrderEvent(
            order_id=order.id,
            sequence_number=seq,
            previous_status=curr_status.value,
            new_status=new_status,
            actor="PAPER_BROKER",
            reason_code="FILL_EXECUTED",
            metadata_json={"fill_qty": fill.fill_quantity_units, "fill_price": fill.fill_price_units, "fee": fill.fee_units},
        )
        db.add(evt)

        # 5. Apply validated position accounting updates
        pos.net_quantity_units = pos_res.new_net_quantity_units
        pos.average_entry_price_units = pos_res.new_average_entry_price_units
        pos.cost_basis_units = pos_res.new_cost_basis_units
        pos.gross_realized_pnl_units += pos_res.incremental_gross_realized_pnl_units
        pos.total_fees_units += pos_res.incremental_fees_units
        pos.net_realized_pnl_units += pos_res.incremental_net_realized_pnl_units

        # Cash Ledger accounting with unambiguous explicit deltas (Item 7)
        max_seq = db.query(func.coalesce(func.max(AccountLedgerEntry.sequence_number), 0)).filter(AccountLedgerEntry.account_id == acct.id).scalar()
        l_seq = max_seq + 1
        fill_cost = fill.fill_quantity_units * fill.fill_price_units

        if order.side == OrderSide.BUY.value:
            # Deduct cash & release reservation
            acct.total_cash_units -= (fill_cost + fill.fee_units)
            reserved_released = min(fill_cost + fill.fee_units, acct.reserved_cash_units)
            acct.reserved_cash_units -= reserved_released

            l1 = AccountLedgerEntry(
                account_id=acct.id,
                owner_id=runtime.owner_id,
                sequence_number=l_seq,
                entry_type=LedgerEntryType.BUY_FILL.value,
                amount_units=-(fill_cost + fill.fee_units),
                balance_after_units=acct.total_cash_units,
                settled_cash_delta_units=-(fill_cost + fill.fee_units),
                reserved_cash_delta_units=-reserved_released,
                settled_cash_after_units=acct.total_cash_units,
                reserved_cash_after_units=acct.reserved_cash_units,
                order_id=order.id,
                fill_id=db_fill.id,
                reason_code="BUY_FILL_EXECUTED",
                idempotency_key=f"buy_fill:{db_fill.id}",
            )
            db.add(l1)
        else:
            # SELL proceeds credited
            proceeds = fill_cost - fill.fee_units
            acct.total_cash_units += proceeds

            l1 = AccountLedgerEntry(
                account_id=acct.id,
                owner_id=runtime.owner_id,
                sequence_number=l_seq,
                entry_type=LedgerEntryType.SELL_FILL.value,
                amount_units=proceeds,
                balance_after_units=acct.total_cash_units,
                settled_cash_delta_units=proceeds,
                reserved_cash_delta_units=0,
                settled_cash_after_units=acct.total_cash_units,
                reserved_cash_after_units=acct.reserved_cash_units,
                order_id=order.id,
                fill_id=db_fill.id,
                reason_code="SELL_FILL_EXECUTED",
                idempotency_key=f"sell_fill:{db_fill.id}",
            )
            db.add(l1)
        db.flush()

    @staticmethod
    def _check_order_expirations(db: Session, runtime: StrategyRuntime, candle_timestamp: datetime) -> None:
        # Check DAY orders expiration if hour >= 15 and minute >= 30
        if candle_timestamp.hour > 15 or (candle_timestamp.hour == 15 and candle_timestamp.minute >= 30):
            day_orders = db.query(Order).join(OrderIntent, Order.intent_id == OrderIntent.id).filter(
                Order.runtime_id == runtime.id,
                Order.status.in_([OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value]),
                OrderIntent.time_in_force == TimeInForce.DAY.value
            ).all()

            for o in day_orders:
                curr_status = OrderStatus(o.status)
                validate_order_transition(curr_status, OrderStatus.EXPIRED, actor="SYSTEM_CLOCK", reason_code="EXPIRY_SESSION_CLOSE")
                o.status = OrderStatus.EXPIRED.value
                seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == o.id).scalar() + 1
                evt = OrderEvent(
                    order_id=o.id,
                    sequence_number=seq,
                    previous_status=curr_status.value,
                    new_status=OrderStatus.EXPIRED.value,
                    actor="SYSTEM_CLOCK",
                    reason_code="EXPIRY_SESSION_CLOSE",
                )
                db.add(evt)

                # Release cash reservation if BUY order
                if o.side == OrderSide.BUY.value:
                    acct = db.query(PaperAccount).filter(PaperAccount.id == o.account_id).with_for_update().first()
                    if acct:
                        remaining_qty = o.quantity_units - o.filled_quantity_units
                        price = o.limit_price_units or 0
                        fee = (remaining_qty * price * 5) // 10000
                        release_amount = (remaining_qty * price) + fee
                        release_amount = min(release_amount, acct.reserved_cash_units)
                        acct.reserved_cash_units = max(0, acct.reserved_cash_units - release_amount)

                        l_seq = db.query(func.coalesce(func.max(AccountLedgerEntry.sequence_number), 0)).filter(AccountLedgerEntry.account_id == acct.id).scalar() + 1
                        ledger = AccountLedgerEntry(
                            account_id=acct.id,
                            owner_id=o.owner_id,
                            sequence_number=l_seq,
                            entry_type=LedgerEntryType.RESERVATION_RELEASE.value,
                            amount_units=release_amount,
                            balance_after_units=acct.total_cash_units,
                            settled_cash_delta_units=0,
                            reserved_cash_delta_units=-release_amount,
                            settled_cash_after_units=acct.total_cash_units,
                            reserved_cash_after_units=acct.reserved_cash_units,
                            order_id=o.id,
                            reason_code="ORDER_EXPIRED:EXPIRY_SESSION_CLOSE",
                            idempotency_key=f"expire_release:{o.id}:{l_seq}",
                        )
                        db.add(ledger)

    @staticmethod
    def _process_action_trigger(
        db: Session,
        runtime: StrategyRuntime,
        action_mapping: Dict[str, Any],
        inst_spec: InstrumentSpec,
        candle_timestamp: datetime,
        eval_close_units: int,
        strat_payload: Dict[str, Any],
        risk_policy_payload: Dict[str, Any],
    ) -> Optional[OrderIntent]:
        qty_units = decimal_to_units(str(action_mapping.get("quantity", 50)), inst_spec.quantity_scale)
        side = OrderSide(action_mapping.get("side", "BUY"))
        order_type = OrderType(action_mapping.get("order_type", "MARKET"))
        intent_type = IntentType(action_mapping.get("intent_type", "ENTRY"))
        limit_price = action_mapping.get("limit_price")
        limit_price_units = decimal_to_units(str(limit_price), inst_spec.price_scale) if limit_price else None
        time_in_force = action_mapping.get("time_in_force", "DAY")

        # Canonical order intent identity computation (Item 5)
        fingerprint = (runtime.action_policy_snapshot or {}).get("fingerprint", str(runtime.version))
        trigger_key = compute_order_intent_identity(
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            runtime_snapshot_fingerprint=fingerprint,
            instrument_id=inst_spec.instrument_id,
            candle_timestamp=candle_timestamp,
            action_mapping_id=action_mapping.get("mapping_id", "entry_1"),
            side=side.value,
            position_effect=intent_type.value,
            quantity_units=qty_units,
            order_type=order_type.value,
            limit_price_units=limit_price_units,
            time_in_force=time_in_force,
        )

        # Idempotency check: see if intent already exists for this trigger event
        existing_intent = db.query(OrderIntent).filter(
            OrderIntent.runtime_id == runtime.id,
            OrderIntent.trigger_event_key == trigger_key
        ).first()
        if existing_intent:
            return existing_intent

        # Check reduce-only / existing position
        pos = db.query(PaperPosition).filter(
            PaperPosition.account_id == runtime.account_id,
            PaperPosition.instrument_id == inst_spec.instrument_id
        ).first()
        pos_qty = pos.net_quantity_units if pos else 0

        if intent_type in (IntentType.EXIT, IntentType.REDUCE):
            if pos_qty == 0:
                # Log ignored decision
                db.add(ActionDecision(
                    runtime_id=runtime.id,
                    candle_timestamp=candle_timestamp,
                    action_mapping_id=action_mapping.get("mapping_id", "entry_1"),
                    decision="IGNORED",
                    reason_code=ActionIgnoredReasonCode.ACTION_IGNORED_NO_POSITION_TO_REDUCE.value,
                ))
                return None
            qty_units = min(qty_units, abs(pos_qty))

        eval_fingerprint = compute_evaluation_fingerprint(
            strategy_payload=strat_payload,
            dataset_id=runtime.dataset_id,
            dataset_checksum=runtime.dataset_checksum or "mock_checksum",
            candle_timestamp=candle_timestamp,
        )

        intent = OrderIntent(
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            action_mapping_id=action_mapping.get("mapping_id", "entry_1"),
            requested_instrument_id=inst_spec.instrument_id,
            resolved_instrument_id=inst_spec.instrument_id,
            intent_type=intent_type.value,
            reduce_only=(intent_type in (IntentType.EXIT, IntentType.REDUCE)),
            side=side.value,
            quantity_units=qty_units,
            order_type=order_type.value,
            limit_price_units=limit_price_units,
            time_in_force=time_in_force,
            source_candle_timestamp=candle_timestamp,
            source_evaluation_fingerprint=eval_fingerprint,
            trigger_event_key=trigger_key,
        )
        db.add(intent)
        db.flush()

        # Run pre-trade risk evaluation using snapshot
        acct = db.query(PaperAccount).filter(PaperAccount.id == runtime.account_id).with_for_update().first()
        available_cash = acct.total_cash_units - acct.reserved_cash_units

        open_orders = db.query(Order).filter(
            Order.account_id == acct.id,
            Order.status.in_([OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value])
        ).all()
        open_orders_data = [
            {"instrument_id": o.instrument_id, "quantity_units": o.quantity_units, "filled_quantity_units": o.filled_quantity_units, "limit_price_units": o.limit_price_units}
            for o in open_orders
        ]

        positions_dict = {
            p.instrument_id: {"net_quantity_units": p.net_quantity_units, "last_mark_price_units": p.last_mark_price_units}
            for p in db.query(PaperPosition).filter(PaperPosition.account_id == acct.id).all()
        }

        # Daily trades & loss calculation
        trades_today = db.query(func.count(Order.id)).filter(
            Order.account_id == acct.id,
            Order.created_at >= candle_timestamp.replace(hour=0, minute=0, second=0)
        ).scalar()

        risk_res = PureRiskEngine.evaluate_pre_trade_risk(
            trading_mode=TradingMode(runtime.trading_mode),
            instrument_spec=inst_spec,
            side=side,
            order_type=order_type,
            quantity_units=qty_units,
            limit_price_units=limit_price_units,
            reference_price_units=eval_close_units,
            price_timestamp=candle_timestamp,
            current_time=candle_timestamp,
            risk_policy=risk_policy_payload,
            available_cash_units=available_cash,
            open_orders=open_orders_data,
            current_positions=positions_dict,
            daily_trades_count=trades_today,
            daily_realized_loss_units=0,
            kill_switch_active=False,
        )

        # Record risk decision
        risk_dec = RiskDecision(
            intent_id=intent.id,
            owner_id=intent.owner_id,
            passed=risk_res.passed,
            reason_code=risk_res.reason_code.value,
            message=risk_res.message,
            metrics_json=risk_res.metrics,
        )
        db.add(risk_dec)

        if not risk_res.passed:
            # Create order in RISK_REJECTED
            o_seq = db.query(func.coalesce(func.max(Order.order_sequence_number), 0)).filter(Order.runtime_id == runtime.id).scalar() + 1
            order = Order(
                owner_id=runtime.owner_id,
                runtime_id=runtime.id,
                intent_id=intent.id,
                account_id=acct.id,
                order_sequence_number=o_seq,
                instrument_id=inst_spec.instrument_id,
                side=side.value,
                order_type=order_type.value,
                quantity_units=qty_units,
                limit_price_units=limit_price_units,
                filled_quantity_units=0,
                status=OrderStatus.RISK_REJECTED.value,
            )
            db.add(order)
            return intent

        # Passed risk -> Create order in ACCEPTED & reserve cash if BUY
        o_seq = db.query(func.coalesce(func.max(Order.order_sequence_number), 0)).filter(Order.runtime_id == runtime.id).scalar() + 1
        order = Order(
            owner_id=runtime.owner_id,
            runtime_id=runtime.id,
            intent_id=intent.id,
            account_id=acct.id,
            order_sequence_number=o_seq,
            instrument_id=inst_spec.instrument_id,
            side=side.value,
            order_type=order_type.value,
            quantity_units=qty_units,
            limit_price_units=limit_price_units,
            filled_quantity_units=0,
            status=OrderStatus.ACCEPTED.value,
        )
        db.add(order)
        db.flush()

        # Record CREATED -> ACCEPTED event
        evt = OrderEvent(
            order_id=order.id,
            sequence_number=1,
            previous_status=OrderStatus.CREATED.value,
            new_status=OrderStatus.ACCEPTED.value,
            actor="SYSTEM_OMS",
            reason_code="RISK_CHECK_PASSED",
        )
        db.add(evt)

        # If BUY: Reserve cash with unambiguous delta accounting
        if side == OrderSide.BUY:
            price = limit_price_units or eval_close_units
            fee = (qty_units * price * 5) // 10000 + 2000
            reserve_amount = (qty_units * price) + fee
            acct.reserved_cash_units += reserve_amount

            max_seq = db.query(func.coalesce(func.max(AccountLedgerEntry.sequence_number), 0)).filter(AccountLedgerEntry.account_id == acct.id).scalar()
            l_seq = max_seq + 1
            ledger = AccountLedgerEntry(
                account_id=acct.id,
                owner_id=runtime.owner_id,
                sequence_number=l_seq,
                entry_type=LedgerEntryType.CASH_RESERVATION.value,
                amount_units=reserve_amount,
                balance_after_units=acct.total_cash_units,
                settled_cash_delta_units=0,
                reserved_cash_delta_units=reserve_amount,
                settled_cash_after_units=acct.total_cash_units,
                reserved_cash_after_units=acct.reserved_cash_units,
                order_id=order.id,
                reason_code="BUY_ORDER_RESERVED",
                idempotency_key=f"reserve:{order.id}:{l_seq}",
            )
            db.add(ledger)
            db.flush()

        return intent

    @staticmethod
    def _mark_positions_to_market(db: Session, account_id: str, instrument_id: str, current_price_units: int) -> None:
        pos = db.query(PaperPosition).filter(
            PaperPosition.account_id == account_id,
            PaperPosition.instrument_id == instrument_id
        ).first()
        if pos and pos.net_quantity_units != 0:
            pos.last_mark_price_units = current_price_units
            pos.unrealized_pnl_units = AccountingEngine.calculate_unrealized_pnl(
                net_quantity_units=pos.net_quantity_units,
                average_entry_price_units=pos.average_entry_price_units,
                last_mark_price_units=current_price_units,
            )

    # --- Kill Switch Management ---

    @staticmethod
    def get_kill_switch_status(db: Session, user_id: str) -> Dict[str, Any]:
        global_ks = db.query(KillSwitch).filter(KillSwitch.target_key == "GLOBAL").first()
        user_ks = db.query(KillSwitch).filter(KillSwitch.target_key == f"USER:{user_id}").first()

        return {
            "global_active": bool(global_ks.is_active) if global_ks else False,
            "global_engaged_at": global_ks.engaged_at if global_ks else None,
            "global_reason": global_ks.reason if global_ks else None,
            "user_active": bool(user_ks.is_active) if user_ks else False,
            "user_engaged_at": user_ks.engaged_at if user_ks else None,
            "user_reason": user_ks.reason if user_ks else None,
        }

    @staticmethod
    def engage_kill_switch(db: Session, scope: str, user_id: Optional[str], actor_id: str, reason: str, batch_size: int = 50) -> KillSwitch:
        now_utc = datetime.now(timezone.utc)
        target_key = "GLOBAL" if scope == "GLOBAL" else f"USER:{user_id}"

        ks = db.query(KillSwitch).filter(KillSwitch.target_key == target_key).first()
        if not ks:
            ks = KillSwitch(
                target_key=target_key,
                scope=scope,
                user_id=user_id if scope == "USER" else None,
                is_active=True,
                engaged_by=actor_id,
                engaged_at=now_utc,
                reason=reason,
            )
            db.add(ks)
        else:
            ks.is_active = True
            ks.engaged_by = actor_id
            ks.engaged_at = now_utc
            ks.reason = reason

        # Persist switch activation state first before bounded batch processing (Item 11)
        db.flush()

        if scope == "GLOBAL":
            # Halt all active runtimes in bounded batches
            while True:
                runtimes = db.query(StrategyRuntime).filter(
                    StrategyRuntime.status.in_([RuntimeStatus.RUNNING.value, RuntimeStatus.PAUSED.value])
                ).limit(batch_size).all()
                if not runtimes:
                    break
                for r in runtimes:
                    r.status = RuntimeStatus.HALTED.value
                db.flush()

            # Cancel open orders in bounded batches
            while True:
                open_orders = db.query(Order).filter(
                    Order.status.in_([OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value])
                ).limit(batch_size).all()
                if not open_orders:
                    break
                for o in open_orders:
                    PaperService._cancel_order_internal(db, o, actor="GLOBAL_KILL_SWITCH", reason=reason)

        elif scope == "USER":
            if not user_id:
                raise ValueError("user_id is required for USER scoped kill switch.")

            while True:
                runtimes = db.query(StrategyRuntime).filter(
                    StrategyRuntime.owner_id == user_id,
                    StrategyRuntime.status.in_([RuntimeStatus.RUNNING.value, RuntimeStatus.PAUSED.value])
                ).limit(batch_size).all()
                if not runtimes:
                    break
                for r in runtimes:
                    r.status = RuntimeStatus.HALTED.value
                db.flush()

            while True:
                open_orders = db.query(Order).filter(
                    Order.owner_id == user_id,
                    Order.status.in_([OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value])
                ).limit(batch_size).all()
                if not open_orders:
                    break
                for o in open_orders:
                    PaperService._cancel_order_internal(db, o, actor="USER_KILL_SWITCH", reason=reason)

        db.commit()
        db.refresh(ks)
        return ks

    @staticmethod
    def reset_kill_switch(db: Session, scope: str, user_id: Optional[str], actor_id: str, reason: str) -> KillSwitch:
        now_utc = datetime.now(timezone.utc)
        target_key = "GLOBAL" if scope == "GLOBAL" else f"USER:{user_id}"

        # Reject reset if cancellation processing is in-flight (Item 11)
        pending_query = db.query(Order).filter(Order.status == OrderStatus.CANCEL_PENDING.value)
        if scope == "USER":
            pending_query = pending_query.filter(Order.owner_id == user_id)
        if pending_query.count() > 0:
            raise ValueError("Cannot reset kill switch while cancellations are still pending.")

        ks = db.query(KillSwitch).filter(KillSwitch.target_key == target_key).first()
        if ks:
            ks.is_active = False
            ks.reason = f"Reset by {actor_id}: {reason}"
            ks.engaged_at = now_utc
            db.commit()
            db.refresh(ks)
        return ks
