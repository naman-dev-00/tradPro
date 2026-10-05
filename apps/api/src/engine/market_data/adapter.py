import json
import logging
import os
import time
import urllib.parse
from decimal import Decimal
from typing import Any, Dict, List, Optional
import httpx
from .contracts import (
    DEFAULT_UPSTOX_MARKET_DATA_BASE_URL,
    APPROVED_MARKET_DATA_HOSTS,
    TIMEFRAME_TO_UPSTOX_INTERVAL,
    MarketDataError,
    MarketDataDisabledError,
    MarketDataAuthenticationError,
    MarketDataRateLimitedError,
    MarketDataServiceUnavailableError,
    MarketDataValidationError,
    MarketDataForbiddenError,
)

logger = logging.getLogger("tradepro.market_data_adapter")

MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 Megabytes limit
MAX_RETRY_AFTER_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TRANSIENT_RETRIES = 2
MAX_CANDLE_COUNT = 5000


def evaluate_operator_authorization(
    configured_owner_id: Optional[str],
    requesting_user_id: Optional[str],
) -> tuple[bool, bool, str, str]:
    """
    Shared operator authorization evaluator for readiness and acquisition.
    Returns:
        (operator_configured: bool, is_authorized: bool, status: str, detail_message: str)
    """
    cfg = (configured_owner_id or "").strip()
    req = (requesting_user_id or "").strip()

    if not cfg:
        return (
            False,
            False,
            "OPERATOR_NOT_CONFIGURED",
            "Market data operator (UPSTOX_MARKET_DATA_OWNER_ID) is not configured on the server.",
        )

    if not req or req != cfg:
        return (
            True,
            False,
            "FORBIDDEN_OPERATOR",
            "Current user is not the configured market data operator.",
        )

    return (
        True,
        True,
        "CONFIGURED_AND_ENABLED",
        "Authorized operator.",
    )


def validate_provider_endpoint(
    base_url: Optional[str],
    allowed_hosts: Optional[List[str]] = None,
) -> tuple[bool, str, str]:
    """
    Pure validation of market data provider base URL shared between readiness and acquisition.
    Returns:
        (is_valid: bool, sanitized_url: str, error_reason: str)
    Guarantees:
        - Never throws unhandled ValueError on malformed ports or schemes.
        - Never reflects raw credentials, tokens, or URL userinfo in error messages.
        - Strictly requires HTTPS, port 443 (or None), and host in approved list.
        - Forbids query strings, fragments, and non-empty base paths.
    """
    if not base_url or not isinstance(base_url, str):
        return (False, DEFAULT_UPSTOX_MARKET_DATA_BASE_URL, "Provider base URL is missing or empty.")

    raw = base_url.strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
    except Exception:
        return (False, DEFAULT_UPSTOX_MARKET_DATA_BASE_URL, "Malformed provider URL cannot be parsed.")

    # Check for userinfo / credentials in netloc or parsed attributes
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        return (
            False,
            DEFAULT_UPSTOX_MARKET_DATA_BASE_URL,
            "Provider endpoint must not contain userinfo credentials in URL.",
        )

    # Validate scheme
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        return (
            False,
            DEFAULT_UPSTOX_MARKET_DATA_BASE_URL,
            "Provider endpoint scheme must strictly be 'https'.",
        )

    # Validate port safely against ValueError (e.g. :bad or non-integers)
    try:
        port = parsed.port
    except ValueError:
        return (
            False,
            DEFAULT_UPSTOX_MARKET_DATA_BASE_URL,
            "Provider endpoint port must be a valid integer.",
        )

    if port is not None and port != 443:
        return (
            False,
            DEFAULT_UPSTOX_MARKET_DATA_BASE_URL,
            "Provider endpoint port must be standard 443 or omitted.",
        )

    # Validate host
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return (
            False,
            DEFAULT_UPSTOX_MARKET_DATA_BASE_URL,
            "Provider endpoint hostname is missing.",
        )

    approved = [h.lower() for h in (allowed_hosts or APPROVED_MARKET_DATA_HOSTS)]
    if hostname not in approved:
        return (
            False,
            DEFAULT_UPSTOX_MARKET_DATA_BASE_URL,
            f"Provider host is not in approved hosts whitelist.",
        )

    # Validate query
    if parsed.query:
        return (
            False,
            DEFAULT_UPSTOX_MARKET_DATA_BASE_URL,
            "Provider endpoint must not contain query parameters.",
        )

    # Validate fragment
    if parsed.fragment:
        return (
            False,
            DEFAULT_UPSTOX_MARKET_DATA_BASE_URL,
            "Provider endpoint must not contain fragment components.",
        )

    # Validate path
    if parsed.path and parsed.path.rstrip("/") != "":
        return (
            False,
            DEFAULT_UPSTOX_MARKET_DATA_BASE_URL,
            "Provider endpoint base path must be empty or '/'.",
        )

    sanitized = f"https://{hostname}"
    return (True, sanitized, "")


class UpstoxMarketDataAdapter:
    """
    Read-only Upstox V3 Market Data Adapter.
    Enforces explicit network enablement, server-side owner scoping,
    HTTPS endpoint validation, bounded streaming reads, rate-limit backoff,
    and credential sanitization.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        access_token: Optional[str] = None,
        network_enabled: Optional[bool] = None,
        configured_owner_id: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        allowed_hosts: Optional[List[str]] = None,
    ):
        raw_base_url = (base_url or os.environ.get("UPSTOX_MARKET_DATA_BASE_URL") or DEFAULT_UPSTOX_MARKET_DATA_BASE_URL).rstrip("/")
        self.allowed_hosts = allowed_hosts or APPROVED_MARKET_DATA_HOSTS

        is_valid, sanitized_url, error_reason = validate_provider_endpoint(raw_base_url, self.allowed_hosts)
        if not is_valid:
            raise MarketDataValidationError(error_reason)

        self.base_url = sanitized_url
        self._token = access_token or os.environ.get("UPSTOX_MARKET_DATA_ACCESS_TOKEN", "").strip()

        if network_enabled is not None:
            self.network_enabled = network_enabled
        else:
            self.network_enabled = os.environ.get("UPSTOX_MARKET_DATA_ENABLED", "false").lower() in ("true", "1", "yes")

        self.configured_owner_id = configured_owner_id or os.environ.get("UPSTOX_MARKET_DATA_OWNER_ID", "").strip()
        self.transport = transport
        self.timeout = timeout

    def _validate_endpoint(self) -> None:
        """
        Validates provider endpoint strictly for production safety.
        """
        is_valid, sanitized_url, error_reason = validate_provider_endpoint(self.base_url, self.allowed_hosts)
        if not is_valid:
            raise MarketDataValidationError(error_reason)
        self.base_url = sanitized_url

    def verify_operator_access(self, requesting_user_id: str) -> None:
        """
        Enforces server-side credentials scoped to the explicitly configured owner.
        Fails closed if owner configuration is missing, blank, or mismatched.
        """
        op_configured, is_authorized, _, detail = evaluate_operator_authorization(
            self.configured_owner_id, requesting_user_id
        )
        if not is_authorized:
            logger.warning(
                "Market data access denied for requesting user %s: %s",
                requesting_user_id,
                detail,
            )
            raise MarketDataForbiddenError(detail)

    def _check_network_and_credentials(self) -> None:
        """
        Verifies network enablement and credential presence before any HTTP socket operation.
        """
        if not self.network_enabled:
            raise MarketDataDisabledError(
                "Market data external network calls are disabled by server policy. "
                "Set UPSTOX_MARKET_DATA_ENABLED=true to enable."
            )

        if not self._token:
            raise MarketDataAuthenticationError(
                "Market data access token (UPSTOX_MARKET_DATA_ACCESS_TOKEN) is not configured on the server."
            )

    def get_historical_candles(
        self,
        instrument_key: str,
        timeframe: str,
        from_date: str,
        to_date: str,
    ) -> List[Any]:
        """
        Submits GET /v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}.
        Strictly read-only GET.
        """
        self._check_network_and_credentials()

        if timeframe not in TIMEFRAME_TO_UPSTOX_INTERVAL:
            raise MarketDataValidationError(f"Unsupported timeframe '{timeframe}'")

        unit, interval = TIMEFRAME_TO_UPSTOX_INTERVAL[timeframe]
        encoded_key = urllib.parse.quote(instrument_key, safe="")

        endpoint = f"/v3/historical-candle/{encoded_key}/{unit}/{interval}/{to_date}/{from_date}"
        return self._execute_bounded_get(endpoint)

    def get_intraday_candles(
        self,
        instrument_key: str,
        timeframe: str,
    ) -> List[Any]:
        """
        Submits GET /v3/historical-candle/intraday/{instrument_key}/{unit}/{interval}.
        Strictly read-only GET.
        """
        self._check_network_and_credentials()

        if timeframe not in TIMEFRAME_TO_UPSTOX_INTERVAL:
            raise MarketDataValidationError(f"Unsupported timeframe '{timeframe}'")

        unit, interval = TIMEFRAME_TO_UPSTOX_INTERVAL[timeframe]
        encoded_key = urllib.parse.quote(instrument_key, safe="")

        endpoint = f"/v3/historical-candle/intraday/{encoded_key}/{unit}/{interval}"
        return self._execute_bounded_get(endpoint)

    def _execute_bounded_get(self, endpoint: str) -> List[Any]:
        """
        Executes bounded streaming GET with chunk-level size enforcement,
        immediate termination on boundary overrun, and credential redaction.
        """
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

        retries_remaining = MAX_TRANSIENT_RETRIES
        backoff_delay = 0.2

        while True:
            try:
                with httpx.Client(
                    base_url=self.base_url,
                    headers=headers,
                    transport=self.transport,
                    timeout=self.timeout,
                    follow_redirects=False,
                ) as client:
                    with client.stream("GET", endpoint) as response:
                        status_code = response.status_code
                        headers_dict = {k.lower(): v for k, v in response.headers.items()}

                        # Reject redirects upfront to prevent credential leakage
                        if response.is_redirect or 300 <= status_code < 400:
                            raise MarketDataValidationError(
                                f"Provider returned unexpected redirect (HTTP {status_code}). Automatic redirect following disabled for credential safety."
                            )

                        # Check Content-Length header upfront if present
                        content_length_header = headers_dict.get("content-length")
                        if content_length_header:
                            try:
                                if int(content_length_header) > MAX_RESPONSE_BYTES:
                                    raise MarketDataValidationError(
                                        f"Provider response Content-Length exceeds maximum limit ({content_length_header} > {MAX_RESPONSE_BYTES} bytes)"
                                    )
                            except ValueError:
                                pass

                        # Bounded streaming read in chunks; stop reading immediately if limit exceeded
                        chunks = []
                        total_bytes = 0
                        chunk_size = 65536
                        for chunk in response.iter_bytes(chunk_size=chunk_size):
                            total_bytes += len(chunk)
                            if total_bytes > MAX_RESPONSE_BYTES:
                                raise MarketDataValidationError(
                                    f"Provider response payload exceeded maximum limit of {MAX_RESPONSE_BYTES} bytes during streaming read"
                                )
                            chunks.append(chunk)

                        body_bytes = b"".join(chunks)

            except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as e:
                sanitized_err_type = type(e).__name__
                if retries_remaining > 0:
                    retries_remaining -= 1
                    logger.warning(
                        "Transient error '%s' connecting to market data provider; retrying in %.2fs...",
                        sanitized_err_type,
                        backoff_delay,
                    )
                    time.sleep(backoff_delay)
                    backoff_delay *= 2
                    continue
                logger.error("Market data transport error after retries: %s", sanitized_err_type)
                raise MarketDataServiceUnavailableError(
                    f"Market data transport error: {sanitized_err_type}. Service unavailable."
                ) from None

            if status_code >= 500 and retries_remaining > 0:
                retries_remaining -= 1
                logger.warning(
                    "Provider returned HTTP %d; retrying in %.2fs...",
                    status_code,
                    backoff_delay,
                )
                time.sleep(backoff_delay)
                backoff_delay *= 2
                continue

            return self._handle_response(status_code, headers_dict, body_bytes)

    def _handle_response(self, status_code: int, headers: Dict[str, str], body_bytes: bytes) -> List[Any]:
        """
        Parses provider response, producing safe, bounded public errors without
        reflecting raw provider tokens, error strings, or status fields.
        """
        # 1. HTTP 200 OK
        if status_code == 200:
            try:
                body_text = body_bytes.decode("utf-8")
                data = json.loads(body_text, parse_float=Decimal)
            except Exception:
                raise MarketDataValidationError("Provider returned HTTP 200 but body was not valid JSON.")

            if not isinstance(data, dict):
                raise MarketDataValidationError("Provider response JSON is not an object.")

            if data.get("status") == "success" and "data" in data and isinstance(data["data"], dict):
                candles = data["data"].get("candles")
                if isinstance(candles, list):
                    if len(candles) > MAX_CANDLE_COUNT:
                        raise MarketDataValidationError(
                            f"Provider returned candle count exceeding maximum limit of {MAX_CANDLE_COUNT} ({len(candles)})."
                        )
                    return candles
                raise MarketDataValidationError("Provider response missing 'candles' array in data object.")

            raise MarketDataValidationError("Provider response structure is unexpected or malformed.")

        # 2. HTTP 429 Rate Limiting
        if status_code == 429:
            retry_after = self._parse_retry_after(headers.get("retry-after"))
            raise MarketDataRateLimitedError(
                retry_after=retry_after,
                message="Provider rate limit reached.",
            )

        # 3. HTTP 401 / 403 Authentication
        if status_code in (401, 403):
            raise MarketDataAuthenticationError(
                "Provider rejected credentials or unauthorized access."
            )

        # 4. HTTP 400 / 404 / 422 Client Request Errors
        if 400 <= status_code < 500:
            raise MarketDataValidationError(
                f"Provider rejected request with HTTP {status_code}."
            )

        # 5. HTTP 5xx Server Errors
        raise MarketDataServiceUnavailableError(
            f"Provider service unavailable (HTTP {status_code})."
        )

    def _parse_retry_after(self, header_val: Optional[str]) -> int:
        if not header_val:
            return 5
        try:
            val = int(header_val.strip())
            return max(1, min(val, MAX_RETRY_AFTER_SECONDS))
        except (ValueError, TypeError):
            return 5
