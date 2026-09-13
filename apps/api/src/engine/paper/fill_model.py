from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
from datetime import datetime
from pydantic import BaseModel, ConfigDict
from src.engine.paper.models import OrderSide, OrderType, OrderStatus, InstrumentSpec
from src.engine.paper.units import decimal_to_units, units_to_decimal

class FillResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    fill_quantity_units: int
    fill_price_units: int
    fee_units: int
    is_full_fill: bool
    remaining_quantity_units: int
    candle_timestamp: datetime
    fill_idempotency_key: str

class CandleExecutionSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candle_timestamp: datetime
    instrument_id: str
    total_volume: int
    allocated_volume: int
    remaining_volume: int
    fills: List[FillResult]

class DeterministicFillEngine:
    @staticmethod
    def calculate_order_fills(
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
        volume_participation_pct: Decimal = Decimal("0.10"),  # Max 10% of candle volume
    ) -> CandleExecutionSummary:
        """
        Deterministically simulates fills for all competing open orders on a given candle.
        Volume is a finite shared capacity across orders on this candle.
        """
        # Sort competing orders strictly deterministically:
        # 1. Monotonic order sequence number (integer ascending)
        # 2. Deterministic intent trigger key (SHA256 string ascending tie-breaker)
        sorted_orders = sorted(
            open_orders,
            key=lambda o: (
                o.get("order_sequence_number", 0),
                str(o.get("intent_trigger_key", "") or o.get("order_sequence_number", 0)),
            )
        )

        max_allocatable_volume = int(Decimal(candle_volume_units) * volume_participation_pct)
        remaining_volume = max_allocatable_volume
        allocated_volume = 0
        fills: List[FillResult] = []

        for order in sorted_orders:
            if remaining_volume <= 0:
                # No more shared volume left in this candle
                break

            order_id = order["id"]
            side = OrderSide(order["side"])
            order_type = OrderType(order["order_type"])
            qty_units = order["quantity_units"]
            filled_qty_units = order.get("filled_quantity_units", 0)
            remaining_order_qty = qty_units - filled_qty_units

            if remaining_order_qty <= 0:
                continue

            limit_price_units = order.get("limit_price_units")
            fill_price_units: Optional[int] = None

            # Determine price eligibility
            if order_type == OrderType.MARKET:
                # MARKET order fills at Open with slippage
                if side == OrderSide.BUY:
                    slippage = (candle_open_units * slippage_basis_points) // 10000
                    fill_price_units = candle_open_units + slippage
                else:
                    slippage = (candle_open_units * slippage_basis_points) // 10000
                    fill_price_units = max(1, candle_open_units - slippage)

            elif order_type == OrderType.LIMIT:
                assert limit_price_units is not None
                if side == OrderSide.BUY:
                    # Gap down: Open is below limit -> price improvement at Open
                    if candle_open_units <= limit_price_units:
                        fill_price_units = candle_open_units
                    # Low reached or breached limit price
                    elif candle_low_units <= limit_price_units:
                        fill_price_units = limit_price_units
                elif side == OrderSide.SELL:
                    # Gap up: Open is above limit -> price improvement at Open
                    if candle_open_units >= limit_price_units:
                        fill_price_units = candle_open_units
                    # High reached or breached limit price
                    elif candle_high_units >= limit_price_units:
                        fill_price_units = limit_price_units

            # If not price-eligible, order cannot fill in this candle
            if fill_price_units is None:
                continue

            # Align fill quantity to instrument lot size
            allocatable_qty = min(remaining_order_qty, remaining_volume)
            if instrument_spec.lot_size_units > 1:
                allocatable_qty = (allocatable_qty // instrument_spec.lot_size_units) * instrument_spec.lot_size_units

            if allocatable_qty <= 0:
                continue

            # Fee calculation:
            # Flat fee charged ONCE on the first fill of the order
            is_first_fill = (filled_qty_units == 0)
            percentage_fee = (allocatable_qty * fill_price_units * fee_basis_points) // 10000
            order_flat_fee = flat_fee_units if is_first_fill else 0
            total_fill_fee = percentage_fee + order_flat_fee

            new_filled_total = filled_qty_units + allocatable_qty
            is_full = (new_filled_total >= qty_units)
            remaining_after = qty_units - new_filled_total

            # Generate stable fill idempotency key
            iso_ts = candle_timestamp.isoformat()
            stable_intent_key = order.get("intent_trigger_key") or order_id
            fill_key = f"{stable_intent_key}:{iso_ts}:{new_filled_total}"

            fills.append(
                FillResult(
                    order_id=order_id,
                    fill_quantity_units=allocatable_qty,
                    fill_price_units=fill_price_units,
                    fee_units=total_fill_fee,
                    is_full_fill=is_full,
                    remaining_quantity_units=remaining_after,
                    candle_timestamp=candle_timestamp,
                    fill_idempotency_key=fill_key,
                )
            )

            remaining_volume -= allocatable_qty
            allocated_volume += allocatable_qty

        return CandleExecutionSummary(
            candle_timestamp=candle_timestamp,
            instrument_id=instrument_spec.instrument_id,
            total_volume=candle_volume_units,
            allocated_volume=allocated_volume,
            remaining_volume=remaining_volume,
            fills=fills,
        )
