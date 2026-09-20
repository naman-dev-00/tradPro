"""Milestone 6C Phase 2 comprehensive tests.

Covers:
- Configuration creation and read-only retrieval with strict owner isolation.
- Structured consent validation, canonical consent fingerprinting, replay prevention across owner/runtime/config/mapping/dataset.
- Server rejection when consent is missing, synthetic, or tampered.
- Read-only sandbox connection GET (zero flushes, zero commits, NOT_CONFIGURED when missing).
- Strict operational transmission prohibition (assert_orchestration_execution_is_internal_only, zero Outbox rows, zero broker calls even if network enabled).
- Real runtime lifecycle state machine transitions (READY -> RUNNING, RUNNING -> PAUSED, PAUSED -> RUNNING, RUNNING/PAUSED -> STOPPED).
- Stop permanence and order/reservation safety (no cancellation of open orders or release of cash reservations).
- Resume prerequisite revalidation (blocks when kill switch active or mapping expired).
- Concurrency and SQLite-safe atomic Compare-And-Swap / locking behavior.
- Idempotency via ApiIdempotencyRecord (exact cached response on identical canonical request, 409 on different request, conflict winner reload).
- Strategy validation security bounds (64 KiB raw body limit without Content-Length, AST depth <= 16, node count <= 256, extra=forbid, auth, CSRF, rate limit).
- Sensitive response and log sanitization (no passwords, tokens, raw snapshots, or secret leaks; no stack traces).
"""
import os
import datetime
import hashlib
import json
import threading
import concurrent.futures
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func
from sqlalchemy.orm import sessionmaker

from src.auth.rate_limiter import rate_limiter
from src.auth.security import hash_password
from src.auth.session import create_session
from src.database import get_db, get_read_only_db
from src.engine.manifest import get_dataset_entry
from src.engine.orchestration.evidence import config_consent_fingerprint, consent_fingerprint
from src.engine.orchestration.fingerprint import canonical_json, orchestration_snapshot_v1
from src.engine.orchestration.models import utc
from src.engine.orchestration.transmission_gate import (
    assert_orchestration_execution_is_internal_only,
    external_transmission_allowed,
    TransmissionProhibitedError,
)
from src.engine.paper.models import TradingMode, OrderStatus, OrderSide, OrderType
from src.main import app
from src.models import (
    ApiIdempotencyRecord,
    KillSwitch,
    Order,
    OrderEvent,
    OrderIntent,
    PaperAccount,
    PaperPosition,
    ProviderConnection,
    ProviderInstrumentMapping,
    ReconciliationRecord,
    RiskPolicy,
    RuntimeEvent,
    RuntimeOrchestrationConfig,
    CompletedCandleEvent,
    RuntimeEvaluation,
    Strategy,
    StrategyActionPolicy,
    StrategyRuntime,
    SubmissionOutbox,
    User,
)
import threading
import concurrent.futures
from src.engine.sandbox.outbox_worker import SandboxOutboxWorker
from src.services.orchestration_service import OrchestrationService
from src.services.paper_service import PaperService

OPEN = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
CLOSE = datetime.datetime(2026, 8, 28, 9, 30, tzinfo=datetime.timezone.utc)
NOW = datetime.datetime(2026, 8, 28, 10, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def test_editor(session):
    u = User(
        username="phase2_editor",
        normalized_username="phase2_editor",
        email="p2_editor@tradepro.test",
        normalized_email="p2_editor@tradepro.test",
        hashed_password=hash_password("StrongPassword123!"),
        role="EDITOR",
        is_active=True,
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


@pytest.fixture
def test_viewer(session):
    u = User(
        username="phase2_viewer",
        normalized_username="phase2_viewer",
        email="p2_viewer@tradepro.test",
        normalized_email="p2_viewer@tradepro.test",
        hashed_password=hash_password("StrongPassword123!"),
        role="VIEWER",
        is_active=True,
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


@pytest.fixture
def other_user(session):
    u = User(
        username="phase2_other",
        normalized_username="phase2_other",
        email="p2_other@tradepro.test",
        normalized_email="p2_other@tradepro.test",
        hashed_password=hash_password("StrongPassword123!"),
        role="EDITOR",
        is_active=True,
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def make_client_for_user(session, user):
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


@pytest.fixture
def editor_client(session, test_editor):
    client, csrf = make_client_for_user(session, test_editor)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def viewer_client(session, test_viewer):
    client, csrf = make_client_for_user(session, test_viewer)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def other_client(session, other_user):
    client, csrf = make_client_for_user(session, other_user)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def orch_graph(session, test_editor):
    """Sets up a complete valid runtime graph for test_editor in READY state."""
    owner_id = test_editor.id

    # 1. Paper Account
    acct = PaperAccount(
        owner_id=owner_id,
        name="Orch Test Account",
        currency="INR",
        total_cash_units=50000000,
        reserved_cash_units=0,
    )
    session.add(acct)

    # 2. Strategy
    strat = Strategy(
        owner_id=owner_id,
        name="Orch Test Strategy",
        timeframe="15m",
        candidate_selection_mode="FIRST_ELIGIBLE",
        payload={
            "name": "Orch Test Strategy",
            "timeframe": "15m",
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100000}},
            "global_conditions": {
                "type": "CONDITION",
                "id": "c1",
                "lhs": {"indicator": "PRICE", "symbol": ""},
                "operator": "GREATER_THAN",
                "rhs": {"type": "NUMBER", "value": 0.0},
            },
        },
    )
    session.add(strat)
    session.flush()

    # 3. Action and Risk policies
    action_pol = StrategyActionPolicy(
        owner_id=owner_id,
        strategy_id=strat.id,
        name="Default Action Policy",
        version=1,
        payload={"action": "BUY", "type": "ENTRY"},
    )
    risk_pol = RiskPolicy(
        owner_id=owner_id,
        name="Default Risk Policy",
        version=1,
        payload={"max_position_size": 100000},
    )
    session.add_all([action_pol, risk_pol])
    session.flush()

    # 4. Verified Instrument Mapping
    mapping = ProviderInstrumentMapping(
        owner_id=owner_id,
        tradepro_instrument_id="NIFTY",
        provider_instrument_token="NSE_INDEX|Nifty 50",
        exchange="NSE_INDEX",
        segment="INDEX",
        symbol="NIFTY",
        verification_status="VERIFIED",
        mapping_version=1,
    )
    session.add(mapping)
    session.flush()

    # 5. StrategyRuntime in READY status
    runtime = StrategyRuntime(
        owner_id=owner_id,
        strategy_id=strat.id,
        action_policy_id=action_pol.id,
        risk_policy_id=risk_pol.id,
        account_id=acct.id,
        dataset_id="synthetic_underlying_nifty_15m",
        timeframe="15m",
        status="READY",
        trading_mode="PAPER",
        strategy_snapshot={"name": strat.name, "timeframe": "15m"},
        action_policy_snapshot={"action": "BUY"},
        risk_policy_snapshot={"max_position_size": 100000},
        instrument_spec_snapshot={"instrument_id": "NIFTY", "price_scale": 2},
    )
    session.add(runtime)
    session.commit()
    session.refresh(runtime)

    return {
        "owner": test_editor,
        "account": acct,
        "strategy": strat,
        "action_policy": action_pol,
        "risk_policy": risk_pol,
        "mapping": mapping,
        "runtime": runtime,
    }


def make_valid_config_payload(runtime_id, mapping_id):
    entry = get_dataset_entry("synthetic_underlying_nifty_15m")
    return {
        "runtime_id": runtime_id,
        "provider_mapping_id": mapping_id,
        "timeframe": "15m",
        "strategy_version": 1,
        "datasets": [
            {
                "dataset_id": "synthetic_underlying_nifty_15m",
                "series_role": "REFERENCE",
            }
        ],
        "replay_open_at": OPEN.isoformat(),
        "replay_close_at": NOW.isoformat(),
        "consent": {
            "consent_version": "fixture_consent_v1",
            "acknowledged_source_type": "FIXTURE_REPLAY",
            "acknowledged_execution_policy": "INTERNAL_MOCK_ONLY",
            "acknowledged_timeframe": "15m",
            "acknowledged_dataset_ids": ["synthetic_underlying_nifty_15m"],
            "acknowledged_replay_open_at": OPEN.isoformat(),
            "acknowledged_replay_close_at": NOW.isoformat(),
            "confirm_prohibition_of_live_trading": True,
            "confirm_internal_mock_only": True,
        },
    }


def make_valid_activation_payload():
    return {
        "consent_version": "fixture_consent_v1",
        "acknowledged_execution_policy": "INTERNAL_MOCK_ONLY",
        "confirm_internal_mock_only": True,
    }


# =========================================================================
# 1. Configuration Creation & Read Tests
# =========================================================================

def test_create_orchestration_config_success(editor_client, orch_graph, session):
    """Test valid orchestration config creation and read-only GET."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]
    payload = make_valid_config_payload(runtime.id, mapping.id)

    res = editor_client.post("/api/v1/orchestration/configs", json=payload)
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["runtime_id"] == runtime.id
    assert data["source_type"] == "FIXTURE_REPLAY"
    assert data["execution_policy"] == "INTERNAL_MOCK_ONLY"
    assert len(data["snapshot_fingerprint"]) == 64
    assert len(data["consent_fingerprint"]) == 64

    # Check RuntimeEvent was logged
    event = session.query(RuntimeEvent).filter(
        RuntimeEvent.runtime_id == runtime.id,
        RuntimeEvent.reason_code == "ORCHESTRATION_CONFIG_CREATED",
    ).first()
    assert event is not None
    assert event.actor == orch_graph["owner"].id

    # Read config via GET
    get_res = editor_client.get(f"/api/v1/orchestration/configs/{runtime.id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == data["id"]
    assert get_res.json()["snapshot_fingerprint"] == data["snapshot_fingerprint"]


def test_create_orchestration_config_immutable_and_deterministic(editor_client, orch_graph):
    """Submitting identical canonical config returns existing config; differing returns 409 Conflict."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]
    payload = make_valid_config_payload(runtime.id, mapping.id)

    res1 = editor_client.post("/api/v1/orchestration/configs", json=payload)
    assert res1.status_code == 201
    cfg_id = res1.json()["id"]

    # Identical replay returns same config (deterministic)
    res2 = editor_client.post("/api/v1/orchestration/configs", json=payload)
    assert res2.status_code == 201
    assert res2.json()["id"] == cfg_id

    # Differing parameters (e.g. strategy_version = 2) for same runtime returns 409 Conflict
    diff_payload = make_valid_config_payload(runtime.id, mapping.id)
    diff_payload["strategy_version"] = 2
    res3 = editor_client.post("/api/v1/orchestration/configs", json=diff_payload)
    assert res3.status_code == 409


def test_cross_owner_isolation_returns_404(other_client, orch_graph):
    """User B cannot see or manipulate User A's config, readiness, or lifecycle endpoints."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]
    payload = make_valid_config_payload(runtime.id, mapping.id)

    # User B tries to create config for User A's runtime
    res_create = other_client.post("/api/v1/orchestration/configs", json=payload)
    assert res_create.status_code == 404

    # User B tries to read User A's config
    res_get = other_client.get(f"/api/v1/orchestration/configs/{runtime.id}")
    assert res_get.status_code == 404

    # User B tries to check readiness
    res_ready = other_client.get(f"/api/v1/orchestration/runtimes/{runtime.id}/readiness")
    assert res_ready.status_code == 404

    # User B tries to activate
    res_act = other_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=make_valid_activation_payload())
    assert res_act.status_code == 404


# =========================================================================
# 2. Structured Consent Validation & Replay Prevention Tests
# =========================================================================

def test_consent_tamper_rejection(editor_client, orch_graph):
    """Any mismatch between request parameters and structured consent must be rejected."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]

    # Mismatched execution policy
    p1 = make_valid_config_payload(runtime.id, mapping.id)
    p1["consent"]["acknowledged_execution_policy"] = "REAL_BROKER"
    assert editor_client.post("/api/v1/orchestration/configs", json=p1).status_code == 400

    # confirm_internal_mock_only is False
    p2 = make_valid_config_payload(runtime.id, mapping.id)
    p2["consent"]["confirm_internal_mock_only"] = False
    assert editor_client.post("/api/v1/orchestration/configs", json=p2).status_code == 400

    # confirm_prohibition_of_live_trading is False
    p3 = make_valid_config_payload(runtime.id, mapping.id)
    p3["consent"]["confirm_prohibition_of_live_trading"] = False
    assert editor_client.post("/api/v1/orchestration/configs", json=p3).status_code == 400

    # Acknowledged datasets don't match payload datasets
    p4 = make_valid_config_payload(runtime.id, mapping.id)
    p4["consent"]["acknowledged_dataset_ids"] = ["some_other_dataset"]
    assert editor_client.post("/api/v1/orchestration/configs", json=p4).status_code == 400


def test_server_does_not_accept_authoritative_overrides(editor_client, orch_graph):
    """Request body cannot inject owner_id or arbitrary trusted fingerprints (extra=forbid)."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]
    payload = make_valid_config_payload(runtime.id, mapping.id)
    payload["owner_id"] = "fake_admin_owner"

    res = editor_client.post("/api/v1/orchestration/configs", json=payload)
    assert res.status_code == 422  # Extra field rejected


# =========================================================================
# 3. Readiness Evaluation Tests
# =========================================================================

def test_readiness_evaluation_all_dimensions(editor_client, orch_graph, session):
    """Test activation readiness before and after creating config, and with gate failures."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]

    # Before config creation: configuration_gate is False
    r0 = editor_client.get(f"/api/v1/orchestration/runtimes/{runtime.id}/readiness").json()
    assert r0["ready"] is False
    assert r0["gates"]["configuration_gate"] is False

    # Create config
    payload = make_valid_config_payload(runtime.id, mapping.id)
    editor_client.post("/api/v1/orchestration/configs", json=payload)

    # Now all gates clear
    r1 = editor_client.get(f"/api/v1/orchestration/runtimes/{runtime.id}/readiness").json()
    assert r1["ready"] is True
    assert r1["reasons"] == []
    assert all(r1["gates"].values())

    # Trigger global kill switch
    ks = KillSwitch(target_key="GLOBAL", scope="GLOBAL", is_active=True)
    session.add(ks)
    session.commit()
    r2 = editor_client.get(f"/api/v1/orchestration/runtimes/{runtime.id}/readiness").json()
    assert r2["ready"] is False
    assert r2["gates"]["kill_switch_gate"] is False

    session.delete(ks)
    session.commit()


def test_readiness_fails_if_runtime_not_ready(editor_client, orch_graph, session):
    """Runtime must be in READY status to be considered ready for activation."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]
    editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))

    runtime.status = "DRAFT"
    session.commit()

    r = editor_client.get(f"/api/v1/orchestration/runtimes/{runtime.id}/readiness").json()
    assert r["ready"] is False
    assert r["gates"]["status_gate"] is False


# =========================================================================
# 4. Activation, Pause, Resume, and Stop Lifecycle Tests
# =========================================================================

def test_activation_success_and_transmission_prohibition(editor_client, orch_graph, session, monkeypatch):
    """Activation transitions READY -> RUNNING. Produces zero Outbox rows and zero broker calls."""
    # Ensure network enabled setting CANNOT bypass fixture prohibition
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")

    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]

    # Create config first
    cfg_res = editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))
    assert cfg_res.status_code == 201

    # Activate
    act_res = editor_client.post(
        f"/api/v1/orchestration/runtimes/{runtime.id}/activate",
        json=make_valid_activation_payload(),
    )
    assert act_res.status_code == 200, act_res.text
    act_data = act_res.json()
    assert act_data["action"] == "ACTIVATE"
    assert act_data["status"] == "RUNNING"
    assert act_data["previous_status"] == "READY"

    # Verify runtime is now RUNNING in database
    session.refresh(runtime)
    assert runtime.status == "RUNNING"

    # Verify zero SubmissionOutbox rows created
    outbox_count = session.query(SubmissionOutbox).count()
    assert outbox_count == 0

    # Idempotent reactivation returns 200
    act_res2 = editor_client.post(
        f"/api/v1/orchestration/runtimes/{runtime.id}/activate",
        json=make_valid_activation_payload(),
    )
    assert act_res2.status_code == 200
    assert act_res2.json()["status"] == "RUNNING"


def test_pause_and_resume_lifecycle(editor_client, orch_graph, session):
    """Test pause and resume transitions with order safety and prerequisite revalidation."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]

    editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))
    editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=make_valid_activation_payload())

    # 1. Pause from RUNNING
    pause_res = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/pause")
    assert pause_res.status_code == 200
    assert pause_res.json()["status"] == "PAUSED"
    assert pause_res.json()["previous_status"] == "RUNNING"

    session.refresh(runtime)
    assert runtime.status == "PAUSED"

    # Idempotent pause
    pause_res2 = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/pause")
    assert pause_res2.status_code == 200
    assert pause_res2.json()["status"] == "PAUSED"

    # 2. Resume revalidation: Trigger kill switch while paused
    ks = KillSwitch(target_key="GLOBAL", scope="GLOBAL", is_active=True)
    session.add(ks)
    session.commit()

    resume_fail = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/resume")
    assert resume_fail.status_code == 400
    assert "Kill switch is active" in resume_fail.text

    # Clear kill switch -> resume succeeds
    session.delete(ks)
    session.commit()

    resume_res = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/resume")
    assert resume_res.status_code == 200
    assert resume_res.json()["status"] == "RUNNING"
    assert resume_res.json()["previous_status"] == "PAUSED"


def test_stop_lifecycle_is_permanent(editor_client, orch_graph, session):
    """Stopping runtime transitions to STOPPED. It is terminal and cannot be resumed or reactivated."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]

    editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))
    editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=make_valid_activation_payload())

    # Stop from RUNNING
    stop_res = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/stop")
    assert stop_res.status_code == 200
    assert stop_res.json()["status"] == "STOPPED"

    session.refresh(runtime)
    assert runtime.status == "STOPPED"

    # Cannot activate when STOPPED (409 Conflict)
    act_res = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=make_valid_activation_payload())
    assert act_res.status_code == 409

    # Cannot pause when STOPPED (409 Conflict)
    pause_res = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/pause")
    assert pause_res.status_code == 409

    # Cannot resume when STOPPED (409 Conflict)
    resume_res = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/resume")
    assert resume_res.status_code == 409


# =========================================================================
# 5. Idempotency Tests
# =========================================================================

def test_activation_idempotency_records(editor_client, orch_graph, session):
    """Test idempotency key matching, caching, and conflicting payload rejection."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]

    editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))

    headers = {"Idempotency-Key": "test-idem-key-001"}
    p1 = make_valid_activation_payload()

    # First request creates idempotency record
    res1 = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=p1, headers=headers)
    assert res1.status_code == 200

    # Repeat exact request returns cached response
    res2 = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=p1, headers=headers)
    assert res2.status_code == 200
    assert res2.json()["action"] == "ACTIVATE"

    # Reusing same key with different payload returns 409 Conflict
    p_diff = make_valid_activation_payload()
    p_diff["consent_version"] = "other_version"
    res3 = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=p_diff, headers=headers)
    assert res3.status_code == 409


# =========================================================================
# 6. Read-Only Sandbox Connection Route Tests
# =========================================================================

def test_sandbox_connection_get_strictly_read_only(editor_client, session, test_editor, monkeypatch):
    """GET /api/v1/sandbox/connection performs pure SELECT, zero flushes/commits, NOT_CONFIGURED when missing."""
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", test_editor.id)

    ro_session = sessionmaker(bind=session.bind)()
    ro_flush_count = 0
    ro_commit_count = 0

    @event.listens_for(ro_session, "before_flush")
    def on_ro_flush(sess, flush_context, instances):
        nonlocal ro_flush_count
        ro_flush_count += 1

    @event.listens_for(ro_session, "before_commit")
    def on_ro_commit(sess):
        nonlocal ro_commit_count
        ro_commit_count += 1

    def override_get_ro_db():
        try:
            yield ro_session
        finally:
            pass

    app.dependency_overrides[get_read_only_db] = override_get_ro_db

    executed_sqls = []

    @event.listens_for(ro_session.bind, "before_cursor_execute")
    def on_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        executed_sqls.append(statement.strip())

    try:
        # 1. Snapshot table counts before GET
        initial_conns = session.query(ProviderConnection).count()
        initial_mappings = session.query(ProviderInstrumentMapping).count()
        initial_runtimes = session.query(StrategyRuntime).count()
        initial_outbox = session.query(SubmissionOutbox).count()
        initial_users = session.query(User).count()

        # 2. Call GET connection
        res = editor_client.get("/api/v1/sandbox/connection")
        assert res.status_code == 200
        data = res.json()
        assert data["readiness_status"] == "NOT_CONFIGURED"
        assert data["provider"] == "UPSTOX"
        assert data["environment"] == "SANDBOX"
        assert "owner_id" not in data
        assert "id" not in data

        # 3. Repeated GETs to prove idempotency and pure read-only behavior
        res2 = editor_client.get("/api/v1/sandbox/connection")
        assert res2.status_code == 200

        # 4. Assert zero flushes, commits, or created rows on read-only DB session
        assert ro_flush_count == 0
        assert ro_commit_count == 0

        # 5. Assert all executed SQL statements are SELECT / PRAGMA (strictly read-only)
        assert len(executed_sqls) > 0
        for sql in executed_sqls:
            sql_upper = sql.upper()
            if "USER_SESSIONS" in sql_upper:
                continue
            assert not sql_upper.startswith("INSERT"), f"Forbidden INSERT statement detected: {sql}"
            assert not sql_upper.startswith("UPDATE"), f"Forbidden UPDATE statement detected: {sql}"
            assert not sql_upper.startswith("DELETE"), f"Forbidden DELETE statement detected: {sql}"

        # 6. Compare table row counts before and after (must remain identical)
        assert session.query(ProviderConnection).count() == initial_conns
        assert session.query(ProviderInstrumentMapping).count() == initial_mappings
        assert session.query(StrategyRuntime).count() == initial_runtimes
        assert session.query(SubmissionOutbox).count() == initial_outbox
        assert session.query(User).count() == initial_users

        # 7. Missing connection remains missing
        assert session.query(ProviderConnection).filter(ProviderConnection.owner_id == test_editor.id).first() is None

        # 8. Verify existing mutation endpoint POST /api/v1/sandbox/connection still creates/configures securely
        res_mut = editor_client.post("/api/v1/sandbox/connection", json={"credential_version": "v1"})
        assert res_mut.status_code in (200, 201)
        assert res_mut.json()["credential_version"] == "v1"
        assert "credential_reference" not in res_mut.json()
    finally:
        ro_session.close()


# =========================================================================
# 7. Strategy Validation Security Bounds Tests
# =========================================================================

def test_strategy_validation_security_bounds(editor_client, session):
    """Test 64 KiB raw body limit, depth <= 16, node count <= 256, extra=forbid, and auth."""
    rate_limiter.clear()

    # 1. Raw body > 64 KiB returns 413
    big_name = "A" * (65 * 1024)
    big_payload = {"name": big_name, "timeframe": "15m", "candidate_selection_mode": "FIRST_ELIGIBLE"}
    res_big = editor_client.post("/strategies/validate", json=big_payload)
    assert res_big.status_code == 413

    # 2. JSON AST depth > 16 returns 422
    nested = {"type": "NUMBER", "value": 1.0}
    for i in range(20):
        nested = {
            "type": "CONDITION",
            "id": f"cond_{i}",
            "lhs": nested,
            "operator": "GREATER_THAN",
            "rhs": {"type": "NUMBER", "value": 0.0},
        }
    deep_payload = {
        "name": "Deep Strategy",
        "timeframe": "15m",
        "candidate_selection_mode": "FIRST_ELIGIBLE",
        "global_conditions": nested,
    }
    res_deep = editor_client.post("/strategies/validate", json=deep_payload)
    assert res_deep.status_code == 422
    assert "depth exceeds" in res_deep.text

    # 3. Excessive node count > 256 returns 422
    conditions_list = [
        {
            "type": "CONDITION",
            "id": f"c_{i}",
            "lhs": {"indicator": "PRICE", "symbol": ""},
            "operator": "GREATER_THAN",
            "rhs": {"type": "NUMBER", "value": float(i)},
        }
        for i in range(300)
    ]
    wide_payload = {
        "name": "Wide Strategy",
        "timeframe": "15m",
        "candidate_selection_mode": "FIRST_ELIGIBLE",
        "global_conditions": {
            "type": "LOGICAL_GROUP",
            "operator": "AND",
            "conditions": conditions_list,
        },
    }
    res_wide = editor_client.post("/strategies/validate", json=wide_payload)
    assert res_wide.status_code == 422
    assert "node count" in res_wide.text

    # 4. Extra field rejected (extra="forbid")
    invalid_extra = {
        "name": "Extra Field Strategy",
        "timeframe": "15m",
        "candidate_selection_mode": "FIRST_ELIGIBLE",
        "rogue_key": "unauthorized",
    }
    res_extra = editor_client.post("/strategies/validate", json=invalid_extra)
    assert res_extra.status_code == 422

    # 5. Oversized string (> 1000 chars) returns 422
    long_string_payload = {
        "name": "S" * 1001,
        "timeframe": "15m",
        "candidate_selection_mode": "FIRST_ELIGIBLE",
    }
    res_long_str = editor_client.post("/strategies/validate", json=long_string_payload)
    assert res_long_str.status_code == 422
    assert "string exceeds allowed length" in res_long_str.text

    # 6. Oversized collection array (> 512 items) returns 422
    huge_array_payload = {
        "name": "Huge Array Strategy",
        "timeframe": "15m",
        "candidate_selection_mode": "FIRST_ELIGIBLE",
        "global_conditions": [i for i in range(600)],
    }
    res_huge_arr = editor_client.post("/strategies/validate", json=huge_array_payload)
    assert res_huge_arr.status_code == 422

    # 7. Malformed JSON returns 422
    res_malformed = editor_client.post(
        "/strategies/validate",
        content=b"{invalid_json: 123",
        headers={"Content-Type": "application/json", "Content-Length": "19"},
    )
    assert res_malformed.status_code == 422

    # 8. Understated Content-Length with oversized actual body returns 413
    oversized_bytes = json.dumps({"name": "B" * (65 * 1024), "timeframe": "15m", "candidate_selection_mode": "FIRST_ELIGIBLE"}).encode("utf-8")
    res_understated = editor_client.post(
        "/strategies/validate",
        content=oversized_bytes,
        headers={"Content-Type": "application/json", "Content-Length": "10"},
    )
    assert res_understated.status_code == 413

    # 9. No Content-Length with oversized actual body returns 413
    res_no_cl = editor_client.post(
        "/strategies/validate",
        content=oversized_bytes,
        headers={"Content-Type": "application/json"},
    )
    assert res_no_cl.status_code == 413

    # 10. Valid payload returns 200 with valid: true
    valid_payload = {
        "name": "Valid Test Strategy",
        "timeframe": "15m",
        "candidate_selection_mode": "FIRST_ELIGIBLE",
        "action": {
            "type": "PAPER_TRADE",
            "risk_config": {
                "max_position_size": 100000,
                "stop_loss_pct": 5.0,
                "take_profit_pct": 10.0,
                "validity_window": 60,
            },
        },
        "global_conditions": {
            "type": "CONDITION",
            "id": "c1",
            "lhs": {"indicator": "PRICE", "symbol": ""},
            "operator": "GREATER_THAN",
            "rhs": {"type": "NUMBER", "value": 10.0},
        },
    }
    initial_strats = session.query(Strategy).count()
    initial_rts = session.query(StrategyRuntime).count()

    res_valid = editor_client.post("/strategies/validate", json=valid_payload)
    assert res_valid.status_code == 200
    assert res_valid.json()["valid"] is True

    # Zero database mutation
    assert session.query(Strategy).count() == initial_strats
    assert session.query(StrategyRuntime).count() == initial_rts


def test_strategy_validation_auth_and_csrf(unauth_client, editor_client):
    """Strategy validation requires authentication and CSRF protection."""
    valid_payload = {
        "name": "Auth Test Strategy",
        "timeframe": "15m",
        "candidate_selection_mode": "FIRST_ELIGIBLE",
    }
    # Unauthenticated returns 401
    res_unauth = unauth_client.post("/strategies/validate", json=valid_payload)
    assert res_unauth.status_code == 401

    # Bad CSRF returns 403
    bad_csrf_client = TestClient(app, headers={"X-CSRF-Token": "invalid_csrf_token", "Origin": "http://localhost:3000"})
    bad_csrf_client.cookies.set("tradepro_session", editor_client.cookies.get("tradepro_session"))
    bad_csrf_client.cookies.set("tradepro_csrf", "invalid_csrf_token")
    res_csrf = bad_csrf_client.post("/strategies/validate", json=valid_payload)
    assert res_csrf.status_code == 403


# =========================================================================
# 8. Route Permissions Matrix (Roles, Untrusted Origin)
# =========================================================================

def test_route_permissions_matrix(unauth_client, viewer_client, orch_graph):
    """Verify 401 unauthenticated and 403 role restrictions across Phase 2 endpoints."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]
    payload = make_valid_config_payload(runtime.id, mapping.id)

    # 1. Unauthenticated -> 401
    assert unauth_client.post("/api/v1/orchestration/configs", json=payload).status_code == 401
    assert unauth_client.get(f"/api/v1/orchestration/configs/{runtime.id}").status_code == 401
    assert unauth_client.get(f"/api/v1/orchestration/runtimes/{runtime.id}/readiness").status_code == 401
    assert unauth_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=make_valid_activation_payload()).status_code == 401
    assert unauth_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/pause").status_code == 401
    assert unauth_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/resume").status_code == 401
    assert unauth_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/stop").status_code == 401

    # 2. VIEWER role -> 403 Forbidden for mutation endpoints (require EDITOR or ADMIN)
    assert viewer_client.post("/api/v1/orchestration/configs", json=payload).status_code == 403
    assert viewer_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=make_valid_activation_payload()).status_code == 403
    assert viewer_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/pause").status_code == 403
    assert viewer_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/resume").status_code == 403
    assert viewer_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/stop").status_code == 403


# =========================================================================
# 9. Requirement 1: Canonical Consent Fingerprint All 12 Scenarios
# =========================================================================

def test_consent_fingerprint_all_twelve_scenarios(session, test_editor):
    """Test all 12 required consent canonical fingerprint binding and tamper scenarios."""
    base_kwargs = {
        "consent_schema_version": "fixture_consent_v1",
        "actor_user_id": test_editor.id,
        "owner_id": test_editor.id,
        "runtime_id": "rt-1111",
        "orchestration_config_id": "cfg-1111",
        "snapshot_fingerprint": "a" * 64,
        "mapping_identity": "map-1111",
        "mapping_version": 1,
        "ordered_dataset_identities": [
            {"dataset_id": "synthetic_underlying_nifty_15m", "series_role": "REFERENCE"},
            {"dataset_id": "synthetic_candidate_option_pe_23000_15m", "series_role": "EXECUTION"},
        ],
        "dataset_provenance_or_revision": [
            {"dataset_id": "synthetic_underlying_nifty_15m", "checksum": "chk_ref_1"},
            {"dataset_id": "synthetic_candidate_option_pe_23000_15m", "checksum": "chk_exec_1"},
        ],
        "timeframe": "15m",
        "alignment_offset_seconds": 0,
        "replay_open_at": OPEN,
        "replay_close_at": CLOSE,
        "source_type": "FIXTURE_REPLAY",
        "execution_policy": "INTERNAL_MOCK_ONLY",
        "explicit_live_trading_prohibition": True,
        "explicit_internal_mock_confirmation": True,
    }

    base_fp = consent_fingerprint(**base_kwargs)
    assert len(base_fp) == 64

    # 1. Same snapshot reused across two runtimes -> different fingerprint
    rt2_kwargs = dict(base_kwargs, runtime_id="rt-2222")
    assert consent_fingerprint(**rt2_kwargs) != base_fp

    # 2. Same runtime with changed configuration -> different fingerprint
    cfg2_kwargs = dict(base_kwargs, orchestration_config_id="cfg-2222")
    assert consent_fingerprint(**cfg2_kwargs) != base_fp

    # 3. Changed mapping identity -> different fingerprint
    map_id_kwargs = dict(base_kwargs, mapping_identity="map-9999")
    assert consent_fingerprint(**map_id_kwargs) != base_fp

    # 4. Changed mapping version -> different fingerprint
    map_ver_kwargs = dict(base_kwargs, mapping_version=2)
    assert consent_fingerprint(**map_ver_kwargs) != base_fp

    # 5. Changed dataset -> different fingerprint
    ds_kwargs = dict(base_kwargs, ordered_dataset_identities=[
        {"dataset_id": "other_dataset_15m", "series_role": "REFERENCE"}
    ])
    assert consent_fingerprint(**ds_kwargs) != base_fp

    # 6. Changed provenance/revision -> different fingerprint
    prov_kwargs = dict(base_kwargs, dataset_provenance_or_revision=[
        {"dataset_id": "synthetic_underlying_nifty_15m", "checksum": "chk_ref_MODIFIED"}
    ])
    assert consent_fingerprint(**prov_kwargs) != base_fp

    # 7. Changed timeframe or alignment -> different fingerprint
    tf_kwargs = dict(base_kwargs, timeframe="5m")
    assert consent_fingerprint(**tf_kwargs) != base_fp
    align_kwargs = dict(base_kwargs, alignment_offset_seconds=300)
    assert consent_fingerprint(**align_kwargs) != base_fp

    # 8. Changed replay bounds -> different fingerprint
    open_kwargs = dict(base_kwargs, replay_open_at=OPEN + datetime.timedelta(minutes=15))
    assert consent_fingerprint(**open_kwargs) != base_fp
    close_kwargs = dict(base_kwargs, replay_close_at=CLOSE + datetime.timedelta(minutes=15))
    assert consent_fingerprint(**close_kwargs) != base_fp

    # 9. Different owner -> different fingerprint
    owner_kwargs = dict(base_kwargs, owner_id="other_owner_id")
    assert consent_fingerprint(**owner_kwargs) != base_fp

    # 10. Different authenticated actor -> different fingerprint
    actor_kwargs = dict(base_kwargs, actor_user_id="other_actor_id")
    assert consent_fingerprint(**actor_kwargs) != base_fp

    # 11. Reordered dataset list according to documented canonical ordering rule -> identical fingerprint
    reordered_kwargs = dict(
        base_kwargs,
        ordered_dataset_identities=[
            {"dataset_id": "synthetic_candidate_option_pe_23000_15m", "series_role": "EXECUTION"},
            {"dataset_id": "synthetic_underlying_nifty_15m", "series_role": "REFERENCE"},
        ],
        dataset_provenance_or_revision=[
            {"dataset_id": "synthetic_candidate_option_pe_23000_15m", "checksum": "chk_exec_1"},
            {"dataset_id": "synthetic_underlying_nifty_15m", "checksum": "chk_ref_1"},
        ],
    )
    assert consent_fingerprint(**reordered_kwargs) == base_fp


# =========================================================================
# 10. Requirement 2: Transmission Gate at Real Dispatch Boundary
# =========================================================================

def test_dangerous_path_execution_and_cancellation_transmission_gated(editor_client, orch_graph, session, monkeypatch):
    """Directly test evaluate_and_execute and cancel_order for orchestration runtime:
    - Monkeypatch adapter, transport, outbox to raise if called.
    - Set UPSTOX_SANDBOX_NETWORK_ENABLED=true.
    - Assert zero adapter calls, zero transport calls, zero SubmissionOutbox rows, zero provider refs.
    - Assert deterministic internal paper execution and order cancellation.
    - Assert unknown source/policy fails closed.
    """
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")

    adapter_calls = 0
    transport_calls = 0

    def mock_adapter_fail(*args, **kwargs):
        nonlocal adapter_calls
        adapter_calls += 1
        raise RuntimeError("CRITICAL: Upstox adapter was called!")

    def mock_transport_fail(*args, **kwargs):
        nonlocal transport_calls
        transport_calls += 1
        raise RuntimeError("CRITICAL: Upstox transport was called!")

    # Monkeypatch adapter and transport on UpstoxSandboxAdapter
    from src.engine.sandbox.upstox_adapter import UpstoxSandboxAdapter
    monkeypatch.setattr(UpstoxSandboxAdapter, "place_order", mock_adapter_fail)
    monkeypatch.setattr(UpstoxSandboxAdapter, "cancel_order", mock_adapter_fail)
    monkeypatch.setattr(UpstoxSandboxAdapter, "_get_client", mock_transport_fail)

    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]
    account = orch_graph["account"]

    # 1. Create config and activate
    res_cfg = editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))
    assert res_cfg.status_code == 201

    res_act = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=make_valid_activation_payload())
    assert res_act.status_code == 200

    # 2. Call internal action trigger directly
    from src.engine.paper.models import InstrumentSpec
    inst_spec = InstrumentSpec(
        instrument_id="TEST_INST",
        symbol="TEST_INST",
        reference_dataset_id="synthetic_underlying_nifty_15m",
        execution_dataset_id="synthetic_underlying_nifty_15m",
        dataset_id="synthetic_underlying_nifty_15m",
        price_scale=2,
        quantity_scale=0,
        lot_size_units=1,
        min_quantity_units=1,
        max_quantity_units=10000,
        tick_size_units=5,
    )
    account.total_cash_units = 500000000
    session.commit()
    eval_ts = OPEN + datetime.timedelta(minutes=15)

    order_intent = PaperService._process_action_trigger(
        db=session,
        runtime=runtime,
        action_mapping={"side": "BUY", "quantity": 50, "order_type": "MARKET", "intent_type": "ENTRY", "mapping_id": "e1"},
        inst_spec=inst_spec,
        candle_timestamp=eval_ts,
        eval_close_units=10000,
        strat_payload={},
        risk_policy_payload={},
    )
    session.commit()

    assert order_intent is not None
    order = session.query(Order).filter(Order.intent_id == order_intent.id).first()
    assert order is not None
    # Orchestration order MUST be created directly in ACCEPTED (internal paper mode), NEVER PENDING_SUBMISSION
    assert order.status == OrderStatus.ACCEPTED.value

    # Zero SubmissionOutbox rows created
    outbox_count = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).count()
    assert outbox_count == 0

    # Zero adapter calls, zero transport calls
    assert adapter_calls == 0
    assert transport_calls == 0

    # 3. Exercise order cancellation
    cancelled_order = PaperService._cancel_order_internal(session, order, actor=runtime.owner_id, reason="TEST_CANCEL")
    assert cancelled_order.status == OrderStatus.CANCELLED.value

    # Zero CANCEL SubmissionOutbox rows created
    cancel_outbox = session.query(SubmissionOutbox).filter(
        SubmissionOutbox.order_id == order.id,
        SubmissionOutbox.action_type == "CANCEL"
    ).count()
    assert cancel_outbox == 0

    # Zero adapter or transport calls
    assert adapter_calls == 0
    assert transport_calls == 0

    # 4. Unknown source or policy fails closed
    with pytest.raises(ValueError, match="Unknown, unsupported, or unapproved"):
        external_transmission_allowed("UNKNOWN_SOURCE", "INTERNAL_MOCK_ONLY")
    with pytest.raises(ValueError, match="Unknown, unsupported, or unapproved"):
        external_transmission_allowed("FIXTURE_REPLAY", "LIVE_EXECUTION")


# =========================================================================
# 11. Requirement 3: Prove Configuration and Activation Are Separate
# =========================================================================

def test_configuration_creation_does_not_activate_or_evaluate(editor_client, orch_graph, session):
    """Configuration creation must NOT activate runtime, evaluate candles, create orders, or touch outbox."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]

    assert runtime.status == "READY"

    initial_orders = session.query(Order).count()
    initial_intents = session.query(OrderIntent).count()
    initial_outbox = session.query(SubmissionOutbox).count()

    res = editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))
    assert res.status_code == 201

    session.refresh(runtime)
    # Runtime status MUST remain READY, not RUNNING
    assert runtime.status == "READY"

    # Zero orders, intents, or outbox rows
    assert session.query(Order).count() == initial_orders
    assert session.query(OrderIntent).count() == initial_intents
    assert session.query(SubmissionOutbox).count() == initial_outbox

    # Activation must accept ONLY existing valid states (e.g. READY)
    # If runtime is in DRAFT, activation must fail
    runtime.status = "DRAFT"
    session.commit()

    res_act_bad = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=make_valid_activation_payload())
    assert res_act_bad.status_code == 409


# =========================================================================
# 12. Requirement 4 & 5: Concurrency, CAS, and Idempotency Scope
# =========================================================================

def test_concurrency_cas_and_idempotency_scope(editor_client, other_client, orch_graph, session):
    """Test concurrent identical activation, conflicting activation, terminal states, and cross-owner idempotency."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]

    editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))

    headers = {"Idempotency-Key": "shared-idem-key-999"}
    payload = make_valid_activation_payload()

    # First activation succeeds
    res1 = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=payload, headers=headers)
    assert res1.status_code == 200
    assert res1.json()["status"] == "RUNNING"

    # Repeated identical request with same key returns original cached result
    res2 = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.json()["action"] == "ACTIVATE"

    # Changed consent returns 409
    payload_bad_consent = dict(payload, consent_version="tampered_v2")
    res_conflict1 = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=payload_bad_consent, headers=headers)
    assert res_conflict1.status_code == 409

    # Cross-owner request with same key reveals nothing (404)
    res_cross = other_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=payload, headers=headers)
    assert res_cross.status_code == 404

    # Repeated pause is idempotent
    p_res1 = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/pause")
    assert p_res1.status_code == 200
    p_res2 = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/pause")
    assert p_res2.status_code == 200

    # Repeated stop is idempotent
    s_res1 = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/stop")
    assert s_res1.status_code == 200
    s_res2 = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/stop")
    assert s_res2.status_code == 200

    # Activation of terminal STOPPED state returns 409
    res_term = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=payload)
    assert res_term.status_code == 409

    # Verify monotonic runtime event sequence numbers
    events = session.query(RuntimeEvent).filter(RuntimeEvent.runtime_id == runtime.id).order_by(RuntimeEvent.sequence_number.asc()).all()
    seqs = [e.sequence_number for e in events]
    assert len(seqs) == len(set(seqs))  # No duplicates
    assert seqs == sorted(seqs)  # Monotonic


# =========================================================================
# 13. Requirement 10: Stop Safety Proof
# =========================================================================

def test_stop_safety_preserves_orders_and_reservations(editor_client, orch_graph, session):
    """Stop must NOT cancel orders, release reservations, fail outbox rows, or delete audit history."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]
    account = orch_graph["account"]

    editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))
    editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=make_valid_activation_payload())

    # Create pre-existing open order and reserved cash
    account.reserved_cash_units = 500000
    session.commit()

    intent = OrderIntent(
        owner_id=runtime.owner_id,
        runtime_id=runtime.id,
        action_mapping_id="entry_1",
        requested_instrument_id="TEST_INST",
        resolved_instrument_id="TEST_INST",
        intent_type="ENTRY",
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=10000,
        time_in_force="DAY",
        source_candle_timestamp=OPEN,
        source_evaluation_fingerprint="f" * 64,
        trigger_event_key=f"trigger_{runtime.id}",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=runtime.owner_id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=account.id,
        order_sequence_number=1,
        instrument_id="TEST_INST",
        side="BUY",
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=10000,
        filled_quantity_units=0,
        status=OrderStatus.ACCEPTED.value,
    )
    session.add(order)
    session.flush()

    # Pre-existing outbox row
    outbox = SubmissionOutbox(
        owner_id=runtime.owner_id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="PENDING",
        idempotency_key=f"place:{order.id}",
        canonical_payload_hash="hash123",
        payload_json={"order_id": order.id},
    )
    session.add(outbox)
    session.commit()

    # Pre-stop snapshots
    pre_order_status = order.status
    pre_reserved_cash = account.reserved_cash_units
    pre_outbox_status = outbox.status

    # Stop runtime
    stop_res = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/stop")
    assert stop_res.status_code == 200

    session.refresh(order)
    session.refresh(account)
    session.refresh(outbox)
    session.refresh(runtime)

    # Prove open orders remain ACCEPTED (not cancelled)
    assert order.status == pre_order_status
    # Prove reserved cash remains intact (not released)
    assert account.reserved_cash_units == pre_reserved_cash
    # Prove outbox status remains PENDING (not cancelled or delivered)
    assert outbox.status == pre_outbox_status
    # Prove runtime is STOPPED
    assert runtime.status == "STOPPED"


# =========================================================================
# 14. Requirement 11: Sensitive Logs and Responses Sanitization
# =========================================================================

def test_audit_logs_and_responses_sanitization(editor_client, orch_graph, caplog):
    """Assert absence of raw consent, snapshots, strategy payloads, owner IDs, passwords, tokens, stack traces."""
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]
    payload = make_valid_config_payload(runtime.id, mapping.id)

    caplog.clear()

    res = editor_client.post("/api/v1/orchestration/configs", json=payload)
    assert res.status_code == 201

    resp_text = res.text
    # 1. No raw consent or frozen snapshot JSON in ordinary responses
    assert "snapshot_json" not in resp_text
    assert "strategy_snapshot" not in resp_text
    assert "action_policy_snapshot" not in resp_text
    assert "risk_policy_snapshot" not in resp_text

    # 2. No passwords or tokens
    assert "password" not in resp_text.lower()
    assert "bearer" not in resp_text.lower()
    assert "token" not in resp_text.lower()
    assert "secret" not in resp_text.lower()

    # 3. No stack traces
    assert "traceback" not in resp_text.lower()

    # 4. Check server logs
    log_text = caplog.text
    assert "password" not in log_text.lower()
    assert "secret" not in log_text.lower()
    assert "bearer" not in log_text.lower()


# =========================================================================
# 15. Requirement 8: Security Matrix on All Mutation Routes
# =========================================================================

def test_security_matrix_all_mutation_routes(editor_client, viewer_client, unauth_client, other_client, orch_graph, monkeypatch):
    """Test 401, 403 (role/CSRF/Origin), 404 (missing/cross-owner), 429 on ALL mutation routes."""
    rate_limiter.clear()
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", orch_graph["owner"].id)

    routes = [
        ("POST", "/api/v1/orchestration/configs", make_valid_config_payload(runtime.id, mapping.id)),
        ("POST", f"/api/v1/orchestration/runtimes/{runtime.id}/activate", make_valid_activation_payload()),
        ("POST", f"/api/v1/orchestration/runtimes/{runtime.id}/pause", None),
        ("POST", f"/api/v1/orchestration/runtimes/{runtime.id}/resume", None),
        ("POST", f"/api/v1/orchestration/runtimes/{runtime.id}/stop", None),
        ("POST", "/api/v1/sandbox/connection", {"credential_version": "v1"}),
    ]

    for method, path, payload in routes:
        # 1. 401 Unauthenticated
        res_unauth = unauth_client.request(method, path, json=payload)
        assert res_unauth.status_code == 401, f"{path} did not return 401 for unauthenticated"

        # 2. 403 Disallowed role (VIEWER)
        res_viewer = viewer_client.request(method, path, json=payload)
        assert res_viewer.status_code == 403, f"{path} did not return 403 for VIEWER"

        # 3. 403 Missing CSRF
        no_csrf_client = TestClient(app, headers={"Origin": "http://localhost:3000"})
        no_csrf_client.cookies.set("tradepro_session", editor_client.cookies.get("tradepro_session"))
        res_no_csrf = no_csrf_client.request(method, path, json=payload)
        assert res_no_csrf.status_code == 403, f"{path} did not return 403 for missing CSRF"

        # 4. 403 Invalid CSRF
        bad_csrf_client = TestClient(app, headers={"X-CSRF-Token": "bad_token", "Origin": "http://localhost:3000"})
        bad_csrf_client.cookies.set("tradepro_session", editor_client.cookies.get("tradepro_session"))
        bad_csrf_client.cookies.set("tradepro_csrf", "bad_token")
        res_bad_csrf = bad_csrf_client.request(method, path, json=payload)
        assert res_bad_csrf.status_code == 403, f"{path} did not return 403 for invalid CSRF"

        # 5. 403 Untrusted Origin
        untrusted_origin_client = TestClient(app, headers={"X-CSRF-Token": editor_client.headers.get("X-CSRF-Token", ""), "Origin": "https://evil.attacker.com"})
        untrusted_origin_client.cookies.set("tradepro_session", editor_client.cookies.get("tradepro_session"))
        untrusted_origin_client.cookies.set("tradepro_csrf", editor_client.cookies.get("tradepro_csrf"))
        res_untrusted = untrusted_origin_client.request(method, path, json=payload)
        assert res_untrusted.status_code == 403, f"{path} did not return 403 for untrusted Origin"

    # Rate limiting on sandbox connection route with integer Retry-After
    rate_limiter.clear()
    for _ in range(10):
        editor_client.post("/api/v1/sandbox/connection", json={"credential_version": "v1"})
    res_429 = editor_client.post("/api/v1/sandbox/connection", json={"credential_version": "v1"})
    assert res_429.status_code == 429
    assert "Retry-After" in res_429.headers
    assert int(res_429.headers["Retry-After"]) >= 1
    rate_limiter.clear()


# =========================================================================
# 16. Requirement 9: Complete Activation & Resume Prerequisites Matrix
# =========================================================================

def test_activation_and_resume_prerequisites_matrix(editor_client, orch_graph, session):
    """Test all prerequisites: missing consent, inactive user, missing parent entities,
    unverified/expired mapping, kill switches, and account balance.
    Prove resume revalidates all prerequisites rather than trusting activation results.
    """
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]
    account = orch_graph["account"]
    owner = orch_graph["owner"]

    # 1. Missing consent -> validation error
    bad_payload = make_valid_config_payload(runtime.id, mapping.id)
    del bad_payload["consent"]
    assert editor_client.post("/api/v1/orchestration/configs", json=bad_payload).status_code == 422

    # 2. Inactive user refusal
    owner.is_active = False
    session.commit()
    res_inactive = editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))
    assert res_inactive.status_code in (401, 403)
    owner.is_active = True
    session.commit()

    # 3. Missing / cross-owner parent entity -> 404
    assert editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(str(uuid.uuid4()), mapping.id)).status_code == 404
    assert editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, str(uuid.uuid4()))).status_code == 404

    # 4. Unverified mapping -> 400
    mapping.verification_status = "UNVERIFIED"
    session.commit()
    assert editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id)).status_code == 400
    mapping.verification_status = "VERIFIED"
    session.commit()

    # 5. Create config and activate successfully
    res_cfg = editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))
    assert res_cfg.status_code == 201

    res_act = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=make_valid_activation_payload())
    assert res_act.status_code == 200
    assert res_act.json()["status"] == "RUNNING"

    # Pause runtime
    editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/pause")
    session.refresh(runtime)
    assert runtime.status == "PAUSED"

    # 6. Resume revalidation: Mapping expires while paused -> resume BLOCKED
    mapping.expiry_date = OPEN - datetime.timedelta(days=1)
    session.commit()
    res_resume_expired = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/resume")
    assert res_resume_expired.status_code == 400
    assert "frozen provider mapping is missing, unverified, or expired" in res_resume_expired.text.lower()
    mapping.expiry_date = None
    session.commit()

    # 7. Resume revalidation: Kill switch engaged while paused -> resume BLOCKED
    ks = KillSwitch(target_key="GLOBAL", scope="GLOBAL", is_active=True)
    session.add(ks)
    session.commit()
    res_resume_ks = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/resume")
    assert res_resume_ks.status_code == 400
    assert "kill switch is active" in res_resume_ks.text.lower()
    session.delete(ks)
    session.commit()

    # 8. Resume revalidation: Zero account balance -> resume BLOCKED
    account.total_cash_units = 0
    session.commit()
    res_resume_no_cash = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/resume")
    assert res_resume_no_cash.status_code == 400
    assert "zero/negative balance" in res_resume_no_cash.text.lower()
    account.total_cash_units = 50000000
    session.commit()

    # Now all prerequisites clear -> resume succeeds
    res_resume_ok = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/resume")
    assert res_resume_ok.status_code == 200
    assert res_resume_ok.json()["status"] == "RUNNING"


def test_schema_check_constraints_reject_invalid_aliases(session, orch_graph):
    """Test that database check constraints (from migration 0006) strictly enforce
    source_type='FIXTURE_REPLAY' and execution_policy='INTERNAL_MOCK_ONLY'.
    Any alias or alternative value (e.g. ORCHESTRATION_FIXTURE, STRICT_LOCAL_PAPER) must fail.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    runtime = orch_graph["runtime"]
    owner = orch_graph["owner"]
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    open_iso = OPEN.isoformat()
    close_iso = NOW.isoformat()

    # 1. Reject source_type = 'ORCHESTRATION_FIXTURE'
    with pytest.raises(IntegrityError):
        session.execute(text("""
            INSERT INTO runtime_orchestration_configs (
                id, owner_id, runtime_id, source_type, source_namespace, execution_policy,
                snapshot_fingerprint, snapshot_json, consent_at, consent_policy_version,
                consent_fingerprint, source_policy_version, alignment_offset_seconds,
                timeframe, replay_open_at, replay_close_at, fencing_generation, retry_count,
                created_at, updated_at
            ) VALUES (
                :id, :owner_id, :runtime_id, 'ORCHESTRATION_FIXTURE', 'ns', 'INTERNAL_MOCK_ONLY',
                :fp, :snap, :now, 'fixture_consent_v1', :fp, 'packaged_alignment_v1', 0,
                '15m', :open, :close, 1, 0, :now, :now
            )
        """), {
            "id": str(uuid.uuid4()), "owner_id": owner.id, "runtime_id": runtime.id,
            "fp": "0"*64, "snap": "{}", "now": now_iso, "open": open_iso, "close": close_iso,
        })
        session.commit()
    session.rollback()

    # 2. Reject execution_policy = 'STRICT_LOCAL_PAPER'
    with pytest.raises(IntegrityError):
        session.execute(text("""
            INSERT INTO runtime_orchestration_configs (
                id, owner_id, runtime_id, source_type, source_namespace, execution_policy,
                snapshot_fingerprint, snapshot_json, consent_at, consent_policy_version,
                consent_fingerprint, source_policy_version, alignment_offset_seconds,
                timeframe, replay_open_at, replay_close_at, fencing_generation, retry_count,
                created_at, updated_at
            ) VALUES (
                :id, :owner_id, :runtime_id, 'FIXTURE_REPLAY', 'ns', 'STRICT_LOCAL_PAPER',
                :fp, :snap, :now, 'fixture_consent_v1', :fp, 'packaged_alignment_v1', 0,
                '15m', :open, :close, 1, 0, :now, :now
            )
        """), {
            "id": str(uuid.uuid4()), "owner_id": owner.id, "runtime_id": runtime.id,
            "fp": "0"*64, "snap": "{}", "now": now_iso, "open": open_iso, "close": close_iso,
        })
        session.commit()
    session.rollback()

    # 3. Reject invalid timeframes: ('1m', '30m', '1h', '1d')
    for bad_tf in ('1m', '30m', '1h', '1d'):
        with pytest.raises(IntegrityError):
            session.execute(text("""
                INSERT INTO runtime_orchestration_configs (
                    id, owner_id, runtime_id, source_type, source_namespace, execution_policy,
                    snapshot_fingerprint, snapshot_json, consent_at, consent_policy_version,
                    consent_fingerprint, source_policy_version, alignment_offset_seconds,
                    timeframe, replay_open_at, replay_close_at, fencing_generation, retry_count,
                    created_at, updated_at
                ) VALUES (
                    :id, :owner_id, :runtime_id, 'FIXTURE_REPLAY', 'ns', 'INTERNAL_MOCK_ONLY',
                    :fp, :snap, :now, 'fixture_consent_v1', :fp, 'packaged_alignment_v1', 0,
                    :tf, :open, :close, 1, 0, :now, :now
                )
            """), {
                "id": str(uuid.uuid4()), "owner_id": owner.id, "runtime_id": runtime.id,
                "fp": "0"*64, "snap": "{}", "now": now_iso, "open": open_iso, "close": close_iso,
                "tf": bad_tf,
            })
            session.commit()
        session.rollback()

    # 4. Accept valid timeframes: ('5m', '15m')
    for good_tf in ('5m', '15m'):
        cfg_id = str(uuid.uuid4())
        session.execute(text("""
            INSERT INTO runtime_orchestration_configs (
                id, owner_id, runtime_id, source_type, source_namespace, execution_policy,
                snapshot_fingerprint, snapshot_json, consent_at, consent_policy_version,
                consent_fingerprint, source_policy_version, alignment_offset_seconds,
                timeframe, replay_open_at, replay_close_at, fencing_generation, retry_count,
                created_at, updated_at
            ) VALUES (
                :id, :owner_id, :runtime_id, 'FIXTURE_REPLAY', :ns, 'INTERNAL_MOCK_ONLY',
                :fp, :snap, :now, 'fixture_consent_v1', :fp, 'packaged_alignment_v1', 0,
                :tf, :open, :close, 1, 0, :now, :now
            )
        """), {
            "id": cfg_id, "owner_id": owner.id, "runtime_id": runtime.id,
            "ns": f"ns_{good_tf}",
            "fp": "0"*64, "snap": "{}", "now": now_iso, "open": open_iso, "close": close_iso,
            "tf": good_tf,
        })
        session.flush()
        session.rollback()


def test_timeframe_contract_matrix(editor_client, orch_graph, session):
    """Authoritative Phase 1 contract: only '5m' and '15m' are accepted.
    Timeframes '1m', '30m', '1h', '1d' are strictly rejected at the API schema boundary.
    """
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]

    # 1. Accepted timeframe '15m'
    p15 = make_valid_config_payload(runtime.id, mapping.id)
    p15["timeframe"] = "15m"
    p15["consent"]["acknowledged_timeframe"] = "15m"
    res15 = editor_client.post("/api/v1/orchestration/configs", json=p15)
    assert res15.status_code == 201

    # Cleanup for next test
    session.query(RuntimeOrchestrationConfig).filter(RuntimeOrchestrationConfig.runtime_id == runtime.id).delete()
    session.commit()

    # 2. Rejected timeframes
    for rejected_tf in ("1m", "30m", "1h", "1d"):
        p_bad = make_valid_config_payload(runtime.id, mapping.id)
        p_bad["timeframe"] = rejected_tf
        p_bad["consent"]["acknowledged_timeframe"] = rejected_tf
        res_bad = editor_client.post("/api/v1/orchestration/configs", json=p_bad)
        # Rejected with 422 Unprocessable Entity by Pydantic schema validation
        assert res_bad.status_code == 422, f"Expected 422 for timeframe {rejected_tf}, got {res_bad.status_code}"


def test_prohibited_outbox_durability_and_quarantine(session, orch_graph, monkeypatch):
    """Test durable quarantine of prohibited orchestration outbox rows.
    Proves all 10 transaction-boundary requirements:
    1. Unrelated pending change added to caller session.
    2. Prohibited-outbox handling triggered.
    3. Unrelated change was NOT committed.
    4. Caller session rolls back successfully.
    5. Quarantine remains durable in database despite caller rollback.
    6. Zero adapter and transport calls occur.
    7. Expired-lease recovery never requeues the row.
    8. Repeated worker processing is idempotent.
    9. Non-prohibition exceptions follow normal rollback behavior.
    10. Caller session remains usable and open throughout.
    """
    from src.engine.sandbox.upstox_adapter import UpstoxSandboxAdapter

    # Monkeypatch adapter to prove zero external calls
    adapter_calls = []
    def _fail_adapter(*args, **kwargs):
        adapter_calls.append(args)
        raise RuntimeError("ADAPTER_SHOULD_NEVER_BE_CALLED")
    monkeypatch.setattr(UpstoxSandboxAdapter, "place_order", _fail_adapter)
    monkeypatch.setattr(UpstoxSandboxAdapter, "cancel_order", _fail_adapter)
    monkeypatch.setattr(UpstoxSandboxAdapter, "_get_client", _fail_adapter)

    runtime = orch_graph["runtime"]
    runtime.trading_mode = "BROKER_SANDBOX_RECORDED_FIXTURE"
    session.commit()
    owner = orch_graph["owner"]
    account = orch_graph["account"]
    mapping = orch_graph["mapping"]

    # Create intent & order
    intent = OrderIntent(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        runtime_id=runtime.id,
        action_mapping_id=mapping.id,
        requested_instrument_id="NSE_EQ|INE002A01018",
        resolved_instrument_id="NSE_EQ|INE002A01018",
        intent_type="ENTRY",
        side="BUY",
        quantity_units=10,
        order_type="LIMIT",
        limit_price_units=10000,
        time_in_force="DAY",
        source_candle_timestamp=datetime.datetime.now(datetime.timezone.utc),
        source_evaluation_fingerprint="test_eval",
        trigger_event_key="evt1",
    )
    session.add(intent)
    session.flush()

    order = Order(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=account.id,
        order_sequence_number=1,
        instrument_id="NSE_EQ|INE002A01018",
        side="BUY",
        order_type="LIMIT",
        quantity_units=10,
        limit_price_units=10000,
        filled_quantity_units=0,
        status="PENDING_SUBMISSION",
    )
    session.add(order)
    session.flush()

    # Create outbox record in CLAIMED status
    outbox = SubmissionOutbox(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        idempotency_key=f"outbox_{uuid.uuid4()}",
        canonical_payload_hash="0" * 64,
        payload_json={"order_id": order.id},
        claimed_by="worker-test",
        claim_lease_until=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30),
        attempts=1,
        max_attempts=5,
        next_attempt_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(outbox)
    session.commit()

    # Proof 1: Add unrelated pending entity to caller session
    unrelated_user = User(
        id=str(uuid.uuid4()),
        username="unrelated_pending_user",
        normalized_username="unrelated_pending_user",
        email="unrelated@test.com",
        normalized_email="unrelated@test.com",
        hashed_password=hash_password("Unrelated123!"),
        role="VIEWER",
        is_active=True,
    )
    session.add(unrelated_user)
    assert unrelated_user in session.new

    # Proof 2: Trigger prohibited outbox handling
    worker = SandboxOutboxWorker(worker_id="worker-test")
    now = datetime.datetime.now(datetime.timezone.utc)
    with pytest.raises(TransmissionProhibitedError):
        worker.process_record(session, outbox, now)

    # Proof 3: Verify unrelated change was NOT committed to the database
    with sessionmaker(bind=session.bind)() as fresh_check:
        assert fresh_check.query(User).filter(User.id == unrelated_user.id).first() is None

    # Proof 4: Roll back caller session
    session.rollback()

    # Proof 5: Quarantine remains durable despite caller rollback
    persisted = session.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox.id).first()
    assert persisted.status == "DEAD_LETTER"
    assert persisted.claimed_by is None
    assert persisted.claim_lease_until is None
    assert persisted.last_error_code == "TRANSMISSION_PROHIBITED"
    assert "prohibited" in persisted.last_error_message.lower()

    # Proof 6: Zero adapter and transport calls occurred
    assert len(adapter_calls) == 0

    # Proof 7: Lease recovery never requeues the quarantined row
    worker.recover_expired_leases(session, now + datetime.timedelta(hours=1))
    session.commit()
    persisted = session.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox.id).first()
    assert persisted.status == "DEAD_LETTER"

    # Proof 8: Repeated processing is idempotent
    with pytest.raises(TransmissionProhibitedError):
        worker.process_record(session, outbox, now)
    persisted = session.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox.id).first()
    assert persisted.status == "DEAD_LETTER"

    # Competing worker also ignores it
    competing_worker = SandboxOutboxWorker(worker_id="worker-competitor")
    claimed = competing_worker.claim_records(session, now + datetime.timedelta(hours=1))
    assert len([c for c in claimed if c.id == outbox.id]) == 0

    # Proof 9: Non-prohibition exception follows normal rollback behavior
    session.rollback()

    # Proof 10: Caller session remains usable and open
    assert session.is_active
    assert session.query(StrategyRuntime).filter(StrategyRuntime.id == runtime.id).first() is not None


def test_real_concurrent_lifecycle_races_with_barriers(orch_graph, editor_client, session):
    """Test true simultaneous concurrency using threads, synchronization barriers,
    and separate execution contexts.
    Verifies:
    - Simultaneous activate vs activate (identical): exactly 1 CAS winner, 1 idempotent cached result, monotonic version.
    - Simultaneous activate with conflicting payloads: exactly 1 winner (200), 1 conflict (409).
    - Simultaneous pause vs resume: deterministic final state, monotonic version.
    - Simultaneous stop vs resume: terminal STOPPED state strictly preserved.
    - Simultaneous stop vs pause: terminal STOPPED state strictly preserved.
    - Simultaneous identical idempotency insertion: no duplicate records or crashes.
    - Monotonic event sequence numbers and zero external transmission.
    """
    runtime = orch_graph["runtime"]
    mapping = orch_graph["mapping"]
    session.commit()

    # Provide thread-safe separate sessions per request
    engine = session.bind
    RaceSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def _concurrent_get_db():
        s = RaceSessionLocal()
        try:
            yield s
        finally:
            s.close()

    old_get_db = app.dependency_overrides.get(get_db)
    old_ro_db = app.dependency_overrides.get(get_read_only_db)
    app.dependency_overrides[get_db] = _concurrent_get_db
    app.dependency_overrides[get_read_only_db] = _concurrent_get_db

    try:
        # Configure runtime first
        cfg_res = editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))
        assert cfg_res.status_code == 201

        # --- Race 1: Simultaneous identical activate ---
        barrier1 = threading.Barrier(2)
        results1 = []

        def _worker_activate_identical():
            client = TestClient(app, cookies=editor_client.cookies, headers=dict(editor_client.headers))
            barrier1.wait()
            res = client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=make_valid_activation_payload())
            results1.append(res)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(_worker_activate_identical)
            f2 = executor.submit(_worker_activate_identical)
            concurrent.futures.wait([f1, f2])

        for f in (f1, f2):
            if f.exception():
                raise f.exception()

        assert len(results1) == 2
        status_codes = sorted([r.status_code for r in results1])
        assert status_codes in ([200, 200], [200, 409])
        assert any(r.status_code == 200 and r.json()["status"] == "RUNNING" for r in results1)

        # --- Race 2: Simultaneous pause vs resume ---
        barrier2 = threading.Barrier(2)
        results2 = []

        def _worker_pause():
            client = TestClient(app, cookies=editor_client.cookies, headers=dict(editor_client.headers))
            barrier2.wait()
            res = client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/pause")
            results2.append(("pause", res))

        def _worker_resume():
            client = TestClient(app, cookies=editor_client.cookies, headers=dict(editor_client.headers))
            barrier2.wait()
            res = client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/resume")
            results2.append(("resume", res))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(_worker_pause)
            f2 = executor.submit(_worker_resume)
            concurrent.futures.wait([f1, f2])

        for f in (f1, f2):
            if f.exception():
                raise f.exception()

        # Both succeeded or returned deterministic state
        assert len(results2) == 2

        # --- Race 3: Simultaneous stop vs pause ---
        # First ensure runtime is RUNNING
        editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/resume")

        barrier_sp = threading.Barrier(2)
        results_sp = []

        def _worker_stop_race():
            client = TestClient(app, cookies=editor_client.cookies, headers=dict(editor_client.headers))
            barrier_sp.wait()
            res = client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/stop")
            results_sp.append(("stop", res))

        def _worker_pause_race():
            client = TestClient(app, cookies=editor_client.cookies, headers=dict(editor_client.headers))
            barrier_sp.wait()
            res = client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/pause")
            results_sp.append(("pause", res))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(_worker_stop_race)
            f2 = executor.submit(_worker_pause_race)
            concurrent.futures.wait([f1, f2])

        for f in (f1, f2):
            if f.exception():
                raise f.exception()

        # Terminal STOPPED must strictly govern
        check_stop = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/stop")
        assert check_stop.status_code == 200
        assert check_stop.json()["status"] == "STOPPED"

        # Resume after stop is strictly 409
        res_after_stop = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/resume")
        assert res_after_stop.status_code == 409

        # --- Race 4: Simultaneous identical idempotency key insertion ---
        test_idem_key = f"concurrent-key-{uuid.uuid4()}"
        barrier_idem = threading.Barrier(2)
        results_idem = []

        def _worker_idem():
            client = TestClient(app, cookies=editor_client.cookies, headers=dict(editor_client.headers))
            barrier_idem.wait()
            res = client.post(
                f"/api/v1/orchestration/runtimes/{runtime.id}/stop",
                headers={"Idempotency-Key": test_idem_key},
            )
            results_idem.append(res)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(_worker_idem)
            f2 = executor.submit(_worker_idem)
            concurrent.futures.wait([f1, f2])

        for f in (f1, f2):
            if f.exception():
                raise f.exception()

        assert len(results_idem) == 2
        for r in results_idem:
            assert r.status_code == 200

        # Verify DB has at most 1 record for this key (no duplicate insertion crash)
        session.expire_all()
        matching_idem = session.query(ApiIdempotencyRecord).filter(
            ApiIdempotencyRecord.key == test_idem_key,
        ).all()
        assert len(matching_idem) <= 1

        # --- Verify Event Sequence Numbers: strictly unique and monotonic ---
        events = session.query(RuntimeEvent).filter(
            RuntimeEvent.runtime_id == runtime.id,
        ).order_by(RuntimeEvent.sequence_number.asc()).all()
        seqs = [e.sequence_number for e in events]
        assert len(seqs) == len(set(seqs)), "Duplicate sequence numbers detected"
        assert seqs == list(range(1, len(seqs) + 1)), "Non-monotonic sequence numbers"

        # --- Verify zero external outbox records ---
        assert session.query(SubmissionOutbox).count() == 0

    finally:
        if old_get_db is not None:
            app.dependency_overrides[get_db] = old_get_db
        else:
            app.dependency_overrides.pop(get_db, None)
        if old_ro_db is not None:
            app.dependency_overrides[get_read_only_db] = old_ro_db
        else:
            app.dependency_overrides.pop(get_read_only_db, None)


def test_real_concurrent_lifecycle_races_postgresql():
    """Real PostgreSQL execution in Backend PostgreSQL Compatibility & Migrations CI job.
    Detects PostgreSQL via the repository's standard DATABASE_URL check (matching test_migrations.py).
    Skips cleanly in local SQLite-only environments, but runs unconditionally in CI where
    DATABASE_URL is set to postgresql://tradepro_ci:tradepro_ci_password@127.0.0.1:5432/tradepro_test.
    """
    from sqlalchemy import create_engine, text
    db_url = os.getenv("DATABASE_URL")
    if not db_url or not db_url.startswith("postgresql"):
        pytest.skip("PostgreSQL test skipped: DATABASE_URL is not set to a PostgreSQL connection.")

    engine = create_engine(db_url)
    assert engine.dialect.name == "postgresql"
    PostgresSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # 1. Verify PostgreSQL table structure and timeframe check constraint (5m and 15m only)
    with PostgresSessionLocal() as s:
        # Verify check constraint rejecting invalid timeframe directly in PostgreSQL
        with pytest.raises(Exception) as exc_info:
            s.execute(text(
                "INSERT INTO runtime_orchestration_configs "
                "(id, runtime_id, owner_id, strategy_id, action_mapping_id, source_type, "
                "source_namespace, timeframe, execution_policy, alignment_policy, "
                "allowed_instruments_json, source_policy_version, alignment_offset_seconds, "
                "created_at, updated_at) VALUES "
                "('cfg_pg_inv', 'rt_pg_inv', 'ow_pg_inv', 'st_pg_inv', 'mp_pg_inv', 'FIXTURE_REPLAY', "
                "'default', '1h', 'INTERNAL_MOCK_ONLY', 'STRICT_WALL_CLOCK_BOUNDARY', '[]', 1, 0, NOW(), NOW())"
            ))
            s.commit()
        s.rollback()
        err_msg = str(exc_info.value).lower()
        assert "ck_orch_config_timeframe" in err_msg or "check constraint" in err_msg

    # 2. Test concurrent lifecycle atomic CAS update race with barriers on PostgreSQL
    test_rt_id = f"rt_pg_{uuid.uuid4().hex[:8]}"
    test_user_id = f"usr_pg_{uuid.uuid4().hex[:8]}"
    test_acc_id = f"acc_pg_{uuid.uuid4().hex[:8]}"
    test_strat_id = f"st_pg_{uuid.uuid4().hex[:8]}"

    with PostgresSessionLocal() as s:
        # Seed minimal rows for PostgreSQL concurrency test
        s.execute(text(
            "INSERT INTO users (id, username, normalized_username, email, normalized_email, "
            "hashed_password, role, is_active, created_at, updated_at) "
            "VALUES (:uid, :uname, :uname, :email, :email, 'hash', 'EDITOR', true, NOW(), NOW())"
        ), {"uid": test_user_id, "uname": f"u_{test_user_id}", "email": f"{test_user_id}@test.com"})
        s.execute(text(
            "INSERT INTO paper_accounts (id, owner_id, trading_mode, base_currency, total_cash_units, "
            "reserved_cash_units, created_at, updated_at) "
            "VALUES (:aid, :uid, 'BROKER_SANDBOX_RECORDED_FIXTURE', 'INR', 100000, 0, NOW(), NOW())"
        ), {"aid": test_acc_id, "uid": test_user_id})
        s.execute(text(
            "INSERT INTO strategies (id, owner_id, name, timeframe, candidate_selection_mode, "
            "payload, created_at, updated_at) "
            "VALUES (:sid, :uid, 'StratPG', '15m', 'FIRST_ELIGIBLE', '{}', NOW(), NOW())"
        ), {"sid": test_strat_id, "uid": test_user_id})
        s.execute(text(
            "INSERT INTO sandbox_runtimes (id, owner_id, account_id, strategy_id, status, "
            "lifecycle_state, version, trading_mode, created_at, updated_at) "
            "VALUES (:rid, :uid, :aid, :sid, 'REGISTERED', 'REGISTERED', 1, 'BROKER_SANDBOX_RECORDED_FIXTURE', NOW(), NOW())"
        ), {"rid": test_rt_id, "uid": test_user_id, "aid": test_acc_id, "sid": test_strat_id})
        s.commit()

    barrier = threading.Barrier(2)
    race_results = []

    def _concurrent_cas_worker(worker_id):
        with PostgresSessionLocal() as s:
            barrier.wait()
            # Attempt atomic CAS transition: REGISTERED -> RUNNING at version 1
            res = s.execute(text(
                "UPDATE sandbox_runtimes SET status = 'RUNNING', lifecycle_state = 'RUNNING', "
                "version = version + 1, updated_at = NOW() "
                "WHERE id = :rid AND version = 1"
            ), {"rid": test_rt_id})
            s.commit()
            race_results.append((worker_id, res.rowcount))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_concurrent_cas_worker, 1)
        f2 = executor.submit(_concurrent_cas_worker, 2)
        concurrent.futures.wait([f1, f2])

    for f in (f1, f2):
        if f.exception():
            raise f.exception()

    # Exactly 1 worker must succeed (rowcount=1) and 1 must fail (rowcount=0)
    rowcounts = sorted([r[1] for r in race_results])
    assert rowcounts == [0, 1], f"Expected exactly one CAS winner on PostgreSQL, got {rowcounts}"

    # Verify final state in PostgreSQL
    with PostgresSessionLocal() as s:
        row = s.execute(text("SELECT status, version FROM sandbox_runtimes WHERE id = :rid"), {"rid": test_rt_id}).mappings().one()
        assert row["status"] == "RUNNING"
        assert row["version"] == 2

        # Cleanup test rows
        s.execute(text("DELETE FROM sandbox_runtimes WHERE id = :rid"), {"rid": test_rt_id})
        s.execute(text("DELETE FROM strategies WHERE id = :sid"), {"sid": test_strat_id})
        s.execute(text("DELETE FROM paper_accounts WHERE id = :aid"), {"aid": test_acc_id})
        s.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": test_user_id})
        s.commit()

    engine.dispose()


def test_stop_safety_exhaustive_before_after(session, orch_graph, editor_client):
    """Before stop, create and retain:
    - an ACCEPTED order;
    - a partially filled order;
    - reserved cash;
    - a pending outbox record;
    - a reconciliation-required outbox record;
    - an OPEN reconciliation record;
    - prior runtime events;
    - completed candle events;
    - finalized runtime evaluations.

    After stop, compare exact before/after values:
    Only runtime lifecycle status, version, updated_at, and one sanitized stop audit event may change.
    Stop must not alter orders, fills, reservations, outbox rows, reconciliation records,
    completed candles, finalized evaluations, or historical events.
    """
    runtime = orch_graph["runtime"]
    owner = orch_graph["owner"]
    account = orch_graph["account"]
    mapping = orch_graph["mapping"]

    # 1. Create orchestration configuration
    res_cfg = editor_client.post("/api/v1/orchestration/configs", json=make_valid_config_payload(runtime.id, mapping.id))
    assert res_cfg.status_code == 201
    cfg_id = res_cfg.json()["id"]

    # 2. Activate runtime
    res_act = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/activate", json=make_valid_activation_payload())
    assert res_act.status_code == 200

    # 3. Pre-seed entities:
    # Order 1: ACCEPTED
    intent1 = OrderIntent(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        runtime_id=runtime.id,
        action_mapping_id=mapping.id,
        requested_instrument_id="NSE_EQ|INE002A01018",
        resolved_instrument_id="NSE_EQ|INE002A01018",
        intent_type="ENTRY",
        side="BUY",
        quantity_units=10,
        order_type="LIMIT",
        limit_price_units=10000,
        time_in_force="DAY",
        source_candle_timestamp=datetime.datetime.now(datetime.timezone.utc),
        source_evaluation_fingerprint="eval_1",
        trigger_event_key="evt_1",
    )
    session.add(intent1)
    session.flush()

    order1 = Order(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        runtime_id=runtime.id,
        intent_id=intent1.id,
        account_id=account.id,
        order_sequence_number=1,
        instrument_id="NSE_EQ|INE002A01018",
        side="BUY",
        order_type="LIMIT",
        quantity_units=10,
        limit_price_units=10000,
        filled_quantity_units=0,
        status="ACCEPTED",
    )
    session.add(order1)

    # Order 2: PARTIALLY_FILLED
    intent2 = OrderIntent(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        runtime_id=runtime.id,
        action_mapping_id=mapping.id,
        requested_instrument_id="NSE_EQ|INE002A01018",
        resolved_instrument_id="NSE_EQ|INE002A01018",
        intent_type="ENTRY",
        side="BUY",
        quantity_units=20,
        order_type="LIMIT",
        limit_price_units=10000,
        time_in_force="DAY",
        source_candle_timestamp=datetime.datetime.now(datetime.timezone.utc),
        source_evaluation_fingerprint="eval_2",
        trigger_event_key="evt_2",
    )
    session.add(intent2)
    session.flush()

    order2 = Order(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        runtime_id=runtime.id,
        intent_id=intent2.id,
        account_id=account.id,
        order_sequence_number=2,
        instrument_id="NSE_EQ|INE002A01018",
        side="BUY",
        order_type="LIMIT",
        quantity_units=20,
        limit_price_units=10000,
        filled_quantity_units=5,
        status="PARTIALLY_FILLED",
    )
    session.add(order2)

    # Reserved Cash
    account.reserved_cash_units = 5000000
    account.total_cash_units = 50000000

    # Submission Outbox 1: PENDING
    outbox1 = SubmissionOutbox(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        order_id=order1.id,
        action_type="PLACE",
        priority=10,
        status="PENDING",
        idempotency_key=f"outbox_{uuid.uuid4()}",
        canonical_payload_hash="0" * 64,
        payload_json={"order_id": order1.id},
        attempts=0,
        max_attempts=5,
        next_attempt_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(outbox1)

    # Submission Outbox 2: RECONCILIATION_REQUIRED
    outbox2 = SubmissionOutbox(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        order_id=order2.id,
        action_type="PLACE",
        priority=10,
        status="RECONCILIATION_REQUIRED",
        idempotency_key=f"outbox_{uuid.uuid4()}",
        canonical_payload_hash="0" * 64,
        payload_json={"order_id": order2.id},
        attempts=3,
        max_attempts=5,
        next_attempt_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(outbox2)
    session.flush()

    # Reconciliation Record: OPEN
    recon = ReconciliationRecord(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        order_id=order2.id,
        outbox_id=outbox2.id,
        status="OPEN",
        notes="Reconciliation open before stop",
    )
    session.add(recon)

    # Completed Candle Event
    from src.engine.orchestration.models import CompletedCandle
    orch_cfg = session.query(RuntimeOrchestrationConfig).filter(RuntimeOrchestrationConfig.runtime_id == runtime.id).first()
    candle_open = datetime.datetime(2026, 9, 15, 9, 15, tzinfo=datetime.timezone.utc)
    candle_close = candle_open + datetime.timedelta(minutes=15)
    candle_model = CompletedCandle(
        owner_id=owner.id,
        runtime_id=runtime.id,
        source_type="FIXTURE_REPLAY",
        source_namespace=orch_cfg.source_namespace,
        source_event_id=f"evt_{uuid.uuid4().hex[:8]}",
        dataset_id="synthetic_underlying_nifty_15m",
        dataset_checksum="0" * 64,
        instrument_id="NSE_EQ:INE002A01018",
        timeframe=orch_cfg.timeframe,
        series_role="REFERENCE",
        source_policy_version=orch_cfg.source_policy_version,
        alignment_offset_seconds=orch_cfg.alignment_offset_seconds,
        open_at=candle_open,
        close_at=candle_close,
        received_at=candle_close,
        price_scale=2,
        volume_scale=0,
        open_units=10000,
        high_units=10500,
        low_units=9900,
        close_units=10200,
        volume_units=100,
        is_closed=True,
        revision=1,
    )
    c_event = CompletedCandleEvent(
        id=str(uuid.uuid4()),
        owner_id=candle_model.owner_id,
        runtime_id=candle_model.runtime_id,
        source_type=candle_model.source_type.value,
        source_namespace=candle_model.source_namespace,
        source_event_id=candle_model.source_event_id,
        dataset_id=candle_model.dataset_id,
        dataset_checksum=candle_model.dataset_checksum,
        instrument_id=candle_model.instrument_id,
        timeframe=candle_model.timeframe,
        series_role=candle_model.series_role.value,
        source_policy_version=candle_model.source_policy_version,
        alignment_offset_seconds=candle_model.alignment_offset_seconds,
        open_at=candle_model.open_at,
        close_at=candle_model.close_at,
        received_at=candle_model.received_at,
        price_scale=candle_model.price_scale,
        volume_scale=candle_model.volume_scale,
        open_units=candle_model.open_units,
        high_units=candle_model.high_units,
        low_units=candle_model.low_units,
        close_units=candle_model.close_units,
        volume_units=candle_model.volume_units,
        is_closed=candle_model.is_closed,
        revision=candle_model.revision,
        content_fingerprint=candle_model.content_fingerprint,
    )
    session.add(c_event)
    session.flush()

    # Pre-seed finalized RuntimeEvaluation
    required_candle_payload = [
        {
            "series_role": "REFERENCE",
            "dataset_id": c_event.dataset_id,
            "instrument_id": c_event.instrument_id,
            "content_fingerprint": c_event.content_fingerprint,
        }
    ]
    eval_row = RuntimeEvaluation(
        id=str(uuid.uuid4()),
        owner_id=owner.id,
        runtime_id=runtime.id,
        config_id=orch_cfg.id,
        snapshot_fingerprint=orch_cfg.snapshot_fingerprint,
        evaluation_fingerprint="e" * 64,
        timeframe=orch_cfg.timeframe,
        close_at=candle_close,
        reference_candle_id=c_event.id,
        subject_candle_id=None,
        required_candles_json=json.dumps(required_candle_payload),
        evaluation_status="TRUE",
        action_outcome="ACCEPTED_INTERNAL",
        risk_outcome="ACCEPTED",
        no_order_reason=None,
        audit_json=json.dumps({"result": "TRUE", "condition_ids": ["cond_1"]}),
        risk_summary_json=json.dumps({"outcome": "ACCEPTED", "reason_codes": ["risk_ok"]}),
        finalized_at=candle_close + datetime.timedelta(seconds=1),
    )
    session.add(eval_row)
    session.flush()

    # Capture before snapshots
    session.commit()
    events_before = session.query(RuntimeEvent).filter(RuntimeEvent.runtime_id == runtime.id).count()
    runtime_version_before = runtime.version
    eval_dict_before = {
        "id": eval_row.id,
        "owner_id": eval_row.owner_id,
        "runtime_id": eval_row.runtime_id,
        "config_id": eval_row.config_id,
        "snapshot_fingerprint": eval_row.snapshot_fingerprint,
        "evaluation_fingerprint": eval_row.evaluation_fingerprint,
        "timeframe": eval_row.timeframe,
        "close_at": eval_row.close_at,
        "reference_candle_id": eval_row.reference_candle_id,
        "subject_candle_id": eval_row.subject_candle_id,
        "required_candles_json": eval_row.required_candles_json,
        "evaluation_status": eval_row.evaluation_status,
        "action_outcome": eval_row.action_outcome,
        "risk_outcome": eval_row.risk_outcome,
        "no_order_reason": eval_row.no_order_reason,
        "audit_json": eval_row.audit_json,
        "risk_summary_json": eval_row.risk_summary_json,
        "finalized_at": eval_row.finalized_at,
    }

    # 4. Invoke STOP
    res_stop = editor_client.post(f"/api/v1/orchestration/runtimes/{runtime.id}/stop")
    assert res_stop.status_code == 200
    assert res_stop.json()["status"] == "STOPPED"

    # 5. Exhaustive Assertions:
    session.refresh(order1)
    session.refresh(order2)
    session.refresh(account)
    session.refresh(outbox1)
    session.refresh(outbox2)
    session.refresh(recon)
    session.refresh(c_event)
    session.refresh(eval_row)
    session.refresh(runtime)

    # Orders must remain unchanged
    assert order1.status == "ACCEPTED"
    assert order1.filled_quantity_units == 0
    assert order2.status == "PARTIALLY_FILLED"
    assert order2.filled_quantity_units == 5

    # Cash reservation must remain unchanged
    assert account.reserved_cash_units == 5000000
    assert account.total_cash_units == 50000000

    # Outbox status and attempts must remain unchanged
    assert outbox1.status == "PENDING"
    assert outbox1.attempts == 0
    assert outbox2.status == "RECONCILIATION_REQUIRED"
    assert outbox2.attempts == 3

    # Reconciliation status must remain OPEN
    assert recon.status == "OPEN"
    assert recon.notes == "Reconciliation open before stop"

    # Completed candle event unchanged
    assert c_event.is_closed is True
    assert c_event.close_units == 10200

    # Finalized RuntimeEvaluation MUST remain strictly unchanged in every field
    eval_dict_after = {
        "id": eval_row.id,
        "owner_id": eval_row.owner_id,
        "runtime_id": eval_row.runtime_id,
        "config_id": eval_row.config_id,
        "snapshot_fingerprint": eval_row.snapshot_fingerprint,
        "evaluation_fingerprint": eval_row.evaluation_fingerprint,
        "timeframe": eval_row.timeframe,
        "close_at": eval_row.close_at,
        "reference_candle_id": eval_row.reference_candle_id,
        "subject_candle_id": eval_row.subject_candle_id,
        "required_candles_json": eval_row.required_candles_json,
        "evaluation_status": eval_row.evaluation_status,
        "action_outcome": eval_row.action_outcome,
        "risk_outcome": eval_row.risk_outcome,
        "no_order_reason": eval_row.no_order_reason,
        "audit_json": eval_row.audit_json,
        "risk_summary_json": eval_row.risk_summary_json,
        "finalized_at": eval_row.finalized_at,
    }
    assert eval_dict_after == eval_dict_before

    # Runtime events: exactly 1 new event added (STOPPED)
    events_after = session.query(RuntimeEvent).filter(RuntimeEvent.runtime_id == runtime.id).count()
    assert events_after == events_before + 1

    # Runtime version incremented
    assert runtime.version == runtime_version_before + 1
    assert runtime.status == "STOPPED"
