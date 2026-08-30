import pytest
from datetime import datetime, timezone
from src.engine.replay_comparison_engine import ReplayComparisonEngine
from src.engine.fingerprint import compute_request_fingerprint
from src.engine.manifest import get_manifest_checksums_snapshot, MANIFEST_VERSION

class DummyRun:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", "run-123")
        self.status = kwargs.get("status", "COMPLETED")
        self.strategy_id = kwargs.get("strategy_id", "strat-1")
        self.strategy_definition_snapshot = kwargs.get("strategy_definition_snapshot", {"name": "Test Strategy"})
        self.reference_dataset_id = kwargs.get("reference_dataset_id", "synthetic_underlying_nifty_15m")
        self.subject_dataset_ids = kwargs.get("subject_dataset_ids", ["synthetic_candidate_option_ce_23000_15m"])
        self.requested_start_timestamp = kwargs.get("requested_start_timestamp", datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc))
        self.requested_end_timestamp = kwargs.get("requested_end_timestamp", datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc))
        self.engine_version = kwargs.get("engine_version", "1.0.0")
        self.manifest_version = kwargs.get("manifest_version", MANIFEST_VERSION)
        self.manifest_checksums_snapshot = kwargs.get("manifest_checksums_snapshot", get_manifest_checksums_snapshot())
        self.result_payload = kwargs.get("result_payload", {"sampling_step": 1, "replay_schema_version": "1.0.0", "replay_points": []})

        if "request_fingerprint" in kwargs:
            self.request_fingerprint = kwargs["request_fingerprint"]
        else:
            self.request_fingerprint = compute_request_fingerprint(
                self.strategy_definition_snapshot,
                self.reference_dataset_id,
                self.subject_dataset_ids,
                self.requested_start_timestamp,
                self.requested_end_timestamp,
                1,
            )

def test_verify_run_none_returns_invalid():
    res = ReplayComparisonEngine.verify_run(None)
    assert res.verification_status == "INVALID"
    assert "not found" in res.reasons[0]

def test_verify_run_failed_status_returns_invalid():
    run = DummyRun(status="FAILED")
    res = ReplayComparisonEngine.verify_run(run)
    assert res.verification_status == "INVALID"
    assert "not COMPLETED" in res.reasons[0]

def test_verify_run_impossible_timestamps_returns_invalid():
    run = DummyRun(
        requested_start_timestamp=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
        requested_end_timestamp=datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
    )
    res = ReplayComparisonEngine.verify_run(run)
    assert res.verification_status == "INVALID"
    assert "start timestamp is after end" in res.reasons[0]

def test_verify_run_missing_snapshots_returns_unverifiable():
    run = DummyRun(strategy_definition_snapshot=None)
    res = ReplayComparisonEngine.verify_run(run)
    assert res.verification_status == "UNVERIFIABLE"
    assert not res.strategy_snapshot_present
    assert any("strategy_definition_snapshot is missing" in r for r in res.reasons)

def test_verify_run_perfect_match_returns_verified():
    run = DummyRun()
    res = ReplayComparisonEngine.verify_run(run)
    assert res.verification_status == "VERIFIED"
    assert res.fingerprint_matches is True
    assert res.engine_version_matches is True
    assert res.manifest_version_matches is True
    assert res.replay_schema_version_matches is True
    assert all(r.matches for r in res.dataset_checksum_results)
    assert res.reasons == ["Run reproducibility fully verified."]

def test_verify_run_fingerprint_mismatch_returns_mismatch():
    run = DummyRun(request_fingerprint="0" * 64)
    res = ReplayComparisonEngine.verify_run(run)
    assert res.verification_status == "MISMATCH"
    assert res.fingerprint_matches is False
    assert any("fingerprint mismatch" in r for r in res.reasons)

def test_verify_run_engine_version_mismatch_returns_mismatch():
    run = DummyRun(engine_version="0.9.0")
    res = ReplayComparisonEngine.verify_run(run)
    assert res.verification_status == "MISMATCH"
    assert res.engine_version_matches is False
    assert any("Engine version mismatch" in r for r in res.reasons)

def test_verify_run_manifest_version_mismatch_returns_mismatch():
    run = DummyRun(manifest_version="0.9.0")
    res = ReplayComparisonEngine.verify_run(run)
    assert res.verification_status == "MISMATCH"
    assert res.manifest_version_matches is False
    assert any("Manifest version mismatch" in r for r in res.reasons)

def test_verify_run_replay_schema_version_mismatch_returns_mismatch():
    run = DummyRun(result_payload={"sampling_step": 1, "replay_schema_version": "0.5.0"})
    res = ReplayComparisonEngine.verify_run(run)
    assert res.verification_status == "MISMATCH"
    assert res.replay_schema_version_matches is False
    assert any("Replay schema version mismatch" in r for r in res.reasons)

def test_verify_run_dataset_checksum_mismatch_returns_mismatch():
    checksums = get_manifest_checksums_snapshot()
    corrupted_checksums = dict(checksums)
    first_key = list(corrupted_checksums.keys())[0]
    corrupted_checksums[first_key] = "badhash"

    run = DummyRun(manifest_checksums_snapshot=corrupted_checksums)
    res = ReplayComparisonEngine.verify_run(run)
    assert res.verification_status == "MISMATCH"
    assert any("Dataset checksum mismatch" in r for r in res.reasons)

def test_verification_precedence_invalid_over_unverifiable():
    # Both invalid status (FAILED) and missing snapshot
    run = DummyRun(status="FAILED", strategy_definition_snapshot=None)
    res = ReplayComparisonEngine.verify_run(run)
    assert res.verification_status == "INVALID"
