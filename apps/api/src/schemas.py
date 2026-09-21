import datetime
from typing import List, Optional, Any, Literal, Dict
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
    model_config = ConfigDict(extra="forbid")
    max_position_size: float = Field(..., ge=0)
    stop_loss_pct: float = Field(..., ge=0)
    take_profit_pct: float = Field(..., ge=0)
    validity_window: int = Field(..., ge=1)

class PaperTradeAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    risk_config: RiskConfiguration

class StrategyBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Optional[str] = None
    name: str = Field(..., min_length=1)
    description: Optional[str] = None
    timeframe: str
    candidate_selection_mode: str = "FIRST_ELIGIBLE"
    global_conditions: Optional[ConditionNode] = None
    candidate_conditions: Optional[ConditionNode] = None
    action: PaperTradeAction

class StrategyCreate(StrategyBase):
    pass

class StrategyResponse(StrategyBase):
    id: str
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True, extra="forbid")

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

# --- Milestone 6A: Paper Trading Runtime, OMS & Risk Schemas ---

from decimal import Decimal
from src.engine.paper.models import (
    TradingMode,
    IntentType,
    OrderSide,
    OrderType,
    TimeInForce,
    OrderStatus,
    RuntimeStatus,
    KillSwitchScope,
    LedgerEntryType,
    ActionTriggerCondition,
    PositionExistsBehavior,
)

class PaperAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=100)
    initial_balance: Decimal = Field(..., gt=0)
    currency: str = Field(default="INR", min_length=3, max_length=10)

class PaperAccountResponse(BaseModel):
    id: str
    name: str
    currency: str
    total_cash: Decimal
    reserved_cash: Decimal
    available_cash: Decimal
    is_active: bool
    version: int
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)

class AccountLedgerEntryResponse(BaseModel):
    id: str
    account_id: str
    sequence_number: int
    entry_type: str
    settled_cash_delta: Decimal = Decimal("0.00")
    reserved_cash_delta: Decimal = Decimal("0.00")
    settled_cash_after: Decimal = Decimal("0.00")
    reserved_cash_after: Decimal = Decimal("0.00")
    amount: Decimal = Decimal("0.00")
    balance_after: Decimal = Decimal("0.00")
    order_id: Optional[str] = None
    fill_id: Optional[str] = None
    reason_code: str
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)

class ActionRuleMappingSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mapping_id: str = Field(..., min_length=1, max_length=50)
    rule_target: Literal["GLOBAL", "CANDIDATE"]
    trigger_status: ActionTriggerCondition = ActionTriggerCondition.ON_TRUE
    instrument_id: str = Field(..., min_length=1)
    side: OrderSide
    order_type: OrderType
    quantity: Decimal = Field(..., gt=0)
    limit_price: Optional[Decimal] = None
    time_in_force: TimeInForce = TimeInForce.DAY
    cooldown_bars: int = Field(default=0, ge=0)
    intent_type: IntentType = IntentType.ENTRY

class StrategyActionPolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str
    name: str = Field(..., min_length=1, max_length=100)
    entry_mapping: ActionRuleMappingSchema
    exit_mapping: Optional[ActionRuleMappingSchema] = None
    position_exists_behavior: PositionExistsBehavior = PositionExistsBehavior.IGNORE
    max_entries_per_day: int = Field(default=5, ge=1, le=100)

class StrategyActionPolicyResponse(BaseModel):
    id: str
    strategy_id: str
    name: str
    version: int
    payload: Dict[str, Any]
    is_active: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)

class RiskPolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=100)
    max_quantity_per_order: Decimal = Field(default=Decimal("1000"), gt=0)
    max_notional_per_order: Decimal = Field(default=Decimal("500000"), gt=0)
    max_open_orders: int = Field(default=10, ge=1, le=50)
    max_open_positions: int = Field(default=5, ge=1, le=20)
    max_instrument_exposure: Decimal = Field(default=Decimal("1000000"), gt=0)
    max_total_exposure: Decimal = Field(default=Decimal("2000000"), gt=0)
    max_trades_per_day: int = Field(default=20, ge=1, le=200)
    max_daily_realized_loss: Decimal = Field(default=Decimal("50000"), gt=0)
    allowed_instruments: Optional[List[str]] = None
    max_price_staleness_seconds: int = Field(default=3600, ge=1)
    fee_basis_points: int = Field(default=5, ge=0)
    flat_fee: Decimal = Field(default=Decimal("20.00"), ge=0)
    is_default: bool = False

class RiskPolicyResponse(BaseModel):
    id: str
    name: str
    version: int
    payload: Dict[str, Any]
    is_default: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)

class StrategyRuntimeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str
    action_policy_id: Optional[str] = None  # Auto-synthesizes if not supplied
    risk_policy_id: Optional[str] = None    # Auto-synthesizes if not supplied
    account_id: str
    dataset_id: str
    timeframe: str = "15m"
    trading_mode: Optional[Literal["PAPER", "BROKER_SANDBOX", "BROKER_SANDBOX_RECORDED_FIXTURE"]] = "PAPER"
    instrument_mapping_id: Optional[str] = None

class StrategyRuntimeResponse(BaseModel):
    id: str
    strategy_id: str
    action_policy_id: str
    risk_policy_id: str
    account_id: str
    status: str
    trading_mode: str
    dataset_id: str
    timeframe: str
    last_processed_candle_timestamp: Optional[datetime.datetime] = None
    consecutive_errors: int
    version: int
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)

class StrategyRuntimeStepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_count: int = Field(default=1, ge=1, le=50)

class OrderIntentResponse(BaseModel):
    id: str
    runtime_id: str
    action_mapping_id: str
    requested_instrument_id: str
    resolved_instrument_id: str
    intent_type: str
    reduce_only: bool
    side: str
    quantity: Decimal
    order_type: str
    limit_price: Optional[Decimal] = None
    time_in_force: str
    source_candle_timestamp: datetime.datetime
    source_evaluation_fingerprint: str
    created_at: datetime.datetime

class OrderEventResponse(BaseModel):
    id: str
    sequence_number: int
    previous_status: str
    new_status: str
    actor: str
    reason_code: str
    metadata_json: Optional[Dict[str, Any]] = None
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)

class OrderResponse(BaseModel):
    id: str
    runtime_id: str
    intent_id: str
    account_id: str
    order_sequence_number: int
    instrument_id: str
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Optional[Decimal] = None
    filled_quantity: Decimal
    status: str
    version: int
    events: Optional[List[OrderEventResponse]] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime

class FillResponse(BaseModel):
    id: str
    order_id: str
    account_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    candle_timestamp: datetime.datetime
    created_at: datetime.datetime

class PaperPositionResponse(BaseModel):
    id: str
    account_id: str
    instrument_id: str
    net_quantity: Decimal
    average_entry_price: Decimal
    cost_basis: Decimal
    gross_realized_pnl: Decimal
    total_fees: Decimal
    net_realized_pnl: Decimal
    last_mark_price: Decimal
    unrealized_pnl: Decimal
    updated_at: datetime.datetime

class KillSwitchStatusResponse(BaseModel):
    global_active: bool
    global_engaged_at: Optional[datetime.datetime] = None
    global_reason: Optional[str] = None
    user_active: bool
    user_engaged_at: Optional[datetime.datetime] = None
    user_reason: Optional[str] = None

class KillSwitchEngageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["GLOBAL", "USER"]
    reason: str = Field(..., min_length=3, max_length=500)

class KillSwitchResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["GLOBAL", "USER"]
    reason: str = Field(..., min_length=3, max_length=500)


# --- Upstox Sandbox Schemas ---

class SandboxReadinessResponse(BaseModel):
    runtime_id: str
    environment_allowed: bool
    network_enabled: bool
    credential_present: bool
    owner_matches: bool
    provider_matches: bool
    mapping_verified: bool
    mapping_unexpired: bool
    global_kill_switch_clear: bool
    user_kill_switch_clear: bool
    worker_available: bool
    ready_for_submission: bool
    cancel_available: bool
    reasons: List[str] = []


class ProviderConnectionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_version: Optional[str] = Field("v1", max_length=32)


class ProviderConnectionResponse(BaseModel):
    provider: str
    environment: str
    credential_configured: bool
    credential_version: str
    readiness_status: str
    last_successful_transmission_at: Optional[datetime.datetime] = None


class ProviderInstrumentMappingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tradepro_instrument_id: str = Field(..., min_length=1, max_length=100)
    provider_instrument_token: str = Field(..., min_length=1, max_length=100)
    exchange: str = Field(..., min_length=1, max_length=20)
    segment: str = Field(..., min_length=1, max_length=20)
    symbol: str = Field(..., min_length=1, max_length=100)
    expiry_date: Optional[datetime.datetime] = None
    strike_price: Optional[Decimal] = None
    option_type: Optional[Literal["CE", "PE"]] = None
    lot_size: int = Field(1, gt=0)
    tick_size: Decimal = Field(Decimal("0.05"), gt=0)
    freeze_quantity: int = Field(1800, gt=0)


class ProviderInstrumentMappingVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["VERIFIED", "REJECTED", "DISABLED"]
    reason: str = Field(..., min_length=3, max_length=500)


class ProviderInstrumentMappingResponse(BaseModel):
    id: str
    owner_id: str
    tradepro_instrument_id: str
    provider_instrument_token: str
    exchange: str
    segment: str
    symbol: str
    expiry_date: Optional[datetime.datetime] = None
    strike_price: Optional[Decimal] = None
    option_type: Optional[str] = None
    lot_size: int
    tick_size: Decimal
    freeze_quantity: int
    verification_status: str
    verified_by: Optional[str] = None
    verified_at: Optional[datetime.datetime] = None
    verification_audit_json: Optional[Dict[str, Any]] = None
    mapping_version: int
    created_at: datetime.datetime
    updated_at: datetime.datetime


class SubmissionOutboxResponse(BaseModel):
    id: str
    owner_id: str
    order_id: str
    action_type: str
    priority: int = 10
    status: str
    idempotency_key: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime.datetime
    last_error_code: Optional[str] = None
    last_error_message: Optional[str] = None
    transmission_started_at: Optional[datetime.datetime] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class ReconciliationResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outbox_id: Optional[str] = Field(None, max_length=36)
    resolution_type: Literal["PLACE_CONFIRMED", "PLACE_REJECTED", "CANCEL_CONFIRMED", "CANCEL_NOT_CONFIRMED"]
    provider_order_reference: Optional[str] = Field(None, max_length=100)
    notes: str = Field(..., min_length=5, max_length=1000)


class ReconciliationRecordResponse(BaseModel):
    id: str
    owner_id: str
    order_id: str
    outbox_id: str
    status: str
    resolution_type: Optional[Literal["PLACE_CONFIRMED", "PLACE_REJECTED", "CANCEL_CONFIRMED", "CANCEL_NOT_CONFIRMED"]] = None
    resolved_by: Optional[str] = None
    provider_order_reference: Optional[str] = None
    notes: Optional[str] = None
    resolved_at: Optional[datetime.datetime] = None
    created_at: datetime.datetime


# --- Milestone 6C Phase 2 Orchestration Schemas ---

class OrchestrationConsentSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent_version: str = Field("fixture_consent_v1", max_length=50)
    acknowledged_source_type: str = Field(..., max_length=50)
    acknowledged_execution_policy: str = Field(..., max_length=50)
    acknowledged_timeframe: Literal["5m", "15m"]
    acknowledged_replay_open_at: datetime.datetime
    acknowledged_replay_close_at: datetime.datetime
    acknowledged_dataset_ids: List[str] = Field(..., min_length=1, max_length=2)
    confirm_prohibition_of_live_trading: bool = True
    confirm_internal_mock_only: bool = True


class OrchestrationDatasetRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(..., min_length=1, max_length=100)
    series_role: Literal["REFERENCE", "SUBJECT"]


class OrchestrationConfigCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime_id: str = Field(..., min_length=1, max_length=36)
    timeframe: Literal["5m", "15m"]
    replay_open_at: datetime.datetime
    replay_close_at: datetime.datetime
    strategy_version: int = Field(1, gt=0)
    datasets: List[OrchestrationDatasetRef] = Field(..., min_length=1, max_length=2)
    provider_mapping_id: str = Field(..., min_length=1, max_length=36)
    consent: OrchestrationConsentSubmission


class OrchestrationActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent_version: str = Field("fixture_consent_v1", max_length=50)
    acknowledged_execution_policy: str = Field("INTERNAL_MOCK_ONLY", max_length=50)
    confirm_internal_mock_only: bool = True


class OrchestrationConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    runtime_id: str
    source_type: str
    source_namespace: str
    execution_policy: str
    snapshot_fingerprint: str
    consent_fingerprint: str
    consent_policy_version: str
    consent_at: datetime.datetime
    timeframe: str
    replay_open_at: datetime.datetime
    replay_close_at: datetime.datetime
    checkpoint_close_at: Optional[datetime.datetime] = None
    fencing_generation: int
    retry_count: int
    created_at: datetime.datetime
    updated_at: datetime.datetime


class OrchestrationLifecycleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime_id: str
    status: str
    previous_status: Optional[str] = None
    action: str
    message: str
    timestamp: datetime.datetime


class OrchestrationReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime_id: str
    ready: bool
    reasons: List[str]
    gates: Dict[str, bool]


# --- Milestone 6C Phase 3 Evaluation Schemas ---

class RuntimeEvaluationSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    runtime_id: str
    config_id: str
    snapshot_fingerprint: str
    evaluation_fingerprint: str
    timeframe: str
    close_at: datetime.datetime
    reference_candle_id: str
    subject_candle_id: Optional[str] = None
    evaluation_status: str
    action_outcome: str
    risk_outcome: str
    no_order_reason: Optional[str] = None
    finalized_at: datetime.datetime


class RuntimeEvaluationDetailResponse(RuntimeEvaluationSummaryResponse):
    model_config = ConfigDict(extra="forbid")
    required_candles_json: str
    audit_json: str
    risk_summary_json: str


class EvaluationHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime_id: str
    total: int
    limit: int
    offset: int
    evaluations: List[RuntimeEvaluationSummaryResponse]
