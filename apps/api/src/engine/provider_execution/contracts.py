"""Contracts and error definitions for provider-driven evaluation and sandbox execution."""
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class ProviderExecutionError(Exception):
    """Base exception for all provider execution errors."""
    def __init__(self, message: str, error_code: str = "PROVIDER_EXECUTION_ERROR"):
        super().__init__(message)
        self.message = message
        self.error_code = error_code


class LiveTransmissionProhibitedError(ProviderExecutionError):
    """Raised when any live broker transmission or non-sandbox routing is attempted."""
    def __init__(self, message: str = "Live broker execution is strictly prohibited in Phase 6."):
        super().__init__(message, error_code="LIVE_TRANSMISSION_PROHIBITED")


class OwnerIsolationError(ProviderExecutionError):
    """Raised when an operation crosses owner boundaries or fails tenant isolation."""
    def __init__(self, message: str = "Owner isolation violation: entity does not belong to authorized owner."):
        super().__init__(message, error_code="OWNER_ISOLATION_VIOLATION")


class RuntimeLifecycleFencedError(ProviderExecutionError):
    """Raised when a runtime is not in RUNNING state or fails lifecycle fencing."""
    def __init__(self, message: str = "Runtime lifecycle fencing violation: runtime is not RUNNING."):
        super().__init__(message, error_code="RUNTIME_LIFECYCLE_FENCED")


class KillSwitchActiveError(ProviderExecutionError):
    """Raised when an active kill switch blocks execution."""
    def __init__(self, message: str = "Emergency kill-switch is active; execution halted."):
        super().__init__(message, error_code="KILL_SWITCH_ACTIVE")


class MappingNotFoundError(ProviderExecutionError):
    """Raised when no provider instrument mapping exists for the orderable instrument."""
    def __init__(self, message: str = "No provider instrument mapping found for instrument."):
        super().__init__(message, error_code="MAPPING_NOT_FOUND")


class UnverifiedMappingError(ProviderExecutionError):
    """Raised when a provider instrument mapping is not VERIFIED."""
    def __init__(self, message: str = "Provider instrument mapping is not VERIFIED."):
        super().__init__(message, error_code="UNVERIFIED_MAPPING")


class ExpiredMappingError(ProviderExecutionError):
    """Raised when a provider instrument mapping has passed its expiry date."""
    def __init__(self, message: str = "Provider instrument mapping has expired."):
        super().__init__(message, error_code="EXPIRED_MAPPING")


class ConsentMissingError(ProviderExecutionError):
    """Raised when explicit operator consent for automated broker sandbox execution is missing or invalid."""
    def __init__(self, message: str = "Explicit operator consent required for automated broker sandbox execution."):
        super().__init__(message, error_code="CONSENT_MISSING")


class UnclosedCandleError(ProviderExecutionError):
    """Raised when an unclosed or in-progress candle is supplied to the evaluator."""
    def __init__(self, message: str = "Unclosed or in-progress candle rejected; only completed candles admitted."):
        super().__init__(message, error_code="UNCLOSED_CANDLE")


class LookAheadProhibitedError(ProviderExecutionError):
    """Raised when a candle close timestamp is in the future relative to the evaluation clock."""
    def __init__(self, message: str = "Temporal look-ahead prohibited; candle close is in the future."):
        super().__init__(message, error_code="LOOK_AHEAD_PROHIBITED")


class DuplicateCandleError(ProviderExecutionError):
    """Raised when duplicate candle timestamps are detected in the candle series."""
    def __init__(self, message: str = "Duplicate candle timestamp detected in evaluation series."):
        super().__init__(message, error_code="DUPLICATE_CANDLE")


class ConflictingIntervalError(ProviderExecutionError):
    """Raised when unaligned or overlapping intervals are detected in the candle series."""
    def __init__(self, message: str = "Conflicting or overlapping candle intervals detected."):
        super().__init__(message, error_code="CONFLICTING_INTERVAL")


class ProviderEvaluationResult(BaseModel):
    """Immutable audit outcome of a provider-driven evaluation cycle."""
    model_config = ConfigDict(frozen=True)

    runtime_id: str
    owner_id: str
    candle_timestamp: datetime
    evaluated_at: datetime
    rule_status: str
    action_decision: str
    risk_decision: str
    order_ids: List[str] = Field(default_factory=list)
    outbox_ids: List[str] = Field(default_factory=list)
    reason_code: str
    details: Dict[str, Any] = Field(default_factory=dict)
