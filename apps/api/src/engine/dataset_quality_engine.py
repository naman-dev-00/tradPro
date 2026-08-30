import csv
import io
import math
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any, Tuple, Iterable
from src.engine.dataset_quality_models import (
    DatasetQualityStatus,
    DatasetIssueSeverity,
    DatasetIssueCode,
    DatasetQualityIssue,
    DatasetQualitySummary,
    DatasetProvenance,
    DatasetQualityReport,
)
from src.engine.manifest import DatasetCategory, DatasetManifestEntry

AUDIT_RULES_VERSION = "1.0.0"
MINIMUM_RECOMMENDED_COMPLETED_ROWS = 34
MAX_REPORTED_ISSUES = 1000
MAX_DATASET_ROWS = 5000

SUPPORTED_TIMEFRAME_INTERVALS: Dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}

REQUIRED_CSV_COLUMNS: List[str] = [
    "timestamp",
    "instrument_id",
    "timeframe",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "is_closed",
]

ISSUE_SEVERITY_MAP: Dict[DatasetIssueCode, DatasetIssueSeverity] = {
    DatasetIssueCode.FILE_UNAVAILABLE: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.CSV_HEADER_INVALID: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.ROW_MALFORMED: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.TIMESTAMP_INVALID: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.TIMESTAMP_NOT_UTC: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.TIMESTAMP_OUT_OF_ORDER: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.DUPLICATE_TIMESTAMP: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.TIMEFRAME_UNSUPPORTED: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.TIMEFRAME_INTERVAL_MISMATCH: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.MISSING_INTERVAL: DatasetIssueSeverity.WARNING,
    DatasetIssueCode.INSTRUMENT_ID_MISMATCH: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.TIMEFRAME_VALUE_MISMATCH: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.NON_FINITE_VALUE: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.NEGATIVE_PRICE: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.NEGATIVE_VOLUME: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.OHLC_HIGH_BOUND_INVALID: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.OHLC_LOW_BOUND_INVALID: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.INCOMPLETE_CANDLE_PRESENT: DatasetIssueSeverity.WARNING,
    DatasetIssueCode.MANIFEST_COUNT_MISMATCH: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.COMPLETED_COUNT_MISMATCH: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.CHECKSUM_MISMATCH: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.MANIFEST_METADATA_MISMATCH: DatasetIssueSeverity.ERROR,
    DatasetIssueCode.INSUFFICIENT_DATA_FOR_WARMUP: DatasetIssueSeverity.WARNING,
    DatasetIssueCode.DATASET_ROW_LIMIT_EXCEEDED: DatasetIssueSeverity.ERROR,
}

def sanitize_evidence(val: Any, max_len: int = 100) -> Optional[str]:
    if val is None:
        return None
    s = str(val)
    # Remove CR, LF, null, and non-printable control characters
    s = re.sub(r"[\r\n\t\x00-\x1f\x7f-\x9f]", " ", s)
    s = s.strip()
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s

def sort_issues(issues: List[DatasetQualityIssue]) -> List[DatasetQualityIssue]:
    severity_order = {
        DatasetIssueSeverity.ERROR: 0,
        DatasetIssueSeverity.WARNING: 1,
        DatasetIssueSeverity.INFO: 2,
    }
    max_dt = datetime.max.replace(tzinfo=timezone.utc)

    def sort_key(issue: DatasetQualityIssue):
        return (
            severity_order.get(issue.severity, 9),
            issue.row_number if issue.row_number is not None else 0,
            issue.timestamp if issue.timestamp is not None else max_dt,
            issue.code.value,
        )

    return sorted(issues, key=sort_key)


class DatasetQualityEngine:
    """
    Pure framework-independent quality evaluation engine for packaged synthetic datasets.
    """

    @classmethod
    def audit_dataset_content(
        cls,
        dataset_id: str,
        csv_content: str,
        provenance: DatasetProvenance,
        manifest_entry: Optional[DatasetManifestEntry] = None,
        calculated_checksum: Optional[str] = None,
    ) -> DatasetQualityReport:
        all_issues: List[DatasetQualityIssue] = []
        warnings: List[str] = []

        expected_timeframe = provenance.timeframe
        expected_instrument = provenance.instrument_id
        expected_interval_sec = SUPPORTED_TIMEFRAME_INTERVALS.get(expected_timeframe)

        if expected_interval_sec is None:
            all_issues.append(
                DatasetQualityIssue(
                    code=DatasetIssueCode.TIMEFRAME_UNSUPPORTED,
                    severity=DatasetIssueSeverity.ERROR,
                    message=f"Timeframe '{expected_timeframe}' is not supported.",
                    field="timeframe",
                    expected="One of: " + ", ".join(SUPPORTED_TIMEFRAME_INTERVALS.keys()),
                    actual=sanitize_evidence(expected_timeframe),
                )
            )

        # Checksum check
        manifest_checksum = manifest_entry.dataset_checksum if manifest_entry else provenance.fixture_checksum
        checksum_matches: Optional[bool] = None

        if calculated_checksum is not None and manifest_checksum is not None:
            checksum_matches = (calculated_checksum == manifest_checksum)
            if not checksum_matches:
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.CHECKSUM_MISMATCH,
                        severity=DatasetIssueSeverity.ERROR,
                        message="Calculated SHA-256 checksum does not match manifest checksum.",
                        field="dataset_checksum",
                        expected=sanitize_evidence(manifest_checksum),
                        actual=sanitize_evidence(calculated_checksum),
                    )
                )

        # Parse CSV lines (skip comment lines starting with #)
        lines = [line for line in csv_content.splitlines() if line.strip() and not line.strip().startswith("#")]

        if not lines:
            all_issues.append(
                DatasetQualityIssue(
                    code=DatasetIssueCode.CSV_HEADER_INVALID,
                    severity=DatasetIssueSeverity.ERROR,
                    message="CSV content is empty or contains only comments.",
                    field="header",
                )
            )
            summary = DatasetQualitySummary(
                total_rows=0,
                valid_rows=0,
                malformed_rows=0,
                completed_rows=0,
                incomplete_rows=0,
                duplicate_timestamp_count=0,
                missing_interval_count=0,
                first_timestamp=None,
                last_timestamp=None,
                expected_interval_seconds=expected_interval_sec,
                calculated_checksum=calculated_checksum,
                manifest_checksum=manifest_checksum,
                checksum_matches=checksum_matches,
            )
            sorted_issues = sort_issues(all_issues)
            return cls._build_report(dataset_id, provenance, summary, sorted_issues, warnings)

        # Parse Header using standard csv.reader
        header_reader = csv.reader([lines[0]])
        raw_header = next(header_reader, [])
        # Strip UTF-8 BOM if present on first column
        if raw_header and raw_header[0].startswith("\ufeff"):
            raw_header[0] = raw_header[0].replace("\ufeff", "")

        header_cols = [c.strip() for c in raw_header]
        missing_cols = [col for col in REQUIRED_CSV_COLUMNS if col not in header_cols]
        duplicate_cols = [col for col in set(header_cols) if header_cols.count(col) > 1]

        if missing_cols or duplicate_cols or not header_cols:
            all_issues.append(
                DatasetQualityIssue(
                    code=DatasetIssueCode.CSV_HEADER_INVALID,
                    severity=DatasetIssueSeverity.ERROR,
                    message=f"CSV header invalid. Missing: {missing_cols}; Duplicates: {duplicate_cols}",
                    row_number=1,
                    field="header",
                    expected=",".join(REQUIRED_CSV_COLUMNS),
                    actual=sanitize_evidence(",".join(header_cols)),
                )
            )

        col_indices = {col: i for i, col in enumerate(header_cols) if col in REQUIRED_CSV_COLUMNS}
        has_all_required_cols = (len(missing_cols) == 0 and len(duplicate_cols) == 0)

        data_lines = lines[1:]
        total_rows = len(data_lines)

        # Check row limit
        row_limit_exceeded = False
        if total_rows > MAX_DATASET_ROWS:
            row_limit_exceeded = True
            all_issues.append(
                DatasetQualityIssue(
                    code=DatasetIssueCode.DATASET_ROW_LIMIT_EXCEEDED,
                    severity=DatasetIssueSeverity.ERROR,
                    message=f"Dataset row count ({total_rows}) exceeds maximum allowed ({MAX_DATASET_ROWS}).",
                    field="total_rows",
                    expected=f"<= {MAX_DATASET_ROWS}",
                    actual=str(total_rows),
                )
            )
            # Truncate processing to MAX_DATASET_ROWS to prevent unbounded execution
            excess_rows = total_rows - MAX_DATASET_ROWS
            data_lines = data_lines[:MAX_DATASET_ROWS]
        else:
            excess_rows = 0

        valid_rows = 0
        malformed_rows = excess_rows
        completed_rows = 0
        incomplete_rows = 0
        duplicate_timestamp_count = 0
        missing_interval_count = 0


        parsed_timestamps: List[datetime] = []
        last_valid_ts: Optional[datetime] = None

        csv_reader = csv.reader(data_lines)

        for physical_idx, row in enumerate(csv_reader):
            row_num = physical_idx + 2  # 1-based, header was line 1

            if not has_all_required_cols or len(row) < len(REQUIRED_CSV_COLUMNS):
                malformed_rows += 1
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.ROW_MALFORMED,
                        severity=DatasetIssueSeverity.ERROR,
                        message=f"Row column count ({len(row)}) does not match required columns.",
                        row_number=row_num,
                        actual=sanitize_evidence(",".join(row)),
                    )
                )
                continue

            # Extract fields
            try:
                ts_str = row[col_indices["timestamp"]].strip()
                inst_str = row[col_indices["instrument_id"]].strip()
                tf_str = row[col_indices["timeframe"]].strip()
                open_str = row[col_indices["open"]].strip()
                high_str = row[col_indices["high"]].strip()
                low_str = row[col_indices["low"]].strip()
                close_str = row[col_indices["close"]].strip()
                vol_str = row[col_indices["volume"]].strip()
                closed_str = row[col_indices["is_closed"]].strip().lower()
            except Exception:
                malformed_rows += 1
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.ROW_MALFORMED,
                        severity=DatasetIssueSeverity.ERROR,
                        message="Unable to extract required columns from row.",
                        row_number=row_num,
                    )
                )
                continue

            # Parse timestamp
            row_has_fatal_type_error = False
            dt: Optional[datetime] = None
            try:
                raw_dt = datetime.fromisoformat(ts_str)
                if raw_dt.tzinfo is None:
                    all_issues.append(
                        DatasetQualityIssue(
                            code=DatasetIssueCode.TIMESTAMP_NOT_UTC,
                            severity=DatasetIssueSeverity.ERROR,
                            message="Timestamp is naive (missing UTC timezone offset).",
                            row_number=row_num,
                            field="timestamp",
                            expected="ISO 8601 UTC timestamp ending in Z or +00:00",
                            actual=sanitize_evidence(ts_str),
                        )
                    )
                    dt = raw_dt.replace(tzinfo=timezone.utc)
                else:
                    dt = raw_dt.astimezone(timezone.utc)
            except Exception:
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.TIMESTAMP_INVALID,
                        severity=DatasetIssueSeverity.ERROR,
                        message="Invalid timestamp ISO format.",
                        row_number=row_num,
                        field="timestamp",
                        actual=sanitize_evidence(ts_str),
                    )
                )
                row_has_fatal_type_error = True

            # Parse numeric OHLCV
            o = h = l = c = v = None
            try:
                o = float(open_str)
                h = float(high_str)
                l = float(low_str)
                c = float(close_str)
                v = float(vol_str)
            except Exception:
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.ROW_MALFORMED,
                        severity=DatasetIssueSeverity.ERROR,
                        message="Non-numeric value in OHLCV fields.",
                        row_number=row_num,
                    )
                )
                row_has_fatal_type_error = True

            # If type parsing completely failed, count as malformed row
            if row_has_fatal_type_error or o is None or h is None or l is None or c is None or v is None or dt is None:
                malformed_rows += 1
                continue

            # Otherwise, count as a structurally valid row
            valid_rows += 1
            parsed_timestamps.append(dt)

            # Check finite
            for field_name, num_val in [("open", o), ("high", h), ("low", l), ("close", c), ("volume", v)]:
                if math.isnan(num_val) or math.isinf(num_val):
                    all_issues.append(
                        DatasetQualityIssue(
                            code=DatasetIssueCode.NON_FINITE_VALUE,
                            severity=DatasetIssueSeverity.ERROR,
                            message=f"Field '{field_name}' contains non-finite value (NaN/Inf).",
                            row_number=row_num,
                            field=field_name,
                            actual=sanitize_evidence(str(num_val)),
                        )
                    )

            # Check prices > 0
            for field_name, price_val in [("open", o), ("high", h), ("low", l), ("close", c)]:
                if price_val <= 0:
                    all_issues.append(
                        DatasetQualityIssue(
                            code=DatasetIssueCode.NEGATIVE_PRICE,
                            severity=DatasetIssueSeverity.ERROR,
                            message=f"Price field '{field_name}' must be strictly positive (> 0).",
                            row_number=row_num,
                            field=field_name,
                            expected="> 0",
                            actual=sanitize_evidence(str(price_val)),
                        )
                    )

            # Check volume >= 0
            if v < 0:
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.NEGATIVE_VOLUME,
                        severity=DatasetIssueSeverity.ERROR,
                        message="Volume must be non-negative (>= 0).",
                        row_number=row_num,
                        field="volume",
                        expected=">= 0",
                        actual=sanitize_evidence(str(v)),
                    )
                )

            # Check OHLC bounds
            if h < max(o, c, l):
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.OHLC_HIGH_BOUND_INVALID,
                        severity=DatasetIssueSeverity.ERROR,
                        message=f"High ({h}) is less than max(open={o}, close={c}, low={l}).",
                        row_number=row_num,
                        field="high",
                        expected=f">= {max(o, c, l)}",
                        actual=str(h),
                    )
                )

            if l > min(o, c, h):
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.OHLC_LOW_BOUND_INVALID,
                        severity=DatasetIssueSeverity.ERROR,
                        message=f"Low ({l}) is greater than min(open={o}, close={c}, high={h}).",
                        row_number=row_num,
                        field="low",
                        expected=f"<= {min(o, c, h)}",
                        actual=str(l),
                    )
                )

            # Check metadata alignment
            if inst_str != expected_instrument:
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.INSTRUMENT_ID_MISMATCH,
                        severity=DatasetIssueSeverity.ERROR,
                        message=f"Row instrument_id '{inst_str}' does not match expected '{expected_instrument}'.",
                        row_number=row_num,
                        field="instrument_id",
                        expected=sanitize_evidence(expected_instrument),
                        actual=sanitize_evidence(inst_str),
                    )
                )

            if tf_str != expected_timeframe:
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.TIMEFRAME_VALUE_MISMATCH,
                        severity=DatasetIssueSeverity.ERROR,
                        message=f"Row timeframe '{tf_str}' does not match expected '{expected_timeframe}'.",
                        row_number=row_num,
                        field="timeframe",
                        expected=sanitize_evidence(expected_timeframe),
                        actual=sanitize_evidence(tf_str),
                    )
                )

            # Check completion state
            is_closed = closed_str in ("true", "1", "yes")
            if is_closed:
                completed_rows += 1
            else:
                incomplete_rows += 1
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.INCOMPLETE_CANDLE_PRESENT,
                        severity=DatasetIssueSeverity.WARNING,
                        message="Incomplete candle present in dataset (is_closed=False).",
                        row_number=row_num,
                        timestamp=dt,
                        field="is_closed",
                        expected="true",
                        actual="false",
                    )
                )

            # Timestamp continuity and ordering check
            if last_valid_ts is not None:
                if dt < last_valid_ts:
                    all_issues.append(
                        DatasetQualityIssue(
                            code=DatasetIssueCode.TIMESTAMP_OUT_OF_ORDER,
                            severity=DatasetIssueSeverity.ERROR,
                            message=f"Timestamp '{dt.isoformat()}' is out of order (previous: '{last_valid_ts.isoformat()}').",
                            row_number=row_num,
                            timestamp=dt,
                            field="timestamp",
                            expected=f"> {last_valid_ts.isoformat()}",
                            actual=dt.isoformat(),
                        )
                    )
                elif dt == last_valid_ts:
                    duplicate_timestamp_count += 1
                    all_issues.append(
                        DatasetQualityIssue(
                            code=DatasetIssueCode.DUPLICATE_TIMESTAMP,
                            severity=DatasetIssueSeverity.ERROR,
                            message=f"Duplicate timestamp '{dt.isoformat()}' detected.",
                            row_number=row_num,
                            timestamp=dt,
                            field="timestamp",
                            actual=dt.isoformat(),
                        )
                    )
                elif expected_interval_sec is not None:
                    diff_sec = int((dt - last_valid_ts).total_seconds())
                    if diff_sec == expected_interval_sec:
                        pass
                    elif diff_sec > expected_interval_sec and diff_sec % expected_interval_sec == 0:
                        missing_count = (diff_sec // expected_interval_sec) - 1
                        missing_interval_count += missing_count
                        all_issues.append(
                            DatasetQualityIssue(
                                code=DatasetIssueCode.MISSING_INTERVAL,
                                severity=DatasetIssueSeverity.WARNING,
                                message=f"Gap of {missing_count} missing interval(s) detected between {last_valid_ts.isoformat()} and {dt.isoformat()}.",
                                row_number=row_num,
                                timestamp=dt,
                                field="timestamp",
                                expected=f"Interval of {expected_interval_sec}s",
                                actual=f"Gap of {diff_sec}s ({missing_count} missing intervals)",
                            )
                        )
                    else:
                        all_issues.append(
                            DatasetQualityIssue(
                                code=DatasetIssueCode.TIMEFRAME_INTERVAL_MISMATCH,
                                severity=DatasetIssueSeverity.ERROR,
                                message=f"Timestamp difference ({diff_sec}s) does not match expected timeframe interval ({expected_interval_sec}s).",
                                row_number=row_num,
                                timestamp=dt,
                                field="timestamp",
                                expected=f"Multiple of {expected_interval_sec}s",
                                actual=f"{diff_sec}s",
                            )
                        )


            last_valid_ts = dt

        # First and last timestamps across successfully parsed valid rows
        first_ts = min(parsed_timestamps) if parsed_timestamps else None
        last_ts = max(parsed_timestamps) if parsed_timestamps else None

        # Warm-up recommendation check
        if completed_rows < MINIMUM_RECOMMENDED_COMPLETED_ROWS:
            all_issues.append(
                DatasetQualityIssue(
                    code=DatasetIssueCode.INSUFFICIENT_DATA_FOR_WARMUP,
                    severity=DatasetIssueSeverity.WARNING,
                    message=f"Dataset contains {completed_rows} completed row(s), which is less than recommended minimum ({MINIMUM_RECOMMENDED_COMPLETED_ROWS}) for indicators like default MACD.",
                    field="completed_rows",
                    expected=f">= {MINIMUM_RECOMMENDED_COMPLETED_ROWS}",
                    actual=str(completed_rows),
                )
            )

        # Manifest metadata and count verification
        if manifest_entry:
            if completed_rows != manifest_entry.completed_candle_count:
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.COMPLETED_COUNT_MISMATCH,
                        severity=DatasetIssueSeverity.ERROR,
                        message=f"Completed candle count ({completed_rows}) does not match manifest count ({manifest_entry.completed_candle_count}).",
                        field="completed_candle_count",
                        expected=str(manifest_entry.completed_candle_count),
                        actual=str(completed_rows),
                    )
                )

            if manifest_entry.instrument_id != expected_instrument or manifest_entry.timeframe != expected_timeframe:
                all_issues.append(
                    DatasetQualityIssue(
                        code=DatasetIssueCode.MANIFEST_METADATA_MISMATCH,
                        severity=DatasetIssueSeverity.ERROR,
                        message="Manifest metadata does not match dataset configuration.",
                        field="manifest_metadata",
                    )
                )

        summary = DatasetQualitySummary(
            total_rows=total_rows,
            valid_rows=valid_rows,
            malformed_rows=malformed_rows,
            completed_rows=completed_rows,
            incomplete_rows=incomplete_rows,
            duplicate_timestamp_count=duplicate_timestamp_count,
            missing_interval_count=missing_interval_count,
            first_timestamp=first_ts,
            last_timestamp=last_ts,
            expected_interval_seconds=expected_interval_sec,
            calculated_checksum=calculated_checksum,
            manifest_checksum=manifest_checksum,
            checksum_matches=checksum_matches,
        )

        sorted_issues = sort_issues(all_issues)
        return cls._build_report(dataset_id, provenance, summary, sorted_issues, warnings)

    @classmethod
    def _build_report(
        cls,
        dataset_id: str,
        provenance: DatasetProvenance,
        summary: DatasetQualitySummary,
        sorted_issues: List[DatasetQualityIssue],
        warnings: List[str],
    ) -> DatasetQualityReport:
        total_issue_count = len(sorted_issues)
        issues_truncated = total_issue_count > MAX_REPORTED_ISSUES

        if issues_truncated:
            reported_issues = sorted_issues[:MAX_REPORTED_ISSUES]
            warnings.append(
                f"Issues list truncated: showing top {MAX_REPORTED_ISSUES} of {total_issue_count} total detected issues."
            )
        else:
            reported_issues = sorted_issues

        # Determine status over ALL issues (not just truncated subset)
        has_error = any(iss.severity == DatasetIssueSeverity.ERROR for iss in sorted_issues)
        has_warning = any(iss.severity == DatasetIssueSeverity.WARNING for iss in sorted_issues)

        if has_error:
            status = DatasetQualityStatus.FAIL
        elif has_warning:
            status = DatasetQualityStatus.WARN
        else:
            status = DatasetQualityStatus.PASS

        return DatasetQualityReport(
            dataset_id=dataset_id,
            status=status,
            provenance=provenance,
            summary=summary,
            issues=reported_issues,
            total_issue_count=total_issue_count,
            reported_issue_count=len(reported_issues),
            issues_truncated=issues_truncated,
            audit_rules_version=AUDIT_RULES_VERSION,
            warnings=warnings,
        )
