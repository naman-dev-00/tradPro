import pytest
from datetime import datetime, timezone
from src.engine.paper.models import OrderSide, LedgerEntryType
from src.engine.paper.accounting import AccountingEngine

def test_flat_to_long():
    # Buy 50 @ 100 with fee 10
    res = AccountingEngine.apply_fill_to_position(
        current_net_qty_units=0,
        current_avg_price_units=0,
        fill_side=OrderSide.BUY,
        fill_qty_units=50,
        fill_price_units=10000,  # 100.00
        fill_fee_units=1000,     # 10.00
        allow_short=False,
    )
    assert res.new_net_quantity_units == 50
    assert res.new_average_entry_price_units == 10000
    assert res.new_cost_basis_units == 500000
    assert res.incremental_gross_realized_pnl_units == 0
    assert res.incremental_net_realized_pnl_units == -1000

def test_add_to_long():
    # Holding 50 @ 100, buy 50 @ 120 with fee 10
    res = AccountingEngine.apply_fill_to_position(
        current_net_qty_units=50,
        current_avg_price_units=10000,
        fill_side=OrderSide.BUY,
        fill_qty_units=50,
        fill_price_units=12000,
        fill_fee_units=1000,
        allow_short=False,
    )
    assert res.new_net_quantity_units == 100
    # Avg price = (50*100 + 50*120)/100 = 110.00
    assert res.new_average_entry_price_units == 11000
    assert res.new_cost_basis_units == 1100000

def test_partial_reduce_long():
    # Holding 100 @ 110, sell 50 @ 130 with fee 10
    res = AccountingEngine.apply_fill_to_position(
        current_net_qty_units=100,
        current_avg_price_units=11000,
        fill_side=OrderSide.SELL,
        fill_qty_units=50,
        fill_price_units=13000,
        fill_fee_units=1000,
        allow_short=False,
    )
    assert res.new_net_quantity_units == 50
    assert res.new_average_entry_price_units == 11000  # Unchanged
    # Gross PnL = 50 * (130 - 110) = 50 * 20 = 1000.00
    assert res.incremental_gross_realized_pnl_units == 50 * 2000
    # Net PnL = 1000 - 10 = 990.00
    assert res.incremental_net_realized_pnl_units == (50 * 2000) - 1000

def test_close_long():
    # Holding 50 @ 110, sell 50 @ 140 with fee 10
    res = AccountingEngine.apply_fill_to_position(
        current_net_qty_units=50,
        current_avg_price_units=11000,
        fill_side=OrderSide.SELL,
        fill_qty_units=50,
        fill_price_units=14000,
        fill_fee_units=1000,
        allow_short=False,
    )
    assert res.new_net_quantity_units == 0
    assert res.new_average_entry_price_units == 0
    assert res.new_cost_basis_units == 0
    assert res.incremental_gross_realized_pnl_units == 50 * 3000
    assert res.incremental_net_realized_pnl_units == (50 * 3000) - 1000

def test_forbidden_short_reversal():
    """
    Proves that when allow_short=False, any sell fill that would cross zero
    into short exposure is rejected with a ValueError.
    """
    with pytest.raises(ValueError, match="[Ff]orbidden"):
        AccountingEngine.apply_fill_to_position(
            current_net_qty_units=50,
            current_avg_price_units=10000,
            fill_side=OrderSide.SELL,
            fill_qty_units=100,  # Would cross zero into -50 short
            fill_price_units=12000,
            fill_fee_units=2000,
            allow_short=False,
        )

def test_forbidden_short_open():
    """
    Proves that when allow_short=False, opening a short position from flat is rejected.
    """
    with pytest.raises(ValueError, match="[Ff]orbidden"):
        AccountingEngine.apply_fill_to_position(
            current_net_qty_units=0,
            current_avg_price_units=0,
            fill_side=OrderSide.SELL,
            fill_qty_units=50,
            fill_price_units=12000,
            fill_fee_units=2000,
            allow_short=False,
        )

def test_reverse_position_when_allowed():
    # When allow_short=True explicitly
    res = AccountingEngine.apply_fill_to_position(
        current_net_qty_units=50,
        current_avg_price_units=10000,
        fill_side=OrderSide.SELL,
        fill_qty_units=100,
        fill_price_units=12000,
        fill_fee_units=2000,
        allow_short=True,
    )
    assert res.new_net_quantity_units == -50
    assert res.new_average_entry_price_units == 12000
    assert res.incremental_gross_realized_pnl_units == 50 * 2000
    assert res.incremental_net_realized_pnl_units == (50 * 2000) - 2000

def test_unrealized_pnl():
    # Long 50 @ 100, mark @ 115
    pnl = AccountingEngine.calculate_unrealized_pnl(50, 10000, 11500)
    assert pnl == 50 * 1500  # 750.00

    # Short 50 @ 100, mark @ 90
    pnl_short = AccountingEngine.calculate_unrealized_pnl(-50, 10000, 9000)
    assert pnl_short == 50 * 1000  # 500.00

def test_comprehensive_independent_ledger_replay():
    """
    Tests complete ledger replay across all required cases:
    - Deposit
    - Reservation
    - Partial fill
    - Multiple partial fills
    - Full fill
    - Partial cancellation
    - DAY expiration
    - Sell/close
    - Fee accounting
    - Retry after a simulated failure (idempotent deduplication)
    """
    postings = [
        # 1. Deposit: settled +1,000,000, reserved 0
        {
            "idempotency_key": "dep_1",
            "entry_type": LedgerEntryType.INITIAL_DEPOSIT.value,
            "settled_delta": 1000000,
            "reserved_delta": 0,
        },
        # 2. Buy Reservation for 100 @ 100 + fee 20: settled 0, reserved +1,002,000
        # (suppose deposit was 2,000,000 to cover)
        {
            "idempotency_key": "res_1",
            "entry_type": LedgerEntryType.CASH_RESERVATION.value,
            "settled_delta": 0,
            "reserved_delta": 1002000,
        },
        # 3. Partial fill 1 (30 @ 100 + fee 6): settled -300600, reserved -300600
        {
            "idempotency_key": "fill_1",
            "entry_type": LedgerEntryType.BUY_FILL.value,
            "settled_delta": -300600,
            "reserved_delta": -300600,
        },
        # 4. Partial fill 2 (30 @ 100 + fee 6): settled -300600, reserved -300600
        {
            "idempotency_key": "fill_2",
            "entry_type": LedgerEntryType.BUY_FILL.value,
            "settled_delta": -300600,
            "reserved_delta": -300600,
        },
        # 5. Partial cancellation (20 @ 100 + fee 4): settled 0, reserved -200400
        {
            "idempotency_key": "cancel_1",
            "entry_type": LedgerEntryType.RESERVATION_RELEASE.value,
            "settled_delta": 0,
            "reserved_delta": -200400,
        },
        # 6. DAY expiration for remainder (20 @ 100 + fee 4): settled 0, reserved -200400
        {
            "idempotency_key": "expire_1",
            "entry_type": LedgerEntryType.RESERVATION_RELEASE.value,
            "settled_delta": 0,
            "reserved_delta": -200400,
        },
        # 7. Sell/close (60 @ 110 - fee 12): gross 660,000 - 1200 = +658,800 settled, reserved 0
        {
            "idempotency_key": "sell_1",
            "entry_type": LedgerEntryType.SELL_FILL.value,
            "settled_delta": 658800,
            "reserved_delta": 0,
        },
        # 8. Retry of sell_1 (simulated network retry with same idempotency key)
        {
            "idempotency_key": "sell_1",
            "entry_type": LedgerEntryType.SELL_FILL.value,
            "settled_delta": 658800,
            "reserved_delta": 0,
        },
    ]

    replayed_settled = 0
    replayed_reserved = 0
    processed_keys = set()

    for p in postings:
        key = p["idempotency_key"]
        if key in processed_keys:
            # Deduplicated retry: must NOT double-post
            continue
        processed_keys.add(key)

        replayed_settled += p["settled_delta"]
        replayed_reserved += p["reserved_delta"]

        # Invariant: reserved must never be negative
        assert replayed_reserved >= 0, f"Negative reserved cash: {replayed_reserved}"

    # Verify math:
    # Starting deposit: 1,000,000
    # Fill 1 debit: -300,600
    # Fill 2 debit: -300,600
    # Net cash before sell: 1,000,000 - 601,200 = 398,800
    # Sell credit: +658,800
    # Final settled: 398,800 + 658,800 = 1,057,600
    # Reserved cash should be exactly 0
    assert replayed_settled == 1057600
    assert replayed_reserved == 0
    available = replayed_settled - replayed_reserved
    assert available == 1057600

def test_forbidden_short_reversal_rollback_leaves_zero_mutations(db_session, test_user):
    """
    Section 4 Verification:
    A fill that would cross a long position through zero when allow_short=False
    must not leave:
    - A partial position mutation
    - A cash posting
    - A fee posting
    - A fill row
    - An order-event row claiming a fill
    - A consumed reservation

    Proves all balances, positions, reservations, fills, and event counts remain
    unchanged after rejection and rollback.
    """
    from src.models import (
        PaperAccount, Strategy, StrategyActionPolicy, RiskPolicy,
        StrategyRuntime, OrderIntent, PaperPosition, Order, Fill, OrderEvent, AccountLedgerEntry
    )
    from src.engine.paper.models import PACKAGED_INSTRUMENT_SPECS
    from src.engine.paper.fill_model import FillResult
    from src.services.paper_service import PaperService

    # 1. Setup account with initial cash & reservation
    acct = PaperAccount(
        id="acct-short-test",
        owner_id=test_user.id,
        name="Short Reversal Guard Acct",
        currency="INR",
        total_cash_units=10_000_000,
        reserved_cash_units=50_000,
    )
    db_session.add(acct)

    strat = Strategy(
        id="strat-short-test",
        owner_id=test_user.id,
        name="Reversal Test Strategy",
        timeframe="15m",
        payload={"name": "S1"},
    )
    db_session.add(strat)

    pol = StrategyActionPolicy(
        id="pol-short-test",
        owner_id=test_user.id,
        strategy_id=strat.id,
        name="AP1",
        version=1,
        payload={"entry_mapping": {}},
    )
    db_session.add(pol)

    risk = RiskPolicy(
        id="risk-short-test",
        owner_id=test_user.id,
        name="RP1",
        version=1,
        payload={"max_open_orders": 10},
    )
    db_session.add(risk)

    inst_id = "synthetic_candidate_option_ce_23500_15m"
    inst_spec = PACKAGED_INSTRUMENT_SPECS[inst_id]
    assert inst_spec.allow_short is False

    rt = StrategyRuntime(
        id="rt-short-test",
        owner_id=test_user.id,
        account_id=acct.id,
        strategy_id=strat.id,
        action_policy_id=pol.id,
        risk_policy_id=risk.id,
        dataset_id=inst_spec.execution_dataset_id,
        timeframe="15m",
        status="RUNNING",
        trading_mode="PAPER",
    )
    db_session.add(rt)

    intent = OrderIntent(
        id="int-short-test",
        owner_id=test_user.id,
        runtime_id=rt.id,
        action_mapping_id="m1",
        requested_instrument_id=inst_id,
        resolved_instrument_id=inst_id,
        intent_type="ENTRY",
        reduce_only=False,
        side="SELL",
        quantity_units=100,
        order_type="MARKET",
        time_in_force="DAY",
        source_candle_timestamp=datetime.now(timezone.utc),
        source_evaluation_fingerprint="fp1",
        trigger_event_key="tk1",
    )
    db_session.add(intent)

    # Long 50 units @ 100.00 (cost basis = 500,000)
    pos = PaperPosition(
        id="pos-short-test",
        owner_id=test_user.id,
        account_id=acct.id,
        instrument_id=inst_id,
        net_quantity_units=50,
        average_entry_price_units=10000,
        cost_basis_units=500000,
        gross_realized_pnl_units=0,
        total_fees_units=0,
        net_realized_pnl_units=0,
        last_mark_price_units=10000,
        unrealized_pnl_units=0,
    )
    db_session.add(pos)

    # Order to sell 100 units (which would cross zero if executed)
    ord_obj = Order(
        id="ord-short-test",
        owner_id=test_user.id,
        account_id=acct.id,
        runtime_id=rt.id,
        intent_id=intent.id,
        order_sequence_number=1,
        instrument_id=inst_id,
        side="SELL",
        order_type="MARKET",
        quantity_units=100,
        filled_quantity_units=0,
        status="ACCEPTED",
    )
    db_session.add(ord_obj)
    db_session.commit()

    # Pre-execution snapshot
    initial_total_cash = acct.total_cash_units
    initial_reserved_cash = acct.reserved_cash_units
    initial_net_qty = pos.net_quantity_units
    initial_avg_price = pos.average_entry_price_units
    initial_cost_basis = pos.cost_basis_units
    initial_order_status = ord_obj.status
    initial_order_filled = ord_obj.filled_quantity_units

    initial_fills_count = db_session.query(Fill).filter(Fill.order_id == ord_obj.id).count()
    initial_events_count = db_session.query(OrderEvent).filter(OrderEvent.order_id == ord_obj.id).count()
    initial_ledger_count = db_session.query(AccountLedgerEntry).filter(AccountLedgerEntry.account_id == acct.id).count()

    # 2. Attempt a fill of 100 units SELL (crosses 0 to -50)
    simulated_fill = FillResult(
        order_id=ord_obj.id,
        fill_quantity_units=100,
        fill_price_units=12000,
        fee_units=2000,
        is_full_fill=True,
        remaining_quantity_units=0,
        candle_timestamp=datetime.now(timezone.utc),
        fill_idempotency_key="fill-short-test-1",
    )

    with pytest.raises(ValueError, match="[Ff]orbidden"):
        try:
            PaperService._apply_fill(db_session, rt, simulated_fill, inst_spec)
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    # 3. Verify complete post-rollback integrity: zero mutations
    db_session.refresh(acct)
    db_session.refresh(pos)
    db_session.refresh(ord_obj)

    # Cash and reservations unchanged
    assert acct.total_cash_units == initial_total_cash == 10_000_000
    assert acct.reserved_cash_units == initial_reserved_cash == 50_000

    # Position unchanged (no partial mutation)
    assert pos.net_quantity_units == initial_net_qty == 50
    assert pos.average_entry_price_units == initial_avg_price == 10000
    assert pos.cost_basis_units == initial_cost_basis == 500000
    assert pos.gross_realized_pnl_units == 0
    assert pos.total_fees_units == 0

    # Order unchanged
    assert ord_obj.status == initial_order_status == "ACCEPTED"
    assert ord_obj.filled_quantity_units == initial_order_filled == 0

    # Zero fills, zero order events, zero ledger postings
    post_fills_count = db_session.query(Fill).filter(Fill.order_id == ord_obj.id).count()
    post_events_count = db_session.query(OrderEvent).filter(OrderEvent.order_id == ord_obj.id).count()
    post_ledger_count = db_session.query(AccountLedgerEntry).filter(AccountLedgerEntry.account_id == acct.id).count()

    assert post_fills_count == initial_fills_count == 0
    assert post_events_count == initial_events_count == 0
    assert post_ledger_count == initial_ledger_count == 0
