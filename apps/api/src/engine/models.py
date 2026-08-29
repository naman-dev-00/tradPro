from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Union, Dict, Any, List

SUPPORTED_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w"}

@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    instrument_id: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = True

    def __post_init__(self):
        # Timezone validation & normalization
        if self.timestamp.tzinfo is None or self.timestamp.tzinfo.utcoffset(self.timestamp) is None:
            raise ValueError("Naive timestamps are rejected. Candle timestamp must be timezone-aware (UTC).")

        normalized_ts = self.timestamp.astimezone(timezone.utc)
        object.__setattr__(self, "timestamp", normalized_ts)

        # Instrument ID validation
        if not self.instrument_id or not isinstance(self.instrument_id, str):
            raise ValueError("Candle instrument_id must be a non-empty string.")

        # Timeframe validation
        if self.timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe '{self.timeframe}'. Supported timeframes: {sorted(list(SUPPORTED_TIMEFRAMES))}")

        # Price validation (> 0 strictly)
        if self.open <= 0 or self.high <= 0 or self.low <= 0 or self.close <= 0:
            raise ValueError("Candle open, high, low, and close prices must be strictly greater than 0.")

        # Volume validation (>= 0)
        if self.volume < 0:
            raise ValueError("Candle volume cannot be negative.")

        # High/Low relationship bounds
        if self.high < max(self.open, self.close, self.low):
            raise ValueError(f"Invalid candle OHLC: high ({self.high}) cannot be below open ({self.open}), close ({self.close}), or low ({self.low}).")

        if self.low > min(self.open, self.close, self.high):
            raise ValueError(f"Invalid candle OHLC: low ({self.low}) cannot be above open ({self.open}), close ({self.close}), or high ({self.high}).")


@dataclass(frozen=True)
class IndicatorResult:
    timestamp: datetime
    indicator: str
    value: Optional[Union[float, Dict[str, Optional[float]]]]
    available: bool
    warmup_remaining: int

    def __post_init__(self):
        if self.timestamp.tzinfo is None or self.timestamp.tzinfo.utcoffset(self.timestamp) is None:
            raise ValueError("IndicatorResult timestamp must be timezone-aware (UTC).")

        normalized_ts = self.timestamp.astimezone(timezone.utc)
        object.__setattr__(self, "timestamp", normalized_ts)

        if self.warmup_remaining < 0:
            raise ValueError("warmup_remaining cannot be negative.")
