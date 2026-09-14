import json
import pytest
import httpx

from src.engine.sandbox.upstox_adapter import (
    UpstoxSandboxAdapter,
    UpstoxPlaceResult,
    UpstoxCancelResult,
    UpstoxClientError,
    UpstoxRetryable429,
    UpstoxAmbiguousError,
)


def test_place_order_success_and_slice_false():
    captured_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v3/order/place"
        body = json.loads(request.read())
        # Assert slice=False is strictly enforced
        assert body["slice"] is False
        assert body["quantity"] == 50
        assert body["order_type"] == "LIMIT"
        assert body["transaction_type"] == "BUY"
        assert body["price"] == 21500.5
        assert body["instrument_token"] == "NSE_FO|12345"

        return httpx.Response(
            status_code=200,
            json={
                "status": "success",
                "data": {
                    "order_id": "240913000000999"
                }
            }
        )

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxSandboxAdapter(base_url="https://mock.upstox.test", transport=transport)

    payload = {
        "quantity": 50,
        "product": "D",
        "validity": "DAY",
        "price": 21500.5,
        "instrument_token": "NSE_FO|12345",
        "order_type": "LIMIT",
        "transaction_type": "BUY",
    }

    result = adapter.place_order(payload, token="test_tok")
    assert isinstance(result, UpstoxPlaceResult)
    assert result.provider_order_id == "240913000000999"
    assert result.status == "SUCCESS"
    assert len(captured_requests) == 1
    assert captured_requests[0].headers["authorization"] == "Bearer test_tok"


def test_place_order_429_with_json_raises_ambiguous():
    """HTTP 429 with JSON does not guarantee pre-order rejection; must raise UpstoxAmbiguousError."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=429,
            headers={"Retry-After": "15"},
            json={
                "status": "error",
                "errors": [
                    {
                        "errorCode": "RATE_LIMIT_EXCEEDED",
                        "message": "Too many requests. Please throttle submission rate."
                    }
                ]
            }
        )

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxSandboxAdapter(base_url="https://mock.upstox.test", transport=transport)

    payload = {
        "quantity": 50,
        "price": 21500.0,
        "instrument_token": "NSE_FO|999",
        "order_type": "LIMIT",
        "transaction_type": "BUY",
    }

    with pytest.raises(UpstoxAmbiguousError) as exc_info:
        adapter.place_order(payload, token="mock_token")

    assert "429" in str(exc_info.value)
    assert "Retry-After: 15s" in str(exc_info.value)


def test_place_order_429_with_malformed_json_raises_ambiguous():
    """HTTP 429 with malformed JSON must raise UpstoxAmbiguousError."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=429,
            headers={"Retry-After": "10"},
            content=b"{malformed_json: true,"
        )

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxSandboxAdapter(base_url="https://mock.upstox.test", transport=transport)

    payload = {
        "quantity": 50,
        "price": 21500.0,
        "instrument_token": "NSE_FO|999",
        "order_type": "LIMIT",
        "transaction_type": "BUY",
    }

    with pytest.raises(UpstoxAmbiguousError) as exc_info:
        adapter.place_order(payload, token="mock_token")

    assert "429" in str(exc_info.value)


def test_place_order_429_with_html_raises_ambiguous():
    """HTTP 429 with HTML gateway error must raise UpstoxAmbiguousError."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=429,
            text="<html>429 Gateway Rate Limit Exceeded</html>"
        )

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxSandboxAdapter(base_url="https://mock.upstox.test", transport=transport)

    payload = {
        "quantity": 50,
        "price": 21500.0,
        "instrument_token": "NSE_FO|999",
        "order_type": "LIMIT",
        "transaction_type": "BUY",
    }

    with pytest.raises(UpstoxAmbiguousError) as exc_info:
        adapter.place_order(payload, token="mock_token")

    assert "429" in str(exc_info.value)


def test_place_order_429_with_empty_response_raises_ambiguous():
    """HTTP 429 with empty body must raise UpstoxAmbiguousError."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=429, content=b"")

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxSandboxAdapter(base_url="https://mock.upstox.test", transport=transport)

    payload = {
        "quantity": 50,
        "price": 21500.0,
        "instrument_token": "NSE_FO|999",
        "order_type": "LIMIT",
        "transaction_type": "BUY",
    }

    with pytest.raises(UpstoxAmbiguousError) as exc_info:
        adapter.place_order(payload, token="mock_token")

    assert "429" in str(exc_info.value)


def test_place_order_client_error():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=400,
            json={
                "status": "error",
                "errors": [
                    {
                        "errorCode": "UDAPI100010",
                        "message": "Invalid instrument token"
                    }
                ]
            }
        )

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxSandboxAdapter(base_url="https://mock.upstox.test", transport=transport)

    payload = {
        "quantity": 25,
        "instrument_token": "INVALID",
        "order_type": "MARKET",
        "transaction_type": "SELL",
    }

    with pytest.raises(UpstoxClientError) as exc_info:
        adapter.place_order(payload, token="mock_token")

    assert exc_info.value.status_code == 400
    assert exc_info.value.error_code == "UDAPI100010"


def test_place_order_ambiguous_500():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=502,
            text="Bad Gateway from exchange gateway"
        )

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxSandboxAdapter(base_url="https://mock.upstox.test", transport=transport)

    payload = {
        "quantity": 25,
        "instrument_token": "NSE_FO|123",
        "order_type": "MARKET",
        "transaction_type": "BUY",
    }

    with pytest.raises(UpstoxAmbiguousError):
        adapter.place_order(payload, token="mock_token")


def test_place_order_network_timeout():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Connection timed out waiting for Upstox response")

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxSandboxAdapter(base_url="https://mock.upstox.test", transport=transport)

    payload = {
        "quantity": 25,
        "instrument_token": "NSE_FO|123",
        "order_type": "MARKET",
        "transaction_type": "BUY",
    }

    with pytest.raises(UpstoxAmbiguousError) as exc_info:
        adapter.place_order(payload, token="mock_token")

    assert "ReadTimeout" in str(exc_info.value)


def test_cancel_order_success():
    captured_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        assert request.method == "DELETE"
        assert request.url.path == "/v3/order/cancel"
        assert request.url.params["order_id"] == "240913000000999"

        return httpx.Response(
            status_code=200,
            json={
                "status": "success",
                "data": {
                    "order_id": "240913000000999"
                }
            }
        )

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxSandboxAdapter(base_url="https://mock.upstox.test", transport=transport)

    res = adapter.cancel_order("240913000000999", token="mock_token")
    assert isinstance(res, UpstoxCancelResult)
    assert res.cancelled is True
    assert res.provider_order_id == "240913000000999"


def test_cancel_order_timeout():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Timed out connecting to cancel endpoint")

    transport = httpx.MockTransport(mock_handler)
    adapter = UpstoxSandboxAdapter(base_url="https://mock.upstox.test", transport=transport)

    with pytest.raises(UpstoxAmbiguousError):
        adapter.cancel_order("240913000000999", token="mock_token")
