import datetime
import os
import pytest
from src.models import (
    KillSwitch,
    ProviderInstrumentMapping,
    StrategyRuntime,
    WorkerHeartbeat,
    User,
)
from src.services.sandbox_gate_service import SandboxGateService


def test_startup_guard_fails_in_production_when_network_enabled(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")

    with pytest.raises(RuntimeError) as exc_info:
        SandboxGateService.enforce_startup_environment_guard()

    assert "FATAL: Sandbox network transmission is enabled" in str(exc_info.value)


def test_startup_guard_passes_in_production_when_network_disabled(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "false")
    # Should not raise
    SandboxGateService.enforce_startup_environment_guard()


def test_startup_guard_passes_in_local_with_network_enabled(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")
    # Should not raise
    SandboxGateService.enforce_startup_environment_guard()


def test_runtime_readiness_all_gates_pass(session, test_user, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", test_user.id)
    monkeypatch.setenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "valid_sandbox_token")

    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Create runtime in BROKER_SANDBOX
    runtime = StrategyRuntime(
        owner_id=test_user.id,
        strategy_id="mock_strat_id",
        action_policy_id="mock_action_id",
        risk_policy_id="mock_risk_id",
        account_id="mock_acct_id",
        dataset_id="mock_dataset",
        timeframe="15m",
        trading_mode="BROKER_SANDBOX",
        instrument_spec_snapshot={"instrument_id": "NIFTY_FUT"},
        status="RUNNING",
    )
    session.add(runtime)

    # 2. Create verified mapping
    mapping = ProviderInstrumentMapping(
        owner_id=test_user.id,
        tradepro_instrument_id="NIFTY_FUT",
        provider_instrument_token="NSE_FO|12345",
        exchange="NSE_FO",
        segment="FO",
        symbol="NIFTY24SEPFUT",
        verification_status="VERIFIED",
        expiry_date=now + datetime.timedelta(days=10),
    )
    session.add(mapping)

    # 3. Create active worker heartbeat
    hb = WorkerHeartbeat(
        worker_id="test-worker-1",
        owner_id=test_user.id,
        status="HEALTHY",
        last_heartbeat_at=now,
    )
    session.add(hb)
    session.commit()

    readiness = SandboxGateService.evaluate_runtime_readiness(session, runtime, now=now)
    assert readiness["environment_allowed"] is True
    assert readiness["network_enabled"] is True
    assert readiness["credential_present"] is True
    assert readiness["owner_matches"] is True
    assert readiness["provider_matches"] is True
    assert readiness["mapping_verified"] is True
    assert readiness["mapping_unexpired"] is True
    assert readiness["global_kill_switch_clear"] is True
    assert readiness["user_kill_switch_clear"] is True
    assert readiness["worker_available"] is True
    assert readiness["ready_for_submission"] is True
    assert readiness["cancel_available"] is True
    assert len(readiness["reasons"]) == 0


def test_runtime_readiness_kill_switch_blocks_submission_permits_cancel(session, test_user, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", test_user.id)
    monkeypatch.setenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "valid_token")

    now = datetime.datetime.now(datetime.timezone.utc)

    runtime = StrategyRuntime(
        owner_id=test_user.id,
        strategy_id="mock_strat_id",
        action_policy_id="mock_action_id",
        risk_policy_id="mock_risk_id",
        account_id="mock_acct_id",
        dataset_id="mock_dataset",
        timeframe="15m",
        trading_mode="BROKER_SANDBOX",
        instrument_spec_snapshot={"instrument_id": "NIFTY_FUT"},
        status="RUNNING",
    )
    session.add(runtime)

    mapping = ProviderInstrumentMapping(
        owner_id=test_user.id,
        tradepro_instrument_id="NIFTY_FUT",
        provider_instrument_token="NSE_FO|12345",
        exchange="NSE_FO",
        segment="FO",
        symbol="NIFTY24SEPFUT",
        verification_status="VERIFIED",
    )
    session.add(mapping)

    hb = WorkerHeartbeat(
        worker_id="test-worker-1",
        owner_id=test_user.id,
        status="HEALTHY",
        last_heartbeat_at=now,
    )
    session.add(hb)

    # Activate global kill switch
    ks = KillSwitch(
        target_key="GLOBAL",
        scope="GLOBAL",
        is_active=True,
        engaged_by=test_user.id,
        reason="Market emergency",
    )
    session.add(ks)
    session.commit()

    readiness = SandboxGateService.evaluate_runtime_readiness(session, runtime, now=now)
    assert readiness["global_kill_switch_clear"] is False
    assert readiness["ready_for_submission"] is False
    # Critical: cancel_available must remain True!
    assert readiness["cancel_available"] is True
    assert any("kill switch" in r.lower() for r in readiness["reasons"])


def test_transmission_gate_blocks_on_unverified_mapping(session, test_user, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", test_user.id)
    monkeypatch.setenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "token")

    now = datetime.datetime.now(datetime.timezone.utc)

    mapping = ProviderInstrumentMapping(
        owner_id=test_user.id,
        tradepro_instrument_id="NIFTY_FUT",
        provider_instrument_token="NSE_FO|12345",
        exchange="NSE_FO",
        segment="FO",
        symbol="NIFTY24SEPFUT",
        verification_status="UNVERIFIED",  # Unverified!
    )
    session.add(mapping)
    session.commit()

    allowed, reasons = SandboxGateService.check_outbox_transmission_gates(
        db=session,
        owner_id=test_user.id,
        action_type="PLACE",
        target_instrument_id="NIFTY_FUT",
        now=now,
    )

    assert allowed is False
    assert any("not VERIFIED" in r for r in reasons)
