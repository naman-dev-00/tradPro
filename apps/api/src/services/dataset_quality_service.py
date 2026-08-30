import os
import io
import json
import re
from typing import List, Dict, Optional, Any
from fastapi import HTTPException, Response
from src.engine.manifest import (
    MANIFEST_METADATA,
    FIXTURES_DIR,
    calculate_file_sha256,
    get_dataset_entry,
    MANIFEST_VERSION,
    DatasetCategory,
)
from src.engine.dataset_quality_models import (
    DatasetQualityStatus,
    DatasetIssueSeverity,
    DatasetIssueCode,
    DatasetQualityIssue,
    DatasetQualitySummary,
    DatasetProvenance,
    DatasetQualityReport,
    DatasetAuditBatchResponse,
    DatasetQualityListItem,
)
from src.engine.dataset_quality_engine import DatasetQualityEngine

EDUCATIONAL_NOTICE = (
    "Packaged synthetic educational data only. Quality findings describe dataset "
    "integrity and provenance, not market quality, recommendations, rankings, or expected outcomes."
)

MAX_EXPORT_BYTES = 5 * 1024 * 1024  # 5 MB

def get_whitelisted_metadata_map() -> Dict[str, Dict[str, Any]]:
    return {meta["dataset_id"]: meta for meta in MANIFEST_METADATA}

class DatasetQualityService:
    @classmethod
    def get_provenance(cls, dataset_id: str) -> DatasetProvenance:
        meta_map = get_whitelisted_metadata_map()
        if dataset_id not in meta_map:
            raise HTTPException(status_code=404, detail=f"Synthetic dataset '{dataset_id}' not found in manifest whitelist.")

        meta = meta_map[dataset_id]
        filepath = os.path.abspath(os.path.join(FIXTURES_DIR, meta["filename"]))

        # Path traversal guard
        if not filepath.startswith(os.path.abspath(FIXTURES_DIR)):
            raise HTTPException(status_code=400, detail="Invalid fixture path resolution.")

        fixture_checksum: Optional[str] = None
        if os.path.exists(filepath):
            fixture_checksum = calculate_file_sha256(filepath)

        return DatasetProvenance(
            dataset_id=dataset_id,
            display_name=meta["display_name"],
            category=meta["category"],
            instrument_id=meta["instrument_id"],
            timeframe=meta["timeframe"],
            is_synthetic=True,
            manifest_version=MANIFEST_VERSION,
            fixture_checksum=fixture_checksum,
            source_type="PACKAGED_SYNTHETIC_FIXTURE",
            immutable=True,
        )

    @classmethod
    def get_dataset_report(cls, dataset_id: str) -> DatasetQualityReport:
        meta_map = get_whitelisted_metadata_map()
        if dataset_id not in meta_map:
            raise HTTPException(status_code=404, detail=f"Synthetic dataset '{dataset_id}' not found in manifest whitelist.")

        meta = meta_map[dataset_id]
        filepath = os.path.abspath(os.path.join(FIXTURES_DIR, meta["filename"]))

        if not filepath.startswith(os.path.abspath(FIXTURES_DIR)):
            raise HTTPException(status_code=400, detail="Invalid fixture path resolution.")

        provenance = cls.get_provenance(dataset_id)
        manifest_entry = get_dataset_entry(dataset_id)

        if not os.path.exists(filepath):
            # File missing
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
                expected_interval_seconds=None,
                calculated_checksum=None,
                manifest_checksum=manifest_entry.dataset_checksum if manifest_entry else None,
                checksum_matches=False,
            )
            issue = DatasetQualityIssue(
                code=DatasetIssueCode.FILE_UNAVAILABLE,
                severity=DatasetIssueSeverity.ERROR,
                message=f"Packaged fixture file for dataset '{dataset_id}' is unavailable on filesystem.",
                field="file",
            )
            return DatasetQualityReport(
                dataset_id=dataset_id,
                status=DatasetQualityStatus.FAIL,
                provenance=provenance,
                summary=summary,
                issues=[issue],
                total_issue_count=1,
                reported_issue_count=1,
                issues_truncated=False,
                warnings=[],
            )

        calculated_checksum = calculate_file_sha256(filepath)
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            csv_content = f.read()

        return DatasetQualityEngine.audit_dataset_content(
            dataset_id=dataset_id,
            csv_content=csv_content,
            provenance=provenance,
            manifest_entry=manifest_entry,
            calculated_checksum=calculated_checksum,
        )

    @classmethod
    def list_dataset_summaries(cls) -> List[DatasetQualityListItem]:
        meta_map = get_whitelisted_metadata_map()
        items: List[DatasetQualityListItem] = []

        for meta in MANIFEST_METADATA:
            ds_id = meta["dataset_id"]
            report = cls.get_dataset_report(ds_id)
            items.append(
                DatasetQualityListItem(
                    dataset_id=ds_id,
                    display_name=report.provenance.display_name,
                    category=report.provenance.category,
                    instrument_id=report.provenance.instrument_id,
                    timeframe=report.provenance.timeframe,
                    status=report.status,
                    summary=report.summary,
                    provenance=report.provenance,
                )
            )
        return items

    @classmethod
    def batch_audit_datasets(cls, dataset_ids: List[str]) -> DatasetAuditBatchResponse:
        reports: List[DatasetQualityReport] = []
        status_counts = {"PASS": 0, "WARN": 0, "FAIL": 0}

        for ds_id in dataset_ids:
            report = cls.get_dataset_report(ds_id)
            reports.append(report)
            status_counts[report.status.value] += 1

        return DatasetAuditBatchResponse(
            reports=reports,
            status_counts=status_counts,
            total_datasets=len(reports),
            audit_rules_version="1.0.0",
            warnings=[],
        )

    @classmethod
    def generate_json_export(cls, report: DatasetQualityReport) -> Response:
        export_payload = {
            "notice": EDUCATIONAL_NOTICE,
            "report": report.model_dump(mode="json"),
        }

        # Deterministic JSON serialization
        json_str = json.dumps(export_payload, sort_keys=True, indent=2)
        json_bytes = json_str.encode("utf-8")

        if len(json_bytes) > MAX_EXPORT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Export payload size ({len(json_bytes)} bytes) exceeds the maximum allowed 5 MB limit.",
            )

        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", report.dataset_id)
        filename = f"data_quality_{safe_id}.json"

        return Response(
            content=json_bytes,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "application/json; charset=utf-8",
            },
        )
