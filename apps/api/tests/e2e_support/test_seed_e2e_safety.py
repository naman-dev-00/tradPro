import os
import pathlib
import pytest
from tests.e2e_support.seed_e2e import validate_disposable_e2e_environment, SENTINEL_FILENAME

def test_safety_refuses_non_test_app_env(tmp_path):
    temp_dir = tmp_path / "tradepro_e2e_12345"
    temp_dir.mkdir()
    (temp_dir / SENTINEL_FILENAME).touch()
    db_file = temp_dir / "e2e.db"
    db_url = f"sqlite:///{db_file}"

    with pytest.raises(ValueError, match="strictly requires APP_ENV='test'"):
        validate_disposable_e2e_environment(None, db_url, str(temp_dir))

    with pytest.raises(ValueError, match="strictly requires APP_ENV='test'"):
        validate_disposable_e2e_environment("production", db_url, str(temp_dir))

    with pytest.raises(ValueError, match="strictly requires APP_ENV='test'"):
        validate_disposable_e2e_environment("staging", db_url, str(temp_dir))

def test_safety_refuses_non_sqlite_url(tmp_path):
    temp_dir = tmp_path / "tradepro_e2e_12345"
    temp_dir.mkdir()
    (temp_dir / SENTINEL_FILENAME).touch()

    with pytest.raises(ValueError, match="requires a local SQLite URL"):
        validate_disposable_e2e_environment("test", "postgresql://user:pass@localhost:5432/db", str(temp_dir))

def test_safety_refuses_missing_sentinel(tmp_path):
    temp_dir = tmp_path / "tradepro_e2e_12345"
    temp_dir.mkdir()
    # Missing sentinel file
    db_file = temp_dir / "e2e.db"
    db_url = f"sqlite:///{db_file}"

    with pytest.raises(ValueError, match="missing required sentinel file"):
        validate_disposable_e2e_environment("test", db_url, str(temp_dir))

def test_safety_refuses_invalid_temp_dir_name(tmp_path):
    temp_dir = tmp_path / "invalid_name_12345"
    temp_dir.mkdir()
    (temp_dir / SENTINEL_FILENAME).touch()
    db_file = temp_dir / "e2e.db"
    db_url = f"sqlite:///{db_file}"

    with pytest.raises(ValueError, match="name must start with 'tradepro_e2e_'"):
        validate_disposable_e2e_environment("test", db_url, str(temp_dir))

def test_safety_refuses_db_outside_temp_dir(tmp_path):
    temp_dir = tmp_path / "tradepro_e2e_12345"
    temp_dir.mkdir()
    (temp_dir / SENTINEL_FILENAME).touch()

    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    db_file = outside_dir / "e2e.db"
    db_url = f"sqlite:///{db_file}"

    with pytest.raises(ValueError, match="is outside the E2E temporary directory"):
        validate_disposable_e2e_environment("test", db_url, str(temp_dir))

def test_safety_refuses_primary_development_db(tmp_path):
    temp_dir = tmp_path / "tradepro_e2e_12345"
    temp_dir.mkdir()
    (temp_dir / SENTINEL_FILENAME).touch()

    dev_db = temp_dir / "tradepro.db"
    dev_db.touch()
    db_url = f"sqlite:///{dev_db}"

    # Explicitly testing when the resolved path matches primary dev db path
    with pytest.raises(ValueError, match="CRITICAL SAFETY VIOLATION"):
        validate_disposable_e2e_environment(
            "test",
            db_url,
            str(temp_dir),
            primary_dev_db_path=dev_db.resolve()
        )

def test_safety_accepts_valid_disposable_setup(tmp_path):
    temp_dir = tmp_path / "tradepro_e2e_valid123"
    temp_dir.mkdir()
    (temp_dir / SENTINEL_FILENAME).touch()
    db_file = temp_dir / "e2e_disposable.db"
    db_url = f"sqlite:///{db_file}"

    resolved_path = validate_disposable_e2e_environment("test", db_url, str(temp_dir))
    assert resolved_path == db_file.resolve()
