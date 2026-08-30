# Educational Exports Specification

## Overview
TradePro provides deterministic JSON and CSV exports for completed synthetic historical replay runs. All export endpoints enforce strict safety, formula injection protection, size limits, and educational synthetic data notices.

## Export Endpoints
- `GET /api/v1/replays/{run_id}/export?format=json` (and `/export.json`)
- `GET /api/v1/replays/{run_id}/export?format=csv` (and `/export.csv`)

## Safety & Security Standards

### 1. CSV Formula Injection Protection
Textual CSV cells starting with any of the following characters (raw or after stripping leading whitespace) are escaped by prepending a single quote (`'`):
- `=` (equals)
- `+` (plus)
- `-` (minus)
- `@` (at sign)
- `\t` (tab)
- `\r` (carriage return)

*Note*: Legitimate typed numeric values (e.g. `-15.5`, `100`, `42.0`) are preserved as numbers and are not escaped.

### 2. Payload Size Limit
- Strict 10 MB limit (`10,485,760 bytes`) enforced on the final serialized UTF-8 byte payload before sending response headers.

### 3. Safe Filenames & Headers
- Filenames generated strictly from validated run UUID string (e.g., `replay_597a9957-ed19-6a5c-70f1-a6f631b30507.json` or `.csv`).
- Header: `Content-Disposition: attachment; filename="replay_{run_id}.{ext}"`.
- UTF-8 charset specified in `Content-Type`.

### 4. Educational Notice & Excluded Internals
Exports explicitly include an educational disclaimer notice:
> "Educational synthetic historical replay inspection data only. This export does not contain trading signals, execution orders, recommendations, or profitability calculations."

Internal database ORM state, session connections, and non-educational internal fields are excluded.
