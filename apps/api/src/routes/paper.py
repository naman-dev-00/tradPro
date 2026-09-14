import hashlib
import json
from typing import List, Optional, Any, Dict
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.orm import Session
from src.database import get_db, get_read_only_db
from src.models import (
    User,
    PaperAccount,
    StrategyActionPolicy,
    RiskPolicy,
    StrategyRuntime,
    Order,
    OrderEvent,
    Fill,
    PaperPosition,
    RuntimeEvent,
    ApiIdempotencyRecord,
)
from src.schemas import (
    PaperAccountCreate,
    PaperAccountResponse,
    AccountLedgerEntryResponse,
    StrategyActionPolicyCreate,
    StrategyActionPolicyResponse,
    RiskPolicyCreate,
    RiskPolicyResponse,
    StrategyRuntimeCreate,
    StrategyRuntimeResponse,
    StrategyRuntimeStepRequest,
    OrderResponse,
    OrderEventResponse,
    FillResponse,
    PaperPositionResponse,
    KillSwitchStatusResponse,
    KillSwitchEngageRequest,
    KillSwitchResetRequest,
)
from src.auth.dependencies import get_current_user, require_roles, require_csrf
from src.auth.rate_limiter import rate_limiter
from src.services.paper_service import PaperService, ResourceNotFoundError, ConflictError
from src.engine.paper.units import units_to_decimal

router = APIRouter(prefix="/api/v1/paper", tags=["paper"])

# --- Idempotency Helpers ---

def check_idempotency(db: Session, owner_id: str, idempotency_key: Optional[str], endpoint_tag: str, payload_dict: Any) -> Optional[dict]:
    """
    Checks for prior idempotency record.
    Returns cached response_body if request_hash matches.
    Raises 409 Conflict if same key is reused with a different request payload.
    """
    if not idempotency_key:
        return None
    canonical_json = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"), default=str)
    req_hash = hashlib.sha256(f"{endpoint_tag}:{canonical_json}".encode("utf-8")).hexdigest()

    existing = db.query(ApiIdempotencyRecord).filter(
        ApiIdempotencyRecord.owner_id == owner_id,
        ApiIdempotencyRecord.key == idempotency_key
    ).first()
    if existing:
        if existing.request_hash != req_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key reuse with different request payload."
            )
        return existing.response_body
    return None

def save_idempotency(db: Session, owner_id: str, idempotency_key: Optional[str], endpoint_tag: str, payload_dict: Any, response_status: int, response_body: Any) -> None:
    """
    Persists deterministic idempotency record upon successful completion of state-changing request.
    """
    if not idempotency_key:
        return
    canonical_json = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"), default=str)
    req_hash = hashlib.sha256(f"{endpoint_tag}:{canonical_json}".encode("utf-8")).hexdigest()

    record = ApiIdempotencyRecord(
        key=idempotency_key,
        owner_id=owner_id,
        request_hash=req_hash,
        response_status=response_status,
        response_body=response_body,
    )
    db.add(record)
    db.commit()

# --- Accounts ---

@router.get("/accounts", response_model=List[PaperAccountResponse])
def list_accounts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    accounts = PaperService.list_accounts(db, current_user.id)
    return [
        PaperAccountResponse(
            id=a.id,
            name=a.name,
            currency=a.currency,
            total_cash=units_to_decimal(a.total_cash_units, 2),
            reserved_cash=units_to_decimal(a.reserved_cash_units, 2),
            available_cash=units_to_decimal(a.total_cash_units - a.reserved_cash_units, 2),
            is_active=a.is_active,
            version=a.version,
            created_at=a.created_at,
            updated_at=a.updated_at,
        )
        for a in accounts
    ]

@router.post("/accounts", response_model=PaperAccountResponse, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: PaperAccountCreate,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_create_account:{current_user.id}", max_requests=30, window_seconds=60)
    cached = check_idempotency(db, current_user.id, idempotency_key, "create_account", payload.model_dump(mode="json"))
    if cached:
        return cached

    account = PaperService.create_account(
        db,
        owner_id=current_user.id,
        name=payload.name,
        initial_balance=payload.initial_balance,
        currency=payload.currency,
    )
    resp = PaperAccountResponse(
        id=account.id,
        name=account.name,
        currency=account.currency,
        total_cash=units_to_decimal(account.total_cash_units, 2),
        reserved_cash=units_to_decimal(account.reserved_cash_units, 2),
        available_cash=units_to_decimal(account.total_cash_units - account.reserved_cash_units, 2),
        is_active=account.is_active,
        version=account.version,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )
    save_idempotency(db, current_user.id, idempotency_key, "create_account", payload.model_dump(mode="json"), status.HTTP_201_CREATED, resp.model_dump(mode="json"))
    return resp

@router.get("/accounts/{id}", response_model=PaperAccountResponse)
def get_account(
    id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    a = PaperService.get_account(db, id, current_user.id)
    if not a:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Paper account '{id}' not found.")
    return PaperAccountResponse(
        id=a.id,
        name=a.name,
        currency=a.currency,
        total_cash=units_to_decimal(a.total_cash_units, 2),
        reserved_cash=units_to_decimal(a.reserved_cash_units, 2),
        available_cash=units_to_decimal(a.total_cash_units - a.reserved_cash_units, 2),
        is_active=a.is_active,
        version=a.version,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )

@router.get("/accounts/{id}/ledger", response_model=List[AccountLedgerEntryResponse])
def get_account_ledger(
    id: str,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    a = PaperService.get_account(db, id, current_user.id)
    if not a:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Paper account '{id}' not found.")
    entries = PaperService.get_ledger_entries(db, id, current_user.id, limit=limit, offset=offset)
    return [
        AccountLedgerEntryResponse(
            id=e.id,
            account_id=e.account_id,
            sequence_number=e.sequence_number,
            entry_type=e.entry_type,
            amount=units_to_decimal(e.amount_units, 2),
            balance_after=units_to_decimal(e.balance_after_units, 2),
            settled_cash_delta=units_to_decimal(e.settled_cash_delta_units, 2) if e.settled_cash_delta_units is not None else None,
            reserved_cash_delta=units_to_decimal(e.reserved_cash_delta_units, 2) if e.reserved_cash_delta_units is not None else None,
            settled_cash_after=units_to_decimal(e.settled_cash_after_units, 2) if e.settled_cash_after_units is not None else None,
            reserved_cash_after=units_to_decimal(e.reserved_cash_after_units, 2) if e.reserved_cash_after_units is not None else None,
            order_id=e.order_id,
            fill_id=e.fill_id,
            reason_code=e.reason_code,
            created_at=e.created_at,
        )
        for e in entries
    ]

# --- Action Policies ---

@router.get("/action-policies", response_model=List[StrategyActionPolicyResponse])
def list_action_policies(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    return PaperService.list_action_policies(db, current_user.id)

@router.post("/action-policies", response_model=StrategyActionPolicyResponse, status_code=status.HTTP_201_CREATED)
def create_action_policy(
    payload: StrategyActionPolicyCreate,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_create_policy:{current_user.id}", max_requests=30, window_seconds=60)
    cached = check_idempotency(db, current_user.id, idempotency_key, "create_action_policy", payload.model_dump(mode="json"))
    if cached:
        return cached

    try:
        policy = PaperService.create_action_policy(
            db,
            owner_id=current_user.id,
            strategy_id=payload.strategy_id,
            name=payload.name,
            payload=payload.model_dump(mode="json"),
        )
        resp = StrategyActionPolicyResponse.model_validate(policy)
        save_idempotency(db, current_user.id, idempotency_key, "create_action_policy", payload.model_dump(mode="json"), status.HTTP_201_CREATED, resp.model_dump(mode="json"))
        return resp
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.get("/action-policies/{id}", response_model=StrategyActionPolicyResponse)
def get_action_policy(
    id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    policy = PaperService.get_action_policy(db, id, current_user.id)
    if not policy:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Action policy '{id}' not found.")
    return policy

# --- Risk Policies ---

@router.get("/risk-policies", response_model=List[RiskPolicyResponse])
def list_risk_policies(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    return PaperService.list_risk_policies(db, current_user.id)

@router.post("/risk-policies", response_model=RiskPolicyResponse, status_code=status.HTTP_201_CREATED)
def create_risk_policy(
    payload: RiskPolicyCreate,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_create_risk:{current_user.id}", max_requests=30, window_seconds=60)
    cached = check_idempotency(db, current_user.id, idempotency_key, "create_risk_policy", payload.model_dump(mode="json"))
    if cached:
        return cached

    risk_dict = payload.model_dump(mode="json")
    risk_dict["max_quantity_per_order_units"] = int(payload.max_quantity_per_order)
    risk_dict["max_notional_per_order_units"] = int(payload.max_notional_per_order * 100)
    risk_dict["max_instrument_exposure_units"] = int(payload.max_instrument_exposure * 100)
    risk_dict["max_total_exposure_units"] = int(payload.max_total_exposure * 100)
    risk_dict["max_daily_realized_loss_units"] = int(payload.max_daily_realized_loss * 100)
    risk_dict["flat_fee_units"] = int(payload.flat_fee * 100)

    policy = PaperService.create_risk_policy(
        db,
        owner_id=current_user.id,
        name=payload.name,
        payload=risk_dict,
        is_default=payload.is_default,
    )
    resp = RiskPolicyResponse.model_validate(policy)
    save_idempotency(db, current_user.id, idempotency_key, "create_risk_policy", payload.model_dump(mode="json"), status.HTTP_201_CREATED, resp.model_dump(mode="json"))
    return resp

@router.get("/risk-policies/{id}", response_model=RiskPolicyResponse)
def get_risk_policy(
    id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    policy = PaperService.get_risk_policy(db, id, current_user.id)
    if not policy:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Risk policy '{id}' not found.")
    return policy

# --- Runtimes ---

@router.get("/runtimes", response_model=List[StrategyRuntimeResponse])
def list_runtimes(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    return db.query(StrategyRuntime).filter(StrategyRuntime.owner_id == current_user.id).order_by(StrategyRuntime.created_at.desc()).all()

@router.post("/runtimes", response_model=StrategyRuntimeResponse, status_code=status.HTTP_201_CREATED)
def create_runtime(
    payload: StrategyRuntimeCreate,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_create_runtime:{current_user.id}", max_requests=30, window_seconds=60)
    cached = check_idempotency(db, current_user.id, idempotency_key, "create_runtime", payload.model_dump(mode="json"))
    if cached:
        return cached

    try:
        runtime = PaperService.instantiate_runtime(
            db,
            owner_id=current_user.id,
            strategy_id=payload.strategy_id,
            account_id=payload.account_id,
            dataset_id=payload.dataset_id,
            action_policy_id=payload.action_policy_id,
            risk_policy_id=payload.risk_policy_id,
            timeframe=payload.timeframe,
            trading_mode=payload.trading_mode or "PAPER",
            instrument_mapping_id=payload.instrument_mapping_id,
        )
        resp = StrategyRuntimeResponse.model_validate(runtime)
        save_idempotency(db, current_user.id, idempotency_key, "create_runtime", payload.model_dump(mode="json"), status.HTTP_201_CREATED, resp.model_dump(mode="json"))
        return resp
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.get("/runtimes/{id}", response_model=StrategyRuntimeResponse)
def get_runtime(
    id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == id, StrategyRuntime.owner_id == current_user.id).first()
    if not runtime:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Runtime '{id}' not found.")
    return runtime

@router.post("/runtimes/{id}/validate")
def validate_runtime(
    id: str,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_runtime_control:{current_user.id}", max_requests=60, window_seconds=60)
    runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == id, StrategyRuntime.owner_id == current_user.id).first()
    if not runtime:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Runtime '{id}' not found.")
    try:
        return PaperService.validate_runtime(db, id, current_user.id)
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.post("/runtimes/{id}/start", response_model=StrategyRuntimeResponse)
def start_runtime(
    id: str,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_runtime_control:{current_user.id}", max_requests=60, window_seconds=60)
    runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == id, StrategyRuntime.owner_id == current_user.id).first()
    if not runtime:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Runtime '{id}' not found.")
    try:
        return PaperService.start_runtime(db, id, current_user.id)
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.post("/runtimes/{id}/pause", response_model=StrategyRuntimeResponse)
def pause_runtime(
    id: str,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_runtime_control:{current_user.id}", max_requests=60, window_seconds=60)
    runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == id, StrategyRuntime.owner_id == current_user.id).first()
    if not runtime:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Runtime '{id}' not found.")
    try:
        return PaperService.pause_runtime(db, id, current_user.id)
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.post("/runtimes/{id}/resume", response_model=StrategyRuntimeResponse)
def resume_runtime(
    id: str,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_runtime_control:{current_user.id}", max_requests=60, window_seconds=60)
    runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == id, StrategyRuntime.owner_id == current_user.id).first()
    if not runtime:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Runtime '{id}' not found.")
    try:
        return PaperService.resume_runtime(db, id, current_user.id)
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.post("/runtimes/{id}/stop", response_model=StrategyRuntimeResponse)
def stop_runtime(
    id: str,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_runtime_control:{current_user.id}", max_requests=60, window_seconds=60)
    runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == id, StrategyRuntime.owner_id == current_user.id).first()
    if not runtime:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Runtime '{id}' not found.")
    try:
        return PaperService.stop_runtime(db, id, current_user.id)
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.post("/runtimes/{id}/step")
def step_runtime(
    id: str,
    payload: StrategyRuntimeStepRequest,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_runtime_control:{current_user.id}", max_requests=60, window_seconds=60)
    runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == id, StrategyRuntime.owner_id == current_user.id).first()
    if not runtime:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Runtime '{id}' not found.")
    try:
        return PaperService.step_runtime(db, id, current_user.id, step_count=payload.step_count)
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.get("/runtimes/{id}/events", response_model=List[OrderEventResponse])
def get_runtime_events(
    id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    runtime = db.query(StrategyRuntime).filter(StrategyRuntime.id == id, StrategyRuntime.owner_id == current_user.id).first()
    if not runtime:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Runtime '{id}' not found.")
    return db.query(RuntimeEvent).filter(RuntimeEvent.runtime_id == id).order_by(RuntimeEvent.sequence_number.asc()).all()

# --- Orders, Fills, Positions ---

@router.get("/orders", response_model=List[OrderResponse])
def list_orders(
    runtime_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    q = db.query(Order).filter(Order.owner_id == current_user.id)
    if runtime_id:
        q = q.filter(Order.runtime_id == runtime_id)
    orders = q.order_by(Order.created_at.desc()).all()

    return [
        OrderResponse(
            id=o.id,
            runtime_id=o.runtime_id,
            intent_id=o.intent_id,
            account_id=o.account_id,
            order_sequence_number=o.order_sequence_number,
            instrument_id=o.instrument_id,
            side=o.side,
            order_type=o.order_type,
            quantity=units_to_decimal(o.quantity_units, 0),
            limit_price=units_to_decimal(o.limit_price_units, 2) if o.limit_price_units else None,
            filled_quantity=units_to_decimal(o.filled_quantity_units, 0),
            status=o.status,
            version=o.version,
            created_at=o.created_at,
            updated_at=o.updated_at,
        )
        for o in orders
    ]

@router.get("/orders/{id}", response_model=OrderResponse)
def get_order(
    id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    o = db.query(Order).filter(Order.id == id, Order.owner_id == current_user.id).first()
    if not o:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Order '{id}' not found.")

    events = db.query(OrderEvent).filter(OrderEvent.order_id == o.id).order_by(OrderEvent.sequence_number.asc()).all()
    event_responses = [
        OrderEventResponse(
            id=e.id,
            sequence_number=e.sequence_number,
            previous_status=e.previous_status,
            new_status=e.new_status,
            actor=e.actor,
            reason_code=e.reason_code,
            metadata_json=e.metadata_json,
            created_at=e.created_at,
        )
        for e in events
    ]

    return OrderResponse(
        id=o.id,
        runtime_id=o.runtime_id,
        intent_id=o.intent_id,
        account_id=o.account_id,
        order_sequence_number=o.order_sequence_number,
        instrument_id=o.instrument_id,
        side=o.side,
        order_type=o.order_type,
        quantity=units_to_decimal(o.quantity_units, 0),
        limit_price=units_to_decimal(o.limit_price_units, 2) if o.limit_price_units else None,
        filled_quantity=units_to_decimal(o.filled_quantity_units, 0),
        status=o.status,
        version=o.version,
        events=event_responses,
        created_at=o.created_at,
        updated_at=o.updated_at,
    )

@router.post("/orders/{id}/cancel", response_model=OrderResponse)
def cancel_order(
    id: str,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_cancel_order:{current_user.id}", max_requests=60, window_seconds=60)
    order = db.query(Order).filter(Order.id == id, Order.owner_id == current_user.id).first()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Order '{id}' not found.")
    try:
        o = PaperService.cancel_order(db, id, current_user.id, reason="USER_CANCELLED")
        return OrderResponse(
            id=o.id,
            runtime_id=o.runtime_id,
            intent_id=o.intent_id,
            account_id=o.account_id,
            order_sequence_number=o.order_sequence_number,
            instrument_id=o.instrument_id,
            side=o.side,
            order_type=o.order_type,
            quantity=units_to_decimal(o.quantity_units, 0),
            limit_price=units_to_decimal(o.limit_price_units, 2) if o.limit_price_units else None,
            filled_quantity=units_to_decimal(o.filled_quantity_units, 0),
            status=o.status,
            version=o.version,
            created_at=o.created_at,
            updated_at=o.updated_at,
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.get("/fills", response_model=List[FillResponse])
def list_fills(
    account_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    q = db.query(Fill).join(PaperAccount, Fill.account_id == PaperAccount.id).filter(PaperAccount.owner_id == current_user.id)
    if account_id:
        q = q.filter(Fill.account_id == account_id)
    fills = q.order_by(Fill.created_at.desc()).all()

    return [
        FillResponse(
            id=f.id,
            order_id=f.order_id,
            account_id=f.account_id,
            instrument_id=f.instrument_id,
            side=f.side,
            quantity=units_to_decimal(f.quantity_units, 0),
            price=units_to_decimal(f.price_units, 2),
            fee=units_to_decimal(f.fee_units, 2),
            candle_timestamp=f.candle_timestamp,
            created_at=f.created_at,
        )
        for f in fills
    ]

@router.get("/positions", response_model=List[PaperPositionResponse])
def list_positions(
    account_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    q = db.query(PaperPosition).filter(PaperPosition.owner_id == current_user.id)
    if account_id:
        q = q.filter(PaperPosition.account_id == account_id)
    positions = q.all()

    return [
        PaperPositionResponse(
            id=p.id,
            account_id=p.account_id,
            instrument_id=p.instrument_id,
            net_quantity=units_to_decimal(p.net_quantity_units, 0),
            average_entry_price=units_to_decimal(p.average_entry_price_units, 2),
            cost_basis=units_to_decimal(p.cost_basis_units, 2),
            gross_realized_pnl=units_to_decimal(p.gross_realized_pnl_units, 2),
            total_fees=units_to_decimal(p.total_fees_units, 2),
            net_realized_pnl=units_to_decimal(p.net_realized_pnl_units, 2),
            last_mark_price=units_to_decimal(p.last_mark_price_units, 2),
            unrealized_pnl=units_to_decimal(p.unrealized_pnl_units, 2),
            updated_at=p.updated_at,
        )
        for p in positions
    ]

# --- Kill Switches ---

@router.get("/kill-switch", response_model=KillSwitchStatusResponse)
def get_kill_switch(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_read_only_db)
):
    status_dict = PaperService.get_kill_switch_status(db, current_user.id)
    return KillSwitchStatusResponse(**status_dict)

@router.post("/kill-switch")
def engage_kill_switch(
    payload: KillSwitchEngageRequest,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_kill_switch:{current_user.id}", max_requests=30, window_seconds=60)
    if payload.scope == "GLOBAL" and current_user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only ADMIN can engage GLOBAL kill switch.")

    target_user_id = current_user.id if payload.scope == "USER" else None
    ks = PaperService.engage_kill_switch(
        db,
        scope=payload.scope,
        user_id=target_user_id,
        actor_id=current_user.id,
        reason=payload.reason,
    )
    return {"status": "ENGAGED", "scope": ks.scope, "reason": ks.reason}

@router.post("/kill-switch/reset")
def reset_kill_switch(
    payload: KillSwitchResetRequest,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    rate_limiter.check_rate_limit(f"paper_kill_switch:{current_user.id}", max_requests=30, window_seconds=60)
    if payload.scope == "GLOBAL" and current_user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only ADMIN can reset GLOBAL kill switch.")

    target_user_id = current_user.id if payload.scope == "USER" else None
    try:
        ks = PaperService.reset_kill_switch(
            db,
            scope=payload.scope,
            user_id=target_user_id,
            actor_id=current_user.id,
            reason=payload.reason,
        )
        return {"status": "RESET", "scope": payload.scope, "reason": payload.reason}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
