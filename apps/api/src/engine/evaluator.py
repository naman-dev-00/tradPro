from datetime import datetime, timezone
from typing import List, Optional, Tuple, Dict, Any, Union
from src.engine.models import Candle
from src.engine.indicators import (
    preprocess_candle_series,
    calculate_price,
    calculate_sma,
    calculate_ema,
    calculate_rsi,
    calculate_macd,
    calculate_pivot,
    calculate_volume,
    calculate_average_volume,
)
from src.engine.rule_models import (
    EvaluationStatus,
    ConditionResult,
    GroupResult,
    RuleEvaluationResult,
)

DEFAULT_EQUALS_TOLERANCE = 1e-6
DEFAULT_TOUCHES_TOLERANCE = 1e-4

class RuleEvaluator:
    def __init__(self, max_depth: int = 10, max_total_nodes: int = 200, max_candles: int = 5000):
        self.max_depth = max_depth
        self.max_total_nodes = max_total_nodes
        self.max_candles = max_candles
        self._node_count = 0

    def evaluate_strategy_rules(
        self,
        strategy_payload: Dict[str, Any],
        reference_candles: List[Candle],
        subject_candles: Optional[List[Candle]] = None,
        eval_timestamp: Optional[datetime] = None,
    ) -> RuleEvaluationResult:
        self._node_count = 0

        # Validate candle counts
        if len(reference_candles) > self.max_candles:
            raise ValueError(f"Reference series exceeds maximum limit of {self.max_candles} candles.")
        if subject_candles and len(subject_candles) > self.max_candles:
            raise ValueError(f"Subject series exceeds maximum limit of {self.max_candles} candles.")

        # Ensure candles are preprocessed completed candles
        ref_series = preprocess_candle_series(reference_candles)
        if not ref_series:
            raise ValueError("Reference candle series contains no completed candles.")

        subj_series: Optional[List[Candle]] = None
        if subject_candles:
            subj_series = preprocess_candle_series(subject_candles)
            if not subj_series:
                raise ValueError("Subject candle series contains no completed candles.")

        # Timeframe match check
        timeframe = strategy_payload.get("timeframe", "15m")
        for c in ref_series:
            if c.timeframe != timeframe:
                raise ValueError(f"Reference candle timeframe '{c.timeframe}' does not match strategy timeframe '{timeframe}'.")

        if subj_series:
            for c in subj_series:
                if c.timeframe != timeframe:
                    raise ValueError(f"Subject candle timeframe '{c.timeframe}' does not match strategy timeframe '{timeframe}'.")

        # Determine evaluation timestamp
        if eval_timestamp is None:
            eval_dt = ref_series[-1].timestamp
        else:
            if eval_timestamp.tzinfo is None:
                eval_dt = eval_timestamp.replace(tzinfo=timezone.utc)
            else:
                eval_dt = eval_timestamp.astimezone(timezone.utc)

        # Align series to latest completed candle on or before eval_dt
        ref_idx, ref_time = self._find_candle_at_or_before(ref_series, eval_dt)
        if ref_idx is None:
            raise ValueError(f"No reference completed candle exists on or before evaluation timestamp {eval_dt.isoformat()}.")

        subj_idx: Optional[int] = None
        subj_time: Optional[str] = None
        if subj_series:
            subj_idx, subj_time = self._find_candle_at_or_before(subj_series, eval_dt)
            if subj_idx is None:
                raise ValueError(f"No subject completed candle exists on or before evaluation timestamp {eval_dt.isoformat()}.")

        # Extract Action & Validity Window
        action = strategy_payload.get("action", {})
        risk_config = action.get("risk_config", {})
        validity_window = risk_config.get("validity_window", 5)

        # Evaluate Reference Scope (global_conditions)
        ref_result: Optional[Union[GroupResult, ConditionResult]] = None
        global_tree = strategy_payload.get("global_conditions")
        if global_tree:
            ref_result = self._evaluate_node(
                node=global_tree,
                candles=ref_series[: ref_idx + 1],
                target_idx=ref_idx,
                validity_window=validity_window,
                path_prefix="global.0",
                depth=1,
            )

        # Evaluate Subject Scope (candidate_conditions)
        subj_result: Optional[Union[GroupResult, ConditionResult]] = None
        candidate_tree = strategy_payload.get("candidate_conditions")
        if candidate_tree:
            if not subj_series or subj_idx is None:
                # If strategy has candidate_conditions but no subject series provided
                subj_result = ConditionResult(
                    condition_id="candidate.0",
                    status=EvaluationStatus.UNAVAILABLE,
                    operator="NONE",
                    reason="No subject candle series provided for candidate evaluation.",
                )
            else:
                subj_result = self._evaluate_node(
                    node=candidate_tree,
                    candles=subj_series[: subj_idx + 1],
                    target_idx=subj_idx,
                    validity_window=validity_window,
                    path_prefix="candidate.0",
                    depth=1,
                )

        # Calculate Overall Status & Collect IDs
        passed_ids: List[str] = []
        failed_ids: List[str] = []
        unavail_ids: List[str] = []
        invalid_ids: List[str] = []

        for res in [ref_result, subj_result]:
            if res:
                self._collect_results(res, passed_ids, failed_ids, unavail_ids, invalid_ids)

        overall = self._determine_overall_status(ref_result, subj_result)

        return RuleEvaluationResult(
            strategy_id=strategy_payload.get("id"),
            evaluated_at=datetime.now(timezone.utc).isoformat(),
            reference_timestamp=ref_time,
            subject_timestamp=subj_time,
            reference_series_result=ref_result,
            subject_series_result=subj_result,
            overall_status=overall,
            passed_condition_ids=passed_ids,
            failed_condition_ids=failed_ids,
            unavailable_condition_ids=unavail_ids,
            invalid_condition_ids=invalid_ids,
        )

    def _find_candle_at_or_before(self, candles: List[Candle], eval_dt: datetime) -> Tuple[Optional[int], Optional[str]]:
        latest_idx: Optional[int] = None
        for i, c in enumerate(candles):
            if c.timestamp <= eval_dt:
                latest_idx = i
            else:
                break
        if latest_idx is not None:
            return latest_idx, candles[latest_idx].timestamp.isoformat()
        return None, None

    def _evaluate_node(
        self,
        node: Dict[str, Any],
        candles: List[Candle],
        target_idx: int,
        validity_window: int,
        path_prefix: str,
        depth: int,
    ) -> Union[GroupResult, ConditionResult]:
        self._node_count += 1
        if self._node_count > self.max_total_nodes:
            raise ValueError(f"Strategy tree exceeds maximum allowed node limit of {self.max_total_nodes}.")

        if depth > self.max_depth:
            raise ValueError(f"Strategy tree nesting depth exceeds maximum limit of {self.max_depth}.")

        node_type = node.get("type")
        node_id = node.get("id") or path_prefix

        if node_type == "CONDITION":
            return self._evaluate_leaf_condition(
                node=node,
                condition_id=node_id,
                candles=candles,
                target_idx=target_idx,
                validity_window=validity_window,
            )
        elif node_type in ["AND", "OR", "NOT"]:
            return self._evaluate_logical_group(
                node=node,
                group_id=node_id,
                candles=candles,
                target_idx=target_idx,
                validity_window=validity_window,
                path_prefix=path_prefix,
                depth=depth,
            )
        else:
            return GroupResult(
                group_id=node_id,
                logical_operator=str(node_type),
                status=EvaluationStatus.INVALID,
                reason=f"Unknown node type '{node_type}'.",
            )

    def _evaluate_logical_group(
        self,
        node: Dict[str, Any],
        group_id: str,
        candles: List[Candle],
        target_idx: int,
        validity_window: int,
        path_prefix: str,
        depth: int,
    ) -> GroupResult:
        op = node.get("type", "AND")
        raw_children = node.get("conditions", [])

        if op == "NOT":
            if len(raw_children) != 1:
                return GroupResult(
                    group_id=group_id,
                    logical_operator="NOT",
                    status=EvaluationStatus.INVALID,
                    reason=f"NOT operator requires exactly 1 child condition, got {len(raw_children)}.",
                )

        if len(raw_children) == 0:
            return GroupResult(
                group_id=group_id,
                logical_operator=op,
                status=EvaluationStatus.INVALID,
                reason="Logical group contains no child conditions.",
            )

        child_results: List[Union[GroupResult, ConditionResult]] = []
        for idx, child_node in enumerate(raw_children):
            child_prefix = f"{path_prefix}.children.{idx}"
            c_res = self._evaluate_node(
                node=child_node,
                candles=candles,
                target_idx=target_idx,
                validity_window=validity_window,
                path_prefix=child_prefix,
                depth=depth + 1,
            )
            child_results.append(c_res)

        # Apply Logical Propagation Truth Rules (WITHOUT SHORT-CIRCUITING)
        statuses = [c.status for c in child_results]

        if op == "AND":
            if EvaluationStatus.INVALID in statuses:
                group_status = EvaluationStatus.INVALID
                reason = "One or more child conditions evaluated to INVALID."
            elif EvaluationStatus.FALSE in statuses:
                group_status = EvaluationStatus.FALSE
                reason = "One or more child conditions evaluated to FALSE."
            elif EvaluationStatus.UNAVAILABLE in statuses:
                group_status = EvaluationStatus.UNAVAILABLE
                reason = "One or more child conditions evaluated to UNAVAILABLE."
            else:
                group_status = EvaluationStatus.TRUE
                reason = "All child conditions evaluated to TRUE."

        elif op == "OR":
            if EvaluationStatus.INVALID in statuses:
                group_status = EvaluationStatus.INVALID
                reason = "One or more child conditions evaluated to INVALID."
            elif EvaluationStatus.TRUE in statuses:
                group_status = EvaluationStatus.TRUE
                reason = "One or more child conditions evaluated to TRUE."
            elif EvaluationStatus.UNAVAILABLE in statuses:
                group_status = EvaluationStatus.UNAVAILABLE
                reason = "One or more child conditions evaluated to UNAVAILABLE."
            else:
                group_status = EvaluationStatus.FALSE
                reason = "All child conditions evaluated to FALSE."

        elif op == "NOT":
            child_st = statuses[0]
            if child_st == EvaluationStatus.TRUE:
                group_status = EvaluationStatus.FALSE
                reason = "Inverted TRUE child to FALSE."
            elif child_st == EvaluationStatus.FALSE:
                group_status = EvaluationStatus.TRUE
                reason = "Inverted FALSE child to TRUE."
            elif child_st == EvaluationStatus.UNAVAILABLE:
                group_status = EvaluationStatus.UNAVAILABLE
                reason = "Inverted UNAVAILABLE child remains UNAVAILABLE."
            else:
                group_status = EvaluationStatus.INVALID
                reason = "Inverted INVALID child remains INVALID."
        else:
            group_status = EvaluationStatus.INVALID
            reason = f"Unsupported logical operator '{op}'."

        return GroupResult(
            group_id=group_id,
            logical_operator=op,
            status=group_status,
            child_results=child_results,
            reason=reason,
        )

    def _evaluate_leaf_condition(
        self,
        node: Dict[str, Any],
        condition_id: str,
        candles: List[Candle],
        target_idx: int,
        validity_window: int,
    ) -> ConditionResult:
        lhs_expr = node.get("lhs")
        operator = node.get("operator", "GREATER_THAN")
        rhs_expr = node.get("rhs")
        raw_tol = node.get("tolerance")

        if not lhs_expr or not operator or not rhs_expr:
            return ConditionResult(
                condition_id=condition_id,
                status=EvaluationStatus.INVALID,
                operator=str(operator),
                reason="Condition node missing lhs, operator, or rhs configuration.",
            )

        # Validate tolerance
        tolerance: Optional[float] = None
        if raw_tol is not None:
            try:
                tolerance = float(raw_tol)
                if tolerance < 0 or not (tolerance == tolerance) or tolerance == float("inf"):
                    return ConditionResult(
                        condition_id=condition_id,
                        status=EvaluationStatus.INVALID,
                        operator=operator,
                        reason=f"Invalid tolerance '{raw_tol}'. Tolerance must be a non-negative finite number.",
                    )
            except Exception:
                return ConditionResult(
                    condition_id=condition_id,
                    status=EvaluationStatus.INVALID,
                    operator=operator,
                    reason=f"Invalid tolerance '{raw_tol}'.",
                )

        # Set default tolerances if not specified
        if tolerance is None:
            if operator == "EQUALS":
                tolerance = DEFAULT_EQUALS_TOLERANCE
            elif operator == "TOUCHES":
                tolerance = DEFAULT_TOUCHES_TOLERANCE

        # 1. Compute LHS indicator value at current target_idx
        lhs_val, lhs_avail, lhs_warmup, lhs_used = self._compute_indicator_val(lhs_expr, candles, target_idx)
        if "error" in lhs_used:
            return ConditionResult(
                condition_id=condition_id,
                status=EvaluationStatus.INVALID,
                operator=operator,
                reason=str(lhs_used["error"]),
            )

        # 2. Compute RHS value/indicator
        rhs_type = rhs_expr.get("type", "NUMBER")
        rhs_val: Any = None
        rhs_avail = True
        rhs_warmup = 0
        rhs_used: Dict[str, Any] = {}

        if rhs_type == "NUMBER":
            rhs_val = rhs_expr.get("value")
            if rhs_val is None:
                return ConditionResult(
                    condition_id=condition_id,
                    status=EvaluationStatus.INVALID,
                    operator=operator,
                    reason="RHS of type NUMBER missing numeric value.",
                )
        elif rhs_type == "NUMBER_RANGE":
            r = rhs_expr.get("range")
            if not r or len(r) != 2:
                return ConditionResult(
                    condition_id=condition_id,
                    status=EvaluationStatus.INVALID,
                    operator=operator,
                    reason="RHS of type NUMBER_RANGE requires [low, high] pair.",
                )
            low, high = r[0], r[1]
            if low > high:
                return ConditionResult(
                    condition_id=condition_id,
                    status=EvaluationStatus.INVALID,
                    operator=operator,
                    reason=f"RHS range [low, high] invalid: low ({low}) is greater than high ({high}).",
                )
            rhs_val = [low, high]
        elif rhs_type == "INDICATOR":
            r_ind = rhs_expr.get("indicator")
            if not r_ind:
                return ConditionResult(
                    condition_id=condition_id,
                    status=EvaluationStatus.INVALID,
                    operator=operator,
                    reason="RHS of type INDICATOR missing indicator expression.",
                )
            rhs_val, rhs_avail, rhs_warmup, rhs_used = self._compute_indicator_val(r_ind, candles, target_idx)
        else:
            return ConditionResult(
                condition_id=condition_id,
                status=EvaluationStatus.INVALID,
                operator=operator,
                reason=f"Unknown RHS type '{rhs_type}'.",
            )

        indicator_used = {"lhs": lhs_used, "rhs": rhs_used if rhs_type == "INDICATOR" else rhs_val}
        warmup_info = {"lhs_warmup": lhs_warmup, "rhs_warmup": rhs_warmup}
        eval_timestamp = candles[target_idx].timestamp.isoformat()

        # Check Warm-up
        if not lhs_avail or not rhs_avail:
            return ConditionResult(
                condition_id=condition_id,
                status=EvaluationStatus.UNAVAILABLE,
                timestamp=eval_timestamp,
                left_value=lhs_val,
                operator=operator,
                right_value=rhs_val,
                reason=f"Indicator warming up (LHS remaining: {lhs_warmup}, RHS remaining: {rhs_warmup}).",
                indicator_values_used=indicator_used,
                warmup_info=warmup_info,
            )

        # Handle Crossover Operators (CROSSES_ABOVE, CROSSES_BELOW)
        if operator in ["CROSSES_ABOVE", "CROSSES_BELOW"]:
            if target_idx < 1:
                return ConditionResult(
                    condition_id=condition_id,
                    status=EvaluationStatus.UNAVAILABLE,
                    timestamp=eval_timestamp,
                    left_value=lhs_val,
                    operator=operator,
                    right_value=rhs_val,
                    reason="Previous candle values unavailable for crossover comparison.",
                    indicator_values_used=indicator_used,
                    warmup_info=warmup_info,
                )

            prev_lhs_val, prev_lhs_avail, _, _ = self._compute_indicator_val(lhs_expr, candles, target_idx - 1)
            if rhs_type == "INDICATOR":
                prev_rhs_val, prev_rhs_avail, _, _ = self._compute_indicator_val(rhs_expr["indicator"], candles, target_idx - 1)
            else:
                prev_rhs_val, prev_rhs_avail = rhs_val, True

            if not prev_lhs_avail or not prev_rhs_avail:
                return ConditionResult(
                    condition_id=condition_id,
                    status=EvaluationStatus.UNAVAILABLE,
                    timestamp=eval_timestamp,
                    left_value=lhs_val,
                    operator=operator,
                    right_value=rhs_val,
                    reason="Previous candle indicator warming up.",
                    indicator_values_used=indicator_used,
                    warmup_info=warmup_info,
                )

            # Evaluate Crossover
            p_left = self._extract_numeric(prev_lhs_val, lhs_expr)
            p_right = self._extract_numeric(prev_rhs_val, rhs_expr.get("indicator") if rhs_type == "INDICATOR" else None)
            c_left = self._extract_numeric(lhs_val, lhs_expr)
            c_right = self._extract_numeric(rhs_val, rhs_expr.get("indicator") if rhs_type == "INDICATOR" else None)

            if p_left is None or p_right is None or c_left is None or c_right is None:
                return ConditionResult(
                    condition_id=condition_id,
                    status=EvaluationStatus.INVALID,
                    timestamp=eval_timestamp,
                    operator=operator,
                    reason="Could not extract numeric scalar for crossover comparison.",
                )

            if operator == "CROSSES_ABOVE":
                is_crossover = (p_left <= p_right) and (c_left > c_right)
            else:  # CROSSES_BELOW
                is_crossover = (p_left >= p_right) and (c_left < c_right)

            status = EvaluationStatus.TRUE if is_crossover else EvaluationStatus.FALSE
            reason = f"{operator} evaluated to {status.value}."

            # Evaluate Validity Window for Crossovers
            return self._apply_validity_window(
                condition_id=condition_id,
                node=node,
                candles=candles,
                target_idx=target_idx,
                validity_window=validity_window,
                current_status=status,
                eval_timestamp=eval_timestamp,
                lhs_val=lhs_val,
                operator=operator,
                rhs_val=rhs_val,
                indicator_used=indicator_used,
                warmup_info=warmup_info,
            )

        # Standard Comparison Operators
        c_left = self._extract_numeric(lhs_val, lhs_expr)
        c_right = self._extract_numeric(rhs_val, rhs_expr.get("indicator") if rhs_type == "INDICATOR" else None)

        if c_left is None:
            return ConditionResult(
                condition_id=condition_id,
                status=EvaluationStatus.INVALID,
                timestamp=eval_timestamp,
                operator=operator,
                reason="LHS expression produced non-numeric or incompatible value.",
            )

        is_true = False
        if operator == "GREATER_THAN":
            if c_right is None: return self._invalid_res(condition_id, operator, "RHS must be numeric for GREATER_THAN.")
            is_true = c_left > c_right
        elif operator == "LESS_THAN":
            if c_right is None: return self._invalid_res(condition_id, operator, "RHS must be numeric for LESS_THAN.")
            is_true = c_left < c_right
        elif operator == "GREATER_THAN_OR_EQUAL":
            if c_right is None: return self._invalid_res(condition_id, operator, "RHS must be numeric for GREATER_THAN_OR_EQUAL.")
            is_true = c_left >= c_right
        elif operator == "LESS_THAN_OR_EQUAL":
            if c_right is None: return self._invalid_res(condition_id, operator, "RHS must be numeric for LESS_THAN_OR_EQUAL.")
            is_true = c_left <= c_right
        elif operator == "EQUALS":
            if c_right is None: return self._invalid_res(condition_id, operator, "RHS must be numeric for EQUALS.")
            is_true = abs(c_left - c_right) <= (tolerance or DEFAULT_EQUALS_TOLERANCE)
        elif operator == "TOUCHES":
            if c_right is None: return self._invalid_res(condition_id, operator, "RHS target must be numeric for TOUCHES.")
            is_true = abs(c_left - c_right) <= (tolerance or DEFAULT_TOUCHES_TOLERANCE)
        elif operator == "BETWEEN":
            if not isinstance(rhs_val, list) or len(rhs_val) != 2:
                return self._invalid_res(condition_id, operator, "RHS for BETWEEN must be range [low, high].")
            low, high = rhs_val[0], rhs_val[1]
            is_true = (low <= c_left <= high)
        else:
            return self._invalid_res(condition_id, operator, f"Unknown comparison operator '{operator}'.")

        status = EvaluationStatus.TRUE if is_true else EvaluationStatus.FALSE

        # Apply Validity Window Semantics
        return self._apply_validity_window(
            condition_id=condition_id,
            node=node,
            candles=candles,
            target_idx=target_idx,
            validity_window=validity_window,
            current_status=status,
            eval_timestamp=eval_timestamp,
            lhs_val=lhs_val,
            operator=operator,
            rhs_val=rhs_val,
            indicator_used=indicator_used,
            warmup_info=warmup_info,
        )

    def _apply_validity_window(
        self,
        condition_id: str,
        node: Dict[str, Any],
        candles: List[Candle],
        target_idx: int,
        validity_window: int,
        current_status: EvaluationStatus,
        eval_timestamp: str,
        lhs_val: Any,
        operator: str,
        rhs_val: Any,
        indicator_used: Dict[str, Any],
        warmup_info: Dict[str, Any],
    ) -> ConditionResult:
        if current_status == EvaluationStatus.TRUE:
            return ConditionResult(
                condition_id=condition_id,
                status=EvaluationStatus.TRUE,
                timestamp=eval_timestamp,
                left_value=lhs_val,
                operator=operator,
                right_value=rhs_val,
                reason=f"Condition evaluated to TRUE at target candle (age 0 <= window {validity_window}).",
                indicator_values_used=indicator_used,
                warmup_info=warmup_info,
            )

        # If FALSE at current candle, check historical trigger within validity_window (age 1 to validity_window)
        # Search backwards from target_idx - 1 down to max(0, target_idx - validity_window)
        min_idx = max(0, target_idx - validity_window)
        for past_idx in range(target_idx - 1, min_idx - 1, -1):
            past_res = self._evaluate_leaf_raw(node, candles, past_idx)
            if past_res == EvaluationStatus.TRUE:
                age = target_idx - past_idx
                if age <= validity_window:
                    return ConditionResult(
                        condition_id=condition_id,
                        status=EvaluationStatus.TRUE,
                        timestamp=eval_timestamp,
                        left_value=lhs_val,
                        operator=operator,
                        right_value=rhs_val,
                        reason=f"Condition became TRUE at past candle {candles[past_idx].timestamp.isoformat()} (age {age} <= window {validity_window}).",
                        indicator_values_used=indicator_used,
                        warmup_info=warmup_info,
                    )

        # Check if condition became TRUE at an earlier candle beyond validity_window
        earliest_idx = max(0, target_idx - validity_window - 20)
        for past_idx in range(target_idx - validity_window - 1, earliest_idx - 1, -1):
            past_res = self._evaluate_leaf_raw(node, candles, past_idx)
            if past_res == EvaluationStatus.TRUE:
                age = target_idx - past_idx
                return ConditionResult(
                    condition_id=condition_id,
                    status=EvaluationStatus.UNAVAILABLE,
                    timestamp=eval_timestamp,
                    left_value=lhs_val,
                    operator=operator,
                    right_value=rhs_val,
                    reason=f"Condition expired (age {age} > validity_window {validity_window}).",
                    indicator_values_used=indicator_used,
                    warmup_info=warmup_info,
                )

        return ConditionResult(
            condition_id=condition_id,
            status=EvaluationStatus.FALSE,
            timestamp=eval_timestamp,
            left_value=lhs_val,
            operator=operator,
            right_value=rhs_val,
            reason=f"Condition evaluated to FALSE (not triggered within validity window {validity_window}).",
            indicator_values_used=indicator_used,
            warmup_info=warmup_info,
        )

    def _evaluate_leaf_raw(self, node: Dict[str, Any], candles: List[Candle], past_idx: int) -> EvaluationStatus:
        lhs_expr = node.get("lhs")
        operator = node.get("operator", "GREATER_THAN")
        rhs_expr = node.get("rhs")

        if not lhs_expr or not rhs_expr:
            return EvaluationStatus.INVALID

        lhs_val, lhs_avail, _, _ = self._compute_indicator_val(lhs_expr, candles, past_idx)
        if not lhs_avail:
            return EvaluationStatus.UNAVAILABLE

        rhs_type = rhs_expr.get("type", "NUMBER")
        rhs_val: Any = None
        if rhs_type == "NUMBER":
            rhs_val = rhs_expr.get("value")
        elif rhs_type == "NUMBER_RANGE":
            rhs_val = rhs_expr.get("range")
        elif rhs_type == "INDICATOR":
            r_ind = rhs_expr.get("indicator")
            if not r_ind: return EvaluationStatus.INVALID
            rhs_val, rhs_avail, _, _ = self._compute_indicator_val(r_ind, candles, past_idx)
            if not rhs_avail: return EvaluationStatus.UNAVAILABLE

        c_left = self._extract_numeric(lhs_val, lhs_expr)
        c_right = self._extract_numeric(rhs_val, rhs_expr.get("indicator") if rhs_type == "INDICATOR" else None)

        if c_left is None: return EvaluationStatus.INVALID

        if operator in ["CROSSES_ABOVE", "CROSSES_BELOW"]:
            if past_idx < 1:
                return EvaluationStatus.UNAVAILABLE
            prev_lhs_val, prev_lhs_avail, _, _ = self._compute_indicator_val(lhs_expr, candles, past_idx - 1)
            if rhs_type == "INDICATOR":
                prev_rhs_val, prev_rhs_avail, _, _ = self._compute_indicator_val(rhs_expr["indicator"], candles, past_idx - 1)
            else:
                prev_rhs_val, prev_rhs_avail = rhs_val, True
            if not prev_lhs_avail or not prev_rhs_avail:
                return EvaluationStatus.UNAVAILABLE
            p_left = self._extract_numeric(prev_lhs_val, lhs_expr)
            p_right = self._extract_numeric(prev_rhs_val, rhs_expr.get("indicator") if rhs_type == "INDICATOR" else None)
            if p_left is None or p_right is None or c_right is None:
                return EvaluationStatus.INVALID
            if operator == "CROSSES_ABOVE":
                return EvaluationStatus.TRUE if (p_left <= p_right and c_left > c_right) else EvaluationStatus.FALSE
            else:
                return EvaluationStatus.TRUE if (p_left >= p_right and c_left < c_right) else EvaluationStatus.FALSE

        if operator == "GREATER_THAN":
            return EvaluationStatus.TRUE if (c_right is not None and c_left > c_right) else EvaluationStatus.FALSE
        elif operator == "LESS_THAN":
            return EvaluationStatus.TRUE if (c_right is not None and c_left < c_right) else EvaluationStatus.FALSE
        elif operator == "GREATER_THAN_OR_EQUAL":
            return EvaluationStatus.TRUE if (c_right is not None and c_left >= c_right) else EvaluationStatus.FALSE
        elif operator == "LESS_THAN_OR_EQUAL":
            return EvaluationStatus.TRUE if (c_right is not None and c_left <= c_right) else EvaluationStatus.FALSE
        elif operator == "EQUALS":
            tol = node.get("tolerance") or DEFAULT_EQUALS_TOLERANCE
            return EvaluationStatus.TRUE if (c_right is not None and abs(c_left - c_right) <= tol) else EvaluationStatus.FALSE
        elif operator == "TOUCHES":
            tol = node.get("tolerance") or DEFAULT_TOUCHES_TOLERANCE
            return EvaluationStatus.TRUE if (c_right is not None and abs(c_left - c_right) <= tol) else EvaluationStatus.FALSE
        elif operator == "BETWEEN":
            if isinstance(rhs_val, list) and len(rhs_val) == 2:
                return EvaluationStatus.TRUE if (rhs_val[0] <= c_left <= rhs_val[1]) else EvaluationStatus.FALSE

        return EvaluationStatus.FALSE

    def _compute_indicator_val(
        self,
        expr: Dict[str, Any],
        candles: List[Candle],
        idx: int,
    ) -> Tuple[Any, bool, int, Dict[str, Any]]:
        ind = expr.get("indicator", "PRICE")
        params = expr.get("params", {}) or {}

        sliced = candles[: idx + 1]

        if ind == "PRICE":
            res = calculate_price(sliced)
        elif ind == "SMA":
            period = params.get("period", 20)
            res = calculate_sma(sliced, period=period)
        elif ind == "EMA":
            period = params.get("period", 20)
            res = calculate_ema(sliced, period=period)
        elif ind == "RSI":
            period = params.get("period", 14)
            res = calculate_rsi(sliced, period=period)
        elif ind == "MACD":
            fast = params.get("fast_period", 12)
            slow = params.get("slow_period", 26)
            sig = params.get("signal_period", 9)
            res = calculate_macd(sliced, fast_period=fast, slow_period=slow, signal_period=sig)
        elif ind == "PIVOT":
            res = calculate_pivot(sliced)
        elif ind == "VOLUME":
            res = calculate_volume(sliced)
        elif ind == "AVERAGE_VOLUME":
            period = params.get("period", 20)
            res = calculate_average_volume(sliced, period=period)
        else:
            return None, False, 0, {"indicator": ind, "error": f"Unknown indicator '{ind}'"}

        target_res = res[-1]
        val = target_res.value
        avail = target_res.available
        warmup = target_res.warmup_remaining

        used_info = {
            "indicator": ind,
            "params": params,
            "raw_value": val,
            "available": avail,
        }

        return val, avail, warmup, used_info

    def _extract_numeric(self, val: Any, expr: Optional[Dict[str, Any]]) -> Optional[float]:
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, dict):
            if expr and expr.get("indicator") == "PIVOT":
                level = expr.get("params", {}).get("level", "P")
                l_key = level.lower()
                if l_key in val and val[l_key] is not None:
                    return float(val[l_key])
            if expr and expr.get("indicator") == "MACD":
                comp = expr.get("params", {}).get("component", "macd")
                c_key = comp.lower()
                if c_key in val and val[c_key] is not None:
                    return float(val[c_key])
            for k in ["close", "pivot", "macd", "value"]:
                if k in val and val[k] is not None:
                    return float(val[k])
        return None

    def _invalid_res(self, condition_id: str, operator: str, reason: str) -> ConditionResult:
        return ConditionResult(
            condition_id=condition_id,
            status=EvaluationStatus.INVALID,
            operator=operator,
            reason=reason,
        )

    def _collect_results(
        self,
        node: Union[GroupResult, ConditionResult],
        passed: List[str],
        failed: List[str],
        unavail: List[str],
        invalid: List[str],
    ):
        cid = getattr(node, "condition_id", getattr(node, "group_id", None))
        st = node.status

        if cid:
            if st == EvaluationStatus.TRUE:
                passed.append(cid)
            elif st == EvaluationStatus.FALSE:
                failed.append(cid)
            elif st == EvaluationStatus.UNAVAILABLE:
                unavail.append(cid)
            elif st == EvaluationStatus.INVALID:
                invalid.append(cid)

        if isinstance(node, GroupResult):
            for child in node.child_results:
                self._collect_results(child, passed, failed, unavail, invalid)

    def _determine_overall_status(
        self,
        ref_res: Optional[Union[GroupResult, ConditionResult]],
        subj_res: Optional[Union[GroupResult, ConditionResult]],
    ) -> EvaluationStatus:
        statuses = []
        if ref_res: statuses.append(ref_res.status)
        if subj_res: statuses.append(subj_res.status)

        if not statuses:
            return EvaluationStatus.INVALID

        if EvaluationStatus.INVALID in statuses:
            return EvaluationStatus.INVALID
        if EvaluationStatus.FALSE in statuses:
            return EvaluationStatus.FALSE
        if EvaluationStatus.UNAVAILABLE in statuses:
            return EvaluationStatus.UNAVAILABLE
        return EvaluationStatus.TRUE
