from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from src.database import get_db
from src.models import User, LEGACY_PRINCIPAL_ID
from src.schemas import (
    UserResponse,
    UserRoleUpdate,
    UserStatusUpdate,
    LegacyTransferRequest,
    LegacyTransferResponse,
)
from src.auth.dependencies import require_roles, require_csrf
from src.auth.session import revoke_all_user_sessions
from src.auth.admin_service import check_last_admin_protection, transfer_legacy_resources
from src.auth.rate_limiter import rate_limiter, get_client_ip

router = APIRouter(prefix="/api/v1/admin", tags=["User Administration"])

@router.get("/users", response_model=List[UserResponse])
def list_users(
    request: Request,
    include_legacy: bool = False,
    current_admin: User = Depends(require_roles("ADMIN")),
    db: Session = Depends(get_db)
):
    ip = get_client_ip(request)
    rate_limiter.check_rate_limit(f"admin_users:{ip}", max_requests=30, window_seconds=60)

    query = db.query(User)
    if not include_legacy:
        query = query.filter(User.id != LEGACY_PRINCIPAL_ID)
    return query.order_by(User.created_at.asc()).all()

@router.patch("/users/{user_id}/role", response_model=UserResponse)
def update_user_role(
    user_id: str,
    req: UserRoleUpdate,
    request: Request,
    current_admin: User = Depends(require_roles("ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    ip = get_client_ip(request)
    rate_limiter.check_rate_limit(f"admin_role_change:{ip}", max_requests=20, window_seconds=60)

    if user_id == LEGACY_PRINCIPAL_ID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot modify role of the legacy system principal."
        )

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID '{user_id}' not found."
        )

    # Concurrency-safe last administrator protection
    check_last_admin_protection(db, target_user, new_role=req.role)

    target_user.role = req.role
    db.commit()
    db.refresh(target_user)

    # Security requirement: Revoke all active sessions after privilege/role change
    revoke_all_user_sessions(db, target_user.id)

    return target_user

@router.patch("/users/{user_id}/status", response_model=UserResponse)
def update_user_status(
    user_id: str,
    req: UserStatusUpdate,
    request: Request,
    current_admin: User = Depends(require_roles("ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    ip = get_client_ip(request)
    rate_limiter.check_rate_limit(f"admin_status_change:{ip}", max_requests=20, window_seconds=60)

    if user_id == LEGACY_PRINCIPAL_ID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot modify status of the legacy system principal."
        )

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID '{user_id}' not found."
        )

    # Concurrency-safe last administrator protection
    check_last_admin_protection(db, target_user, new_active_status=req.is_active)

    target_user.is_active = req.is_active
    db.commit()
    db.refresh(target_user)

    # If user is deactivated, revoke all active sessions immediately
    if not req.is_active:
        revoke_all_user_sessions(db, target_user.id)

    return target_user

@router.post("/transfers/legacy", response_model=LegacyTransferResponse)
def transfer_legacy(
    transfer_req: LegacyTransferRequest,
    request: Request,
    current_admin: User = Depends(require_roles("ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    ip = get_client_ip(request)
    rate_limiter.check_rate_limit(f"admin_transfer:{ip}", max_requests=10, window_seconds=60)

    transferred, rejected = transfer_legacy_resources(
        db=db,
        target_user_id=transfer_req.target_user_id,
        resource_type=transfer_req.resource_type,
        resource_ids=transfer_req.resource_ids
    )

    return LegacyTransferResponse(
        transferred_count=transferred,
        rejected_count=rejected
    )
