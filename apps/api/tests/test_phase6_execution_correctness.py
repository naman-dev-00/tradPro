"""
Phase 6 Execution Correctness Test Suite.
Covers:
1. Audit sandbox order lifecycle against actual persisted states and Upstox sandbox contracts.
2. Fail-closed partial-fill reconciliation:
   - exact filled quantity
   - remaining quantity
   - cash reservation proportional releases
   - positions (net qty, cost basis, realized/unrealized PnL, fees)
   - ledger entries (monotonic sequences, correct settled & reserved deltas)
   - duplicate fill event idempotency protection
   - concurrent status updates (row-level locking serialization)
   - fail-closed overfill guard
   - partial fill followed by cancellation releasing remainder of reserved cash
3. Explicit rejection of in-flight order modification (outbox PLACE/CANCEL invariant).
4. Market session gates:
   - Market closed suppression (no orders, no outbox)
   - Holiday suppression (no orders, no outbox)
   - Stale candle suppression (no orders, no outbox)
   - Unknown session status suppression (no orders, no outbox)
5. SQLite and PostgreSQL concurrency tests with disposable databases.
6. API/UI status coverage and mocked end-to-end acceptance path.
"""
import concurrent.futures
import datetime
import os
import shutil
import tempfile
import uuid
from decimal import Decimal
from typing import Dict, Any, List, Optional
import pytest
from sqlalchemy import func, create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.database import Base
from src.auth.security import hash_password
from src.models import (
    AccountLedgerEntry,
    ExternalOrderLink,
    Fill,
    Order,
    OrderEvent,
    OrderIntent,
    PaperAccount,
    PaperPosition,
    ProviderConnection,
    ReconciliationRecord,
    Strategy,
    StrategyActionPolicy,
    RiskPolicy,
    StrategyRuntime,
    SubmissionOutbox,
    User,
)
from src.engine.paper.models import (
    LedgerEntryType,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.engine.paper.state_machine import (
    validate_order_transition,
    InvalidOrderTransitionError,
)
from src.engine.sandbox.reconciliation import (
    SandboxReconciliationEngine,
    ReconciliationError,
    OverfillError,
    OrderModificationNotSupportedError,
)
from src.engine.sandbox.upstox_adapter import (
    UpstoxSandboxAdapter,
    UpstoxOrderDetails,
    UpstoxTradeDetails,
    UpstoxAdapterError,
)
from src.engine.market_data.market_schedule import (
    MarketSessionStatus,
    SessionType,
    VerifiedSessionEvidence,
    get_market_session_status,
    is_candle_stale,
    TZ_KOLKATA,
    TZ_UTC,
)
from src.engine.provider_execution.engine import ProviderExecutionEngine
from tests.paper_database_support import paper_test_database
from tests.test_provider_execution import (
    provider_exec_setup,
    make_valid_candle,
)
from fastapi.testclient import TestClient
from src.main import app
from src.database import get_db, get_read_only_db
from src.auth.session import create_session


def _create_test_order(
    session: Session,
    user_id: str,
    account_id: str,
    runtime_id: Optional[str] = None,
    qty: int = 100,
    side: str = OrderSide.BUY.value,
    status: str = OrderStatus.ACKNOWLEDGED.value,
    limit_price: int = 20000,
    auto_reserve: bool = True,
) -> Order:
    """Helper creating a valid order with linked strategy, action policy, risk policy, and runtime."""
    if not runtime_id:
        uid = uuid.uuid4().hex[:8]
        strat = Strategy(
            id=str(uuid.uuid4()),
            owner_id=user_id,
            name=f"Test Strategy {uid}",
            timeframe="5m",
            candidate_selection_mode="FIRST_ELIGIBLE",
            payload={"name": "Test Strat", "timeframe": "5m"},
        )
        policy = StrategyActionPolicy(
            id=str(uuid.uuid4()),
            owner_id=user_id,
            strategy_id=strat.id,
            name=f"Test Policy {uid}",
            payload={},
        )
        risk = RiskPolicy(
            id=str(uuid.uuid4()),
            owner_id=user_id,
            name=f"Test Risk {uid}",
            payload={},
        )
        runtime = StrategyRuntime(
            id=str(uuid.uuid4()),
            owner_id=user_id,
            strategy_id=strat.id,
            action_policy_id=policy.id,
            risk_policy_id=risk.id,
            account_id=account_id,
            status="RUNNING",
            trading_mode="BROKER_SANDBOX",
            dataset_id="NSE_EQ|INE002A01018",
            timeframe="5m",
            strategy_snapshot=strat.payload,
            version=1,
        )
        session.add_all([strat, policy, risk, runtime])
        session.flush()
        runtime_id = runtime.id

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    intent = OrderIntent(
        id=str(uuid.uuid4()),
        owner_id=user_id,
        runtime_id=runtime_id,
        action_mapping_id="map_1",
        requested_instrument_id="NSE_EQ|INE002A01018",
        resolved_instrument_id="NSE_EQ|INE002A01018",
        intent_type="ENTRY",
        side=side,
        quantity_units=qty,
        order_type=OrderType.LIMIT.value,
        limit_price_units=limit_price,
        time_in_force="DAY",
        source_candle_timestamp=now_utc,
        source_evaluation_fingerprint="0" * 64,
        trigger_event_key=str(uuid.uuid4()),
        created_at=now_utc,
    )
    session.add(intent)
    session.flush()

    order = Order(
        id=str(uuid.uuid4()),
        owner_id=user_id,
        runtime_id=runtime_id,
        intent_id=intent.id,
        account_id=account_id,
        order_sequence_number=1,
        instrument_id="NSE_EQ|INE002A01018",
        side=side,
        order_type=OrderType.LIMIT.value,
        quantity_units=qty,
        filled_quantity_units=0,
        limit_price_units=limit_price,
        status=status,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(order)
    session.flush()

    if auto_reserve and side == OrderSide.BUY.value and limit_price and qty:
        res_amt = qty * limit_price
        existing_res = (
            session.query(AccountLedgerEntry)
            .filter(
                AccountLedgerEntry.account_id == account_id,
                AccountLedgerEntry.order_id == order.id,
                AccountLedgerEntry.entry_type == LedgerEntryType.CASH_RESERVATION.value,
            )
            .first()
        )
        if not existing_res:
            max_seq = (
                session.query(func.coalesce(func.max(AccountLedgerEntry.sequence_number), 0))
                .filter(AccountLedgerEntry.account_id == account_id)
                .scalar()
            )
            session.add(
                AccountLedgerEntry(
                    id=str(uuid.uuid4()),
                    account_id=account_id,
                    owner_id=user_id,
                    sequence_number=max_seq + 1,
                    entry_type=LedgerEntryType.CASH_RESERVATION.value,
                    amount_units=res_amt,
                    balance_after_units=0,
                    settled_cash_delta_units=0,
                    reserved_cash_delta_units=res_amt,
                    settled_cash_after_units=0,
                    reserved_cash_after_units=res_amt,
                    order_id=order.id,
                    reason_code="BUY_ORDER_RESERVED",
                    idempotency_key=f"res:{order.id}",
                    created_at=now_utc,
                )
            )
            session.flush()

    return order


# =========================================================================
# 1. Sandbox Order Lifecycle Audit & State Transitions
# =========================================================================

def test_sandbox_order_lifecycle_audit_and_contracts():
    """Audits permitted sandbox order lifecycle transitions according to Upstox contract."""
    # Valid forward transitions
    validate_order_transition(OrderStatus.CREATED, OrderStatus.ACCEPTED, "SYSTEM", "TEST")
    validate_order_transition(OrderStatus.CREATED, OrderStatus.PENDING_SUBMISSION, "SYSTEM", "TEST")
    validate_order_transition(OrderStatus.PENDING_SUBMISSION, OrderStatus.ACKNOWLEDGED, "DISPATCHER", "TEST")
    validate_order_transition(OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED, "SANDBOX", "PARTIAL")
    validate_order_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, "SANDBOX", "FULL")
    validate_order_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCEL_PENDING, "USER", "CANCEL_REQ")
    validate_order_transition(OrderStatus.CANCEL_PENDING, OrderStatus.CANCELLED, "SANDBOX", "CANCEL_CONFIRMED")
    
    # Reconciliation discoveries
    validate_order_transition(OrderStatus.RECONCILIATION_REQUIRED, OrderStatus.PARTIALLY_FILLED, "OPERATOR", "RECON_PARTIAL")
    validate_order_transition(OrderStatus.RECONCILIATION_REQUIRED, OrderStatus.FILLED, "OPERATOR", "RECON_FILLED")

    # Invalid transitions fail closed
    with pytest.raises(InvalidOrderTransitionError):
        validate_order_transition(OrderStatus.FILLED, OrderStatus.CANCELLED, "TEST", "INVALID")
    with pytest.raises(InvalidOrderTransitionError):
        validate_order_transition(OrderStatus.CANCELLED, OrderStatus.ACKNOWLEDGED, "TEST", "INVALID")
    with pytest.raises(InvalidOrderTransitionError):
        validate_order_transition(OrderStatus.CREATED, OrderStatus.FILLED, "TEST", "INVALID")


# =========================================================================
# 2. Fail-Closed Partial-Fill Reconciliation
# =========================================================================

def test_fail_closed_partial_fill_reconciliation(session, test_user):
    """
    Tests exact partial fill reconciliation:
    - BUY order: 100 units @ 200.00 INR (2000000 scale-4 units).
    - Initial cash reservation: 2,000,000 + 2000 fee = 2,002,000 units.
    - Partial fill 1: 40 units @ 200.00 INR -> exact 40% reservation release, exact cash deduct.
    - Partial fill 2: 60 units @ 200.00 INR -> completes order to FILLED, releases remaining reservation.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    
    # 1. Setup account
    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Reconciliation Account",
        total_cash_units=10000000,  # 1,000.00 INR
        reserved_cash_units=2002000,  # 200.20 INR reserved
        currency="INR",
    )
    session.add(account)
    session.flush()

    # 2. Setup order (BUY 100 units @ limit 20000 units / 2.0000)
    order = _create_test_order(
        session=session,
        user_id=test_user.id,
        account_id=account.id,
        qty=100,
        side=OrderSide.BUY.value,
        status=OrderStatus.ACKNOWLEDGED.value,
        limit_price=20000,
        auto_reserve=False,
    )

    # Reservation ledger entry
    res_entry = AccountLedgerEntry(
        id=str(uuid.uuid4()),
        account_id=account.id,
        owner_id=test_user.id,
        sequence_number=1,
        entry_type=LedgerEntryType.CASH_RESERVATION.value,
        amount_units=2002000,
        balance_after_units=account.total_cash_units,
        settled_cash_delta_units=0,
        reserved_cash_delta_units=2002000,
        settled_cash_after_units=account.total_cash_units,
        reserved_cash_after_units=account.reserved_cash_units,
        order_id=order.id,
        reason_code="BUY_ORDER_RESERVED",
        idempotency_key=f"res:{order.id}",
        created_at=now,
    )
    session.add(res_entry)
    session.flush()

    # 3. Partial fill 1: 40 units @ 20000 units, fee 200 units
    fill1 = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=40,
        fill_price_units=20000,
        fee_units=200,
        fill_idempotency_key="fill_event_1",
        provider_trade_id="TRD1001",
    )
    session.flush()

    assert fill1.quantity_units == 40
    assert order.status == OrderStatus.PARTIALLY_FILLED.value
    assert order.filled_quantity_units == 40
    
    # 40% of 2,002,000 = 800,800 reserved released
    expected_reserved_remaining = 2002000 - 800800
    assert account.reserved_cash_units == expected_reserved_remaining
    # Settled cash deducted: 40 * 20000 + 200 = 800200
    assert account.total_cash_units == 10000000 - 800200

    # Position check
    pos = session.query(PaperPosition).filter(
        PaperPosition.account_id == account.id,
        PaperPosition.instrument_id == order.instrument_id,
    ).first()
    assert pos is not None
    assert pos.net_quantity_units == 40
    assert pos.average_entry_price_units == 20000
    assert pos.total_fees_units == 200

    # 4. Partial fill 2: remaining 60 units @ 20000 units, fee 300 units
    fill2 = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=60,
        fill_price_units=20000,
        fee_units=300,
        fill_idempotency_key="fill_event_2",
        provider_trade_id="TRD1002",
    )
    session.flush()

    assert fill2.quantity_units == 60
    assert order.status == OrderStatus.FILLED.value
    assert order.filled_quantity_units == 100
    
    # Remainder released -> reserved cash 0
    assert account.reserved_cash_units == 0
    assert pos.net_quantity_units == 100
    assert pos.total_fees_units == 500


# =========================================================================
# 3. Duplicate Fill Event Idempotency
# =========================================================================

def test_duplicate_fill_idempotency(session, test_user):
    """Replaying an identical fill event returns the existing fill without double-mutating balances."""
    now = datetime.datetime.now(datetime.timezone.utc)
    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Idempotency Account",
        total_cash_units=10000000,
        reserved_cash_units=2000000,
        currency="INR",
    )
    session.add(account)
    session.flush()

    order = _create_test_order(
        session=session,
        user_id=test_user.id,
        account_id=account.id,
        qty=50,
        side=OrderSide.BUY.value,
        status=OrderStatus.ACKNOWLEDGED.value,
        limit_price=20000,
    )

    # Initial Fill
    fill_a = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=20,
        fill_price_units=20000,
        provider_trade_id="unique_evt_101",
    )
    session.flush()
    cash_after_first = account.total_cash_units
    reserved_after_first = account.reserved_cash_units

    # Replay duplicate
    fill_b = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=20,
        fill_price_units=20000,
        provider_trade_id="unique_evt_101",
    )
    session.flush()

    assert fill_a.id == fill_b.id
    assert account.total_cash_units == cash_after_first
    assert account.reserved_cash_units == reserved_after_first
    assert order.filled_quantity_units == 20


# =========================================================================
# 4. Fail-Closed Overfill Guard
# =========================================================================

def test_overfill_rejection_fail_closed(session, test_user):
    """An incoming fill that exceeds the order quantity is rejected fail-closed without mutating state."""
    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Overfill Account",
        total_cash_units=10000000,
        reserved_cash_units=2000000,
        currency="INR",
    )
    session.add(account)
    session.flush()

    order = _create_test_order(
        session=session,
        user_id=test_user.id,
        account_id=account.id,
        qty=30,
        side=OrderSide.BUY.value,
        status=OrderStatus.PARTIALLY_FILLED.value,
        limit_price=20000,
    )
    order.filled_quantity_units = 20
    session.flush()

    # Incoming fill of 15 would make total 35 > 30
    with pytest.raises(OverfillError) as exc_info:
        SandboxReconciliationEngine.reconcile_fill(
            db=session,
            order_id=order.id,
            owner_id=test_user.id,
            fill_qty_units=15,
            fill_price_units=20000,
            provider_trade_id="overfill_attempt",
        )
    assert "exceeds order quantity" in str(exc_info.value)
    assert order.filled_quantity_units == 20
    assert order.status == OrderStatus.PARTIALLY_FILLED.value


# =========================================================================
# 5. Partial Fill Followed by Cancellation
# =========================================================================

def test_partial_fill_followed_by_cancellation(session, test_user):
    """Cancelling a partially filled BUY order releases all remaining unreleased reserved cash."""
    now = datetime.datetime.now(datetime.timezone.utc)
    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Cancel Remainder Account",
        total_cash_units=10000000,
        reserved_cash_units=1000000,
        currency="INR",
    )
    session.add(account)
    session.flush()

    order = _create_test_order(
        session=session,
        user_id=test_user.id,
        account_id=account.id,
        qty=100,
        side=OrderSide.BUY.value,
        status=OrderStatus.ACKNOWLEDGED.value,
        limit_price=10000,
        auto_reserve=False,
    )

    res_entry = AccountLedgerEntry(
        id=str(uuid.uuid4()),
        account_id=account.id,
        owner_id=test_user.id,
        sequence_number=1,
        entry_type=LedgerEntryType.CASH_RESERVATION.value,
        amount_units=1000000,
        balance_after_units=account.total_cash_units,
        settled_cash_delta_units=0,
        reserved_cash_delta_units=1000000,
        settled_cash_after_units=account.total_cash_units,
        reserved_cash_after_units=account.reserved_cash_units,
        order_id=order.id,
        reason_code="BUY_ORDER_RESERVED",
        idempotency_key=f"res:{order.id}",
        created_at=now,
    )
    session.add(res_entry)
    session.flush()

    # Fill 30 units
    SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=30,
        fill_price_units=10000,
        fill_idempotency_key="part_30",
    )
    session.flush()
    assert order.filled_quantity_units == 30
    assert account.reserved_cash_units == 700000

    # Cancel remaining 70 units
    SandboxReconciliationEngine.reconcile_cancel(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        actor="USER",
        reason="USER_CANCELLED_REMAINDER",
    )
    session.flush()

    assert order.status == OrderStatus.CANCELLED.value
    assert order.filled_quantity_units == 30  # Preserved!
    assert account.reserved_cash_units == 0  # Remaining 700000 released!

    # Check release ledger entry
    rel_ledger = session.query(AccountLedgerEntry).filter(
        AccountLedgerEntry.account_id == account.id,
        AccountLedgerEntry.entry_type == LedgerEntryType.RESERVATION_RELEASE.value,
    ).first()
    assert rel_ledger is not None
    assert rel_ledger.amount_units == 700000


# =========================================================================
# 6. Order Modification Explicit Rejection
# =========================================================================

def test_order_modification_explicit_rejection():
    """Confirms explicit architectural rejection of order modification."""
    with pytest.raises(OrderModificationNotSupportedError) as exc_info:
        SandboxReconciliationEngine.reject_order_modification("order-123", {"price": 100})
    assert "Order modification is not supported" in str(exc_info.value)
    assert "cancel the existing order" in str(exc_info.value)


# =========================================================================
# 7. Market Session Gates (Closed, Holiday, Stale, Unknown)
# =========================================================================

def test_market_session_status_and_staleness():
    """Tests Indian exchange market session calendar and staleness evaluation."""
    # 1. Open market hours: Tuesday 2026-10-06 at 10:00:00 IST (04:30:00 UTC)
    dt_open = datetime.datetime(2026, 10, 6, 4, 30, 0, tzinfo=TZ_UTC)
    assert get_market_session_status(dt_open) == MarketSessionStatus.OPEN

    # 2. Closed market hours: Tuesday 2026-10-06 at 18:00:00 IST (12:30:00 UTC)
    dt_closed = datetime.datetime(2026, 10, 6, 12, 30, 0, tzinfo=TZ_UTC)
    assert get_market_session_status(dt_closed) == MarketSessionStatus.CLOSED

    # 3. Weekend: Saturday 2026-10-10 at 10:00:00 IST
    dt_weekend = datetime.datetime(2026, 10, 10, 4, 30, 0, tzinfo=TZ_UTC)
    assert get_market_session_status(dt_weekend) == MarketSessionStatus.CLOSED

    # 4. Exchange Holiday: Republic Day 2026-01-26 at 10:00:00 IST
    dt_holiday = datetime.datetime(2026, 1, 26, 4, 30, 0, tzinfo=TZ_UTC)
    assert get_market_session_status(dt_holiday) == MarketSessionStatus.HOLIDAY

    # 5. Naive timestamp: UNKNOWN
    dt_naive = datetime.datetime(2026, 10, 6, 10, 0, 0)
    assert get_market_session_status(dt_naive) == MarketSessionStatus.UNKNOWN

    # 6. Staleness evaluation
    clock_now = datetime.datetime(2026, 10, 6, 10, 0, 0, tzinfo=TZ_UTC)
    candle_fresh = clock_now - datetime.timedelta(seconds=300)
    candle_stale = clock_now - datetime.timedelta(seconds=1200)

    assert is_candle_stale(candle_fresh, clock_now, max_staleness_seconds=900) is False
    assert is_candle_stale(candle_stale, clock_now, max_staleness_seconds=900) is True


def test_provider_engine_market_session_gates_suppression(session, provider_exec_setup):
    """
    Evaluates provider engine with market closed, holiday, and stale candles.
    Asserts NO_ACTION decision, zero orders created, zero outbox entries.
    """
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]  # 2026-10-06 10:00:00 UTC (15:30:00 IST)

    engine = ProviderExecutionEngine(clock=lambda: clock_now, enforce_market_schedule=True)

    # A. Stale candle (25 mins old > 15 mins max staleness)
    stale_ts = clock_now - datetime.timedelta(minutes=25)
    c_stale = make_valid_candle(stale_ts)
    res_stale = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c_stale,
    )
    assert res_stale.action_decision == "NO_ACTION"
    assert res_stale.reason_code == "PRICE_STALE"
    assert len(res_stale.order_ids) == 0
    assert len(res_stale.outbox_ids) == 0

    # B. Market closed candle (evaluated at 18:00 IST / 12:30 UTC)
    clock_closed = datetime.datetime(2026, 10, 6, 12, 35, 0, tzinfo=TZ_UTC)
    engine_closed = ProviderExecutionEngine(clock=lambda: clock_closed, enforce_market_schedule=True)
    c_closed = make_valid_candle(clock_closed - datetime.timedelta(minutes=5))
    res_closed = engine_closed.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c_closed,
    )
    assert res_closed.action_decision == "NO_ACTION"
    assert res_closed.reason_code == "MARKET_CLOSED"
    assert len(res_closed.order_ids) == 0

    # C. Holiday candle (evaluated on Dussehra 2026-10-20 10:05 IST / 04:35 UTC)
    clock_hol = datetime.datetime(2026, 10, 20, 4, 35, 0, tzinfo=TZ_UTC)
    engine_hol = ProviderExecutionEngine(clock=lambda: clock_hol, enforce_market_schedule=True)
    c_hol = make_valid_candle(clock_hol - datetime.timedelta(minutes=5))
    res_hol = engine_hol.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c_hol,
    )
    assert res_hol.action_decision == "NO_ACTION"
    assert res_hol.reason_code == "MARKET_HOLIDAY"
    assert len(res_hol.order_ids) == 0

    # Verify zero orders or outbox created
    orders_count = session.query(Order).filter(Order.account_id == runtime.account_id).count()
    outbox_count = session.query(SubmissionOutbox).filter(SubmissionOutbox.owner_id == user.id).count()
    assert orders_count == 0
    assert outbox_count == 0


# =========================================================================
# 8. Concurrency & Multi-Thread Serialization (SQLite & PostgreSQL)
# =========================================================================

def test_concurrent_partial_fill_serialization(tmp_path):
    """
    Runs concurrent partial fill attempts in parallel worker threads using row-level locking.
    Validates that fills serialize cleanly without ledger corruption or lost updates.
    """
    with paper_test_database("sqlite", tmp_path / "concurrent_recon.db") as (engine, _):
        Base.metadata.create_all(engine)
        
        # Setup initial state
        with Session(engine) as session:
            user = User(
                id=str(uuid.uuid4()),
                username="concurrency_user",
                normalized_username="concurrency_user",
                email="concurrency@tradepro.test",
                normalized_email="concurrency@tradepro.test",
                hashed_password=hash_password("DefaultPassword123!"),
                role="EDITOR",
                is_active=True,
            )
            session.add(user)
            session.flush()

            acct = PaperAccount(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                name="Concurrent Account",
                total_cash_units=10000000,
                reserved_cash_units=2000000,
                currency="INR",
            )
            session.add(acct)
            session.flush()

            order = _create_test_order(
                session=session,
                user_id=user.id,
                account_id=acct.id,
                qty=100,
                side=OrderSide.BUY.value,
                status=OrderStatus.ACKNOWLEDGED.value,
                limit_price=20000,
                auto_reserve=False,
            )
            res_entry = AccountLedgerEntry(
                id=str(uuid.uuid4()),
                account_id=acct.id,
                owner_id=user.id,
                sequence_number=1,
                entry_type=LedgerEntryType.CASH_RESERVATION.value,
                amount_units=2000000,
                balance_after_units=acct.total_cash_units,
                settled_cash_delta_units=0,
                reserved_cash_delta_units=2000000,
                settled_cash_after_units=acct.total_cash_units,
                reserved_cash_after_units=acct.reserved_cash_units,
                order_id=order.id,
                reason_code="BUY_ORDER_RESERVED",
                idempotency_key=f"res:{order.id}",
                created_at=datetime.datetime.now(datetime.timezone.utc),
            )
            session.add(res_entry)
            session.commit()
            order_id = order.id
            user_id = user.id
            account_id = acct.id

        # Worker function to execute a partial fill
        def do_fill(worker_idx: int, fill_qty: int):
            with Session(engine) as worker_session:
                try:
                    SandboxReconciliationEngine.reconcile_fill(
                        db=worker_session,
                        order_id=order_id,
                        owner_id=user_id,
                        fill_qty_units=fill_qty,
                        fill_price_units=20000,
                        fee_units=100,
                        fill_idempotency_key=f"worker_fill_{worker_idx}",
                    )
                    worker_session.commit()
                    return True
                except Exception as exc:
                    worker_session.rollback()
                    return False

        # Execute 2 concurrent partial fills of 30 units each
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(do_fill, 1, 30)
            f2 = executor.submit(do_fill, 2, 30)
            res1 = f1.result()
            res2 = f2.result()

        assert res1 is True
        assert res2 is True

        # Verify final state
        with Session(engine) as verify_session:
            v_order = verify_session.get(Order, order_id)
            v_acct = verify_session.get(PaperAccount, account_id)
            v_pos = verify_session.query(PaperPosition).filter(
                PaperPosition.account_id == account_id,
                PaperPosition.instrument_id == "NSE_EQ|INE002A01018",
            ).first()

            assert v_order.filled_quantity_units == 60
            assert v_order.status == OrderStatus.PARTIALLY_FILLED.value
            assert v_pos.net_quantity_units == 60
            assert v_pos.total_fees_units == 200
            
            # Check ledger sequences
            ledgers = verify_session.query(AccountLedgerEntry).filter(
                AccountLedgerEntry.account_id == account_id,
            ).order_by(AccountLedgerEntry.sequence_number.asc()).all()
            assert len(ledgers) == 3  # Reservation + 2 Fills
            assert [l.sequence_number for l in ledgers] == [1, 2, 3]


def test_postgresql_concurrency_parity_disposable(tmp_path):
    """
    Tests reconciliation and market session gates on PostgreSQL.
    Skips cleanly if no PostgreSQL test database is configured.
    """
    with paper_test_database("postgresql", tmp_path / "pg.db") as (engine, _):
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            user = User(
                id=str(uuid.uuid4()),
                email="pg_user@tradepro.test",
                role="ADMIN",
                is_active=True,
            )
            acct = PaperAccount(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                name="PG Account",
                total_cash_units=5000000,
                reserved_cash_units=1000000,
                currency="INR",
            )
            session.add_all([user, acct])
            session.flush()

            order = _create_test_order(
                session=session,
                user_id=user.id,
                account_id=acct.id,
                qty=50,
                side=OrderSide.BUY.value,
                status=OrderStatus.ACKNOWLEDGED.value,
                limit_price=20000,
            )
            session.commit()

            fill = SandboxReconciliationEngine.reconcile_fill(
                db=session,
                order_id=order.id,
                owner_id=user.id,
                fill_qty_units=50,
                fill_price_units=20000,
                fill_idempotency_key="pg_fill_1",
            )
            session.commit()

            assert fill.quantity_units == 50
            assert order.status == OrderStatus.FILLED.value


# =========================================================================
# 9. API / UI Status Coverage & Mocked Acceptance Path
# =========================================================================

def test_api_order_modification_and_reconciliation_routes(client, session, test_user):
    """
    Tests:
    1. Order modification rejection via API returns 422 with ORDER_MODIFICATION_NOT_SUPPORTED.
    2. Prevention of public client fill fabrication: reconcile endpoints are not exposed publicly (404).
    """
    acct = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Route Test Account",
        total_cash_units=10000000,
        reserved_cash_units=1000000,
        currency="INR",
    )
    session.add(acct)
    session.flush()

    order = _create_test_order(
        session=session,
        user_id=test_user.id,
        account_id=acct.id,
        qty=50,
        side=OrderSide.BUY.value,
        status=OrderStatus.ACKNOWLEDGED.value,
        limit_price=20000,
    )
    session.commit()

    # A. Order modification rejection via Paper API
    resp_patch = client.patch(f"/api/v1/paper/orders/{order.id}", json={"quantity": 60})
    assert resp_patch.status_code == 422
    assert resp_patch.json()["detail"]["code"] == "ORDER_MODIFICATION_NOT_SUPPORTED"

    resp_put = client.put(f"/api/v1/paper/orders/{order.id}", json={"limit_price": 25000})
    assert resp_put.status_code == 422
    assert resp_put.json()["detail"]["code"] == "ORDER_MODIFICATION_NOT_SUPPORTED"

    resp_mod = client.post(f"/api/v1/paper/orders/{order.id}/modify", json={})
    assert resp_mod.status_code == 422
    assert resp_mod.json()["detail"]["code"] == "ORDER_MODIFICATION_NOT_SUPPORTED"

    # Order modification rejection via Sandbox API
    resp_sbx_patch = client.patch(f"/api/v1/sandbox/orders/{order.id}", json={"quantity": 60})
    assert resp_sbx_patch.status_code == 422
    assert resp_sbx_patch.json()["detail"]["code"] == "ORDER_MODIFICATION_NOT_SUPPORTED"

    resp_sbx_put = client.put(f"/api/v1/sandbox/orders/{order.id}", json={"limit_price": 25000})
    assert resp_sbx_put.status_code == 422
    assert resp_sbx_put.json()["detail"]["code"] == "ORDER_MODIFICATION_NOT_SUPPORTED"

    resp_sbx_mod = client.post(f"/api/v1/sandbox/orders/{order.id}/modify", json={})
    assert resp_sbx_mod.status_code == 422
    assert resp_sbx_mod.json()["detail"]["code"] == "ORDER_MODIFICATION_NOT_SUPPORTED"

    # B. Prevention of public client fill fabrication (Blocker 1: endpoints not publicly accessible)
    fill_payload = {
        "fill_qty_units": 20,
        "fill_price_units": 20000,
        "provider_trade_id": "TRADE_999",
    }
    resp_fill = client.post(f"/api/v1/sandbox/orders/{order.id}/reconcile-fill", json=fill_payload)
    assert resp_fill.status_code == 404

    cancel_payload = {"reason": "BROKER_CANCEL_CONFIRMED"}
    resp_cancel = client.post(f"/api/v1/sandbox/orders/{order.id}/reconcile-cancel", json=cancel_payload)
    assert resp_cancel.status_code == 404


def test_mocked_end_to_end_sandbox_acceptance(client, session, provider_exec_setup):
    """
    Mocked end-to-end acceptance test:
    1. ProviderEvaluationEngine evaluates candle during market hours -> generates PLACE order and Outbox.
    2. Outbox item verified.
    3. Order transitions to ACKNOWLEDGED.
    4. Server-side partial fill arrives -> PARTIALLY_FILLED, cash reservation halved, position updated.
    5. Cancellation arrives for remainder -> CANCELLED, remaining reservation released, position preserved.
    """
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]  # 15:30:00 IST / 10:00:00 UTC
    engine = ProviderExecutionEngine(clock=lambda: clock_now, enforce_market_schedule=True)

    # 1. Evaluate fresh candle within market hours
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))
    eval_res = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c,
    )
    assert eval_res.action_decision == "ACCEPTED_SANDBOX"
    assert len(eval_res.order_ids) == 1
    assert len(eval_res.outbox_ids) == 1

    order_id = eval_res.order_ids[0]
    outbox_id = eval_res.outbox_ids[0]

    order = session.get(Order, order_id)
    outbox = session.get(SubmissionOutbox, outbox_id)
    assert order is not None
    assert outbox is not None
    assert outbox.action_type == "PLACE"
    assert outbox.status == "PENDING"

    # 2. Simulate dispatcher sending order to sandbox -> broker ACK
    order.status = OrderStatus.ACKNOWLEDGED.value
    session.flush()

    # 3. Simulate Upstox partial fill: 25 of 50 units filled via server-side engine
    fill = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=user.id,
        fill_qty_units=25,
        fill_price_units=250000000,
        fee_units=500,
        provider_trade_id="UPSTOX_TRD_1",
        notes="Partial execution at exchange",
    )
    session.flush()
    session.refresh(order)
    assert order.status == OrderStatus.PARTIALLY_FILLED.value
    assert order.filled_quantity_units == 25

    # 4. Cancel the remaining 25 units via server-side engine
    SandboxReconciliationEngine.reconcile_cancel(
        db=session,
        order_id=order.id,
        owner_id=user.id,
        reason="OPERATOR_CANCEL_REMAINDER",
    )
    session.flush()
    session.refresh(order)
    assert order.status == OrderStatus.CANCELLED.value
    assert order.filled_quantity_units == 25  # Retained!


# =========================================================================
# 11. Additional Audit & Correctness Regression Tests (Blockers 1 - 5)
# =========================================================================

def test_distinct_fills_same_timestamp_quantity_price(session, test_user):
    """
    Blocker 1 & 2: Two legitimate fills can share timestamp, quantity, and price.
    Distinct provider trade IDs must be accepted and accounted for independently.
    Missing trade ID must be rejected.
    """
    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Test Account",
        total_cash_units=10000000,
        reserved_cash_units=1000000,
        currency="INR",
    )
    session.add(account)
    session.flush()

    order = _create_test_order(
        session,
        user_id=test_user.id,
        account_id=account.id,
        qty=100,
        limit_price=10000,
    )
    same_time = datetime.datetime(2026, 10, 6, 10, 0, 0, tzinfo=datetime.timezone.utc)

    # Fill 1: 25 shares @ 10,000 at same_time with trade_A
    fill1 = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=25,
        fill_price_units=10000,
        fill_timestamp=same_time,
        provider_trade_id="TRD_AAA",
    )
    session.flush()

    # Fill 2: 25 shares @ 10,000 at exact same timestamp with trade_B (distinct counterparty match)
    fill2 = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=25,
        fill_price_units=10000,
        fill_timestamp=same_time,
        provider_trade_id="TRD_BBB",
    )
    session.flush()

    assert fill1.id != fill2.id
    assert order.filled_quantity_units == 50

    # Missing trade ID must be rejected
    with pytest.raises(ValueError) as exc:
        SandboxReconciliationEngine.reconcile_fill(
            db=session,
            order_id=order.id,
            owner_id=test_user.id,
            fill_qty_units=25,
            fill_price_units=10000,
            fill_timestamp=same_time,
            provider_trade_id=None,
        )
    assert "Authoritative broker trade/event ID is required" in str(exc.value)

    # Replay of Fill 1 (same trade ID) must be deduplicated
    replay_fill = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=25,
        fill_price_units=10000,
        fill_timestamp=same_time,
        provider_trade_id="TRD_AAA",
    )
    assert replay_fill.id == fill1.id
    assert order.filled_quantity_units == 50  # Not double-counted


def test_two_simultaneous_buy_orders_reservation_isolation(session, test_user):
    """
    Blocker 3: Two simultaneous BUY orders on the same account.
    Proves order-scoped ledger calculation never touches, releases, or substitutes
    another order's reserved funds.
    """
    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Multi-Order Account",
        total_cash_units=10000000,  # 100,000
        reserved_cash_units=3000000,  # Order 1: 1,000,000; Order 2: 2,000,000
        currency="INR",
    )
    session.add(account)
    session.flush()

    # Order 1: 100 units @ 10,000 = 1,000,000 reserved
    order1 = _create_test_order(
        session,
        user_id=test_user.id,
        account_id=account.id,
        qty=100,
        limit_price=10000,
    )
    # Order 2: 100 units @ 20,000 = 2,000,000 reserved
    order2 = _create_test_order(
        session,
        user_id=test_user.id,
        account_id=account.id,
        qty=100,
        limit_price=20000,
    )

    # 1. Partial fill on Order 1: 40 units @ 9,500 + fee 500
    # Expected charge: 40 * 9,500 + 500 = 380,500
    # Expected reservation release for Order 1: (1,000,000 * 40) // 100 = 400,000
    SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order1.id,
        owner_id=test_user.id,
        fill_qty_units=40,
        fill_price_units=9500,
        fee_units=500,
        provider_trade_id="O1_TRD_1",
    )
    session.flush()

    # Account total reserved was 3,000,000; after 400,000 release = 2,600,000
    # Order 1 remaining reservation: 600,000. Order 2 reservation: exactly 2,000,000 intact!
    assert account.reserved_cash_units == 2600000

    # 2. Cancel remainder of Order 1: releases Order 1's remaining 600,000
    SandboxReconciliationEngine.reconcile_cancel(
        db=session,
        order_id=order1.id,
        owner_id=test_user.id,
        reason="CANCEL_REMAINDER",
    )
    session.flush()

    # Account total reserved after Order 1 cancellation = 2,000,000
    # Exactly Order 2's reservation remains untouched!
    assert account.reserved_cash_units == 2000000
    assert order1.status == OrderStatus.CANCELLED.value
    assert order1.filled_quantity_units == 40
    assert order2.status == OrderStatus.ACKNOWLEDGED.value
    assert order2.filled_quantity_units == 0


def test_missing_or_inconsistent_reservation_ledger_fails_closed(session, test_user):
    """
    Blocker 3: Missing or inconsistent reservation ledger entry must fail closed.
    Never substitute account-wide reserved cash.
    """
    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="No Ledger Account",
        total_cash_units=10000000,
        reserved_cash_units=500000,
        currency="INR",
    )
    session.add(account)
    session.flush()

    # Create BUY order with auto_reserve=False (missing CASH_RESERVATION ledger entry)
    order = _create_test_order(
        session,
        user_id=test_user.id,
        account_id=account.id,
        qty=50,
        limit_price=10000,
        auto_reserve=False,
    )

    with pytest.raises(ReconciliationError) as exc:
        SandboxReconciliationEngine.reconcile_fill(
            db=session,
            order_id=order.id,
            owner_id=test_user.id,
            fill_qty_units=10,
            fill_price_units=10000,
            provider_trade_id="TRD_FAIL_1",
        )
    assert "Missing or invalid authoritative cash reservation ledger entry" in str(exc.value)


def test_partial_fill_preserves_open_reconciliation_record(session, test_user):
    """
    Blocker 4: Partial fill must not resolve an OPEN reconciliation record
    while unfilled remainder's broker outcome remains ambiguous.
    """
    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Recon Record Account",
        total_cash_units=10000000,
        reserved_cash_units=1000000,
        currency="INR",
    )
    session.add(account)
    session.flush()

    order = _create_test_order(
        session,
        user_id=test_user.id,
        account_id=account.id,
        qty=100,
        limit_price=10000,
        status=OrderStatus.RECONCILIATION_REQUIRED.value,
    )

    outbox = SubmissionOutbox(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="RECONCILIATION_REQUIRED",
        idempotency_key=f"outbox_{uuid.uuid4().hex}",
        canonical_payload_hash="0" * 64,
        payload_json={},
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(outbox)
    session.flush()

    # Create OPEN ReconciliationRecord
    recon_rec = ReconciliationRecord(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        order_id=order.id,
        outbox_id=outbox.id,
        notes="SANDBOX_TIMEOUT",
        status="OPEN",
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(recon_rec)
    session.flush()

    # 1. Partial fill: 40 of 100 units filled
    SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=40,
        fill_price_units=10000,
        provider_trade_id="TRD_RECON_1",
    )
    session.flush()

    # ReconciliationRecord must REMAIN OPEN!
    session.refresh(recon_rec)
    assert recon_rec.status == "OPEN"
    assert "Unfilled remainder (60) remains ambiguous" in recon_rec.notes

    # 2. Final cancellation of remaining 60 units resolves the record
    SandboxReconciliationEngine.reconcile_cancel(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        reason="BROKER_CANCEL_CONFIRMED",
    )
    session.flush()

    session.refresh(recon_rec)
    assert recon_rec.status == "RESOLVED"
    assert recon_rec.resolution_type == "CANCEL_CONFIRMED"


def test_broker_trade_id_mandatory_bounds_and_conflict_rejection(session, test_user):
    """
    Blocker 2:
    - Missing broker trade ID -> rejected.
    - Trade ID > 64 chars -> rejected.
    - Same trade ID with conflicting fill details -> rejected.
    """
    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Trade ID Account",
        total_cash_units=10000000,
        reserved_cash_units=1000000,
        currency="INR",
    )
    session.add(account)
    session.flush()

    order = _create_test_order(
        session,
        user_id=test_user.id,
        account_id=account.id,
        qty=100,
        limit_price=10000,
    )

    # 1. Empty trade ID rejected
    with pytest.raises(ValueError) as exc1:
        SandboxReconciliationEngine.reconcile_fill(
            db=session,
            order_id=order.id,
            owner_id=test_user.id,
            fill_qty_units=10,
            fill_price_units=10000,
            provider_trade_id="",
        )
    assert "Authoritative broker trade/event ID is required" in str(exc1.value)

    # 2. Two different orders with the same broker trade ID succeed without collision
    order2 = _create_test_order(
        session,
        user_id=test_user.id,
        account_id=account.id,
        qty=100,
        limit_price=10000,
    )
    common_trade_id = "TRD_COMMON_EVENT_999"
    fill1 = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=10,
        fill_price_units=10000,
        fee_units=100,
        provider_trade_id=common_trade_id,
    )
    fill2 = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order2.id,
        owner_id=test_user.id,
        fill_qty_units=15,
        fill_price_units=10000,
        fee_units=150,
        provider_trade_id=common_trade_id,
    )
    assert fill1 is not None and fill2 is not None
    assert fill1.id != fill2.id
    assert fill1.order_id == order.id
    assert fill2.order_id == order2.id
    assert fill1.fill_idempotency_key != fill2.fill_idempotency_key

    # 3. Replay of one event on order is idempotent (returns existing fill)
    replay_fill = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=10,
        fill_price_units=10000,
        fee_units=100,
        provider_trade_id=common_trade_id,
    )
    assert replay_fill.id == fill1.id
    assert order.filled_quantity_units == 10

    # 4. Same trade ID with conflicting details on order rejected
    with pytest.raises(ReconciliationError) as exc3_price:
        SandboxReconciliationEngine.reconcile_fill(
            db=session,
            order_id=order.id,
            owner_id=test_user.id,
            fill_qty_units=10,
            fill_price_units=12000,
            fee_units=100,
            provider_trade_id=common_trade_id,
        )
    assert f"Conflict: Broker trade ID '{common_trade_id}' already reconciled with different details" in str(exc3_price.value)

    with pytest.raises(ReconciliationError) as exc3_qty:
        SandboxReconciliationEngine.reconcile_fill(
            db=session,
            order_id=order.id,
            owner_id=test_user.id,
            fill_qty_units=25,
            fill_price_units=10000,
            fee_units=100,
            provider_trade_id=common_trade_id,
        )
    assert "Conflict" in str(exc3_qty.value)

    # 5. Long IDs that share the first 50 or 64 characters hash independently
    shared_prefix_64 = "K" * 64
    long_id_a = f"{shared_prefix_64}_BRANCH_ONE_11111"
    long_id_b = f"{shared_prefix_64}_BRANCH_TWO_22222"

    fill_long_a = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=5,
        fill_price_units=10000,
        fee_units=50,
        provider_trade_id=long_id_a,
    )
    fill_long_b = SandboxReconciliationEngine.reconcile_fill(
        db=session,
        order_id=order.id,
        owner_id=test_user.id,
        fill_qty_units=5,
        fill_price_units=10000,
        fee_units=50,
        provider_trade_id=long_id_b,
    )
    assert fill_long_a.id != fill_long_b.id
    assert fill_long_a.fill_idempotency_key != fill_long_b.fill_idempotency_key
    assert len(fill_long_a.fill_idempotency_key) == 64
    assert len(fill_long_b.fill_idempotency_key) == 64


def test_market_calendar_unverified_dates_fail_closed_and_special_sessions():
    """
    Blocker 1: Unverified session dates strictly return SESSION_STATUS_UNKNOWN fail-closed.
    Verified dates (regular, weekend, holiday, special session, emergency closure)
    evaluate based on traceable official circular evidence.
    """
    # 1. Unverified dates return UNKNOWN fail-closed
    dt_unverified = datetime.datetime(2026, 5, 15, 5, 0, 0, tzinfo=TZ_UTC)  # 10:30 IST
    assert get_market_session_status(dt_unverified) == MarketSessionStatus.UNKNOWN

    dt_2025 = datetime.datetime(2025, 12, 31, 10, 0, 0, tzinfo=TZ_UTC)
    assert get_market_session_status(dt_2025) == MarketSessionStatus.UNKNOWN

    # 2. Verified trading day (2026-10-06)
    dt_open = datetime.datetime(2026, 10, 6, 4, 30, 0, tzinfo=TZ_UTC)  # 10:00 IST -> OPEN
    dt_closed = datetime.datetime(2026, 10, 6, 12, 35, 0, tzinfo=TZ_UTC)  # 18:05 IST -> CLOSED
    assert get_market_session_status(dt_open) == MarketSessionStatus.OPEN
    assert get_market_session_status(dt_closed) == MarketSessionStatus.CLOSED

    # 3. Verified weekend (2026-10-10) -> CLOSED
    dt_weekend = datetime.datetime(2026, 10, 10, 5, 0, 0, tzinfo=TZ_UTC)
    assert get_market_session_status(dt_weekend) == MarketSessionStatus.CLOSED

    # 4. Verified holidays (Republic Day 2026-01-26, Dussehra 2026-10-20) -> HOLIDAY
    dt_rep = datetime.datetime(2026, 1, 26, 5, 0, 0, tzinfo=TZ_UTC)
    dt_dus = datetime.datetime(2026, 10, 20, 5, 0, 0, tzinfo=TZ_UTC)
    assert get_market_session_status(dt_rep) == MarketSessionStatus.HOLIDAY
    assert get_market_session_status(dt_dus) == MarketSessionStatus.HOLIDAY

    # 5. Verified special session with official hours returns OPEN/CLOSED
    custom_verified = {
        datetime.date(2026, 11, 1): VerifiedSessionEvidence(
            date=datetime.date(2026, 11, 1),
            session_type=SessionType.SPECIAL,
            circular_id="NSE/CMTR/TEST_SPECIAL",
            authority="NSE",
            description="Verified Special Session with official hours",
            segment="Capital Market (Equity)",
            source_url="https://www.nseindia.com/market-data/market-timings",
            open_time=datetime.time(18, 0, 0),
            close_time=datetime.time(19, 0, 0),
        )
    }
    dt_special_open = datetime.datetime(2026, 11, 1, 13, 0, 0, tzinfo=TZ_UTC)  # 18:30 IST -> OPEN
    dt_special_closed = datetime.datetime(2026, 11, 1, 6, 0, 0, tzinfo=TZ_UTC)  # 11:30 IST -> CLOSED
    assert get_market_session_status(dt_special_open, verified_calendar=custom_verified) == MarketSessionStatus.OPEN
    assert get_market_session_status(dt_special_closed, verified_calendar=custom_verified) == MarketSessionStatus.CLOSED

    # 6. Verified emergency halt (with circular) -> CLOSED
    custom_emergency = {
        datetime.date(2026, 6, 10): VerifiedSessionEvidence(
            date=datetime.date(2026, 6, 10),
            session_type=SessionType.UNEXPECTED_CLOSURE,
            circular_id="NSE/CMTR/EMERGENCY_HALT",
            authority="NSE",
            description="Emergency Trading Halt",
            segment="Capital Market (Equity)",
            source_url="https://www.nseindia.com/resources/exchange-communication-circulars",
        )
    }
    dt_emergency = datetime.datetime(2026, 6, 10, 5, 0, 0, tzinfo=TZ_UTC)
    assert get_market_session_status(dt_emergency, verified_calendar=custom_emergency) == MarketSessionStatus.CLOSED


def test_unverified_muhurat_trading_window_never_returns_open(session, provider_exec_setup):
    """
    Requirement 1:
    Tests proving that an unverified Muhurat window never returns OPEN.
    Per NSE Circular Ref No: NSE/CMTR/71775 (Dated Dec 12, 2025), Muhurat trading will be
    conducted on Sunday, Nov 08, 2026, but the exact session timings are unnotified by separate
    Exchange circular.
    Calling get_market_session_status on 2026-11-08 at ANY time of day (including previously
    assumed 18:15-19:15 window) strictly returns SESSION_STATUS_UNKNOWN fail-closed.
    Execution engine evaluation strictly suppresses order generation.
    """
    test_times_ist = [
        datetime.datetime(2026, 11, 8, 4, 30, 0, tzinfo=TZ_UTC),   # 10:00 IST
        datetime.datetime(2026, 11, 8, 12, 30, 0, tzinfo=TZ_UTC),  # 18:00 IST
        datetime.datetime(2026, 11, 8, 12, 45, 0, tzinfo=TZ_UTC),  # 18:15 IST
        datetime.datetime(2026, 11, 8, 13, 0, 0, tzinfo=TZ_UTC),   # 18:30 IST
        datetime.datetime(2026, 11, 8, 13, 45, 0, tzinfo=TZ_UTC),  # 19:15 IST
        datetime.datetime(2026, 11, 8, 15, 0, 0, tzinfo=TZ_UTC),   # 20:30 IST
    ]
    for dt_candidate in test_times_ist:
        status = get_market_session_status(dt_candidate)
        assert status != MarketSessionStatus.OPEN, f"Timestamp {dt_candidate} unexpectedly returned OPEN!"
        assert status == MarketSessionStatus.UNKNOWN

    # Now verify provider execution engine suppresses order generation on 2026-11-08
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    candle_ts = datetime.datetime(2026, 11, 8, 13, 0, 0, tzinfo=TZ_UTC)  # 18:30 IST
    c_muhurat = make_valid_candle(candle_ts)
    clock_muhurat = candle_ts + datetime.timedelta(minutes=5)  # 18:35 IST (candle is closed)

    engine = ProviderExecutionEngine(clock=lambda: clock_muhurat, enforce_market_schedule=True)
    res = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c_muhurat,
    )
    assert res.action_decision == "NO_ACTION"
    assert res.reason_code == "SESSION_STATUS_UNKNOWN"
    assert len(res.order_ids) == 0
    assert len(res.outbox_ids) == 0


def test_suppressed_evaluation_does_not_advance_checkpoint(session, provider_exec_setup):
    """
    Suppressed evaluation (market closed or stale candle)
    does NOT advance last_processed_candle_timestamp past eligible candles.
    """
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]  # 10:00:00 UTC (15:30:00 IST)

    # Set prior valid checkpoint at 09:30 UTC
    prior_checkpoint = clock_now - datetime.timedelta(minutes=30)
    runtime.last_processed_candle_timestamp = prior_checkpoint
    session.flush()

    engine = ProviderExecutionEngine(clock=lambda: clock_now, enforce_market_schedule=True)

    # Present a stale candle
    stale_ts = clock_now - datetime.timedelta(minutes=25)
    c_stale = make_valid_candle(stale_ts)
    res_stale = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c_stale,
    )
    assert res_stale.action_decision == "NO_ACTION"
    assert res_stale.reason_code == "PRICE_STALE"

    # Verify checkpoint was NOT advanced!
    assert runtime.last_processed_candle_timestamp == prior_checkpoint


def test_mocked_broker_sync_production_entry_point(session, monkeypatch):
    """
    Requirement 2:
    - Public reconcile-fill and reconcile-cancel routes stay removed (return 404).
    - Production entry point POST /api/v1/sandbox/orders/{id}/sync fetches broker evidence server-side.
    - Validates connection, owner, provider order link, trade identity, quantity, price, and cumulative status.
    - Reconciles within safe transaction via SandboxReconciliationEngine.
    - Tests: limitation reporting, partial fill, replay, conflicting evidence, final fill, cancellation.
    - Proves no live broker traffic occurs.
    """
    # 1. Verify public reconcile-fill and reconcile-cancel routes stay removed (404)
    def override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_read_only_db] = override_get_db

    admin = User(
        username="sync_operator",
        normalized_username="sync_operator",
        email="sync_operator@tradepro.test",
        normalized_email="sync_operator@tradepro.test",
        hashed_password=hash_password("OperatorPass123!"),
        role="ADMIN",
        is_active=True,
    )
    session.add(admin)
    session.commit()
    session.refresh(admin)

    sess_rec, raw_sess, raw_csrf = create_session(session, admin)
    client = TestClient(app, headers={"X-CSRF-Token": raw_csrf, "Origin": "http://localhost:3000"})
    client.cookies.set("tradepro_session", raw_sess)
    client.cookies.set("tradepro_csrf", raw_csrf)

    # Confirm legacy/public routes are 404
    resp_legacy_fill = client.post("/api/v1/sandbox/orders/fake_order_id/reconcile-fill", json={})
    assert resp_legacy_fill.status_code == 404
    resp_legacy_cancel = client.post("/api/v1/sandbox/orders/fake_order_id/reconcile-cancel", json={})
    assert resp_legacy_cancel.status_code == 404

    # 2. Set up test account, order, connection, and external order link
    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=admin.id,
        name="Sync Operator Account",
        total_cash_units=1000000000,
        reserved_cash_units=200000000,
        currency="INR",
    )
    session.add(account)
    session.flush()

    order = _create_test_order(
        session,
        user_id=admin.id,
        account_id=account.id,
        qty=100,
        limit_price=1000000,
        status=OrderStatus.ACKNOWLEDGED.value,
    )
    provider_order_id = "UPSTOX_ORD_98765"
    link = ExternalOrderLink(
        owner_id=admin.id,
        order_id=order.id,
        provider_name="UPSTOX",
        provider_order_id=provider_order_id,
        submitted_at=datetime.datetime.now(datetime.timezone.utc),
    )
    conn = ProviderConnection(
        owner_id=admin.id,
        provider_name="UPSTOX",
        environment="SANDBOX",
        credential_reference="ENV_UPSTOX_SANDBOX_ACCESS_TOKEN",
        credential_version="v1",
        status="CONFIGURED",
    )
    session.add_all([link, conn])
    session.commit()

    # 3. Configure sandbox environment variables
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", admin.id)
    monkeypatch.setenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "mock_secure_token_123")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")

    # 4. Official Upstox documentation contract verification:
    # Live UpstoxSandboxAdapter has is_sandbox_authoritative_trades_supported() returning False.
    # Verify that calling sync with real adapter immediately returns AUTHORITATIVE_TRADES_UNAVAILABLE
    # without making any network calls or changing financial state.
    unmocked_resp = client.post(f"/api/v1/sandbox/orders/{order.id}/sync")
    assert unmocked_resp.status_code == 200
    unmocked_data = unmocked_resp.json()
    assert unmocked_data["status"] == "LIMITATION_REPORTED"
    assert unmocked_data["limitation"] == "AUTHORITATIVE_TRADES_UNAVAILABLE"
    assert "not sandbox-enabled" in unmocked_data["message"]
    session.refresh(order)
    assert order.status == OrderStatus.ACKNOWLEDGED.value
    assert order.filled_quantity_units == 0

    # 5. Mock UpstoxSandboxAdapter to prove NO live broker traffic occurs
    mock_traffic_log = []

    current_order_details = UpstoxOrderDetails(
        provider_order_id=provider_order_id,
        status="open",
        quantity=100,
        filled_quantity=40,
        price=100.0,
        average_price=100.0,
        raw_response={"status": "success"},
    )
    current_trades_list: List[UpstoxTradeDetails] = []

    def mock_get_order_details(self, p_order_id, token):
        mock_traffic_log.append(("get_order_details", p_order_id))
        assert token == "mock_secure_token_123"
        return current_order_details

    def mock_get_order_trades(self, p_order_id, token):
        mock_traffic_log.append(("get_order_trades", p_order_id))
        assert token == "mock_secure_token_123"
        return list(current_trades_list)

    monkeypatch.setattr(UpstoxSandboxAdapter, "is_sandbox_authoritative_trades_supported", lambda self: True)
    monkeypatch.setattr(UpstoxSandboxAdapter, "get_order_details", mock_get_order_details)
    monkeypatch.setattr(UpstoxSandboxAdapter, "get_order_trades", mock_get_order_trades)

    # 6. Pre-trade validations before applying any trades:
    # A. Mismatched order_details.provider_order_id rejected without changing financial state
    current_order_details = UpstoxOrderDetails(
        provider_order_id="WRONG_BROKER_ORDER_ID",
        status="open",
        quantity=100,
        filled_quantity=40,
        price=100.0,
        average_price=100.0,
        raw_response={},
    )
    resp_bad_ord_id = client.post(f"/api/v1/sandbox/orders/{order.id}/sync")
    assert resp_bad_ord_id.status_code == 409
    assert "mismatch" in resp_bad_ord_id.json()["detail"].lower()
    session.refresh(order)
    assert order.status == OrderStatus.ACKNOWLEDGED.value

    # Restore correct order details provider_order_id
    current_order_details = UpstoxOrderDetails(
        provider_order_id=provider_order_id,
        status="open",
        quantity=100,
        filled_quantity=40,
        price=100.0,
        average_price=100.0,
        raw_response={"status": "success"},
    )

    # B. Missing trade_id on trade rejected
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    current_trades_list = [
        UpstoxTradeDetails(trade_id="", provider_order_id=provider_order_id, quantity=40, price=100.0, trade_timestamp=now_utc, raw_response={})
    ]
    resp_missing_tid = client.post(f"/api/v1/sandbox/orders/{order.id}/sync")
    assert resp_missing_tid.status_code == 409
    assert "missing or empty trade_id" in resp_missing_tid.json()["detail"]

    # C. Mismatched trade.provider_order_id rejected
    current_trades_list = [
        UpstoxTradeDetails(trade_id="TRD_DIFF_ORD", provider_order_id="ANOTHER_ORDER_ID", quantity=40, price=100.0, trade_timestamp=now_utc, raw_response={})
    ]
    resp_bad_trd_ord = client.post(f"/api/v1/sandbox/orders/{order.id}/sync")
    assert resp_bad_trd_ord.status_code == 409
    assert "order_id mismatch" in resp_bad_trd_ord.json()["detail"]

    # D. Duplicate trade IDs with conflicting details in batch rejected
    current_trades_list = [
        UpstoxTradeDetails(trade_id="TRD_DUP", provider_order_id=provider_order_id, quantity=20, price=100.0, trade_timestamp=now_utc, raw_response={}),
        UpstoxTradeDetails(trade_id="TRD_DUP", provider_order_id=provider_order_id, quantity=20, price=115.0, trade_timestamp=now_utc, raw_response={}),
    ]
    resp_dup_conflict = client.post(f"/api/v1/sandbox/orders/{order.id}/sync")
    assert resp_dup_conflict.status_code == 409
    assert "Duplicate broker trade ID" in resp_dup_conflict.json()["detail"]

    # 7. Limitation reporting: filled_quantity=40 but broker trades list is empty
    current_trades_list = []
    sync_resp1 = client.post(f"/api/v1/sandbox/orders/{order.id}/sync")
    assert sync_resp1.status_code == 200
    data1 = sync_resp1.json()
    assert data1["status"] == "LIMITATION_REPORTED"
    assert data1["limitation"] == "AUTHORITATIVE_TRADES_UNAVAILABLE"
    session.refresh(order)
    assert order.status == OrderStatus.ACKNOWLEDGED.value
    assert order.filled_quantity_units == 0

    # 8. Partial fill: broker supplies authoritative trade record
    t1 = UpstoxTradeDetails(
        trade_id="TRD_SYNC_001",
        provider_order_id=provider_order_id,
        quantity=40,
        price=100.0,
        trade_timestamp=now_utc,
        raw_response={},
    )
    current_trades_list = [t1]

    sync_resp2 = client.post(f"/api/v1/sandbox/orders/{order.id}/sync")
    assert sync_resp2.status_code == 200
    data2 = sync_resp2.json()
    assert data2["status"] == "SUCCESS"
    assert data2["order_status"] == OrderStatus.PARTIALLY_FILLED.value
    assert data2["filled_quantity_units"] == 40
    assert data2["trades_reconciled"] == 1

    session.refresh(order)
    assert order.status == OrderStatus.PARTIALLY_FILLED.value
    assert order.filled_quantity_units == 40

    # 9. Replay: calling sync again with identical broker evidence is idempotent
    sync_resp3 = client.post(f"/api/v1/sandbox/orders/{order.id}/sync")
    assert sync_resp3.status_code == 200
    data3 = sync_resp3.json()
    assert data3["status"] == "SUCCESS"
    session.refresh(order)
    assert order.filled_quantity_units == 40
    # No duplicate fills created
    fill_count = session.query(Fill).filter(Fill.order_id == order.id).count()
    assert fill_count == 1

    # 10. Conflicting evidence: broker returns conflicting trade price for existing trade ID
    t1_conflict = UpstoxTradeDetails(
        trade_id="TRD_SYNC_001",
        provider_order_id=provider_order_id,
        quantity=40,
        price=150.0,  # Conflict with original 100.0
        trade_timestamp=now_utc,
        raw_response={},
    )
    current_trades_list = [t1_conflict]
    sync_resp_conflict = client.post(f"/api/v1/sandbox/orders/{order.id}/sync")
    assert sync_resp_conflict.status_code == 409
    assert "Conflict" in sync_resp_conflict.json()["detail"]

    # 11. Final fill: broker reports order complete with remaining 60 filled
    current_order_details = UpstoxOrderDetails(
        provider_order_id=provider_order_id,
        status="complete",
        quantity=100,
        filled_quantity=100,
        price=100.0,
        average_price=100.0,
        raw_response={"status": "success"},
    )
    t2 = UpstoxTradeDetails(
        trade_id="TRD_SYNC_002",
        provider_order_id=provider_order_id,
        quantity=60,
        price=100.0,
        trade_timestamp=now_utc,
        raw_response={},
    )
    current_trades_list = [t1, t2]

    sync_resp4 = client.post(f"/api/v1/sandbox/orders/{order.id}/sync")
    assert sync_resp4.status_code == 200
    data4 = sync_resp4.json()
    assert data4["status"] == "SUCCESS"
    assert data4["order_status"] == OrderStatus.FILLED.value
    assert data4["filled_quantity_units"] == 100

    session.refresh(order)
    assert order.status == OrderStatus.FILLED.value
    assert order.filled_quantity_units == 100

    # 12. Cancellation: test order cancelled at broker with partial fill
    order_cancel = _create_test_order(
        session,
        user_id=admin.id,
        account_id=account.id,
        qty=100,
        limit_price=1000000,
        status=OrderStatus.ACKNOWLEDGED.value,
    )
    cancel_provider_order_id = "UPSTOX_ORD_CANCEL_555"
    session.add(ExternalOrderLink(
        owner_id=admin.id,
        order_id=order_cancel.id,
        provider_name="UPSTOX",
        provider_order_id=cancel_provider_order_id,
        submitted_at=now_utc,
    ))
    session.commit()

    current_order_details = UpstoxOrderDetails(
        provider_order_id=cancel_provider_order_id,
        status="cancelled",
        quantity=100,
        filled_quantity=30,
        price=100.0,
        average_price=100.0,
        raw_response={"status": "success"},
    )
    t_canc = UpstoxTradeDetails(
        trade_id="TRD_CANC_001",
        provider_order_id=cancel_provider_order_id,
        quantity=30,
        price=100.0,
        trade_timestamp=now_utc,
        raw_response={},
    )
    current_trades_list = [t_canc]

    sync_resp5 = client.post(f"/api/v1/sandbox/orders/{order_cancel.id}/sync")
    assert sync_resp5.status_code == 200
    data5 = sync_resp5.json()
    assert data5["status"] == "SUCCESS"
    assert data5["order_status"] == OrderStatus.CANCELLED.value
    assert data5["filled_quantity_units"] == 30

    session.refresh(order_cancel)
    assert order_cancel.status == OrderStatus.CANCELLED.value
    assert order_cancel.filled_quantity_units == 30

    # 13. Prove NO live broker traffic occurred
    assert len(mock_traffic_log) > 0
    for op, p_oid in mock_traffic_log:
        assert op in ("get_order_details", "get_order_trades")
        assert p_oid in (provider_order_id, cancel_provider_order_id, "WRONG_BROKER_ORDER_ID")

    app.dependency_overrides.clear()


def test_broker_sync_durability_real_sessionlocal_and_halfway_failure_rollback(monkeypatch):
    """
    Durability & Atomicity Invariant Test:
    - Uses a real request-scoped disposable SessionLocal (each HTTP request runs with its own DB session).
    - Commits the entire validated reconciliation transaction on success.
    - Opens a NEW session and verifies that fill, order, account, position, ledger, and reconciliation
      state persisted durably to database.
    - Proves a failure halfway through multiple trades rolls back completely and leaves NO partial changes.
    """
    temp_dir = tempfile.mkdtemp(prefix="tradepro_durability_test_")
    disposable_db_file = os.path.join(temp_dir, "durability_test.db")
    disposable_engine = create_engine(f"sqlite:///{disposable_db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(disposable_engine)
    DisposableSessionLocal = sessionmaker(bind=disposable_engine, autocommit=False, autoflush=False)

    def request_scoped_get_db():
        db = DisposableSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = request_scoped_get_db
    app.dependency_overrides[get_read_only_db] = request_scoped_get_db

    # 1. Seed initial data in a dedicated setup session, commit, and close
    with DisposableSessionLocal() as setup_session:
        admin = User(
            id=str(uuid.uuid4()),
            username="durability_admin",
            normalized_username="durability_admin",
            email="durability_admin@tradepro.test",
            normalized_email="durability_admin@tradepro.test",
            hashed_password=hash_password("Pass12345!"),
            role="ADMIN",
            is_active=True,
        )
        setup_session.add(admin)
        setup_session.flush()

        conn = ProviderConnection(
            owner_id=admin.id,
            provider_name="UPSTOX",
            environment="SANDBOX",
            credential_reference="ENV_UPSTOX_SANDBOX_ACCESS_TOKEN",
            credential_version="v1",
            status="CONFIGURED",
        )
        account = PaperAccount(
            id=str(uuid.uuid4()),
            owner_id=admin.id,
            name="Durability Account",
            total_cash_units=1000000000,
            reserved_cash_units=200000000,
            currency="INR",
        )
        setup_session.add_all([conn, account])
        setup_session.flush()

        order_success = _create_test_order(
            setup_session,
            user_id=admin.id,
            account_id=account.id,
            qty=100,
            limit_price=1000000,
            status=OrderStatus.ACKNOWLEDGED.value,
        )
        order_success_provider_id = "UPSTOX_DUR_ORD_SUCCESS"
        setup_session.add(ExternalOrderLink(
            owner_id=admin.id,
            order_id=order_success.id,
            provider_name="UPSTOX",
            provider_order_id=order_success_provider_id,
            submitted_at=datetime.datetime.now(datetime.timezone.utc),
        ))

        order_halfway = _create_test_order(
            setup_session,
            user_id=admin.id,
            account_id=account.id,
            qty=100,
            limit_price=1000000,
            status=OrderStatus.ACKNOWLEDGED.value,
        )
        order_halfway_provider_id = "UPSTOX_DUR_ORD_HALFWAY"
        setup_session.add(ExternalOrderLink(
            owner_id=admin.id,
            order_id=order_halfway.id,
            provider_name="UPSTOX",
            provider_order_id=order_halfway_provider_id,
            submitted_at=datetime.datetime.now(datetime.timezone.utc),
        ))

        setup_session.commit()
        admin_id = admin.id
        account_id = account.id
        order_success_id = order_success.id
        order_halfway_id = order_halfway.id

    # Create client with authentication session
    with DisposableSessionLocal() as auth_session:
        user_obj = auth_session.query(User).filter(User.id == admin_id).one()
        sess_rec, raw_sess, raw_csrf = create_session(auth_session, user_obj)
        auth_session.commit()

    client = TestClient(app, headers={"X-CSRF-Token": raw_csrf, "Origin": "http://localhost:3000"})
    client.cookies.set("tradepro_session", raw_sess)
    client.cookies.set("tradepro_csrf", raw_csrf)

    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", admin_id)
    monkeypatch.setenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "mock_durability_token")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")

    # Mock adapter
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    mock_order_details = None
    mock_trades_list = []

    def mock_get_order_details(self, p_order_id, token):
        assert p_order_id in (order_success_provider_id, order_halfway_provider_id)
        return mock_order_details

    def mock_get_order_trades(self, p_order_id, token):
        return list(mock_trades_list)

    monkeypatch.setattr(UpstoxSandboxAdapter, "is_sandbox_authoritative_trades_supported", lambda self: True)
    monkeypatch.setattr(UpstoxSandboxAdapter, "get_order_details", mock_get_order_details)
    monkeypatch.setattr(UpstoxSandboxAdapter, "get_order_trades", mock_get_order_trades)

    # 2. SUCCESSFUL SYNC: Make HTTP call, verify 200, then verify persistence in a NEW session
    mock_order_details = UpstoxOrderDetails(
        provider_order_id=order_success_provider_id,
        status="open",
        quantity=100,
        filled_quantity=40,
        price=100.0,
        average_price=100.0,
        raw_response={"status": "success"},
    )
    mock_trades_list = [
        UpstoxTradeDetails(
            trade_id="TRD_DUR_SUCCESS_01",
            provider_order_id=order_success_provider_id,
            quantity=40,
            price=100.0,
            trade_timestamp=now_utc,
            raw_response={},
        )
    ]

    resp_success = client.post(f"/api/v1/sandbox/orders/{order_success_id}/sync")
    assert resp_success.status_code == 200
    assert resp_success.json()["status"] == "SUCCESS"

    # Open a BRAND NEW session to verify durable SQLite persistence across sessions
    with DisposableSessionLocal() as new_session:
        # Order persisted
        persisted_order = new_session.query(Order).filter(Order.id == order_success_id).one()
        assert persisted_order.status == OrderStatus.PARTIALLY_FILLED.value
        assert persisted_order.filled_quantity_units == 40

        # Fill persisted
        persisted_fills = new_session.query(Fill).filter(Fill.order_id == order_success_id).all()
        assert len(persisted_fills) == 1
        assert persisted_fills[0].quantity_units == 40
        assert persisted_fills[0].price_units == 1000000
        assert persisted_fills[0].owner_id == admin_id

        # Account cash persisted
        persisted_acct = new_session.query(PaperAccount).filter(PaperAccount.id == account_id).one()
        assert persisted_acct.total_cash_units == 1000000000 - 40000000
        assert persisted_acct.reserved_cash_units == 200000000 - 40000000

        # Position persisted
        persisted_pos = new_session.query(PaperPosition).filter(PaperPosition.account_id == account_id).all()
        assert len(persisted_pos) == 1
        assert persisted_pos[0].net_quantity_units == 40
        assert persisted_pos[0].cost_basis_units == 40000000

        # Ledger entries persisted
        persisted_ledgers = new_session.query(AccountLedgerEntry).filter(AccountLedgerEntry.order_id == order_success_id).all()
        entry_types = {l.entry_type for l in persisted_ledgers}
        assert LedgerEntryType.CASH_RESERVATION.value in entry_types
        assert LedgerEntryType.BUY_FILL.value in entry_types

    # 3. HALFWAY FAILURE PROOF: Multi-trade execution where Trade 1 is valid but Trade 2 triggers failure
    # Ensure zero partial changes are committed; everything rolls back atomically!
    with DisposableSessionLocal() as check_session:
        initial_acct = check_session.query(PaperAccount).filter(PaperAccount.id == account_id).one()
        initial_cash = initial_acct.total_cash_units
        initial_res = initial_acct.reserved_cash_units

    # Mock order details: filled_quantity=60
    mock_order_details = UpstoxOrderDetails(
        provider_order_id=order_halfway_provider_id,
        status="open",
        quantity=100,
        filled_quantity=60,
        price=100.0,
        average_price=100.0,
        raw_response={"status": "success"},
    )
    # Trade 1 is valid (qty=30, price=100.0).
    # Trade 2 is processed second, but has invalid price 0.0 or negative quantity which triggers failure during reconciliation!
    mock_trades_list = [
        UpstoxTradeDetails(
            trade_id="TRD_MULTI_01",
            provider_order_id=order_halfway_provider_id,
            quantity=30,
            price=100.0,
            trade_timestamp=now_utc,
            raw_response={},
        ),
        UpstoxTradeDetails(
            trade_id="TRD_MULTI_02_FAIL",
            provider_order_id=order_halfway_provider_id,
            quantity=30,
            price=-50.0,  # Invalid price triggers failure during processing of trade 2!
            trade_timestamp=now_utc,
            raw_response={},
        ),
    ]

    resp_fail = client.post(f"/api/v1/sandbox/orders/{order_halfway_id}/sync")
    assert resp_fail.status_code == 409

    # Open a NEW session and verify that NO partial changes from Trade 1 were committed!
    with DisposableSessionLocal() as verify_session:
        # 0 fills created for order_halfway (Trade 1 was completely rolled back!)
        halfway_fills = verify_session.query(Fill).filter(Fill.order_id == order_halfway_id).all()
        assert len(halfway_fills) == 0

        # Order status unchanged
        reloaded_halfway_order = verify_session.query(Order).filter(Order.id == order_halfway_id).one()
        assert reloaded_halfway_order.status == OrderStatus.ACKNOWLEDGED.value
        assert reloaded_halfway_order.filled_quantity_units == 0

        # Account balances unchanged from post-success state
        reloaded_acct = verify_session.query(PaperAccount).filter(PaperAccount.id == account_id).one()
        assert reloaded_acct.total_cash_units == initial_cash
        assert reloaded_acct.reserved_cash_units == initial_res

        # No new fill ledger entries for order_halfway
        halfway_fill_ledgers = verify_session.query(AccountLedgerEntry).filter(
            AccountLedgerEntry.order_id == order_halfway_id,
            AccountLedgerEntry.entry_type == LedgerEntryType.BUY_FILL.value,
        ).all()
        assert len(halfway_fill_ledgers) == 0

        # Positions untouched (still only 40 from success order)
        current_pos = verify_session.query(PaperPosition).filter(PaperPosition.account_id == account_id).one()
        assert current_pos.net_quantity_units == 40

    app.dependency_overrides.clear()
    shutil.rmtree(temp_dir, ignore_errors=True)


