import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock
from src.config import Settings
from src import database

def test_database_url_configured_success():
    with patch("src.config.settings.DATABASE_URL", "sqlite:///:memory:"), \
         patch("src.config.settings.APP_ENV", "local"):
        # verify_database_connection should succeed
        database.verify_database_connection()

def test_database_url_configured_failure_raises_runtime_error():
    fake_pg_url = "postgresql://user:secret_pass@localhost:5432/fake_db"

    with patch("src.config.settings.DATABASE_URL", fake_pg_url), \
         patch("src.config.settings.APP_ENV", "local"), \
         patch.object(database.engine, "connect", side_effect=Exception("Connection refused")):

        with pytest.raises(RuntimeError) as exc_info:
            database.verify_database_connection()

        assert "Failed to connect to configured database" in str(exc_info.value)
        # Verify secret_pass is masked in error message
        assert "secret_pass" not in str(exc_info.value)

def test_missing_database_url_in_production_raises_error():
    with patch("src.config.settings.DATABASE_URL", None), \
         patch("src.config.settings.APP_ENV", "production"):

        with pytest.raises(RuntimeError) as exc_info:
            database.get_db_url()

        assert "DATABASE_URL environment variable is required" in str(exc_info.value)

def test_missing_database_url_in_local_env_uses_sqlite():
    with patch("src.config.settings.DATABASE_URL", None), \
         patch("src.config.settings.APP_ENV", "local"):

        url = database.get_db_url()
        assert url.startswith("sqlite")

def test_isolated_temp_sqlite_database(tmp_path):
    temp_db_file = tmp_path / "isolated_test.db"
    temp_db_url = f"sqlite:///{temp_db_file}"

    with patch("src.config.settings.DATABASE_URL", temp_db_url), \
         patch("src.config.settings.APP_ENV", "test"):

        # Test creation of engine on isolated temp db
        test_engine = database.create_engine(temp_db_url)
        with test_engine.connect() as conn:
            pass
        test_engine.dispose()
        assert temp_db_file.exists()
