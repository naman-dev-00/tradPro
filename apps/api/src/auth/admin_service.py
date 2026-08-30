from typing import List, Tuple, Optional
from sqlalchemy.orm import Session
from sqlalchemy import text
from fastapi import HTTPException, status
from src.models import User, Strategy, InspectionRun, LEGACY_PRINCIPAL_ID

def check_last_admin_protection(
    db: Session,
    target_user: User,
    new_role: Optional[str] = None,
    new_active_status: Optional[bool] = None,
) -> None:
    """
    Concurrency-safe check preventing the last active administrator from being demoted or deactivated.
    """
    if target_user.role != "ADMIN" or not target_user.is_active:
        return

    will_lose_admin = (new_role is not None and new_role != "ADMIN") or (new_active_status is False)
    if not will_lose_admin:
        return

    # Check database dialect for row locking
    is_postgres = db.bind and db.bind.dialect.name == "postgresql"
    if is_postgres:
        active_admin_count = db.execute(
            text("SELECT COUNT(*) FROM users WHERE role = 'ADMIN' AND is_active = True FOR UPDATE")
        ).scalar()
    else:
        active_admin_count = db.query(User).filter(
            User.role == "ADMIN",
            User.is_active.is_(True)
        ).count()

    if active_admin_count <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot deactivate or demote the last active administrator."
        )

def transfer_legacy_resources(
    db: Session,
    target_user_id: str,
    resource_type: str,
    resource_ids: List[str],
) -> Tuple[int, int]:
    """
    Transfers bounded batch of legacy-owned resources (max 100) to an active user.
    Returns: (transferred_count, rejected_count)
    """
    if not resource_ids or len(resource_ids) > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Resource IDs list must contain between 1 and 100 items."
        )

    if resource_type not in ("ALL", "STRATEGIES", "REPLAYS"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resource_type must be one of: 'ALL', 'STRATEGIES', 'REPLAYS'."
        )

    target_user = db.query(User).filter(User.id == target_user_id).first()
    if not target_user or not target_user.is_active or target_user.id == LEGACY_PRINCIPAL_ID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target user does not exist, is inactive, or is the legacy principal."
        )

    transferred = 0

    if resource_type in ("ALL", "STRATEGIES"):
        updated_strategies = db.query(Strategy).filter(
            Strategy.owner_id == LEGACY_PRINCIPAL_ID,
            Strategy.id.in_(resource_ids)
        ).update({Strategy.owner_id: target_user_id}, synchronize_session=False)
        transferred += updated_strategies

    if resource_type in ("ALL", "REPLAYS"):
        updated_replays = db.query(InspectionRun).filter(
            InspectionRun.owner_id == LEGACY_PRINCIPAL_ID,
            InspectionRun.id.in_(resource_ids)
        ).update({InspectionRun.owner_id: target_user_id}, synchronize_session=False)
        transferred += updated_replays

    db.commit()
    rejected = len(resource_ids) - transferred
    return transferred, rejected
