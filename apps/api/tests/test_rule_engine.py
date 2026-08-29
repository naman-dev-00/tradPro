import pytest
from datetime import datetime, timezone, timedelta
from src.engine.models import Candle
from src.engine.evaluator import RuleEvaluator
from src.engine.rule_models import EvaluationStatus

def make_test_candles(
    count: int,
    start_price: float = 100.0,
    step: float = 1.0,
    timeframe: str = "15m",
    instrument_id: str = "NIFTY",
    start_time: datetime = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc),
) -> list[Candle]:
    candles = []
    for i in range(count):
        price = start_price + i * step
        candles.append(
            Candle(
                timestamp=start_time + timedelta(minutes=15 * i),
                instrument_id=instrument_id,
                timeframe=timeframe,
                open=price,
                high=price + 2.0,
                low=price - 1.0,
                close=price + 0.5,
                volume=1000.0 + i * 10,
                is_closed=True,
            )
        )
    return candles


# 1. All Nine Comparison Operators
def test_all_nine_comparison_operators():
    evaluator = RuleEvaluator()
    candles = make_test_candles(30) # price close is 100.5 + i

    ops = [
        ("GREATER_THAN", {"type": "NUMBER", "value": 100.0}, EvaluationStatus.TRUE),
        ("LESS_THAN", {"type": "NUMBER", "value": 200.0}, EvaluationStatus.TRUE),
        ("GREATER_THAN_OR_EQUAL", {"type": "NUMBER", "value": 129.5}, EvaluationStatus.TRUE),
        ("LESS_THAN_OR_EQUAL", {"type": "NUMBER", "value": 129.5}, EvaluationStatus.TRUE),
        ("EQUALS", {"type": "NUMBER", "value": 129.5}, EvaluationStatus.TRUE),
        ("BETWEEN", {"type": "NUMBER_RANGE", "range": [100.0, 150.0]}, EvaluationStatus.TRUE),
        ("CROSSES_ABOVE", {"type": "NUMBER", "value": 128.0}, EvaluationStatus.TRUE),
        ("CROSSES_BELOW", {"type": "NUMBER", "value": 135.0}, EvaluationStatus.FALSE),
        ("TOUCHES", {"type": "NUMBER", "value": 129.5}, EvaluationStatus.TRUE),
    ]

    for op_name, rhs_val, expected_st in ops:
        strat = {
            "id": f"strat-{op_name}",
            "timeframe": "15m",
            "global_conditions": {
                "id": f"cond-{op_name}",
                "type": "CONDITION",
                "lhs": {"indicator": "PRICE", "params": {"source": "close"}},
                "operator": op_name,
                "rhs": rhs_val,
            },
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}},
        }
        res = evaluator.evaluate_strategy_rules(strat, candles)
        assert res.reference_series_result.status == expected_st, f"Operator {op_name} failed expected status {expected_st}"


# 2. Equality and TOUCHES Tolerance Boundaries
def test_equality_and_touches_tolerance_boundaries():
    evaluator = RuleEvaluator()
    candles = make_test_candles(10) # candle 9 close is 109.5

    # Target 109.5 with tolerance 0.5 (exact boundary: abs(109.5 - 109.0) == 0.5) -> TRUE
    strat_bound_pass = {
        "timeframe": "15m",
        "global_conditions": {
            "id": "c1", "type": "CONDITION", "lhs": {"indicator": "PRICE"},
            "operator": "EQUALS", "tolerance": 0.5, "rhs": {"type": "NUMBER", "value": 109.0}
        },
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }
    assert evaluator.evaluate_strategy_rules(strat_bound_pass, candles).overall_status == EvaluationStatus.TRUE

    # Target 109.5 with tolerance 0.4 (abs(109.5 - 109.0) == 0.5 > 0.4) -> FALSE
    strat_bound_fail = {
        "timeframe": "15m",
        "global_conditions": {
            "id": "c2", "type": "CONDITION", "lhs": {"indicator": "PRICE"},
            "operator": "EQUALS", "tolerance": 0.4, "rhs": {"type": "NUMBER", "value": 109.0}
        },
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 1}}
    }
    assert evaluator.evaluate_strategy_rules(strat_bound_fail, candles).overall_status == EvaluationStatus.FALSE


# 3. Negative / Non-finite Tolerance Rejection
def test_negative_and_non_finite_tolerance_rejection():
    evaluator = RuleEvaluator()
    candles = make_test_candles(5)

    for bad_tol in [-0.5, float("nan"), float("inf")]:
        strat = {
            "timeframe": "15m",
            "global_conditions": {
                "id": "c_bad_tol", "type": "CONDITION", "lhs": {"indicator": "PRICE"},
                "operator": "EQUALS", "tolerance": bad_tol, "rhs": {"type": "NUMBER", "value": 100.0}
            },
            "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
        }
        res = evaluator.evaluate_strategy_rules(strat, candles)
        assert res.overall_status == EvaluationStatus.INVALID
        assert "Tolerance" in res.reference_series_result.reason or "Invalid tolerance" in res.reference_series_result.reason


# 4. Crossover Previous / Current Values
def test_crossover_previous_and_current_values():
    evaluator = RuleEvaluator()
    candles = make_test_candles(30)

    strat = {
        "timeframe": "15m",
        "global_conditions": {
            "id": "c_cross", "type": "CONDITION", "lhs": {"indicator": "PRICE"},
            "operator": "CROSSES_ABOVE", "rhs": {"type": "NUMBER", "value": 128.0}
        },
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }
    res = evaluator.evaluate_strategy_rules(strat, candles)
    assert res.overall_status == EvaluationStatus.TRUE
    assert res.reference_series_result.left_value is not None


# 5. Indicator-to-Indicator Crossover
def test_indicator_to_indicator_crossover():
    evaluator = RuleEvaluator()
    candles = make_test_candles(40)

    strat = {
        "timeframe": "15m",
        "global_conditions": {
            "id": "c_ind_cross", "type": "CONDITION",
            "lhs": {"indicator": "EMA", "params": {"period": 5}},
            "operator": "CROSSES_ABOVE",
            "rhs": {"type": "INDICATOR", "indicator": {"indicator": "EMA", "params": {"period": 20}}}
        },
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }
    res = evaluator.evaluate_strategy_rules(strat, candles)
    assert res.reference_series_result.status in [EvaluationStatus.TRUE, EvaluationStatus.FALSE]
    assert "lhs" in res.reference_series_result.indicator_values_used
    assert "rhs" in res.reference_series_result.indicator_values_used


# 6. Warm-up Behavior
def test_warmup_behavior():
    evaluator = RuleEvaluator()
    candles = make_test_candles(10) # 10 candles total

    # EMA period 20 needs 20 candles -> returns UNAVAILABLE
    strat = {
        "timeframe": "15m",
        "global_conditions": {
            "id": "c_warmup", "type": "CONDITION",
            "lhs": {"indicator": "EMA", "params": {"period": 20}},
            "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 100.0}
        },
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }
    res = evaluator.evaluate_strategy_rules(strat, candles)
    assert res.overall_status == EvaluationStatus.UNAVAILABLE
    assert res.reference_series_result.warmup_info["lhs_warmup"] > 0


# 7. AND / OR / NOT Truth Propagation
def test_and_or_not_truth_propagation():
    evaluator = RuleEvaluator()
    candles = make_test_candles(30)

    # AND: TRUE + FALSE -> FALSE
    strat_and = {
        "timeframe": "15m",
        "global_conditions": {
            "id": "g_and", "type": "AND",
            "conditions": [
                {"id": "c1", "type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 50.0}},
                {"id": "c2", "type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "LESS_THAN", "rhs": {"type": "NUMBER", "value": 50.0}},
            ]
        },
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }
    res_and = evaluator.evaluate_strategy_rules(strat_and, candles)
    assert res_and.reference_series_result.status == EvaluationStatus.FALSE

    # OR: TRUE + FALSE -> TRUE
    strat_or = {
        "timeframe": "15m",
        "global_conditions": {
            "id": "g_or", "type": "OR",
            "conditions": [
                {"id": "c1", "type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 50.0}},
                {"id": "c2", "type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "LESS_THAN", "rhs": {"type": "NUMBER", "value": 50.0}},
            ]
        },
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }
    res_or = evaluator.evaluate_strategy_rules(strat_or, candles)
    assert res_or.reference_series_result.status == EvaluationStatus.TRUE

    # NOT: TRUE -> FALSE
    strat_not = {
        "timeframe": "15m",
        "global_conditions": {
            "id": "g_not", "type": "NOT",
            "conditions": [
                {"id": "c1", "type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 50.0}}
            ]
        },
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }
    res_not = evaluator.evaluate_strategy_rules(strat_not, candles)
    assert res_not.reference_series_result.status == EvaluationStatus.FALSE


# 8. Empty and Invalid Groups
def test_empty_and_invalid_groups():
    evaluator = RuleEvaluator()
    candles = make_test_candles(10)

    # Empty group
    strat_empty = {
        "timeframe": "15m",
        "global_conditions": {"id": "g_empty", "type": "AND", "conditions": []},
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }
    res_empty = evaluator.evaluate_strategy_rules(strat_empty, candles)
    assert res_empty.overall_status == EvaluationStatus.INVALID

    # NOT group with 0 children
    strat_not0 = {
        "timeframe": "15m",
        "global_conditions": {"id": "g_not0", "type": "NOT", "conditions": []},
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }
    res_not0 = evaluator.evaluate_strategy_rules(strat_not0, candles)
    assert res_not0.overall_status == EvaluationStatus.INVALID


# 9. Maximum Depth (10)
def test_maximum_depth():
    evaluator = RuleEvaluator(max_depth=3)
    candles = make_test_candles(5)

    deep_tree = {
        "type": "AND", "conditions": [
            {"type": "AND", "conditions": [
                {"type": "AND", "conditions": [
                    {"type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 10.0}}
                ]}
            ]}
        ]
    }
    strat = {"timeframe": "15m", "global_conditions": deep_tree, "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}}
    with pytest.raises(ValueError, match="nesting depth exceeds maximum limit"):
        evaluator.evaluate_strategy_rules(strat, candles)


# 10. Maximum 200 Nodes
def test_maximum_200_nodes():
    evaluator = RuleEvaluator(max_total_nodes=5)
    candles = make_test_candles(5)

    big_tree = {
        "type": "AND",
        "conditions": [
            {"type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 1.0}}
            for _ in range(10)
        ]
    }
    strat = {"timeframe": "15m", "global_conditions": big_tree, "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}}
    with pytest.raises(ValueError, match="exceeds maximum allowed node limit"):
        evaluator.evaluate_strategy_rules(strat, candles)


# 11. Deterministic Legacy Condition IDs
def test_deterministic_legacy_condition_ids():
    evaluator = RuleEvaluator()
    candles = make_test_candles(5)

    legacy_strat = {
        "timeframe": "15m",
        "global_conditions": {
            "type": "AND",
            "conditions": [
                {"type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 10.0}}
            ]
        },
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }
    res = evaluator.evaluate_strategy_rules(legacy_strat, candles)
    assert res.reference_series_result.group_id == "global.0"
    assert res.reference_series_result.child_results[0].condition_id == "global.0.children.0"


# 12 & 13. Reference/Subject Alignment & Different Timeframes
def test_reference_subject_alignment_and_different_timeframes():
    evaluator = RuleEvaluator()
    ref_candles = make_test_candles(10, timeframe="15m")
    subj_candles = make_test_candles(10, timeframe="5m")

    strat = {
        "timeframe": "15m",
        "global_conditions": {"type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 10.0}},
        "candidate_conditions": {"type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 10.0}},
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }

    with pytest.raises(ValueError, match="does not match strategy timeframe"):
        evaluator.evaluate_strategy_rules(strat, ref_candles, subj_candles)


# 14. Missing Timestamps Rejection
def test_missing_timestamps_rejection():
    evaluator = RuleEvaluator()
    candles = make_test_candles(5)
    too_early_dt = datetime(2020, 1, 1, tzinfo=timezone.utc)

    strat = {
        "timeframe": "15m",
        "global_conditions": {"type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 10.0}},
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }

    with pytest.raises(ValueError, match="No reference completed candle exists"):
        evaluator.evaluate_strategy_rules(strat, candles, eval_timestamp=too_early_dt)


# 15. Incomplete Candles Exclusion
def test_incomplete_candles_exclusion():
    evaluator = RuleEvaluator()
    base_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    raw_candles = [
        Candle(timestamp=base_dt, instrument_id="NIFTY", timeframe="15m", open=100, high=105, low=99, close=102, volume=1000, is_closed=False)
    ]
    strat = {
        "timeframe": "15m",
        "global_conditions": {"type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 10.0}},
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }
    with pytest.raises(ValueError, match="contains no completed candles"):
        evaluator.evaluate_strategy_rules(strat, raw_candles)


# 16. Validity Window 0, Inside, Boundary and Expired
def test_validity_window_0_inside_boundary_and_expired():
    evaluator = RuleEvaluator()
    base_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    candles = []
    for i in range(20):
        p = 300.0 if i == 5 else 100.0
        candles.append(Candle(timestamp=base_dt + timedelta(minutes=15 * i), instrument_id="NIFTY", timeframe="15m", open=p, high=p, low=p, close=p, volume=1000, is_closed=True))

    strat = {
        "timeframe": "15m",
        "global_conditions": {"type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "EQUALS", "rhs": {"type": "NUMBER", "value": 300.0}},
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 2}}
    }

    # At index 5: age 0 -> TRUE
    assert evaluator.evaluate_strategy_rules(strat, candles, eval_timestamp=candles[5].timestamp).overall_status == EvaluationStatus.TRUE
    # At index 7: age 2 == validity_window 2 (Boundary) -> TRUE
    assert evaluator.evaluate_strategy_rules(strat, candles, eval_timestamp=candles[7].timestamp).overall_status == EvaluationStatus.TRUE
    # At index 8: age 3 > validity_window 2 (Expired) -> UNAVAILABLE
    res_exp = evaluator.evaluate_strategy_rules(strat, candles, eval_timestamp=candles[8].timestamp)
    assert res_exp.overall_status == EvaluationStatus.UNAVAILABLE
    assert "Condition expired" in res_exp.reference_series_result.reason


# 17. Future-Data Leakage Prevention
def test_future_data_leakage_prevention():
    evaluator = RuleEvaluator()
    candles = make_test_candles(30)
    eval_dt = candles[10].timestamp

    strat = {
        "timeframe": "15m",
        "global_conditions": {"type": "CONDITION", "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN", "rhs": {"type": "NUMBER", "value": 10.0}},
        "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 100, "stop_loss_pct": 1, "take_profit_pct": 2, "validity_window": 5}}
    }

    res = evaluator.evaluate_strategy_rules(strat, candles, eval_timestamp=eval_dt)
    assert res.reference_timestamp == eval_dt.isoformat()
    assert res.reference_series_result.timestamp == eval_dt.isoformat()
