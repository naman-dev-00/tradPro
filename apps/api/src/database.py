import logging
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import sessionmaker, declarative_base
from src.config import settings
from src.database_safety import require_disposable_target

logger = logging.getLogger("tradepro.database")
logging.basicConfig(level=logging.INFO)

from datetime import datetime, timezone
from sqlalchemy.types import TypeDecorator, DateTime

Base = declarative_base()

class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise ValueError("Timestamp must be a datetime object.")
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("Naive timestamps are forbidden. Timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

def mask_db_url(url_str: str) -> str:
    try:
        url_obj = make_url(url_str)
        return url_obj.render_as_string(hide_password=True)
    except Exception:
        return url_str

def get_db_url() -> str:
    if settings.DATABASE_URL:
        if settings.APP_ENV == "test":
            require_disposable_target(settings.DATABASE_URL)
        return settings.DATABASE_URL

    if settings.APP_ENV == "test":
        raise RuntimeError("Test mode requires an explicit disposable DATABASE_URL")

    if settings.APP_ENV == "local":
        return settings.FALLBACK_DB_URL

    raise RuntimeError("DATABASE_URL environment variable is required in staging and production environments.")

def create_db_engine(db_url: str):
    if settings.APP_ENV == "test":
        require_disposable_target(db_url)
    masked_url = mask_db_url(db_url)
    connect_args = {}

    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 30.0

    eng = create_engine(db_url, connect_args=connect_args)

    if db_url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(eng, "connect")
        def do_connect(dbapi_connection, connection_record):
            # disable pysqlite's emitting of the default BEGIN statement
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(eng, "begin")
        def do_begin(conn):
            # Check if this transaction is explicitly marked read_only
            if conn.info.get("read_only", False) or conn.get_execution_options().get("read_only", False):
                conn.exec_driver_sql("BEGIN")
            else:
                conn.exec_driver_sql("BEGIN IMMEDIATE")

        @event.listens_for(eng, "checkin")
        def do_checkin(dbapi_connection, connection_record):
            # Clear connection info to ensure read_only cannot leak across pooled checkouts
            if connection_record and hasattr(connection_record, "info"):
                connection_record.info.pop("read_only", None)

    return eng

def create_active_engine():
    db_url = get_db_url()
    masked_url = mask_db_url(db_url)
    logger.info(f"Initialized database engine for environment '{settings.APP_ENV}' with database: {masked_url}")
    return create_db_engine(db_url)

engine = create_active_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Deliberately pinned until a separate development 0006 migration is authorized.
LOCAL_SCHEMA_REVISION = "0005_upstox_sandbox"


def expected_schema_revision():
    if settings.APP_ENV == "local":
        return LOCAL_SCHEMA_REVISION
    from pathlib import Path
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def verify_database_connection():
    """Read-only readiness check; never create a missing SQLite file or schema."""
    from pathlib import Path
    expected = expected_schema_revision()
    instruction = f"Database schema is unavailable or outdated. Run Alembic upgrade {expected} against the intended database."
    url = engine.url
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        if not Path(url.database).is_file():
            raise RuntimeError(instruction)
    try:
        with engine.connect().execution_options(read_only=True) as conn:
            conn.execute(text("SELECT 1"))
            if not inspect(conn).has_table("alembic_version"):
                raise RuntimeError(instruction)
            versions = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            if versions != [expected]:
                raise RuntimeError(instruction)
    except Exception:
        # Never expose a URL, credentials, SQL error text or exception chain.
        raise RuntimeError(instruction) from None


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_read_only_db():
    """
    Read-only database dependency for GET requests:
    - Sets execution_options(read_only=True) on the engine so SQLite emits standard BEGIN without write reservation.
    - Connection checkin listener and session close ensure read_only flag is never leaked
      to subsequent write requests on pooled connections.
    """
    db = SessionLocal(bind=engine.execution_options(read_only=True))
    try:
        yield db
    finally:
        db.close()
