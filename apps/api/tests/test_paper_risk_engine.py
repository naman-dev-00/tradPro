import pytest
from datetime import datetime, timezone, timedelta
from src.engine.paper.models import (
    TradingMode,
    OrderSide,
    OrderType,
    RiskReasonCode,
    PACKAGED_INSTRUMENT_SPECS,
)
from src.engine.paper.risk_engine import PureRiskEngine

@pytest.fixture
def base_spec():
    return PACKAGED_INSTRUMENT_SPECS["synthetic_candidate_option_pe_23000_15m"]

@pytest.fixture
def default_policy():
    return {
        "max_quantity_per_order_units": 1000,
        "max_notional_per_order_units": 50000000,  # 500k INR
        "max_open_orders": 5,
        "max_open_positions": 3,
        "max_instrument_exposure_units": 100000000,
        "max_total_exposure_units": 200000000,
        "max_trades_per_day": 10,
        "max_daily_realized_loss_units": 5000000,
        "allowed_instruments": ["synthetic_candidate_option_pe_23000_15m"],
        "max_price_staleness_seconds": 3600,
        "fee_basis_points": 5,
        "flat_fee_units": 2000,
    }

def test_live_mode_rejected(base_spec, default_policy):
    now = datetime.now(timezone.utc)
    res = PureRiskEngine.evaluate_pre_trade_risk(
        trading_mode=TradingMode.LIVE,
        instrument_spec=base_spec,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity_units=50,
        limit_price_units=None,
        reference_price_units=10000,
        price_timestamp=now,
        current_time=now,
        risk_policy=default_policy,
        available_cash_units=10000000,
        open_orders=[],
        current_positions={},
        daily_trades_count=0,
        daily_realized_loss_units=0,
        kill_switch_active=False,
    )
    assert not res.passed
    assert res.reason_code == RiskReasonCode.RISK_MODE_LIVE_FORBIDDEN

def test_kill_switch_active_rejected(base_spec, default_policy):
    now = datetime.now(timezone.utc)
    res = PureRiskEngine.evaluate_pre_trade_risk(
        trading_mode=TradingMode.PAPER,
        instrument_spec=base_spec,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity_units=50,
        limit_price_units=None,
        reference_price_units=10000,
        price_timestamp=now,
        current_time=now,
        risk_policy=default_policy,
        available_cash_units=10000000,
        open_orders=[],
        current_positions={},
        daily_trades_count=0,
        daily_realized_loss_units=0,
        kill_switch_active=True,
    )
    assert not res.passed
    assert res.reason_code == RiskReasonCode.RISK_KILL_SWITCH_ACTIVE

def test_stale_price_rejected(base_spec, default_policy):
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(seconds=7200)  # 2 hours old
    res = PureRiskEngine.evaluate_pre_trade_risk(
        trading_mode=TradingMode.PAPER,
        instrument_spec=base_spec,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity_units=50,
        limit_price_units=None,
        reference_price_units=10000,
        price_timestamp=old_time,
        current_time=now,
        risk_policy=default_policy,
        available_cash_units=10000000,
        open_orders=[],
        current_positions={},
        daily_trades_count=0,
        daily_realized_loss_units=0,
        kill_switch_active=False,
    )
    assert not res.passed
    assert res.reason_code == RiskReasonCode.RISK_PRICE_STALE

def test_lot_size_modulo_rejection(base_spec, default_policy):
    now = datetime.now(timezone.utc)
    # Lot size is 50, trying 75
    res = PureRiskEngine.evaluate_pre_trade_risk(
        trading_mode=TradingMode.PAPER,
        instrument_spec=base_spec,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity_units=75,
        limit_price_units=None,
        reference_price_units=10000,
        price_timestamp=now,
        current_time=now,
        risk_policy=default_policy,
        available_cash_units=10000000,
        open_orders=[],
        current_positions={},
        daily_trades_count=0,
        daily_realized_loss_units=0,
        kill_switch_active=False,
    )
    assert not res.passed
    assert res.reason_code == RiskReasonCode.RISK_QTY_INVALID

def test_insufficient_cash_rejected(base_spec, default_policy):
    now = datetime.now(timezone.utc)
    # Need 50 * 10000 = 500,000 paise = 5,000 INR
    res = PureRiskEngine.evaluate_pre_trade_risk(
        trading_mode=TradingMode.PAPER,
        instrument_spec=base_spec,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity_units=50,
        limit_price_units=None,
        reference_price_units=10000,
        price_timestamp=now,
        current_time=now,
        risk_policy=default_policy,
        available_cash_units=1000,  # only 10 INR available
        open_orders=[],
        current_positions={},
        daily_trades_count=0,
        daily_realized_loss_units=0,
        kill_switch_active=False,
    )
    assert not res.passed
    assert res.reason_code == RiskReasonCode.RISK_INSUFFICIENT_AVAILABLE_CASH

def test_risk_passed_cleanly(base_spec, default_policy):
    now = datetime.now(timezone.utc)
    res = PureRiskEngine.evaluate_pre_trade_risk(
        trading_mode=TradingMode.PAPER,
        instrument_spec=base_spec,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity_units=50,
        limit_price_units=None,
        reference_price_units=10000,
        price_timestamp=now,
        current_time=now,
        risk_policy=default_policy,
        available_cash_units=10000000,
        open_orders=[],
        current_positions={},
        daily_trades_count=0,
        daily_realized_loss_units=0,
        kill_switch_active=False,
    )
    assert res.passed
    assert res.reason_code == RiskReasonCode.RISK_OK
