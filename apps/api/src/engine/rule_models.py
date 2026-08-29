from enum import Enum
from typing import List, Optional, Any, Dict, Union
from pydantic import BaseModel, Field, ConfigDict

class EvaluationStatus(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"

class ConditionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_id: str
    status: EvaluationStatus
    timestamp: Optional[str] = None
    left_value: Optional[Any] = None
    operator: str
    right_value: Optional[Any] = None
    reason: Optional[str] = None
    indicator_values_used: Dict[str, Any] = Field(default_factory=dict)
    warmup_info: Dict[str, Any] = Field(default_factory=dict)

class GroupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str
    logical_operator: str
    status: EvaluationStatus
    child_results: List[Union['GroupResult', ConditionResult]] = Field(default_factory=list)
    reason: Optional[str] = None

GroupResult.model_rebuild()

class RuleEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: Optional[str] = None
    evaluated_at: str
    reference_timestamp: Optional[str] = None
    subject_timestamp: Optional[str] = None
    reference_series_result: Optional[Union[GroupResult, ConditionResult]] = None
    subject_series_result: Optional[Union[GroupResult, ConditionResult]] = None
    overall_status: EvaluationStatus
    passed_condition_ids: List[str] = Field(default_factory=list)
    failed_condition_ids: List[str] = Field(default_factory=list)
    unavailable_condition_ids: List[str] = Field(default_factory=list)
    invalid_condition_ids: List[str] = Field(default_factory=list)
