import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional
import httpx

logger = logging.getLogger("tradepro.upstox_adapter")

DEFAULT_UPSTOX_BASE_URL = "https://api-hft.upstox.com"
MAX_RETRY_AFTER_SECONDS = 30


class UpstoxAdapterError(Exception):
    """Base error for Upstox adapter."""
    pass


class UpstoxClientError(UpstoxAdapterError):
    """Unambiguous pre-creation client rejection (4xx except ambiguous 429)."""
    def __init__(self, status_code: int, error_code: str, message: str):
        super().__init__(f"Upstox client error {status_code} [{error_code}]: {message}")
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


class UpstoxRetryable429(UpstoxAdapterError):
    """Confirmed pre-order 429 rejection with bounded Retry-After."""
    def __init__(self, retry_after: int, error_code: str, message: str):
        super().__init__(f"Upstox 429 rate limited [{error_code}]. Retry after {retry_after}s: {message}")
        self.retry_after = retry_after
        self.error_code = error_code
        self.message = message


class UpstoxAmbiguousError(UpstoxAdapterError):
    """Ambiguous outcome (5xx, timeout, unparseable 429/response) requiring reconciliation."""
    def __init__(self, message: str, original_exception: Optional[Exception] = None):
        super().__init__(message)
        self.original_exception = original_exception


@dataclass(frozen=True)
class UpstoxPlaceResult:
    provider_order_id: str
    status: str
    raw_response: Dict[str, Any]


@dataclass(frozen=True)
class UpstoxCancelResult:
    provider_order_id: str
    cancelled: bool
    raw_response: Dict[str, Any]


class UpstoxSandboxAdapter:
    """
    Upstox Sandbox Order Execution Adapter.
    Strictly implements Place Order V3 and Cancel Order V3.
    Enforces slice=false, conservative 429 handling, and fail-safe ambiguity classification.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 10.0,
    ):
        self.base_url = (base_url or os.environ.get("UPSTOX_SANDBOX_BASE_URL") or DEFAULT_UPSTOX_BASE_URL).rstrip("/")
        self.transport = transport
        self.timeout = timeout

    def _get_client(self, token: str) -> httpx.Client:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        return httpx.Client(
            base_url=self.base_url,
            headers=headers,
            transport=self.transport,
            timeout=self.timeout,
        )

    def place_order(self, payload: Dict[str, Any], token: str) -> UpstoxPlaceResult:
        """
        Submits POST /v3/order/place.
        Enforces slice=False.
        """
        request_body = {
            "quantity": payload["quantity"],
            "product": payload.get("product", "D"),
            "validity": payload.get("validity", "DAY"),
            "price": float(payload.get("price", 0.0)),
            "tag": payload.get("tag", "TradePro"),
            "instrument_token": payload["instrument_token"],
            "order_type": payload["order_type"],
            "transaction_type": payload["transaction_type"],
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
            "slice": False,  # Mandatory: auto-slicing disabled
        }

        try:
            with self._get_client(token) as client:
                response = client.post("/v3/order/place", json=request_body)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as e:
            logger.error("Network error during Upstox place order: %s", type(e).__name__)
            raise UpstoxAmbiguousError(
                f"Network error during place order: {type(e).__name__}. Order submission outcome ambiguous.",
                original_exception=e,
            ) from e

        return self._handle_place_response(response)

    def cancel_order(self, provider_order_id: str, token: str) -> UpstoxCancelResult:
        """
        Submits DELETE /v3/order/cancel?order_id={provider_order_id}.
        """
        try:
            with self._get_client(token) as client:
                response = client.delete(
                    "/v3/order/cancel",
                    params={"order_id": provider_order_id},
                )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as e:
            logger.error("Network error during Upstox cancel order: %s", type(e).__name__)
            raise UpstoxAmbiguousError(
                f"Network error during cancel order: {type(e).__name__}. Cancellation outcome ambiguous.",
                original_exception=e,
            ) from e

        return self._handle_cancel_response(response, provider_order_id)

    def _handle_place_response(self, response: httpx.Response) -> UpstoxPlaceResult:
        status_code = response.status_code

        # 1. Successful response
        if status_code == 200:
            try:
                data = response.json()
            except Exception as e:
                raise UpstoxAmbiguousError("HTTP 200 received but response body was not valid JSON", original_exception=e) from e

            if data.get("status") == "success" and "data" in data and "order_id" in data["data"]:
                order_id = str(data["data"]["order_id"])
                return UpstoxPlaceResult(
                    provider_order_id=order_id,
                    status="SUCCESS",
                    raw_response={"status": "success", "order_id": order_id},
                )
            raise UpstoxAmbiguousError(f"HTTP 200 received but unexpected response format: {data}")

        # 2. Conservative 429 Rate Limiting
        if status_code == 429:
            self._handle_conservative_429(response)

        # 3. Client Rejections (400, 401, 403, 422)
        if 400 <= status_code < 500:
            error_code, message = self._parse_error_body(response)
            raise UpstoxClientError(status_code=status_code, error_code=error_code, message=message)

        # 4. Server Errors (5xx)
        error_code, message = self._parse_error_body(response)
        raise UpstoxAmbiguousError(f"Upstox server error HTTP {status_code} [{error_code}]: {message}")

    def _handle_cancel_response(self, response: httpx.Response, expected_order_id: str) -> UpstoxCancelResult:
        status_code = response.status_code

        # 1. Successful cancellation confirmation
        if status_code == 200:
            try:
                data = response.json()
            except Exception as e:
                raise UpstoxAmbiguousError("HTTP 200 received on cancel but body was not valid JSON", original_exception=e) from e

            if data.get("status") == "success" and "data" in data and "order_id" in data["data"]:
                order_id = str(data["data"]["order_id"])
                return UpstoxCancelResult(
                    provider_order_id=order_id,
                    cancelled=True,
                    raw_response={"status": "success", "order_id": order_id},
                )
            raise UpstoxAmbiguousError(f"HTTP 200 received on cancel but response format ambiguous: {data}")

        # 2. Conservative 429
        if status_code == 429:
            self._handle_conservative_429(response)

        # 3. Client error on cancel (e.g. 400 order already completed)
        if 400 <= status_code < 500:
            error_code, message = self._parse_error_body(response)
            raise UpstoxClientError(status_code=status_code, error_code=error_code, message=message)

        # 4. 5xx on cancel
        error_code, message = self._parse_error_body(response)
        raise UpstoxAmbiguousError(f"Ambiguous 5xx response on cancellation: HTTP {status_code} [{error_code}]: {message}")

    def _handle_conservative_429(self, response: httpx.Response) -> None:
        """
        Conservative 429 handling for Part 1:
        Under the currently documented Place Order V3 and Cancel Order V3 capability set,
        HTTP 429 does not prove pre-order rejection. Retry-After alone does not prove
        the original order was not accepted.
        Therefore, HTTP 429 must not be blindly retransmitted. It fails closed by
        raising UpstoxAmbiguousError, transitioning the operation to RECONCILIATION_REQUIRED.
        """
        retry_after = self._parse_bounded_retry_after(response.headers.get("Retry-After"))
        error_code, message = self._parse_error_body(response)
        raise UpstoxAmbiguousError(
            f"HTTP 429 received from Upstox [{error_code}]: {message}. "
            f"Rate limiting does not rule out order acceptance; outcome is ambiguous (Retry-After: {retry_after}s)."
        )

    def _parse_bounded_retry_after(self, header_val: Optional[str]) -> int:
        if not header_val:
            return 5
        try:
            val = int(header_val.strip())
            return max(1, min(val, MAX_RETRY_AFTER_SECONDS))
        except (ValueError, TypeError):
            return 5

    def _parse_error_body(self, response: httpx.Response) -> tuple[str, str]:
        try:
            body = response.json()
            if isinstance(body, dict):
                if "errors" in body and isinstance(body["errors"], list) and len(body["errors"]) > 0:
                    err = body["errors"][0]
                    return str(err.get("errorCode", "UNKNOWN_ERROR")), str(err.get("message", response.text[:200]))
                if "message" in body:
                    return str(body.get("errorCode", "ERROR")), str(body["message"])
        except Exception:
            pass
        return f"HTTP_{response.status_code}", response.text[:200]
