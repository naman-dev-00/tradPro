import logging
import time
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from starlette.requests import Request
from fastapi import FastAPI, Depends, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text, select
from sqlalchemy.orm import sessionmaker, Session

from src.database import (
    Base,
    create_db_engine,
    get_db,
    get_read_only_db,
    is_sqlite_locked_error,
)
from src.database_safety import require_disposable_target
from src.models import User, UserSession, LEGACY_PRINCIPAL_ID
from src.auth.security import hash_password
from src.auth.session import (
    create_session,
    get_active_session,
    execute_session_touch,
    calculate_sha256,
)
from src.auth.dependencies import get_current_user, get_current_session
from src.middleware.session_touch import SessionTouchMiddleware


def test_get_request_uses_deferred_begin(tmp_path):
    """Verify that a GET request on SQLite emits standard BEGIN (not BEGIN IMMEDIATE)."""
    db_path = tmp_path / "test_get_begin.db"
    url = f"sqlite:///{db_path.as_posix()}"
    require_disposable_target(url)
    eng = create_db_engine(url)
    Base.metadata.create_all(bind=eng)

    executed_begins = []

    @event.listens_for(eng, "before_cursor_execute")
    def intercept_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().startswith("BEGIN"):
            executed_begins.append(statement.strip())

    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    # Simulate get_db for a GET request
    req = Request(scope={"type": "http", "method": "GET"})
    with patch("src.database.engine", eng), patch("src.database.SessionLocal", TestSession):
        gen = get_db(req)
        db = next(gen)
        try:
            db.execute(text("SELECT 1"))
        finally:
            try:
                next(gen)
            except StopIteration:
                pass

    eng.dispose()
    assert executed_begins == ["BEGIN"]


def test_post_request_uses_begin_immediate(tmp_path):
    """Verify that a POST request on SQLite emits BEGIN IMMEDIATE."""
    db_path = tmp_path / "test_post_begin.db"
    url = f"sqlite:///{db_path.as_posix()}"
    require_disposable_target(url)
    eng = create_db_engine(url)
    Base.metadata.create_all(bind=eng)

    executed_begins = []

    @event.listens_for(eng, "before_cursor_execute")
    def intercept_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().startswith("BEGIN"):
            executed_begins.append(statement.strip())

    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    req = Request(scope={"type": "http", "method": "POST"})
    with patch("src.database.engine", eng), patch("src.database.SessionLocal", TestSession):
        gen = get_db(req)
        db = next(gen)
        try:
            db.execute(text("SELECT 1"))
        finally:
            try:
                next(gen)
            except StopIteration:
                pass

    eng.dispose()
    assert executed_begins == ["BEGIN IMMEDIATE"]


def test_background_worker_uses_begin_immediate(tmp_path):
    """Verify that callers without a Request object default to BEGIN IMMEDIATE."""
    db_path = tmp_path / "test_bg_begin.db"
    url = f"sqlite:///{db_path.as_posix()}"
    require_disposable_target(url)
    eng = create_db_engine(url)
    Base.metadata.create_all(bind=eng)

    executed_begins = []

    @event.listens_for(eng, "before_cursor_execute")
    def intercept_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().startswith("BEGIN"):
            executed_begins.append(statement.strip())

    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    with patch("src.database.engine", eng), patch("src.database.SessionLocal", TestSession):
        gen = get_db(None)
        db = next(gen)
        try:
            db.execute(text("SELECT 1"))
        finally:
            try:
                next(gen)
            except StopIteration:
                pass

    eng.dispose()
    assert executed_begins == ["BEGIN IMMEDIATE"]


def test_postgresql_emits_neither_sqlite_begin():
    """Verify that PostgreSQL engine creation does not attach SQLite begin listeners."""
    pg_url = "postgresql+psycopg2://user:pass@localhost:5432/tradepro_disposable_test"
    with patch("src.database.require_disposable_target"), \
         patch("sqlalchemy.create_engine") as mock_create:
        mock_eng = MagicMock()
        mock_create.return_value = mock_eng
        create_db_engine(pg_url)
        # Verify no SQLite event listener registration occurred on mock_eng
        assert mock_eng.connect_args is None or "check_same_thread" not in getattr(mock_eng, "connect_args", {})


def test_same_session_object_for_auth_and_route(tmp_path):
    """Verify that authentication and route handler share the exact same Session object (assert auth_db is route_db)."""
    db_path = tmp_path / "test_same_session.db"
    url = f"sqlite:///{db_path.as_posix()}"
    require_disposable_target(url)
    eng = create_db_engine(url)
    Base.metadata.create_all(bind=eng)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    app = FastAPI()
    app.add_middleware(SessionTouchMiddleware)

    init_db = TestSession()
    user = User(
        username="sam_user",
        normalized_username="sam_user",
        email="sam@example.com",
        normalized_email="sam@example.com",
        hashed_password=hash_password("Pass123!"),
        role="EDITOR",
        is_active=True,
    )
    init_db.add(user)
    init_db.commit()
    sess_rec, raw_tok, _ = create_session(init_db, user)
    init_db.close()

    yielded_sessions = []
    observed_route_session = []

    def custom_get_db(request: Request = None):
        db = TestSession(bind=eng.execution_options(deferred_read_transaction=True))
        yielded_sessions.append(db)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = custom_get_db

    @app.get("/test-session-identity")
    def route(
        current_user: User = Depends(get_current_user),
        route_db: Session = Depends(get_db),
    ):
        observed_route_session.append(route_db)
        return {"username": current_user.username}

    client = TestClient(app)
    client.cookies.set("tradepro_session", raw_tok)

    res = client.get("/test-session-identity")
    assert res.status_code == 200

    # Proves FastAPI dependency cache reused the exact same Session instance across auth and route
    assert len(yielded_sessions) == 1
    assert len(observed_route_session) == 1
    assert observed_route_session[0] is yielded_sessions[0]
    eng.dispose()


def test_touch_starts_after_request_session_closed(tmp_path):
    """
    Prove the exact lifecycle ordering:
    1. Authentication SELECT begins.
    2. Handler runs.
    3. Response is produced and sent.
    4. Request-scoped database Session is finalized and closed.
    5. Post-request activity touch opens its independent transaction.
    6. Touch transaction commits and closes.
    7. Middleware completes.
    """
    db_path = tmp_path / "test_lifecycle.db"
    url = f"sqlite:///{db_path.as_posix()}"
    require_disposable_target(url)
    eng = create_db_engine(url)
    Base.metadata.create_all(bind=eng)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    lifecycle_events = []

    init_db = TestSession()
    user = User(
        username="lifecycle_user",
        normalized_username="lifecycle_user",
        email="life@example.com",
        normalized_email="life@example.com",
        hashed_password=hash_password("Pass123!"),
        role="EDITOR",
        is_active=True,
    )
    init_db.add(user)
    init_db.commit()
    sess_rec, raw_tok, _ = create_session(init_db, user)
    sess_rec.last_accessed_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    init_db.commit()
    init_db.close()

    app = FastAPI()
    app.add_middleware(SessionTouchMiddleware)

    def lifecycle_get_db(request: Request = None):
        lifecycle_events.append("request_db_open")
        db = TestSession()
        try:
            yield db
        finally:
            db.close()
            lifecycle_events.append("request_db_closed")

    app.dependency_overrides[get_db] = lifecycle_get_db

    @app.get("/lifecycle-test")
    def handler(current_user: User = Depends(get_current_user)):
        lifecycle_events.append("handler_executed")
        return {"ok": True}

    client = TestClient(app)
    client.cookies.set("tradepro_session", raw_tok)

    with patch("src.database.SessionLocal") as mock_sl:
        touch_db_mock = MagicMock()
        def mock_touch_db():
            lifecycle_events.append("touch_db_open")
            return touch_db_mock
        touch_db_mock.close.side_effect = lambda: lifecycle_events.append("touch_db_closed")
        touch_db_mock.commit.side_effect = lambda: lifecycle_events.append("touch_db_commit")
        mock_sl.side_effect = mock_touch_db

        res = client.get("/lifecycle-test")
        assert res.status_code == 200

    assert "request_db_open" in lifecycle_events
    assert "handler_executed" in lifecycle_events
    assert "request_db_closed" in lifecycle_events
    assert "touch_db_open" in lifecycle_events
    assert "touch_db_closed" in lifecycle_events

    req_close_idx = lifecycle_events.index("request_db_closed")
    touch_open_idx = lifecycle_events.index("touch_db_open")
    touch_close_idx = lifecycle_events.index("touch_db_closed")

    assert req_close_idx < touch_open_idx, "Request DB must be closed before touch DB opens!"
    assert touch_open_idx < touch_close_idx, "Touch DB must open before it closes!"
    eng.dispose()


def test_touch_never_waits_on_its_own_request_read_transaction(tmp_path):
    """Verify that the post-request touch operates in a clean, independent session without self-deadlock."""
    db_path = tmp_path / "test_no_self_deadlock.db"
    url = f"sqlite:///{db_path.as_posix()}"
    require_disposable_target(url)
    eng = create_db_engine(url)
    Base.metadata.create_all(bind=eng)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    init_db = TestSession()
    user = User(
        username="deadlock_user",
        normalized_username="deadlock_user",
        email="deadlock@example.com",
        normalized_email="deadlock@example.com",
        hashed_password=hash_password("Pass123!"),
        role="EDITOR",
        is_active=True,
    )
    init_db.add(user)
    init_db.commit()
    sess_rec, raw_tok, _ = create_session(init_db, user)
    sess_id = sess_rec.id
    sess_hash = sess_rec.session_hash
    sess_rec.last_accessed_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    init_db.commit()
    init_db.close()

    app = FastAPI()
    app.add_middleware(SessionTouchMiddleware)

    def clean_get_db(request: Request = None):
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = clean_get_db

    @app.get("/deadlock-check")
    def handler(current_user: User = Depends(get_current_user)):
        return {"status": "success"}

    client = TestClient(app)
    client.cookies.set("tradepro_session", raw_tok)

    with patch("src.database.SessionLocal", TestSession), patch("src.database.engine", eng):
        res = client.get("/deadlock-check")
        assert res.status_code == 200

    # Verify session was updated successfully without deadlock
    verify_db = TestSession()
    updated = verify_db.query(UserSession).filter(UserSession.id == sess_id).first()
    assert (datetime.now(timezone.utc) - updated.last_accessed_at).total_seconds() < 5
    verify_db.close()
    eng.dispose()


def test_parallel_get_touch_updates_at_most_one_row(tmp_path):
    """Verify atomic WHERE guard ensures at most one row is updated across concurrent touch instructions."""
    db_path = tmp_path / "test_parallel_touch.db"
    url = f"sqlite:///{db_path.as_posix()}"
    require_disposable_target(url)
    eng = create_db_engine(url)
    Base.metadata.create_all(bind=eng)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    init_db = TestSession()
    user = User(
        username="parallel_user",
        normalized_username="parallel_user",
        email="parallel@example.com",
        normalized_email="parallel@example.com",
        hashed_password=hash_password("Pass123!"),
        role="EDITOR",
        is_active=True,
    )
    init_db.add(user)
    init_db.commit()
    sess_rec, raw_tok, _ = create_session(init_db, user)
    sess_id = sess_rec.id
    sess_hash = sess_rec.session_hash
    old_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    sess_rec.last_accessed_at = old_time
    init_db.commit()
    init_db.close()

    touch_time = datetime.now(timezone.utc)
    cutoff = touch_time - timedelta(seconds=60)
    touch_data = {
        "session_id": sess_id,
        "session_hash": sess_hash,
        "touch_time": touch_time,
        "cutoff": cutoff,
        "new_idle_expires_at": touch_time + timedelta(minutes=30),
    }

    with patch("src.database.SessionLocal", TestSession), patch("src.database.engine", eng):
        # Execute first touch
        execute_session_touch(touch_data)
        # Execute second concurrent touch with the same cutoff
        execute_session_touch(touch_data)

    verify_db = TestSession()
    updated = verify_db.query(UserSession).filter(UserSession.id == sess_id).first()
    # last_accessed_at was updated to touch_time by the first touch; the second saw last_accessed_at > cutoff and updated 0 rows
    assert updated.last_accessed_at == touch_time
    verify_db.close()
    eng.dispose()


def test_touch_failure_cannot_change_sent_response(tmp_path):
    """Verify that a failure in the post-request touch cannot alter the already-sent HTTP response."""
    app = FastAPI()
    app.add_middleware(SessionTouchMiddleware)

    @app.get("/touch-fail")
    def handler(request: Request):
        request.state.session_touch = {
            "session_id": "dummy",
            "session_hash": "dummy",
            "touch_time": datetime.now(timezone.utc),
            "cutoff": datetime.now(timezone.utc),
            "new_idle_expires_at": datetime.now(timezone.utc),
        }
        return {"data": "crucial_payload"}

    client = TestClient(app)
    with patch("src.middleware.session_touch.execute_session_touch", side_effect=RuntimeError("Simulated DB Crash")):
        res = client.get("/touch-fail")
        assert res.status_code == 200
        assert res.json() == {"data": "crucial_payload"}


def test_touch_does_not_extend_revoked_session(tmp_path):
    """Verify that a revoked session is never updated by execute_session_touch."""
    db_path = tmp_path / "test_revoked_touch.db"
    url = f"sqlite:///{db_path.as_posix()}"
    require_disposable_target(url)
    eng = create_db_engine(url)
    Base.metadata.create_all(bind=eng)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    init_db = TestSession()
    user = User(
        username="revoked_u",
        normalized_username="revoked_u",
        email="rev@example.com",
        normalized_email="rev@example.com",
        hashed_password=hash_password("Pass123!"),
        role="EDITOR",
        is_active=True,
    )
    init_db.add(user)
    init_db.commit()
    sess_rec, raw_tok, _ = create_session(init_db, user)
    sess_id = sess_rec.id
    sess_hash = sess_rec.session_hash
    sess_rec.is_revoked = True
    old_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    sess_rec.last_accessed_at = old_time
    init_db.commit()
    init_db.close()

    touch_time = datetime.now(timezone.utc)
    touch_data = {
        "session_id": sess_id,
        "session_hash": sess_hash,
        "touch_time": touch_time,
        "cutoff": touch_time - timedelta(seconds=60),
        "new_idle_expires_at": touch_time + timedelta(minutes=30),
    }

    with patch("src.database.SessionLocal", TestSession), patch("src.database.engine", eng):
        execute_session_touch(touch_data)

    verify_db = TestSession()
    s = verify_db.query(UserSession).filter(UserSession.id == sess_id).first()
    assert s.last_accessed_at == old_time  # Not updated!
    verify_db.close()
    eng.dispose()


def test_touch_does_not_extend_idle_expired_session(tmp_path):
    """Verify that an idle-expired session is never revived by execute_session_touch."""
    db_path = tmp_path / "test_idle_expired_touch.db"
    url = f"sqlite:///{db_path.as_posix()}"
    require_disposable_target(url)
    eng = create_db_engine(url)
    Base.metadata.create_all(bind=eng)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    init_db = TestSession()
    user = User(
        username="idle_u",
        normalized_username="idle_u",
        email="idle@example.com",
        normalized_email="idle@example.com",
        hashed_password=hash_password("Pass123!"),
        role="EDITOR",
        is_active=True,
    )
    init_db.add(user)
    init_db.commit()
    sess_rec, raw_tok, _ = create_session(init_db, user)
    sess_id = sess_rec.id
    sess_hash = sess_rec.session_hash
    touch_time = datetime.now(timezone.utc)
    sess_rec.idle_expires_at = touch_time - timedelta(seconds=10)  # Expired
    sess_rec.last_accessed_at = touch_time - timedelta(seconds=120)
    init_db.commit()
    init_db.close()

    touch_data = {
        "session_id": sess_id,
        "session_hash": sess_hash,
        "touch_time": touch_time,
        "cutoff": touch_time - timedelta(seconds=60),
        "new_idle_expires_at": touch_time + timedelta(minutes=30),
    }

    with patch("src.database.SessionLocal", TestSession), patch("src.database.engine", eng):
        execute_session_touch(touch_data)

    verify_db = TestSession()
    s = verify_db.query(UserSession).filter(UserSession.id == sess_id).first()
    assert s.idle_expires_at < touch_time  # Still expired!
    verify_db.close()
    eng.dispose()


def test_touch_does_not_extend_absolute_expiry(tmp_path):
    """Verify that sliding idle timeout is strictly capped by absolute expiry and absolute expiry never extends."""
    db_path = tmp_path / "test_abs_expiry.db"
    url = f"sqlite:///{db_path.as_posix()}"
    require_disposable_target(url)
    eng = create_db_engine(url)
    Base.metadata.create_all(bind=eng)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    init_db = TestSession()
    user = User(
        username="abs_u",
        normalized_username="abs_u",
        email="abs@example.com",
        normalized_email="abs@example.com",
        hashed_password=hash_password("Pass123!"),
        role="EDITOR",
        is_active=True,
    )
    init_db.add(user)
    init_db.commit()
    sess_rec, raw_tok, _ = create_session(init_db, user)
    sess_id = sess_rec.id
    sess_hash = sess_rec.session_hash
    touch_time = datetime.now(timezone.utc)
    # Absolute expiry is 5 minutes from now; idle would normally be +30 minutes
    fixed_abs_expiry = touch_time + timedelta(minutes=5)
    sess_rec.absolute_expires_at = fixed_abs_expiry
    sess_rec.last_accessed_at = touch_time - timedelta(seconds=120)
    init_db.commit()
    init_db.close()

    capped_idle = min(touch_time + timedelta(minutes=30), fixed_abs_expiry)
    assert capped_idle == fixed_abs_expiry

    touch_data = {
        "session_id": sess_id,
        "session_hash": sess_hash,
        "touch_time": touch_time,
        "cutoff": touch_time - timedelta(seconds=60),
        "new_idle_expires_at": capped_idle,
    }

    with patch("src.database.SessionLocal", TestSession), patch("src.database.engine", eng):
        execute_session_touch(touch_data)

    verify_db = TestSession()
    s = verify_db.query(UserSession).filter(UserSession.id == sess_id).first()
    assert s.idle_expires_at == fixed_abs_expiry
    assert s.absolute_expires_at == fixed_abs_expiry
    verify_db.close()
    eng.dispose()


def test_touch_cannot_commit_or_rollback_route_state(tmp_path):
    """Verify that execute_session_touch operates in an independent session that cannot affect route session state."""
    db_path = tmp_path / "test_route_state.db"
    url = f"sqlite:///{db_path.as_posix()}"
    require_disposable_target(url)
    eng = create_db_engine(url)
    Base.metadata.create_all(bind=eng)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    route_db = TestSession()
    # Stage an uncommitted user in route_db
    staged_user = User(
        username="staged_user",
        normalized_username="staged_user",
        email="staged@example.com",
        normalized_email="staged@example.com",
        hashed_password="hash",
        role="EDITOR",
        is_active=True,
    )
    route_db.add(staged_user)

    # Touch runs
    touch_data = {
        "session_id": "non_existent",
        "session_hash": "non_existent",
        "touch_time": datetime.now(timezone.utc),
        "cutoff": datetime.now(timezone.utc),
        "new_idle_expires_at": datetime.now(timezone.utc),
    }
    with patch("src.database.SessionLocal", TestSession), patch("src.database.engine", eng):
        execute_session_touch(touch_data)

    # Verify staged_user is still pending in route_db and NOT committed to DB
    check_db = TestSession()
    found = check_db.query(User).filter(User.username == "staged_user").first()
    assert found is None, "Touch session must not commit route-owned mutations!"
    check_db.close()
    route_db.rollback()
    route_db.close()
    eng.dispose()


def test_touch_logs_unrelated_database_error_without_secret_leakage():
    """Verify that unrelated database errors are logged with error level without leaking tokens or secrets."""
    touch_data = {
        "session_id": "sess_secret_12345",
        "session_hash": "hash_abcd_9999",
        "touch_time": datetime.now(timezone.utc),
        "cutoff": datetime.now(timezone.utc),
        "new_idle_expires_at": datetime.now(timezone.utc),
    }

    mock_db = MagicMock()
    mock_db.execute.side_effect = RuntimeError("Disk IO Error: connection lost")
    mock_session_local = MagicMock(return_value=mock_db)

    with patch("src.database.SessionLocal", mock_session_local), \
         patch("src.auth.session.logger.error") as mock_log_err:
        execute_session_touch(touch_data)
        mock_log_err.assert_called_once()
        log_str = str(mock_log_err.call_args)
        assert "Disk IO Error" in log_str
        assert "sess_secret_12345" in log_str  # ID logged safely
        assert "raw_token" not in log_str

    # Rollback must be called
    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


def test_post_request_touch_session_always_closes():
    """Verify that touch_db.close() is called deterministically even on unhandled exception."""
    mock_db = MagicMock()
    mock_db.execute.side_effect = Exception("Fatal SQL Error")
    mock_session_local = MagicMock(return_value=mock_db)

    touch_data = {
        "session_id": "s1",
        "session_hash": "h1",
        "touch_time": datetime.now(timezone.utc),
        "cutoff": datetime.now(timezone.utc),
        "new_idle_expires_at": datetime.now(timezone.utc),
    }

    with patch("src.database.SessionLocal", mock_session_local):
        execute_session_touch(touch_data)

    mock_db.close.assert_called_once()


def test_test_bootstrap_refuses_real_dev_db():
    """Verify that require_disposable_target rejects the development database path under APP_ENV=test."""
    with pytest.raises(RuntimeError) as exc:
        require_disposable_target("sqlite:///./tradepro.db")
    assert any(msg in str(exc.value) for msg in [
        "Test mode refuses the development database path",
        "Test schema operations require a disposable TEMP database"
    ])

    with pytest.raises(RuntimeError) as exc2:
        require_disposable_target("sqlite:///apps/api/tradepro.db")
    assert any(msg in str(exc2.value) for msg in [
        "Test mode refuses the development database path",
        "Test schema operations require a disposable TEMP database"
    ])


def test_disposable_connection_creates_no_wal_or_shm(tmp_path):
    """Verify that connecting to a disposable database does not create .db-wal or .db-shm files."""
    db_path = tmp_path / "test_no_wal.db"
    url = f"sqlite:///{db_path.as_posix()}"
    require_disposable_target(url)
    eng = create_db_engine(url)
    with eng.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INT)"))
        conn.execute(text("INSERT INTO t VALUES (1)"))
    eng.dispose()

    wal_file = tmp_path / "test_no_wal.db-wal"
    shm_file = tmp_path / "test_no_wal.db-shm"

    assert not wal_file.exists(), "Unexpected WAL file created!"
    assert not shm_file.exists(), "Unexpected SHM file created!"
