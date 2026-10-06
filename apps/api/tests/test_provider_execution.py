"""Milestone 6C Phase 6: Provider-Driven Paper & Automated Broker Sandbox Execution Tests.

Covers:
- Zero live broker transmission prohibition
- Strict owner isolation
- Lifecycle fencing and emergency kill switch
- Verified provider mapping and explicit operator consent binding
- Zero look-ahead temporal guard and completed candle verification
- Idempotency on repeated candle evaluation
- Sandbox outbox priorities (PLACE=10, CANCEL=0)
- Durable pre-transmission marker (transmission_started_at) and fail-closed reconciliation
- Conservative 429 ambiguity reconciliation
- Multi-session concurrency protection
- PostgreSQL compatibility
"""
import datetime
import hashlib
import json
import os
import threading
import uuid
import pytest
import httpx
from decimal import Decimal
from fastapi.testclient import TestClient
from src.main import app

from sqlalchemy import or_, and_
from sqlalchemy.orm import Session, sessionmaker

from src.database import SessionLocal
from src.engine.market_data.contracts import MarketDataCandle, MarketDataProvenance
from src.engine.paper.models import (
    LedgerEntryType,
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
    get_instrument_spec,
)
from src.engine.paper.units import decimal_to_units
from src.engine.provider_execution import (
    ConflictingIntervalError,
    ConsentMissingError,
    DuplicateCandleError,
    ExpiredMappingError,
    KillSwitchActiveError,
    LiveTransmissionProhibitedError,
    LookAheadProhibitedError,
    MappingNotFoundError,
    OwnerIsolationError,
    ProviderEvaluationResult,
    ProviderExecutionEngine,
    ProviderExecutionError,
    ProviderEvaluationWorker,
    RuntimeLifecycleFencedError,
    UnclosedCandleError,
    UnverifiedMappingError,
)
from src.engine.market_data.adapter import UpstoxMarketDataAdapter
from src.engine.orchestration.evidence import config_consent_fingerprint
from src.engine.orchestration.fingerprint import canonical_json, orchestration_snapshot_v1
from src.engine.orchestration.models import OrchestrationSnapshot, ProviderMappingIdentity
from src.engine.sandbox.outbox_worker import SandboxOutboxWorker
from src.engine.sandbox.upstox_adapter import (
    UpstoxAmbiguousError,
    UpstoxCancelResult,
    UpstoxPlaceResult,
    UpstoxRetryable429,
    UpstoxSandboxAdapter,
)
from src.models import (
    AccountLedgerEntry,
    Base,
    CompletedCandleEvent,
    ExternalOrderLink,
    KillSwitch,
    Order,
    OrderEvent,
    OrderIntent,
    PaperAccount,
    PaperPosition,
    ProviderInstrumentMapping,
    ReconciliationRecord,
    RiskPolicy,
    RuntimeEvaluation,
    RuntimeOrchestrationConfig,
    Strategy,
    StrategyActionPolicy,
    StrategyRuntime,
    SubmissionOutbox,
    User,
    WorkerHeartbeat,
)
from tests.paper_database_support import paper_test_database


@pytest.fixture
def provider_exec_setup(session, test_user, monkeypatch):
    """Sets up an isolated, authorized BROKER_SANDBOX runtime with verified mapping and consent."""
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", test_user.id)
    monkeypatch.setenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "mock_sandbox_token")

    now = datetime.datetime(2026, 10, 6, 10, 0, 0, tzinfo=datetime.timezone.utc)

    # 1. Paper Account
    acct = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Provider Sandbox Account",
        total_cash_units=100000000000,  # 10,000,000 INR
        reserved_cash_units=0,
        currency="INR",
    )
    session.add(acct)
    session.flush()

    # 2. Strategy with ALWAYS TRUE rule for testing triggers
    strat = Strategy(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Provider Test Strategy",
        timeframe="5m",
        candidate_selection_mode="FIRST_ELIGIBLE",
        payload={
            "name": "Provider Test Strategy",
            "timeframe": "5m",
            "action": {
                "type": "PAPER_TRADE",
                "risk_config": {
                    "max_position_size": 100000,
                    "stop_loss_pct": 2,
                    "take_profit_pct": 5,
                    "validity_window": 5,
                },
            },
            "global_conditions": {
                "type": "CONDITION",
                "id": "c1",
                "lhs": {"indicator": "PRICE", "symbol": ""},
                "operator": "GREATER_THAN",
                "rhs": {"type": "NUMBER", "value": 0},
            },
        },
    )
    session.add(strat)
    session.flush()

    # 3. Action Policy with explicit operator consent
    policy = StrategyActionPolicy(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        strategy_id=strat.id,
        name="Provider Sandbox Policy",
        version=1,
        payload={
            "entry_mapping": {
                "mapping_id": "auto_entry_1",
                "rule_target": "GLOBAL",
                "trigger_status": "ON_TRUE",
                "instrument_id": "NSE_INDEX|Nifty 50",
                "side": "BUY",
                "order_type": "LIMIT",
                "limit_price": 25000,
                "quantity": 50,
                "time_in_force": "DAY",
                "cooldown_bars": 1,
                "intent_type": "ENTRY",
            },
            "exit_mapping": {
                "mapping_id": "auto_exit_1",
                "rule_target": "GLOBAL",
                "trigger_status": "ON_FALSE",
                "instrument_id": "NSE_INDEX|Nifty 50",
                "side": "SELL",
                "order_type": "MARKET",
                "quantity": 50,
                "time_in_force": "DAY",
                "intent_type": "EXIT",
            },
            "position_exists_behavior": "IGNORE",
            "max_entries_per_day": 5,
        },
        is_active=True,
    )
    session.add(policy)
    session.flush()

    # 4. Verified Provider Instrument Mapping
    mapping = ProviderInstrumentMapping(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        tradepro_instrument_id="NSE_INDEX|Nifty 50",
        provider_instrument_token="NSE_INDEX|Nifty 50",
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY50",
        verification_status="VERIFIED",
        verified_by=test_user.id,
        verified_at=now,
    )
    session.add(mapping)
    session.flush()

    # 4b. Authoritative Risk Policy
    risk_policy = RiskPolicy(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Provider Risk Policy",
        payload={
            "max_quantity_per_order": 100,
            "max_notional_per_order": 5000000,
            "max_instrument_exposure": 10000000,
            "max_total_exposure": 20000000,
            "max_daily_realized_loss": 500000,
            "max_open_orders": 10,
            "max_open_positions": 5,
            "max_trades_per_day": 50,
            "max_price_staleness_seconds": 900,
            "flat_fee": 20,
            "fee_basis_points": 5,
        },
    )
    session.add(risk_policy)
    session.flush()

    # 5. Strategy Runtime in BROKER_SANDBOX mode
    runtime = StrategyRuntime(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        strategy_id=strat.id,
        action_policy_id=policy.id,
        risk_policy_id=risk_policy.id,
        account_id=acct.id,
        status="RUNNING",
        trading_mode="BROKER_SANDBOX",
        dataset_id="NSE_INDEX|Nifty 50",
        timeframe="5m",
        strategy_snapshot=strat.payload,
        action_policy_snapshot=policy.payload,
        risk_policy_snapshot=risk_policy.payload,
        instrument_spec_snapshot={
            "instrument_id": "NSE_INDEX|Nifty 50",
            "quantity_scale": 0,
            "price_scale": 4,
            "currency_scale": 4,
        },
        version=1,
    )
    session.add(runtime)
    session.flush()

    # 6. Authoritative RuntimeOrchestrationConfig with verified operator consent
    snap_obj = OrchestrationSnapshot(
        owner_id=test_user.id,
        runtime_id=runtime.id,
        strategy_version=1,
        strategy_snapshot=strat.payload,
        action_policy_snapshot=policy.payload,
        risk_policy_snapshot=risk_policy.payload,
        instrument_specification={
            "instrument_id": "NSE_INDEX|Nifty 50",
            "quantity_scale": 0,
            "price_scale": 4,
            "currency_scale": 4,
        },
        provider_mapping=ProviderMappingIdentity(
            mapping_id=mapping.id,
            mapping_version=mapping.mapping_version,
            verification_state="VERIFIED",
            expiry_at=mapping.expiry_date,
        ),
        source_type="PROVIDER_SANDBOX",
        source_namespace="provider.sandbox.nse",
        datasets=(),
        timeframe="5m",
        source_policy_version="provider_completed_v1",
        alignment_offset_seconds=0,
        replay_open_at=now - datetime.timedelta(days=1),
        replay_close_at=now + datetime.timedelta(days=1),
        execution_policy="EXTERNAL_SANDBOX_DISPATCH",
        external_transmission_allowed=False,
    )
    snap_json = canonical_json(snap_obj.model_dump(mode="python"))
    snap_fp = orchestration_snapshot_v1(snap_obj)

    orch_cfg = RuntimeOrchestrationConfig(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        runtime_id=runtime.id,
        source_type="PROVIDER_SANDBOX",
        source_namespace="provider.sandbox.nse",
        execution_policy="EXTERNAL_SANDBOX_DISPATCH",
        snapshot_fingerprint=snap_fp,
        snapshot_json=snap_json,
        consent_at=now,
        consent_policy_version="sandbox_consent_v1",
        consent_fingerprint="0" * 64,
        source_policy_version="provider_completed_v1",
        alignment_offset_seconds=0,
        timeframe="5m",
        replay_open_at=now - datetime.timedelta(days=1),
        replay_close_at=now + datetime.timedelta(days=1),
        fencing_generation=1,
        retry_count=0,
        created_at=now,
        updated_at=now,
    )
    orch_cfg.consent_fingerprint = config_consent_fingerprint(orch_cfg)
    session.add(orch_cfg)
    session.commit()

    return {
        "user": test_user,
        "account": acct,
        "strategy": strat,
        "policy": policy,
        "mapping": mapping,
        "runtime": runtime,
        "orch_cfg": orch_cfg,
        "consent_token": orch_cfg.consent_fingerprint,
        "clock_now": now,
    }


def make_valid_provenance(candle_or_candles, source_type="PROVIDER_SANDBOX"):
    from src.engine.market_data.contracts import MarketDataProvenance
    from src.engine.market_data.provenance import compute_market_data_fingerprint
    if not isinstance(candle_or_candles, list):
        candle_or_candles = [candle_or_candles]
    fp = compute_market_data_fingerprint(candle_or_candles)
    return MarketDataProvenance(
        provider="UPSTOX",
        source_type=source_type,
        retrieved_at=datetime.datetime.now(datetime.timezone.utc),
        requested_instrument_key="NSE_INDEX|Nifty 50",
        timeframe="5m",
        mode="intraday",
        candle_count=len(candle_or_candles),
        content_fingerprint=fp,
        completeness="COMPLETE",
        is_complete_series=True,
    )


def make_valid_candle(open_ts: datetime.datetime, open_p=25000.0, high_p=None, low_p=None, close_p=25010.0, vol=10000, is_closed=True, provenance=...):
    """Helper to create a canonical MarketDataCandle."""
    scale = 4
    o = Decimal(str(open_p))
    c = Decimal(str(close_p))
    if high_p is None:
        h = max(o, c, Decimal("25050.0"))
    else:
        h = Decimal(str(high_p))
    if low_p is None:
        l = min(o, c, Decimal("24950.0"))
    else:
        l = Decimal(str(low_p))
    raw_candle = MarketDataCandle(
        timestamp=open_ts,
        open=o,
        high=h,
        low=l,
        close=c,
        open_units=decimal_to_units(o, scale),
        high_units=decimal_to_units(h, scale),
        low_units=decimal_to_units(l, scale),
        close_units=decimal_to_units(c, scale),
        volume=vol,
        is_closed=is_closed,
    )
    if provenance is ...:
        prov = make_valid_provenance(raw_candle)
    else:
        prov = provenance
    if prov is not None:
        return MarketDataCandle(
            timestamp=open_ts,
            open=o,
            high=h,
            low=l,
            close=c,
            open_units=decimal_to_units(o, scale),
            high_units=decimal_to_units(h, scale),
            low_units=decimal_to_units(l, scale),
            close_units=decimal_to_units(c, scale),
            volume=vol,
            is_closed=is_closed,
            provenance=prov,
        )
    return raw_candle


# -------------------------------------------------------------------------
# 1. Zero Live Broker Transmission Prohibition
# -------------------------------------------------------------------------

def test_zero_live_broker_transmission_prohibited(session, provider_exec_setup):
    """Attempting any live transmission or non-sandbox routing fails closed immediately."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)

    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    # A. Explicit allow_live_broker=True flag is rejected
    with pytest.raises(LiveTransmissionProhibitedError) as exc_a:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c,
            allow_live_broker=True,
        )
    assert "Live broker transmission is strictly forbidden" in str(exc_a.value)

    # B. Schema check constraint forbids BROKER_LIVE
    runtime.trading_mode = "BROKER_LIVE"
    with pytest.raises(Exception):
        session.flush()
    session.rollback()

    # C. Runtime in non-sandbox mode (PAPER) is rejected by engine
    runtime = session.get(StrategyRuntime, runtime.id)
    runtime.trading_mode = "PAPER"
    session.flush()
    with pytest.raises(ProviderExecutionError) as exc_c:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c,
        )
    assert "expected 'BROKER_SANDBOX'" in str(exc_c.value)


# -------------------------------------------------------------------------
# 2. Strict Owner Isolation
# -------------------------------------------------------------------------

def test_owner_isolation_enforced(session, provider_exec_setup):
    """Cross-tenant evaluation fails closed with OwnerIsolationError; zero order creation."""
    runtime = provider_exec_setup["runtime"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    other_user_id = str(uuid.uuid4())

    with pytest.raises(OwnerIsolationError) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=other_user_id,
            candle=c,
        )
    assert "owner mismatch" in str(exc.value)

    # Confirm no orders or outbox entries exist
    assert session.query(Order).count() == 0
    assert session.query(SubmissionOutbox).count() == 0


# -------------------------------------------------------------------------
# 3. Lifecycle Fencing & Emergency Kill Switch
# -------------------------------------------------------------------------

def test_lifecycle_fencing_and_kill_switch(session, provider_exec_setup):
    """Runtime must be RUNNING; PAUSED/HALTED/STOPPED or active kill switch blocks execution."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    # A. PAUSED runtime is fenced
    runtime.status = "PAUSED"
    session.flush()
    with pytest.raises(RuntimeLifecycleFencedError) as exc_paused:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c,
        )
    assert "status 'PAUSED'; execution is fenced" in str(exc_paused.value)

    # B. Active Kill Switch halts RUNNING runtime
    runtime.status = "RUNNING"
    ks = KillSwitch(
        id=str(uuid.uuid4()),
        target_key="global_test_kill",
        scope="GLOBAL",
        is_active=True,
    )
    session.add(ks)
    session.flush()

    with pytest.raises(KillSwitchActiveError) as exc_ks:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c,
        )
    assert "kill-switch [GLOBAL] engaged" in str(exc_ks.value)
    assert runtime.status == "HALTED"


# -------------------------------------------------------------------------
# 4. Verified Mapping & Explicit Consent Binding
# -------------------------------------------------------------------------

def test_verified_mapping_and_consent_binding(session, provider_exec_setup):
    """Unverified, expired mapping or missing consent blocks automated sandbox evaluation."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    mapping = provider_exec_setup["mapping"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    # A. UNVERIFIED mapping fails closed
    mapping.verification_status = "UNVERIFIED"
    session.flush()
    with pytest.raises(UnverifiedMappingError) as exc_unver:
        engine.evaluate_runtime_candle(session, runtime_id=runtime.id, owner_id=user.id, candle=c)
    assert "must be 'VERIFIED'" in str(exc_unver.value)

    # B. Expired mapping fails closed
    mapping.verification_status = "VERIFIED"
    mapping.expiry_date = clock_now - datetime.timedelta(days=1)
    session.flush()
    with pytest.raises(ExpiredMappingError) as exc_exp:
        engine.evaluate_runtime_candle(session, runtime_id=runtime.id, owner_id=user.id, candle=c)
    assert "expired at" in str(exc_exp.value)

    # C. Missing operator consent fails closed
    mapping.expiry_date = None
    runtime_no_cfg = StrategyRuntime(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        strategy_id=provider_exec_setup["strategy"].id,
        action_policy_id=provider_exec_setup["policy"].id,
        risk_policy_id=runtime.risk_policy_id,
        account_id=runtime.account_id,
        status="RUNNING",
        trading_mode="BROKER_SANDBOX",
        dataset_id=runtime.dataset_id,
        timeframe=runtime.timeframe,
        strategy_snapshot=runtime.strategy_snapshot,
        action_policy_snapshot=runtime.action_policy_snapshot,
        risk_policy_snapshot=runtime.risk_policy_snapshot,
        instrument_spec_snapshot=runtime.instrument_spec_snapshot,
        version=1,
    )
    session.add(runtime_no_cfg)
    session.flush()
    with pytest.raises(ConsentMissingError) as exc_consent:
        engine.evaluate_runtime_candle(session, runtime_id=runtime_no_cfg.id, owner_id=user.id, candle=c)
    assert "No persisted orchestration configuration" in str(exc_consent.value)


def test_reject_loose_consent_prefix(session, provider_exec_setup):
    """Finding 1: Loose or prefix-only consent tokens like 'consent_sandbox_v1:anything' are strictly rejected."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    with pytest.raises(ConsentMissingError) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c,
            operator_consent_token="consent_sandbox_v1:anything",
        )
    assert "Prefix-only consent token 'consent_sandbox_v1:...' is strictly rejected" in str(exc.value)


def test_consent_rejected_on_mapping_version_mismatch(session, provider_exec_setup):
    """Finding 1: Consent snapshot bound to mapping version 1 is invalidated if mapping version changes."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    mapping = provider_exec_setup["mapping"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    mapping.mapping_version = 2
    session.flush()

    with pytest.raises(ConsentMissingError) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c,
        )
    assert "Consent bound to mapping version" in str(exc.value)


# -------------------------------------------------------------------------
# 5. Temporal Guard & Zero Look-Ahead & Historical Validation
# -------------------------------------------------------------------------

def test_temporal_guard_and_no_look_ahead(session, provider_exec_setup):
    """Future candles or in-progress/unclosed candles are strictly rejected."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)

    # A. Candle closing in the future relative to clock
    future_open = clock_now + datetime.timedelta(minutes=5)
    c_future = make_valid_candle(future_open)
    with pytest.raises(LookAheadProhibitedError) as exc_fut:
        engine.evaluate_runtime_candle(session, runtime_id=runtime.id, owner_id=user.id, candle=c_future)
    assert "in the future relative to clock" in str(exc_fut.value)

    # B. In-progress / unclosed candle rejected
    c_unclosed = make_valid_candle(clock_now - datetime.timedelta(minutes=10), is_closed=False)
    with pytest.raises(UnclosedCandleError) as exc_unclosed:
        engine.evaluate_runtime_candle(session, runtime_id=runtime.id, owner_id=user.id, candle=c_unclosed)
    assert "not closed; in-progress candles rejected" in str(exc_unclosed.value)


def test_unclosed_future_history_candle_rejected(session, provider_exec_setup):
    """Finding 2: Unclosed candle in history that closes in the future is rejected strictly."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    current_c = make_valid_candle(clock_now - datetime.timedelta(minutes=5))

    # Historical candle that is unclosed AND in the future
    future_hist_c = make_valid_candle(clock_now + datetime.timedelta(minutes=5), is_closed=False)

    with pytest.raises((UnclosedCandleError, LookAheadProhibitedError)) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=current_c,
            candle_history=[future_hist_c],
        )
    assert "not closed" in str(exc.value) or "in the future" in str(exc.value)


def test_duplicate_and_conflicting_history_candles_rejected(session, provider_exec_setup):
    """Finding 2: Duplicate candles or candles not aligned to interval boundaries are rejected."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    current_c = make_valid_candle(clock_now - datetime.timedelta(minutes=5))

    # A. Duplicate candle in history
    hist1 = make_valid_candle(clock_now - datetime.timedelta(minutes=10))
    hist2 = make_valid_candle(clock_now - datetime.timedelta(minutes=10))
    with pytest.raises(DuplicateCandleError) as exc_dup:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=current_c,
            candle_history=[hist1, hist2],
        )
    assert "Duplicate candle timestamp" in str(exc_dup.value)

    # B. Conflicting / misaligned interval
    misaligned = make_valid_candle(clock_now - datetime.timedelta(minutes=7, seconds=30))
    with pytest.raises(ConflictingIntervalError) as exc_conf:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=current_c,
            candle_history=[misaligned],
        )
    assert "not aligned" in str(exc_conf.value)


# -------------------------------------------------------------------------
# 6. Idempotency on Repeated Candle Evaluation
# -------------------------------------------------------------------------

def test_idempotency_on_repeated_candle_evaluation(session, provider_exec_setup):
    """Evaluating the same candle twice produces an idempotent result with zero duplicate orders."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    # 1. First evaluation: triggers entry, creates order and outbox
    res1 = engine.evaluate_runtime_candle(session, runtime_id=runtime.id, owner_id=user.id, candle=c)
    assert res1.action_decision == "ACCEPTED_SANDBOX", f"details: {res1.details}, reason: {res1.reason_code}"
    assert len(res1.order_ids) == 1
    assert len(res1.outbox_ids) == 1

    orders_count1 = session.query(Order).filter(Order.runtime_id == runtime.id).count()
    outbox_count1 = session.query(SubmissionOutbox).filter(SubmissionOutbox.owner_id == user.id).count()
    assert orders_count1 == 1
    assert outbox_count1 == 1

    # 2. Second evaluation with identical candle: IDEMPOTENT_SKIPPED
    res2 = engine.evaluate_runtime_candle(session, runtime_id=runtime.id, owner_id=user.id, candle=c)
    assert res2.action_decision == "IDEMPOTENT_SKIPPED"
    assert res2.reason_code == "CANDLE_ALREADY_PROCESSED"

    orders_count2 = session.query(Order).filter(Order.runtime_id == runtime.id).count()
    outbox_count2 = session.query(SubmissionOutbox).filter(SubmissionOutbox.owner_id == user.id).count()
    assert orders_count2 == 1
    assert outbox_count2 == 1


# -------------------------------------------------------------------------
# 7. Sandbox Outbox Priorities (PLACE=10, CANCEL=0)
# -------------------------------------------------------------------------

def test_sandbox_outbox_priorities_place_and_cancel(session, provider_exec_setup):
    """PLACE outbox has priority 10, CANCEL outbox has priority 0; CANCEL is prioritized."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)

    # 1. Trigger ENTRY -> generates PLACE outbox (priority 10)
    c1 = make_valid_candle(clock_now - datetime.timedelta(minutes=10))
    res1 = engine.evaluate_runtime_candle(session, runtime_id=runtime.id, owner_id=user.id, candle=c1)
    place_outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.id == res1.outbox_ids[0]).one()
    assert place_outbox.action_type == "PLACE"
    assert place_outbox.priority == 10
    assert place_outbox.status == "PENDING"
    assert place_outbox.transmission_started_at is None  # Durable pre-transmission marker is None

    # 2. Trigger EXIT -> generates CANCEL outbox (priority 0)
    c2 = make_valid_candle(clock_now - datetime.timedelta(minutes=5))
    res2 = engine._process_exit_action(
        db=session,
        runtime=runtime,
        orch_cfg=provider_exec_setup["orch_cfg"],
        account=provider_exec_setup["account"],
        mapping=provider_exec_setup["mapping"],
        inst_spec=None,
        action_cfg={"mapping_id": "auto_exit_1"},
        candle=c2,
        candle_close=c2.timestamp + datetime.timedelta(minutes=5),
        now=clock_now,
    )
    cancel_outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.id == res2["outbox_ids"][0]).one()
    assert cancel_outbox.action_type == "CANCEL"
    assert cancel_outbox.priority == 0
    assert cancel_outbox.status == "PENDING"

    # 3. Verify SandboxOutboxWorker claims CANCEL first due to priority order
    worker = SandboxOutboxWorker(worker_id="test_priority_worker", clock=lambda: clock_now)
    claimed = worker.claim_records(session, clock_now)
    assert len(claimed) >= 2
    assert claimed[0].action_type == "CANCEL"
    assert claimed[1].action_type == "PLACE"


def _make_order_with_intent(
    session,
    user_id: str,
    runtime_id: str,
    account_id: str,
    seq: int,
    instrument_id: str = "NSE_INDEX|Nifty 50",
    side: str = "BUY",
    order_type: str = "LIMIT",
    quantity_units: int = 50,
    limit_price_units: int = 250000000,
    status: str = "PENDING_SUBMISSION",
) -> Order:
    intent_id = str(uuid.uuid4())
    intent = OrderIntent(
        id=intent_id,
        owner_id=user_id,
        runtime_id=runtime_id,
        action_mapping_id="entry_1",
        requested_instrument_id=instrument_id,
        resolved_instrument_id=instrument_id,
        intent_type="ENTRY",
        reduce_only=False,
        side=side,
        quantity_units=quantity_units,
        order_type=order_type,
        limit_price_units=limit_price_units,
        time_in_force="DAY",
        source_candle_timestamp=datetime.datetime(2026, 10, 6, 9, 30, tzinfo=datetime.timezone.utc),
        source_evaluation_fingerprint=f"fp_{intent_id}",
        trigger_event_key=f"trig_{intent_id}",
    )
    session.add(intent)
    session.flush()

    o = Order(
        id=str(uuid.uuid4()),
        owner_id=user_id,
        runtime_id=runtime_id,
        intent_id=intent.id,
        account_id=account_id,
        order_sequence_number=seq,
        instrument_id=instrument_id,
        side=side,
        order_type=order_type,
        quantity_units=quantity_units,
        limit_price_units=limit_price_units,
        filled_quantity_units=0,
        status=status,
        version=1,
    )
    session.add(o)
    session.flush()
    return o


# -------------------------------------------------------------------------
# 8. Durable Pre-Transmission Marker & Fail-Closed Reconciliation
# -------------------------------------------------------------------------

def test_durable_pre_transmission_marker_and_fail_closed_reconciliation(session, provider_exec_setup):
    """
    If lease expires with transmission_started_at is None -> safe requeue (RETRY_SCHEDULED).
    If lease expires with transmission_started_at is NOT None -> fails closed to RECONCILIATION_REQUIRED.
    """
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    past = clock_now - datetime.timedelta(minutes=10)

    # Order 1: Never began transmission
    o1 = _make_order_with_intent(
        session,
        user_id=user.id,
        runtime_id=provider_exec_setup["runtime"].id,
        account_id=provider_exec_setup["account"].id,
        seq=101,
    )

    outbox_safe = SubmissionOutbox(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        order_id=o1.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        idempotency_key=f"safe_lease_test:{o1.id}",
        canonical_payload_hash="a" * 64,
        payload_json={"test": "safe"},
        claimed_by="dead_worker_1",
        claim_lease_until=past,
        transmission_started_at=None,  # Not transmitted!
    )
    session.add(outbox_safe)

    # Order 2: Started transmission before crash
    o2 = _make_order_with_intent(
        session,
        user_id=user.id,
        runtime_id=provider_exec_setup["runtime"].id,
        account_id=provider_exec_setup["account"].id,
        seq=102,
    )

    outbox_unsafe = SubmissionOutbox(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        order_id=o2.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        idempotency_key=f"unsafe_lease_test:{o2.id}",
        canonical_payload_hash="b" * 64,
        payload_json={"test": "unsafe"},
        claimed_by="dead_worker_2",
        claim_lease_until=past,
        transmission_started_at=past - datetime.timedelta(seconds=5),  # Started!
    )
    session.add(outbox_unsafe)
    session.commit()

    # Run expired lease recovery
    worker = SandboxOutboxWorker(worker_id="reconciler_worker", clock=lambda: clock_now)
    worker.recover_expired_leases(session, clock_now)
    session.commit()

    # Verify outbox_safe was requeued
    session.refresh(outbox_safe)
    assert outbox_safe.status == "RETRY_SCHEDULED"

    # Verify outbox_unsafe failed closed to RECONCILIATION_REQUIRED
    session.refresh(outbox_unsafe)
    assert outbox_unsafe.status == "RECONCILIATION_REQUIRED"

    # Verify an open reconciliation record was created
    recon = session.query(ReconciliationRecord).filter(ReconciliationRecord.order_id == o2.id).first()
    assert recon is not None
    assert recon.status == "OPEN"


# -------------------------------------------------------------------------
# 9. Conservative 429 Ambiguity Reconciliation
# -------------------------------------------------------------------------

def test_conservative_429_ambiguity_reconciliation(session, provider_exec_setup, monkeypatch):
    """
    On order submission, an HTTP 429 response from Upstox does not prove non-acceptance;
    it immediately transitions the order to RECONCILIATION_REQUIRED without auto-retry.
    """
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]

    o = _make_order_with_intent(
        session,
        user_id=user.id,
        runtime_id=provider_exec_setup["runtime"].id,
        account_id=provider_exec_setup["account"].id,
        seq=103,
    )

    outbox = SubmissionOutbox(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        order_id=o.id,
        action_type="PLACE",
        priority=10,
        status="PENDING",
        idempotency_key=f"test_429:{o.id}",
        canonical_payload_hash="c" * 64,
        payload_json={"instrument_token": "NSE_INDEX|Nifty 50", "quantity": 50, "transaction_type": "BUY"},
        transmission_started_at=None,
        next_attempt_at=clock_now,
    )
    session.add(outbox)
    session.commit()

    # Mock adapter returning UpstoxRetryable429
    class Mock429Adapter(UpstoxSandboxAdapter):
        def place_order(self, *a, **kw):
            raise UpstoxRetryable429(10, "RATE_LIMIT_EXCEEDED", "Rate limit hit during order placement")

    worker = SandboxOutboxWorker(
        worker_id="test_429_worker",
        adapter=Mock429Adapter(),
        clock=lambda: clock_now,
    )

    claimed = worker.claim_records(session, clock_now)
    assert len(claimed) >= 1
    worker.process_record(session, claimed[0], clock_now)
    session.commit()

    session.refresh(outbox)
    session.refresh(o)
    assert outbox.status == "RECONCILIATION_REQUIRED"
    assert o.status == OrderStatus.RECONCILIATION_REQUIRED.value


# -------------------------------------------------------------------------
# 10. Multi-Session Concurrency & Row Locking
# -------------------------------------------------------------------------

def test_concurrency_locking_and_fencing(session, provider_exec_setup):
    """
    Two concurrent sessions evaluating with the same candle are serialized via row locks;
    exactly one evaluation succeeds in creating orders, while the second produces an idempotent skip.
    """
    session.commit()
    runtime_id = str(provider_exec_setup["runtime"].id)
    user_id = str(provider_exec_setup["user"].id)
    clock_now = provider_exec_setup["clock_now"]
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    results = []
    errors = []

    session_factory = sessionmaker(bind=session.bind)

    def run_eval():
        local_db = session_factory()
        try:
            eng = ProviderExecutionEngine(clock=lambda: clock_now)
            res = eng.evaluate_runtime_candle(
                local_db,
                runtime_id=runtime_id,
                owner_id=user_id,
                candle=c,
            )
            local_db.commit()
            results.append(res)
        except Exception as e:
            local_db.rollback()
            errors.append(e)
        finally:
            local_db.close()

    t1 = threading.Thread(target=run_eval)
    t2 = threading.Thread(target=run_eval)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one executed and one skipped idempotently
    assert len(errors) == 0
    assert len(results) == 2
    decisions = [r.action_decision for r in results]
    assert "ACCEPTED_SANDBOX" in decisions
    assert "IDEMPOTENT_SKIPPED" in decisions

    # Exactly 1 order was generated
    order_count = session.query(Order).filter(Order.runtime_id == runtime_id).count()
    assert order_count == 1


# -------------------------------------------------------------------------
# 11. PostgreSQL Parity Test
# -------------------------------------------------------------------------

def test_postgresql_provider_execution_parity(tmp_path, monkeypatch):
    """Verifies that provider execution succeeds seamlessly on PostgreSQL with native transactions."""
    with paper_test_database("postgresql", tmp_path / "pg.db") as (engine, _):
        Base.metadata.create_all(engine)
        SessionPostgres = sessionmaker(bind=engine, autoflush=False)
        with SessionPostgres() as db:
            user_id = str(uuid.uuid4())
            monkeypatch.setenv("APP_ENV", "local")
            monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")
            monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", user_id)
            monkeypatch.setenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "mock_sandbox_token")

            now = datetime.datetime(2026, 10, 6, 10, 0, 0, tzinfo=datetime.timezone.utc)
            user = User(
                id=user_id,
                username="pg_user",
                normalized_username="pg_user",
                email="pg@test.tradepro",
                normalized_email="pg@test.tradepro",
                hashed_password="pw",
                role="EDITOR",
                is_active=True,
            )
            db.add(user)
            db.flush()

            acct = PaperAccount(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                name="Provider Sandbox Account",
                total_cash_units=100000000000,
                reserved_cash_units=0,
                currency="INR",
            )
            db.add(acct)

            strat = Strategy(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                name="Provider Test Strategy",
                timeframe="5m",
                candidate_selection_mode="FIRST_ELIGIBLE",
                payload={
                    "name": "Provider Test Strategy",
                    "timeframe": "5m",
                    "action": {
                        "type": "PAPER_TRADE",
                        "risk_config": {"max_position_size": 100000, "stop_loss_pct": 2},
                    },
                    "global_conditions": {
                        "type": "CONDITION",
                        "id": "c1",
                        "lhs": {"indicator": "PRICE", "symbol": ""},
                        "operator": "GREATER_THAN",
                        "rhs": {"type": "NUMBER", "value": 0},
                    },
                },
            )
            db.add(strat)

            policy = StrategyActionPolicy(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                strategy_id=strat.id,
                name="PG Policy",
                payload={
                    "entry_mapping": {
                        "mapping_id": "auto_entry_1",
                        "rule_target": "GLOBAL",
                        "trigger_status": "ON_TRUE",
                        "instrument_id": "NSE_INDEX|Nifty 50",
                        "side": "BUY",
                        "intent_type": "ENTRY",
                        "quantity": 10,
                        "order_type": "LIMIT",
                        "limit_price": 25000,
                    },
                },
                is_active=True,
            )
            db.add(policy)

            risk_policy = RiskPolicy(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                name="PG Risk Policy",
                payload={
                    "max_quantity_per_order": 100,
                    "max_notional_per_order": 5000000,
                    "max_instrument_exposure": 10000000,
                    "max_total_exposure": 20000000,
                    "max_daily_realized_loss": 500000,
                    "max_open_orders": 10,
                    "max_open_positions": 5,
                    "max_trades_per_day": 50,
                    "max_price_staleness_seconds": 900,
                    "flat_fee": 20,
                    "fee_basis_points": 5,
                },
            )
            db.add(risk_policy)

            mapping = ProviderInstrumentMapping(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                tradepro_instrument_id="NSE_INDEX|Nifty 50",
                provider_instrument_token="NSE_INDEX|Nifty 50",
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY50",
                lot_size_units=1,
                tick_size_units=5,
                freeze_quantity_units=1800,
                verification_status="VERIFIED",
                mapping_version=1,
                created_at=now,
                expiry_date=now + datetime.timedelta(days=30),
            )
            db.add(mapping)

            runtime = StrategyRuntime(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                strategy_id=strat.id,
                action_policy_id=policy.id,
                risk_policy_id=risk_policy.id,
                account_id=acct.id,
                status="RUNNING",
                trading_mode="BROKER_SANDBOX",
                dataset_id="NSE_INDEX|Nifty 50",
                timeframe="5m",
                strategy_snapshot=strat.payload,
                action_policy_snapshot=policy.payload,
                risk_policy_snapshot=risk_policy.payload,
                instrument_spec_snapshot={
                    "instrument_id": "NSE_INDEX|Nifty 50",
                    "quantity_scale": 0,
                    "price_scale": 4,
                    "currency_scale": 4,
                },
                version=1,
                created_at=now,
            )
            db.add(runtime)
            db.flush()

            pg_snap_obj = OrchestrationSnapshot(
                owner_id=user.id,
                runtime_id=runtime.id,
                strategy_version=1,
                strategy_snapshot=strat.payload,
                action_policy_snapshot=policy.payload,
                risk_policy_snapshot=risk_policy.payload,
                instrument_specification={
                    "instrument_id": "NSE_INDEX|Nifty 50",
                    "quantity_scale": 0,
                    "price_scale": 4,
                    "currency_scale": 4,
                },
                provider_mapping=ProviderMappingIdentity(
                    mapping_id=mapping.id,
                    mapping_version=mapping.mapping_version,
                    verification_state="VERIFIED",
                    expiry_at=mapping.expiry_date,
                ),
                source_type="PROVIDER_SANDBOX",
                source_namespace="provider.sandbox.nse",
                datasets=(),
                timeframe="5m",
                source_policy_version="provider_completed_v1",
                alignment_offset_seconds=0,
                replay_open_at=now - datetime.timedelta(days=1),
                replay_close_at=now + datetime.timedelta(days=1),
                execution_policy="EXTERNAL_SANDBOX_DISPATCH",
                external_transmission_allowed=False,
            )
            pg_snap_json = canonical_json(pg_snap_obj.model_dump(mode="python"))
            pg_snap_fp = orchestration_snapshot_v1(pg_snap_obj)

            orch_cfg = RuntimeOrchestrationConfig(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                runtime_id=runtime.id,
                source_type="PROVIDER_SANDBOX",
                source_namespace="provider.sandbox.nse",
                execution_policy="EXTERNAL_SANDBOX_DISPATCH",
                snapshot_fingerprint=pg_snap_fp,
                snapshot_json=pg_snap_json,
                consent_at=now,
                consent_policy_version="sandbox_consent_v1",
                consent_fingerprint="0" * 64,
                source_policy_version="provider_completed_v1",
                alignment_offset_seconds=0,
                timeframe="5m",
                replay_open_at=now - datetime.timedelta(days=1),
                replay_close_at=now + datetime.timedelta(days=1),
                fencing_generation=1,
                retry_count=0,
                created_at=now,
                updated_at=now,
            )
            orch_cfg.consent_fingerprint = config_consent_fingerprint(orch_cfg)
            db.add(orch_cfg)
            db.commit()

            c = make_valid_candle(now - datetime.timedelta(minutes=5), open_p=25000.0, high_p=25050.0, low_p=24950.0, close_p=25000.0)
            eng = ProviderExecutionEngine(clock=lambda: now)
            res = eng.evaluate_runtime_candle(
                db,
                runtime_id=runtime.id,
                owner_id=user.id,
                candle=c,
            )
            db.commit()

            assert res.action_decision == "ACCEPTED_SANDBOX"
            assert len(res.order_ids) == 1
            assert len(res.outbox_ids) == 1
            outbox = db.query(SubmissionOutbox).filter(SubmissionOutbox.id == res.outbox_ids[0]).first()
            assert outbox is not None
            assert outbox.action_type == "PLACE"
            assert outbox.status == "PENDING"
            assert outbox.transmission_started_at is None


# -------------------------------------------------------------------------
# 12. Savepoint Isolation on Duplicate Collisions (Finding 5)
# -------------------------------------------------------------------------

def test_failed_duplicate_insert_rolls_back_only_savepoint(session, provider_exec_setup):
    """
    Finding 5: When an evaluation insert fails due to a duplicate collision, only the inner savepoint
    rolls back, preserving unrelated caller work in session.new.
    """
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    # First evaluation succeeds and creates orders/outbox
    res1 = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c,
    )
    assert res1.action_decision == "ACCEPTED_SANDBOX"

    # Reset last_processed_candle_timestamp so engine attempts to insert duplicates
    runtime.last_processed_candle_timestamp = None
    session.autoflush = False

    # Caller adds an unrelated pending object before the duplicate attempt
    unrelated_account = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        name="Unrelated Caller Account",
        total_cash_units=500000000,
        reserved_cash_units=0,
        currency="INR",
    )
    session.add(unrelated_account)
    assert unrelated_account in session.new

    # Second evaluation attempts to insert duplicate rows, hitting unique constraint.
    # The inner savepoint catches the IntegrityError and rolls back only the savepoint.
    res2 = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c,
    )
    assert res2.action_decision == "IDEMPOTENT_SKIPPED"
    assert res2.reason_code == "CANDLE_ALREADY_PROCESSED_CONCURRENT"

    # Unrelated caller work is preserved in session!
    assert unrelated_account in session
    session.flush()
    saved = session.get(PaperAccount, unrelated_account.id)
    assert saved is not None
    assert saved.name == "Unrelated Caller Account"


# -------------------------------------------------------------------------
# 13. End-to-End Worker Integration & Fail-Closed Guard (Finding 6)
# -------------------------------------------------------------------------

def test_e2e_activated_provider_runtime_to_sandbox_outbox_transmission(session, provider_exec_setup):
    """
    Finding 6: End-to-end worker test proving an activated provider runtime processes a completed
    provider candle and hands an order to the existing sandbox outbox without hitting the
    fixture-transmission prohibition.
    """
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)

    # 1. Evaluate completed candle
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))
    eval_res = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c,
    )
    session.commit()

    assert eval_res.action_decision == "ACCEPTED_SANDBOX"
    assert len(eval_res.order_ids) == 1
    assert len(eval_res.outbox_ids) == 1

    outbox_id = eval_res.outbox_ids[0]
    order_id = eval_res.order_ids[0]

    outbox_row = session.get(SubmissionOutbox, outbox_id)
    assert outbox_row.status == "PENDING"
    assert outbox_row.action_type == "PLACE"
    assert outbox_row.priority == 10
    assert outbox_row.transmission_started_at is None

    # 2. Run SandboxOutboxWorker with mock Upstox adapter returning a successful placement
    class MockSuccessAdapter(UpstoxSandboxAdapter):
        def place_order(self, order_payload, idempotency_key):
            return UpstoxPlaceResult(
                provider_order_id="UPSTOX_SANDBOX_ORDER_999",
                status="SUBMITTED",
                raw_response={"status": "success"},
            )

    worker = SandboxOutboxWorker(
        worker_id="test_e2e_worker",
        adapter=MockSuccessAdapter(),
        clock=lambda: clock_now,
    )

    processed = worker.process_batch(session)
    session.commit()

    assert processed >= 1

    # 3. Verify outbox row transitioned to DELIVERED
    session.refresh(outbox_row)
    assert outbox_row.status == "DELIVERED"
    assert outbox_row.transmission_started_at is not None

    # 4. Verify external order link created
    ext_link = (
        session.query(ExternalOrderLink)
        .filter(ExternalOrderLink.order_id == order_id)
        .first()
    )
    assert ext_link is not None
    assert ext_link.provider_order_id == "UPSTOX_SANDBOX_ORDER_999"

    # 5. Verify order transitioned to ACKNOWLEDGED
    order_row = session.get(Order, order_id)
    assert order_row.status == OrderStatus.ACKNOWLEDGED.value


def test_e2e_provider_runtime_ambiguous_outcome_fail_closed(session, provider_exec_setup):
    """
    Finding 6: End-to-end worker test proving ambiguous outcomes remain strictly fail-closed
    to RECONCILIATION_REQUIRED, never auto-retries blindly, and creates an open reconciliation record.
    """
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)

    # 1. Evaluate completed candle
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))
    eval_res = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c,
    )
    session.commit()

    outbox_id = eval_res.outbox_ids[0]
    order_id = eval_res.order_ids[0]

    # 2. Run SandboxOutboxWorker with mock Upstox adapter raising UpstoxAmbiguousError
    class MockAmbiguousAdapter(UpstoxSandboxAdapter):
        def place_order(self, order_payload, idempotency_key):
            raise UpstoxAmbiguousError("SANDBOX_TIMEOUT", "Socket timeout during transmission")

    worker = SandboxOutboxWorker(
        worker_id="test_ambiguous_worker",
        adapter=MockAmbiguousAdapter(),
        clock=lambda: clock_now,
    )

    processed = worker.process_batch(session)
    session.commit()
    assert processed >= 1

    # 3. Verify outbox transitioned to RECONCILIATION_REQUIRED
    outbox_row = session.get(SubmissionOutbox, outbox_id)
    assert outbox_row.status == "RECONCILIATION_REQUIRED"
    assert outbox_row.transmission_started_at is not None

    # 4. Verify order transitioned to RECONCILIATION_REQUIRED
    order_row = session.get(Order, order_id)
    assert order_row.status == OrderStatus.RECONCILIATION_REQUIRED.value

    # 5. Verify open ReconciliationRecord exists
    recon = (
        session.query(ReconciliationRecord)
        .filter(ReconciliationRecord.order_id == order_id)
        .first()
    )
    assert recon is not None
    assert recon.status == "OPEN"


# -------------------------------------------------------------------------
# 14. Negative Regression Probes & Public Activation Acceptance
# -------------------------------------------------------------------------

def test_negative_probe1_persisted_consent_fingerprint_mismatch(session, provider_exec_setup):
    """Negative Probe 1: Persisted consent fingerprint modified in DB fails closed to ConsentMissingError."""
    import sqlalchemy as sa
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    orch_cfg = provider_exec_setup["orch_cfg"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    # Tamper consent fingerprint in database via raw SQL (bypasses ORM before_update immutability check)
    session.execute(
        sa.text("UPDATE runtime_orchestration_configs SET consent_fingerprint = :fp WHERE id = :id"),
        {"fp": "0" * 64, "id": orch_cfg.id},
    )
    session.commit()

    with pytest.raises(ConsentMissingError) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c,
        )
    assert "does not match persisted consent fingerprint" in str(exc.value)


def test_negative_probe1_persisted_snapshot_fingerprint_mismatch(session, provider_exec_setup):
    """Negative Probe 1: Persisted snapshot fingerprint modified in DB fails closed to ConsentMissingError."""
    import sqlalchemy as sa
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    orch_cfg = provider_exec_setup["orch_cfg"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    # Tamper snapshot fingerprint in database via raw SQL with a valid 64-hex string that does not match
    session.execute(
        sa.text("UPDATE runtime_orchestration_configs SET snapshot_fingerprint = :fp WHERE id = :id"),
        {"fp": "1" * 64, "id": orch_cfg.id},
    )
    session.commit()

    with pytest.raises(ConsentMissingError) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c,
        )
    assert "does not match persisted snapshot fingerprint" in str(exc.value)


def test_negative_probe1_mapping_id_mismatch(session, provider_exec_setup):
    """Negative Probe 1: Active mapping ID different from frozen snapshot mapping ID fails closed."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    mapping = provider_exec_setup["mapping"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    # Delete existing mapping first so unique constraint on (owner_id, tradepro_instrument_id, mapping_version) is not violated
    session.delete(mapping)
    session.commit()

    new_mapping = ProviderInstrumentMapping(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        tradepro_instrument_id="NSE_INDEX|Nifty 50",
        provider_instrument_token="NSE_INDEX|Nifty 50",
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY50",
        verification_status="VERIFIED",
        verified_by=user.id,
        verified_at=clock_now,
    )
    session.add(new_mapping)
    session.commit()

    with pytest.raises((ConsentMissingError, MappingNotFoundError)) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c,
        )
    assert "mapping" in str(exc.value).lower()


def test_negative_probe2_history_candle_at_current_timestamp_differing_content(session, provider_exec_setup):
    """Negative Probe 2: History candle with current timestamp and differing content raises ConflictingIntervalError."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c_ts = clock_now - datetime.timedelta(minutes=10)
    current_candle = make_valid_candle(c_ts, close_p=25010.0)

    # History candle has exact same timestamp but differing close price
    history_differing = make_valid_candle(c_ts, close_p=25099.0)

    with pytest.raises(ConflictingIntervalError) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=current_candle,
            candle_history=[history_differing],
        )
    assert "has conflicting content compared to current candle" in str(exc.value)


def test_negative_probe2_history_candle_at_current_timestamp_identical_content(session, provider_exec_setup):
    """Negative Probe 2: History candle with current timestamp and identical content raises DuplicateCandleError."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c_ts = clock_now - datetime.timedelta(minutes=10)
    current_candle = make_valid_candle(c_ts, close_p=25010.0)
    history_identical = make_valid_candle(c_ts, close_p=25010.0)

    with pytest.raises(DuplicateCandleError) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=current_candle,
            candle_history=[history_identical],
        )
    assert "detected in history and current input" in str(exc.value)


def test_negative_probe2_missing_provider_provenance_rejected(session, provider_exec_setup):
    """Negative Probe 2: Candle without validated Phase 5 provider provenance is strictly rejected."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c_ts = clock_now - datetime.timedelta(minutes=10)
    candle_no_prov = make_valid_candle(c_ts, provenance=None)

    with pytest.raises(ProviderExecutionError) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=candle_no_prov,
        )
    assert "Phase 5 provider provenance" in str(exc.value)


def test_negative_probe2_invalid_provider_provenance_rejected(session, provider_exec_setup):
    """Negative Probe 2: Candle with non-provider provenance or corrupted fingerprint is strictly rejected."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c_ts = clock_now - datetime.timedelta(minutes=10)

    # 1. FIXTURE_REPLAY source_type is rejected
    candle_fixture = make_valid_candle(c_ts)
    fixture_prov = make_valid_provenance(candle_fixture, source_type="FIXTURE_REPLAY")
    candle_fixture = MarketDataCandle(
        timestamp=candle_fixture.timestamp,
        open=candle_fixture.open,
        high=candle_fixture.high,
        low=candle_fixture.low,
        close=candle_fixture.close,
        open_units=candle_fixture.open_units,
        high_units=candle_fixture.high_units,
        low_units=candle_fixture.low_units,
        close_units=candle_fixture.close_units,
        volume=candle_fixture.volume,
        is_closed=candle_fixture.is_closed,
        provenance=fixture_prov,
    )
    with pytest.raises(ProviderExecutionError) as exc1:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=candle_fixture,
        )
    assert "Invalid market data provenance source_type" in str(exc1.value)

    # 2. Invalid short content_fingerprint is rejected
    from src.engine.market_data.contracts import MarketDataProvenance
    corrupted_prov = MarketDataProvenance(
        provider="UPSTOX",
        source_type="PROVIDER_SANDBOX",
        retrieved_at=datetime.datetime.now(datetime.timezone.utc),
        requested_instrument_key="NSE_INDEX|Nifty 50",
        timeframe="5m",
        mode="intraday",
        candle_count=1,
        content_fingerprint="short_invalid_fingerprint",
        completeness="COMPLETE",
        is_complete_series=True,
    )
    candle_corrupt = MarketDataCandle(
        timestamp=candle_fixture.timestamp,
        open=candle_fixture.open,
        high=candle_fixture.high,
        low=candle_fixture.low,
        close=candle_fixture.close,
        open_units=candle_fixture.open_units,
        high_units=candle_fixture.high_units,
        low_units=candle_fixture.low_units,
        close_units=candle_fixture.close_units,
        volume=candle_fixture.volume,
        is_closed=candle_fixture.is_closed,
        provenance=corrupted_prov,
    )
    with pytest.raises(ProviderExecutionError) as exc2:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=candle_corrupt,
        )
    assert "valid 64-character SHA-256 content fingerprint" in str(exc2.value)


def test_negative_probe_wrong_provider_instrument_provenance_rejected(session, provider_exec_setup):
    """Negative Finding 1: Provenance with mismatched instrument token is strictly rejected."""
    from src.engine.market_data.provenance import compute_market_data_fingerprint
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c_ts = clock_now - datetime.timedelta(minutes=10)
    c = make_valid_candle(c_ts)
    wrong_prov = MarketDataProvenance(
        provider="UPSTOX",
        source_type="PROVIDER_SANDBOX",
        retrieved_at=clock_now,
        requested_instrument_key="WRONG_INSTRUMENT_TOKEN",
        timeframe="5m",
        mode="intraday",
        candle_count=1,
        content_fingerprint=compute_market_data_fingerprint([c]),
        completeness="COMPLETE",
        is_complete_series=True,
    )
    c_wrong = c.model_copy(update={"provenance": wrong_prov})

    with pytest.raises(ProviderExecutionError) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c_wrong,
        )
    assert "instrument token" in str(exc.value).lower()


def test_negative_probe_wrong_provenance_timeframe_rejected(session, provider_exec_setup):
    """Negative Finding 2: Provenance with mismatched timeframe is strictly rejected."""
    from src.engine.market_data.provenance import compute_market_data_fingerprint
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c_ts = clock_now - datetime.timedelta(minutes=10)
    c = make_valid_candle(c_ts)
    wrong_prov = MarketDataProvenance(
        provider="UPSTOX",
        source_type="PROVIDER_SANDBOX",
        retrieved_at=clock_now,
        requested_instrument_key="NSE_INDEX|Nifty 50",
        timeframe="15m",
        mode="intraday",
        candle_count=1,
        content_fingerprint=compute_market_data_fingerprint([c]),
        completeness="COMPLETE",
        is_complete_series=True,
    )
    c_wrong = c.model_copy(update={"provenance": wrong_prov})

    with pytest.raises(ProviderExecutionError) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c_wrong,
        )
    assert "timeframe" in str(exc.value).lower()


def test_negative_probe_fabricated_content_fingerprint_rejected(session, provider_exec_setup):
    """Negative Finding 3: Fabricated content fingerprint ('0' * 64) is strictly rejected by recomputation."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c_ts = clock_now - datetime.timedelta(minutes=10)
    c = make_valid_candle(c_ts)
    fab_prov = MarketDataProvenance(
        provider="UPSTOX",
        source_type="PROVIDER_SANDBOX",
        retrieved_at=clock_now,
        requested_instrument_key="NSE_INDEX|Nifty 50",
        timeframe="5m",
        mode="intraday",
        candle_count=1,
        content_fingerprint="0" * 64,
        completeness="COMPLETE",
        is_complete_series=True,
    )
    c_fab = c.model_copy(update={"provenance": fab_prov})

    with pytest.raises(ProviderExecutionError) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c_fab,
        )
    assert "fingerprint mismatch" in str(exc.value).lower()


def test_negative_probe_altered_ohlcv_with_unchanged_provenance_rejected(session, provider_exec_setup):
    """Negative Finding 4: Altered OHLCV with unchanged genuine provenance fingerprint is strictly rejected."""
    from src.engine.market_data.provenance import compute_market_data_fingerprint
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c_ts = clock_now - datetime.timedelta(minutes=10)
    c_orig = make_valid_candle(c_ts, close_p=25000.0)
    orig_fp = compute_market_data_fingerprint([c_orig])

    valid_prov = MarketDataProvenance(
        provider="UPSTOX",
        source_type="PROVIDER_SANDBOX",
        retrieved_at=clock_now,
        requested_instrument_key="NSE_INDEX|Nifty 50",
        timeframe="5m",
        mode="intraday",
        candle_count=1,
        content_fingerprint=orig_fp,
        completeness="COMPLETE",
        is_complete_series=True,
    )

    c_altered = MarketDataCandle(
        timestamp=c_orig.timestamp,
        open=c_orig.open,
        high=Decimal("25150.0000"),
        low=c_orig.low,
        close=Decimal("25100.0000"),
        open_units=c_orig.open_units,
        high_units=251500000,
        low_units=c_orig.low_units,
        close_units=251000000,
        volume=c_orig.volume,
        is_closed=True,
        provenance=valid_prov,
    )

    with pytest.raises(ProviderExecutionError) as exc:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c_altered,
        )
    assert "fingerprint mismatch" in str(exc.value).lower()


def test_persisted_candle_event_content_fingerprint(session, provider_exec_setup):
    """Verifies that CompletedCandleEvent persists the actual validated content fingerprint."""
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    res = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c,
    )
    session.commit()

    candle_ev = (
        session.query(CompletedCandleEvent)
        .filter(CompletedCandleEvent.runtime_id == runtime.id)
        .first()
    )
    assert candle_ev is not None
    assert len(candle_ev.content_fingerprint) == 64


def _create_exit_test_environment(session, user, account, mapping, risk_policy, clock_now):
    """Helper creating a runtime with an exit strategy mapping where rule evaluates to FALSE."""
    strat = Strategy(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        name="Exit Test Strategy",
        timeframe="5m",
        candidate_selection_mode="FIRST_ELIGIBLE",
        payload={
            "name": "Exit Test Strategy",
            "timeframe": "5m",
            "action": {
                "type": "PAPER_TRADE",
                "risk_config": {
                    "max_position_size": 100000,
                    "stop_loss_pct": 2,
                    "take_profit_pct": 5,
                    "validity_window": 5,
                },
            },
            "global_conditions": {
                "type": "CONDITION",
                "id": "c1",
                "lhs": {"indicator": "PRICE", "symbol": ""},
                "operator": "LESS_THAN",
                "rhs": {"type": "NUMBER", "value": 0},
            },
        },
    )
    session.add(strat)

    policy = StrategyActionPolicy(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        strategy_id=strat.id,
        name="Exit Test Policy",
        version=1,
        payload={
            "entry_mapping": {
                "mapping_id": "auto_entry_1",
                "rule_target": "GLOBAL",
                "trigger_status": "ON_TRUE",
                "instrument_id": "NSE_INDEX|Nifty 50",
                "side": "BUY",
                "order_type": "LIMIT",
                "limit_price": 25000,
                "quantity": 50,
                "time_in_force": "DAY",
                "cooldown_bars": 1,
                "intent_type": "ENTRY",
            },
            "exit_mapping": {
                "mapping_id": "auto_exit_1",
                "rule_target": "GLOBAL",
                "trigger_status": "ON_FALSE",
                "instrument_id": "NSE_INDEX|Nifty 50",
                "side": "SELL",
                "order_type": "MARKET",
                "quantity": 50,
                "time_in_force": "DAY",
                "intent_type": "EXIT",
            },
            "position_exists_behavior": "IGNORE",
            "max_entries_per_day": 5,
        },
        is_active=True,
    )
    session.add(policy)

    runtime = StrategyRuntime(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        strategy_id=strat.id,
        action_policy_id=policy.id,
        risk_policy_id=risk_policy.id,
        account_id=account.id,
        status="RUNNING",
        trading_mode="BROKER_SANDBOX",
        dataset_id="NSE_INDEX|Nifty 50",
        timeframe="5m",
        strategy_snapshot=strat.payload,
        action_policy_snapshot=policy.payload,
        risk_policy_snapshot=risk_policy.payload,
        instrument_spec_snapshot={
            "instrument_id": "NSE_INDEX|Nifty 50",
            "quantity_scale": 0,
            "price_scale": 4,
            "currency_scale": 4,
        },
        version=1,
    )
    session.add(runtime)
    session.flush()

    snap_obj = OrchestrationSnapshot(
        owner_id=user.id,
        runtime_id=runtime.id,
        strategy_version=1,
        strategy_snapshot=strat.payload,
        action_policy_snapshot=policy.payload,
        risk_policy_snapshot=risk_policy.payload,
        instrument_specification=runtime.instrument_spec_snapshot,
        provider_mapping=ProviderMappingIdentity(
            mapping_id=mapping.id,
            mapping_version=mapping.mapping_version,
            verification_state="VERIFIED",
            expiry_at=mapping.expiry_date,
        ),
        source_type="PROVIDER_SANDBOX",
        source_namespace="provider.sandbox.nse",
        datasets=(),
        timeframe="5m",
        source_policy_version="provider_completed_v1",
        alignment_offset_seconds=0,
        replay_open_at=clock_now - datetime.timedelta(days=1),
        replay_close_at=clock_now + datetime.timedelta(days=1),
        execution_policy="EXTERNAL_SANDBOX_DISPATCH",
        external_transmission_allowed=False,
    )
    snap_json = canonical_json(snap_obj.model_dump(mode="python"))
    snap_fp = orchestration_snapshot_v1(snap_obj)

    orch_cfg = RuntimeOrchestrationConfig(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        runtime_id=runtime.id,
        source_type="PROVIDER_SANDBOX",
        source_namespace="provider.sandbox.nse",
        execution_policy="EXTERNAL_SANDBOX_DISPATCH",
        snapshot_fingerprint=snap_fp,
        snapshot_json=snap_json,
        consent_at=clock_now,
        consent_policy_version="sandbox_consent_v1",
        consent_fingerprint="0" * 64,
        source_policy_version="provider_completed_v1",
        alignment_offset_seconds=0,
        timeframe="5m",
        replay_open_at=clock_now - datetime.timedelta(days=1),
        replay_close_at=clock_now + datetime.timedelta(days=1),
        fencing_generation=1,
        retry_count=0,
        created_at=clock_now,
        updated_at=clock_now,
    )
    orch_cfg.consent_fingerprint = config_consent_fingerprint(orch_cfg)
    session.add(orch_cfg)
    session.commit()

    return strat, policy, runtime, orch_cfg


def test_exit_path_evaluation_status_and_cancellation_outbox(session, provider_exec_setup):
    """Exit path: When rule evaluates to FALSE, exit action triggers and cancels open orders with priority 0."""
    user = provider_exec_setup["user"]
    account = provider_exec_setup["account"]
    mapping = provider_exec_setup["mapping"]
    risk_policy = session.get(RiskPolicy, provider_exec_setup["runtime"].risk_policy_id)
    clock_now = provider_exec_setup["clock_now"]

    strat, policy, runtime, orch_cfg = _create_exit_test_environment(
        session, user, account, mapping, risk_policy, clock_now
    )

    intent = OrderIntent(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="auto_entry_1",
        requested_instrument_id="NSE_INDEX|Nifty 50",
        resolved_instrument_id="NSE_INDEX|Nifty 50",
        intent_type="ENTRY",
        reduce_only=False,
        side=OrderSide.BUY.value,
        quantity_units=50,
        order_type=OrderType.LIMIT.value,
        limit_price_units=250000000,
        time_in_force="DAY",
        source_candle_timestamp=clock_now - datetime.timedelta(minutes=15),
        source_evaluation_fingerprint="dummy_fp",
        trigger_event_key="dummy_key",
        created_at=clock_now - datetime.timedelta(minutes=15),
    )
    session.add(intent)
    session.flush()

    open_order = Order(
        id=str(uuid.uuid4()),
        runtime_id=runtime.id,
        account_id=account.id,
        owner_id=user.id,
        intent_id=intent.id,
        order_sequence_number=1,
        instrument_id="NSE_INDEX|Nifty 50",
        order_type=OrderType.LIMIT.value,
        side=OrderSide.BUY.value,
        status=OrderStatus.PENDING_SUBMISSION.value,
        quantity_units=50,
        filled_quantity_units=0,
        limit_price_units=250000000,
        created_at=clock_now - datetime.timedelta(minutes=15),
        updated_at=clock_now - datetime.timedelta(minutes=15),
    )
    session.add(open_order)
    session.commit()

    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    res = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c,
    )
    session.commit()

    assert res.action_decision == "CANCEL_SUBMITTED"
    assert len(res.outbox_ids) >= 1

    eval_row = (
        session.query(RuntimeEvaluation)
        .filter(RuntimeEvaluation.runtime_id == runtime.id)
        .first()
    )
    assert eval_row is not None
    assert eval_row.evaluation_status == "FALSE"
    assert eval_row.action_outcome == "ACCEPTED_SANDBOX"

    outbox_item = session.get(SubmissionOutbox, res.outbox_ids[0])
    assert outbox_item is not None
    assert outbox_item.action_type == "CANCEL"
    assert outbox_item.priority == 0


def test_exit_path_no_open_orders_persists_no_action(session, provider_exec_setup):
    """Exit path: When rule evaluates to FALSE and no open orders exist, persists NO_ACTION evaluation."""
    user = provider_exec_setup["user"]
    account = provider_exec_setup["account"]
    mapping = provider_exec_setup["mapping"]
    risk_policy = session.get(RiskPolicy, provider_exec_setup["runtime"].risk_policy_id)
    clock_now = provider_exec_setup["clock_now"]

    strat, policy, runtime, orch_cfg = _create_exit_test_environment(
        session, user, account, mapping, risk_policy, clock_now
    )

    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    res = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c,
    )
    session.commit()

    assert res.action_decision == "NO_ACTION"
    assert res.reason_code == "NO_OPEN_ORDERS_TO_CANCEL"

    eval_row = (
        session.query(RuntimeEvaluation)
        .filter(RuntimeEvaluation.runtime_id == runtime.id)
        .first()
    )
    assert eval_row is not None
    assert eval_row.evaluation_status == "FALSE"
    assert eval_row.action_outcome == "NO_ACTION"
    assert eval_row.risk_outcome == "NOT_RUN"
    assert eval_row.no_order_reason == "NO_OPEN_ORDERS_TO_CANCEL"


def test_acknowledged_sandbox_order_cancelled_by_exit_action(session, provider_exec_setup):
    """
    Task 2: Verified exit cancellation of an ACKNOWLEDGED sandbox order.
    1. Obtains mocked broker acknowledgement (ACKNOWLEDGED status + ExternalOrderLink).
    2. Exit trigger creates priority-0 CANCEL outbox row (action_decision=CANCEL_SUBMITTED).
    3. SandboxOutboxWorker transmits cancellation, transitions order to CANCELLED,
       emits OrderEvent with actor=UPSTOX_SANDBOX, and releases reserved cash.
    4. Ambiguity handling: when cancel_order raises UpstoxAmbiguousError, fails closed
       to RECONCILIATION_REQUIRED with an open ReconciliationRecord.
    5. Repeated exits: subsequent exit evaluations return NO_ACTION (NO_OPEN_ORDERS_TO_CANCEL)
       and do not create duplicate cancellation outboxes.
    """
    user = provider_exec_setup["user"]
    account = provider_exec_setup["account"]
    mapping = provider_exec_setup["mapping"]
    risk_policy = session.get(RiskPolicy, provider_exec_setup["runtime"].risk_policy_id)
    clock_now = provider_exec_setup["clock_now"]

    strat, policy, runtime, orch_cfg = _create_exit_test_environment(
        session, user, account, mapping, risk_policy, clock_now
    )

    # 1. Create order in ACKNOWLEDGED status with an ExternalOrderLink and reserved cash
    reserved_amount = 250000000 * 50
    account.reserved_cash_units = reserved_amount
    session.flush()

    intent = OrderIntent(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="auto_entry_1",
        requested_instrument_id="NSE_INDEX|Nifty 50",
        resolved_instrument_id="NSE_INDEX|Nifty 50",
        intent_type="ENTRY",
        reduce_only=False,
        side=OrderSide.BUY.value,
        quantity_units=50,
        order_type=OrderType.LIMIT.value,
        limit_price_units=250000000,
        time_in_force="DAY",
        source_candle_timestamp=clock_now - datetime.timedelta(minutes=15),
        source_evaluation_fingerprint="dummy_fp",
        trigger_event_key="dummy_key",
        created_at=clock_now - datetime.timedelta(minutes=15),
    )
    session.add(intent)
    session.flush()

    ack_order = Order(
        id=str(uuid.uuid4()),
        runtime_id=runtime.id,
        account_id=account.id,
        owner_id=user.id,
        intent_id=intent.id,
        order_sequence_number=1,
        instrument_id="NSE_INDEX|Nifty 50",
        order_type=OrderType.LIMIT.value,
        side=OrderSide.BUY.value,
        status=OrderStatus.ACKNOWLEDGED.value,
        quantity_units=50,
        filled_quantity_units=0,
        limit_price_units=250000000,
        created_at=clock_now - datetime.timedelta(minutes=15),
        updated_at=clock_now - datetime.timedelta(minutes=15),
    )
    session.add(ack_order)
    session.flush()

    ext_link = ExternalOrderLink(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        order_id=ack_order.id,
        provider_name="UPSTOX",
        provider_order_id="UPSTOX_SANDBOX_ACK_123",
        created_at=clock_now - datetime.timedelta(minutes=15),
    )
    session.add(ext_link)
    session.commit()

    # 2. Trigger exit evaluation
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c1 = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    res = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c1,
    )
    session.commit()

    assert res.action_decision == "CANCEL_SUBMITTED"
    assert len(res.outbox_ids) == 1
    cancel_outbox_id = res.outbox_ids[0]

    outbox_item = session.get(SubmissionOutbox, cancel_outbox_id)
    assert outbox_item.action_type == "CANCEL"
    assert outbox_item.priority == 0
    assert outbox_item.status == "PENDING"

    # 3. Process cancellation via SandboxOutboxWorker with mocked cancel adapter
    class MockCancelSuccessAdapter(UpstoxSandboxAdapter):
        def cancel_order(self, provider_order_id, token):
            assert provider_order_id == "UPSTOX_SANDBOX_ACK_123"
            return UpstoxCancelResult(
                provider_order_id=provider_order_id,
                cancelled=True,
                raw_response={"status": "success"},
            )

    worker = SandboxOutboxWorker(
        worker_id="test_cancel_worker",
        adapter=MockCancelSuccessAdapter(),
        clock=lambda: clock_now,
    )
    processed = worker.process_batch(session)
    session.commit()

    assert processed >= 1
    session.refresh(outbox_item)
    assert outbox_item.status == "DELIVERED"

    session.refresh(ack_order)
    assert ack_order.status == OrderStatus.CANCELLED.value

    # Verify OrderEvent emitted
    evt = (
        session.query(OrderEvent)
        .filter(OrderEvent.order_id == ack_order.id, OrderEvent.new_status == OrderStatus.CANCELLED.value)
        .first()
    )
    assert evt is not None
    assert evt.actor == "UPSTOX_SANDBOX"
    assert evt.reason_code == "CANCEL_ORDER_V3"

    # Verify reserved cash released
    session.refresh(account)
    assert account.reserved_cash_units == 0

    # 4. Repeated exit trigger must be idempotent and create NO duplicate outboxes
    c2 = make_valid_candle(clock_now - datetime.timedelta(minutes=5))
    res2 = engine.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c2,
    )
    session.commit()

    assert res2.action_decision == "NO_ACTION"
    assert res2.reason_code == "NO_OPEN_ORDERS_TO_CANCEL"
    assert len(res2.outbox_ids) == 0

    total_cancel_outboxes = (
        session.query(SubmissionOutbox)
        .filter(SubmissionOutbox.order_id == ack_order.id, SubmissionOutbox.action_type == "CANCEL")
        .count()
    )
    assert total_cancel_outboxes == 1

    # 5. Ambiguity handling test on cancellation
    intent2 = OrderIntent(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="auto_entry_1",
        requested_instrument_id="NSE_INDEX|Nifty 50",
        resolved_instrument_id="NSE_INDEX|Nifty 50",
        intent_type="ENTRY",
        reduce_only=False,
        side=OrderSide.BUY.value,
        quantity_units=50,
        order_type=OrderType.LIMIT.value,
        limit_price_units=250000000,
        time_in_force="DAY",
        source_candle_timestamp=clock_now - datetime.timedelta(minutes=5),
        source_evaluation_fingerprint="dummy_fp_2",
        trigger_event_key="dummy_key_2",
        created_at=clock_now,
    )
    session.add(intent2)
    session.flush()

    ack_order2 = Order(
        id=str(uuid.uuid4()),
        runtime_id=runtime.id,
        account_id=account.id,
        owner_id=user.id,
        intent_id=intent2.id,
        order_sequence_number=2,
        instrument_id="NSE_INDEX|Nifty 50",
        order_type=OrderType.LIMIT.value,
        side=OrderSide.BUY.value,
        status=OrderStatus.ACKNOWLEDGED.value,
        quantity_units=50,
        filled_quantity_units=0,
        limit_price_units=250000000,
        created_at=clock_now,
        updated_at=clock_now,
    )
    session.add(ack_order2)
    session.flush()
    ext_link2 = ExternalOrderLink(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        order_id=ack_order2.id,
        provider_name="UPSTOX",
        provider_order_id="UPSTOX_SANDBOX_ACK_456",
        created_at=clock_now,
    )
    session.add(ext_link2)
    session.commit()

    now3 = clock_now + datetime.timedelta(minutes=5)
    c3 = make_valid_candle(clock_now)
    engine3 = ProviderExecutionEngine(clock=lambda: now3)
    res3 = engine3.evaluate_runtime_candle(
        session,
        runtime_id=runtime.id,
        owner_id=user.id,
        candle=c3,
    )
    session.commit()
    assert res3.action_decision == "CANCEL_SUBMITTED"
    outbox2 = session.get(SubmissionOutbox, res3.outbox_ids[0])

    class MockCancelAmbiguousAdapter(UpstoxSandboxAdapter):
        def cancel_order(self, provider_order_id, token):
            raise UpstoxAmbiguousError("SANDBOX_TIMEOUT", "Cancel timeout")

    worker_amb = SandboxOutboxWorker(
        worker_id="test_cancel_amb_worker",
        adapter=MockCancelAmbiguousAdapter(),
        clock=lambda: now3,
    )
    worker_amb.process_batch(session)
    session.commit()

    session.refresh(outbox2)
    session.refresh(ack_order2)
    assert outbox2.status == "RECONCILIATION_REQUIRED"
    assert ack_order2.status == OrderStatus.RECONCILIATION_REQUIRED.value

    recon = session.query(ReconciliationRecord).filter(ReconciliationRecord.order_id == ack_order2.id).first()
    assert recon is not None
    assert recon.status == "OPEN"


def test_non_unique_integrity_error_propagates_without_exposure(session, provider_exec_setup, monkeypatch):
    """Verified non-unique IntegrityErrors (e.g. check constraints, foreign keys) propagate sanitized without error leakage."""
    from sqlalchemy.exc import IntegrityError
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]
    engine = ProviderExecutionEngine(clock=lambda: clock_now)
    c = make_valid_candle(clock_now - datetime.timedelta(minutes=10))

    orig_error = Exception("CHECK constraint failed: check_cash_positive on table paper_accounts (SQL: INSERT ... raw internal info)")
    def mock_flush(*args, **kwargs):
        raise IntegrityError("INSERT INTO paper_accounts ...", params={}, orig=orig_error)

    real_flush = session.flush
    call_count = [0]
    def intercepted_flush(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:
            mock_flush()
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", intercepted_flush)

    with pytest.raises(ProviderExecutionError) as exc_info:
        engine.evaluate_runtime_candle(
            session,
            runtime_id=runtime.id,
            owner_id=user.id,
            candle=c,
        )

    assert "Database integrity constraint violated." in str(exc_info.value)
    assert "CHECK constraint" not in str(exc_info.value)
    assert "SQL:" not in str(exc_info.value)
    assert "paper_accounts" not in str(exc_info.value)


def test_public_activation_and_execution_acceptance(session, test_user, client, monkeypatch):
    """
    Public path acceptance test:
    - Does NOT seed RUNNING configurations directly.
    - Sets up user, account, strategy, policy, verified provider instrument mapping, risk policy.
    - Runtime created in READY status with trading_mode='BROKER_SANDBOX'.
    - Uses public API to configure explicit sandbox consent via POST /api/v1/orchestration/configs.
    - Verifies GET /api/v1/orchestration/runtimes/{id}/readiness returns ready=True.
    - Calls POST /api/v1/orchestration/runtimes/{id}/activate -> transitions READY -> RUNNING.
    - Acquires provider candle with validated Phase 5 provenance.
    - Runs durable evaluation via ProviderExecutionEngine -> creates order + outbox (priority 10).
    - Runs SandboxOutboxWorker batch -> mock Upstox transmission succeeds -> ACKNOWLEDGED, ExternalOrderLink created.
    """
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", test_user.id)
    monkeypatch.setenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "mock_sandbox_token")

    now = datetime.datetime(2026, 10, 6, 10, 0, 0, tzinfo=datetime.timezone.utc)

    # 1. Paper Account
    acct = PaperAccount(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Public Activation Account",
        total_cash_units=100000000000,
        reserved_cash_units=0,
        currency="INR",
    )
    session.add(acct)

    # 2. Strategy
    strat = Strategy(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Public Activation Strategy",
        timeframe="5m",
        candidate_selection_mode="FIRST_ELIGIBLE",
        payload={
            "name": "Public Activation Strategy",
            "timeframe": "5m",
            "action": {
                "type": "PAPER_TRADE",
                "risk_config": {
                    "max_position_size": 100000,
                    "stop_loss_pct": 2,
                    "take_profit_pct": 5,
                    "validity_window": 5,
                },
            },
            "global_conditions": {
                "type": "CONDITION",
                "id": "c1",
                "lhs": {"indicator": "PRICE", "symbol": ""},
                "operator": "GREATER_THAN",
                "rhs": {"type": "NUMBER", "value": 0},
            },
        },
    )
    session.add(strat)

    # 3. Action Policy
    policy = StrategyActionPolicy(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        strategy_id=strat.id,
        name="Public Activation Policy",
        version=1,
        payload={
            "entry_mapping": {
                "mapping_id": "auto_entry_1",
                "rule_target": "GLOBAL",
                "trigger_status": "ON_TRUE",
                "instrument_id": "NSE_INDEX|Nifty 50",
                "side": "BUY",
                "order_type": "LIMIT",
                "limit_price": 25000,
                "quantity": 50,
                "time_in_force": "DAY",
                "cooldown_bars": 1,
                "intent_type": "ENTRY",
            },
            "position_exists_behavior": "IGNORE",
            "max_entries_per_day": 5,
        },
        is_active=True,
    )
    session.add(policy)

    # 4. Verified Provider Instrument Mapping
    mapping = ProviderInstrumentMapping(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        tradepro_instrument_id="NSE_INDEX|Nifty 50",
        provider_instrument_token="NSE_INDEX|Nifty 50",
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY50",
        verification_status="VERIFIED",
        verified_by=test_user.id,
        verified_at=now,
    )
    session.add(mapping)

    # 5. Risk Policy
    risk_policy = RiskPolicy(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        name="Public Activation Risk Policy",
        payload={
            "max_quantity_per_order": 100,
            "max_notional_per_order": 5000000,
            "max_instrument_exposure": 10000000,
            "max_total_exposure": 20000000,
            "max_daily_realized_loss": 500000,
            "max_open_orders": 10,
            "max_open_positions": 5,
            "max_trades_per_day": 50,
            "max_price_staleness_seconds": 900,
            "flat_fee": 20,
            "fee_basis_points": 5,
        },
    )
    session.add(risk_policy)

    # 6. Runtime in READY status (NOT RUNNING!)
    runtime = StrategyRuntime(
        id=str(uuid.uuid4()),
        owner_id=test_user.id,
        strategy_id=strat.id,
        action_policy_id=policy.id,
        risk_policy_id=risk_policy.id,
        account_id=acct.id,
        status="READY",
        trading_mode="BROKER_SANDBOX",
        dataset_id="NSE_INDEX|Nifty 50",
        timeframe="5m",
        strategy_snapshot=strat.payload,
        action_policy_snapshot=policy.payload,
        risk_policy_snapshot=risk_policy.payload,
        instrument_spec_snapshot={
            "instrument_id": "NSE_INDEX|Nifty 50",
            "quantity_scale": 0,
            "price_scale": 4,
            "currency_scale": 4,
        },
        version=1,
    )
    runtime_id = runtime.id
    mapping_id = mapping.id
    session.add(runtime)
    session.commit()

    # Invariant: session has no active transaction after commit, avoiding SQLite locks on subsequent client requests
    assert not session.in_transaction()

    # Step A: POST /api/v1/orchestration/configs with explicit sandbox consent
    open_time = now - datetime.timedelta(days=1)
    close_time = now + datetime.timedelta(days=1)
    cfg_resp = client.post(
        "/api/v1/orchestration/configs",
        json={
            "runtime_id": runtime_id,
            "timeframe": "5m",
            "replay_open_at": open_time.isoformat(),
            "replay_close_at": close_time.isoformat(),
            "strategy_version": 1,
            "provider_mapping_id": mapping_id,
            "source_type": "PROVIDER_SANDBOX",
            "execution_policy": "EXTERNAL_SANDBOX_DISPATCH",
            "consent": {
                "consent_version": "sandbox_consent_v1",
                "acknowledged_source_type": "PROVIDER_SANDBOX",
                "acknowledged_execution_policy": "EXTERNAL_SANDBOX_DISPATCH",
                "acknowledged_timeframe": "5m",
                "acknowledged_replay_open_at": open_time.isoformat(),
                "acknowledged_replay_close_at": close_time.isoformat(),
                "acknowledged_dataset_ids": [],
                "confirm_prohibition_of_live_trading": True,
                "confirm_external_sandbox_dispatch": True,
            },
        },
    )
    assert cfg_resp.status_code == 201, f"Config creation failed: {cfg_resp.text}"
    cfg_data = cfg_resp.json()
    assert cfg_data["source_type"] == "PROVIDER_SANDBOX"
    assert cfg_data["execution_policy"] == "EXTERNAL_SANDBOX_DISPATCH"

    # Step B: GET /api/v1/orchestration/runtimes/{runtime_id}/readiness
    readiness_resp = client.get(f"/api/v1/orchestration/runtimes/{runtime_id}/readiness")
    assert readiness_resp.status_code == 200, f"Readiness check failed: {readiness_resp.text}"
    readiness_data = readiness_resp.json()
    assert readiness_data["ready"] is True, f"Runtime not ready: {readiness_data}"

    # Step C: POST /api/v1/orchestration/runtimes/{runtime_id}/activate
    act_resp = client.post(
        f"/api/v1/orchestration/runtimes/{runtime_id}/activate",
        json={
            "consent_version": "sandbox_consent_v1",
            "acknowledged_execution_policy": "EXTERNAL_SANDBOX_DISPATCH",
            "confirm_external_sandbox_dispatch": True,
        },
    )
    assert act_resp.status_code == 200, f"Activation failed: {act_resp.text}"
    activated_runtime = session.get(StrategyRuntime, runtime_id)
    assert activated_runtime.status == "RUNNING"

    # Step D: Market data provider mock transport boundary (raw Upstox JSON response)
    candle_ts = now - datetime.timedelta(minutes=10)
    raw_candle_data = [
        [candle_ts.isoformat(), 25000.0, 25050.0, 24950.0, 25020.0, 1000, 0]
    ]

    def mock_market_data_handler(request: httpx.Request) -> httpx.Response:
        assert "/historical-candle/intraday" in str(request.url)
        return httpx.Response(
            200,
            json={"status": "success", "data": {"candles": raw_candle_data}},
        )

    md_adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        transport=httpx.MockTransport(mock_market_data_handler),
        access_token="test_mock_token_123",
        network_enabled=True,
        configured_owner_id=test_user.id,
    )

    # Step E: ProviderEvaluationWorker executes the public application path
    eval_worker = ProviderEvaluationWorker(
        worker_id="acceptance_eval_worker",
        market_data_adapter=md_adapter,
        clock=lambda: now,
    )

    eval_res = eval_worker.process_runtime(session, runtime_id)
    assert eval_res is not None
    assert eval_res.action_decision == "ACCEPTED_SANDBOX"
    assert len(eval_res.order_ids) == 1
    assert len(eval_res.outbox_ids) == 1

    order_id = eval_res.order_ids[0]
    outbox_id = eval_res.outbox_ids[0]

    outbox_row = session.get(SubmissionOutbox, outbox_id)
    assert outbox_row.status == "PENDING"
    assert outbox_row.priority == 10

    # Step F: SandboxOutboxWorker batch processing -> mocked Upstox transmission
    class MockPublicAdapter(UpstoxSandboxAdapter):
        def place_order(self, order_payload, idempotency_key):
            return UpstoxPlaceResult(
                provider_order_id="UPSTOX_PUBLIC_ORDER_001",
                status="SUBMITTED",
                raw_response={"status": "success"},
            )

    worker = SandboxOutboxWorker(
        worker_id="test_public_acceptance_worker",
        adapter=MockPublicAdapter(),
        clock=lambda: now,
    )
    processed = worker.process_batch(session)
    session.commit()

    assert processed >= 1
    session.refresh(outbox_row)
    assert outbox_row.status == "DELIVERED"

    ext_link = session.query(ExternalOrderLink).filter(ExternalOrderLink.order_id == order_id).first()
    assert ext_link is not None
    assert ext_link.provider_order_id == "UPSTOX_PUBLIC_ORDER_001"

    order_row = session.get(Order, order_id)
    assert order_row.status == OrderStatus.ACKNOWLEDGED.value


def _make_mock_md_adapter(candles_data, user_id):
    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert "/historical-candle/intraday" in str(request.url)
        return httpx.Response(
            200,
            json={"status": "success", "data": {"candles": candles_data}},
        )

    return UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        transport=httpx.MockTransport(mock_handler),
        access_token="test_mock_token_123",
        network_enabled=True,
        configured_owner_id=user_id,
    )


def test_provider_worker_consecutive_candles_replay_and_scoped_provenance(session, provider_exec_setup):
    """
    Requirement 3: Three consecutive 5m candles.
    Start with a checkpoint at the first candle's close.
    Assert cycle 1 evaluates the second candle, then cycle 2 evaluates the third,
    with no skipped boundary or fingerprint rejection.
    """
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]

    c0_ts = clock_now - datetime.timedelta(minutes=20)
    c1_ts = clock_now - datetime.timedelta(minutes=15)
    c2_ts = clock_now - datetime.timedelta(minutes=10)

    # Checkpoint at C0 close (c0_ts + 5m == c1_ts)
    c0_close = c0_ts + datetime.timedelta(minutes=5)
    runtime.last_processed_candle_timestamp = c0_close
    session.commit()

    raw_candles = [
        [c0_ts.isoformat(), 25000.0, 25050.0, 24950.0, 25010.0, 1000, 0],
        [c1_ts.isoformat(), 25010.0, 25060.0, 24960.0, 25020.0, 1200, 0],
        [c2_ts.isoformat(), 25020.0, 25070.0, 24970.0, 25030.0, 1500, 0],
    ]

    adapter = _make_mock_md_adapter(raw_candles, user.id)
    worker = ProviderEvaluationWorker(
        worker_id="worker_consecutive_test",
        market_data_adapter=adapter,
        clock=lambda: clock_now,
    )

    # Cycle 1: Must evaluate C1 (not skipped, not jumped to C2)
    res1 = worker.process_runtime(session, runtime.id)
    assert res1 is not None
    assert res1.candle_timestamp == c0_close  # c0_close == c1_ts
    assert res1.action_decision in ("ACCEPTED_SANDBOX", "NO_ACTION")
    session.refresh(runtime)
    c1_close = c1_ts + datetime.timedelta(minutes=5)
    assert runtime.last_processed_candle_timestamp == c1_close

    # Cycle 2: Must evaluate C2
    res2 = worker.process_runtime(session, runtime.id)
    assert res2 is not None
    assert res2.candle_timestamp == c1_close  # c1_close == c2_ts
    assert res2.action_decision in ("ACCEPTED_SANDBOX", "NO_ACTION")
    session.refresh(runtime)
    c2_close = c2_ts + datetime.timedelta(minutes=5)
    assert runtime.last_processed_candle_timestamp == c2_close


def test_provider_worker_initial_backlog_replay(session, provider_exec_setup):
    """
    Requirement 3: Initial backlog replay with multiple available closed candles.
    Processes C0, then C1, then C2 in sequential order without skipping.
    """
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]

    c0_ts = clock_now - datetime.timedelta(minutes=20)
    c1_ts = clock_now - datetime.timedelta(minutes=15)
    c2_ts = clock_now - datetime.timedelta(minutes=10)

    # Initial state: no checkpoint
    runtime.last_processed_candle_timestamp = None
    session.commit()

    raw_candles = [
        [c0_ts.isoformat(), 25000.0, 25050.0, 24950.0, 25010.0, 1000, 0],
        [c1_ts.isoformat(), 25010.0, 25060.0, 24960.0, 25020.0, 1200, 0],
        [c2_ts.isoformat(), 25020.0, 25070.0, 24970.0, 25030.0, 1500, 0],
    ]

    adapter = _make_mock_md_adapter(raw_candles, user.id)
    worker = ProviderEvaluationWorker(
        worker_id="worker_backlog_test",
        market_data_adapter=adapter,
        clock=lambda: clock_now,
    )

    # Cycle 1 evaluates C0
    res0 = worker.process_runtime(session, runtime.id)
    assert res0 is not None
    assert res0.candle_timestamp == c0_ts
    session.refresh(runtime)
    assert runtime.last_processed_candle_timestamp == c0_ts + datetime.timedelta(minutes=5)

    # Cycle 2 evaluates C1
    res1 = worker.process_runtime(session, runtime.id)
    assert res1 is not None
    assert res1.candle_timestamp == c1_ts
    session.refresh(runtime)
    assert runtime.last_processed_candle_timestamp == c1_ts + datetime.timedelta(minutes=5)

    # Cycle 3 evaluates C2
    res2 = worker.process_runtime(session, runtime.id)
    assert res2 is not None
    assert res2.candle_timestamp == c2_ts
    session.refresh(runtime)
    assert runtime.last_processed_candle_timestamp == c2_ts + datetime.timedelta(minutes=5)


def test_provider_worker_missing_intermediate_candle_fails_closed(session, provider_exec_setup):
    """
    Requirement 3: Missing intermediate candle between checkpoint and available series.
    Start with checkpoint at C0 close. Provider returns C0 and C2 (C1 missing).
    Worker must fail closed, refuse to jump to C2, and preserve checkpoint.
    """
    runtime = provider_exec_setup["runtime"]
    user = provider_exec_setup["user"]
    clock_now = provider_exec_setup["clock_now"]

    c0_ts = clock_now - datetime.timedelta(minutes=20)
    # c1_ts = clock_now - datetime.timedelta(minutes=15) is OMITTED!
    c2_ts = clock_now - datetime.timedelta(minutes=10)

    # Checkpoint at C0 close
    c0_close = c0_ts + datetime.timedelta(minutes=5)
    runtime.last_processed_candle_timestamp = c0_close
    session.commit()

    # Provider returns C0 and C2 (gap between 10:05 and 10:10)
    raw_candles = [
        [c0_ts.isoformat(), 25000.0, 25050.0, 24950.0, 25010.0, 1000, 0],
        [c2_ts.isoformat(), 25020.0, 25070.0, 24970.0, 25030.0, 1500, 0],
    ]

    adapter = _make_mock_md_adapter(raw_candles, user.id)
    worker = ProviderEvaluationWorker(
        worker_id="worker_gap_test",
        market_data_adapter=adapter,
        clock=lambda: clock_now,
    )

    with pytest.raises(ProviderExecutionError) as exc:
        worker.process_runtime(session, runtime.id)

    assert "missing" in str(exc.value).lower() or "gap" in str(exc.value).lower()
    session.refresh(runtime)
    # Checkpoint remains unchanged at C0 close — did NOT jump to C2!
    assert runtime.last_processed_candle_timestamp == c0_close


def test_provider_evaluation_worker_cli_safety(monkeypatch):
    """Verify provider-evaluation-worker CLI argument validation and environment refusal."""
    from src.cli import main

    # 1. Refuse production environment
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr("sys.argv", ["cli.py", "provider-evaluation-worker"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1

    # 2. Refuse invalid CLI arguments
    monkeypatch.setenv("APP_ENV", "development")

    # Invalid batch size
    monkeypatch.setattr("sys.argv", ["cli.py", "provider-evaluation-worker", "--batch-size", "0"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2

    # Invalid lease duration
    monkeypatch.setattr("sys.argv", ["cli.py", "provider-evaluation-worker", "--lease-duration", "2"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2

    # Invalid worker ID characters
    monkeypatch.setattr("sys.argv", ["cli.py", "provider-evaluation-worker", "--worker-id", "bad id with spaces!"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_provider_evaluation_worker_cli_smoke(session, provider_exec_setup, monkeypatch):
    """
    CLI Smoke Test:
    - Proves an activated provider runtime is discovered and evaluated through the command's worker path
      using mocked market data, with no live broker traffic.
    - Proves fixture runtimes remain handled by the existing strategy-evaluation-worker.
    - Confirms provider worker leaves fixture runtimes untouched and strategy worker leaves provider runtimes untouched.
    """
    from src.cli import main
    from src.engine.orchestration.worker import StrategyEvaluationWorker
    from tests.test_orchestration_worker import make_test_user, setup_orchestration_stack

    user = provider_exec_setup["user"]
    provider_runtime = provider_exec_setup["runtime"]
    clock_now = provider_exec_setup["clock_now"]

    # Pre-commit caching of all primitive IDs and initial states to avoid ORM reads after commit
    user_id = str(user.id)
    runtime_id = str(provider_runtime.id)
    initial_provider_last_candle = provider_runtime.last_processed_candle_timestamp

    # 1. Setup a RUNNING fixture replay runtime with distinct user to avoid unique mapping collision
    fixture_user = make_test_user(session, "cli_smoke_fixture_user")
    fixture_runtime, fixture_cfg, _ = setup_orchestration_stack(
        session, fixture_user, status="RUNNING"
    )
    fixture_user_id = str(fixture_user.id)
    fixture_runtime_id = str(fixture_runtime.id)
    fixture_cfg_id = str(fixture_cfg.id)
    initial_fixture_checkpoint = fixture_cfg.checkpoint_close_at

    # Verify initial states using pre-commit cached primitives
    assert initial_provider_last_candle is None
    assert initial_fixture_checkpoint is None

    session.commit()

    # Invariant: session must not be in a transaction after commit before invoking CLI
    assert session.in_transaction() is False

    # 2. Mock market data for provider runtime: completed closed candle
    c_ts = clock_now - datetime.timedelta(minutes=10)
    raw_candle_data = [
        [c_ts.isoformat(), 25000.0, 25050.0, 24950.0, 25020.0, 1000, 0]
    ]

    traffic_attempted = []

    def mock_md_handler(request: httpx.Request) -> httpx.Response:
        traffic_attempted.append(str(request.url))
        assert "historical-candle" in str(request.url)
        return httpx.Response(
            200,
            json={"status": "success", "data": {"candles": raw_candle_data}},
        )

    # Monkeypatch SessionLocal so CLI worker creates disposable sessions bound to our test db
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=session.bind)
    monkeypatch.setattr("src.database.SessionLocal", TestSession)
    monkeypatch.setattr("src.engine.provider_execution.worker.SessionLocal", TestSession)
    monkeypatch.setattr("src.engine.orchestration.worker.SessionLocal", TestSession)

    # Inject mock market data transport into UpstoxMarketDataAdapter (using cached user_id)
    mock_transport = httpx.MockTransport(mock_md_handler)
    orig_adapter_init = UpstoxMarketDataAdapter.__init__

    def patched_adapter_init(self, *args, **kwargs):
        kwargs["transport"] = mock_transport
        kwargs["network_enabled"] = True
        kwargs["configured_owner_id"] = user_id
        kwargs["access_token"] = "mock_token_123"
        orig_adapter_init(self, *args, **kwargs)

    monkeypatch.setattr(UpstoxMarketDataAdapter, "__init__", patched_adapter_init)

    # Patch worker clocks so evaluations align with test timestamp
    orig_prov_init = ProviderEvaluationWorker.__init__

    def patched_prov_init(self, *args, **kwargs):
        if "clock" not in kwargs:
            kwargs["clock"] = lambda: clock_now
        orig_prov_init(self, *args, **kwargs)

    monkeypatch.setattr(ProviderEvaluationWorker, "__init__", patched_prov_init)

    orig_orch_init = StrategyEvaluationWorker.__init__

    def patched_orch_init(self, *args, **kwargs):
        if "clock" not in kwargs:
            kwargs["clock"] = lambda: clock_now
        orig_orch_init(self, *args, **kwargs)

    monkeypatch.setattr(StrategyEvaluationWorker, "__init__", patched_orch_init)

    # 3. Execute provider-evaluation-worker CLI command for 1 run
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setattr("sys.argv", [
        "cli.py", "provider-evaluation-worker",
        "--batch-size", "5",
        "--lease-duration", "15",
        "--poll-interval", "0.5",
        "--worker-id", "smoke-prov-worker",
        "--max-runs", "1",
    ])

    # Invariant: session has no active transaction immediately before running CLI command
    assert session.in_transaction() is False
    main()

    # Assert:
    # A. Provider runtime was discovered and evaluated!
    session.expire_all()
    refreshed_prov_rt = session.get(StrategyRuntime, runtime_id)
    assert refreshed_prov_rt.last_processed_candle_timestamp is not None
    # Candle open is c_ts (5m interval), close boundary is c_ts + 5m
    assert refreshed_prov_rt.last_processed_candle_timestamp == c_ts + datetime.timedelta(minutes=5)

    prov_eval = session.query(RuntimeEvaluation).filter(RuntimeEvaluation.runtime_id == runtime_id).first()
    assert prov_eval is not None
    assert prov_eval.action_outcome == "ACCEPTED_SANDBOX"

    order = session.query(Order).filter(Order.runtime_id == runtime_id).first()
    assert order is not None

    # Outbox item created (priority 10 for PLACE) with zero live broker transmission
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).first()
    assert outbox is not None
    assert outbox.priority == 10
    assert outbox.status == "PENDING"
    assert outbox.transmission_started_at is None  # No live broker transmission!

    # B. Fixture runtime was NOT touched by provider-evaluation-worker!
    refreshed_fix_cfg = session.get(RuntimeOrchestrationConfig, fixture_cfg_id)
    assert refreshed_fix_cfg.checkpoint_close_at is None
    fixture_evals_before = session.query(RuntimeEvaluation).filter(RuntimeEvaluation.runtime_id == fixture_runtime_id).count()
    assert fixture_evals_before == 0

    # C. Confirm market data mock transport handled the acquisition, no live broker traffic
    assert len(traffic_attempted) == 1

    # Close any read transaction opened by later ORM access
    session.commit()
    assert session.in_transaction() is False

    # 4. Now run strategy-evaluation-worker CLI command for 1 run
    monkeypatch.setattr("sys.argv", [
        "cli.py", "strategy-evaluation-worker",
        "--batch-size", "5",
        "--lease-duration", "15",
        "--poll-interval", "0.5",
        "--worker-id", "smoke-fix-worker",
        "--max-runs", "1",
    ])
    assert session.in_transaction() is False
    main()

    # Assert:
    # Fixture runtime WAS discovered and evaluated by strategy-evaluation-worker!
    session.expire_all()
    refreshed_fix_cfg = session.get(RuntimeOrchestrationConfig, fixture_cfg_id)
    assert refreshed_fix_cfg.checkpoint_close_at is not None
    fixture_evals_after = session.query(RuntimeEvaluation).filter(RuntimeEvaluation.runtime_id == fixture_runtime_id).count()
    assert fixture_evals_after >= 1

    # Provider runtime last_processed_candle_timestamp was unchanged by strategy-evaluation-worker
    refreshed_prov_rt = session.get(StrategyRuntime, runtime_id)
    assert refreshed_prov_rt.last_processed_candle_timestamp == c_ts + datetime.timedelta(minutes=5)

    # Close any read transaction opened by later ORM access
    session.commit()
    assert session.in_transaction() is False


def test_postgresql_provider_execution_concurrent_evaluations(tmp_path):
    """PostgreSQL integration & concurrency test for ProviderExecutionEngine."""
    with paper_test_database("postgresql", tmp_path / "pg_prov_conc.db") as (engine, url):
        Base.metadata.create_all(engine)
        maker = sessionmaker(bind=engine)
        now = datetime.datetime(2026, 10, 6, 10, 0, 0, tzinfo=datetime.timezone.utc)

        with maker() as db:
            uname = f"pg_conc_{uuid.uuid4().hex[:8]}"
            uemail = f"{uname}@example.com"
            user = User(
                id=str(uuid.uuid4()),
                username=uname,
                normalized_username=uname.lower(),
                email=uemail,
                normalized_email=uemail.lower(),
                hashed_password="pw",
                role="EDITOR",
                is_active=True,
            )
            db.add(user)
            db.flush()

            acct = PaperAccount(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                name="PG Conc Account",
                total_cash_units=100000000000,
                reserved_cash_units=0,
                currency="INR",
            )
            db.add(acct)

            strat = Strategy(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                name="PG Conc Strategy",
                timeframe="5m",
                candidate_selection_mode="FIRST_ELIGIBLE",
                payload={
                    "name": "PG Conc Strategy",
                    "timeframe": "5m",
                    "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100000}},
                    "global_conditions": {
                        "type": "CONDITION",
                        "id": "c1",
                        "lhs": {"indicator": "PRICE", "symbol": ""},
                        "operator": "GREATER_THAN",
                        "rhs": {"type": "NUMBER", "value": 0},
                    },
                },
            )
            db.add(strat)

            policy = StrategyActionPolicy(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                strategy_id=strat.id,
                name="PG Conc Policy",
                version=1,
                payload={
                    "entry_mapping": {
                        "mapping_id": "auto_entry_1",
                        "rule_target": "GLOBAL",
                        "trigger_status": "ON_TRUE",
                        "instrument_id": "NSE_INDEX|Nifty 50",
                        "side": "BUY",
                        "order_type": "LIMIT",
                        "limit_price": 25000,
                        "quantity": 50,
                        "time_in_force": "DAY",
                        "cooldown_bars": 1,
                        "intent_type": "ENTRY",
                    },
                },
                is_active=True,
            )
            db.add(policy)

            mapping = ProviderInstrumentMapping(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                tradepro_instrument_id="NSE_INDEX|Nifty 50",
                provider_instrument_token="NSE_INDEX|Nifty 50",
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY50",
                verification_status="VERIFIED",
                verified_by=user.id,
                verified_at=now,
            )
            db.add(mapping)

            risk_policy = RiskPolicy(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                name="PG Conc Risk Policy",
                payload={
                    "max_quantity_per_order": 100,
                    "max_notional_per_order": 5000000,
                    "max_instrument_exposure": 10000000,
                    "max_total_exposure": 20000000,
                    "max_daily_realized_loss": 500000,
                    "max_open_orders": 10,
                    "max_open_positions": 5,
                    "max_trades_per_day": 50,
                    "max_price_staleness_seconds": 900,
                    "flat_fee": 20,
                    "fee_basis_points": 5,
                },
            )
            db.add(risk_policy)

            runtime = StrategyRuntime(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                strategy_id=strat.id,
                action_policy_id=policy.id,
                risk_policy_id=risk_policy.id,
                account_id=acct.id,
                status="RUNNING",
                trading_mode="BROKER_SANDBOX",
                dataset_id="NSE_INDEX|Nifty 50",
                timeframe="5m",
                strategy_snapshot=strat.payload,
                action_policy_snapshot=policy.payload,
                risk_policy_snapshot=risk_policy.payload,
                instrument_spec_snapshot={
                    "instrument_id": "NSE_INDEX|Nifty 50",
                    "quantity_scale": 0,
                    "price_scale": 4,
                    "currency_scale": 4,
                },
                version=1,
            )
            db.add(runtime)
            db.flush()

            snap_obj = OrchestrationSnapshot(
                owner_id=user.id,
                runtime_id=runtime.id,
                strategy_version=1,
                strategy_snapshot=strat.payload,
                action_policy_snapshot=policy.payload,
                risk_policy_snapshot=risk_policy.payload,
                instrument_specification=runtime.instrument_spec_snapshot,
                provider_mapping=ProviderMappingIdentity(
                    mapping_id=mapping.id,
                    mapping_version=mapping.mapping_version,
                    verification_state="VERIFIED",
                    expiry_at=mapping.expiry_date,
                ),
                source_type="PROVIDER_SANDBOX",
                source_namespace="provider.sandbox.nse",
                datasets=(),
                timeframe="5m",
                source_policy_version="provider_completed_v1",
                alignment_offset_seconds=0,
                replay_open_at=now - datetime.timedelta(days=1),
                replay_close_at=now + datetime.timedelta(days=1),
                execution_policy="EXTERNAL_SANDBOX_DISPATCH",
                external_transmission_allowed=False,
            )
            pg_snap_json = canonical_json(snap_obj.model_dump(mode="python"))
            pg_snap_fp = orchestration_snapshot_v1(snap_obj)

            orch_cfg = RuntimeOrchestrationConfig(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                runtime_id=runtime.id,
                source_type="PROVIDER_SANDBOX",
                source_namespace="provider.sandbox.nse",
                execution_policy="EXTERNAL_SANDBOX_DISPATCH",
                snapshot_fingerprint=pg_snap_fp,
                snapshot_json=pg_snap_json,
                consent_at=now,
                consent_policy_version="sandbox_consent_v1",
                consent_fingerprint="0" * 64,
                source_policy_version="provider_completed_v1",
                alignment_offset_seconds=0,
                timeframe="5m",
                replay_open_at=now - datetime.timedelta(days=1),
                replay_close_at=now + datetime.timedelta(days=1),
                fencing_generation=1,
                retry_count=0,
                created_at=now,
                updated_at=now,
            )
            orch_cfg.consent_fingerprint = config_consent_fingerprint(orch_cfg)
            db.add(orch_cfg)
            db.commit()

            user_id = user.id
            runtime_id = runtime.id

        results = []
        errors = []

        def worker_task():
            try:
                with maker() as worker_db:
                    c = make_valid_candle(now - datetime.timedelta(minutes=5))
                    eng = ProviderExecutionEngine(clock=lambda: now)
                    res = eng.evaluate_runtime_candle(
                        worker_db,
                        runtime_id=runtime_id,
                        owner_id=user_id,
                        candle=c,
                    )
                    worker_db.commit()
                    results.append(res)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker_task)
        t2 = threading.Thread(target=worker_task)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0, f"Concurrent execution produced errors: {errors}"
        decisions = [r.action_decision for r in results]
        assert "ACCEPTED_SANDBOX" in decisions
        assert "IDEMPOTENT_SKIPPED" in decisions or decisions.count("ACCEPTED_SANDBOX") == 1
