"""Internal evidence projections. Not public API schemas or request containers."""
import json
import re
from .fingerprint import canonical_json, _fingerprint


# Only named domain fields can enter snapshot section objects. Deliberately omit
# descriptions, notes, raw payloads, credentials and transport metadata.
SNAPSHOT_KEYS = frozenset("""name timeframe candidate_selection_mode global_conditions candidate_conditions
action type id conditions lhs operator rhs tolerance indicator symbol params
period source level value range risk_config max_position_size stop_loss_pct
take_profit_pct validity_window strategy_id version entry_mapping exit_mapping
position_exists_behavior max_entries_per_day mapping_id rule_target trigger_status
instrument_id side order_type quantity quantity_units limit_price limit_price_units
time_in_force cooldown_bars intent_type max_quantity_per_order max_notional_per_order
max_open_orders max_open_positions max_instrument_exposure max_total_exposure
max_trades_per_day max_daily_realized_loss allowed_instruments max_price_staleness_seconds
fee_basis_points flat_fee is_default reference_dataset_id execution_dataset_id
dataset_id currency currency_scale price_scale quantity_scale tick_size_units
lot_size_units min_quantity_units max_quantity_units allow_fractional allow_short
is_tradable timezone session_open_time session_close_time spec_version
""".split())


def safe_text(value: str, maximum: int = 100) -> str:
    if len(value) > maximum or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError("Evidence text is oversized or contains control characters")
    if re.search(r"(?i)bearer\s|authorization|cookie|token[=: ]|password[=: ]|https?://|traceback|-----BEGIN", value):
        raise ValueError("Transport, credential or diagnostic evidence is forbidden")
    return value


def snapshot_object(value) -> str:
    if isinstance(value, str):
        if len(value) > 65536:
            raise ValueError("Evidence exceeds limit")
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Evidence must be a JSON object")

    def check(item, depth=0):
        if depth > 16:
            raise ValueError("Evidence nesting exceeds limit")
        if isinstance(item, dict):
            if not set(item) <= SNAPSHOT_KEYS:
                raise ValueError("Unapproved snapshot evidence field")
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            if len(item) > 256:
                raise ValueError("Evidence collection exceeds limit")
            for child in item:
                check(child, depth + 1)
        elif isinstance(item, str):
            safe_text(item)
    check(value)
    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("Evidence exceeds limit")
    return encoded


def consent_fingerprint(
    *,
    consent_schema_version: str = "fixture_consent_v1",
    actor_user_id: str,
    owner_id: str,
    runtime_id: str,
    orchestration_config_id: str,
    snapshot_fingerprint: str,
    mapping_identity: str,
    mapping_version: int,
    ordered_dataset_identities: list,
    dataset_provenance_or_revision: list,
    timeframe: str,
    alignment_offset_seconds: int,
    replay_open_at,
    replay_close_at,
    source_type: str,
    execution_policy: str,
    explicit_live_trading_prohibition: bool = True,
    explicit_internal_mock_confirmation: bool = True,
    **extra_kwargs,
) -> str:
    """Canonical consent fingerprinting strictly binding all 18 authoritative dimensions."""
    def _sort_key(item):
        if isinstance(item, dict):
            return str(item.get("dataset_id", ""))
        return str(item)

    payload = {
        "consent_schema_version": str(consent_schema_version),
        "actor_user_id": str(actor_user_id),
        "owner_id": str(owner_id),
        "runtime_id": str(runtime_id),
        "orchestration_config_id": str(orchestration_config_id),
        "snapshot_fingerprint": str(snapshot_fingerprint),
        "mapping_identity": str(mapping_identity),
        "mapping_version": int(mapping_version),
        "ordered_dataset_identities": sorted(ordered_dataset_identities, key=_sort_key),
        "dataset_provenance_or_revision": sorted(dataset_provenance_or_revision, key=_sort_key),
        "timeframe": str(timeframe),
        "alignment_offset_seconds": int(alignment_offset_seconds),
        "replay_open_at": replay_open_at,
        "replay_close_at": replay_close_at,
        "source_type": str(source_type),
        "execution_policy": str(execution_policy),
        "explicit_live_trading_prohibition": bool(explicit_live_trading_prohibition),
        "explicit_internal_mock_confirmation": bool(explicit_internal_mock_confirmation),
    }
    return _fingerprint("orchestration_consent_v1", payload)


def config_consent_fingerprint(config, actor_user_id: str = None) -> str:
    """Derive canonical consent fingerprint from a RuntimeOrchestrationConfig model."""
    snap_dict = {}
    snap_json = getattr(config, "snapshot_json", None)
    if snap_json:
        if isinstance(snap_json, dict):
            snap_dict = snap_json
        elif isinstance(snap_json, str):
            try:
                snap_dict = json.loads(snap_json)
            except Exception:
                snap_dict = {}

    provider_map = snap_dict.get("provider_mapping", {})
    mapping_id = provider_map.get("mapping_id", "")
    mapping_ver = provider_map.get("mapping_version", 1)

    datasets = snap_dict.get("datasets", [])
    ordered_dataset_ids = [
        {"dataset_id": d["dataset_id"], "series_role": d.get("series_role", "REFERENCE")}
        for d in datasets if isinstance(d, dict) and "dataset_id" in d
    ]
    dataset_provenance = [
        {"dataset_id": d["dataset_id"], "checksum": d.get("checksum", "")}
        for d in datasets if isinstance(d, dict) and "dataset_id" in d
    ]

    actor = actor_user_id or getattr(config, "_actor_user_id", None) or getattr(config, "owner_id", "")
    config_id = getattr(config, "id", "") or ""

    return consent_fingerprint(
        consent_schema_version=getattr(config, "consent_policy_version", "fixture_consent_v1") or "fixture_consent_v1",
        actor_user_id=actor,
        owner_id=getattr(config, "owner_id", ""),
        runtime_id=getattr(config, "runtime_id", ""),
        orchestration_config_id=config_id,
        snapshot_fingerprint=getattr(config, "snapshot_fingerprint", ""),
        mapping_identity=mapping_id,
        mapping_version=mapping_ver,
        ordered_dataset_identities=ordered_dataset_ids,
        dataset_provenance_or_revision=dataset_provenance,
        timeframe=getattr(config, "timeframe", ""),
        alignment_offset_seconds=getattr(config, "alignment_offset_seconds", 0) or 0,
        replay_open_at=getattr(config, "replay_open_at"),
        replay_close_at=getattr(config, "replay_close_at"),
        source_type=getattr(config, "source_type", ""),
        execution_policy=getattr(config, "execution_policy", ""),
        explicit_live_trading_prohibition=True,
        explicit_internal_mock_confirmation=True,
    )


def evaluation_evidence(value, *, risk=False):
    """Phase 1 bounded projection; no evaluator or provider response dump."""
    if not isinstance(value, str) or len(value) > 65536:
        raise ValueError("Evidence exceeds limit")
    obj = json.loads(value)
    allowed = {"outcome", "reason_codes"} if risk else {"result", "condition_ids"}
    if not isinstance(obj, dict) or not set(obj) <= allowed:
        raise ValueError("Unapproved evaluation evidence field")
    for key, item in obj.items():
        if key in ("result", "outcome"):
            values = {"NOT_RUN", "REJECTED", "ACCEPTED"} if risk else {"TRUE", "FALSE", "UNAVAILABLE", "INVALID"}
            if item not in values:
                raise ValueError("Invalid evidence status")
        else:
            if not isinstance(item, list) or len(item) > 256:
                raise ValueError("Invalid evidence collection")
            for code in item:
                if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code):
                    raise ValueError("Invalid evidence code")
    return canonical_json(obj)
