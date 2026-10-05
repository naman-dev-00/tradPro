"""Pre-intent paper action planning. Caller holds runtime/config/account locks."""
import datetime
from decimal import Decimal
from sqlalchemy import func, or_

from src.schemas import StrategyActionPolicyCreate
from src.models import Order, OrderIntent, PaperPosition, Fill, KillSwitch
from src.engine.paper.models import IntentType, OrderSide, TradingMode
from src.engine.paper.risk_engine import PureRiskEngine
from src.engine.paper.accounting import AccountingEngine
from .candle_source import scaled_units


def frozen_risk_policy(raw, spec):
    """Normalize the frozen public policy exactly, checking stored scaled fields."""
    policy = dict(raw)
    scales = {"max_quantity_per_order": spec.quantity_scale,
              "max_notional_per_order": spec.currency_scale,
              "max_instrument_exposure": spec.currency_scale,
              "max_total_exposure": spec.currency_scale,
              "max_daily_realized_loss": spec.currency_scale, "flat_fee": spec.currency_scale}
    for key, scale in scales.items():
        units_key = key + "_units"
        if key in raw:
            value = scaled_units(raw[key], scale)
            if units_key in raw and raw[units_key] != value:
                raise ValueError("Inconsistent frozen risk units")
            policy[units_key] = value
        if type(policy.get(units_key)) is not int or policy[units_key] < (0 if key == "flat_fee" else 1):
            raise ValueError("Missing or invalid frozen risk limit")
    for key in ("max_open_orders", "max_open_positions", "max_trades_per_day", "max_price_staleness_seconds", "fee_basis_points"):
        if type(policy.get(key)) is not int or policy[key] < (0 if key == "fee_basis_points" else 1):
            raise ValueError("Missing or invalid frozen risk limit")
    return policy


def _daily_loss(db, account, boundary):
    # Replay-time realized P&L, reconstructed from immutable fills rather than
    # operational timestamps or a mutable all-time position total.
    positions = {}
    realized = 0
    start = boundary.replace(hour=0, minute=0, second=0, microsecond=0)
    fills = db.query(Fill).filter(Fill.account_id == account.id, Fill.owner_id == account.owner_id,
        Fill.candle_timestamp <= boundary).order_by(Fill.candle_timestamp, Fill.created_at, Fill.id).all()
    for fill in fills:
        qty, avg = positions.get(fill.instrument_id, (0, 0))
        result = AccountingEngine.apply_fill_to_position(current_net_qty_units=qty, current_avg_price_units=avg,
            fill_side=OrderSide(fill.side), fill_qty_units=fill.quantity_units,
            fill_price_units=fill.price_units, fill_fee_units=fill.fee_units, allow_short=False)
        positions[fill.instrument_id] = (result.new_net_quantity_units, result.new_average_entry_price_units)
        if fill.candle_timestamp >= start:
            realized += result.incremental_net_realized_pnl_units
    return max(0, -realized)


def plan_actions(db, runtime, account, evaluation, spec, action_raw, risk_raw, candle, rule_results):
    def rejected(mapping_id, reason, risk="NOT_RUN"):
        return {"mapping_id": mapping_id, "accepted": False, "reason": reason, "risk_outcome": risk}
    try:
        policy = StrategyActionPolicyCreate.model_validate({
            "strategy_id": runtime.strategy_id, "name": "Frozen policy", **action_raw})
        mappings = [m for m in (policy.entry_mapping, policy.exit_mapping) if m is not None]
        if len({m.mapping_id for m in mappings}) != len(mappings):
            raise ValueError("Duplicate mapping identity")
        risk = frozen_risk_policy(risk_raw, spec)
        for raw in (action_raw.get("entry_mapping"), action_raw.get("exit_mapping")):
            if raw and any(isinstance(raw.get(key), (float, bool)) for key in ("quantity", "limit_price")):
                raise ValueError("Inexact mapping number")
        # Validate all conversions before approving any action.
        converted = {m.mapping_id: (scaled_units(m.quantity, spec.quantity_scale),
            scaled_units(m.limit_price, spec.price_scale) if m.limit_price is not None else None) for m in mappings}
    except (ValueError, TypeError, OverflowError):
        return [rejected("invalid_policy", "INVALID_ACTION_OR_RISK_POLICY")]
    # Manifest instrument IDs and orderable catalog IDs use distinct namespaces;
    # the frozen execution dataset is their authoritative association.
    if candle is None or candle.dataset_id != spec.execution_dataset_id:
        return [rejected(m.mapping_id, "EXECUTION_INSTRUMENT_MISMATCH") for m in mappings]
    db.flush()
    positions = {p.instrument_id: {"net_quantity_units": p.net_quantity_units, "last_mark_price_units": p.last_mark_price_units}
        for p in db.query(PaperPosition).filter(PaperPosition.account_id == account.id,
            PaperPosition.owner_id == account.owner_id).with_for_update().all()}
    open_orders = [{"instrument_id": o.instrument_id, "quantity_units": o.quantity_units,
        "filled_quantity_units": o.filled_quantity_units, "limit_price_units": o.limit_price_units, "side": o.side}
        for o in db.query(Order).filter(Order.account_id == account.id, Order.owner_id == account.owner_id,
            Order.status.in_(["CREATED", "ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING"]))
            .order_by(Order.id).with_for_update().all()]
    available = account.total_cash_units - account.reserved_cash_units
    boundary = evaluation.close_at
    day_start = boundary.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + datetime.timedelta(days=1)
    daily_intents = db.query(OrderIntent).join(Order, Order.intent_id == OrderIntent.id).filter(
        Order.account_id == account.id, Order.owner_id == account.owner_id,
        OrderIntent.source_candle_timestamp >= day_start, OrderIntent.source_candle_timestamp < day_end).all()
    daily_trades = len(daily_intents)
    entries = sum(i.runtime_id == runtime.id and i.intent_type == "ENTRY" for i in daily_intents)
    daily_loss = _daily_loss(db, account, boundary)
    kill_active = db.query(KillSwitch.id).filter(KillSwitch.is_active.is_(True), or_(
        KillSwitch.scope == "GLOBAL", (KillSwitch.scope == "USER") & (KillSwitch.user_id == runtime.owner_id))).first() is not None
    results = []
    # Stable mapping-ID ordering applies provisional limits to both contract slots.
    for mapping in sorted(mappings, key=lambda m: m.mapping_id):
        m_id = mapping.mapping_id
        status = rule_results.get(mapping.rule_target)
        if status != mapping.trigger_status.value.removeprefix("ON_"):
            results.append(rejected(m_id, "RULE_CONDITION_NOT_MET"))
            continue
        if mapping.instrument_id != spec.instrument_id:
            results.append(rejected(m_id, "RISK_INSTRUMENT_NOT_ALLOWED", "REJECTED"))
            continue
        qty, limit_px = converted[m_id]
        previous = db.query(func.max(OrderIntent.source_candle_timestamp)).filter(
            OrderIntent.owner_id == runtime.owner_id, OrderIntent.runtime_id == runtime.id,
            OrderIntent.action_mapping_id == m_id).scalar()
        from .models import TIMEFRAME_SECONDS
        if previous is not None and boundary <= previous + datetime.timedelta(seconds=mapping.cooldown_bars * TIMEFRAME_SECONDS[runtime.timeframe]):
            results.append(rejected(m_id, "ACTION_IGNORED_COOLDOWN_ACTIVE"))
            continue
        holding = positions.get(spec.instrument_id, {}).get("net_quantity_units", 0)
        pending_sell = sum(o["quantity_units"] - o["filled_quantity_units"] for o in open_orders
            if o["instrument_id"] == spec.instrument_id and o["side"] == "SELL")
        pending_buy = any(o["instrument_id"] == spec.instrument_id and o["side"] == "BUY" for o in open_orders)
        if mapping.intent_type in (IntentType.EXIT, IntentType.REDUCE):
            if mapping.side != OrderSide.SELL or holding - pending_sell <= 0:
                results.append(rejected(m_id, "ACTION_IGNORED_NO_POSITION_TO_REDUCE"))
                continue
            qty = min(qty, holding - pending_sell)
        elif mapping.intent_type == IntentType.REVERSE:
            results.append(rejected(m_id, "RISK_INSTRUMENT_NOT_ALLOWED", "REJECTED"))
            continue
        elif holding or pending_buy:
            if policy.position_exists_behavior.value != "SCALE":
                reason = "ACTION_IGNORED_POSITION_EXISTS" if policy.position_exists_behavior.value == "IGNORE" else "POSITION_RULE_REJECTED"
                results.append(rejected(m_id, reason))
                continue
        if mapping.intent_type == IntentType.ENTRY and entries >= policy.max_entries_per_day:
            results.append(rejected(m_id, "MAX_ENTRIES_PER_DAY_EXCEEDED"))
            continue
        risk_positions = {key: dict(value) for key, value in positions.items()}
        if spec.instrument_id in risk_positions and mapping.side == OrderSide.SELL:
            risk_positions[spec.instrument_id]["net_quantity_units"] -= pending_sell
        result = PureRiskEngine.evaluate_pre_trade_risk(trading_mode=TradingMode.PAPER, instrument_spec=spec,
            side=mapping.side, order_type=mapping.order_type, quantity_units=qty, limit_price_units=limit_px,
            reference_price_units=candle.close_units, price_timestamp=candle.close_at, current_time=boundary,
            risk_policy=risk, available_cash_units=available, open_orders=open_orders, current_positions=risk_positions,
            daily_trades_count=daily_trades, daily_realized_loss_units=daily_loss, kill_switch_active=kill_active)
        if not result.passed:
            results.append(rejected(m_id, result.reason_code.value, "REJECTED"))
            continue
        price = limit_px if limit_px is not None else candle.close_units
        cost = qty * price + qty * price * risk["fee_basis_points"] // 10000 + risk["flat_fee_units"] if mapping.side == OrderSide.BUY else 0
        available -= cost
        daily_trades += 1
        entries += mapping.intent_type == IntentType.ENTRY
        open_orders.append({"instrument_id": spec.instrument_id, "quantity_units": qty, "filled_quantity_units": 0,
                            "limit_price_units": price, "side": mapping.side.value})
        results.append({"mapping_id": m_id, "accepted": True, "reason": result.reason_code.value,
            "risk_outcome": "ACCEPTED", "risk_result": result, "side": mapping.side.value, "qty": qty,
            "order_type": mapping.order_type.value, "limit_price": limit_px, "cost": cost,
            "intent_type": mapping.intent_type.value, "time_in_force": mapping.time_in_force.value})
    return results
