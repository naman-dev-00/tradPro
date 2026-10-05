import datetime
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from src.engine.paper.units import decimal_to_units
from .contracts import (
    SUPPORTED_MARKET_DATA_TIMEFRAMES,
    TIMEFRAME_TO_SECONDS,
    PRICE_SCALE,
    MarketDataCandle,
    MarketDataValidationError,
)

logger = logging.getLogger("tradepro.market_data_normalizer")

MAX_WARNINGS_COUNT = 50
EXCHANGE_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MAX_PRICE = Decimal("1000000000")  # 10^9
MAX_VOLUME = Decimal("100000000000000")  # 10^14


class NormalizeResult(tuple):
    """
    Tuple containing (candles, warnings, is_complete_series) with completeness attribute.
    Unpacks as 3 elements for complete backwards-compatibility with existing call sites.
    """
    def __new__(cls, candles: List[MarketDataCandle], warnings: List[str], is_complete_series: bool, completeness: str = "UNKNOWN"):
        return super().__new__(cls, (candles, warnings, is_complete_series))

    def __init__(self, candles: List[MarketDataCandle], warnings: List[str], is_complete_series: bool, completeness: str = "UNKNOWN"):
        self.candles = candles
        self.warnings = warnings
        self.is_complete_series = is_complete_series
        self.completeness = completeness


class MarketDataNormalizer:
    """
    Normalizes provider candle payloads into canonical, UTC-normalized,
    scaled-integer MarketDataCandles.
    Enforces exact numeric parsing without silent rounding, strict geometry validation,
    timeframe interval alignment, clock-injected candle completion filtering,
    quarantining of conflicting revisions, and honest completeness reporting.
    """

    def __init__(
        self,
        clock: Optional[Callable[[], datetime.datetime]] = None,
        scale: int = PRICE_SCALE,
    ):
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.scale = scale

    def normalize_candles(
        self,
        raw_candles: Sequence[Any],
        timeframe: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        mode: str = "historical",
    ) -> NormalizeResult:
        """
        Normalizes a raw list of candles from Upstox.
        Returns:
            NormalizeResult: 3-tuple (normalized_candles, warnings, is_complete_series) with .completeness
        """
        if timeframe not in SUPPORTED_MARKET_DATA_TIMEFRAMES:
            raise MarketDataValidationError(
                f"Unsupported timeframe '{timeframe}'. Supported: {SUPPORTED_MARKET_DATA_TIMEFRAMES}"
            )

        interval_seconds = TIMEFRAME_TO_SECONDS[timeframe]
        interval_delta = datetime.timedelta(seconds=interval_seconds)
        now_utc = self._get_clock_now_utc()
        current_exchange_date = now_utc.astimezone(EXCHANGE_TZ).date()

        warnings: List[str] = []
        parsed_candidates: List[MarketDataCandle] = []

        # Optional date boundary parsing
        d_from = datetime.date.fromisoformat(from_date) if from_date else None
        d_to = datetime.date.fromisoformat(to_date) if to_date else None

        for idx, item in enumerate(raw_candles):
            candle = self._parse_single_candle(item, idx, timeframe)

            # Intraday mode: validate candle against current exchange session date
            candle_exchange_date = candle.timestamp.astimezone(EXCHANGE_TZ).date()
            if mode == "intraday":
                if candle_exchange_date != current_exchange_date:
                    warnings.append(
                        f"Excluded stale prior-day or future candle at {candle.timestamp.isoformat()} "
                        f"(candle session date {candle_exchange_date} does not match current session date {current_exchange_date})"
                    )
                    continue

            # Historical mode: Date range filtering if requested
            c_date = candle.timestamp.date()
            if d_from and c_date < d_from:
                warnings.append(f"Excluded out-of-range historical candle before from_date: {candle.timestamp.isoformat()}")
                continue
            if d_to and c_date > d_to:
                warnings.append(f"Excluded out-of-range historical candle after to_date: {candle.timestamp.isoformat()}")
                continue

            # Exclude unfinished or future candles using injectable clock
            candle_end = candle.timestamp + interval_delta
            if candle_end > now_utc:
                warnings.append(
                    f"Excluded in-progress or future candle at {candle.timestamp.isoformat()} "
                    f"(closes at {candle_end.isoformat()}, clock is {now_utc.isoformat()})"
                )
                continue

            parsed_candidates.append(candle)

        if not parsed_candidates:
            warnings.append("Empty candle series received from provider or all candles were excluded.")
            return NormalizeResult([], warnings[:MAX_WARNINGS_COUNT], False, "INCOMPLETE")

        # Deterministic chronological sort
        parsed_candidates.sort(key=lambda c: (c.timestamp, c.open_units, c.high_units, c.low_units, c.close_units, c.volume))

        # Group by timestamp to detect identical duplicates vs conflicting revisions
        grouped: Dict[datetime.datetime, List[MarketDataCandle]] = {}
        for cand in parsed_candidates:
            grouped.setdefault(cand.timestamp, []).append(cand)

        deduped: List[MarketDataCandle] = []
        has_conflict = False

        for ts in sorted(grouped.keys()):
            c_list = grouped[ts]
            if len(c_list) == 1:
                deduped.append(c_list[0])
            else:
                first = c_list[0]
                all_identical = all(
                    c.open_units == first.open_units
                    and c.high_units == first.high_units
                    and c.low_units == first.low_units
                    and c.close_units == first.close_units
                    and c.volume == first.volume
                    for c in c_list
                )
                if all_identical:
                    deduped.append(first)
                    warnings.append(f"Duplicate identical candle dropped at {ts.isoformat()}")
                else:
                    # Conflicting revision for same timestamp: QUARANTINE!
                    # Do not invent an authoritative candle.
                    has_conflict = True
                    warnings.append(
                        f"Quarantined conflicting candle interval at {ts.isoformat()}: "
                        f"multiple conflicting revisions ({len(c_list)} candles) detected without provider revision provenance. "
                        f"Interval excluded from series."
                    )

        # Completeness and gap auditing
        if not deduped:
            warnings.append("All candles were excluded or quarantined due to revision conflicts.")
            return NormalizeResult([], warnings[:MAX_WARNINGS_COUNT], False, "INCOMPLETE")

        has_gap = False
        has_session_boundary = False

        for i in range(len(deduped) - 1):
            curr_ts = deduped[i].timestamp
            next_ts = deduped[i + 1].timestamp
            delta = next_ts - curr_ts

            if delta < interval_delta:
                warnings.append(
                    f"Overlapping or invalid interval detected between {curr_ts.isoformat()} and {next_ts.isoformat()} ({delta.total_seconds():.0f}s)"
                )
                has_gap = True
            elif delta > interval_delta:
                if curr_ts.date() != next_ts.date():
                    warnings.append(
                        f"Inter-session boundary or gap detected across dates: {curr_ts.date()} to {next_ts.date()} "
                        f"({delta.total_seconds():.0f}s); series completeness cannot be established without exchange trading calendar"
                    )
                    has_session_boundary = True
                else:
                    warnings.append(
                        f"Intraday data gap detected: {delta.total_seconds():.0f}s between {curr_ts.isoformat()} "
                        f"and {next_ts.isoformat()} exceeds expected {interval_seconds}s interval"
                    )
                    has_gap = True

        # Check requested date range boundary coverage if applicable
        has_boundary_gap = False
        if d_from and deduped[0].timestamp.date() > d_from:
            warnings.append(f"Historical series begins at {deduped[0].timestamp.date()}, missing start date {d_from}")
            has_boundary_gap = True
        if d_to and deduped[-1].timestamp.date() < d_to:
            warnings.append(f"Historical series ends at {deduped[-1].timestamp.date()}, missing end date {d_to}")
            has_boundary_gap = True

        # Classify final completeness
        # CRITICAL SAFETY:
        # - Any confirmed conflict, internal gap, or boundary gap -> INCOMPLETE, is_complete_series=False
        #   (Even if quarantine leaves only one surviving candle, INCOMPLETE is preserved!)
        # - If no conflict/gaps detected, but calendar/session coverage cannot be proven -> UNKNOWN, is_complete_series=False
        # - Two consecutive candles or a partial contiguous day do not establish complete coverage of a requested day.
        # - COMPLETE is never inferred merely from consecutive timestamps without calendar evidence.
        if has_conflict or has_gap or has_boundary_gap:
            completeness = "INCOMPLETE"
            is_complete_series = False
        else:
            if len(deduped) == 1:
                warnings.append("Single candle received; range coverage cannot be established without exchange trading session context.")
            else:
                warnings.append("No internal gaps detected, but session/range coverage cannot be verified without exchange trading calendar context.")
            completeness = "UNKNOWN"
            is_complete_series = False

        return NormalizeResult(deduped, warnings[:MAX_WARNINGS_COUNT], is_complete_series, completeness)

    def _get_clock_now_utc(self) -> datetime.datetime:
        now = self.clock()
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            return now.replace(tzinfo=datetime.timezone.utc)
        return now.astimezone(datetime.timezone.utc)

    def _parse_single_candle(self, item: Any, index: int, timeframe: str) -> MarketDataCandle:
        """
        Parses a raw Upstox candle item:
        [timestamp_str, open, high, low, close, volume, open_interest]
        Enforces bounded error diagnostics without echoing raw provider content.
        Enforces exact numeric validation without silent rounding.
        """
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            raise MarketDataValidationError(
                f"Malformed candle record at index {index}: expected list of at least 6 elements."
            )

        ts_raw, o_raw, h_raw, l_raw, c_raw, v_raw = item[0], item[1], item[2], item[3], item[4], item[5]

        # 1. Parse and validate timestamp
        if not isinstance(ts_raw, str):
            raise MarketDataValidationError(
                f"Invalid timestamp type at index {index}: expected ISO-8601 string."
            )

        try:
            ts = datetime.datetime.fromisoformat(ts_raw)
        except (ValueError, TypeError) as e:
            # Bounded error message without echoing raw provider timestamp content or parser exception
            raise MarketDataValidationError(
                f"Unparseable ISO-8601 timestamp at index {index}: invalid datetime format."
            ) from e

        if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
            raise MarketDataValidationError(
                f"Naive timestamp rejected at index {index}: timestamps must be timezone-aware."
            )

        ts_utc = ts.astimezone(datetime.timezone.utc)

        # Enforce timeframe interval alignment
        if timeframe == "5m":
            if ts_utc.minute % 5 != 0 or ts_utc.second != 0 or ts_utc.microsecond != 0:
                raise MarketDataValidationError(
                    f"Candle timestamp at index {index} is not aligned to 5m interval boundary."
                )
        elif timeframe == "15m":
            if ts_utc.minute % 15 != 0 or ts_utc.second != 0 or ts_utc.microsecond != 0:
                raise MarketDataValidationError(
                    f"Candle timestamp at index {index} is not aligned to 15m interval boundary."
                )

        # 2. Strict exact price parsing (no silent rounding, bounded diagnostics)
        quantizer = Decimal("10") ** (-self.scale)

        def parse_strict_price(val: Any, field_name: str) -> Decimal:
            if isinstance(val, bool):
                raise MarketDataValidationError(f"Boolean value rejected for {field_name} at index {index}.")
            if isinstance(val, (int, float, str, Decimal)):
                try:
                    d = Decimal(str(val))
                except (InvalidOperation, ValueError, TypeError) as e:
                    raise MarketDataValidationError(f"Invalid numeric value for {field_name} at index {index}.") from e
            else:
                raise MarketDataValidationError(f"Unsupported type for {field_name} at index {index}.")

            if not d.is_finite():
                raise MarketDataValidationError(f"Non-finite value rejected for {field_name} at index {index}.")

            if d <= Decimal("0"):
                raise MarketDataValidationError(f"Price must be strictly positive for {field_name} at index {index}.")

            if d > MAX_PRICE:
                raise MarketDataValidationError(f"Price exceeds maximum supported limit for {field_name} at index {index}.")

            # Disallow silent rounding: value must fit exactly within self.scale
            if d != d.quantize(quantizer):
                raise MarketDataValidationError(
                    f"Unsupported price precision for {field_name} at index {index}: exceeds {self.scale} decimal places. Silent rounding is prohibited."
                )

            return d.quantize(quantizer)

        o_dec = parse_strict_price(o_raw, "open")
        h_dec = parse_strict_price(h_raw, "high")
        l_dec = parse_strict_price(l_raw, "low")
        c_dec = parse_strict_price(c_raw, "close")

        # 3. Strict volume parsing (integer, non-negative, no booleans, no fractions, bounded checks before int conversion)
        def parse_strict_volume(val: Any) -> int:
            if isinstance(val, bool):
                raise MarketDataValidationError(f"Boolean value rejected for volume at index {index}.")
            if isinstance(val, (int, float, str, Decimal)):
                try:
                    d = Decimal(str(val))
                except (InvalidOperation, ValueError, TypeError) as e:
                    raise MarketDataValidationError(f"Invalid numeric value for volume at index {index}.") from e
            else:
                raise MarketDataValidationError(f"Unsupported type for volume at index {index}.")

            if not d.is_finite():
                raise MarketDataValidationError(f"Non-finite value rejected for volume at index {index}.")

            if d < Decimal("0"):
                raise MarketDataValidationError(f"Negative volume rejected at index {index}.")

            # Bounded check on Decimal directly before converting to int or expanding digits
            if d > MAX_VOLUME:
                raise MarketDataValidationError(f"Volume exceeds maximum supported bound at index {index}.")

            if d != d.to_integral_value():
                raise MarketDataValidationError(f"Fractional volume rejected at index {index}: volume must be an integer.")

            return int(d)

        vol_int = parse_strict_volume(v_raw)

        # 4. Strict OHLC Geometry checks with exact values (bounded diagnostics)
        if h_dec < max(o_dec, c_dec, l_dec):
            raise MarketDataValidationError(
                f"Invalid OHLC geometry at index {index}: high price is lower than open, close, or low."
            )

        if l_dec > min(o_dec, c_dec, h_dec):
            raise MarketDataValidationError(
                f"Invalid OHLC geometry at index {index}: low price is higher than open, close, or high."
            )

        # 5. Scaled integer conversion
        o_units = decimal_to_units(o_dec, self.scale)
        h_units = decimal_to_units(h_dec, self.scale)
        l_units = decimal_to_units(l_dec, self.scale)
        c_units = decimal_to_units(c_dec, self.scale)

        return MarketDataCandle(
            timestamp=ts_utc,
            open=o_dec,
            high=h_dec,
            low=l_dec,
            close=c_dec,
            open_units=o_units,
            high_units=h_units,
            low_units=l_units,
            close_units=c_units,
            volume=vol_int,
            is_closed=True,
        )
