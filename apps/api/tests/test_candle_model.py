import pytest
from datetime import datetime, timezone, timedelta
from src.engine.models import Candle, IndicatorResult

def test_valid_candle_creation():
    now_utc = datetime.now(timezone.utc)
    c = Candle(
        timestamp=now_utc,
        instrument_id="NIFTY",
        timeframe="15m",
        open=22000.0,
        high=22100.0,
        low=21950.0,
        close=22050.0,
        volume=1500.0,
        is_closed=True
    )
    assert c.open == 22000.0
    assert c.high == 22100.0
    assert c.low == 21950.0
    assert c.close == 22050.0
    assert c.volume == 1500.0
    assert c.timestamp.tzinfo == timezone.utc

def test_naive_timestamp_rejected():
    naive_dt = datetime(2026, 8, 28, 9, 15)
    with pytest.raises(ValueError, match="Naive timestamps are rejected"):
        Candle(
            timestamp=naive_dt,
            instrument_id="NIFTY",
            timeframe="15m",
            open=100.0, high=105.0, low=95.0, close=102.0, volume=10.0
        )

def test_timezone_normalized_to_utc():
    # EST timezone (-5 hours)
    est_tz = timezone(timedelta(hours=-5))
    est_dt = datetime(2026, 8, 28, 9, 15, tzinfo=est_tz)

    c = Candle(
        timestamp=est_dt,
        instrument_id="NIFTY",
        timeframe="15m",
        open=100.0, high=105.0, low=95.0, close=102.0, volume=10.0
    )
    assert c.timestamp.tzinfo == timezone.utc
    assert c.timestamp.hour == 14  # 9:15 EST = 14:15 UTC

def test_unsupported_timeframe_rejected():
    now_utc = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        Candle(
            timestamp=now_utc,
            instrument_id="NIFTY",
            timeframe="10m",
            open=100.0, high=105.0, low=95.0, close=102.0, volume=10.0
        )

def test_non_positive_price_rejected():
    now_utc = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="must be strictly greater than 0"):
        Candle(
            timestamp=now_utc,
            instrument_id="NIFTY",
            timeframe="15m",
            open=0.0, high=105.0, low=95.0, close=102.0, volume=10.0
        )

def test_zero_volume_allowed_negative_volume_rejected():
    now_utc = datetime.now(timezone.utc)
    # Zero volume is valid
    c = Candle(
        timestamp=now_utc,
        instrument_id="NIFTY",
        timeframe="15m",
        open=100.0, high=105.0, low=95.0, close=102.0, volume=0.0
    )
    assert c.volume == 0.0

    # Negative volume is invalid
    with pytest.raises(ValueError, match="volume cannot be negative"):
        Candle(
            timestamp=now_utc,
            instrument_id="NIFTY",
            timeframe="15m",
            open=100.0, high=105.0, low=95.0, close=102.0, volume=-5.0
        )

def test_invalid_ohlc_relationships():
    now_utc = datetime.now(timezone.utc)
    # High below close
    with pytest.raises(ValueError, match="high .* cannot be below"):
        Candle(
            timestamp=now_utc,
            instrument_id="NIFTY",
            timeframe="15m",
            open=100.0, high=101.0, low=95.0, close=105.0, volume=10.0
        )

    # Low above open
    with pytest.raises(ValueError, match="low .* cannot be above"):
        Candle(
            timestamp=now_utc,
            instrument_id="NIFTY",
            timeframe="15m",
            open=100.0, high=105.0, low=101.0, close=102.0, volume=10.0
        )

def test_candle_immutability():
    now_utc = datetime.now(timezone.utc)
    c = Candle(
        timestamp=now_utc,
        instrument_id="NIFTY",
        timeframe="15m",
        open=100.0, high=105.0, low=95.0, close=102.0, volume=10.0
    )
    with pytest.raises(AttributeError):
        c.close = 110.0
