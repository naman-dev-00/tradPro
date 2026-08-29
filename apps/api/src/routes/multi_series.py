from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session
from src.database import get_db
from src.models import Strategy
from src.engine.multi_series_models import MultiSeriesEvaluationResult, ensure_utc_datetime
from src.engine.manifest import get_dataset_manifest, DatasetManifestEntry
from src.engine.multi_series_evaluator import MultiSeriesEvaluator

router = APIRouter(prefix="/multi-series", tags=["multi-series"])

class MultiSeriesEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: Optional[str] = None
    strategy: Optional[Dict[str, Any]] = None
    reference_dataset_id: str
    subject_dataset_ids: List[str] = Field(..., min_length=1, max_length=20)
    eval_timestamp: datetime

    @field_validator("eval_timestamp", mode="after")
    @classmethod
    def validate_eval_timestamp(cls, v: datetime) -> datetime:
        return ensure_utc_datetime(v)


@router.get("/datasets", response_model=List[DatasetManifestEntry])
def get_multi_series_datasets():
    """Returns the safe synthetic dataset manifest in stable pre-defined order."""
    return get_dataset_manifest()


@router.post("/evaluate", response_model=MultiSeriesEvaluationResult)
def evaluate_multi_series_rules(
    req: MultiSeriesEvaluationRequest,
    db: Session = Depends(get_db),
):
    """
    Evaluates rules independently across multiple packaged synthetic subject datasets.
    Educational synthetic data only. Results are independent Boolean inspections.
    """
    # 1. Resolve Strategy Definition
    strategy_payload: Optional[Dict[str, Any]] = None
    if req.strategy_id:
        db_strat = db.query(Strategy).filter(Strategy.id == req.strategy_id).first()
        if not db_strat:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Saved strategy with ID '{req.strategy_id}' not found.",
            )
        strategy_payload = db_strat.payload
    elif req.strategy:
        strategy_payload = req.strategy
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Evaluation request must provide either 'strategy_id' or 'strategy'.",
        )

    # 2. Perform Multi-Series Rule Evaluation
    evaluator = MultiSeriesEvaluator()
    try:
        res = evaluator.evaluate_multi_series(
            strategy_payload=strategy_payload,
            reference_dataset_id=req.reference_dataset_id,
            subject_dataset_ids=req.subject_dataset_ids,
            eval_timestamp=req.eval_timestamp,
            strategy_id=req.strategy_id,
        )
        return res
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve),
        )
    except Exception as e:
        # Prevent stack trace or file path exposure
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Multi-series evaluation failed: {str(e)}",
        )
