"""Deterministic fixture-replay candle ingestion for strategy orchestration.

Reads exclusively from canonical manifest datasets and packaged fixture CSVs.
Enforces Phase 1 source policies, exact scaled-integer conversions, and
idempotent persistence in completed_candle_events.
"""
import csv
import datetime
import hashlib
import json
import os
import uuid
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from src.engine.manifest import FIXTURES_DIR, get_dataset_entry
from src.engine.orchestration.candle_source import accept_completed_candle, scaled_units
from src.engine.orchestration.fingerprint import canonical_json
from src.engine.orchestration.models import (
    CandleSourceType,
    CompletedCandle,
    OrchestrationSnapshot,
    SeriesRole,
    TIMEFRAME_SECONDS,
    utc,
)
from src.engine.orchestration.source_policy import _APPROVED, packaged_alignment
from src.models import CompletedCandleEvent, RuntimeOrchestrationConfig, StrategyRuntime


class IngestionError(Exception):
    """Raised when candle ingestion fails closed due to malformed or conflicting data."""
    pass


class CandleIngestionConflictError(IngestionError):
    """Raised when candle ingestion encounters conflicting payloads for the same canonical event or interval."""
    pass



def canonical_source_event_id_v1(
    *,
    dataset_id: str,
    instrument_id: str,
    timeframe: str,
    open_at_iso: str,
    revision: int = 1,
) -> str:
    """Deterministic, collision-resistant canonical source event ID.

    Uses versioned canonical JSON hashing consistently for every source event,
    preventing ambiguous concatenation or truncation under arbitrary Phase 1 identifiers.
    Prefix 'c1sha256:' with 64-hex digest is strictly <= 100 characters.
    """
    material = {
        "contract_version": "1",
        "payload": {
            "dataset_id": dataset_id,
            "instrument_id": instrument_id,
            "open_at": open_at_iso,
            "revision": revision,
            "timeframe": timeframe,
        },
    }
    encoded = canonical_json(material)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"c1sha256:{digest}"



def load_fixture_rows(filename: str) -> List[Dict[str, str]]:
    """Read raw string rows from packaged fixture CSV, preserving string precision."""
    filepath = os.path.join(FIXTURES_DIR, filename)
    if not os.path.exists(filepath):
        raise IngestionError(f"Packaged fixture CSV file missing: '{filename}'")

    rows: List[Dict[str, str]] = []
    with open(filepath, mode="r", encoding="utf-8") as f:
        # Filter out comment lines
        lines = [line for line in f if not line.strip().startswith("#")]
        reader = csv.DictReader(lines)
        for row in reader:
            rows.append({k.strip(): v.strip() for k, v in row.items() if k is not None})
    return rows


def parse_and_validate_candle(
    raw_row: Dict[str, str],
    *,
    dataset_id: str,
    series_role: SeriesRole,
    snapshot: OrchestrationSnapshot,
    clock: Optional[Callable[[], datetime.datetime]] = None,
    row_index: int = 0,
) -> CompletedCandle:
    """Parse a single raw fixture row into a validated CompletedCandle contract."""
    # 1. Verify dataset in approved policy & manifest
    if dataset_id not in _APPROVED:
        raise IngestionError(f"Dataset '{dataset_id}' has no approved source policy")

    entry = get_dataset_entry(dataset_id)
    if not entry:
        raise IngestionError(f"Dataset '{dataset_id}' not found in manifest")

    timeframe = snapshot.timeframe
    if entry.timeframe != timeframe:
        raise IngestionError(f"Dataset timeframe '{entry.timeframe}' does not match snapshot timeframe '{timeframe}'")

    # 2. Check closed flag
    is_closed_str = raw_row.get("is_closed", "true").lower()
    if is_closed_str not in ("true", "1", "yes"):
        raise IngestionError(f"Incomplete candle encountered in dataset '{dataset_id}' (is_closed={is_closed_str})")

    # 3. Parse and normalize timestamp
    ts_str = raw_row.get("timestamp")
    if not ts_str:
        raise IngestionError(f"Missing timestamp in candle row {row_index} for dataset '{dataset_id}'")

    try:
        dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        open_at = utc(dt)
    except Exception as exc:
        raise IngestionError(f"Invalid timestamp format '{ts_str}' in row {row_index}: {exc}") from exc

    tf_seconds = TIMEFRAME_SECONDS.get(timeframe)
    if not tf_seconds:
        raise IngestionError(f"Unsupported timeframe '{timeframe}'")

    close_at = open_at + datetime.timedelta(seconds=tf_seconds)
    received_at = close_at  # Packaged replay: delivery at close time

    # 4. Extract price scale and volume scale from instrument specification
    price_scale = 2
    volume_scale = 0
    try:
        inst_spec = json.loads(snapshot.instrument_specification)
        if isinstance(inst_spec, dict):
            price_scale = int(inst_spec.get("price_scale", 2))
    except Exception:
        price_scale = 2

    # 5. Exact integer scaled conversion using Phase 1 canonical helper
    try:
        open_units = scaled_units(raw_row["open"], price_scale)
        high_units = scaled_units(raw_row["high"], price_scale)
        low_units = scaled_units(raw_row["low"], price_scale)
        close_units = scaled_units(raw_row["close"], price_scale)
        volume_units = scaled_units(raw_row.get("volume", "0"), volume_scale)
    except Exception as exc:
        raise IngestionError(f"Numeric conversion error in row {row_index}: {exc}") from exc

    if open_units <= 0 or high_units <= 0 or low_units <= 0 or close_units <= 0:
        raise IngestionError(f"Candle price units must be strictly positive in row {row_index}")
    if volume_units < 0:
        raise IngestionError(f"Candle volume units cannot be negative in row {row_index}")

    # 6. Source event ID: derived from canonical provenance (dataset, instrument, timeframe, open_at, revision)
    # File line position is excluded to preserve deduplication across row reordering.
    open_at_iso = open_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    source_event_id = canonical_source_event_id_v1(
        dataset_id=dataset_id,
        instrument_id=entry.instrument_id,
        timeframe=timeframe,
        open_at_iso=open_at_iso,
        revision=1,
    )

    payload = {
        "owner_id": snapshot.owner_id,
        "runtime_id": snapshot.runtime_id,
        "source_namespace": snapshot.source_namespace,
        "source_event_id": source_event_id,
        "source_type": CandleSourceType.FIXTURE_REPLAY,
        "dataset_id": dataset_id,
        "dataset_checksum": entry.dataset_checksum,
        "instrument_id": entry.instrument_id,
        "timeframe": timeframe,
        "series_role": series_role,
        "source_policy_version": snapshot.source_policy_version,
        "alignment_offset_seconds": snapshot.alignment_offset_seconds,
        "open_at": open_at,
        "close_at": close_at,
        "received_at": received_at,
        "price_scale": price_scale,
        "volume_scale": volume_scale,
        "open_units": open_units,
        "high_units": high_units,
        "low_units": low_units,
        "close_units": close_units,
        "volume_units": volume_units,
        "is_closed": True,
        "revision": 1,
    }

    try:
        clock_fn = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        candle = accept_completed_candle(payload, snapshot=snapshot, clock=clock_fn)
        return candle
    except Exception as exc:
        raise IngestionError(f"Candle acceptance failed for row {row_index}: {exc}") from exc


def persist_completed_candle(
    db: Session,
    candle: CompletedCandle,
) -> CompletedCandleEvent:
    """Persist a CompletedCandle idempotently into completed_candle_events.

    - If exact same content fingerprint exists: return existing row.
    - If conflicting interval/event exists with different fingerprint: fail closed.
    - If new: insert and return.
    """
    fingerprint = candle.content_fingerprint

    # Check existing by content fingerprint
    existing = db.query(CompletedCandleEvent).filter(
        CompletedCandleEvent.owner_id == candle.owner_id,
        CompletedCandleEvent.runtime_id == candle.runtime_id,
        CompletedCandleEvent.content_fingerprint == fingerprint,
    ).first()

    if existing:
        return existing

    # Check for interval collision (same interval, different content)
    interval_conflict = db.query(CompletedCandleEvent).filter(
        CompletedCandleEvent.owner_id == candle.owner_id,
        CompletedCandleEvent.runtime_id == candle.runtime_id,
        CompletedCandleEvent.series_role == candle.series_role.value,
        CompletedCandleEvent.instrument_id == candle.instrument_id,
        CompletedCandleEvent.timeframe == candle.timeframe,
        CompletedCandleEvent.close_at == candle.close_at,
    ).first()

    if interval_conflict:
        if interval_conflict.content_fingerprint != fingerprint:
            raise CandleIngestionConflictError(
                f"Conflicting duplicate candle detected: Conflicting candle payload for interval ending {candle.close_at}: "
                f"existing fingerprint {interval_conflict.content_fingerprint} != {fingerprint}"
            )
        return interval_conflict

    # Check for event collision (same source event ID, different content)
    event_conflict = db.query(CompletedCandleEvent).filter(
        CompletedCandleEvent.owner_id == candle.owner_id,
        CompletedCandleEvent.runtime_id == candle.runtime_id,
        CompletedCandleEvent.source_namespace == candle.source_namespace,
        CompletedCandleEvent.series_role == candle.series_role.value,
        CompletedCandleEvent.dataset_id == candle.dataset_id,
        CompletedCandleEvent.source_event_id == candle.source_event_id,
    ).first()

    if event_conflict:
        if event_conflict.content_fingerprint != fingerprint:
            raise CandleIngestionConflictError(
                f"Conflicting duplicate candle detected: Conflicting candle payload for event '{candle.source_event_id}': "
                f"existing fingerprint {event_conflict.content_fingerprint} != {fingerprint}"
            )
        return event_conflict

    # Insert new record
    candle_dict = candle.model_dump(mode="python")
    candle_dict.pop("contract_version", None)

    event_record = CompletedCandleEvent(
        id=str(uuid.uuid4()),
        content_fingerprint=fingerprint,
        **candle_dict,
    )
    db.add(event_record)
    db.flush()
    return event_record


def ingest_fixture_dataset(
    db: Session,
    config: RuntimeOrchestrationConfig,
    dataset_id: str,
    series_role: SeriesRole,
    *,
    up_to_close_at: Optional[datetime.datetime] = None,
    clock: Optional[Callable[[], datetime.datetime]] = None,
) -> List[CompletedCandleEvent]:
    """Ingest all valid completed candles from a packaged fixture dataset for an orchestration config."""
    snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

    from src.engine.manifest import _FILENAME_MAP
    filename = _FILENAME_MAP.get(dataset_id)
    if not filename:
        raise IngestionError(f"No fixture file mapped for dataset '{dataset_id}'")

    raw_rows = load_fixture_rows(filename)
    persisted_events: List[CompletedCandleEvent] = []

    for idx, row in enumerate(raw_rows):
        # 1. First check if row is marked closed before converting
        is_closed_str = row.get("is_closed", "true").lower()
        if is_closed_str not in ("true", "1", "yes"):
            # Incomplete candle in fixture: skip or reject based on whether it's within bounds
            continue

        try:
            candle = parse_and_validate_candle(
                row,
                dataset_id=dataset_id,
                series_role=series_role,
                snapshot=snapshot,
                clock=clock,
                row_index=idx + 1,
            )
        except IngestionError:
            # Re-raise explicit ingestion errors
            raise

        # Check bounds
        if candle.close_at <= snapshot.replay_open_at:
            continue
        if candle.close_at > snapshot.replay_close_at:
            continue
        if up_to_close_at and candle.close_at > up_to_close_at:
            continue

        event = persist_completed_candle(db, candle)
        persisted_events.append(event)

    return persisted_events


def ingest_all_required_fixture_candles(
    db: Session,
    config: RuntimeOrchestrationConfig,
    *,
    up_to_close_at: Optional[datetime.datetime] = None,
    clock: Optional[Callable[[], datetime.datetime]] = None,
) -> Dict[str, List[CompletedCandleEvent]]:
    """Ingest candles for all datasets declared in the frozen orchestration snapshot."""
    snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)
    results: Dict[str, List[CompletedCandleEvent]] = {}

    for d in snapshot.datasets:
        events = ingest_fixture_dataset(
            db,
            config,
            dataset_id=d.dataset_id,
            series_role=d.series_role,
            up_to_close_at=up_to_close_at,
            clock=clock,
        )
        results[d.dataset_id] = events

    return results
