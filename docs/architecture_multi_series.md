# Architecture Specification: Educational Multi-Series Rule Inspection Engine

This document specifies the technical architecture, domain models, evaluation pipeline, manifest safety rules, determinism guarantees, and API specifications for **Milestone 3A: Educational Multi-Series Rule Inspection**.

---

## 1. Educational Synthetic-Data Scope

The Multi-Series Rule Inspection Engine evaluates Boolean rules independently across multiple packaged synthetic subject datasets (`synthetic_candidate_option_ce_23000_15m`, `synthetic_candidate_option_pe_23000_15m`, etc.) against a shared reference underlying dataset (`synthetic_underlying_nifty_15m`).

### Strict Neutral Scope Boundaries
- **Neutral Statuses**: Every series evaluates to one of four neutral Boolean statuses: `TRUE`, `FALSE`, `UNAVAILABLE`, `INVALID`.
- **Prohibited Concepts**: The engine strictly does **NOT**:
  - Rank, score, or sort subject series by evaluation status or preference.
  - Pick a "winner" or "best candidate".
  - Generate trading recommendations, opportunity claims, or BUY/SELL signals.
  - Simulate orders, positions, risk metrics, or profitability claims.
  - Connect to broker APIs or consume live market data.

---

## 2. Framework-Independent Domain Models

Models are defined with `model_config = ConfigDict(extra="forbid")` and strict timezone-aware `datetime` validation.

### `SeriesEvaluationResult`
```python
class SeriesEvaluationResult(BaseModel):
    dataset_id: str
    instrument_id: str
    timeframe: str
    evaluation_timestamp: datetime  # Timezone-aware UTC datetime
    candle_timestamp_used: Optional[datetime] = None
    overall_status: EvaluationStatus  # TRUE | FALSE | UNAVAILABLE | INVALID
    reference_result: Optional[Union[ConditionResult, GroupResult]] = None
    subject_result: Optional[Union[ConditionResult, GroupResult]] = None
    passed_condition_ids: List[str] = []
    failed_condition_ids: List[str] = []
    unavailable_condition_ids: List[str] = []
    invalid_condition_ids: List[str] = []
    inspection_summary: str  # Neutral human-readable inspection text
```

### `MultiSeriesEvaluationResult`
```python
class MultiSeriesEvaluationResult(BaseModel):
    strategy_id: Optional[str] = None
    requested_evaluation_timestamp: datetime  # Timezone-aware UTC datetime
    reference_dataset_id: str
    reference_timestamp_used: Optional[datetime] = None
    results: List[SeriesEvaluationResult]  # Retains exact input order of subject_dataset_ids
    status_counts: Dict[str, int]  # Keys ALWAYS contain TRUE, FALSE, UNAVAILABLE, INVALID
    total_series_evaluated: int
    warnings: List[str] = []
```

### Status Counts Invariant
The `status_counts` dictionary always contains integer values for all four keys, satisfying:
$$\text{status\_counts}["\text{TRUE}"] + \text{status\_counts}["\text{FALSE}"] + \text{status\_counts}["\text{UNAVAILABLE}"] + \text{status\_counts}["\text{INVALID}"] == \text{total\_series\_evaluated}$$

---

## 3. Safe Synthetic Dataset Manifest Registry

- **Category Enum**: `DatasetCategory(str, Enum)` with values `REFERENCE = "REFERENCE"` and `SUBJECT = "SUBJECT"`.
- **Validation Rules**:
  - Validates duplicate manifest dataset IDs at server startup.
  - Dynamically calculates `candle_count` and `completed_candle_count` from packaged CSV fixtures.
  - Server-side whitelist ONLY. Arbitrary file paths or parameters are forbidden.
  - Rejects `REFERENCE` datasets used as subjects and `SUBJECT` datasets used as reference.

---

## 4. Multi-Series Evaluator Service Architecture

### Shared Reference Evaluation
1. Evaluates reference scope (`global_conditions`) ONCE using `RuleEvaluator._evaluate_node`.
2. Reuses `ref_result` across all subject dataset evaluations without recalculation.

### Independent Subject Evaluation & Error Separation
1. **Request-Level 4xx Errors** (rejects entire request before expensive calculation):
   - Unknown reference or subject dataset IDs.
   - Wrong dataset categories (`REFERENCE` vs `SUBJECT`).
   - Empty subject dataset list (`subject_dataset_ids = []`).
   - Duplicate subject dataset IDs.
   - Subject dataset count > 20.
   - Invalid or naive `eval_timestamp`.
   - Invalid strategy blueprint.
   - Combined completed-candle limit exceeded (> 50,000 candles).
   - Reference dataset calculation failure (all subjects depend on reference scope).
2. **Per-Series `INVALID` Results** (continues evaluating remaining subjects):
   - Subject timeframe mismatch (e.g. 5m subject vs 15m strategy).
   - Subject candle validation failure or corrupt CSV.
   - Subject dataset missing a completed candle at or before `eval_timestamp`.
   - Subject indicator calculation failure.

---

## 5. API Endpoints

- `GET /multi-series/datasets`: Returns pre-packaged safe dataset manifest entries in stable order.
- `POST /multi-series/evaluate`: Evaluates rules across 1 to 20 subject datasets and returns `MultiSeriesEvaluationResult`.

---

## 6. Accessibility & Responsive UI Specs

- Live region (`role="status"`, `aria-live="polite"`) for screen-reader announcements upon evaluation completion.
- Keyboard-accessible multi-select with visible focus rings (`focus:ring-2 focus:ring-sky-500`).
- Status communicated via color + text + icons for colorblind accessibility.
- Expandable result trees using `aria-expanded` and `aria-controls`.
- Responsive layout supporting 375px mobile screens without horizontal scrollbars.
