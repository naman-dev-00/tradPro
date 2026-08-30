import io
import csv
import json
import re
from typing import Dict, Any
from fastapi import HTTPException, Response
from src.models import InspectionRun
from src.engine.replay_comparison_engine import ReplayComparisonEngine

EDUCATIONAL_NOTICE = (
    "Educational synthetic historical replay inspection data only. "
    "This export does not contain trading signals, execution orders, recommendations, "
    "or profitability calculations."
)

MAX_EXPORT_BYTES = 10 * 1024 * 1024  # 10 MB

def is_numeric_value(val: Any) -> bool:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return True
    if isinstance(val, str):
        # Check if string represents a pure numeric value (int or float)
        # e.g., "-1.5", "+42", "100"
        val_stripped = val.strip()
        if re.match(r"^[\+\-]?\d+(\.\d+)?$", val_stripped):
            return True
    return False

def sanitize_csv_cell(value: Any) -> str:
    if value is None:
        return ""

    if is_numeric_value(value):
        return str(value)

    val_str = str(value)
    raw_first = val_str[0] if val_str else ""
    stripped = val_str.lstrip()
    stripped_first = stripped[0] if stripped else ""

    # Formula injection protection: check raw or stripped first character
    if raw_first in ("=", "+", "-", "@", "\t", "\r") or stripped_first in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + val_str

    return val_str


def sanitize_filename(run_id: str, ext: str) -> str:
    # Ensure run_id is sanitized UUID/alphanumeric string without path characters
    clean_id = re.sub(r"[^a-zA-Z0-9\-_]", "", str(run_id))
    if not clean_id:
        clean_id = "export"
    return f"replay_{clean_id}.{ext}"

class ExportService:

    @staticmethod
    def generate_json_export(run: InspectionRun) -> Response:
        if run.status != "COMPLETED":
            raise HTTPException(status_code=400, detail="Only COMPLETED inspection runs can be exported.")

        repro_verification = ReplayComparisonEngine.verify_run(run)

        export_dict = {
            "notice": EDUCATIONAL_NOTICE,
            "run_id": run.id,
            "strategy_id": run.strategy_id,
            "run_type": run.run_type,
            "reference_dataset_id": run.reference_dataset_id,
            "subject_dataset_ids": run.subject_dataset_ids,
            "requested_start_timestamp": run.requested_start_timestamp.isoformat() if run.requested_start_timestamp else None,
            "requested_end_timestamp": run.requested_end_timestamp.isoformat() if run.requested_end_timestamp else None,
            "timeframe": run.timeframe,
            "engine_version": run.engine_version,
            "manifest_version": run.manifest_version,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "status": run.status,
            "result_summary": run.result_summary,
            "reproducibility_verification": repro_verification.model_dump(mode="json"),
            "strategy_snapshot": run.strategy_definition_snapshot,
            "result_payload": run.result_payload,
        }

        json_bytes = json.dumps(export_dict, indent=2, sort_keys=True).encode("utf-8")
        if len(json_bytes) > MAX_EXPORT_BYTES:
            raise HTTPException(status_code=400, detail=f"Export payload size ({len(json_bytes)} bytes) exceeds 10 MB limit.")

        filename = sanitize_filename(run.id, "json")
        return Response(
            content=json_bytes,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @staticmethod
    def generate_csv_export(run: InspectionRun) -> Response:
        if run.status != "COMPLETED":
            raise HTTPException(status_code=400, detail="Only COMPLETED inspection runs can be exported.")

        if not run.result_payload or "replay_points" not in run.result_payload:
            raise HTTPException(status_code=400, detail="Run does not contain valid replay_points payload for CSV export.")

        repro_verification = ReplayComparisonEngine.verify_run(run)

        output = io.StringIO()
        writer = csv.writer(output)

        # Header comment with notice and reproducibility metadata
        writer.writerow([f"# NOTICE: {EDUCATIONAL_NOTICE}"])
        writer.writerow([f"# RUN_ID: {run.id}"])
        writer.writerow([f"# REPRODUCIBILITY_STATUS: {repro_verification.verification_status}"])
        writer.writerow([f"# ENGINE_VERSION: {run.engine_version}"])
        writer.writerow([f"# MANIFEST_VERSION: {run.manifest_version}"])
        writer.writerow([])

        # Table Header
        writer.writerow(["evaluation_timestamp", "dataset_id", "status", "passed_conditions", "failed_conditions", "inspection_summary"])


        for point in run.result_payload.get("replay_points", []):
            eval_ts = point.get("evaluation_timestamp", "")
            for res in point.get("results", []):
                ds_id = res.get("dataset_id", "")
                status_val = res.get("overall_status", res.get("status", ""))
                passed_conds = ";".join(res.get("passed_condition_ids", []))
                failed_conds = ";".join(res.get("failed_condition_ids", []))
                summary_text = res.get("inspection_summary", "")

                writer.writerow([
                    sanitize_csv_cell(eval_ts),
                    sanitize_csv_cell(ds_id),
                    sanitize_csv_cell(status_val),
                    sanitize_csv_cell(passed_conds),
                    sanitize_csv_cell(failed_conds),
                    sanitize_csv_cell(summary_text),
                ])

        csv_bytes = output.getvalue().encode("utf-8")
        if len(csv_bytes) > MAX_EXPORT_BYTES:
            raise HTTPException(status_code=400, detail=f"Export payload size ({len(csv_bytes)} bytes) exceeds 10 MB limit.")

        filename = sanitize_filename(run.id, "csv")
        return Response(
            content=csv_bytes,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
