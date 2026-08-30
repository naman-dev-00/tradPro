# Historical Replay Reproducibility Verification

## Overview
Reproducibility Verification evaluates whether a stored historical replay run is reproducible using its stored strategy snapshot, ordered dataset IDs, requested timestamps, fixture checksums, engine version, manifest version, and replay schema version.

## Verification Status Precedence

Verification status evaluation enforces a strict deterministic precedence hierarchy:
` INVALID > UNVERIFIABLE > MISMATCH > VERIFIED `

### Decision Matrix

1. **INVALID**
   - Run record is null or status is not `COMPLETED`.
   - `requested_start_timestamp` > `requested_end_timestamp`.
   - `result_payload` is malformed or invalid JSON structure.

2. **UNVERIFIABLE**
   - Required metadata or snapshots missing (`strategy_definition_snapshot`, `result_payload`, `manifest_checksums_snapshot`, `request_fingerprint`, `engine_version`, or `manifest_version`).

3. **MISMATCH**
   - Any of the following differ from expected current state:
     - `fingerprint_matches`: `stored_request_fingerprint` != recomputed canonical request fingerprint.
     - `engine_version_matches`: `stored_engine_version` != `"1.0.0"`.
     - `manifest_version_matches`: `stored_manifest_version` != `"1.0.0"`.
     - `replay_schema_version_matches`: `stored_replay_schema_version` != `"1.0.0"`.
     - `dataset_checksum_results`: any fixture dataset checksum differs from current fixture SHA-256 hash.

4. **VERIFIED**
   - All required fields are present and 100% of fingerprint, version, and checksum checks match.

## Deterministic Reasons Ordering
Reasons explaining non-verified status are returned in a stable, deterministic order:
1. Structural or timestamp validity warnings.
2. Missing snapshot or metadata warnings.
3. Request fingerprint mismatch warning.
4. Engine, Manifest, and Replay Schema version warnings.
5. Dataset checksum warnings (ordered by `dataset_id` ascending).
