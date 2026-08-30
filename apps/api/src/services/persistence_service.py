import json
import hashlib
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from src.models import InspectionRun
from src.engine.manifest import get_dataset_entry, get_manifest_checksums_snapshot, MANIFEST_VERSION
from src.engine.historical_replay_evaluator import HistoricalReplayEvaluator
from src.engine.historical_replay_models import HistoricalReplayResult
from src.engine.multi_series_models import ensure_utc_datetime
from src.engine.fingerprint import compute_request_fingerprint, canonicalize_json

logger = logging.getLogger("tradepro.persistence")

MAX_RESULT_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

def sanitize_error_message(err_str: str) -> str:
    # Ensure no file paths, stack traces, or raw SQL are exposed
    clean_msg = err_str.replace("\\", "/").split("\n")[0]
    if "Traceback" in clean_msg or "SQL" in clean_msg or "/src/" in clean_msg:
        return "Historical replay execution failed due to an internal processing error."
    return clean_msg[:2048]

class PersistenceService:

    @staticmethod
    def execute_or_reuse_historical_replay(
        db: Session,
        owner_id: str,
        strategy_payload: Dict[str, Any],
        reference_dataset_id: str,
        subject_dataset_ids: List[str],
        start_timestamp: datetime,
        end_timestamp: datetime,
        sampling_step: int = 1,
        strategy_id: Optional[str] = None,
    ) -> Tuple[InspectionRun, bool]:
        # 1. Datetime normalization
        start_dt = ensure_utc_datetime(start_timestamp)
        end_dt = ensure_utc_datetime(end_timestamp)

        # 2. Compute canonical request fingerprint (preserves subject order and bit-for-bit exact golden vector)
        fingerprint = compute_request_fingerprint(
            strategy_payload=strategy_payload,
            reference_dataset_id=reference_dataset_id,
            subject_dataset_ids=subject_dataset_ids,
            start_timestamp=start_dt,
            end_timestamp=end_dt,
            sampling_step=sampling_step,
        )

        # 3. Check for existing COMPLETED run scoped strictly to this owner_id
        existing_run = (
            db.query(InspectionRun)
            .filter(
                InspectionRun.owner_id == owner_id,
                InspectionRun.completed_fingerprint == fingerprint,
                InspectionRun.status == "COMPLETED",
            )
            .first()
        )
        if existing_run:
            logger.info(f"Reusing existing COMPLETED inspection run '{existing_run.id}' for owner '{owner_id[:8]}...' fingerprint '{fingerprint[:8]}...'")
            return existing_run, True

        ref_entry = get_dataset_entry(reference_dataset_id)
        timeframe = ref_entry.timeframe if ref_entry else "15m"
        current_checksums = get_manifest_checksums_snapshot()

        # 4. Execute replay calculation
        try:
            result: HistoricalReplayResult = HistoricalReplayEvaluator.evaluate_replay(
                strategy_payload=strategy_payload,
                reference_dataset_id=reference_dataset_id,
                subject_dataset_ids=subject_dataset_ids,
                start_timestamp=start_dt,
                end_timestamp=end_dt,
                sampling_step=sampling_step,
                strategy_id=strategy_id,
            )

            result_dict = result.model_dump(mode="json")
            serialized_payload = json.dumps(result_dict)
            if len(serialized_payload.encode("utf-8")) > MAX_RESULT_PAYLOAD_BYTES:
                raise ValueError(f"Serialized replay result payload size ({len(serialized_payload)} bytes) exceeds maximum allowed limit of 10 MB.")

            # 5. Create & save COMPLETED record with owner_id
            now_utc = datetime.now(timezone.utc)
            run = InspectionRun(
                owner_id=owner_id,
                strategy_id=strategy_id,
                strategy_version_snapshot="1.0.0",
                strategy_definition_snapshot=strategy_payload,
                run_type="HISTORICAL_REPLAY",
                reference_dataset_id=reference_dataset_id,
                subject_dataset_ids=subject_dataset_ids,
                requested_start_timestamp=start_dt,
                requested_end_timestamp=end_dt,
                requested_evaluation_timestamp=None,
                timeframe=timeframe,
                engine_version="1.0.0",
                manifest_version=MANIFEST_VERSION,
                created_at=now_utc,
                completed_at=now_utc,
                status="COMPLETED",
                failure_summary=None,
                result_summary=f"Evaluated {result.total_evaluations} series across {result.sampled_timestamp_count} timestamps.",
                result_payload=result_dict,
                synthetic_data_confirmed=True,
                request_fingerprint=fingerprint,
                completed_fingerprint=fingerprint,
                manifest_checksums_snapshot=current_checksums,
            )
            # Update run_id in payload
            result_dict["run_id"] = run.id
            run.result_payload = result_dict

            db.add(run)
            db.commit()
            db.refresh(run)
            return run, False

        except IntegrityError:
            # Race condition: concurrent duplicate request for same owner committed first
            db.rollback()
            concurrent_run = (
                db.query(InspectionRun)
                .filter(
                    InspectionRun.owner_id == owner_id,
                    InspectionRun.completed_fingerprint == fingerprint,
                    InspectionRun.status == "COMPLETED",
                )
                .first()
            )
            if concurrent_run:
                return concurrent_run, True
            raise

        except Exception as e:
            # Rollback partial transaction write
            db.rollback()
            sanitized_msg = sanitize_error_message(str(e))

            # Record FAILED status in a separate short transaction
            try:
                now_utc = datetime.now(timezone.utc)
                failed_run = InspectionRun(
                    owner_id=owner_id,
                    strategy_id=strategy_id,
                    strategy_version_snapshot="1.0.0",
                    strategy_definition_snapshot=strategy_payload,
                    run_type="HISTORICAL_REPLAY",
                    reference_dataset_id=reference_dataset_id,
                    subject_dataset_ids=subject_dataset_ids,
                    requested_start_timestamp=start_dt,
                    requested_end_timestamp=end_dt,
                    timeframe=timeframe,
                    engine_version="1.0.0",
                    manifest_version=MANIFEST_VERSION,
                    created_at=now_utc,
                    completed_at=None,
                    status="FAILED",
                    failure_summary=sanitized_msg,
                    result_summary=None,
                    result_payload=None,
                    synthetic_data_confirmed=True,
                    request_fingerprint=fingerprint,
                    completed_fingerprint=None,  # Null completed_fingerprint allows retries without unique constraint error
                    manifest_checksums_snapshot=current_checksums,
                )
                db.add(failed_run)
                db.commit()
            except Exception as fail_e:
                logger.error(f"Failed to record FAILED inspection run: {fail_e}")
                db.rollback()

            raise ValueError(sanitized_msg) from e
