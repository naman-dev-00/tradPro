"""Broker Connection and Live Readiness Routes (Phase 7A).

Provides:
- OAuth authorization URL generation with single-use, session-bound state (protected POST).
- OAuth callback exchange with atomic replay protection (protected POST).
- Read-only connection status and disconnection.
- Authenticated snapshot refresh and read-only snapshot retrieval.
- Multi-gate live readiness checks (live_dispatch_enabled=False).
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from src.auth.dependencies import (
    get_current_session,
    get_current_user,
    require_csrf,
    require_roles,
)
from src.auth.rate_limiter import get_client_ip, rate_limiter
from src.database import get_db
from src.engine.broker.upstox_read_adapter import (
    BrokerAuthError,
    BrokerRateLimitError,
    BrokerTransportError,
)
from src.models import User, UserSession
from src.schemas import (
    BrokerAuthorizeUrlRequest,
    BrokerAuthorizeUrlResponse,
    BrokerConnectionStatusResponse,
    BrokerOAuthCallbackRequest,
    BrokerReadinessResponse,
    BrokerSnapshotRefreshResponse,
    BrokerSnapshotResponse,
)
from src.services.broker_service import (
    BrokerConflictError,
    BrokerNotConfiguredError,
    BrokerSecurityError,
    BrokerService,
    BrokerServiceError,
    BrokerUnsupportedCapabilityError,
    InvalidOAuthStateError,
    OAuthExpiredError,
    OAuthReplayError,
    OAuthSecurityError,
)


logger = logging.getLogger("tradepro.routes.broker")

router = APIRouter(prefix="/api/v1/broker", tags=["broker"])


@router.post("/auth/authorize-url", response_model=BrokerAuthorizeUrlResponse)
def get_authorize_url(
    req: BrokerAuthorizeUrlRequest,
    request: Request,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    current_session: UserSession = Depends(get_current_session),
    csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """
    Generates a single-use, expiring OAuth state token and Upstox login dialog URL.
    Protected mutation bound strictly to the initiating user, session, and role.
    """
    client_ip = get_client_ip(request)
    rate_limiter.check_rate_limit(f"broker_auth_url:{current_user.id}", max_requests=20, window_seconds=60)

    try:
        data = BrokerService.generate_authorization_url(
            db=db,
            owner_id=current_user.id,
            session_id=current_session.id,
            environment=req.environment,
            client_ip=client_ip,
            redirect_uri_override=req.redirect_uri,
        )
        return BrokerAuthorizeUrlResponse(**data)
    except BrokerUnsupportedCapabilityError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except OAuthSecurityError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except BrokerNotConfiguredError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        )


@router.post("/auth/callback", response_model=BrokerConnectionStatusResponse)
def handle_oauth_callback(
    req: BrokerOAuthCallbackRequest,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    current_session: UserSession = Depends(get_current_session),
    csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """
    Completes OAuth 2.0 authorization code flow.
    Atomically consumes single-use state, exchanges code for daily token,
    and binds provider-confirmed UCC identity to the local owner.
    """
    rate_limiter.check_rate_limit(f"broker_callback:{current_user.id}", max_requests=10, window_seconds=60)

    try:
        conn_dict = BrokerService.handle_oauth_callback(
            db=db,
            code=req.code,
            state_token=req.state,
            current_user_id=current_user.id,
            current_session_id=current_session.id,
        )
        return BrokerConnectionStatusResponse(**conn_dict)
    except (InvalidOAuthStateError, OAuthExpiredError, OAuthReplayError, BrokerUnsupportedCapabilityError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except (BrokerSecurityError, OAuthSecurityError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Broker authorization security validation failed.")
    except BrokerConflictError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This broker account is already connected to another user.")
    except BrokerNotConfiguredError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Broker credentials are not configured on this server.")
    except BrokerAuthError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Broker authentication failed with provider.")
    except BrokerRateLimitError:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Broker rate limit exceeded.")
    except (BrokerTransportError, BrokerServiceError):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to communicate with broker service.")


@router.get("/connection", response_model=BrokerConnectionStatusResponse)
def get_broker_connection(
    request: Request,
    environment: str = Query("LIVE", pattern="^(LIVE|SANDBOX)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Pure read-only query returning sanitized broker connection metadata.
    Zero mutations, flushes, or commits. Suppresses session touch writes.
    """
    # Guarantee no-write contract by suppressing post-request session touch write
    request.state.session_touch = None

    rate_limiter.check_rate_limit(f"broker_conn:{current_user.id}", max_requests=60, window_seconds=60)
    data = BrokerService.get_connection_status(db=db, owner_id=current_user.id, environment=environment)
    return BrokerConnectionStatusResponse(**data)


@router.post("/connection/disconnect", response_model=BrokerConnectionStatusResponse)
def disconnect_broker_connection(
    environment: str = Query("LIVE", pattern="^(LIVE|SANDBOX)$"),
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """
    Disconnects the active broker connection and wipes encrypted tokens.
    """
    rate_limiter.check_rate_limit(f"broker_disconnect:{current_user.id}", max_requests=10, window_seconds=60)
    data = BrokerService.disconnect(db=db, owner_id=current_user.id, environment=environment)
    return BrokerConnectionStatusResponse(**data)


@router.post("/snapshots/refresh", response_model=BrokerSnapshotRefreshResponse)
def refresh_broker_snapshots(
    environment: str = Query("LIVE", pattern="^(LIVE|SANDBOX)$"),
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """
    Fetches read-only snapshot of balances, margins, holdings, positions, and orders.
    Rechecks connection version before saving. Produces zero paper ledger mutations.
    """
    rate_limiter.check_rate_limit(f"broker_snapshot_refresh:{current_user.id}", max_requests=15, window_seconds=60)

    try:
        snapshot_summary = BrokerService.refresh_snapshots(
            db=db,
            owner_id=current_user.id,
            environment=environment,
        )
        return BrokerSnapshotRefreshResponse(**snapshot_summary)
    except BrokerUnsupportedCapabilityError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except BrokerAuthError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Broker token is expired or unauthorized.")
    except BrokerRateLimitError:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Broker rate limit exceeded.")
    except BrokerConflictError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Broker connection state modified concurrently.")
    except BrokerSecurityError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Broker security validation failed.")
    except BrokerTransportError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to fetch snapshot from broker service.")
    except BrokerServiceError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Broker snapshot request failed.")


@router.get("/snapshots/latest", response_model=BrokerSnapshotResponse)
def get_latest_broker_snapshot(
    request: Request,
    environment: str = Query("LIVE", pattern="^(LIVE|SANDBOX)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Pure read-only query returning the latest persisted broker snapshot.
    Zero DB writes, flushes, or network calls. Suppresses session touch writes.
    """
    # Guarantee no-write contract by suppressing post-request session touch write
    request.state.session_touch = None

    rate_limiter.check_rate_limit(f"broker_snapshot_latest:{current_user.id}", max_requests=60, window_seconds=60)

    snapshot_data = BrokerService.get_latest_snapshot(db=db, owner_id=current_user.id, environment=environment)
    if not snapshot_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No broker snapshot found. Please trigger a refresh.",
        )
    return BrokerSnapshotResponse(**snapshot_data)


@router.get("/readiness", response_model=BrokerReadinessResponse)
def get_broker_readiness(
    request: Request,
    environment: str = Query("LIVE", pattern="^(LIVE|SANDBOX)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Pure read-only evaluation of live broker readiness gates.
    Live order dispatch is strictly disabled (live_dispatch_enabled=False).
    Suppresses session touch writes.
    """
    # Guarantee no-write contract by suppressing post-request session touch write
    request.state.session_touch = None

    rate_limiter.check_rate_limit(f"broker_readiness:{current_user.id}", max_requests=60, window_seconds=60)
    data = BrokerService.evaluate_live_readiness(db=db, owner_id=current_user.id, environment=environment)
    return BrokerReadinessResponse(**data)
