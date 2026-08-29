import logging
from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import sessionmaker, declarative_base
from src.config import settings

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
        return settings.DATABASE_URL

    if settings.APP_ENV in ["local", "test"]:
        return settings.FALLBACK_DB_URL

    raise RuntimeError("DATABASE_URL environment variable is required in staging and production environments.")

def create_active_engine():
    db_url = get_db_url()
    masked_url = mask_db_url(db_url)
    connect_args = {}

    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    logger.info(f"Initialized database engine for environment '{settings.APP_ENV}' with database: {masked_url}")
    return create_engine(db_url, connect_args=connect_args)

engine = create_active_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def verify_database_connection():
    db_url = get_db_url()
    masked_url = mask_db_url(db_url)

    # If DATABASE_URL is explicitly set (e.g. postgresql://...), test connection
    if settings.DATABASE_URL:
        try:
            logger.info(f"Verifying connection to configured database: {masked_url}")
            with engine.connect() as conn:
                pass
            logger.info("Successfully verified connection to configured database.")
        except Exception as e:
            logger.error(f"Failed to connect to configured database ({masked_url}): {e}")
            raise RuntimeError(f"Failed to connect to configured database ({masked_url}).") from e
    else:
        logger.info(f"Using local/test SQLite database ({masked_url}). Connection verification skipped.")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
