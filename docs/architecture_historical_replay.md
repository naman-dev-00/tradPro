# Architecture: Persistent Inspection History & Historical Boolean Rule Replay (Milestone 3B + 4A)

## 1. Overview & Educational Scope

The **Historical Boolean Rule Replay Engine** and **Persistent Inspection History Service** provide a deterministic, reproducible, audit-trailed analytics platform for evaluating strategy rules repeatedly across historical synthetic candle timestamps.

### Strict Prohibitions
- **NO Financial Trade Simulation**: Zero trade entry/exit markers, order execution, position sizing, or portfolio simulation.
- **NO Performance Metrics**: Zero calculation of profit, loss, win rate, returns, drawdown, transaction costs, or slippage.
- **NO Ranking or Winner Selection**: Zero sorting or selection of "best candidates".
- **NO Signal Generation or Recommendations**: Output consists exclusively of four neutral Boolean statuses: `TRUE`, `FALSE`, `UNAVAILABLE`, `INVALID`.
- **NO Broker Integration**: Operates exclusively on local packaged synthetic CSV fixtures.

### Architectural Limitation
> [!NOTE]
> Authentication is currently unbuilt. Inspection history and replay records are stored as a local single-user educational feature.

---

## 2. Component Architecture

```
                               ┌─────────────────────────────┐
                               │ Next.js Frontend Workspace  │
                               │ /historical-replay-lab      │
                               │ /inspection-history         │
                               └──────────────┬──────────────┘
                                              │ REST API / JSON
                                              ▼
                               ┌─────────────────────────────┐
                               │  FastAPI Versioned Router   │
                               │   /api/v1/replays           │
                               └──────────────┬──────────────┘
                                              │
                     ┌────────────────────────┴────────────────────────┐
                     ▼                                                 ▼
     ┌───────────────────────────────┐                 ┌───────────────────────────────┐
     │  Historical Replay Evaluator  │                 │      Persistence Service      │
     │  (Precomputed Context Engine) │                 │ (SHA-256 Deduplication & DB)  │
     └───────────────┬───────────────┘                 └───────────────┬───────────────┘
                     │                                                 │
                     ▼                                                 ▼
     ┌───────────────────────────────┐                 ┌───────────────────────────────┐
     │  MultiSeriesEvaluator Engine  │                 │    SQLAlchemy InspectionRun    │
     │  & RuleEvaluator (Core)       │                 │  (SQLite & PostgreSQL Unified)│
     └───────────────────────────────┘                 └───────────────────────────────┘
```

---

## 3. Persistent Data Schema (`inspection_runs`)

| Field Name | Type | Constraints | Description |
|---|---|---|---|
| `id` | `VARCHAR(36)` | Primary Key | UUID identifier |
| `strategy_id` | `VARCHAR(36)` | Index, Nullable | Referenced strategy ID |
| `strategy_version_snapshot` | `VARCHAR(100)` | Nullable | Strategy version string |
| `strategy_definition_snapshot` | `JSON` | Nullable (Req. COMPLETED) | Complete strategy JSON payload |
| `run_type` | `VARCHAR(50)` | Index, CheckConstraint | `SINGLE_SERIES`, `MULTI_SERIES`, `HISTORICAL_REPLAY` |
| `reference_dataset_id` | `VARCHAR(255)` | Nullable (Req. COMPLETED) | Reference dataset identifier |
| `subject_dataset_ids` | `JSON` | Not Null | List of subject dataset identifiers |
| `requested_start_timestamp` | `TIMESTAMP` | UTCDateTime, Nullable | UTC range start |
| `requested_end_timestamp` | `TIMESTAMP` | UTCDateTime, Nullable | UTC range end |
| `timeframe` | `VARCHAR(50)` | Not Null | Candle timeframe (e.g. `15m`) |
| `engine_version` | `VARCHAR(50)` | Default `'1.0.0'` | Engine version constant |
| `manifest_version` | `VARCHAR(50)` | Default `'1.0.0'` | Manifest version constant |
| `created_at` | `TIMESTAMP` | UTCDateTime, Index, Not Null | Creation timestamp |
| `completed_at` | `TIMESTAMP` | UTCDateTime, Nullable (Req. COMPLETED) | Completion timestamp |
| `status` | `VARCHAR(50)` | Index, CheckConstraint | `COMPLETED` or `FAILED` |
| `failure_summary` | `VARCHAR(2048)` | Nullable (Req. FAILED) | Sanitized error summary (FAILED only) |
| `result_summary` | `VARCHAR(2048)` | Nullable | Human-readable summary |
| `result_payload` | `JSON` | Nullable (Req. COMPLETED) | Compact replay result JSON (max 10 MB) |
| `synthetic_data_confirmed` | `BOOLEAN` | Default `1`, CheckConstraint | Confirms synthetic dataset usage |
| `request_fingerprint` | `VARCHAR(64)` | Index, Nullable | Optional non-unique request fingerprint audit field |
| `completed_fingerprint` | `VARCHAR(64)` | Index, UniqueConstraint, Nullable | SHA-256 fingerprint for COMPLETED runs (null on FAILED) |
| `manifest_checksums_snapshot` | `JSON` | Nullable (Req. COMPLETED) | Map of dataset ID -> SHA-256 fixture hash |

---

## 4. Compact Payload Format Specification

The `result_payload` stores evaluation results token-efficiently to fit within safety limits:
- **Shared Reference Metadata & Tree**: Stored once at the top level (`reference_metadata`). Full reference condition trees are NOT duplicated across timestamps.
- **Per Timestamp Replay Point**: Stores timestamp, reference timestamp used, and aggregate status counts (`TRUE`, `FALSE`, `UNAVAILABLE`, `INVALID`).
- **Per Subject/Timestamp Evaluation**: Stores subject dataset ID, overall neutral status, condition IDs, and bounded inspection summary text.
- **Subject Timelines**: Stored once at the top level per subject (`points` array, `transition_counts`, `consecutive_status_runs`).
- **Payload Limit Enforcement**: Replays are rejected before database persistence if the serialized UTF-8 JSON payload exceeds 10 MB.

---

## 5. Race-Safe Request Deduplication & Failure Semantics

1. **Fingerprint Canonicalization**: SHA-256 hex string calculated over canonical strategy JSON snapshot, reference dataset ID, **array-ordered** subject dataset IDs (preserving input array order), normalized UTC start/end timestamps, sampling step, engine version (`1.0.0`), manifest version (`1.0.0`), replay schema version (`1.0.0`), and ordered fixture checksums mapping.
2. **Deduplication & Retry Behavior**:
   - `COMPLETED` runs set `completed_fingerprint = fingerprint` with a `UniqueConstraint`.
   - Queries DB for existing `COMPLETED` run matching `completed_fingerprint == fingerprint`. Returns `(existing_run, status=200)` if found.
   - `FAILED` runs set `completed_fingerprint = None` (request_fingerprint is stored for auditing). `FAILED` runs are never reused and never block subsequent retries.
   - On concurrent duplicate submissions, catches `IntegrityError` on `completed_fingerprint`, rolls back, re-queries, and returns the existing `COMPLETED` run.
3. **Transaction Safety on Error**:
   - Partial writes are completely rolled back upon evaluation failure.
   - A short separate transaction saves a sanitized `FAILED` record with no stack traces, raw SQL, or filesystem paths.
