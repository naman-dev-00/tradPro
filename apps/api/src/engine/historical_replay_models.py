from datetime import datetime
from typing import List, Dict, Optional, Any
from pydantic import BaseModel, ConfigDict, field_validator
from src.engine.rule_models import EvaluationStatus
from src.engine.multi_series_models import SeriesEvaluationResult, ensure_utc_datetime

class ReplayPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_timestamp: datetime
    reference_timestamp_used: Optional[datetime] = None
    results: List[SeriesEvaluationResult]
    status_counts: Dict[str, int]
    warnings: List[str] = []

    @field_validator("evaluation_timestamp", "reference_timestamp_used", mode="before")
    def validate_utc(cls, v):
        if v is None:
            return None
        return ensure_utc_datetime(v)


class SubjectStatusTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    points: List[Dict[str, Any]]
    transition_counts: Dict[str, int]
    consecutive_status_runs: Dict[str, int]
    first_available_timestamp: Optional[datetime] = None
    unavailable_point_count: int = 0
    invalid_point_count: int = 0

    @field_validator("first_available_timestamp", mode="before")
    def validate_utc(cls, v):
        if v is None:
            return None
        return ensure_utc_datetime(v)


class HistoricalReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Optional[str] = None
    strategy_id: Optional[str] = None
    start_timestamp: datetime
    end_timestamp: datetime
    sampling_step: int = 1
    sampled_timestamp_count: int
    total_evaluations: int
    reference_dataset_id: str
    reference_metadata: Dict[str, Any]
    subject_dataset_ids: List[str]
    subject_metadata: List[Dict[str, Any]]
    replay_points: List[ReplayPoint]
    subject_timelines: List[SubjectStatusTimeline]
    aggregate_status_counts: Dict[str, int]
    reproducibility: Dict[str, Any]
    warnings: List[str] = []

    @field_validator("start_timestamp", "end_timestamp", mode="before")
    def validate_utc(cls, v):
        return ensure_utc_datetime(v)
