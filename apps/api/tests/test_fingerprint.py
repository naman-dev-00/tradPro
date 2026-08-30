import pytest
import json
import hashlib
from datetime import datetime, timezone
from src.engine.fingerprint import compute_request_fingerprint, canonicalize_json

GOLDEN_STRATEGY = {
    "action": {
        "risk_config": {
            "max_position_size": 1.0,
            "stop_loss_pct": 1.0,
            "take_profit_pct": 2.0,
            "validity_window": 5,
        },
        "type": "BUY",
    },
    "candidate_selection_mode": "FIRST_ELIGIBLE",
    "name": "Golden Vector Strategy",
    "timeframe": "15m",
}

GOLDEN_REF = "synthetic_underlying_nifty_15m"
GOLDEN_SUBJECTS = ["synthetic_candidate_option_ce_23000_15m", "synthetic_candidate_option_pe_23000_15m"]
GOLDEN_START = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
GOLDEN_END = datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc)
GOLDEN_STEP = 1

def test_golden_vector_milestone_4a_request():
    hash_val = compute_request_fingerprint(
        strategy_payload=GOLDEN_STRATEGY,
        reference_dataset_id=GOLDEN_REF,
        subject_dataset_ids=GOLDEN_SUBJECTS,
        start_timestamp=GOLDEN_START,
        end_timestamp=GOLDEN_END,
        sampling_step=GOLDEN_STEP,
    )

    # Exact golden hash matching Milestone 4A canonical fingerprint contract
    expected_golden = "c8343e76541fad090df872857c2e7e7c45b30e051d64c23de5900d84b63cb999"
    assert hash_val == expected_golden

def test_golden_vector_reversed_subject_order_alters_hash():
    normal_hash = compute_request_fingerprint(
        GOLDEN_STRATEGY, GOLDEN_REF, GOLDEN_SUBJECTS, GOLDEN_START, GOLDEN_END, GOLDEN_STEP
    )
    reversed_hash = compute_request_fingerprint(
        GOLDEN_STRATEGY, GOLDEN_REF, list(reversed(GOLDEN_SUBJECTS)), GOLDEN_START, GOLDEN_END, GOLDEN_STEP
    )

    assert normal_hash != reversed_hash

def test_golden_vector_reordered_dict_keys_produce_same_hash():
    reordered_strategy = {
        "timeframe": "15m",
        "name": "Golden Vector Strategy",
        "candidate_selection_mode": "FIRST_ELIGIBLE",
        "action": {
            "type": "BUY",
            "risk_config": {
                "validity_window": 5,
                "take_profit_pct": 2.0,
                "stop_loss_pct": 1.0,
                "max_position_size": 1.0,
            },
        },
    }

    normal_hash = compute_request_fingerprint(
        GOLDEN_STRATEGY, GOLDEN_REF, GOLDEN_SUBJECTS, GOLDEN_START, GOLDEN_END, GOLDEN_STEP
    )
    reordered_hash = compute_request_fingerprint(
        reordered_strategy, GOLDEN_REF, GOLDEN_SUBJECTS, GOLDEN_START, GOLDEN_END, GOLDEN_STEP
    )

    assert normal_hash == reordered_hash

def test_version_and_checksum_field_variations_alter_fingerprint(monkeypatch):
    base_hash = compute_request_fingerprint(
        GOLDEN_STRATEGY, GOLDEN_REF, GOLDEN_SUBJECTS, GOLDEN_START, GOLDEN_END, GOLDEN_STEP
    )

    # 1. Changing engine_version alters fingerprint
    monkeypatch.setattr("src.engine.fingerprint.ENGINE_VERSION", "2.0.0")
    engine_var_hash = compute_request_fingerprint(
        GOLDEN_STRATEGY, GOLDEN_REF, GOLDEN_SUBJECTS, GOLDEN_START, GOLDEN_END, GOLDEN_STEP
    )
    assert base_hash != engine_var_hash
    monkeypatch.undo()

    # 2. Changing manifest_version alters fingerprint
    monkeypatch.setattr("src.engine.fingerprint.MANIFEST_VERSION", "2.0.0")
    manifest_var_hash = compute_request_fingerprint(
        GOLDEN_STRATEGY, GOLDEN_REF, GOLDEN_SUBJECTS, GOLDEN_START, GOLDEN_END, GOLDEN_STEP
    )
    assert base_hash != manifest_var_hash
    monkeypatch.undo()

    # 3. Changing replay_schema_version alters fingerprint
    monkeypatch.setattr("src.engine.fingerprint.REPLAY_SCHEMA_VERSION", "2.0.0")
    schema_var_hash = compute_request_fingerprint(
        GOLDEN_STRATEGY, GOLDEN_REF, GOLDEN_SUBJECTS, GOLDEN_START, GOLDEN_END, GOLDEN_STEP
    )
    assert base_hash != schema_var_hash
    monkeypatch.undo()

    # 4. Changing fixture checksum alters fingerprint
    def mock_checksums_modified():
        return {
            GOLDEN_REF: "modified_hash_ref",
            GOLDEN_SUBJECTS[0]: "hash_subj_0",
            GOLDEN_SUBJECTS[1]: "hash_subj_1",
        }
    monkeypatch.setattr("src.engine.fingerprint.get_manifest_checksums_snapshot", mock_checksums_modified)
    checksum_mod_hash = compute_request_fingerprint(
        GOLDEN_STRATEGY, GOLDEN_REF, GOLDEN_SUBJECTS, GOLDEN_START, GOLDEN_END, GOLDEN_STEP
    )
    assert base_hash != checksum_mod_hash
    monkeypatch.undo()

def test_reordering_fixture_checksum_mapping_keys_does_not_change_fingerprint(monkeypatch):
    def mock_checksums_order_1():
        return {"a_ds": "hash_a", "b_ds": "hash_b"}

    def mock_checksums_order_2():
        return {"b_ds": "hash_b", "a_ds": "hash_a"}

    monkeypatch.setattr("src.engine.fingerprint.get_manifest_checksums_snapshot", mock_checksums_order_1)
    hash_1 = compute_request_fingerprint(
        GOLDEN_STRATEGY, "a_ds", ["b_ds"], GOLDEN_START, GOLDEN_END, GOLDEN_STEP
    )

    monkeypatch.setattr("src.engine.fingerprint.get_manifest_checksums_snapshot", mock_checksums_order_2)
    hash_2 = compute_request_fingerprint(
        GOLDEN_STRATEGY, "a_ds", ["b_ds"], GOLDEN_START, GOLDEN_END, GOLDEN_STEP
    )

    assert hash_1 == hash_2
