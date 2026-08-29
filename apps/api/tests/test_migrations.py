import os
import pytest
from sqlalchemy import create_engine, inspect, text
from alembic.config import Config
from alembic import command
from src.database import Base
from src.config import settings

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

    # 2. Upgrade to head (0002_inspection_history)
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

    # 3. Test downgrade to 0001_initial_schema
    command.downgrade(config, "0001_initial_schema")
    inspector_downgraded = inspect(engine)
    tables_downgraded = inspector_downgraded.get_table_names()
    assert "strategies" in tables_downgraded
    assert "inspection_runs" not in tables_downgraded

    # 4. Test re-upgrade back to head
    command.upgrade(config, "head")
    inspector_reup = inspect(engine)
    assert "inspection_runs" in inspector_reup.get_table_names()

    import gc
    engine.dispose()
    gc.collect()

def test_alembic_revision_identifiers_and_graph_invariants():
    from alembic.script import ScriptDirectory
    alembic_ini_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config = Config(alembic_ini_path)
    config.set_main_option("script_location", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "migrations")))

    script_directory = ScriptDirectory.from_config(config)
    heads = script_directory.get_heads()

    # 1. Alembic has exactly one head revision
    assert len(heads) == 1
    assert heads[0] == "0002_inspection_history"

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

    # 1. Verify connection and SELECT 1
    with engine.connect() as conn:
        res = conn.execute(text("SELECT 1")).scalar()
        assert res == 1

    # 2. Run Alembic upgrade to head
    alembic_ini_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config = Config(alembic_ini_path)
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "migrations")))

    command.upgrade(config, "head")

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "inspection_runs" in tables
    assert "strategies" in tables

    columns = {col["name"]: col for col in inspector.get_columns("inspection_runs")}
    assert "synthetic_data_confirmed" in columns
    assert "completed_fingerprint" in columns
    assert "request_fingerprint" in columns

    # 3. Check current revision reported by Alembic context is 0002_inspection_history
    from alembic.migration import MigrationContext
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        current_rev = ctx.get_current_revision()
        assert current_rev == "0002_inspection_history"

    engine.dispose()

def test_alembic_url_precedence_explicit_url_takes_priority(monkeypatch, tmp_path):
    pg_url = "postgresql://user:secret@localhost:5432/testdb"
    monkeypatch.setattr(settings, "DATABASE_URL", pg_url)
    monkeypatch.setenv("DATABASE_URL", pg_url)

    db_file = tmp_path / "explicit_sqlite.db"
    sqlite_url = f"sqlite:///{db_file}"

    alembic_ini_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config = Config(alembic_ini_path)
    config.set_main_option("sqlalchemy.url", sqlite_url)
    config.set_main_option("script_location", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "migrations")))

    from src.migrations.env import configure_database_url
    res_url = configure_database_url(config)
    assert res_url == sqlite_url
    assert config.get_main_option("sqlalchemy.url") == sqlite_url

    command.upgrade(config, "head")

    engine = create_engine(sqlite_url)
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "strategies" in tables
    assert "inspection_runs" in tables

    import gc
    engine.dispose()
    gc.collect()

def test_alembic_url_precedence_runtime_url_fallback(monkeypatch):
    pg_url = "postgresql://user:pass%40word@localhost:5432/testdb"
    monkeypatch.setattr(settings, "DATABASE_URL", pg_url)
    monkeypatch.setenv("DATABASE_URL", pg_url)

    alembic_ini_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config = Config(alembic_ini_path)

    from src.migrations.env import configure_database_url
    res_url = configure_database_url(config)
    assert res_url == pg_url
    # Ensure section parsing works without ConfigParser InterpolationSyntaxError
    section = config.get_section(config.config_ini_section, {})
    assert section.get("sqlalchemy.url") == pg_url
