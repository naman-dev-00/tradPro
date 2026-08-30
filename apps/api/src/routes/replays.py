import io
import csv
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session
from src.database import get_db
from src.models import InspectionRun, Strategy
from src.services.persistence_service import PersistenceService
from src.engine.manifest import compare_manifest_checksums
from src.engine.multi_series_models import ensure_utc_datetime
from src.engine.replay_comparison_models import (
    ReplayVerificationResult,
    ReplayComparisonRequest,
    ReplayComparisonResult,
)
from src.engine.replay_comparison_engine import ReplayComparisonEngine
from src.services.export_service import ExportService

router = APIRouter(prefix="/api/v1/replays", tags=["Historical Replays & Persistence"])



class HistoricalReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_payload: Optional[Dict[str, Any]] = None
    strategy_id: Optional[str] = None
    reference_dataset_id: str
    subject_dataset_ids: List[str]
    start_timestamp: datetime
    end_timestamp: datetime
    sampling_step: int = 1

    @field_validator("start_timestamp", "end_timestamp", mode="before")
    def validate_utc(cls, v):
        return ensure_utc_datetime(v)

class InspectionRunSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    strategy_id: Optional[str]
    strategy_name: Optional[str] = None
    run_type: str
    reference_dataset_id: Optional[str]
    subject_dataset_ids: List[str]
    requested_start_timestamp: Optional[datetime]
    requested_end_timestamp: Optional[datetime]
    requested_evaluation_timestamp: Optional[datetime]
    timeframe: str
    engine_version: str
    manifest_version: str
    created_at: datetime
    completed_at: Optional[datetime]
    status: str
    failure_summary: Optional[str]
    result_summary: Optional[str]
    synthetic_data_confirmed: bool
    is_exact_match: bool = True

class PaginatedInspectionRunList(BaseModel):
    items: List[InspectionRunSummaryResponse]
    total: int
    page: int
    page_size: int
    total_pages: int

def sanitize_csv_cell(value: Any) -> str:
    val_str = str(value) if value is not None else ""
    stripped = val_str.lstrip()
    if stripped and stripped[0] in ("=", "+", "-", "@"):
        return "'" + val_str
    return val_str

@router.post("", status_code=status.HTTP_201_CREATED)
def create_historical_replay(
    req: HistoricalReplayRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    strategy_dict = req.strategy_payload
    if not strategy_dict and req.strategy_id:
        strat = db.query(Strategy).filter(Strategy.id == req.strategy_id).first()
        if not strat:
            raise HTTPException(status_code=404, detail=f"Strategy with ID '{req.strategy_id}' not found.")
        strategy_dict = strat.payload

    if not strategy_dict:
        raise HTTPException(
            status_code=400,
            detail="Either 'strategy_payload' or a valid 'strategy_id' must be provided.",
        )

    try:
        run, is_reused = PersistenceService.execute_or_reuse_historical_replay(
            db=db,
            strategy_payload=strategy_dict,
            reference_dataset_id=req.reference_dataset_id,
            subject_dataset_ids=req.subject_dataset_ids,
            start_timestamp=req.start_timestamp,
            end_timestamp=req.end_timestamp,
            sampling_step=req.sampling_step,
            strategy_id=req.strategy_id,
        )

        if is_reused:
            response.status_code = status.HTTP_200_OK

        return {
            "run_id": run.id,
            "status": run.status,
            "is_reused": is_reused,
            "run": {
                "id": run.id,
                "strategy_id": run.strategy_id,
                "run_type": run.run_type,
                "reference_dataset_id": run.reference_dataset_id,
                "subject_dataset_ids": run.subject_dataset_ids,
                "requested_start_timestamp": run.requested_start_timestamp.isoformat() if run.requested_start_timestamp else None,
                "requested_end_timestamp": run.requested_end_timestamp.isoformat() if run.requested_end_timestamp else None,
                "timeframe": run.timeframe,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "status": run.status,
                "result_summary": run.result_summary,
                "result_payload": run.result_payload,
            }
        }
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as err:
        raise HTTPException(status_code=500, detail="Historical replay creation failed.")

@router.get("", response_model=PaginatedInspectionRunList)
def list_inspection_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    strategy_id: Optional[str] = None,
    status: Optional[str] = None,
    run_type: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    db: Session = Depends(get_db),
):
    query = db.query(InspectionRun)

    if strategy_id:
        query = query.filter(InspectionRun.strategy_id == strategy_id)
    if status:
        query = query.filter(InspectionRun.status == status)
    if run_type:
        query = query.filter(InspectionRun.run_type == run_type)
    if start_date:
        query = query.filter(InspectionRun.created_at >= ensure_utc_datetime(start_date))
    if end_date:
        query = query.filter(InspectionRun.created_at <= ensure_utc_datetime(end_date))

    total = query.count()
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    runs = (
        query.order_by(InspectionRun.created_at.desc(), InspectionRun.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = []
    for r in runs:
        strat_name = None
        if r.strategy_definition_snapshot:
            strat_name = r.strategy_definition_snapshot.get("name")

        checksum_res = compare_manifest_checksums(r.manifest_checksums_snapshot)

        items.append(
            InspectionRunSummaryResponse(
                id=r.id,
                strategy_id=r.strategy_id,
                strategy_name=strat_name,
                run_type=r.run_type,
                reference_dataset_id=r.reference_dataset_id,
                subject_dataset_ids=r.subject_dataset_ids or [],
                requested_start_timestamp=r.requested_start_timestamp,
                requested_end_timestamp=r.requested_end_timestamp,
                requested_evaluation_timestamp=r.requested_evaluation_timestamp,
                timeframe=r.timeframe,
                engine_version=r.engine_version,
                manifest_version=r.manifest_version,
                created_at=r.created_at,
                completed_at=r.completed_at,
                status=r.status,
                failure_summary=r.failure_summary,
                result_summary=r.result_summary,
                synthetic_data_confirmed=r.synthetic_data_confirmed,
                is_exact_match=checksum_res["is_exact_match"],
            )
        )

    return PaginatedInspectionRunList(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )

@router.get("/{run_id}")
def get_inspection_run_detail(run_id: str, db: Session = Depends(get_db)):
    run = db.query(InspectionRun).filter(InspectionRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Inspection run with ID '{run_id}' not found.")

    repro = compare_manifest_checksums(run.manifest_checksums_snapshot)

    return {
        "id": run.id,
        "strategy_id": run.strategy_id,
        "strategy_version_snapshot": run.strategy_version_snapshot,
        "strategy_definition_snapshot": run.strategy_definition_snapshot,
        "run_type": run.run_type,
        "reference_dataset_id": run.reference_dataset_id,
        "subject_dataset_ids": run.subject_dataset_ids,
        "requested_start_timestamp": run.requested_start_timestamp.isoformat() if run.requested_start_timestamp else None,
        "requested_end_timestamp": run.requested_end_timestamp.isoformat() if run.requested_end_timestamp else None,
        "requested_evaluation_timestamp": run.requested_evaluation_timestamp.isoformat() if run.requested_evaluation_timestamp else None,
        "timeframe": run.timeframe,
        "engine_version": run.engine_version,
        "manifest_version": run.manifest_version,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "status": run.status,
        "failure_summary": run.failure_summary,
        "result_summary": run.result_summary,
        "result_payload": run.result_payload,
        "synthetic_data_confirmed": run.synthetic_data_confirmed,
        "reproducibility": repro,
    }

@router.get("/{run_id}/reproducibility")
def get_inspection_run_reproducibility(run_id: str, db: Session = Depends(get_db)):
    run = db.query(InspectionRun).filter(InspectionRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Inspection run with ID '{run_id}' not found.")

    repro = compare_manifest_checksums(run.manifest_checksums_snapshot)
    return {
        "run_id": run.id,
        "is_exact_match": repro["is_exact_match"],
        "mismatches": repro["mismatches"],
        "warning": repro["warning"],
        "stored_checksums": run.manifest_checksums_snapshot,
    }

@router.get("/{run_id}/verify", response_model=ReplayVerificationResult)
def verify_inspection_run(run_id: str, db: Session = Depends(get_db)):
    run = db.query(InspectionRun).filter(InspectionRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Inspection run with ID '{run_id}' not found.")
    try:
        return ReplayComparisonEngine.verify_run(run)
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception:
        raise HTTPException(status_code=500, detail="Verification processing failed.")

@router.post("/compare", response_model=ReplayComparisonResult)
def compare_historical_replays(
    req: ReplayComparisonRequest,
    db: Session = Depends(get_db),
):
    if req.baseline_run_id == req.comparison_run_id:
        raise HTTPException(status_code=400, detail="Cannot compare a historical replay run with itself.")

    baseline_run = db.query(InspectionRun).filter(InspectionRun.id == req.baseline_run_id).first()
    if not baseline_run:
        raise HTTPException(status_code=404, detail=f"Baseline inspection run with ID '{req.baseline_run_id}' not found.")

    comparison_run = db.query(InspectionRun).filter(InspectionRun.id == req.comparison_run_id).first()
    if not comparison_run:
        raise HTTPException(status_code=404, detail=f"Comparison inspection run with ID '{req.comparison_run_id}' not found.")

    try:
        return ReplayComparisonEngine.compare_runs(
            baseline_run=baseline_run,
            comparison_run=comparison_run,
            include_unchanged=req.include_unchanged,
        )
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as err:
        raise HTTPException(status_code=500, detail="Replay comparison failed.")

@router.get("/{run_id}/export")
def export_inspection_run(
    run_id: str,
    format: str = Query("json"),
    db: Session = Depends(get_db),
):
    fmt = format.lower().strip()
    if fmt not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="Export format must be either 'json' or 'csv'.")

    run = db.query(InspectionRun).filter(InspectionRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Inspection run with ID '{run_id}' not found.")

    if fmt == "csv":
        return ExportService.generate_csv_export(run)
    return ExportService.generate_json_export(run)

@router.get("/{run_id}/export.json")
def export_inspection_run_json(run_id: str, db: Session = Depends(get_db)):
    run = db.query(InspectionRun).filter(InspectionRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Inspection run with ID '{run_id}' not found.")
    return ExportService.generate_json_export(run)

@router.get("/{run_id}/export.csv")
def export_inspection_run_csv(run_id: str, db: Session = Depends(get_db)):
    run = db.query(InspectionRun).filter(InspectionRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Inspection run with ID '{run_id}' not found.")
    return ExportService.generate_csv_export(run)
