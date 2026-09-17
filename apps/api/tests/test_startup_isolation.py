"""Subprocess tests cover the boundary before pytest/application imports."""
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from alembic import command
from alembic.config import Config

API = Path(__file__).resolve().parents[1]


def evidence(path):
    if not path.exists():
        return None
    stat = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime_ns


def run(code, path, app_env="local"):
    env = os.environ.copy()
    env.update(APP_ENV=app_env, DATABASE_URL="sqlite:///" + path.as_posix(),
               COOKIE_SECURE="true", UPSTOX_SANDBOX_NETWORK_ENABLED="false",
               PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-B", "-c", code], cwd=API, env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("module", ["src.database", "src.models", "src.main", "src.routes.paper"])
def test_imports_and_application_construction_issue_no_sql(tmp_path, module):
    target = tmp_path / "not_created.db"
    result = run(f"""
from sqlalchemy import event
from sqlalchemy.engine import Engine
@event.listens_for(Engine, 'before_cursor_execute')
def forbid_sql(*args):
    raise AssertionError('Import executed SQL')
import importlib
module = importlib.import_module({module!r})
if {module!r} == 'src.main':
    assert module.app is not None
print('IMPORT_OK')
""", target)
    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK" in result.stdout
    assert not target.exists()


@pytest.mark.parametrize("state", ["absent_file", "empty_schema", "missing_version", "outdated"])
def test_local_startup_fails_without_mutation(tmp_path, state):
    target = tmp_path / "local.db"
    if state != "absent_file":
        with sqlite3.connect(target) as conn:
            if state == "missing_version":
                conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
            elif state == "outdated":
                conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
                conn.execute("INSERT INTO alembic_version VALUES ('0004_paper_runtime')")
    before = evidence(target)
    result = run("""
from fastapi.testclient import TestClient
from src.main import app
try:
    with TestClient(app):
        raise AssertionError('Startup should fail')
except RuntimeError as exc:
    assert str(exc) == 'Database schema is unavailable or outdated. Run Alembic upgrade 0005_upstox_sandbox against the intended database.'
print('FAIL_CLOSED')
""", target)
    assert result.returncode == 0, result.stderr
    assert evidence(target) == before


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_deployed_environments_have_no_schema_bypass(tmp_path, app_env):
    target = tmp_path / "unmigrated.db"
    result = run("""
import os
os.environ.update(SKIP_SCHEMA_CHECK='true', TEST_SCHEMA_BYPASS='true')
from src.database import verify_database_connection
try:
    verify_database_connection()
    raise AssertionError('Schema verification was bypassed')
except RuntimeError as exc:
    assert 'Run Alembic upgrade 0006_strategy_orchestrator' in str(exc)
""", target, app_env)
    assert result.returncode == 0, result.stderr
    assert not target.exists()


def test_test_mode_refuses_development_before_connect():
    target = API / "tradepro.db"
    before = evidence(target)
    result = run("import src.database", target, "test")
    assert result.returncode != 0
    assert "Test mode refuses the development database path" in result.stderr
    assert evidence(target) == before


def test_environment_is_disposable_before_application_import():
    from tests.bootstrap import TEST_ROOT
    from src.config import settings
    from src.database import engine
    from src.database_safety import require_disposable_target
    assert settings.APP_ENV == os.environ["APP_ENV"] == "test"
    assert settings.DATABASE_URL == os.environ["DATABASE_URL"]
    require_disposable_target(engine.url.render_as_string(hide_password=False))
    assert (TEST_ROOT / ".tradepro-pytest-disposable").is_file()


def test_clean_local_0005_startup_passes_without_mutation(tmp_path):
    target = tmp_path / "local_0005.db"
    cfg = Config(str(API / "alembic.ini"))
    cfg.set_main_option("script_location", str(API / "src/migrations"))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + target.as_posix())
    command.upgrade(cfg, "0005_upstox_sandbox")
    before = evidence(target)
    result = run("""
from fastapi.testclient import TestClient
from src.main import app
with TestClient(app) as client:
    assert client.get('/health').status_code == 200
""", target)
    assert result.returncode == 0, result.stderr
    assert evidence(target) == before
    with sqlite3.connect(target.as_uri() + '?mode=ro', uri=True) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0005_upstox_sandbox"
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='runtime_orchestration_configs'").fetchall()


def test_disposable_schema_guard_rejects_repository_targets():
    from src.database_safety import require_disposable_target
    for name in ("tradepro.db", "test_tradepro.db"):
        with pytest.raises(RuntimeError):
            require_disposable_target("sqlite:///" + (API / name).as_posix())


@pytest.mark.parametrize("form", ["absolute", "relative", "uri"])
def test_canonical_development_aliases_are_rejected(form):
    from src.database_safety import reject_development_test_target
    path = API / "tradepro.db"
    urls = {"absolute": "sqlite:///" + path.as_posix(), "relative": "sqlite:///./tradepro.db",
            "uri": "sqlite:///" + path.as_uri() + "?mode=ro&uri=true"}
    with pytest.raises(RuntimeError, match="development database"):
        reject_development_test_target(urls[form])


def test_test_mode_without_explicit_url_fails_before_import(tmp_path):
    result = run("import os; os.environ.pop('DATABASE_URL'); import src.database", tmp_path / "unused.db", "test")
    assert result.returncode != 0
    assert "explicit disposable DATABASE_URL" in result.stderr
