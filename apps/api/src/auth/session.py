import secrets
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import text
from src.models import User, UserSession, LEGACY_PRINCIPAL_ID
from src.config import settings

def calculate_sha256(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

def create_session(db: Session, user: User) -> Tuple[UserSession, str, str]:
    """
    Creates a new authenticated server-side session for a user.
    Returns: (session_record, raw_session_token, raw_csrf_token)
    """
    if user.id == LEGACY_PRINCIPAL_ID or not user.is_active:
        raise ValueError("Cannot create session for inactive user or legacy principal.")

    raw_session_token = secrets.token_urlsafe(32)
    raw_csrf_token = secrets.token_urlsafe(32)

    session_hash = calculate_sha256(raw_session_token)
    csrf_hash = calculate_sha256(raw_csrf_token)

    now = datetime.now(timezone.utc)
    idle_expires_at = now + timedelta(minutes=settings.SESSION_IDLE_TIMEOUT_MINUTES)
    absolute_expires_at = now + timedelta(hours=settings.SESSION_ABSOLUTE_TIMEOUT_HOURS)

    session = UserSession(
        user_id=user.id,
        session_hash=session_hash,
        csrf_hash=csrf_hash,
        created_at=now,
        last_accessed_at=now,
        idle_expires_at=idle_expires_at,
        absolute_expires_at=absolute_expires_at,
        is_revoked=False,
    )

    db.add(session)
    db.commit()
    db.refresh(session)
    return session, raw_session_token, raw_csrf_token

def get_active_session(db: Session, raw_session_token: str) -> Optional[UserSession]:
    """
    Looks up a session by raw token, enforcing non-revocation, idle expiration, and absolute expiration.
    Performs sliding window update of last_accessed_at and idle_expires_at.
    """
    if not raw_session_token or not isinstance(raw_session_token, str):
        return None

    session_hash = calculate_sha256(raw_session_token)
    now = datetime.now(timezone.utc)

    session = db.query(UserSession).filter(
        UserSession.session_hash == session_hash,
        UserSession.is_revoked.is_(False),
        UserSession.idle_expires_at > now,
        UserSession.absolute_expires_at > now,
    ).first()

    if not session:
        return None

    # Sliding window refresh for idle expiration
    new_idle = now + timedelta(minutes=settings.SESSION_IDLE_TIMEOUT_MINUTES)
    # Ensure idle expiration does not exceed absolute expiration
    if new_idle > session.absolute_expires_at:
        new_idle = session.absolute_expires_at

    session.last_accessed_at = now
    session.idle_expires_at = new_idle
    db.commit()
    db.refresh(session)
    return session

def revoke_session(db: Session, session: UserSession) -> None:
    now = datetime.now(timezone.utc)
    session.is_revoked = True
    session.revoked_at = now
    db.commit()

def revoke_all_user_sessions(db: Session, user_id: str) -> int:
    """Revokes all active sessions for a user (called on role change, password reset, or deactivation)."""
    now = datetime.now(timezone.utc)
    updated_count = db.query(UserSession).filter(
        UserSession.user_id == user_id,
        UserSession.is_revoked.is_(False)
    ).update({
        UserSession.is_revoked: True,
        UserSession.revoked_at: now
    }, synchronize_session=False)
    db.commit()
    return updated_count

def cleanup_sessions(db: Session, retention_days: int = 7, batch_size: int = 500) -> int:
    """
    Bounded, batch-oriented cleanup of expired or revoked sessions older than retention_days.
    Never removes active, valid sessions.
    """
    now = datetime.now(timezone.utc)
    retention_cutoff = now - timedelta(days=retention_days)

    total_deleted = 0
    while True:
        # Select batch of candidate session IDs to delete
        candidates = db.query(UserSession.id).filter(
            (UserSession.is_revoked.is_(True) & (UserSession.revoked_at < retention_cutoff)) |
            (UserSession.absolute_expires_at < retention_cutoff) |
            (UserSession.idle_expires_at < retention_cutoff)
        ).limit(batch_size).all()

        if not candidates:
            break

        candidate_ids = [c[0] for c in candidates]
        deleted = db.query(UserSession).filter(
            UserSession.id.in_(candidate_ids)
        ).delete(synchronize_session=False)

        db.commit()
        total_deleted += deleted

        if len(candidate_ids) < batch_size:
            break

    return total_deleted
