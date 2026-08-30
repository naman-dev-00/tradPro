import pytest
from datetime import datetime, timezone
from src.models import InspectionRun
from src.routes.replays import sanitize_csv_cell

def make_sample_strategy():
    return {
        "name": "Persistence Test Strategy",
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

def make_sample_replay_payload():
    return {
        "strategy_payload": make_sample_strategy(),
        "reference_dataset_id": "synthetic_underlying_nifty_15m",
        "subject_dataset_ids": [
            "synthetic_candidate_option_ce_23000_15m",
            "synthetic_candidate_option_pe_23000_15m"
        ],
        "start_timestamp": "2026-08-28T09:15:00.000Z",
        "end_timestamp": "2026-08-28T12:00:00.000Z",
        "sampling_step": 1,
    }

def test_post_create_historical_replay_and_deduplication(client, session):
    payload = make_sample_replay_payload()

    # First request -> 201 Created
    response1 = client.post("/api/v1/replays", json=payload)
    assert response1.status_code == 201
    data1 = response1.json()
    assert data1["is_reused"] is False
    run_id1 = data1["run_id"]

    # Duplicate request -> 200 OK (reused existing run)
    response2 = client.post("/api/v1/replays", json=payload)
    assert response2.status_code == 200
    data2 = response2.json()
    assert data2["is_reused"] is True
    assert data2["run_id"] == run_id1

def test_get_list_inspection_runs_pagination_and_ordering(client, session):
    payload = make_sample_replay_payload()
    client.post("/api/v1/replays", json=payload)

    response = client.get("/api/v1/replays?page=1&page_size=10")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert data["page"] == 1
    assert data["page_size"] == 10
    assert len(data["items"]) >= 1

def test_get_inspection_run_detail_success_and_not_found(client, session):
    payload = make_sample_replay_payload()
    create_res = client.post("/api/v1/replays", json=payload)
    assert create_res.status_code in (200, 201)
    run_id = create_res.json()["run_id"]

    detail_res = client.get(f"/api/v1/replays/{run_id}")
    assert detail_res.status_code == 200
    data = detail_res.json()
    assert data["id"] == run_id
    assert "reproducibility" in data

    unknown_res = client.get("/api/v1/replays/non-existent-uuid-9999")
    assert unknown_res.status_code == 404
    assert "not found" in unknown_res.json()["detail"]

def test_get_inspection_run_reproducibility(client, session):
    payload = make_sample_replay_payload()
    create_res = client.post("/api/v1/replays", json=payload)
    assert create_res.status_code in (200, 201)
    run_id = create_res.json()["run_id"]

    repro_res = client.get(f"/api/v1/replays/{run_id}/reproducibility")
    assert repro_res.status_code == 200
    data = repro_res.json()
    assert "is_exact_match" in data
    assert data["is_exact_match"] is True

def test_export_json_and_csv_with_formula_injection_escaping(client, session):
    payload = make_sample_replay_payload()
    create_res = client.post("/api/v1/replays", json=payload)
    assert create_res.status_code in (200, 201)
    run_id = create_res.json()["run_id"]

    # 1. JSON Export
    json_res = client.get(f"/api/v1/replays/{run_id}/export.json")
    assert json_res.status_code == 200
    assert "application/json" in json_res.headers["content-type"]
    assert f'filename="replay_{run_id}.json"' in json_res.headers["content-disposition"]

    # 2. CSV Export
    csv_res = client.get(f"/api/v1/replays/{run_id}/export.csv")
    assert csv_res.status_code == 200
    assert "text/csv" in csv_res.headers["content-type"]
    assert f'filename="replay_{run_id}.csv"' in csv_res.headers["content-disposition"]
    csv_text = csv_res.text
    assert "evaluation_timestamp,dataset_id,status" in csv_text

def test_csv_formula_injection_sanitization():
    assert sanitize_csv_cell("=1+1") == "'=1+1"
    assert sanitize_csv_cell("+cmd|' /C calc'!A0") == "'+cmd|' /C calc'!A0"
    assert sanitize_csv_cell("-1+1") == "'-1+1"
    assert sanitize_csv_cell("@SUM(A1:A10)") == "'@SUM(A1:A10)"
    assert sanitize_csv_cell("  =cmd.exe") == "'  =cmd.exe"
    assert sanitize_csv_cell("Normal Text") == "Normal Text"

def test_different_subject_order_produces_different_fingerprints_and_runs(client, session):
    strat = make_sample_strategy()
    payload_order1 = {
        "strategy_payload": strat,
        "reference_dataset_id": "synthetic_underlying_nifty_15m",
        "subject_dataset_ids": [
            "synthetic_candidate_option_ce_23000_15m",
            "synthetic_candidate_option_pe_23000_15m"
        ],
        "start_timestamp": "2026-08-28T09:15:00.000Z",
        "end_timestamp": "2026-08-28T12:00:00.000Z",
        "sampling_step": 1,
    }
    payload_order2 = {
        "strategy_payload": strat,
        "reference_dataset_id": "synthetic_underlying_nifty_15m",
        "subject_dataset_ids": [
            "synthetic_candidate_option_pe_23000_15m",
            "synthetic_candidate_option_ce_23000_15m"
        ],
        "start_timestamp": "2026-08-28T09:15:00.000Z",
        "end_timestamp": "2026-08-28T12:00:00.000Z",
        "sampling_step": 1,
    }

    res1 = client.post("/api/v1/replays", json=payload_order1)
    assert res1.status_code == 201
    data1 = res1.json()
    assert data1["is_reused"] is False

    res2 = client.post("/api/v1/replays", json=payload_order2)
    assert res2.status_code == 201
    data2 = res2.json()
    assert data2["is_reused"] is False
    assert data1["run_id"] != data2["run_id"]

def test_failed_run_does_not_block_retry(client, session, test_user):
    strat = make_sample_strategy()
    payload = {
        "strategy_payload": strat,
        "reference_dataset_id": "synthetic_underlying_nifty_15m",
        "subject_dataset_ids": ["synthetic_candidate_option_ce_23000_15m"],
        "start_timestamp": "2026-08-28T09:15:00.000Z",
        "end_timestamp": "2026-08-28T12:00:00.000Z",
        "sampling_step": 1,
    }

    # Manually insert a FAILED record with completed_fingerprint=None
    failed_run = InspectionRun(
        owner_id=test_user.id,
        run_type="HISTORICAL_REPLAY",
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        timeframe="15m",
        created_at=datetime.now(timezone.utc),
        completed_at=None,
        status="FAILED",
        failure_summary="Simulated execution error",
        result_payload=None,
        synthetic_data_confirmed=True,
        request_fingerprint="test_fp_failed_1",
        completed_fingerprint=None,
    )
    session.add(failed_run)
    session.commit()

    # Retrying request should succeed and create a COMPLETED run (not blocked!)
    res = client.post("/api/v1/replays", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "COMPLETED"
    assert data["is_reused"] is False

def test_multiple_failed_attempts_allowed(session, test_user):
    # Insert 2 FAILED runs with the same request_fingerprint and null completed_fingerprint
    f1 = InspectionRun(
        owner_id=test_user.id,
        run_type="HISTORICAL_REPLAY",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        timeframe="15m",
        created_at=datetime.now(timezone.utc),
        completed_at=None,
        status="FAILED",
        failure_summary="Failed attempt 1",
        result_payload=None,
        synthetic_data_confirmed=True,
        request_fingerprint="common_request_fp",
        completed_fingerprint=None,
    )
    f2 = InspectionRun(
        owner_id=test_user.id,
        run_type="HISTORICAL_REPLAY",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        timeframe="15m",
        created_at=datetime.now(timezone.utc),
        completed_at=None,
        status="FAILED",
        failure_summary="Failed attempt 2",
        result_payload=None,
        synthetic_data_confirmed=True,
        request_fingerprint="common_request_fp",
        completed_fingerprint=None,
    )
    session.add(f1)
    session.add(f2)
    session.commit()

    count = session.query(InspectionRun).filter(InspectionRun.request_fingerprint == "common_request_fp").count()
    assert count == 2

def test_canonical_fingerprint_repeatability():
    from src.services.persistence_service import compute_request_fingerprint
    strat = make_sample_strategy()
    dt_start = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    dt_end = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

    fp1 = compute_request_fingerprint(strat, "synthetic_underlying_nifty_15m", ["synthetic_candidate_option_ce_23000_15m"], dt_start, dt_end, 1)
    fp2 = compute_request_fingerprint(strat, "synthetic_underlying_nifty_15m", ["synthetic_candidate_option_ce_23000_15m"], dt_start, dt_end, 1)
    assert fp1 == fp2

def test_meaningful_input_fingerprint_changes():
    from src.services.persistence_service import compute_request_fingerprint
    strat = make_sample_strategy()
    dt_start = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    dt_end = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

    fp_base = compute_request_fingerprint(strat, "synthetic_underlying_nifty_15m", ["synthetic_candidate_option_ce_23000_15m"], dt_start, dt_end, 1)

    fp_step = compute_request_fingerprint(strat, "synthetic_underlying_nifty_15m", ["synthetic_candidate_option_ce_23000_15m"], dt_start, dt_end, 2)
    assert fp_base != fp_step

    fp_ref = compute_request_fingerprint(strat, "synthetic_short_insufficient_5m", ["synthetic_candidate_option_ce_23000_15m"], dt_start, dt_end, 1)
    assert fp_base != fp_ref

def test_sanitized_error_summary_suppresses_stacktraces_sql_paths():
    from src.services.persistence_service import sanitize_error_message
    raw_stacktrace = "Traceback (most recent call last):\n  File '/src/app.py', line 42, in <module>\n    SELECT * FROM secret_table"
    sanitized = sanitize_error_message(raw_stacktrace)

    assert "Traceback" not in sanitized
    assert "SELECT" not in sanitized
    assert "/src/" not in sanitized
    assert "internal processing error" in sanitized

def test_utc_datetime_persistence_round_trip(session, test_user):
    now_utc = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)
    run = InspectionRun(
        owner_id=test_user.id,
        run_type="HISTORICAL_REPLAY",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        timeframe="15m",
        created_at=now_utc,
        completed_at=now_utc,
        status="COMPLETED",
        failure_summary=None,
        strategy_definition_snapshot={"action": "PAPER_TRADE"},
        reference_dataset_id="synthetic_underlying_nifty_15m",
        requested_start_timestamp=now_utc,
        requested_end_timestamp=now_utc,
        result_payload={"summary": "ok"},
        manifest_checksums_snapshot={"ds": "hash"},
        synthetic_data_confirmed=True,
    )
    session.add(run)
    session.commit()

    retrieved = session.query(InspectionRun).filter(InspectionRun.id == run.id).first()
    assert retrieved.created_at.tzinfo is not None
    assert retrieved.created_at == now_utc

def test_conditional_completed_constraints_validation(session, test_user):
    from sqlalchemy.exc import IntegrityError
    now_utc = datetime.now(timezone.utc)
    invalid_completed = InspectionRun(
        owner_id=test_user.id,
        run_type="HISTORICAL_REPLAY",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        timeframe="15m",
        created_at=now_utc,
        completed_at=None,
        status="COMPLETED",
        failure_summary=None,
        strategy_definition_snapshot={"action": "PAPER_TRADE"},
        reference_dataset_id="synthetic_underlying_nifty_15m",
        requested_start_timestamp=now_utc,
        requested_end_timestamp=now_utc,
        result_payload={"summary": "ok"},
        manifest_checksums_snapshot={"ds": "hash"},
        synthetic_data_confirmed=True,
    )
    session.add(invalid_completed)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

def test_conditional_failed_constraints_validation(session, test_user):
    from sqlalchemy.exc import IntegrityError
    now_utc = datetime.now(timezone.utc)
    invalid_failed = InspectionRun(
        owner_id=test_user.id,
        run_type="HISTORICAL_REPLAY",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        timeframe="15m",
        created_at=now_utc,
        completed_at=None,
        status="FAILED",
        failure_summary="Failed message",
        result_payload={"summary": "invalid_for_failed"},
        synthetic_data_confirmed=True,
    )
    session.add(invalid_failed)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

def test_completed_fingerprint_uniqueness(session, test_user):
    from sqlalchemy.exc import IntegrityError
    now_utc = datetime.now(timezone.utc)
    r1 = InspectionRun(
        owner_id=test_user.id,
        run_type="HISTORICAL_REPLAY",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        timeframe="15m",
        created_at=now_utc,
        completed_at=now_utc,
        status="COMPLETED",
        failure_summary=None,
        strategy_definition_snapshot={"action": "PAPER_TRADE"},
        reference_dataset_id="synthetic_underlying_nifty_15m",
        requested_start_timestamp=now_utc,
        requested_end_timestamp=now_utc,
        result_payload={"summary": "ok"},
        manifest_checksums_snapshot={"ds": "hash"},
        synthetic_data_confirmed=True,
        completed_fingerprint="unique_fp_123",
    )
    r2 = InspectionRun(
        owner_id=test_user.id,
        run_type="HISTORICAL_REPLAY",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        timeframe="15m",
        created_at=now_utc,
        completed_at=now_utc,
        status="COMPLETED",
        failure_summary=None,
        strategy_definition_snapshot={"action": "PAPER_TRADE"},
        reference_dataset_id="synthetic_underlying_nifty_15m",
        requested_start_timestamp=now_utc,
        requested_end_timestamp=now_utc,
        result_payload={"summary": "ok"},
        manifest_checksums_snapshot={"ds": "hash"},
        synthetic_data_confirmed=True,
        completed_fingerprint="unique_fp_123",
    )
    session.add(r1)
    session.commit()
    session.add(r2)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

def test_synthetic_data_confirmed_boolean_constraint(session, test_user):
    from sqlalchemy.exc import IntegrityError
    now_utc = datetime.now(timezone.utc)
    invalid_synthetic = InspectionRun(
        owner_id=test_user.id,
        run_type="HISTORICAL_REPLAY",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        timeframe="15m",
        created_at=now_utc,
        completed_at=now_utc,
        status="COMPLETED",
        failure_summary=None,
        strategy_definition_snapshot={"action": "PAPER_TRADE"},
        reference_dataset_id="synthetic_underlying_nifty_15m",
        requested_start_timestamp=now_utc,
        requested_end_timestamp=now_utc,
        result_payload={"summary": "ok"},
        manifest_checksums_snapshot={"ds": "hash"},
        synthetic_data_confirmed=False,
    )
    session.add(invalid_synthetic)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
