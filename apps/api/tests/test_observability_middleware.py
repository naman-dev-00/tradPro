import pytest
import uuid
import re

def test_request_id_valid_propagation(client):
    valid_id = "custom-req-id_123.ABC-test"
    resp = client.get("/health", headers={"X-Request-ID": valid_id})
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == valid_id

def test_request_id_missing_generates_valid_uuid(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    req_id = resp.headers.get("X-Request-ID")
    assert req_id is not None
    # Validate UUID format
    parsed_uuid = uuid.UUID(req_id)
    assert str(parsed_uuid) == req_id

def test_request_id_oversized_is_discarded_and_replaced(client):
    oversized_id = "a" * 65  # 65 chars > 64 limit
    resp = client.get("/health", headers={"X-Request-ID": oversized_id})
    assert resp.status_code == 200
    req_id = resp.headers.get("X-Request-ID")
    assert req_id != oversized_id
    assert uuid.UUID(req_id)

def test_request_id_crlf_and_whitespace_discarded(client):
    # CRLF header injection attempt
    malicious_id = "req-123\r\nInjected: True"
    resp = client.get("/health", headers={"X-Request-ID": malicious_id})
    assert resp.status_code == 200
    req_id = resp.headers.get("X-Request-ID")
    assert "\r" not in req_id
    assert "\n" not in req_id
    assert uuid.UUID(req_id)

def test_request_id_unicode_and_special_symbols_discarded(client):
    invalid_id = "req_123_invalid@#$*()"
    resp = client.get("/health", headers={"X-Request-ID": invalid_id})
    assert resp.status_code == 200
    req_id = resp.headers.get("X-Request-ID")
    assert req_id != invalid_id
    assert uuid.UUID(req_id)


def test_request_id_present_on_404_and_422_errors(client):
    # 404
    resp_404 = client.get("/api/v1/data-quality/datasets/unknown_id")
    assert resp_404.status_code == 404
    assert resp_404.headers.get("X-Request-ID") is not None

    # 422
    resp_422 = client.post("/api/v1/data-quality/audit", json={"dataset_ids": []})
    assert resp_422.status_code == 422
    assert resp_422.headers.get("X-Request-ID") is not None

def test_observability_preserves_existing_routes(client):
    resp_health = client.get("/health")
    assert resp_health.status_code == 200
    assert resp_health.json()["status"] == "healthy"

    resp_ind = client.get("/indicators/supported")
    assert resp_ind.status_code == 200
    assert "indicators" in resp_ind.json()
