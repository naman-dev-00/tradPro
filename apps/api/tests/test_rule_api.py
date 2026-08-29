import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from src.main import app
from src.database import get_db, Base, engine
from src.models import Strategy

client = TestClient(app)

def make_api_candles(count: int, instrument_id: str = "NIFTY", timeframe: str = "15m"):
    base_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    candles = []
    for i in range(count):
        ts = base_dt + timedelta(minutes=15 * i)
        candles.append({
            "timestamp": ts.isoformat(),
            "instrument_id": instrument_id,
            "timeframe": timeframe,
            "open": 100.0 + i,
            "high": 105.0 + i,
            "low": 98.0 + i,
            "close": 102.0 + i,
            "volume": 1000.0,
            "is_closed": True
        })
    return candles


# 18. Unknown Saved Strategy ID
def test_unknown_saved_strategy_id():
    payload = {
        "strategy_id": "00000000-0000-0000-0000-000000000000",
        "reference_dataset_id": "synthetic_underlying_nifty_15m"
    }
    response = client.post("/rules/evaluate", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert "Saved strategy with ID" in data["detail"]
    assert "not found" in data["detail"]


# 19. Unexpected Request Fields
def test_unexpected_request_fields_extra_forbid():
    payload = {
        "strategy": {
            "name": "Test", "timeframe": "15m", "candidate_selection_mode": "FIRST_ELIGIBLE",
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
        },
        "reference_dataset_id": "synthetic_underlying_nifty_15m",
        "unexpected_field": "forbidden"
    }
    response = client.post("/rules/evaluate", json=payload)
    assert response.status_code == 422
    assert "Extra inputs are not permitted" in str(response.json())


# 20. Unknown Indicators and Operators
def test_unknown_indicators_and_operators():
    payload = {
        "strategy": {
            "name": "Unknown Test", "timeframe": "15m", "candidate_selection_mode": "FIRST_ELIGIBLE",
            "global_conditions": {
                "id": "c1", "type": "CONDITION",
                "lhs": {"indicator": "UNKNOWN_MAGIC_IND"},
                "operator": "MAGIC_OPERATOR",
                "rhs": {"type": "NUMBER", "value": 10.0}
            },
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
        },
        "reference_dataset_id": "synthetic_underlying_nifty_15m"
    }
    response = client.post("/rules/evaluate", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["overall_status"] == "INVALID"
    assert "Unknown indicator" in data["reference_series_result"]["reason"] or "Unknown" in data["reference_series_result"]["reason"]


# 21. Empty Series Rejection
def test_empty_series_rejection():
    payload = {
        "strategy": {
            "name": "Test", "timeframe": "15m", "candidate_selection_mode": "FIRST_ELIGIBLE",
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
        },
        "reference_candles": []
    }
    response = client.post("/rules/evaluate", json=payload)
    assert response.status_code == 400
    assert "contains no completed candles" in response.json()["detail"]


# 22. Duplicate Timestamps Rejection
def test_duplicate_timestamps_rejection():
    c1 = make_api_candles(1)[0]
    c2 = make_api_candles(1)[0] # Duplicate timestamp
    payload = {
        "strategy": {
            "name": "Test", "timeframe": "15m", "candidate_selection_mode": "FIRST_ELIGIBLE",
            "global_conditions": {"type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 10.0}},
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
        },
        "reference_candles": [c1, c2]
    }
    response = client.post("/rules/evaluate", json=payload)
    assert response.status_code == 400
    assert "Duplicate timestamp" in response.json()["detail"]


# 23. Mismatched Instrument IDs inside one series
def test_mismatched_instrument_ids():
    c1 = make_api_candles(1, instrument_id="NIFTY")[0]
    c2 = make_api_candles(2, instrument_id="BANKNIFTY")[1]
    payload = {
        "strategy": {
            "name": "Test", "timeframe": "15m", "candidate_selection_mode": "FIRST_ELIGIBLE",
            "global_conditions": {"type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 10.0}},
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
        },
        "reference_candles": [c1, c2]
    }
    response = client.post("/rules/evaluate", json=payload)
    assert response.status_code == 200 # Processed series


# 24. 5,000-Candle Limit
def test_5000_candle_limit_exceeded():
    candles = make_api_candles(5001)
    payload = {
        "strategy": {
            "name": "Test", "timeframe": "15m", "candidate_selection_mode": "FIRST_ELIGIBLE",
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
        },
        "reference_candles": candles
    }
    response = client.post("/rules/evaluate", json=payload)
    assert response.status_code == 400
    assert "exceeds maximum limit" in response.json()["detail"]


# 25. Seeded Milestone 1 Strategy Compatibility
def test_seeded_milestone_1_strategy_compatibility(session):
    app.dependency_overrides[get_db] = lambda: session
    try:
        strat_payload = {
            "name": "Seeded M1 Strategy",
            "timeframe": "15m",
            "candidate_selection_mode": "FIRST_ELIGIBLE",
            "global_conditions": {
                "type": "CONDITION",
                "lhs": {"indicator": "PRICE", "symbol": "NIFTY"},
                "operator": "GREATER_THAN",
                "rhs": {"type": "NUMBER", "value": 100.0}
            },
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100000, "stop_loss_pct": 2.5, "take_profit_pct": 5, "validity_window": 5}}
        }
        db_strat = Strategy(id="m1-seeded-uuid", name="Seeded M1 Strategy", description="Seeded test strategy", timeframe="15m", payload=strat_payload)
        session.add(db_strat)
        session.commit()

        req_payload = {
            "strategy_id": "m1-seeded-uuid",
            "reference_dataset_id": "synthetic_underlying_nifty_15m"
        }
        response = client.post("/rules/evaluate", json=req_payload)
        assert response.status_code == 200
        data = response.json()
        assert data["overall_status"] == "TRUE"
        assert data["reference_series_result"].get("condition_id") == "global.0" or data["reference_series_result"].get("group_id") == "global.0"
    finally:
        app.dependency_overrides.clear()


# 26. Stack Trace Suppression
def test_stack_trace_suppression():
    payload = {
        "strategy": {
            "name": "Test", "timeframe": "15m", "candidate_selection_mode": "FIRST_ELIGIBLE",
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
        },
        "reference_dataset_id": "synthetic_underlying_nifty_15m"
    }
    response = client.post("/rules/evaluate", json=payload)
    assert response.status_code == 200
    content = response.text
    assert "Traceback (most recent call last)" not in content
    assert "File \"" not in content
