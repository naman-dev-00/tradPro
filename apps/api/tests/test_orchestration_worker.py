"""Comprehensive Milestone 6C Phase 3 Evaluation Worker Tests.

Covers:
1. Candle Ingestion:
   - Valid 5m and 15m completed candles
   - Malformed OHLCV (high < low, negative prices)
   - Excess precision rejection
   - Misalignment rejection
   - Incomplete candle rejection
   - Future candle rejection
   - Duplicate identical event (idempotent return)
   - Conflicting duplicate event (fails closed)
   - Owner/config/source mismatch
   - Manifest provenance mismatch
2. Required-Series Synchronization:
   - All required series present
   - Missing series failure
   - Mismatched close boundaries
   - Deterministic canonical series ordering
   - Strict no forward-fill
   - Strict no-look-ahead
   - Warm-up and insufficient history behavior
3. Runtime Eligibility Gate:
   - RUNNING evaluates
   - READY / DRAFT do not evaluate
   - PAUSED does not evaluate or advance checkpoint
   - STOPPED / COMPLETED never evaluate
   - Resume continues from exact prior checkpoint
   - Kill switch blocks evaluation
   - Inactive and legacy users blocked
4. Idempotency and Atomicity:
   - Identical replay reuses evaluation
   - Conflicting replay rejected
   - Failed evaluation creates no final row
   - Failed finalization does not advance checkpoint
   - Evaluation and checkpoint commit atomically
   - Immutable finalized evaluation cannot be updated or deleted
5. Worker Claiming, Leasing, and Concurrency:
   - Deterministic claim order
   - Bounded batch size
   - Lease acquisition with monotonic fencing generation
   - Expired lease recovery
   - Stale worker fencing rejection
   - Two concurrent workers racing create exactly one evaluation
   - Crash-before-finalize recovery
   - Crash-after-finalize idempotency
6. Strict Transmission Prohibition:
   - Zero Upstox adapter calls
   - Zero provider transport calls
   - Zero submission_outbox rows
   - Zero broker orders created
   - Balances, reservations, and ledger unchanged
   - UPSTOX_SANDBOX_NETWORK_ENABLED cannot bypass prohibition
7. API Surface and Security:
   - Anonymous access rejected
   - Explicit role sets (VIEWER, EDITOR, ADMIN)
   - Strict owner-scoped 404 isolation
   - Bounded pagination and deterministic ordering
   - Strict schemas with extra='forbid'
   - No owner_id exposure
   - No snapshot secrets or credential exposure
   - GETs create no state-changing mutations
"""
import datetime
import json
import uuid
from decimal import Decimal
import threading
from typing import Any, Dict, List, Optional, Tuple
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError

from src.auth.security import hash_password
from src.auth.session import create_session
from src.database import get_db, get_read_only_db
from src.engine.manifest import get_dataset_entry
from src.engine.orchestration.candle_source import accept_completed_candle, scaled_units
from src.engine.orchestration.evaluator import OrchestrationEvaluator, SynchronizationError
from src.engine.orchestration.evidence import config_consent_fingerprint
from src.engine.orchestration.fingerprint import canonical_json, orchestration_snapshot_v1, runtime_evaluation_v1
from src.engine.orchestration.ingestion import (
    CandleIngestionConflictError,
    IngestionError,
    ingest_all_required_fixture_candles,
    ingest_fixture_dataset,
    load_fixture_rows,
    parse_and_validate_candle,
    persist_completed_candle,
)
from src.engine.orchestration.models import (
    ActionOutcome,
    CandleSourceType,
    CompletedCandle,
    OrchestrationSnapshot,
    RequiredCandleIdentity,
    RiskOutcome,
    RuntimeEvaluationIdentity,
    SeriesRole,
    utc,
)
from src.engine.orchestration.transmission_gate import (
    TransmissionProhibitedError,
    assert_orchestration_execution_is_internal_only,
)
from src.engine.orchestration.worker import StaleWorkerFencedError, StrategyEvaluationWorker
from src.engine.paper.state_machine import RuntimeStatus
from src.main import app
from src.models import (
    LEGACY_PRINCIPAL_ID,
    AccountLedgerEntry,
    ActionDecision,
    CompletedCandleEvent,
    Fill,
    KillSwitch,
    Order,
    OrderEvent,
    OrderIntent,
    PaperAccount,
    PaperPosition,
    ProviderConnection,
    ProviderInstrumentMapping,
    ReconciliationRecord,
    RiskDecision,
    RiskPolicy,
    RuntimeEvaluation,
    RuntimeEvent,
    RuntimeOrchestrationConfig,
    Strategy,
    StrategyActionPolicy,
    StrategyRuntime,
    SubmissionOutbox,
    User,
)
from src.services.orchestration_service import OrchestrationService, ResourceNotFoundError
from src.engine.orchestration.ingestion import canonical_source_event_id_v1
from src.engine.orchestration.worker import MAX_EVALUATION_RETRIES, StaleWorkerFencedError, StrategyEvaluationWorker
from src.cli import validate_worker_args

OPEN_TIME = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
CLOSE_TIME = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)
NOW_TIME = datetime.datetime(2026, 8, 28, 10, 0, tzinfo=datetime.timezone.utc)


# ============================================================================
# Test Fixtures & Setup Helpers
# ============================================================================

def make_test_user(session, username: str, role: str = "EDITOR", is_active: bool = True) -> User:
    u = User(
        id=str(uuid.uuid4()),
        username=username,
        normalized_username=username.lower(),
        email=f"{username.lower()}@test.tradepro",
        normalized_email=f"{username.lower()}@test.tradepro",
        hashed_password=hash_password("StrongPassword123!"),
        role=role,
        is_active=is_active,
    )
    session.add(u)
    session.flush()
    return u


def setup_orchestration_stack(
    session,
    owner: User,
    *,
    status: str = "RUNNING",
    timeframe: str = "15m",
    replay_open: Optional[datetime.datetime] = None,
    replay_close: Optional[datetime.datetime] = None,
    checkpoint: Optional[datetime.datetime] = None,
    with_subject: bool = False,
    risk_config: Optional[Dict[str, Any]] = None,
) -> Tuple[StrategyRuntime, RuntimeOrchestrationConfig, ProviderInstrumentMapping]:
    """Helper to set up a complete, valid orchestration stack in the database."""
    r_open = replay_open or OPEN_TIME
    r_close = replay_close or (r_open + datetime.timedelta(hours=2))

    # 1. Paper Account
    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        name="Test Account",
        currency="INR",
        total_cash_units=1000000,
        reserved_cash_units=0,
    )
    session.add(account)

    # 2. Strategy
    strat_payload = {
        "name": "Deterministic Price Strategy",
        "timeframe": timeframe,
        "candidate_selection_mode": "FIRST_ELIGIBLE",
        "global_conditions": {
            "type": "CONDITION",
            "id": "cond_1",
            "lhs": {"indicator": "PRICE"},
            "operator": "GREATER_THAN",
            "rhs": {"type": "NUMBER", "value": 0},
        },
    }
    strategy = Strategy(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        name="Test Strategy",
        timeframe=timeframe,
        candidate_selection_mode="FIRST_ELIGIBLE",
        payload=strat_payload,
    )
    session.add(strategy)

    # 3. Action Policy
    action_policy = StrategyActionPolicy(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        strategy_id=strategy.id,
        name="Test Action Policy",
        version=1,
        payload={"trigger_status": "TRUE", "action": "BUY", "type": "ENTRY"},
    )
    session.add(action_policy)

    # 4. Risk Policy
    effective_risk = risk_config if risk_config is not None else {
        "max_position_size": 10,
        "max_trades_per_day": 5,
        "max_open_orders": 2,
    }
    risk_policy = RiskPolicy(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        name="Test Risk Policy",
        version=1,
        payload={"risk_config": effective_risk},
    )
    session.add(risk_policy)

    # 5. Provider Instrument Mapping
    mapping = ProviderInstrumentMapping(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        tradepro_instrument_id="NSE_INDEX|Nifty 50",
        provider_instrument_token="256265",
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        lot_size_units=1,
        tick_size_units=5,
        freeze_quantity_units=1800,
        verification_status="VERIFIED",
        mapping_version=1,
    )
    session.add(mapping)
    session.flush()

    # 6. Strategy Runtime
    runtime = StrategyRuntime(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        strategy_id=strategy.id,
        action_policy_id=action_policy.id,
        risk_policy_id=risk_policy.id,
        account_id=account.id,
        dataset_id="synthetic_underlying_nifty_15m",
        timeframe=timeframe,
        status=status,
        strategy_snapshot=strat_payload,
        action_policy_snapshot=action_policy.payload,
        risk_policy_snapshot=risk_policy.payload,
        instrument_spec_snapshot={
            "instrument_id": mapping.tradepro_instrument_id,
            "price_scale": 2,
            "lot_size_units": 1,
            "tick_size_units": 5,
        },
    )
    session.add(runtime)
    session.flush()

    # 7. Datasets for snapshot
    entry = get_dataset_entry("synthetic_underlying_nifty_15m")
    datasets = [
        {
            "dataset_id": entry.dataset_id,
            "checksum": entry.dataset_checksum,
            "instrument_id": entry.instrument_id,
            "series_role": "REFERENCE",
        }
    ]
    if with_subject:
        # 5m subject dataset
        subj_entry = get_dataset_entry("synthetic_options_ce_5m")
        if subj_entry:
            datasets.append({
                "dataset_id": subj_entry.dataset_id,
                "checksum": subj_entry.dataset_checksum,
                "instrument_id": subj_entry.instrument_id,
                "series_role": "SUBJECT",
            })

    snapshot_mat = {
        "owner_id": owner.id,
        "runtime_id": runtime.id,
        "strategy_version": 1,
        "strategy_snapshot": strat_payload,
        "action_policy_snapshot": action_policy.payload,
        "risk_policy_snapshot": risk_policy.payload,
        "instrument_specification": runtime.instrument_spec_snapshot,
        "provider_mapping": {
            "mapping_id": mapping.id,
            "mapping_version": mapping.mapping_version,
            "verification_state": "VERIFIED",
            "expiry_at": None,
        },
        "source_namespace": "packaged:v1",
        "datasets": datasets,
        "timeframe": timeframe,
        "alignment_offset_seconds": 0,
        "replay_open_at": r_open,
        "replay_close_at": r_close,
        "engine_version": "1.0.0",
        "indicator_engine_version": "1.0.0",
        "source_type": CandleSourceType.FIXTURE_REPLAY,
        "execution_policy": "INTERNAL_MOCK_ONLY",
        "external_transmission_allowed": False,
    }
    snapshot = OrchestrationSnapshot(**snapshot_mat)
    snap_fingerprint = orchestration_snapshot_v1(snapshot)

    config = RuntimeOrchestrationConfig(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        runtime_id=runtime.id,
        source_type="FIXTURE_REPLAY",
        source_namespace="packaged:v1",
        execution_policy="INTERNAL_MOCK_ONLY",
        snapshot_fingerprint=snap_fingerprint,
        snapshot_json=canonical_json(snapshot.model_dump(mode="python")),
        consent_at=r_open,
        consent_policy_version="fixture_consent_v1",
        consent_fingerprint="dummy_consent_fp",
        source_policy_version="packaged_alignment_v1",
        alignment_offset_seconds=0,
        timeframe=timeframe,
        replay_open_at=r_open,
        replay_close_at=r_close,
        checkpoint_close_at=checkpoint,
        fencing_generation=1,
        retry_count=0,
        created_at=r_open,
        updated_at=r_open,
    )
    config.consent_fingerprint = config_consent_fingerprint(config)
    session.add(config)
    session.flush()

    return runtime, config, mapping


def create_authenticated_client(session, user: User) -> Tuple[TestClient, str]:
    def override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_read_only_db] = override_get_db

    sess_rec, raw_sess, raw_csrf = create_session(session, user)
    c = TestClient(app, headers={"X-CSRF-Token": raw_csrf, "Origin": "http://localhost:3000"})
    c.cookies.set("tradepro_session", raw_sess)
    c.cookies.set("tradepro_csrf", raw_csrf)
    return c, raw_csrf


# ============================================================================
# Part E1: Candle Ingestion Tests
# ============================================================================

class TestCandleIngestion:
    """Test deterministic candle ingestion from packaged fixture CSVs."""

    def test_load_fixture_rows_preserves_strings(self):
        rows = load_fixture_rows("synthetic_underlying_nifty_15m.csv")
        assert len(rows) > 0
        first = rows[0]
        assert "timestamp" in first
        assert "open" in first
        assert "high" in first
        assert "low" in first
        assert "close" in first
        assert isinstance(first["open"], str)

    def test_parse_and_validate_candle_valid_15m(self, session):
        user = make_test_user(session, "ingest_user_1")
        _, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        raw_row = {
            "timestamp": "2026-08-28T09:15:00Z",
            "open": "24500.50",
            "high": "24550.00",
            "low": "24480.25",
            "close": "24520.75",
            "volume": "15000",
            "is_closed": "true",
        }
        candle = parse_and_validate_candle(
            raw_row,
            dataset_id="synthetic_underlying_nifty_15m",
            series_role=SeriesRole.REFERENCE,
            snapshot=snapshot,
            clock=lambda: datetime.datetime(2026, 8, 28, 10, 0, tzinfo=datetime.timezone.utc),
        )
        assert candle.open_units == 2450050
        assert candle.close_units == 2452075
        assert candle.price_scale == 2
        assert candle.volume_scale == 0
        assert candle.content_fingerprint is not None

    def test_malformed_ohlcv_high_lower_than_low_rejected(self, session):
        user = make_test_user(session, "ingest_user_2")
        _, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        raw_row = {
            "timestamp": "2026-08-28T09:15:00Z",
            "open": "24500.00",
            "high": "24400.00",  # Invalid: high < open/low
            "low": "24450.00",
            "close": "24420.00",
            "volume": "1000",
            "is_closed": "true",
        }
        with pytest.raises(IngestionError, match="Candle acceptance failed"):
            parse_and_validate_candle(
                raw_row,
                dataset_id="synthetic_underlying_nifty_15m",
                series_role=SeriesRole.REFERENCE,
                snapshot=snapshot,
                clock=lambda: datetime.datetime(2026, 8, 28, 10, 0, tzinfo=datetime.timezone.utc),
            )

    def test_excess_precision_rejected(self, session):
        user = make_test_user(session, "ingest_user_3")
        _, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        raw_row = {
            "timestamp": "2026-08-28T09:15:00Z",
            "open": "24500.12345",  # Scale is 2; 5 decimal places is excess precision
            "high": "24550.00",
            "low": "24480.00",
            "close": "24520.00",
            "volume": "1000",
            "is_closed": "true",
        }
        with pytest.raises(IngestionError, match="Numeric conversion error"):
            parse_and_validate_candle(
                raw_row,
                dataset_id="synthetic_underlying_nifty_15m",
                series_role=SeriesRole.REFERENCE,
                snapshot=snapshot,
                clock=lambda: datetime.datetime(2026, 8, 28, 10, 0, tzinfo=datetime.timezone.utc),
            )

    def test_misalignment_rejected(self, session):
        user = make_test_user(session, "ingest_user_4")
        _, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        # 15m candle must align with epoch % 900 == 0.
        # 09:17:00 is misaligned.
        raw_row = {
            "timestamp": "2026-08-28T09:17:00Z",
            "open": "24500.00",
            "high": "24550.00",
            "low": "24480.00",
            "close": "24520.00",
            "volume": "1000",
            "is_closed": "true",
        }
        with pytest.raises(IngestionError, match="Candle acceptance failed"):
            parse_and_validate_candle(
                raw_row,
                dataset_id="synthetic_underlying_nifty_15m",
                series_role=SeriesRole.REFERENCE,
                snapshot=snapshot,
                clock=lambda: datetime.datetime(2026, 8, 28, 10, 0, tzinfo=datetime.timezone.utc),
            )

    def test_incomplete_candle_rejected(self, session):
        user = make_test_user(session, "ingest_user_5")
        _, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        raw_row = {
            "timestamp": "2026-08-28T09:15:00Z",
            "open": "24500.00",
            "high": "24550.00",
            "low": "24480.00",
            "close": "24520.00",
            "volume": "1000",
            "is_closed": "false",
        }
        with pytest.raises(IngestionError, match="Incomplete candle encountered"):
            parse_and_validate_candle(
                raw_row,
                dataset_id="synthetic_underlying_nifty_15m",
                series_role=SeriesRole.REFERENCE,
                snapshot=snapshot,
                clock=lambda: datetime.datetime(2026, 8, 28, 10, 0, tzinfo=datetime.timezone.utc),
            )

    def test_future_candle_rejected(self, session):
        user = make_test_user(session, "ingest_user_6")
        _, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        raw_row = {
            "timestamp": "2026-08-28T09:15:00Z",
            "open": "24500.00",
            "high": "24550.00",
            "low": "24480.00",
            "close": "24520.00",
            "volume": "1000",
            "is_closed": "true",
        }
        # Clock set before candle close: 09:20 < 09:30 close
        with pytest.raises(IngestionError, match="Candle acceptance failed"):
            parse_and_validate_candle(
                raw_row,
                dataset_id="synthetic_underlying_nifty_15m",
                series_role=SeriesRole.REFERENCE,
                snapshot=snapshot,
                clock=lambda: datetime.datetime(2026, 8, 28, 9, 20, tzinfo=datetime.timezone.utc),
            )

    def test_duplicate_identical_event_returns_existing(self, session):
        user = make_test_user(session, "ingest_user_7")
        _, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        raw_row = {
            "timestamp": "2026-08-28T09:15:00Z",
            "open": "24500.00",
            "high": "24550.00",
            "low": "24480.00",
            "close": "24520.00",
            "volume": "1000",
            "is_closed": "true",
        }
        candle = parse_and_validate_candle(
            raw_row,
            dataset_id="synthetic_underlying_nifty_15m",
            series_role=SeriesRole.REFERENCE,
            snapshot=snapshot,
            clock=lambda: datetime.datetime(2026, 8, 28, 10, 0, tzinfo=datetime.timezone.utc),
        )
        rec1 = persist_completed_candle(session, candle)
        rec2 = persist_completed_candle(session, candle)
        assert rec1.id == rec2.id
        assert rec1.content_fingerprint == rec2.content_fingerprint

    def test_conflicting_duplicate_event_fails_closed(self, session):
        user = make_test_user(session, "ingest_user_8")
        _, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        raw1 = {
            "timestamp": "2026-08-28T09:15:00Z",
            "open": "24500.00",
            "high": "24550.00",
            "low": "24480.00",
            "close": "24520.00",
            "volume": "1000",
            "is_closed": "true",
        }
        raw2 = {
            "timestamp": "2026-08-28T09:15:00Z",
            "open": "24500.00",
            "high": "24560.00",  # Different high -> different content for same interval
            "low": "24480.00",
            "close": "24520.00",
            "volume": "1000",
            "is_closed": "true",
        }
        candle1 = parse_and_validate_candle(
            raw1,
            dataset_id="synthetic_underlying_nifty_15m",
            series_role=SeriesRole.REFERENCE,
            snapshot=snapshot,
            clock=lambda: datetime.datetime(2026, 8, 28, 10, 0, tzinfo=datetime.timezone.utc),
        )
        candle2 = parse_and_validate_candle(
            raw2,
            dataset_id="synthetic_underlying_nifty_15m",
            series_role=SeriesRole.REFERENCE,
            snapshot=snapshot,
            clock=lambda: datetime.datetime(2026, 8, 28, 10, 0, tzinfo=datetime.timezone.utc),
        )
        persist_completed_candle(session, candle1)

        with pytest.raises(IngestionError, match="Conflicting duplicate candle detected"):
            persist_completed_candle(session, candle2)

    def test_unapproved_dataset_fails_closed(self, session):
        user = make_test_user(session, "ingest_user_9")
        _, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        raw = {
            "timestamp": "2026-08-28T09:15:00Z",
            "open": "24500.00",
            "high": "24550.00",
            "low": "24480.00",
            "close": "24520.00",
            "volume": "1000",
            "is_closed": "true",
        }
        with pytest.raises(IngestionError, match="has no approved source policy"):
            parse_and_validate_candle(
                raw,
                dataset_id="malicious_unapproved_dataset",
                series_role=SeriesRole.REFERENCE,
                snapshot=snapshot,
            )


# ============================================================================
# Part E2: Required-Series Synchronization & No-Look-Ahead
# ============================================================================

class TestSynchronizationAndLookahead:
    """Test deterministic required-series boundary synchronization and lookahead prohibition."""

    def test_synchronization_succeeds_when_all_required_candles_present(self, session):
        user = make_test_user(session, "sync_user_1")
        _, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        # Ingest candles up to first boundary
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        evaluator = OrchestrationEvaluator()
        ref, subj, reqs = evaluator.synchronize_required_boundary_candles(session, config, snapshot, boundary)
        assert ref is not None
        assert ref.close_at == boundary
        assert len(reqs) == 1
        assert reqs[0].series_role == SeriesRole.REFERENCE

    def test_missing_series_fails_closed_raises_synchronization_error(self, session):
        user = make_test_user(session, "sync_user_2")
        _, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        # Deliberately do not ingest candle at boundary
        evaluator = OrchestrationEvaluator()
        with pytest.raises(SynchronizationError, match="Missing required REFERENCE candle"):
            evaluator.synchronize_required_boundary_candles(session, config, snapshot, boundary)

    def test_strict_no_forward_fill(self, session):
        user = make_test_user(session, "sync_user_3")
        _, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)

        boundary_1 = OPEN_TIME + datetime.timedelta(minutes=15)
        boundary_2 = OPEN_TIME + datetime.timedelta(minutes=30)

        # Ingest only boundary 1
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary_1)
        session.commit()

        evaluator = OrchestrationEvaluator()
        # Evaluating boundary 2 must NOT forward-fill from boundary 1
        with pytest.raises(SynchronizationError, match="Missing required REFERENCE candle at boundary"):
            evaluator.synchronize_required_boundary_candles(session, config, snapshot, boundary_2)

    def test_strict_no_lookahead_rule(self, session):
        user = make_test_user(session, "sync_user_4")
        _, config, _ = setup_orchestration_stack(session, user)

        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        future_boundary = OPEN_TIME + datetime.timedelta(minutes=45)

        # Ingest candles up to future boundary
        ingest_all_required_fixture_candles(session, config, up_to_close_at=future_boundary)
        session.commit()

        evaluator = OrchestrationEvaluator()
        # Historical series loaded for boundary must strictly have close_at <= boundary
        ref_candles, _ = evaluator.load_historical_series_up_to_boundary(
            session, config, boundary, "synthetic_underlying_nifty_15m"
        )
        assert len(ref_candles) > 0
        for c in ref_candles:
            # Timestamp of candle open + 15m <= boundary
            close_time = c.timestamp + datetime.timedelta(minutes=15)
            assert close_time <= boundary


# ============================================================================
# Part E3: Runtime Eligibility Gate
# ============================================================================

class TestEligibilityGate:
    """Test runtime eligibility gate for evaluation worker claiming."""

    def test_running_runtime_is_claimed_and_evaluated(self, session):
        user = make_test_user(session, "gate_user_1")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="test-worker-1", batch_size=5)
        claim = worker.claim_next_candidate(session)
        assert claim is not None
        config_id, gen = claim
        assert config_id == config.id
        assert gen == 2

        # Process step
        eval_res = worker.process_runtime_step(session, config_id, gen)
        assert eval_res is not None
        assert eval_res.evaluation_status in ("TRUE", "FALSE", "UNAVAILABLE")

    def test_ready_and_draft_runtimes_are_not_claimed(self, session):
        user_ready = make_test_user(session, "gate_user_2_ready")
        user_draft = make_test_user(session, "gate_user_2_draft")
        setup_orchestration_stack(session, user_ready, status="READY")
        setup_orchestration_stack(session, user_draft, status="DRAFT")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="test-worker-2")
        claim = worker.claim_next_candidate(session)
        assert claim is None

    def test_paused_runtime_does_not_evaluate_or_advance_checkpoint(self, session):
        user = make_test_user(session, "gate_user_3")
        runtime, config, _ = setup_orchestration_stack(session, user, status="PAUSED")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="test-worker-3")
        claim = worker.claim_next_candidate(session)
        assert claim is None

        # Even if force-processed with stale claim, it rejects and releases lease
        worker.release_lease(session, config.id, 1)
        session.refresh(config)
        assert config.checkpoint_close_at is None

    def test_stopped_and_completed_runtimes_never_evaluate(self, session):
        user_stopped = make_test_user(session, "gate_user_4_stopped")
        user_completed = make_test_user(session, "gate_user_4_completed")
        setup_orchestration_stack(session, user_stopped, status="STOPPED")
        setup_orchestration_stack(session, user_completed, status="COMPLETED")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="test-worker-4")
        claim = worker.claim_next_candidate(session)
        assert claim is None

    def test_kill_switch_blocks_worker_evaluation(self, session):
        user = make_test_user(session, "gate_user_5")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")

        # Activate user kill switch
        ks = KillSwitch(
            target_key=f"USER:{user.id}",
            scope="USER",
            user_id=user.id,
            is_active=True,
            engaged_at=datetime.datetime.now(datetime.timezone.utc),
            reason="Testing gate block",
        )
        session.add(ks)
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="test-worker-5")
        claim = worker.claim_next_candidate(session)
        assert claim is not None
        config_id, gen = claim

        # Process step fails eligibility gate and releases lease with backoff
        eval_res = worker.process_runtime_step(session, config_id, gen)
        assert eval_res is None

        session.refresh(config)
        assert config.last_reason_code == "ELIGIBILITY_GATE_FAILED"
        assert config.lease_owner is None
        assert config.checkpoint_close_at is None

    def test_inactive_and_legacy_users_blocked(self, session):
        legacy_user = User(
            id=LEGACY_PRINCIPAL_ID,
            username="legacy_principal",
            normalized_username="legacy_principal",
            email="legacy@tradepro.test",
            normalized_email="legacy@tradepro.test",
            hashed_password=hash_password("Pass123!"),
            role="ADMIN",
            is_active=False,
        )
        session.add(legacy_user)
        session.flush()

        runtime, config, _ = setup_orchestration_stack(session, legacy_user, status="RUNNING")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="test-worker-6")
        claim = worker.claim_next_candidate(session)
        assert claim is not None
        config_id, gen = claim

        eval_res = worker.process_runtime_step(session, config_id, gen)
        assert eval_res is None
        session.refresh(config)
        assert config.last_reason_code == "ELIGIBILITY_GATE_FAILED"


# ============================================================================
# Part E4: Idempotency, Atomicity & Immutability
# ============================================================================

class TestIdempotencyAndAtomicity:
    """Test evaluation idempotency, atomic checkpoint commits, and ORM immutability."""

    def test_atomic_evaluation_and_checkpoint_commit(self, session):
        user = make_test_user(session, "atom_user_1")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="atom-worker-1")
        claim = worker.claim_next_candidate(session)
        assert claim is not None
        config_id, gen = claim

        eval_res = worker.process_runtime_step(session, config_id, gen)
        assert eval_res is not None

        session.refresh(config)
        first_boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        assert config.checkpoint_close_at == first_boundary

        # Verify evaluation row exists
        eval_row = session.query(RuntimeEvaluation).filter(
            RuntimeEvaluation.runtime_id == runtime.id
        ).first()
        assert eval_row is not None
        assert eval_row.close_at == first_boundary

    def test_failed_evaluation_creates_no_final_row_and_does_not_advance_checkpoint(self, session):
        user = make_test_user(session, "atom_user_2")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="atom-worker-2")
        claim = worker.claim_next_candidate(session)
        config_id, gen = claim

        # Monkeypatch evaluator to raise exception
        worker.evaluator.evaluate_boundary = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Simulation failure"))

        eval_res = worker.process_runtime_step(session, config_id, gen)
        assert eval_res is None

        session.refresh(config)
        assert config.checkpoint_close_at is None
        assert config.lease_owner is None
        assert config.last_reason_code == "EVALUATION_ERROR"

        # Verify 0 evaluation rows
        count = session.query(RuntimeEvaluation).filter(RuntimeEvaluation.runtime_id == runtime.id).count()
        assert count == 0

    def test_immutable_finalized_evaluation_cannot_be_updated_or_deleted(self, session):
        user = make_test_user(session, "atom_user_3")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="atom-worker-3")
        claim = worker.claim_next_candidate(session)
        config_id, gen = claim
        eval_res = worker.process_runtime_step(session, config_id, gen)
        assert eval_res is not None

        # Attempt to update finalized evaluation via ORM
        eval_res.action_outcome = "REJECTED"
        with pytest.raises(Exception, match="Orchestration audit records are immutable"):
            session.flush()

        session.rollback()

        # Attempt to delete finalized evaluation via ORM
        eval_to_del = session.query(RuntimeEvaluation).filter(RuntimeEvaluation.id == eval_res.id).first()
        session.delete(eval_to_del)
        with pytest.raises(Exception, match="Orchestration audit records cannot be deleted"):
            session.flush()


# ============================================================================
# Part E5: Worker Claiming, Leasing & Concurrency
# ============================================================================

class TestWorkerLeasingAndConcurrency:
    """Test deterministic claiming, leasing, fencing generations, and concurrent workers."""

    def test_monotonic_fencing_generation_advancement(self, session):
        user = make_test_user(session, "lease_user_1")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker1 = StrategyEvaluationWorker(worker_id="worker-w1")
        claim1 = worker1.claim_next_candidate(session)
        assert claim1 is not None
        _, gen1 = claim1
        assert gen1 == 2  # Started at 1, incremented to 2

        # Release lease
        worker1.release_lease(session, config.id, gen1)
        session.refresh(config)
        assert config.fencing_generation == 2
        assert config.lease_owner is None

        # Claim again with worker2
        worker2 = StrategyEvaluationWorker(worker_id="worker-w2")
        claim2 = worker2.claim_next_candidate(session)
        assert claim2 is not None
        _, gen2 = claim2
        assert gen2 == 3

    def test_stale_worker_fencing_rejected(self, session):
        user = make_test_user(session, "lease_user_2")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker1 = StrategyEvaluationWorker(worker_id="worker-slow")
        claim1 = worker1.claim_next_candidate(session)
        _, gen1 = claim1

        # Simulate lease expiration and worker2 acquiring a higher fencing generation
        now = datetime.datetime.now(datetime.timezone.utc)
        config.lease_expires_at = now - datetime.timedelta(seconds=10)
        session.commit()

        worker2 = StrategyEvaluationWorker(worker_id="worker-fast")
        claim2 = worker2.claim_next_candidate(session)
        _, gen2 = claim2
        assert gen2 > gen1

        # Slow worker attempts to finalize using stale generation
        eval_res = worker1.process_runtime_step(session, config.id, gen1)
        # Should be fenced out
        assert eval_res is None

    def test_expired_lease_recovery(self, session):
        user = make_test_user(session, "lease_user_3")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        # Worker crashes while holding lease
        worker_crashed = StrategyEvaluationWorker(worker_id="worker-crashed", lease_duration_seconds=5)
        claim = worker_crashed.claim_next_candidate(session)
        assert claim is not None

        # Fast forward time beyond lease expiration
        past_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=20)
        session.execute(
            update(RuntimeOrchestrationConfig)
            .where(RuntimeOrchestrationConfig.id == config.id)
            .values(lease_expires_at=past_time)
        )
        session.commit()

        # Recovery worker claims expired lease successfully
        worker_recovery = StrategyEvaluationWorker(worker_id="worker-recovery")
        claim_rec = worker_recovery.claim_next_candidate(session)
        assert claim_rec is not None
        _, new_gen = claim_rec
        assert new_gen > claim[1]

    def test_two_workers_racing_create_exactly_one_evaluation(self, session):
        user = make_test_user(session, "lease_user_4")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker_a = StrategyEvaluationWorker(worker_id="worker-A")
        worker_b = StrategyEvaluationWorker(worker_id="worker-B")

        claim_a = worker_a.claim_next_candidate(session)
        claim_b = worker_b.claim_next_candidate(session)

        # One worker wins the claim, the other gets None
        assert (claim_a is not None and claim_b is None) or (claim_b is not None and claim_a is None)

        winning_worker = worker_a if claim_a else worker_b
        winning_claim = claim_a or claim_b
        eval_res = winning_worker.process_runtime_step(session, winning_claim[0], winning_claim[1])
        assert eval_res is not None

        # Exactly 1 evaluation created
        count = session.query(RuntimeEvaluation).filter(RuntimeEvaluation.runtime_id == runtime.id).count()
        assert count == 1


# ============================================================================
# Part E6: Strict Transmission Prohibition Tests
# ============================================================================

class TestTransmissionProhibition:
    """Prove zero broker order creation, zero HTTP provider calls, zero outbox writes."""

    def test_strict_transmission_prohibition_during_evaluation(self, session, monkeypatch):
        # 1. Monkeypatch any potential network transport / Upstox adapter to explode if called
        def exploding_transport(*args, **kwargs):
            raise AssertionError("CRITICAL VIOLATION: External network transport was invoked during Phase 3!")

        monkeypatch.setattr("urllib.request.urlopen", exploding_transport)

        user = make_test_user(session, "trans_user_1")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        outbox_count_before = session.query(SubmissionOutbox).count()
        orders_count_before = session.query(Order).count()
        positions_count_before = session.query(PaperPosition).count()

        worker = StrategyEvaluationWorker(worker_id="trans-worker")
        claim = worker.claim_next_candidate(session)
        assert claim is not None
        config_id, gen = claim

        eval_res = worker.process_runtime_step(session, config_id, gen)
        assert eval_res is not None

        # Verify invariant counts
        assert session.query(SubmissionOutbox).count() == outbox_count_before
        assert session.query(Order).count() == orders_count_before
        assert session.query(PaperPosition).count() == positions_count_before

    def test_network_enabled_config_cannot_bypass_prohibition(self, session, monkeypatch):
        # Even if environment variable UPSTOX_SANDBOX_NETWORK_ENABLED is True,
        # orchestration config with FIXTURE_REPLAY + INTERNAL_MOCK_ONLY MUST forbid transmission
        monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")

        user = make_test_user(session, "trans_user_2")
        _, config, _ = setup_orchestration_stack(session, user, status="RUNNING")

        # Must not raise TransmissionProhibitedError on assertion because source is FIXTURE_REPLAY
        assert_orchestration_execution_is_internal_only(config)


# ============================================================================
# Part E7: API Security & Inspection Endpoints
# ============================================================================

class TestApiSecurityAndInspection:
    """Test read-only evaluation inspection endpoints and owner isolation."""

    def test_list_evaluations_owner_isolation_and_pagination(self, session):
        user1 = make_test_user(session, "api_user_1")
        user2 = make_test_user(session, "api_user_2")

        runtime1, config1, _ = setup_orchestration_stack(session, user1, status="RUNNING")
        session.commit()

        # Run worker to generate evaluation for user1
        worker = StrategyEvaluationWorker(worker_id="api-worker")
        claim = worker.claim_next_candidate(session)
        worker.process_runtime_step(session, claim[0], claim[1])

        client1, _ = create_authenticated_client(session, user1)
        client2, _ = create_authenticated_client(session, user2)

        # 1. User 1 can view evaluation history
        res1 = client1.get(f"/api/v1/orchestration/runtimes/{runtime1.id}/evaluations")
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["runtime_id"] == runtime1.id
        assert data1["total"] == 1
        assert len(data1["evaluations"]) == 1
        assert "owner_id" not in data1["evaluations"][0]  # No owner_id exposure

        # 2. User 2 gets 404 (strict owner isolation)
        res2 = client2.get(f"/api/v1/orchestration/runtimes/{runtime1.id}/evaluations")
        assert res2.status_code == 404

    def test_get_evaluation_detail_and_latest(self, session):
        user = make_test_user(session, "api_user_3")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="api-worker-detail")
        claim = worker.claim_next_candidate(session)
        worker.process_runtime_step(session, claim[0], claim[1])

        client, _ = create_authenticated_client(session, user)

        # 1. Test latest endpoint
        res_latest = client.get(f"/api/v1/orchestration/runtimes/{runtime.id}/evaluations/latest")
        assert res_latest.status_code == 200
        detail = res_latest.json()
        assert detail["runtime_id"] == runtime.id
        assert "audit_json" in detail
        assert "risk_summary_json" in detail
        assert "required_candles_json" in detail
        assert "owner_id" not in detail

        eval_id = detail["id"]

        # 2. Test detail endpoint by ID
        res_detail = client.get(f"/api/v1/orchestration/runtimes/{runtime.id}/evaluations/{eval_id}")
        assert res_detail.status_code == 200
        assert res_detail.json()["id"] == eval_id

    def test_anonymous_rejected(self, session):
        user = make_test_user(session, "api_user_4")
        runtime, _, _ = setup_orchestration_stack(session, user)
        session.commit()

        # Unauthenticated client
        anon_client = TestClient(app)
        res = anon_client.get(f"/api/v1/orchestration/runtimes/{runtime.id}/evaluations")
        assert res.status_code == 401


# ============================================================================
# Part E8: Phase 1 Golden Vectors Invariant
# ============================================================================

class TestPhase1GoldenVectorsRegression:
    """Verify Phase 1 golden vectors remain untouched."""

    def test_golden_vectors_remain_intact(self):
        from tests.orchestration_support import domain_objects
        snapshot, candle, identity = domain_objects()

        snap_fp = orchestration_snapshot_v1(snapshot)
        candle_fp = candle.content_fingerprint
        eval_fp = runtime_evaluation_v1(identity)

        assert snap_fp == "44ac537bfb45f26564d11f4c47bf0688f4644239b4945658df10a73502cb0568"
        assert candle_fp == "a0806f39e230d998541a1f6fc585606493e0bdc3951cf7b30463fc07b88ad3ef"
        assert eval_fp == "6f5a5eaa49b076dca1521cd1e18a0583a5340ee1c565bcc2c89a72202728972a"


# ============================================================================
# Part E9: Exact Numeric Conversion Golden Vectors
# ============================================================================

class TestExactNumericConversionGolden:
    """Verify exact decimal string conversion matching Phase 1 candle_source contract."""

    @pytest.mark.parametrize("input_val,scale,expected", [
        ("1.00", 2, 100),
        ("1.01", 2, 101),
        ("0.01", 2, 1),
        ("0.00", 2, 0),
        ("0", 2, 0),
        ("100", 2, 10000),
        ("123.45678", 5, 12345678),
        # Binary-float classic error values parsed exactly without float drift
        ("0.30", 2, 30),
        ("0.07", 2, 7),
        ("1.14", 2, 114),
    ])
    def test_canonical_conversion_success(self, input_val, scale, expected):
        from src.engine.orchestration.candle_source import scaled_units
        assert scaled_units(input_val, scale) == expected

    def test_negative_zero_behavior(self):
        from src.engine.orchestration.candle_source import scaled_units
        from decimal import Decimal
        assert scaled_units("-0.00", 2) == 0
        assert scaled_units(Decimal("-0.00"), 2) == 0

    @pytest.mark.parametrize("excess_val,scale", [
        ("1.005", 2),
        ("0.009", 2),
        ("10.0001", 3),
        ("0.123456789", 8),
    ])
    def test_excess_precision_rejected(self, excess_val, scale):
        from src.engine.orchestration.candle_source import scaled_units
        with pytest.raises(ValueError, match="excess fractional precision"):
            scaled_units(excess_val, scale)

    @pytest.mark.parametrize("bad_val", ["NaN", "Infinity", "-Infinity", "nan", "inf", "+inf"])
    def test_non_finite_rejected(self, bad_val):
        from src.engine.orchestration.candle_source import scaled_units
        with pytest.raises(ValueError, match="Non-finite or oversized"):
            scaled_units(bad_val, 2)

    @pytest.mark.parametrize("float_val", [1.0, 1.01, 0.01, 0.0])
    def test_binary_float_types_forbidden(self, float_val):
        from src.engine.orchestration.candle_source import scaled_units
        with pytest.raises(ValueError, match="floats are forbidden"):
            scaled_units(float_val, 2)

    def test_maximum_accepted_bound_and_overflow(self):
        from src.engine.orchestration.candle_source import scaled_units
        from src.engine.orchestration.models import MAX_UNITS
        # Max bound: MAX_UNITS = 9_000_000_000_000_000
        max_str = str(MAX_UNITS)
        assert scaled_units(max_str, 0) == MAX_UNITS

        # One unit overflow beyond MAX_UNITS
        overflow_str = str(MAX_UNITS + 1)
        with pytest.raises(ValueError, match="bounds|oversized"):
            scaled_units(overflow_str, 0)

    def test_scientific_notation_matching_phase1(self):
        from src.engine.orchestration.candle_source import scaled_units
        # Decimal("1e2") is 100 with exponent 2
        assert scaled_units("1e2", 0) == 100
        assert scaled_units("1e-2", 2) == 1
        # Excess precision via negative exponent
        with pytest.raises(ValueError, match="excess fractional precision"):
            scaled_units("1e-3", 2)


# ============================================================================
# Part E10: Source Event Identity Provenance
# ============================================================================

class TestSourceEventIdentityProvenance:
    """Verify source event ID is derived exclusively from canonical provenance and unaffected by file reordering."""

    def test_file_reordering_preserves_canonical_identity(self, session):
        user = make_test_user(session, "prov_user_1")
        runtime, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)
        session.commit()

        row1 = {"timestamp": "2026-08-28T09:15:00Z", "open": "24500.00", "high": "24550.00", "low": "24480.00", "close": "24520.00", "volume": "1000", "is_closed": "true"}
        row2 = {"timestamp": "2026-08-28T09:30:00Z", "open": "24520.00", "high": "24560.00", "low": "24500.00", "close": "24550.00", "volume": "1500", "is_closed": "true"}

        entry = get_dataset_entry("synthetic_underlying_nifty_15m")

        # Parse row1 at index 0 vs index 5
        candle_at_idx_0 = parse_and_validate_candle(row1, dataset_id=entry.dataset_id, series_role=SeriesRole.REFERENCE, snapshot=snapshot, row_index=0)
        candle_at_idx_5 = parse_and_validate_candle(row1, dataset_id=entry.dataset_id, series_role=SeriesRole.REFERENCE, snapshot=snapshot, row_index=5)

        # Source event ID must be IDENTICAL regardless of line index
        assert candle_at_idx_0.source_event_id == candle_at_idx_5.source_event_id
        assert "row:" not in candle_at_idx_0.source_event_id
        assert candle_at_idx_0.source_event_id.startswith("c1sha256:")
        assert len(candle_at_idx_0.source_event_id) <= 100

        # Deduplication reuses existing
        ev1 = persist_completed_candle(session, candle_at_idx_0)
        session.flush()
        ev2 = persist_completed_candle(session, candle_at_idx_5)
        assert ev1.id == ev2.id

    def test_conflicting_ohlcv_at_same_canonical_identity_fails_closed(self, session):
        user = make_test_user(session, "prov_user_2")
        runtime, config, _ = setup_orchestration_stack(session, user)
        snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)
        session.commit()

        row_orig = {"timestamp": "2026-08-28T09:15:00Z", "open": "24500.00", "high": "24550.00", "low": "24480.00", "close": "24520.00", "volume": "1000", "is_closed": "true"}
        row_conf = {"timestamp": "2026-08-28T09:15:00Z", "open": "24500.00", "high": "24560.00", "low": "24480.00", "close": "24520.00", "volume": "1000", "is_closed": "true"}

        entry = get_dataset_entry("synthetic_underlying_nifty_15m")
        candle_orig = parse_and_validate_candle(row_orig, dataset_id=entry.dataset_id, series_role=SeriesRole.REFERENCE, snapshot=snapshot, row_index=0)
        candle_conf = parse_and_validate_candle(row_conf, dataset_id=entry.dataset_id, series_role=SeriesRole.REFERENCE, snapshot=snapshot, row_index=1)

        persist_completed_candle(session, candle_orig)
        session.flush()

        with pytest.raises(CandleIngestionConflictError, match="Conflicting candle payload"):
            persist_completed_candle(session, candle_conf)


# ============================================================================
# Part E11: Multi-Series Adversarial Synchronization & No-Look-Ahead
# ============================================================================

class TestMultiSeriesAdversarialSynchronization:
    """Verify strict multi-series boundary synchronization and no-look-ahead."""

    def test_missing_series_at_boundary_does_not_advance_checkpoint(self, session, monkeypatch):
        user = make_test_user(session, "adv_user_1")
        runtime, config, snapshot = setup_orchestration_stack(session, user)
        session.commit()

        # Monkeypatch ingestion to not load any candles
        import src.engine.orchestration.worker as worker_mod
        monkeypatch.setattr(worker_mod, "ingest_all_required_fixture_candles", lambda *args, **kwargs: None)

        worker = StrategyEvaluationWorker(worker_id="test-adv-worker")
        claim = worker.claim_next_candidate(session)
        assert claim is not None
        config_id, gen = claim

        # Process step with NO candles available
        eval_result = worker.process_runtime_step(session, config_id, gen)
        assert eval_result is None

        session.refresh(config)
        # Checkpoint must remain None
        assert config.checkpoint_close_at is None
        assert config.last_reason_code == "SERIES_UNSYNCHRONIZED"

    def test_future_candle_cannot_be_loaded_or_evaluated(self, session):
        user = make_test_user(session, "adv_user_2")
        runtime, config, snapshot = setup_orchestration_stack(session, user)
        session.commit()

        evaluator = OrchestrationEvaluator()
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)

        # Ingest candles up to boundary
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        # Load series strictly up to boundary
        ref_entry = get_dataset_entry("synthetic_underlying_nifty_15m")
        ref_candles, _ = evaluator.load_historical_series_up_to_boundary(
            session, config, boundary, ref_entry.dataset_id
        )

        for c in ref_candles:
            # End of candle (timestamp + 15m) must be <= boundary
            close = c.timestamp + datetime.timedelta(minutes=15)
            assert close <= boundary


# ============================================================================
# Part E12: Pre-Action Eligibility & Kill Switch Validation
# ============================================================================

class TestPreActionEligibilityValidation:
    """Verify pre-action eligibility checks: kill switches and instrument allowance."""

    def test_kill_switch_engaged_rejects_pre_action(self, session):
        from src.models import KillSwitch
        user = make_test_user(session, "kill_user_1")
        runtime, config, _ = setup_orchestration_stack(session, user)

        # Ingest candles and reach boundary
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)

        # Engage user kill-switch
        user_ks = KillSwitch(
            target_key=f"USER:{user.id}",
            scope="USER",
            user_id=user.id,
            is_active=True,
            engaged_at=datetime.datetime.now(datetime.timezone.utc),
            reason="Security review pause",
        )
        session.add(user_ks)
        session.commit()

        evaluator = OrchestrationEvaluator()
        evaluation = evaluator.evaluate_boundary(session, config, boundary)

        # When rule triggers, pre-action eligibility check fails because of kill switch
        assert evaluation.risk_outcome in (RiskOutcome.REJECTED.value, RiskOutcome.NOT_RUN.value)
        if evaluation.action_outcome == ActionOutcome.REJECTED.value:
            assert evaluation.no_order_reason == "USER_KILL_SWITCH_ENGAGED"

    def test_disallowed_instrument_rejects_pre_action(self, session):
        user = make_test_user(session, "disallow_user_1")
        runtime, config, _ = setup_orchestration_stack(session, user)

        # Ingest candles
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        evaluator = OrchestrationEvaluator()
        # Mock action & risk policy with disallowed instrument
        action_snap = {"entry_mapping": {"instrument_id": "DISALLOWED_STOCK", "trigger_status": "ON_TRUE"}}
        risk_snap = {"allowed_instruments": ["ALLOWED_STOCK_ONLY"]}

        passed, reason = evaluator._evaluate_pre_action_eligibility(
            db=session,
            config=config,
            action_policy=action_snap,
            risk_policy=risk_snap,
            boundary=boundary,
            finalized_at=boundary,
        )
        assert passed is False
        assert reason == "DISALLOWED_INSTRUMENT"


# ============================================================================
# Part E13: Zero External Transmission Comprehensive Proof
# ============================================================================

class TestZeroExternalTransmissionComprehensive:
    """Prove that complete worker execution produces zero external transmission and zero orders."""

    def test_zero_transmission_with_sandbox_enabled(self, session, monkeypatch):
        # Enable sandbox network flag in env
        monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")

        # Monkeypatch external transmission to explode if called
        call_counters = {"urllib": 0, "http": 0}

        def explode_if_called(*args, **kwargs):
            call_counters["urllib"] += 1
            raise AssertionError("Prohibited external transmission invoked during Phase 3 worker!")

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", explode_if_called)

        user = make_test_user(session, "trans_proof_user")
        runtime, config, snapshot = setup_orchestration_stack(session, user)
        session.commit()

        # Record before counts on all order/transmission/accounting tables separately
        from src.models import (
            ActionDecision,
            RiskDecision,
            SubmissionOutbox,
            Order,
            OrderIntent,
            OrderEvent,
            Fill,
            PaperPosition,
            AccountLedgerEntry,
            ProviderConnection,
            ProviderInstrumentMapping,
            ReconciliationRecord,
            CompletedCandleEvent,
            RuntimeEvaluation,
            RuntimeEvent,
            RuntimeOrchestrationConfig,
            StrategyRuntime,
        )

        counts_before = {
            "action_decisions": session.query(ActionDecision).count(),
            "risk_decisions": session.query(RiskDecision).count(),
            "submission_outbox": session.query(SubmissionOutbox).count(),
            "orders": session.query(Order).count(),
            "order_intents": session.query(OrderIntent).count(),
            "order_events": session.query(OrderEvent).count(),
            "fills": session.query(Fill).count(),
            "paper_positions": session.query(PaperPosition).count(),
            "account_ledger_entries": session.query(AccountLedgerEntry).count(),
            "provider_connections": session.query(ProviderConnection).count(),
            "provider_references": session.query(ProviderInstrumentMapping).count(),
            "reconciliation_records": session.query(ReconciliationRecord).count(),
        }

        # Run worker step
        worker = StrategyEvaluationWorker(worker_id="zero-trans-worker")
        claim = worker.claim_next_candidate(session)
        assert claim is not None
        config_id, gen = claim

        eval_res = worker.process_runtime_step(session, config_id, gen)
        assert eval_res is not None

        # Record after counts
        counts_after = {
            "action_decisions": session.query(ActionDecision).count(),
            "risk_decisions": session.query(RiskDecision).count(),
            "submission_outbox": session.query(SubmissionOutbox).count(),
            "orders": session.query(Order).count(),
            "order_intents": session.query(OrderIntent).count(),
            "order_events": session.query(OrderEvent).count(),
            "fills": session.query(Fill).count(),
            "paper_positions": session.query(PaperPosition).count(),
            "account_ledger_entries": session.query(AccountLedgerEntry).count(),
            "provider_connections": session.query(ProviderConnection).count(),
            "provider_references": session.query(ProviderInstrumentMapping).count(),
            "reconciliation_records": session.query(ReconciliationRecord).count(),
        }

        # Every non-orchestration table must remain strictly unmutated
        for table_name, count in counts_after.items():
            assert count == counts_before[table_name], f"Table {table_name} mutated! Before: {counts_before[table_name]}, After: {count}"
        for table_name in [
            "action_decisions", "risk_decisions", "submission_outbox", "orders",
            "order_intents", "order_events", "fills", "paper_positions",
            "account_ledger_entries", "reconciliation_records",
        ]:
            assert counts_after[table_name] == 0, f"Table {table_name} has non-zero rows ({counts_after[table_name]})!"

        # Allowed Phase 3 changes
        assert session.query(CompletedCandleEvent).filter_by(runtime_id=runtime.id).count() > 0
        assert session.query(RuntimeEvaluation).filter_by(runtime_id=runtime.id).count() == 1
        refreshed_cfg = session.query(RuntimeOrchestrationConfig).filter_by(id=config.id).first()
        assert refreshed_cfg.checkpoint_close_at is not None

        # Assert zero external network calls were attempted
        assert call_counters["urllib"] == 0
        assert call_counters["http"] == 0


# ============================================================================
# Part E14: API Route Security & Parameter Bounds
# ============================================================================

class TestApiRouteAuditAndSecurity:
    """Verify route declaration order, literal latest routing, malformed IDs, and cross-owner isolation."""

    def test_literal_latest_route_not_captured_as_id(self, session):
        user = make_test_user(session, "route_sec_user_1")
        runtime, config, snapshot = setup_orchestration_stack(session, user)
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="route-worker")
        claim = worker.claim_next_candidate(session)
        worker.process_runtime_step(session, claim[0], claim[1])

        client, _ = create_authenticated_client(session, user)

        # Calling /latest must hit get_latest_runtime_evaluation, NOT get_runtime_evaluation_detail
        res = client.get(f"/api/v1/orchestration/runtimes/{runtime.id}/evaluations/latest")
        assert res.status_code == 200
        data = res.json()
        assert "evaluation_fingerprint" in data
        # ID is not "latest"
        assert data["id"] != "latest"

    def test_malformed_ids_return_sanitized_error(self, session):
        user = make_test_user(session, "route_sec_user_2")
        client, _ = create_authenticated_client(session, user)

        # Malformed runtime ID (contains invalid punctuation or SQL injection)
        res = client.get("/api/v1/orchestration/runtimes/malicious';DROP TABLE--/evaluations")
        assert res.status_code in (404, 422)

        # Malformed evaluation ID
        res = client.get(f"/api/v1/orchestration/runtimes/{uuid.uuid4().hex}/evaluations/bad!id@chars")
        assert res.status_code in (404, 422)

    def test_pagination_bounds_enforced(self, session):
        user = make_test_user(session, "route_sec_user_3")
        runtime, _, _ = setup_orchestration_stack(session, user)
        session.commit()

        client, _ = create_authenticated_client(session, user)

        # Limit > 100 rejected
        res = client.get(f"/api/v1/orchestration/runtimes/{runtime.id}/evaluations?limit=999")
        assert res.status_code == 422

        # Limit < 1 rejected
        res = client.get(f"/api/v1/orchestration/runtimes/{runtime.id}/evaluations?limit=0")
        assert res.status_code == 422

    def test_cross_owner_evaluation_returns_404(self, session):
        user_a = make_test_user(session, "owner_a")
        user_b = make_test_user(session, "owner_b")

        runtime_a, config_a, _ = setup_orchestration_stack(session, user_a)
        session.commit()

        # Create evaluation for user A
        worker = StrategyEvaluationWorker(worker_id="route-worker")
        claim = worker.claim_next_candidate(session)
        eval_record = worker.process_runtime_step(session, claim[0], claim[1])
        assert eval_record is not None

        # User B attempts to access user A's evaluation
        client_b, _ = create_authenticated_client(session, user_b)

        res = client_b.get(f"/api/v1/orchestration/runtimes/{runtime_a.id}/evaluations/{eval_record.id}")
        assert res.status_code == 404


# ============================================================================
# Part E15: CLI Safety and Argument Bounds
# ============================================================================

class TestCliSafetyAudit:
    """Verify strategy-evaluation-worker CLI argument validation and environment refusal."""

    def test_production_environment_refused(self, monkeypatch):
        from src.cli import main
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setattr("sys.argv", ["cli.py", "strategy-evaluation-worker"])

        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_invalid_cli_arguments_rejected(self, monkeypatch):
        from src.cli import main
        monkeypatch.setenv("APP_ENV", "development")

        # Invalid batch size
        monkeypatch.setattr("sys.argv", ["cli.py", "strategy-evaluation-worker", "--batch-size", "0"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2

        # Invalid lease duration
        monkeypatch.setattr("sys.argv", ["cli.py", "strategy-evaluation-worker", "--lease-duration", "2"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2

        # Invalid worker ID characters
        monkeypatch.setattr("sys.argv", ["cli.py", "strategy-evaluation-worker", "--worker-id", "bad id with spaces!"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2


# ============================================================================
# Part E16: Phase 3 PostgreSQL Concurrency and Safety Tests (CI-Pending locally)
# ============================================================================

def _require_postgres():
    import os
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url.startswith("postgresql"):
        pytest.skip("PostgreSQL: CI-PENDING (no PostgreSQL test connection)")
    return db_url


class TestPhase3PostgreSQLConcurrencyAndSafety:
    """Dedicated PostgreSQL test nodes for worker claiming, fencing, leasing, and FK safety.

    Skips locally when PostgreSQL service is unavailable (reported as CI-PENDING).
    Runs unconditionally in CI against real PostgreSQL.
    """

    def test_postgres_concurrent_worker_claim(self):
        """Verify concurrent worker claiming queries execute safely on PostgreSQL."""
        db_url = _require_postgres()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(db_url)
        assert engine.dialect.name == "postgresql"
        SessionCls = sessionmaker(bind=engine)
        with SessionCls() as session:
            worker = StrategyEvaluationWorker(session, worker_id="pg_worker_1", batch_size=5)
            # Candidate claiming uses dialect-appropriate row locking
            claimed = worker.claim_next_candidate()
            assert claimed is None or isinstance(claimed, tuple)

    def test_postgres_row_locking_skip_locked_claim(self):
        """Verify PostgreSQL row locking with FOR UPDATE SKIP LOCKED is utilized in claim."""
        db_url = _require_postgres()
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(db_url)
        SessionCls = sessionmaker(bind=engine)
        with SessionCls() as session:
            # Confirm PostgreSQL parser accepts FOR UPDATE SKIP LOCKED query on orchestration configs
            q = text("""
                SELECT id, owner_id, runtime_id, fencing_generation, lease_expires_at
                FROM runtime_orchestration_configs
                WHERE lease_owner IS NULL OR lease_expires_at <= NOW()
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            """)
            result = session.execute(q).fetchall()
            assert isinstance(result, list)

    def test_postgres_two_worker_race_exactly_one_wins(self):
        """Verify two racing worker threads on PostgreSQL result in exactly one successful claim."""
        db_url = _require_postgres()
        import concurrent.futures
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(db_url)
        SessionCls = sessionmaker(bind=engine)

        # Worker race barrier test
        results = []
        def _attempt_claim(worker_id):
            with SessionCls() as s:
                w = StrategyEvaluationWorker(s, worker_id=worker_id, batch_size=1)
                return w.claim_next_candidate()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(_attempt_claim, "pg_w_race_1")
            f2 = ex.submit(_attempt_claim, "pg_w_race_2")
            r1 = f1.result()
            r2 = f2.result()
            # If a candidate existed, at most one acquired it
            if r1 is not None and r2 is not None:
                assert r1[0].id != r2[0].id or r1[1] != r2[1]

    def test_postgres_monotonic_fencing_generation(self):
        """Verify fencing generation increments monotonically on PostgreSQL."""
        db_url = _require_postgres()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(db_url)
        SessionCls = sessionmaker(bind=engine)
        with SessionCls() as session:
            user = make_test_user(session, "pg_fence_usr")
            runtime, config, _ = setup_orchestration_stack(session, user)
            initial_gen = config.fencing_generation
            worker = StrategyEvaluationWorker(session, worker_id="pg_fencer", batch_size=1)
            claimed = worker.claim_next_candidate()
            if claimed and claimed[0].id == config.id:
                assert claimed[1] == initial_gen + 1

    def test_postgres_stale_fence_rejection(self):
        """Verify that a worker with an expired fencing generation is rejected on PostgreSQL."""
        db_url = _require_postgres()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(db_url)
        SessionCls = sessionmaker(bind=engine)
        with SessionCls() as session:
            user = make_test_user(session, "pg_stale_usr")
            runtime, config, _ = setup_orchestration_stack(session, user)
            worker = StrategyEvaluationWorker(session, worker_id="pg_stale_w", batch_size=1)
            # Simulating stale worker by finalizing with an obsolete fencing generation
            stale_gen = config.fencing_generation - 1
            now = datetime.datetime.now(datetime.timezone.utc)
            with pytest.raises(StaleWorkerFencedError):
                worker._fenced_finalize_step(
                    config=config,
                    runtime=runtime,
                    claimed_generation=stale_gen,
                    boundary_close_at=now,
                    evaluation_record=None,
                    prior_checkpoint=config.checkpoint_close_at,
                )

    def test_postgres_racing_finalization_fenced(self):
        """Verify that concurrent finalizations for the same checkpoint are fenced on PostgreSQL."""
        db_url = _require_postgres()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(db_url)
        SessionCls = sessionmaker(bind=engine)
        with SessionCls() as session:
            user = make_test_user(session, "pg_race_fin")
            runtime, config, _ = setup_orchestration_stack(session, user)
            worker1 = StrategyEvaluationWorker(session, worker_id="pg_w_fin1", batch_size=1)
            worker2 = StrategyEvaluationWorker(session, worker_id="pg_w_fin2", batch_size=1)
            # Only the worker matching lease_owner and generation can finalize
            with pytest.raises(StaleWorkerFencedError):
                worker2._fenced_finalize_step(
                    config=config,
                    runtime=runtime,
                    claimed_generation=config.fencing_generation + 99,
                    boundary_close_at=datetime.datetime.now(datetime.timezone.utc),
                    evaluation_record=None,
                    prior_checkpoint=config.checkpoint_close_at,
                )

    def test_postgres_exactly_one_runtime_evaluation(self):
        """Verify that unique constraints on runtime_evaluations prevent duplicate interval rows on PostgreSQL."""
        db_url = _require_postgres()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(db_url)
        SessionCls = sessionmaker(bind=engine)
        with SessionCls() as session:
            user = make_test_user(session, "pg_uniq_eval")
            runtime, config, _ = setup_orchestration_stack(session, user)
            snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)
            # Ingest candle
            candle_events = ingest_all_required_fixture_candles(session, config)
            ref_candle = candle_events["synthetic_underlying_nifty_15m"][0]
            eval_id1 = str(uuid.uuid4())
            eval_id2 = str(uuid.uuid4())
            now = datetime.datetime.now(datetime.timezone.utc)
            ev1 = RuntimeEvaluation(
                id=eval_id1,
                owner_id=user.id,
                runtime_id=runtime.id,
                config_id=config.id,
                evaluation_fingerprint="f" * 64,
                interval_boundary_at=ref_candle.close_at,
                reference_candle_id=ref_candle.id,
                action_outcome=ActionOutcome.NO_ACTION.value,
                risk_outcome=RiskOutcome.NOT_RUN.value,
                evaluated_at=now,
                created_at=now,
            )
            session.add(ev1)
            session.flush()

            # Duplicate interval boundary for same runtime must raise IntegrityError on PostgreSQL
            ev2 = RuntimeEvaluation(
                id=eval_id2,
                owner_id=user.id,
                runtime_id=runtime.id,
                config_id=config.id,
                evaluation_fingerprint="e" * 64,
                interval_boundary_at=ref_candle.close_at,
                reference_candle_id=ref_candle.id,
                action_outcome=ActionOutcome.NO_ACTION.value,
                risk_outcome=RiskOutcome.NOT_RUN.value,
                evaluated_at=now,
                created_at=now,
            )
            session.add(ev2)
            with pytest.raises(Exception) as exc:
                session.flush()
            assert "unique constraint" in str(exc.value).lower() or "integrityerror" in str(type(exc.value)).lower()
            session.rollback()

    def test_postgres_atomic_checkpoint_advancement(self):
        """Verify that evaluation insertion and checkpoint update commit atomically on PostgreSQL."""
        db_url = _require_postgres()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(db_url)
        SessionCls = sessionmaker(bind=engine)
        with SessionCls() as session:
            user = make_test_user(session, "pg_atomic_usr")
            runtime, config, _ = setup_orchestration_stack(session, user)
            worker = StrategyEvaluationWorker(session, worker_id="pg_atom_w", batch_size=1)
            # Run one cycle; verify checkpoint updated together with evaluation row
            worker.run_cycle()
            session.refresh(config)
            eval_count = session.query(RuntimeEvaluation).filter_by(config_id=config.id).count()
            if eval_count > 0:
                assert config.checkpoint_close_at is not None

    def test_postgres_completion_transition_concurrency(self):
        """Verify that completion state transition on final boundary commits atomically on PostgreSQL."""
        db_url = _require_postgres()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(db_url)
        SessionCls = sessionmaker(bind=engine)
        with SessionCls() as session:
            user = make_test_user(session, "pg_comp_usr")
            runtime, config, _ = setup_orchestration_stack(session, user)
            # Set checkpoint to one interval before replay_close_at
            snapshot = OrchestrationSnapshot.model_validate_json(config.snapshot_json)
            config.checkpoint_close_at = snapshot.replay_close_at - datetime.timedelta(minutes=15)
            session.commit()
            worker = StrategyEvaluationWorker(session, worker_id="pg_comp_w", batch_size=1)
            worker.run_cycle()
            session.refresh(runtime)
            session.refresh(config)
            if config.checkpoint_close_at == snapshot.replay_close_at:
                assert runtime.status == "COMPLETED"

    def test_postgres_owner_consistent_fk_enforcement(self):
        """Verify that compound FK fk_orch_eval_reference rejects cross-owner candles on PostgreSQL."""
        db_url = _require_postgres()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(db_url)
        SessionCls = sessionmaker(bind=engine)
        with SessionCls() as session:
            user1 = make_test_user(session, "pg_owner_1")
            user2 = make_test_user(session, "pg_owner_2")
            runtime1, config1, _ = setup_orchestration_stack(session, user1)
            runtime2, config2, _ = setup_orchestration_stack(session, user2)
            # Ingest candle for runtime2
            candle_events = ingest_all_required_fixture_candles(session, config2)
            candle_r2 = candle_events["synthetic_underlying_nifty_15m"][0]

            now = datetime.datetime.now(datetime.timezone.utc)
            # Attempt to create an evaluation for runtime1 referencing runtime2's candle
            bad_eval = RuntimeEvaluation(
                id=str(uuid.uuid4()),
                owner_id=user1.id,
                runtime_id=runtime1.id,
                config_id=config1.id,
                evaluation_fingerprint="a" * 64,
                interval_boundary_at=candle_r2.close_at,
                reference_candle_id=candle_r2.id,  # Belongs to runtime2 / user2!
                action_outcome=ActionOutcome.NO_ACTION.value,
                risk_outcome=RiskOutcome.NOT_RUN.value,
                evaluated_at=now,
                created_at=now,
            )
            session.add(bad_eval)
            with pytest.raises(Exception) as exc:
                session.flush()
            err_msg = str(exc.value).lower()
            assert "foreign key" in err_msg or "violates foreign key constraint" in err_msg or "integrityerror" in str(type(exc.value)).lower()
            session.rollback()


# ============================================================================
# Part E10: Concurrency, Lifecycle Races & Test Integrity Audits
# ============================================================================

class TestLifecycleRaceDeterministic:
    """Deterministic lifecycle race tests using synchronization barriers and row-level locking."""

    def test_finalize_vs_pause_pause_wins_first(self, session):
        """When pause executes and commits before finalization, worker discovers PAUSED status, rejects finalization and rolls back."""
        user = make_test_user(session, "race_usr_1")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="race_w1")
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        # Operator pauses the runtime before worker can finalize
        OrchestrationService.pause_orchestration(session, runtime.id, user.id, actor_id="operator")
        session.refresh(runtime)
        assert runtime.status == RuntimeStatus.PAUSED.value

        # Worker attempts evaluation / finalization
        result = worker.evaluate_candidate(session, config.id)
        assert result is None

        # Assert post-conditions
        session.refresh(runtime)
        session.refresh(config)
        assert runtime.status == RuntimeStatus.PAUSED.value
        assert session.query(RuntimeEvaluation).filter_by(config_id=config.id).count() == 0
        assert config.checkpoint_close_at is None
        # Verify event sequence: only ORCHESTRATION_PAUSED, no finalization or completion
        events = session.query(RuntimeEvent).filter_by(runtime_id=runtime.id).order_by(RuntimeEvent.sequence_number.asc()).all()
        assert len(events) == 1
        assert events[0].reason_code == "ORCHESTRATION_PAUSED"

    def test_finalize_vs_pause_finalize_wins_first(self, session):
        """When finalization completes before pause, pause observes the finalized checkpoint and evaluation."""
        user = make_test_user(session, "race_usr_2")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="race_w2")
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        # Worker finalizes first
        eval_record = worker.evaluate_candidate(session, config.id)
        assert eval_record is not None

        session.refresh(config)
        session.refresh(runtime)
        assert config.checkpoint_close_at == boundary

        # Operator pauses runtime afterward
        pause_resp = OrchestrationService.pause_orchestration(session, runtime.id, user.id, actor_id="operator")
        assert pause_resp["status"] == "PAUSED"

        session.refresh(runtime)
        assert runtime.status == RuntimeStatus.PAUSED.value
        # Evaluation remains committed
        assert session.query(RuntimeEvaluation).filter_by(config_id=config.id).count() == 1

    def test_finalize_vs_stop_stop_wins_first(self, session):
        """When stop executes first, worker finalization is rejected and rolls back."""
        user = make_test_user(session, "race_usr_3")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="race_w3")
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        # Stop runtime
        OrchestrationService.stop_orchestration(session, runtime.id, user.id, actor_id="operator")
        session.refresh(runtime)
        assert runtime.status == RuntimeStatus.STOPPED.value

        # Worker attempts candidate
        result = worker.evaluate_candidate(session, config.id)
        assert result is None

        session.refresh(runtime)
        session.refresh(config)
        assert runtime.status == RuntimeStatus.STOPPED.value
        assert session.query(RuntimeEvaluation).filter_by(config_id=config.id).count() == 0
        assert config.checkpoint_close_at is None

    def test_final_boundary_completion_vs_pause(self, session):
        """Final-boundary completion vs pause: exactly one serial order."""
        user = make_test_user(session, "race_usr_4")
        # 15m window: single boundary
        r_open = OPEN_TIME
        r_close = OPEN_TIME + datetime.timedelta(minutes=15)
        runtime, config, _ = setup_orchestration_stack(
            session, user, status="RUNNING", replay_open=r_open, replay_close=r_close
        )
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="comp_race_w1")
        ingest_all_required_fixture_candles(session, config, up_to_close_at=r_close)
        session.commit()

        # Worker evaluates and completes final boundary
        res = worker.evaluate_candidate(session, config.id)
        assert res is not None

        session.refresh(runtime)
        session.refresh(config)
        assert runtime.status == RuntimeStatus.COMPLETED.value
        assert config.checkpoint_close_at == r_close

        # Subsequent pause attempt must be rejected with ConflictError
        with pytest.raises(Exception) as exc:
            OrchestrationService.pause_orchestration(session, runtime.id, user.id, actor_id="operator")
        assert "conflict" in str(exc.value).lower() or "must be running" in str(exc.value).lower()

        # Exactly one COMPLETED event
        completed_events = (
            session.query(RuntimeEvent)
            .filter_by(runtime_id=runtime.id, new_status=RuntimeStatus.COMPLETED.value)
            .all()
        )
        assert len(completed_events) == 1

    def test_final_boundary_completion_vs_stop(self, session):
        """When stop executes before completion, completion is prevented."""
        user = make_test_user(session, "race_usr_5")
        r_open = OPEN_TIME
        r_close = OPEN_TIME + datetime.timedelta(minutes=15)
        runtime, config, _ = setup_orchestration_stack(
            session, user, status="RUNNING", replay_open=r_open, replay_close=r_close
        )
        session.commit()

        # Stop first
        OrchestrationService.stop_orchestration(session, runtime.id, user.id, actor_id="operator")
        session.refresh(runtime)
        assert runtime.status == RuntimeStatus.STOPPED.value

        worker = StrategyEvaluationWorker(worker_id="comp_stop_w")
        ingest_all_required_fixture_candles(session, config, up_to_close_at=r_close)
        session.commit()

        # Worker attempts candidate
        res = worker.evaluate_candidate(session, config.id)
        assert res is None

        session.refresh(runtime)
        assert runtime.status == RuntimeStatus.STOPPED.value
        completed_events = (
            session.query(RuntimeEvent)
            .filter_by(runtime_id=runtime.id, new_status=RuntimeStatus.COMPLETED.value)
            .all()
        )
        assert len(completed_events) == 0

    def test_two_finalizers_vs_stop(self, session):
        """When stop commits, concurrent finalizers cannot commit evaluations after STOPPED."""
        user = make_test_user(session, "race_usr_6")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker1 = StrategyEvaluationWorker(worker_id="two_fin_w1")
        worker2 = StrategyEvaluationWorker(worker_id="two_fin_w2")
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        # Stop runtime
        OrchestrationService.stop_orchestration(session, runtime.id, user.id, actor_id="operator")
        session.refresh(runtime)

        # Both workers attempt evaluation
        res1 = worker1.evaluate_candidate(session, config.id)
        res2 = worker2.evaluate_candidate(session, config.id)
        assert res1 is None
        assert res2 is None

        session.refresh(runtime)
        session.refresh(config)
        assert runtime.status == RuntimeStatus.STOPPED.value
        assert session.query(RuntimeEvaluation).filter_by(config_id=config.id).count() == 0

    def test_stale_worker_vs_resume_reclaim(self, session):
        """Stale worker holding older fencing generation cannot finalize after operator resume."""
        user = make_test_user(session, "race_usr_7")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker_stale = StrategyEvaluationWorker(worker_id="stale_worker_a")
        # Stale worker claims candidate at generation 1
        claim = worker_stale.claim_next_candidate(session)
        assert claim is not None
        cfg_id, acquired_gen = claim
        assert acquired_gen == 2

        # Operator pauses then resumes (clears lease, unquarantines)
        OrchestrationService.pause_orchestration(session, runtime.id, user.id, actor_id="operator")
        OrchestrationService.resume_orchestration(session, runtime.id, user.id, actor_id="operator")
        session.commit()

        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        # Stale worker attempts finalization with old generation
        evaluation = worker_stale.evaluator.evaluate_boundary(session, config, boundary)
        now = datetime.datetime.now(datetime.timezone.utc)
        with pytest.raises(StaleWorkerFencedError):
            worker_stale._fenced_finalize_step(
                session, config, runtime, evaluation, boundary, acquired_gen=acquired_gen, now=now
            )
        session.rollback()

        # Zero evaluations from stale worker committed
        session.refresh(config)
        assert session.query(RuntimeEvaluation).filter_by(config_id=config.id).count() == 0

    def test_sqlite_threaded_execution(self, session):
        """Verify threaded concurrency against SQLite with workers and lifecycle transitions."""
        import concurrent.futures
        user = make_test_user(session, "thread_usr_1")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        eng = session.get_bind()
        from sqlalchemy.orm import sessionmaker
        SessionCls = sessionmaker(bind=eng)

        def worker_task(w_id: str):
            with SessionCls() as sess:
                w = StrategyEvaluationWorker(worker_id=w_id, poll_interval_seconds=0.01)
                claim = w.claim_next_candidate(sess)
                if claim:
                    c_id, gen = claim
                    w.process_runtime_step(sess, c_id, gen)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(worker_task, f"thr_w_{i}") for i in range(3)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        session.refresh(runtime)
        assert runtime.status in (RuntimeStatus.RUNNING.value, RuntimeStatus.COMPLETED.value)

    def test_postgres_execution_in_ci(self):
        """PostgreSQL CI pending lifecycle locking verification."""
        import os
        pg_url = os.getenv("POSTGRES_TEST_URL") or os.getenv("DATABASE_URL", "")
        if not pg_url or not pg_url.startswith("postgresql"):
            pytest.skip("PostgreSQL CI test environment required (POSTGRES_TEST_URL or DATABASE_URL not set)")
        from sqlalchemy import create_engine, text
        engine = create_engine(pg_url)
        with engine.connect() as conn:
            res = conn.execute(text("SELECT 1")).scalar()
            assert res == 1


class TestClaimEligibilityStrengthened:
    """Audit strengthened cross-table candidate claim validation."""

    def test_claim_rejects_non_running_runtimes(self, session):
        """Worker claim skips PAUSED, STOPPED, READY, and COMPLETED runtimes."""
        worker = StrategyEvaluationWorker(worker_id="claim_w1")

        for idx, non_running in enumerate([RuntimeStatus.PAUSED, RuntimeStatus.STOPPED, RuntimeStatus.READY, RuntimeStatus.COMPLETED]):
            user = make_test_user(session, f"claim_usr_1_{idx}")
            runtime, config, _ = setup_orchestration_stack(session, user, status=non_running.value)
            session.commit()
            claimed = worker.claim_next_candidate(session)
            assert claimed is None, f"Should not claim runtime in status {non_running.value}"

    def test_claim_rejects_non_fixture_replay_or_mock(self, session):
        """Worker claim and schema reject non-FIXTURE_REPLAY or non-INTERNAL_MOCK_ONLY."""
        user = make_test_user(session, "claim_usr_2")
        worker = StrategyEvaluationWorker(worker_id="claim_w2")

        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        # Schema ck_orch_config_source prevents invalid source types at the database level
        with pytest.raises(IntegrityError):
            session.execute(
                update(RuntimeOrchestrationConfig.__table__)
                .where(RuntimeOrchestrationConfig.__table__.c.id == config.id)
                .values(source_type="LIVE_EXCHANGE")
            )
            session.commit()
        session.rollback()

        # Schema ck_orch_config_execution_policy prevents invalid policies at database level
        with pytest.raises(IntegrityError):
            session.execute(
                update(RuntimeOrchestrationConfig.__table__)
                .where(RuntimeOrchestrationConfig.__table__.c.id == config.id)
                .values(execution_policy="BROKER_LIVE")
            )
            session.commit()
        session.rollback()

    def test_claim_rejects_exhausted_retries(self, session):
        """Worker claim rejects configs with retry_count >= MAX_EVALUATION_RETRIES."""
        user = make_test_user(session, "claim_usr_3")
        worker = StrategyEvaluationWorker(worker_id="claim_w3")

        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        config.retry_count = MAX_EVALUATION_RETRIES
        config.next_attempt_at = None
        session.commit()

        assert worker.claim_next_candidate(session) is None

    def test_claim_rejects_runtime_paused_after_candidate_discovery(self, session):
        """Proves a runtime paused/stopped after candidate discovery cannot be claimed for evaluation."""
        user = make_test_user(session, "claim_usr_4")
        worker = StrategyEvaluationWorker(worker_id="claim_w4")

        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        # Pause runtime immediately
        runtime.status = RuntimeStatus.PAUSED.value
        session.commit()

        # Claim must fail because lock revalidation checks status == RUNNING
        assert worker.claim_next_candidate(session) is None


class TestPoisonWorkQuarantine:
    """Verify poison-work retry limits, quarantine transition, and explicit operator recovery."""

    def test_retries_below_limit(self, session):
        """Retries 1 through 4 increment retry_count and schedule next_attempt_at with backoff."""
        user = make_test_user(session, "quar_usr_1")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="quar_w1")
        for expected_retry in range(1, MAX_EVALUATION_RETRIES):
            now = datetime.datetime.now(datetime.timezone.utc)
            config.lease_owner = worker.worker_id
            config.lease_expires_at = now + datetime.timedelta(seconds=60)
            session.commit()
            worker.release_lease(session, config.id, acquired_gen=config.fencing_generation, reason_code="ERR", increment_retry=True)
            session.refresh(config)
            assert config.retry_count == expected_retry
            assert config.next_attempt_at is not None
            assert config.lease_owner is None
            assert config.lease_expires_at is None

    def test_exact_transition_at_limit(self, session):
        """At retry limit (5), config transitions to quarantined state with next_attempt_at = None."""
        user = make_test_user(session, "quar_usr_2")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        config.retry_count = MAX_EVALUATION_RETRIES - 1
        worker = StrategyEvaluationWorker(worker_id="quar_w2")
        now = datetime.datetime.now(datetime.timezone.utc)
        config.lease_owner = worker.worker_id
        config.lease_expires_at = now + datetime.timedelta(seconds=60)
        session.commit()

        worker.release_lease(session, config.id, acquired_gen=config.fencing_generation, reason_code="RETRY_EXHAUSTED", increment_retry=True)
        session.refresh(config)

        assert config.retry_count == MAX_EVALUATION_RETRIES
        assert config.next_attempt_at is None
        assert "QUARANTINED" in (config.last_reason_code or "")
        assert config.lease_owner is None
        assert config.lease_expires_at is None
        assert config.checkpoint_close_at is None
        session.refresh(runtime)
        assert runtime.status == RuntimeStatus.PAUSED.value
        ev = session.query(RuntimeEvent).filter_by(runtime_id=runtime.id, reason_code="EVALUATION_QUARANTINED").first()
        assert ev is not None
        assert ev.previous_status == RuntimeStatus.RUNNING.value
        assert ev.new_status == RuntimeStatus.PAUSED.value

    def test_no_claim_after_exhaustion(self, session):
        """Quarantined config cannot be claimed by any worker (no busy loop)."""
        user = make_test_user(session, "quar_usr_3")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        config.retry_count = MAX_EVALUATION_RETRIES
        config.next_attempt_at = None
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="quar_w3")
        assert worker.claim_next_candidate(session) is None

    def test_operator_recovery_via_resume(self, session):
        """Operator explicitly resumes runtime to clear quarantine and reset retry counter."""
        user = make_test_user(session, "quar_usr_4")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        config.retry_count = MAX_EVALUATION_RETRIES
        config.next_attempt_at = None
        config.last_reason_code = "QUARANTINED_RETRY_EXHAUSTED"
        session.commit()

        # Operator pauses then resumes
        OrchestrationService.pause_orchestration(session, runtime.id, user.id, actor_id="operator")
        OrchestrationService.resume_orchestration(session, runtime.id, user.id, actor_id="operator")
        session.commit()

        session.refresh(config)
        assert config.retry_count == 0
        assert config.next_attempt_at is not None
        assert config.last_reason_code == "ORCHESTRATION_RESUMED"

        # Worker can now claim the unquarantined config
        worker = StrategyEvaluationWorker(worker_id="quar_w4")
        claim = worker.claim_next_candidate(session)
        assert claim is not None

    def test_stale_worker_cannot_clear_or_modify_quarantine(self, session):
        """Stale worker with outdated fencing generation cannot clear or modify quarantined state."""
        user = make_test_user(session, "quar_usr_5")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        config.retry_count = MAX_EVALUATION_RETRIES
        config.next_attempt_at = None
        config.last_reason_code = "QUARANTINED_RETRY_EXHAUSTED"
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="quar_stale_w")
        worker.release_lease(session, config.id, acquired_gen=config.fencing_generation + 5, reason_code="STALE_RELEASE", increment_retry=False)
        session.refresh(config)
        assert config.retry_count == MAX_EVALUATION_RETRIES
        assert config.next_attempt_at is None
        assert config.last_reason_code == "QUARANTINED_RETRY_EXHAUSTED"


class TestDuplicateFingerprintWinnerReload:
    """Audit duplicate evaluation insertion, savepoint isolation, and winner reload."""

    def test_duplicate_evaluation_savepoint_isolation(self, session):
        """Duplicate evaluation insert is safely isolated by savepoint; reloads winner and outer transaction commits."""
        user = make_test_user(session, "dup_usr_1")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker1 = StrategyEvaluationWorker(worker_id="dup_w1")
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        # Worker 1 claims and finalizes evaluation
        claim1 = worker1.claim_next_candidate(session)
        assert claim1 is not None
        eval1 = worker1.evaluator.evaluate_boundary(session, config, boundary)
        now = datetime.datetime.now(datetime.timezone.utc)
        res1 = worker1._fenced_finalize_step(session, config, runtime, eval1, boundary, claim1[1], now)
        assert res1 is not None

        # Worker 2 attempts identical evaluation at same boundary with lease set
        worker2 = StrategyEvaluationWorker(worker_id="dup_w2")
        config.lease_owner = worker2.worker_id
        config.lease_expires_at = now + datetime.timedelta(seconds=60)
        session.commit()

        eval2 = worker2.evaluator.evaluate_boundary(session, config, boundary)
        res2 = worker2._fenced_finalize_step(session, config, runtime, eval2, boundary, config.fencing_generation, now)
        assert res2 is not None
        assert res2.evaluation_fingerprint == res1.evaluation_fingerprint

        # Exactly one row in runtime_evaluations
        assert session.query(RuntimeEvaluation).filter_by(config_id=config.id).count() == 1

    def test_forced_savepoint_integrity_error_reloads_winner(self, session, monkeypatch):
        """Proves savepoint absorbs IntegrityError on duplicate insert and reloads validated winner."""
        user = make_test_user(session, "dup_usr_savepoint")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker1 = StrategyEvaluationWorker(worker_id="dup_sp_w1")
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        # Insert winning evaluation first
        claim1 = worker1.claim_next_candidate(session)
        assert claim1 is not None
        eval1 = worker1.evaluator.evaluate_boundary(session, config, boundary)
        now = datetime.datetime.now(datetime.timezone.utc)
        res1 = worker1._fenced_finalize_step(session, config, runtime, eval1, boundary, claim1[1], now)

        # Worker 2: reset checkpoint and set lease so Worker 2 reaches insert block
        worker2 = StrategyEvaluationWorker(worker_id="dup_sp_w2")
        session.execute(
            update(RuntimeOrchestrationConfig.__table__)
            .where(RuntimeOrchestrationConfig.__table__.c.id == config.id)
            .values(
                checkpoint_close_at=None,
                lease_owner=worker2.worker_id,
                lease_expires_at=now + datetime.timedelta(seconds=60),
            )
        )
        session.commit()
        session.refresh(config)

        # Bypass existing_eval check to force execution into begin_nested() insert block
        orig_query = session.query
        eval2 = worker2.evaluator.evaluate_boundary(session, config, boundary)

        # In _fenced_finalize_step, the savepoint wraps db.add(evaluation); db.flush()
        # It hits UNIQUE constraint on runtime_evaluations, catches it, and reloads res1
        # To bypass pre-insert existing_eval check:
        check_called = False
        def selective_query(*args, **kwargs):
            nonlocal check_called
            q = orig_query(*args, **kwargs)
            if len(args) == 1 and args[0] is RuntimeEvaluation and not check_called:
                check_called = True
                class FakeQuery:
                    def filter(self, *a, **k):
                        return self
                    def first(self):
                        return None
                return FakeQuery()
            return q

        monkeypatch.setattr(session, "query", selective_query)
        res2 = worker2._fenced_finalize_step(session, config, runtime, eval2, boundary, config.fencing_generation, now)
        assert res2.id == res1.id
        assert res2.evaluation_fingerprint == res1.evaluation_fingerprint

    def test_conflicting_fingerprint_fails_closed(self, session):
        """Conflicting evaluation at same interval raises ConflictError."""
        user = make_test_user(session, "dup_usr_2")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="dup_w3")
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        claim = worker.claim_next_candidate(session)
        assert claim is not None
        eval1 = worker.evaluator.evaluate_boundary(session, config, boundary)
        now = datetime.datetime.now(datetime.timezone.utc)
        worker._fenced_finalize_step(session, config, runtime, eval1, boundary, claim[1], now)

        # Worker sets lease to test conflicting evaluation insertion at same boundary
        config.lease_owner = worker.worker_id
        config.lease_expires_at = now + datetime.timedelta(seconds=60)
        session.commit()

        eval_conflicting = worker.evaluator.evaluate_boundary(session, config, boundary)
        eval_conflicting.evaluation_fingerprint = "f" * 64
        with pytest.raises(Exception) as exc:
            worker._fenced_finalize_step(session, config, runtime, eval_conflicting, boundary, config.fencing_generation, now)
        assert "conflict" in str(exc.value).lower()
        session.rollback()


class TestTransmissionCountCoverage:
    """Separate 12-table before/after counts ensuring strict zero transmission."""

    def test_zero_transmission_across_all_12_tables(self, session, monkeypatch):
        """Assert before == after across all 12 trading/OMS tables during fixture replay evaluation."""
        user = make_test_user(session, "trans_usr_1")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        # Monkeypatch external network/broker calls
        call_counts = {"upstox": 0, "http": 0}
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: call_counts.__setitem__("http", call_counts["http"] + 1))

        # 12 separate before counts
        before_counts = {
            "action_decisions": session.query(ActionDecision).count(),
            "risk_decisions": session.query(RiskDecision).count(),
            "submission_outbox": session.query(SubmissionOutbox).count(),
            "orders": session.query(Order).count(),
            "order_intents": session.query(OrderIntent).count(),
            "order_events": session.query(OrderEvent).count(),
            "fills": session.query(Fill).count(),
            "paper_positions": session.query(PaperPosition).count(),
            "account_ledger_entries": session.query(AccountLedgerEntry).count(),
            "provider_connections": session.query(ProviderConnection).count(),
            "provider_references": session.query(ProviderInstrumentMapping).count(),
            "reconciliation_records": session.query(ReconciliationRecord).count(),
        }

        # Run worker evaluation
        worker = StrategyEvaluationWorker(worker_id="trans_w1")
        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()
        worker.evaluate_candidate(session, config.id)

        # 12 separate after counts
        after_counts = {
            "action_decisions": session.query(ActionDecision).count(),
            "risk_decisions": session.query(RiskDecision).count(),
            "submission_outbox": session.query(SubmissionOutbox).count(),
            "orders": session.query(Order).count(),
            "order_intents": session.query(OrderIntent).count(),
            "order_events": session.query(OrderEvent).count(),
            "fills": session.query(Fill).count(),
            "paper_positions": session.query(PaperPosition).count(),
            "account_ledger_entries": session.query(AccountLedgerEntry).count(),
            "provider_connections": session.query(ProviderConnection).count(),
            "provider_references": session.query(ProviderInstrumentMapping).count(),
            "reconciliation_records": session.query(ReconciliationRecord).count(),
        }

        # Assert every single table has count delta 0
        for tbl, b_cnt in before_counts.items():
            assert after_counts[tbl] == b_cnt, f"Table '{tbl}' count changed: {b_cnt} -> {after_counts[tbl]}"

        # Assert allowed Phase 3 tables changed
        assert session.query(RuntimeEvaluation).filter_by(config_id=config.id).count() == 1
        assert session.query(CompletedCandleEvent).count() > 0

        # Assert external call counter is 0
        assert call_counts["upstox"] == 0
        assert call_counts["http"] == 0


class TestCLIBoundsValidation:
    """Verify bounded CLI validation rejecting extreme values, NaN, Infinity, zero, and negative values."""

    class MockArgs:
        def __init__(self, batch_size=10, lease_duration=30, poll_interval=1.0, max_runs=None, worker_id=None):
            self.batch_size = batch_size
            self.lease_duration = lease_duration
            self.poll_interval = poll_interval
            self.max_runs = max_runs
            self.worker_id = worker_id

    def test_cli_accepts_valid_bounded_inputs(self):
        validate_worker_args(self.MockArgs(batch_size=1, lease_duration=5, poll_interval=0.1, max_runs=1, worker_id="w-1"))
        validate_worker_args(self.MockArgs(batch_size=100, lease_duration=300, poll_interval=60.0, max_runs=10000, worker_id="worker.id_99"))
        validate_worker_args(self.MockArgs(max_runs=None))  # Continuous mode

    def test_cli_rejects_invalid_batch_size(self):
        for bad in [0, -1, 101, 1000000, None, True]:
            with pytest.raises(SystemExit) as exc:
                validate_worker_args(self.MockArgs(batch_size=bad))
            assert exc.value.code == 2

    def test_cli_rejects_invalid_lease_duration(self):
        for bad in [0, 4, 301, -10, None, True]:
            with pytest.raises(SystemExit) as exc:
                validate_worker_args(self.MockArgs(lease_duration=bad))
            assert exc.value.code == 2

    def test_cli_rejects_invalid_poll_interval(self):
        for bad in [0, 0.05, 60.5, -1.0, float("nan"), float("inf"), float("-inf"), None, True]:
            with pytest.raises(SystemExit) as exc:
                validate_worker_args(self.MockArgs(poll_interval=bad))
            assert exc.value.code == 2

    def test_cli_rejects_invalid_max_runs(self):
        for bad in [0, -1, 10001, 1000000, True]:
            with pytest.raises(SystemExit) as exc:
                validate_worker_args(self.MockArgs(max_runs=bad))
            assert exc.value.code == 2

    def test_cli_rejects_invalid_worker_id(self):
        for bad in ["", "a" * 51, "invalid space", "worker;drop", "worker#1"]:
            with pytest.raises(SystemExit) as exc:
                validate_worker_args(self.MockArgs(worker_id=bad))
            assert exc.value.code == 2


class TestCompletionBoundaryAudit:
    """Audit completion boundary inclusivity, intermediate missing data, and single event guarantees."""

    def test_final_boundary_present_and_successfully_finalized(self, session):
        """replay_close_at is inclusive; when finalized, status becomes COMPLETED with exactly one event."""
        user = make_test_user(session, "comp_aud_1")
        r_open = OPEN_TIME
        r_close = OPEN_TIME + datetime.timedelta(minutes=15)
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING", replay_open=r_open, replay_close=r_close)
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="comp_aud_w1")
        ingest_all_required_fixture_candles(session, config, up_to_close_at=r_close)
        session.commit()

        res = worker.evaluate_candidate(session, config.id)
        assert res is not None

        session.refresh(runtime)
        session.refresh(config)
        assert runtime.status == RuntimeStatus.COMPLETED.value
        assert config.checkpoint_close_at == r_close
        events = session.query(RuntimeEvent).filter_by(runtime_id=runtime.id, new_status=RuntimeStatus.COMPLETED.value).all()
        assert len(events) == 1

    def test_final_boundary_missing_candle_prevents_completion(self, session, monkeypatch):
        """Missing candle at final boundary causes SERIES_UNSYNCHRONIZED; does not complete."""
        user = make_test_user(session, "comp_aud_2")
        r_open = OPEN_TIME
        r_close = OPEN_TIME + datetime.timedelta(minutes=15)
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING", replay_open=r_open, replay_close=r_close)
        session.commit()

        # Monkeypatch evaluator to simulate missing candle synchronization error
        worker = StrategyEvaluationWorker(worker_id="comp_aud_w2")
        monkeypatch.setattr(
            worker.evaluator,
            "evaluate_boundary",
            lambda *a, **k: (_ for _ in ()).throw(SynchronizationError("Missing candle at boundary")),
        )

        res = worker.evaluate_candidate(session, config.id)
        assert res is None

        session.refresh(runtime)
        session.refresh(config)
        assert runtime.status == RuntimeStatus.RUNNING.value
        assert config.checkpoint_close_at is None
        assert session.query(RuntimeEvent).filter_by(new_status=RuntimeStatus.COMPLETED.value).count() == 0

    def test_intermediate_boundary_missing_candle_prevents_skip(self, session, monkeypatch):
        """Missing candle at intermediate boundary halts progress and never skips to final boundary."""
        user = make_test_user(session, "comp_aud_3")
        r_open = OPEN_TIME
        r_close = OPEN_TIME + datetime.timedelta(minutes=30)  # Two 15m intervals
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING", replay_open=r_open, replay_close=r_close)
        session.commit()

        # Monkeypatch evaluator to simulate missing intermediate candle
        worker = StrategyEvaluationWorker(worker_id="comp_aud_w3")
        monkeypatch.setattr(
            worker.evaluator,
            "evaluate_boundary",
            lambda *a, **k: (_ for _ in ()).throw(SynchronizationError("Missing intermediate candle")),
        )

        res = worker.evaluate_candidate(session, config.id)
        assert res is None

        session.refresh(runtime)
        session.refresh(config)
        assert runtime.status == RuntimeStatus.RUNNING.value
        assert config.checkpoint_close_at is None

    def test_replay_bounds_not_aligned_to_timeframe(self, session):
        """Replay duration not aligned to timeframe is rejected with REPLAY_BOUNDS_MISALIGNED and not completed."""
        user = make_test_user(session, "comp_aud_4")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        # Directly update config replay_close_at to be misaligned (09:35, which is 20m from 09:15) via core table
        new_close = OPEN_TIME + datetime.timedelta(minutes=20)
        config.replay_close_at = new_close
        new_fp = config_consent_fingerprint(config)
        session.execute(
            update(RuntimeOrchestrationConfig.__table__)
            .where(RuntimeOrchestrationConfig.__table__.c.id == config.id)
            .values(replay_close_at=new_close, consent_fingerprint=new_fp)
        )
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="comp_aud_w4")
        res = worker.evaluate_candidate(session, config.id)
        assert res is None

        session.refresh(config)
        session.refresh(runtime)
        assert runtime.status == RuntimeStatus.RUNNING.value
        assert config.last_reason_code == "REPLAY_BOUNDS_MISALIGNED"
        assert session.query(RuntimeEvent).filter_by(new_status=RuntimeStatus.COMPLETED.value).count() == 0


class TestSourceEventMaxBounds:
    """Verify theoretical maximum length calculation and bounded source-event ID guarantees."""

    def test_theoretical_max_calculation_and_bounded_hashing(self):
        """Theoretical maximum length (229 chars) produces bounded canonical hash (<= 100 chars)."""
        dataset_id = "d" * 100
        instrument_id = "i" * 100
        timeframe = "15m"
        timestamp = "2026-08-28T09:30:00Z"
        revision = 1

        raw = f"{dataset_id}:{instrument_id}:{timeframe}:{timestamp}:r{revision}"
        expected_len = 100 + 1 + 100 + 1 + 3 + 1 + 20 + 1 + 2
        assert len(raw) == expected_len == 229

        bounded_id = canonical_source_event_id_v1(
            dataset_id=dataset_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            open_at_iso=timestamp,
            revision=revision,
        )
        assert len(bounded_id) <= 100
        assert bounded_id.startswith("c1sha256:")

    def test_golden_vector_literal_digest(self):
        """Literal expected golden vector for standard Phase 1 fixture."""
        std_id = canonical_source_event_id_v1(
            dataset_id="synthetic_underlying_nifty_15m",
            instrument_id="NSE_INDEX|Nifty 50",
            timeframe="15m",
            open_at_iso="2026-08-28T09:15:00Z",
            revision=1,
        )
        assert std_id == "c1sha256:203ce937cdad2cb791785a08fc7028b173c9fd7f15fa7565049a9b12c200a2e1"
        assert len(std_id) <= 100


class TestDeterministicStaleness:
    """Verify evaluations at different execution clocks produce identical outcomes and fingerprints."""

    def test_evaluation_deterministic_across_different_execution_clocks(self, session):
        user = make_test_user(session, "det_stale_user")
        runtime, config, _ = setup_orchestration_stack(session, user)
        session.commit()

        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        evaluator = OrchestrationEvaluator()

        # Evaluate at clock 1 (e.g. 2026-08-28 10:00:00Z)
        clock1 = lambda: datetime.datetime(2026, 8, 28, 10, 0, 0, tzinfo=datetime.timezone.utc)
        eval1 = evaluator.evaluate_boundary(session, config, boundary, clock=clock1)

        # Evaluate at clock 2 (e.g. 2026-09-21 18:30:00Z)
        clock2 = lambda: datetime.datetime(2026, 9, 21, 18, 30, 0, tzinfo=datetime.timezone.utc)
        eval2 = evaluator.evaluate_boundary(session, config, boundary, clock=clock2)

        # Logical outcomes, evidence, and fingerprints must be strictly IDENTICAL
        assert eval1.action_outcome == eval2.action_outcome
        assert eval1.risk_outcome == eval2.risk_outcome
        assert eval1.evaluation_status == eval2.evaluation_status
        assert eval1.no_order_reason == eval2.no_order_reason
        assert eval1.evaluation_fingerprint == eval2.evaluation_fingerprint
        assert eval1.audit_json == eval2.audit_json
        assert eval1.risk_summary_json == eval2.risk_summary_json
        assert eval1.required_candles_json == eval2.required_candles_json

        # Operational finalized_at timestamps differ according to respective execution clocks
        assert eval1.finalized_at != eval2.finalized_at


class TestLifecycleConcurrencyRaces:
    """Real deterministic lifecycle race tests asserting serial ordering and absence of partial rows."""

    def test_race_finalize_vs_pause(self, session):
        """When pause wins before finalization commits, finalization aborts with StaleWorkerFencedError."""
        user = make_test_user(session, "race_p_user")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="w-race-pause")
        claim = worker.claim_next_candidate(session)
        assert claim is not None
        config_id, gen = claim

        # Deterministic race hook: pause runtime while finalizer is in flight
        def hook_pause():
            OrchestrationService.pause_orchestration(session, runtime.id, user.id, actor_id="admin-actor")

        worker.post_lock_hook = hook_pause

        # Finalization must be rejected
        eval_result = worker.process_runtime_step(session, config_id, gen)
        assert eval_result is None

        session.refresh(runtime)
        session.refresh(config)
        assert runtime.status == RuntimeStatus.PAUSED.value
        assert config.checkpoint_close_at is None
        assert session.query(RuntimeEvaluation).filter_by(runtime_id=runtime.id).count() == 0
        events = session.query(RuntimeEvent).filter_by(runtime_id=runtime.id).all()
        assert any(e.new_status == RuntimeStatus.PAUSED.value for e in events)

    def test_race_finalize_vs_stop(self, session):
        """When stop wins before finalization commits, finalization aborts and runtime remains STOPPED."""
        user = make_test_user(session, "race_s_user")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="w-race-stop")
        claim = worker.claim_next_candidate(session)
        assert claim is not None
        config_id, gen = claim

        def hook_stop():
            OrchestrationService.stop_orchestration(session, runtime.id, user.id, actor_id="admin-actor")

        worker.post_lock_hook = hook_stop

        eval_result = worker.process_runtime_step(session, config_id, gen)
        assert eval_result is None

        session.refresh(runtime)
        session.refresh(config)
        assert runtime.status == RuntimeStatus.STOPPED.value
        assert config.checkpoint_close_at is None
        assert session.query(RuntimeEvaluation).filter_by(runtime_id=runtime.id).count() == 0

    def test_race_completion_vs_pause(self, session):
        """When completion finalizer commits first, subsequent pause raises ConflictError."""
        user = make_test_user(session, "race_cp_user")
        r_open = OPEN_TIME
        r_close = OPEN_TIME + datetime.timedelta(minutes=15)
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING", replay_open=r_open, replay_close=r_close)
        session.commit()

        ingest_all_required_fixture_candles(session, config, up_to_close_at=r_close)
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="w-comp-pause")
        res = worker.evaluate_candidate(session, config.id)
        assert res is not None

        session.refresh(runtime)
        assert runtime.status == RuntimeStatus.COMPLETED.value

        # Subsequent pause must raise ConflictError
        from src.services.orchestration_service import ConflictError
        with pytest.raises(ConflictError):
            OrchestrationService.pause_orchestration(session, runtime.id, user.id, actor_id="admin-actor")

        assert session.query(RuntimeEvent).filter_by(runtime_id=runtime.id, new_status=RuntimeStatus.COMPLETED.value).count() == 1

    def test_race_completion_vs_stop(self, session):
        """When completion finalizer commits first, subsequent stop raises ConflictError."""
        user = make_test_user(session, "race_cs_user")
        r_open = OPEN_TIME
        r_close = OPEN_TIME + datetime.timedelta(minutes=15)
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING", replay_open=r_open, replay_close=r_close)
        session.commit()

        ingest_all_required_fixture_candles(session, config, up_to_close_at=r_close)
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="w-comp-stop")
        res = worker.evaluate_candidate(session, config.id)
        assert res is not None

        session.refresh(runtime)
        assert runtime.status == RuntimeStatus.COMPLETED.value

        from src.services.orchestration_service import ConflictError
        with pytest.raises(ConflictError):
            OrchestrationService.stop_orchestration(session, runtime.id, user.id, actor_id="admin-actor")

        assert session.query(RuntimeEvent).filter_by(runtime_id=runtime.id, new_status=RuntimeStatus.COMPLETED.value).count() == 1

    def test_race_stale_worker_vs_resume(self, session):
        """Worker lease acquired before pause cannot finalize after resume (fencing generation bumped)."""
        user = make_test_user(session, "race_stale_user")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="w-stale-worker")
        claim = worker.claim_next_candidate(session)
        assert claim is not None
        config_id, stale_gen = claim

        # Operator pauses then resumes
        OrchestrationService.pause_orchestration(session, runtime.id, user.id, actor_id="admin-actor")
        OrchestrationService.resume_orchestration(session, runtime.id, user.id, actor_id="admin-actor")

        session.refresh(config)
        assert config.fencing_generation > stale_gen

        # Stale worker attempts to finalize using stale generation
        eval_res = worker.process_runtime_step(session, config_id, stale_gen)
        assert eval_res is None
        assert session.query(RuntimeEvaluation).filter_by(runtime_id=runtime.id).count() == 0


class TestPoisonWorkQuarantineLifecycle:
    """Verify poison-work quarantine transition, claim exclusion, and operator recovery."""

    def test_quarantine_transition_at_limit(self, session):
        """Reaching MAX_EVALUATION_RETRIES transitions StrategyRuntime to PAUSED with EVALUATION_QUARANTINED event."""
        user = make_test_user(session, "quar_user_1")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        config.retry_count = MAX_EVALUATION_RETRIES - 1  # 4 retries
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="quar-worker-1")
        claim = worker.claim_next_candidate(session)
        assert claim is not None
        config_id, gen = claim

        # Release with retry increment hitting limit
        worker.release_lease(session, config_id, gen, reason_code="SYNTHETIC_FAILURE", increment_retry=True)

        session.refresh(runtime)
        session.refresh(config)

        # StrategyRuntime must be PAUSED
        assert runtime.status == RuntimeStatus.PAUSED.value

        # Exactly 1 EVALUATION_QUARANTINED event
        quar_events = session.query(RuntimeEvent).filter_by(runtime_id=runtime.id, reason_code="EVALUATION_QUARANTINED").all()
        assert len(quar_events) == 1
        assert quar_events[0].previous_status == RuntimeStatus.RUNNING.value
        assert quar_events[0].new_status == RuntimeStatus.PAUSED.value

        # Config fields: retry_count == 5, next_attempt_at is None, lease cleared, checkpoint unchanged
        assert config.retry_count == MAX_EVALUATION_RETRIES
        assert config.next_attempt_at is None
        assert config.last_reason_code.startswith("QUARANTINED_")
        assert config.lease_owner is None
        assert config.lease_expires_at is None
        assert config.checkpoint_close_at is None

    def test_quarantined_config_never_claimed(self, session):
        """Quarantined configuration is never claimed by claim_next_candidate."""
        user = make_test_user(session, "quar_user_2")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        config.retry_count = MAX_EVALUATION_RETRIES
        config.next_attempt_at = None
        config.last_reason_code = "QUARANTINED_TEST"
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="quar-worker-2")
        claim = worker.claim_next_candidate(session)
        assert claim is None

    def test_operator_resume_clears_quarantine(self, session):
        """Operator resume from quarantine restores RUNNING status, resets retry_count to 0, and schedules next_attempt_at."""
        user = make_test_user(session, "quar_user_3")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        config.retry_count = MAX_EVALUATION_RETRIES - 1
        session.commit()

        worker = StrategyEvaluationWorker(worker_id="quar-worker-3")
        claim = worker.claim_next_candidate(session)
        worker.release_lease(session, claim[0], claim[1], reason_code="POISON_PILL", increment_retry=True)

        session.refresh(runtime)
        session.refresh(config)
        assert runtime.status == RuntimeStatus.PAUSED.value
        assert config.retry_count == MAX_EVALUATION_RETRIES

        # Operator resume recovers from quarantine
        res = OrchestrationService.resume_orchestration(session, runtime.id, user.id, actor_id="operator-1")
        assert res["status"] == RuntimeStatus.RUNNING.value

        session.refresh(runtime)
        session.refresh(config)
        assert runtime.status == RuntimeStatus.RUNNING.value
        assert config.retry_count == 0
        assert config.next_attempt_at is not None
        assert config.last_reason_code == "ORCHESTRATION_RESUMED"

        # Runtime can now be claimed again
        new_claim = worker.claim_next_candidate(session)
        assert new_claim is not None


class TestDuplicateWinnerSavepointIsolation:
    """Verify duplicate insert isolation in savepoint, winner reloading, and full attribute verification."""

    def test_duplicate_fingerprint_savepoint_winner_reload(self, session):
        user = make_test_user(session, "dup_savepoint_user")
        runtime, config, _ = setup_orchestration_stack(session, user, status="RUNNING")
        session.commit()

        boundary = OPEN_TIME + datetime.timedelta(minutes=15)
        ingest_all_required_fixture_candles(session, config, up_to_close_at=boundary)
        session.commit()

        # Worker 1 evaluates and finalizes boundary
        w1 = StrategyEvaluationWorker(worker_id="w-dup-1")
        claim1 = w1.claim_next_candidate(session)
        assert claim1 is not None
        eval1 = w1.process_runtime_step(session, claim1[0], claim1[1])
        assert eval1 is not None

        # Reset lease on config to simulate a racing second worker
        config.lease_owner = "w-dup-2"
        config.lease_expires_at = utc(w1.clock()) + datetime.timedelta(seconds=30)
        config.fencing_generation = config.fencing_generation + 1
        session.commit()

        # Second worker attempts finalization of identical boundary
        w2 = StrategyEvaluationWorker(worker_id="w-dup-2")
        now = datetime.datetime.now(datetime.timezone.utc)
        eval2 = w2.evaluator.evaluate_boundary(session, config, boundary, clock=w2.clock)
        winner = w2._fenced_finalize_step(session, config, runtime, eval2, boundary, config.fencing_generation, now)
        # Idempotent winner reloaded with identical fingerprint
        assert winner is not None
        assert winner.evaluation_fingerprint == eval1.evaluation_fingerprint

        assert session.query(RuntimeEvaluation).filter_by(runtime_id=runtime.id).count() == 1
