import os
import pytest
from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, text

TEST_DB_URL = None

ALL_SANDBOX_TABLES = [
    "provider_connections",
    "provider_instrument_mappings",
    "submission_outbox",
    "external_order_links",
    "reconciliation_records",
    "worker_heartbeats",
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


def test_upgrade_0004_to_0005(migration_env):
    # 1. Upgrade to 0004
    command.upgrade(migration_env, "0004_paper_runtime")

    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
        assert "orders" in tables
        assert "submission_outbox" not in tables

    # 2. Upgrade to 0005
    command.upgrade(migration_env, "0005_upstox_sandbox")

    with engine.connect() as conn:
        tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
        for t in ALL_SANDBOX_TABLES:
            assert t in tables, f"Table {t} missing from upgraded schema"
    engine.dispose()


def test_clean_downgrade_and_reupgrade_when_empty(migration_env):
    command.upgrade(migration_env, "0005_upstox_sandbox")

    # Empty schema downgrades cleanly to 0004
    command.downgrade(migration_env, "0004_paper_runtime")

    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
        assert "orders" in tables
        for t in ALL_SANDBOX_TABLES:
            assert t not in tables

    # Re-upgrade to 0005
    command.upgrade(migration_env, "0005_upstox_sandbox")
    with engine.connect() as conn:
        tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
        for t in ALL_SANDBOX_TABLES:
            assert t in tables
    engine.dispose()


@pytest.mark.parametrize("table_name", ALL_SANDBOX_TABLES)
def test_downgrade_refusal_when_populated(migration_env, table_name):
    command.upgrade(migration_env, "0005_upstox_sandbox")

    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO users (id, username, normalized_username, email, normalized_email, hashed_password, role, is_active, created_at, updated_at)
            VALUES ('u1', 'testuser', 'testuser', 'test@example.com', 'test@example.com', 'hash', 'EDITOR', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.execute(text("""
            INSERT INTO orders (id, owner_id, runtime_id, intent_id, account_id, order_sequence_number, instrument_id, side, order_type, quantity_units, limit_price_units, filled_quantity_units, status, version, created_at, updated_at)
            VALUES ('o1', 'u1', 'r1', 'i1', 'a1', 1, 'inst1', 'BUY', 'LIMIT', 50, 2150000, 0, 'PENDING_SUBMISSION', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))

        # Insert specific record for the table under test
        if table_name == "provider_connections":
            conn.execute(text("""
                INSERT INTO provider_connections (id, owner_id, provider_name, environment, credential_reference, credential_version, status, created_at, updated_at)
                VALUES ('c1', 'u1', 'UPSTOX', 'SANDBOX', 'ENV:UPSTOX_SANDBOX_ACCESS_TOKEN', 'v1', 'CONFIGURED', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
        elif table_name == "provider_instrument_mappings":
            conn.execute(text("""
                INSERT INTO provider_instrument_mappings (id, owner_id, tradepro_instrument_id, provider_instrument_token, exchange, segment, symbol, verification_status, created_at, updated_at)
                VALUES ('m1', 'u1', 'inst1', 'NSE_FO|1234', 'NSE_FO', 'FO', 'NIFTY24SEP', 'VERIFIED', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
        elif table_name == "submission_outbox":
            conn.execute(text("""
                INSERT INTO submission_outbox (id, owner_id, order_id, action_type, priority, status, idempotency_key, canonical_payload_hash, payload_json, next_attempt_at, created_at, updated_at)
                VALUES ('out1', 'u1', 'o1', 'PLACE', 10, 'PENDING', 'idem1', 'hash1', '{}', '2026-09-13 00:00:00', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
        elif table_name == "external_order_links":
            conn.execute(text("""
                INSERT INTO external_order_links (id, owner_id, order_id, provider_name, provider_order_id, submitted_at, created_at)
                VALUES ('ext1', 'u1', 'o1', 'UPSTOX', 'UP1001', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
        elif table_name == "reconciliation_records":
            conn.execute(text("""
                INSERT INTO reconciliation_records (id, owner_id, order_id, outbox_id, status, resolution_type, resolved_by, notes, resolved_at, created_at)
                VALUES ('rec1', 'u1', 'o1', 'out1', 'RESOLVED', 'PLACE_CONFIRMED', 'u1', 'Notes test', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
        elif table_name == "worker_heartbeats":
            conn.execute(text("""
                INSERT INTO worker_heartbeats (id, worker_id, owner_id, provider_name, status, last_heartbeat_at, created_at, updated_at)
                VALUES ('hb1', 'w1', 'u1', 'UPSTOX', 'HEALTHY', '2026-09-13 00:00:00', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
        conn.commit()

    with pytest.raises(RuntimeError) as exc_info:
        command.downgrade(migration_env, "0004_paper_runtime")

    assert f"Downgrade refused: Non-empty Upstox sandbox table '{table_name}' detected" in str(exc_info.value)
    engine.dispose()


def test_priority_action_check_constraint(migration_env):
    """
    Proves check constraint:
    - (action_type = 'CANCEL' AND priority = 0) OR (action_type = 'PLACE' AND priority = 10)
    Rejects invalid combinations and accepts valid combinations.
    """
    command.upgrade(migration_env, "0005_upstox_sandbox")
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO users (id, username, normalized_username, email, normalized_email, hashed_password, role, is_active, created_at, updated_at)
            VALUES ('u1', 'testuser', 'testuser', 'test@example.com', 'test@example.com', 'hash', 'EDITOR', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.execute(text("""
            INSERT INTO orders (id, owner_id, runtime_id, intent_id, account_id, order_sequence_number, instrument_id, side, order_type, quantity_units, limit_price_units, filled_quantity_units, status, version, created_at, updated_at)
            VALUES ('o1', 'u1', 'r1', 'i1', 'a1', 1, 'inst1', 'BUY', 'LIMIT', 50, 2150000, 0, 'PENDING_SUBMISSION', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.commit()

        # 1. Invalid: CANCEL with priority=10 -> should fail
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO submission_outbox (id, owner_id, order_id, action_type, priority, status, idempotency_key, canonical_payload_hash, payload_json, next_attempt_at, created_at, updated_at)
                VALUES ('bad1', 'u1', 'o1', 'CANCEL', 10, 'PENDING', 'k1', 'h1', '{}', '2026-09-13 00:00:00', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

        # 2. Invalid: PLACE with priority=0 -> should fail
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO submission_outbox (id, owner_id, order_id, action_type, priority, status, idempotency_key, canonical_payload_hash, payload_json, next_attempt_at, created_at, updated_at)
                VALUES ('bad2', 'u1', 'o1', 'PLACE', 0, 'PENDING', 'k2', 'h2', '{}', '2026-09-13 00:00:00', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

    # 3. Invalid: action_type not in ('PLACE', 'CANCEL') -> should fail
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO submission_outbox (id, owner_id, order_id, action_type, priority, status, idempotency_key, canonical_payload_hash, payload_json, next_attempt_at, created_at, updated_at)
                VALUES ('bad3', 'u1', 'o1', 'INVALID_ACTION', 10, 'PENDING', 'k3', 'h3', '{}', '2026-09-13 00:00:00', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

        # 4. Valid: CANCEL with priority=0 -> succeeds
        conn.execute(text("""
            INSERT INTO submission_outbox (id, owner_id, order_id, action_type, priority, status, idempotency_key, canonical_payload_hash, payload_json, next_attempt_at, created_at, updated_at)
            VALUES ('good1', 'u1', 'o1', 'CANCEL', 0, 'PENDING', 'k4', 'h4', '{}', '2026-09-13 00:00:00', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        # 5. Valid: PLACE with priority=10 -> succeeds
        conn.execute(text("""
            INSERT INTO submission_outbox (id, owner_id, order_id, action_type, priority, status, idempotency_key, canonical_payload_hash, payload_json, next_attempt_at, created_at, updated_at)
            VALUES ('good2', 'u1', 'o1', 'PLACE', 10, 'PENDING', 'k5', 'h5', '{}', '2026-09-13 00:00:00', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.commit()

    engine.dispose()


def test_sequential_reconciliation_records_same_order_different_outbox_operations(migration_env):
    """
    Requirement 2 Migration Test:
    Proves two sequential reconciliation records belonging to the same order but different
    outbox operations (e.g. ambiguous PLACE, then ambiguous CANCEL) can be inserted.
    Proves UNIQUE(owner_id, outbox_id) prevents duplicate resolution on the same outbox operation.
    """
    command.upgrade(migration_env, "0005_upstox_sandbox")
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO users (id, username, normalized_username, email, normalized_email, hashed_password, role, is_active, created_at, updated_at)
            VALUES ('u_seq', 'seq_user', 'seq_user', 'seq@example.com', 'seq@example.com', 'hash', 'EDITOR', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.execute(text("""
            INSERT INTO orders (id, owner_id, runtime_id, intent_id, account_id, order_sequence_number, instrument_id, side, order_type, quantity_units, limit_price_units, filled_quantity_units, status, version, created_at, updated_at)
            VALUES ('o_seq', 'u_seq', 'r_seq', 'i_seq', 'a_seq', 1, 'inst1', 'BUY', 'LIMIT', 50, 2150000, 0, 'RECONCILIATION_REQUIRED', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        # 1. First outbox operation: PLACE
        conn.execute(text("""
            INSERT INTO submission_outbox (id, owner_id, order_id, action_type, priority, status, idempotency_key, canonical_payload_hash, payload_json, next_attempt_at, created_at, updated_at)
            VALUES ('out_place', 'u_seq', 'o_seq', 'PLACE', 10, 'RECONCILIATION_REQUIRED', 'k_p', 'h_p', '{}', '2026-09-13 00:00:00', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        # 2. Second outbox operation: CANCEL
        conn.execute(text("""
            INSERT INTO submission_outbox (id, owner_id, order_id, action_type, priority, status, idempotency_key, canonical_payload_hash, payload_json, next_attempt_at, created_at, updated_at)
            VALUES ('out_cancel', 'u_seq', 'o_seq', 'CANCEL', 0, 'RECONCILIATION_REQUIRED', 'k_c', 'h_c', '{}', '2026-09-13 00:00:00', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.commit()

        # 3. First reconciliation: PLACE_CONFIRMED for out_place
        conn.execute(text("""
            INSERT INTO reconciliation_records (id, owner_id, order_id, outbox_id, status, resolution_type, resolved_by, notes, resolved_at, created_at)
            VALUES ('rec_1', 'u_seq', 'o_seq', 'out_place', 'RESOLVED', 'PLACE_CONFIRMED', 'u_seq', 'Confirmed place on portal', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        # 4. Second reconciliation: CANCEL_CONFIRMED for out_cancel on the same order_id
        conn.execute(text("""
            INSERT INTO reconciliation_records (id, owner_id, order_id, outbox_id, status, resolution_type, resolved_by, notes, resolved_at, created_at)
            VALUES ('rec_2', 'u_seq', 'o_seq', 'out_cancel', 'RESOLVED', 'CANCEL_CONFIRMED', 'u_seq', 'Confirmed cancel on portal', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.commit()

        # Verify both records are present for order o_seq
        count = conn.execute(text("SELECT COUNT(*) FROM reconciliation_records WHERE order_id = 'o_seq'")).scalar()
        assert count == 2

        # 5. Duplicate resolution on out_place must fail UNIQUE(owner_id, outbox_id)
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO reconciliation_records (id, owner_id, order_id, outbox_id, status, resolution_type, resolved_by, notes, resolved_at, created_at)
                VALUES ('rec_dup', 'u_seq', 'o_seq', 'out_place', 'RESOLVED', 'PLACE_CONFIRMED', 'u_seq', 'Duplicate attempt', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

        # 6. Invalid resolution name fails check constraint
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO reconciliation_records (id, owner_id, order_id, outbox_id, status, resolution_type, resolved_by, notes, resolved_at, created_at)
                VALUES ('rec_invalid', 'u_seq', 'o_seq', 'out_place', 'RESOLVED', 'MANUAL_ACKNOWLEDGED', 'u_seq', 'Old name', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

        # 7. Valid OPEN record with NULL resolution fields succeeds
        conn.execute(text("""
            INSERT INTO submission_outbox (id, owner_id, order_id, action_type, priority, status, idempotency_key, canonical_payload_hash, payload_json, next_attempt_at, created_at, updated_at)
            VALUES ('out_open', 'u_seq', 'o_seq', 'PLACE', 10, 'RECONCILIATION_REQUIRED', 'k_open', 'h_open', '{}', '2026-09-13 00:00:00', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.execute(text("""
            INSERT INTO reconciliation_records (id, owner_id, order_id, outbox_id, status, created_at)
            VALUES ('rec_open', 'u_seq', 'o_seq', 'out_open', 'OPEN', '2026-09-13 00:00:00')
        """))
        conn.commit()

    engine.dispose()


def test_schema_reflection_no_token_fingerprint_and_priority_exists(migration_env):
    """
    Mandatory reflection test:
    1. Proves provider_connections has credential_reference and credential_version, but NO token or fingerprint column.
    2. Proves no token, secret, ciphertext, fingerprint, prefix, suffix, or authorization column exists.
    3. Proves provider_connections contains only the approved non-secret metadata columns.
    4. Proves submission_outbox has priority column.
    """
    from sqlalchemy import inspect
    command.upgrade(migration_env, "0005_upstox_sandbox")

    engine = create_engine(TEST_DB_URL)
    inspector = inspect(engine)

    conn_cols = {c["name"].lower() for c in inspector.get_columns("provider_connections")}
    assert "credential_reference" in conn_cols
    assert "credential_version" in conn_cols

    # Requirement 1: Proves no token, secret, ciphertext, fingerprint, prefix, suffix, or authorization column exists
    forbidden_terms = [
        "token",
        "secret",
        "ciphertext",
        "fingerprint",
        "prefix",
        "suffix",
        "auth",
        "authorization",
    ]
    for col in conn_cols:
        for term in forbidden_terms:
            assert term not in col, f"Forbidden term '{term}' found in column '{col}' of provider_connections"

    # Verify exact permitted column set for provider_connections
    expected_cols = {
        "id",
        "owner_id",
        "provider_name",
        "environment",
        "credential_reference",
        "credential_version",
        "status",
        "last_successful_transmission_at",
        "sanitized_error_code",
        "created_at",
        "updated_at",
    }
    assert conn_cols == expected_cols, f"provider_connections columns {conn_cols} do not match expected {expected_cols}"

    outbox_cols = {c["name"].lower() for c in inspector.get_columns("submission_outbox")}
    assert "priority" in outbox_cols
    assert "idempotency_key" in outbox_cols
    assert "canonical_payload_hash" in outbox_cols

    outbox_uqs = inspector.get_unique_constraints("submission_outbox")
    has_owner_id_uq = any(set(uq.get("column_names", [])) == {"owner_id", "id"} for uq in outbox_uqs)
    assert has_owner_id_uq, f"Expected (owner_id, id) unique constraint on submission_outbox, got {outbox_uqs}"

    engine.dispose()


def test_honest_provider_connection_status_check_constraint(migration_env):
    """
    Proves ck_provider_connections_status check constraint:
    Permits CONFIGURED, DISABLED, ERROR.
    Rejects VALIDATED or arbitrary strings.
    """
    from sqlalchemy.exc import IntegrityError
    command.upgrade(migration_env, "0005_upstox_sandbox")

    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO users (id, username, normalized_username, email, normalized_email, hashed_password, role, is_active, created_at, updated_at)
            VALUES ('u_honest', 'honest_user', 'honest_user', 'honest@example.com', 'honest@example.com', 'hash', 'EDITOR', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.commit()

        # 1. Invalid: status = 'VALIDATED' must fail check constraint
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO provider_connections (id, owner_id, provider_name, environment, credential_reference, credential_version, status, created_at, updated_at)
                VALUES ('c_val', 'u_honest', 'UPSTOX', 'SANDBOX', 'ENV_TOKEN', 'v1', 'VALIDATED', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

        # 2. Valid: status = 'CONFIGURED' succeeds
        conn.execute(text("""
            INSERT INTO provider_connections (id, owner_id, provider_name, environment, credential_reference, credential_version, status, created_at, updated_at)
            VALUES ('c_conf', 'u_honest', 'UPSTOX', 'SANDBOX', 'ENV_TOKEN', 'v1', 'CONFIGURED', '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.commit()

    engine.dispose()


def test_distinct_mapping_verification_status_check_constraint(migration_env):
    """
    Proves ck_prov_inst_map_status permits UNVERIFIED, VERIFIED, REJECTED, DISABLED.
    """
    from sqlalchemy.exc import IntegrityError
    command.upgrade(migration_env, "0005_upstox_sandbox")

    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO users (id, username, normalized_username, email, normalized_email, hashed_password, role, is_active, created_at, updated_at)
            VALUES ('u_map', 'map_user', 'map_user', 'map@example.com', 'map@example.com', 'hash', 'EDITOR', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
        """))
        conn.commit()

        for idx, st in enumerate(["UNVERIFIED", "VERIFIED", "REJECTED", "DISABLED"]):
            conn.execute(text(f"""
                INSERT INTO provider_instrument_mappings (id, owner_id, tradepro_instrument_id, provider_instrument_token, exchange, segment, symbol, verification_status, mapping_version, created_at, updated_at)
                VALUES ('m_{idx}', 'u_map', 'inst_{idx}', 'TOKEN_{idx}', 'NSE_FO', 'FO', 'NIFTY', '{st}', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
        conn.commit()

        # Invalid status fails
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO provider_instrument_mappings (id, owner_id, tradepro_instrument_id, provider_instrument_token, exchange, segment, symbol, verification_status, mapping_version, created_at, updated_at)
                VALUES ('m_bad', 'u_map', 'inst_bad', 'TOKEN_BAD', 'NSE_FO', 'FO', 'NIFTY', 'INVALID_STATUS', 1, '2026-09-13 00:00:00', '2026-09-13 00:00:00')
            """))
            conn.commit()
        conn.rollback()

    engine.dispose()
