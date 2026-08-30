import pytest
from datetime import datetime, timezone
from src.models import InspectionRun
from src.engine.fingerprint import compute_request_fingerprint
from src.engine.manifest import get_manifest_checksums_snapshot

def test_api_verify_replay_run_success(client, session, test_user):
    checksums = get_manifest_checksums_snapshot()
    strat_dict = {"name": "API Test Strat"}
    start = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    end = datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc)
    ref = "synthetic_underlying_nifty_15m"
    subjs = ["synthetic_candidate_option_ce_23000_15m"]

    fp = compute_request_fingerprint(strat_dict, ref, subjs, start, end, 1)

    run = InspectionRun(
        owner_id=test_user.id,
        id="run-api-verify-1",
        status="COMPLETED",
        run_type="HISTORICAL_REPLAY",
        reference_dataset_id=ref,
        subject_dataset_ids=subjs,
        requested_start_timestamp=start,
        requested_end_timestamp=end,
        timeframe="15m",
        engine_version="1.0.0",
        manifest_version="1.0.0",
        result_payload={"sampling_step": 1, "replay_schema_version": "1.0.0", "replay_points": []},
        manifest_checksums_snapshot=checksums,
        strategy_definition_snapshot=strat_dict,
        request_fingerprint=fp,
        completed_fingerprint=fp,
        created_at=datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc),
        completed_at=datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc),
        synthetic_data_confirmed=True,
    )

    session.add(run)
    session.commit()

    resp = client.get("/api/v1/replays/run-api-verify-1/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["verification_status"] == "VERIFIED"
    assert data["fingerprint_matches"] is True
    assert data["engine_version_matches"] is True

def test_api_verify_replay_run_not_found(client):
    resp = client.get("/api/v1/replays/unknown-run-id/verify")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]

def test_api_compare_replays_success(client, session, test_user):
    checksums = get_manifest_checksums_snapshot()
    strat_dict = {"name": "API Test Strat"}
    start = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    end = datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc)
    ref = "synthetic_underlying_nifty_15m"
    subjs = ["synthetic_candidate_option_ce_23000_15m"]

    run1 = InspectionRun(
        owner_id=test_user.id,
        id="run-cmp-base",
        status="COMPLETED",
        run_type="HISTORICAL_REPLAY",
        reference_dataset_id=ref,
        subject_dataset_ids=subjs,
        requested_start_timestamp=start,
        requested_end_timestamp=end,
        timeframe="15m",
        engine_version="1.0.0",
        manifest_version="1.0.0",
        result_payload={
            "sampling_step": 1,
            "replay_schema_version": "1.0.0",
            "replay_points": [
                {
                    "evaluation_timestamp": "2026-08-28T09:15:00Z",
                    "results": [{"dataset_id": "synthetic_candidate_option_ce_23000_15m", "overall_status": "TRUE"}],
                }
            ],
        },
        manifest_checksums_snapshot=checksums,
        strategy_definition_snapshot=strat_dict,
        request_fingerprint="fp1",
        completed_fingerprint="fp1",
        created_at=datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc),
        completed_at=datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc),
        synthetic_data_confirmed=True,
    )

    run2 = InspectionRun(
        owner_id=test_user.id,
        id="run-cmp-target",
        status="COMPLETED",
        run_type="HISTORICAL_REPLAY",
        reference_dataset_id=ref,
        subject_dataset_ids=subjs,
        requested_start_timestamp=start,
        requested_end_timestamp=end,
        timeframe="15m",
        engine_version="1.0.0",
        manifest_version="1.0.0",
        result_payload={
            "sampling_step": 1,
            "replay_schema_version": "1.0.0",
            "replay_points": [
                {
                    "evaluation_timestamp": "2026-08-28T09:15:00Z",
                    "results": [{"dataset_id": "synthetic_candidate_option_ce_23000_15m", "overall_status": "FALSE"}],
                }
            ],
        },
        manifest_checksums_snapshot=checksums,
        strategy_definition_snapshot=strat_dict,
        request_fingerprint="fp2",
        completed_fingerprint="fp2",
        created_at=datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc),
        completed_at=datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc),
        synthetic_data_confirmed=True,
    )

    session.add_all([run1, run2])
    session.commit()

    payload = {
        "baseline_run_id": "run-cmp-base",
        "comparison_run_id": "run-cmp-target",
        "include_unchanged": False,
    }

    resp = client.post("/api/v1/replays/compare", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["aligned_point_count"] == 1
    assert data["changed_point_count"] == 1
    assert data["status_transition_counts"]["TRUE -> FALSE"] == 1
    assert len(data["differences"]) == 1

def test_api_compare_replays_extra_fields_forbidden(client):
    payload = {
        "baseline_run_id": "run-1",
        "comparison_run_id": "run-2",
        "forbidden_extra_field": "hacked",
    }
    resp = client.post("/api/v1/replays/compare", json=payload)
    assert resp.status_code == 422

def test_api_compare_same_run_rejected(client):
    payload = {
        "baseline_run_id": "run-1",
        "comparison_run_id": "run-1",
    }
    resp = client.post("/api/v1/replays/compare", json=payload)
    assert resp.status_code == 400
    assert "Cannot compare" in resp.json()["detail"]

def test_api_export_query_format(client, session, test_user):
    run = InspectionRun(
        owner_id=test_user.id,
        id="run-export-fmt-1",
        status="COMPLETED",
        run_type="HISTORICAL_REPLAY",
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        timeframe="15m",
        engine_version="1.0.0",
        manifest_version="1.0.0",
        result_payload={"sampling_step": 1, "replay_schema_version": "1.0.0", "replay_points": []},
        manifest_checksums_snapshot=get_manifest_checksums_snapshot(),
        strategy_definition_snapshot={"name": "Test Strat"},
        request_fingerprint="fp1",
        completed_fingerprint="fp1_export_test",
        requested_start_timestamp=datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc),
        requested_end_timestamp=datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc),
        created_at=datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc),
        completed_at=datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc),
        synthetic_data_confirmed=True,
    )

    session.add(run)
    session.commit()

    resp_json = client.get("/api/v1/replays/run-export-fmt-1/export?format=json")
    assert resp_json.status_code == 200
    assert resp_json.headers["content-type"].startswith("application/json")

    resp_csv = client.get("/api/v1/replays/run-export-fmt-1/export?format=csv")
    assert resp_csv.status_code == 200
    assert resp_csv.headers["content-type"].startswith("text/csv")
