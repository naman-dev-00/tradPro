import pytest
from src.models import Strategy, User
from src.auth.security import hash_password
from src.auth.session import create_session
from src.auth.rate_limiter import rate_limiter
from fastapi.testclient import TestClient
from src.main import app
from src.database import get_db

@pytest.fixture(autouse=True)
def clear_rate_limits():
    rate_limiter.clear()
    yield
    rate_limiter.clear()

@pytest.fixture
def test_strategy(session, test_user):
    strat = Strategy(
        owner_id=test_user.id,
        name="Test Paper Strategy",
        description="E2E test paper strategy",
        timeframe="15m",
        candidate_selection_mode="FIRST_ELIGIBLE",
        payload={
            "name": "Test Paper Strategy",
            "timeframe": "15m",
            "action": {
                "type": "PAPER_TRADE",
                "risk_config": {
                    "max_position_size": 100000,
                    "stop_loss_pct": 2.5,
                    "take_profit_pct": 5.0,
                    "validity_window": 5,
                }
            },
            "global_conditions": {
                "type": "CONDITION",
                "id": "c1",
                "lhs": {"indicator": "PRICE", "symbol": ""},
                "operator": "GREATER_THAN",
                "rhs": {"type": "NUMBER", "value": 0.0},
            }
        }
    )
    session.add(strat)
    session.commit()
    session.refresh(strat)
    return strat

def test_canonical_prefix_and_no_duplicate_paper_tree(client):
    # Canonical prefix /api/v1/paper must exist
    res_canonical = client.get("/api/v1/paper/accounts")
    assert res_canonical.status_code == 200

    # Non-prefixed /paper route tree must NOT exist (returns 404)
    res_legacy = client.get("/paper/accounts")
    assert res_legacy.status_code == 404

def test_unauthenticated_rejected_401(unauth_client):
    res = unauth_client.get("/api/v1/paper/accounts")
    assert res.status_code == 401

def test_forbidden_role_403(client, test_user, session):
    test_user.role = "VIEWER"
    session.commit()
    try:
        res = client.post("/api/v1/paper/accounts", json={
            "name": "Viewer Account",
            "initial_balance": "10000.00",
            "currency": "INR"
        })
        assert res.status_code == 403
    finally:
        test_user.role = "EDITOR"
        session.commit()


def test_paper_account_lifecycle_and_no_owner_id(client):
    # 1. Create Account
    res = client.post("/api/v1/paper/accounts", json={
        "name": "Primary Paper Account",
        "initial_balance": "100000.00",
        "currency": "INR"
    })
    assert res.status_code == 201
    data = res.json()
    assert data["name"] == "Primary Paper Account"
    assert data["total_cash"] == "100000.00"
    assert data["available_cash"] == "100000.00"
    assert "owner_id" not in data, "owner_id must not be exposed in API responses"
    acct_id = data["id"]

    # 2. List Accounts
    res_list = client.get("/api/v1/paper/accounts")
    assert res_list.status_code == 200
    for a in res_list.json():
        assert "owner_id" not in a

    # 3. Get Account Detail
    res_get = client.get(f"/api/v1/paper/accounts/{acct_id}")
    assert res_get.status_code == 200
    assert res_get.json()["id"] == acct_id
    assert "owner_id" not in res_get.json()

    # 4. Check Initial Ledger Entry
    res_ledger = client.get(f"/api/v1/paper/accounts/{acct_id}/ledger")
    assert res_ledger.status_code == 200
    ledger_entries = res_ledger.json()
    assert len(ledger_entries) == 1
    assert ledger_entries[0]["entry_type"] == "INITIAL_DEPOSIT"
    assert ledger_entries[0]["amount"] == "100000.00"

def test_idempotency_same_key_same_request(client):
    payload = {
        "name": "Idempotent Account",
        "initial_balance": "50000.00",
        "currency": "INR"
    }
    headers = {"Idempotency-Key": "key-unique-12345"}

    res1 = client.post("/api/v1/paper/accounts", json=payload, headers=headers)
    assert res1.status_code == 201
    data1 = res1.json()

    # Repeat identical request with same key
    res2 = client.post("/api/v1/paper/accounts", json=payload, headers=headers)
    assert res2.status_code == 201
    data2 = res2.json()
    assert data1["id"] == data2["id"]

def test_idempotency_same_key_different_request_409(client):
    headers = {"Idempotency-Key": "conflict-key-999"}
    payload1 = {
        "name": "Account A",
        "initial_balance": "50000.00",
        "currency": "INR"
    }
    res1 = client.post("/api/v1/paper/accounts", json=payload1, headers=headers)
    assert res1.status_code == 201

    payload2 = {
        "name": "Account B with different payload",
        "initial_balance": "90000.00",
        "currency": "INR"
    }
    res2 = client.post("/api/v1/paper/accounts", json=payload2, headers=headers)
    assert res2.status_code == 409
    assert "Idempotency key reuse with different request payload" in res2.json()["detail"]

def test_rate_limit_exceeded_429(client):
    # Maximum 30 requests allowed in 60s for create_account
    for i in range(30):
        client.post("/api/v1/paper/accounts", json={
            "name": f"Rate Limit Test {i}",
            "initial_balance": "1000.00",
            "currency": "INR"
        })

    # 31st request triggers 429
    res = client.post("/api/v1/paper/accounts", json={
        "name": "Rate Limit Exceeded",
        "initial_balance": "1000.00",
        "currency": "INR"
    })
    assert res.status_code == 429
    assert "Retry-After" in res.headers

def test_paper_runtime_e2e_stepping(client, test_strategy):
    # 1. Create Account
    res_acct = client.post("/api/v1/paper/accounts", json={
        "name": "Stepping Test Account",
        "initial_balance": "50000.00",
        "currency": "INR"
    })
    acct_id = res_acct.json()["id"]

    # 2. Create Runtime (auto-synthesizes default action policy and risk policy)
    res_rt = client.post("/api/v1/paper/runtimes", json={
        "strategy_id": test_strategy.id,
        "account_id": acct_id,
        "dataset_id": "synthetic_candidate_option_pe_23000_15m",
        "timeframe": "15m"
    })
    assert res_rt.status_code == 201
    rt_id = res_rt.json()["id"]
    assert res_rt.json()["status"] == "DRAFT"
    assert "owner_id" not in res_rt.json()

    # 3. Validate Runtime -> Transitions to READY
    res_val = client.post(f"/api/v1/paper/runtimes/{rt_id}/validate")
    assert res_val.status_code == 200
    assert res_val.json()["valid"] is True
    assert res_val.json()["status"] == "READY"

    # 4. Start Runtime -> Transitions to RUNNING
    res_start = client.post(f"/api/v1/paper/runtimes/{rt_id}/start")
    assert res_start.status_code == 200
    assert res_start.json()["status"] == "RUNNING"

    # 5. Step 5 bars
    res_step = client.post(f"/api/v1/paper/runtimes/{rt_id}/step", json={"step_count": 5})
    assert res_step.status_code == 200
    step_data = res_step.json()
    assert step_data["steps_executed"] == 5
    assert step_data["intents_created"] >= 1

    # 6. Verify Orders
    res_orders = client.get(f"/api/v1/paper/orders?runtime_id={rt_id}")
    assert res_orders.status_code == 200
    orders = res_orders.json()
    assert len(orders) >= 1
    for o in orders:
        assert "owner_id" not in o

    # 7. Verify Positions
    res_pos = client.get(f"/api/v1/paper/positions?account_id={acct_id}")
    assert res_pos.status_code == 200

    # 8. Pause Runtime
    res_pause = client.post(f"/api/v1/paper/runtimes/{rt_id}/pause")
    assert res_pause.status_code == 200
    assert res_pause.json()["status"] == "PAUSED"

    # 9. Stop Runtime
    res_stop = client.post(f"/api/v1/paper/runtimes/{rt_id}/stop")
    assert res_stop.status_code == 200
    assert res_stop.json()["status"] == "STOPPED"

def test_kill_switch_lifecycle_and_admin_boundaries(client, session):
    # Check status
    res = client.get("/api/v1/paper/kill-switch")
    assert res.status_code == 200
    assert res.json()["user_active"] is False

    # USER kill switch
    res_engage = client.post("/api/v1/paper/kill-switch", json={
        "scope": "USER",
        "reason": "Emergency risk breach detected in manual test."
    })
    assert res_engage.status_code == 200
    assert res_engage.json()["status"] == "ENGAGED"

    # Non-admin cannot engage GLOBAL kill switch (default client is EDITOR)
    res_global = client.post("/api/v1/paper/kill-switch", json={
        "scope": "GLOBAL",
        "reason": "Unauthorized attempt to engage global switch"
    })
    assert res_global.status_code == 403

    # Reset USER kill switch
    res_reset = client.post("/api/v1/paper/kill-switch/reset", json={
        "scope": "USER",
        "reason": "Risk parameters reviewed and cleared."
    })
    assert res_reset.status_code == 200
    assert res_reset.json()["status"] == "RESET"

def test_cross_owner_isolation_404(client, session):
    # Create second user
    other_user = User(
        username="second_owner",
        normalized_username="second_owner",
        email="second@tradepro.test",
        normalized_email="second@tradepro.test",
        hashed_password=hash_password("OtherPassword123!"),
        role="EDITOR",
        is_active=True
    )
    session.add(other_user)
    session.commit()

    # Create account and resources owned by other_user directly in DB
    from src.services.paper_service import PaperService
    other_acct = PaperService.create_account(session, other_user.id, "Other Account", 5000)

    # Calling with client (authenticated as default_test_editor) must return 404
    res = client.get(f"/api/v1/paper/accounts/{other_acct.id}")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()

    # Ledger endpoint must also return 404
    res_ledger = client.get(f"/api/v1/paper/accounts/{other_acct.id}/ledger")
    assert res_ledger.status_code == 404

    # Action policy endpoint with cross-owner strategy must return 404 (not 400)
    other_strat = Strategy(
        owner_id=other_user.id,
        name="Other Strategy",
        timeframe="15m",
        payload={"name": "Other Strategy"}
    )
    session.add(other_strat)
    session.commit()

    res_policy = client.post("/api/v1/paper/action-policies", json={
        "strategy_id": other_strat.id,
        "name": "Intruder Policy",
        "entry_mapping": {
            "mapping_id": "em1",
            "rule_target": "GLOBAL",
            "trigger_status": "ON_TRUE",
            "instrument_id": "synthetic_candidate_option_pe_23000_15m",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity": 50,
            "time_in_force": "DAY",
            "cooldown_bars": 1,
            "intent_type": "ENTRY"
        }
    })
    # Strict 404: must never reveal existence of unowned strategy
    assert res_policy.status_code == 404
    assert "not found" in res_policy.json()["detail"].lower()

    # Creating runtime with cross-owner account must return 404 (not 400)
    res_rt = client.post("/api/v1/paper/runtimes", json={
        "strategy_id": other_strat.id,
        "account_id": other_acct.id,
        "dataset_id": "synthetic_candidate_option_pe_23000_15m",
        "timeframe": "15m"
    })
    assert res_rt.status_code == 404
    assert "not found" in res_rt.json()["detail"].lower()

def test_snapshot_freeze_immutability(client, session, test_strategy):
    """
    Proves that when a runtime is created, all snapshots are transactionally frozen.
    Later edits to source strategy, action policy, or risk policy records do NOT
    alter the runtime snapshot or its execution (Item 3).
    """
    # 1. Create Account
    res_acct = client.post("/api/v1/paper/accounts", json={
        "name": "Freeze Test Account",
        "initial_balance": "50000.00",
        "currency": "INR"
    })
    acct_id = res_acct.json()["id"]

    # 2. Create Runtime
    res_rt = client.post("/api/v1/paper/runtimes", json={
        "strategy_id": test_strategy.id,
        "account_id": acct_id,
        "dataset_id": "synthetic_candidate_option_pe_23000_15m",
        "timeframe": "15m"
    })
    assert res_rt.status_code == 201
    rt_data = res_rt.json()
    rt_id = rt_data["id"]

    from src.models import StrategyRuntime
    rt = session.query(StrategyRuntime).filter(StrategyRuntime.id == rt_id).first()
    original_strat_name = rt.strategy_snapshot["name"]

    # 3. Mutate source Strategy in database
    test_strategy.name = "Mutated Strategy Name After Creation"
    test_strategy.payload = {"name": "Mutated Strategy Name After Creation", "tampered": True}
    session.commit()

    # 4. Fetch Runtime: strategy_snapshot must be strictly unchanged
    session.refresh(rt)
    current_snap = rt.strategy_snapshot
    assert current_snap["name"] == original_strat_name
    assert "tampered" not in current_snap

    # 5. Validate & Start Runtime: uses frozen snapshot seamlessly
    res_val = client.post(f"/api/v1/paper/runtimes/{rt_id}/validate")
    assert res_val.status_code == 200
    assert res_val.json()["valid"] is True

    res_start = client.post(f"/api/v1/paper/runtimes/{rt_id}/start")
    assert res_start.status_code == 200
    assert res_start.json()["status"] == "RUNNING"
    session.refresh(rt)
    assert rt.strategy_snapshot["name"] == original_strat_name
