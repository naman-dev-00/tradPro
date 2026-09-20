import os
import datetime
import logging
from typing import List, Optional
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from src.database import get_db, get_read_only_db
from src.models import User, SubmissionOutbox
from src.schemas import (
    SandboxReadinessResponse,
    ProviderConnectionUpdateRequest,
    ProviderConnectionResponse,
    ProviderInstrumentMappingCreate,
    ProviderInstrumentMappingVerifyRequest,
    ProviderInstrumentMappingResponse,
    SubmissionOutboxResponse,
    ReconciliationResolutionRequest,
    ReconciliationRecordResponse,
)
from src.auth.dependencies import get_current_user, require_roles, require_csrf
from src.auth.rate_limiter import rate_limiter
from src.services.sandbox_service import (
    SandboxService,
    ResourceNotFoundError,
    PermissionDeniedError,
    InvalidOperationError,
    ConflictError,
)
from src.engine.paper.units import units_to_decimal

logger = logging.getLogger("tradepro.sandbox_routes")

router = APIRouter(prefix="/api/v1/sandbox", tags=["sandbox"])


# --- Readiness Endpoint ---

@router.get("/readiness", response_model=SandboxReadinessResponse)
def get_sandbox_readiness(
    runtime_id: str = Query(..., description="ID of the strategy runtime"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db),
):
    """
    Mandatory Correction 6:
    Runtime-specific readiness check.
    Cross-owner or non-existent runtime returns identical 404.
    Evaluates all 8 gates with zero external network calls.
    """
    rate_limiter.check_rate_limit(f"sandbox_readiness:{current_user.id}", max_requests=60, window_seconds=60)
    try:
        readiness_dict = SandboxService.get_runtime_readiness(db, runtime_id, current_user.id)
        return SandboxReadinessResponse(**readiness_dict)
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


# --- Connection Metadata ---

@router.get("/connection", response_model=ProviderConnectionResponse)
def get_sandbox_connection(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db),
):
    """
    Returns sanitized connection metadata for the single configured sandbox owner.
    Credential reference is kept server-side only and never exposed in the response.
    Others receive 404.
    Strictly read-only: performs zero DB mutations, insertions, flushes, commits, or network calls.
    If no connection has been configured, returns a safe NOT_CONFIGURED representation.
    """
    rate_limiter.check_rate_limit(f"sandbox_connection:{current_user.id}", max_requests=60, window_seconds=60)
    try:
        conn = SandboxService.get_connection_read_only(db, current_user.id)
        has_env_token = bool(os.environ.get("UPSTOX_SANDBOX_ACCESS_TOKEN", "").strip())

        if not conn:
            return ProviderConnectionResponse(
                provider="UPSTOX",
                environment="SANDBOX",
                credential_configured=has_env_token,
                credential_version="v1",
                readiness_status="NOT_CONFIGURED",
                last_successful_transmission_at=None,
            )

        return ProviderConnectionResponse(
            provider=conn.provider_name,
            environment=conn.environment,
            credential_configured=has_env_token,
            credential_version=conn.credential_version,
            readiness_status=conn.status,
            last_successful_transmission_at=conn.last_successful_transmission_at,
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/connection", response_model=ProviderConnectionResponse)
def update_sandbox_connection(
    payload: ProviderConnectionUpdateRequest,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """
    Registers or updates connection versioning metadata for the configured sandbox operator.
    Enforces:
    - Configured sandbox owner isolation
    - EDITOR or ADMIN roles
    - CSRF protection
    - Rate limiting
    - No token in request body (enforced via strict ConfigDict(extra='forbid'))
    - Credential reference kept strictly server-side
    """
    cfg_owner = os.environ.get("UPSTOX_SANDBOX_OWNER_ID")
    if not cfg_owner or current_user.id != cfg_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current user is not the configured sandbox operator.",
        )

    rate_limiter.check_rate_limit(f"sandbox_conn_update:{current_user.id}", max_requests=10, window_seconds=60)

    conn = SandboxService.get_or_create_connection(db, current_user.id)
    if payload.credential_version:
        conn.credential_version = payload.credential_version
        conn.updated_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()
        db.refresh(conn)

    has_env_token = bool(os.environ.get("UPSTOX_SANDBOX_ACCESS_TOKEN", "").strip())

    return ProviderConnectionResponse(
        provider=conn.provider_name,
        environment=conn.environment,
        credential_configured=has_env_token,
        credential_version=conn.credential_version,
        readiness_status=conn.status,
        last_successful_transmission_at=conn.last_successful_transmission_at,
    )


# --- Instrument Mappings ---

@router.get("/instruments", response_model=List[ProviderInstrumentMappingResponse])
def list_instrument_mappings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db),
):
    rate_limiter.check_rate_limit(f"sandbox_inst_list:{current_user.id}", max_requests=60, window_seconds=60)
    mappings = SandboxService.list_instrument_mappings(db, current_user.id)
    return [
        ProviderInstrumentMappingResponse(
            id=m.id,
            owner_id=m.owner_id,
            tradepro_instrument_id=m.tradepro_instrument_id,
            provider_instrument_token=m.provider_instrument_token,
            exchange=m.exchange,
            segment=m.segment,
            symbol=m.symbol,
            expiry_date=m.expiry_date,
            strike_price=units_to_decimal(m.strike_price_units, 2) if m.strike_price_units is not None else None,
            option_type=m.option_type,
            lot_size=m.lot_size_units,
            tick_size=units_to_decimal(m.tick_size_units, 2),
            freeze_quantity=m.freeze_quantity_units,
            verification_status=m.verification_status,
            verified_by=m.verified_by,
            verified_at=m.verified_at,
            verification_audit_json=m.verification_audit_json,
            mapping_version=m.mapping_version,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )
        for m in mappings
    ]


@router.post("/instruments", response_model=ProviderInstrumentMappingResponse, status_code=status.HTTP_201_CREATED)
def create_instrument_mapping(
    payload: ProviderInstrumentMappingCreate,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    rate_limiter.check_rate_limit(f"sandbox_inst_create:{current_user.id}", max_requests=30, window_seconds=60)
    m = SandboxService.create_instrument_mapping(
        db,
        owner_id=current_user.id,
        tradepro_instrument_id=payload.tradepro_instrument_id,
        provider_instrument_token=payload.provider_instrument_token,
        exchange=payload.exchange,
        segment=payload.segment,
        symbol=payload.symbol,
        expiry_date=payload.expiry_date,
        strike_price=payload.strike_price,
        option_type=payload.option_type,
        lot_size=payload.lot_size,
        tick_size=payload.tick_size,
        freeze_quantity=payload.freeze_quantity,
    )
    return ProviderInstrumentMappingResponse(
        id=m.id,
        owner_id=m.owner_id,
        tradepro_instrument_id=m.tradepro_instrument_id,
        provider_instrument_token=m.provider_instrument_token,
        exchange=m.exchange,
        segment=m.segment,
        symbol=m.symbol,
        expiry_date=m.expiry_date,
        strike_price=units_to_decimal(m.strike_price_units, 2) if m.strike_price_units is not None else None,
        option_type=m.option_type,
        lot_size=m.lot_size_units,
        tick_size=units_to_decimal(m.tick_size_units, 2),
        freeze_quantity=m.freeze_quantity_units,
        verification_status=m.verification_status,
        verified_by=m.verified_by,
        verified_at=m.verified_at,
        verification_audit_json=m.verification_audit_json,
        mapping_version=m.mapping_version,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


@router.post("/instruments/{id}/verify", response_model=ProviderInstrumentMappingResponse)
def verify_instrument_mapping(
    id: str,
    payload: ProviderInstrumentMappingVerifyRequest,
    current_user: User = Depends(require_roles("ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """
    Mandatory Correction 5:
    ADMIN may verify or reject a mapping belonging to the configured UPSTOX_SANDBOX_OWNER_ID.
    Every cross-owner administrative verification is fully audited.
    """
    rate_limiter.check_rate_limit(f"sandbox_inst_verify:{current_user.id}", max_requests=30, window_seconds=60)
    try:
        m = SandboxService.verify_instrument_mapping(
            db,
            mapping_id=id,
            actor_user=current_user,
            new_status=payload.status,
            reason=payload.reason,
        )
        return ProviderInstrumentMappingResponse(
            id=m.id,
            owner_id=m.owner_id,
            tradepro_instrument_id=m.tradepro_instrument_id,
            provider_instrument_token=m.provider_instrument_token,
            exchange=m.exchange,
            segment=m.segment,
            symbol=m.symbol,
            expiry_date=m.expiry_date,
            strike_price=units_to_decimal(m.strike_price_units, 2) if m.strike_price_units is not None else None,
            option_type=m.option_type,
            lot_size=m.lot_size_units,
            tick_size=units_to_decimal(m.tick_size_units, 2),
            freeze_quantity=m.freeze_quantity_units,
            verification_status=m.verification_status,
            verified_by=m.verified_by,
            verified_at=m.verified_at,
            verification_audit_json=m.verification_audit_json,
            mapping_version=m.mapping_version,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.post("/instruments/{id}/disable", response_model=ProviderInstrumentMappingResponse)
def disable_instrument_mapping(
    id: str,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    rate_limiter.check_rate_limit(f"sandbox_inst_disable:{current_user.id}", max_requests=30, window_seconds=60)
    try:
        m = SandboxService.disable_instrument_mapping(db, id, current_user)
        return ProviderInstrumentMappingResponse(
            id=m.id,
            owner_id=m.owner_id,
            tradepro_instrument_id=m.tradepro_instrument_id,
            provider_instrument_token=m.provider_instrument_token,
            exchange=m.exchange,
            segment=m.segment,
            symbol=m.symbol,
            expiry_date=m.expiry_date,
            strike_price=units_to_decimal(m.strike_price_units, 2) if m.strike_price_units is not None else None,
            option_type=m.option_type,
            lot_size=m.lot_size_units,
            tick_size=units_to_decimal(m.tick_size_units, 2),
            freeze_quantity=m.freeze_quantity_units,
            verification_status=m.verification_status,
            verified_by=m.verified_by,
            verified_at=m.verified_at,
            verification_audit_json=m.verification_audit_json,
            mapping_version=m.mapping_version,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


# --- Outbox Status ---

@router.get("/outbox", response_model=List[SubmissionOutboxResponse])
def list_outbox_entries(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db),
):
    rate_limiter.check_rate_limit(f"sandbox_outbox_list:{current_user.id}", max_requests=60, window_seconds=60)
    entries = SandboxService.list_outbox(db, current_user.id)
    return [
        SubmissionOutboxResponse(
            id=e.id,
            owner_id=e.owner_id,
            order_id=e.order_id,
            action_type=e.action_type,
            priority=e.priority,
            status=e.status,
            idempotency_key=e.idempotency_key,
            attempts=e.attempts,
            max_attempts=e.max_attempts,
            next_attempt_at=e.next_attempt_at,
            last_error_code=e.last_error_code,
            last_error_message=e.last_error_message,
            transmission_started_at=e.transmission_started_at,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )
        for e in entries
    ]


# --- Reconciliation ---

@router.get("/reconciliations", response_model=List[ReconciliationRecordResponse])
def list_reconciliation_records(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db),
):
    rate_limiter.check_rate_limit(f"sandbox_recon_list:{current_user.id}", max_requests=60, window_seconds=60)
    records = SandboxService.list_reconciliation_records(db, current_user.id)
    return [
        ReconciliationRecordResponse(
            id=r.id,
            owner_id=r.owner_id,
            order_id=r.order_id,
            outbox_id=r.outbox_id,
            status=r.status,
            resolution_type=r.resolution_type,
            resolved_by=r.resolved_by,
            provider_order_reference=r.provider_order_reference,
            notes=r.notes,
            resolved_at=r.resolved_at,
            created_at=r.created_at,
        )
        for r in records
    ]


@router.post("/reconciliations/{id}/resolve", response_model=ReconciliationRecordResponse)
def resolve_reconciliation(
    id: str,
    payload: ReconciliationResolutionRequest,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """
    Mandatory Corrections 8 & 9:
    Manual resolution endpoint for orders requiring human verification in Upstox web portal.
    Never accepts owner_id or resolved_by from client.
    """
    rate_limiter.check_rate_limit(f"sandbox_reconcile:{current_user.id}", max_requests=30, window_seconds=60)
    try:
        rec = SandboxService.resolve_reconciliation(
            db,
            actor_user=current_user,
            resolution_type=payload.resolution_type,
            provider_order_reference=payload.provider_order_reference,
            notes=payload.notes,
            outbox_id=payload.outbox_id,
            identifier=id,
        )
        return ReconciliationRecordResponse(
            id=rec.id,
            owner_id=rec.owner_id,
            order_id=rec.order_id,
            outbox_id=rec.outbox_id,
            status=rec.status,
            resolution_type=rec.resolution_type,
            resolved_by=rec.resolved_by,
            provider_order_reference=rec.provider_order_reference,
            notes=rec.notes,
            resolved_at=rec.resolved_at,
            created_at=rec.created_at,
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except InvalidOperationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
