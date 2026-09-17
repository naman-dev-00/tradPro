"""Runs before application imports, including pytest collection."""
import hashlib
import os
from pathlib import Path
import tempfile

DEVELOPMENT_DB = Path(__file__).resolve().parents[1] / "tradepro.db"


def development_evidence():
    if not DEVELOPMENT_DB.exists():
        return None
    stat = DEVELOPMENT_DB.stat()
    return hashlib.sha256(DEVELOPMENT_DB.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime_ns


BEFORE_COLLECTION = development_evidence()
os.environ["APP_ENV"] = "test"
TEST_ROOT = Path(tempfile.mkdtemp(prefix="tradepro_pytest_"))
(TEST_ROOT / ".tradepro-pytest-disposable").touch()
configured = os.environ.get("DATABASE_URL")
if configured:
    # Reject an unsafe supplied URL instead of silently hiding it.
    from src.database_safety import require_disposable_target
    require_disposable_target(configured)
else:
    os.environ["DATABASE_URL"] = "sqlite:///" + (TEST_ROOT / "startup.db").as_posix()


def assert_development_preserved():
    assert development_evidence() == BEFORE_COLLECTION, "Development DB changed during collection/execution"
