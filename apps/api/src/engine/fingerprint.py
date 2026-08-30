import json
import hashlib
from datetime import datetime
from typing import Dict, Any, List
from src.engine.manifest import get_manifest_checksums_snapshot, MANIFEST_VERSION
from src.engine.multi_series_models import ensure_utc_datetime

REPLAY_SCHEMA_VERSION = "1.0.0"
ENGINE_VERSION = "1.0.0"

def canonicalize_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))

def compute_request_fingerprint(
    strategy_payload: Dict[str, Any],
    reference_dataset_id: str,
    subject_dataset_ids: List[str],
    start_timestamp: datetime,
    end_timestamp: datetime,
    sampling_step: int,
) -> str:
    start_dt = ensure_utc_datetime(start_timestamp)
    end_dt = ensure_utc_datetime(end_timestamp)

    # PRESERVE array order of subject_dataset_ids for deterministic output contract!
    subject_ids_ordered = list(subject_dataset_ids)

    checksums = get_manifest_checksums_snapshot()
    relevant_ds = sorted(set([reference_dataset_id] + subject_ids_ordered))
    relevant_checksums = {
        ds_id: checksums.get(ds_id, "")
        for ds_id in relevant_ds
    }

    canonical_dict = {
        "strategy": strategy_payload,
        "reference_dataset_id": reference_dataset_id,
        "subject_dataset_ids": subject_ids_ordered,  # Array order preserved!
        "start_timestamp": start_dt.isoformat(),
        "end_timestamp": end_dt.isoformat(),
        "sampling_step": sampling_step,
        "engine_version": ENGINE_VERSION,
        "manifest_version": MANIFEST_VERSION,
        "replay_schema_version": REPLAY_SCHEMA_VERSION,
        "fixture_checksums": relevant_checksums,
    }

    canonical_str = canonicalize_json(canonical_dict)
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
