import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from src.main import app
from src.database import get_db
from src.models import User, Strategy, InspectionRun, LEGACY_PRINCIPAL_ID
from src.auth.security import hash_password
from src.auth.session import create_session
from src.auth.rate_limiter import rate_limiter
from src.engine.fingerprint import compute_request_fingerprint

def create_authenticated_client(db, username, email, role):
    user = User(
        username=username,
        normalized_username=username.lower(),
        email=email,
        normalized_email=email.lower(),
        hashed_password=hash_password("ValidPassword123!"),
        role=role,
        is_active=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    session_rec, raw_sess, raw_csrf = create_session(db, user)

    client = TestClient(app, headers={"X-CSRF-Token": raw_csrf, "Origin": "http://localhost:3000"})
    client.cookies.set("tradepro_session", raw_sess)
    client.cookies.set("tradepro_csrf", raw_csrf)
    return client, user, raw_csrf

def test_fingerprint_golden_vector_preserved():
    """Confirms representative Milestone 4A golden vector produces identical hash."""
    golden_strat = {
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

    start_dt = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)
    end_dt = datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc)

    h = compute_request_fingerprint(
        strategy_payload=golden_strat,
        reference_dataset_id="synthetic_underlying_nifty_15m",
        subject_dataset_ids=["synthetic_candidate_option_ce_23000_15m", "synthetic_candidate_option_pe_23000_15m"],
        start_timestamp=start_dt,
        end_timestamp=end_dt,
        sampling_step=1,
    )

    assert h == "c8343e76541fad090df872857c2e7e7c45b30e051d64c23de5900d84b63cb999"

def test_strategy_ownership_and_404_isolation(session):
    def override_get_db():
        try:
            yield session
        finally:
            pass
    app.dependency_overrides[get_db] = override_get_db

    try:
        client_a, user_a, csrf_a = create_authenticated_client(session, "user_alpha", "alpha@test.com", "EDITOR")
        client_b, user_b, csrf_b = create_authenticated_client(session, "user_beta", "beta@test.com", "EDITOR")
        client_v, user_v, csrf_v = create_authenticated_client(session, "user_viewer", "viewer@test.com", "VIEWER")

        strategy_payload = {
            "name": "Alpha Secret Strategy",
            "timeframe": "15m",
            "candidate_selection_mode": "FIRST_ELIGIBLE",
            "action": {
                "type": "PAPER_TRADE",
                "risk_config": {"max_position_size": 1.0, "stop_loss_pct": 0.01, "take_profit_pct": 0.02, "validity_window": 1}
            },
            "global_conditions": {
                "type": "CONDITION",
                "id": "c1",
                "lhs": {"indicator": "PRICE"},
                "operator": "GREATER_THAN",
                "rhs": {"type": "NUMBER", "value": 100.0}
            }
        }

        # 1. VIEWER cannot create strategy -> 403
        v_create_res = client_v.post(
            "/strategies",
            json=strategy_payload,
            headers={"X-CSRF-Token": csrf_v, "Origin": "http://localhost:3000"}
        )
        assert v_create_res.status_code == 403

        # 2. User A creates strategy -> 201
        a_create_res = client_a.post(
            "/strategies",
            json=strategy_payload,
            headers={"X-CSRF-Token": csrf_a, "Origin": "http://localhost:3000"}
        )
        assert a_create_res.status_code == 201
        strat_id = a_create_res.json()["id"]

        # 3. User A can view strategy -> 200
        a_get_res = client_a.get(f"/strategies/{strat_id}")
        assert a_get_res.status_code == 200
        assert a_get_res.json()["name"] == "Alpha Secret Strategy"

        # 4. User B cannot see User A's strategy -> Strictly 404
        b_get_res = client_b.get(f"/strategies/{strat_id}")
        assert b_get_res.status_code == 404

        # 5. User B cannot update User A's strategy -> Strictly 404
        b_put_res = client_b.put(
            f"/strategies/{strat_id}",
            json=strategy_payload,
            headers={"X-CSRF-Token": csrf_b, "Origin": "http://localhost:3000"}
        )
        assert b_put_res.status_code == 404

        # 6. Listing strategies returns only user's own strategies
        a_list = client_a.get("/strategies").json()
        b_list = client_b.get("/strategies").json()
        assert len(a_list) == 1
        assert len(b_list) == 0
    finally:
        app.dependency_overrides.clear()

def test_replay_ownership_scoped_deduplication(session):
    def override_get_db():
        try:
            yield session
        finally:
            pass
    app.dependency_overrides[get_db] = override_get_db

    try:
        client_a, user_a, csrf_a = create_authenticated_client(session, "trader_one", "one@test.com", "EDITOR")
        client_b, user_b, csrf_b = create_authenticated_client(session, "trader_two", "two@test.com", "EDITOR")

        replay_req = {
            "strategy_payload": {
                "name": "Shared Strategy Replay",
                "timeframe": "15m",
                "candidate_selection_mode": "FIRST_ELIGIBLE",
                "action": {
                    "type": "PAPER_TRADE",
                    "risk_config": {"max_position_size": 1.0, "stop_loss_pct": 0.01, "take_profit_pct": 0.02, "validity_window": 1}
                },
                "global_conditions": {
                    "type": "CONDITION",
                    "id": "c1",
                    "lhs": {"indicator": "RSI", "params": {"period": 14}},
                    "operator": "GREATER_THAN",
                    "rhs": {"type": "NUMBER", "value": 50.0}
                }
            },
            "reference_dataset_id": "synthetic_underlying_nifty_15m",
            "subject_dataset_ids": ["synthetic_candidate_option_ce_23000_15m"],
            "start_timestamp": "2026-08-28T09:15:00Z",
            "end_timestamp": "2026-08-28T11:30:00Z",
            "sampling_step": 1
        }

        # 1. User A executes replay -> 201 Created (new run)
        res_a1 = client_a.post("/api/v1/replays", json=replay_req, headers={"X-CSRF-Token": csrf_a, "Origin": "http://localhost:3000"})
        assert res_a1.status_code == 201
        data_a1 = res_a1.json()
        assert data_a1["is_reused"] is False
        run_id_a = data_a1["run_id"]

        # 2. User A executes identical request -> 200 OK (deduplicated for User A)
        res_a2 = client_a.post("/api/v1/replays", json=replay_req, headers={"X-CSRF-Token": csrf_a, "Origin": "http://localhost:3000"})
        assert res_a2.status_code == 200
        data_a2 = res_a2.json()
        assert data_a2["is_reused"] is True
        assert data_a2["run_id"] == run_id_a

        # 3. User B executes identical request -> 201 Created (separate run created for User B!)
        res_b1 = client_b.post("/api/v1/replays", json=replay_req, headers={"X-CSRF-Token": csrf_b, "Origin": "http://localhost:3000"})
        assert res_b1.status_code == 201
        data_b1 = res_b1.json()
        assert data_b1["is_reused"] is False
        run_id_b = data_b1["run_id"]
        assert run_id_b != run_id_a

        # 4. User B cannot access User A's run details -> 404
        assert client_b.get(f"/api/v1/replays/{run_id_a}").status_code == 404
        assert client_b.get(f"/api/v1/replays/{run_id_a}/verify").status_code == 404
        assert client_b.get(f"/api/v1/replays/{run_id_a}/export").status_code == 404

        # 5. Comparing unowned runs -> 404
        comp_res = client_b.post(
            "/api/v1/replays/compare",
            json={"baseline_run_id": run_id_a, "comparison_run_id": run_id_b, "include_unchanged": True},
            headers={"Origin": "http://localhost:3000"}
        )
        assert comp_res.status_code == 404
    finally:
        app.dependency_overrides.clear()

def test_last_admin_protection(session):
    def override_get_db():
        try:
            yield session
        finally:
            pass
    app.dependency_overrides[get_db] = override_get_db

    try:
        admin_client, admin_user, admin_csrf = create_authenticated_client(session, "sole_admin", "admin@test.com", "ADMIN")

        # Attempt to demote sole admin to VIEWER -> 409 Conflict
        demote_res = admin_client.patch(
            f"/api/v1/admin/users/{admin_user.id}/role",
            json={"role": "VIEWER"},
            headers={"X-CSRF-Token": admin_csrf, "Origin": "http://localhost:3000"}
        )
        assert demote_res.status_code == 409
        assert "last active administrator" in demote_res.json()["detail"]

        # Attempt to disable sole admin -> 409 Conflict
        disable_res = admin_client.patch(
            f"/api/v1/admin/users/{admin_user.id}/status",
            json={"is_active": False},
            headers={"X-CSRF-Token": admin_csrf, "Origin": "http://localhost:3000"}
        )
        assert disable_res.status_code == 409
        assert "last active administrator" in disable_res.json()["detail"]
    finally:
        app.dependency_overrides.clear()

def test_bounded_legacy_transfer(session):
    def override_get_db():
        try:
            yield session
        finally:
            pass
    app.dependency_overrides[get_db] = override_get_db

    try:
        admin_client, admin_user, admin_csrf = create_authenticated_client(session, "admin_t", "admin_t@test.com", "ADMIN")
        target_client, target_user, _ = create_authenticated_client(session, "target_u", "target@test.com", "EDITOR")

        # Insert legacy principal
        legacy_user = User(
            id=LEGACY_PRINCIPAL_ID,
            username="system_legacy_owner",
            normalized_username="system_legacy_owner",
            email="system_legacy@tradepro.internal",
            normalized_email="system_legacy@tradepro.internal",
            hashed_password="!DISABLED",
            role="VIEWER",
            is_active=False
        )
        session.add(legacy_user)

        # Insert legacy-owned strategy
        legacy_strat = Strategy(
            id="legacy-strat-001",
            owner_id=LEGACY_PRINCIPAL_ID,
            name="Legacy Strategy",
            timeframe="15m",
            payload={
                "name": "Legacy Strategy",
                "timeframe": "15m",
                "candidate_selection_mode": "FIRST_ELIGIBLE",
                "action": {"type": "PAPER_TRADE", "risk_config": {"max_position_size": 1.0, "stop_loss_pct": 0.01, "take_profit_pct": 0.02, "validity_window": 1}}
            }
        )
        session.add(legacy_strat)
        session.commit()

        # Transfer legacy strategy to target user
        trans_res = admin_client.post(
            "/api/v1/admin/transfers/legacy",
            json={
                "target_user_id": target_user.id,
                "resource_type": "STRATEGIES",
                "resource_ids": ["legacy-strat-001"]
            },
            headers={"X-CSRF-Token": admin_csrf, "Origin": "http://localhost:3000"}
        )
        assert trans_res.status_code == 200
        assert trans_res.json()["transferred_count"] == 1
        assert trans_res.json()["rejected_count"] == 0

        # Target user can now access the transferred strategy
        strat_res = target_client.get("/strategies/legacy-strat-001")
        assert strat_res.status_code == 200
        assert strat_res.json()["name"] == "Legacy Strategy"
    finally:
        app.dependency_overrides.clear()
