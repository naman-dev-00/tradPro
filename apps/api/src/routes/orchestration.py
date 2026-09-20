"""API routes for strategy orchestration configuration, activation, and lifecycle management."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from src.auth.dependencies import get_current_user, require_csrf, require_roles
from src.auth.rate_limiter import rate_limiter
from src.database import get_db, get_read_only_db
from src.models import User
from src.schemas import (
    OrchestrationActivationRequest,
    OrchestrationConfigCreateRequest,
    OrchestrationConfigResponse,
    OrchestrationLifecycleResponse,
    OrchestrationReadinessResponse,
)
from src.services.orchestration_service import (
    ConflictError,
    OrchestrationService,
    PermissionDeniedError,
    ResourceNotFoundError,
)

logger = logging.getLogger("tradepro.orchestration_routes")

router = APIRouter(prefix="/api/v1/orchestration", tags=["orchestration"])


@router.post("/configs", response_model=OrchestrationConfigResponse, status_code=status.HTTP_201_CREATED)
def create_orchestration_config(
    payload: OrchestrationConfigCreateRequest,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """Create an orchestration configuration with user-submitted structured consent."""
    rate_limiter.check_rate_limit(f"orch_config_create:{current_user.id}", max_requests=30, window_seconds=60)
    try:
        config = OrchestrationService.create_orchestration_config(
            db=db,
            owner_id=current_user.id,
            payload=payload,
            actor_id=current_user.id,
        )
        return OrchestrationConfigResponse(
            id=config.id,
            runtime_id=config.runtime_id,
            source_type=config.source_type,
            source_namespace=config.source_namespace,
            execution_policy=config.execution_policy,
            snapshot_fingerprint=config.snapshot_fingerprint,
            consent_fingerprint=config.consent_fingerprint,
            consent_policy_version=config.consent_policy_version,
            consent_at=config.consent_at,
            timeframe=config.timeframe,
            replay_open_at=config.replay_open_at,
            replay_close_at=config.replay_close_at,
            checkpoint_close_at=config.checkpoint_close_at,
            fencing_generation=config.fencing_generation,
            retry_count=config.retry_count,
            created_at=config.created_at,
            updated_at=config.updated_at,
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except PermissionDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/configs/{runtime_id}", response_model=OrchestrationConfigResponse)
def get_orchestration_config(
    runtime_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db),
):
    """Read-only retrieval of orchestration configuration for an owned runtime."""
    rate_limiter.check_rate_limit(f"orch_config_get:{current_user.id}", max_requests=60, window_seconds=60)
    try:
        config = OrchestrationService.get_orchestration_config(db, runtime_id, current_user.id)
        return OrchestrationConfigResponse(
            id=config.id,
            runtime_id=config.runtime_id,
            source_type=config.source_type,
            source_namespace=config.source_namespace,
            execution_policy=config.execution_policy,
            snapshot_fingerprint=config.snapshot_fingerprint,
            consent_fingerprint=config.consent_fingerprint,
            consent_policy_version=config.consent_policy_version,
            consent_at=config.consent_at,
            timeframe=config.timeframe,
            replay_open_at=config.replay_open_at,
            replay_close_at=config.replay_close_at,
            checkpoint_close_at=config.checkpoint_close_at,
            fencing_generation=config.fencing_generation,
            retry_count=config.retry_count,
            created_at=config.created_at,
            updated_at=config.updated_at,
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.get("/runtimes/{runtime_id}/readiness", response_model=OrchestrationReadinessResponse)
def get_orchestration_readiness(
    runtime_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db),
):
    """Read-only evaluation of activation readiness gates for an owned runtime."""
    rate_limiter.check_rate_limit(f"orch_readiness:{current_user.id}", max_requests=60, window_seconds=60)
    try:
        readiness = OrchestrationService.evaluate_activation_readiness(db, runtime_id, current_user.id)
        return OrchestrationReadinessResponse(**readiness)
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/runtimes/{runtime_id}/activate", response_model=OrchestrationLifecycleResponse)
def activate_orchestration(
    runtime_id: str,
    payload: OrchestrationActivationRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """Activate orchestration for an eligible runtime."""
    rate_limiter.check_rate_limit(f"orch_activate:{current_user.id}", max_requests=30, window_seconds=60)
    try:
        res = OrchestrationService.activate_orchestration(
            db=db,
            runtime_id=runtime_id,
            owner_id=current_user.id,
            payload=payload,
            actor_id=current_user.id,
            idempotency_key=idempotency_key,
        )
        return OrchestrationLifecycleResponse(**res)
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except PermissionDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/runtimes/{runtime_id}/pause", response_model=OrchestrationLifecycleResponse)
def pause_orchestration(
    runtime_id: str,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """Pause orchestration evaluations."""
    rate_limiter.check_rate_limit(f"orch_pause:{current_user.id}", max_requests=30, window_seconds=60)
    try:
        res = OrchestrationService.pause_orchestration(
            db=db,
            runtime_id=runtime_id,
            owner_id=current_user.id,
            actor_id=current_user.id,
        )
        return OrchestrationLifecycleResponse(**res)
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except PermissionDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.post("/runtimes/{runtime_id}/resume", response_model=OrchestrationLifecycleResponse)
def resume_orchestration(
    runtime_id: str,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """Resume orchestration, revalidating all activation prerequisites."""
    rate_limiter.check_rate_limit(f"orch_resume:{current_user.id}", max_requests=30, window_seconds=60)
    try:
        res = OrchestrationService.resume_orchestration(
            db=db,
            runtime_id=runtime_id,
            owner_id=current_user.id,
            actor_id=current_user.id,
        )
        return OrchestrationLifecycleResponse(**res)
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except PermissionDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/runtimes/{runtime_id}/stop", response_model=OrchestrationLifecycleResponse)
def stop_orchestration(
    runtime_id: str,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """Permanently stop orchestration. Preserves orders, outbox, and audit history."""
    rate_limiter.check_rate_limit(f"orch_stop:{current_user.id}", max_requests=30, window_seconds=60)
    try:
        res = OrchestrationService.stop_orchestration(
            db=db,
            runtime_id=runtime_id,
            owner_id=current_user.id,
            actor_id=current_user.id,
        )
        return OrchestrationLifecycleResponse(**res)
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except PermissionDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
