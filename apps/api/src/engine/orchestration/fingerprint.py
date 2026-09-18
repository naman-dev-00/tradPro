"""Versioned canonical identities, deliberately independent of legacy fingerprints."""

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def canonical_json(value: Any) -> str:
    """Only exact JSON values and aware timestamps; never coerce floats to strings."""
    def normalize(item: Any) -> Any:
        if isinstance(item, Enum):
            return normalize(item.value)
        if isinstance(item, datetime):
            if item.tzinfo is None or item.utcoffset() is None:
                raise ValueError("Naive timestamps are forbidden")
            return item.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if item is None or type(item) in (str, int, bool):
            return item
        if isinstance(item, (list, tuple)):
            return [normalize(element) for element in item]
        if isinstance(item, dict) and all(type(key) is str for key in item):
            return {key: normalize(element) for key, element in item.items()}
        raise ValueError("Canonical JSON requires exact values: floats and implicit conversions are forbidden")

    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _fingerprint(algorithm: str, payload: dict) -> str:
    encoded = canonical_json({"algorithm": algorithm, "payload": payload})
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def orchestration_snapshot_v1(snapshot) -> str:
    payload = snapshot.model_dump(mode="python")
    for field in ("strategy_snapshot", "action_policy_snapshot", "risk_policy_snapshot", "instrument_specification"):
        payload[field] = json.loads(payload[field])
    return _fingerprint("orchestration_snapshot_v1", payload)


def completed_candle_v1(candle) -> str:
    # Delivery aliases and arrival time are not market content. Duplicate delivery
    # may have a different event ID/time, but must not acquire another identity.
    payload = candle.model_dump(mode="python")
    payload.pop("source_event_id")
    payload.pop("received_at")
    return _fingerprint("completed_candle_v1", payload)


def runtime_evaluation_v1(identity) -> str:
    return _fingerprint("runtime_evaluation_v1", identity.model_dump(mode="python"))
