"""
TradePro Phase 5: Provider Market Data Engine.
Provides read-only acquisition, canonical validation, numeric scaling,
injectable-clock candle completion, and dataset provenance for Upstox V3 candle data.
"""

from .contracts import (
    SUPPORTED_MARKET_DATA_TIMEFRAMES,
    TIMEFRAME_TO_UPSTOX_INTERVAL,
    TIMEFRAME_TO_SECONDS,
    DEFAULT_UPSTOX_MARKET_DATA_BASE_URL,
    MarketDataCandle,
    MarketDataProvenance,
    MarketDataReadinessResponse,
    MarketDataInstrument,
    MarketDataCandlesResponse,
    MarketDataError,
    MarketDataDisabledError,
    MarketDataAuthenticationError,
    MarketDataRateLimitedError,
    MarketDataServiceUnavailableError,
    MarketDataValidationError,
    MarketDataForbiddenError,
)
from .adapter import UpstoxMarketDataAdapter
from .normalizer import MarketDataNormalizer
from .provenance import compute_market_data_fingerprint

__all__ = [
    "SUPPORTED_MARKET_DATA_TIMEFRAMES",
    "TIMEFRAME_TO_UPSTOX_INTERVAL",
    "TIMEFRAME_TO_SECONDS",
    "DEFAULT_UPSTOX_MARKET_DATA_BASE_URL",
    "MarketDataCandle",
    "MarketDataProvenance",
    "MarketDataReadinessResponse",
    "MarketDataInstrument",
    "MarketDataCandlesResponse",
    "MarketDataError",
    "MarketDataDisabledError",
    "MarketDataAuthenticationError",
    "MarketDataRateLimitedError",
    "MarketDataServiceUnavailableError",
    "MarketDataValidationError",
    "MarketDataForbiddenError",
    "UpstoxMarketDataAdapter",
    "MarketDataNormalizer",
    "compute_market_data_fingerprint",
]
