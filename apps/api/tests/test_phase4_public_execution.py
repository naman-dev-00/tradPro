"""Phase 4 regressions using public policy, runtime, configuration and lifecycle APIs."""
import datetime as dt
import json
import pytest
from sqlalchemy.orm import Session

from tests.test_paper_orchestration_execution import paper_engine, session
from src.models import (Strategy, ProviderInstrumentMapping, Order, OrderIntent, RiskDecision,
                        AccountLedgerEntry, RuntimeEvaluation, RuntimeOrchestrationConfig, PaperAccount,
                        ActionDecision, StrategyRuntime, PaperPosition, SubmissionOutbox)
from src.engine.orchestration.worker import StrategyEvaluationWorker

INSTRUMENT = "synthetic_candidate_option_pe_23000_15m"
OPEN = "2026-08-28T09:15:00Z"
CLOSE = "2026-08-28T10:00:00Z"


def post(client, path, payload=None):
    response = client.post(path, json=payload) if payload is not None else client.post(path)
    assert response.status_code in (200, 201), response.text
    return response.json()


def public_runtime(client, session, test_user, *, risk=None, entry=None, exit=None, configure=True):
    strategy = Strategy(owner_id=test_user.id, name="Public execution", timeframe="15m",
                        candidate_selection_mode="FIRST_ELIGIBLE", payload={
                            "name": "Public execution", "timeframe": "15m",
                            "candidate_selection_mode": "FIRST_ELIGIBLE",
                            "global_conditions": {"type": "CONDITION", "id": "price",
                                "lhs": {"indicator": "PRICE"}, "operator": "GREATER_THAN",
                                "rhs": {"type": "NUMBER", "value": 0}}})
    session.add(strategy)
    mapping = ProviderInstrumentMapping(owner_id=test_user.id, tradepro_instrument_id=INSTRUMENT,
        provider_instrument_token="fixture", exchange="NSE", segment="OPTION", symbol="TEST",
        lot_size_units=50, tick_size_units=5, verification_status="VERIFIED", mapping_version=1)
    session.add(mapping)
    session.commit()
    action = {"mapping_id": "a_entry", "rule_target": "GLOBAL", "trigger_status": "ON_TRUE",
              "instrument_id": INSTRUMENT, "side": "BUY", "order_type": "LIMIT",
              "quantity": "50", "limit_price": "250.00", "time_in_force": "GTC",
              "intent_type": "ENTRY", **(entry or {})}
    policy = post(client, "/api/v1/paper/action-policies", {
        "strategy_id": strategy.id, "name": "API action", "entry_mapping": action,
        "exit_mapping": exit, "position_exists_behavior": "SCALE"})
    risk_policy = post(client, "/api/v1/paper/risk-policies", {"name": "API risk", **(risk or {})})
    account = post(client, "/api/v1/paper/accounts", {"name": "API account", "initial_balance": "1000000.00"})
    runtime = post(client, "/api/v1/paper/runtimes", {
        "strategy_id": strategy.id, "action_policy_id": policy["id"], "risk_policy_id": risk_policy["id"],
        "account_id": account["id"], "dataset_id": INSTRUMENT, "timeframe": "15m"})
    post(client, f'/api/v1/paper/runtimes/{runtime["id"]}/validate')
    if not configure:
        return runtime, mapping
    payload = {"runtime_id": runtime["id"], "timeframe": "15m", "replay_open_at": OPEN,
        "replay_close_at": CLOSE, "provider_mapping_id": mapping.id, "execution_policy": "INTERNAL_PAPER",
        "datasets": [{"dataset_id": "synthetic_underlying_nifty_15m", "series_role": "REFERENCE"},
                     {"dataset_id": INSTRUMENT, "series_role": "SUBJECT"}],
        "consent": {"consent_version": "fixture_paper_consent_v1", "acknowledged_source_type": "FIXTURE_REPLAY",
                    "acknowledged_execution_policy": "INTERNAL_PAPER", "acknowledged_timeframe": "15m",
                    "acknowledged_replay_open_at": OPEN, "acknowledged_replay_close_at": CLOSE,
                    "acknowledged_dataset_ids": ["synthetic_underlying_nifty_15m", INSTRUMENT],
                    "confirm_prohibition_of_live_trading": True, "confirm_internal_mock_only": False,
                    "confirm_internal_paper_execution": True}}
    config = post(client, "/api/v1/orchestration/configs", payload)
    return runtime, config


def activate(client, runtime):
    return post(client, f'/api/v1/orchestration/runtimes/{runtime["id"]}/activate', {
        "consent_version": "fixture_paper_consent_v1", "acknowledged_execution_policy": "INTERNAL_PAPER",
        "confirm_internal_mock_only": False, "confirm_internal_paper_execution": True})


def test_public_action_exact_quantity_price_and_identity(client, session, test_user):
    runtime, config = public_runtime(client, session, test_user)
    activate(client, runtime)
    result = StrategyEvaluationWorker().process_runtime_step(session, config["id"], 1)
    assert result is not None
    session.commit()
    with Session(session.get_bind()) as fresh:
        order = fresh.query(Order).one()
        intent = fresh.get(OrderIntent, order.intent_id)
        assert (order.quantity_units, order.limit_price_units, order.side) == (50, 25000, "BUY")
        assert (intent.intent_type, intent.time_in_force, intent.requested_instrument_id) == ("ENTRY", "GTC", INSTRUMENT)
        assert fresh.query(ActionDecision).one().action_mapping_id == "a_entry"


@pytest.mark.parametrize("limit,expected", [("25", "RISK_QTY_EXCEEDED"), ("1000", None)])
def test_public_risk_evidence_pre_intent(client, session, test_user, limit, expected):
    runtime, config = public_runtime(client, session, test_user, risk={"max_quantity_per_order": limit})
    activate(client, runtime)
    result = StrategyEvaluationWorker().process_runtime_step(session, config["id"], 1)
    assert result is not None
    session.commit()
    with Session(session.get_bind()) as fresh:
        evaluation = fresh.query(RuntimeEvaluation).one()
        if expected:
            assert (evaluation.action_outcome, evaluation.risk_outcome, evaluation.no_order_reason) == ("REJECTED", "REJECTED", expected)
            assert expected in json.loads(evaluation.risk_summary_json)["reason_codes"]
            for model in (Order, OrderIntent, RiskDecision, AccountLedgerEntry):
                query = fresh.query(model)
                if model == AccountLedgerEntry:
                    query = query.filter(model.entry_type == "CASH_RESERVATION")
                assert query.count() == 0
        else:
            assert (evaluation.action_outcome, evaluation.risk_outcome) == ("ACCEPTED_INTERNAL", "ACCEPTED")
        assert fresh.query(SubmissionOutbox).count() == 0


@pytest.mark.parametrize("action,status", [("start", "READY"), ("pause", "RUNNING"), ("resume", "PAUSED"), ("stop", "RUNNING")])
def test_legacy_lifecycle_cannot_bypass_orchestration(client, session, test_user, action, status):
    # Existing Phase 4 helper avoids coupling this regression to public configuration creation.
    from tests.test_paper_orchestration_execution import setup_paper_orchestration_runtime
    runtime, config, account = setup_paper_orchestration_runtime(session, test_user, status=status)
    session.commit()
    runtime_id, config_id, generation = runtime.id, config.id, config.fencing_generation
    response = client.post(f"/api/v1/paper/runtimes/{runtime_id}/{action}")
    assert response.status_code == 409, response.text
    session.rollback()
    with Session(session.get_bind()) as fresh:
        assert fresh.get(StrategyRuntime, runtime_id).status == status
        assert fresh.get(RuntimeOrchestrationConfig, config_id).fencing_generation == generation


def test_runtime_without_directly_seeded_configuration(client, session, test_user):
    # Public runtime created without an orchestration configuration
    runtime, mapping = public_runtime(client, session, test_user, configure=False)

    # Verification: no configuration exists and direct activation is rejected with 404
    cfg_resp = client.get(f'/api/v1/orchestration/configs/{runtime["id"]}')
    assert cfg_resp.status_code == 404

    act_fail = client.post(
        f'/api/v1/orchestration/runtimes/{runtime["id"]}/activate',
        json={
            "consent_version": "fixture_paper_consent_v1",
            "acknowledged_execution_policy": "INTERNAL_PAPER",
            "confirm_internal_mock_only": False,
            "confirm_internal_paper_execution": True,
        },
    )
    assert act_fail.status_code == 404

    # Verification: readiness reports configuration blocker
    readiness = client.get(f'/api/v1/orchestration/runtimes/{runtime["id"]}/readiness').json()
    assert readiness["ready"] is False
    assert any("not configured" in r.lower() or "missing" in r.lower() or "configuration" in r.lower() for r in readiness["reasons"])

    # Create orchestration configuration through public API
    config_payload = {
        "runtime_id": runtime["id"],
        "timeframe": "15m",
        "replay_open_at": OPEN,
        "replay_close_at": CLOSE,
        "provider_mapping_id": mapping.id,
        "execution_policy": "INTERNAL_PAPER",
        "datasets": [
            {"dataset_id": "synthetic_underlying_nifty_15m", "series_role": "REFERENCE"},
            {"dataset_id": INSTRUMENT, "series_role": "SUBJECT"},
        ],
        "consent": {
            "consent_version": "fixture_paper_consent_v1",
            "acknowledged_source_type": "FIXTURE_REPLAY",
            "acknowledged_execution_policy": "INTERNAL_PAPER",
            "acknowledged_timeframe": "15m",
            "acknowledged_replay_open_at": OPEN,
            "acknowledged_replay_close_at": CLOSE,
            "acknowledged_dataset_ids": ["synthetic_underlying_nifty_15m", INSTRUMENT],
            "confirm_prohibition_of_live_trading": True,
            "confirm_internal_mock_only": False,
            "confirm_internal_paper_execution": True,
        },
    }
    config = post(client, "/api/v1/orchestration/configs", config_payload)
    assert config["id"] is not None

    # Verification: readiness passes after configuration
    readiness_after = client.get(f'/api/v1/orchestration/runtimes/{runtime["id"]}/readiness').json()
    assert readiness_after["ready"] is True

    # Activation succeeds
    act_resp = activate(client, runtime)
    assert act_resp["status"] == "RUNNING"

    # Worker step processes successfully and produces accepted order
    result = StrategyEvaluationWorker().process_runtime_step(session, config["id"], 1)
    assert result is not None
    session.commit()
    with Session(session.get_bind()) as fresh:
        order = fresh.query(Order).filter(Order.runtime_id == runtime["id"]).one()
        assert order.status == "ACCEPTED"
        assert order.quantity_units == 50


def test_public_action_exit_mapping_behavior(client, session, test_user):
    import uuid
    exit_action = {
        "mapping_id": "b_exit",
        "rule_target": "GLOBAL",
        "trigger_status": "ON_TRUE",
        "instrument_id": INSTRUMENT,
        "side": "SELL",
        "order_type": "LIMIT",
        "quantity": "50",
        "limit_price": "260.00",
        "time_in_force": "GTC",
        "intent_type": "EXIT",
    }
    # No existing position: exit action is ignored
    runtime, config = public_runtime(client, session, test_user, exit=exit_action, entry={"trigger_status": "ON_FALSE"})
    activate(client, runtime)
    result = StrategyEvaluationWorker().process_runtime_step(session, config["id"], 1)
    assert result is not None
    session.commit()

    decision = session.query(ActionDecision).filter(ActionDecision.action_mapping_id == "b_exit").one()
    assert decision.decision == "IGNORED"
    assert decision.reason_code == "ACTION_IGNORED_NO_POSITION_TO_REDUCE"
    assert session.query(Order).count() == 0

    # Seed position: exit action is now eligible
    pos = PaperPosition(
        id=str(uuid.uuid4()),
        account_id=runtime["account_id"],
        owner_id=test_user.id,
        instrument_id=INSTRUMENT,
        net_quantity_units=50,
        average_entry_price_units=25000,
        last_mark_price_units=26000,
        cost_basis_units=1250000,
        net_realized_pnl_units=0,
        unrealized_pnl_units=50000,
    )
    session.add(pos)
    session.commit()

    # Step 2: Now that position exists, exit action executes with exact price/quantity
    result2 = StrategyEvaluationWorker().process_runtime_step(session, config["id"], 1)
    assert result2 is not None
    session.commit()

    sell_order = session.query(Order).filter(Order.side == "SELL").one()
    sell_intent = session.get(OrderIntent, sell_order.intent_id)
    assert (sell_order.quantity_units, sell_order.limit_price_units, sell_order.side) == (50, 26000, "SELL")
    assert (sell_intent.intent_type, sell_intent.reduce_only) == ("EXIT", True)
    dec = session.query(ActionDecision).filter(ActionDecision.action_mapping_id == "b_exit", ActionDecision.decision == "EXECUTED").one()
    assert dec.reason_code == "RULE_CONDITIONS_MET"


def test_orchestration_pause_resume_stop_preserves_orders_and_reservations(client, session, test_user):
    from src.services.orchestration_service import OrchestrationService
    runtime, config = public_runtime(client, session, test_user)
    activate(client, runtime)
    # Step 1 generates BUY order with cash reservation
    result = StrategyEvaluationWorker().process_runtime_step(session, config["id"], 1)
    assert result is not None
    session.commit()

    order = session.query(Order).filter(Order.runtime_id == runtime["id"]).one()
    assert order.status == "ACCEPTED"
    account = session.get(PaperAccount, runtime["account_id"])
    reserved_before = account.reserved_cash_units
    assert reserved_before > 0

    # Pause orchestration
    OrchestrationService.pause_orchestration(session, runtime["id"], test_user.id, test_user.id)
    session.commit()
    assert session.get(StrategyRuntime, runtime["id"]).status == "PAUSED"
    # Order and reservation are strictly preserved
    order = session.query(Order).filter(Order.runtime_id == runtime["id"]).one()
    assert order.status == "ACCEPTED"
    assert session.get(PaperAccount, runtime["account_id"]).reserved_cash_units == reserved_before

    # Resume orchestration
    OrchestrationService.resume_orchestration(session, runtime["id"], test_user.id, test_user.id)
    session.commit()
    assert session.get(StrategyRuntime, runtime["id"]).status == "RUNNING"
    assert session.get(PaperAccount, runtime["account_id"]).reserved_cash_units == reserved_before

    # Stop orchestration
    OrchestrationService.stop_orchestration(session, runtime["id"], test_user.id, test_user.id)
    session.commit()
    assert session.get(StrategyRuntime, runtime["id"]).status == "STOPPED"
    # Order and reservation remain preserved
    order = session.query(Order).filter(Order.runtime_id == runtime["id"]).one()
    assert order.status == "ACCEPTED"
    assert session.get(PaperAccount, runtime["account_id"]).reserved_cash_units == reserved_before


def test_binding_verified_mapping_to_runtime_instrument(client, session, test_user):
    # 1. Create a public runtime bound to INSTRUMENT ("synthetic_candidate_option_pe_23000_15m")
    runtime, valid_mapping = public_runtime(client, session, test_user, configure=False)

    # 2. Create another verified mapping for a different instrument
    other_mapping = ProviderInstrumentMapping(
        owner_id=test_user.id,
        tradepro_instrument_id="synthetic_candidate_option_ce_23000_15m",
        provider_instrument_token="tok_other_ce",
        exchange="NSE",
        segment="OPTION",
        symbol="NIFTY26SEP23000CE",
        lot_size_units=50,
        tick_size_units=5,
        verification_status="VERIFIED",
        mapping_version=1,
    )
    session.add(other_mapping)
    session.commit()

    # 3. Attempting to create an orchestration configuration with the mismatched mapping is rejected (400 Bad Request)
    mismatched_payload = {
        "runtime_id": runtime["id"],
        "timeframe": "15m",
        "replay_open_at": OPEN,
        "replay_close_at": CLOSE,
        "provider_mapping_id": other_mapping.id,
        "execution_policy": "INTERNAL_PAPER",
        "datasets": [
            {"dataset_id": "synthetic_underlying_nifty_15m", "series_role": "REFERENCE"},
            {"dataset_id": INSTRUMENT, "series_role": "SUBJECT"},
        ],
        "consent": {
            "consent_version": "fixture_paper_consent_v1",
            "acknowledged_source_type": "FIXTURE_REPLAY",
            "acknowledged_execution_policy": "INTERNAL_PAPER",
            "acknowledged_timeframe": "15m",
            "acknowledged_replay_open_at": OPEN,
            "acknowledged_replay_close_at": CLOSE,
            "acknowledged_dataset_ids": ["synthetic_underlying_nifty_15m", INSTRUMENT],
            "confirm_prohibition_of_live_trading": True,
            "confirm_internal_mock_only": False,
            "confirm_internal_paper_execution": True,
        },
    }
    cfg_resp = client.post("/api/v1/orchestration/configs", json=mismatched_payload)
    assert cfg_resp.status_code == 400
    assert "does not match runtime orderable instrument" in cfg_resp.text

    # 4. If an existing config exists with mismatched mapping, readiness rejects and activation fails
    valid_payload = dict(mismatched_payload)
    valid_payload["provider_mapping_id"] = valid_mapping.id
    valid_cfg = post(client, "/api/v1/orchestration/configs", valid_payload)
    assert valid_cfg["id"] is not None

    # Mutate mapping instrument to simulate legacy/corrupted configuration mismatch
    valid_mapping_record = session.get(ProviderInstrumentMapping, valid_mapping.id)
    valid_mapping_record.tradepro_instrument_id = "MISMATCHED_INSTRUMENT_IDENTITY"
    session.commit()

    readiness = client.get(f'/api/v1/orchestration/runtimes/{runtime["id"]}/readiness').json()
    assert readiness["ready"] is False
    assert readiness["gates"]["mapping_gate"] is False
    assert any("does not match runtime orderable instrument" in r for r in readiness["reasons"])

    act_resp = client.post(
        f'/api/v1/orchestration/runtimes/{runtime["id"]}/activate',
        json={
            "consent_version": "fixture_paper_consent_v1",
            "acknowledged_execution_policy": "INTERNAL_PAPER",
            "confirm_internal_mock_only": False,
            "confirm_internal_paper_execution": True,
        },
    )
    assert act_resp.status_code == 400
    assert "does not match runtime orderable instrument" in act_resp.text or "Activation prerequisites failed" in act_resp.text


def test_explicit_consent_in_public_api(client, session, test_user):
    runtime, mapping = public_runtime(client, session, test_user, configure=False)

    base_consent = {
        "consent_version": "fixture_paper_consent_v1",
        "acknowledged_source_type": "FIXTURE_REPLAY",
        "acknowledged_execution_policy": "INTERNAL_PAPER",
        "acknowledged_timeframe": "15m",
        "acknowledged_replay_open_at": OPEN,
        "acknowledged_replay_close_at": CLOSE,
        "acknowledged_dataset_ids": ["synthetic_underlying_nifty_15m", INSTRUMENT],
    }
    base_config = {
        "runtime_id": runtime["id"],
        "timeframe": "15m",
        "replay_open_at": OPEN,
        "replay_close_at": CLOSE,
        "provider_mapping_id": mapping.id,
        "execution_policy": "INTERNAL_PAPER",
        "datasets": [
            {"dataset_id": "synthetic_underlying_nifty_15m", "series_role": "REFERENCE"},
            {"dataset_id": INSTRUMENT, "series_role": "SUBJECT"},
        ],
    }

    # 1. Config creation: Omission of confirm_prohibition_of_live_trading cannot count as consent (422)
    c1 = dict(base_consent)
    c1["confirm_internal_paper_execution"] = True
    res = client.post("/api/v1/orchestration/configs", json={**base_config, "consent": c1})
    assert res.status_code == 422, res.text

    # 2. Config creation: confirm_prohibition_of_live_trading=False is rejected (400)
    c2 = dict(base_consent)
    c2["confirm_prohibition_of_live_trading"] = False
    c2["confirm_internal_paper_execution"] = True
    res = client.post("/api/v1/orchestration/configs", json={**base_config, "consent": c2})
    assert res.status_code == 400
    assert "prohibition of live trading" in res.text

    # 3. Config creation: INTERNAL_PAPER omitting confirm_internal_paper_execution is rejected (400)
    c3 = dict(base_consent)
    c3["confirm_prohibition_of_live_trading"] = True
    res = client.post("/api/v1/orchestration/configs", json={**base_config, "consent": c3})
    assert res.status_code == 400
    assert "confirm internal paper execution" in res.text

    # 4. Config creation: INTERNAL_PAPER with confirm_internal_paper_execution=False is rejected (400)
    c4 = dict(base_consent)
    c4["confirm_prohibition_of_live_trading"] = True
    c4["confirm_internal_paper_execution"] = False
    res = client.post("/api/v1/orchestration/configs", json={**base_config, "consent": c4})
    assert res.status_code == 400
    assert "confirm internal paper execution" in res.text

    # 5. Config creation: valid explicit consent succeeds
    c5 = dict(base_consent)
    c5["confirm_prohibition_of_live_trading"] = True
    c5["confirm_internal_mock_only"] = False
    c5["confirm_internal_paper_execution"] = True
    valid_cfg = post(client, "/api/v1/orchestration/configs", {**base_config, "consent": c5})
    assert valid_cfg["id"] is not None

    # 6. Activation: omitting confirm_internal_paper_execution for INTERNAL_PAPER is rejected (400)
    act_omit = client.post(
        f'/api/v1/orchestration/runtimes/{runtime["id"]}/activate',
        json={
            "consent_version": "fixture_paper_consent_v1",
            "acknowledged_execution_policy": "INTERNAL_PAPER",
        },
    )
    assert act_omit.status_code == 400
    assert "internal paper execution" in act_omit.text

    # 7. Activation: confirm_internal_paper_execution=False is rejected (400)
    act_false = client.post(
        f'/api/v1/orchestration/runtimes/{runtime["id"]}/activate',
        json={
            "consent_version": "fixture_paper_consent_v1",
            "acknowledged_execution_policy": "INTERNAL_PAPER",
            "confirm_internal_paper_execution": False,
        },
    )
    assert act_false.status_code == 400
    assert "internal paper execution" in act_false.text

    # 8. Activation: explicit valid confirmation succeeds (200)
    act_ok = client.post(
        f'/api/v1/orchestration/runtimes/{runtime["id"]}/activate',
        json={
            "consent_version": "fixture_paper_consent_v1",
            "acknowledged_execution_policy": "INTERNAL_PAPER",
            "confirm_internal_mock_only": False,
            "confirm_internal_paper_execution": True,
        },
    )
    assert act_ok.status_code == 200
    assert act_ok.json()["status"] == "RUNNING"


def test_mixed_action_evidence_selection_and_agreement(client, session, test_user):
    # Action 1: entry mapping with trigger_status="ON_FALSE"
    # Action 2: exit mapping with trigger_status="ON_TRUE", but violates max_quantity_per_order limit
    entry_skipped = {
        "mapping_id": "a_entry_skipped",
        "rule_target": "GLOBAL",
        "trigger_status": "ON_FALSE",
        "instrument_id": INSTRUMENT,
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": "50",
        "limit_price": "250.00",
        "time_in_force": "GTC",
        "intent_type": "ENTRY",
    }
    exit_risk_rejected = {
        "mapping_id": "b_exit_rejected",
        "rule_target": "GLOBAL",
        "trigger_status": "ON_TRUE",
        "instrument_id": INSTRUMENT,
        "side": "SELL",
        "order_type": "LIMIT",
        "quantity": "5000",
        "limit_price": "260.00",
        "time_in_force": "GTC",
        "intent_type": "EXIT",
    }
    runtime, config = public_runtime(
        client,
        session,
        test_user,
        entry=entry_skipped,
        exit=exit_risk_rejected,
        risk={"max_quantity_per_order": "25"},
    )
    import uuid
    # Seed position so exit mapping is eligible to evaluate risk rather than being ignored for no position
    pos = PaperPosition(
        id=str(uuid.uuid4()),
        account_id=runtime["account_id"],
        owner_id=test_user.id,
        instrument_id=INSTRUMENT,
        net_quantity_units=5000,
        average_entry_price_units=25000,
        last_mark_price_units=26000,
        cost_basis_units=125000000,
        net_realized_pnl_units=0,
        unrealized_pnl_units=50000,
    )
    session.add(pos)
    session.commit()

    activate(client, runtime)

    result = StrategyEvaluationWorker().process_runtime_step(session, config["id"], 1)
    assert result is not None
    session.commit()

    with Session(session.get_bind()) as fresh:
        evaluation = fresh.query(RuntimeEvaluation).filter(RuntimeEvaluation.runtime_id == runtime["id"]).one()
        # Evaluation outcome must be REJECTED, and no_order_reason must agree with the risk rejection
        assert evaluation.action_outcome == "REJECTED"
        assert evaluation.risk_outcome == "REJECTED"
        assert evaluation.no_order_reason == "RISK_QTY_EXCEEDED"

        # Verify persisted risk_summary_json agrees with the aggregate evaluation and individual actions
        risk_summary = json.loads(evaluation.risk_summary_json)
        assert risk_summary["outcome"] == "REJECTED"
        assert "RISK_QTY_EXCEEDED" in risk_summary["reason_codes"]
        assert "RULE_CONDITION_NOT_MET" in risk_summary["reason_codes"]

        actions_by_id = {act["mapping_id"]: act for act in risk_summary["actions"]}
        assert actions_by_id["a_entry_skipped"]["accepted"] is False
        assert actions_by_id["a_entry_skipped"]["risk_outcome"] == "NOT_RUN"
        assert actions_by_id["a_entry_skipped"]["reason_code"] == "RULE_CONDITION_NOT_MET"

        assert actions_by_id["b_exit_rejected"]["accepted"] is False
        assert actions_by_id["b_exit_rejected"]["risk_outcome"] == "REJECTED"
        assert actions_by_id["b_exit_rejected"]["reason_code"] == "RISK_QTY_EXCEEDED"

        # Action decisions must match individual mapping outcomes
        decisions = {d.action_mapping_id: d for d in fresh.query(ActionDecision).filter(ActionDecision.evaluation_id == evaluation.id).all()}
        assert decisions["a_entry_skipped"].decision == "IGNORED"
        assert decisions["a_entry_skipped"].reason_code == "RULE_CONDITION_NOT_MET"
        assert decisions["b_exit_rejected"].decision == "IGNORED"
        assert decisions["b_exit_rejected"].reason_code == "RISK_QTY_EXCEEDED"

        # No order or intent created
        assert fresh.query(Order).filter(Order.runtime_id == runtime["id"]).count() == 0
        assert fresh.query(OrderIntent).filter(OrderIntent.runtime_id == runtime["id"]).count() == 0
