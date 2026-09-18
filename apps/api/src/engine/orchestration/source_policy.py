"""Server-owned packaged source policy; no request-supplied alignment authority."""
from dataclasses import dataclass

from src.engine.manifest import get_dataset_entry
from .models import OrchestrationSnapshot


@dataclass(frozen=True)
class SourceAlignment:
    version: str
    timeframe: str
    offset_seconds: int


# Explicit approval list. Adding datasets/timeframes requires a policy revision.
_APPROVED = {
    "synthetic_underlying_nifty_15m": ("15m", 0),
    "synthetic_candidate_option_ce_23000_15m": ("15m", 0),
    "synthetic_candidate_option_pe_23000_15m": ("15m", 0),
    "synthetic_candidate_option_ce_23500_15m": ("15m", 0),
    "synthetic_short_insufficient_5m": ("5m", 0),
    "synthetic_with_incomplete_candle_15m": ("15m", 0),
}


def packaged_alignment(dataset_id: str) -> SourceAlignment:
    entry = get_dataset_entry(dataset_id)
    if entry is None or dataset_id not in _APPROVED:
        raise ValueError("Dataset has no approved source policy")
    timeframe, offset = _APPROVED[dataset_id]
    if entry.timeframe != timeframe:
        raise ValueError("Manifest disagrees with approved source policy")
    return SourceAlignment("packaged_alignment_v1", timeframe, offset)


def freeze_packaged_snapshot(*, confirmed_user, runtime, material: dict) -> OrchestrationSnapshot:
    """Internal factory; caller supplies authenticated server objects, never a request dump."""
    forbidden = {"owner_id", "runtime_id", "alignment_offset_seconds", "source_policy_version", "source_namespace"}
    if forbidden & material.keys():
        raise ValueError("Identity and source policy must be server-derived")
    if confirmed_user.id != runtime.owner_id or not confirmed_user.is_active:
        raise ValueError("Confirmed user must own the runtime")
    policies = []
    for dataset in material.get("datasets", ()):
        entry = get_dataset_entry(dataset["dataset_id"])
        policies.append(packaged_alignment(dataset["dataset_id"]))
        if (dataset["checksum"], dataset["instrument_id"], dataset["series_role"]) != (
            entry.dataset_checksum, entry.instrument_id, entry.category.value
        ):
            raise ValueError("Dataset provenance disagrees with approved manifest")
    if not policies or len(set(policies)) != 1 or policies[0].timeframe != runtime.timeframe:
        raise ValueError("Datasets must share the runtime source policy")
    policy = policies[0]
    if material.get("timeframe") != policy.timeframe:
        raise ValueError("Snapshot timeframe disagrees with source")
    return OrchestrationSnapshot(
        **material, owner_id=runtime.owner_id, runtime_id=runtime.id,
        source_namespace="packaged:v1", source_policy_version=policy.version,
        alignment_offset_seconds=policy.offset_seconds,
    )


def prepare_configuration(*, confirmed_user, runtime, material: dict, confirmed: bool, clock):
    """Prepare an unsaved internal record with bounded, server-derived consent.

    No activation, transaction, public request schema or Phase 2 service is added.
    """
    from src.models import RuntimeOrchestrationConfig
    from .models import utc
    from .fingerprint import canonical_json, orchestration_snapshot_v1
    from .evidence import config_consent_fingerprint
    if confirmed is not True:
        raise ValueError("Explicit fixture consent is required")
    snapshot = freeze_packaged_snapshot(confirmed_user=confirmed_user, runtime=runtime, material=material)
    now = utc(clock())
    config = RuntimeOrchestrationConfig(
        **{field: getattr(snapshot, field) for field in (
            "owner_id", "runtime_id", "source_type", "source_namespace", "execution_policy",
            "timeframe", "source_policy_version", "alignment_offset_seconds", "replay_open_at", "replay_close_at",
        )},
        snapshot_json=canonical_json(snapshot.model_dump(mode="python")),
        snapshot_fingerprint=orchestration_snapshot_v1(snapshot), consent_at=now,
        consent_policy_version="fixture_consent_v1", created_at=now, updated_at=now,
    )
    config.consent_fingerprint = config_consent_fingerprint(config)
    return config
