import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.database import Base, get_db
from src.main import app
from src.models import User
from src.auth.security import hash_password
from src.auth.session import create_session
from src.auth.rate_limiter import rate_limiter

# Use isolated SQLite file for testing
SQLALCHEMY_DATABASE_URL = "sqlite:///./test_tradepro.db"

@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        connect_args={"check_same_thread": False}
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Create tables
    Base.metadata.create_all(bind=engine)
    rate_limiter.clear()

    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        rate_limiter.clear()
        # Drop tables
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
        # Remove file
        if os.path.exists("./test_tradepro.db"):
            try:
                os.remove("./test_tradepro.db")
            except Exception:
                pass

@pytest.fixture(name="db_session")
def db_session_fixture(session):
    return session

@pytest.fixture(name="test_user")
def test_user_fixture(session):
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
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
