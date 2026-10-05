import datetime
from decimal import Decimal
from typing import Dict, List, Literal, Optional, Tuple, Any
from pydantic import BaseModel, Field, ConfigDict

# Supported timeframes for Phase 5
SUPPORTED_MARKET_DATA_TIMEFRAMES = ["5m", "15m"]

# Map internal timeframe strings to Upstox V3 path components: (unit, interval_int)
TIMEFRAME_TO_UPSTOX_INTERVAL: Dict[str, Tuple[str, int]] = {
    "5m": ("minutes", 5),
    "15m": ("minutes", 15),
}

TIMEFRAME_TO_SECONDS: Dict[str, int] = {
    "5m": 300,
    "15m": 900,
}

PRICE_SCALE = 4
DEFAULT_UPSTOX_MARKET_DATA_BASE_URL = "https://api.upstox.com"
APPROVED_MARKET_DATA_HOSTS = ["api.upstox.com"]


# --- Error Classes ---

def sanitize_secret_text(text: str) -> str:
    """Removes Bearer tokens and sensitive token strings from error text."""
    import re
    if not text:
        return ""
    # Redact Bearer tokens
    text = re.sub(r'Bearer\s+[A-Za-z0-9_\-\.]+', 'Bearer [REDACTED]', text, flags=re.IGNORECASE)
    # Redact token= or secret= query parameters
    text = re.sub(r'(token|secret|access_token|authorization)=([^&\s]+)', r'\1=[REDACTED]', text, flags=re.IGNORECASE)
    return text


class MarketDataError(Exception):
    """Base exception for all market data errors. Sanitized against credential leakage."""
    def __init__(self, message: str, error_code: str = "MARKET_DATA_ERROR"):
        safe_message = sanitize_secret_text(str(message))
        super().__init__(safe_message)
        self.message = safe_message
        self.error_code = error_code


class MarketDataDisabledError(MarketDataError):
    """Raised when external market data networking is disabled by server policy."""
    def __init__(self, message: str = "Market data external network calls are disabled by server policy."):
        super().__init__(message, error_code="MARKET_DATA_DISABLED")


class MarketDataAuthenticationError(MarketDataError):
    """Raised on invalid, expired, or missing provider credentials without echoing raw tokens."""
    def __init__(self, message: str = "Market data authentication failed with provider."):
        super().__init__(message, error_code="AUTHENTICATION_FAILED")


class MarketDataRateLimitedError(MarketDataError):
    """Raised when the provider rate limits requests (HTTP 429)."""
    def __init__(self, retry_after: int = 5, message: str = "Provider rate limit reached."):
        super().__init__(f"{message} Retry after {retry_after}s.", error_code="RATE_LIMITED")
        self.retry_after = retry_after


class MarketDataServiceUnavailableError(MarketDataError):
    """Raised when provider returns 5xx or network transport fails."""
    def __init__(self, message: str = "Provider service unavailable."):
        super().__init__(message, error_code="SERVICE_UNAVAILABLE")


class MarketDataValidationError(MarketDataError):
    """Raised on invalid parameters, malformed provider response, or payload limits exceeded."""
    def __init__(self, message: str):
        super().__init__(message, error_code="VALIDATION_ERROR")


class MarketDataForbiddenError(MarketDataError):
    """Raised when the requesting user is not authorized for market data operations."""
    def __init__(self, message: str = "Market data access denied for this operator."):
        super().__init__(message, error_code="FORBIDDEN")


# --- Data Models ---

class MarketDataCandle(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime.datetime = Field(..., description="Normalized UTC timestamp for candle open")
    open: Decimal = Field(..., description="Exact open price quantized to scale")
    high: Decimal = Field(..., description="Exact high price quantized to scale")
    low: Decimal = Field(..., description="Exact low price quantized to scale")
    close: Decimal = Field(..., description="Exact close price quantized to scale")
    open_units: int = Field(..., description="Scaled integer representation of open price")
    high_units: int = Field(..., description="Scaled integer representation of high price")
    low_units: int = Field(..., description="Scaled integer representation of low price")
    close_units: int = Field(..., description="Scaled integer representation of close price")
    volume: int = Field(..., ge=0, description="Volume traded in this candle")
    is_closed: bool = Field(True, description="Strictly True for completed historical candles")


class MarketDataProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str = Field("UPSTOX", description="Market data provider name")
    source_type: str = Field("PROVIDER_UPSTOX_V3", description="Strict provenance source identifier. Never FIXTURE_REPLAY.")
    retrieved_at: datetime.datetime = Field(..., description="UTC timestamp of retrieval")
    requested_instrument_key: str = Field(..., description="Requested provider instrument token")
    timeframe: str = Field(..., description="Candle timeframe e.g. 5m or 15m")
    mode: Literal["intraday", "historical"] = Field(..., description="Acquisition mode")
    date_range: Optional[Dict[str, str]] = Field(None, description="Requested date range if historical")
    candle_count: int = Field(..., ge=0, description="Number of completed candles returned")
    content_fingerprint: str = Field(..., description="Deterministic SHA-256 fingerprint of the normalized candle series")
    completeness: Literal["COMPLETE", "INCOMPLETE", "UNKNOWN"] = Field("UNKNOWN", description="Completeness classification")
    is_complete_series: bool = Field(False, description="Strictly True only when completeness is COMPLETE")
    warnings: List[str] = Field(default_factory=list, description="Diagnostic warnings e.g. duplicates dropped, gaps found")


class MarketDataInstrument(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_key: str = Field(..., description="Upstox instrument token e.g. NSE_INDEX|Nifty 50")
    tradepro_instrument_id: str = Field(..., description="TradePro canonical instrument identity")
    name: str = Field(..., description="Human readable display name")
    exchange: str = Field(..., description="Exchange e.g. NSE")
    segment: str = Field(..., description="Segment e.g. INDEX, EQ, FO")
    lot_size: int = Field(1, ge=1)
    tick_size: Decimal = Field(Decimal("0.05"))
    supported_timeframes: List[str] = Field(default_factory=lambda: ["5m", "15m"])


class MarketDataReadinessResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    network_enabled: bool = Field(..., description="Whether UPSTOX_MARKET_DATA_ENABLED is set to true")
    credential_configured: bool = Field(..., description="Whether server-side UPSTOX_MARKET_DATA_ACCESS_TOKEN is provisioned")
    operator_configured: bool = Field(..., description="Whether UPSTOX_MARKET_DATA_OWNER_ID is configured")
    is_authorized_operator: bool = Field(..., description="Whether the requesting user matches the configured operator")
    status: Literal[
        "CONFIGURED_AND_ENABLED",
        "NETWORK_DISABLED",
        "CREDENTIALS_MISSING",
        "FORBIDDEN_OPERATOR",
        "OPERATOR_NOT_CONFIGURED",
        "INVALID_ENDPOINT_CONFIGURATION",
    ] = Field(...)
    base_url: str = Field(..., description="Configured provider base URL")
    approved_hosts: List[str] = Field(default_factory=lambda: APPROVED_MARKET_DATA_HOSTS)
    supported_timeframes: List[str] = Field(default_factory=lambda: SUPPORTED_MARKET_DATA_TIMEFRAMES)


class MarketDataCandlesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_key: str
    tradepro_instrument_id: Optional[str] = None
    timeframe: str
    mode: Literal["intraday", "historical"]
    candles: List[MarketDataCandle]
    provenance: MarketDataProvenance
