"""Small deterministic Phase 1 fixtures, independent of application services."""
from datetime import datetime, timezone

from src.engine.orchestration.models import OrchestrationSnapshot, RuntimeEvaluationIdentity
from src.engine.orchestration.candle_source import accept_completed_candle
from src.engine.orchestration.fingerprint import canonical_json, orchestration_snapshot_v1, runtime_evaluation_v1
from src.models import (
    User, Strategy, PaperAccount, StrategyActionPolicy, RiskPolicy, StrategyRuntime,
    RuntimeOrchestrationConfig, CompletedCandleEvent, RuntimeEvaluation,
)

OPEN = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
CLOSE = datetime(2026, 8, 28, 9, 30, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)


def snapshot_payload(owner="owner", runtime="runtime"):
    return dict(
        owner_id=owner, runtime_id=runtime, strategy_version=1,
        strategy_snapshot={"timeframe": "15m", "name": "Deterministic fixture"},
        action_policy_snapshot={"quantity_units": 50}, risk_policy_snapshot={"max_open_orders": 1},
        instrument_specification={"price_scale": 2, "instrument_id": "NIFTY"},
        provider_mapping={"mapping_id": "mapping", "mapping_version": 1, "verification_state": "VERIFIED", "expiry_at": None},
        source_namespace="packaged:v1", datasets=[{"dataset_id": "fixture", "checksum": "a" * 64, "instrument_id": "NIFTY", "series_role": "REFERENCE"}],
        timeframe="15m", alignment_offset_seconds=0, replay_open_at=OPEN, replay_close_at=NOW,
        engine_version="1.0.0", indicator_engine_version="1.0.0",
    )


def candle_payload(owner="owner", runtime="runtime"):
    return dict(
        owner_id=owner, runtime_id=runtime, source_namespace="packaged:v1", source_event_id="row:1",
        dataset_id="fixture", dataset_checksum="a" * 64, instrument_id="NIFTY", timeframe="15m",
        series_role="REFERENCE", alignment_offset_seconds=0, open_at=OPEN, close_at=CLOSE, received_at=NOW,
        price_scale=2, volume_scale=0, open_units=100, high_units=120, low_units=90,
        close_units=110, volume_units=1000, is_closed=True,
    )


def domain_objects(owner="owner", runtime="runtime", *, packaged=False):
    snapshot_data = snapshot_payload(owner, runtime)
    candle_data = candle_payload(owner, runtime)
    if packaged:
        from src.engine.manifest import get_dataset_entry
        entry = get_dataset_entry("synthetic_underlying_nifty_15m")
        snapshot_data["datasets"] = [dict(dataset_id=entry.dataset_id, checksum=entry.dataset_checksum,
                                           instrument_id=entry.instrument_id, series_role="REFERENCE")]
        candle_data.update(dataset_id=entry.dataset_id, dataset_checksum=entry.dataset_checksum)
    snapshot = OrchestrationSnapshot(**snapshot_data)
    candle = accept_completed_candle(candle_data, snapshot=snapshot, clock=lambda: NOW)
    identity = RuntimeEvaluationIdentity(
        owner_id=owner, runtime_id=runtime, snapshot_fingerprint=orchestration_snapshot_v1(snapshot),
        mapping_id="mapping", mapping_version=1, timeframe="15m", close_at=CLOSE,
        required_candles=[dict(series_role="REFERENCE", dataset_id=candle.dataset_id, instrument_id="NIFTY", content_fingerprint=candle.content_fingerprint)],
    )
    return snapshot, candle, identity


def seed_parent(session, suffix=""):
    owner, runtime = "owner" + suffix, "runtime" + suffix
    session.add(User(id=owner, username=owner, normalized_username=owner, email=owner + "@test.invalid", normalized_email=owner + "@test.invalid", hashed_password="unused-test-hash", role="EDITOR", is_active=True))
    session.flush()
    session.add_all([
        Strategy(id="strategy" + suffix, owner_id=owner, name="fixture", timeframe="15m", candidate_selection_mode="FIRST_ELIGIBLE", payload={}),
        PaperAccount(id="account" + suffix, owner_id=owner, name="fixture", currency="INR", total_cash_units=100000, reserved_cash_units=0),
        RiskPolicy(id="risk" + suffix, owner_id=owner, name="fixture", version=1, payload={}),
    ])
    session.flush()
    session.add(StrategyActionPolicy(id="action" + suffix, owner_id=owner, strategy_id="strategy" + suffix, name="fixture", version=1, payload={}))
    session.flush()
    session.add(StrategyRuntime(id=runtime, owner_id=owner, strategy_id="strategy" + suffix, action_policy_id="action" + suffix, risk_policy_id="risk" + suffix, account_id="account" + suffix, dataset_id="fixture", timeframe="15m", status="DRAFT"))
    session.flush()


def seed_graph(session, level=3):
    seed_parent(session)
    snapshot, candle, identity = domain_objects(packaged=True)
    config = RuntimeOrchestrationConfig(
        id="config", owner_id="owner", runtime_id="runtime", source_type="FIXTURE_REPLAY",
        source_namespace="packaged:v1", execution_policy="INTERNAL_MOCK_ONLY",
        snapshot_fingerprint=identity.snapshot_fingerprint, snapshot_json=canonical_json(snapshot.model_dump(mode="python")),
        consent_at=NOW, consent_policy_version="fixture_consent_v1", consent_fingerprint="a" * 64,
        source_policy_version="packaged_alignment_v1", alignment_offset_seconds=0, timeframe="15m",
        replay_open_at=OPEN, replay_close_at=NOW, created_at=NOW, updated_at=NOW,
    )
    from src.engine.orchestration.evidence import config_consent_fingerprint
    config.consent_fingerprint = config_consent_fingerprint(config)
    session.add(config)
    session.flush()
    if level < 2:
        return
    payload = candle.model_dump(mode="python")
    payload.pop("contract_version")
    session.add(CompletedCandleEvent(id="candle", content_fingerprint=candle.content_fingerprint, **payload))
    session.flush()
    if level < 3:
        return
    session.add(RuntimeEvaluation(
        id="evaluation", owner_id="owner", runtime_id="runtime", config_id="config",
        snapshot_fingerprint=identity.snapshot_fingerprint, evaluation_fingerprint=runtime_evaluation_v1(identity),
        timeframe="15m", close_at=CLOSE, reference_candle_id="candle",
        required_candles_json=canonical_json([item.model_dump(mode="python") for item in identity.required_candles]),
        evaluation_status="FALSE", action_outcome="NO_ACTION", risk_outcome="NOT_RUN",
        no_order_reason="RULE_FALSE", audit_json='{"result":"FALSE"}', risk_summary_json='{}', finalized_at=NOW,
    ))
    session.flush()
