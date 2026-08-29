from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set
from src.engine.models import Candle
from src.engine.rule_models import EvaluationStatus, ConditionResult, GroupResult
from src.engine.multi_series_models import SeriesEvaluationResult, MultiSeriesEvaluationResult, ensure_utc_datetime
from src.engine.manifest import (
    get_dataset_entry,
    load_dataset_candles,
    DatasetCategory,
)
from src.engine.evaluator import RuleEvaluator

MAX_SUBJECTS_PER_REQUEST = 20
MAX_COMBINED_COMPLETED_CANDLES = 50000

class MultiSeriesEvaluator:
    def __init__(self, rule_evaluator: Optional[RuleEvaluator] = None):
        self.rule_evaluator = rule_evaluator or RuleEvaluator()

    def evaluate_multi_series(
        self,
        strategy_payload: Dict[str, Any],
        reference_dataset_id: str,
        subject_dataset_ids: List[str],
        eval_timestamp: datetime,
        strategy_id: Optional[str] = None,
    ) -> MultiSeriesEvaluationResult:
        # 1. Validate eval_timestamp (timezone-aware UTC)
        eval_dt = ensure_utc_datetime(eval_timestamp)

        # 2. Validate Subject List Constraints (Request-Level 4xx Errors)
        if not subject_dataset_ids:
            raise ValueError("Evaluation request must specify at least 1 subject_dataset_id.")
        if len(subject_dataset_ids) > MAX_SUBJECTS_PER_REQUEST:
            raise ValueError(f"Evaluation request exceeds maximum limit of {MAX_SUBJECTS_PER_REQUEST} subject datasets.")

        # Check Duplicate Subject IDs
        seen_subjects: Set[str] = set()
        for sid in subject_dataset_ids:
            if sid in seen_subjects:
                raise ValueError(f"Duplicate subject_dataset_id '{sid}' in request.")
            seen_subjects.add(sid)

        # 3. Validate Reference Dataset (Request-Level 4xx Error)
        ref_entry = get_dataset_entry(reference_dataset_id)
        if not ref_entry:
            raise ValueError(f"Unknown reference dataset ID '{reference_dataset_id}'.")
        if ref_entry.category != DatasetCategory.REFERENCE:
            raise ValueError(f"Dataset '{reference_dataset_id}' has category '{ref_entry.category.value}', expected '{DatasetCategory.REFERENCE.value}'.")

        strategy_timeframe = strategy_payload.get("timeframe", "15m")
        if ref_entry.timeframe != strategy_timeframe:
            raise ValueError(f"Reference dataset timeframe '{ref_entry.timeframe}' does not match strategy timeframe '{strategy_timeframe}'.")

        ref_candles = load_dataset_candles(reference_dataset_id)

        # 4. Validate All Subject Dataset IDs & Category (Request-Level 4xx Errors)
        subject_entries = []
        for sid in subject_dataset_ids:
            entry = get_dataset_entry(sid)
            if not entry:
                raise ValueError(f"Unknown subject dataset ID '{sid}'.")
            if entry.category != DatasetCategory.SUBJECT:
                raise ValueError(f"Dataset '{sid}' has category '{entry.category.value}', expected '{DatasetCategory.SUBJECT.value}'.")
            subject_entries.append(entry)

        # 5. Validate Combined Candle Limits Across Reference + Subjects (Request-Level 4xx Error)
        total_candles = ref_entry.completed_candle_count + sum(e.completed_candle_count for e in subject_entries)
        if total_candles > MAX_COMBINED_COMPLETED_CANDLES:
            raise ValueError(
                f"Combined request candle count ({total_candles}) exceeds maximum limit of {MAX_COMBINED_COMPLETED_CANDLES} candles."
            )

        # 6. Evaluate Reference Scope ONCE (Shared Reference Calculation)
        try:
            ref_result, ref_time_dt = self._evaluate_reference_once(strategy_payload, ref_candles, eval_dt)
        except Exception as e:
            # Reference dataset failure rejects entire request (Request-Level 4xx Error)
            raise ValueError(f"Failed reference series evaluation: {str(e)}")

        # 7. Evaluate Subject Series Independently (Preserving Exact Input Order)
        results: List[SeriesEvaluationResult] = []
        status_counts = {"TRUE": 0, "FALSE": 0, "UNAVAILABLE": 0, "INVALID": 0}

        for sid in subject_dataset_ids:
            ser_res = self._evaluate_single_subject(
                strategy_payload=strategy_payload,
                subject_dataset_id=sid,
                ref_result=ref_result,
                eval_dt=eval_dt,
            )
            results.append(ser_res)
            st_key = ser_res.overall_status.value
            status_counts[st_key] = status_counts.get(st_key, 0) + 1

        return MultiSeriesEvaluationResult(
            strategy_id=strategy_id or strategy_payload.get("id"),
            requested_evaluation_timestamp=eval_dt,
            reference_dataset_id=reference_dataset_id,
            reference_timestamp_used=ref_time_dt,
            results=results,
            status_counts=status_counts,
            total_series_evaluated=len(results),
            warnings=[],
        )

    def _evaluate_reference_once(
        self,
        strategy_payload: Dict[str, Any],
        ref_candles: List[Candle],
        eval_dt: datetime,
    ) -> tuple[Optional[Any], Optional[datetime]]:
        # Preprocess completed candles
        from src.engine.indicators import preprocess_candle_series
        ref_series = preprocess_candle_series(ref_candles)
        if not ref_series:
            raise ValueError("Reference dataset contains no completed candles.")

        ref_idx, ref_time_str = self.rule_evaluator._find_candle_at_or_before(ref_series, eval_dt)
        if ref_idx is None or not ref_time_str:
            raise ValueError(f"No reference completed candle exists on or before timestamp {eval_dt.isoformat()}.")

        ref_time_dt = datetime.fromisoformat(ref_time_str).astimezone(timezone.utc)

        global_tree = strategy_payload.get("global_conditions")
        ref_result = None
        if global_tree:
            action = strategy_payload.get("action", {})
            risk_config = action.get("risk_config", {})
            validity_window = risk_config.get("validity_window", 5)

            ref_result = self.rule_evaluator._evaluate_node(
                node=global_tree,
                candles=ref_series[: ref_idx + 1],
                target_idx=ref_idx,
                validity_window=validity_window,
                path_prefix="global.0",
                depth=1,
            )

        return ref_result, ref_time_dt

    def _evaluate_single_subject(
        self,
        strategy_payload: Dict[str, Any],
        subject_dataset_id: str,
        ref_result: Optional[Any],
        eval_dt: datetime,
    ) -> SeriesEvaluationResult:
        entry = get_dataset_entry(subject_dataset_id)
        if not entry:
            return self._build_invalid_series_result(
                dataset_id=subject_dataset_id,
                instrument_id="UNKNOWN",
                timeframe="UNKNOWN",
                eval_dt=eval_dt,
                reason=f"Unknown subject dataset ID '{subject_dataset_id}'.",
                ref_result=ref_result,
            )

        strategy_timeframe = strategy_payload.get("timeframe", "15m")
        if entry.timeframe != strategy_timeframe:
            return self._build_invalid_series_result(
                dataset_id=subject_dataset_id,
                instrument_id=entry.instrument_id,
                timeframe=entry.timeframe,
                eval_dt=eval_dt,
                reason=f"Timeframe mismatch: Subject '{entry.timeframe}' vs Strategy '{strategy_timeframe}'.",
                ref_result=ref_result,
            )

        try:
            subj_candles = load_dataset_candles(subject_dataset_id)
        except Exception as e:
            return self._build_invalid_series_result(
                dataset_id=subject_dataset_id,
                instrument_id=entry.instrument_id,
                timeframe=entry.timeframe,
                eval_dt=eval_dt,
                reason=f"Failed loading subject dataset candles: {str(e)}",
                ref_result=ref_result,
            )

        from src.engine.indicators import preprocess_candle_series
        subj_series = preprocess_candle_series(subj_candles)
        if not subj_series:
            return self._build_invalid_series_result(
                dataset_id=subject_dataset_id,
                instrument_id=entry.instrument_id,
                timeframe=entry.timeframe,
                eval_dt=eval_dt,
                reason="Subject dataset contains no completed candles.",
                ref_result=ref_result,
            )

        subj_idx, subj_time_str = self.rule_evaluator._find_candle_at_or_before(subj_series, eval_dt)
        if subj_idx is None or not subj_time_str:
            return self._build_invalid_series_result(
                dataset_id=subject_dataset_id,
                instrument_id=entry.instrument_id,
                timeframe=entry.timeframe,
                eval_dt=eval_dt,
                reason=f"No completed subject candle at or before timestamp {eval_dt.isoformat()}.",
                ref_result=ref_result,
            )

        subj_time_dt = datetime.fromisoformat(subj_time_str).astimezone(timezone.utc)

        # Evaluate Candidate Conditions against Subject Series
        candidate_tree = strategy_payload.get("candidate_conditions")
        subj_result = None
        if candidate_tree:
            action = strategy_payload.get("action", {})
            risk_config = action.get("risk_config", {})
            validity_window = risk_config.get("validity_window", 5)

            try:
                subj_result = self.rule_evaluator._evaluate_node(
                    node=candidate_tree,
                    candles=subj_series[: subj_idx + 1],
                    target_idx=subj_idx,
                    validity_window=validity_window,
                    path_prefix="candidate.0",
                    depth=1,
                )
            except Exception as e:
                return self._build_invalid_series_result(
                    dataset_id=subject_dataset_id,
                    instrument_id=entry.instrument_id,
                    timeframe=entry.timeframe,
                    eval_dt=eval_dt,
                    reason=f"Candidate condition evaluation error: {str(e)}",
                    ref_result=ref_result,
                )

        # Combine Reference and Subject Results using AND logic
        overall = self.rule_evaluator._determine_overall_status(ref_result, subj_result)

        passed_ids: List[str] = []
        failed_ids: List[str] = []
        unavail_ids: List[str] = []
        invalid_ids: List[str] = []

        for r in [ref_result, subj_result]:
            if r:
                self.rule_evaluator._collect_results(r, passed_ids, failed_ids, unavail_ids, invalid_ids)

        summary = self._build_inspection_summary(overall, passed_ids, failed_ids, unavail_ids, invalid_ids, ref_result, subj_result)

        return SeriesEvaluationResult(
            dataset_id=subject_dataset_id,
            instrument_id=entry.instrument_id,
            timeframe=entry.timeframe,
            evaluation_timestamp=eval_dt,
            candle_timestamp_used=subj_time_dt,
            overall_status=overall,
            reference_result=ref_result,
            subject_result=subj_result,
            passed_condition_ids=passed_ids,
            failed_condition_ids=failed_ids,
            unavailable_condition_ids=unavail_ids,
            invalid_condition_ids=invalid_ids,
            inspection_summary=summary,
        )

    def _build_invalid_series_result(
        self,
        dataset_id: str,
        instrument_id: str,
        timeframe: str,
        eval_dt: datetime,
        reason: str,
        ref_result: Optional[Any],
    ) -> SeriesEvaluationResult:
        inv_cond = ConditionResult(
            condition_id=f"subject.{dataset_id}",
            status=EvaluationStatus.INVALID,
            operator="ERROR",
            reason=reason,
        )
        return SeriesEvaluationResult(
            dataset_id=dataset_id,
            instrument_id=instrument_id,
            timeframe=timeframe,
            evaluation_timestamp=eval_dt,
            candle_timestamp_used=None,
            overall_status=EvaluationStatus.INVALID,
            reference_result=ref_result,
            subject_result=inv_cond,
            passed_condition_ids=[],
            failed_condition_ids=[],
            unavailable_condition_ids=[],
            invalid_condition_ids=[f"subject.{dataset_id}"],
            inspection_summary=f"Series evaluated to INVALID: {reason}",
        )

    def _build_inspection_summary(
        self,
        overall: EvaluationStatus,
        passed: List[str],
        failed: List[str],
        unavail: List[str],
        invalid: List[str],
        ref_result: Optional[Any],
        subj_result: Optional[Any],
    ) -> str:
        if overall == EvaluationStatus.TRUE:
            return f"Series evaluated to TRUE ({len(passed)} condition{'s' if len(passed) != 1 else ''} passed)."
        elif overall == EvaluationStatus.FALSE:
            return f"Series evaluated to FALSE ({len(failed)} condition{'s' if len(failed) != 1 else ''} failed)."
        elif overall == EvaluationStatus.UNAVAILABLE:
            reasons = []
            for r in [ref_result, subj_result]:
                if r and r.status == EvaluationStatus.UNAVAILABLE and getattr(r, "reason", None):
                    reasons.append(r.reason)
            reason_str = f" - {reasons[0]}" if reasons else ""
            return f"Series evaluated to UNAVAILABLE ({len(unavail)} condition{'s' if len(unavail) != 1 else ''} unavailable{reason_str})."
        else:
            reasons = []
            for r in [ref_result, subj_result]:
                if r and r.status == EvaluationStatus.INVALID and getattr(r, "reason", None):
                    reasons.append(r.reason)
            reason_str = f" - {reasons[0]}" if reasons else ""
            return f"Series evaluated to INVALID ({len(invalid)} condition{'s' if len(invalid) != 1 else ''} invalid{reason_str})."
