import os
import pytest
from src.engine.csv_loader import load_candles_from_csv

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "fixtures")

def test_load_underlying_nifty_csv():
    file_path = os.path.join(FIXTURES_DIR, "synthetic_underlying_nifty_15m.csv")
    candles = load_candles_from_csv(file_path)

    assert len(candles) == 35
    assert candles[0].instrument_id == "NIFTY"
    assert candles[0].timeframe == "15m"
    assert candles[0].close == 22040.0
    assert all(c.is_closed for c in candles)

def test_load_option_candidates_csv():
    ce_path = os.path.join(FIXTURES_DIR, "synthetic_candidate_option_ce_23000_15m.csv")
    pe_path = os.path.join(FIXTURES_DIR, "synthetic_candidate_option_pe_23000_15m.csv")
    ce2_path = os.path.join(FIXTURES_DIR, "synthetic_candidate_option_ce_23500_15m.csv")

    ce_candles = load_candles_from_csv(ce_path)
    pe_candles = load_candles_from_csv(pe_path)
    ce2_candles = load_candles_from_csv(ce2_path)

    assert len(ce_candles) == 10
    assert len(pe_candles) == 10
    assert len(ce2_candles) == 10
    assert ce_candles[0].instrument_id == "NIFTY_23000_CE"
    assert pe_candles[0].instrument_id == "NIFTY_23000_PE"
    assert ce2_candles[0].instrument_id == "NIFTY_23500_CE"

def test_load_incomplete_candle_csv():
    file_path = os.path.join(FIXTURES_DIR, "synthetic_with_incomplete_candle_15m.csv")
    candles = load_candles_from_csv(file_path)

    # Total 10 rows in CSV, 1 is_closed=false => load_candles_from_csv returns 9 completed candles
    assert len(candles) == 9
    assert all(c.is_closed for c in candles)

def test_load_short_insufficient_csv():
    file_path = os.path.join(FIXTURES_DIR, "synthetic_short_insufficient_5m.csv")
    candles = load_candles_from_csv(file_path)
    assert len(candles) == 3
