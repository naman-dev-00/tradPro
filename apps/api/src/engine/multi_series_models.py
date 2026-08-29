from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator
from src.engine.rule_models import EvaluationStatus, ConditionResult, GroupResult

def ensure_utc_datetime(v: datetime) -> datetime:
    if v is None:
        return v
    if not isinstance(v, datetime):
        raise ValueError("Timestamp must be a datetime object.")
    if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
        raise ValueError("Naive timestamps are not allowed. Timestamp must be timezone-aware.")
    return v.astimezone(timezone.utc)

class SeriesEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    instrument_id: str
    timeframe: str
    evaluation_timestamp: datetime
    candle_timestamp_used: Optional[datetime] = None
    overall_status: EvaluationStatus
    reference_result: Optional[Union[ConditionResult, GroupResult]] = None
    subject_result: Optional[Union[ConditionResult, GroupResult]] = None
    passed_condition_ids: List[str] = Field(default_factory=list)
    failed_condition_ids: List[str] = Field(default_factory=list)
    unavailable_condition_ids: List[str] = Field(default_factory=list)
    invalid_condition_ids: List[str] = Field(default_factory=list)
    inspection_summary: str

    @field_validator("evaluation_timestamp", "candle_timestamp_used", mode="after")
    @classmethod
    def validate_utc_timestamps(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None:
            return ensure_utc_datetime(v)
        return v


class MultiSeriesEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: Optional[str] = None
    requested_evaluation_timestamp: datetime
    reference_dataset_id: str
    reference_timestamp_used: Optional[datetime] = None
    results: List[SeriesEvaluationResult] = Field(default_factory=list)
    status_counts: Dict[str, int] = Field(default_factory=dict)
    total_series_evaluated: int = 0
    warnings: List[str] = Field(default_factory=list)

    @field_validator("requested_evaluation_timestamp", "reference_timestamp_used", mode="after")
    @classmethod
    def validate_utc_timestamps(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None:
            return ensure_utc_datetime(v)
        return v

    @field_validator("status_counts", mode="after")
    @classmethod
    def validate_status_counts(cls, v: Dict[str, int]) -> Dict[str, int]:
        required_keys = ["TRUE", "FALSE", "UNAVAILABLE", "INVALID"]
        for key in required_keys:
            if key not in v:
                v[key] = 0
        return v
