from typing import Optional, Callable
from fastapi import Request, Depends, HTTPException, status
from sqlalchemy.orm import Session
from src.database import get_db
from src.models import User, UserSession, LEGACY_PRINCIPAL_ID
from src.auth.session import get_active_session
from src.auth.csrf import verify_csrf_hash, validate_origin

def get_current_session(request: Request, db: Session = Depends(get_db)) -> UserSession:
    raw_token = request.cookies.get("tradepro_session")
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required."
        )

    session = get_active_session(db, raw_token)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required."
        )

    return session

def get_current_user(
    session: UserSession = Depends(get_current_session),
    db: Session = Depends(get_db)
) -> User:
    user = db.query(User).filter(User.id == session.user_id).first()
    if not user or not user.is_active or user.id == LEGACY_PRINCIPAL_ID:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required."
        )
    return user

def get_optional_current_user(
    request: Request,
    db: Session = Depends(get_db)
) -> Optional[User]:
    raw_token = request.cookies.get("tradepro_session")
    if not raw_token:
        return None
    session = get_active_session(db, raw_token)
    if not session:
        return None
    user = db.query(User).filter(User.id == session.user_id).first()
    if not user or not user.is_active or user.id == LEGACY_PRINCIPAL_ID:
        return None
    return user

def require_roles(*allowed_roles: str) -> Callable:
    """
    Returns a dependency enforcing that the authenticated user's role is in allowed_roles.
    """
    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Insufficient role permissions."
            )
        return current_user
    return role_checker

def require_csrf(
    request: Request,
    session: UserSession = Depends(get_current_session)
) -> None:
    """
    Enforces double-submit CSRF token matching and Origin header validation for state-changing requests.
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # Validate Origin or Referer
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if not validate_origin(origin):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed: Invalid or untrusted origin."
            )

        submitted_csrf = request.headers.get("X-CSRF-Token")
        if not submitted_csrf or not verify_csrf_hash(submitted_csrf, session.csrf_hash):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed: Invalid CSRF token."
            )
