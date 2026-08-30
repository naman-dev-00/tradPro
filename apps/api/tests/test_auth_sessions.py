import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.database import Base
from src.models import User, UserSession, LEGACY_PRINCIPAL_ID
from src.auth.session import (
    create_session,
    get_active_session,
    revoke_session,
    revoke_all_user_sessions,
    cleanup_sessions,
    calculate_sha256,
)

def test_session_lifecycle(session):
    user = User(
        username="trader_bob",
        normalized_username="trader_bob",
        email="bob@example.com",
        normalized_email="bob@example.com",
        hashed_password="$argon2id$mock",
        role="EDITOR",
        is_active=True
    )
    session.add(user)
    session.commit()

    # 1. Create session
    sess_rec, raw_sess, raw_csrf = create_session(session, user)
    assert sess_rec.user_id == user.id
    assert sess_rec.session_hash == calculate_sha256(raw_sess)
    assert sess_rec.csrf_hash == calculate_sha256(raw_csrf)
    assert sess_rec.is_revoked is False

    # 2. Lookup active session
    active = get_active_session(session, raw_sess)
    assert active is not None
    assert active.id == sess_rec.id

    # 3. Revoke single session
    revoke_session(session, active)
    assert get_active_session(session, raw_sess) is None

def test_session_revocation_on_privilege_change(session):
    user = User(
        username="trader_alice",
        normalized_username="trader_alice",
        email="alice@example.com",
        normalized_email="alice@example.com",
        hashed_password="$argon2id$mock",
        role="VIEWER",
        is_active=True
    )
    session.add(user)
    session.commit()

    # Create two active sessions
    _, raw_sess1, _ = create_session(session, user)
    _, raw_sess2, _ = create_session(session, user)

    assert get_active_session(session, raw_sess1) is not None
    assert get_active_session(session, raw_sess2) is not None

    # Revoke all user sessions
    revoked_count = revoke_all_user_sessions(session, user.id)
    assert revoked_count == 2

    assert get_active_session(session, raw_sess1) is None
    assert get_active_session(session, raw_sess2) is None

def test_legacy_principal_cannot_receive_session(session):
    legacy = User(
        id=LEGACY_PRINCIPAL_ID,
        username="system_legacy_owner",
        normalized_username="system_legacy_owner",
        email="system_legacy@tradepro.internal",
        normalized_email="system_legacy@tradepro.internal",
        hashed_password="!DISABLED",
        role="VIEWER",
        is_active=False
    )
    session.add(legacy)
    session.commit()

    with pytest.raises(ValueError) as exc:
        create_session(session, legacy)
    assert "Cannot create session" in str(exc.value)

def test_session_cleanup_retention(session):
    user = User(
        username="trader_carol",
        normalized_username="trader_carol",
        email="carol@example.com",
        normalized_email="carol@example.com",
        hashed_password="$argon2id$mock",
        role="EDITOR",
        is_active=True
    )
    session.add(user)
    session.commit()

    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=10)

    # 1. Active valid session (must NOT be deleted)
    active_sess, raw_act, _ = create_session(session, user)

    # 2. Recently revoked session (3 days old < 7 days retention, must NOT be deleted)
    recent_revoked, raw_rec, _ = create_session(session, user)
    recent_revoked.is_revoked = True
    recent_revoked.revoked_at = now - timedelta(days=3)
    session.commit()

    # 3. Old revoked session (10 days old > 7 days retention, MUST be deleted)
    old_revoked, raw_old, _ = create_session(session, user)
    old_revoked.is_revoked = True
    old_revoked.revoked_at = old_time
    session.commit()
    old_revoked_id = old_revoked.id

    deleted = cleanup_sessions(session, retention_days=7)
    assert deleted == 1

    # Verify remaining
    remaining = session.query(UserSession).all()
    remaining_ids = {r.id for r in remaining}
    assert active_sess.id in remaining_ids
    assert recent_revoked.id in remaining_ids
    assert old_revoked_id not in remaining_ids

def test_session_idle_and_absolute_expiration(session):
    user = User(
        username="trader_dave",
        normalized_username="trader_dave",
        email="dave@example.com",
        normalized_email="dave@example.com",
        hashed_password="$argon2id$mock",
        role="EDITOR",
        is_active=True
    )
    session.add(user)
    session.commit()

    now = datetime.now(timezone.utc)

    # 1. Idle expired session (idle_expires_at in the past)
    sess_idle, raw_idle, _ = create_session(session, user)
    sess_idle.idle_expires_at = now - timedelta(minutes=5)
    session.commit()
    assert get_active_session(session, raw_idle) is None

    # 2. Absolute expired session (absolute_expires_at in the past)
    sess_abs, raw_abs, _ = create_session(session, user)
    sess_abs.absolute_expires_at = now - timedelta(minutes=5)
    session.commit()
    assert get_active_session(session, raw_abs) is None
