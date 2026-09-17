"""Target validation without importing the application or opening a database."""
import os
from pathlib import Path
from sqlalchemy.engine.url import make_url

DEVELOPMENT_DB = Path(__file__).resolve().parents[1] / "tradepro.db"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def reject_development_test_target(url: str) -> None:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return
    name = parsed.database
    if not name or name == ":memory:":
        return
    if name.startswith("file:"):
        from urllib.parse import urlsplit
        from urllib.request import url2pathname
        name = url2pathname(urlsplit(name).path)
    target = Path(name).resolve()
    if target == DEVELOPMENT_DB.resolve() or (
        target.exists() and DEVELOPMENT_DB.exists() and os.path.samefile(target, DEVELOPMENT_DB)
    ):
        raise RuntimeError("Test mode refuses the development database path")


def require_disposable_target(url: str) -> None:
    """Explicit test DDL is limited to TEMP SQLite or the named local CI database."""
    import tempfile
    reject_development_test_target(url)
    parsed = make_url(url)
    if parsed.get_backend_name() == "postgresql":
        if parsed.host not in {"localhost", "127.0.0.1", "postgres"} or parsed.database != "tradepro_test":
            raise RuntimeError("PostgreSQL tests require the local disposable tradepro_test database")
        return
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
        raise RuntimeError("Tests require an explicit disposable database path")
    target = Path(parsed.database).resolve()
    if not target.is_relative_to(Path(tempfile.gettempdir()).resolve()) or target.is_relative_to(REPOSITORY_ROOT):
        raise RuntimeError("Test schema operations require a disposable TEMP database")
