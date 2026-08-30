import logging
from fastapi import APIRouter, Depends, HTTPException, Response, Request, status
from sqlalchemy.orm import Session
from src.database import get_db
from src.models import User, UserSession, LEGACY_PRINCIPAL_ID
from src.schemas import UserLoginRequest, UserResponse, CSRFTokenResponse
from src.config import settings
from src.auth.security import verify_password, hash_password
from src.auth.normalization import normalize_username, normalize_email
from src.auth.session import create_session, revoke_session, get_active_session
from src.auth.csrf import generate_preauth_csrf_token, verify_csrf_hash, validate_origin
from src.auth.dependencies import get_current_user, get_current_session, require_csrf, get_optional_current_user
from src.auth.rate_limiter import rate_limiter, get_client_ip

logger = logging.getLogger("tradepro.routes.auth")

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

@router.get("/csrf-token", response_model=CSRFTokenResponse)
def get_csrf_token(request: Request, response: Response):
    """
    Generates a pre-authentication CSRF token for login protection.
    """
    ip = get_client_ip(request)
    rate_limiter.check_rate_limit(f"csrf_bootstrap:{ip}", max_requests=60, window_seconds=60)

    token = generate_preauth_csrf_token()
    response.set_cookie(
        key="tradepro_csrf_preauth",
        value=token,
        httponly=False,
        samesite=settings.COOKIE_SAMESITE,
        secure=settings.COOKIE_SECURE,
        domain=settings.COOKIE_DOMAIN,
        path="/",
    )
    return {"csrf_token": token}

@router.post("/login", response_model=UserResponse)
def login(
    login_req: UserLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    existing_user: User = Depends(get_optional_current_user)
):
    """
    Authenticates a user via Argon2id, rotates sessions, and sets HttpOnly session cookies.
    """
    ip = get_client_ip(request)
    rate_limiter.check_rate_limit(f"login_ip:{ip}", max_requests=10, window_seconds=60)
    rate_limiter.check_rate_limit(f"login_target:{login_req.username_or_email.strip().casefold()}", max_requests=5, window_seconds=60)

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Already authenticated. Please log out first."
        )

    # 1. Validate Origin / Referer
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not validate_origin(origin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed: Invalid or untrusted origin."
        )

    # 2. Validate Login CSRF Token
    preauth_cookie = request.cookies.get("tradepro_csrf_preauth")
    submitted_csrf = request.headers.get("X-CSRF-Token")
    if not preauth_cookie or not submitted_csrf or preauth_cookie != submitted_csrf:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Login CSRF validation failed."
        )

    # 3. Lookup user by normalized username or normalized email
    lookup_val = login_req.username_or_email.strip().casefold()
    user = db.query(User).filter(
        (User.normalized_username == lookup_val) | (User.normalized_email == lookup_val)
    ).first()

    # Hard-block legacy principal, inactive users, and missing accounts with generic 401
    if not user or user.id == LEGACY_PRINCIPAL_ID or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials."
        )

    # 4. Verify password with Argon2id
    is_valid, needs_rehash = verify_password(login_req.password, user.hashed_password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials."
        )

    # 5. Handle transparent rehash parameter upgrade
    if needs_rehash:
        user.hashed_password = hash_password(login_req.password)
        db.commit()

    # 6. Create session and issue cookies
    session_rec, raw_session_token, raw_csrf_token = create_session(db, user)

    response.set_cookie(
        key="tradepro_session",
        value=raw_session_token,
        httponly=True,
        samesite=settings.COOKIE_SAMESITE,
        secure=settings.COOKIE_SECURE,
        domain=settings.COOKIE_DOMAIN,
        path="/",
    )
    response.set_cookie(
        key="tradepro_csrf",
        value=raw_csrf_token,
        httponly=False,
        samesite=settings.COOKIE_SAMESITE,
        secure=settings.COOKIE_SECURE,
        domain=settings.COOKIE_DOMAIN,
        path="/",
    )
    # Clear preauth cookie
    response.delete_cookie(
        key="tradepro_csrf_preauth",
        path="/",
        domain=settings.COOKIE_DOMAIN,
        samesite=settings.COOKIE_SAMESITE,
        secure=settings.COOKIE_SECURE,
    )

    return user

@router.post("/logout")
def logout(
    response: Response,
    session: UserSession = Depends(get_current_session),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    """
    Revokes the current server-side session and clears authentication cookies.
    """
    revoke_session(db, session)

    for cookie_name in ("tradepro_session", "tradepro_csrf", "tradepro_csrf_preauth"):
        response.delete_cookie(
            key=cookie_name,
            path="/",
            domain=settings.COOKIE_DOMAIN,
            samesite=settings.COOKIE_SAMESITE,
            secure=settings.COOKIE_SECURE,
        )

    return {"message": "Logged out successfully."}

@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """Returns profile and role of currently authenticated user."""
    return current_user
