import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from src.database import get_db
from src.models import Strategy
from src.schemas import StrategyBase, StrategyCreate, StrategyResponse
from src.validation import validate_strategy_rules

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
def create_strategy(payload: dict, db: Session = Depends(get_db)):
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
        name=strategy_data.name,
        description=strategy_data.description,
        timeframe=strategy_data.timeframe,
        candidate_selection_mode=strategy_data.candidate_selection_mode,
        payload=payload
    )

    db.add(db_strategy)
    db.commit()
    db.refresh(db_strategy)

    # Return structured strategy
    return db_strategy

@router.get("", response_model=List[StrategyResponse])
def list_strategies(db: Session = Depends(get_db)):
    strategies = db.query(Strategy).all()
    return strategies

@router.get("/{id}", response_model=StrategyResponse)
def get_strategy(id: str, db: Session = Depends(get_db)):
    db_strategy = db.query(Strategy).filter(Strategy.id == id).first()
    if not db_strategy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy with ID '{id}' not found."
        )
    return db_strategy

@router.put("/{id}", response_model=StrategyResponse)
def update_strategy(id: str, payload: dict, db: Session = Depends(get_db)):
    db_strategy = db.query(Strategy).filter(Strategy.id == id).first()
    if not db_strategy:
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
