"""Immutable, bounded Phase 1 contracts. REALTIME_PROVIDER is not supported."""

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.engine.rule_models import EvaluationStatus  # Reuse the existing rule-result contract.

from .fingerprint import canonical_json

MAX_UNITS = 9_000_000_000_000_000
MAX_EVIDENCE = 65536
TIMEFRAME_SECONDS = {"5m": 300, "15m": 900}
Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:/-]+$")]
ResourceID = Annotated[str, Field(min_length=1, max_length=36, pattern=r"^[A-Za-z0-9_-]+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Timeframe = Literal["5m", "15m"]
Units = Annotated[int, Field(strict=True, ge=0, le=MAX_UNITS)]
PriceUnits = Annotated[int, Field(strict=True, gt=0, le=MAX_UNITS)]
Scale = Annotated[int, Field(strict=True, ge=0, le=8)]


class CandleSourceType(str, Enum):
    FIXTURE_REPLAY = "FIXTURE_REPLAY"


class SeriesRole(str, Enum):
    REFERENCE = "REFERENCE"
    SUBJECT = "SUBJECT"


class ActionOutcome(str, Enum):
    NO_ACTION = "NO_ACTION"
    REJECTED = "REJECTED"
    ACCEPTED_INTERNAL = "ACCEPTED_INTERNAL"


class RiskOutcome(str, Enum):
    NOT_RUN = "NOT_RUN"
    REJECTED = "REJECTED"
    ACCEPTED = "ACCEPTED"


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Naive timestamps are forbidden")
    return value.astimezone(timezone.utc)


from .evidence import snapshot_object as bounded_object


class FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=False)

    @field_validator("*", mode="before")
    @classmethod
    def normalize_timestamps(cls, value, info):
        if not info.field_name.endswith("_at") or value is None:
            return value
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime):
            raise ValueError("An explicit aware datetime or ISO timestamp is required")
        return utc(value)


class DatasetProvenance(FrozenContract):
    dataset_id: Identifier
    checksum: Digest
    instrument_id: Identifier
    series_role: SeriesRole


class ProviderMappingIdentity(FrozenContract):
    mapping_id: ResourceID
    mapping_version: Annotated[int, Field(strict=True, gt=0, le=2147483647)]
    verification_state: Literal["VERIFIED"]
    expiry_at: datetime | None


class OrchestrationSnapshot(FrozenContract):
    contract_version: Literal["1"] = "1"
    owner_id: ResourceID
    runtime_id: ResourceID
    strategy_version: Annotated[int, Field(strict=True, gt=0, le=2147483647)]
    # Canonical object strings make nested data immutable as well as the model.
    strategy_snapshot: str
    action_policy_snapshot: str
    risk_policy_snapshot: str
    instrument_specification: str
    provider_mapping: ProviderMappingIdentity
    source_type: CandleSourceType = CandleSourceType.FIXTURE_REPLAY
    source_namespace: Identifier
    datasets: Annotated[tuple[DatasetProvenance, ...], Field(min_length=1, max_length=2)]
    timeframe: Timeframe
    source_policy_version: Literal["packaged_alignment_v1"] = "packaged_alignment_v1"
    alignment_offset_seconds: Annotated[int, Field(strict=True, ge=0)]
    replay_open_at: datetime
    replay_close_at: datetime
    engine_version: Identifier
    indicator_engine_version: Identifier
    orchestration_policy_version: Literal["1"] = "1"
    execution_policy: Literal["INTERNAL_MOCK_ONLY"] = "INTERNAL_MOCK_ONLY"
    external_transmission_allowed: Literal[False] = False

    @field_validator("strategy_snapshot", "action_policy_snapshot", "risk_policy_snapshot", "instrument_specification", mode="before")
    @classmethod
    def freeze_objects(cls, value):
        return bounded_object(value)

    @model_validator(mode="after")
    def validate_replay(self):
        seconds = TIMEFRAME_SECONDS[self.timeframe]
        if self.replay_close_at <= self.replay_open_at:
            raise ValueError("Replay bounds must increase")
        for boundary in (self.replay_open_at, self.replay_close_at):
            require_alignment(boundary, seconds, self.alignment_offset_seconds)
        roles = [dataset.series_role for dataset in self.datasets]
        if len(set(roles)) != len(roles) or SeriesRole.REFERENCE not in roles:
            raise ValueError("Require one reference and at most one subject series")
        if len(canonical_json(self.model_dump(mode="python"))) > 262144:
            raise ValueError("Complete snapshot exceeds persistence limit")
        return self


def require_alignment(value: datetime, seconds: int, offset: int) -> None:
    value = utc(value)
    if type(seconds) is not int or seconds <= 0:
        raise ValueError("Duration must be a positive integer")
    if type(offset) is not int or not 0 <= offset < seconds:
        raise ValueError("Source offset must be an integer within the timeframe")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = value - epoch
    if value.microsecond or (delta.days * 86400 + delta.seconds) % seconds != offset:
        raise ValueError("Candle boundary is not aligned to the frozen source offset")


class CompletedCandle(FrozenContract):
    contract_version: Literal["1"] = "1"
    owner_id: ResourceID
    runtime_id: ResourceID
    source_namespace: Identifier
    source_event_id: Identifier
    source_type: CandleSourceType = CandleSourceType.FIXTURE_REPLAY
    dataset_id: Identifier
    dataset_checksum: Digest
    instrument_id: Identifier
    timeframe: Timeframe
    series_role: SeriesRole
    source_policy_version: Literal["packaged_alignment_v1"] = "packaged_alignment_v1"
    alignment_offset_seconds: Annotated[int, Field(strict=True, ge=0)]
    open_at: datetime
    close_at: datetime
    received_at: datetime
    price_scale: Scale
    volume_scale: Scale
    open_units: PriceUnits
    high_units: PriceUnits
    low_units: PriceUnits
    close_units: PriceUnits
    volume_units: Units
    is_closed: Literal[True]
    revision: Annotated[int, Field(strict=True, ge=1, le=1)] = 1

    @field_validator("is_closed", mode="before")
    @classmethod
    def closed_only(cls, value):
        if value is not True:
            raise ValueError("Only explicitly completed candles are accepted")
        return value

    @model_validator(mode="after")
    def validate_candle(self):
        seconds = TIMEFRAME_SECONDS[self.timeframe]
        require_alignment(self.open_at, seconds, self.alignment_offset_seconds)
        if (self.close_at - self.open_at).total_seconds() != seconds:
            raise ValueError("Close must be exactly one timeframe after open")
        if self.received_at < self.close_at:
            raise ValueError("Candle delivered before close")
        if self.high_units < max(self.open_units, self.close_units, self.low_units):
            raise ValueError("Invalid OHLC high")
        if self.low_units > min(self.open_units, self.close_units, self.high_units):
            raise ValueError("Invalid OHLC low")
        return self

    @property
    def content_fingerprint(self) -> str:
        from .fingerprint import completed_candle_v1
        return completed_candle_v1(self)


class RequiredCandleIdentity(FrozenContract):
    series_role: SeriesRole
    dataset_id: Identifier
    instrument_id: Identifier
    content_fingerprint: Digest


class RuntimeEvaluationIdentity(FrozenContract):
    contract_version: Literal["1"] = "1"
    owner_id: ResourceID
    runtime_id: ResourceID
    snapshot_fingerprint: Digest
    mapping_id: ResourceID
    mapping_version: Annotated[int, Field(strict=True, gt=0, le=2147483647)]
    timeframe: Timeframe
    close_at: datetime
    required_candles: Annotated[tuple[RequiredCandleIdentity, ...], Field(min_length=1, max_length=2)]

    @model_validator(mode="after")
    def validate_series(self):
        # Alignment is validated against the referenced frozen configuration.
        roles = [item.series_role for item in self.required_candles]
        if len(set(roles)) != len(roles) or SeriesRole.REFERENCE not in roles:
            raise ValueError("Require one reference and at most one subject series")
        return self
