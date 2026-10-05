"""Guarded disposable engines for Phase 4 execution and migration tests."""
from contextlib import contextmanager
import os
import uuid

import pytest
import sqlalchemy as sa

from src.database import create_db_engine
from src.database_safety import require_disposable_target


@contextmanager
def paper_test_database(dialect, sqlite_path):
    admin = None
    engine = None
    schema = None
    if dialect == "sqlite":
        url = "sqlite:///" + sqlite_path.as_posix()
    else:
        required = os.environ.get("REQUIRE_POSTGRES", "").lower() == "true"
        configured = os.environ.get("POSTGRES_TEST_URL") or os.environ.get("DATABASE_URL", "")
        try:
            parsed = sa.engine.make_url(configured)
            if parsed.get_backend_name() != "postgresql":
                raise ValueError("Wrong dialect")
            require_disposable_target(configured)
        except Exception:
            if required:
                pytest.fail("REQUIRE_POSTGRES=true: missing or invalid disposable PostgreSQL configuration", pytrace=False)
            pytest.skip("PostgreSQL: CI-PENDING (no disposable PostgreSQL test configuration)")
        try:
            admin = sa.create_engine(configured, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5})
            schema = "test_paper_" + uuid.uuid4().hex
            with admin.connect() as conn:
                conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        except Exception:
            if admin is not None:
                admin.dispose()
            if required:
                pytest.fail("REQUIRE_POSTGRES=true: PostgreSQL connection/schema setup failed", pytrace=False)
            pytest.skip("PostgreSQL: CI-PENDING (connection/schema setup unavailable)")
        options = dict(parsed.query)
        options["options"] = "-csearch_path=" + schema + " -clock_timeout=5000 -cstatement_timeout=15000"
        url = parsed.set(query=options).render_as_string(hide_password=False)
    try:
        require_disposable_target(url)
        engine = create_db_engine(url)
        assert engine.dialect.name == dialect
        yield engine, url
    finally:
        if engine is not None:
            engine.dispose()
        if admin is not None:
            try:
                with admin.connect() as conn:
                    conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            finally:
                admin.dispose()
