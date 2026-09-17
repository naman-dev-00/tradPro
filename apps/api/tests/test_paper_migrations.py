import os
import pytest
from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

TEST_DB_URL = None

ALL_PAPER_TABLES = [
    "paper_accounts",
    "account_ledger_entries",
    "strategy_action_policies",
    "risk_policies",
    "strategy_runtimes",
    "order_intents",
    "orders",
    "fills",
    "paper_positions",
    "runtime_events",
    "order_events",
    "action_decisions",
    "risk_decisions",
    "kill_switches",
    "api_idempotency_records",
]

@pytest.fixture
def migration_env(tmp_path, monkeypatch):
    from src.database_safety import require_disposable_target
    url = "sqlite:///" + (tmp_path / "migration.db").as_posix()
    require_disposable_target(url)
    monkeypatch.setitem(globals(), "TEST_DB_URL", url)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    config = Config(os.path.join(root, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(root, "src", "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    yield config


def test_upgrade_0003_to_0004(migration_env):
    # 1. Upgrade to 0003
    command.upgrade(migration_env, "0003_auth_ownership")

    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
        assert "users" in tables
        assert "strategies" in tables
        assert "paper_accounts" not in tables

    # 2. Upgrade to 0004
    command.upgrade(migration_env, "0004_paper_runtime")

    with engine.connect() as conn:
        tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
        for t in ALL_PAPER_TABLES:
            assert t in tables, f"Table {t} missing from upgraded schema"
    engine.dispose()

def test_clean_downgrade_when_empty(migration_env):
    command.upgrade(migration_env, "0004_paper_runtime")

    # Empty schema downgrades cleanly to 0003
    command.downgrade(migration_env, "0003_auth_ownership")
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
        for t in ALL_PAPER_TABLES:
            assert t not in tables
        assert "users" in tables
        assert "strategies" in tables
    engine.dispose()

def _setup_base_records(conn):
    conn.execute(text("""
        INSERT INTO users (id, username, normalized_username, email, normalized_email, hashed_password, role, is_active, created_at, updated_at)
        VALUES ('u1', 'testuser', 'testuser', 'test@example.com', 'test@example.com', 'hash', 'EDITOR', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
    """))
    conn.execute(text("""
        INSERT INTO strategies (id, owner_id, name, timeframe, payload, created_at, updated_at)
        VALUES ('strat1', 'u1', 'Strat 1', '15m', '{"name":"S1"}', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
    """))
    conn.execute(text("""
        INSERT INTO paper_accounts (id, owner_id, name, currency, total_cash_units, reserved_cash_units, is_active, version, created_at, updated_at)
        VALUES ('acc1', 'u1', 'Acc 1', 'INR', 1000000, 0, 1, 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
    """))
    conn.execute(text("""
        INSERT INTO strategy_action_policies (id, owner_id, strategy_id, name, version, payload, is_active, created_at, updated_at)
        VALUES ('ap1', 'u1', 'strat1', 'AP 1', 1, '{"entry_mapping":{}}', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
    """))
    conn.execute(text("""
        INSERT INTO risk_policies (id, owner_id, name, version, payload, is_default, created_at, updated_at)
        VALUES ('rp1', 'u1', 'RP 1', 1, '{"max_open_orders": 10}', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
    """))
    conn.execute(text("""
        INSERT INTO strategy_runtimes (id, owner_id, strategy_id, action_policy_id, risk_policy_id, account_id, status, trading_mode, dataset_id, timeframe, strategy_snapshot, action_policy_snapshot, risk_policy_snapshot, instrument_spec_snapshot, fee_model_snapshot, slippage_model_snapshot, manifest_version, engine_version, runtime_schema_version, consecutive_errors, version, created_at, updated_at)
        VALUES ('rt1', 'u1', 'strat1', 'ap1', 'rp1', 'acc1', 'DRAFT', 'PAPER', 'synthetic_candidate_option_pe_23000_15m', '15m', '{}', '{}', '{}', '{}', '{}', '{}', '1.0.0', '1.0.0', '1.0.0', 0, 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
    """))
    conn.execute(text("""
        INSERT INTO order_intents (id, owner_id, runtime_id, action_mapping_id, requested_instrument_id, resolved_instrument_id, intent_type, reduce_only, side, quantity_units, order_type, time_in_force, source_candle_timestamp, source_evaluation_fingerprint, trigger_event_key, created_at)
        VALUES ('int1', 'u1', 'rt1', 'm1', 'inst1', 'inst1', 'ENTRY', 0, 'BUY', 50, 'MARKET', 'DAY', '2026-09-13 00:00:00', 'fp', 'tk1', '2026-09-13 00:00:00')
    """))
    conn.execute(text("""
        INSERT INTO orders (id, owner_id, runtime_id, intent_id, account_id, order_sequence_number, instrument_id, side, order_type, quantity_units, filled_quantity_units, status, version, created_at, updated_at)
        VALUES ('ord1', 'u1', 'rt1', 'int1', 'acc1', 1, 'inst1', 'BUY', 'MARKET', 50, 0, 'ACCEPTED', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
    """))
    conn.commit()

@pytest.mark.parametrize("target_table", ALL_PAPER_TABLES)
def test_all_15_tables_prevent_downgrade_when_nonempty(migration_env, target_table):
    command.upgrade(migration_env, "0004_paper_runtime")

    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        # Clear any prior rows to ensure clean state on Windows
        for t in reversed(ALL_PAPER_TABLES):
            conn.execute(text(f"DELETE FROM {t}"))
        conn.execute(text("DELETE FROM strategies"))
        conn.execute(text("DELETE FROM users"))
        conn.commit()

        _setup_base_records(conn)

        # Clear everything except target_table
        # Insert 1 row specifically into target_table if not already populated
        # If target_table is not already populated by _setup_base_records, insert it
        insert_sqls = {
            "account_ledger_entries": "INSERT INTO account_ledger_entries (id, account_id, owner_id, sequence_number, entry_type, settled_cash_delta_units, reserved_cash_delta_units, settled_cash_after_units, reserved_cash_after_units, amount_units, balance_after_units, reason_code, idempotency_key, created_at) VALUES ('le1', 'acc1', 'u1', 1, 'INITIAL_DEPOSIT', 100000, 0, 100000, 0, 100000, 100000, 'INIT', 'ik1', '2026-09-13 00:00:00')",
            "fills": "INSERT INTO fills (id, owner_id, order_id, account_id, instrument_id, side, quantity_units, price_units, fee_units, candle_timestamp, fill_idempotency_key, created_at) VALUES ('fill1', 'u1', 'ord1', 'acc1', 'inst1', 'BUY', 50, 10000, 100, '2026-09-13 00:00:00', 'fik1', '2026-09-13 00:00:00')",
            "paper_positions": "INSERT INTO paper_positions (id, owner_id, account_id, instrument_id, net_quantity_units, average_entry_price_units, cost_basis_units, gross_realized_pnl_units, total_fees_units, net_realized_pnl_units, last_mark_price_units, unrealized_pnl_units, updated_at) VALUES ('pos1', 'u1', 'acc1', 'inst1', 50, 10000, 500000, 0, 100, -100, 10000, 0, '2026-09-13 00:00:00')",
            "runtime_events": "INSERT INTO runtime_events (id, runtime_id, sequence_number, previous_status, new_status, actor, reason_code, created_at) VALUES ('re1', 'rt1', 1, 'NONE', 'DRAFT', 'u1', 'INIT', '2026-09-13 00:00:00')",
            "order_events": "INSERT INTO order_events (id, owner_id, order_id, sequence_number, previous_status, new_status, actor, reason_code, created_at) VALUES ('oe1', 'u1', 'ord1', 1, 'CREATED', 'ACCEPTED', 'OMS', 'RISK_PASSED', '2026-09-13 00:00:00')",
            "action_decisions": "INSERT INTO action_decisions (id, owner_id, runtime_id, candle_timestamp, action_mapping_id, decision, reason_code, created_at) VALUES ('ad1', 'u1', 'rt1', '2026-09-13 00:00:00', 'm1', 'TRIGGERED', 'ON_TRUE', '2026-09-13 00:00:00')",
            "risk_decisions": "INSERT INTO risk_decisions (id, owner_id, intent_id, passed, reason_code, message, created_at) VALUES ('rd1', 'u1', 'int1', 1, 'PASSED', 'Passed risk', '2026-09-13 00:00:00')",
            "kill_switches": "INSERT INTO kill_switches (id, target_key, scope, is_active, engaged_at) VALUES ('ks1', 'GLOBAL', 'GLOBAL', 1, '2026-09-13 00:00:00')",
            "api_idempotency_records": "INSERT INTO api_idempotency_records (id, key, owner_id, request_hash, response_status, response_body, created_at) VALUES ('ir1', 'key1', 'u1', 'hash1', 200, '{}', '2026-09-13 00:00:00')",
        }

        if target_table in insert_sqls:
            conn.execute(text(insert_sqls[target_table]))
            conn.commit()

    engine.dispose()

    # Downgrade MUST refuse
    with pytest.raises(RuntimeError, match="Downgrade refused: Non-empty paper trading table"):
        command.downgrade(migration_env, "0003_auth_ownership")

def test_global_kill_switch_uniqueness(migration_env):
    """Proves that a second GLOBAL switch row is rejected by the database unique constraint."""
    command.upgrade(migration_env, "0004_paper_runtime")
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO kill_switches (id, target_key, scope, is_active, engaged_at)
            VALUES ('ks1', 'GLOBAL', 'GLOBAL', 1, '2026-09-13 00:00:00')
        """))
        conn.commit()

        # Inserting second GLOBAL row MUST fail with integrity error
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO kill_switches (id, target_key, scope, is_active, engaged_at)
                VALUES ('ks2', 'GLOBAL', 'GLOBAL', 0, '2026-09-13 00:00:00')
            """))
            conn.commit()
    engine.dispose()

def test_negative_reserved_cash_constraint(migration_env):
    """Proves that reserved_cash_units < 0 is rejected by check constraint."""
    command.upgrade(migration_env, "0004_paper_runtime")
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        for t in reversed(ALL_PAPER_TABLES):
            conn.execute(text(f"DELETE FROM {t}"))
        conn.execute(text("DELETE FROM strategies"))
        conn.execute(text("DELETE FROM users"))
        conn.commit()

        conn.execute(text("""
            INSERT INTO users (id, username, normalized_username, email, normalized_email, hashed_password, role, is_active, created_at, updated_at)
            VALUES ('u1', 'testuser', 'testuser', 'test@example.com', 'test@example.com', 'hash', 'EDITOR', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.commit()

        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO paper_accounts (id, owner_id, name, currency, total_cash_units, reserved_cash_units, is_active, version, created_at, updated_at)
                VALUES ('acc_neg', 'u1', 'Neg Acc', 'INR', 100000, -500, 1, 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()
    engine.dispose()

def test_order_constraints(migration_env):
    """Proves that invalid order rows (zero quantity, excessive filled quantity) fail constraints."""
    command.upgrade(migration_env, "0004_paper_runtime")
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        for t in reversed(ALL_PAPER_TABLES):
            conn.execute(text(f"DELETE FROM {t}"))
        conn.execute(text("DELETE FROM strategies"))
        conn.execute(text("DELETE FROM users"))
        conn.commit()

        _setup_base_records(conn)

        # Zero quantity
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO orders (id, owner_id, runtime_id, intent_id, account_id, order_sequence_number, instrument_id, side, order_type, quantity_units, filled_quantity_units, status, version, created_at, updated_at)
                VALUES ('ord_bad1', 'u1', 'rt1', 'int1', 'acc1', 3, 'inst1', 'BUY', 'MARKET', 0, 0, 'ACCEPTED', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()

        conn.rollback()

        # Filled > quantity
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO orders (id, owner_id, runtime_id, intent_id, account_id, order_sequence_number, instrument_id, side, order_type, quantity_units, filled_quantity_units, status, version, created_at, updated_at)
                VALUES ('ord_bad2', 'u1', 'rt1', 'int1', 'acc1', 4, 'inst1', 'BUY', 'MARKET', 50, 60, 'ACCEPTED', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()
    engine.dispose()

def test_database_enforced_owner_consistency(migration_env):
    """
    Item 3: Proves that composite foreign keys enforce owner consistency at the database engine level.
    Direct SQL inserts where child owner_id != parent owner_id are rejected by the database.
    """
    from src.database import create_db_engine
    command.upgrade(migration_env, "0004_paper_runtime")
    engine = create_db_engine(TEST_DB_URL)
    with engine.connect() as conn:
        for t in reversed(ALL_PAPER_TABLES):
            conn.execute(text(f"DELETE FROM {t}"))
        conn.execute(text("DELETE FROM strategies"))
        conn.execute(text("DELETE FROM users"))
        conn.commit()

        _setup_base_records(conn)
        # Seed second distinct user
        conn.execute(text("""
            INSERT INTO users (id, username, normalized_username, email, normalized_email, hashed_password, role, is_active, created_at, updated_at)
            VALUES ('u2', 'user2', 'user2', 'u2@example.com', 'u2@example.com', 'hash', 'EDITOR', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.commit()

        # 1. Runtime owned by User 2 referencing Account owned by User 1 -> REJECTED
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO strategy_runtimes (id, owner_id, strategy_id, action_policy_id, risk_policy_id, account_id, status, trading_mode, dataset_id, timeframe, created_at, updated_at)
                VALUES ('rt_x1', 'u2', 'strat1', 'ap1', 'rp1', 'acc1', 'DRAFT', 'PAPER', 'synthetic_candidate_option_pe_23000_15m', '15m', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

        # 2. Runtime owned by User 2 referencing Strategy owned by User 1 -> REJECTED
        conn.execute(text("""
            INSERT INTO paper_accounts (id, owner_id, name, currency, total_cash_units, reserved_cash_units, is_active, version, created_at, updated_at)
            VALUES ('acc2', 'u2', 'Acc 2', 'INR', 1000000, 0, 1, 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.commit()
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO strategy_runtimes (id, owner_id, strategy_id, action_policy_id, risk_policy_id, account_id, status, trading_mode, dataset_id, timeframe, created_at, updated_at)
                VALUES ('rt_x2', 'u2', 'strat1', 'ap1', 'rp1', 'acc2', 'DRAFT', 'PAPER', 'synthetic_candidate_option_pe_23000_15m', '15m', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

        # 3. OrderIntent owned by User 2 referencing Runtime owned by User 1 -> REJECTED
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO order_intents (id, owner_id, runtime_id, action_mapping_id, requested_instrument_id, resolved_instrument_id, intent_type, side, quantity_units, order_type, time_in_force, source_candle_timestamp, source_evaluation_fingerprint, trigger_event_key, created_at)
                VALUES ('int_x3', 'u2', 'rt1', 'm1', 'inst1', 'inst1', 'ENTRY', 'BUY', 50, 'MARKET', 'DAY', '2026-09-13 00:00:00', 'fp', 'key_x3', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

        # 4. Order owned by User 2 referencing Runtime owned by User 1 -> REJECTED
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO orders (id, owner_id, runtime_id, intent_id, account_id, order_sequence_number, instrument_id, side, order_type, quantity_units, status, version, created_at, updated_at)
                VALUES ('ord_x4', 'u2', 'rt1', 'int1', 'acc2', 99, 'inst1', 'BUY', 'MARKET', 50, 'ACCEPTED', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

        # 5. Fill owned by User 2 referencing Order owned by User 1 -> REJECTED
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO fills (id, owner_id, order_id, account_id, instrument_id, side, quantity_units, price_units, fee_units, candle_timestamp, fill_idempotency_key, created_at)
                VALUES ('fill_x5', 'u2', 'ord1', 'acc2', 'inst1', 'BUY', 50, 10000, 2000, '2026-09-13 00:00:00', 'fill_key_x5', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

        # 6. Position owned by User 2 referencing Account owned by User 1 -> REJECTED
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO paper_positions (id, owner_id, account_id, instrument_id, net_quantity_units, updated_at)
                VALUES ('pos_x6', 'u2', 'acc1', 'inst1', 50, '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

        # 7. Ledger entry owned by User 2 referencing Account owned by User 1 -> REJECTED
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO account_ledger_entries (id, owner_id, account_id, sequence_number, entry_type, reason_code, idempotency_key, created_at)
                VALUES ('led_x7', 'u2', 'acc1', 99, 'DEPOSIT', 'DEPOSIT', 'idem_x7', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

        # 8. RiskDecision owned by User 2 referencing OrderIntent owned by User 1 -> REJECTED
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO risk_decisions (id, owner_id, intent_id, passed, reason_code, message, created_at)
                VALUES ('rd_x8', 'u2', 'int1', 1, 'PASSED', 'Passed risk', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

    engine.dispose()
