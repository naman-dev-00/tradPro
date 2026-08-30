import pytest
from src.services.dataset_quality_service import DatasetQualityService
from src.engine.dataset_quality_models import DatasetQualityStatus, DatasetIssueCode

def test_audit_all_whitelisted_packaged_fixtures():
    summaries = DatasetQualityService.list_dataset_summaries()
    assert len(summaries) == 6

    expected_fixture_results = {
        "synthetic_underlying_nifty_15m": {
            "expected_status": DatasetQualityStatus.PASS,
            "expected_total_rows": 35,
            "expected_completed": 35,
            "expected_incomplete": 0,
            "expected_issues": [],
        },
        "synthetic_candidate_option_ce_23000_15m": {
            "expected_status": DatasetQualityStatus.WARN,
            "expected_total_rows": 10,
            "expected_completed": 10,
            "expected_incomplete": 0,
            "expected_issues": [DatasetIssueCode.INSUFFICIENT_DATA_FOR_WARMUP],
        },
        "synthetic_candidate_option_pe_23000_15m": {
            "expected_status": DatasetQualityStatus.WARN,
            "expected_total_rows": 10,
            "expected_completed": 10,
            "expected_incomplete": 0,
            "expected_issues": [DatasetIssueCode.INSUFFICIENT_DATA_FOR_WARMUP],
        },
        "synthetic_candidate_option_ce_23500_15m": {
            "expected_status": DatasetQualityStatus.WARN,
            "expected_total_rows": 10,
            "expected_completed": 10,
            "expected_incomplete": 0,
            "expected_issues": [DatasetIssueCode.INSUFFICIENT_DATA_FOR_WARMUP],
        },
        "synthetic_short_insufficient_5m": {
            "expected_status": DatasetQualityStatus.WARN,
            "expected_total_rows": 3,
            "expected_completed": 3,
            "expected_incomplete": 0,
            "expected_issues": [DatasetIssueCode.INSUFFICIENT_DATA_FOR_WARMUP],
        },
        "synthetic_with_incomplete_candle_15m": {
            "expected_status": DatasetQualityStatus.WARN,
            "expected_total_rows": 10,
            "expected_completed": 9,
            "expected_incomplete": 1,
            "expected_issues": [
                DatasetIssueCode.INCOMPLETE_CANDLE_PRESENT,
                DatasetIssueCode.INSUFFICIENT_DATA_FOR_WARMUP,
            ],
        },
    }

    for ds_id, exp in expected_fixture_results.items():
        report = DatasetQualityService.get_dataset_report(ds_id)
        assert report.status == exp["expected_status"], f"Status mismatch for {ds_id}: got {report.status}, expected {exp['expected_status']}"
        assert report.summary.total_rows == exp["expected_total_rows"], f"Total rows mismatch for {ds_id}"
        assert report.summary.completed_rows == exp["expected_completed"], f"Completed rows mismatch for {ds_id}"
        assert report.summary.incomplete_rows == exp["expected_incomplete"], f"Incomplete rows mismatch for {ds_id}"
        assert report.summary.checksum_matches is True, f"Checksum mismatch for {ds_id}"

        issue_codes = [i.code for i in report.issues]
        for exp_code in exp["expected_issues"]:
            assert exp_code in issue_codes, f"Expected {exp_code} in issues for {ds_id}"
