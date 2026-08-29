import datetime
from typing import List, Optional, Any
from pydantic import BaseModel, Field, ConfigDict

class IndicatorParams(BaseModel):
    period: Optional[int] = Field(None, ge=1)
    source: Optional[str] = None
    level: Optional[str] = None

class IndicatorExpression(BaseModel):
    indicator: str
    symbol: Optional[str] = None
    params: Optional[IndicatorParams] = None

class ComparisonValue(BaseModel):
    type: str
    value: Optional[float] = None
    range: Optional[List[float]] = None
    indicator: Optional[IndicatorExpression] = None

class ConditionNode(BaseModel):
    type: str
    conditions: Optional[List['ConditionNode']] = None
    lhs: Optional[IndicatorExpression] = None
    operator: Optional[str] = None
    rhs: Optional[ComparisonValue] = None

ConditionNode.model_rebuild()

class RiskConfiguration(BaseModel):
    max_position_size: float = Field(..., ge=0)
    stop_loss_pct: float = Field(..., ge=0)
    take_profit_pct: float = Field(..., ge=0)
    validity_window: int = Field(..., ge=1)

class PaperTradeAction(BaseModel):
    type: str
    risk_config: RiskConfiguration

class StrategyBase(BaseModel):
    name: str = Field(..., min_length=1)
    description: Optional[str] = None
    timeframe: str
    candidate_selection_mode: str = "FIRST_ELIGIBLE"
    global_conditions: Optional[ConditionNode] = None
    candidate_conditions: Optional[ConditionNode] = None
    action: PaperTradeAction

class StrategyCreate(StrategyBase):
    id: Optional[str] = None

class StrategyResponse(StrategyBase):
    id: str
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)
DefinitionRef = Any
