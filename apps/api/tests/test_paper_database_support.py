"""Required PostgreSQL must fail closed without leaking connection details."""
import pytest
from tests.paper_database_support import paper_test_database


@pytest.mark.parametrize("case", ["missing", "wrong_dialect", "connection_failed"])
def test_required_postgres_fails_closed(monkeypatch, tmp_path, case):
    monkeypatch.setenv("REQUIRE_POSTGRES", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_TEST_URL", raising=False)
    if case == "wrong_dialect":
        monkeypatch.setenv("POSTGRES_TEST_URL", "sqlite:///:memory:")
    elif case == "connection_failed":
        monkeypatch.setenv("POSTGRES_TEST_URL", "postgresql://localhost/tradepro_test")
        def fail_connection(*args, **kwargs):
            raise RuntimeError("sensitive connection error sentinel")
        monkeypatch.setattr("tests.paper_database_support.sa.create_engine", fail_connection)
    with pytest.raises(pytest.fail.Exception, match="REQUIRE_POSTGRES=true") as exc:
        with paper_test_database("postgresql", tmp_path / "unused.db"):
            pytest.fail("Unavailable required PostgreSQL must not yield")
    assert "sensitive connection error sentinel" not in str(exc.value)


def test_sqlite_ci_allows_postgres_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("REQUIRE_POSTGRES", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_TEST_URL", raising=False)
    with pytest.raises(pytest.skip.Exception, match="CI-PENDING"):
        with paper_test_database("postgresql", tmp_path / "unused.db"):
            pytest.fail("Unconfigured PostgreSQL must skip in SQLite CI")
