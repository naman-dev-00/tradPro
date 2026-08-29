import pytest
from datetime import datetime, timezone, timedelta
from src.engine.models import Candle
from src.engine.indicators import (
    preprocess_candle_series,
    calculate_price,
    calculate_sma,
    calculate_ema,
    calculate_rsi,
    calculate_macd,
    calculate_pivot,
    calculate_volume,
    calculate_average_volume,
)

def make_candles(closes, highs=None, lows=None, volumes=None, start_dt=None, is_closed_list=None):
    if start_dt is None:
        start_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    if highs is None:
        highs = [c + 2.0 for c in closes]
    if lows is None:
        lows = [c - 2.0 for c in closes]
    if volumes is None:
        volumes = [100.0 * (i + 1) for i in range(len(closes))]

    candles = []
    for i, c_val in enumerate(closes):
        ts = start_dt + timedelta(minutes=15 * i)
        is_c = is_closed_list[i] if is_closed_list else True
        candles.append(
            Candle(
                timestamp=ts,
                instrument_id="NIFTY",
                timeframe="15m",
                open=c_val,
                high=highs[i],
                low=lows[i],
                close=c_val,
                volume=volumes[i],
                is_closed=is_c
            )
        )
    return candles


def test_series_preprocessing_filtering_and_ordering():
    # Test filtering incomplete candles and chronological order checking
    dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    c1 = Candle(timestamp=dt, instrument_id="NIFTY", timeframe="15m", open=10, high=12, low=8, close=10, volume=10, is_closed=True)
    c2 = Candle(timestamp=dt + timedelta(minutes=15), instrument_id="NIFTY", timeframe="15m", open=10, high=12, low=8, close=11, volume=10, is_closed=False)
    c3 = Candle(timestamp=dt + timedelta(minutes=30), instrument_id="NIFTY", timeframe="15m", open=10, high=12, low=8, close=12, volume=10, is_closed=True)

    processed = preprocess_candle_series([c1, c2, c3])
    assert len(processed) == 2
    assert processed[0].close == 10
    assert processed[1].close == 12

    # Duplicate timestamp error
    with pytest.raises(ValueError, match="Duplicate timestamp"):
        preprocess_candle_series([c1, c1])

    # Out of order timestamp error
    with pytest.raises(ValueError, match="not in chronological order"):
        preprocess_candle_series([c3, c1])


def test_sma_calculation():
    # Independent calculation check for 3-period SMA
    closes = [10.0, 20.0, 30.0, 40.0]
    candles = make_candles(closes)
    res = calculate_sma(candles, period=3)

    assert len(res) == 4
    # Candle 1 & 2: warm-up
    assert res[0].available is False
    assert res[0].warmup_remaining == 2
    assert res[0].value is None

    assert res[1].available is False
    assert res[1].warmup_remaining == 1
    assert res[1].value is None

    # Candle 3: SMA(10, 20, 30) = 20.0
    assert res[2].available is True
    assert res[2].warmup_remaining == 0
    assert res[2].value == pytest.approx(20.0)

    # Candle 4: SMA(20, 30, 40) = 30.0
    assert res[3].available is True
    assert res[3].value == pytest.approx(30.0)


def test_ema_calculation():
    # Independent calculation check for 3-period EMA
    # Multiplier = 2 / (3 + 1) = 0.5
    closes = [10.0, 20.0, 30.0, 40.0]
    candles = make_candles(closes)
    res = calculate_ema(candles, period=3)

    # Candle 1 & 2: warm-up
    assert res[0].available is False
    assert res[1].available is False

    # Candle 3: Initial EMA = SMA(10, 20, 30) = 20.0
    assert res[2].available is True
    assert res[2].value == pytest.approx(20.0)

    # Candle 4: EMA = (40.0 - 20.0) * 0.5 + 20.0 = 30.0
    assert res[3].available is True
    assert res[3].value == pytest.approx(30.0)


def test_rsi_calculation_and_edge_cases():
    # 2-period RSI requires 3 candles (2 price changes)
    closes = [10.0, 20.0, 30.0]  # Gains: +10, +10. Losses: 0, 0
    candles = make_candles(closes)
    res = calculate_rsi(candles, period=2)

    # Candle 1 & 2: warm-up
    assert res[0].available is False
    assert res[1].available is False

    # Candle 3: avg_gain = 10, avg_loss = 0 => RSI = 100.0
    assert res[2].available is True
    assert res[2].value == pytest.approx(100.0)

    # Test flat price series (gain=0, loss=0 => RSI=50.0)
    flat_closes = [10.0, 10.0, 10.0]
    flat_candles = make_candles(flat_closes)
    flat_res = calculate_rsi(flat_candles, period=2)
    assert flat_res[2].value == pytest.approx(50.0)


def test_macd_component_warmup_alignment():
    # Fast=2, Slow=4, Signal=3
    # MACD line available at slow=4 candles
    # Signal/Hist available at 4 + 3 - 1 = 6 candles
    closes = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0]
    candles = make_candles(closes)
    res = calculate_macd(candles, fast_period=2, slow_period=4, signal_period=3)

    # Candles 1-3: complete warm-up
    assert res[0].value == {"macd": None, "signal": None, "histogram": None}
    assert res[2].value == {"macd": None, "signal": None, "histogram": None}

    # Candle 4 (k=4): MACD line is available, signal is None
    assert res[3].value["macd"] is not None
    assert res[3].value["signal"] is None
    assert res[3].available is False

    # Candle 6 (k=6): Full MACD set available
    assert res[5].value["macd"] is not None
    assert res[5].value["signal"] is not None
    assert res[5].value["histogram"] is not None
    assert res[5].available is True


def test_pivot_calculation():
    # Candle 1: H=12, L=8, C=10
    # Candle 2: P = (12+8+10)/3 = 10.0, S1 = 20 - 12 = 8.0, R1 = 20 - 8 = 12.0
    closes = [10.0, 20.0]
    highs = [12.0, 22.0]
    lows = [8.0, 18.0]
    candles = make_candles(closes, highs=highs, lows=lows)

    res = calculate_pivot(candles)
    assert res[0].available is False
    assert res[0].value == {"pivot": None, "s1": None, "r1": None}

    assert res[1].available is True
    assert res[1].value["pivot"] == pytest.approx(10.0)
    assert res[1].value["s1"] == pytest.approx(8.0)
    assert res[1].value["r1"] == pytest.approx(12.0)


def test_prevention_of_future_data_leakage():
    closes = [10.0, 20.0, 30.0, 40.0, 50.0]
    candles1 = make_candles(closes)

    # Modify future candle at index 4 (candle 5)
    closes_mod = [10.0, 20.0, 30.0, 40.0, 999.0]
    candles2 = make_candles(closes_mod)

    sma1 = calculate_sma(candles1, period=3)
    sma2 = calculate_sma(candles2, period=3)

    # Results up to index 3 (candle 4) MUST be identical
    assert sma1[2].value == sma2[2].value
    assert sma1[3].value == sma2[3].value
    # Index 4 will differ
    assert sma1[4].value != sma2[4].value
