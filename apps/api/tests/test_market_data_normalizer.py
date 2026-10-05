import datetime
from decimal import Decimal
import pytest
from src.engine.market_data.normalizer import MarketDataNormalizer
from src.engine.market_data.provenance import compute_market_data_fingerprint
from src.engine.market_data.contracts import MarketDataValidationError


def test_normalizer_converts_raw_upstox_format():
    """
    Validates conversion of raw Upstox 6-element list to scaled-integer MarketDataCandle.
    """
    fixed_clock = lambda: datetime.datetime(2026, 10, 5, 20, 0, 0, tzinfo=datetime.timezone.utc)
    normalizer = MarketDataNormalizer(clock=fixed_clock)

    raw = [
        ["2026-10-05T15:15:00+05:30", 25200.0, 25250.0, 25190.0, 25240.0, 50000, 0]
    ]

    candles, warnings, is_complete = normalizer.normalize_candles(raw, "5m")

    assert len(candles) == 1
    candle = candles[0]
    # Timestamp converted to UTC: 15:15 IST (UTC+05:30) == 09:45 UTC
    assert candle.timestamp == datetime.datetime(2026, 10, 5, 9, 45, 0, tzinfo=datetime.timezone.utc)
    assert candle.open == Decimal("25200.0000")
    assert candle.high == Decimal("25250.0000")
    assert candle.low == Decimal("25190.0000")
    assert candle.close == Decimal("25240.0000")
    assert candle.open_units == 252000000
    assert candle.high_units == 252500000
    assert candle.low_units == 251900000
    assert candle.close_units == 252400000
    assert candle.volume == 50000
    assert candle.is_closed is True


def test_malformed_records_raise_validation_error():
    """
    Malformed records (short arrays, bad types, naive timestamps) must raise MarketDataValidationError.
    """
    normalizer = MarketDataNormalizer()

    # Too short
    with pytest.raises(MarketDataValidationError) as exc_info:
        normalizer.normalize_candles([["2026-10-05T10:00:00Z", 100, 105]], "5m")
    assert "at least 6 elements" in str(exc_info.value)

    # Naive timestamp (no tz)
    with pytest.raises(MarketDataValidationError) as exc_info:
        normalizer.normalize_candles([["2026-10-05T10:00:00", 100, 105, 95, 102, 1000, 0]], "5m")
    assert "must be timezone-aware" in str(exc_info.value)


def test_strict_geometry_validation():
    """
    OHLC geometry violations must be rejected immediately.
    """
    normalizer = MarketDataNormalizer()

    # High lower than open
    raw_bad_high = [
        ["2026-10-05T10:00:00+00:00", 25000.0, 24900.0, 24800.0, 24950.0, 1000, 0]
    ]
    with pytest.raises(MarketDataValidationError) as exc_info:
        normalizer.normalize_candles(raw_bad_high, "5m")
    assert "high" in str(exc_info.value) and "lower than open" in str(exc_info.value)

    # Low higher than close
    raw_bad_low = [
        ["2026-10-05T10:00:00+00:00", 25000.0, 25100.0, 25050.0, 25020.0, 1000, 0]
    ]
    with pytest.raises(MarketDataValidationError) as exc_info:
        normalizer.normalize_candles(raw_bad_low, "5m")
    assert "low" in str(exc_info.value) and "higher than" in str(exc_info.value)


def test_non_positive_prices_rejected():
    """
    Zero or negative prices must be rejected.
    """
    normalizer = MarketDataNormalizer()

    raw_zero = [
        ["2026-10-05T10:00:00+00:00", 0.0, 100.0, 0.0, 50.0, 1000, 0]
    ]
    with pytest.raises(MarketDataValidationError) as exc_info:
        normalizer.normalize_candles(raw_zero, "5m")
    assert "strictly positive" in str(exc_info.value)


def test_injectable_clock_excludes_unfinished_and_future_candles():
    """
    Unfinished (in-progress) or future candles must NOT become completed inputs.
    Clock: 2026-10-05 10:00:00 UTC.
    5m candle at 09:50:00 UTC closes at 09:55:00 UTC -> COMPLETED (admitted).
    5m candle at 10:00:00 UTC closes at 10:05:00 UTC -> IN-PROGRESS (excluded).
    5m candle at 10:05:00 UTC closes at 10:10:00 UTC -> FUTURE (excluded).
    """
    fixed_clock = lambda: datetime.datetime(2026, 10, 5, 10, 0, 0, tzinfo=datetime.timezone.utc)
    normalizer = MarketDataNormalizer(clock=fixed_clock)

    raw = [
        ["2026-10-05T09:50:00+00:00", 25000.0, 25050.0, 24950.0, 25010.0, 1000, 0],
        ["2026-10-05T10:00:00+00:00", 25010.0, 25080.0, 25000.0, 25070.0, 800, 0],
        ["2026-10-05T10:05:00+00:00", 25070.0, 25100.0, 25060.0, 25090.0, 500, 0],
    ]

    candles, warnings, is_complete = normalizer.normalize_candles(raw, "5m")

    assert len(candles) == 1
    assert candles[0].timestamp == datetime.datetime(2026, 10, 5, 9, 50, 0, tzinfo=datetime.timezone.utc)
    assert any("Excluded in-progress or future candle" in w for w in warnings)


def test_chronological_sorting_and_deduplication():
    """
    Input candles provided out of order (e.g. descending) must be sorted chronologically ascending.
    Identical duplicate records must be dropped.
    """
    normalizer = MarketDataNormalizer(
        clock=lambda: datetime.datetime(2026, 10, 5, 20, 0, 0, tzinfo=datetime.timezone.utc)
    )

    raw = [
        ["2026-10-05T10:10:00+00:00", 25020.0, 25040.0, 25010.0, 25030.0, 1500, 0],
        ["2026-10-05T10:00:00+00:00", 25000.0, 25020.0, 24990.0, 25010.0, 1200, 0],
        ["2026-10-05T10:05:00+00:00", 25010.0, 25030.0, 25000.0, 25020.0, 1300, 0],
        # Duplicate of 10:05
        ["2026-10-05T10:05:00+00:00", 25010.0, 25030.0, 25000.0, 25020.0, 1300, 0],
    ]

    candles, warnings, is_complete = normalizer.normalize_candles(raw, "5m")

    assert len(candles) == 3
    assert candles[0].timestamp == datetime.datetime(2026, 10, 5, 10, 0, 0, tzinfo=datetime.timezone.utc)
    assert candles[1].timestamp == datetime.datetime(2026, 10, 5, 10, 5, 0, tzinfo=datetime.timezone.utc)
    assert candles[2].timestamp == datetime.datetime(2026, 10, 5, 10, 10, 0, tzinfo=datetime.timezone.utc)
    assert is_complete is False
    assert any("Duplicate identical candle dropped" in w for w in warnings)


def test_conflicting_revision_resolution():
    """
    Conflicting values for the same timestamp must be quarantined rather than selecting
    an arbitrary winner.
    """
    normalizer = MarketDataNormalizer(
        clock=lambda: datetime.datetime(2026, 10, 5, 20, 0, 0, tzinfo=datetime.timezone.utc)
    )

    raw = [
        ["2026-10-05T10:00:00+00:00", 25000.0, 25020.0, 24990.0, 25010.0, 1000, 0],
        # Conflicting revision for same timestamp
        ["2026-10-05T10:00:00+00:00", 25000.0, 25030.0, 24990.0, 25025.0, 1100, 0],
    ]

    candles, warnings, is_complete = normalizer.normalize_candles(raw, "5m")

    # Conflicted interval is quarantined: no candle arbitrarily returned
    assert len(candles) == 0
    assert is_complete is False
    assert any("Quarantined conflicting candle interval" in w for w in warnings)


def test_gap_detection_does_not_silently_fill():
    """
    Gaps in candle intervals must be audited and flagged with is_complete_series=False.
    """
    normalizer = MarketDataNormalizer(
        clock=lambda: datetime.datetime(2026, 10, 5, 20, 0, 0, tzinfo=datetime.timezone.utc)
    )

    # 10:00 -> 10:15 has a 15m delta instead of 5m (missing 10:05 and 10:10)
    raw = [
        ["2026-10-05T10:00:00+00:00", 25000.0, 25020.0, 24990.0, 25010.0, 1000, 0],
        ["2026-10-05T10:15:00+00:00", 25030.0, 25050.0, 25020.0, 25040.0, 1200, 0],
    ]

    candles, warnings, is_complete = normalizer.normalize_candles(raw, "5m")

    assert len(candles) == 2
    assert is_complete is False
    assert any("Intraday data gap detected" in w for w in warnings)


def test_inter_session_gap_reporting():
    """
    Cross-date candle step must warn about unverified exchange calendar and set is_complete=False.
    """
    normalizer = MarketDataNormalizer(
        clock=lambda: datetime.datetime(2026, 10, 6, 20, 0, 0, tzinfo=datetime.timezone.utc)
    )

    raw = [
        ["2026-10-05T15:25:00+00:00", 25000.0, 25020.0, 24990.0, 25010.0, 1000, 0],
        ["2026-10-06T09:15:00+00:00", 25030.0, 25050.0, 25020.0, 25040.0, 1200, 0],
    ]

    candles, warnings, is_complete = normalizer.normalize_candles(raw, "5m")

    assert len(candles) == 2
    assert is_complete is False
    assert any("Inter-session boundary or gap detected" in w for w in warnings)


def test_exact_price_precision_and_rejections():
    """
    Exact price parsing without silent rounding:
    - Rejects unsupported precision (e.g. 100.00005)
    - Rejects NaN, Infinity, -Infinity
    - Rejects booleans (True, False)
    - Rejects oversized prices
    """
    normalizer = MarketDataNormalizer()

    # 1. Unsupported precision (5 decimal places): must be rejected, not rounded!
    raw_imprecise = [
        ["2026-10-05T10:00:00+00:00", "100.00005", "100.05", "99.95", "100.01", 1000, 0]
    ]
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles(raw_imprecise, "5m")
    assert "Unsupported price precision" in str(exc.value)
    assert "Silent rounding is prohibited" in str(exc.value)

    # 2. NaN price
    raw_nan = [
        ["2026-10-05T10:00:00+00:00", float("nan"), 105.0, 95.0, 100.0, 1000, 0]
    ]
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles(raw_nan, "5m")
    assert "Non-finite value" in str(exc.value)

    # 3. Infinite price
    raw_inf = [
        ["2026-10-05T10:00:00+00:00", float("inf"), 105.0, 95.0, 100.0, 1000, 0]
    ]
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles(raw_inf, "5m")
    assert "Non-finite value" in str(exc.value)

    # 4. Boolean price
    raw_bool = [
        ["2026-10-05T10:00:00+00:00", True, 105.0, 95.0, 100.0, 1000, 0]
    ]
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles(raw_bool, "5m")
    assert "Boolean value rejected" in str(exc.value)

    # 5. Oversized price
    raw_oversized = [
        ["2026-10-05T10:00:00+00:00", "1000000005", "1000000010", "1000000000", "1000000005", 1000, 0]
    ]
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles(raw_oversized, "5m")
    assert "exceeds maximum supported limit" in str(exc.value)


def test_exact_volume_validation_and_rejections():
    """
    Exact volume validation:
    - Rejects negative volume (-0.5 or -10)
    - Rejects fractional volume (1.9)
    - Rejects boolean volume
    - Rejects NaN/Infinite volume
    """
    normalizer = MarketDataNormalizer()

    # 1. Negative volume
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles([["2026-10-05T10:00:00+00:00", 100.0, 105.0, 95.0, 102.0, -0.5, 0]], "5m")
    assert "Negative volume" in str(exc.value)

    # 2. Fractional volume
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles([["2026-10-05T10:00:00+00:00", 100.0, 105.0, 95.0, 102.0, 1.9, 0]], "5m")
    assert "Fractional volume" in str(exc.value)

    # 3. Boolean volume
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles([["2026-10-05T10:00:00+00:00", 100.0, 105.0, 95.0, 102.0, True, 0]], "5m")
    assert "Boolean value rejected for volume" in str(exc.value)

    # 4. Extreme volume "1e5000" must be rejected cleanly without int conversion crash
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles([["2026-10-05T10:00:00+00:00", 100.0, 105.0, 95.0, 102.0, "1e5000", 0]], "5m")
    assert "Volume exceeds maximum supported bound at index 0." in str(exc.value)
    assert len(str(exc.value)) < 100


def test_bounded_diagnostics_prevent_error_disclosure():
    """
    Error messages must be short, bounded, and contain row index/field/reason
    WITHOUT echoing raw provider values, parser exceptions, or secrets.
    """
    normalizer = MarketDataNormalizer()
    dummy_secret = "secret_access_token_bearer_998877"

    # 1. Dummy token in timestamp
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles([[dummy_secret, 100.0, 105.0, 95.0, 102.0, 100, 0]], "5m")
    assert dummy_secret not in str(exc.value)
    assert "Unparseable ISO-8601 timestamp at index 0: invalid datetime format." in str(exc.value)

    # 2. 8KB malformed timestamp produces short, controlled error
    huge_ts = "2026-10-05T" + ("X" * 8192)
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles([[huge_ts, 100.0, 105.0, 95.0, 102.0, 100, 0]], "5m")
    assert len(str(exc.value)) < 120
    assert "Unparseable ISO-8601 timestamp at index 0: invalid datetime format." in str(exc.value)
    assert "X" * 10 not in str(exc.value)

    # 3. Dummy token in price field
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles([["2026-10-05T10:00:00+00:00", dummy_secret, 105.0, 95.0, 102.0, 100, 0]], "5m")
    assert dummy_secret not in str(exc.value)
    assert "Invalid numeric value for open at index 0." in str(exc.value)

    # 4. Dummy token in volume field
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles([["2026-10-05T10:00:00+00:00", 100.0, 105.0, 95.0, 102.0, dummy_secret, 0]], "5m")
    assert dummy_secret not in str(exc.value)
    assert "Invalid numeric value for volume at index 0." in str(exc.value)


def test_timeframe_alignment_validation():
    """
    Candle open timestamps must strictly align with timeframe intervals.
    """
    normalizer = MarketDataNormalizer()

    # Misaligned 5m (minute 17)
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles([["2026-10-05T10:17:00+00:00", 100.0, 105.0, 95.0, 102.0, 100, 0]], "5m")
    assert "not aligned to 5m interval boundary" in str(exc.value)

    # Misaligned seconds
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles([["2026-10-05T10:15:30+00:00", 100.0, 105.0, 95.0, 102.0, 100, 0]], "5m")
    assert "not aligned to 5m interval boundary" in str(exc.value)

    # Misaligned 15m (minute 20)
    with pytest.raises(MarketDataValidationError) as exc:
        normalizer.normalize_candles([["2026-10-05T10:20:00+00:00", 100.0, 105.0, 95.0, 102.0, 100, 0]], "15m")
    assert "not aligned to 15m interval boundary" in str(exc.value)


def test_completeness_and_boundary_conditions():
    """
    Empty series and single candle series must report honest completeness.
    """
    normalizer = MarketDataNormalizer()

    # 1. Empty series
    res_empty = normalizer.normalize_candles([], "5m")
    assert len(res_empty.candles) == 0
    assert res_empty.is_complete_series is False
    assert res_empty.completeness == "INCOMPLETE"

    # 2. Single candle series
    res_single = normalizer.normalize_candles([
        ["2026-10-05T10:00:00+00:00", 100.0, 105.0, 95.0, 102.0, 100, 0]
    ], "5m")
    assert len(res_single.candles) == 1
    assert res_single.is_complete_series is False
    assert res_single.completeness == "UNKNOWN"

    # 3. Date boundary coverage in historical mode
    res_hist = normalizer.normalize_candles(
        [
            ["2026-10-03T10:00:00+00:00", 100.0, 105.0, 95.0, 102.0, 100, 0],
            ["2026-10-03T10:05:00+00:00", 102.0, 106.0, 101.0, 105.0, 110, 0],
        ],
        "5m",
        from_date="2026-10-01",
        to_date="2026-10-05",
        mode="historical",
    )
    assert res_hist.is_complete_series is False
    assert res_hist.completeness == "INCOMPLETE"
    assert any("missing start date 2026-10-01" in w for w in res_hist.warnings)

    # 4. Partial contiguous day returns UNKNOWN with is_complete_series=False
    res_partial = normalizer.normalize_candles([
        ["2026-10-05T09:15:00+05:30", 100.0, 105.0, 95.0, 102.0, 100, 0],
        ["2026-10-05T09:20:00+05:30", 102.0, 106.0, 101.0, 105.0, 100, 0],
    ], "5m", mode="intraday")
    assert len(res_partial.candles) == 2
    assert res_partial.completeness == "UNKNOWN"
    assert res_partial.is_complete_series is False

    # 5. Stale prior-day intraday data is excluded
    clock_now = datetime.datetime(2026, 10, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
    norm_stale = MarketDataNormalizer(clock=lambda: clock_now)
    res_stale = norm_stale.normalize_candles([
        ["2026-10-04T09:15:00+05:30", 100.0, 105.0, 95.0, 102.0, 100, 0],
        ["2026-10-04T09:20:00+05:30", 102.0, 106.0, 101.0, 105.0, 100, 0],
    ], "5m", mode="intraday")
    assert len(res_stale.candles) == 0
    assert res_stale.completeness == "INCOMPLETE"
    assert res_stale.is_complete_series is False
    assert any("Excluded stale prior-day" in w for w in res_stale.warnings)

    # 6. Quarantined conflict leaving one surviving candle preserves INCOMPLETE
    res_conflict_plus_one = normalizer.normalize_candles([
        # Two conflicting candles at 09:15
        ["2026-10-05T09:15:00+05:30", 100.0, 105.0, 95.0, 102.0, 100, 0],
        ["2026-10-05T09:15:00+05:30", 101.0, 107.0, 99.0, 103.0, 150, 0],
        # One valid candle at 09:20
        ["2026-10-05T09:20:00+05:30", 102.0, 106.0, 101.0, 105.0, 100, 0],
    ], "5m")
    assert len(res_conflict_plus_one.candles) == 1
    assert res_conflict_plus_one.completeness == "INCOMPLETE"
    assert res_conflict_plus_one.is_complete_series is False
    assert any("Quarantined conflicting candle" in w for w in res_conflict_plus_one.warnings)


def test_deterministic_provenance_fingerprinting():
    """
    Content fingerprint must be reproducible and sensitive to any modification.
    """
    normalizer = MarketDataNormalizer(
        clock=lambda: datetime.datetime(2026, 10, 5, 20, 0, 0, tzinfo=datetime.timezone.utc)
    )

    raw1 = [
        ["2026-10-05T10:00:00+00:00", 25000.0, 25020.0, 24990.0, 25010.0, 1000, 0],
        ["2026-10-05T10:05:00+00:00", 25010.0, 25030.0, 25000.0, 25020.0, 1200, 0],
    ]
    candles1, _, _ = normalizer.normalize_candles(raw1, "5m")
    fp1 = compute_market_data_fingerprint(candles1)

    # Same data -> same fingerprint
    candles2, _, _ = normalizer.normalize_candles(raw1, "5m")
    fp2 = compute_market_data_fingerprint(candles2)
    assert fp1 == fp2
    assert len(fp1) == 64

    # Altered volume -> different fingerprint
    raw3 = [
        ["2026-10-05T10:00:00+00:00", 25000.0, 25020.0, 24990.0, 25010.0, 1001, 0],
        ["2026-10-05T10:05:00+00:00", 25010.0, 25030.0, 25000.0, 25020.0, 1200, 0],
    ]
    candles3, _, _ = normalizer.normalize_candles(raw3, "5m")
    fp3 = compute_market_data_fingerprint(candles3)
    assert fp1 != fp3


def test_fingerprint_independent_of_retrieval_time_and_response_ordering():
    """
    Fingerprint must be strictly identical regardless of retrieval time and response ordering.
    """
    raw_forward = [
        ["2026-10-05T10:00:00+00:00", 25000.0, 25020.0, 24990.0, 25010.0, 1000, 0],
        ["2026-10-05T10:05:00+00:00", 25010.0, 25030.0, 25000.0, 25020.0, 1200, 0],
        ["2026-10-05T10:10:00+00:00", 25020.0, 25040.0, 25010.0, 25030.0, 1100, 0],
    ]
    raw_reverse = list(reversed(raw_forward))

    norm_t1 = MarketDataNormalizer(
        clock=lambda: datetime.datetime(2026, 10, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
    )
    norm_t2 = MarketDataNormalizer(
        clock=lambda: datetime.datetime(2026, 10, 5, 18, 30, 0, tzinfo=datetime.timezone.utc)
    )

    candles_fwd, _, _ = norm_t1.normalize_candles(raw_forward, "5m")
    candles_rev, _, _ = norm_t2.normalize_candles(raw_reverse, "5m")

    fp_fwd = compute_market_data_fingerprint(candles_fwd)
    fp_rev = compute_market_data_fingerprint(candles_rev)

    assert fp_fwd == fp_rev
