import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)

def make_api_candles(count: int):
    base_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    candles = []
    for i in range(count):
        ts = base_dt + timedelta(minutes=15 * i)
        candles.append({
            "timestamp": ts.isoformat(),
            "instrument_id": "NIFTY",
            "timeframe": "15m",
            "open": 100.0 + i,
            "high": 105.0 + i,
            "low": 98.0 + i,
            "close": 102.0 + i,
            "volume": 1000.0,
            "is_closed": True
        })
    return candles


def test_get_supported_indicators():
    response = client.get("/indicators/supported")
    assert response.status_code == 200
    data = response.json()
    assert "indicators" in data
    names = [item["name"] for item in data["indicators"]]
    assert "PRICE" in names
    assert "SMA" in names
    assert "EMA" in names
    assert "RSI" in names
    assert "MACD" in names
    assert "PIVOT" in names
    assert "VOLUME" in names
    assert "AVERAGE_VOLUME" in names


def test_get_synthetic_datasets():
    response = client.get("/indicators/datasets")
    assert response.status_code == 200
    data = response.json()
    assert "datasets" in data
    ids = [d["id"] for d in data["datasets"]]
    assert "synthetic_underlying_nifty_15m" in ids
    assert "synthetic_candidate_option_ce_23000_15m" in ids


def test_get_synthetic_dataset_detail_success():
    response = client.get("/indicators/datasets/synthetic_underlying_nifty_15m")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "synthetic_underlying_nifty_15m"
    assert data["instrument_id"] == "NIFTY"
    assert data["completed_candles"] == 35
    assert len(data["candles"]) == 35


def test_get_synthetic_dataset_detail_incomplete_candle_filtering():
    response = client.get("/indicators/datasets/synthetic_with_incomplete_candle_15m")
    assert response.status_code == 200
    data = response.json()
    assert data["total_candles"] == 10
    assert data["completed_candles"] == 9
    assert data["excluded_incomplete_candles"] == 1


def test_get_synthetic_dataset_detail_not_found():
    response = client.get("/indicators/datasets/unknown_non_existent_dataset")
    assert response.status_code == 404
    data = response.json()
    assert "not found" in data["detail"]


def test_post_calculate_indicator_success():
    candles = make_api_candles(25)
    payload = {
        "candles": candles,
        "indicator": "SMA",
        "params": {"period": 20}
    }
    response = client.post("/indicators/calculate", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["indicator"] == "SMA"
    assert len(data["results"]) == 25
    assert data["results"][-1]["available"] is True
    assert data["results"][-1]["value"] is not None


def test_post_calculate_indicator_max_limit_exceeded():
    candles = make_api_candles(5001)
    payload = {
        "candles": candles,
        "indicator": "SMA",
        "params": {"period": 20}
    }
    response = client.post("/indicators/calculate", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert "exceeds maximum limit" in data["detail"]


def test_post_calculate_unknown_indicator():
    candles = make_api_candles(5)
    payload = {
        "candles": candles,
        "indicator": "SUPER_INDICATOR",
        "params": {}
    }
    response = client.post("/indicators/calculate", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert "Unknown indicator" in data["detail"]


def test_post_calculate_unexpected_parameter():
    candles = make_api_candles(5)
    payload = {
        "candles": candles,
        "indicator": "SMA",
        "params": {"period": 20, "unexpected_param": True}
    }
    response = client.post("/indicators/calculate", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert "Unexpected parameters" in data["detail"]


def test_post_calculate_no_stack_trace_leak():
    # Pass invalid candle OHLC
    candles = make_api_candles(2)
    candles[0]["high"] = 50.0  # High < Low/Open
    payload = {
        "candles": candles,
        "indicator": "SMA",
        "params": {"period": 20}
    }
    response = client.post("/indicators/calculate", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert "Invalid candle" in data["detail"]
    # Verify no file path or stack trace in error
    assert "Traceback" not in data["detail"]
    assert "src/engine" not in data["detail"]
