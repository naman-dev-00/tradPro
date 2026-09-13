from typing import Tuple, Dict, Any, Optional
from pydantic import BaseModel, ConfigDict
from src.engine.paper.models import OrderSide

class PositionUpdateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    new_net_quantity_units: int
    new_average_entry_price_units: int
    new_cost_basis_units: int
    incremental_gross_realized_pnl_units: int
    incremental_fees_units: int
    incremental_net_realized_pnl_units: int

class AccountingEngine:
    @staticmethod
    def apply_fill_to_position(
        current_net_qty_units: int,
        current_avg_price_units: int,
        fill_side: OrderSide,
        fill_qty_units: int,
        fill_price_units: int,
        fill_fee_units: int,
        allow_short: bool = False,
    ) -> PositionUpdateResult:
        """
        Updates a position based on an executed fill.
        Signed quantity convention:
          positive = Long
          negative = Short (strictly rejected when allow_short=False)
          0 = Flat
        """
        assert fill_qty_units > 0, "Fill quantity must be strictly positive"
        assert fill_price_units > 0, "Fill price must be strictly positive"

        # Incoming signed delta
        signed_delta = fill_qty_units if fill_side == OrderSide.BUY else -fill_qty_units

        # Case 1: Currently Flat (opening new position)
        if current_net_qty_units == 0:
            if signed_delta < 0 and not allow_short:
                raise ValueError("Forbidden short exposure: cannot open short position when allow_short is False.")
            new_qty = signed_delta
            new_avg_price = fill_price_units
            new_cost_basis = abs(new_qty) * new_avg_price
            return PositionUpdateResult(
                new_net_quantity_units=new_qty,
                new_average_entry_price_units=new_avg_price,
                new_cost_basis_units=new_cost_basis,
                incremental_gross_realized_pnl_units=0,
                incremental_fees_units=fill_fee_units,
                incremental_net_realized_pnl_units=-fill_fee_units,
            )

        # Case 2: Adding to position (same direction)
        same_direction = (current_net_qty_units > 0 and signed_delta > 0) or (current_net_qty_units < 0 and signed_delta < 0)
        if same_direction:
            new_qty = current_net_qty_units + signed_delta
            # Weighted average price
            total_cost = (abs(current_net_qty_units) * current_avg_price_units) + (fill_qty_units * fill_price_units)
            new_avg_price = total_cost // abs(new_qty)
            new_cost_basis = abs(new_qty) * new_avg_price
            return PositionUpdateResult(
                new_net_quantity_units=new_qty,
                new_average_entry_price_units=new_avg_price,
                new_cost_basis_units=new_cost_basis,
                incremental_gross_realized_pnl_units=0,
                incremental_fees_units=fill_fee_units,
                incremental_net_realized_pnl_units=-fill_fee_units,
            )

        # Case 3: Reducing or closing or reversing (opposite direction)
        current_abs_qty = abs(current_net_qty_units)

        if fill_qty_units <= current_abs_qty:
            # Partial reduction or complete close
            closed_qty = fill_qty_units
            # Gross PnL
            if current_net_qty_units > 0:  # Closing Long (SELL)
                gross_pnl = closed_qty * (fill_price_units - current_avg_price_units)
            else:  # Closing Short (BUY)
                gross_pnl = closed_qty * (current_avg_price_units - fill_price_units)

            net_pnl = gross_pnl - fill_fee_units
            new_qty = current_net_qty_units + signed_delta

            if new_qty == 0:
                # Fully closed
                new_avg_price = 0
                new_cost_basis = 0
            else:
                # Average entry price remains unchanged on partial close
                new_avg_price = current_avg_price_units
                new_cost_basis = abs(new_qty) * new_avg_price

            return PositionUpdateResult(
                new_net_quantity_units=new_qty,
                new_average_entry_price_units=new_avg_price,
                new_cost_basis_units=new_cost_basis,
                incremental_gross_realized_pnl_units=gross_pnl,
                incremental_fees_units=fill_fee_units,
                incremental_net_realized_pnl_units=net_pnl,
            )
        else:
            # Reversal: fill_qty > current_abs_qty
            # Leg 1: Close current position completely
            closed_qty = current_abs_qty
            if current_net_qty_units > 0:
                gross_pnl = closed_qty * (fill_price_units - current_avg_price_units)
            else:
                gross_pnl = closed_qty * (current_avg_price_units - fill_price_units)

            net_pnl = gross_pnl - fill_fee_units

            # Leg 2: Open opposite position with remainder
            remainder_qty = fill_qty_units - current_abs_qty
            if signed_delta < 0 and not allow_short:
                raise ValueError("Reversal into short position is forbidden for this instrument because shorting is not allowed.")
            new_qty = remainder_qty if signed_delta > 0 else -remainder_qty
            new_avg_price = fill_price_units
            new_cost_basis = abs(new_qty) * new_avg_price

            return PositionUpdateResult(
                new_net_quantity_units=new_qty,
                new_average_entry_price_units=new_avg_price,
                new_cost_basis_units=new_cost_basis,
                incremental_gross_realized_pnl_units=gross_pnl,
                incremental_fees_units=fill_fee_units,
                incremental_net_realized_pnl_units=net_pnl,
            )

    @staticmethod
    def calculate_unrealized_pnl(
        net_quantity_units: int,
        average_entry_price_units: int,
        last_mark_price_units: int,
    ) -> int:
        if net_quantity_units == 0 or average_entry_price_units <= 0:
            return 0
        if net_quantity_units > 0:
            # Long unrealized PnL: (Mark - Entry) * Qty
            return net_quantity_units * (last_mark_price_units - average_entry_price_units)
        else:
            # Short unrealized PnL: (Entry - Mark) * |Qty|
            return abs(net_quantity_units) * (average_entry_price_units - last_mark_price_units)
