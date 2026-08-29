# Inspection Exports & Security

## 1. Supported Export Formats

Every completed inspection run can be exported in neutral JSON and CSV formats.

### JSON Export (`GET /api/v1/replays/{run_id}/export.json`)
- Returns the complete `InspectionRun` record, strategy definition snapshot, and full `result_payload`.
- Header: `Content-Disposition: attachment; filename="replay_<run_id>.json"`

### CSV Export (`GET /api/v1/replays/{run_id}/export.csv`)
- Returns tabular dataset inspection timestamps, dataset IDs, and neutral Boolean statuses (`TRUE`, `FALSE`, `UNAVAILABLE`, `INVALID`).
- Header: `Content-Disposition: attachment; filename="replay_<run_id>.csv"`

---

## 2. CSV Formula Injection Protection

Spreadsheet applications (e.g. Microsoft Excel, Google Sheets, LibreOffice Calc) interpret cells beginning with `=`, `+`, `-`, or `@` as executable formulas.

### Sanitization Policy
Before serializing CSV cell values, TradePro inspects every string. If the first non-whitespace character is `=`, `+`, `-`, or `@`, the value is prefixed with a single quote (`'`).

Examples:
- `=SUM(A1:A10)` → `'=SUM(A1:A10)`
- `  =cmd.exe` → `'  =cmd.exe`
- `+100` → `'+100`
- `@SUM(A1)` → `'@SUM(A1)`

This ensures cells render strictly as text in spreadsheet software without formula execution risks.
