from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from src.engine.manifest import (
    get_dataset_entry,
    load_dataset_candles,
    DatasetCategory,
    compare_manifest_checksums,
    get_manifest_checksums_snapshot,
)
from src.engine.rule_models import EvaluationStatus
from src.engine.multi_series_evaluator import MultiSeriesEvaluator
from src.engine.historical_replay_models import (
    ReplayPoint,
    SubjectStatusTimeline,
    HistoricalReplayResult,
)
from src.engine.multi_series_models import ensure_utc_datetime

MAX_SUBJECTS = 20
MAX_SAMPLED_TIMESTAMPS = 1000
MAX_TOTAL_EVALUATIONS = 20000

class HistoricalReplayEvaluator:

    @staticmethod
    def evaluate_replay(
        strategy_payload: Dict[str, Any],
        reference_dataset_id: str,
        subject_dataset_ids: List[str],
        start_timestamp: datetime,
        end_timestamp: datetime,
        sampling_step: int = 1,
        strategy_id: Optional[str] = None,
    ) -> HistoricalReplayResult:
        # 1. Datetime validation & normalization
        start_dt = ensure_utc_datetime(start_timestamp)
        end_dt = ensure_utc_datetime(end_timestamp)

        if start_dt > end_dt:
            raise ValueError(f"start_timestamp ({start_dt.isoformat()}) must be less than or equal to end_timestamp ({end_dt.isoformat()}).")

        if sampling_step < 1:
            raise ValueError("sampling_step must be a positive integer >= 1.")

        # 2. Manifest validation
        ref_entry = get_dataset_entry(reference_dataset_id)
        if not ref_entry:
            raise ValueError(f"Unknown reference dataset ID '{reference_dataset_id}'.")
        if ref_entry.category != DatasetCategory.REFERENCE:
            raise ValueError(f"Dataset '{reference_dataset_id}' has category '{ref_entry.category.value}', but 'REFERENCE' is required.")

        if not subject_dataset_ids:
            raise ValueError("At least 1 subject dataset ID must be selected.")
        if len(subject_dataset_ids) > MAX_SUBJECTS:
            raise ValueError(f"Maximum {MAX_SUBJECTS} subject datasets allowed per replay request (got {len(subject_dataset_ids)}).")

        subject_entries = []
        for s_id in subject_dataset_ids:
            s_entry = get_dataset_entry(s_id)
            if not s_entry:
                raise ValueError(f"Unknown subject dataset ID '{s_id}'.")
            if s_entry.category != DatasetCategory.SUBJECT:
                raise ValueError(f"Dataset '{s_id}' has category '{s_entry.category.value}', but 'SUBJECT' is required.")
            subject_entries.append(s_entry)

        # 3. Load fixture candles ONCE into precomputed context
        ref_all_candles = load_dataset_candles(reference_dataset_id)
        ref_completed_candles = [c for c in ref_all_candles if c.is_closed]

        # Filter completed reference candles in target time range [start_dt, end_dt]
        bounded_ref_candles = [
            c for c in ref_completed_candles
            if start_dt <= ensure_utc_datetime(c.timestamp) <= end_dt
        ]

        if not bounded_ref_candles:
            raise ValueError(
                f"No completed reference candles found in range [{start_dt.isoformat()} to {end_dt.isoformat()}]."
            )

        # Sample timestamps according to sampling_step
        sampled_ref_candles = bounded_ref_candles[::sampling_step]
        sampled_timestamp_count = len(sampled_ref_candles)

        if sampled_timestamp_count > MAX_SAMPLED_TIMESTAMPS:
            raise ValueError(
                f"Requested sampling yielded {sampled_timestamp_count} timestamps, exceeding maximum limit of {MAX_SAMPLED_TIMESTAMPS}."
            )

        total_evaluations = len(subject_dataset_ids) * sampled_timestamp_count
        if total_evaluations > MAX_TOTAL_EVALUATIONS:
            raise ValueError(
                f"Total evaluations ({len(subject_dataset_ids)} subjects * {sampled_timestamp_count} timestamps = {total_evaluations}) exceeds maximum limit of {MAX_TOTAL_EVALUATIONS}."
            )

        # 4. Execute MultiSeriesEvaluator at each sampled timestamp (no future leakage)
        multi_series_eval = MultiSeriesEvaluator()
        replay_points: List[ReplayPoint] = []
        aggregate_status_counts = {
            EvaluationStatus.TRUE.value: 0,
            EvaluationStatus.FALSE.value: 0,
            EvaluationStatus.UNAVAILABLE.value: 0,
            EvaluationStatus.INVALID.value: 0,
        }

        # Track per-subject timeline sequences
        subject_timeline_points: Dict[str, List[Dict[str, Any]]] = {s_id: [] for s_id in subject_dataset_ids}

        for ref_candle in sampled_ref_candles:
            eval_dt = ensure_utc_datetime(ref_candle.timestamp)

            res = multi_series_eval.evaluate_multi_series(
                strategy_payload=strategy_payload,
                reference_dataset_id=reference_dataset_id,
                subject_dataset_ids=subject_dataset_ids,
                eval_timestamp=eval_dt,
                strategy_id=strategy_id,
            )

            # Build ReplayPoint
            replay_point = ReplayPoint(
                evaluation_timestamp=res.requested_evaluation_timestamp,
                reference_timestamp_used=res.reference_timestamp_used,
                results=res.results,
                status_counts=res.status_counts,
                warnings=res.warnings,
            )
            replay_points.append(replay_point)

            # Accumulate status counts
            for status_key, count in res.status_counts.items():
                aggregate_status_counts[status_key] = aggregate_status_counts.get(status_key, 0) + count

            # Record subject point status
            for s_res in res.results:
                subject_timeline_points[s_res.dataset_id].append({
                    "timestamp": eval_dt,
                    "status": s_res.overall_status,
                    "inspection_summary": s_res.inspection_summary,
                })

        # 5. Build SubjectStatusTimelines
        subject_timelines: List[SubjectStatusTimeline] = []
        for s_id in subject_dataset_ids:
            points = subject_timeline_points[s_id]
            transitions: Dict[str, int] = {}
            runs: Dict[str, int] = {
                EvaluationStatus.TRUE.value: 0,
                EvaluationStatus.FALSE.value: 0,
                EvaluationStatus.UNAVAILABLE.value: 0,
                EvaluationStatus.INVALID.value: 0,
            }
            curr_run_status: Optional[str] = None
            curr_run_len = 0

            first_available_dt: Optional[datetime] = None
            unavail_count = 0
            invalid_count = 0

            prev_status: Optional[str] = None

            for pt in points:
                st_val = pt["status"].value if isinstance(pt["status"], EvaluationStatus) else str(pt["status"])

                if st_val == EvaluationStatus.UNAVAILABLE.value:
                    unavail_count += 1
                elif st_val == EvaluationStatus.INVALID.value:
                    invalid_count += 1
                elif first_available_dt is None:
                    first_available_dt = pt["timestamp"]

                # Track transitions
                if prev_status is not None and prev_status != st_val:
                    trans_key = f"{prev_status}_TO_{st_val}"
                    transitions[trans_key] = transitions.get(trans_key, 0) + 1

                # Track run lengths
                if st_val == curr_run_status:
                    curr_run_len += 1
                else:
                    if curr_run_status is not None:
                        runs[curr_run_status] = max(runs.get(curr_run_status, 0), curr_run_len)
                    curr_run_status = st_val
                    curr_run_len = 1

                prev_status = st_val

            if curr_run_status is not None:
                runs[curr_run_status] = max(runs.get(curr_run_status, 0), curr_run_len)

            timeline = SubjectStatusTimeline(
                dataset_id=s_id,
                points=points,
                transition_counts=transitions,
                consecutive_status_runs=runs,
                first_available_timestamp=first_available_dt,
                unavailable_point_count=unavail_count,
                invalid_point_count=invalid_count,
            )
            subject_timelines.append(timeline)

        # 6. Reproducibility metadata
        current_checksums = get_manifest_checksums_snapshot()
        reproducibility = {
            "is_exact_match": True,
            "manifest_checksums": current_checksums,
            "warning": None,
        }

        return HistoricalReplayResult(
            strategy_id=strategy_id,
            start_timestamp=start_dt,
            end_timestamp=end_dt,
            sampling_step=sampling_step,
            sampled_timestamp_count=sampled_timestamp_count,
            total_evaluations=total_evaluations,
            reference_dataset_id=reference_dataset_id,
            reference_metadata={
                "dataset_id": ref_entry.dataset_id,
                "display_name": ref_entry.display_name,
                "timeframe": ref_entry.timeframe,
                "completed_candle_count": ref_entry.completed_candle_count,
            },
            subject_dataset_ids=subject_dataset_ids,
            subject_metadata=[
                {
                    "dataset_id": se.dataset_id,
                    "display_name": se.display_name,
                    "timeframe": se.timeframe,
                    "completed_candle_count": se.completed_candle_count,
                }
                for se in subject_entries
            ],
            replay_points=replay_points,
            subject_timelines=subject_timelines,
            aggregate_status_counts=aggregate_status_counts,
            reproducibility=reproducibility,
            warnings=[],
        )
