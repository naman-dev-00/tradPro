import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from src.main import app
from src.database import get_db
from src.models import Strategy

client = TestClient(app)

SAMPLE_STRATEGY = {
    "name": "API Multi Series Test",
    "timeframe": "15m",
    "candidate_selection_mode": "FIRST_ELIGIBLE",
    "global_conditions": {
        "id": "c1", "type": "CONDITION",
        "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 100.0}
    },
    "candidate_conditions": {
        "id": "c2", "type": "CONDITION",
        "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 10.0}
    },
    "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
}


def test_get_multi_series_datasets():
    response = client.get("/multi-series/datasets")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 5
    ref_entries = [d for d in data if d["category"] == "REFERENCE"]
    subj_entries = [d for d in data if d["category"] == "SUBJECT"]
    assert len(ref_entries) >= 1
    assert len(subj_entries) >= 4


def test_post_evaluate_multi_series_success():
    payload = {
        "strategy": SAMPLE_STRATEGY,
        "reference_dataset_id": "synthetic_underlying_nifty_15m",
        "subject_dataset_ids": [
            "synthetic_candidate_option_ce_23000_15m",
            "synthetic_candidate_option_pe_23000_15m"
        ],
        "eval_timestamp": "2026-08-28T17:45:00.000Z"
    }
    response = client.post("/multi-series/evaluate", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["reference_dataset_id"] == "synthetic_underlying_nifty_15m"
    assert data["total_series_evaluated"] == 2
    assert len(data["results"]) == 2
    assert "status_counts" in data
    assert sum(data["status_counts"].values()) == 2


def test_post_evaluate_unknown_strategy_id():
    payload = {
        "strategy_id": "00000000-0000-0000-0000-000000000000",
        "reference_dataset_id": "synthetic_underlying_nifty_15m",
        "subject_dataset_ids": ["synthetic_candidate_option_ce_23000_15m"],
        "eval_timestamp": "2026-08-28T17:45:00.000Z"
    }
    response = client.post("/multi-series/evaluate", json=payload)
    assert response.status_code == 400
    assert "not found" in response.json()["detail"]


def test_post_evaluate_unexpected_fields_forbid():
    payload = {
        "strategy": SAMPLE_STRATEGY,
        "reference_dataset_id": "synthetic_underlying_nifty_15m",
        "subject_dataset_ids": ["synthetic_candidate_option_ce_23000_15m"],
        "eval_timestamp": "2026-08-28T17:45:00.000Z",
        "forbidden_extra_field": "error"
    }
    response = client.post("/multi-series/evaluate", json=payload)
    assert response.status_code == 422
    assert "Extra inputs are not permitted" in str(response.json())


def test_post_evaluate_stack_trace_suppression():
    payload = {
        "strategy": SAMPLE_STRATEGY,
        "reference_dataset_id": "non_existent_ref_dataset",
        "subject_dataset_ids": ["synthetic_candidate_option_ce_23000_15m"],
        "eval_timestamp": "2026-08-28T17:45:00.000Z"
    }
    response = client.post("/multi-series/evaluate", json=payload)
    assert response.status_code == 400
    content = response.text
    assert "Traceback (most recent call last)" not in content
    assert "File \"" not in content
