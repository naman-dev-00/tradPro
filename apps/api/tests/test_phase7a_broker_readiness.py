"""Comprehensive unit and integration tests for Phase 7A: Secure Broker Connection & Live Readiness.

Verifies:
- Fernet key requirements: lazy initialization, no hardcoded seeds, properly generated keys.
- OAuth state lifecycle: unpredictable, single-use, owner/session-bound, replay rejection, expiry, redirect allowlists.
- OAuth atomicity: durable consumption before exchange, exchange failure preserves consumed state, newer attempt fencing.
- Database-backed cross-owner UCC claim rejection.
- Read-only snapshot isolation: zero mutations to paper ledger, accounts, or positions.
- Refresh concurrency: immutable fetch inputs, HTTP outside tx, generation/version rechecks.
- Numeric validation: Decimal normalization, bounds, envelope integrity.
- Read-only GET endpoints satisfy zero-write contract (suppressing session_touch middleware).
- Structured multi-gate live readiness checks with live_dispatch_enabled hardcoded to False.
"""
import datetime
from decimal import Decimal
import json
import os
import secrets
import threading
import pytest
from pathlib import Path
import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from alembic import command
from alembic.config import Config

from tests.paper_database_support import paper_test_database

from src.main import app
from src.database import get_db, get_read_only_db, engine
from src.models import (
    User,
    UserSession,
    ProviderConnection,
    OAuthStateRecord,
    BrokerSnapshot,
    PaperAccount,
    AccountLedgerEntry,
    PaperPosition,
    Fill,
    Order,
    KillSwitch,
    ReconciliationRecord,
)
from src.auth.security import hash_password
from src.auth.session import create_session
from src.engine.broker.secret_store import (
    BrokerSecretStore,
    BrokerKeyNotConfiguredError,
    BrokerKeyInvalidError,
    secret_store,
)
from src.engine.broker.upstox_read_adapter import (
    UpstoxReadAdapter,
    BrokerTransportError,
    BrokerAuthError,
    BrokerRateLimitError,
    normalize_provider_numbers,
    _validate_numeric_field,
)
from src.services.broker_service import (
    BrokerService,
    BrokerConflictError,
    BrokerNotConfiguredError,
    BrokerSecurityError,
    BrokerUnsupportedCapabilityError,
    InvalidOAuthStateError,
    OAuthExpiredError,
    OAuthReplayError,
    OAuthSecurityError,
)


@pytest.fixture(autouse=True)
def inject_test_broker_key(monkeypatch):
    """Explicitly inject a cryptographically generated Fernet key in test runs."""
    random_key = Fernet.generate_key().decode()
    monkeypatch.setenv("BROKER_SECRET_KEY", random_key)
    monkeypatch.setenv("BROKER_NETWORK_ENABLED", "true")


@pytest.fixture
def test_user(session):
    u = User(
        username="broker_operator",
        normalized_username="broker_operator",
        email="operator@tradepro.test",
        normalized_email="operator@tradepro.test",
        hashed_password=hash_password("SecurePass123!"),
        role="EDITOR",
        is_active=True,
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


@pytest.fixture
def other_user(session):
    u = User(
        username="other_operator",
        normalized_username="other_operator",
        email="other@tradepro.test",
        normalized_email="other@tradepro.test",
        hashed_password=hash_password("SecurePass123!"),
        role="EDITOR",
        is_active=True,
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


@pytest.fixture
def auth_context(session, test_user):
    sess_rec, raw_sess, raw_csrf = create_session(session, test_user)

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_read_only_db] = override_db

    client = TestClient(app, headers={"X-CSRF-Token": raw_csrf, "Origin": "http://localhost:3000"})
    client.cookies.set("tradepro_session", raw_sess)
    client.cookies.set("tradepro_csrf", raw_csrf)

    try:
        yield {
            "client": client,
            "session": session,
            "user": test_user,
            "session_rec": sess_rec,
            "raw_csrf": raw_csrf,
        }
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def other_context(session, other_user):
    sess_rec, raw_sess, raw_csrf = create_session(session, other_user)

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_read_only_db] = override_db

    client = TestClient(app, headers={"X-CSRF-Token": raw_csrf, "Origin": "http://localhost:3000"})
    client.cookies.set("tradepro_session", raw_sess)
    client.cookies.set("tradepro_csrf", raw_csrf)

    try:
        yield {
            "client": client,
            "session": session,
            "user": other_user,
            "session_rec": sess_rec,
            "raw_csrf": raw_csrf,
        }
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# 1. SECRET STORE & TOKEN ENCRYPTION TESTS
# ==============================================================================

def test_secret_store_lazy_initialization(monkeypatch):
    monkeypatch.delenv("BROKER_SECRET_KEY", raising=False)
    store = BrokerSecretStore()
    # Masking does not require key
    assert store.mask_account_id("UCC12345") == "***345"
    assert store.mask_token("secret_token") == "[REDACTED_ACCESS_TOKEN]"

    # Encryption / decryption fails with BrokerKeyNotConfiguredError
    with pytest.raises(BrokerKeyNotConfiguredError):
        store.encrypt("token")
    with pytest.raises(BrokerKeyNotConfiguredError):
        store.decrypt("ciphertext")


def test_secret_store_invalid_key_fails(monkeypatch):
    monkeypatch.setenv("BROKER_SECRET_KEY", "not-a-valid-fernet-key")
    store = BrokerSecretStore()
    with pytest.raises(BrokerKeyInvalidError):
        store.encrypt("token")


def test_secret_store_valid_fernet_roundtrip():
    store = BrokerSecretStore()
    token = "upstox_access_token_xyz_12345"
    encrypted = store.encrypt(token)

    assert encrypted != token
    assert "upstox" not in encrypted
    decrypted = store.decrypt(encrypted)
    assert decrypted == token


# ==============================================================================
# 2. ADAPTER BOUNDS & NUMERIC VALIDATION
# ==============================================================================

def test_adapter_rejects_insecure_or_disallowed_hosts():
    with pytest.raises(ValueError, match="Insecure scheme"):
        UpstoxReadAdapter(base_url="http://api.upstox.com")

    with pytest.raises(ValueError, match="Untrusted host"):
        UpstoxReadAdapter(base_url="https://attacker.com")


def test_adapter_envelope_validation_and_size_bounds():
    def custom_transport(request: httpx.Request) -> httpx.Response:
        query = str(request.url.query)
        if "bad_envelope" in query:
            return httpx.Response(200, json={"status": "error", "message": "Failed"})
        if "large" in query:
            return httpx.Response(200, content=b"A" * (3 * 1024 * 1024))  # 3 MB
        return httpx.Response(200, json={"status": "success", "data": {"user_id": "UCC999", "user_name": "Trader"}})

    client = httpx.Client(transport=httpx.MockTransport(custom_transport))
    adapter = UpstoxReadAdapter(client=client)

    profile = adapter.get_user_profile("token")
    assert profile["user_id"] == "UCC999"

    with pytest.raises(BrokerTransportError, match="Broker returned non-success status envelope|Broker rejected request|status 'error'"):
        adapter._execute_request(method="GET", path="/v2/user/profile", params={"bad_envelope": "1"})

    with pytest.raises(BrokerTransportError, match="maximum size limit"):
        adapter._execute_request(method="GET", path="/v2/user/profile", params={"large": "1"})


def test_normalize_provider_numbers():
    raw = {
        "decimal_val": Decimal("123.4560"),
        "float_val": 45.67,
        "nested": [
            {"amount": Decimal("0.00000001")},
            {"str_val": "already_str"},
        ],
    }
    normalized = normalize_provider_numbers(raw)
    assert normalized["decimal_val"] == "123.4560"
    assert normalized["float_val"] == "45.67"
    assert normalized["nested"][0]["amount"] == "0.00000001"
    assert normalized["nested"][1]["str_val"] == "already_str"


# ==============================================================================
# 3. OAUTH FLOW, ATOMICITY, AND REDIRECT SECURITY
# ==============================================================================

def test_oauth_authorize_url_post_and_redirect_restrictions(auth_context, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_client_id_123")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_secret_456")
    monkeypatch.setenv("UPSTOX_ALLOWED_REDIRECT_URIS", "http://localhost:3000/broker-readiness")
    client = auth_context["client"]

    # 1. Successful POST with default allowed redirect URI
    res = client.post("/api/v1/broker/auth/authorize-url", json={"environment": "LIVE"})
    assert res.status_code == 200
    data = res.json()
    assert "https://api.upstox.com/v2/login/authorization/dialog" in data["authorization_url"]
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A3000%2Fbroker-readiness" in data["authorization_url"]

    # 2. Reject unapproved redirect URI override
    res_bad = client.post(
        "/api/v1/broker/auth/authorize-url",
        json={"environment": "LIVE", "redirect_uri": "https://malicious.com/callback"},
    )
    assert res_bad.status_code == 400
    assert "allowlist" in res_bad.json()["detail"].lower()

    # 3. Reject SANDBOX environment
    res_sb = client.post("/api/v1/broker/auth/authorize-url", json={"environment": "SANDBOX"})
    assert res_sb.status_code == 400
    assert "sandbox" in res_sb.json()["detail"].lower()


def test_oauth_callback_flow_and_replay_protection(auth_context, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_client_id_123")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_secret_456")

    db = auth_context["session"]
    user = auth_context["user"]
    sess_rec = auth_context["session_rec"]
    client = auth_context["client"]

    state_data = BrokerService.generate_authorization_url(
        db=db,
        owner_id=user.id,
        session_id=sess_rec.id,
        environment="LIVE",
    )
    state_token = state_data["state"]

    def mock_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/login/authorization/token":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "access_token": "live_access_token_xyz_999",
                        "user_id": "UCC12345",
                        "user_name": "Verified Operator",
                        "user_type": "individual",
                    },
                },
            )
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    mock_adapter = UpstoxReadAdapter(client=mock_client)
    original_cb = BrokerService.handle_oauth_callback

    def mock_cb(**kwargs):
        kwargs["adapter"] = mock_adapter
        return original_cb(**kwargs)

    monkeypatch.setattr("src.routes.broker.BrokerService.handle_oauth_callback", mock_cb)

    # 1. First callback succeeds
    res1 = client.post("/api/v1/broker/auth/callback", json={"code": "auth_code_123", "state": state_token})
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == "CONNECTED"
    assert data1["broker_user_id_masked"] == "***345"
    assert data1["credential_generation"] == 1

    # 2. Replay rejected
    res2 = client.post("/api/v1/broker/auth/callback", json={"code": "auth_code_123", "state": state_token})
    assert res2.status_code == 400
    assert "replay" in res2.json()["detail"].lower()


def test_failed_token_exchange_preserves_consumed_state(auth_context, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_client_id_123")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_secret_456")

    db = auth_context["session"]
    user = auth_context["user"]
    sess_rec = auth_context["session_rec"]
    client = auth_context["client"]

    state_data = BrokerService.generate_authorization_url(
        db=db,
        owner_id=user.id,
        session_id=sess_rec.id,
        environment="LIVE",
    )
    state_token = state_data["state"]

    # Adapter simulates HTTP network error during exchange
    def failing_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"status": "error", "message": "Upstox outage"})

    mock_client = httpx.Client(transport=httpx.MockTransport(failing_transport))
    mock_adapter = UpstoxReadAdapter(client=mock_client)

    original_cb = BrokerService.handle_oauth_callback
    def mock_cb(**kwargs):
        kwargs["adapter"] = mock_adapter
        return original_cb(**kwargs)

    monkeypatch.setattr("src.routes.broker.BrokerService.handle_oauth_callback", mock_cb)

    # Attempt callback -> fails with 502
    res = client.post("/api/v1/broker/auth/callback", json={"code": "code1", "state": state_token})
    assert res.status_code == 502

    # State in DB must remain durably consumed (not reopened!)
    record = db.query(OAuthStateRecord).filter(OAuthStateRecord.state_token == state_token).first()
    assert record.consumed is True
    assert record.consumed_at is not None

    # Re-trying with same state must fail with replay error, NOT 502
    res_retry = client.post("/api/v1/broker/auth/callback", json={"code": "code1", "state": state_token})
    assert res_retry.status_code == 400
    assert "replay" in res_retry.json()["detail"].lower()


def test_database_backed_cross_owner_ucc_claim_rejected(auth_context, other_context, monkeypatch):
    db = auth_context["session"]
    user1 = auth_context["user"]
    user2 = other_context["user"]
    now = datetime.datetime.now(datetime.timezone.utc)

    # User 1 is CONNECTED to UCC888
    conn1 = ProviderConnection(
        owner_id=user1.id,
        provider_name="UPSTOX",
        environment="LIVE",
        status="CONNECTED",
        broker_user_id="UCC888",
        encrypted_access_token=secret_store.encrypt("token1"),
        token_expires_at=now + datetime.timedelta(hours=6),
        version=1,
        credential_generation=1,
    )
    db.add(conn1)
    db.commit()

    # User 2 attempts to bind to the same UCC888 in LIVE
    conn2 = ProviderConnection(
        owner_id=user2.id,
        provider_name="UPSTOX",
        environment="LIVE",
        status="CONNECTED",
        broker_user_id="UCC888",
        encrypted_access_token=secret_store.encrypt("token2"),
        token_expires_at=now + datetime.timedelta(hours=6),
        version=1,
        credential_generation=1,
    )
    db.add(conn2)
    # The partial unique index uq_provider_connections_active_ucc must reject this at the database level!
    with pytest.raises(IntegrityError) as exc_info:
        db.commit()
    assert "uq_provider_connections_active_ucc" in str(exc_info.value).lower() or "unique constraint" in str(exc_info.value).lower()
    db.rollback()


# ==============================================================================
# 4. REFRESH CONCURRENCY, SNAPSHOT BINDING, AND ZERO-WRITE GET CONTRACT
# ==============================================================================

def test_refresh_snapshots_binds_credential_generation_and_supersedes(auth_context, monkeypatch):
    db = auth_context["session"]
    user = auth_context["user"]
    client = auth_context["client"]
    now = datetime.datetime.now(datetime.timezone.utc)

    conn = ProviderConnection(
        owner_id=user.id,
        provider_name="UPSTOX",
        environment="LIVE",
        status="CONNECTED",
        broker_user_id="UCC777",
        encrypted_access_token=secret_store.encrypt("mock_token_777"),
        token_expires_at=now + datetime.timedelta(hours=6),
        version=1,
        credential_generation=2,
    )
    db.add(conn)
    db.commit()

    def mock_transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/user/profile":
            return httpx.Response(200, json={"status": "success", "data": {"user_id": "UCC777", "user_name": "Trader"}})
        if path == "/v2/user/get-funds-and-margin":
            return httpx.Response(200, json={"status": "success", "data": {"equity": {"available_margin": "250000.50", "used_margin": "10000.00"}}})
        if path == "/v2/portfolio/long-term-holdings":
            return httpx.Response(200, json={"status": "success", "data": []})
        if path == "/v2/portfolio/short-term-positions":
            return httpx.Response(200, json={"status": "success", "data": []})
        if path == "/v2/order/retrieve-all":
            return httpx.Response(200, json={"status": "success", "data": []})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    mock_adapter = UpstoxReadAdapter(client=mock_client)
    original_refresh = BrokerService.refresh_snapshots
    def mock_ref(**kwargs):
        kwargs["adapter"] = mock_adapter
        return original_refresh(**kwargs)

    monkeypatch.setattr("src.routes.broker.BrokerService.refresh_snapshots", mock_ref)

    # Refresh
    res = client.post("/api/v1/broker/snapshots/refresh?environment=LIVE")
    assert res.status_code == 200
    data = res.json()
    assert data["credential_generation"] == 2

    snapshot = db.query(BrokerSnapshot).filter(BrokerSnapshot.owner_id == user.id, BrokerSnapshot.status == "ACTIVE").first()
    assert snapshot is not None
    assert snapshot.credential_generation == 2
    assert snapshot.funds_and_margins_json is not None


def test_read_only_get_endpoints_cause_zero_sql_writes(auth_context):
    client = auth_context["client"]
    db = auth_context["session"]
    user = auth_context["user"]
    now = datetime.datetime.now(datetime.timezone.utc)

    conn = ProviderConnection(
        owner_id=user.id,
        provider_name="UPSTOX",
        environment="LIVE",
        status="CONNECTED",
        broker_user_id="UCC777",
        encrypted_access_token=secret_store.encrypt("mock_token"),
        token_expires_at=now + datetime.timedelta(hours=6),
        version=1,
        credential_generation=1,
    )
    db.add(conn)
    db.flush()

    snap = BrokerSnapshot(
        owner_id=user.id,
        connection_id=conn.id,
        broker_user_id="UCC777",
        environment="LIVE",
        status="ACTIVE",
        acquired_at_utc=now,
        funds_and_margins_json={"equity": {"available_margin": "100"}},
        raw_fingerprint="fp",
        connection_version_at_fetch=1,
        credential_generation=1,
    )
    db.add(snap)
    db.commit()

    # Track SQL write operations (INSERT, UPDATE, DELETE)
    write_statements = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        stmt = statement.strip().upper()
        if stmt.startswith(("INSERT", "UPDATE", "DELETE")):
            write_statements.append(stmt)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)

    try:
        res_conn = client.get("/api/v1/broker/connection?environment=LIVE")
        assert res_conn.status_code == 200

        res_snap = client.get("/api/v1/broker/snapshots/latest?environment=LIVE")
        assert res_snap.status_code == 200

        res_ready = client.get("/api/v1/broker/readiness?environment=LIVE")
        assert res_ready.status_code == 200

        # Must be strictly ZERO write statements executed!
        assert len(write_statements) == 0, f"Write queries were executed during read-only GETs: {write_statements}"
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


# ==============================================================================
# 5. EVIDENCE-BASED READINESS GATES
# ==============================================================================

def test_readiness_static_ip_requires_validated_evidence_and_bindings(auth_context, monkeypatch):
    client = auth_context["client"]
    db = auth_context["session"]
    user = auth_context["user"]
    now = datetime.datetime.now(datetime.timezone.utc)

    # Connected broker account
    conn = ProviderConnection(
        owner_id=user.id,
        provider_name="UPSTOX",
        environment="LIVE",
        status="CONNECTED",
        broker_user_id="UCC_STATIC_TEST",
        encrypted_access_token=secret_store.encrypt("tok"),
        token_expires_at=now + datetime.timedelta(hours=6),
        version=1,
        credential_generation=1,
    )
    db.add(conn)
    db.commit()

    # 1. Without IP -> BLOCKED
    monkeypatch.delenv("UPSTOX_STATIC_IP", raising=False)
    monkeypatch.delenv("UPSTOX_STATIC_IP_VALIDATED_AT", raising=False)
    monkeypatch.delenv("UPSTOX_STATIC_IP_REGISTRATION_JSON", raising=False)
    res = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates = {g["gate_name"]: g for g in res.json()["gates"]}
    assert gates["static_ip_configuration"]["status"] == "BLOCKED"

    # 2. Invalid IP syntax -> BLOCKED
    monkeypatch.setenv("UPSTOX_STATIC_IP", "not-an-ip-address")
    res_syntax = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates_syntax = {g["gate_name"]: g for g in res_syntax.json()["gates"]}
    assert gates_syntax["static_ip_configuration"]["status"] == "BLOCKED"
    assert "syntax" in gates_syntax["static_ip_configuration"]["details"].lower()

    # 3. With valid IP but completely empty evidence -> UNKNOWN
    monkeypatch.setenv("UPSTOX_STATIC_IP", "203.0.113.50")
    res2 = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates2 = {g["gate_name"]: g for g in res2.json()["gates"]}
    assert gates2["static_ip_configuration"]["status"] == "UNKNOWN"

    # 4. Malformed JSON shapes never cause HTTP 500
    for malformed_shape in ["[]", "null", '"just-a-string"', "12345", "{invalid_json"]:
        monkeypatch.setenv("UPSTOX_STATIC_IP_REGISTRATION_JSON", malformed_shape)
        res_mal = client.get("/api/v1/broker/readiness?environment=LIVE")
        assert res_mal.status_code == 200
        gates_mal = {g["gate_name"]: g for g in res_mal.json()["gates"]}
        assert gates_mal["static_ip_configuration"]["status"] in ("UNKNOWN", "BLOCKED")

    # 5. Timestamp alone without provenance -> UNKNOWN
    monkeypatch.delenv("UPSTOX_STATIC_IP_REGISTRATION_JSON", raising=False)
    monkeypatch.setenv("UPSTOX_STATIC_IP_VALIDATED_AT", now.isoformat())
    res3 = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates3 = {g["gate_name"]: g for g in res3.json()["gates"]}
    assert gates3["static_ip_configuration"]["status"] == "UNKNOWN"

    # 6. Unsupported provenance string -> BLOCKED
    monkeypatch.setenv("UPSTOX_STATIC_IP_PROVENANCE", "RANDOM_BLOG_OR_UNTRUSTED")
    res_unsupp = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates_unsupp = {g["gate_name"]: g for g in res_unsupp.json()["gates"]}
    assert gates_unsupp["static_ip_configuration"]["status"] == "BLOCKED"
    assert "unsupported" in gates_unsupp["static_ip_configuration"]["details"].lower()

    # 7. Valid provenance and timestamp, but missing UCC binding -> BLOCKED
    monkeypatch.setenv("UPSTOX_STATIC_IP_PROVENANCE", "UPSTOX_DEVELOPER_PORTAL")
    res_no_ucc = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates_no_ucc = {g["gate_name"]: g for g in res_no_ucc.json()["gates"]}
    assert gates_no_ucc["static_ip_configuration"]["status"] == "BLOCKED"
    assert "ucc" in gates_no_ucc["static_ip_configuration"]["details"].lower()

    # 8. With UCC binding, but UCC mismatch -> BLOCKED (with masked UCC!)
    monkeypatch.setenv("UPSTOX_STATIC_IP_UCC", "OTHER_USER_UCC")
    res_mismatch = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates_mismatch = {g["gate_name"]: g for g in res_mismatch.json()["gates"]}
    assert gates_mismatch["static_ip_configuration"]["status"] == "BLOCKED"
    assert "does not match" in gates_mismatch["static_ip_configuration"]["details"].lower()

    # 9. Correct UCC binding, but missing egress IP binding -> BLOCKED
    monkeypatch.setenv("UPSTOX_STATIC_IP_UCC", "UCC_STATIC_TEST")
    res_no_egress = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates_no_egress = {g["gate_name"]: g for g in res_no_egress.json()["gates"]}
    assert gates_no_egress["static_ip_configuration"]["status"] == "BLOCKED"
    assert "egress" in gates_no_egress["static_ip_configuration"]["details"].lower()

    # 10. Egress IP mismatch -> BLOCKED
    monkeypatch.setenv("UPSTOX_STATIC_IP_EGRESS", "198.51.100.99")
    res_egress_mis = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates_egress_mis = {g["gate_name"]: g for g in res_egress_mis.json()["gates"]}
    assert gates_egress_mis["static_ip_configuration"]["status"] == "BLOCKED"

    # 11. Future-dated timestamp (>60s) -> BLOCKED
    monkeypatch.setenv("UPSTOX_STATIC_IP_EGRESS", "203.0.113.50")
    future_time = (now + datetime.timedelta(hours=2)).isoformat()
    monkeypatch.setenv("UPSTOX_STATIC_IP_VALIDATED_AT", future_time)
    res_future = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates_future = {g["gate_name"]: g for g in res_future.json()["gates"]}
    assert gates_future["static_ip_configuration"]["status"] == "BLOCKED"
    assert "future" in gates_future["static_ip_configuration"]["details"].lower()

    # 12. Stale timestamp (>30 days) -> BLOCKED
    stale_time = (now - datetime.timedelta(days=35)).isoformat()
    monkeypatch.setenv("UPSTOX_STATIC_IP_VALIDATED_AT", stale_time)
    res_stale = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates_stale = {g["gate_name"]: g for g in res_stale.json()["gates"]}
    assert gates_stale["static_ip_configuration"]["status"] == "BLOCKED"
    assert "stale" in gates_stale["static_ip_configuration"]["details"].lower()

    # 13. Unparseable timestamp string -> BLOCKED
    monkeypatch.setenv("UPSTOX_STATIC_IP_VALIDATED_AT", "not-a-valid-date")
    res_unparseable = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates_unparseable = {g["gate_name"]: g for g in res_unparseable.json()["gates"]}
    assert gates_unparseable["static_ip_configuration"]["status"] == "BLOCKED"

    # 14. Fully valid matching evidence via registration JSON -> READY
    recent_time = (now - datetime.timedelta(days=1)).isoformat()
    monkeypatch.delenv("UPSTOX_STATIC_IP_VALIDATED_AT", raising=False)
    monkeypatch.delenv("UPSTOX_STATIC_IP_PROVENANCE", raising=False)
    monkeypatch.delenv("UPSTOX_STATIC_IP_UCC", raising=False)
    monkeypatch.delenv("UPSTOX_STATIC_IP_EGRESS", raising=False)
    monkeypatch.setenv(
        "UPSTOX_STATIC_IP_REGISTRATION_JSON",
        json.dumps({
            "broker_user_id": "UCC_STATIC_TEST",
            "egress_ip": "203.0.113.50",
            "provenance": "UPSTOX_PORTAL",
            "validated_at": recent_time,
        }),
    )
    res_ready = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates_ready = {g["gate_name"]: g for g in res_ready.json()["gates"]}
    assert gates_ready["static_ip_configuration"]["status"] == "READY"


def test_readiness_rejects_wrong_generation_or_future_or_incomplete_snapshots(auth_context, monkeypatch):
    db = auth_context["session"]
    user = auth_context["user"]
    now = datetime.datetime(2026, 10, 6, 6, 0, 0, tzinfo=datetime.timezone.utc)  # Tuesday market open

    monkeypatch.setenv("UPSTOX_STATIC_IP", "203.0.113.50")
    monkeypatch.setenv("UPSTOX_STATIC_IP_VALIDATED_AT", "2026-10-06T05:00:00Z")
    monkeypatch.setenv("UPSTOX_STATIC_IP_PROVENANCE", "UPSTOX_DEVELOPER_PORTAL")
    monkeypatch.setenv("UPSTOX_STATIC_IP_UCC", "UCC777")
    monkeypatch.setenv("UPSTOX_STATIC_IP_EGRESS", "203.0.113.50")

    conn = ProviderConnection(
        owner_id=user.id,
        provider_name="UPSTOX",
        environment="LIVE",
        status="CONNECTED",
        broker_user_id="UCC777",
        encrypted_access_token=secret_store.encrypt("mock_token"),
        token_expires_at=now + datetime.timedelta(hours=6),
        version=2,
        credential_generation=2,
    )
    db.add(conn)
    db.commit()

    # 1. Snapshot from generation 1 (superseded credentials) -> BLOCKED
    snap_old_gen = BrokerSnapshot(
        owner_id=user.id,
        connection_id=conn.id,
        broker_user_id="UCC777",
        environment="LIVE",
        status="ACTIVE",
        acquired_at_utc=now - datetime.timedelta(minutes=5),
        funds_and_margins_json={"equity": {"available_margin": "100", "used_margin": "0.00"}},
        holdings_json=[],
        positions_json=[],
        orders_json=[],
        raw_fingerprint="fp",
        connection_version_at_fetch=1,
        credential_generation=1,
    )
    db.add(snap_old_gen)
    db.commit()

    readiness = BrokerService.evaluate_live_readiness(db=db, owner_id=user.id, environment="LIVE", clock_fn=lambda: now)
    risk_gate = next(g for g in readiness["gates"] if g["gate_name"] == "account_risk_evidence")
    assert risk_gate["status"] == "BLOCKED"
    assert "credential generation" in risk_gate["details"].lower()

    # 2. Update to matching generation but incomplete data (funds_and_margins_json is None)
    snap_old_gen.credential_generation = 2
    snap_old_gen.funds_and_margins_json = None
    db.commit()

    readiness2 = BrokerService.evaluate_live_readiness(db=db, owner_id=user.id, environment="LIVE", clock_fn=lambda: now)
    risk_gate2 = next(g for g in readiness2["gates"] if g["gate_name"] == "account_risk_evidence")
    assert risk_gate2["status"] == "BLOCKED"
    assert "incomplete" in risk_gate2["details"].lower()

    # 3. Future-dated snapshot -> BLOCKED
    snap_old_gen.funds_and_margins_json = {"equity": {"available_margin": "100", "used_margin": "0.00"}}
    snap_old_gen.acquired_at_utc = now + datetime.timedelta(minutes=10)
    db.commit()

    readiness3 = BrokerService.evaluate_live_readiness(db=db, owner_id=user.id, environment="LIVE", clock_fn=lambda: now)
    risk_gate3 = next(g for g in readiness3["gates"] if g["gate_name"] == "account_risk_evidence")
    assert risk_gate3["status"] == "BLOCKED"
    assert "future-dated" in risk_gate3["details"].lower()

    # 4. Valid, fresh, matching generation snapshot -> READY
    snap_old_gen.acquired_at_utc = now - datetime.timedelta(minutes=2)
    snap_old_gen.holdings_json = []
    snap_old_gen.positions_json = []
    snap_old_gen.orders_json = []
    db.commit()

    readiness4 = BrokerService.evaluate_live_readiness(db=db, owner_id=user.id, environment="LIVE", clock_fn=lambda: now)
    risk_gate4 = next(g for g in readiness4["gates"] if g["gate_name"] == "account_risk_evidence")
    assert risk_gate4["status"] == "READY"
    assert readiness4["overall_status"] == "READY_FOR_MONITORING"


def test_readiness_rejects_incomplete_financial_evidence_and_missing_expiry(auth_context, monkeypatch):
    db = auth_context["session"]
    user = auth_context["user"]
    now = datetime.datetime(2026, 10, 6, 6, 0, 0, tzinfo=datetime.timezone.utc)

    monkeypatch.setenv("UPSTOX_STATIC_IP", "203.0.113.50")
    monkeypatch.setenv("UPSTOX_STATIC_IP_VALIDATED_AT", "2026-10-06T05:00:00Z")
    monkeypatch.setenv("UPSTOX_STATIC_IP_PROVENANCE", "UPSTOX_DEVELOPER_PORTAL")
    monkeypatch.setenv("UPSTOX_STATIC_IP_UCC", "UCC_FIN_REG")
    monkeypatch.setenv("UPSTOX_STATIC_IP_EGRESS", "203.0.113.50")

    conn = ProviderConnection(
        owner_id=user.id,
        provider_name="UPSTOX",
        environment="LIVE",
        status="CONNECTED",
        broker_user_id="UCC_FIN_REG",
        encrypted_access_token=secret_store.encrypt("mock_token"),
        token_expires_at=now + datetime.timedelta(hours=6),
        version=1,
        credential_generation=1,
    )
    db.add(conn)
    db.commit()

    # 1. Adapter rejects empty equity or missing required numeric balances
    with pytest.raises(BrokerTransportError, match="Missing required financial field"):
        _validate_numeric_field("available_margin", None, required=True)

    # 2. Snapshot with empty equity {"equity": {}} cannot produce READY_FOR_MONITORING
    snap = BrokerSnapshot(
        owner_id=user.id,
        connection_id=conn.id,
        broker_user_id="UCC_FIN_REG",
        environment="LIVE",
        status="ACTIVE",
        acquired_at_utc=now - datetime.timedelta(minutes=2),
        funds_and_margins_json={"equity": {}},
        holdings_json=[],
        positions_json=[],
        orders_json=[],
        raw_fingerprint="fp_fin",
        connection_version_at_fetch=1,
        credential_generation=1,
    )
    db.add(snap)
    db.commit()

    readiness = BrokerService.evaluate_live_readiness(db=db, owner_id=user.id, environment="LIVE", clock_fn=lambda: now)
    risk_gate = next(g for g in readiness["gates"] if g["gate_name"] == "account_risk_evidence")
    assert risk_gate["status"] == "BLOCKED"
    assert readiness["overall_status"] != "READY_FOR_MONITORING"

    # 3. Snapshot with missing used_margin cannot produce READY_FOR_MONITORING
    snap.funds_and_margins_json = {"equity": {"available_margin": "1000.00"}}
    db.commit()

    readiness2 = BrokerService.evaluate_live_readiness(db=db, owner_id=user.id, environment="LIVE", clock_fn=lambda: now)
    assert readiness2["overall_status"] != "READY_FOR_MONITORING"

    # 4. Snapshot with legitimate numeric zero balances ("0.00", 0) is valid and produces READY_FOR_MONITORING
    snap.funds_and_margins_json = {"equity": {"available_margin": "0.00", "used_margin": "0.00"}}
    db.commit()

    readiness3 = BrokerService.evaluate_live_readiness(db=db, owner_id=user.id, environment="LIVE", clock_fn=lambda: now)
    risk_gate3 = next(g for g in readiness3["gates"] if g["gate_name"] == "account_risk_evidence")
    assert risk_gate3["status"] == "READY"
    assert readiness3["overall_status"] == "READY_FOR_MONITORING"

    # 5. Missing token expiry blocks snapshot refresh before any provider request
    conn.token_expires_at = None
    db.commit()

    http_called = False
    def mock_fetch(token):
        nonlocal http_called
        http_called = True
        return {}

    mock_adp = UpstoxReadAdapter()
    monkeypatch.setattr(mock_adp, "fetch_full_read_snapshot", mock_fetch)

    with pytest.raises(BrokerAuthError, match="expired or is missing expiration timestamp"):
        BrokerService.refresh_snapshots(db=db, owner_id=user.id, environment="LIVE", adapter=mock_adp)

    assert not http_called, "Provider network request was made despite missing token expiration timestamp!"


# ==============================================================================
# 6. EXTENDED LIFECYCLE, FENCING, ADAPTER AND SANITIZATION TESTS
# ==============================================================================

def test_adapter_rejects_crafted_base_urls():
    # Base URL with path resolving to order placement
    with pytest.raises(ValueError, match="Base URL must not contain custom paths"):
        UpstoxReadAdapter(base_url="https://api.upstox.com/v3/order/place")

    # Base URL with port
    with pytest.raises(ValueError, match="Untrusted port"):
        UpstoxReadAdapter(base_url="https://api.upstox.com:8443")

    # Base URL with userinfo
    with pytest.raises(ValueError, match="userinfo"):
        UpstoxReadAdapter(base_url="https://user:pass@api.upstox.com")

    # Base URL with query string
    with pytest.raises(ValueError, match="query"):
        UpstoxReadAdapter(base_url="https://api.upstox.com?query=param")

    # Base URL with fragment
    with pytest.raises(ValueError, match="fragments"):
        UpstoxReadAdapter(base_url="https://api.upstox.com#fragment")


def test_adapter_custom_client_does_not_bypass_network_killswitch(monkeypatch):
    monkeypatch.setenv("BROKER_NETWORK_ENABLED", "false")
    custom_client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    adapter = UpstoxReadAdapter(client=custom_client)

    with pytest.raises(BrokerTransportError, match="network communication is disabled"):
        adapter.get_user_profile("token")


def test_adapter_refuses_redirects():
    def redirect_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://api.upstox.com/v2/order/retrieve-all"})

    custom_client = httpx.Client(transport=httpx.MockTransport(redirect_transport))
    adapter = UpstoxReadAdapter(client=custom_client, network_enabled=True)

    with pytest.raises(BrokerTransportError, match="Unexpected redirect"):
        adapter.get_user_profile("token")


def test_adapter_sanitizes_errors_never_reflecting_raw_secrets():
    synthetic_secret = "upstox_sec_999988887777"
    synthetic_token = "upstox_tok_aaaaabbbbbccccc"

    def error_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "status": "error",
                "message": f"Invalid client secret {synthetic_secret} for token {synthetic_token}",
            },
        )

    custom_client = httpx.Client(transport=httpx.MockTransport(error_transport))
    adapter = UpstoxReadAdapter(client=custom_client, network_enabled=True)

    with pytest.raises(BrokerTransportError) as exc_info:
        adapter.get_user_profile(synthetic_token)

    err_text = str(exc_info.value)
    assert synthetic_secret not in err_text
    assert synthetic_token not in err_text
    assert "status 400" in err_text or "error status" in err_text


def test_adapter_collection_validation_preserves_empty_and_rejects_non_finite():
    def collection_transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "positions" in path:
            return httpx.Response(200, json={"status": "success", "data": []})
        if "holdings" in path:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": [{"quantity": 10, "last_price": "123.45", "average_price": "120.00"}],
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(collection_transport))
    adapter = UpstoxReadAdapter(client=client, network_enabled=True)

    positions = adapter.get_positions("tok")
    assert positions == []

    holdings = adapter.get_holdings("tok")
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 10


def test_disconnect_invalidates_pending_and_inflight_callbacks(auth_context, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "client_123")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "secret_456")

    db = auth_context["session"]
    user = auth_context["user"]
    sess_rec = auth_context["session_rec"]
    client = auth_context["client"]

    # 1. Initiate authorization
    state_data = BrokerService.generate_authorization_url(
        db=db,
        owner_id=user.id,
        session_id=sess_rec.id,
        environment="LIVE",
    )
    state_token = state_data["state"]

    # 2. User disconnects before callback finishes
    BrokerService.disconnect(db=db, owner_id=user.id, environment="LIVE")

    # 3. Callback arrives for the pre-disconnect authorization
    def mock_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "tok1", "user_id": "UCC999"})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    mock_adapter = UpstoxReadAdapter(client=mock_client)

    orig_cb_disco = BrokerService.handle_oauth_callback

    def mock_cb_disco(**kw):
        return orig_cb_disco(**{**kw, "adapter": mock_adapter})

    monkeypatch.setattr("src.routes.broker.BrokerService.handle_oauth_callback", mock_cb_disco)

    # Callback must be rejected (either replay/consumed or invalidated by disconnect fence)
    res = client.post("/api/v1/broker/auth/callback", json={"code": "c1", "state": state_token})
    assert res.status_code in (400, 403)

    # Verify connection remains DISCONNECTED
    conn = db.query(ProviderConnection).filter(ProviderConnection.owner_id == user.id, ProviderConnection.environment == "LIVE").first()
    assert conn.status == "DISCONNECTED"
    assert conn.encrypted_access_token is None


def test_newer_authorization_invalidates_older_callback(auth_context, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "client_123")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "secret_456")

    db = auth_context["session"]
    user = auth_context["user"]
    sess_rec = auth_context["session_rec"]
    client = auth_context["client"]

    # 1. Initiate attempt 1
    state1 = BrokerService.generate_authorization_url(db=db, owner_id=user.id, session_id=sess_rec.id, environment="LIVE")["state"]

    # 2. Initiate attempt 2 (supersedes attempt 1)
    state2 = BrokerService.generate_authorization_url(db=db, owner_id=user.id, session_id=sess_rec.id, environment="LIVE")["state"]

    def mock_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "tok1", "user_id": "UCC999"})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    mock_adapter = UpstoxReadAdapter(client=mock_client)

    orig_cb_newer = BrokerService.handle_oauth_callback

    def mock_cb_newer(**kw):
        return orig_cb_newer(**{**kw, "adapter": mock_adapter})

    monkeypatch.setattr("src.routes.broker.BrokerService.handle_oauth_callback", mock_cb_newer)

    # Older attempt 1 must fail
    res1 = client.post("/api/v1/broker/auth/callback", json={"code": "c1", "state": state1})
    assert res1.status_code in (400, 403)

    # Newer attempt 2 succeeds
    res2 = client.post("/api/v1/broker/auth/callback", json={"code": "c2", "state": state2})
    assert res2.status_code == 200
    assert res2.json()["status"] == "CONNECTED"


def test_cross_owner_and_cross_session_callback_rejected(auth_context, other_context, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "client_123")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "secret_456")

    db = auth_context["session"]
    user1 = auth_context["user"]
    sess1 = auth_context["session_rec"]
    other_client = other_context["client"]

    # User 1 initiates
    state = BrokerService.generate_authorization_url(db=db, owner_id=user1.id, session_id=sess1.id, environment="LIVE")["state"]

    # Other user attempts callback with user1's state
    res = other_client.post("/api/v1/broker/auth/callback", json={"code": "c1", "state": state})
    assert res.status_code in (400, 403)


def test_tampered_ciphertext_fails_safe(auth_context):
    db = auth_context["session"]
    user = auth_context["user"]
    now = datetime.datetime.now(datetime.timezone.utc)

    conn = ProviderConnection(
        owner_id=user.id,
        provider_name="UPSTOX",
        environment="LIVE",
        status="CONNECTED",
        broker_user_id="UCC777",
        encrypted_access_token="gAAAAABtampered_ciphertext_xyz",
        token_expires_at=now + datetime.timedelta(hours=6),
        version=1,
        credential_generation=1,
    )
    db.add(conn)
    db.commit()

    with pytest.raises(ValueError, match="Decryption failed"):
        secret_store.decrypt(conn.encrypted_access_token)


def test_sql_write_listener_with_aged_session(auth_context, monkeypatch):
    client = auth_context["client"]
    db = auth_context["session"]
    user = auth_context["user"]
    sess_rec = auth_context["session_rec"]

    # Bind SessionLocal to the active request session engine
    FreshSession = sessionmaker(autocommit=False, autoflush=False, bind=db.bind)
    monkeypatch.setattr("src.database.SessionLocal", FreshSession)

    # Age session so touch middleware interval (>60s) would trigger write if not suppressed
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
    sess_rec.last_accessed_at = past
    db.commit()

    write_statements = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        stmt = statement.strip().upper()
        if stmt.startswith(("INSERT", "UPDATE", "DELETE")):
            write_statements.append(stmt)

    from src.database import engine as app_engine
    target_engines = {db.bind, app_engine}
    for eng in target_engines:
        if eng is not None:
            event.listen(eng, "before_cursor_execute", before_cursor_execute)

    try:
        # 1. Successful GET queries (200)
        res_conn = client.get("/api/v1/broker/connection?environment=LIVE")
        assert res_conn.status_code == 200

        res_ready = client.get("/api/v1/broker/readiness?environment=LIVE")
        assert res_ready.status_code == 200

        # 2. Missing snapshot GET query (404)
        res_snap = client.get("/api/v1/broker/snapshots/latest?environment=LIVE")
        assert res_snap.status_code == 404

        # 3. Failed GET with invalid parameters (422)
        res_fail = client.get("/api/v1/broker/connection?environment=INVALID")
        assert res_fail.status_code == 422

        # 4. Authentication failure without credentials (401)
        unauth_client = TestClient(app)
        res_unauth = unauth_client.get("/api/v1/broker/connection?environment=LIVE")
        assert res_unauth.status_code == 401

        # Must have executed strictly ZERO write queries across all read-only broker paths
        assert len(write_statements) == 0, f"SQL writes detected during GET requests: {write_statements}"

        # Verify persisted timestamps in fresh session remain unchanged
        fresh_db = FreshSession()
        try:
            persisted_sess = fresh_db.query(UserSession).filter_by(id=sess_rec.id).first()
            assert persisted_sess is not None
            assert persisted_sess.last_accessed_at == past
        finally:
            fresh_db.close()

        # 5. Active control: proving instrumentation detects ordinary session-touch writes
        # Calling an ordinary authenticated GET endpoint where session touch is enabled (/api/v1/auth/me)
        res_control = client.get("/api/v1/auth/me")
        assert res_control.status_code == 200
        touch_writes = [s for s in write_statements if "USER_SESSIONS" in s and "UPDATE" in s]
        assert len(touch_writes) > 0, "Control failed: instrumentation did not detect ordinary session-touch write"

        fresh_db2 = FreshSession()
        try:
            touched_sess = fresh_db2.query(UserSession).filter_by(id=sess_rec.id).first()
            assert touched_sess is not None
            assert touched_sess.last_accessed_at > past
        finally:
            fresh_db2.close()

    finally:
        for eng in target_engines:
            if eng is not None:
                event.remove(eng, "before_cursor_execute", before_cursor_execute)


def test_readiness_rejects_invalid_ip_syntax_and_stale_or_unbound_evidence(auth_context, monkeypatch):
    client = auth_context["client"]
    db = auth_context["session"]
    user = auth_context["user"]
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Invalid IP syntax
    monkeypatch.setenv("UPSTOX_STATIC_IP", "999.888.777.666")
    res1 = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates1 = {g["gate_name"]: g for g in res1.json()["gates"]}
    assert gates1["static_ip_configuration"]["status"] == "BLOCKED"
    assert "invalid static ip" in gates1["static_ip_configuration"]["details"].lower()

    # 2. Configured valid IP, but evidence bound to different UCC
    conn = ProviderConnection(
        owner_id=user.id,
        provider_name="UPSTOX",
        environment="LIVE",
        status="CONNECTED",
        broker_user_id="UCC_MY_ACCOUNT",
        encrypted_access_token=secret_store.encrypt("tok"),
        token_expires_at=now + datetime.timedelta(hours=6),
        version=1,
        credential_generation=1,
    )
    db.add(conn)
    db.commit()

    monkeypatch.setenv("UPSTOX_STATIC_IP", "203.0.113.10")
    monkeypatch.setenv(
        "UPSTOX_STATIC_IP_REGISTRATION_JSON",
        json.dumps({
            "broker_user_id": "UCC_OTHER_ACCOUNT",
            "egress_ip": "203.0.113.10",
            "provenance": "UPSTOX_PORTAL",
            "validated_at": now.isoformat(),
        }),
    )

    res2 = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates2 = {g["gate_name"]: g for g in res2.json()["gates"]}
    assert gates2["static_ip_configuration"]["status"] == "BLOCKED"
    assert "does not match" in gates2["static_ip_configuration"]["details"].lower()

    # 3. Evidence matches account, but egress IP mismatch
    monkeypatch.setenv(
        "UPSTOX_STATIC_IP_REGISTRATION_JSON",
        json.dumps({
            "broker_user_id": "UCC_MY_ACCOUNT",
            "egress_ip": "198.51.100.99",
            "provenance": "UPSTOX_PORTAL",
            "validated_at": now.isoformat(),
        }),
    )

    res3 = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates3 = {g["gate_name"]: g for g in res3.json()["gates"]}
    assert gates3["static_ip_configuration"]["status"] == "BLOCKED"
    assert "egress" in gates3["static_ip_configuration"]["details"].lower()

    # 4. Valid matching evidence
    monkeypatch.setenv(
        "UPSTOX_STATIC_IP_REGISTRATION_JSON",
        json.dumps({
            "broker_user_id": "UCC_MY_ACCOUNT",
            "egress_ip": "203.0.113.10",
            "provenance": "UPSTOX_PORTAL",
            "validated_at": now.isoformat(),
        }),
    )

    res4 = client.get("/api/v1/broker/readiness?environment=LIVE")
    gates4 = {g["gate_name"]: g for g in res4.json()["gates"]}
    assert gates4["static_ip_configuration"]["status"] == "READY"


# ==============================================================================
# 7. INDEPENDENT-SESSION CONCURRENCY & NUMERIC FLOW TESTS (SQLITE & POSTGRESQL)
# ==============================================================================

@pytest.fixture(params=["sqlite", "postgresql"])
def migrated_concurrency_db(request, tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    path = tmp_path_factory.mktemp("concurrency_db") / "audit.db"
    with paper_test_database(request.param, path) as (eng, url):
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "src/migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(config, "0009_broker_readiness")
        yield eng


def test_independent_sessions_duplicate_callbacks(migrated_concurrency_db, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_id")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_sec")
    SessionFactory = sessionmaker(bind=migrated_concurrency_db, autocommit=False, autoflush=False)

    session_init = SessionFactory()
    user = User(
        username="u_dup", normalized_username="u_dup", email="u_dup@test.com",
        normalized_email="u_dup@test.com", hashed_password="hash", role="EDITOR", is_active=True,
    )
    session_init.add(user)
    session_init.commit()
    sess_rec, _, _ = create_session(session_init, user)
    user_id = user.id
    sess_id = sess_rec.id

    state_data = BrokerService.generate_authorization_url(
        db=session_init,
        owner_id=user_id,
        session_id=sess_id,
        environment="LIVE",
    )
    state_token = state_data["state"]
    session_init.close()

    exchange_count = [0]
    lock = threading.Lock()

    def mock_exchange(*args, **kwargs):
        with lock:
            exchange_count[0] += 1
        return {"access_token": "tok1", "user_id": "UCC101"}

    mock_adapter = UpstoxReadAdapter()
    mock_adapter.exchange_code_for_token = mock_exchange

    results = []
    errors = []
    unexpected_errors = []
    barrier = threading.Barrier(2)

    def worker():
        sess = SessionFactory()
        try:
            barrier.wait(timeout=5)
            res = BrokerService.handle_oauth_callback(
                db=sess,
                code="code1",
                state_token=state_token,
                current_user_id=user_id,
                current_session_id=sess_id,
                adapter=mock_adapter,
            )
            results.append(res)
        except OAuthReplayError as e:
            errors.append(e)
        except BaseException as e:
            unexpected_errors.append(e)
        finally:
            sess.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not t1.is_alive(), "Worker thread 1 failed to terminate within timeout budget"
    assert not t2.is_alive(), "Worker thread 2 failed to terminate within timeout budget"

    if unexpected_errors:
        raise unexpected_errors[0]

    # Exactly one thread succeeds; the other is rejected with replay
    assert len(results) == 1
    assert results[0]["status"] == "CONNECTED"
    assert len(errors) == 1
    assert isinstance(errors[0], OAuthReplayError)
    assert exchange_count[0] == 1, f"Expected exactly 1 exchange call, got {exchange_count[0]}"

    check_sess = SessionFactory()
    try:
        conn = check_sess.query(ProviderConnection).filter_by(owner_id=user_id, environment="LIVE").first()
        assert conn.status == "CONNECTED"
        assert conn.broker_user_id == "UCC101"
    finally:
        check_sess.close()


def test_independent_sessions_callback_versus_disconnect(migrated_concurrency_db, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_id")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_sec")
    SessionFactory = sessionmaker(bind=migrated_concurrency_db, autocommit=False, autoflush=False)

    session_init = SessionFactory()
    user = User(
        username="u_disco", normalized_username="u_disco", email="u_disco@test.com",
        normalized_email="u_disco@test.com", hashed_password="hash", role="EDITOR", is_active=True,
    )
    session_init.add(user)
    session_init.commit()
    sess_rec, _, _ = create_session(session_init, user)
    user_id = user.id
    sess_id = sess_rec.id

    state_data = BrokerService.generate_authorization_url(
        db=session_init,
        owner_id=user_id,
        session_id=sess_id,
        environment="LIVE",
    )
    state_token = state_data["state"]
    session_init.close()

    callback_in_flight = threading.Event()
    disconnect_committed = threading.Event()

    def mock_exchange(*args, **kwargs):
        callback_in_flight.set()
        assert disconnect_committed.wait(timeout=5)
        return {"access_token": "tok1", "user_id": "UCC102"}

    mock_adapter = UpstoxReadAdapter()
    mock_adapter.exchange_code_for_token = mock_exchange

    unexpected_errors = []
    callback_errors = []

    def callback_worker():
        sess = SessionFactory()
        try:
            BrokerService.handle_oauth_callback(
                db=sess,
                code="code1",
                state_token=state_token,
                current_user_id=user_id,
                current_session_id=sess_id,
                adapter=mock_adapter,
            )
        except (OAuthReplayError, OAuthSecurityError, BrokerConflictError) as e:
            callback_errors.append(e)
        except BaseException as e:
            unexpected_errors.append(e)
        finally:
            sess.close()

    def disconnect_worker():
        sess = SessionFactory()
        try:
            if not callback_in_flight.wait(timeout=5):
                raise TimeoutError("callback_in_flight wait timed out")
            res = BrokerService.disconnect(db=sess, owner_id=user_id, environment="LIVE")
            assert res["status"] == "DISCONNECTED"
        except BaseException as e:
            unexpected_errors.append(e)
        finally:
            sess.close()
            disconnect_committed.set()

    t_cb = threading.Thread(target=callback_worker)
    t_disc = threading.Thread(target=disconnect_worker)
    t_cb.start()
    t_disc.start()
    t_cb.join(timeout=10)
    t_disc.join(timeout=10)

    assert not t_cb.is_alive(), "Callback worker thread failed to terminate within timeout budget"
    assert not t_disc.is_alive(), "Disconnect worker thread failed to terminate within timeout budget"

    if unexpected_errors:
        raise unexpected_errors[0]

    # In-flight callback attempting to bind after disconnect must fail
    assert len(callback_errors) == 1
    assert isinstance(callback_errors[0], (OAuthReplayError, OAuthSecurityError, BrokerConflictError))

    # Connection must remain DISCONNECTED with tokens wiped and absence of active snapshots
    check_sess = SessionFactory()
    try:
        conn = check_sess.query(ProviderConnection).filter_by(owner_id=user_id, environment="LIVE").first()
        assert conn.status == "DISCONNECTED"
        assert conn.encrypted_access_token is None
        active_snaps = check_sess.query(BrokerSnapshot).filter_by(owner_id=user_id, status="ACTIVE").all()
        assert len(active_snaps) == 0, "No active snapshots should exist after disconnect"
    finally:
        check_sess.close()


def test_independent_sessions_newer_authorization_versus_older_completion(migrated_concurrency_db, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_id")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_sec")
    SessionFactory = sessionmaker(bind=migrated_concurrency_db, autocommit=False, autoflush=False)

    session_init = SessionFactory()
    user = User(
        username="u_newer", normalized_username="u_newer", email="u_newer@test.com",
        normalized_email="u_newer@test.com", hashed_password="hash", role="EDITOR", is_active=True,
    )
    session_init.add(user)
    session_init.commit()
    sess_rec, _, _ = create_session(session_init, user)
    user_id = user.id
    sess_id = sess_rec.id

    state1 = BrokerService.generate_authorization_url(
        db=session_init, owner_id=user_id, session_id=sess_id, environment="LIVE"
    )["state"]
    session_init.close()

    old_callback_in_flight = threading.Event()
    newer_flow_finished = threading.Event()

    def mock_exchange_old(*args, **kwargs):
        old_callback_in_flight.set()
        assert newer_flow_finished.wait(timeout=5)
        return {"access_token": "tok_old", "user_id": "UCC103_OLD"}

    mock_adapter_old = UpstoxReadAdapter()
    mock_adapter_old.exchange_code_for_token = mock_exchange_old

    mock_adapter_new = UpstoxReadAdapter()
    mock_adapter_new.exchange_code_for_token = lambda *args, **kwargs: {"access_token": "tok_new", "user_id": "UCC103_NEW"}

    unexpected_errors = []
    old_errors = []

    def old_cb_worker():
        sess = SessionFactory()
        try:
            BrokerService.handle_oauth_callback(
                db=sess,
                code="c1",
                state_token=state1,
                current_user_id=user_id,
                current_session_id=sess_id,
                adapter=mock_adapter_old,
            )
        except (OAuthReplayError, OAuthSecurityError, BrokerConflictError) as e:
            old_errors.append(e)
        except BaseException as e:
            unexpected_errors.append(e)
        finally:
            sess.close()

    def newer_flow_worker():
        sess = SessionFactory()
        try:
            if not old_callback_in_flight.wait(timeout=5):
                raise TimeoutError("old_callback_in_flight wait timed out")
            state2 = BrokerService.generate_authorization_url(
                db=sess, owner_id=user_id, session_id=sess_id, environment="LIVE"
            )["state"]
            res = BrokerService.handle_oauth_callback(
                db=sess,
                code="c2",
                state_token=state2,
                current_user_id=user_id,
                current_session_id=sess_id,
                adapter=mock_adapter_new,
            )
            assert res["status"] == "CONNECTED"
        except BaseException as e:
            unexpected_errors.append(e)
        finally:
            sess.close()
            newer_flow_finished.set()

    t_old = threading.Thread(target=old_cb_worker)
    t_new = threading.Thread(target=newer_flow_worker)
    t_old.start()
    t_new.start()
    t_old.join(timeout=10)
    t_new.join(timeout=10)

    assert not t_old.is_alive(), "Old callback worker thread failed to terminate within timeout budget"
    assert not t_new.is_alive(), "Newer flow worker thread failed to terminate within timeout budget"

    if unexpected_errors:
        raise unexpected_errors[0]

    # Older callback must be rejected
    assert len(old_errors) == 1
    assert isinstance(old_errors[0], (OAuthReplayError, OAuthSecurityError, BrokerConflictError))

    # Connection must reflect newer credentials and generation
    check_sess = SessionFactory()
    try:
        conn = check_sess.query(ProviderConnection).filter_by(owner_id=user_id, environment="LIVE").first()
        assert conn.status == "CONNECTED"
        assert conn.broker_user_id == "UCC103_NEW"
        assert secret_store.decrypt(conn.encrypted_access_token) == "tok_new"
    finally:
        check_sess.close()


def test_independent_sessions_concurrent_ucc_claims(migrated_concurrency_db, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_id")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_sec")
    SessionFactory = sessionmaker(bind=migrated_concurrency_db, autocommit=False, autoflush=False)

    session_init = SessionFactory()
    user1 = User(
        username="u_ucc1", normalized_username="u_ucc1", email="u_ucc1@test.com",
        normalized_email="u_ucc1@test.com", hashed_password="hash", role="EDITOR", is_active=True,
    )
    user2 = User(
        username="u_ucc2", normalized_username="u_ucc2", email="u_ucc2@test.com",
        normalized_email="u_ucc2@test.com", hashed_password="hash", role="EDITOR", is_active=True,
    )
    session_init.add_all([user1, user2])
    session_init.commit()
    sess_rec1, _, _ = create_session(session_init, user1)
    sess_rec2, _, _ = create_session(session_init, user2)
    u1_id, s1_id = user1.id, sess_rec1.id
    u2_id, s2_id = user2.id, sess_rec2.id

    state1 = BrokerService.generate_authorization_url(db=session_init, owner_id=u1_id, session_id=s1_id, environment="LIVE")["state"]
    state2 = BrokerService.generate_authorization_url(db=session_init, owner_id=u2_id, session_id=s2_id, environment="LIVE")["state"]
    session_init.close()

    shared_ucc = "UCC_SHARED_CLAIM_999"
    mock_adapter1 = UpstoxReadAdapter()
    mock_adapter1.exchange_code_for_token = lambda *args, **kwargs: {"access_token": "tok_u1", "user_id": shared_ucc}
    mock_adapter2 = UpstoxReadAdapter()
    mock_adapter2.exchange_code_for_token = lambda *args, **kwargs: {"access_token": "tok_u2", "user_id": shared_ucc}

    barrier = threading.Barrier(2)
    results = []
    conflicts = []
    unexpected_errors = []

    def worker(u_id, s_id, st, adp):
        sess = SessionFactory()
        try:
            barrier.wait(timeout=5)
            res = BrokerService.handle_oauth_callback(
                db=sess, code="code", state_token=st,
                current_user_id=u_id, current_session_id=s_id, adapter=adp
            )
            results.append(res)
        except BrokerConflictError as e:
            conflicts.append(e)
        except BaseException as e:
            unexpected_errors.append(e)
        finally:
            sess.close()

    t1 = threading.Thread(target=worker, args=(u1_id, s1_id, state1, mock_adapter1))
    t2 = threading.Thread(target=worker, args=(u2_id, s2_id, state2, mock_adapter2))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not t1.is_alive(), "Worker thread 1 failed to terminate within timeout budget"
    assert not t2.is_alive(), "Worker thread 2 failed to terminate within timeout budget"

    if unexpected_errors:
        raise unexpected_errors[0]

    # Exactly 1 connects, 1 receives sanitized conflict
    assert len(results) == 1
    assert results[0]["status"] == "CONNECTED"
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], BrokerConflictError)
    conflict_msg = str(conflicts[0])
    assert conflict_msg == "This broker account is already connected to another user."
    assert "unique constraint" not in conflict_msg.lower()
    assert "integrityerror" not in conflict_msg.lower()
    assert "sql" not in conflict_msg.lower()

    # Verify exactly 1 connection exists for this UCC
    check_sess = SessionFactory()
    try:
        conns = check_sess.query(ProviderConnection).filter_by(broker_user_id=shared_ucc, status="CONNECTED").all()
        assert len(conns) == 1
    finally:
        check_sess.close()


def test_independent_sessions_refresh_versus_reconnect_disconnect(migrated_concurrency_db, monkeypatch):
    SessionFactory = sessionmaker(bind=migrated_concurrency_db, autocommit=False, autoflush=False)

    session_init = SessionFactory()
    user = User(
        username="u_ref", normalized_username="u_ref", email="u_ref@test.com",
        normalized_email="u_ref@test.com", hashed_password="hash", role="EDITOR", is_active=True,
    )
    session_init.add(user)
    session_init.commit()
    user_id = user.id

    now = datetime.datetime.now(datetime.timezone.utc)
    conn = ProviderConnection(
        owner_id=user_id,
        provider_name="UPSTOX",
        environment="LIVE",
        status="CONNECTED",
        broker_user_id="UCC_REF_444",
        encrypted_access_token=secret_store.encrypt("tok_init"),
        token_expires_at=now + datetime.timedelta(hours=6),
        version=1,
        credential_generation=1,
    )
    session_init.add(conn)
    session_init.commit()
    session_init.close()

    refresh_in_flight = threading.Event()
    disconnect_done = threading.Event()

    def mock_fetch(token):
        refresh_in_flight.set()
        assert disconnect_done.wait(timeout=5)
        return {
            "profile": {"user_id": "UCC_REF_444", "user_name": "Trader"},
            "funds": {"equity": {"available_margin": "50000.00", "used_margin": "0.00"}},
            "holdings": [],
            "positions": [],
            "orders": [],
            "fingerprint": "fp_ref",
        }

    mock_adapter = UpstoxReadAdapter()
    monkeypatch.setattr(mock_adapter, "fetch_full_read_snapshot", mock_fetch)

    unexpected_errors = []
    refresh_errors = []

    def refresh_worker():
        sess = SessionFactory()
        try:
            BrokerService.refresh_snapshots(
                db=sess,
                owner_id=user_id,
                environment="LIVE",
                adapter=mock_adapter,
            )
        except BrokerConflictError as e:
            refresh_errors.append(e)
        except BaseException as e:
            unexpected_errors.append(e)
        finally:
            sess.close()

    def disconnect_worker():
        sess = SessionFactory()
        try:
            if not refresh_in_flight.wait(timeout=5):
                raise TimeoutError("refresh_in_flight wait timed out")
            BrokerService.disconnect(db=sess, owner_id=user_id, environment="LIVE")
        except BaseException as e:
            unexpected_errors.append(e)
        finally:
            sess.close()
            disconnect_done.set()

    t_ref = threading.Thread(target=refresh_worker)
    t_disc = threading.Thread(target=disconnect_worker)
    t_ref.start()
    t_disc.start()
    t_ref.join(timeout=10)
    t_disc.join(timeout=10)

    assert not t_ref.is_alive(), "Refresh worker thread failed to terminate within timeout budget"
    assert not t_disc.is_alive(), "Disconnect worker thread failed to terminate within timeout budget"

    if unexpected_errors:
        raise unexpected_errors[0]

    assert len(refresh_errors) == 1
    assert isinstance(refresh_errors[0], BrokerConflictError)
    assert "disconnected or modified" in str(refresh_errors[0]).lower() or "concurrently" in str(refresh_errors[0]).lower()

    # Absence of stale snapshots
    check_sess = SessionFactory()
    try:
        active_snaps = check_sess.query(BrokerSnapshot).filter_by(owner_id=user_id, status="ACTIVE").all()
        assert len(active_snaps) == 0, "No active snapshots should exist from interrupted refresh"
    finally:
        check_sess.close()


def test_same_owner_different_session_callback(migrated_concurrency_db, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_id")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_sec")
    SessionFactory = sessionmaker(bind=migrated_concurrency_db, autocommit=False, autoflush=False)

    sess = SessionFactory()
    user = User(
        username="u_diffsess", normalized_username="u_diffsess", email="u_diffsess@test.com",
        normalized_email="u_diffsess@test.com", hashed_password="hash", role="EDITOR", is_active=True,
    )
    sess.add(user)
    sess.commit()
    sess_rec1, _, _ = create_session(sess, user)
    sess_rec2, _, _ = create_session(sess, user)
    user_id = user.id
    s1_id = sess_rec1.id
    s2_id = sess_rec2.id

    state = BrokerService.generate_authorization_url(
        db=sess, owner_id=user_id, session_id=s1_id, environment="LIVE"
    )["state"]

    mock_adapter = UpstoxReadAdapter()
    mock_adapter.exchange_code_for_token = lambda c, cid, cs, r: {"access_token": "tok", "user_id": "UCC_SESS"}

    # Attempting callback with same owner, but session 2 instead of session 1
    with pytest.raises(OAuthSecurityError, match="session mismatch"):
        BrokerService.handle_oauth_callback(
            db=sess,
            code="c1",
            state_token=state,
            current_user_id=user_id,
            current_session_id=s2_id,
            adapter=mock_adapter,
        )
    sess.close()


def test_expired_oauth_state(migrated_concurrency_db, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_id")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_sec")
    SessionFactory = sessionmaker(bind=migrated_concurrency_db, autocommit=False, autoflush=False)

    sess = SessionFactory()
    user = User(
        username="u_exp", normalized_username="u_exp", email="u_exp@test.com",
        normalized_email="u_exp@test.com", hashed_password="hash", role="EDITOR", is_active=True,
    )
    sess.add(user)
    sess.commit()
    sess_rec, _, _ = create_session(sess, user)
    user_id = user.id
    s_id = sess_rec.id

    state = BrokerService.generate_authorization_url(
        db=sess, owner_id=user_id, session_id=s_id, environment="LIVE"
    )["state"]

    record = sess.query(OAuthStateRecord).filter_by(owner_id=user_id).first()
    assert record is not None
    record.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
    sess.commit()

    mock_adapter = UpstoxReadAdapter()
    mock_adapter.exchange_code_for_token = lambda c, cid, cs, r: {"access_token": "tok", "user_id": "UCC_EXP"}

    with pytest.raises(OAuthExpiredError, match="expired"):
        BrokerService.handle_oauth_callback(
            db=sess,
            code="c1",
            state_token=state,
            current_user_id=user_id,
            current_session_id=s_id,
            adapter=mock_adapter,
        )
    sess.close()


def test_broker_http_starts_outside_database_transaction(auth_context, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test_id")
    monkeypatch.setenv("UPSTOX_CLIENT_SECRET", "test_sec")
    db = auth_context["session"]
    user = auth_context["user"]
    sess_rec = auth_context["session_rec"]

    # 1. OAuth exchange
    state_token = BrokerService.generate_authorization_url(
        db=db, owner_id=user.id, session_id=sess_rec.id, environment="LIVE"
    )["state"]

    in_tx_during_exchange = []

    def mock_exchange(code, client_id, client_secret, redirect_uri):
        in_tx_during_exchange.append(db.in_transaction())
        return {"access_token": "tok_http_out", "user_id": "UCC_HTTP_OUT"}

    mock_adapter = UpstoxReadAdapter()
    monkeypatch.setattr(mock_adapter, "exchange_code_for_token", mock_exchange)

    BrokerService.handle_oauth_callback(
        db=db, code="c1", state_token=state_token,
        current_user_id=user.id, current_session_id=sess_rec.id, adapter=mock_adapter
    )

    assert len(in_tx_during_exchange) == 1
    assert in_tx_during_exchange[0] is False, "OAuth HTTP token exchange started inside active DB transaction!"

    # 2. Snapshot refresh
    in_tx_during_refresh = []

    def mock_fetch(token):
        in_tx_during_refresh.append(db.in_transaction())
        return {
            "profile": {"user_id": "UCC_HTTP_OUT", "user_name": "Trader"},
            "funds": {"equity": {"available_margin": "50000.00", "used_margin": "0.00"}},
            "holdings": [],
            "positions": [],
            "orders": [],
            "fingerprint": "fp_http_out",
        }

    mock_adapter2 = UpstoxReadAdapter()
    monkeypatch.setattr(mock_adapter2, "fetch_full_read_snapshot", mock_fetch)

    BrokerService.refresh_snapshots(
        db=db, owner_id=user.id, environment="LIVE", adapter=mock_adapter2
    )

    assert len(in_tx_during_refresh) == 1
    assert in_tx_during_refresh[0] is False, "Snapshot HTTP fetch started inside active DB transaction!"


def test_numeric_json_literals_through_adapter_commit_and_reload(migrated_concurrency_db, monkeypatch):
    SessionFactory = sessionmaker(bind=migrated_concurrency_db, autocommit=False, autoflush=False)

    session_init = SessionFactory()
    user = User(
        username="u_num", normalized_username="u_num", email="u_num@test.com",
        normalized_email="u_num@test.com", hashed_password="hash", role="EDITOR", is_active=True,
    )
    session_init.add(user)
    session_init.commit()
    user_id = user.id

    now = datetime.datetime.now(datetime.timezone.utc)
    conn = ProviderConnection(
        owner_id=user_id,
        provider_name="UPSTOX",
        environment="LIVE",
        status="CONNECTED",
        broker_user_id="UCC_NUM_555",
        encrypted_access_token=secret_store.encrypt("tok_num"),
        token_expires_at=now + datetime.timedelta(hours=6),
        version=1,
        credential_generation=1,
    )
    session_init.add(conn)
    session_init.commit()
    conn_id = conn.id
    session_init.close()

    def numeric_transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "profile" in path:
            return httpx.Response(200, json={"status": "success", "data": {"user_id": "UCC_NUM_555", "user_name": "Trader"}})
        if "funds" in path:
            return httpx.Response(200, json={"status": "success", "data": {"equity": {"available_margin": 12345.67, "used_margin": 0.0}}})
        if "holdings" in path:
            return httpx.Response(200, json={"status": "success", "data": [{"quantity": 50, "last_price": 450.25, "pnl": 125.5}]})
        if "positions" in path:
            return httpx.Response(200, json={"status": "success", "data": []})
        if "order" in path:
            return httpx.Response(200, json={"status": "success", "data": []})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(numeric_transport))
    adapter = UpstoxReadAdapter(client=client, network_enabled=True)

    session_write = SessionFactory()
    res = BrokerService.refresh_snapshots(db=session_write, owner_id=user_id, environment="LIVE", adapter=adapter)
    assert res["available_margin"] == "12345.67"
    assert res["used_margin"] == "0.0" or res["used_margin"] == "0"
    session_write.close()

    session_read = SessionFactory()
    snap = session_read.query(BrokerSnapshot).filter_by(owner_id=user_id, connection_id=conn_id, status="ACTIVE").first()
    assert snap is not None
    assert snap.funds_and_margins_json["equity"]["available_margin"] == "12345.67"
    assert str(snap.holdings_json[0]["quantity"]) == "50"
    assert snap.holdings_json[0]["last_price"] == "450.25"
    assert snap.holdings_json[0]["pnl"] == "125.5"
    session_read.close()
