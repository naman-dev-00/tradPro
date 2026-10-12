"""Tests for migration 0009_broker_readiness: populated upgrade, schema parity, and downgrade guards."""
import os
import uuid
from pathlib import Path
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from src.database import Base
from tests.paper_database_support import paper_test_database

HEAD = "0009_broker_readiness"
PREVIOUS = "0008_provider_execution"


def migration_config(url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def migrated_database(request, tmp_path_factory):
    path = tmp_path_factory.mktemp("broker_mig") / "audit.db"
    with paper_test_database(request.param, path) as (engine, url):
        config = migration_config(url)
        command.upgrade(config, HEAD)
        yield engine, config


@pytest.fixture
def clean_migrated(migrated_database):
    engine, config = migrated_database
    yield engine, config
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_target(request, tmp_path_factory):
    path = tmp_path_factory.mktemp("mig_target_0009") / "audit.db"
    with paper_test_database(request.param, path) as (engine, url):
        yield engine, migration_config(url)


def test_0009_revision_graph():
    graph = ScriptDirectory.from_config(migration_config("sqlite:///:memory:"))
    assert graph.get_heads() == [HEAD]
    rev = graph.get_revision(HEAD)
    assert rev.down_revision == PREVIOUS
    assert len(HEAD) <= 32


def test_0009_schema_parity(clean_migrated):
    engine, _ = clean_migrated
    inspector = sa.inspect(engine)

    # 1. Check constraints on provider_connections
    checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints("provider_connections")}
    assert "ck_provider_connections_env" in checks
    assert "LIVE" in checks["ck_provider_connections_env"]
    assert "ck_provider_connections_status" in checks
    assert "CONNECTED" in checks["ck_provider_connections_status"]

    # 2. Check added columns on provider_connections
    cols = {c["name"]: c for c in inspector.get_columns("provider_connections")}
    assert "broker_user_id" in cols
    assert "account_name" in cols
    assert "encrypted_access_token" in cols
    assert "token_expires_at" in cols
    assert "version" in cols
    assert "credential_generation" in cols

    # 3. Check oauth_states table
    assert "oauth_states" in inspector.get_table_names()
    oauth_cols = {c["name"]: c for c in inspector.get_columns("oauth_states")}
    assert "state_token" in oauth_cols
    assert "owner_id" in oauth_cols
    assert "session_id" in oauth_cols
    assert "consumed" in oauth_cols
    assert "expires_at" in oauth_cols

    # 4. Check broker_snapshots table
    assert "broker_snapshots" in inspector.get_table_names()
    snap_cols = {c["name"]: c for c in inspector.get_columns("broker_snapshots")}
    assert "broker_user_id" in snap_cols
    assert "raw_fingerprint" in snap_cols
    assert "funds_and_margins_json" in snap_cols
    assert "positions_json" in snap_cols
    assert "orders_json" in snap_cols
    assert "credential_generation" in snap_cols

    # 5. Check indexes
    indices = {idx["name"]: idx for idx in inspector.get_indexes("provider_connections")}
    assert "uq_provider_connections_active_ucc" in indices


def test_0008_to_0009_populated_upgrade_preserves_sandbox(migration_target):
    engine, config = migration_target
    command.upgrade(config, PREVIOUS)

    user_id = str(uuid.uuid4())
    conn_id = str(uuid.uuid4())

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, username, normalized_username, email, normalized_email, hashed_password, role, is_active, created_at, updated_at) "
                "VALUES (:id, 'sandbox_user', 'sandbox_user', 'sandbox@test.com', 'sandbox@test.com', 'hash', 'EDITOR', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": user_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO provider_connections (id, owner_id, provider_name, environment, credential_reference, credential_version, status, created_at, updated_at) "
                "VALUES (:id, :owner_id, 'UPSTOX', 'SANDBOX', 'sandbox_seed', 'v1', 'CONFIGURED', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": conn_id, "owner_id": user_id},
        )

    # Upgrade to 0009
    command.upgrade(config, HEAD)

    # Verify existing sandbox record is completely preserved
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT id, owner_id, environment, status, version, credential_generation FROM provider_connections WHERE id = :id"),
            {"id": conn_id},
        ).fetchone()

        assert row is not None
        assert row[0] == conn_id
        assert row[1] == user_id
        assert row[2] == "SANDBOX"
        assert row[3] == "CONFIGURED"
        assert row[4] == 1  # server_default="1" applied
        assert row[5] == 1  # server_default="1" applied


def test_0009_downgrade_guards(migration_target):
    engine, config = migration_target
    command.upgrade(config, HEAD)

    # Insert user to satisfy FK
    user_id = str(uuid.uuid4())
    conn_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, username, normalized_username, email, normalized_email, hashed_password, role, is_active, created_at, updated_at) "
                "VALUES (:id, 'operator', 'operator', 'op@test.com', 'op@test.com', 'hash', 'EDITOR', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": user_id},
        )

        conn.execute(
            sa.text(
                "INSERT INTO provider_connections (id, owner_id, provider_name, environment, credential_reference, credential_version, status, version, credential_generation, created_at, updated_at) "
                "VALUES (:id, :owner_id, 'UPSTOX', 'SANDBOX', 'test', 'v1', 'CONFIGURED', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": conn_id, "owner_id": user_id},
        )
        # Guard 1: Populated snapshot blocks downgrade
        snap_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO broker_snapshots (id, owner_id, connection_id, broker_user_id, environment, status, acquired_at_utc, raw_fingerprint, connection_version_at_fetch, credential_generation, created_at) "
                "VALUES (:id, :owner_id, :conn_id, 'UCC123', 'SANDBOX', 'ACTIVE', CURRENT_TIMESTAMP, 'fp', 1, 1, CURRENT_TIMESTAMP)"
            ),
            {"id": snap_id, "owner_id": user_id, "conn_id": conn_id},
        )

    with pytest.raises(Exception, match="broker_snapshots contains records"):
        command.downgrade(config, PREVIOUS)

    # Verify tables and data preserved after snapshot refusal
    inspector = sa.inspect(engine)
    assert "broker_snapshots" in inspector.get_table_names()
    assert "oauth_states" in inspector.get_table_names()

    # Clear snapshot, test Guard 2 (pending or consumed oauth_states blocks downgrade)
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM broker_snapshots"))
        conn.execute(
            sa.text(
                "INSERT INTO user_sessions (id, user_id, session_hash, csrf_hash, created_at, last_accessed_at, idle_expires_at, absolute_expires_at) "
                "VALUES ('sess1', :uid, 'hash1', 'csrf1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"uid": user_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO oauth_states (id, state_token, owner_id, session_id, environment, redirect_uri, consumed, expires_at, created_at) "
                "VALUES ('st1', 'token1', :uid, 'sess1', 'LIVE', 'http://localhost/cb', FALSE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"uid": user_id},
        )

    with pytest.raises(Exception, match="oauth_states contains records"):
        command.downgrade(config, PREVIOUS)

    # Verify tables preserved after oauth refusal
    inspector = sa.inspect(engine)
    assert "oauth_states" in inspector.get_table_names()

    # Clear oauth_states, test Guard 3 with SANDBOX + DISCONNECTED (incompatible status check)
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM oauth_states"))
        conn.execute(
            sa.text("UPDATE provider_connections SET environment = 'SANDBOX', status = 'DISCONNECTED' WHERE id = :id"),
            {"id": conn_id},
        )

    with pytest.raises(Exception, match="active/disconnected/expired Phase 7A statuses"):
        command.downgrade(config, PREVIOUS)

    # Verify tables preserved after status refusal
    inspector = sa.inspect(engine)
    assert "broker_snapshots" in inspector.get_table_names()

    # Test Guard 3 with SANDBOX + EXPIRED
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE provider_connections SET environment = 'SANDBOX', status = 'EXPIRED' WHERE id = :id"),
            {"id": conn_id},
        )

    with pytest.raises(Exception, match="active/disconnected/expired Phase 7A statuses"):
        command.downgrade(config, PREVIOUS)

    # Test Guard 3 with LIVE environment
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE provider_connections SET environment = 'LIVE', status = 'CONNECTED' WHERE id = :id"),
            {"id": conn_id},
        )

    with pytest.raises(Exception, match="LIVE records"):
        command.downgrade(config, PREVIOUS)

    # Revert to clean legacy SANDBOX (status CONFIGURED and zero Phase 7A fields), downgrade succeeds
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE provider_connections SET environment = 'SANDBOX', status = 'CONFIGURED', authorization_fence = NULL WHERE id = :id"),
            {"id": conn_id},
        )

    command.downgrade(config, PREVIOUS)

    # Verify 0008 state after downgrade
    inspector = sa.inspect(engine)
    assert "broker_snapshots" not in inspector.get_table_names()
    assert "oauth_states" not in inspector.get_table_names()
    cols = {c["name"]: c for c in inspector.get_columns("provider_connections")}
    assert "broker_user_id" not in cols
    assert "encrypted_access_token" not in cols
    assert "authorization_fence" not in cols
