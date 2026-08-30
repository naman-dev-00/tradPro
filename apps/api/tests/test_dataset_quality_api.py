import pytest
import json

def test_api_list_datasets_success(client):
    resp = client.get("/api/v1/data-quality/datasets")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 6
    assert "X-Request-ID" in resp.headers

    ds_ids = [d["dataset_id"] for d in data]
    assert "synthetic_underlying_nifty_15m" in ds_ids
    assert "synthetic_candidate_option_ce_23000_15m" in ds_ids

def test_api_get_single_dataset_report_success(client):
    resp = client.get("/api/v1/data-quality/datasets/synthetic_underlying_nifty_15m")
    assert resp.status_code == 200
    data = resp.json()
    assert data["dataset_id"] == "synthetic_underlying_nifty_15m"
    assert data["status"] == "PASS"
    assert data["provenance"]["is_synthetic"] is True
    assert data["provenance"]["immutable"] is True
    assert data["summary"]["checksum_matches"] is True
    assert "X-Request-ID" in resp.headers

def test_api_get_dataset_report_unknown_id_returns_404(client):
    resp = client.get("/api/v1/data-quality/datasets/non_existent_dataset")
    assert resp.status_code == 404
    err = resp.json()
    assert "not found" in err["detail"].lower()
    assert "X-Request-ID" in resp.headers

def test_api_batch_audit_preserves_request_order_and_counts(client):
    requested_ids = [
        "synthetic_candidate_option_pe_23000_15m",
        "synthetic_underlying_nifty_15m",
        "synthetic_short_insufficient_5m",
    ]
    resp = client.post("/api/v1/data-quality/audit", json={"dataset_ids": requested_ids})
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["reports"]) == 3
    # Order preserved exactly
    returned_ids = [r["dataset_id"] for r in data["reports"]]
    assert returned_ids == requested_ids

    assert data["total_datasets"] == 3
    assert data["status_counts"]["PASS"] + data["status_counts"]["WARN"] + data["status_counts"]["FAIL"] == 3
    assert data["status_counts"]["PASS"] == 1  # nifty_15m
    assert data["status_counts"]["WARN"] == 2  # pe_23000 and short_5m

def test_api_batch_audit_validation_errors(client):
    # Empty dataset_ids
    resp_empty = client.post("/api/v1/data-quality/audit", json={"dataset_ids": []})
    assert resp_empty.status_code == 422

    # Duplicates in dataset_ids
    resp_dup = client.post(
        "/api/v1/data-quality/audit",
        json={"dataset_ids": ["synthetic_underlying_nifty_15m", "synthetic_underlying_nifty_15m"]},
    )
    assert resp_dup.status_code == 422

    # Over 20 datasets
    over_20 = [f"ds_{i}" for i in range(21)]
    resp_over = client.post("/api/v1/data-quality/audit", json={"dataset_ids": over_20})
    assert resp_over.status_code == 422

    # Extra forbidden fields
    resp_extra = client.post(
        "/api/v1/data-quality/audit",
        json={"dataset_ids": ["synthetic_underlying_nifty_15m"], "forbidden": "extra"},
    )
    assert resp_extra.status_code == 422

def test_api_export_dataset_quality_report_json(client):
    resp1 = client.get("/api/v1/data-quality/datasets/synthetic_underlying_nifty_15m/export")
    assert resp1.status_code == 200
    assert resp1.headers["content-type"] == "application/json; charset=utf-8"
    assert resp1.headers["content-disposition"] == 'attachment; filename="data_quality_synthetic_underlying_nifty_15m.json"'

    resp2 = client.get("/api/v1/data-quality/datasets/synthetic_underlying_nifty_15m/export")
    assert resp2.status_code == 200

    # Byte-for-byte equality
    assert resp1.content == resp2.content
    exact_bytes_len = len(resp1.content)
    assert exact_bytes_len > 0
    assert exact_bytes_len < 5 * 1024 * 1024

    payload = resp1.json()
    assert "notice" in payload
    assert "Packaged synthetic educational data only" in payload["notice"]
    assert payload["report"]["dataset_id"] == "synthetic_underlying_nifty_15m"
    assert payload["report"]["status"] == "PASS"

    # Verify absence of dynamic timestamps, execution durations, or local filesystem paths
    content_str = resp1.content.decode("utf-8")
    assert "duration" not in content_str.lower()
    assert "c:\\" not in content_str.lower()
    assert "/users/" not in content_str.lower()

def test_api_export_unknown_dataset_returns_404(client):
    resp = client.get("/api/v1/data-quality/datasets/unknown_fixture/export")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()
