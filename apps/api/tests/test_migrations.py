import os
import pytest
from sqlalchemy import create_engine, inspect, text
from alembic.config import Config
from alembic import command
from src.database import Base

def test_alembic_migration_empty_to_head_direct(tmp_path):
    db_file = tmp_path / "test_migration_empty.db"
    db_url = f"sqlite:///{db_file}"

    alembic_ini_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config = Config(alembic_ini_path)
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "migrations")))

    engine = create_engine(db_url)
    command.upgrade(config, "head")

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "strategies" in tables
    assert "inspection_runs" in tables

    columns = {col["name"]: col for col in inspector.get_columns("inspection_runs")}
    assert "request_fingerprint" in columns
    assert "completed_fingerprint" in columns
    assert "manifest_checksums_snapshot" in columns
    assert "synthetic_data_confirmed" in columns

    indexes = {idx["name"]: idx for idx in inspector.get_indexes("inspection_runs")}
    assert "ix_inspection_runs_completed_fingerprint" in indexes
    assert "ix_inspection_runs_request_fingerprint" in indexes

    import gc
    engine.dispose()
    gc.collect()

def test_alembic_migration_fresh_and_upgrade(tmp_path):
    db_file = tmp_path / "test_migration.db"
    db_url = f"sqlite:///{db_file}"

    # Set up alembic config targeting temporary database
    alembic_ini_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config = Config(alembic_ini_path)
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "migrations")))

    engine = create_engine(db_url)

    # 1. Upgrade to 0001_initial_schema first
    command.upgrade(config, "0001_initial_schema")

    # Insert a sample strategy to test data preservation during migration
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO strategies (id, name, timeframe, candidate_selection_mode, payload) "
                "VALUES ('test-strat-1', 'Test Strategy', '15m', 'FIRST_ELIGIBLE', '{\"action\": \"BUY\"}')"
            )
        )
        conn.commit()

    # 2. Upgrade to head (0002_add_inspection_history_and_replays)
    command.upgrade(config, "head")

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "strategies" in tables
    assert "inspection_runs" in tables

    # Verify pre-existing strategy survived upgrade
    with engine.connect() as conn:
        res = conn.execute(text("SELECT name FROM strategies WHERE id = 'test-strat-1'")).fetchone()
        assert res is not None
        assert res[0] == "Test Strategy"

    # Verify inspection_runs columns and indexes
    columns = {col["name"]: col for col in inspector.get_columns("inspection_runs")}
    assert "request_fingerprint" in columns
    assert "completed_fingerprint" in columns
    assert "manifest_checksums_snapshot" in columns
    assert "synthetic_data_confirmed" in columns

    indexes = {idx["name"]: idx for idx in inspector.get_indexes("inspection_runs")}
    assert "ix_inspection_runs_strategy_id" in indexes
    assert "ix_inspection_runs_run_type" in indexes
    assert "ix_inspection_runs_status" in indexes
    assert "ix_inspection_runs_request_fingerprint" in indexes
    assert "ix_inspection_runs_completed_fingerprint" in indexes

    import gc
    engine.dispose()
    gc.collect()
