import pytest
from datetime import datetime, timezone
from src.engine.replay_comparison_engine import ReplayComparisonEngine, ALL_TRANSITION_STATUSES
from src.engine.replay_comparison_models import ReplayComparisonResult

class DummyRun:
    def __init__(self, run_id, status="COMPLETED", ref_ds="ref-1", subject_ds=None, replay_points=None):
        self.id = run_id
        self.status = status
        self.strategy_id = "strat-1"
        self.reference_dataset_id = ref_ds
        self.subject_dataset_ids = subject_ds or ["subj-1", "subj-2"]
        self.requested_start_timestamp = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
        self.requested_end_timestamp = datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc)
        self.engine_version = "1.0.0"
        self.manifest_version = "1.0.0"
        self.created_at = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
        self.result_payload = {
            "sampling_step": 1,
            "replay_points": replay_points or [],
        }

def test_compare_same_run_id_rejected():
    run1 = DummyRun("run-1")
    with pytest.raises(ValueError, match="Cannot compare a historical replay run with itself"):
        ReplayComparisonEngine.compare_runs(run1, run1)

def test_compare_non_completed_run_rejected():
    run1 = DummyRun("run-1", status="FAILED")
    run2 = DummyRun("run-2", status="COMPLETED")
    with pytest.raises(ValueError, match="Only COMPLETED runs can be compared"):
        ReplayComparisonEngine.compare_runs(run1, run2)

def test_compare_identical_payloads_different_ids():
    points = [
        {
            "evaluation_timestamp": "2026-08-28T09:15:00Z",
            "results": [
                {
                    "dataset_id": "subj-1",
                    "overall_status": "TRUE",
                    "passed_condition_ids": ["c1"],
                    "failed_condition_ids": [],
                    "unavailable_condition_ids": [],
                    "invalid_condition_ids": [],
                    "inspection_summary": "Condition passed",
                }
            ],
        }
    ]

    run1 = DummyRun("run-1", replay_points=points)
    run2 = DummyRun("run-2", replay_points=points)

    res = ReplayComparisonEngine.compare_runs(run1, run2, include_unchanged=True)
    assert res.aligned_point_count == 1
    assert res.changed_point_count == 0
    assert res.unchanged_point_count == 1
    assert res.status_transition_counts["TRUE -> TRUE"] == 1
    assert len(res.differences) == 1
    assert res.differences[0].changed is False

def test_status_transition_matrix_all_25_combinations():
    # Construct 25 points covering all (baseline, comparison) status pairs
    b_points = []
    c_points = []

    statuses = ["TRUE", "FALSE", "UNAVAILABLE", "INVALID", "ABSENT"]
    idx = 0
    for b_st in statuses:
        for c_st in statuses:
            ts = f"2026-08-28T10:{idx:02d}:00Z"
            idx += 1

            if b_st != "ABSENT":
                b_points.append({
                    "evaluation_timestamp": ts,
                    "results": [{"dataset_id": "subj-1", "overall_status": b_st}]
                })
            if c_st != "ABSENT":
                c_points.append({
                    "evaluation_timestamp": ts,
                    "results": [{"dataset_id": "subj-1", "overall_status": c_st}]
                })

    run_b = DummyRun("run-b", replay_points=b_points)
    run_c = DummyRun("run-c", replay_points=c_points)

    res = ReplayComparisonEngine.compare_runs(run_b, run_c, include_unchanged=True)
    assert res.aligned_point_count == 24

    # Verify transition matrix counts
    for b_st in statuses:
        for c_st in statuses:
            key = f"{b_st} -> {c_st}"
            expected = 0 if (b_st == "ABSENT" and c_st == "ABSENT") else 1
            assert res.status_transition_counts[key] == expected

def test_absent_vs_unavailable_distinction():
    # Point 1: absent in baseline (baseline_present=False)
    # Point 2: present in baseline with status UNAVAILABLE (baseline_present=True, baseline_status="UNAVAILABLE")
    b_points = [
        {
            "evaluation_timestamp": "2026-08-28T09:30:00Z",
            "results": [{"dataset_id": "subj-1", "overall_status": "UNAVAILABLE"}]
        }
    ]

    c_points = [
        {
            "evaluation_timestamp": "2026-08-28T09:15:00Z",
            "results": [{"dataset_id": "subj-1", "overall_status": "TRUE"}]
        },
        {
            "evaluation_timestamp": "2026-08-28T09:30:00Z",
            "results": [{"dataset_id": "subj-1", "overall_status": "TRUE"}]
        }
    ]

    run_b = DummyRun("run-b", replay_points=b_points)
    run_c = DummyRun("run-c", replay_points=c_points)

    res = ReplayComparisonEngine.compare_runs(run_b, run_c, include_unchanged=True)
    assert res.aligned_point_count == 2

    # Point 09:15: ABSENT -> TRUE
    pt_0915 = [d for d in res.differences if d.timestamp == "2026-08-28T09:15:00+00:00"][0]
    assert pt_0915.baseline_present is False
    assert pt_0915.baseline_status is None
    assert pt_0915.comparison_status == "TRUE"

    # Point 09:30: UNAVAILABLE -> TRUE
    pt_0930 = [d for d in res.differences if d.timestamp == "2026-08-28T09:30:00+00:00"][0]
    assert pt_0930.baseline_present is True
    assert pt_0930.baseline_status == "UNAVAILABLE"
    assert pt_0930.comparison_status == "TRUE"

def test_changed_condition_id_extraction():
    b_points = [
        {
            "evaluation_timestamp": "2026-08-28T09:15:00Z",
            "results": [
                {
                    "dataset_id": "subj-1",
                    "overall_status": "TRUE",
                    "passed_condition_ids": ["cond-1", "cond-2"],
                    "failed_condition_ids": ["cond-3"],
                }
            ]
        }
    ]

    c_points = [
        {
            "evaluation_timestamp": "2026-08-28T09:15:00Z",
            "results": [
                {
                    "dataset_id": "subj-1",
                    "overall_status": "TRUE",
                    "passed_condition_ids": ["cond-1", "cond-4"],
                    "failed_condition_ids": ["cond-2"],
                }
            ]
        }
    ]

    run_b = DummyRun("run-b", replay_points=b_points)
    run_c = DummyRun("run-c", replay_points=c_points)

    res = ReplayComparisonEngine.compare_runs(run_b, run_c, include_unchanged=True)
    diff = res.differences[0]

    assert diff.changed is True  # overall status same, but condition IDs changed
    assert diff.newly_true_condition_ids == ["cond-4"]
    assert diff.no_longer_true_condition_ids == ["cond-2"]
    assert diff.newly_false_condition_ids == ["cond-2"]
    assert diff.no_longer_false_condition_ids == ["cond-3"]

def test_subject_order_preservation():
    b_points = [
        {"evaluation_timestamp": "2026-08-28T09:15:00Z", "results": [{"dataset_id": "subj-A", "overall_status": "TRUE"}, {"dataset_id": "subj-B", "overall_status": "TRUE"}]}
    ]

    run_b = DummyRun("run-b", subject_ds=["subj-B", "subj-A"], replay_points=b_points)
    run_c = DummyRun("run-c", subject_ds=["subj-B", "subj-C"])

    res = ReplayComparisonEngine.compare_runs(run_b, run_c, include_unchanged=True)
    # Order of datasets emitted for 09:15 should be subj-B then subj-A
    ds_order = [d.dataset_id for d in res.differences]
    assert ds_order == ["subj-B", "subj-A"]


def test_complexity_limits_subjects():
    over_limit_subjects = [f"subj-{i}" for i in range(21)]
    run_b = DummyRun("run-b", subject_ds=over_limit_subjects)
    run_c = DummyRun("run-c", subject_ds=["subj-1"])

    with pytest.raises(ValueError, match="maximum 20 subjects allowed"):
        ReplayComparisonEngine.compare_runs(run_b, run_c)
