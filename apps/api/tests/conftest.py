from tests.bootstrap import TEST_ROOT, assert_development_preserved
import os
import hashlib
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from src.database import Base, get_db, get_read_only_db, create_db_engine
from src.main import app
from src.models import User
from src.auth.security import hash_password
from src.auth.session import create_session
from src.auth.rate_limiter import rate_limiter

# Use isolated SQLite file for testing
from src.database_safety import require_disposable_target


def pytest_collection_finish(session):
    assert_development_preserved()


def pytest_sessionfinish(session, exitstatus):
    assert_development_preserved()


@pytest.fixture(scope="session", autouse=True)
def initialize_disposable_application_schema():
    # The environment/engine already selected this target before collection.
    from alembic import command
    from alembic.config import Config
    from src import database
    url = database.engine.url.render_as_string(hide_password=False)
    require_disposable_target(url)
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
    yield
    database.engine.dispose()
    assert_development_preserved()


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    url = "sqlite:///" + (tmp_path / "unit.db").as_posix()
    require_disposable_target(url)
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    rate_limiter.clear()
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        rate_limiter.clear()
        engine.dispose()

@pytest.fixture(name="db_session")
def db_session_fixture(session):
    return session

@pytest.fixture(name="test_user")
def test_user_fixture(session):
    user = session.query(User).filter(User.normalized_username == "default_test_editor").first()
    if not user:
        user = User(
            username="default_test_editor",
            normalized_username="default_test_editor",
            email="editor@tradepro.test",
            normalized_email="editor@tradepro.test",
            hashed_password=hash_password("DefaultPassword123!"),
            role="EDITOR",
            is_active=True
        )
        session.add(user)
        session.commit()
        session.refresh(user)
    return user

@pytest.fixture(name="client")
def client_fixture(session, test_user):
    def override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_read_only_db] = override_get_db

    # Create session for default authenticated test user
    sess_rec, raw_sess, raw_csrf = create_session(session, test_user)

    c = TestClient(app, headers={"X-CSRF-Token": raw_csrf, "Origin": "http://localhost:3000"})
    c.cookies.set("tradepro_session", raw_sess)
    c.cookies.set("tradepro_csrf", raw_csrf)

    try:
        yield c
    finally:
        app.dependency_overrides.clear()

@pytest.fixture(name="unauth_client")
def unauth_client_fixture(session):
    def override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_read_only_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
