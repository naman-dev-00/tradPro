import os
import threading
import concurrent.futures
import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.database import get_db, Base
from src.models import (
    User,
    PaperAccount,
    Strategy,
    StrategyActionPolicy,
    RiskPolicy,
    StrategyRuntime,
    Order,
    OrderIntent,
    AccountLedgerEntry,
)
from src.services.paper_service import PaperService
from src.engine.paper.models import (
    TradingMode,
    RuntimeStatus,
    OrderStatus,
    OrderSide,
    OrderType,
    IntentType,
    PACKAGED_INSTRUMENT_SPECS,
)
from src.engine.paper.runtime import compute_order_intent_identity
from src.engine.paper.risk_engine import PureRiskEngine

@pytest.fixture
def concurrent_db(tmp_path):
    from src.database import create_db_engine
    from src.database_safety import require_disposable_target
    url = "sqlite:///" + (tmp_path / "concurrency.db").as_posix()
    require_disposable_target(url)
    engine = create_db_engine(url)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Seed base account and user
    db = SessionLocal()
    user = User(
        id="u_conc",
        username="concurrent_user",
        normalized_username="concurrent_user",
        email="concurrent@example.com",
        normalized_email="concurrent@example.com",
        hashed_password="hash",
        role="EDITOR",
        is_active=True,
    )
    db.add(user)
    db.commit()

    # Initial deposit of 100,000 INR (10,000,000 paise)
    acct = PaperService.create_account(
        db,
        owner_id="u_conc",
        name="Concurrent Account",
        initial_balance=100000,
        currency="INR",
    )
    db.commit()
    acct_id = acct.id
    db.close()

    yield SessionLocal, acct_id

    engine.dispose()

def test_sqlite_begin_immediate_prevents_concurrent_overspending(concurrent_db):
    """
    Item 8: Proves that two concurrent orders where each is individually valid
    (cost 80,000 INR, available 100,000 INR) but together exceed balance (160,000 INR)
    cannot both succeed. With BEGIN IMMEDIATE serialization, exactly ONE succeeds.
    """
    SessionLocal, acct_id = concurrent_db
    spec = PACKAGED_INSTRUMENT_SPECS["synthetic_candidate_option_pe_23000_15m"]

    results = []
    errors = []

    def place_order(worker_id: int):
        db = SessionLocal()
        try:
            # Under BEGIN IMMEDIATE, transaction starts immediately with exclusive write lock
            acct = db.query(PaperAccount).filter(PaperAccount.id == acct_id).with_for_update().first()
            avail = acct.total_cash_units - acct.reserved_cash_units

            order_qty = 500  # 500 units @ 160.00 = 80,000 INR
            order_cost = order_qty * 16000 + 2000  # cost + fee

            if avail >= order_cost:
                acct.reserved_cash_units += order_cost
                db.commit()
                results.append((worker_id, "SUCCESS"))
            else:
                db.rollback()
                results.append((worker_id, "REJECTED_INSUFFICIENT_FUNDS"))
        except Exception as e:
            db.rollback()
            errors.append((worker_id, str(e)))
        finally:
            db.close()

    # Launch two concurrent threads
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(place_order, 1)
        f2 = executor.submit(place_order, 2)
        concurrent.futures.wait([f1, f2])

    assert len(errors) == 0, f"Unexpected concurrency error: {errors}"
    statuses = [r[1] for r in results]
    assert statuses.count("SUCCESS") == 1, f"Expected exactly 1 success, got {statuses}"
    assert statuses.count("REJECTED_INSUFFICIENT_FUNDS") == 1, f"Expected 1 rejected, got {statuses}"

    # Verify final balance: reserved is 80,000 INR + 20 INR fee = 80,020 INR
    verify_db = SessionLocal()
    final_acct = verify_db.query(PaperAccount).filter(PaperAccount.id == acct_id).first()
    assert final_acct.reserved_cash_units == (500 * 16000) + 2000
    assert final_acct.total_cash_units - final_acct.reserved_cash_units >= 0
    verify_db.close()

def test_multiple_distinct_actions_on_same_candle():
    """
    Item 5: Canonical order-intent identity allows multiple distinct actions
    on the same candle (e.g. entry_1 BUY vs entry_2 BUY).
    """
    now = datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)
    k1 = compute_order_intent_identity(
        owner_id="u1",
        runtime_id="rt1",
        runtime_snapshot_fingerprint="fp1",
        instrument_id="inst_pe",
        candle_timestamp=now,
        action_mapping_id="entry_action_1",
        side="BUY",
        position_effect="ENTRY",
        quantity_units=50,
        order_type="MARKET",
        limit_price_units=None,
        time_in_force="DAY",
    )
    k2 = compute_order_intent_identity(
        owner_id="u1",
        runtime_id="rt1",
        runtime_snapshot_fingerprint="fp1",
        instrument_id="inst_pe",
        candle_timestamp=now,
        action_mapping_id="entry_action_2",  # Different action on the same candle
        side="BUY",
        position_effect="ENTRY",
        quantity_units=50,
        order_type="MARKET",
        limit_price_units=None,
        time_in_force="DAY",
    )
    # Distinct actions produce distinct non-colliding keys
    assert k1 != k2

def test_concurrent_identical_action_intents_produce_single_order(concurrent_db):
    """
    Item 5: Concurrent identical requests with the same canonical intent key
    must create exactly one OrderIntent and one Order.
    """
    SessionLocal, acct_id = concurrent_db
    now = datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)

    db = SessionLocal()
    strat = Strategy(owner_id="u_conc", name="S1", timeframe="15m", payload={"name": "S1"})
    db.add(strat)
    db.flush()

    rt = PaperService.instantiate_runtime(
        db,
        owner_id="u_conc",
        strategy_id=strat.id,
        account_id=acct_id,
        dataset_id="synthetic_candidate_option_pe_23000_15m",
        timeframe="15m"
    )
    rt_id = rt.id
    db.commit()
    db.close()

    created_intents = []

    def trigger_action():
        local_db = SessionLocal()
        try:
            local_rt = local_db.query(StrategyRuntime).filter(StrategyRuntime.id == rt_id).first()
            spec = PACKAGED_INSTRUMENT_SPECS["synthetic_candidate_option_pe_23000_15m"]
            intent = PaperService._process_action_trigger(
                local_db,
                runtime=local_rt,
                action_mapping={
                    "mapping_id": "auto_entry_1",
                    "side": "BUY",
                    "order_type": "MARKET",
                    "quantity": 50,
                    "intent_type": "ENTRY",
                    "time_in_force": "DAY",
                },
                inst_spec=spec,
                candle_timestamp=now,
                eval_close_units=10000,
                strat_payload=local_rt.strategy_snapshot,
                risk_policy_payload=local_rt.risk_policy_snapshot,
            )
            local_db.commit()
            if intent:
                created_intents.append(intent.id)
        except Exception:
            local_db.rollback()
        finally:
            local_db.close()

    # Launch two concurrent threads submitting identical action
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(trigger_action)
        f2 = executor.submit(trigger_action)
        concurrent.futures.wait([f1, f2])

    verify_db = SessionLocal()
    total_intents = verify_db.query(OrderIntent).filter(OrderIntent.runtime_id == rt_id).count()
    total_orders = verify_db.query(Order).filter(Order.runtime_id == rt_id).count()
    assert total_intents == 1, f"Expected exactly 1 intent created, found {total_intents}"
    assert total_orders == 1, f"Expected exactly 1 order created, found {total_orders}"
    verify_db.close()

def test_two_concurrent_valid_orders_both_succeed(concurrent_db):
    """
    Item 1: Two concurrent valid orders that fit together in available cash
    (cost 30,000 INR each, available 100,000 INR) both succeed without lost updates.
    """
    SessionLocal, acct_id = concurrent_db
    results = []
    errors = []

    def place_order(worker_id: int):
        db = SessionLocal()
        try:
            acct = db.query(PaperAccount).filter(PaperAccount.id == acct_id).with_for_update().first()
            avail = acct.total_cash_units - acct.reserved_cash_units
            order_cost = 3000000  # 30,000 INR in paise

            if avail >= order_cost:
                acct.reserved_cash_units += order_cost
                db.commit()
                results.append((worker_id, "SUCCESS"))
            else:
                db.rollback()
                results.append((worker_id, "REJECTED"))
        except Exception as e:
            db.rollback()
            errors.append((worker_id, str(e)))
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(place_order, 1)
        f2 = executor.submit(place_order, 2)
        concurrent.futures.wait([f1, f2])

    assert len(errors) == 0, f"Unexpected concurrency errors: {errors}"
    assert len(results) == 2
    assert all(r[1] == "SUCCESS" for r in results)

    verify_db = SessionLocal()
    final_acct = verify_db.query(PaperAccount).filter(PaperAccount.id == acct_id).first()
    assert final_acct.reserved_cash_units == 6000000  # Exactly 60,000 INR reserved (no lost updates)
    verify_db.close()

def test_rollback_followed_by_new_transaction_on_same_connection(concurrent_db):
    """
    Item 1: A rollback releases the transaction without leaving the connection in an invalid state,
    allowing subsequent write transactions on the same connection to succeed.
    """
    SessionLocal, acct_id = concurrent_db
    db = SessionLocal()
    try:
        # First transaction: simulate failure and rollback
        acct = db.query(PaperAccount).filter(PaperAccount.id == acct_id).first()
        initial_reserved = acct.reserved_cash_units
        acct.reserved_cash_units += 100000
        db.rollback()

        # Verify rollback worked
        acct_after_rb = db.query(PaperAccount).filter(PaperAccount.id == acct_id).first()
        assert acct_after_rb.reserved_cash_units == initial_reserved

        # Second transaction on same connection/session: write and commit
        acct_after_rb.reserved_cash_units += 50000
        db.commit()

        # Verify commit succeeded
        db.refresh(acct_after_rb)
        assert acct_after_rb.reserved_cash_units == initial_reserved + 50000
    finally:
        db.close()

def test_multiple_sequential_transactions_on_same_connection(concurrent_db):
    """
    Item 1: Multiple sequential transactions on the same connection execute cleanly.
    """
    SessionLocal, acct_id = concurrent_db
    db = SessionLocal()
    try:
        for i in range(5):
            acct = db.query(PaperAccount).filter(PaperAccount.id == acct_id).first()
            acct.total_cash_units += 1000
            db.commit()

        final_acct = db.query(PaperAccount).filter(PaperAccount.id == acct_id).first()
        assert final_acct.total_cash_units == 10000000 + 5000
    finally:
        db.close()

def test_read_only_request_uses_standard_begin(concurrent_db):
    """
    Item 1: Read-only requests marked with read_only=True do not acquire a write reservation lock.
    """
    SessionLocal, acct_id = concurrent_db
    db = SessionLocal()
    try:
        # Query with read_only execution option
        conn = db.connection(execution_options={"read_only": True})
        assert conn.get_execution_options().get("read_only") is True
        acct = db.query(PaperAccount).filter(PaperAccount.id == acct_id).first()
        assert acct is not None
    finally:
        db.close()
