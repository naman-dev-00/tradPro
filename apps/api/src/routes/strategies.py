import json
import uuid
from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from src.database import get_db
from src.models import Strategy, User
from src.schemas import StrategyBase, StrategyCreate, StrategyResponse
from src.validation import validate_strategy_rules
from src.auth.dependencies import get_current_user, require_roles, require_csrf
from src.auth.rate_limiter import rate_limiter

router = APIRouter(prefix="/strategies", tags=["strategies"])

MAX_STRATEGY_PAYLOAD_BYTES = 65536
MAX_STRATEGY_DEPTH = 16
MAX_STRATEGY_NODES = 256
MAX_STRATEGY_STRING_LEN = 1000
MAX_STRATEGY_ARRAY_LEN = 512


def _check_payload_complexity(data: Any, depth: int = 0, node_count: int = 0) -> int:
    if depth > MAX_STRATEGY_DEPTH:
        raise HTTPException(
            status_code=422,
            detail="Strategy nesting depth exceeds allowed limit (16).",
        )
    node_count += 1
    if node_count > MAX_STRATEGY_NODES:
        raise HTTPException(
            status_code=422,
            detail="Strategy complexity exceeds allowed node count limit (256).",
        )
    if isinstance(data, dict):
        for val in data.values():
            node_count = _check_payload_complexity(val, depth + 1, node_count)
    elif isinstance(data, (list, tuple)):
        if len(data) > MAX_STRATEGY_ARRAY_LEN:
            raise HTTPException(
                status_code=422,
                detail=f"Strategy collection exceeds allowed array limit ({MAX_STRATEGY_ARRAY_LEN}).",
            )
        for item in data:
            node_count = _check_payload_complexity(item, depth + 1, node_count)
    elif isinstance(data, str):
        if len(data) > MAX_STRATEGY_STRING_LEN:
            raise HTTPException(
                status_code=422,
                detail=f"Strategy string exceeds allowed length limit ({MAX_STRATEGY_STRING_LEN}).",
            )
    return node_count


def _validate_strategy_internal(payload: dict) -> dict:
    errors = []
    try:
        strategy = StrategyBase(**payload)
        custom_errors = validate_strategy_rules(strategy)
        errors.extend(custom_errors)
    except Exception as e:
        if hasattr(e, "errors") and callable(getattr(e, "errors")):
            for err in e.errors():
                loc_path = ".".join(str(l) for l in err["loc"])
                errors.append(f"{loc_path}: {err['msg']}")
        else:
            errors.append(f"Payload validation error: {str(e)}")

    return {
        "valid": len(errors) == 0,
        "errors": errors
    }


@router.post("/validate")
async def validate_strategy(
    request: Request,
    current_user: User = Depends(require_roles("VIEWER", "EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
):
    rate_limiter.check_rate_limit(f"strategy_validate:{current_user.id}", max_requests=60, window_seconds=60)

    # 1. Reject an oversized declared Content-Length
    cl_header = request.headers.get("content-length")
    if cl_header is not None:
        try:
            cl_val = int(cl_header)
            if cl_val > MAX_STRATEGY_PAYLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Strategy payload exceeds 64 KiB limit.",
                )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.")

    # 2. Independently bound the actual bytes read
    body_chunks = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > MAX_STRATEGY_PAYLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Strategy payload exceeds 64 KiB limit.",
            )
        body_chunks.append(chunk)

    body_bytes = b"".join(body_chunks)

    # 3. Parse JSON only after actual-byte check
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="Invalid JSON payload.",
        )

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422,
            detail="Strategy payload must be a JSON object.",
        )

    # 4. Complexity limits (depth <= 16, nodes <= 256, strings <= 1000, arrays <= 256)
    _check_payload_complexity(payload)

    # 5. Apply Pydantic validation manually after size/depth/node checks (extra="forbid")
    try:
        StrategyBase.model_validate(payload)
    except Exception as e:
        if hasattr(e, "errors") and callable(getattr(e, "errors")):
            for err in e.errors():
                if err.get("type") == "extra_forbidden":
                    raise HTTPException(
                        status_code=422,
                        detail=f"Extra field not permitted: {err.get('loc')}",
                    )

    # 6. Pure validation (no DB persistence, no ownership leakage)
    return _validate_strategy_internal(payload)


@router.post("", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
def create_strategy(
    payload: dict,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    # Validate payload
    validation_res = _validate_strategy_internal(payload)
    if not validation_res["valid"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Strategy validation failed", "errors": validation_res["errors"]}
        )

    try:
        strategy_data = StrategyBase(**payload)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Schema validation error: {str(e)}"
        )

    strategy_id = payload.get("id") or str(uuid.uuid4())

    # Check if id already exists
    existing = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Strategy with ID '{strategy_id}' already exists."
        )

    db_strategy = Strategy(
        id=strategy_id,
        owner_id=current_user.id,
        name=strategy_data.name,
        description=strategy_data.description,
        timeframe=strategy_data.timeframe,
        candidate_selection_mode=strategy_data.candidate_selection_mode,
        payload=payload
    )

    db.add(db_strategy)
    db.commit()
    db.refresh(db_strategy)

    return db_strategy

@router.get("", response_model=List[StrategyResponse])
def list_strategies(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Strictly return only strategies owned by current user
    strategies = db.query(Strategy).filter(
        Strategy.owner_id == current_user.id
    ).order_by(Strategy.created_at.desc()).all()
    return strategies

@router.get("/{id}", response_model=StrategyResponse)
def get_strategy(
    id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    db_strategy = db.query(Strategy).filter(Strategy.id == id).first()
    # Security requirement: Always return 404 for unowned resources to never leak existence
    if not db_strategy or db_strategy.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy with ID '{id}' not found."
        )
    return db_strategy

@router.put("/{id}", response_model=StrategyResponse)
def update_strategy(
    id: str,
    payload: dict,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    db_strategy = db.query(Strategy).filter(Strategy.id == id).first()
    # Security requirement: Always return 404 for unowned resources to never leak existence
    if not db_strategy or db_strategy.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy with ID '{id}' not found."
        )

    validation_res = _validate_strategy_internal(payload)
    if not validation_res["valid"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Strategy validation failed", "errors": validation_res["errors"]}
        )

    try:
        strategy_data = StrategyBase(**payload)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Schema validation error: {str(e)}"
        )

    db_strategy.name = strategy_data.name
    db_strategy.description = strategy_data.description
    db_strategy.timeframe = strategy_data.timeframe
    db_strategy.candidate_selection_mode = strategy_data.candidate_selection_mode
    db_strategy.payload = payload

    db.commit()
    db.refresh(db_strategy)
    return db_strategy
