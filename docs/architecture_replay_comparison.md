# Architecture: Deterministic Replay Comparison

## Overview
The Replay Comparison Engine (`src/engine/replay_comparison_engine.py`) provides pure, framework-independent comparison and evaluation of completed synthetic historical replay runs in TradePro.

## Core Architectural Principles

### 1. Pure Engine Independence
- Independent of FastAPI, SQLAlchemy sessions, and database persistence services.
- Operates on immutable stored run metadata and `result_payload` structures.
- Does not modify stored payloads or mutate state.

### 2. Alignment Key
Points across two historical replay runs are aligned using the tuple:
` (normalized_utc_timestamp_iso, dataset_id) `

- Missing points in either run are represented explicitly using `baseline_present: bool`, `comparison_present: bool`, `baseline_status: Optional[str]`, and `comparison_status: Optional[str]`.
- Missing points are never treated as `FALSE`.

### 3. Deterministic Ordering
Aligned points are emitted in a strict, reproducible sequence:
1. **Normalized UTC Timestamp**: Ascending order (`timestamp ascending`).
2. **Reference Point**: Reference dataset ID first (if present).
3. **Subject Datasets**: Subjects ordered strictly according to `subject_dataset_ids` in the baseline run, followed by subjects in the comparison run not present in baseline.
4. **Fallback**: Alphabetical sorting by `dataset_id` for subjects not present in array metadata.
- Original subject dataset arrays are never sorted during fingerprinting or comparison.

### 4. Complexity & Limit Enforcement
- Maximum 2 runs per comparison.
- Maximum 20 subjects per run.
- Maximum 5,000 replay timestamps per run.
- Maximum 100,000 aligned points total.
- Maximum 10 MB export payload limit.

### 5. Transition Summary Matrix
A 5x5 matrix tracking all transition combinations across the neutral evaluation statuses (`TRUE`, `FALSE`, `UNAVAILABLE`, `INVALID`) and the alignment axis label (`ABSENT`).
- `ABSENT` is exclusively an alignment axis label and never an `EvaluationStatus`.
