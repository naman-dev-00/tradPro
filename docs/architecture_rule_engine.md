# Architecture Specification: Educational Rule Evaluation Engine (Milestone 2B)

## 1. Overview & Educational Scope Boundaries

The TradePro Educational Rule Evaluation Engine provides deterministic evaluation of options strategy rules against packaged synthetic educational candle datasets.

> [!IMPORTANT]
> **Educational & Neutral Scope**:
> The engine produces strictly neutral Boolean status values (`TRUE`, `FALSE`, `UNAVAILABLE`, `INVALID`). It generates **no BUY or SELL signals, no trading recommendations, no profitability claims, no candidate ranking, and no paper or live order execution**.

---

## 2. Neutral Evaluation Statuses

Every condition and logical group node evaluates to exactly one of four neutral statuses:

| Status | Meaning |
| :--- | :--- |
| **`TRUE`** | Rule condition or logical group satisfied deterministically at or within validity window. |
| **`FALSE`** | Rule condition or logical group evaluated and failed to meet requirements. |
| **`UNAVAILABLE`** | Technical indicator warming up, crossover missing previous candle, or condition expired. |
| **`INVALID`** | Structural error, invalid parameters, empty group, invalid NOT child count, or non-finite tolerance. |

---

## 3. Comparison Operators & Tolerance Rules

| Operator | Symbol | Evaluation Formula / Rule | Default Tolerance |
| :--- | :---: | :--- | :---: |
| `GREATER_THAN` | `>` | `left > right` | N/A |
| `LESS_THAN` | `<` | `left < right` | N/A |
| `GREATER_THAN_OR_EQUAL` | `>=` | `left >= right` | N/A |
| `LESS_THAN_OR_EQUAL` | `<=` | `left <= right` | N/A |
| `EQUALS` | `==` | `abs(left - right) <= tolerance` | `1e-6` |
| `BETWEEN` | `in range` | `low <= left <= high` (requires `low <= high`) | N/A |
| `CROSSES_ABOVE` | `crosses above` | `prev_left <= prev_right AND curr_left > curr_right` | N/A |
| `CROSSES_BELOW` | `crosses below` | `prev_left >= prev_right AND curr_left < curr_right` | N/A |
| `TOUCHES` | `touches` | `abs(left - target) <= tolerance` | `1e-4` |

*Note: Overridden tolerance values specified in strategy conditions take precedence over defaults.*

---

## 4. Logical Group Propagation Matrix (No Short-Circuiting)

All child nodes are evaluated to populate the full inspection tree in the UI. Overall group status is determined via:

```
AND:
  - INVALID     if ANY child is INVALID
  - Otherwise FALSE if ANY child is FALSE
  - Otherwise UNAVAILABLE if ANY child is UNAVAILABLE
  - Otherwise TRUE

OR:
  - INVALID     if ANY child is INVALID
  - Otherwise TRUE  if ANY child is TRUE
  - Otherwise UNAVAILABLE if ANY child is UNAVAILABLE
  - Otherwise FALSE

NOT:
  - Requires EXACTLY ONE child (returns INVALID otherwise)
  - TRUE        -> FALSE
  - FALSE       -> TRUE
  - UNAVAILABLE -> UNAVAILABLE
  - INVALID     -> INVALID
```

---

## 5. Time Alignment & Validity Window

- **Timeframe Alignment**: Both Reference and Subject candle series must match the strategy timeframe (e.g. `15m`).
- **UTC Timestamp Matching**: Selects latest completed candle where $candle.timestamp \le eval\_timestamp$. Prevents future-data leakage.
- **Validity Window**:
  - Candle where condition becomes `TRUE` is **age 0**.
  - Next completed candle is **age 1**.
  - Valid while `age <= validity_window`.
  - Expires when `age > validity_window` (returns `UNAVAILABLE`).

---

## 6. API Endpoints

- `POST /rules/evaluate`: Evaluates rules for reference & subject series.
- `GET /rules/operators`: Returns supported operator metadata and parameter defaults.

---

## 7. GitHub Actions CI Workflow

Automated CI workflow `.github/workflows/ci.yml` runs on Python 3.12 and Node 22:
- Backend: Pytest with strict warning enforcement (`pytest.ini`).
- Frontend: Vitest (`npm run test`), ESLint (`npm run lint`), TypeScript check (`npx tsc --noEmit`), Next.js build (`npm run build`).
