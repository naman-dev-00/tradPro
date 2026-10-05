import os
import pytest
import httpx
from src.engine.market_data.adapter import (
    UpstoxMarketDataAdapter,
    MAX_RESPONSE_BYTES,
    evaluate_operator_authorization,
)
from src.engine.market_data.contracts import (
    MarketDataDisabledError,
    MarketDataAuthenticationError,
    MarketDataRateLimitedError,
    MarketDataServiceUnavailableError,
    MarketDataValidationError,
    MarketDataForbiddenError,
)


def test_disabled_networking_makes_zero_provider_calls():
    """
    When UPSTOX_MARKET_DATA_ENABLED is false (or default),
    adapter must raise MarketDataDisabledError and make zero HTTP calls.
    """
    calls = []

    def mock_handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(200, json={"status": "success", "data": {"candles": []}})

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="test_token_123",
        network_enabled=False,
        transport=transport,
    )

    with pytest.raises(MarketDataDisabledError) as exc_info:
        adapter.get_intraday_candles("NSE_INDEX|Nifty 50", "5m")

    assert "disabled by server policy" in str(exc_info.value)
    assert len(calls) == 0

    with pytest.raises(MarketDataDisabledError):
        adapter.get_historical_candles("NSE_INDEX|Nifty 50", "5m", "2026-10-01", "2026-10-05")

    assert len(calls) == 0


def test_missing_token_makes_zero_calls():
    """
    When network is enabled but access token is missing,
    adapter must raise MarketDataAuthenticationError and make zero HTTP calls.
    """
    calls = []

    def mock_handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(200, json={"status": "success", "data": {"candles": []}})

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="",
        network_enabled=True,
        transport=transport,
    )

    with pytest.raises(MarketDataAuthenticationError) as exc_info:
        adapter.get_intraday_candles("NSE_INDEX|Nifty 50", "5m")

    assert "not configured on the server" in str(exc_info.value)
    assert len(calls) == 0


def test_endpoint_validation_strictly_enforces_approved_https_destination():
    """
    Production acquisition must strictly use approved HTTPS endpoint with no
    userinfo, no query parameters, standard port, and empty base path.
    """
    # 1. Scheme other than https
    with pytest.raises(MarketDataValidationError) as exc:
        UpstoxMarketDataAdapter(base_url="http://api.upstox.com", access_token="t")
    assert "scheme must strictly be 'https'" in str(exc.value)

    # 2. Userinfo credentials in URL
    with pytest.raises(MarketDataValidationError) as exc:
        UpstoxMarketDataAdapter(base_url="https://user:secret@api.upstox.com", access_token="t")
    assert "must not contain userinfo" in str(exc.value)

    # 3. Unapproved host (including localhost/127.0.0.1 in production allowlist)
    with pytest.raises(MarketDataValidationError) as exc:
        UpstoxMarketDataAdapter(base_url="https://localhost", access_token="t")
    assert "not in approved hosts whitelist" in str(exc.value)

    # 4. Port specified and not 443
    with pytest.raises(MarketDataValidationError) as exc:
        UpstoxMarketDataAdapter(base_url="https://api.upstox.com:8443", access_token="t")
    assert "port must be standard 443" in str(exc.value)

    # 4b. Malformed port (e.g. :bad) must raise MarketDataValidationError, not uncaught ValueError
    with pytest.raises(MarketDataValidationError) as exc:
        UpstoxMarketDataAdapter(base_url="https://api.upstox.com:bad", access_token="t")
    assert "port must be a valid integer" in str(exc.value)

    # 5. Query parameters rejected
    with pytest.raises(MarketDataValidationError) as exc:
        UpstoxMarketDataAdapter(base_url="https://api.upstox.com?token=xyz", access_token="t")
    assert "must not contain query parameters" in str(exc.value)

    # 6. Fragment rejected
    with pytest.raises(MarketDataValidationError) as exc:
        UpstoxMarketDataAdapter(base_url="https://api.upstox.com#hash", access_token="t")
    assert "must not contain fragment" in str(exc.value)

    # 7. Unexpected base path rejected
    with pytest.raises(MarketDataValidationError) as exc:
        UpstoxMarketDataAdapter(base_url="https://api.upstox.com/unapproved-path", access_token="t")
    assert "base path must be empty or '/'" in str(exc.value)

    # 8. Test validate_provider_endpoint directly
    from src.engine.market_data.adapter import validate_provider_endpoint
    ok, sanitized, reason = validate_provider_endpoint("http://localhost:8123")
    assert not ok and "scheme must strictly be 'https'" in reason

    ok, sanitized, reason = validate_provider_endpoint("https://api.upstox.com:bad")
    assert not ok and "port must be a valid integer" in reason

    ok, sanitized, reason = validate_provider_endpoint("https://secret_token_12345@api.upstox.com")
    assert not ok and "userinfo credentials" in reason
    assert "secret_token_12345" not in reason



def test_operator_configuration_fails_closed_when_absent_or_mismatched():
    """
    Missing or blank configured_owner_id must fail closed with MarketDataForbiddenError.
    Shared evaluate_operator_authorization must report OPERATOR_NOT_CONFIGURED.
    """
    # Helper checks
    op_cfg, is_auth, status, msg = evaluate_operator_authorization(None, "user-1")
    assert not op_cfg and not is_auth and status == "OPERATOR_NOT_CONFIGURED"

    op_cfg, is_auth, status, msg = evaluate_operator_authorization("   ", "user-1")
    assert not op_cfg and not is_auth and status == "OPERATOR_NOT_CONFIGURED"

    op_cfg, is_auth, status, msg = evaluate_operator_authorization("owner-1", "user-2")
    assert op_cfg and not is_auth and status == "FORBIDDEN_OPERATOR"

    op_cfg, is_auth, status, msg = evaluate_operator_authorization("owner-1", "owner-1")
    assert op_cfg and is_auth and status == "CONFIGURED_AND_ENABLED"

    # Adapter verify_operator_access checks
    adapter_no_owner = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="test_token",
        network_enabled=True,
        configured_owner_id="",
    )
    with pytest.raises(MarketDataForbiddenError) as exc_info:
        adapter_no_owner.verify_operator_access("any-user")
    assert "not configured on the server" in str(exc_info.value)

    adapter_with_owner = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="test_token",
        network_enabled=True,
        configured_owner_id="authorized-owner-uuid",
    )
    adapter_with_owner.verify_operator_access("authorized-owner-uuid")

    with pytest.raises(MarketDataForbiddenError) as exc_info:
        adapter_with_owner.verify_operator_access("unauthorized-user-uuid")
    assert "not the configured market data operator" in str(exc_info.value)


def test_successful_v3_request_paths_and_auth_header():
    """
    Verifies that requests use exact V3 endpoint paths, URL-encode instrument keys,
    and include Authorization: Bearer token header.
    """
    calls = []

    def mock_handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "candles": [
                        ["2026-10-05T15:15:00+05:30", 25200.0, 25250.0, 25190.0, 25240.0, 50000, 0]
                    ]
                },
            },
        )

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="secret_upstox_token_xyz",
        network_enabled=True,
        transport=transport,
    )

    # 1. Intraday 5m
    candles_intra = adapter.get_intraday_candles("NSE_INDEX|Nifty 50", "5m")
    assert len(candles_intra) == 1
    req1 = calls[0]
    assert req1.method == "GET"
    assert req1.url.raw_path.decode("ascii") == "/v3/historical-candle/intraday/NSE_INDEX%7CNifty%2050/minutes/5"
    assert req1.headers["Authorization"] == "Bearer secret_upstox_token_xyz"

    # 2. Historical 15m
    candles_hist = adapter.get_historical_candles("NSE_EQ|INE002A01018", "15m", "2026-10-01", "2026-10-05")
    assert len(candles_hist) == 1
    req2 = calls[1]
    assert req2.method == "GET"
    assert req2.url.raw_path.decode("ascii") == "/v3/historical-candle/NSE_EQ%7CINE002A01018/minutes/15/2026-10-05/2026-10-01"


def test_rate_limiting_429_parses_retry_after():
    """
    HTTP 429 returns MarketDataRateLimitedError with bounded retry_after.
    """
    def mock_handler(request: httpx.Request):
        return httpx.Response(
            429,
            headers={"Retry-After": "15"},
            json={"errors": [{"errorCode": "UDAPI100050", "message": "Rate limit exceeded"}]},
        )

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="test_token",
        network_enabled=True,
        transport=transport,
    )

    with pytest.raises(MarketDataRateLimitedError) as exc_info:
        adapter.get_intraday_candles("NSE_INDEX|Nifty 50", "5m")

    assert exc_info.value.retry_after == 15
    assert "Retry after 15s" in str(exc_info.value)


def test_credential_sanitization_on_echoed_secrets():
    """
    Ensures that when a provider echoes the bearer token in an error message,
    errorCode, status, or malformed body, the token is NEVER exposed to caller.
    """
    token = "secret_raw_bearer_token_super_private_98765"

    def mock_handler_401(request: httpx.Request):
        return httpx.Response(
            401,
            json={"errors": [{"errorCode": "AUTH_FAIL", "message": f"Invalid token Bearer {token}"}]},
        )

    transport = httpx.MockTransport(mock_handler_401)
    adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token=token,
        network_enabled=True,
        transport=transport,
    )

    with pytest.raises(MarketDataAuthenticationError) as exc_info:
        adapter.get_intraday_candles("NSE_INDEX|Nifty 50", "5m")

    err_msg = str(exc_info.value)
    assert token not in err_msg
    assert "Provider rejected credentials" in err_msg

    # Also test unexpected status field with echoed secret
    def mock_handler_status(request: httpx.Request):
        return httpx.Response(200, json={"status": f"token={token}", "data": {}})

    adapter2 = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token=token,
        network_enabled=True,
        transport=httpx.MockTransport(mock_handler_status),
    )
    with pytest.raises(MarketDataValidationError) as exc_info2:
        adapter2.get_intraday_candles("NSE_INDEX|Nifty 50", "5m")

    assert token not in str(exc_info2.value)
    assert "unexpected or malformed" in str(exc_info2.value)


def test_bounded_streaming_read_stops_early_without_consuming_excess_chunks():
    """
    Proves that streaming reads enforce MAX_RESPONSE_BYTES chunk-by-chunk and stop
    reading immediately without consuming trailing chunks.
    """
    chunks_yielded = []

    def chunk_generator():
        # Yield 8 chunks of 1 MiB each (total 8 MiB)
        for i in range(8):
            chunks_yielded.append(i)
            yield b"X" * (1024 * 1024)

    def mock_handler(request: httpx.Request):
        return httpx.Response(200, content=chunk_generator())

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="test_token",
        network_enabled=True,
        transport=transport,
    )

    with pytest.raises(MarketDataValidationError) as exc_info:
        adapter.get_intraday_candles("NSE_INDEX|Nifty 50", "5m")

    assert "exceeded maximum limit of 5242880 bytes during streaming read" in str(exc_info.value)
    # 5 MiB limit: Should have consumed at most 6 chunks (0, 1, 2, 3, 4, 5) and stopped!
    # Chunks 6 and 7 must NEVER have been yielded!
    assert len(chunks_yielded) <= 6
    assert 6 not in chunks_yielded
    assert 7 not in chunks_yielded


def test_content_length_header_fails_fast_before_reading():
    """
    If Content-Length header exceeds limit, fails immediately.
    """
    def mock_handler(request: httpx.Request):
        return httpx.Response(200, headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1000)}, content=b"")

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="test_token",
        network_enabled=True,
        transport=transport,
    )

    with pytest.raises(MarketDataValidationError) as exc:
        adapter.get_intraday_candles("NSE_INDEX|Nifty 50", "5m")

    assert "Content-Length exceeds maximum limit" in str(exc.value)


def test_transient_5xx_retries_and_bounded_failure():
    """
    5xx server errors retry up to MAX_TRANSIENT_RETRIES and then fail with MarketDataServiceUnavailableError.
    """
    calls = []

    def mock_handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(502, json={"message": "Upstream Gateway Error"})

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="test_token",
        network_enabled=True,
        transport=transport,
    )

    with pytest.raises(MarketDataServiceUnavailableError) as exc_info:
        adapter.get_intraday_candles("NSE_INDEX|Nifty 50", "5m")

    # 1 initial attempt + 2 retries = 3 calls total
    assert len(calls) == 3
    assert "service unavailable (HTTP 502)" in str(exc_info.value)


def test_unexpected_3xx_redirect_fails_fast_to_prevent_token_leakage():
    """
    3xx redirects must be rejected immediately to prevent leaking Bearer tokens to untrusted hosts.
    """
    def mock_handler(request: httpx.Request):
        return httpx.Response(302, headers={"Location": "https://untrusted-host.com/evil"})

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxMarketDataAdapter(
        base_url="https://api.upstox.com",
        access_token="test_secret_token",
        network_enabled=True,
        transport=transport,
    )

    with pytest.raises(MarketDataValidationError) as exc_info:
        adapter.get_intraday_candles("NSE_INDEX|Nifty 50", "5m")

    assert "unexpected redirect (HTTP 302)" in str(exc_info.value)
    assert "Automatic redirect following disabled" in str(exc_info.value)
