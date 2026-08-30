import pytest
from datetime import datetime, timezone
from pydantic import ValidationError
from src.engine.dataset_quality_models import (
    DatasetQualityStatus,
    DatasetIssueSeverity,
    DatasetIssueCode,
    DatasetQualityIssue,
    DatasetQualitySummary,
    DatasetProvenance,
    DatasetQualityReport,
    DatasetAuditBatchRequest,
    DatasetAuditBatchResponse,
)
from src.engine.manifest import DatasetCategory

def test_models_forbid_extra_fields():
    with pytest.raises(ValidationError):
        DatasetQualityIssue(
            code=DatasetIssueCode.ROW_MALFORMED,
            severity=DatasetIssueSeverity.ERROR,
            message="Test error",
            unrecognized_extra="not_allowed",
        )

    with pytest.raises(ValidationError):
        DatasetAuditBatchRequest(
            dataset_ids=["synthetic_underlying_nifty_15m"],
            forbidden_field="hacked",
        )

def test_naive_timestamp_rejected():
    naive_dt = datetime(2026, 8, 28, 9, 15)  # No tzinfo
    with pytest.raises(ValidationError, match="timezone-aware"):
        DatasetQualityIssue(
            code=DatasetIssueCode.TIMESTAMP_NOT_UTC,
            severity=DatasetIssueSeverity.ERROR,
            message="Naive ts",
            timestamp=naive_dt,
        )

def test_timezone_aware_timestamp_normalized_to_utc():
    utc_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    issue = DatasetQualityIssue(
        code=DatasetIssueCode.TIMESTAMP_INVALID,
        severity=DatasetIssueSeverity.ERROR,
        message="Valid UTC",
        timestamp=utc_dt,
    )
    assert issue.timestamp == utc_dt
    assert issue.timestamp.tzinfo == timezone.utc

def test_provenance_strict_literals():
    prov = DatasetProvenance(
        dataset_id="test_ds",
        display_name="Test Display",
        category=DatasetCategory.REFERENCE,
        instrument_id="TEST_INST",
        timeframe="15m",
        manifest_version="1.0.0",
        fixture_checksum="abc",
    )
    assert prov.is_synthetic is True
    assert prov.immutable is True
    assert prov.source_type == "PACKAGED_SYNTHETIC_FIXTURE"

    # Attempting to forge is_synthetic=False or immutable=False must fail
    with pytest.raises(ValidationError):
        DatasetProvenance(
            dataset_id="test_ds",
            display_name="Test Display",
            category=DatasetCategory.REFERENCE,
            instrument_id="TEST_INST",
            timeframe="15m",
            is_synthetic=False,  # Forbidden
            manifest_version="1.0.0",
        )

def test_summary_row_count_invariants():
    # valid_rows + malformed_rows must equal total_rows
    with pytest.raises(ValidationError, match="Row count invariant violated"):
        DatasetQualitySummary(
            total_rows=10,
            valid_rows=8,
            malformed_rows=1,  # 8 + 1 != 10
            completed_rows=8,
            incomplete_rows=0,
            duplicate_timestamp_count=0,
            missing_interval_count=0,
        )

    # completed_rows + incomplete_rows must equal valid_rows
    with pytest.raises(ValidationError, match="Row completion invariant violated"):
        DatasetQualitySummary(
            total_rows=10,
            valid_rows=9,
            malformed_rows=1,
            completed_rows=8,
            incomplete_rows=0,  # 8 + 0 != 9
            duplicate_timestamp_count=0,
            missing_interval_count=0,
        )

def test_report_issue_count_invariants():
    prov = DatasetProvenance(
        dataset_id="test_ds",
        display_name="Test",
        category=DatasetCategory.SUBJECT,
        instrument_id="INST",
        timeframe="15m",
        manifest_version="1.0.0",
    )
    summary = DatasetQualitySummary(
        total_rows=10,
        valid_rows=10,
        malformed_rows=0,
        completed_rows=10,
        incomplete_rows=0,
        duplicate_timestamp_count=0,
        missing_interval_count=0,
    )
    issue1 = DatasetQualityIssue(
        code=DatasetIssueCode.MISSING_INTERVAL,
        severity=DatasetIssueSeverity.WARNING,
        message="Gap",
    )

    # reported_issue_count != len(issues) must raise error
    with pytest.raises(ValidationError, match="reported_issue_count"):
        DatasetQualityReport(
            dataset_id="test_ds",
            status=DatasetQualityStatus.WARN,
            provenance=prov,
            summary=summary,
            issues=[issue1],
            total_issue_count=1,
            reported_issue_count=2,  # mismatch with len(issues)==1
            issues_truncated=False,
        )

def test_batch_response_status_count_invariants():
    prov = DatasetProvenance(
        dataset_id="test_ds",
        display_name="Test",
        category=DatasetCategory.SUBJECT,
        instrument_id="INST",
        timeframe="15m",
        manifest_version="1.0.0",
    )
    summary = DatasetQualitySummary(
        total_rows=5,
        valid_rows=5,
        malformed_rows=0,
        completed_rows=5,
        incomplete_rows=0,
        duplicate_timestamp_count=0,
        missing_interval_count=0,
    )
    report = DatasetQualityReport(
        dataset_id="test_ds",
        status=DatasetQualityStatus.PASS,
        provenance=prov,
        summary=summary,
        issues=[],
        total_issue_count=0,
        reported_issue_count=0,
        issues_truncated=False,
    )

    # status_counts sum must equal total_datasets
    with pytest.raises(ValidationError, match="status_counts sum"):
        DatasetAuditBatchResponse(
            reports=[report],
            status_counts={"PASS": 0, "WARN": 0, "FAIL": 0},  # sum 0 != 1
            total_datasets=1,
            audit_rules_version="1.0.0",
        )
