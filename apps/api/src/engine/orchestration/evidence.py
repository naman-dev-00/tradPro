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


def consent_fingerprint(*, owner_id, consent_at, snapshot_fingerprint,
                        source_type, replay_open_at, replay_close_at, execution_policy):
    return _fingerprint("orchestration_consent_v1", dict(
        consent_policy_version="fixture_consent_v1", confirmed_user_id=owner_id,
        confirmed_at=consent_at, snapshot_fingerprint=snapshot_fingerprint,
        source_type=source_type, approved_replay_open_at=replay_open_at,
        approved_replay_close_at=replay_close_at, execution_policy=execution_policy,
    ))


def config_consent_fingerprint(config):
    return consent_fingerprint(**{key: getattr(config, key) for key in (
        "owner_id", "consent_at", "snapshot_fingerprint", "source_type",
        "replay_open_at", "replay_close_at", "execution_policy",
    )})


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
