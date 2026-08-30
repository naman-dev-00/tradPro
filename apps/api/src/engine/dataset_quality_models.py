from enum import Enum
from datetime import datetime, timezone
from typing import List, Optional, Dict, Literal
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from src.engine.manifest import DatasetCategory

class DatasetQualityStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"

class DatasetIssueSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"

class DatasetIssueCode(str, Enum):
    FILE_UNAVAILABLE = "FILE_UNAVAILABLE"
    CSV_HEADER_INVALID = "CSV_HEADER_INVALID"
    ROW_MALFORMED = "ROW_MALFORMED"
    TIMESTAMP_INVALID = "TIMESTAMP_INVALID"
    TIMESTAMP_NOT_UTC = "TIMESTAMP_NOT_UTC"
    TIMESTAMP_OUT_OF_ORDER = "TIMESTAMP_OUT_OF_ORDER"
    DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"
    TIMEFRAME_UNSUPPORTED = "TIMEFRAME_UNSUPPORTED"
    TIMEFRAME_INTERVAL_MISMATCH = "TIMEFRAME_INTERVAL_MISMATCH"
    MISSING_INTERVAL = "MISSING_INTERVAL"
    INSTRUMENT_ID_MISMATCH = "INSTRUMENT_ID_MISMATCH"
    TIMEFRAME_VALUE_MISMATCH = "TIMEFRAME_VALUE_MISMATCH"
    NON_FINITE_VALUE = "NON_FINITE_VALUE"
    NEGATIVE_PRICE = "NEGATIVE_PRICE"
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
    OHLC_HIGH_BOUND_INVALID = "OHLC_HIGH_BOUND_INVALID"
    OHLC_LOW_BOUND_INVALID = "OHLC_LOW_BOUND_INVALID"
    INCOMPLETE_CANDLE_PRESENT = "INCOMPLETE_CANDLE_PRESENT"
    MANIFEST_COUNT_MISMATCH = "MANIFEST_COUNT_MISMATCH"
    COMPLETED_COUNT_MISMATCH = "COMPLETED_COUNT_MISMATCH"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    MANIFEST_METADATA_MISMATCH = "MANIFEST_METADATA_MISMATCH"
    INSUFFICIENT_DATA_FOR_WARMUP = "INSUFFICIENT_DATA_FOR_WARMUP"
    DATASET_ROW_LIMIT_EXCEEDED = "DATASET_ROW_LIMIT_EXCEEDED"

def ensure_utc_optional_datetime(v: Optional[datetime]) -> Optional[datetime]:
    if v is None:
        return None
    if not isinstance(v, datetime):
        raise ValueError("Must be a valid datetime")
    if v.tzinfo is None:
        raise ValueError("Datetime must be timezone-aware (UTC)")
    return v.astimezone(timezone.utc)

class DatasetQualityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: DatasetIssueCode
    severity: DatasetIssueSeverity
    message: str
    row_number: Optional[int] = None
    timestamp: Optional[datetime] = None
    field: Optional[str] = None
    expected: Optional[str] = None
    actual: Optional[str] = None

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        return ensure_utc_optional_datetime(v)

class DatasetQualitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_rows: int
    valid_rows: int
    malformed_rows: int
    completed_rows: int
    incomplete_rows: int
    duplicate_timestamp_count: int
    missing_interval_count: int
    first_timestamp: Optional[datetime] = None
    last_timestamp: Optional[datetime] = None
    expected_interval_seconds: Optional[int] = None
    calculated_checksum: Optional[str] = None
    manifest_checksum: Optional[str] = None
    checksum_matches: Optional[bool] = None

    @field_validator("first_timestamp", "last_timestamp")
    @classmethod
    def validate_summary_timestamps(cls, v: Optional[datetime]) -> Optional[datetime]:
        return ensure_utc_optional_datetime(v)

    @model_validator(mode="after")
    def validate_row_counts(self):
        if self.valid_rows + self.malformed_rows != self.total_rows:
            raise ValueError(
                f"Row count invariant violated: valid_rows ({self.valid_rows}) + malformed_rows ({self.malformed_rows}) != total_rows ({self.total_rows})"
            )
        if self.completed_rows + self.incomplete_rows != self.valid_rows:
            raise ValueError(
                f"Row completion invariant violated: completed_rows ({self.completed_rows}) + incomplete_rows ({self.incomplete_rows}) != valid_rows ({self.valid_rows})"
            )
        if self.duplicate_timestamp_count < 0:
            raise ValueError("duplicate_timestamp_count must be >= 0")
        if self.missing_interval_count < 0:
            raise ValueError("missing_interval_count must be >= 0")
        return self

class DatasetProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    display_name: str
    category: DatasetCategory
    instrument_id: str
    timeframe: str
    is_synthetic: Literal[True] = True
    manifest_version: str
    fixture_checksum: Optional[str] = None
    source_type: Literal["PACKAGED_SYNTHETIC_FIXTURE"] = "PACKAGED_SYNTHETIC_FIXTURE"
    immutable: Literal[True] = True

class DatasetQualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    status: DatasetQualityStatus
    provenance: DatasetProvenance
    summary: DatasetQualitySummary
    issues: List[DatasetQualityIssue]
    total_issue_count: int
    reported_issue_count: int
    issues_truncated: bool
    audit_rules_version: str = "1.0.0"
    warnings: List[str] = []

    @model_validator(mode="after")
    def validate_issue_counts(self):
        if self.reported_issue_count != len(self.issues):
            raise ValueError(
                f"reported_issue_count ({self.reported_issue_count}) must equal len(issues) ({len(self.issues)})"
            )
        if self.reported_issue_count > self.total_issue_count:
            raise ValueError(
                f"reported_issue_count ({self.reported_issue_count}) cannot exceed total_issue_count ({self.total_issue_count})"
            )
        expected_truncated = self.reported_issue_count < self.total_issue_count
        if self.issues_truncated != expected_truncated:
            raise ValueError(
                f"issues_truncated ({self.issues_truncated}) must match (reported_issue_count < total_issue_count: {expected_truncated})"
            )
        return self

class DatasetQualityListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    display_name: str
    category: DatasetCategory
    instrument_id: str
    timeframe: str
    status: DatasetQualityStatus
    summary: DatasetQualitySummary
    provenance: DatasetProvenance

class DatasetAuditBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_ids: List[str]

    @field_validator("dataset_ids")
    @classmethod
    def validate_dataset_ids(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("dataset_ids must contain at least 1 dataset ID.")
        if len(v) > 20:
            raise ValueError("dataset_ids cannot exceed 20 dataset IDs per batch audit.")
        if len(v) != len(set(v)):
            raise ValueError("dataset_ids must not contain duplicate dataset IDs.")
        for ds_id in v:
            if not isinstance(ds_id, str) or not ds_id.strip():
                raise ValueError("dataset_ids must contain non-empty string IDs.")
        return v

class DatasetAuditBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reports: List[DatasetQualityReport]
    status_counts: Dict[str, int]
    total_datasets: int
    audit_rules_version: str = "1.0.0"
    warnings: List[str] = []

    @model_validator(mode="after")
    def validate_status_counts(self):
        for st in ("PASS", "WARN", "FAIL"):
            if st not in self.status_counts:
                raise ValueError(f"status_counts must contain key '{st}'")
            if self.status_counts[st] < 0:
                raise ValueError(f"status_counts['{st}'] must be >= 0")
        sum_counts = sum(self.status_counts.get(st, 0) for st in ("PASS", "WARN", "FAIL"))
        if sum_counts != self.total_datasets or sum_counts != len(self.reports):
            raise ValueError(
                f"status_counts sum ({sum_counts}) must equal total_datasets ({self.total_datasets}) and len(reports) ({len(self.reports)})"
            )
        return self
