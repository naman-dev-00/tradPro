import os
import pytest
from sqlalchemy import create_engine, inspect, text
from alembic.config import Config
from alembic import command
from src.database import Base
from src.config import settings
from src.models import LEGACY_PRINCIPAL_ID

def get_alembic_config(db_url: str) -> Config:
    alembic_ini_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config = Config(alembic_ini_path)
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "migrations")))
    return config

def test_alembic_migration_empty_to_head_direct(tmp_path):
    db_file = tmp_path / "test_migration_empty.db"
    db_url = f"sqlite:///{db_file}"
    config = get_alembic_config(db_url)
    engine = create_engine(db_url)

    command.upgrade(config, "head")

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "users" in tables
    assert "user_sessions" in tables
    assert "strategies" in tables
    assert "inspection_runs" in tables

    # Verify legacy principal was seeded during migration
    with engine.connect() as conn:
        legacy = conn.execute(text(f"SELECT username, is_active FROM users WHERE id = '{LEGACY_PRINCIPAL_ID}'")).fetchone()
        assert legacy is not None
        assert legacy[0] == "system_legacy_owner"
        assert bool(legacy[1]) is False

    engine.dispose()

def test_alembic_migration_stepwise_and_backfill(tmp_path):
    db_file = tmp_path / "test_stepwise.db"
    db_url = f"sqlite:///{db_file}"
    config = get_alembic_config(db_url)
    engine = create_engine(db_url)

    # 1. Upgrade to 0002_inspection_history
    command.upgrade(config, "0002_inspection_history")

    # Insert unowned strategy and inspection run
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO strategies (id, name, timeframe, candidate_selection_mode, payload) "
                "VALUES ('strat-old-1', 'Legacy Strat', '15m', 'FIRST_ELIGIBLE', '{\"action\": \"BUY\"}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO inspection_runs (id, run_type, timeframe, engine_version, manifest_version, "
                "created_at, completed_at, status, synthetic_data_confirmed, completed_fingerprint, subject_dataset_ids, "
                "reference_dataset_id, requested_start_timestamp, requested_end_timestamp, strategy_definition_snapshot, result_payload, manifest_checksums_snapshot) "
                "VALUES ('run-old-1', 'HISTORICAL_REPLAY', '15m', '1.0.0', '1.0.0', "
                "'2026-08-30 00:00:00', '2026-08-30 00:00:00', 'COMPLETED', 1, 'fp_legacy_001', '[\"subj1\"]', "
                "'ref_ds', '2026-08-30 00:00:00', '2026-08-30 00:00:00', '{\"a\": 1}', '{\"res\": 1}', '{\"chk\": 1}')"
            )
        )
        conn.commit()

    # 2. Upgrade to head (0003_auth_ownership)
    command.upgrade(config, "head")

    # Verify existing unowned rows were backfilled to legacy principal
    with engine.connect() as conn:
        strat = conn.execute(text("SELECT owner_id FROM strategies WHERE id = 'strat-old-1'")).fetchone()
        assert strat[0] == LEGACY_PRINCIPAL_ID

        run = conn.execute(text("SELECT owner_id FROM inspection_runs WHERE id = 'run-old-1'")).fetchone()
        assert run[0] == LEGACY_PRINCIPAL_ID

    # 3. Test downgrade to 0002_inspection_history without collisions
    command.downgrade(config, "0002_inspection_history")
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "users" not in tables
    assert "user_sessions" not in tables
    assert "inspection_runs" in tables

    engine.dispose()

def test_alembic_downgrade_collision_refusal(tmp_path):
    db_file = tmp_path / "test_collision.db"
    db_url = f"sqlite:///{db_file}"
    config = get_alembic_config(db_url)
    engine = create_engine(db_url)

    # Upgrade to head (0003_auth_ownership)
    command.upgrade(config, "head")

    # Insert two users
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO users (id, username, normalized_username, email, normalized_email, hashed_password, role, is_active, created_at, updated_at)
            VALUES ('user-1', 'user1', 'user1', 'u1@test.com', 'u1@test.com', 'hash', 'EDITOR', 1, '2026-08-30 00:00:00', '2026-08-30 00:00:00'),
                   ('user-2', 'user2', 'user2', 'u2@test.com', 'u2@test.com', 'hash', 'EDITOR', 1, '2026-08-30 00:00:00', '2026-08-30 00:00:00')
        """))
        # Insert two runs for different owners with SAME completed_fingerprint
        conn.execute(text("""
            INSERT INTO inspection_runs (id, owner_id, run_type, timeframe, engine_version, manifest_version, created_at, completed_at, status, synthetic_data_confirmed, completed_fingerprint, subject_dataset_ids, reference_dataset_id, requested_start_timestamp, requested_end_timestamp, strategy_definition_snapshot, result_payload, manifest_checksums_snapshot)
            VALUES ('run-u1', 'user-1', 'HISTORICAL_REPLAY', '15m', '1.0.0', '1.0.0', '2026-08-30 00:00:00', '2026-08-30 00:00:00', 'COMPLETED', 1, 'duplicate_fp_123', '[\"subj1\"]', 'ref_ds', '2026-08-30 00:00:00', '2026-08-30 00:00:00', '{\"a\": 1}', '{\"res\": 1}', '{\"chk\": 1}'),
                   ('run-u2', 'user-2', 'HISTORICAL_REPLAY', '15m', '1.0.0', '1.0.0', '2026-08-30 00:00:00', '2026-08-30 00:00:00', 'COMPLETED', 1, 'duplicate_fp_123', '[\"subj1\"]', 'ref_ds', '2026-08-30 00:00:00', '2026-08-30 00:00:00', '{\"a\": 1}', '{\"res\": 1}', '{\"chk\": 1}')
        """))
        conn.commit()

    # Attempt downgrade -> Must safely abort with collision error to protect data integrity
    with pytest.raises(Exception) as exc_info:
        command.downgrade(config, "0002_inspection_history")
    assert "cross-owner duplicate completed_fingerprint" in str(exc_info.value)

    engine.dispose()

def test_alembic_revision_identifiers_and_graph_invariants():
    from alembic.script import ScriptDirectory
    alembic_ini_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config = Config(alembic_ini_path)
    config.set_main_option("script_location", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "migrations")))

    script_directory = ScriptDirectory.from_config(config)
    heads = script_directory.get_heads()

    # 1. Alembic has exactly one head revision
    assert len(heads) == 1
    assert heads[0] == "0003_auth_ownership"

    # 2. Every revision identifier is non-empty, <= 32 chars, and down_revision resolves
    for script in script_directory.walk_revisions():
        rev_id = script.revision
        assert rev_id is not None and len(rev_id) > 0
        assert len(rev_id) <= 32, f"Revision identifier '{rev_id}' exceeds maximum length of 32 characters! ({len(rev_id)} chars)"

        if script.down_revision:
            if isinstance(script.down_revision, tuple):
                for parent in script.down_revision:
                    assert script_directory.get_revision(parent) is not None
            else:
                assert script_directory.get_revision(script.down_revision) is not None

def test_alembic_migration_postgres_compatibility():
    db_url = os.getenv("DATABASE_URL")
    if not db_url or not db_url.startswith("postgresql"):
        pytest.skip("PostgreSQL test skipped: DATABASE_URL is not set to a PostgreSQL connection.")

    engine = create_engine(db_url)

    with engine.connect() as conn:
        res = conn.execute(text("SELECT 1")).scalar()
        assert res == 1

    config = get_alembic_config(db_url)
    command.upgrade(config, "head")

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "users" in tables
    assert "user_sessions" in tables
    assert "inspection_runs" in tables
    assert "strategies" in tables

    from alembic.migration import MigrationContext
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        current_rev = ctx.get_current_revision()
        assert current_rev == "0003_auth_ownership"

    engine.dispose()
