from typing import List, Dict, Optional, Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class DatasetChecksumResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    stored_checksum: Optional[str] = None
    current_checksum: Optional[str] = None
    matches: bool


class ReplayVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    verification_status: Literal["VERIFIED", "MISMATCH", "UNVERIFIABLE", "INVALID"]
    stored_request_fingerprint: Optional[str] = None
    recomputed_request_fingerprint: Optional[str] = None
    fingerprint_matches: bool = False
    stored_manifest_version: Optional[str] = None
    current_manifest_version: Optional[str] = None
    manifest_version_matches: bool = False
    stored_engine_version: Optional[str] = None
    current_engine_version: Optional[str] = None
    engine_version_matches: bool = False
    stored_replay_schema_version: Optional[str] = None
    current_replay_schema_version: Optional[str] = None
    replay_schema_version_matches: bool = False
    dataset_checksum_results: List[DatasetChecksumResult] = Field(default_factory=list)
    strategy_snapshot_present: bool = False
    result_payload_present: bool = False
    reasons: List[str] = Field(default_factory=list)


class ReplayComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_run_id: str
    comparison_run_id: str
    include_unchanged: bool = False


class ReplayStatusDifference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    dataset_id: str
    baseline_present: bool
    comparison_present: bool
    baseline_status: Optional[str] = None
    comparison_status: Optional[str] = None
    changed: bool
    baseline_condition_ids: Dict[str, List[str]] = Field(default_factory=dict)
    comparison_condition_ids: Dict[str, List[str]] = Field(default_factory=dict)
    newly_true_condition_ids: List[str] = Field(default_factory=list)
    no_longer_true_condition_ids: List[str] = Field(default_factory=list)
    newly_false_condition_ids: List[str] = Field(default_factory=list)
    no_longer_false_condition_ids: List[str] = Field(default_factory=list)
    newly_unavailable_condition_ids: List[str] = Field(default_factory=list)
    newly_invalid_condition_ids: List[str] = Field(default_factory=list)
    explanation: str


class ReplayComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_metadata: Dict[str, Any]
    comparison_metadata: Dict[str, Any]
    aligned_point_count: int
    baseline_only_point_count: int
    comparison_only_point_count: int
    unchanged_point_count: int
    changed_point_count: int
    status_transition_counts: Dict[str, int]
    differences: List[ReplayStatusDifference] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
