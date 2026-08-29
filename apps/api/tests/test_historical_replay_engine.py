import pytest
from datetime import datetime, timezone
from src.engine.historical_replay_evaluator import HistoricalReplayEvaluator
from src.engine.rule_models import EvaluationStatus
from src.engine.multi_series_evaluator import MultiSeriesEvaluator
from src.engine.models import Candle

def make_sample_strategy():
    return {
        "name": "Replay Test Strategy",
        "timeframe": "15m",
        "candidate_selection_mode": "FIRST_ELIGIBLE",
        "global_conditions": {
            "id": "c1", "type": "CONDITION",
            "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 100.0}
        },
        "candidate_conditions": {
            "id": "c2", "type": "CONDITION",
            "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 10.0}
        },
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }

def test_historical_replay_valid_execution():
    strat = make_sample_strategy()
    start_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    end_dt = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

    res = HistoricalReplayEvaluator.evaluate_replay(
        strategy_payload=strat,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=[
            "synthetic_candidate_option_ce_23000_15m",
            "synthetic_candidate_option_pe_23000_15m",
        ],
        start_timestamp=start_dt,
        end_timestamp=end_dt,
        sampling_step=1,
    )

    assert res.start_timestamp == start_dt
    assert res.end_timestamp == end_dt
    assert res.sampled_timestamp_count > 0
    assert len(res.replay_points) == res.sampled_timestamp_count
    assert len(res.subject_timelines) == 2
    assert res.subject_timelines[0].dataset_id == "synthetic_candidate_option_ce_23000_15m"
    assert res.subject_timelines[1].dataset_id == "synthetic_candidate_option_pe_23000_15m"
    assert "TRUE_TO_FALSE" in res.subject_timelines[0].transition_counts or res.subject_timelines[0].transition_counts == {}

def test_precomputed_replay_equivalence_to_single_evaluations():
    # Verify exact equivalence between precomputed historical replay and single timestamp MultiSeriesEvaluator calls
    strat = make_sample_strategy()
    start_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    end_dt = datetime(2026, 8, 28, 11, 0, tzinfo=timezone.utc)

    replay_res = HistoricalReplayEvaluator.evaluate_replay(
        strategy_payload=strat,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        start_timestamp=start_dt,
        end_timestamp=end_dt,
        sampling_step=1,
    )

    for pt in replay_res.replay_points:
        eval_dt = pt.evaluation_timestamp
        multi_eval = MultiSeriesEvaluator()
        single_res = multi_eval.evaluate_multi_series(
            strategy_payload=strat,
            reference_dataset_id="synthetic_underlying_nifty_15m",
            subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
            eval_timestamp=eval_dt,
        )

        assert len(pt.results) == len(single_res.results)
        assert pt.results[0].overall_status == single_res.results[0].overall_status
        assert pt.status_counts == single_res.status_counts

def test_historical_replay_future_leakage_prevention():
    strat = make_sample_strategy()
    start_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    end_dt = datetime(2026, 8, 28, 9, 30, tzinfo=timezone.utc)

    res = HistoricalReplayEvaluator.evaluate_replay(
        strategy_payload=strat,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        start_timestamp=start_dt,
        end_timestamp=end_dt,
        sampling_step=1,
    )

    for pt in res.replay_points:
        # Check evaluation timestamp does not exceed pt.evaluation_timestamp
        assert pt.evaluation_timestamp <= end_dt
        if pt.reference_timestamp_used:
            assert pt.reference_timestamp_used <= pt.evaluation_timestamp

def test_historical_replay_over_limit_rejections():
    strat = make_sample_strategy()
    start_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    end_dt = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

    # 1. Start > End
    with pytest.raises(ValueError, match="start_timestamp .* must be less than or equal to end_timestamp"):
        HistoricalReplayEvaluator.evaluate_replay(
            strategy_payload=strat,
            reference_dataset_id="synthetic_underlying_nifty_15m",
            subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
            start_timestamp=end_dt,
            end_timestamp=start_dt,
        )

    # 2. Max subjects (> 20)
    over_20_subjects = [f"subj-{i}" for i in range(21)]
    with pytest.raises(ValueError, match="Maximum 20 subject datasets allowed"):
        HistoricalReplayEvaluator.evaluate_replay(
            strategy_payload=strat,
            reference_dataset_id="synthetic_underlying_nifty_15m",
            subject_dataset_ids=over_20_subjects,
            start_timestamp=start_dt,
            end_timestamp=end_dt,
        )

def test_manifest_fixed_metadata_and_version_constants():
    from src.engine.manifest import MANIFEST_VERSION, CANDLE_SCHEMA_VERSION, get_dataset_manifest
    assert MANIFEST_VERSION == "1.0.0"
    assert CANDLE_SCHEMA_VERSION == "1.0.0"
    m = get_dataset_manifest()
    for entry in m:
        assert entry.manifest_version == "1.0.0"
        assert entry.generated_at == "2026-08-28T00:00:00Z"
        assert len(entry.dataset_checksum) == 64

def test_checksum_stability_and_mismatch_verification():
    from src.engine.manifest import compare_manifest_checksums, get_manifest_checksums_snapshot
    snapshot = get_manifest_checksums_snapshot()
    res = compare_manifest_checksums(snapshot)
    assert res["is_exact_match"] is True
    assert res["mismatches"] == {}

    # Simulate tampered snapshot
    tampered = dict(snapshot)
    tampered["synthetic_underlying_nifty_15m"] = "tampered_hash_12345"
    res_tampered = compare_manifest_checksums(tampered)
    assert res_tampered["is_exact_match"] is False
    assert "synthetic_underlying_nifty_15m" in res_tampered["mismatches"]

def test_replay_result_payload_compactness():
    strat = make_sample_strategy()
    start_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    end_dt = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

    res = HistoricalReplayEvaluator.evaluate_replay(
        strategy_payload=strat,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        start_timestamp=start_dt,
        end_timestamp=end_dt,
        sampling_step=1,
    )
    payload_dict = res.model_dump(mode="json")
    # Verify shared reference metadata stored once at top level
    assert "reference_metadata" in payload_dict
    assert "replay_points" in payload_dict
    # Replay points store aggregate status counts and results, not duplicated full reference trees
    for pt in payload_dict["replay_points"]:
        assert "status_counts" in pt
        assert "results" in pt

def test_historical_replay_transition_counts_and_consecutive_runs():
    strat = make_sample_strategy()
    start_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    end_dt = datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc)

    res = HistoricalReplayEvaluator.evaluate_replay(
        strategy_payload=strat,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        start_timestamp=start_dt,
        end_timestamp=end_dt,
        sampling_step=1,
    )
    timeline = res.subject_timelines[0]
    assert hasattr(timeline, "transition_counts")
    assert hasattr(timeline, "consecutive_status_runs")
    assert "TRUE" in timeline.consecutive_status_runs or "FALSE" in timeline.consecutive_status_runs

def test_historical_replay_boundary_conditions():
    strat = make_sample_strategy()
    # Boundary: exact single candle timestamp
    single_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    res = HistoricalReplayEvaluator.evaluate_replay(
        strategy_payload=strat,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        start_timestamp=single_dt,
        end_timestamp=single_dt,
        sampling_step=1,
    )
    assert res.sampled_timestamp_count == 1
    assert len(res.replay_points) == 1
