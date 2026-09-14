from enum import Enum
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field, ConfigDict

class TradingMode(str, Enum):
    PAPER = "PAPER"
    BROKER_SANDBOX = "BROKER_SANDBOX"
    BROKER_SANDBOX_RECORDED_FIXTURE = "BROKER_SANDBOX_RECORDED_FIXTURE"
    LIVE = "LIVE"

class IntentType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    REDUCE = "REDUCE"
    REVERSE = "REVERSE"

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"

class TimeInForce(str, Enum):
    DAY = "DAY"
    GTC = "GTC"
    IOC = "IOC"

class OrderStatus(str, Enum):
    CREATED = "CREATED"
    ACCEPTED = "ACCEPTED"
    PENDING_SUBMISSION = "PENDING_SUBMISSION"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    EXPIRED = "EXPIRED"
    RISK_REJECTED = "RISK_REJECTED"
    ERROR = "ERROR"

class RuntimeStatus(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    HALTED = "HALTED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"

class KillSwitchScope(str, Enum):
    GLOBAL = "GLOBAL"
    USER = "USER"

class LedgerEntryType(str, Enum):
    INITIAL_DEPOSIT = "INITIAL_DEPOSIT"
    CASH_RESERVATION = "CASH_RESERVATION"
    RESERVATION_RELEASE = "RESERVATION_RELEASE"
    BUY_FILL = "BUY_FILL"
    SELL_FILL = "SELL_FILL"
    FEE = "FEE"
    ADJUSTMENT = "ADJUSTMENT"

class ActionTriggerCondition(str, Enum):
    ON_TRUE = "ON_TRUE"
    ON_FALSE = "ON_FALSE"

class PositionExistsBehavior(str, Enum):
    IGNORE = "IGNORE"
    REJECT = "REJECT"
    SCALE = "SCALE"
    REVERSE = "REVERSE"

class RiskReasonCode(str, Enum):
    RISK_OK = "RISK_OK"
    RISK_MODE_LIVE_FORBIDDEN = "RISK_MODE_LIVE_FORBIDDEN"
    RISK_INSTRUMENT_NOT_ALLOWED = "RISK_INSTRUMENT_NOT_ALLOWED"
    RISK_PRICE_MISSING = "RISK_PRICE_MISSING"
    RISK_PRICE_STALE = "RISK_PRICE_STALE"
    RISK_QTY_INVALID = "RISK_QTY_INVALID"
    RISK_QTY_EXCEEDED = "RISK_QTY_EXCEEDED"
    RISK_NOTIONAL_EXCEEDED = "RISK_NOTIONAL_EXCEEDED"
    RISK_MAX_ORDERS_EXCEEDED = "RISK_MAX_ORDERS_EXCEEDED"
    RISK_MAX_POSITIONS_EXCEEDED = "RISK_MAX_POSITIONS_EXCEEDED"
    RISK_INSTRUMENT_EXPOSURE_EXCEEDED = "RISK_INSTRUMENT_EXPOSURE_EXCEEDED"
    RISK_TOTAL_EXPOSURE_EXCEEDED = "RISK_TOTAL_EXPOSURE_EXCEEDED"
    RISK_DAILY_TRADES_EXCEEDED = "RISK_DAILY_TRADES_EXCEEDED"
    RISK_DAILY_LOSS_EXCEEDED = "RISK_DAILY_LOSS_EXCEEDED"
    RISK_KILL_SWITCH_ACTIVE = "RISK_KILL_SWITCH_ACTIVE"
    RISK_DUPLICATE_INTENT = "RISK_DUPLICATE_INTENT"
    RISK_INSUFFICIENT_AVAILABLE_CASH = "RISK_INSUFFICIENT_AVAILABLE_CASH"
    RISK_UNSUPPORTED_SANDBOX_ORDER_TYPE = "RISK_UNSUPPORTED_SANDBOX_ORDER_TYPE"

class ActionIgnoredReasonCode(str, Enum):
    ACTION_IGNORED_NO_POSITION_TO_REDUCE = "ACTION_IGNORED_NO_POSITION_TO_REDUCE"
    ACTION_IGNORED_COOLDOWN_ACTIVE = "ACTION_IGNORED_COOLDOWN_ACTIVE"
    ACTION_IGNORED_POSITION_EXISTS = "ACTION_IGNORED_POSITION_EXISTS"
    ACTION_IGNORED_CONFLICT_EXIT_PRECEDENCE = "ACTION_IGNORED_CONFLICT_EXIT_PRECEDENCE"
    ACTION_IGNORED_ZERO_ALLOCATED_VOLUME = "ACTION_IGNORED_ZERO_ALLOCATED_VOLUME"

class InstrumentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str
    symbol: str
    reference_dataset_id: str         # Reference dataset for indicators & strategy rules (e.g. synthetic_underlying_nifty_15m)
    execution_dataset_id: str         # Execution dataset whose OHLCV candles determine fills (e.g. synthetic_candidate_option_ce_23500_15m)
    dataset_id: str                   # Canonical execution dataset ID (alias/primary)
    currency: str = "INR"
    currency_scale: int = 2
    price_scale: int = 2
    quantity_scale: int = 0
    tick_size_units: int = 5          # 0.05 price increment scaled by 10^2 = 5
    lot_size_units: int = 50          # 50 contracts per lot
    min_quantity_units: int = 50
    max_quantity_units: int = 5000
    allow_fractional: bool = False
    allow_short: bool = False         # Naked shorting forbidden for Milestone 6A
    is_tradable: bool = True          # False for reference-only datasets
    timezone: str = "Asia/Kolkata"
    session_open_time: str = "09:15:00"
    session_close_time: str = "15:30:00"
    spec_version: str = "1.0.0"
    provider_mapping: Optional[Dict[str, Any]] = None

    def validate_order(
        self,
        side: OrderSide,
        order_type: OrderType,
        quantity_units: int,
        limit_price_units: Optional[int]
    ) -> List[str]:
        errors = []
        if not self.is_tradable:
            errors.append(f"Instrument '{self.instrument_id}' is a non-tradable reference instrument and cannot be ordered.")
            return errors

        if quantity_units <= 0:
            errors.append("Quantity must be strictly positive.")
        if quantity_units < self.min_quantity_units:
            errors.append(f"Quantity {quantity_units} is below minimum {self.min_quantity_units}.")
        if quantity_units > self.max_quantity_units:
            errors.append(f"Quantity {quantity_units} exceeds maximum {self.max_quantity_units}.")
        if self.lot_size_units > 0 and (quantity_units % self.lot_size_units != 0):
            errors.append(f"Quantity {quantity_units} must be a multiple of lot size {self.lot_size_units}.")

        if order_type == OrderType.LIMIT:
            if limit_price_units is None or limit_price_units <= 0:
                errors.append("LIMIT order requires a positive limit price.")
            elif self.tick_size_units > 0 and (limit_price_units % self.tick_size_units != 0):
                errors.append(f"Limit price {limit_price_units} must align with tick size {self.tick_size_units}.")
        elif order_type == OrderType.MARKET:
            if limit_price_units is not None:
                errors.append("MARKET order must not specify a limit price.")

        return errors

# Catalog of known packaged instruments
PACKAGED_INSTRUMENT_SPECS: Dict[str, InstrumentSpec] = {
    "synthetic_candidate_option_pe_23000_15m": InstrumentSpec(
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        symbol="NIFTY26SEP23000PE",
        reference_dataset_id="synthetic_underlying_nifty_15m",
        execution_dataset_id="synthetic_candidate_option_pe_23000_15m",
        dataset_id="synthetic_candidate_option_pe_23000_15m",
        currency="INR",
        currency_scale=2,
        price_scale=2,
        quantity_scale=0,
        tick_size_units=5,
        lot_size_units=50,
        min_quantity_units=50,
        max_quantity_units=5000,
        allow_fractional=False,
        allow_short=False,
        is_tradable=True,
        timezone="Asia/Kolkata",
        session_open_time="09:15:00",
        session_close_time="15:30:00",
        spec_version="1.0.0",
    ),
    "synthetic_candidate_option_ce_23000_15m": InstrumentSpec(
        instrument_id="synthetic_candidate_option_ce_23000_15m",
        symbol="NIFTY26SEP23000CE",
        reference_dataset_id="synthetic_underlying_nifty_15m",
        execution_dataset_id="synthetic_candidate_option_ce_23000_15m",
        dataset_id="synthetic_candidate_option_ce_23000_15m",
        currency="INR",
        currency_scale=2,
        price_scale=2,
        quantity_scale=0,
        tick_size_units=5,
        lot_size_units=50,
        min_quantity_units=50,
        max_quantity_units=5000,
        allow_fractional=False,
        allow_short=False,
        is_tradable=True,
        timezone="Asia/Kolkata",
        session_open_time="09:15:00",
        session_close_time="15:30:00",
        spec_version="1.0.0",
    ),
    "synthetic_candidate_option_ce_23500_15m": InstrumentSpec(
        instrument_id="synthetic_candidate_option_ce_23500_15m",
        symbol="NIFTY26SEP23500CE",
        reference_dataset_id="synthetic_underlying_nifty_15m",
        execution_dataset_id="synthetic_candidate_option_ce_23500_15m",
        dataset_id="synthetic_candidate_option_ce_23500_15m",
        currency="INR",
        currency_scale=2,
        price_scale=2,
        quantity_scale=0,
        tick_size_units=5,
        lot_size_units=50,
        min_quantity_units=50,
        max_quantity_units=5000,
        allow_fractional=False,
        allow_short=False,
        is_tradable=True,
        timezone="Asia/Kolkata",
        session_open_time="09:15:00",
        session_close_time="15:30:00",
        spec_version="1.0.0",
    ),
    "synthetic_underlying_nifty_15m": InstrumentSpec(
        instrument_id="synthetic_underlying_nifty_15m",
        symbol="NIFTY50",
        reference_dataset_id="synthetic_underlying_nifty_15m",
        execution_dataset_id="synthetic_underlying_nifty_15m",
        dataset_id="synthetic_underlying_nifty_15m",
        currency="INR",
        currency_scale=2,
        price_scale=2,
        quantity_scale=0,
        tick_size_units=5,
        lot_size_units=50,
        min_quantity_units=50,
        max_quantity_units=5000,
        allow_fractional=False,
        allow_short=False,
        is_tradable=False,  # Reference-only underlying index, NOT directly orderable
        timezone="Asia/Kolkata",
        session_open_time="09:15:00",
        session_close_time="15:30:00",
        spec_version="1.0.0",
    ),
}

def get_instrument_spec(instrument_id: str) -> Optional[InstrumentSpec]:
    return PACKAGED_INSTRUMENT_SPECS.get(instrument_id)
