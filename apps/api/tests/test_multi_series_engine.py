import pytest
from datetime import datetime, timezone, timedelta
from src.engine.multi_series_models import MultiSeriesEvaluationResult, SeriesEvaluationResult
from src.engine.multi_series_evaluator import MultiSeriesEvaluator
from src.engine.evaluator import RuleEvaluator
from src.engine.rule_models import EvaluationStatus

SAMPLE_STRATEGY = {
    "id": "strat-multi-test",
    "name": "Multi Series Test Strategy",
    "timeframe": "15m",
    "candidate_selection_mode": "FIRST_ELIGIBLE",
    "global_conditions": {
        "id": "global.cond.1",
        "type": "CONDITION",
        "lhs": {"indicator": "PRICE", "symbol": "NIFTY"},
        "operator": "GREATER_THAN",
        "rhs": {"type": "NUMBER", "value": 100.0},
    },
    "candidate_conditions": {
        "id": "candidate.cond.1",
        "type": "CONDITION",
        "lhs": {"indicator": "PRICE", "symbol": "CANDIDATE"},
        "operator": "GREATER_THAN",
        "rhs": {"type": "NUMBER", "value": 10.0},
    },
    "action": {
        "type": "PAPER_TRADE",
        "risk_config": {
            "max_position_size": 100000,
            "stop_loss_pct": 2.5,
            "take_profit_pct": 5,
            "validity_window": 5,
        },
    },
}

EVAL_DT = datetime(2026, 8, 28, 17, 45, tzinfo=timezone.utc)


def test_one_subject():
    evaluator = MultiSeriesEvaluator()
    res = evaluator.evaluate_multi_series(
        strategy_payload=SAMPLE_STRATEGY,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        eval_timestamp=EVAL_DT,
    )
    assert res.total_series_evaluated == 1
    assert len(res.results) == 1
    assert res.results[0].dataset_id == "synthetic_candidate_option_ce_23000_15m"
    assert res.results[0].overall_status in [EvaluationStatus.TRUE, EvaluationStatus.FALSE, EvaluationStatus.UNAVAILABLE, EvaluationStatus.INVALID]


def test_multiple_subjects_and_stable_order():
    evaluator = MultiSeriesEvaluator()
    subjects = [
        "synthetic_candidate_option_pe_23000_15m",
        "synthetic_candidate_option_ce_23000_15m",
        "synthetic_candidate_option_ce_23500_15m",
    ]
    res = evaluator.evaluate_multi_series(
        strategy_payload=SAMPLE_STRATEGY,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=subjects,
        eval_timestamp=EVAL_DT,
    )
    assert res.total_series_evaluated == 3
    # Exact input order preservation
    assert [r.dataset_id for r in res.results] == subjects


def test_status_counts_and_invariant_sum():
    evaluator = MultiSeriesEvaluator()
    subjects = [
        "synthetic_candidate_option_ce_23000_15m",
        "synthetic_candidate_option_pe_23000_15m",
        "synthetic_short_insufficient_5m",  # Will evaluate to per-series INVALID due to timeframe
    ]
    res = evaluator.evaluate_multi_series(
        strategy_payload=SAMPLE_STRATEGY,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=subjects,
        eval_timestamp=EVAL_DT,
    )
    counts = res.status_counts
    assert "TRUE" in counts
    assert "FALSE" in counts
    assert "UNAVAILABLE" in counts
    assert "INVALID" in counts
    total_sum = counts["TRUE"] + counts["FALSE"] + counts["UNAVAILABLE"] + counts["INVALID"]
    assert total_sum == res.total_series_evaluated == 3


def test_invalid_subject_isolation():
    evaluator = MultiSeriesEvaluator()
    subjects = [
        "synthetic_candidate_option_ce_23000_15m",
        "synthetic_short_insufficient_5m",  # Timeframe 5m vs 15m strategy -> INVALID
        "synthetic_candidate_option_pe_23000_15m",
    ]
    res = evaluator.evaluate_multi_series(
        strategy_payload=SAMPLE_STRATEGY,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=subjects,
        eval_timestamp=EVAL_DT,
    )
    assert len(res.results) == 3
    # Subject 1 (5m timeframe) is INVALID
    assert res.results[1].dataset_id == "synthetic_short_insufficient_5m"
    assert res.results[1].overall_status == EvaluationStatus.INVALID
    assert "Timeframe mismatch" in res.results[1].inspection_summary

    # Subject 0 and 2 were not blocked by subject 1's failure
    assert res.results[0].overall_status != EvaluationStatus.INVALID
    assert res.results[2].overall_status != EvaluationStatus.INVALID


def test_shared_reference_determinism():
    multi_eval = MultiSeriesEvaluator()
    single_eval = RuleEvaluator()

    # Evaluate via multi-series evaluator
    multi_res = multi_eval.evaluate_multi_series(
        strategy_payload=SAMPLE_STRATEGY,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
        eval_timestamp=EVAL_DT,
    )

    # Evaluate via single-series evaluator directly
    from src.engine.manifest import load_dataset_candles
    ref_candles = load_dataset_candles("synthetic_underlying_nifty_15m")
    subj_candles = load_dataset_candles("synthetic_candidate_option_ce_23000_15m")

    single_res = single_eval.evaluate_strategy_rules(
        strategy_payload=SAMPLE_STRATEGY,
        reference_candles=ref_candles,
        subject_candles=subj_candles,
        eval_timestamp=EVAL_DT,
    )

    # Prove overall status equivalence
    assert multi_res.results[0].overall_status == single_res.overall_status


def test_duplicate_subject_ids_rejection():
    evaluator = MultiSeriesEvaluator()
    with pytest.raises(ValueError, match="Duplicate subject_dataset_id"):
        evaluator.evaluate_multi_series(
            strategy_payload=SAMPLE_STRATEGY,
            reference_dataset_id="synthetic_underlying_nifty_15m",
            subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m", "synthetic_candidate_option_ce_23000_15m"],
            eval_timestamp=EVAL_DT,
        )


def test_empty_subjects_rejection():
    evaluator = MultiSeriesEvaluator()
    with pytest.raises(ValueError, match="specify at least 1 subject_dataset_id"):
        evaluator.evaluate_multi_series(
            strategy_payload=SAMPLE_STRATEGY,
            reference_dataset_id="synthetic_underlying_nifty_15m",
            subject_dataset_ids=[],
            eval_timestamp=EVAL_DT,
        )


def test_more_than_20_subjects_rejection():
    evaluator = MultiSeriesEvaluator()
    subjects = [f"subj-{i}" for i in range(21)]
    with pytest.raises(ValueError, match="maximum limit of 20 subject datasets"):
        evaluator.evaluate_multi_series(
            strategy_payload=SAMPLE_STRATEGY,
            reference_dataset_id="synthetic_underlying_nifty_15m",
            subject_dataset_ids=subjects,
            eval_timestamp=EVAL_DT,
        )


def test_unknown_dataset_ids_rejection():
    evaluator = MultiSeriesEvaluator()
    with pytest.raises(ValueError, match="Unknown reference dataset ID"):
        evaluator.evaluate_multi_series(
            strategy_payload=SAMPLE_STRATEGY,
            reference_dataset_id="non_existent_ref",
            subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
            eval_timestamp=EVAL_DT,
        )

    with pytest.raises(ValueError, match="Unknown subject dataset ID"):
        evaluator.evaluate_multi_series(
            strategy_payload=SAMPLE_STRATEGY,
            reference_dataset_id="synthetic_underlying_nifty_15m",
            subject_dataset_ids=["non_existent_subj"],
            eval_timestamp=EVAL_DT,
        )


def test_invalid_dataset_categories_rejection():
    evaluator = MultiSeriesEvaluator()

    # Pass subject dataset as reference
    with pytest.raises(ValueError, match="has category 'SUBJECT', expected 'REFERENCE'"):
        evaluator.evaluate_multi_series(
            strategy_payload=SAMPLE_STRATEGY,
            reference_dataset_id="synthetic_candidate_option_ce_23000_15m",
            subject_dataset_ids=["synthetic_candidate_option_pe_23000_15m"],
            eval_timestamp=EVAL_DT,
        )

    # Pass reference dataset as subject
    with pytest.raises(ValueError, match="has category 'REFERENCE', expected 'SUBJECT'"):
        evaluator.evaluate_multi_series(
            strategy_payload=SAMPLE_STRATEGY,
            reference_dataset_id="synthetic_underlying_nifty_15m",
            subject_dataset_ids=["synthetic_underlying_nifty_15m"],
            eval_timestamp=EVAL_DT,
        )


def test_naive_timestamp_rejection():
    evaluator = MultiSeriesEvaluator()
    naive_dt = datetime(2026, 8, 28, 17, 45)  # No tzinfo
    with pytest.raises(ValueError, match="Naive timestamps are not allowed"):
        evaluator.evaluate_multi_series(
            strategy_payload=SAMPLE_STRATEGY,
            reference_dataset_id="synthetic_underlying_nifty_15m",
            subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m"],
            eval_timestamp=naive_dt,
        )


def test_repeat_run_determinism():
    evaluator = MultiSeriesEvaluator()
    subjects = [
        "synthetic_candidate_option_ce_23000_15m",
        "synthetic_candidate_option_pe_23000_15m",
    ]
    res1 = evaluator.evaluate_multi_series(
        strategy_payload=SAMPLE_STRATEGY,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=subjects,
        eval_timestamp=EVAL_DT,
    )
    res2 = evaluator.evaluate_multi_series(
        strategy_payload=SAMPLE_STRATEGY,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=subjects,
        eval_timestamp=EVAL_DT,
    )
    assert res1.model_dump() == res2.model_dump()
