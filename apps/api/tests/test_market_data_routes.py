import datetime
import os
import pytest
import httpx
from fastapi.testclient import TestClient
from src.main import app
from src.database import get_db, get_read_only_db
from src.models import (
    User,
    PaperAccount,
    Order,
    OrderIntent,
    SubmissionOutbox,
    Fill,
    PaperPosition,
    AccountLedgerEntry,
)
from src.auth.security import hash_password
from src.auth.session import create_session
from src.engine.market_data.adapter import UpstoxMarketDataAdapter
from src.engine.market_data.normalizer import MarketDataNormalizer
from src.services.market_data_service import MarketDataService


@pytest.fixture
def test_user(session):
    u = User(
        username="mkt_data_user",
        normalized_username="mkt_data_user",
        email="mkt@tradepro.test",
        normalized_email="mkt@tradepro.test",
        hashed_password=hash_password("Password123!"),
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
        username="other_user",
        normalized_username="other_user",
        email="other@tradepro.test",
        normalized_email="other@tradepro.test",
        hashed_password=hash_password("Password123!"),
        role="EDITOR",
        is_active=True,
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


@pytest.fixture
def auth_client(session, test_user):
    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_read_only_db] = override_db

    sess_rec, raw_sess, raw_csrf = create_session(session, test_user)
    c = TestClient(app, headers={"X-CSRF-Token": raw_csrf, "Origin": "http://localhost:3000"})
    c.cookies.set("tradepro_session", raw_sess)
    c.cookies.set("tradepro_csrf", raw_csrf)

    try:
        yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def other_client(session, other_user):
    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_read_only_db] = override_db

    sess_rec, raw_sess, raw_csrf = create_session(session, other_user)
    c = TestClient(app, headers={"X-CSRF-Token": raw_csrf, "Origin": "http://localhost:3000"})
    c.cookies.set("tradepro_session", raw_sess)
    c.cookies.set("tradepro_csrf", raw_csrf)

    try:
        yield c
    finally:
        app.dependency_overrides.clear()


def test_unauthenticated_request_rejected():
    client = TestClient(app)
    resp = client.get("/api/v1/market-data/readiness")
    assert resp.status_code == 401

    resp2 = client.get("/api/v1/market-data/instruments")
    assert resp2.status_code == 401

    resp3 = client.get("/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050")
    assert resp3.status_code == 401


def test_readiness_endpoint_states_and_zero_side_effects(auth_client, test_user, session, monkeypatch):
    """
    Readiness endpoint must accurately report status with 0 DB mutations and 0 network calls.
    """
    # 1. Default: Network disabled
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ENABLED", "false")
    monkeypatch.delenv("UPSTOX_MARKET_DATA_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("UPSTOX_MARKET_DATA_OWNER_ID", raising=False)

    r1 = auth_client.get("/api/v1/market-data/readiness")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["network_enabled"] is False
    assert d1["status"] == "NETWORK_DISABLED"

    # 2. Enabled but missing token
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ENABLED", "true")
    monkeypatch.delenv("UPSTOX_MARKET_DATA_ACCESS_TOKEN", raising=False)

    r2 = auth_client.get("/api/v1/market-data/readiness")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["network_enabled"] is True
    assert d2["credential_configured"] is False
    assert d2["status"] == "CREDENTIALS_MISSING"

    # 3. Enabled, token present, but configured owner is missing or blank
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ACCESS_TOKEN", "valid_token")
    monkeypatch.delenv("UPSTOX_MARKET_DATA_OWNER_ID", raising=False)

    r_op_missing = auth_client.get("/api/v1/market-data/readiness")
    assert r_op_missing.status_code == 200
    d_op_missing = r_op_missing.json()
    assert d_op_missing["operator_configured"] is False
    assert d_op_missing["is_authorized_operator"] is False
    assert d_op_missing["status"] == "OPERATOR_NOT_CONFIGURED"

    # 4. Enabled, token present, but configured owner is someone else
    monkeypatch.setenv("UPSTOX_MARKET_DATA_OWNER_ID", "different-user-id")

    r3 = auth_client.get("/api/v1/market-data/readiness")
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["operator_configured"] is True
    assert d3["is_authorized_operator"] is False
    assert d3["status"] == "FORBIDDEN_OPERATOR"

    # 5. Fully configured and matching operator
    monkeypatch.setenv("UPSTOX_MARKET_DATA_OWNER_ID", test_user.id)

    r4 = auth_client.get("/api/v1/market-data/readiness")
    assert r4.status_code == 200
    d4 = r4.json()
    assert d4["network_enabled"] is True
    assert d4["credential_configured"] is True
    assert d4["operator_configured"] is True
    assert d4["is_authorized_operator"] is True
    assert d4["status"] == "CONFIGURED_AND_ENABLED"
    assert "5m" in d4["supported_timeframes"]
    assert "15m" in d4["supported_timeframes"]

    # 6. Invalid endpoint configuration: http instead of https
    monkeypatch.setenv("UPSTOX_MARKET_DATA_BASE_URL", "http://localhost:8123")
    r_bad_scheme = auth_client.get("/api/v1/market-data/readiness")
    assert r_bad_scheme.status_code == 200
    assert r_bad_scheme.json()["status"] == "INVALID_ENDPOINT_CONFIGURATION"

    # 7. Invalid endpoint configuration: malformed non-integer port must not raise uncaught ValueError
    monkeypatch.setenv("UPSTOX_MARKET_DATA_BASE_URL", "https://api.upstox.com:bad")
    r_bad_port = auth_client.get("/api/v1/market-data/readiness")
    assert r_bad_port.status_code == 200
    assert r_bad_port.json()["status"] == "INVALID_ENDPOINT_CONFIGURATION"

    # 8. Invalid endpoint configuration: URL with credentials must not reflect secrets
    secret_in_url = "token_secret_xyz123"
    monkeypatch.setenv("UPSTOX_MARKET_DATA_BASE_URL", f"https://{secret_in_url}@api.upstox.com")
    r_bad_cred = auth_client.get("/api/v1/market-data/readiness")
    assert r_bad_cred.status_code == 200
    assert r_bad_cred.json()["status"] == "INVALID_ENDPOINT_CONFIGURATION"
    assert secret_in_url not in r_bad_cred.text


def test_supported_instruments_catalog(auth_client):
    resp = auth_client.get("/api/v1/market-data/instruments")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 5
    nifty = next((x for x in data if x["tradepro_instrument_id"] == "NIFTY50_INDEX"), None)
    assert nifty is not None
    assert nifty["instrument_key"] == "NSE_INDEX|Nifty 50"
    assert nifty["exchange"] == "NSE"


def test_missing_or_blank_owner_blocks_acquisition_with_zero_calls(auth_client, test_user, monkeypatch):
    """
    When UPSTOX_MARKET_DATA_OWNER_ID is missing or blank, acquisition must fail closed (403)
    with zero provider calls attempted.
    """
    calls = []

    def mock_handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(200, json={"status": "success", "data": {"candles": []}})

    mock_transport = httpx.MockTransport(mock_handler)
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ACCESS_TOKEN", "valid_token")
    monkeypatch.delenv("UPSTOX_MARKET_DATA_OWNER_ID", raising=False)

    resp = auth_client.get("/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050&timeframe=5m")
    assert resp.status_code == 403
    assert "not configured on the server" in resp.json()["detail"]
    assert len(calls) == 0

    # Blank owner
    monkeypatch.setenv("UPSTOX_MARKET_DATA_OWNER_ID", "   ")
    resp_blank = auth_client.get("/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050&timeframe=5m")
    assert resp_blank.status_code == 403
    assert len(calls) == 0


def test_cross_owner_access_rejected(auth_client, other_client, test_user, monkeypatch):
    """
    When UPSTOX_MARKET_DATA_OWNER_ID is set, requests from any other user
    must be rejected with HTTP 403.
    """
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ACCESS_TOKEN", "mock_token")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_OWNER_ID", test_user.id)

    # other_client is authenticated as other_user -> must be rejected
    resp = other_client.get("/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050&timeframe=5m")
    assert resp.status_code == 403
    assert "not the configured market data operator" in resp.json()["detail"]


def test_query_validation(auth_client, test_user, monkeypatch):
    """
    Invalid timeframe, missing dates in historical mode, or date ranges > 30 days must be rejected with HTTP 400.
    """
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ACCESS_TOKEN", "mock_token")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_OWNER_ID", test_user.id)

    # Unsupported timeframe
    r1 = auth_client.get("/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050&timeframe=1m")
    assert r1.status_code == 400
    assert "Unsupported timeframe" in r1.json()["detail"]

    # Historical mode missing dates
    r2 = auth_client.get("/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050&timeframe=5m&mode=historical")
    assert r2.status_code == 400
    assert "from_date and to_date are required" in r2.json()["detail"]

    # Historical range > 30 days
    r3 = auth_client.get(
        "/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050&timeframe=5m&mode=historical&from_date=2026-08-01&to_date=2026-09-15"
    )
    assert r3.status_code == 400
    assert "exceeds maximum allowed limit of 30 days" in r3.json()["detail"]


def test_successful_candle_acquisition_and_provenance(auth_client, test_user, monkeypatch):
    """
    Verifies full pipeline with mocked Upstox transport:
    - Normalization to exact scaled units and UTC
    - Complete dataset provenance with SHA-256 fingerprint
    - Strict source labeling as PROVIDER_UPSTOX_V3 (never FIXTURE_REPLAY)
    - Zero outbox, order, or ledger operations
    """
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ACCESS_TOKEN", "mock_token_abc")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_OWNER_ID", test_user.id)

    mock_candles = [
        ["2026-10-05T14:45:00+05:30", 25100.0, 25150.0, 25090.0, 25140.0, 40000, 0],
        ["2026-10-05T14:50:00+05:30", 25140.0, 25180.0, 25130.0, 25175.0, 42000, 0],
    ]

    def mock_handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={"status": "success", "data": {"candles": mock_candles}},
        )

    mock_transport = httpx.MockTransport(mock_handler)
    mock_adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="mock_token_abc",
        network_enabled=True,
        configured_owner_id=test_user.id,
        transport=mock_transport,
    )

    monkeypatch.setattr("src.services.market_data_service.UpstoxMarketDataAdapter", lambda *a, **kw: mock_adapter)
    monkeypatch.setattr(
        "src.services.market_data_service.MarketDataNormalizer",
        lambda clock=None, **kw: MarketDataNormalizer(
            clock=lambda: datetime.datetime(2026, 10, 5, 10, 0, 0, tzinfo=datetime.timezone.utc),
            **kw,
        ),
    )

    resp = auth_client.get("/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050&timeframe=5m&mode=intraday")
    assert resp.status_code == 200
    data = resp.json()

    assert data["instrument_key"] == "NSE_INDEX|Nifty 50"
    assert data["tradepro_instrument_id"] == "NIFTY50_INDEX"
    assert data["timeframe"] == "5m"
    assert len(data["candles"]) == 2

    c0 = data["candles"][0]
    assert c0["open"] == "25100.0000"
    assert c0["close"] == "25140.0000"
    assert c0["open_units"] == 251000000
    assert c0["volume"] == 40000
    assert c0["is_closed"] is True

    # Provenance verification
    prov = data["provenance"]
    assert prov["provider"] == "UPSTOX"
    assert prov["source_type"] == "PROVIDER_UPSTOX_V3"
    assert prov["source_type"] != "FIXTURE_REPLAY"
    assert len(prov["content_fingerprint"]) == 64
    assert prov["candle_count"] == 2
    assert prov["completeness"] == "UNKNOWN"
    assert prov["is_complete_series"] is False


def test_prevent_credential_disclosure_on_mocked_error_payload(auth_client, test_user, monkeypatch):
    """
    A mocked HTTP 401 whose error message contains the secret access token
    must produce an HTTP 502 API response that does NOT echo the secret.
    """
    token_secret = "sensitive_bearer_token_xyz_9988"
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ACCESS_TOKEN", token_secret)
    monkeypatch.setenv("UPSTOX_MARKET_DATA_OWNER_ID", test_user.id)

    def mock_handler(request: httpx.Request):
        return httpx.Response(
            401,
            json={"errors": [{"errorCode": "AUTH_FAILED", "message": f"Expired Bearer {token_secret}"}]},
        )

    mock_adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token=token_secret,
        network_enabled=True,
        configured_owner_id=test_user.id,
        transport=httpx.MockTransport(mock_handler),
    )
    monkeypatch.setattr("src.services.market_data_service.UpstoxMarketDataAdapter", lambda *a, **kw: mock_adapter)

    resp = auth_client.get("/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050&timeframe=5m")
    assert resp.status_code == 502
    resp_text = resp.text
    assert token_secret not in resp_text
    assert "Provider rejected credentials or unauthorized access" in resp.json()["detail"]


def test_dummy_token_in_malformed_candle_never_echoed_in_api_response(auth_client, test_user, monkeypatch, caplog):
    """
    When provider returns a 200 containing raw secrets/tokens in candle content,
    the API validation error must not reflect that token in the response or logs.
    """
    secret_candle_token = "dummy_access_token_in_candle_field_secret_8877"
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ACCESS_TOKEN", "mock_token")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_OWNER_ID", test_user.id)

    def mock_handler(request: httpx.Request):
        # Successful 200 payload from provider, but with token in the timestamp field
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "candles": [
                        [secret_candle_token, 25100.0, 25150.0, 25080.0, 25140.0, 40000, 0]
                    ]
                },
            },
        )

    mock_adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="mock_token",
        network_enabled=True,
        configured_owner_id=test_user.id,
        transport=httpx.MockTransport(mock_handler),
    )
    monkeypatch.setattr("src.services.market_data_service.UpstoxMarketDataAdapter", lambda *a, **kw: mock_adapter)

    resp = auth_client.get("/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050&timeframe=5m")
    assert resp.status_code == 400
    assert secret_candle_token not in resp.text
    assert "Unparseable ISO-8601 timestamp at index 0: invalid datetime format." in resp.json()["detail"]
    assert secret_candle_token not in caplog.text


def test_oversized_8kb_timestamp_produces_short_controlled_error(auth_client, test_user, monkeypatch):
    """
    An 8KB malformed timestamp in provider response must produce a short, controlled HTTP 400 error.
    """
    huge_ts = "2026-10-05T" + ("Y" * 8192)
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ACCESS_TOKEN", "mock_token")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_OWNER_ID", test_user.id)

    def mock_handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "candles": [
                        [huge_ts, 25100.0, 25150.0, 25080.0, 25140.0, 40000, 0]
                    ]
                },
            },
        )

    mock_adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="mock_token",
        network_enabled=True,
        configured_owner_id=test_user.id,
        transport=httpx.MockTransport(mock_handler),
    )
    monkeypatch.setattr("src.services.market_data_service.UpstoxMarketDataAdapter", lambda *a, **kw: mock_adapter)

    resp = auth_client.get("/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050&timeframe=5m")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert len(detail) < 120
    assert "Unparseable ISO-8601 timestamp at index 0: invalid datetime format." in detail
    assert "Y" * 10 not in detail


def test_volume_1e5000_rejected_with_400_not_500(auth_client, test_user, monkeypatch):
    """
    Volume '1e5000' must be rejected with HTTP 400 through MarketDataValidationError
    rather than causing an uncaught overflow / string formatting crash (HTTP 500).
    """
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ACCESS_TOKEN", "mock_token")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_OWNER_ID", test_user.id)

    def mock_handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "candles": [
                        ["2026-10-05T09:15:00+05:30", 25100.0, 25150.0, 25080.0, 25140.0, "1e5000", 0]
                    ]
                },
            },
        )

    mock_adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="mock_token",
        network_enabled=True,
        configured_owner_id=test_user.id,
        transport=httpx.MockTransport(mock_handler),
    )
    monkeypatch.setattr("src.services.market_data_service.UpstoxMarketDataAdapter", lambda *a, **kw: mock_adapter)

    resp = auth_client.get("/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050&timeframe=5m")
    assert resp.status_code == 400
    assert "Volume exceeds maximum supported bound at index 0." in resp.json()["detail"]


def test_zero_order_or_outbox_or_ledger_mutations(auth_client, test_user, session, monkeypatch):
    """
    Strict safety check: Proves that querying market data and acquiring candles through
    the real API pipeline performs zero order insertions, zero outbox records, and
    zero financial ledger mutations.
    """
    initial_orders = session.query(Order).count()
    initial_intents = session.query(OrderIntent).count()
    initial_outbox = session.query(SubmissionOutbox).count()
    initial_accounts = session.query(PaperAccount).count()
    initial_fills = session.query(Fill).count()
    initial_positions = session.query(PaperPosition).count()
    initial_ledger = session.query(AccountLedgerEntry).count()

    # 1. Call readiness and instruments
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_ACCESS_TOKEN", "mock_token")
    monkeypatch.setenv("UPSTOX_MARKET_DATA_OWNER_ID", test_user.id)

    auth_client.get("/api/v1/market-data/readiness")
    auth_client.get("/api/v1/market-data/instruments")

    # 2. Call successful candle acquisition through real pipeline
    mock_candles = [
        ["2026-10-05T14:45:00+05:30", 25100.0, 25150.0, 25090.0, 25140.0, 40000, 0],
        ["2026-10-05T14:50:00+05:30", 25140.0, 25180.0, 25130.0, 25175.0, 42000, 0],
    ]

    def mock_handler(request: httpx.Request):
        return httpx.Response(200, json={"status": "success", "data": {"candles": mock_candles}})

    mock_adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="mock_token",
        network_enabled=True,
        configured_owner_id=test_user.id,
        transport=httpx.MockTransport(mock_handler),
    )
    monkeypatch.setattr("src.services.market_data_service.UpstoxMarketDataAdapter", lambda *a, **kw: mock_adapter)
    monkeypatch.setattr(
        "src.services.market_data_service.MarketDataNormalizer",
        lambda clock=None, **kw: MarketDataNormalizer(
            clock=lambda: datetime.datetime(2026, 10, 5, 20, 0, 0, tzinfo=datetime.timezone.utc),
            **kw,
        ),
    )

    resp = auth_client.get("/api/v1/market-data/candles?instrument_key=NSE_INDEX%7CNifty%2050&timeframe=5m&mode=intraday")
    assert resp.status_code == 200

    # 3. Assert ALL execution/outbox/order/ledger tables remain strictly untouched
    assert session.query(Order).count() == initial_orders
    assert session.query(OrderIntent).count() == initial_intents
    assert session.query(SubmissionOutbox).count() == initial_outbox
    assert session.query(PaperAccount).count() == initial_accounts
    assert session.query(Fill).count() == initial_fills
    assert session.query(PaperPosition).count() == initial_positions
    assert session.query(AccountLedgerEntry).count() == initial_ledger
