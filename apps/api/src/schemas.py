import datetime
from typing import List, Optional, Any, Literal
from pydantic import BaseModel, Field, ConfigDict

# --- User & Auth Schemas ---

class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    role: Literal["VIEWER", "EDITOR", "ADMIN"]
    is_active: bool
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)

class UserLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username_or_email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)

class CSRFTokenResponse(BaseModel):
    csrf_token: str

class UserRoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["VIEWER", "EDITOR", "ADMIN"]

class UserStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_active: bool

class LegacyTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_user_id: str
    resource_type: Literal["ALL", "STRATEGIES", "REPLAYS"]
    resource_ids: List[str] = Field(..., min_length=1, max_length=100)

class LegacyTransferResponse(BaseModel):
    transferred_count: int
    rejected_count: int

# --- Strategy & Indicator Schemas ---

class CandleInput(BaseModel):
    timestamp: datetime.datetime
    instrument_id: str
    timeframe: str
    open: float = Field(..., gt=0)
    high: float = Field(..., gt=0)
    low: float = Field(..., gt=0)
    close: float = Field(..., gt=0)
    volume: float = Field(..., ge=0)
    is_closed: bool = True

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
    id: Optional[str] = None
    conditions: Optional[List['ConditionNode']] = None
    lhs: Optional[IndicatorExpression] = None
    operator: Optional[str] = None
    rhs: Optional[ComparisonValue] = None
    tolerance: Optional[float] = Field(None, ge=0)

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

from src.engine.replay_comparison_models import (
    DatasetChecksumResult,
    ReplayVerificationResult,
    ReplayComparisonRequest,
    ReplayStatusDifference,
    ReplayComparisonResult,
)
from src.engine.dataset_quality_models import (
    DatasetQualityStatus,
    DatasetIssueSeverity,
    DatasetIssueCode,
    DatasetQualityIssue,
    DatasetQualitySummary,
    DatasetProvenance,
    DatasetQualityReport,
    DatasetAuditBatchRequest,
    DatasetAuditBatchResponse,
    DatasetQualityListItem,
)
