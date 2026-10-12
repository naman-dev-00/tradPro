"""Read-only Upstox Broker Adapter for account identity and portfolio snapshots.

Enforces:
- Disabled-by-default broker network access (BROKER_NETWORK_ENABLED must be explicitly true).
- Injected HTTP client does not automatically bypass BROKER_NETWORK_ENABLED.
- Strict host allowlisting (HTTPS only, https://api.upstox.com or https://api-sandbox.upstox.com).
- Canonical origin/method/path policy: base URLs with paths, query params, fragments, userinfo, or ports are rejected.
- Redirect refusal on every request, including custom clients.
- Bounded response size (streaming max 2MB), bounded timeouts (5s).
- Strict response envelope validation: read endpoints require status='success'.
- Exact, finite numeric validation for financial fields; empty lists and documented nulls preserved.
- Sensitive credential sanitization: provider errors never leak raw tokens, secrets, or codes.
- Zero order placement or cancellation capabilities.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import math
import os
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("tradepro.broker.upstox_read_adapter")

ALLOWED_HOSTS: Set[str] = {
    "api.upstox.com",
    "api-sandbox.upstox.com",
}

# Explicit approved method + path combinations
ALLOWED_ENDPOINTS: Set[Tuple[str, str]] = {
    ("POST", "/v2/login/authorization/token"),
    ("GET", "/v2/user/profile"),
    ("GET", "/v2/user/get-funds-and-margin"),
    ("GET", "/v2/portfolio/long-term-holdings"),
    ("GET", "/v2/portfolio/short-term-positions"),
    ("GET", "/v2/order/retrieve-all"),
}

MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB
DEFAULT_TIMEOUT_SECONDS = 5.0


class BrokerTransportError(Exception):
    """Raised when communication with Upstox fails."""
    def __init__(self, message: str, status_code: Optional[int] = None, error_code: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class BrokerAuthError(BrokerTransportError):
    """Raised on 401 Unauthorized or 403 Forbidden."""
    pass


class BrokerRateLimitError(BrokerTransportError):
    """Raised on 429 Too Many Requests."""
    pass


def normalize_provider_numbers(obj: Any) -> Any:
    """
    Recursively normalizes Decimals and numeric values in broker responses
    into finite, JSON-safe strings or primitive types. Rejects NaN / Inf.
    """
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise ValueError("Non-finite Decimal encountered in broker payload")
        return format(obj, "f")
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("Non-finite float encountered in broker payload")
        return str(Decimal(str(obj)))
    if isinstance(obj, dict):
        return {k: normalize_provider_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_provider_numbers(item) for item in obj]
    return obj


def _validate_numeric_field(field_name: str, val: Any, required: bool = False) -> None:
    """Validates that a numeric field is finite. If required=True, val must not be None."""
    if val is None:
        if required:
            raise BrokerTransportError(f"Missing required financial field: '{field_name}'.")
        return
    try:
        dec = Decimal(str(val))
        if not dec.is_finite():
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise BrokerTransportError(f"Field '{field_name}' contains invalid non-finite numeric value.")


class UpstoxReadAdapter:
    """Read-only adapter for Upstox V2/V3 APIs."""

    def __init__(
        self,
        base_url: str = "https://api.upstox.com",
        client: Optional[httpx.Client] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        network_enabled: Optional[bool] = None,
    ):
        self.base_url = self._validate_base_url(base_url)
        self._custom_client = client
        self.timeout = timeout
        self._network_enabled = network_enabled

    @staticmethod
    def _validate_base_url(url: str) -> str:
        """
        Validates base URL strictly against origin policy.
        Rejects base URLs containing unexpected paths, queries, fragments, userinfo, or ports.
        """
        if not url or not isinstance(url, str):
            raise ValueError("Base URL must be a non-empty string.")
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise ValueError(f"Insecure scheme '{parsed.scheme}'. Only HTTPS is permitted.")
        if parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError(f"Untrusted host '{parsed.hostname}'. Permitted hosts: {ALLOWED_HOSTS}")
        if parsed.port not in (None, 443):
            raise ValueError(f"Untrusted port '{parsed.port}'. Permitted port is standard 443 only.")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Base URL must not contain userinfo/credentials.")
        clean_path = parsed.path.rstrip("/")
        if clean_path != "":
            raise ValueError(f"Base URL must not contain custom paths: '{parsed.path}'")
        if parsed.query:
            raise ValueError("Base URL must not contain query strings.")
        if parsed.fragment:
            raise ValueError("Base URL must not contain fragments.")
        return f"https://{parsed.hostname}"

    def _is_network_enabled(self) -> bool:
        """
        Checks if outbound broker network communication is explicitly enabled.
        An injected HTTP client must NOT automatically bypass this check.
        """
        if self._network_enabled is not None:
            return self._network_enabled
        val = os.environ.get("BROKER_NETWORK_ENABLED", "").strip().lower()
        return val in ("1", "true", "yes", "enabled")

    def _get_client(self) -> httpx.Client:
        if self._custom_client is not None:
            return self._custom_client
        return httpx.Client(
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    def _execute_request(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        expect_read_envelope: bool = True,
    ) -> Dict[str, Any]:
        """
        Executes a bounded, sanitized HTTP request to an allowlisted Upstox endpoint.
        Enforces canonical URL policy, redirect refusal, size bounds, and error sanitization.
        """
        method_upper = method.upper()

        # Enforce method/path allowlist
        if (method_upper, path) not in ALLOWED_ENDPOINTS:
            raise BrokerTransportError(f"Endpoint '{method_upper} {path}' is not in approved broker allowlist.")

        canonical_url = f"{self.base_url}{path}"
        parsed_canonical = urlparse(canonical_url)
        if parsed_canonical.scheme != "https" or parsed_canonical.hostname not in ALLOWED_HOSTS:
            raise BrokerTransportError("Canonical URL policy violation.")

        if not self._is_network_enabled():
            raise BrokerTransportError(
                "Broker network communication is disabled by default. "
                "Set BROKER_NETWORK_ENABLED=true in environment to permit outbound broker queries."
            )

        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")

        client = self._get_client()
        should_close = self._custom_client is None

        try:
            # Stream response to strictly enforce payload size bounds and refuse redirects
            with client.stream(
                method=method_upper,
                url=canonical_url,
                headers=req_headers,
                params=params,
                data=data,
                follow_redirects=False,
            ) as response:
                # Enforce redirect refusal on each request, including with custom clients
                if getattr(response, "history", None):
                    raise BrokerTransportError("Unexpected redirect followed by client.")

                if 300 <= response.status_code < 400:
                    raise BrokerTransportError(
                        f"Unexpected redirect from broker endpoint: status {response.status_code}",
                        status_code=response.status_code,
                    )

                # Ensure final response URL matches canonical URL
                resp_clean_url = str(response.url).split("?")[0].split("#")[0]
                if resp_clean_url != canonical_url:
                    raise BrokerTransportError("Response URL does not match canonical allowlisted endpoint.")

                content_chunks = []
                total_bytes = 0
                for chunk in response.iter_bytes(chunk_size=16384):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_RESPONSE_BYTES:
                        raise BrokerTransportError(
                            f"Broker response payload exceeded maximum size limit ({MAX_RESPONSE_BYTES} bytes)"
                        )
                    content_chunks.append(chunk)

                raw_bytes = b"".join(content_chunks)

                if response.status_code in (401, 403):
                    raise BrokerAuthError(
                        "Broker authentication failed. Credentials or permissions are invalid.",
                        status_code=response.status_code,
                    )

                if response.status_code == 429:
                    raise BrokerRateLimitError(
                        "Broker read rate limit exceeded. Please retry later.",
                        status_code=429,
                    )

                if response.status_code >= 400:
                    raise BrokerTransportError(
                        f"Broker request failed with status {response.status_code}.",
                        status_code=response.status_code,
                    )

                try:
                    parsed_json = json.loads(raw_bytes.decode("utf-8"), parse_float=Decimal)
                except Exception:
                    raise BrokerTransportError("Malformed JSON response received from broker.") from None

                if not isinstance(parsed_json, dict):
                    raise BrokerTransportError("Broker response is not a valid JSON object.")

                # If read endpoint requires documented successful envelope
                if expect_read_envelope:
                    status_field = str(parsed_json.get("status", "")).lower()
                    if status_field != "success":
                        raise BrokerTransportError("Broker returned non-success status envelope.")
                    if "data" not in parsed_json:
                        raise BrokerTransportError("Broker response missing 'data' envelope.")

                return parsed_json

        except httpx.TimeoutException:
            raise BrokerTransportError("Broker connection timed out.") from None
        except httpx.RequestError:
            raise BrokerTransportError("Broker service unreachable or network error.") from None
        finally:
            if should_close:
                client.close()

    def exchange_code_for_token(
        self,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> Dict[str, Any]:
        """
        Exchanges single-use authorization code for daily access token.
        Supports documented flat format and status-wrapped format.
        """
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        res = self._execute_request(
            method="POST",
            path="/v2/login/authorization/token",
            headers=headers,
            data=data,
            expect_read_envelope=False,
        )

        # Handle wrapped format: {"status": "success", "data": {...}}
        if "data" in res and isinstance(res.get("data"), dict):
            status_field = str(res.get("status", "")).lower()
            if status_field != "success":
                raise BrokerTransportError("Broker rejected token exchange with non-success status.")
            payload = res["data"]
        else:
            payload = res
            status_field = str(payload.get("status", "")).lower()
            if status_field in ("error", "failed", "pending", "unknown"):
                raise BrokerTransportError("Broker rejected token exchange.")

        if not isinstance(payload, dict):
            raise BrokerTransportError("Token exchange returned invalid payload shape.")

        access_token = payload.get("access_token")
        user_id = payload.get("user_id")

        if not access_token or not isinstance(access_token, str) or not access_token.strip():
            raise BrokerTransportError("Token exchange returned missing or invalid access_token.")
        if not user_id or not isinstance(user_id, str) or not user_id.strip():
            raise BrokerTransportError("Token exchange returned missing or invalid user_id.")

        return payload

    def get_user_profile(self, access_token: str) -> Dict[str, Any]:
        """Retrieves user profile and authoritative UCC client ID."""
        headers = {"Authorization": f"Bearer {access_token}"}
        res = self._execute_request(
            method="GET",
            path="/v2/user/profile",
            headers=headers,
            expect_read_envelope=True,
        )
        data = res.get("data")
        if not isinstance(data, dict) or not data.get("user_id"):
            raise BrokerTransportError("User profile response missing authoritative user_id (UCC).")
        return data

    def get_funds_and_margin(self, access_token: str) -> Dict[str, Any]:
        """Retrieves equity and commodity available/used funds and margins."""
        headers = {"Authorization": f"Bearer {access_token}"}
        res = self._execute_request(
            method="GET",
            path="/v2/user/get-funds-and-margin",
            headers=headers,
            expect_read_envelope=True,
        )
        data = res.get("data")
        if not isinstance(data, dict):
            raise BrokerTransportError("Funds and margin response missing 'data' envelope.")

        equity = data.get("equity")
        if not isinstance(equity, dict):
            raise BrokerTransportError("Funds response missing required 'equity' margin structure.")

        # Validate required financial fields with finite, bounded numeric representations
        for field in ("available_margin", "used_margin"):
            val = equity.get(field)
            if val is None:
                raise BrokerTransportError(f"Missing required financial field: '{field}' in equity margin.")
            _validate_numeric_field(field, val, required=True)

        commodity = data.get("commodity")
        if commodity is not None and isinstance(commodity, dict):
            for field in ("available_margin", "used_margin"):
                val = commodity.get(field)
                _validate_numeric_field(field, val)

        return normalize_provider_numbers(data)

    def get_holdings(self, access_token: str) -> List[Dict[str, Any]]:
        """Retrieves demat holdings. Preserves legitimate empty lists."""
        headers = {"Authorization": f"Bearer {access_token}"}
        res = self._execute_request(
            method="GET",
            path="/v2/portfolio/long-term-holdings",
            headers=headers,
            expect_read_envelope=True,
        )
        data = res.get("data")
        if not isinstance(data, list):
            raise BrokerTransportError("Holdings response missing 'data' list envelope.")

        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                raise BrokerTransportError(f"Holdings item at index {idx} is malformed.")
            for field in ("quantity", "last_price", "pnl", "average_price"):
                if field in item:
                    _validate_numeric_field(field, item[field])

        return normalize_provider_numbers(data)

    def get_positions(self, access_token: str) -> List[Dict[str, Any]]:
        """Retrieves intraday and delivery positions. Preserves legitimate empty lists."""
        headers = {"Authorization": f"Bearer {access_token}"}
        res = self._execute_request(
            method="GET",
            path="/v2/portfolio/short-term-positions",
            headers=headers,
            expect_read_envelope=True,
        )
        data = res.get("data")
        if not isinstance(data, list):
            raise BrokerTransportError("Positions response missing 'data' list envelope.")

        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                raise BrokerTransportError(f"Positions item at index {idx} is malformed.")
            for field in ("quantity", "buy_price", "sell_price", "pnl"):
                if field in item:
                    _validate_numeric_field(field, item[field])

        return normalize_provider_numbers(data)

    def get_open_orders(self, access_token: str) -> List[Dict[str, Any]]:
        """Retrieves current day order book. Preserves legitimate empty lists."""
        headers = {"Authorization": f"Bearer {access_token}"}
        res = self._execute_request(
            method="GET",
            path="/v2/order/retrieve-all",
            headers=headers,
            expect_read_envelope=True,
        )
        data = res.get("data")
        if not isinstance(data, list):
            raise BrokerTransportError("Orders response missing 'data' list envelope.")

        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                raise BrokerTransportError(f"Orders item at index {idx} is malformed.")
            for field in ("quantity", "price", "trigger_price", "filled_quantity"):
                if field in item:
                    _validate_numeric_field(field, item[field])

        return normalize_provider_numbers(data)

    def fetch_full_read_snapshot(self, access_token: str) -> Dict[str, Any]:
        """
        Executes bounded read-only calls for profile, funds, holdings, positions, and orders.
        Calculates SHA-256 fingerprint over the combined canonical normalized representation.
        """
        profile = self.get_user_profile(access_token)
        funds = self.get_funds_and_margin(access_token)
        holdings = self.get_holdings(access_token)
        positions = self.get_positions(access_token)
        orders = self.get_open_orders(access_token)

        # Build canonical fingerprint source
        fingerprint_source = {
            "user_id": str(profile.get("user_id")),
            "funds": funds,
            "holdings": holdings,
            "positions": positions,
            "orders": orders,
        }

        canonical_json_str = json.dumps(
            fingerprint_source,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical_json_str.encode("utf-8")).hexdigest()

        return {
            "profile": profile,
            "funds": funds,
            "holdings": holdings,
            "positions": positions,
            "orders": orders,
            "fingerprint": fingerprint,
        }
