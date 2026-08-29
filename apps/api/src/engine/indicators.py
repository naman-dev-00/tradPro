from typing import List, Optional, Dict, Any, Union
from src.engine.models import Candle, IndicatorResult

def preprocess_candle_series(candles: List[Candle]) -> List[Candle]:
    """
    Filters out incomplete candles and validates strict chronological ordering
    without duplicate timestamps for a given series.
    """
    if not candles:
        return []

    # Exclude incomplete candles
    closed_candles = [c for c in candles if c.is_closed]
    if not closed_candles:
        return []

    # Validate series-level chronological ordering & duplicate timestamps
    for i in range(1, len(closed_candles)):
        prev_ts = closed_candles[i - 1].timestamp
        curr_ts = closed_candles[i].timestamp
        if curr_ts == prev_ts:
            raise ValueError(f"Duplicate timestamp '{curr_ts.isoformat()}' detected in candle series.")
        if curr_ts < prev_ts:
            raise ValueError(
                f"Candle series is not in chronological order. Timestamp '{curr_ts.isoformat()}' appears after '{prev_ts.isoformat()}'."
            )

    return closed_candles


def calculate_price(candles: List[Candle]) -> List[IndicatorResult]:
    closed = preprocess_candle_series(candles)
    results = []
    for c in closed:
        results.append(
            IndicatorResult(
                timestamp=c.timestamp,
                indicator="PRICE",
                value=c.close,
                available=True,
                warmup_remaining=0
            )
        )
    return results


def calculate_sma(candles: List[Candle], period: int = 20) -> List[IndicatorResult]:
    if period < 1:
        raise ValueError("SMA period must be at least 1.")

    closed = preprocess_candle_series(candles)
    results = []

    for idx, c in enumerate(closed):
        k = idx + 1
        if k < period:
            results.append(
                IndicatorResult(
                    timestamp=c.timestamp,
                    indicator="SMA",
                    value=None,
                    available=False,
                    warmup_remaining=period - k
                )
            )
        else:
            window = [closed[i].close for i in range(idx - period + 1, idx + 1)]
            sma_val = sum(window) / period
            results.append(
                IndicatorResult(
                    timestamp=c.timestamp,
                    indicator="SMA",
                    value=sma_val,
                    available=True,
                    warmup_remaining=0
                )
            )
    return results


def calculate_ema(candles: List[Candle], period: int = 20) -> List[IndicatorResult]:
    if period < 1:
        raise ValueError("EMA period must be at least 1.")

    closed = preprocess_candle_series(candles)
    results = []
    multiplier = 2.0 / (period + 1)
    prev_ema: Optional[float] = None

    for idx, c in enumerate(closed):
        k = idx + 1
        if k < period:
            results.append(
                IndicatorResult(
                    timestamp=c.timestamp,
                    indicator="EMA",
                    value=None,
                    available=False,
                    warmup_remaining=period - k
                )
            )
        elif k == period:
            # Initial EMA is the SMA of first `period` candles
            window = [closed[i].close for i in range(0, period)]
            initial_ema = sum(window) / period
            prev_ema = initial_ema
            results.append(
                IndicatorResult(
                    timestamp=c.timestamp,
                    indicator="EMA",
                    value=initial_ema,
                    available=True,
                    warmup_remaining=0
                )
            )
        else:
            current_ema = (c.close - prev_ema) * multiplier + prev_ema
            prev_ema = current_ema
            results.append(
                IndicatorResult(
                    timestamp=c.timestamp,
                    indicator="EMA",
                    value=current_ema,
                    available=True,
                    warmup_remaining=0
                )
            )
    return results


def calculate_rsi(candles: List[Candle], period: int = 14) -> List[IndicatorResult]:
    if period < 1:
        raise ValueError("RSI period must be at least 1.")

    closed = preprocess_candle_series(candles)
    results = []

    # Total completed candles needed for initial RSI is period + 1
    req_candles = period + 1

    prev_avg_gain: Optional[float] = None
    prev_avg_loss: Optional[float] = None

    for idx, c in enumerate(closed):
        k = idx + 1
        if k < req_candles:
            results.append(
                IndicatorResult(
                    timestamp=c.timestamp,
                    indicator="RSI",
                    value=None,
                    available=False,
                    warmup_remaining=req_candles - k
                )
            )
        elif k == req_candles:
            # Calculate initial avg gain/loss over initial `period` changes
            gains = []
            losses = []
            for i in range(1, req_candles):
                change = closed[i].close - closed[i - 1].close
                if change > 0:
                    gains.append(change)
                    losses.append(0.0)
                else:
                    gains.append(0.0)
                    losses.append(abs(change))

            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
            prev_avg_gain = avg_gain
            prev_avg_loss = avg_loss

            rsi_val = _compute_rsi_val(avg_gain, avg_loss)
            results.append(
                IndicatorResult(
                    timestamp=c.timestamp,
                    indicator="RSI",
                    value=rsi_val,
                    available=True,
                    warmup_remaining=0
                )
            )
        else:
            change = c.close - closed[idx - 1].close
            gain = max(change, 0.0)
            loss = max(-change, 0.0)

            # Wilder's smoothing
            avg_gain = (prev_avg_gain * (period - 1) + gain) / period
            avg_loss = (prev_avg_loss * (period - 1) + loss) / period
            prev_avg_gain = avg_gain
            prev_avg_loss = avg_loss

            rsi_val = _compute_rsi_val(avg_gain, avg_loss)
            results.append(
                IndicatorResult(
                    timestamp=c.timestamp,
                    indicator="RSI",
                    value=rsi_val,
                    available=True,
                    warmup_remaining=0
                )
            )
    return results


def _compute_rsi_val(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        if avg_gain > 0.0:
            return 100.0
        return 50.0  # Both gain and loss are zero (flat price series)
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_macd(
    candles: List[Candle],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9
) -> List[IndicatorResult]:
    if fast_period < 1 or slow_period < 1 or signal_period < 1:
        raise ValueError("MACD periods must be positive integers.")
    if slow_period <= fast_period:
        raise ValueError("MACD slow_period must be strictly greater than fast_period.")

    closed = preprocess_candle_series(candles)
    results = []

    # Calculate fast and slow EMAs
    fast_ema_res = calculate_ema(closed, period=fast_period)
    slow_ema_res = calculate_ema(closed, period=slow_period)

    # Component 1: MACD line = Fast EMA - Slow EMA (Available at index slow_period - 1, i.e., candle count slow_period)
    macd_series: List[Optional[float]] = []
    for f_res, s_res in zip(fast_ema_res, slow_ema_res):
        if s_res.available and f_res.value is not None and s_res.value is not None:
            macd_series.append(f_res.value - s_res.value)
        else:
            macd_series.append(None)

    # Component 2 & 3: Signal line (EMA of MACD line over signal_period) & Histogram
    # Signal line requires signal_period completed MACD values.
    # Total candles required for Signal line = slow_period + signal_period - 1.
    full_warmup_req = slow_period + signal_period - 1

    # EMA calculation on macd_series for available values
    signal_series: List[Optional[float]] = [None] * len(closed)

    # Collect non-None MACD values
    macd_avail_indices = [i for i, v in enumerate(macd_series) if v is not None]
    if len(macd_avail_indices) >= signal_period:
        multiplier = 2.0 / (signal_period + 1)
        # Initial Signal line value is SMA of first `signal_period` MACD values
        first_signal_idx = macd_avail_indices[signal_period - 1]
        initial_window = [macd_series[i] for i in macd_avail_indices[:signal_period]]  # type: ignore
        prev_signal = sum(initial_window) / signal_period
        signal_series[first_signal_idx] = prev_signal

        for i in range(signal_period, len(macd_avail_indices)):
            idx = macd_avail_indices[i]
            curr_macd = macd_series[idx]
            curr_signal = (curr_macd - prev_signal) * multiplier + prev_signal  # type: ignore
            signal_series[idx] = curr_signal
            prev_signal = curr_signal

    for idx, c in enumerate(closed):
        k = idx + 1
        m_val = macd_series[idx]
        s_val = signal_series[idx]
        h_val = (m_val - s_val) if (m_val is not None and s_val is not None) else None

        full_available = (m_val is not None) and (s_val is not None)
        warmup_rem = max(0, full_warmup_req - k)

        results.append(
            IndicatorResult(
                timestamp=c.timestamp,
                indicator="MACD",
                value={
                    "macd": m_val,
                    "signal": s_val,
                    "histogram": h_val
                },
                available=full_available,
                warmup_remaining=warmup_rem
            )
        )
    return results


def calculate_pivot(candles: List[Candle]) -> List[IndicatorResult]:
    closed = preprocess_candle_series(candles)
    results = []

    for idx, c in enumerate(closed):
        k = idx + 1
        if k < 2:
            results.append(
                IndicatorResult(
                    timestamp=c.timestamp,
                    indicator="PIVOT",
                    value={"pivot": None, "s1": None, "r1": None},
                    available=False,
                    warmup_remaining=1
                )
            )
        else:
            prev_c = closed[idx - 1]
            p = (prev_c.high + prev_c.low + prev_c.close) / 3.0
            s1 = (2.0 * p) - prev_c.high
            r1 = (2.0 * p) - prev_c.low
            results.append(
                IndicatorResult(
                    timestamp=c.timestamp,
                    indicator="PIVOT",
                    value={"pivot": p, "s1": s1, "r1": r1},
                    available=True,
                    warmup_remaining=0
                )
            )
    return results


def calculate_volume(candles: List[Candle]) -> List[IndicatorResult]:
    closed = preprocess_candle_series(candles)
    results = []
    for c in closed:
        results.append(
            IndicatorResult(
                timestamp=c.timestamp,
                indicator="VOLUME",
                value=c.volume,
                available=True,
                warmup_remaining=0
            )
        )
    return results


def calculate_average_volume(candles: List[Candle], period: int = 20) -> List[IndicatorResult]:
    if period < 1:
        raise ValueError("AVERAGE_VOLUME period must be at least 1.")

    closed = preprocess_candle_series(candles)
    results = []
    for idx, c in enumerate(closed):
        k = idx + 1
        if k < period:
            results.append(
                IndicatorResult(
                    timestamp=c.timestamp,
                    indicator="AVERAGE_VOLUME",
                    value=None,
                    available=False,
                    warmup_remaining=period - k
                )
            )
        else:
            window = [closed[i].volume for i in range(idx - period + 1, idx + 1)]
            avg_vol = sum(window) / period
            results.append(
                IndicatorResult(
                    timestamp=c.timestamp,
                    indicator="AVERAGE_VOLUME",
                    value=avg_vol,
                    available=True,
                    warmup_remaining=0
                )
            )
    return results
