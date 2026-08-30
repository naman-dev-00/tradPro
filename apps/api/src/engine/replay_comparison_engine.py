from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple
from src.engine.manifest import get_manifest_checksums_snapshot, MANIFEST_VERSION
from src.engine.fingerprint import compute_request_fingerprint, ENGINE_VERSION, REPLAY_SCHEMA_VERSION
from src.engine.multi_series_models import ensure_utc_datetime
from src.engine.replay_comparison_models import (
    DatasetChecksumResult,
    ReplayVerificationResult,
    ReplayStatusDifference,
    ReplayComparisonResult,
)

MAX_SUBJECTS_PER_RUN = 20
MAX_TIMESTAMPS_PER_RUN = 5000
MAX_ALIGNED_POINTS = 100000
MAX_RESULT_BYTES = 10 * 1024 * 1024  # 10 MB

VALID_NEUTRAL_STATUSES = ["TRUE", "FALSE", "UNAVAILABLE", "INVALID"]
ALL_TRANSITION_STATUSES = ["TRUE", "FALSE", "UNAVAILABLE", "INVALID", "ABSENT"]


class ReplayComparisonEngine:

    @staticmethod
    def verify_run(run: Any) -> ReplayVerificationResult:
        if run is None:
            return ReplayVerificationResult(
                run_id="UNKNOWN",
                verification_status="INVALID",
                reasons=["Inspection run record not found."]
            )

        run_id = str(getattr(run, "id", "UNKNOWN"))
        reasons: List[str] = []

        # 1. Check INVALID conditions
        status_val = getattr(run, "status", None)
        if status_val != "COMPLETED":
            reasons.append(f"Inspection run is not COMPLETED (current status: '{status_val}').")
            return ReplayVerificationResult(
                run_id=run_id,
                verification_status="INVALID",
                reasons=reasons
            )

        req_start = getattr(run, "requested_start_timestamp", None)
        req_end = getattr(run, "requested_end_timestamp", None)
        if req_start and req_end:
            try:
                dt_start = ensure_utc_datetime(req_start)
                dt_end = ensure_utc_datetime(req_end)
                if dt_start > dt_end:
                    reasons.append("Requested start timestamp is after end timestamp.")
                    return ReplayVerificationResult(
                        run_id=run_id,
                        verification_status="INVALID",
                        reasons=reasons
                    )
            except Exception as dt_err:
                reasons.append(f"Invalid timestamp metadata: {dt_err}")
                return ReplayVerificationResult(
                    run_id=run_id,
                    verification_status="INVALID",
                    reasons=reasons
                )

        result_payload = getattr(run, "result_payload", None)
        if result_payload is not None and not isinstance(result_payload, dict):
            reasons.append("Stored result_payload is not a valid JSON object.")
            return ReplayVerificationResult(
                run_id=run_id,
                verification_status="INVALID",
                reasons=reasons
            )

        # 2. Check UNVERIFIABLE conditions
        strat_snapshot = getattr(run, "strategy_definition_snapshot", None)
        stored_fingerprint = getattr(run, "request_fingerprint", None) or getattr(run, "completed_fingerprint", None)
        manifest_checksums_snapshot = getattr(run, "manifest_checksums_snapshot", None)

        strategy_snapshot_present = strat_snapshot is not None and isinstance(strat_snapshot, dict)
        result_payload_present = result_payload is not None and isinstance(result_payload, dict)

        stored_engine_ver = getattr(run, "engine_version", None)
        stored_manifest_ver = getattr(run, "manifest_version", None)

        stored_schema_ver = None
        if result_payload_present:
            stored_schema_ver = result_payload.get("replay_schema_version", "1.0.0")

        if not strategy_snapshot_present:
            reasons.append("Stored strategy_definition_snapshot is missing.")
        if not result_payload_present:
            reasons.append("Stored result_payload is missing.")
        if not manifest_checksums_snapshot:
            reasons.append("Stored manifest_checksums_snapshot is missing.")
        if not stored_fingerprint:
            reasons.append("Stored request_fingerprint is missing.")
        if not stored_engine_ver:
            reasons.append("Stored engine_version is missing.")
        if not stored_manifest_ver:
            reasons.append("Stored manifest_version is missing.")

        if (
            not strategy_snapshot_present
            or not result_payload_present
            or not manifest_checksums_snapshot
            or not stored_fingerprint
            or not stored_engine_ver
            or not stored_manifest_ver
        ):
            return ReplayVerificationResult(
                run_id=run_id,
                verification_status="UNVERIFIABLE",
                stored_request_fingerprint=stored_fingerprint,
                stored_engine_version=stored_engine_ver,
                current_engine_version=ENGINE_VERSION,
                stored_manifest_version=stored_manifest_ver,
                current_manifest_version=MANIFEST_VERSION,
                stored_replay_schema_version=stored_schema_ver,
                current_replay_schema_version=REPLAY_SCHEMA_VERSION,
                strategy_snapshot_present=strategy_snapshot_present,
                result_payload_present=result_payload_present,
                reasons=reasons,
            )

        # 3. Compute fingerprint and check matches (MISMATCH vs VERIFIED)
        ref_ds_id = getattr(run, "reference_dataset_id", "")
        subj_ds_ids = getattr(run, "subject_dataset_ids", [])
        sampling_step = result_payload.get("sampling_step", 1) if result_payload_present else 1

        try:
            recomputed_fingerprint = compute_request_fingerprint(
                strategy_payload=strat_snapshot,
                reference_dataset_id=ref_ds_id,
                subject_dataset_ids=subj_ds_ids,
                start_timestamp=req_start,
                end_timestamp=req_end,
                sampling_step=sampling_step,
            )
        except Exception as fp_err:
            reasons.append(f"Failed to recompute request fingerprint: {fp_err}")
            return ReplayVerificationResult(
                run_id=run_id,
                verification_status="UNVERIFIABLE",
                stored_request_fingerprint=stored_fingerprint,
                strategy_snapshot_present=True,
                result_payload_present=True,
                reasons=reasons,
            )

        fingerprint_matches = stored_fingerprint == recomputed_fingerprint
        engine_version_matches = stored_engine_ver == ENGINE_VERSION
        manifest_version_matches = stored_manifest_ver == MANIFEST_VERSION
        replay_schema_version_matches = stored_schema_ver == REPLAY_SCHEMA_VERSION

        if not fingerprint_matches:
            reasons.append(f"Request fingerprint mismatch (stored: '{stored_fingerprint[:8]}...', recomputed: '{recomputed_fingerprint[:8]}...').")
        if not engine_version_matches:
            reasons.append(f"Engine version mismatch (stored: '{stored_engine_ver}', current: '{ENGINE_VERSION}').")
        if not manifest_version_matches:
            reasons.append(f"Manifest version mismatch (stored: '{stored_manifest_ver}', current: '{MANIFEST_VERSION}').")
        if not replay_schema_version_matches:
            reasons.append(f"Replay schema version mismatch (stored: '{stored_schema_ver}', current: '{REPLAY_SCHEMA_VERSION}').")

        # Check dataset checksums deterministically sorted by dataset_id
        current_checksums = get_manifest_checksums_snapshot()
        dataset_checksum_results: List[DatasetChecksumResult] = []

        all_ds_ids = sorted(set(manifest_checksums_snapshot.keys()))
        for ds_id in all_ds_ids:
            stored_hash = manifest_checksums_snapshot.get(ds_id)
            curr_hash = current_checksums.get(ds_id)
            matches = stored_hash == curr_hash
            dataset_checksum_results.append(
                DatasetChecksumResult(
                    dataset_id=ds_id,
                    stored_checksum=stored_hash,
                    current_checksum=curr_hash,
                    matches=matches,
                )
            )
            if not matches:
                reasons.append(f"Dataset checksum mismatch for '{ds_id}' (stored: '{stored_hash}', current: '{curr_hash or 'MISSING'}').")

        all_matches = (
            fingerprint_matches
            and engine_version_matches
            and manifest_version_matches
            and replay_schema_version_matches
            and all(r.matches for r in dataset_checksum_results)
        )

        verification_status = "VERIFIED" if all_matches else "MISMATCH"
        if verification_status == "VERIFIED":
            reasons = ["Run reproducibility fully verified."]

        return ReplayVerificationResult(
            run_id=run_id,
            verification_status=verification_status,
            stored_request_fingerprint=stored_fingerprint,
            recomputed_request_fingerprint=recomputed_fingerprint,
            fingerprint_matches=fingerprint_matches,
            stored_manifest_version=stored_manifest_ver,
            current_manifest_version=MANIFEST_VERSION,
            manifest_version_matches=manifest_version_matches,
            stored_engine_version=stored_engine_ver,
            current_engine_version=ENGINE_VERSION,
            engine_version_matches=engine_version_matches,
            stored_replay_schema_version=stored_schema_ver,
            current_replay_schema_version=REPLAY_SCHEMA_VERSION,
            replay_schema_version_matches=replay_schema_version_matches,
            dataset_checksum_results=dataset_checksum_results,
            strategy_snapshot_present=True,
            result_payload_present=True,
            reasons=reasons,
        )

    @staticmethod
    def compare_runs(
        baseline_run: Any,
        comparison_run: Any,
        include_unchanged: bool = False,
    ) -> ReplayComparisonResult:
        if baseline_run is None or comparison_run is None:
            raise ValueError("Baseline and comparison inspection runs must both be provided.")

        base_id = str(getattr(baseline_run, "id", "BASELINE"))
        comp_id = str(getattr(comparison_run, "id", "COMPARISON"))

        if base_id == comp_id:
            raise ValueError("Cannot compare a historical replay run with itself.")

        base_status = getattr(baseline_run, "status", None)
        comp_status = getattr(comparison_run, "status", None)

        if base_status != "COMPLETED":
            raise ValueError(f"Baseline run '{base_id}' is not COMPLETED (status: '{base_status}'). Only COMPLETED runs can be compared.")
        if comp_status != "COMPLETED":
            raise ValueError(f"Comparison run '{comp_id}' is not COMPLETED (status: '{comp_status}'). Only COMPLETED runs can be compared.")

        base_payload = getattr(baseline_run, "result_payload", None)
        comp_payload = getattr(comparison_run, "result_payload", None)

        if not isinstance(base_payload, dict) or "replay_points" not in base_payload:
            raise ValueError(f"Baseline run '{base_id}' contains malformed or missing replay_points payload.")
        if not isinstance(comp_payload, dict) or "replay_points" not in comp_payload:
            raise ValueError(f"Comparison run '{comp_id}' contains malformed or missing replay_points payload.")

        base_subjects = getattr(baseline_run, "subject_dataset_ids", []) or []
        comp_subjects = getattr(comparison_run, "subject_dataset_ids", []) or []

        if len(base_subjects) > MAX_SUBJECTS_PER_RUN or len(comp_subjects) > MAX_SUBJECTS_PER_RUN:
            raise ValueError(f"Replay comparison complexity limit exceeded: maximum {MAX_SUBJECTS_PER_RUN} subjects allowed per run.")

        base_replay_points = base_payload.get("replay_points", [])
        comp_replay_points = comp_payload.get("replay_points", [])

        if len(base_replay_points) > MAX_TIMESTAMPS_PER_RUN or len(comp_replay_points) > MAX_TIMESTAMPS_PER_RUN:
            raise ValueError(f"Replay comparison complexity limit exceeded: maximum {MAX_TIMESTAMPS_PER_RUN} replay timestamps allowed per run.")

        # Build baseline and comparison point maps: (iso_timestamp, dataset_id) -> SeriesEvaluationResult dict
        base_points_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for pt in base_replay_points:
            eval_ts = pt.get("evaluation_timestamp")
            if not eval_ts:
                continue
            iso_ts = ensure_utc_datetime(eval_ts).isoformat()
            for s_res in pt.get("results", []):
                ds_id = s_res.get("dataset_id")
                if ds_id:
                    base_points_map[(iso_ts, ds_id)] = s_res

        comp_points_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for pt in comp_replay_points:
            eval_ts = pt.get("evaluation_timestamp")
            if not eval_ts:
                continue
            iso_ts = ensure_utc_datetime(eval_ts).isoformat()
            for s_res in pt.get("results", []):
                ds_id = s_res.get("dataset_id")
                if ds_id:
                    comp_points_map[(iso_ts, ds_id)] = s_res

        all_keys = set(base_points_map.keys()) | set(comp_points_map.keys())
        if len(all_keys) > MAX_ALIGNED_POINTS:
            raise ValueError(f"Replay comparison complexity limit exceeded: maximum {MAX_ALIGNED_POINTS} aligned points allowed.")

        # Determine dataset ordering per timestamp:
        # Reference dataset first, then subjects in baseline array order, then subjects in comparison array order, then fallback sorted
        base_ref = getattr(baseline_run, "reference_dataset_id", None)
        comp_ref = getattr(comparison_run, "reference_dataset_id", None)

        preferred_ds_order: List[str] = []
        seen_ds = set()

        for ref_id in [base_ref, comp_ref]:
            if ref_id and ref_id not in seen_ds:
                preferred_ds_order.append(ref_id)
                seen_ds.add(ref_id)

        for s_id in base_subjects:
            if s_id and s_id not in seen_ds:
                preferred_ds_order.append(s_id)
                seen_ds.add(s_id)

        for s_id in comp_subjects:
            if s_id and s_id not in seen_ds:
                preferred_ds_order.append(s_id)
                seen_ds.add(s_id)

        all_ds_ids = set(k[1] for k in all_keys)
        remaining_ds = sorted(all_ds_ids - seen_ds)
        ordered_dataset_list = preferred_ds_order + remaining_ds
        ds_order_index = {ds_id: idx for idx, ds_id in enumerate(ordered_dataset_list)}

        # Sort keys by: (1) timestamp ascending, (2) dataset position in ds_order_index
        sorted_keys = sorted(
            all_keys,
            key=lambda k: (k[0], ds_order_index.get(k[1], 999999), k[1])
        )

        # Initialize status transition counts matrix (5x5 = 25 keys)
        status_transition_counts: Dict[str, int] = {}
        for b_st in ALL_TRANSITION_STATUSES:
            for c_st in ALL_TRANSITION_STATUSES:
                status_transition_counts[f"{b_st} -> {c_st}"] = 0

        baseline_only_count = 0
        comparison_only_count = 0
        unchanged_count = 0
        changed_count = 0
        differences: List[ReplayStatusDifference] = []

        for ts, ds_id in sorted_keys:
            b_res = base_points_map.get((ts, ds_id))
            c_res = comp_points_map.get((ts, ds_id))

            b_present = b_res is not None
            c_present = c_res is not None

            b_status = b_res.get("overall_status") if b_present else None
            c_status = c_res.get("overall_status") if c_present else None

            # Matrix key uses ABSENT for missing point
            b_matrix_st = b_status if b_present else "ABSENT"
            c_matrix_st = c_status if c_present else "ABSENT"
            status_transition_counts[f"{b_matrix_st} -> {c_matrix_st}"] += 1

            if b_present and not c_present:
                baseline_only_count += 1
            elif c_present and not b_present:
                comparison_only_count += 1

            # Condition IDs extraction
            b_cond_ids = {
                "TRUE": b_res.get("passed_condition_ids", []) if b_present else [],
                "FALSE": b_res.get("failed_condition_ids", []) if b_present else [],
                "UNAVAILABLE": b_res.get("unavailable_condition_ids", []) if b_present else [],
                "INVALID": b_res.get("invalid_condition_ids", []) if b_present else [],
            }
            c_cond_ids = {
                "TRUE": c_res.get("passed_condition_ids", []) if c_present else [],
                "FALSE": c_res.get("failed_condition_ids", []) if c_present else [],
                "UNAVAILABLE": c_res.get("unavailable_condition_ids", []) if c_present else [],
                "INVALID": c_res.get("invalid_condition_ids", []) if c_present else [],
            }

            newly_true = sorted(set(c_cond_ids["TRUE"]) - set(b_cond_ids["TRUE"]))
            no_longer_true = sorted(set(b_cond_ids["TRUE"]) - set(c_cond_ids["TRUE"]))
            newly_false = sorted(set(c_cond_ids["FALSE"]) - set(b_cond_ids["FALSE"]))
            no_longer_false = sorted(set(b_cond_ids["FALSE"]) - set(c_cond_ids["FALSE"]))
            newly_unavailable = sorted(set(c_cond_ids["UNAVAILABLE"]) - set(b_cond_ids["UNAVAILABLE"]))
            newly_invalid = sorted(set(c_cond_ids["INVALID"]) - set(b_cond_ids["INVALID"]))

            conditions_changed = bool(
                newly_true or no_longer_true or newly_false or no_longer_false or newly_unavailable or newly_invalid
            )

            is_changed = (
                b_present != c_present
                or b_status != c_status
                or conditions_changed
            )

            if is_changed:
                changed_count += 1
            else:
                unchanged_count += 1

            # Construct explanation
            if not b_present and c_present:
                explanation = f"Point present in comparison run only (status: {c_status})."
            elif b_present and not c_present:
                explanation = f"Point present in baseline run only (status: {b_status})."
            elif b_status != c_status:
                explanation = f"Overall evaluation status changed from '{b_status}' to '{c_status}'."
            elif conditions_changed:
                explanation = f"Overall status unchanged ('{b_status}'), but condition evaluations changed."
            else:
                explanation = f"Identical evaluation status ('{b_status}') and condition results."

            diff_obj = ReplayStatusDifference(
                timestamp=ts,
                dataset_id=ds_id,
                baseline_present=b_present,
                comparison_present=c_present,
                baseline_status=b_status,
                comparison_status=c_status,
                changed=is_changed,
                baseline_condition_ids=b_cond_ids,
                comparison_condition_ids=c_cond_ids,
                newly_true_condition_ids=newly_true,
                no_longer_true_condition_ids=no_longer_true,
                newly_false_condition_ids=newly_false,
                no_longer_false_condition_ids=no_longer_false,
                newly_unavailable_condition_ids=newly_unavailable,
                newly_invalid_condition_ids=newly_invalid,
                explanation=explanation,
            )

            if include_unchanged or is_changed:
                differences.append(diff_obj)

        baseline_meta = {
            "id": base_id,
            "strategy_id": getattr(baseline_run, "strategy_id", None),
            "reference_dataset_id": base_ref,
            "subject_dataset_ids": base_subjects,
            "requested_start_timestamp": getattr(baseline_run, "requested_start_timestamp", None).isoformat() if getattr(baseline_run, "requested_start_timestamp", None) else None,
            "requested_end_timestamp": getattr(baseline_run, "requested_end_timestamp", None).isoformat() if getattr(baseline_run, "requested_end_timestamp", None) else None,
            "engine_version": getattr(baseline_run, "engine_version", "1.0.0"),
            "manifest_version": getattr(baseline_run, "manifest_version", "1.0.0"),
            "created_at": getattr(baseline_run, "created_at", None).isoformat() if getattr(baseline_run, "created_at", None) else None,
        }

        comparison_meta = {
            "id": comp_id,
            "strategy_id": getattr(comparison_run, "strategy_id", None),
            "reference_dataset_id": comp_ref,
            "subject_dataset_ids": comp_subjects,
            "requested_start_timestamp": getattr(comparison_run, "requested_start_timestamp", None).isoformat() if getattr(comparison_run, "requested_start_timestamp", None) else None,
            "requested_end_timestamp": getattr(comparison_run, "requested_end_timestamp", None).isoformat() if getattr(comparison_run, "requested_end_timestamp", None) else None,
            "engine_version": getattr(comparison_run, "engine_version", "1.0.0"),
            "manifest_version": getattr(comparison_run, "manifest_version", "1.0.0"),
            "created_at": getattr(comparison_run, "created_at", None).isoformat() if getattr(comparison_run, "created_at", None) else None,
        }

        warnings: List[str] = []
        if baseline_only_count > 0:
            warnings.append(f"{baseline_only_count} point(s) exist only in the baseline run.")
        if comparison_only_count > 0:
            warnings.append(f"{comparison_only_count} point(s) exist only in the comparison run.")

        return ReplayComparisonResult(
            baseline_metadata=baseline_meta,
            comparison_metadata=comparison_meta,
            aligned_point_count=len(all_keys),
            baseline_only_point_count=baseline_only_count,
            comparison_only_point_count=comparison_only_count,
            unchanged_point_count=unchanged_count,
            changed_point_count=changed_count,
            status_transition_counts=status_transition_counts,
            differences=differences,
            warnings=warnings,
        )
