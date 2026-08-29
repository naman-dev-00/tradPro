import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from src.database import get_db
from src.models import Strategy
from src.schemas import CandleInput
from src.engine.models import Candle
from src.engine.evaluator import RuleEvaluator
from src.engine.rule_models import RuleEvaluationResult
from src.routes.indicators import PACKAGED_DATASETS, load_dataset_from_fixture

logger = logging.getLogger("tradepro.routes.rules")

rules_router = APIRouter(prefix="/rules", tags=["rules"])

class RuleEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: Optional[str] = Field(None, description="Optional ID of saved strategy in DB")
    strategy: Optional[Dict[str, Any]] = Field(None, description="Inline strategy definition JSON payload")

    reference_dataset_id: Optional[str] = Field(None, description="Whitelisted ID of synthetic reference dataset fixture")
    reference_candles: Optional[List[CandleInput]] = Field(None, description="Raw reference OHLCV candles")

    subject_dataset_id: Optional[str] = Field(None, description="Whitelisted ID of synthetic subject dataset fixture")
    subject_candles: Optional[List[CandleInput]] = Field(None, description="Raw subject OHLCV candles")

    eval_timestamp: Optional[datetime] = Field(None, description="Optional UTC evaluation timestamp")

class OperatorParamMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str
    default_tolerance: Optional[float] = None
    requires_range: bool = False
    requires_previous_candle: bool = False

class OperatorMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str
    symbol: str
    details: OperatorParamMetadata

SUPPORTED_OPERATORS_METADATA = [
    {
        "name": "GREATER_THAN",
        "symbol": ">",
        "description": "Evaluates TRUE if left operand is strictly greater than right operand.",
        "details": {
            "name": "GREATER_THAN",
            "description": "left > right",
            "requires_range": False,
            "requires_previous_candle": False,
        },
    },
    {
        "name": "LESS_THAN",
        "symbol": "<",
        "description": "Evaluates TRUE if left operand is strictly less than right operand.",
        "details": {
            "name": "LESS_THAN",
            "description": "left < right",
            "requires_range": False,
            "requires_previous_candle": False,
        },
    },
    {
        "name": "GREATER_THAN_OR_EQUAL",
        "symbol": ">=",
        "description": "Evaluates TRUE if left operand is greater than or equal to right operand.",
        "details": {
            "name": "GREATER_THAN_OR_EQUAL",
            "description": "left >= right",
            "requires_range": False,
            "requires_previous_candle": False,
        },
    },
    {
        "name": "LESS_THAN_OR_EQUAL",
        "symbol": "<=",
        "description": "Evaluates TRUE if left operand is less than or equal to right operand.",
        "details": {
            "name": "LESS_THAN_OR_EQUAL",
            "description": "left <= right",
            "requires_range": False,
            "requires_previous_candle": False,
        },
    },
    {
        "name": "EQUALS",
        "symbol": "==",
        "description": "Evaluates TRUE if left and right operands are equal within tolerance.",
        "details": {
            "name": "EQUALS",
            "description": "abs(left - right) <= tolerance",
            "default_tolerance": 1e-6,
            "requires_range": False,
            "requires_previous_candle": False,
        },
    },
    {
        "name": "BETWEEN",
        "symbol": "in range",
        "description": "Evaluates TRUE if left operand is inclusively within range [low, high].",
        "details": {
            "name": "BETWEEN",
            "description": "low <= left <= high",
            "requires_range": True,
            "requires_previous_candle": False,
        },
    },
    {
        "name": "CROSSES_ABOVE",
        "symbol": "crossed above",
        "description": "Evaluates TRUE if left crossed from at-or-below right on previous candle to strictly above right on current candle.",
        "details": {
            "name": "CROSSES_ABOVE",
            "description": "prev_left <= prev_right AND curr_left > curr_right",
            "requires_range": False,
            "requires_previous_candle": True,
        },
    },
    {
        "name": "CROSSES_BELOW",
        "symbol": "crossed below",
        "description": "Evaluates TRUE if left crossed from at-or-above right on previous candle to strictly below right on current candle.",
        "details": {
            "name": "CROSSES_BELOW",
            "description": "prev_left >= prev_right AND curr_left < curr_right",
            "requires_range": False,
            "requires_previous_candle": True,
        },
    },
    {
        "name": "TOUCHES",
        "symbol": "touches",
        "description": "Evaluates TRUE if distance between left operand and right target is within explicit tolerance.",
        "details": {
            "name": "TOUCHES",
            "description": "abs(left - target) <= tolerance",
            "default_tolerance": 1e-4,
            "requires_range": False,
            "requires_previous_candle": False,
        },
    },
]

@rules_router.get("/operators", status_code=status.HTTP_200_OK)
def get_supported_operators():
    return {"operators": SUPPORTED_OPERATORS_METADATA}

@rules_router.post("/evaluate", response_model=RuleEvaluationResult, status_code=status.HTTP_200_OK)
def evaluate_rules(req: RuleEvaluationRequest, db: Session = Depends(get_db)):
    # 1. Resolve Strategy Payload
    strategy_payload: Optional[Dict[str, Any]] = None
    if req.strategy_id:
        db_strategy = db.query(Strategy).filter(Strategy.id == req.strategy_id).first()
        if not db_strategy:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Saved strategy with ID '{req.strategy_id}' not found.",
            )
        strategy_payload = db_strategy.payload
        strategy_payload["id"] = db_strategy.id
    elif req.strategy:
        strategy_payload = req.strategy
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Evaluation request must provide either 'strategy_id' or inline 'strategy' payload.",
        )

    # 2. Resolve Reference Candles
    ref_candles: List[Candle] = []
    if req.reference_dataset_id:
        if req.reference_dataset_id not in PACKAGED_DATASETS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown reference dataset ID '{req.reference_dataset_id}'. Must be one of {sorted(PACKAGED_DATASETS.keys())}",
            )
        filename = PACKAGED_DATASETS[req.reference_dataset_id]["filename"]
        ref_candles = load_dataset_from_fixture(filename)
    elif req.reference_candles is not None:
        ref_candles = [
            Candle(
                timestamp=c.timestamp,
                instrument_id=c.instrument_id,
                timeframe=c.timeframe,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                is_closed=c.is_closed,
            )
            for c in req.reference_candles
        ]
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Evaluation request must provide either 'reference_dataset_id' or 'reference_candles'.",
        )

    # 3. Resolve Subject Candles
    subj_candles: Optional[List[Candle]] = None
    if req.subject_dataset_id:
        if req.subject_dataset_id not in PACKAGED_DATASETS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown subject dataset ID '{req.subject_dataset_id}'. Must be one of {sorted(PACKAGED_DATASETS.keys())}",
            )
        filename = PACKAGED_DATASETS[req.subject_dataset_id]["filename"]
        subj_candles = load_dataset_from_fixture(filename)
    elif req.subject_candles is not None:
        subj_candles = [
            Candle(
                timestamp=c.timestamp,
                instrument_id=c.instrument_id,
                timeframe=c.timeframe,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                is_closed=c.is_closed,
            )
            for c in req.subject_candles
        ]

    # 4. Trigger Rule Evaluator
    evaluator = RuleEvaluator(max_depth=10, max_total_nodes=200, max_candles=5000)
    try:
        result = evaluator.evaluate_strategy_rules(
            strategy_payload=strategy_payload,
            reference_candles=ref_candles,
            subject_candles=subj_candles,
            eval_timestamp=req.eval_timestamp,
        )
        return result
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve),
        )
    except Exception as e:
        logger.error(f"Rule evaluation failure: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Rule evaluation failed: {str(e)}",
        )
