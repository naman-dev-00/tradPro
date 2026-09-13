import json
import hashlib
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone
from decimal import Decimal
from src.engine.paper.models import (
    TradingMode,
    IntentType,
    OrderSide,
    OrderType,
    TimeInForce,
    OrderStatus,
    RuntimeStatus,
    RiskReasonCode,
    ActionIgnoredReasonCode,
    InstrumentSpec,
    get_instrument_spec,
)
from src.engine.paper.units import decimal_to_units, units_to_decimal
from src.engine.paper.state_machine import validate_order_transition, validate_runtime_transition
from src.engine.paper.risk_engine import PureRiskEngine, RiskCheckResult
from src.engine.paper.fill_model import DeterministicFillEngine, CandleExecutionSummary, FillResult
from src.engine.paper.accounting import AccountingEngine, PositionUpdateResult

def canonicalize_json(data: Any) -> str:
    """Produces sorted, deterministic canonical JSON string."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)

def compute_order_intent_identity(
    owner_id: str,
    runtime_id: str,
    runtime_snapshot_fingerprint: str,
    instrument_id: str,
    candle_timestamp: datetime,
    action_mapping_id: str,
    side: str,
    position_effect: str,
    quantity_units: int,
    order_type: str,
    time_in_force: str,
    limit_price_units: Optional[int] = None,
) -> str:
    """
    Computes deterministic canonical order-intent identity from all material order fields:
    Owner, Runtime, Runtime snapshot fingerprint, Instrument, Normalized UTC candle timestamp,
    Rule/action identity, Side, Position effect, Quantity, Order type, Limit price, Time in force.
    """
    if candle_timestamp.tzinfo is None:
        utc_ts = candle_timestamp.replace(tzinfo=timezone.utc).isoformat()
    else:
        utc_ts = candle_timestamp.astimezone(timezone.utc).isoformat()

    raw = {
        "owner_id": owner_id,
        "runtime_id": runtime_id,
        "runtime_snapshot_fingerprint": runtime_snapshot_fingerprint,
        "instrument_id": instrument_id,
        "candle_timestamp": utc_ts,
        "action_mapping_id": action_mapping_id,
        "side": side,
        "position_effect": position_effect,
        "quantity_units": quantity_units,
        "order_type": order_type,
        "limit_price_units": limit_price_units,
        "time_in_force": time_in_force,
    }
    return hashlib.sha256(canonicalize_json(raw).encode("utf-8")).hexdigest()

def compute_trigger_event_key(
    runtime_id: str,
    config_version: int,
    candle_timestamp: datetime,
    instrument_id: str,
    action_mapping_id: str,
    intent_type: str,
    trigger_occurrence: int = 1,
    owner_id: Optional[str] = None,
    side: Optional[str] = None,
    quantity_units: Optional[int] = None,
    order_type: Optional[str] = None,
    limit_price_units: Optional[int] = None,
    time_in_force: Optional[str] = None,
    snapshot_fingerprint: Optional[str] = None,
) -> str:
    if candle_timestamp.tzinfo is None:
        utc_ts = candle_timestamp.replace(tzinfo=timezone.utc).isoformat()
    else:
        utc_ts = candle_timestamp.astimezone(timezone.utc).isoformat()

    raw = {
        "owner_id": owner_id or "default_owner",
        "runtime_id": runtime_id,
        "runtime_snapshot_fingerprint": snapshot_fingerprint or f"v_{config_version}",
        "instrument_id": instrument_id,
        "candle_timestamp": utc_ts,
        "action_mapping_id": action_mapping_id,
        "side": side or "BUY",
        "position_effect": intent_type,
        "quantity_units": quantity_units if quantity_units is not None else 50,
        "order_type": order_type or "MARKET",
        "limit_price_units": limit_price_units,
        "time_in_force": time_in_force or "DAY",
        "trigger_occurrence": trigger_occurrence,
    }
    return hashlib.sha256(canonicalize_json(raw).encode("utf-8")).hexdigest()

def compute_evaluation_fingerprint(
    strategy_payload: Dict[str, Any],
    dataset_id: str,
    dataset_checksum: str,
    candle_timestamp: datetime,
    engine_version: str = "1.0.0"
) -> str:
    raw = {
        "strategy": strategy_payload,
        "dataset_id": dataset_id,
        "dataset_checksum": dataset_checksum,
        "candle_timestamp": candle_timestamp.isoformat(),
        "engine_version": engine_version,
    }
    return hashlib.sha256(canonicalize_json(raw).encode("utf-8")).hexdigest()
