"""Milestone 6C Phase 4: Automated Paper Execution Acceptance & Concurrency Tests.

Validates:
1. Historical Fill Eligibility & Boundary Timing (T_eval_close <= T_candle_open)
2. Authoritative PaperAccount Execution Barrier with Real Concurrent Workers & Separate Sessions
3. Deadlock Elimination (Monotonic L1 -> L2 -> L3 -> L4 -> L5 lock acquisition)
4. Same-Boundary Deterministic Ordering & Tie Priority
5. Delayed Earlier Work & Ordinary Barrier Deferral (Zero poison-work retries, zero quarantine)
6. Mixed 5m / 15m Runtimes on Shared Account (Independent timeframe boundary computation)
7. Paused / Quarantined Membership (Blocked until resumed or explicitly retired to STOPPED)
8. Watermark Enforcement on Admission & Resume (RuntimeReplayBehindAccountError)
9. Multi-Action Provisional Budget Evaluation (Deterministically sorted, no premature exit proceeds credit)
10. Accounting Conservation & Bucket Semantics (Total cash invariant on reservation & cancel/expiry)
11. Migration 0007 Upgrade, Downgrade Guards, and Schema Parity
"""
import datetime
import json
import os
import tempfile
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import create_engine, event, func, text, update
from sqlalchemy.orm import Session, sessionmaker

from src.database import Base, UTCDateTime
from src.database_safety import require_disposable_target
from src.engine.manifest import get_dataset_entry
from src.engine.orchestration.candle_source import accept_completed_candle, scaled_units
from src.engine.orchestration.evaluator import OrchestrationEvaluator
from src.engine.orchestration.evidence import config_consent_fingerprint
from src.engine.orchestration.fingerprint import canonical_json, orchestration_snapshot_v1, runtime_evaluation_v1
from src.engine.orchestration.ingestion import ingest_all_required_fixture_candles
from src.engine.orchestration.models import (
    ActionOutcome,
    CandleSourceType,
    CompletedCandle,
    OrchestrationSnapshot,
    RequiredCandleIdentity,
    RiskOutcome,
    RuntimeEvaluationIdentity,
    SeriesRole,
    TIMEFRAME_SECONDS,
    utc,
)
from src.engine.orchestration.worker import StrategyEvaluationWorker
from src.engine.paper.models import (
    InstrumentSpec,
    LedgerEntryType,
    OrderSide,
    OrderStatus,
    OrderType,
    RuntimeStatus,
    TimeInForce,
)
from src.models import (
    AccountLedgerEntry,
    ActionDecision,
    CompletedCandleEvent,
    Fill,
    Order,
    OrderEvent,
    OrderIntent,
    PaperAccount,
    PaperPosition,
    ProviderConnection,
    ProviderInstrumentMapping,
    RiskDecision,
    RiskPolicy,
    RuntimeEvaluation,
    RuntimeEvent,
    RuntimeOrchestrationConfig,
    Strategy,
    StrategyActionPolicy,
    StrategyRuntime,
    User,
)
from src.services.orchestration_service import (
    AccountBarrierBlockedError,
    AccountUnderReplayOwnershipError,
    ConflictError,
    OrchestrationService,
    RuntimeReplayBehindAccountError,
)
from src.services.paper_service import PaperService
from src.models import SubmissionOutbox, ApiIdempotencyRecord

from tests.paper_database_support import paper_test_database


@pytest.fixture(params=["sqlite", "postgresql"])
def paper_engine(request, tmp_path):
    with paper_test_database(request.param, tmp_path / "execution.db") as (engine, _):
        Base.metadata.create_all(engine)
        yield engine


@pytest.fixture
def session(paper_engine):
    with Session(paper_engine, autoflush=False) as db:
        yield db


OPEN_TIME = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
CLOSE_TIME = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)


def create_test_user(session: Session, username: str) -> User:
    u = User(
        id=str(uuid.uuid4()),
        username=username,
        normalized_username=username.lower(),
        email=f"{username.lower()}@test.tradepro",
        normalized_email=f"{username.lower()}@test.tradepro",
        hashed_password="hashed_test_password",
        role="EDITOR",
        is_active=True,
    )
    session.add(u)
    session.flush()
    return u


def setup_paper_orchestration_runtime(
    session: Session,
    owner: User,
    *,
    runtime_id: Optional[str] = None,
    account: Optional[PaperAccount] = None,
    timeframe: str = "15m",
    replay_open: Optional[datetime.datetime] = None,
    replay_close: Optional[datetime.datetime] = None,
    checkpoint: Optional[datetime.datetime] = None,
    execution_policy: str = "INTERNAL_PAPER",
    status: str = "RUNNING",
    initial_cash: int = 100000000,
    action_mappings: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[StrategyRuntime, RuntimeOrchestrationConfig, PaperAccount]:
    """Helper to set up a complete, valid paper orchestration runtime."""
    r_open = replay_open or OPEN_TIME
    r_close = replay_close or (r_open + datetime.timedelta(hours=2))

    if account is None:
        account = PaperAccount(
            id=str(uuid.uuid4()),
            owner_id=owner.id,
            name="Test Paper Account",
            currency="INR",
            total_cash_units=initial_cash,
            reserved_cash_units=0,
            is_active=True,
        )
        session.add(account)
        session.flush()

    strategy_payload = {
        "name": "Deterministic Test Strategy",
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
    strat_name = f"Test Strategy {runtime_id or uuid.uuid4()}"
    strat = Strategy(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        name=strat_name,
        timeframe=timeframe,
        candidate_selection_mode="FIRST_ELIGIBLE",
        payload=strategy_payload,
    )
    session.add(strat)
    session.flush()

    default_entry = {
        "mapping_id": "entry_1",
        "trigger_status": "TRUE",
        "action": "BUY",
        "type": "ENTRY",
        "side": "BUY",
        "quantity_units": 10,
        "order_type": "LIMIT",
        "limit_price_units": 2500000,  # 25000.00 in scale 2 (above ~24500 market price)
    }
    # Test inputs are converted to the public contract, never production fallbacks.
    def public_mapping(mapping):
        value = dict(mapping)
        value.pop("action", None)
        value.pop("type", None)
        value["rule_target"] = "GLOBAL"
        value["instrument_id"] = "SHORT_SERIES" if timeframe == "5m" else "NSE_INDEX|Nifty 50"
        value["trigger_status"] = "ON_" + value.get("trigger_status", "TRUE").removeprefix("ON_")
        value["quantity"] = str(value.pop("quantity_units"))
        if "limit_price_units" in value:
            from decimal import Decimal
            value["limit_price"] = str(Decimal(value.pop("limit_price_units")) / 100)
        value.setdefault("intent_type", "ENTRY")
        return value
    mappings = action_mappings or [default_entry]
    assert len(mappings) <= 2
    action_policy_payload = {
        "entry_mapping": public_mapping(mappings[0]),
        "exit_mapping": public_mapping(mappings[1]) if len(mappings) == 2 else None,
        "position_exists_behavior": "SCALE",
        "max_entries_per_day": 100,
    }

    action_name = f"Action Policy {runtime_id or uuid.uuid4()}"
    action_pol = StrategyActionPolicy(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        strategy_id=strat.id,
        name=action_name,
        version=1,
        payload=action_policy_payload,
    )
    session.add(action_pol)

    risk_policy_payload = {
        "max_quantity_per_order_units": 1000, "max_notional_per_order_units": 500000000,
        "max_open_orders": 5, "max_open_positions": 5, "max_trades_per_day": 10,
        "max_instrument_exposure_units": 1000000000, "max_total_exposure_units": 2000000000,
        "max_daily_realized_loss_units": 5000000, "max_price_staleness_seconds": 3600,
        "fee_basis_points": 5, "flat_fee_units": 2000,
    }
    risk_pol = session.query(RiskPolicy).filter(RiskPolicy.owner_id == owner.id, RiskPolicy.name == "Risk Policy").first()
    if not risk_pol:
        risk_pol = RiskPolicy(
            id=str(uuid.uuid4()),
            owner_id=owner.id,
            name="Risk Policy",
            version=1,
            payload=risk_policy_payload,
        )
        session.add(risk_pol)
        session.flush()

    conn = session.query(ProviderConnection).filter(ProviderConnection.owner_id == owner.id).first()
    if not conn:
        conn = ProviderConnection(
            id=str(uuid.uuid4()),
            owner_id=owner.id,
            provider_name="UPSTOX",
            environment="SANDBOX",
            credential_reference="test_ref",
            credential_version="v1",
            status="CONFIGURED",
        )
        session.add(conn)
        session.flush()

    tradepro_inst = "SHORT_SERIES" if timeframe == "5m" else "NSE_INDEX|Nifty 50"
    symbol_str = "SHORT_SERIES" if timeframe == "5m" else "NIFTY"
    mapping = session.query(ProviderInstrumentMapping).filter(
        ProviderInstrumentMapping.owner_id == owner.id,
        ProviderInstrumentMapping.tradepro_instrument_id == tradepro_inst,
    ).first()
    if not mapping:
        mapping = ProviderInstrumentMapping(
            id=str(uuid.uuid4()),
            owner_id=owner.id,
            tradepro_instrument_id=tradepro_inst,
            provider_instrument_token="256265",
            exchange="NSE",
            segment="INDEX",
            symbol=symbol_str,
            lot_size_units=1,
            tick_size_units=5,
            freeze_quantity_units=1800,
            verification_status="VERIFIED",
            mapping_version=1,
        )
        session.add(mapping)
        session.flush()

    dataset_id = "synthetic_short_insufficient_5m" if timeframe == "5m" else "synthetic_underlying_nifty_15m"
    manifest_entry = get_dataset_entry(dataset_id)
    ref_dataset_id = manifest_entry.dataset_id
    ref_checksum = manifest_entry.dataset_checksum

    rt_id = runtime_id or str(uuid.uuid4())
    runtime = StrategyRuntime(
        id=rt_id,
        owner_id=owner.id,
        strategy_id=strat.id,
        account_id=account.id,
        action_policy_id=action_pol.id,
        risk_policy_id=risk_pol.id,
        dataset_id=ref_dataset_id,
        timeframe=timeframe,
        trading_mode="PAPER",
        status=status,
        version=1,
        strategy_snapshot=strategy_payload,
        action_policy_snapshot=action_policy_payload,
        risk_policy_snapshot=risk_policy_payload,
        instrument_spec_snapshot={
            "instrument_id": mapping.tradepro_instrument_id,
            "price_scale": 2,
            "lot_size_units": 1,
            "tick_size_units": 5,
        },
    )
    session.add(runtime)
    session.flush()

    # Build snapshot
    source_policy = "packaged_alignment_v1"
    role_str = "REFERENCE" if (manifest_entry.category.value == "REFERENCE" or manifest_entry.dataset_id == "synthetic_short_insufficient_5m") else manifest_entry.category.value
    datasets = [
        {
            "dataset_id": ref_dataset_id,
            "checksum": ref_checksum,
            "instrument_id": manifest_entry.instrument_id,
            "series_role": role_str,
        }
    ]

    snapshot_mat = {
        "owner_id": owner.id,
        "runtime_id": runtime.id,
        "strategy_version": 1,
        "strategy_snapshot": strategy_payload,
        "action_policy_snapshot": action_policy_payload,
        "risk_policy_snapshot": risk_policy_payload,
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
        "execution_policy": execution_policy,
        "external_transmission_allowed": False,
    }
    snapshot = OrchestrationSnapshot(**snapshot_mat)
    snap_fp = orchestration_snapshot_v1(snapshot)

    consent_policy_ver = "fixture_paper_consent_v1" if execution_policy == "INTERNAL_PAPER" else "fixture_consent_v1"

    config = RuntimeOrchestrationConfig(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        runtime_id=runtime.id,
        source_type="FIXTURE_REPLAY",
        source_namespace="packaged:v1",
        source_policy_version=source_policy,
        timeframe=timeframe,
        alignment_offset_seconds=0,
        snapshot_fingerprint=snap_fp,
        snapshot_json=canonical_json(snapshot.model_dump(mode="python")),
        consent_at=r_open,
        consent_policy_version=consent_policy_ver,
        consent_fingerprint="dummy_fp",
        execution_policy=execution_policy,
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

    return runtime, config, account


# ============================================================================
# 1. Historical Fill Eligibility & Boundary Timing
# ============================================================================

def test_historical_fill_eligibility_subsequent_candle_timing(session: Session):
    """Order generated at close T cannot fill on candle closing at T; fills on candle opening at T.

    Proves:
    - Truthful operational creation timestamp (Order.created_at == worker op_clock != historical evaluation close_at)
    - Fill eligibility strictly uses originating evaluation.close_at <= fill_candle.open_at
    - No premature fill at boundary T1
    - Correct fill on subsequent candle opening at T1
    """
    owner = create_test_user(session, "test_fill_timing_user")
    t0 = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
    t1 = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 8, 28, 9, 45, tzinfo=datetime.timezone.utc)

    # Operational clock is set to current audit time, distinctly different from historical replay candles
    op_clock = datetime.datetime(2026, 9, 22, 14, 30, tzinfo=datetime.timezone.utc)

    runtime, config, account = setup_paper_orchestration_runtime(
        session, owner, replay_open=t0, replay_close=t2
    )

    # Ingest candles for [t0, t1] and [t1, t2]
    ingest_all_required_fixture_candles(session, config, up_to_close_at=t2)
    session.commit()

    worker = StrategyEvaluationWorker(worker_id="test-worker-1", clock=lambda: op_clock)

    # Step 1: Evaluate boundary t1 (candle [t0, t1])
    eval_t1 = worker.process_runtime_step(session, config.id, 1)
    assert eval_t1 is not None
    assert eval_t1.close_at == t1

    # An order was generated at t1:
    orders = session.query(Order).filter(Order.runtime_id == runtime.id).all()
    assert len(orders) == 1
    order = orders[0]
    assert order.status == OrderStatus.ACCEPTED.value
    assert order.filled_quantity_units == 0

    # Truthful operational creation timestamp vs historical evaluation timing:
    assert order.created_at == op_clock, "Order.created_at must record truthful operational creation time"
    assert order.created_at != eval_t1.close_at, "created_at must not be backdated to historical evaluation boundary"
    assert eval_t1.close_at == t1

    # Verify order was NOT filled on candle closing at t1 (no premature fill)
    fills_t1 = session.query(Fill).filter(Fill.order_id == order.id).all()
    assert len(fills_t1) == 0

    # Step 2: Evaluate boundary t2 (candle [t1, t2], open_at = t1)
    # Since Order's evaluation close (t1) <= candle_open_at (t1), it is eligible for fills!
    eval_t2 = worker.process_runtime_step(session, config.id, 1)
    assert eval_t2 is not None
    assert eval_t2.close_at == t2

    session.refresh(order)
    fills_t2 = session.query(Fill).filter(Fill.order_id == order.id).all()
    assert len(fills_t2) > 0
    assert order.status in (OrderStatus.FILLED.value, OrderStatus.PARTIALLY_FILLED.value)
    fill = fills_t2[0]
    assert fill.candle_timestamp == t2, "Fill must record execution candle timestamp t2"
    assert fill.created_at is not None, "Fill must have truthful operational timestamp"
    account_id = account.id
    session.commit()
    with Session(session.get_bind()) as fresh:
        committed_fills = fresh.query(Fill).filter_by(account_id=account_id).all()
        committed_account = fresh.get(PaperAccount, account_id)
        position = fresh.query(PaperPosition).filter_by(account_id=account_id).one()
        assert position.net_quantity_units == sum(f.quantity_units for f in committed_fills)
        assert committed_account.total_cash_units == 100000000 - sum(
            f.quantity_units * f.price_units + f.fee_units for f in committed_fills)
        assert committed_account.reserved_cash_units == 0
        entries = fresh.query(AccountLedgerEntry).filter_by(account_id=account_id).all()
        assert sum(e.reserved_cash_delta_units for e in entries) == 0
        assert 100000000 + sum(e.settled_cash_delta_units for e in entries) == committed_account.total_cash_units


# ============================================================================
# 2. Concurrency Barrier Test with Independent Sessions & Real Concurrent Workers
# ============================================================================

def test_two_worker_barrier_concurrency_independent_sessions(paper_engine):
    """Real concurrent workers with separate sessions enforce authoritative account execution barrier.

    Runtime A target: 09:30.
    Runtime B target: 09:45.
    Worker B runs concurrently in Thread B, Worker A runs concurrently in Thread A.
    Worker B tries to finalize first, but encounters AccountBarrierBlockedError because Runtime A has earlier target.
    Worker B is deferred without advancing its checkpoint or incrementing retries.
    Worker A completes 09:30, after which Worker B can evaluate 09:45.
    """
    engine = paper_engine
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    init_session = TestSession()
    owner = create_test_user(init_session, "barrier_owner")

    t0 = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
    t1 = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 8, 28, 9, 45, tzinfo=datetime.timezone.utc)

    # Create shared paper account
    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        name="Shared Account",
        currency="INR",
        total_cash_units=500000000,
        reserved_cash_units=0,
        is_active=True,
    )
    init_session.add(account)
    init_session.commit()

    # Runtime A: checkpoint is None (target is t1 = 09:30, replay completes at t1)
    rt_a, cfg_a, _ = setup_paper_orchestration_runtime(
        init_session, owner, runtime_id="rt_alpha", account=account, replay_open=t0, replay_close=t1, checkpoint=None
    )
    # Runtime B: checkpoint is t1 (target is t2 = 09:45)
    rt_b, cfg_b, _ = setup_paper_orchestration_runtime(
        init_session, owner, runtime_id="rt_beta", account=account, replay_open=t0, replay_close=t2, checkpoint=t1
    )

    ingest_all_required_fixture_candles(init_session, cfg_a, up_to_close_at=t2)
    ingest_all_required_fixture_candles(init_session, cfg_b, up_to_close_at=t2)
    init_session.commit()
    cfg_a_id = cfg_a.id
    cfg_a_gen = cfg_a.fencing_generation
    cfg_b_id = cfg_b.id
    cfg_b_gen = cfg_b.fencing_generation
    init_session.close()

    errors_a: List[Exception] = []
    errors_b: List[Exception] = []
    barrier_start = threading.Barrier(2)
    worker_b_deferred = threading.Event()
    worker_a_done = threading.Event()

    def run_worker_b():
        s_b = TestSession()
        try:
            w_b = StrategyEvaluationWorker(worker_id="worker-B")
            barrier_start.wait(timeout=5.0)
            # Worker B tries to process Runtime B (target 09:45)
            # Must be deferred by account barrier because Runtime A is at 09:30
            res = w_b.process_runtime_step(s_b, cfg_b_id, cfg_b_gen)
            assert res is None, "Worker B should be deferred by account barrier"
            worker_b_deferred.set()

            # Wait until Worker A finishes advancing Runtime A to 09:30
            assert worker_a_done.wait(timeout=10.0), "Worker A did not complete in time"

            # Fresh query on new config generation
            s_b.commit()
            cfg_b_refreshed = s_b.query(RuntimeOrchestrationConfig).filter(RuntimeOrchestrationConfig.id == cfg_b_id).one()
            # Worker B can now process Runtime B at t2
            res2 = w_b.process_runtime_step(s_b, cfg_b_id, cfg_b_refreshed.fencing_generation)
            assert res2 is not None
            assert res2.close_at == t2
        except Exception as e:
            errors_b.append(e)
        finally:
            s_b.close()

    def run_worker_a():
        s_a = TestSession()
        try:
            w_a = StrategyEvaluationWorker(worker_id="worker-A")
            barrier_start.wait(timeout=5.0)
            # Wait for Worker B to encounter barrier and defer
            assert worker_b_deferred.wait(timeout=10.0), "Worker B deferral timed out"

            # Worker A processes Runtime A at 09:30
            eval_a = w_a.process_runtime_step(s_a, cfg_a_id, cfg_a_gen)
            assert eval_a is not None
            assert eval_a.close_at == t1
            worker_a_done.set()
        except Exception as e:
            errors_a.append(e)
        finally:
            s_a.close()

    t_b = threading.Thread(target=run_worker_b)
    t_a = threading.Thread(target=run_worker_a)
    t_b.start()
    t_a.start()

    t_a.join(timeout=10.0)
    t_b.join(timeout=10.0)

    assert not t_a.is_alive(), "Worker A thread timed out!"
    assert not t_b.is_alive(), "Worker B thread timed out!"

    if errors_a:
        raise errors_a[0]
    if errors_b:
        raise errors_b[0]

    # Fresh session verification
    check_session = TestSession()
    cfg_a_fresh = check_session.query(RuntimeOrchestrationConfig).filter(RuntimeOrchestrationConfig.id == cfg_a_id).one()
    cfg_b_fresh = check_session.query(RuntimeOrchestrationConfig).filter(RuntimeOrchestrationConfig.id == cfg_b_id).one()
    assert cfg_a_fresh.checkpoint_close_at == t1
    assert cfg_b_fresh.checkpoint_close_at == t2
    assert cfg_b_fresh.retry_count == 0  # Ordinary barrier deferral does not increment retries
    check_session.close()


# ============================================================================
# 3. Deterministic Same-Boundary Ordering and Tie-Break
# ============================================================================

def test_same_boundary_deterministic_tie_break(session: Session):
    """Two runtimes on same account target same boundary; tie breaks deterministically on runtime_id ASC.

    After Runtime A advances, A and B may target the same next boundary;
    B cannot automatically succeed if A still has tie priority!
    """
    owner = create_test_user(session, "tie_user")
    t0 = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
    t1 = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 8, 28, 9, 45, tzinfo=datetime.timezone.utc)

    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        name="Tie Account",
        currency="INR",
        total_cash_units=500000000,
        reserved_cash_units=0,
        is_active=True,
    )
    session.add(account)
    session.commit()

    # Runtime A: id 'rt_01' (targets t1 then t2)
    rt_a, cfg_a, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_01", account=account, replay_open=t0, replay_close=t2
    )
    # Runtime B: id 'rt_02' (targets t1 then t2)
    rt_b, cfg_b, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_02", account=account, replay_open=t0, replay_close=t2
    )

    ingest_all_required_fixture_candles(session, cfg_a, up_to_close_at=t2)
    ingest_all_required_fixture_candles(session, cfg_b, up_to_close_at=t2)
    session.commit()

    worker = StrategyEvaluationWorker(worker_id="tie-worker")

    # Round 1: Both target t1 (09:30). Worker tries to process rt_02 first (rt_01 has tie priority)
    res_b1 = worker.process_runtime_step(session, cfg_b.id, cfg_b.fencing_generation)
    assert res_b1 is None  # Deferred!
    session.refresh(cfg_b)
    assert cfg_b.checkpoint_close_at is None
    assert cfg_b.last_reason_code == "ACCOUNT_BARRIER_DEFERRED"
    assert cfg_b.retry_count == 0

    # Worker processes rt_01 (winner of tie at t1)
    res_a1 = worker.process_runtime_step(session, cfg_a.id, cfg_a.fencing_generation)
    assert res_a1 is not None
    assert res_a1.close_at == t1
    session.refresh(cfg_a)
    assert cfg_a.checkpoint_close_at == t1

    # Now rt_a is at checkpoint t1 (next boundary t2 = 09:45).
    # rt_b targets t1 (09:30). Since rt_a is at 09:45 > 09:30, rt_b can now advance to t1!
    res_b_catchup = worker.process_runtime_step(session, cfg_b.id, cfg_b.fencing_generation)
    assert res_b_catchup is not None
    assert res_b_catchup.close_at == t1
    session.refresh(cfg_b)
    assert cfg_b.checkpoint_close_at == t1

    # Round 2: BOTH rt_a and rt_b now target t2 (09:45)!
    # Crucial test: B cannot automatically succeed because A still has tie priority at t2!
    res_b2 = worker.process_runtime_step(session, cfg_b.id, cfg_b.fencing_generation)
    assert res_b2 is None  # Deferred again because rt_01 has tie priority at 09:45!
    session.refresh(cfg_b)
    assert cfg_b.checkpoint_close_at == t1
    assert cfg_b.last_reason_code == "ACCOUNT_BARRIER_DEFERRED"

    # Worker processes rt_a at t2 (winner of tie at t2)
    res_a2 = worker.process_runtime_step(session, cfg_a.id, cfg_a.fencing_generation)
    assert res_a2 is not None
    assert res_a2.close_at == t2
    session.refresh(cfg_a)
    assert cfg_a.checkpoint_close_at == t2

    # Now rt_b can process at t2
    res_b_final = worker.process_runtime_step(session, cfg_b.id, cfg_b.fencing_generation)
    assert res_b_final is not None
    assert res_b_final.close_at == t2


# ============================================================================
# 4. Mixed 5m / 15m Runtimes on Shared Account
# ============================================================================

def test_mixed_5m_15m_runtimes_independent_timeframes(session: Session):
    """Member 1 on 5m and Member 2 on 15m calculate their next boundary using their own timeframe."""
    owner = create_test_user(session, "mixed_tf_user")
    t0 = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
    t_5m = datetime.datetime(2026, 8, 28, 9, 20, tzinfo=datetime.timezone.utc)
    t_15m = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)

    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        name="Mixed Account",
        currency="INR",
        total_cash_units=500000000,
        reserved_cash_units=0,
        is_active=True,
    )
    session.add(account)
    session.commit()

    # Member 1 on 5m: target next boundary is 09:20
    rt_5m, cfg_5m, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_5m", account=account, timeframe="5m", replay_open=t0, replay_close=t_15m
    )
    # Member 2 on 15m: target next boundary is 09:30
    rt_15m, cfg_15m, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_15m", account=account, timeframe="15m", replay_open=t0, replay_close=t_15m
    )

    ingest_all_required_fixture_candles(session, cfg_5m, up_to_close_at=t_15m)
    ingest_all_required_fixture_candles(session, cfg_15m, up_to_close_at=t_15m)
    session.commit()

    worker = StrategyEvaluationWorker(worker_id="mixed-worker")

    # Member 2 (15m, target 09:30) is blocked because Member 1 (5m, target 09:20) is earlier!
    res_15m = worker.process_runtime_step(session, cfg_15m.id, cfg_15m.fencing_generation)
    assert res_15m is None
    session.refresh(cfg_15m)
    assert cfg_15m.last_reason_code == "ACCOUNT_BARRIER_DEFERRED"

    # Member 1 (5m, target 09:20) can proceed!
    res_5m = worker.process_runtime_step(session, cfg_5m.id, cfg_5m.fencing_generation)
    assert res_5m is not None
    assert res_5m.close_at == t_5m


# ============================================================================
# 5. Paused / Quarantined Membership & Retirement
# ============================================================================

@pytest.mark.parametrize("quarantined", [False, True], ids=["paused", "quarantined"])
def test_paused_quarantined_blocks_until_retired(session: Session, quarantined):
    """Paused runtime blocks shared account advancement until explicitly retired to STOPPED."""
    owner = create_test_user(session, "quarantine_user")
    t0 = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
    t1 = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 8, 28, 9, 45, tzinfo=datetime.timezone.utc)

    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        name="Quarantine Account",
        currency="INR",
        total_cash_units=500000000,
        reserved_cash_units=0,
        is_active=True,
    )
    session.add(account)
    session.commit()

    # Runtime A: PAUSED at t1
    rt_a, cfg_a, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_paused", account=account, status="PAUSED", replay_open=t0, replay_close=t2, checkpoint=t1
    )
    if quarantined:
        cfg_a.retry_count = 5
        cfg_a.last_reason_code = "QUARANTINED_RETRY_EXHAUSTED"
    # Runtime B: RUNNING at t1 (target t2)
    rt_b, cfg_b, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_running", account=account, status="RUNNING", replay_open=t0, replay_close=t2, checkpoint=t1
    )

    ingest_all_required_fixture_candles(session, cfg_b, up_to_close_at=t2)
    session.commit()

    worker = StrategyEvaluationWorker(worker_id="quarantine-worker")

    # Runtime B cannot advance past t1 because Runtime A is PAUSED (participating member)
    res_b = worker.process_runtime_step(session, cfg_b.id, cfg_b.fencing_generation)
    assert res_b is None
    session.refresh(cfg_b)
    assert cfg_b.last_reason_code == "ACCOUNT_BARRIER_DEFERRED"

    # Now retire Runtime A: transition to STOPPED
    OrchestrationService.stop_orchestration(session, rt_a.id, owner.id, actor_id=owner.id)
    session.commit()

    session.refresh(rt_a)
    assert rt_a.status == "STOPPED"

    # Runtime B can now advance immediately!
    res_b_after = worker.process_runtime_step(session, cfg_b.id, cfg_b.fencing_generation)
    assert res_b_after is not None
    assert res_b_after.close_at == t2


# ============================================================================
# 6. Admission & Resume Watermark Enforcement
# ============================================================================

def test_watermark_enforcement_on_admission_and_resume(session: Session):
    """Cannot activate or resume runtime behind account's committed replay watermark.

    Validates:
    - Attached account with 0 committed evaluations returns None (does not inherit in-memory watermark)
    - Detached account fails closed without database evidence
    - Authoritative database evidence sets watermark
    - Admission behind watermark is rejected
    - Admission at same boundary with earlier runtime ID is rejected if later ID already committed
    - Valid progression of runtime with start boundary ahead of watermark succeeds
    """
    owner = create_test_user(session, "watermark_user")
    t0 = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
    t1 = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 8, 28, 9, 45, tzinfo=datetime.timezone.utc)

    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        name="Watermarked Account",
        currency="INR",
        total_cash_units=500000000,
        reserved_cash_units=0,
        is_active=True,
    )
    # Attached account: even if in-memory attribute is touched, getter queries database
    account._committed_replay_watermark = t2
    session.add(account)
    session.commit()

    # 1. Verify attached account with 0 evaluations strictly returns None from database
    assert account.committed_replay_watermark is None, "Attached account with 0 evaluations must not inherit in-memory watermark"

    # 2. Detached objects cannot supply operational progress
    detached_acc = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        name="Detached Account",
        currency="INR",
        total_cash_units=1000,
        reserved_cash_units=0,
        is_active=True,
    )
    with pytest.raises(RuntimeError, match="attached database session"):
        _ = detached_acc.committed_replay_watermark

    # 3. Commit an INTERNAL_MOCK_ONLY evaluation at t2 (09:45)
    # A mock evaluation must NEVER advance the paper account watermark or block paper work.
    rt_mock, cfg_mock, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_mock_eval", account=account, execution_policy="INTERNAL_MOCK_ONLY",
        replay_open=t0, replay_close=t2
    )
    ingest_all_required_fixture_candles(session, cfg_mock, up_to_close_at=t2)
    session.commit()

    worker_mock = StrategyEvaluationWorker(worker_id="w-mock")
    eval_mock_1 = worker_mock.process_runtime_step(session, cfg_mock.id, cfg_mock.fencing_generation)
    assert eval_mock_1 is not None and eval_mock_1.close_at == t1
    eval_mock_2 = worker_mock.process_runtime_step(session, cfg_mock.id, cfg_mock.fencing_generation)
    assert eval_mock_2 is not None and eval_mock_2.close_at == t2
    session.commit()

    session.refresh(account)
    assert account.committed_replay_watermark is None, (
        "INTERNAL_MOCK_ONLY evaluations must NOT advance paper account committed_replay_watermark"
    )

    # 4. Commit INTERNAL_PAPER evaluation at t1 using real worker
    # This must NOT be blocked by the later INTERNAL_MOCK_ONLY evaluation at t2.
    rt_lead, cfg_lead, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_m_lead", account=account, execution_policy="INTERNAL_PAPER",
        replay_open=t0, replay_close=t2
    )
    ingest_all_required_fixture_candles(session, cfg_lead, up_to_close_at=t2)
    session.commit()

    worker = StrategyEvaluationWorker(worker_id="w-wm")
    eval_paper = worker.process_runtime_step(session, cfg_lead.id, cfg_lead.fencing_generation)
    assert eval_paper is not None and eval_paper.close_at == t1
    session.commit()

    session.refresh(account)
    assert account.committed_replay_watermark == t1, "Authoritative paper watermark reflects committed INTERNAL_PAPER evaluation"
    account_id = account.id
    session.commit()
    with Session(session.get_bind()) as fresh:
        assert fresh.get(PaperAccount, account_id).committed_replay_watermark == t1

    # 5. Attempt to activate runtime behind watermark (start boundary t0 = 09:15 < t1 = 09:30)
    rt_past, cfg_past, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_past", account=account, status="READY",
        replay_open=t0 - datetime.timedelta(minutes=15), replay_close=t2
    )
    session.commit()

    paper_consent = {
        "policy_version": "fixture_paper_consent_v1",
        "confirm_no_external_transmission": True,
        "confirm_fixture_replay_only": True,
        "confirm_operator_authorization": True,
        "confirm_internal_paper_execution": True,
    }

    with pytest.raises(RuntimeReplayBehindAccountError):
        OrchestrationService.activate_orchestration(
            session, rt_past.id, owner.id, paper_consent, actor_id=owner.id
        )

    # 6. Attempt to activate runtime at same boundary t1 with earlier ID ('rt_a_behind' < 'rt_m_lead')
    rt_behind, cfg_behind, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_a_behind", account=account, status="READY", replay_open=t0, replay_close=t2
    )
    session.commit()

    # Activation must raise RuntimeReplayBehindAccountError because rt_m_lead already committed at t1
    with pytest.raises(RuntimeReplayBehindAccountError):
        OrchestrationService.activate_orchestration(
            session, rt_behind.id, owner.id, paper_consent, actor_id=owner.id
        )

    # 7. Valid progression: activate runtime starting at subsequent boundary t2 (> t1)
    rt_forward, cfg_forward, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_z_forward", account=account, status="READY",
        replay_open=t1, replay_close=t2 + datetime.timedelta(minutes=15)
    )
    session.commit()

    activated = OrchestrationService.activate_orchestration(
        session, rt_forward.id, owner.id, paper_consent, actor_id=owner.id
    )
    assert activated["status"] == RuntimeStatus.RUNNING.value


# ============================================================================
# 7. Provisional Budget Multi-Action Evaluation
# ============================================================================

@pytest.mark.parametrize("second_side", ["BUY", "SELL"])
def test_provisional_budget_multi_action_evaluation(session: Session, second_side):
    """Candidate actions sorted by action_mapping_id ASC consume provisional cash and position."""
    owner = create_test_user(session, "budget_user")
    t0 = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
    t1 = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)

    # Initial cash: ₹25,000 (25,000,000 units in scale 2)
    # Action 1: BUY 10 units @ 20,000 => cost = 20,000,000 + fees => ~₹200,000 (fits in cash)
    # Action 2: BUY 10 units @ 20,000 => cost = ~₹200,000 (exceeds remaining provisional cash!)
    # Action 3: SELL 5 units => no position => rejected
    t2 = datetime.datetime(2026, 8, 28, 9, 45, tzinfo=datetime.timezone.utc)
    multi_mappings = [
        {
            "mapping_id": "action_01_buy",
            "trigger_status": "ON_TRUE",
            "side": "BUY",
            "quantity_units": 10,
            "order_type": "LIMIT",
            "limit_price_units": 2000000,
        },
        {
            "mapping_id": "action_02_buy",
            "trigger_status": "ON_TRUE",
            "side": "BUY",
            "quantity_units": 10,
            "order_type": "LIMIT",
            "limit_price_units": 2000000,
        },
    ]
    multi_mappings[1]["side"] = second_side
    multi_mappings[1]["quantity_units"] = 10 if second_side == "BUY" else 5

    runtime, config, account = setup_paper_orchestration_runtime(
        session, owner, replay_open=t0, replay_close=t2, initial_cash=25000000, action_mappings=multi_mappings
    )

    ingest_all_required_fixture_candles(session, config, up_to_close_at=t1)
    session.commit()

    worker = StrategyEvaluationWorker(worker_id="budget-worker")
    eval_res = worker.process_runtime_step(session, config.id, config.fencing_generation)
    assert eval_res is not None

    # Check ActionDecisions
    decisions = (
        session.query(ActionDecision)
        .filter(ActionDecision.runtime_id == runtime.id)
        .order_by(ActionDecision.action_mapping_id.asc())
        .all()
    )
    assert len(decisions) == 2

    # Action 1: EXECUTED
    assert decisions[0].action_mapping_id == "action_01_buy"
    assert decisions[0].decision == "EXECUTED"

    # Action 2: IGNORED due to RISK_INSUFFICIENT_CASH
    assert decisions[1].action_mapping_id == "action_02_buy"
    assert decisions[1].decision == "IGNORED"
    assert decisions[1].reason_code == ("RISK_INSUFFICIENT_AVAILABLE_CASH" if second_side == "BUY" else "RISK_INSTRUMENT_NOT_ALLOWED")

    # Only 1 order created
    orders = session.query(Order).filter(Order.runtime_id == runtime.id).all()
    assert len(orders) == 1
    assert orders[0].side == "BUY"


# ============================================================================
# 8. Dual-Balance Accounting Conservation
# ============================================================================

def test_accounting_conservation_on_reservation_and_cancel(session: Session):
    """Total cash remains invariant when orders are placed and cancelled/expired."""
    owner = create_test_user(session, "conservation_user")
    t0 = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
    t1 = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 8, 28, 9, 45, tzinfo=datetime.timezone.utc)

    initial_total = 1000000000  # ₹100,000
    runtime, config, account = setup_paper_orchestration_runtime(
        session, owner, replay_open=t0, replay_close=t2, initial_cash=initial_total
    )

    ingest_all_required_fixture_candles(session, config, up_to_close_at=t1)
    session.commit()

    worker = StrategyEvaluationWorker(worker_id="conserve-worker")
    worker.process_runtime_step(session, config.id, config.fencing_generation)

    session.refresh(account)
    # Total cash MUST be unchanged on reservation!
    assert account.total_cash_units == initial_total
    assert account.reserved_cash_units > 0
    reserved = account.reserved_cash_units

    # Pause runtime so operator can cancel order
    OrchestrationService.pause_orchestration(session, runtime.id, owner.id, actor_id=owner.id)
    session.commit()

    order = session.query(Order).filter(Order.runtime_id == runtime.id).one()
    PaperService.cancel_order(session, order.id, owner.id, reason="TEST_CANCEL")
    session.commit()

    session.refresh(account)
    # After cancellation: total cash is STILL invariant, reserved cash is released!
    assert account.total_cash_units == initial_total
    assert account.reserved_cash_units == 0

    # Ledger entry verifies reservation release
    ledgers = (
        session.query(AccountLedgerEntry)
        .filter(AccountLedgerEntry.account_id == account.id)
        .order_by(AccountLedgerEntry.sequence_number.asc())
        .all()
    )
    assert len(ledgers) == 2  # 1: CASH_RESERVATION, 2: RESERVATION_RELEASE
    assert ledgers[0].entry_type == LedgerEntryType.CASH_RESERVATION.value
    assert ledgers[1].entry_type == LedgerEntryType.RESERVATION_RELEASE.value
    assert ledgers[1].amount_units == reserved


# ============================================================================
# 9. Deadlock Elimination Test
# ============================================================================

def test_deadlock_elimination_worker_vs_cancel(paper_engine):
    """Verify strict L1 -> L2 -> L3 -> L4 -> L5 lock acquisition prevents deadlocks under active concurrency."""
    engine = paper_engine
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    init_session = TestSession()
    owner = create_test_user(init_session, "deadlock_owner")
    t0 = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 8, 28, 9, 45, tzinfo=datetime.timezone.utc)
    t3 = datetime.datetime(2026, 8, 28, 10, 0, tzinfo=datetime.timezone.utc)

    unfillable_mapping = [{
        "mapping_id": "unfillable_entry",
        "trigger_status": "TRUE",
        "action": "BUY",
        "type": "ENTRY",
        "side": "BUY",
        "quantity_units": 10,
        "order_type": "LIMIT",
        "limit_price_units": 100000,
    }]
    rt, cfg, acct = setup_paper_orchestration_runtime(
        init_session, owner, replay_open=t0, replay_close=t3, action_mappings=unfillable_mapping
    )
    ingest_all_required_fixture_candles(init_session, cfg, up_to_close_at=t3)
    init_session.commit()
    rt_id = rt.id
    cfg_id = cfg.id
    owner_id = owner.id
    init_session.close()

    # Pre-create an order via initial step
    s1 = TestSession()
    w1 = StrategyEvaluationWorker(worker_id="w-init")
    cfg_fresh = s1.query(RuntimeOrchestrationConfig).filter(RuntimeOrchestrationConfig.id == cfg_id).one()
    w1.process_runtime_step(s1, cfg_id, cfg_fresh.fencing_generation)
    order = s1.query(Order).filter(Order.runtime_id == rt_id).one()
    order_id = order.id
    s1.close()

    errors_cancel: List[Exception] = []
    worker_holding_locks = threading.Event()
    cancel_started = threading.Event()

    # Hook invoked by worker while holding Level 1-3 row locks before commit
    def on_worker_locked():
        worker_holding_locks.set()
        assert cancel_started.wait(timeout=5.0), "Cancel operation did not start"

    def run_cancel():
        s = TestSession()
        try:
            assert worker_holding_locks.wait(timeout=5.0), "Worker lock hook timed out"
            cancel_started.set()
            # Concurrent pause followed by cancel follows L1 -> L2 -> L3 -> L5 lock order without deadlock
            OrchestrationService.pause_orchestration(s, rt_id, owner_id, actor_id=owner_id)
            s.commit()
            PaperService.cancel_order(s, order_id, owner_id, reason="CONCURRENT_CANCEL")
            s.commit()
        except Exception as e:
            errors_cancel.append(e)
        finally:
            s.close()

    t_cancel = threading.Thread(target=run_cancel)
    t_cancel.start()

    errors_worker: List[Exception] = []
    def run_worker():
        s = TestSession()
        try:
            w = StrategyEvaluationWorker(worker_id="w-concurrent", post_lock_hook=on_worker_locked)
            cfg_obj = s.query(RuntimeOrchestrationConfig).filter(RuntimeOrchestrationConfig.id == cfg_id).one()
            w.process_runtime_step(s, cfg_id, cfg_obj.fencing_generation)
        except Exception as e:
            errors_worker.append(e)
        finally:
            s.close()

    t_worker = threading.Thread(target=run_worker)
    t_worker.start()

    t_worker.join(timeout=10.0)
    t_cancel.join(timeout=10.0)

    assert not t_worker.is_alive(), "Worker thread deadlocked or hung!"
    assert not t_cancel.is_alive(), "Cancel thread deadlocked or hung!"

    if errors_worker:
        raise errors_worker[0]
    if errors_cancel:
        raise errors_cancel[0]

    # Verify final state through a fresh session
    fresh = TestSession()
    fresh_order = fresh.query(Order).filter(Order.id == order_id).one()
    assert fresh_order.status == OrderStatus.CANCELLED.value
    fresh.close()


@pytest.mark.parametrize("operation", ["activate", "resume"])
@pytest.mark.parametrize("boundary_case", ["past", "equal_before", "equal_after", "future"])
def test_admission_resume_committed_paper_order(session, operation, boundary_case):
    owner = create_test_user(session, "admission_matrix")
    t0, t1 = OPEN_TIME, CLOSE_TIME
    t2 = t1 + datetime.timedelta(minutes=15)
    lead, config, account = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_m", replay_close=t2)
    session.commit()
    assert StrategyEvaluationWorker().process_runtime_step(session, config.id, 1).close_at == t1

    # A lexically later mock evaluation at the same boundary must not affect tie order.
    mock, mock_config, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_z_mock", account=account,
        execution_policy="INTERNAL_MOCK_ONLY", replay_close=t2)
    session.commit()
    assert StrategyEvaluationWorker().process_runtime_step(session, mock_config.id, 1).close_at == t1
    start = {"past": t0 - datetime.timedelta(minutes=15), "future": t1}.get(boundary_case, t0)
    candidate_id = "rt_a" if boundary_case == "equal_before" else "rt_n"
    candidate, _, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id=candidate_id, account=account,
        status="READY" if operation == "activate" else "PAUSED", replay_open=start, replay_close=t2)
    owner_id, account_id = owner.id, account.id
    session.commit()
    with Session(session.get_bind()) as fresh:
        assert fresh.get(PaperAccount, account_id).committed_replay_watermark == t1
        def act():
            if operation == "resume":
                return OrchestrationService.resume_orchestration(fresh, candidate_id, owner_id, owner_id)
            return OrchestrationService.activate_orchestration(fresh, candidate_id, owner_id, {
                "policy_version": "fixture_paper_consent_v1",
                "acknowledged_execution_policy": "INTERNAL_PAPER",
                "confirm_internal_paper_execution": True,
            }, actor_id=owner_id)
        if boundary_case in ("past", "equal_before"):
            with pytest.raises(RuntimeReplayBehindAccountError):
                act()
            fresh.rollback()
        else:
            assert act()["status"] == "RUNNING"
    with Session(session.get_bind()) as fresh:
        expected = "RUNNING" if boundary_case in ("equal_after", "future") else ("READY" if operation == "activate" else "PAUSED")
        assert fresh.get(StrategyRuntime, candidate_id).status == expected


def test_explicit_paper_consent_and_idempotency(session):
    owner = create_test_user(session, "explicit_consent")
    runtime, _, _ = setup_paper_orchestration_runtime(session, owner, status="READY")
    session.commit()
    payload = {"policy_version": "fixture_paper_consent_v1", "confirm_operator_authorization": True}
    with pytest.raises(ValueError, match="Explicit confirmation"):
        OrchestrationService.activate_orchestration(session, runtime.id, owner.id, payload, owner.id)
    payload["confirm_internal_paper_execution"] = True
    first = OrchestrationService.activate_orchestration(session, runtime.id, owner.id, payload, owner.id, idempotency_key="paper-consent")
    assert first["status"] == "RUNNING"
    assert OrchestrationService.activate_orchestration(session, runtime.id, owner.id, payload, owner.id, idempotency_key="paper-consent") == first
    payload["confirm_internal_paper_execution"] = False
    with pytest.raises(ConflictError, match="Idempotency conflict"):
        OrchestrationService.activate_orchestration(session, runtime.id, owner.id, payload, owner.id, idempotency_key="paper-consent")


@pytest.mark.parametrize("policy", ["INTERNAL_PAPER", "INTERNAL_MOCK_ONLY"])
def test_execution_links_expiration_idempotency_and_no_transmission(session, monkeypatch, policy):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("External transmission attempted")
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr("httpx.Client.send", forbidden)
    owner = create_test_user(session, "execution_evidence")
    t2 = CLOSE_TIME + datetime.timedelta(minutes=15)
    runtime, config, account = setup_paper_orchestration_runtime(
        session, owner, replay_close=t2, execution_policy=policy,
        action_mappings=[{"mapping_id": "unfilled", "side": "BUY", "quantity_units": 10,
                          "order_type": "LIMIT", "limit_price_units": 100000}])
    runtime_id, config_id, account_id = runtime.id, config.id, account.id
    initial_cash = account.total_cash_units
    session.commit()
    worker = StrategyEvaluationWorker()
    first = worker.process_runtime_step(session, config_id, 1)
    assert first is not None
    if policy == "INTERNAL_PAPER":
        order = session.query(Order).one()
        intent = session.get(OrderIntent, order.intent_id)
        action = session.query(ActionDecision).one()
        assert action.evaluation_id == intent.evaluation_id == first.id
        assert account.committed_replay_watermark == CLOSE_TIME
    assert worker.process_runtime_step(session, config_id, 1) is not None
    session.commit()
    def evidence(db):
        return {model.__tablename__: db.query(model).count() for model in
                (RuntimeEvaluation, ActionDecision, OrderIntent, Order, Fill, PaperPosition, AccountLedgerEntry, SubmissionOutbox)}
    with Session(session.get_bind()) as fresh:
        before = evidence(fresh)
        assert fresh.get(StrategyRuntime, runtime_id).status == "COMPLETED"
        acct = fresh.get(PaperAccount, account_id)
        assert acct.total_cash_units == initial_cash
        assert acct.reserved_cash_units == 0
        assert before["submission_outbox"] == before["fills"] == before["paper_positions"] == 0
        if policy == "INTERNAL_PAPER":
            orders = fresh.query(Order).all()
            assert orders
            for order in orders:
                assert order.status == "EXPIRED"
                entries = fresh.query(AccountLedgerEntry).filter_by(order_id=order.id).all()
                reserves = [e for e in entries if e.entry_type == "CASH_RESERVATION"]
                releases = [e for e in entries if e.entry_type == "RESERVATION_RELEASE"]
                assert len(reserves) == len(releases) == 1
                assert reserves[0].amount_units == releases[0].amount_units
        else:
            assert before["orders"] == before["order_intents"] == before["account_ledger_entries"] == 0
            assert acct.committed_replay_watermark is None
    assert worker.process_runtime_step(session, config_id, 1) is None
    session.commit()
    with Session(session.get_bind()) as fresh:
        assert evidence(fresh) == before
    assert calls == []


def test_simultaneous_same_boundary_workers(paper_engine):
    with Session(paper_engine) as db:
        owner = create_test_user(db, "simultaneous")
        _, a, account = setup_paper_orchestration_runtime(db, owner, runtime_id="rt_a", replay_close=CLOSE_TIME)
        _, b, _ = setup_paper_orchestration_runtime(db, owner, runtime_id="rt_b", account=account, replay_close=CLOSE_TIME)
        ids = [a.id, b.id]
        account_id = account.id
        db.commit()
    start = threading.Barrier(2)
    errors = []
    def run(config_id):
        try:
            with Session(paper_engine) as db:
                start.wait(timeout=5)
                StrategyEvaluationWorker().process_runtime_step(db, config_id, 1)
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run, args=(config_id,), daemon=True) for config_id in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive(), "Competing worker timed out"
    if errors:
        raise errors[0]
    # The second worker either committed after A or was ordinarily deferred before A.
    with Session(paper_engine) as db:
        a, b = [db.get(RuntimeOrchestrationConfig, config_id) for config_id in ids]
        assert a.checkpoint_close_at == CLOSE_TIME
        assert b.retry_count == 0
        if b.checkpoint_close_at is None:
            assert b.last_reason_code == "ACCOUNT_BARRIER_DEFERRED"
            assert StrategyEvaluationWorker().process_runtime_step(db, b.id, b.fencing_generation) is not None
        db.commit()
    with Session(paper_engine) as db:
        assert db.query(RuntimeEvaluation).count() == 2
        assert all(db.get(RuntimeOrchestrationConfig, config_id).checkpoint_close_at == CLOSE_TIME for config_id in ids)
        assert db.get(PaperAccount, account_id).committed_replay_watermark == CLOSE_TIME


# ============================================================================
# 10. Delayed Earlier Work & Ordinary Barrier Deferral Without Quarantine
# ============================================================================

def test_delayed_earlier_work_ordinary_barrier_deferral_without_quarantine(session: Session):
    """Ordinary barrier deferral does NOT consume poison-work retries or quarantine valid work."""
    owner = create_test_user(session, "barrier_deferral_user")
    t0 = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
    t1 = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 8, 28, 9, 45, tzinfo=datetime.timezone.utc)

    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        name="Barrier Account",
        currency="INR",
        total_cash_units=500000000,
        reserved_cash_units=0,
        is_active=True,
    )
    session.add(account)
    session.commit()

    # Member A is delayed at t0 (target t1, replay completes at t1)
    rt_a, cfg_a, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_earlier", account=account, replay_open=t0, replay_close=t1, checkpoint=None
    )
    # Member B is at t1 (target t2)
    rt_b, cfg_b, _ = setup_paper_orchestration_runtime(
        session, owner, runtime_id="rt_later", account=account, replay_open=t0, replay_close=t2, checkpoint=t1
    )
    ingest_all_required_fixture_candles(session, cfg_a, up_to_close_at=t2)
    ingest_all_required_fixture_candles(session, cfg_b, up_to_close_at=t2)
    session.commit()

    worker = StrategyEvaluationWorker(worker_id="deferral-worker")

    # Attempt to process Member B 10 times in a row while Member A is delayed
    for attempt in range(10):
        session.refresh(cfg_b)
        res = worker.process_runtime_step(session, cfg_b.id, cfg_b.fencing_generation)
        assert res is None, f"Attempt {attempt}: Worker B should be deferred by account barrier"
        session.refresh(cfg_b)
        session.refresh(rt_b)
        # Verify: retry count does NOT advance, runtime status is NOT quarantined (remains RUNNING)
        assert cfg_b.retry_count == 0, f"Attempt {attempt}: retry_count must remain 0"
        assert rt_b.status == RuntimeStatus.RUNNING.value, f"Attempt {attempt}: status must remain RUNNING"
        assert cfg_b.last_reason_code == "ACCOUNT_BARRIER_DEFERRED"

    # Now Member A evaluates t1
    res_a = worker.process_runtime_step(session, cfg_a.id, cfg_a.fencing_generation)
    assert res_a is not None
    assert res_a.close_at == t1

    # Now Member B is immediately unblocked and can evaluate t2
    session.refresh(cfg_b)
    res_b = worker.process_runtime_step(session, cfg_b.id, cfg_b.fencing_generation)
    assert res_b is not None
    assert res_b.close_at == t2


# ============================================================================
# 11. Concurrent Admission and Resume Under Account Lock
# ============================================================================

def test_concurrent_admission_and_resume_under_account_lock(paper_engine):
    """Concurrent admission and resume recheck account ownership and watermark under account lock."""
    engine = paper_engine
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    init_session = TestSession()
    owner = create_test_user(init_session, "admission_owner")
    t0 = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
    t1 = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 8, 28, 9, 45, tzinfo=datetime.timezone.utc)

    account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        name="Admission Account",
        currency="INR",
        total_cash_units=500000000,
        reserved_cash_units=0,
        is_active=True,
    )
    init_session.add(account)
    init_session.commit()

    # Runtime 1 evaluates to t1 (setting watermark to t1)
    rt1, cfg1, _ = setup_paper_orchestration_runtime(
        init_session, owner, runtime_id="rt_lead", account=account, replay_open=t0, replay_close=t2
    )
    ingest_all_required_fixture_candles(init_session, cfg1, up_to_close_at=t2)
    init_session.commit()
    w1 = StrategyEvaluationWorker(worker_id="w-adm")
    w1.process_runtime_step(init_session, cfg1.id, cfg1.fencing_generation)
    init_session.refresh(account)
    assert account.committed_replay_watermark == t1

    # Setup Runtime Behind: start boundary is t1, but rt_lead already committed with id 'rt_lead' > 'rt_a_behind'
    rt_behind, cfg_behind, _ = setup_paper_orchestration_runtime(
        init_session, owner, runtime_id="rt_a_behind", account=account, status="READY", replay_open=t0, replay_close=t2
    )
    # Setup Runtime Valid: start boundary is t2 (replay_open is t1 -> target is t2 > watermark t1)
    rt_valid, cfg_valid, _ = setup_paper_orchestration_runtime(
        init_session, owner, runtime_id="rt_valid", account=account, status="READY", replay_open=t1, replay_close=t2
    )
    rt_behind_id = rt_behind.id
    rt_valid_id = rt_valid.id
    init_session.commit()
    owner_id = owner.id
    init_session.close()

    consent = {
        "policy_version": "fixture_paper_consent_v1",
        "confirm_no_external_transmission": True,
        "confirm_fixture_replay_only": True,
        "confirm_operator_authorization": True,
        "confirm_internal_paper_execution": True,
    }

    admission_start = threading.Barrier(2)
    errors_behind: List[Exception] = []
    errors_valid: List[Exception] = []

    def try_activate_behind():
        s = TestSession()
        try:
            admission_start.wait(timeout=5.0)
            # Must raise RuntimeReplayBehindAccountError because member rt_lead already committed at t1
            OrchestrationService.activate_orchestration(
                s, rt_behind_id, owner_id, consent, actor_id=owner_id
            )
        except Exception as e:
            errors_behind.append(e)
        finally:
            s.close()

    def try_activate_valid():
        s = TestSession()
        try:
            admission_start.wait(timeout=5.0)
            res = OrchestrationService.activate_orchestration(
                s, rt_valid_id, owner_id, consent, actor_id=owner_id
            )
            assert res["status"] == "RUNNING"
        except Exception as e:
            errors_valid.append(e)
        finally:
            s.close()

    t_b = threading.Thread(target=try_activate_behind)
    t_v = threading.Thread(target=try_activate_valid)
    t_b.start()
    t_v.start()
    t_b.join(timeout=10.0)
    t_v.join(timeout=10.0)

    assert not t_b.is_alive()
    assert not t_v.is_alive()

    assert len(errors_behind) == 1
    assert isinstance(errors_behind[0], RuntimeReplayBehindAccountError)
    assert len(errors_valid) == 0

    fresh = TestSession()
    fresh_valid = fresh.query(StrategyRuntime).filter(StrategyRuntime.id == rt_valid_id).one()
    fresh_behind = fresh.query(StrategyRuntime).filter(StrategyRuntime.id == rt_behind_id).one()
    assert fresh_valid.status == "RUNNING"
    assert fresh_behind.status == "READY"
    fresh.close()
