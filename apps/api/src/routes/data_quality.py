import logging
from typing import List
from fastapi import APIRouter, HTTPException, Response, Request
from src.engine.dataset_quality_models import (
    DatasetQualityReport,
    DatasetAuditBatchRequest,
    DatasetAuditBatchResponse,
    DatasetQualityListItem,
)
from src.services.dataset_quality_service import DatasetQualityService

logger = logging.getLogger("tradepro.data_quality")

router = APIRouter(prefix="/api/v1/data-quality", tags=["Data Quality"])

@router.get("/datasets", response_model=List[DatasetQualityListItem])
def list_whitelisted_datasets():
    return DatasetQualityService.list_dataset_summaries()

@router.post("/audit", response_model=DatasetAuditBatchResponse)
def batch_audit_datasets(request: DatasetAuditBatchRequest, req: Request):
    # Log validated dataset count safely
    req_id = getattr(req.state, "request_id", "unknown")
    logger.info(
        "batch_audit_requested",
        extra={"request_id": req_id, "dataset_count": len(request.dataset_ids)},
    )
    return DatasetQualityService.batch_audit_datasets(request.dataset_ids)

@router.get("/datasets/{dataset_id}/export")
def export_dataset_quality_report(dataset_id: str):
    report = DatasetQualityService.get_dataset_report(dataset_id)
    return DatasetQualityService.generate_json_export(report)

@router.get("/datasets/{dataset_id}", response_model=DatasetQualityReport)
def get_dataset_quality_report(dataset_id: str):
    return DatasetQualityService.get_dataset_report(dataset_id)
