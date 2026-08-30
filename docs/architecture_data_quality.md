# Architecture: Synthetic Dataset Quality & Diagnostics Engine

## Overview
The Dataset Quality Engine (`src/engine/dataset_quality_engine.py`) provides pure, deterministic, framework-independent evaluation of packaged synthetic educational datasets in TradePro.

## Core Architectural Principles

### 1. Pure Engine Boundary
- Accepts CSV text / lines iterable and manifest/provenance metadata.
- Zero FastAPI, SQLAlchemy, or database dependencies.
- Zero client path acceptance or direct disk I/O inside the pure engine.

### 2. Tolerant Parsing vs Strict Loader
- **Strict Loader** (`csv_loader.py`): Rejects incomplete candles and aborts immediately on first structural violation during indicator and strategy execution.
- **Tolerant Quality Parser** (`dataset_quality_engine.py`): Iterates through every line, records bounded structural and semantic issues (`row_number`, `field`, `code`, sanitized preview), counts malformed vs valid rows, and builds an exhaustive diagnostic report.

### 3. Non-Mutating Audits
- Packaged synthetic dataset files on disk are **never modified, rewritten, deduplicated, or normalized** during audits.

### 4. Complexity & Limit Enforcement
- Maximum 5,000 rows per dataset.
- Maximum 1,000 reported issues per audit (total issue count preserved; deterministic warning added when truncated).
- Maximum 5 MB serialized report payload limit.
