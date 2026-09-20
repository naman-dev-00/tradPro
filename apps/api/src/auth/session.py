import secrets
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
import logging
from starlette.requests import Request
from sqlalchemy.orm import Session
from sqlalchemy import text, update, or_
from src.models import User, UserSession, LEGACY_PRINCIPAL_ID
from src.config import settings

logger = logging.getLogger("tradepro.auth.session")

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

def get_active_session(
    db: Session,
    raw_session_token: str,
    request: Optional[Request] = None
) -> Optional[UserSession]:
    """
    Looks up a session by raw token, enforcing non-revocation, idle expiration, and absolute expiration.
    Performs purely read-only validation on the request-scoped db session.
    If the sliding-window throttle (60s) has elapsed, schedules a bounded post-request touch
    on request.state.session_touch. Never writes or commits in this function.
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

    # Sliding window refresh for idle expiration (throttled to at most once per 60 seconds)
    cutoff = now - timedelta(seconds=60)
    if session.last_accessed_at is None or session.last_accessed_at <= cutoff:
        new_idle = now + timedelta(minutes=settings.SESSION_IDLE_TIMEOUT_MINUTES)
        if new_idle > session.absolute_expires_at:
            new_idle = session.absolute_expires_at

        if request is not None and hasattr(request, "state"):
            request.state.session_touch = {
                "session_id": session.id,
                "session_hash": session_hash,
                "touch_time": now,
                "cutoff": cutoff,
                "new_idle_expires_at": new_idle,
            }

    return session

def execute_session_touch(touch_data: dict) -> None:
    """
    Executes a bounded, post-request session activity touch in an isolated, short-lived session.
    Must be called only AFTER the request-scoped database session has finalized and closed.
    Guards:
    - Session ID and session_hash match
    - is_revoked is False
    - idle_expires_at > touch_time (never revive idle-expired session)
    - absolute_expires_at > touch_time (never revive absolute-expired session)
    - last_accessed_at <= cutoff OR is NULL (at most 1 effective update per throttle interval)
    """
    if not touch_data:
        return

    session_id = touch_data.get("session_id")
    session_hash = touch_data.get("session_hash")
    touch_time = touch_data.get("touch_time")
    cutoff = touch_data.get("cutoff")
    new_idle_expires_at = touch_data.get("new_idle_expires_at")

    if not session_id or not session_hash or not touch_time or not cutoff or not new_idle_expires_at:
        return

    from src.database import SessionLocal, is_sqlite_locked_error

    touch_db = SessionLocal()
    try:
        stmt = (
            update(UserSession)
            .where(
                UserSession.id == session_id,
                UserSession.session_hash == session_hash,
                UserSession.is_revoked.is_(False),
                UserSession.idle_expires_at > touch_time,
                UserSession.absolute_expires_at > touch_time,
                or_(
                    UserSession.last_accessed_at.is_(None),
                    UserSession.last_accessed_at <= cutoff,
                ),
            )
            .values(
                last_accessed_at=touch_time,
                idle_expires_at=new_idle_expires_at,
            )
        )
        res = touch_db.execute(stmt)
        if res.rowcount > 0:
            touch_db.commit()
        else:
            touch_db.rollback()
    except Exception as exc:
        touch_db.rollback()
        if is_sqlite_locked_error(exc):
            logger.warning(
                "Transient SQLite lock during post-request session activity touch for session %s (best-effort skipped)",
                session_id,
            )
        else:
            logger.error(
                "Unexpected error during session activity touch for session %s: %s",
                session_id,
                exc,
                exc_info=True,
            )
    finally:
        touch_db.close()

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
