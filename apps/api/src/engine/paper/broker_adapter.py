from typing import Protocol, List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, ConfigDict
from src.engine.paper.models import TradingMode, OrderStatus, OrderSide, OrderType, InstrumentSpec
from src.engine.paper.fill_model import DeterministicFillEngine, CandleExecutionSummary

class BrokerOrderSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    account_id: str
    instrument_id: str
    side: OrderSide
    order_type: OrderType
    quantity_units: int
    filled_quantity_units: int
    limit_price_units: Optional[int]
    status: OrderStatus
    accepted_at: Optional[datetime]

class BrokerPositionSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    instrument_id: str
    net_quantity_units: int
    average_entry_price_units: int
    unrealized_pnl_units: int
    last_mark_price_units: int

class BrokerAdapter(Protocol):
    """
    Broker-neutral protocol for TradePro execution adapters.
    Designed to support future broker-specific compliance review without altering OMS or risk engine.
    """
    def submit_order(self, order_dict: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def cancel_order(self, order_id: str, reason: str) -> Dict[str, Any]:
        ...

    def get_order(self, order_id: str) -> Optional[BrokerOrderSnapshot]:
        ...

    def list_open_orders(self, account_id: str) -> List[BrokerOrderSnapshot]:
        ...

    def get_positions(self, account_id: str) -> List[BrokerPositionSnapshot]:
        ...

class PaperBrokerAdapter:
    """
    Deterministic in-memory/local paper broker implementation.
    Makes zero external network calls.
    Fails closed if live mode is requested.
    """
    def __init__(self, trading_mode: TradingMode = TradingMode.PAPER):
        if trading_mode != TradingMode.PAPER:
            raise ValueError(f"PaperBrokerAdapter strictly rejects non-PAPER trading mode '{trading_mode}'.")
        self.trading_mode = trading_mode

    def process_candle(
        self,
        open_orders: List[Dict[str, Any]],
        instrument_spec: InstrumentSpec,
        candle_open_units: int,
        candle_high_units: int,
        candle_low_units: int,
        candle_close_units: int,
        candle_volume_units: int,
        candle_timestamp: datetime,
        slippage_basis_points: int = 5,
        fee_basis_points: int = 5,
        flat_fee_units: int = 2000,
    ) -> CandleExecutionSummary:
        """
        Executes deterministic fill logic against open paper orders for a new candle.
        """
        return DeterministicFillEngine.calculate_order_fills(
            open_orders=open_orders,
            instrument_spec=instrument_spec,
            candle_open_units=candle_open_units,
            candle_high_units=candle_high_units,
            candle_low_units=candle_low_units,
            candle_close_units=candle_close_units,
            candle_volume_units=candle_volume_units,
            candle_timestamp=candle_timestamp,
            slippage_basis_points=slippage_basis_points,
            fee_basis_points=fee_basis_points,
            flat_fee_units=flat_fee_units,
        )
