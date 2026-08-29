# Dataset Reproducibility Model & Fixture Checksums

## 1. Safe Manifest & Fixture SHA-256 Checksums

TradePro enforces deterministic auditability across synthetic educational dataset fixtures.

### Server-Side Fixture Hashes
During server startup, `src.engine.manifest` computes SHA-256 checksums over the exact binary bytes of every packaged fixture CSV file:

- `synthetic_underlying_nifty_15m.csv`
- `synthetic_candidate_option_ce_23000_15m.csv`
- `synthetic_candidate_option_pe_23000_15m.csv`
- `synthetic_candidate_option_ce_23500_15m.csv`
- `synthetic_short_insufficient_5m.csv`
- `synthetic_with_incomplete_candle_15m.csv`

The manifest metadata response (`GET /indicators/datasets` or `GET /multi-series/datasets`) includes:
- `manifest_version`: `"1.0.0"`
- `candle_schema_version`: `"1.0.0"`
- `generated_at`: `"2026-08-28T00:00:00Z"` (fixed fixture metadata timestamp)
- `dataset_checksum`: SHA-256 hex string

---

## 2. Inspection Snapshot Verification

Every `InspectionRun` persisted to the database captures `manifest_checksums_snapshot` at the time of execution.

### Reproducibility Verification Endpoint
`GET /api/v1/replays/{run_id}/reproducibility`

Compares the stored `manifest_checksums_snapshot` against live fixture checksums:
- **`is_exact_match: true`**: All requested fixture checksums match live files exactly.
- **`is_exact_match: false`**: Displays mismatch banner and flags datasets that have changed since the run was created.
