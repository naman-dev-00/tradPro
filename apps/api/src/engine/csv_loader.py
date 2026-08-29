import csv
from datetime import datetime, timezone
from typing import List
from src.engine.models import Candle
from src.engine.indicators import preprocess_candle_series

def load_candles_from_csv(file_path: str) -> List[Candle]:
    """
    Parses a CSV file containing synthetic educational OHLCV candle data,
    normalizes timestamps to UTC, validates each row against the Candle domain model,
    and preprocesses the series (filtering incomplete candles & checking chronological order).
    """
    raw_candles: List[Candle] = []

    with open(file_path, mode="r", encoding="utf-8") as f:
        # Skip top comment lines starting with '#'
        lines = [line for line in f if not line.strip().startswith("#")]
        reader = csv.DictReader(lines)

        for row in reader:
            ts_str = row["timestamp"].strip()
            # Handle ISO string parsing & timezone enforcement
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)

            is_closed_val = str(row.get("is_closed", "true")).strip().lower() in ("true", "1", "yes")

            c = Candle(
                timestamp=dt,
                instrument_id=row["instrument_id"].strip(),
                timeframe=row["timeframe"].strip(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                is_closed=is_closed_val
            )
            raw_candles.append(c)

    return preprocess_candle_series(raw_candles)
