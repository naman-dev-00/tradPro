import pytest
import json
from datetime import datetime, timezone
from src.services.export_service import ExportService, sanitize_csv_cell, sanitize_filename
from src.models import InspectionRun



def test_sanitize_csv_cell_escapes_formulas_and_special_chars():
    # Malicious text formula cells must be escaped with single quote
    assert sanitize_csv_cell("=SUM(A1:A10)") == "'=SUM(A1:A10)"
    assert sanitize_csv_cell("+cmd|' /C calc'!A0") == "'+cmd|' /C calc'!A0"
    assert sanitize_csv_cell("-1+1") == "'-1+1"
    assert sanitize_csv_cell("@SUM(1,2)") == "'@SUM(1,2)"
    assert sanitize_csv_cell("\tMALICIOUS_TAB") == "'\tMALICIOUS_TAB"
    assert sanitize_csv_cell("\rCARRIAGE_RETURN") == "'\rCARRIAGE_RETURN"
    assert sanitize_csv_cell("  =LEAD_SPACE_FORMULA") == "'  =LEAD_SPACE_FORMULA"

def test_sanitize_csv_cell_preserves_numeric_values():
    # Legitimate integers and floats must NOT be corrupted with single quotes
    assert sanitize_csv_cell(100) == "100"
    assert sanitize_csv_cell(-15.5) == "-15.5"
    assert sanitize_csv_cell(42.0) == "42.0"
    assert sanitize_csv_cell("+42") == "+42"
    assert sanitize_csv_cell("-100") == "-100"
    assert sanitize_csv_cell("0.005") == "0.005"

def test_sanitize_filename_prevents_path_traversal():
    dirty_id = "../../etc/passwd"
    clean_name = sanitize_filename(dirty_id, "json")
    assert clean_name == "replay_etcpasswd.json"
    assert "/" not in clean_name
    assert "\\" not in clean_name

def test_generate_json_export_structure(session):

    run = InspectionRun(
        id="597a9957-ed19-6a5c-70f1-a6f631b30507",
        status="COMPLETED",
        run_type="HISTORICAL_REPLAY",
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        timeframe="15m",
        engine_version="1.0.0",
        manifest_version="1.0.0",
        result_payload={"sampling_step": 1, "replay_schema_version": "1.0.0", "replay_points": []},
        manifest_checksums_snapshot={"synthetic_underlying_nifty_15m": "hash1"},
        strategy_definition_snapshot={"name": "Test Strat"},
        request_fingerprint="fp123",
        requested_start_timestamp=datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc),
        requested_end_timestamp=datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc),
        created_at=datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc),
        completed_at=datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc),
        synthetic_data_confirmed=True,
    )

    session.add(run)
    session.commit()


    resp = ExportService.generate_json_export(run)
    assert resp.status_code == 200
    assert "attachment; filename=\"replay_597a9957-ed19-6a5c-70f1-a6f631b30507.json\"" in resp.headers["Content-Disposition"]

    data = json.loads(resp.body.decode("utf-8"))
    assert "Educational synthetic" in data["notice"]
    assert data["run_id"] == "597a9957-ed19-6a5c-70f1-a6f631b30507"
    assert "reproducibility_verification" in data

def test_generate_csv_export_structure(session):

    run = InspectionRun(
        id="88888888-4444-4444-4444-1234567890ab",
        status="COMPLETED",
        run_type="HISTORICAL_REPLAY",
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        timeframe="15m",
        engine_version="1.0.0",
        manifest_version="1.0.0",
        result_payload={
            "sampling_step": 1,
            "replay_schema_version": "1.0.0",
            "replay_points": [
                {
                    "evaluation_timestamp": "2026-08-28T09:15:00Z",
                    "results": [
                        {
                            "dataset_id": "synthetic_candidate_option_ce_23000_15m",
                            "overall_status": "TRUE",
                            "passed_condition_ids": ["c1"],
                            "failed_condition_ids": ["=MALICIOUS_FORMULA"],
                            "inspection_summary": "Passed evaluation",
                        }
                    ],
                }
            ],
        },
        manifest_checksums_snapshot={"synthetic_underlying_nifty_15m": "hash1"},
        strategy_definition_snapshot={"name": "Test Strat"},
        request_fingerprint="fp123",
        requested_start_timestamp=datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc),
        requested_end_timestamp=datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc),
        created_at=datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc),
        completed_at=datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc),
        synthetic_data_confirmed=True,
    )

    session.add(run)
    session.commit()


    resp = ExportService.generate_csv_export(run)
    assert resp.status_code == 200
    assert "attachment; filename=\"replay_88888888-4444-4444-4444-1234567890ab.csv\"" in resp.headers["Content-Disposition"]

    csv_text = resp.body.decode("utf-8")
    assert "# NOTICE: Educational synthetic" in csv_text
    assert "'=MALICIOUS_FORMULA" in csv_text
