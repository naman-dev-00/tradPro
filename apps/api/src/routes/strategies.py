import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from src.database import get_db
from src.models import Strategy, User
from src.schemas import StrategyBase, StrategyCreate, StrategyResponse
from src.validation import validate_strategy_rules
from src.auth.dependencies import get_current_user, require_roles, require_csrf

router = APIRouter(prefix="/strategies", tags=["strategies"])

@router.post("/validate")
def validate_strategy(payload: dict):
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

@router.post("", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
def create_strategy(
    payload: dict,
    current_user: User = Depends(require_roles("EDITOR", "ADMIN")),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db)
):
    # Validate payload
    validation_res = validate_strategy(payload)
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

    validation_res = validate_strategy(payload)
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
