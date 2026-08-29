import pytest
from src.schemas import StrategyBase
from src.validation import validate_strategy_rules

def get_valid_strategy_dict():
    return {
        "id": "7b5ef35b-1175-430c-ab23-f22287955c45",
        "name": "Nifty RSI Touch Strategy",
        "description": "Cross above EMA and candidate RSI touch S1",
        "timeframe": "15m",
        "candidate_selection_mode": "FIRST_ELIGIBLE",
        "global_conditions": {
            "type": "CONDITION",
            "lhs": {
                "indicator": "PRICE",
                "symbol": "NIFTY"
            },
            "operator": "GREATER_THAN",
            "rhs": {
                "type": "INDICATOR",
                "indicator": {
                    "indicator": "EMA",
                    "symbol": "NIFTY",
                    "params": {"period": 200}
                }
            }
        },
        "candidate_conditions": {
            "type": "AND",
            "conditions": [
                {
                    "type": "CONDITION",
                    "lhs": {
                        "indicator": "RSI",
                        "symbol": "CANDIDATE",
                        "params": {"period": 14}
                    },
                    "operator": "LESS_THAN",
                    "rhs": {
                        "type": "NUMBER",
                        "value": 40.0
                    }
                },
                {
                    "type": "CONDITION",
                    "lhs": {
                        "indicator": "PRICE",
                        "symbol": "CANDIDATE"
                    },
                    "operator": "TOUCHES",
                    "rhs": {
                        "type": "INDICATOR",
                        "indicator": {
                            "indicator": "PIVOT",
                            "symbol": "CANDIDATE",
                            "params": {"level": "S1"}
                        }
                    }
                }
            ]
        },
        "action": {
            "type": "PAPER_TRADE",
            "risk_config": {
                "max_position_size": 100000.0,
                "stop_loss_pct": 2.5,
                "take_profit_pct": 5.0,
                "validity_window": 5
            }
        }
    }

def test_valid_strategy():
    data = get_valid_strategy_dict()
    strategy = StrategyBase(**data)
    errors = validate_strategy_rules(strategy)
    assert len(errors) == 0, f"Expected no validation errors, got: {errors}"

def test_action_without_conditions():
    data = get_valid_strategy_dict()
    data["global_conditions"] = None
    data["candidate_conditions"] = None
    strategy = StrategyBase(**data)
    errors = validate_strategy_rules(strategy)
    assert any("must contain at least one" in err for err in errors)

def test_empty_logical_group():
    data = get_valid_strategy_dict()
    data["candidate_conditions"] = {
        "type": "AND",
        "conditions": []
    }
    strategy = StrategyBase(**data)
    errors = validate_strategy_rules(strategy)
    assert any("Empty logical group" in err for err in errors)

def test_not_logical_group_validation():
    data = get_valid_strategy_dict()
    # NOT group with 2 conditions instead of 1
    data["candidate_conditions"] = {
        "type": "NOT",
        "conditions": [
            {
                "type": "CONDITION",
                "lhs": {"indicator": "RSI", "symbol": "CANDIDATE"},
                "operator": "LESS_THAN",
                "rhs": {"type": "NUMBER", "value": 30.0}
            },
            {
                "type": "CONDITION",
                "lhs": {"indicator": "PRICE", "symbol": "CANDIDATE"},
                "operator": "GREATER_THAN",
                "rhs": {"type": "NUMBER", "value": 100.0}
            }
        ]
    }
    strategy = StrategyBase(**data)
    errors = validate_strategy_rules(strategy)
    assert any("must contain exactly 1 condition" in err for err in errors)

def test_unrelated_instrument_in_candidate_conditions():
    data = get_valid_strategy_dict()
    # LHS indicator in candidate condition references NIFTY
    data["candidate_conditions"]["conditions"][0]["lhs"]["symbol"] = "NIFTY"
    strategy = StrategyBase(**data)
    errors = validate_strategy_rules(strategy)
    assert any("cannot reference an unrelated instrument" in err for err in errors)

def test_live_trading_action_rejected():
    data = get_valid_strategy_dict()
    data["action"]["type"] = "LIVE_TRADE"
    strategy = StrategyBase(**data)
    errors = validate_strategy_rules(strategy)
    assert any("Live trading actions" in err for err in errors)

def test_unknown_indicator():
    data = get_valid_strategy_dict()
    data["global_conditions"]["lhs"]["indicator"] = "UNKNOWN_INDICATOR"
    strategy = StrategyBase(**data)
    errors = validate_strategy_rules(strategy)
    assert any("Unknown indicator" in err for err in errors)

def test_unknown_operator():
    data = get_valid_strategy_dict()
    data["global_conditions"]["operator"] = "UNKNOWN_OPERATOR"
    strategy = StrategyBase(**data)
    errors = validate_strategy_rules(strategy)
    assert any("Unknown operator" in err for err in errors)
