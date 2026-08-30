import pytest
from src.engine.dataset_quality_engine import (
    DatasetQualityEngine,
    AUDIT_RULES_VERSION,
    MINIMUM_RECOMMENDED_COMPLETED_ROWS,
)
from src.engine.dataset_quality_models import (
    DatasetQualityStatus,
    DatasetIssueSeverity,
    DatasetIssueCode,
    DatasetProvenance,
)
from src.engine.manifest import DatasetCategory, DatasetManifestEntry

VALID_CSV = """# Synthetic Educational Data
timestamp,instrument_id,timeframe,open,high,low,close,volume,is_closed
2026-08-28T09:15:00Z,TEST_INST,15m,100.0,105.0,95.0,102.0,500.0,true
2026-08-28T09:30:00Z,TEST_INST,15m,102.0,108.0,100.0,106.0,600.0,true
"""

def make_test_provenance(ds_id="test_ds", timeframe="15m", instrument_id="TEST_INST"):
    return DatasetProvenance(
        dataset_id=ds_id,
        display_name="Test Dataset",
        category=DatasetCategory.REFERENCE,
        instrument_id=instrument_id,
        timeframe=timeframe,
        manifest_version="1.0.0",
        fixture_checksum="dummy_checksum",
    )

def test_clean_fixture_produces_pass_or_warmup_warn():
    # If 2 rows (< 34), produces WARN for INSUFFICIENT_DATA_FOR_WARMUP
    prov = make_test_provenance()
    manifest_entry = DatasetManifestEntry(
        dataset_id="test_ds",
        display_name="Test",
        description="desc",
        instrument_id="TEST_INST",
        timeframe="15m",
        candle_count=2,
        completed_candle_count=2,
        category=DatasetCategory.REFERENCE,
        dataset_checksum="dummy_checksum",
    )
    report = DatasetQualityEngine.audit_dataset_content(
        dataset_id="test_ds",
        csv_content=VALID_CSV,
        provenance=prov,
        manifest_entry=manifest_entry,
        calculated_checksum="dummy_checksum",
    )
    assert report.summary.total_rows == 2
    assert report.summary.completed_rows == 2
    assert report.summary.malformed_rows == 0
    assert report.summary.checksum_matches is True
    # Has warning for warmup (< 34 rows)
    assert report.status == DatasetQualityStatus.WARN
    assert any(i.code == DatasetIssueCode.INSUFFICIENT_DATA_FOR_WARMUP for i in report.issues)

def test_clean_35_rows_produces_pass():
    rows = ["timestamp,instrument_id,timeframe,open,high,low,close,volume,is_closed"]
    base_m = 15
    for i in range(35):
        hour = 9 + (base_m + i * 15) // 60
        minute = (base_m + i * 15) % 60
        ts = f"2026-08-28T{hour:02d}:{minute:02d}:00Z"
        rows.append(f"{ts},TEST_INST,15m,100.0,105.0,95.0,102.0,500.0,true")
    csv_35 = "\n".join(rows)

    prov = make_test_provenance()
    manifest_entry = DatasetManifestEntry(
        dataset_id="test_ds",
        display_name="Test",
        description="desc",
        instrument_id="TEST_INST",
        timeframe="15m",
        candle_count=35,
        completed_candle_count=35,
        category=DatasetCategory.REFERENCE,
        dataset_checksum="hash35",
    )
    report = DatasetQualityEngine.audit_dataset_content(
        dataset_id="test_ds",
        csv_content=csv_35,
        provenance=prov,
        manifest_entry=manifest_entry,
        calculated_checksum="hash35",
    )
    assert report.status == DatasetQualityStatus.PASS
    assert report.total_issue_count == 0
    assert report.summary.total_rows == 35
    assert report.summary.completed_rows == 35

def test_csv_header_missing_columns_and_bom():
    # Header with BOM on first col and missing 'is_closed'
    csv_bad_header = "\ufefftimestamp,instrument_id,timeframe,open,high,low,close,volume\n2026-08-28T09:15:00Z,TEST_INST,15m,100,105,95,102,500\n"
    prov = make_test_provenance()
    report = DatasetQualityEngine.audit_dataset_content("test_ds", csv_bad_header, prov)

    assert report.status == DatasetQualityStatus.FAIL
    assert any(i.code == DatasetIssueCode.CSV_HEADER_INVALID for i in report.issues)

def test_malformed_numeric_rows():
    csv_malformed = """timestamp,instrument_id,timeframe,open,high,low,close,volume,is_closed
2026-08-28T09:15:00Z,TEST_INST,15m,INVALID_OPEN,105.0,95.0,102.0,500.0,true
"""
    prov = make_test_provenance()
    report = DatasetQualityEngine.audit_dataset_content("test_ds", csv_malformed, prov)

    assert report.status == DatasetQualityStatus.FAIL
    assert report.summary.malformed_rows == 1
    assert report.summary.valid_rows == 0
    assert any(i.code == DatasetIssueCode.ROW_MALFORMED for i in report.issues)

def test_naive_timestamp_and_invalid_timestamp():
    csv_naive = """timestamp,instrument_id,timeframe,open,high,low,close,volume,is_closed
2026-08-28 09:15:00,TEST_INST,15m,100.0,105.0,95.0,102.0,500.0,true
NOT_A_DATE,TEST_INST,15m,100.0,105.0,95.0,102.0,500.0,true
"""
    prov = make_test_provenance()
    report = DatasetQualityEngine.audit_dataset_content("test_ds", csv_naive, prov)

    assert report.status == DatasetQualityStatus.FAIL
    codes = [i.code for i in report.issues]
    assert DatasetIssueCode.TIMESTAMP_NOT_UTC in codes
    assert DatasetIssueCode.TIMESTAMP_INVALID in codes

def test_timestamp_ordering_and_duplicates():
    csv_order = """timestamp,instrument_id,timeframe,open,high,low,close,volume,is_closed
2026-08-28T09:30:00Z,TEST_INST,15m,100.0,105.0,95.0,102.0,500.0,true
2026-08-28T09:15:00Z,TEST_INST,15m,100.0,105.0,95.0,102.0,500.0,true
2026-08-28T09:15:00Z,TEST_INST,15m,100.0,105.0,95.0,102.0,500.0,true
"""
    prov = make_test_provenance()
    report = DatasetQualityEngine.audit_dataset_content("test_ds", csv_order, prov)

    assert report.status == DatasetQualityStatus.FAIL
    codes = [i.code for i in report.issues]
    assert DatasetIssueCode.TIMESTAMP_OUT_OF_ORDER in codes
    assert DatasetIssueCode.DUPLICATE_TIMESTAMP in codes
    assert report.summary.duplicate_timestamp_count == 1

def test_exact_missing_interval_calculation():
    # Gap between 09:15 and 10:00 is 45m = 3 intervals of 15m (missing 2 intervals)
    csv_gap = """timestamp,instrument_id,timeframe,open,high,low,close,volume,is_closed
2026-08-28T09:15:00Z,TEST_INST,15m,100.0,105.0,95.0,102.0,500.0,true
2026-08-28T10:00:00Z,TEST_INST,15m,100.0,105.0,95.0,102.0,500.0,true
"""
    prov = make_test_provenance()
    report = DatasetQualityEngine.audit_dataset_content("test_ds", csv_gap, prov)

    assert report.summary.missing_interval_count == 2
    assert any(i.code == DatasetIssueCode.MISSING_INTERVAL for i in report.issues)

def test_timeframe_interval_mismatch():
    # 15m timeframe, but timestamp diff is 7 minutes (not a multiple of 15m)
    csv_mismatch = """timestamp,instrument_id,timeframe,open,high,low,close,volume,is_closed
2026-08-28T09:15:00Z,TEST_INST,15m,100.0,105.0,95.0,102.0,500.0,true
2026-08-28T09:22:00Z,TEST_INST,15m,100.0,105.0,95.0,102.0,500.0,true
"""
    prov = make_test_provenance()
    report = DatasetQualityEngine.audit_dataset_content("test_ds", csv_mismatch, prov)

    assert report.status == DatasetQualityStatus.FAIL
    assert any(i.code == DatasetIssueCode.TIMEFRAME_INTERVAL_MISMATCH for i in report.issues)

def test_non_finite_and_negative_prices_and_ohlc_bounds():
    csv_bounds = """timestamp,instrument_id,timeframe,open,high,low,close,volume,is_closed
2026-08-28T09:15:00Z,TEST_INST,15m,100.0,90.0,95.0,102.0,500.0,true
2026-08-28T09:30:00Z,TEST_INST,15m,-5.0,105.0,95.0,102.0,-10.0,true
2026-08-28T09:45:00Z,TEST_INST,15m,100.0,105.0,110.0,102.0,500.0,true
"""
    prov = make_test_provenance()
    report = DatasetQualityEngine.audit_dataset_content("test_ds", csv_bounds, prov)

    assert report.status == DatasetQualityStatus.FAIL
    codes = [i.code for i in report.issues]
    assert DatasetIssueCode.OHLC_HIGH_BOUND_INVALID in codes
    assert DatasetIssueCode.OHLC_LOW_BOUND_INVALID in codes
    assert DatasetIssueCode.NEGATIVE_PRICE in codes
    assert DatasetIssueCode.NEGATIVE_VOLUME in codes

def test_issue_truncation_when_over_1000_issues():
    # Generate 1,100 malformed rows
    rows = ["timestamp,instrument_id,timeframe,open,high,low,close,volume,is_closed"]
    for i in range(1100):
        rows.append(f"2026-08-28T09:15:00Z,TEST_INST,15m,BAD_OPEN,105.0,95.0,102.0,500.0,true")
    csv_large = "\n".join(rows)

    prov = make_test_provenance()
    report = DatasetQualityEngine.audit_dataset_content("test_ds", csv_large, prov)

    assert report.status == DatasetQualityStatus.FAIL
    assert report.total_issue_count >= 1100
    assert report.reported_issue_count == 1000
    assert report.issues_truncated is True
    assert len(report.issues) == 1000
    assert any("truncated" in w for w in report.warnings)

def test_row_limit_exceeded_emits_error():
    rows = ["timestamp,instrument_id,timeframe,open,high,low,close,volume,is_closed"]
    for i in range(5005):
        rows.append(f"2026-08-28T09:15:00Z,TEST_INST,15m,100.0,105.0,95.0,102.0,500.0,true")
    csv_5005 = "\n".join(rows)

    prov = make_test_provenance()
    report = DatasetQualityEngine.audit_dataset_content("test_ds", csv_5005, prov)

    assert report.status == DatasetQualityStatus.FAIL
    assert any(i.code == DatasetIssueCode.DATASET_ROW_LIMIT_EXCEEDED for i in report.issues)
