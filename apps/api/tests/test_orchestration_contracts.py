from datetime import timedelta, timezone
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from src.engine.orchestration.candle_source import accept_completed_candle as _accept_completed_candle, scaled_units
from src.engine.orchestration.fingerprint import canonical_json, orchestration_snapshot_v1, runtime_evaluation_v1
from src.engine.orchestration.models import CompletedCandle, OrchestrationSnapshot, RuntimeEvaluationIdentity
from tests.orchestration_support import OPEN, CLOSE, NOW, candle_payload, snapshot_payload, domain_objects


def accept_completed_candle(payload, *, snapshot=None, clock):
    if snapshot is None:
        material = snapshot_payload()
        if payload.get("timeframe") in ("5m", "15m"):
            material["timeframe"] = payload["timeframe"]
        snapshot = OrchestrationSnapshot(**material)
    return _accept_completed_candle(payload, snapshot=snapshot, clock=clock)


@pytest.mark.parametrize("change", [
    {"is_closed": False}, {"is_closed": 1}, {"open_at": OPEN.replace(tzinfo=None)},
    {"close_at": CLOSE.replace(tzinfo=None)}, {"received_at": NOW.replace(tzinfo=None)},
    {"close_at": OPEN}, {"close_at": CLOSE + timedelta(seconds=1)},
    {"open_at": OPEN + timedelta(seconds=1), "close_at": CLOSE + timedelta(seconds=1)},
    {"received_at": OPEN}, {"received_at": NOW + timedelta(seconds=1)},
    {"open_units": 0}, {"volume_units": -1}, {"high_units": 95}, {"low_units": 111},
    {"close_units": 9_000_000_000_000_001}, {"open_units": float("nan")},
    {"open_units": float("inf")}, {"open_units": 1.0}, {"open_units": True},
    {"source_event_id": "x" * 101}, {"source_namespace": ""}, {"owner_id": "x" * 37},
    {"dataset_checksum": "z" * 64}, {"revision": 2}, {"revision": True},
    {"timeframe": "1d"}, {"source_type": "REALTIME_PROVIDER"}, {"price_scale": 9},
    {"series_role": "EXECUTION"}, {"unexpected": 1}, {"open_at": 0}, {"open_at": "0"},
])
def test_reject_invalid_candle(change):
    with pytest.raises((ValueError, ValidationError)):
        accept_completed_candle(candle_payload() | change, clock=lambda: NOW)


def test_clock_and_exact_close_boundary():
    payload = candle_payload() | {"received_at": CLOSE}
    assert accept_completed_candle(payload, clock=lambda: CLOSE).close_at == CLOSE
    with pytest.raises(ValueError):
        accept_completed_candle(payload, clock=lambda: OPEN)
    with pytest.raises(ValueError):
        accept_completed_candle(payload, clock=lambda: NOW.replace(tzinfo=None))
    future = {key: value + timedelta(days=1) if key.endswith("_at") else value for key, value in payload.items()}
    with pytest.raises(ValueError, match="Future"):
        accept_completed_candle(future, clock=lambda: NOW)


@pytest.mark.parametrize("timeframe,minutes", [("5m", 5), ("15m", 15)])
def test_supported_timeframe_alignment(timeframe, minutes):
    start = OPEN.replace(hour=8, minute=0)
    candle = accept_completed_candle(candle_payload() | {"timeframe": timeframe, "open_at": start, "close_at": start + timedelta(minutes=minutes)}, clock=lambda: NOW)
    assert candle.timeframe == timeframe


@pytest.mark.parametrize("value", [1.0, float("nan"), Decimal("1"), {1: "not-string"}, OPEN.replace(tzinfo=None)])
def test_canonical_representation_rejects_implicit_conversions(value):
    with pytest.raises(ValueError):
        canonical_json(value)


@pytest.mark.parametrize("value,scale,expected", [("1.25", 2, 125), (Decimal("0.00000001"), 8, 1), ("1e2", 0, 100), (0, 0, 0), ("90000000000000", 2, 9_000_000_000_000_000)])
def test_exact_scaled_conversion(value, scale, expected):
    with localcontext() as context:
        context.prec = 2
        assert scaled_units(value, scale) == expected


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", float("nan"), float("inf"), 1.25, True, "0.001", "1e1000", "1e-999999999", "9" * 101, "bad"])
def test_conversion_rejects_nonfinite_inexact_or_oversized(value):
    with pytest.raises(ValueError):
        scaled_units(value, 2)


@pytest.mark.parametrize("scale", [-1, 9, True, 2.0])
def test_conversion_scale_bounds(scale):
    with pytest.raises(ValueError):
        scaled_units("1", scale)


def test_domain_is_deeply_immutable():
    snapshot, candle, identity = domain_objects()
    for obj, field, value in [(snapshot, "strategy_version", 2), (candle, "volume_units", 2), (identity, "owner_id", "other")]:
        with pytest.raises(ValidationError):
            setattr(obj, field, value)
    assert isinstance(snapshot.strategy_snapshot, str)
    assert isinstance(snapshot.datasets, tuple)
    with pytest.raises(ValidationError):
        snapshot.datasets[0].dataset_id = "different"


def test_snapshot_policy_and_evidence_bounds():
    for change in [
        {"external_transmission_allowed": True}, {"execution_policy": "EXTERNAL"},
        {"source_type": "REALTIME_PROVIDER"}, {"strategy_snapshot": {"x": float("nan")}},
        {"strategy_snapshot": {"x": "a" * 65536}}, {"replay_close_at": OPEN},
        {"datasets": []}, {"provider_mapping": {"mapping_id": "m", "mapping_version": 1, "verification_state": "DISABLED", "expiry_at": None}},
    ]:
        with pytest.raises(ValueError):
            OrchestrationSnapshot(**(snapshot_payload() | change))


def test_canonical_key_order_and_timestamp_normalization():
    assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})
    snapshot, candle, identity = domain_objects()
    offset = timezone(timedelta(hours=5, minutes=30))
    changed = candle_payload() | {key: value.astimezone(offset) for key, value in candle_payload().items() if key.endswith("_at")}
    assert CompletedCandle(**changed).content_fingerprint == candle.content_fingerprint
    payload = snapshot_payload()
    payload["strategy_snapshot"] = dict(reversed(list(payload["strategy_snapshot"].items())))
    assert orchestration_snapshot_v1(OrchestrationSnapshot(**payload)) == orchestration_snapshot_v1(snapshot)
    equivalent = snapshot_payload() | {"replay_open_at": OPEN.astimezone(offset), "replay_close_at": NOW.astimezone(offset)}
    assert orchestration_snapshot_v1(OrchestrationSnapshot(**equivalent)) == orchestration_snapshot_v1(snapshot)
    for field in ("replay_open_at", "replay_close_at"):
        with pytest.raises(ValueError, match="Naive"):
            OrchestrationSnapshot(**(snapshot_payload() | {field: OPEN.replace(tzinfo=None)}))
    assert runtime_evaluation_v1(RuntimeEvaluationIdentity(**(identity.model_dump() | {"close_at": CLOSE.astimezone(offset)}))) == runtime_evaluation_v1(identity)


def test_delivery_aliases_do_not_change_content():
    candle = CompletedCandle(**candle_payload())
    other = CompletedCandle(**(candle_payload() | {"source_event_id": "another", "received_at": NOW + timedelta(seconds=1)}))
    assert other.content_fingerprint == candle.content_fingerprint


def test_identity_separation_and_ordered_series():
    snapshot, candle, identity = domain_objects()
    for key, value in [("owner_id", "other"), ("runtime_id", "other"), ("strategy_version", 2), ("engine_version", "2")]:
        changed = OrchestrationSnapshot(**(snapshot_payload() | {key: value}))
        assert orchestration_snapshot_v1(changed) != orchestration_snapshot_v1(snapshot)
    for key, value in [("owner_id", "other"), ("runtime_id", "other"), ("snapshot_fingerprint", "b" * 64), ("mapping_id", "other"), ("mapping_version", 2), ("close_at", NOW)]:
        changed = RuntimeEvaluationIdentity(**(identity.model_dump() | {key: value}))
        assert runtime_evaluation_v1(changed) != runtime_evaluation_v1(identity)
    for key in ("owner_id", "runtime_id", "source_namespace"):
        assert CompletedCandle(**(candle_payload() | {key: "other"})).content_fingerprint != candle.content_fingerprint
    items = list(identity.model_dump()["required_candles"])
    items.append(items[0] | {"series_role": "SUBJECT", "content_fingerprint": "b" * 64})
    first = RuntimeEvaluationIdentity(**(identity.model_dump() | {"required_candles": items}))
    second = RuntimeEvaluationIdentity(**(identity.model_dump() | {"required_candles": list(reversed(items))}))
    assert runtime_evaluation_v1(first) != runtime_evaluation_v1(second)


def test_golden_vectors():
    snapshot, candle, identity = domain_objects()
    assert orchestration_snapshot_v1(snapshot) == "44ac537bfb45f26564d11f4c47bf0688f4644239b4945658df10a73502cb0568"
    assert candle.content_fingerprint == "a0806f39e230d998541a1f6fc585606493e0bdc3951cf7b30463fc07b88ad3ef"
    assert runtime_evaluation_v1(identity) == "6f5a5eaa49b076dca1521cd1e18a0583a5340ee1c565bcc2c89a72202728972a"


@pytest.mark.parametrize("offset", [-1, 900, 901, True, 1.0, "0"])
def test_invalid_source_offsets(offset):
    for model, payload in [(CompletedCandle, candle_payload()), (OrchestrationSnapshot, snapshot_payload())]:
        with pytest.raises(ValueError):
            model(**(payload | {"alignment_offset_seconds": offset}))


def test_nonzero_alignment_is_frozen_and_not_epoch_alignment():
    from src.engine.orchestration.models import require_alignment
    shift = timedelta(seconds=60)
    snapshot = OrchestrationSnapshot(**(snapshot_payload() | {
        "alignment_offset_seconds": 60, "replay_open_at": OPEN + shift,
        "replay_close_at": NOW + shift,
    }))
    payload = candle_payload() | {"alignment_offset_seconds": 60, "open_at": OPEN + shift, "close_at": CLOSE + shift}
    candle = accept_completed_candle(payload, snapshot=snapshot, clock=lambda: NOW)
    assert candle.open_at.timestamp() % 900 == 60
    with pytest.raises(ValueError, match="source offset"):
        CompletedCandle(**(candle_payload() | {"alignment_offset_seconds": 60}))
    with pytest.raises(ValueError, match="frozen source"):
        accept_completed_candle(candle_payload(), snapshot=snapshot, clock=lambda: NOW)
    assert orchestration_snapshot_v1(snapshot) != orchestration_snapshot_v1(OrchestrationSnapshot(**snapshot_payload()))
    # Isolate offset's contribution to hashing; model_copy deliberately bypasses
    # validation here only, and must never be used as a persistence constructor.
    assert orchestration_snapshot_v1(snapshot.model_copy(update={"alignment_offset_seconds": 61})) != orchestration_snapshot_v1(snapshot)
    # Pure arithmetic only: 30m/1h have no operational fixtures in Phase 1.
    for duration in (1800, 3600):
        require_alignment(OPEN, duration, int(OPEN.timestamp()) % duration)
        with pytest.raises(ValueError):
            require_alignment(OPEN, duration, 0)


@pytest.mark.parametrize("timeframe", ["1m", "3m", "30m", "1h"])
def test_unapproved_timeframes_are_rejected(timeframe):
    with pytest.raises(ValueError):
        CompletedCandle(**(candle_payload() | {"timeframe": timeframe}))


def test_all_packaged_fixture_alignments():
    from src.engine.manifest import get_dataset_manifest, load_dataset_candles
    from src.engine.orchestration.models import TIMEFRAME_SECONDS, require_alignment
    from src.engine.orchestration.source_policy import packaged_alignment
    assert {entry.timeframe for entry in get_dataset_manifest()} == set(TIMEFRAME_SECONDS)
    for entry in get_dataset_manifest():
        policy = packaged_alignment(entry.dataset_id)
        for candle in load_dataset_candles(entry.dataset_id):
            require_alignment(candle.timestamp, TIMEFRAME_SECONDS[entry.timeframe], policy.offset_seconds)


def test_snapshot_factory_derives_identity_and_source_policy():
    from types import SimpleNamespace
    from src.engine.manifest import get_dataset_entry
    from src.engine.orchestration.source_policy import freeze_packaged_snapshot
    entry = get_dataset_entry("synthetic_underlying_nifty_15m")
    material = snapshot_payload()
    for field in ("owner_id", "runtime_id", "alignment_offset_seconds", "source_namespace"):
        material.pop(field)
    material["datasets"] = [dict(dataset_id=entry.dataset_id, checksum=entry.dataset_checksum,
                                instrument_id=entry.instrument_id, series_role="REFERENCE")]
    user = SimpleNamespace(id="owner", is_active=True)
    runtime = SimpleNamespace(id="runtime", owner_id="owner", timeframe="15m")
    snapshot = freeze_packaged_snapshot(confirmed_user=user, runtime=runtime, material=material)
    assert snapshot.alignment_offset_seconds == 0
    from src.engine.orchestration.source_policy import prepare_configuration
    from src.engine.orchestration.evidence import config_consent_fingerprint
    config = prepare_configuration(confirmed_user=user, runtime=runtime, material=material, confirmed=True, clock=lambda: NOW)
    assert config.owner_id == user.id
    assert config.consent_at == NOW
    assert config.consent_fingerprint == config_consent_fingerprint(config)
    for confirmation in (False, 1, "yes"):
        with pytest.raises(ValueError, match="consent"):
            prepare_configuration(confirmed_user=user, runtime=runtime, material=material, confirmed=confirmation, clock=lambda: NOW)
    for field, value in [("owner_id", "other"), ("alignment_offset_seconds", 60)]:
        with pytest.raises(ValueError, match="server-derived"):
            freeze_packaged_snapshot(confirmed_user=user, runtime=runtime, material=material | {field: value})
    with pytest.raises(ValueError, match="own"):
        freeze_packaged_snapshot(confirmed_user=SimpleNamespace(id="other", is_active=True), runtime=runtime, material=material)


def test_snapshot_golden_material_is_exact():
    import hashlib
    import json
    from pathlib import Path
    snapshot, _, _ = domain_objects()
    payload = snapshot.model_dump(mode="python")
    for field in ("strategy_snapshot", "action_policy_snapshot", "risk_policy_snapshot", "instrument_specification"):
        payload[field] = json.loads(payload[field])
    expected = Path(__file__).resolve().parents[3] / "docs/orchestration_snapshot_v1_golden.json"
    material = expected.read_text().rstrip("\n")
    assert material == canonical_json({"algorithm": "orchestration_snapshot_v1", "payload": payload})
    assert hashlib.sha256(material.encode()).hexdigest() == orchestration_snapshot_v1(snapshot)


@pytest.mark.parametrize("evidence", [
    {"authorization": "redacted"}, {"cookie": "redacted"}, {"request_body": {}},
    {"name": "a\nb"}, {"name": "Bearer test"}, {"name": "Traceback test"},
    {"name": "a" * 101}, {"conditions": [{"token": "test"}]},
])
def test_snapshot_rejects_transport_secrets_controls_and_unbounded_text(evidence):
    with pytest.raises(ValueError):
        OrchestrationSnapshot(**(snapshot_payload() | {"strategy_snapshot": evidence}))
