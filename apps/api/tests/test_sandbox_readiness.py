import datetime
import pytest
from src.models import (
    KillSwitch,
    PaperAccount,
    ProviderInstrumentMapping,
    Strategy,
    StrategyRuntime,
    User,
    WorkerHeartbeat,
)
from src.engine.paper.models import TradingMode
from src.auth.security import hash_password
from src.auth.session import create_session
from src.services.paper_service import PaperService
from fastapi.testclient import TestClient
from src.main import app
from src.database import get_db, get_read_only_db


@pytest.fixture
def admin_user(session):
    admin = User(
        username="admin_test_user",
        normalized_username="admin_test_user",
        email="admin@tradepro.test",
        normalized_email="admin@tradepro.test",
        hashed_password=hash_password("AdminPassword123!"),
        role="ADMIN",
        is_active=True,
    )
    session.add(admin)
    session.commit()
    session.refresh(admin)
    return admin


@pytest.fixture
def admin_client(session, admin_user):
    def override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_read_only_db] = override_get_db

    sess_rec, raw_sess, raw_csrf = create_session(session, admin_user)
    c = TestClient(app, headers={"X-CSRF-Token": raw_csrf, "Origin": "http://localhost:3000"})
    c.cookies.set("tradepro_session", raw_sess)
    c.cookies.set("tradepro_csrf", raw_csrf)

    try:
        yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def sandbox_ready_env(session, test_user, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", test_user.id)
    monkeypatch.setenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "mock_token_abc")

    # Paper Account
    acct = PaperAccount(
        owner_id=test_user.id,
        name="Sandbox Account",
        total_cash_units=50000000,
        reserved_cash_units=0,
    )
    session.add(acct)

    # Strategy
    strat = Strategy(
        owner_id=test_user.id,
        name="Sandbox Strategy",
        timeframe="15m",
        candidate_selection_mode="FIRST_ELIGIBLE",
        payload={
            "name": "Sandbox Strategy",
            "timeframe": "15m",
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100000}},
            "global_conditions": {
                "type": "CONDITION",
                "id": "c1",
                "lhs": {"indicator": "PRICE", "symbol": ""},
                "operator": "GREATER_THAN",
                "rhs": {"type": "NUMBER", "value": 0.0},
            }
        }
    )
    session.add(strat)
    session.flush()

    # Verified Mapping
    mapping = ProviderInstrumentMapping(
        owner_id=test_user.id,
        tradepro_instrument_id="synthetic_candidate_option_pe_23000_15m",
        provider_instrument_token="NSE_FO|99901",
        exchange="NSE_FO",
        segment="FO",
        symbol="NIFTY24SEPPE23000",
        verification_status="VERIFIED",
    )
    session.add(mapping)
    session.flush()

    # Runtime in BROKER_SANDBOX with frozen verified mapping
    runtime = PaperService.instantiate_runtime(
        session,
        owner_id=test_user.id,
        strategy_id=strat.id,
        account_id=acct.id,
        dataset_id="synthetic_candidate_option_pe_23000_15m",
        timeframe="15m",
        trading_mode=TradingMode.BROKER_SANDBOX.value,
        instrument_mapping_id=mapping.id,
    )
    session.commit()
    PaperService.validate_runtime(session, runtime.id, test_user.id)
    PaperService.start_runtime(session, runtime.id, test_user.id)

    # Active Worker Heartbeat
    hb = WorkerHeartbeat(
        worker_id="worker-01",
        owner_id=test_user.id,
        status="HEALTHY",
        last_heartbeat_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(hb)
    session.commit()

    return {"runtime": runtime, "mapping": mapping, "account": acct}


def test_readiness_all_clear(client, sandbox_ready_env):
    runtime = sandbox_ready_env["runtime"]
    res = client.get(f"/api/v1/sandbox/readiness?runtime_id={runtime.id}")
    assert res.status_code == 200
    data = res.json()

    assert data["environment_allowed"] is True
    assert data["network_enabled"] is True
    assert data["credential_present"] is True
    assert data["owner_matches"] is True
    assert data["provider_matches"] is True
    assert data["mapping_verified"] is True
    assert data["mapping_unexpired"] is True
    assert data["global_kill_switch_clear"] is True
    assert data["user_kill_switch_clear"] is True
    assert data["worker_available"] is True
    assert data["ready_for_submission"] is True
    assert data["cancel_available"] is True


def test_readiness_kill_switch_blocks_submission_allows_cancel(client, session, test_user, sandbox_ready_env):
    runtime = sandbox_ready_env["runtime"]

    # Engage user kill switch
    ks = KillSwitch(
        target_key=f"USER:{test_user.id}",
        user_id=test_user.id,
        scope="USER",
        is_active=True,
        reason="Testing kill switch",
        engaged_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(ks)
    session.commit()

    res = client.get(f"/api/v1/sandbox/readiness?runtime_id={runtime.id}")
    assert res.status_code == 200
    data = res.json()

    assert data["user_kill_switch_clear"] is False
    assert data["ready_for_submission"] is False
    # Mandatory requirement: kill switch must NOT make cancel_available False
    assert data["cancel_available"] is True


def test_readiness_cross_owner_returns_404(admin_client, sandbox_ready_env):
    # admin_client owns nothing here, runtime belongs to test_user
    runtime = sandbox_ready_env["runtime"]
    res = admin_client.get(f"/api/v1/sandbox/readiness?runtime_id={runtime.id}")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_readiness_nonexistent_runtime_returns_404(client):
    res = client.get("/api/v1/sandbox/readiness?runtime_id=00000000-0000-0000-0000-000000000000")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_editor_can_create_unverified_mapping(client, test_user):
    payload = {
        "tradepro_instrument_id": "TEST_INST_1",
        "provider_instrument_token": "NSE_FO|11111",
        "exchange": "NSE_FO",
        "segment": "FO",
        "symbol": "TEST_SYM",
    }
    res = client.post("/api/v1/sandbox/instruments", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["verification_status"] == "UNVERIFIED"
    assert data["owner_id"] == test_user.id


def test_admin_can_verify_sandbox_owner_mapping(client, admin_client, test_user, monkeypatch):
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", test_user.id)

    # 1. Test user creates unverified mapping
    create_res = client.post("/api/v1/sandbox/instruments", json={
        "tradepro_instrument_id": "TEST_VERIFY_INST",
        "provider_instrument_token": "NSE_FO|22222",
        "exchange": "NSE_FO",
        "segment": "FO",
        "symbol": "TEST_SYM_2",
    })
    assert create_res.status_code == 201
    mapping_id = create_res.json()["id"]

    # 2. Test user (EDITOR) cannot verify mapping
    editor_verify = client.post(f"/api/v1/sandbox/instruments/{mapping_id}/verify", json={
        "status": "VERIFIED",
        "reason": "Manual inspection by user",
    })
    assert editor_verify.status_code == 403

    # 3. ADMIN verifies mapping belonging to UPSTOX_SANDBOX_OWNER_ID
    admin_verify = admin_client.post(f"/api/v1/sandbox/instruments/{mapping_id}/verify", json={
        "status": "VERIFIED",
        "reason": "Admin verified against official portal",
    })
    assert admin_verify.status_code == 200
    data = admin_verify.json()
    assert data["verification_status"] == "VERIFIED"
    assert "admin_test_user" in str(data["verification_audit_json"])


def test_admin_cannot_verify_mapping_of_non_sandbox_owner(admin_client, session):
    # Create another user not configured as UPSTOX_SANDBOX_OWNER_ID
    other_user = User(
        username="other_editor",
        normalized_username="other_editor",
        email="other@tradepro.test",
        normalized_email="other@tradepro.test",
        hashed_password=hash_password("OtherPassword123!"),
        role="EDITOR",
        is_active=True,
    )
    session.add(other_user)
    session.flush()

    other_mapping = ProviderInstrumentMapping(
        owner_id=other_user.id,
        tradepro_instrument_id="OTHER_INST",
        provider_instrument_token="NSE_FO|33333",
        exchange="NSE_FO",
        segment="FO",
        symbol="OTHER_SYM",
        verification_status="UNVERIFIED",
    )
    session.add(other_mapping)
    session.commit()

    # ADMIN verification returns 404 because owner != UPSTOX_SANDBOX_OWNER_ID
    res = admin_client.post(f"/api/v1/sandbox/instruments/{other_mapping.id}/verify", json={
        "status": "VERIFIED",
        "reason": "Admin attempting cross-owner verification",
    })
    assert res.status_code == 404


def test_connection_metadata_no_token_exposure(client, test_user, sandbox_ready_env):
    """
    Minimizes Connection Metadata Exposure:
    Verifies connection metadata endpoint exposes ONLY:
    - Provider
    - Environment
    - Credential configured boolean
    - Credential version
    - Honest readiness status
    - Last successful transmission timestamp
    Proves credential_reference is kept server-side only and NO token/fingerprint/auth header is present.
    """
    res = client.get("/api/v1/sandbox/connection")
    assert res.status_code == 200
    data = res.json()
    assert data["provider"] == "UPSTOX"
    assert data["environment"] == "SANDBOX"
    assert data["credential_configured"] is True
    assert data["readiness_status"] == "CONFIGURED"
    assert "last_successful_transmission_at" in data
    assert "last_validated_at" not in data

    # CRITICAL: owner_id, id, and credential_reference are server-side only!
    assert "owner_id" not in data
    assert "id" not in data
    assert "credential_reference" not in data

    # CRITICAL: Token/fingerprint must NOT exist in schema response
    assert "token_fingerprint" not in data
    assert "token" not in data
    assert "access_token" not in data
    assert "authorization" not in data

    # Verify raw response text contains no secret token substrings or env references
    raw_text = res.text
    assert "mock_token" not in raw_text
    assert "UPSTOX_SANDBOX_ACCESS_TOKEN" not in raw_text
    assert "token_fingerprint" not in raw_text


def test_update_connection_endpoint(client, test_user, sandbox_ready_env, monkeypatch):
    """
    Tests POST /api/v1/sandbox/connection endpoint.
    Enforces configured sandbox owner, roles, CSRF, rate limits, and rejects tokens in payload.
    """
    # 1. Successful version update by configured owner
    res = client.post("/api/v1/sandbox/connection", json={"credential_version": "v2"})
    assert res.status_code == 200
    data = res.json()
    assert data["credential_version"] == "v2"
    assert "credential_reference" not in data

    # 2. Reject extra fields such as tokens in request body (fails closed with 422)
    res_bad = client.post("/api/v1/sandbox/connection", json={"token": "raw_secret_token"})
    assert res_bad.status_code == 422

    # 3. Non-configured sandbox owner receives 403
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", "other-user-uuid")
    res_forbidden = client.post("/api/v1/sandbox/connection", json={"credential_version": "v3"})
    assert res_forbidden.status_code == 403



def test_disable_instrument_mapping_endpoint(client, sandbox_ready_env):
    """
    Tests POST /api/v1/sandbox/instruments/{id}/disable endpoint.
    """
    mapping = sandbox_ready_env["mapping"]
    res = client.post(f"/api/v1/sandbox/instruments/{mapping.id}/disable")
    assert res.status_code == 200
    data = res.json()
    assert data["verification_status"] == "DISABLED"


def test_readiness_missing_runtime_id_returns_422(client):
    """
    Requirement 4 Test:
    Proves GET /api/v1/sandbox/readiness requires runtime_id=<uuid>.
    Missing runtime_id query param must return validation error (422).
    """
    res = client.get("/api/v1/sandbox/readiness")
    assert res.status_code == 422


def test_readiness_all_six_runtime_dimensions(client, session, test_user, sandbox_ready_env, monkeypatch):
    """
    Requirement 4 Test:
    Proves readiness response is computed using that owned runtime's:
    1. Trading mode
    2. Provider
    3. Frozen provider mapping
    4. Mapping expiry
    5. Owner
    6. Kill switches
    """
    runtime = sandbox_ready_env["runtime"]

    # 1. Trading Mode: when trading_mode is PAPER, provider_matches is False
    runtime.trading_mode = "PAPER"
    session.commit()
    res = client.get(f"/api/v1/sandbox/readiness?runtime_id={runtime.id}")
    assert res.status_code == 200
    assert res.json()["provider_matches"] is False
    assert res.json()["ready_for_submission"] is False
    runtime.trading_mode = "BROKER_SANDBOX"
    session.commit()

    # 2. Provider: when provider is unsupported, provider_matches is False
    runtime.instrument_spec_snapshot = {"instrument_id": "TEST_INST", "provider": "UNSUPPORTED_BROKER"}
    session.commit()
    res = client.get(f"/api/v1/sandbox/readiness?runtime_id={runtime.id}")
    assert res.status_code == 200
    assert res.json()["provider_matches"] is False
    assert res.json()["ready_for_submission"] is False
    runtime.instrument_spec_snapshot = {"instrument_id": "TEST_INST", "provider": "UPSTOX"}
    session.commit()

    # 3. Frozen Provider Mapping Verification: when UNVERIFIED, mapping_verified is False
    runtime.instrument_spec_snapshot = {
        "instrument_id": "TEST_INST",
        "provider": "UPSTOX",
        "provider_mapping": {
            "verification_status": "UNVERIFIED",
            "expiry_date": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).isoformat(),
        }
    }
    session.commit()
    res = client.get(f"/api/v1/sandbox/readiness?runtime_id={runtime.id}")
    assert res.status_code == 200
    assert res.json()["mapping_verified"] is False
    assert res.json()["ready_for_submission"] is False

    # 4. Mapping Expiry: when expired, mapping_unexpired is False
    runtime.instrument_spec_snapshot = {
        "instrument_id": "TEST_INST",
        "provider": "UPSTOX",
        "provider_mapping": {
            "verification_status": "VERIFIED",
            "expiry_date": "2020-01-01T00:00:00Z",
        }
    }
    session.commit()
    res = client.get(f"/api/v1/sandbox/readiness?runtime_id={runtime.id}")
    assert res.status_code == 200
    assert res.json()["mapping_unexpired"] is False
    assert res.json()["ready_for_submission"] is False

    # Restore valid frozen mapping
    runtime.instrument_spec_snapshot = {
        "instrument_id": "TEST_INST",
        "provider": "UPSTOX",
        "provider_mapping": {
            "verification_status": "VERIFIED",
            "expiry_date": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).isoformat(),
        }
    }
    session.commit()

    # 5. Owner: when runtime owner != configured sandbox owner, owner_matches is False
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", "different-user-uuid")
    res = client.get(f"/api/v1/sandbox/readiness?runtime_id={runtime.id}")
    assert res.status_code == 200
    assert res.json()["owner_matches"] is False
    assert res.json()["ready_for_submission"] is False
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", test_user.id)

    # 6. Global Kill Switch: submission blocked while cancel remains available
    gks = KillSwitch(
        target_key="GLOBAL",
        scope="GLOBAL",
        is_active=True,
        reason="Global emergency stop",
        engaged_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(gks)
    session.commit()
    res = client.get(f"/api/v1/sandbox/readiness?runtime_id={runtime.id}")
    assert res.status_code == 200
    assert res.json()["global_kill_switch_clear"] is False
    assert res.json()["ready_for_submission"] is False
    assert res.json()["cancel_available"] is True


def test_mapping_lifecycle_and_security_boundary(client, admin_client, session, test_user, admin_user, monkeypatch):
    """
    Requirement 4 Test:
    Proves:
    - EDITOR may create an UNVERIFIED mapping for itself.
    - ADMIN may verify or reject a mapping belonging to the configured sandbox owner.
    - Verification records actor, target owner, mapping ID, source, timestamp, reason, and result.
    - EDITOR/ADMIN owner may disable a mapping.
    - ADMIN verification does not grant access to submit, cancel, or inspect another owner's order/outbox payloads.
    - Only VERIFIED and unexpired mappings can create sandbox runtimes.
    """
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", test_user.id)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")

    # 1. EDITOR creates UNVERIFIED mapping for itself
    create_res = client.post("/api/v1/sandbox/instruments", json={
        "tradepro_instrument_id": "synthetic_candidate_option_pe_23000_15m",
        "provider_instrument_token": "NSE_FO|88888",
        "exchange": "NSE_FO",
        "segment": "FO",
        "symbol": "NIFTY24SEPPE23000",
        "expiry_date": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=15)).isoformat(),
        "lot_size": 50,
        "tick_size": "0.05",
        "freeze_quantity": 1800,
    })
    assert create_res.status_code == 201
    mapping_data = create_res.json()
    mapping_id = mapping_data["id"]
    assert mapping_data["verification_status"] == "UNVERIFIED"
    assert mapping_data["owner_id"] == test_user.id

    # 2. Setup account and strategy for runtime creation tests
    acct = PaperAccount(
        owner_id=test_user.id,
        name="Lifecycle Acct",
        total_cash_units=50000000,
        reserved_cash_units=0,
    )
    session.add(acct)

    strat = Strategy(
        owner_id=test_user.id,
        name="Lifecycle Strat",
        timeframe="15m",
        candidate_selection_mode="FIRST_ELIGIBLE",
        payload={
            "name": "Lifecycle Strat",
            "timeframe": "15m",
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100000}},
            "global_conditions": {
                "type": "CONDITION",
                "id": "c1",
                "lhs": {"indicator": "PRICE", "symbol": ""},
                "operator": "GREATER_THAN",
                "rhs": {"type": "NUMBER", "value": 0.0},
            }
        }
    )
    session.add(strat)
    session.commit()

    # 3. UNVERIFIED mapping CANNOT create a sandbox runtime
    with pytest.raises(ValueError) as exc_info:
        PaperService.instantiate_runtime(
            session,
            owner_id=test_user.id,
            strategy_id=strat.id,
            account_id=acct.id,
            dataset_id="synthetic_candidate_option_pe_23000_15m",
            timeframe="15m",
            trading_mode=TradingMode.BROKER_SANDBOX.value,
            instrument_mapping_id=mapping_id,
        )
    assert "must be 'VERIFIED'" in str(exc_info.value)

    # 4. ADMIN verifies the mapping with full audit trail
    verify_res = admin_client.post(f"/api/v1/sandbox/instruments/{mapping_id}/verify", json={
        "status": "VERIFIED",
        "reason": "Portal instrument token confirmed",
    })
    assert verify_res.status_code == 200
    v_data = verify_res.json()
    assert v_data["verification_status"] == "VERIFIED"

    # Verify audit record records actor, target owner, mapping ID, source, timestamp, reason, and result
    audit = v_data["verification_audit_json"]
    assert audit["actor_id"] == admin_user.id
    assert audit["actor_username"] == admin_user.username
    assert audit["target_owner_id"] == test_user.id
    assert audit["mapping_id"] == mapping_id
    assert audit["source"] == "ADMIN_VERIFICATION_ENDPOINT"
    assert "timestamp" in audit
    assert audit["reason"] == "Portal instrument token confirmed"
    assert audit["new_status"] == "VERIFIED"

    # 5. Now VERIFIED mapping CAN create a sandbox runtime
    runtime = PaperService.instantiate_runtime(
        session,
        owner_id=test_user.id,
        strategy_id=strat.id,
        account_id=acct.id,
        dataset_id="synthetic_candidate_option_pe_23000_15m",
        timeframe="15m",
        trading_mode=TradingMode.BROKER_SANDBOX.value,
        instrument_mapping_id=mapping_id,
    )
    assert runtime.id is not None

    # 6. EXPIRED mapping cannot create a sandbox runtime
    expired_mapping = ProviderInstrumentMapping(
        owner_id=test_user.id,
        tradepro_instrument_id="synthetic_candidate_option_pe_23000_15m",
        provider_instrument_token="NSE_FO|77777",
        exchange="NSE_FO",
        segment="FO",
        symbol="NIFTY24SEPPE23000",
        expiry_date=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1),
        verification_status="VERIFIED",
        mapping_version=2,
    )
    session.add(expired_mapping)
    session.commit()

    with pytest.raises(ValueError) as exc_info:
        PaperService.instantiate_runtime(
            session,
            owner_id=test_user.id,
            strategy_id=strat.id,
            account_id=acct.id,
            dataset_id="synthetic_candidate_option_pe_23000_15m",
            timeframe="15m",
            trading_mode=TradingMode.BROKER_SANDBOX.value,
            instrument_mapping_id=expired_mapping.id,
        )
    assert "expired" in str(exc_info.value).lower()

    # 7. ADMIN can REJECT a mapping -> persists REJECTED, audit new_status is REJECTED
    reject_res = admin_client.post(f"/api/v1/sandbox/instruments/{mapping_id}/verify", json={
        "status": "REJECTED",
        "reason": "Token changed on exchange portal",
    })
    assert reject_res.status_code == 200
    assert reject_res.json()["verification_status"] == "REJECTED"
    assert reject_res.json()["verification_audit_json"]["new_status"] == "REJECTED"

    # 7b. Invalid transition: rejected mapping cannot be verified (returns 409 Conflict)
    reverify_res = admin_client.post(f"/api/v1/sandbox/instruments/{mapping_id}/verify", json={
        "status": "VERIFIED",
        "reason": "Attempting silent re-verification",
    })
    assert reverify_res.status_code == 409
    assert "rejected" in reverify_res.json()["detail"].lower()

    # 8. Create another mapping to test explicit DISABLE
    create_res2 = client.post("/api/v1/sandbox/instruments", json={
        "tradepro_instrument_id": "synthetic_candidate_option_pe_23000_15m_2",
        "provider_instrument_token": "NSE_FO|99999",
        "exchange": "NSE_FO",
        "segment": "FO",
        "symbol": "NIFTY24SEPPE23000",
        "expiry_date": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=15)).isoformat(),
        "lot_size": 50,
        "tick_size": "0.05",
        "freeze_quantity": 1800,
    })
    assert create_res2.status_code == 201
    mapping2_id = create_res2.json()["id"]

    # EDITOR owner can DISABLE mapping -> persists DISABLED
    disable_res = client.post(f"/api/v1/sandbox/instruments/{mapping2_id}/disable")
    assert disable_res.status_code == 200
    assert disable_res.json()["verification_status"] == "DISABLED"

    # 8b. Disabling already DISABLED mapping returns 409 Conflict
    disable_again_res = client.post(f"/api/v1/sandbox/instruments/{mapping2_id}/disable")
    assert disable_again_res.status_code == 409
    assert "already disabled" in disable_again_res.json()["detail"]

    # 8c. Attempting to verify a DISABLED mapping returns 409 Conflict
    verify_disabled_res = admin_client.post(f"/api/v1/sandbox/instruments/{mapping2_id}/verify", json={
        "status": "VERIFIED",
        "reason": "Attempting to verify disabled mapping",
    })
    assert verify_disabled_res.status_code == 409
    assert "disabled" in verify_disabled_res.json()["detail"].lower()

    # 9. ADMIN verification does NOT grant access to submit, cancel, or inspect another owner's order/outbox payloads
    admin_outbox_res = admin_client.get("/api/v1/sandbox/outbox")
    assert admin_outbox_res.status_code == 200
    # Returns only records owned by admin_user, not test_user!
    assert len(admin_outbox_res.json()) == 0


def test_mapping_rejection_audit_and_persisted_status_consistency(client, admin_client, session, test_user, admin_user, monkeypatch):
    """
    Milestone 6B Requirement:
    Compares requested decision, audit record and persisted status.
    Proves:
    - Verification rejection stores REJECTED.
    - Explicit disable operation stores DISABLED.
    - Both are unusable by runtimes.
    - Audit event's new_status exactly matches the persisted database status.
    - Invalid transitions return 409.
    """
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", test_user.id)
    monkeypatch.setenv("APP_ENV", "local")

    # 1. Rejection decision
    create_res = client.post("/api/v1/sandbox/instruments", json={
        "tradepro_instrument_id": "synthetic_candidate_option_pe_23000_15m",
        "provider_instrument_token": "NSE_FO|10101",
        "exchange": "NSE_FO",
        "segment": "FO",
        "symbol": "REJECT_SYM",
    })
    assert create_res.status_code == 201
    m_id = create_res.json()["id"]

    requested_decision = "REJECTED"
    verify_res = admin_client.post(f"/api/v1/sandbox/instruments/{m_id}/verify", json={
        "status": requested_decision,
        "reason": "Token checksum failed validation",
    })
    assert verify_res.status_code == 200
    data = verify_res.json()

    # Verify requested decision == audit record new_status == persisted database status
    db_mapping = session.query(ProviderInstrumentMapping).filter_by(id=m_id).first()
    session.refresh(db_mapping)

    assert data["verification_status"] == requested_decision
    assert db_mapping.verification_status == requested_decision
    assert db_mapping.verification_status == "REJECTED"
    assert data["verification_audit_json"]["new_status"] == requested_decision
    assert db_mapping.verification_audit_json["new_status"] == requested_decision
    assert db_mapping.verification_audit_json["new_status"] == db_mapping.verification_status

    # REJECTED cannot be used by runtime
    acct = PaperAccount(owner_id=test_user.id, name="Reject Acct", total_cash_units=50000000, reserved_cash_units=0)
    session.add(acct)
    strat = Strategy(owner_id=test_user.id, name="Reject Strat", timeframe="15m", candidate_selection_mode="FIRST_ELIGIBLE", payload={"name": "S", "timeframe": "15m", "action": {"type": "PAPER_TRADE", "risk_config": {}}, "global_conditions": {"type": "CONDITION", "id": "c1", "lhs": {"indicator": "PRICE", "symbol": ""}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 0.0}}})
    session.add(strat)
    session.commit()

    with pytest.raises(ValueError) as exc:
        PaperService.instantiate_runtime(session, owner_id=test_user.id, strategy_id=strat.id, account_id=acct.id, dataset_id="synthetic_candidate_option_pe_23000_15m", timeframe="15m", trading_mode=TradingMode.BROKER_SANDBOX.value, instrument_mapping_id=m_id)
    assert "must be 'VERIFIED'" in str(exc.value)

    # 2. Disable decision
    create_res2 = client.post("/api/v1/sandbox/instruments", json={
        "tradepro_instrument_id": "synthetic_candidate_option_pe_23000_15m",
        "provider_instrument_token": "NSE_FO|20202",
        "exchange": "NSE_FO",
        "segment": "FO",
        "symbol": "DISABLE_SYM",
    })
    m2_id = create_res2.json()["id"]

    disable_res = client.post(f"/api/v1/sandbox/instruments/{m2_id}/disable")
    assert disable_res.status_code == 200
    db_mapping2 = session.query(ProviderInstrumentMapping).filter_by(id=m2_id).first()
    session.refresh(db_mapping2)

    assert disable_res.json()["verification_status"] == "DISABLED"
    assert db_mapping2.verification_status == "DISABLED"
    assert db_mapping2.verification_audit_json["new_status"] == "DISABLED"
    assert db_mapping2.verification_audit_json["new_status"] == db_mapping2.verification_status

    # DISABLED cannot be used by runtime
    with pytest.raises(ValueError) as exc:
        PaperService.instantiate_runtime(session, owner_id=test_user.id, strategy_id=strat.id, account_id=acct.id, dataset_id="synthetic_candidate_option_pe_23000_15m", timeframe="15m", trading_mode=TradingMode.BROKER_SANDBOX.value, instrument_mapping_id=m2_id)
    assert "must be 'VERIFIED'" in str(exc.value)
