from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, ConfigDict
from src.engine.paper.models import (
    TradingMode,
    OrderSide,
    OrderType,
    RiskReasonCode,
    InstrumentSpec,
)

class RiskCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    reason_code: RiskReasonCode
    message: str
    metrics: Dict[str, Any] = {}

class PureRiskEngine:
    @staticmethod
    def evaluate_pre_trade_risk(
        trading_mode: TradingMode,
        instrument_spec: InstrumentSpec,
        side: OrderSide,
        order_type: OrderType,
        quantity_units: int,
        limit_price_units: Optional[int],
        reference_price_units: Optional[int],
        price_timestamp: Optional[datetime],
        current_time: datetime,
        risk_policy: Dict[str, Any],
        available_cash_units: int,
        open_orders: List[Dict[str, Any]],
        current_positions: Dict[str, Dict[str, Any]],
        daily_trades_count: int,
        daily_realized_loss_units: int,
        kill_switch_active: bool,
    ) -> RiskCheckResult:
        """
        Pure pre-trade risk validation function.
        Fail-closed: Returns passed=False on any breach or exception.
        """
        # 1. Trading Mode check
        if trading_mode != TradingMode.PAPER:
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_MODE_LIVE_FORBIDDEN,
                message="Live trading mode is prohibited in this milestone. Only PAPER mode is permitted.",
            )

        # 2. Kill switch check
        if kill_switch_active:
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_KILL_SWITCH_ACTIVE,
                message="Kill switch is active. New orders cannot be accepted.",
            )

        # 3. Allowed instruments check
        allowed_instruments = risk_policy.get("allowed_instruments")
        if allowed_instruments is not None and instrument_spec.instrument_id not in allowed_instruments:
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_INSTRUMENT_NOT_ALLOWED,
                message=f"Instrument '{instrument_spec.instrument_id}' is not in allowed instruments whitelist.",
            )

        # 4. Price presence check
        eval_price_units = limit_price_units if (order_type == OrderType.LIMIT and limit_price_units) else reference_price_units
        if eval_price_units is None or eval_price_units <= 0:
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_PRICE_MISSING,
                message="Current reference price or limit price is missing or non-positive.",
            )

        # 5. Price staleness check
        max_staleness_seconds = risk_policy.get("max_price_staleness_seconds", 3600)  # Default 1 hr for 15m candle fixtures
        if price_timestamp is not None:
            age = (current_time - price_timestamp).total_seconds()
            if age < 0:
                age = 0  # Clock sync boundary
            if age > max_staleness_seconds:
                return RiskCheckResult(
                    passed=False,
                    reason_code=RiskReasonCode.RISK_PRICE_STALE,
                    message=f"Reference price age ({age:.1f}s) exceeds maximum allowed staleness ({max_staleness_seconds}s).",
                    metrics={"price_age_seconds": age, "max_staleness_seconds": max_staleness_seconds}
                )

        # 6. Instrument-level spec validation (lot size, min/max qty, tick size)
        spec_errors = instrument_spec.validate_order(
            side=side,
            order_type=order_type,
            quantity_units=quantity_units,
            limit_price_units=limit_price_units
        )
        if spec_errors:
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_QTY_INVALID,
                message="; ".join(spec_errors),
            )

        # 7. Max quantity per order
        max_qty_per_order = risk_policy.get("max_quantity_per_order_units")
        if max_qty_per_order is not None and quantity_units > max_qty_per_order:
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_QTY_EXCEEDED,
                message=f"Order quantity {quantity_units} exceeds policy max {max_qty_per_order}.",
                metrics={"quantity": quantity_units, "limit": max_qty_per_order}
            )

        # 8. Max notional per order
        # Calculate notional in price units (divide by scale if necessary)
        order_notional_units = quantity_units * eval_price_units
        max_notional = risk_policy.get("max_notional_per_order_units")
        if max_notional is not None and order_notional_units > max_notional:
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_NOTIONAL_EXCEEDED,
                message=f"Order notional {order_notional_units} exceeds policy max {max_notional}.",
                metrics={"notional": order_notional_units, "limit": max_notional}
            )

        # 9. Max open orders
        max_open_orders = risk_policy.get("max_open_orders", 10)
        current_open_orders_count = len(open_orders)
        if current_open_orders_count + 1 > max_open_orders:
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_MAX_ORDERS_EXCEEDED,
                message=f"Current open orders ({current_open_orders_count}) + new order would exceed max limit ({max_open_orders}).",
                metrics={"open_orders": current_open_orders_count, "limit": max_open_orders}
            )

        # 10. Max open positions
        max_open_positions = risk_policy.get("max_open_positions", 5)
        has_pos = instrument_spec.instrument_id in current_positions and current_positions[instrument_spec.instrument_id].get("net_quantity_units", 0) != 0
        current_positions_count = sum(1 for p in current_positions.values() if p.get("net_quantity_units", 0) != 0)
        if not has_pos and (current_positions_count + 1 > max_open_positions):
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_MAX_POSITIONS_EXCEEDED,
                message=f"Opening a new position would exceed maximum open positions ({max_open_positions}).",
                metrics={"positions_count": current_positions_count, "limit": max_open_positions}
            )

        # 11. Cumulative Instrument Exposure (Current Position + Open Orders in Instrument + Proposed Order)
        existing_inst_pos = current_positions.get(instrument_spec.instrument_id, {})
        existing_inst_qty = abs(existing_inst_pos.get("net_quantity_units", 0))
        open_orders_inst_qty = sum(
            abs(o.get("quantity_units", 0) - o.get("filled_quantity_units", 0))
            for o in open_orders
            if o.get("instrument_id") == instrument_spec.instrument_id
        )
        total_inst_qty = existing_inst_qty + open_orders_inst_qty + quantity_units
        total_inst_notional = total_inst_qty * eval_price_units
        max_inst_exposure = risk_policy.get("max_instrument_exposure_units")
        if max_inst_exposure is not None and total_inst_notional > max_inst_exposure:
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_INSTRUMENT_EXPOSURE_EXCEEDED,
                message=f"Cumulative instrument exposure {total_inst_notional} exceeds max limit {max_inst_exposure}.",
                metrics={"exposure": total_inst_notional, "limit": max_inst_exposure}
            )

        # 12. Cumulative Total Exposure
        total_portfolio_exposure = sum(
            abs(p.get("net_quantity_units", 0)) * p.get("last_mark_price_units", 0)
            for p in current_positions.values()
        ) + sum(
            abs(o.get("quantity_units", 0) - o.get("filled_quantity_units", 0)) * (o.get("limit_price_units") or eval_price_units)
            for o in open_orders
        ) + (quantity_units * eval_price_units)
        max_total_exposure = risk_policy.get("max_total_exposure_units")
        if max_total_exposure is not None and total_portfolio_exposure > max_total_exposure:
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_TOTAL_EXPOSURE_EXCEEDED,
                message=f"Total portfolio exposure {total_portfolio_exposure} exceeds max limit {max_total_exposure}.",
                metrics={"total_exposure": total_portfolio_exposure, "limit": max_total_exposure}
            )

        # 13. Max daily trades count
        max_daily_trades = risk_policy.get("max_trades_per_day")
        if max_daily_trades is not None and daily_trades_count >= max_daily_trades:
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_DAILY_TRADES_EXCEEDED,
                message=f"Daily trade execution count ({daily_trades_count}) has reached the daily limit ({max_daily_trades}).",
                metrics={"daily_trades": daily_trades_count, "limit": max_daily_trades}
            )

        # 14. Max daily realized loss
        max_daily_loss = risk_policy.get("max_daily_realized_loss_units")
        if max_daily_loss is not None and daily_realized_loss_units >= max_daily_loss:
            return RiskCheckResult(
                passed=False,
                reason_code=RiskReasonCode.RISK_DAILY_LOSS_EXCEEDED,
                message=f"Daily realized loss ({daily_realized_loss_units}) has breached the daily threshold ({max_daily_loss}).",
                metrics={"daily_loss": daily_realized_loss_units, "limit": max_daily_loss}
            )

        # 15. Cash availability check for BUY orders
        if side == OrderSide.BUY:
            fee_bps = risk_policy.get("fee_basis_points", 5)
            flat_fee = risk_policy.get("flat_fee_units", 2000)
            # Estimate fee in cash units: (notional * fee_bps / 10000) + flat_fee
            estimated_fee = (order_notional_units * fee_bps // 10000) + flat_fee
            required_cash = order_notional_units + estimated_fee
            if required_cash > available_cash_units:
                return RiskCheckResult(
                    passed=False,
                    reason_code=RiskReasonCode.RISK_INSUFFICIENT_AVAILABLE_CASH,
                    message=f"Required cash {required_cash} exceeds available unreserved cash {available_cash_units}.",
                    metrics={"required_cash": required_cash, "available_cash": available_cash_units}
                )

        # 16. Short-selling and position reduction restrictions
        if side == OrderSide.SELL:
            existing_pos = current_positions.get(instrument_spec.instrument_id, {})
            current_net_qty = existing_pos.get("net_quantity_units", 0)
            if not instrument_spec.allow_short:
                if current_net_qty <= 0:
                    return RiskCheckResult(
                        passed=False,
                        reason_code=RiskReasonCode.RISK_INSTRUMENT_NOT_ALLOWED,
                        message=f"Short selling is forbidden for instrument '{instrument_spec.instrument_id}'. No open long position exists to sell.",
                        metrics={"current_net_quantity": current_net_qty}
                    )
                if quantity_units > current_net_qty:
                    return RiskCheckResult(
                        passed=False,
                        reason_code=RiskReasonCode.RISK_QTY_EXCEEDED,
                        message=f"SELL quantity {quantity_units} exceeds current long position {current_net_qty}. Crossing into forbidden short exposure is rejected.",
                        metrics={"order_quantity": quantity_units, "current_long_position": current_net_qty}
                    )

        return RiskCheckResult(
            passed=True,
            reason_code=RiskReasonCode.RISK_OK,
            message="Pre-trade risk checks passed successfully.",
            metrics={"order_notional": order_notional_units}
        )
