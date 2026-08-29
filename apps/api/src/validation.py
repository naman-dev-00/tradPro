from typing import List, Optional
from src.schemas import StrategyBase, ConditionNode, IndicatorExpression, ComparisonValue

VALID_INDICATORS = {"PRICE", "EMA", "RSI", "MACD", "PIVOT", "VOLUME"}
VALID_OPERATORS = {
    "GREATER_THAN",
    "LESS_THAN",
    "GREATER_THAN_OR_EQUAL",
    "LESS_THAN_OR_EQUAL",
    "EQUALS",
    "CROSSES_ABOVE",
    "CROSSES_BELOW",
    "TOUCHES",
    "BETWEEN",
}
VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w"}

def validate_indicator(expr: Optional[IndicatorExpression], path: str, is_candidate: bool) -> List[str]:
    errors = []
    if not expr:
        return [f"{path}: Indicator expression is missing."]

    if expr.indicator not in VALID_INDICATORS:
        errors.append(f"{path}: Unknown indicator '{expr.indicator}'. Must be one of {sorted(VALID_INDICATORS)}")

    # Candidate condition instrument restriction
    if is_candidate and expr.symbol:
        sym = expr.symbol.strip().upper()
        if sym not in {"CANDIDATE", ""}:
            errors.append(f"{path}: Candidate conditions cannot reference an unrelated instrument '{expr.symbol}'. Only 'CANDIDATE' or empty symbol is allowed.")

    # Validate parameters
    if expr.params:
        p = expr.params
        if expr.indicator == "EMA" and not p.period:
            errors.append(f"{path}: EMA indicator requires a period parameter.")
        if expr.indicator == "RSI" and not p.period:
            errors.append(f"{path}: RSI indicator requires a period parameter.")
        if expr.indicator == "PIVOT" and not p.level:
            errors.append(f"{path}: PIVOT indicator requires a level parameter (e.g. S1, R1).")

    return errors

def validate_comparison_value(val: Optional[ComparisonValue], path: str, is_candidate: bool) -> List[str]:
    errors = []
    if not val:
        return [f"{path}: Comparison value is missing."]

    if val.type not in {"NUMBER", "INDICATOR", "NUMBER_RANGE"}:
        errors.append(f"{path}: Unknown comparison value type '{val.type}'.")

    if val.type == "NUMBER" and val.value is None:
        errors.append(f"{path}: Comparison type is NUMBER but value is missing.")

    if val.type == "NUMBER_RANGE":
        if not val.range or len(val.range) != 2:
            errors.append(f"{path}: Comparison type is NUMBER_RANGE but range must contain exactly 2 numbers.")

    if val.type == "INDICATOR":
        if not val.indicator:
            errors.append(f"{path}: Comparison type is INDICATOR but indicator details are missing.")
        else:
            errors.extend(validate_indicator(val.indicator, f"{path}.indicator", is_candidate))

    return errors

def validate_condition_node(node: Optional[ConditionNode], path: str, is_candidate: bool) -> List[str]:
    errors = []
    if not node:
        return errors

    node_type = node.type.strip().upper()
    if node_type not in {"AND", "OR", "NOT", "CONDITION"}:
        errors.append(f"{path}: Unknown condition node type '{node.type}'.")
        return errors

    if node_type in {"AND", "OR", "NOT"}:
        if node.conditions is None or len(node.conditions) == 0:
            errors.append(f"{path}: Empty logical group '{node_type}' is rejected.")
        else:
            if node_type == "NOT" and len(node.conditions) != 1:
                errors.append(f"{path}: Logical group 'NOT' must contain exactly 1 condition.")
            for i, child in enumerate(node.conditions):
                errors.extend(validate_condition_node(child, f"{path}.conditions[{i}]", is_candidate))
    elif node_type == "CONDITION":
        if not node.operator:
            errors.append(f"{path}: Operator is missing.")
        elif node.operator.strip().upper() not in VALID_OPERATORS:
            errors.append(f"{path}: Unknown operator '{node.operator}'. Must be one of {sorted(VALID_OPERATORS)}")

        errors.extend(validate_indicator(node.lhs, f"{path}.lhs", is_candidate))
        errors.extend(validate_comparison_value(node.rhs, f"{path}.rhs", is_candidate))

        # BETWEEN operator check
        if node.operator and node.operator.strip().upper() == "BETWEEN":
            if node.rhs and node.rhs.type != "NUMBER_RANGE":
                errors.append(f"{path}: Operator 'BETWEEN' requires a NUMBER_RANGE comparison value.")

    return errors

def validate_strategy_rules(strategy: StrategyBase) -> List[str]:
    errors = []

    # 1. Action validation
    if not strategy.action:
        errors.append("strategy: Action configuration is missing.")
    else:
        if strategy.action.type != "PAPER_TRADE":
            errors.append(f"strategy.action: Live trading actions '{strategy.action.type}' are not supported in this milestone. Action must be 'PAPER_TRADE'.")

    # 2. Timeframe validation
    if strategy.timeframe not in VALID_TIMEFRAMES:
        errors.append(f"strategy: Unknown timeframe '{strategy.timeframe}'. Must be one of {sorted(VALID_TIMEFRAMES)}")

    # 3. Action cannot exist without conditions
    global_node = strategy.global_conditions
    candidate_node = strategy.candidate_conditions

    if not global_node and not candidate_node:
        errors.append("strategy: Strategy must contain at least one global or candidate condition.")
    else:
        if global_node:
            errors.extend(validate_condition_node(global_node, "strategy.global_conditions", is_candidate=False))
        if candidate_node:
            errors.extend(validate_condition_node(candidate_node, "strategy.candidate_conditions", is_candidate=True))

    return errors
