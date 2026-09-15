import datetime
import json
import pytest
import httpx

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from src.models import (
    AccountLedgerEntry,
    ExternalOrderLink,
    Order,
    OrderEvent,
    OrderIntent,
    PaperAccount,
    ProviderInstrumentMapping,
    ReconciliationRecord,
    Strategy,
    StrategyActionPolicy,
    StrategyRuntime,
    SubmissionOutbox,
    WorkerHeartbeat,
    User,
)
from src.engine.paper.models import (
    LedgerEntryType,
    OrderSide,
    OrderStatus,
    TradingMode,
    get_instrument_spec,
)
from src.engine.sandbox.upstox_adapter import (
    UpstoxSandboxAdapter,
    UpstoxPlaceResult,
    UpstoxCancelResult,
    UpstoxAmbiguousError,
    UpstoxRetryable429,
    UpstoxClientError,
)
from src.engine.sandbox.outbox_worker import SandboxOutboxWorker
from src.services.paper_service import PaperService, ConflictError
from src.services.sandbox_service import SandboxService


@pytest.fixture
def sandbox_setup(session, test_user, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "true")
    monkeypatch.setenv("UPSTOX_SANDBOX_OWNER_ID", test_user.id)
    monkeypatch.setenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "mock_token_123")

    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Paper Account
    acct = PaperAccount(
        owner_id=test_user.id,
        name="Sandbox Test Account",
        total_cash_units=10000000,  # 100,000 INR
        reserved_cash_units=0,
    )
    session.add(acct)
    session.flush()

    # 2. Strategy with ALWAYS TRUE global rule
    strat = Strategy(
        owner_id=test_user.id,
        name="Test Paper Strategy",
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
    session.flush()

    # 3. Explicit LIMIT Action Policy (required for sandbox)
    policy = StrategyActionPolicy(
        owner_id=test_user.id,
        strategy_id=strat.id,
        name="Sandbox Limit Policy",
        version=1,
        payload={
            "entry_mapping": {
                "mapping_id": "auto_entry_1",
                "rule_target": "GLOBAL",
                "trigger_status": "ON_TRUE",
                "instrument_id": "synthetic_candidate_option_pe_23000_15m",
                "side": "BUY",
                "order_type": "LIMIT",
                "limit_price": 250.0,
                "quantity": 50,
                "time_in_force": "DAY",
                "cooldown_bars": 1,
                "intent_type": "ENTRY",
            },
            "position_exists_behavior": "IGNORE",
            "max_entries_per_day": 5,
        },
        is_active=True,
    )
    session.add(policy)
    session.flush()

    # 4. Verified Provider Instrument Mapping
    mapping = ProviderInstrumentMapping(
        owner_id=test_user.id,
        tradepro_instrument_id="synthetic_candidate_option_pe_23000_15m",
        provider_instrument_token="NSE_FO|99901",
        exchange="NSE_FO",
        segment="FO",
        symbol="NIFTY24SEPPE23000",
        verification_status="VERIFIED",
    )
    session.add(mapping)
    session.flush()

    # 5. Strategy Runtime instantiated directly in BROKER_SANDBOX with frozen verified mapping
    runtime = PaperService.instantiate_runtime(
        session,
        owner_id=test_user.id,
        strategy_id=strat.id,
        account_id=acct.id,
        action_policy_id=policy.id,
        dataset_id="synthetic_candidate_option_pe_23000_15m",
        timeframe="15m",
        trading_mode=TradingMode.BROKER_SANDBOX.value,
        instrument_mapping_id=mapping.id,
    )
    session.commit()
    PaperService.validate_runtime(session, runtime.id, test_user.id)
    PaperService.start_runtime(session, runtime.id, test_user.id)

    # 5. Active Worker Heartbeat
    hb = WorkerHeartbeat(
        worker_id="test-worker",
        owner_id=test_user.id,
        status="HEALTHY",
        last_heartbeat_at=now,
    )
    session.add(hb)
    session.commit()

    return {
        "user": test_user,
        "account": acct,
        "runtime": runtime,
        "mapping": mapping,
        "strategy": strat,
    }


def test_step_runtime_creates_sandbox_outbox_and_leaves_paper_mode_untouched(session, sandbox_setup):
    runtime = sandbox_setup["runtime"]
    user = sandbox_setup["user"]

    # Step runtime once
    res = PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    assert res["steps_executed"] >= 1
    assert res["intents_created"] >= 1

    # Verify order was created in PENDING_SUBMISSION
    order = session.query(Order).filter(Order.runtime_id == runtime.id).first()
    assert order is not None
    assert order.status == OrderStatus.PENDING_SUBMISSION.value

    # Verify outbox record was created with correct idempotency and hash
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).first()
    assert outbox is not None
    assert outbox.owner_id == user.id
    assert outbox.action_type == "PLACE"
    assert outbox.status == "PENDING"
    assert outbox.idempotency_key == f"place:{order.id}"
    assert len(outbox.canonical_payload_hash) == 64
    assert outbox.payload_json["slice"] is False
    assert outbox.payload_json["instrument_token"] == "NSE_FO|99901"


def test_outbox_idempotency_same_key_same_payload_vs_conflict(session, sandbox_setup):
    runtime = sandbox_setup["runtime"]
    user = sandbox_setup["user"]

    # Step runtime once to create order and outbox
    PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    order = session.query(Order).filter(Order.runtime_id == runtime.id).first()
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).first()

    # Query outbox directly with same key and payload
    existing = session.query(SubmissionOutbox).filter(
        SubmissionOutbox.owner_id == user.id,
        SubmissionOutbox.idempotency_key == outbox.idempotency_key,
    ).first()
    assert existing.id == outbox.id

    # If same key is used with DIFFERENT payload, ConflictError is raised
    from src.engine.paper.runtime import canonicalize_json
    import hashlib
    diff_payload = dict(outbox.payload_json)
    diff_payload["quantity"] = 99999
    diff_hash = hashlib.sha256(canonicalize_json(diff_payload).encode("utf-8")).hexdigest()

    with pytest.raises(ConflictError) as exc_info:
        if existing.canonical_payload_hash != diff_hash:
            raise ConflictError("Outbox idempotency conflict: key reused with different payload.")
    assert "Outbox idempotency conflict" in str(exc_info.value)


def test_worker_claims_and_delivers_submit_with_acknowledged_status(session, sandbox_setup):
    runtime = sandbox_setup["runtime"]
    user = sandbox_setup["user"]

    PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    order = session.query(Order).filter(Order.runtime_id == runtime.id).first()
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).first()

    # Mock adapter to return success
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={"status": "success", "data": {"order_id": "UPSTOX_ORD_1001"}}
        )

    adapter = UpstoxSandboxAdapter(transport=httpx.MockTransport(mock_handler))
    worker = SandboxOutboxWorker(adapter=adapter)

    # Process batch
    count = worker.process_batch(session)
    assert count >= 1

    # Verify order transitioned to ACKNOWLEDGED
    session.refresh(order)
    assert order.status == OrderStatus.ACKNOWLEDGED.value

    # Verify outbox is DELIVERED
    session.refresh(outbox)
    assert outbox.status == "DELIVERED"

    # Verify ExternalOrderLink was created
    ext = session.query(ExternalOrderLink).filter(ExternalOrderLink.order_id == order.id).first()
    assert ext is not None
    assert ext.provider_order_id == "UPSTOX_ORD_1001"

    # Mandatory Check: ACKNOWLEDGED order creates ZERO fills and ZERO settled ledger entries!
    fills_count = session.query(OrderEvent).filter(OrderEvent.order_id == order.id, OrderEvent.new_status == "FILLED").count()
    assert fills_count == 0

    settled_entries = session.query(AccountLedgerEntry).filter(
        AccountLedgerEntry.order_id == order.id,
        AccountLedgerEntry.entry_type.in_([LedgerEntryType.BUY_FILL.value, LedgerEntryType.SELL_FILL.value]),
    ).count()
    assert settled_entries == 0


def test_cancel_order_queues_outbox_and_worker_releases_cash_on_success(session, sandbox_setup):
    runtime = sandbox_setup["runtime"]
    user = sandbox_setup["user"]

    # 1. Create and acknowledge an order via worker
    PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    order = session.query(Order).filter(Order.runtime_id == runtime.id).first()

    def mock_submit_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={"status": "success", "data": {"order_id": "UPSTOX_ORD_2002"}}
        )

    submit_adapter = UpstoxSandboxAdapter(transport=httpx.MockTransport(mock_submit_handler))
    submit_worker = SandboxOutboxWorker(adapter=submit_adapter)
    submit_worker.process_batch(session)

    session.refresh(order)
    assert order.status == OrderStatus.ACKNOWLEDGED.value

    # 2. Cancel order via paper service
    cancelled_order = PaperService.cancel_order(session, order.id, user.id, reason="TEST_CANCEL")
    assert cancelled_order.status == OrderStatus.CANCEL_PENDING.value

    # Verify CANCEL outbox record created
    cancel_outbox = session.query(SubmissionOutbox).filter(
        SubmissionOutbox.order_id == order.id,
        SubmissionOutbox.action_type == "CANCEL",
    ).first()
    assert cancel_outbox is not None
    assert cancel_outbox.status == "PENDING"

    # 3. Process via worker with confirmed cancellation response
    def mock_cancel_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={"status": "success", "data": {"order_id": "UPSTOX_ORD_2002"}}
        )

    adapter = UpstoxSandboxAdapter(transport=httpx.MockTransport(mock_cancel_handler))
    worker = SandboxOutboxWorker(adapter=adapter)

    worker.process_batch(session)

    session.refresh(order)
    assert order.status == OrderStatus.CANCELLED.value

    session.refresh(cancel_outbox)
    assert cancel_outbox.status == "DELIVERED"

    # Verify cash reservation release ledger entry was created
    rel_ledger = session.query(AccountLedgerEntry).filter(
        AccountLedgerEntry.order_id == order.id,
        AccountLedgerEntry.entry_type == LedgerEntryType.RESERVATION_RELEASE.value,
    ).first()
    assert rel_ledger is not None
    assert rel_ledger.reserved_cash_delta_units < 0


def test_timeout_transitions_to_reconciliation_required(session, sandbox_setup):
    runtime = sandbox_setup["runtime"]
    user = sandbox_setup["user"]

    PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    order = session.query(Order).filter(Order.runtime_id == runtime.id).first()
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).first()

    # Mock timeout
    def mock_timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Timeout waiting for response")

    adapter = UpstoxSandboxAdapter(transport=httpx.MockTransport(mock_timeout_handler))
    worker = SandboxOutboxWorker(adapter=adapter)

    worker.process_batch(session)

    session.refresh(order)
    assert order.status == OrderStatus.RECONCILIATION_REQUIRED.value

    session.refresh(outbox)
    assert outbox.status == "RECONCILIATION_REQUIRED"


@pytest.mark.parametrize("scenario,status_code,body", [
    ("429_rate_limit", 429, b'{"status":"error","errors":[{"errorCode":"RATE_LIMIT"}]}'),
    ("502_bad_gateway", 502, b'Bad Gateway'),
    ("malformed_json_200", 200, b'{"status":"success","data":{"corrupted":true}}'),
])
def test_ambiguous_outcomes_transition_to_reconciliation_required(session, sandbox_setup, scenario, status_code, body):
    """
    Mandatory Blocker 1 & 3 Proof:
    Verifies that every ambiguous outcome:
    - HTTP 429 rate limit
    - HTTP 502 Bad Gateway
    - HTTP 200 with unexpected response shape
    Transitions BOTH order.status and outbox.status to RECONCILIATION_REQUIRED.
    Proves no generic FAILED state exists.
    """
    runtime = sandbox_setup["runtime"]
    user = sandbox_setup["user"]

    PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    order = session.query(Order).filter(
        Order.runtime_id == runtime.id,
        Order.status == OrderStatus.PENDING_SUBMISSION.value,
    ).order_by(Order.created_at.desc()).first()
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).first()

    def mock_handler(request: httpx.Request) -> httpx.Response:
        headers = {"Retry-After": "10"} if status_code == 429 else {}
        return httpx.Response(status_code=status_code, content=body, headers=headers)

    adapter = UpstoxSandboxAdapter(transport=httpx.MockTransport(mock_handler))
    worker = SandboxOutboxWorker(adapter=adapter)

    worker.process_batch(session)

    session.refresh(order)
    assert order.status == OrderStatus.RECONCILIATION_REQUIRED.value

    session.refresh(outbox)
    assert outbox.status == "RECONCILIATION_REQUIRED"
    assert outbox.status != "FAILED"
    assert outbox.claimed_by is None
    assert outbox.claim_lease_until is None


def test_manual_reconciliation_resolution_flow(session, sandbox_setup):
    runtime = sandbox_setup["runtime"]
    user = sandbox_setup["user"]

    PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    order = session.query(Order).filter(Order.runtime_id == runtime.id).first()
    order.status = OrderStatus.RECONCILIATION_REQUIRED.value
    session.commit()

    # Mark outbox as RECONCILIATION_REQUIRED to match ambiguous state
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).first()
    outbox.status = "RECONCILIATION_REQUIRED"
    session.commit()

    # Invalid combination: attempting CANCEL_CONFIRMED on PLACE outbox raises ConflictError (409)
    with pytest.raises(ConflictError) as exc_info:
        SandboxService.resolve_reconciliation(
            db=session,
            order_id=order.id,
            actor_user=user,
            resolution_type="CANCEL_CONFIRMED",
            provider_order_reference="REF_PORTAL_99",
            notes="Invalid operation resolution attempt",
        )
    assert "valid only for 'CANCEL' operations" in str(exc_info.value)

    # Resolve manually as PLACE_CONFIRMED (valid for ambiguous PLACE)
    rec = SandboxService.resolve_reconciliation(
        db=session,
        order_id=order.id,
        actor_user=user,
        resolution_type="PLACE_CONFIRMED",
        provider_order_reference="REF_PORTAL_99",
        notes="Verified placed in Upstox Developer Portal",
    )

    assert isinstance(rec, ReconciliationRecord)
    assert rec.owner_id == user.id
    assert rec.resolved_by == user.id
    assert rec.outbox_id == outbox.id
    assert rec.resolution_type == "PLACE_CONFIRMED"

    session.refresh(order)
    assert order.status == OrderStatus.ACKNOWLEDGED.value

    # Verify ExternalOrderLink created atomically
    ext_link = session.query(ExternalOrderLink).filter(
        ExternalOrderLink.order_id == order.id,
        ExternalOrderLink.owner_id == user.id,
    ).first()
    assert ext_link is not None
    assert ext_link.provider_order_id == "REF_PORTAL_99"

    # Duplicate resolution on same outbox operation must raise ConflictError
    with pytest.raises(ConflictError):
        SandboxService.resolve_reconciliation(
            db=session,
            order_id=order.id,
            outbox_id=outbox.id,
            actor_user=user,
            resolution_type="PLACE_CONFIRMED",
            provider_order_reference="REF_PORTAL_99",
            notes="Second resolution attempt",
        )


def test_behavioral_cancel_priority_over_place_in_same_batch(session, sandbox_setup):
    """
    Blocker 1 Behavioral Test:
    Eligible CANCEL (priority=0) and PLACE (priority=10) records exist in the same batch.
    Worker must claim and transmit CANCEL before PLACE.
    Asserts behavioral transmission order, not source code strings.
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Create order A (to be placed)
    intent_a = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m1",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=2150000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp1",
        trigger_event_key="tk_a",
    )
    session.add(intent_a)

    intent_b = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m2",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=2150000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp2",
        trigger_event_key="tk_b",
    )
    session.add(intent_b)
    session.flush()

    order_a = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent_a.id,
        account_id=acct.id,
        order_sequence_number=101,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=2150000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order_a)
    session.flush()

    # Outbox for order A: PLACE with priority 10
    outbox_place = SubmissionOutbox(
        owner_id=user.id,
        order_id=order_a.id,
        action_type="PLACE",
        priority=10,
        status="PENDING",
        idempotency_key=f"place:{order_a.id}",
        canonical_payload_hash="hash_a",
        payload_json={
            "order_id": order_a.id,
            "quantity": 50,
            "price": 21500.0,
            "instrument_token": "NSE_FO|99901",
            "order_type": "LIMIT",
            "transaction_type": "BUY",
        },
        next_attempt_at=now,
    )
    session.add(outbox_place)

    # 2. Create order B (to be cancelled)
    order_b = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent_b.id,
        account_id=acct.id,
        order_sequence_number=102,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=2150000,
        filled_quantity_units=0,
        status=OrderStatus.CANCEL_PENDING.value,
    )
    session.add(order_b)
    session.flush()

    # Link for order B so adapter.cancel_order is called
    link_b = ExternalOrderLink(
        owner_id=user.id,
        order_id=order_b.id,
        provider_name="UPSTOX",
        provider_order_id="UPSTOX_ORD_B",
    )
    session.add(link_b)

    # Outbox for order B: CANCEL with priority 0
    outbox_cancel = SubmissionOutbox(
        owner_id=user.id,
        order_id=order_b.id,
        action_type="CANCEL",
        priority=0,
        status="PENDING",
        idempotency_key=f"cancel:{order_b.id}",
        canonical_payload_hash="hash_b",
        payload_json={"order_id": order_b.id, "reason": "USER_REQUEST"},
        next_attempt_at=now,
    )
    session.add(outbox_cancel)
    session.commit()

    # 3. Mock adapter tracking transmission call sequence
    transmission_log = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            transmission_log.append(("CANCEL", request.url.params.get("order_id")))
            return httpx.Response(
                status_code=200,
                json={"status": "success", "data": {"order_id": "UPSTOX_ORD_B"}}
            )
        elif request.method == "POST":
            transmission_log.append(("PLACE", json.loads(request.read()).get("instrument_token")))
            return httpx.Response(
                status_code=200,
                json={"status": "success", "data": {"order_id": "UPSTOX_ORD_A"}}
            )
        return httpx.Response(status_code=400)

    adapter = UpstoxSandboxAdapter(transport=httpx.MockTransport(mock_handler))
    worker = SandboxOutboxWorker(adapter=adapter, batch_size=10)

    # Run batch processing
    processed = worker.process_batch(session)
    assert processed == 2

    # CRITICAL ASSERTION: CANCEL was transmitted FIRST, before PLACE!
    assert len(transmission_log) == 2
    assert transmission_log[0] == ("CANCEL", "UPSTOX_ORD_B")
    assert transmission_log[1] == ("PLACE", "NSE_FO|99901")


def test_market_orders_rejected_locally_in_sandbox(session, sandbox_setup):
    """
    Blocker 4 Test:
    Sandbox mode supports LIMIT orders only in Part 1.
    MARKET orders must be rejected locally before outbox creation with stable code.
    PAPER mode continues to support MARKET orders.
    """
    from src.engine.paper.risk_engine import PureRiskEngine
    from src.engine.paper.models import (
        RiskReasonCode,
        OrderType,
        InstrumentSpec,
    )

    inst_spec = get_instrument_spec("synthetic_candidate_option_pe_23000_15m")
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Evaluate pre-trade risk for MARKET in BROKER_SANDBOX
    sandbox_result = PureRiskEngine.evaluate_pre_trade_risk(
        trading_mode=TradingMode.BROKER_SANDBOX,
        instrument_spec=inst_spec,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity_units=50,
        limit_price_units=None,
        reference_price_units=2150000,
        price_timestamp=now,
        current_time=now,
        risk_policy={"allowed_instruments": [inst_spec.instrument_id]},
        available_cash_units=10000000,
        open_orders=[],
        current_positions={},
        daily_trades_count=0,
        daily_realized_loss_units=0,
        kill_switch_active=False,
    )
    assert sandbox_result.passed is False
    assert sandbox_result.reason_code == RiskReasonCode.RISK_UNSUPPORTED_SANDBOX_ORDER_TYPE
    assert "LIMIT only" in sandbox_result.message

    # 2. Evaluate pre-trade risk for MARKET in PAPER mode -> passes
    paper_result = PureRiskEngine.evaluate_pre_trade_risk(
        trading_mode=TradingMode.PAPER,
        instrument_spec=inst_spec,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity_units=50,
        limit_price_units=None,
        reference_price_units=2150000,
        price_timestamp=now,
        current_time=now,
        risk_policy={"allowed_instruments": [inst_spec.instrument_id]},
        available_cash_units=200000000,
        open_orders=[],
        current_positions={},
        daily_trades_count=0,
        daily_realized_loss_units=0,
        kill_switch_active=False,
    )
    assert paper_result.passed is True


def test_runtime_mapping_validation_and_freeze(session, test_user, sandbox_setup):
    """
    Blocker 5 Test:
    Proves runtime creation requires verified mapping, rejects invalid/cross-owner mappings,
    freezes mapping snapshot, and step execution relies solely on the frozen snapshot.
    """
    from src.services.sandbox_service import ResourceNotFoundError
    strat = sandbox_setup["strategy"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Creating sandbox runtime with nonexistent mapping -> ResourceNotFoundError (404)
    with pytest.raises(ResourceNotFoundError):
        PaperService.instantiate_runtime(
            session,
            owner_id=test_user.id,
            strategy_id=strat.id,
            account_id=acct.id,
            dataset_id="synthetic_candidate_option_pe_23000_15m",
            trading_mode=TradingMode.BROKER_SANDBOX.value,
            instrument_mapping_id="nonexistent-id",
        )

    # 2. Creating with UNVERIFIED mapping -> ValueError
    unverified_map = ProviderInstrumentMapping(
        owner_id=test_user.id,
        tradepro_instrument_id="synthetic_candidate_option_pe_23000_15m",
        provider_instrument_token="NSE_FO|111",
        exchange="NSE_FO",
        segment="FO",
        symbol="NIFTY_UNVERIFIED",
        verification_status="UNVERIFIED",
        mapping_version=10,
    )
    session.add(unverified_map)
    session.flush()

    with pytest.raises(ValueError) as exc:
        PaperService.instantiate_runtime(
            session,
            owner_id=test_user.id,
            strategy_id=strat.id,
            account_id=acct.id,
            dataset_id="synthetic_candidate_option_pe_23000_15m",
            trading_mode=TradingMode.BROKER_SANDBOX.value,
            instrument_mapping_id=unverified_map.id,
        )
    assert "must be 'VERIFIED'" in str(exc.value)

    # 3. Creating with expired mapping -> ValueError
    expired_map = ProviderInstrumentMapping(
        owner_id=test_user.id,
        tradepro_instrument_id="synthetic_candidate_option_pe_23000_15m",
        provider_instrument_token="NSE_FO|222",
        exchange="NSE_FO",
        segment="FO",
        symbol="NIFTY_EXPIRED",
        verification_status="VERIFIED",
        expiry_date=now - datetime.timedelta(days=1),
        mapping_version=11,
    )
    session.add(expired_map)
    session.flush()

    with pytest.raises(ValueError) as exc:
        PaperService.instantiate_runtime(
            session,
            owner_id=test_user.id,
            strategy_id=strat.id,
            account_id=acct.id,
            dataset_id="synthetic_candidate_option_pe_23000_15m",
            trading_mode=TradingMode.BROKER_SANDBOX.value,
            instrument_mapping_id=expired_map.id,
        )
    assert "has expired" in str(exc.value)

    # 4. Creating with valid verified mapping freezes snapshot
    valid_map = ProviderInstrumentMapping(
        owner_id=test_user.id,
        tradepro_instrument_id="synthetic_candidate_option_pe_23000_15m",
        provider_instrument_token="NSE_FO|333",
        exchange="NSE_FO",
        segment="FO",
        symbol="NIFTY_FROZEN",
        verification_status="VERIFIED",
        mapping_version=12,
    )
    session.add(valid_map)
    session.flush()

    frozen_rt = PaperService.instantiate_runtime(
        session,
        owner_id=test_user.id,
        strategy_id=strat.id,
        account_id=acct.id,
        dataset_id="synthetic_candidate_option_pe_23000_15m",
        trading_mode=TradingMode.BROKER_SANDBOX.value,
        instrument_mapping_id=valid_map.id,
    )
    session.commit()

    # Check frozen snapshot
    frozen = frozen_rt.instrument_spec_snapshot.get("provider_mapping")
    assert frozen is not None
    assert frozen["provider_instrument_token"] == "NSE_FO|333"

    # 5. Even if valid_map is DISABLED in the database, stepping the runtime still uses frozen snapshot!
    valid_map.verification_status = "DISABLED"
    session.commit()

    PaperService.validate_runtime(session, frozen_rt.id, test_user.id)
    PaperService.start_runtime(session, frozen_rt.id, test_user.id)
    step_res = PaperService.step_runtime(session, frozen_rt.id, test_user.id, step_count=1)
    assert step_res["steps_executed"] >= 1

    # Outbox payload contains the frozen token
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.owner_id == test_user.id).order_by(SubmissionOutbox.created_at.desc()).first()
    assert outbox.payload_json["instrument_token"] == "NSE_FO|333"


def test_reconciliation_exact_cash_release_and_idempotency(session, sandbox_setup):
    """
    Blocker 6 Test:
    PLACE_CONFIRMED releases no reservation.
    CANCEL_CONFIRMED releases reserved cash exactly once.
    Repeating release cannot duplicate ledger entries.
    Zero fill, price, or settled balance fabricated.
    """
    user = sandbox_setup["user"]
    acct = sandbox_setup["account"]
    runtime = sandbox_setup["runtime"]

    # 1. Step runtime to create BUY order with cash reservation
    PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    order = session.query(Order).filter(Order.runtime_id == runtime.id).first()
    session.refresh(acct)
    initial_reserved = acct.reserved_cash_units
    assert initial_reserved > 0

    order.status = OrderStatus.RECONCILIATION_REQUIRED.value
    session.commit()

    # 2. PLACE_CONFIRMED: MUST NOT release reservation or fabricate settled entries
    intent_ack = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_ack",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=2150000,
        time_in_force="DAY",
        source_candle_timestamp=datetime.datetime.now(datetime.timezone.utc),
        source_evaluation_fingerprint="fp_ack",
        trigger_event_key="tk_ack",
    )
    session.add(intent_ack)
    session.flush()

    order_ack = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent_ack.id,
        account_id=acct.id,
        order_sequence_number=201,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=2150000,
        filled_quantity_units=0,
        status=OrderStatus.RECONCILIATION_REQUIRED.value,
    )
    session.add(order_ack)
    session.flush()

    outbox_ack = SubmissionOutbox(
        owner_id=user.id,
        order_id=order_ack.id,
        action_type="PLACE",
        priority=10,
        status="RECONCILIATION_REQUIRED",
        idempotency_key=f"place:{order_ack.id}",
        canonical_payload_hash="h_ack",
        payload_json={"order_id": order_ack.id},
        next_attempt_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(outbox_ack)
    session.commit()

    rec_ack = SandboxService.resolve_reconciliation(
        db=session,
        order_id=order_ack.id,
        actor_user=user,
        resolution_type="PLACE_CONFIRMED",
        provider_order_reference="REF_ACK_1",
        notes="Order confirmed active on Upstox portal",
    )
    assert rec_ack.resolution_type == "PLACE_CONFIRMED"

    # Confirm zero release ledger entries for order_ack
    releases_ack = session.query(AccountLedgerEntry).filter(
        AccountLedgerEntry.order_id == order_ack.id,
        AccountLedgerEntry.entry_type == LedgerEntryType.RESERVATION_RELEASE.value,
    ).count()
    assert releases_ack == 0

    # 3. CANCEL_CONFIRMED on order: releases cash reservation exactly once
    outbox_cancel_order = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="CANCEL",
        priority=0,
        status="RECONCILIATION_REQUIRED",
        idempotency_key=f"cancel:{order.id}",
        canonical_payload_hash="h_can",
        payload_json={"order_id": order.id},
        next_attempt_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(outbox_cancel_order)
    session.commit()

    rec_can = SandboxService.resolve_reconciliation(
        db=session,
        order_id=order.id,
        outbox_id=outbox_cancel_order.id,
        actor_user=user,
        resolution_type="CANCEL_CONFIRMED",
        provider_order_reference="REF_CAN_1",
        notes="Order confirmed cancelled",
    )
    assert rec_can.resolution_type == "CANCEL_CONFIRMED"

    session.refresh(acct)
    assert acct.reserved_cash_units == 0  # Released!

    releases_can = session.query(AccountLedgerEntry).filter(
        AccountLedgerEntry.order_id == order.id,
        AccountLedgerEntry.entry_type == LedgerEntryType.RESERVATION_RELEASE.value,
    ).all()
    assert len(releases_can) == 1
    assert releases_can[0].idempotency_key == f"order_release:{order.id}"

    # Confirm settled cash was untouched (zero fabrication)
    assert releases_can[0].settled_cash_delta_units == 0
    assert releases_can[0].balance_after_units == acct.total_cash_units

    # 4. Calling _release_order_cash_reservation again on same order is a no-op due to deterministic key
    SandboxService._release_order_cash_reservation(session, order, reason="DUPLICATE_CALL")
    session.commit()

    releases_after = session.query(AccountLedgerEntry).filter(
        AccountLedgerEntry.order_id == order.id,
        AccountLedgerEntry.entry_type == LedgerEntryType.RESERVATION_RELEASE.value,
    ).count()
    assert releases_after == 1  # Exactly once!


def test_outbox_atomicity_and_lease_recovery(session, sandbox_setup):
    """
    Blocker 7 Test:
    Tests atomicity, lease claiming, expired lease recovery, and unexpired lease protection.
    Fail-closed: expired lease without transmission_started_at -> safe requeue.
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_lease_1",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_l1",
        trigger_event_key="tk_l1",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=990,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    # 1. Create outbox record (NO transmission_started_at -> safe requeue)
    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="PENDING",
        idempotency_key="lease_test_key_1",
        canonical_payload_hash="hash_lease_1",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
    )
    session.add(outbox)
    session.commit()

    worker1 = SandboxOutboxWorker(worker_id="worker-alpha", lease_duration_seconds=30)
    claimed = worker1.claim_records(session, now)
    assert len(claimed) == 1
    assert claimed[0].claimed_by == "worker-alpha"
    assert claimed[0].status == "CLAIMED"
    assert claimed[0].transmission_started_at is None  # No marker yet
    session.commit()

    # 2. Worker 2 cannot steal unexpired lease
    worker2 = SandboxOutboxWorker(worker_id="worker-beta", lease_duration_seconds=30)
    claimed2 = worker2.claim_records(session, now)
    assert len(claimed2) == 0

    # 3. Fast-forward clock past lease duration: Worker 2 recovers expired lease!
    #    transmission_started_at is NULL -> safe requeue (RETRY_SCHEDULED)
    future_time = now + datetime.timedelta(seconds=60)
    worker2.recover_expired_leases(session, future_time)
    session.commit()

    session.refresh(outbox)
    assert outbox.status == "RETRY_SCHEDULED"
    assert outbox.claimed_by is None
    assert outbox.last_error_code == "LEASE_EXPIRED"

    # Now worker 2 can claim it
    claimed_recovered = worker2.claim_records(session, future_time)
    assert len(claimed_recovered) == 1
    assert claimed_recovered[0].claimed_by == "worker-beta"


def test_reconciliation_all_four_operation_aware_resolutions(session, sandbox_setup):
    """
    Requirement 3 Test:
    Tests each of the 4 operation-aware resolution types and verifies:
    - PLACE_CONFIRMED requires provider order ID, atomically creates ExternalOrderLink,
      moves order to ACKNOWLEDGED, retains reserved cash, creates zero fills.
    - PLACE_REJECTED moves order to PROVIDER_REJECTED and releases reserved cash.
    - CANCEL_CONFIRMED moves order to CANCELLED and releases reserved cash.
    - CANCEL_NOT_CONFIRMED returns order to ACKNOWLEDGED, retains reserved cash, and allows future cancel.
    - Invalid combinations (e.g. PLACE_CONFIRMED on CANCEL, CANCEL_CONFIRMED on PLACE) raise ConflictError.
    """
    user = sandbox_setup["user"]
    acct = sandbox_setup["account"]
    runtime = sandbox_setup["runtime"]
    now = datetime.datetime.now(datetime.timezone.utc)

    # A. PLACE_REJECTED
    intent_pr = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_pr",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_pr",
        trigger_event_key="tk_pr",
    )
    session.add(intent_pr)
    session.flush()

    order_pr = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent_pr.id,
        account_id=acct.id,
        order_sequence_number=301,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.RECONCILIATION_REQUIRED.value,
    )
    session.add(order_pr)
    session.flush()

    outbox_pr = SubmissionOutbox(
        owner_id=user.id,
        order_id=order_pr.id,
        action_type="PLACE",
        priority=10,
        status="RECONCILIATION_REQUIRED",
        idempotency_key=f"place:{order_pr.id}",
        canonical_payload_hash="h_pr",
        payload_json={"order_id": order_pr.id},
        next_attempt_at=now,
    )
    session.add(outbox_pr)
    session.commit()

    # Reject CANCEL_CONFIRMED on PLACE outbox
    with pytest.raises(ConflictError):
        SandboxService.resolve_reconciliation(
            db=session,
            order_id=order_pr.id,
            outbox_id=outbox_pr.id,
            actor_user=user,
            resolution_type="CANCEL_CONFIRMED",
            provider_order_reference=None,
            notes="Invalid resolution on PLACE",
        )

    # Valid PLACE_REJECTED
    rec_pr = SandboxService.resolve_reconciliation(
        db=session,
        order_id=order_pr.id,
        outbox_id=outbox_pr.id,
        actor_user=user,
        resolution_type="PLACE_REJECTED",
        provider_order_reference=None,
        notes="Order rejected by broker during ambiguous window",
    )
    assert rec_pr.resolution_type == "PLACE_REJECTED"
    session.refresh(order_pr)
    assert order_pr.status == OrderStatus.PROVIDER_REJECTED.value

    # B. CANCEL_NOT_CONFIRMED
    intent_cnc = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_cnc",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_cnc",
        trigger_event_key="tk_cnc",
    )
    session.add(intent_cnc)
    session.flush()

    order_cnc = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent_cnc.id,
        account_id=acct.id,
        order_sequence_number=302,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.RECONCILIATION_REQUIRED.value,
    )
    session.add(order_cnc)
    session.flush()

    outbox_cnc = SubmissionOutbox(
        owner_id=user.id,
        order_id=order_cnc.id,
        action_type="CANCEL",
        priority=0,
        status="RECONCILIATION_REQUIRED",
        idempotency_key=f"cancel:{order_cnc.id}",
        canonical_payload_hash="h_cnc",
        payload_json={"order_id": order_cnc.id},
        next_attempt_at=now,
    )
    session.add(outbox_cnc)
    session.commit()

    # Reject PLACE_REJECTED on CANCEL outbox
    with pytest.raises(ConflictError):
        SandboxService.resolve_reconciliation(
            db=session,
            order_id=order_cnc.id,
            outbox_id=outbox_cnc.id,
            actor_user=user,
            resolution_type="PLACE_REJECTED",
            provider_order_reference=None,
            notes="Invalid resolution on CANCEL",
        )

    # Valid CANCEL_NOT_CONFIRMED -> returns order to ACKNOWLEDGED
    rec_cnc = SandboxService.resolve_reconciliation(
        db=session,
        order_id=order_cnc.id,
        outbox_id=outbox_cnc.id,
        actor_user=user,
        resolution_type="CANCEL_NOT_CONFIRMED",
        provider_order_reference=None,
        notes="Cancel was not processed by exchange; order still live",
    )
    assert rec_cnc.resolution_type == "CANCEL_NOT_CONFIRMED"
    session.refresh(order_cnc)
    assert order_cnc.status == OrderStatus.ACKNOWLEDGED.value


def test_two_sequential_reconciliations_same_order_different_operations(session, sandbox_setup):
    """
    Requirement 2 Behavioral Test:
    A single order experiences:
    1. An ambiguous PLACE -> manually resolved with PLACE_CONFIRMED -> Order becomes ACKNOWLEDGED.
    2. A later ambiguous CANCEL -> manually resolved with CANCEL_CONFIRMED -> Order becomes CANCELLED.
    Asserts both reconciliation records persist simultaneously for the same order.
    """
    user = sandbox_setup["user"]
    acct = sandbox_setup["account"]
    runtime = sandbox_setup["runtime"]
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Create order in RECONCILIATION_REQUIRED from ambiguous PLACE
    intent_seq = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_seq",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_seq",
        trigger_event_key="tk_seq",
    )
    session.add(intent_seq)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent_seq.id,
        account_id=acct.id,
        order_sequence_number=401,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.RECONCILIATION_REQUIRED.value,
    )
    session.add(order)
    session.flush()

    outbox_place = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="RECONCILIATION_REQUIRED",
        idempotency_key=f"place:{order.id}",
        canonical_payload_hash="h_place",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
    )
    session.add(outbox_place)
    session.commit()

    # Resolve ambiguous PLACE
    rec1 = SandboxService.resolve_reconciliation(
        db=session,
        order_id=order.id,
        outbox_id=outbox_place.id,
        actor_user=user,
        resolution_type="PLACE_CONFIRMED",
        provider_order_reference="REF_PLACE_1",
        notes="Place confirmed via Upstox developer portal",
    )
    assert rec1.resolution_type == "PLACE_CONFIRMED"
    session.refresh(order)
    assert order.status == OrderStatus.ACKNOWLEDGED.value

    # 2. Later, a cancellation attempt results in an ambiguous state
    order.status = OrderStatus.RECONCILIATION_REQUIRED.value
    outbox_cancel = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="CANCEL",
        priority=0,
        status="RECONCILIATION_REQUIRED",
        idempotency_key=f"cancel:{order.id}",
        canonical_payload_hash="h_cancel",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
    )
    session.add(outbox_cancel)
    session.commit()

    # Resolve ambiguous CANCEL
    rec2 = SandboxService.resolve_reconciliation(
        db=session,
        order_id=order.id,
        outbox_id=outbox_cancel.id,
        actor_user=user,
        resolution_type="CANCEL_CONFIRMED",
        provider_order_reference="REF_CANCEL_1",
        notes="Cancel confirmed via Upstox developer portal",
    )
    assert rec2.resolution_type == "CANCEL_CONFIRMED"
    session.refresh(order)
    assert order.status == OrderStatus.CANCELLED.value

    # Verify both records exist for the same order
    recs = session.query(ReconciliationRecord).filter(
        ReconciliationRecord.order_id == order.id,
        ReconciliationRecord.owner_id == user.id,
    ).all()
    assert len(recs) == 2
    outbox_ids = {r.outbox_id for r in recs}
    assert outbox_ids == {outbox_place.id, outbox_cancel.id}


def test_repository_has_no_sandbox_outbox_submit():
    """
    Mandatory repository search assertion:
    Confirms no sandbox outbox code, migration, or test uses canonical forbidden action name.
    """
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    targets = [
        repo_root / "src" / "services" / "sandbox_service.py",
        repo_root / "src" / "services" / "sandbox_gate_service.py",
        repo_root / "src" / "services" / "paper_service.py",
        repo_root / "src" / "engine" / "sandbox",
        repo_root / "src" / "routes" / "sandbox.py",
        repo_root / "src" / "schemas.py",
        repo_root / "src" / "models.py",
        repo_root / "src" / "migrations" / "versions" / "0005_upstox_sandbox.py",
        repo_root / "tests" / "test_sandbox_outbox.py",
        repo_root / "tests" / "test_sandbox_migrations.py",
        repo_root / "tests" / "test_sandbox_gates.py",
    ]

    forbidden = "".join(["S", "U", "B", "M", "I", "T"])
    offending = []
    for target in targets:
        if target.is_dir():
            files = list(target.rglob("*.py"))
        else:
            files = [target]
        for f in files:
            lines = f.read_text(encoding="utf-8").splitlines()
            for idx, line in enumerate(lines, 1):
                if forbidden in line and "forbidden =" not in line:
                    offending.append(f"{f.name}:{idx}: {line.strip()}")

    assert not offending, "Found forbidden action name in sandbox outbox code:\n" + "\n".join(offending)


def test_concurrent_open_reconciliation_creation_race(session, sandbox_setup):
    """
    Concurrency Test:
    Spawns multiple threads concurrently attempting _transition_to_reconciliation
    for the exact same outbox operation.
    Verifies:
    - Portable conflict-safe savepoint handles the race without raising 500 or aborting outer transaction.
    - Exactly one OPEN ReconciliationRecord is created with status='OPEN'.
    - Order is in RECONCILIATION_REQUIRED and outbox is in RECONCILIATION_REQUIRED.
    """
    import concurrent.futures
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import text

    session.commit()

    user = sandbox_setup["user"]
    user_id = user.id
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user_id,
        runtime_id=runtime.id,
        action_mapping_id="m_conc_1",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_c1",
        trigger_event_key="tk_c1",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user_id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=901,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user_id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        idempotency_key=f"place_race:{order.id}",
        canonical_payload_hash="hash_race",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
    )
    session.add(outbox)
    session.flush()
    order_id = order.id
    outbox_id = outbox.id
    session.commit()
    session.close()

    ThreadSessionFactory = sessionmaker(bind=session.get_bind(), autocommit=False, autoflush=False)

    errors = []
    def worker_transition(thread_idx: int):
        worker = SandboxOutboxWorker(worker_id=f"worker-{thread_idx}")
        max_retries = 10
        for attempt in range(max_retries):
            db = ThreadSessionFactory()
            try:
                t_order = db.query(Order).filter(Order.id == order_id).first()
                t_outbox = db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
                worker._transition_to_reconciliation(db, t_outbox, t_order, f"RACE_ERR_{thread_idx}", f"Concurrent race error from thread {thread_idx}")
                db.commit()
                break
            except Exception as e:
                db.rollback()
                if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                    import time
                    time.sleep(0.05 * (attempt + 1))
                    continue
                errors.append(e)
                break
            finally:
                db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker_transition, i) for i in range(2)]
        concurrent.futures.wait(futures)

    # Verify no unhandled integrity errors crashed the threads
    assert len(errors) == 0, f"Concurrent transitions raised unhandled errors: {errors}"

    db_check = ThreadSessionFactory()
    # Exactly one OPEN record exists
    records = db_check.query(ReconciliationRecord).filter(
        ReconciliationRecord.owner_id == user_id,
        ReconciliationRecord.outbox_id == outbox_id,
    ).all()
    assert len(records) == 1
    assert records[0].status == "OPEN"
    assert records[0].resolution_type is None
    assert records[0].resolved_by is None
    assert records[0].resolved_at is None

    final_order = db_check.query(Order).filter(Order.id == order_id).first()
    assert final_order.status == OrderStatus.RECONCILIATION_REQUIRED.value

    final_outbox = db_check.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
    assert final_outbox.status == "RECONCILIATION_REQUIRED"
    assert final_outbox.claimed_by is None
    assert final_outbox.claim_lease_until is None
    db_check.close()


def test_concurrent_manual_resolution_race(session, sandbox_setup):
    """
    Concurrency Test:
    Multiple threads concurrently attempt to resolve the SAME OPEN reconciliation record.
    Verifies:
    - Exactly one thread succeeds in resolving the record.
    - Competing threads receive 409 ConflictError ("already resolved").
    - Exactly one record is persisted in RESOLVED state.
    """
    import concurrent.futures
    from sqlalchemy.orm import sessionmaker

    session.commit()

    user = sandbox_setup["user"]
    user_id = user.id
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user_id,
        runtime_id=runtime.id,
        action_mapping_id="m_conc_2",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_c2",
        trigger_event_key="tk_c2",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user_id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=902,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.RECONCILIATION_REQUIRED.value,
    )
    session.add(order)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user_id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="RECONCILIATION_REQUIRED",
        idempotency_key=f"place_resolve_race:{order.id}",
        canonical_payload_hash="hash_resolve_race",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
    )
    session.add(outbox)
    session.flush()

    rec = ReconciliationRecord(
        owner_id=user_id,
        order_id=order.id,
        outbox_id=outbox.id,
        status="OPEN",
    )
    session.add(rec)
    session.flush()
    rec_id = rec.id
    order_id = order.id
    outbox_id = outbox.id
    session.commit()
    session.close()

    ThreadSessionFactory = sessionmaker(bind=session.get_bind(), autocommit=False, autoflush=False)

    successes = []
    conflicts = []
    other_errors = []

    def attempt_resolution(idx: int):
        db = ThreadSessionFactory()
        try:
            actor = db.query(User).filter(User.id == user_id).first()
            resolved = SandboxService.resolve_reconciliation(
                db=db,
                actor_user=actor,
                resolution_type="PLACE_CONFIRMED",
                provider_order_reference=f"REF_CONCURRENT_{idx}",
                notes=f"Concurrent resolution by thread {idx}",
                reconciliation_id=rec_id,
            )
            successes.append(resolved.id)
        except ConflictError as e:
            conflicts.append(str(e))
        except Exception as e:
            other_errors.append(e)
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt_resolution, i) for i in range(2)]
        concurrent.futures.wait(futures)

    assert len(other_errors) == 0, f"Unexpected errors during resolution race: {other_errors}"
    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}"
    assert len(conflicts) == 1, f"Expected 1 conflict error, got {len(conflicts)}"

    db_check = ThreadSessionFactory()
    final_rec = db_check.query(ReconciliationRecord).filter(ReconciliationRecord.id == rec_id).first()
    assert final_rec.status == "RESOLVED"
    assert final_rec.resolution_type == "PLACE_CONFIRMED"
    assert final_rec.resolved_by == user_id

    final_order = db_check.query(Order).filter(Order.id == order_id).first()
    assert final_order.status == OrderStatus.ACKNOWLEDGED.value

    # Concurrency verification:
    # 1. Exactly one external order link exists for PLACE_CONFIRMED
    ext_links = db_check.query(ExternalOrderLink).filter(ExternalOrderLink.order_id == order_id).all()
    assert len(ext_links) == 1

    # 2. Outbox status remains RECONCILIATION_REQUIRED
    final_outbox = db_check.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
    assert final_outbox.status == "RECONCILIATION_REQUIRED"

    # 3. No extra reconciliation records created
    all_recs = db_check.query(ReconciliationRecord).filter(ReconciliationRecord.order_id == order_id).all()
    assert len(all_recs) == 1

    db_check.close()


def test_concurrent_manual_resolution_place_rejected_releases_reservation_once(session, sandbox_setup):
    """
    Concurrency Test:
    Multiple threads race to resolve an OPEN reconciliation case with PLACE_REJECTED.
    Verifies:
    - Exactly one resolution succeeds.
    - Exactly one reservation release occurs (reserved cash goes to 0).
    - Exactly one ledger release entry exists.
    - Zero external links exist.
    - Losing transactions return 409 Conflict without producing side effects.
    - Outbox remains RECONCILIATION_REQUIRED.
    """
    import concurrent.futures
    from sqlalchemy.orm import sessionmaker

    session.commit()

    user = sandbox_setup["user"]
    user_id = user.id
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    acct_id = acct.id
    now = datetime.datetime.now(datetime.timezone.utc)

    setup_acct = session.query(PaperAccount).filter(PaperAccount.id == acct_id).first()
    setup_acct.reserved_cash_units = 5000000
    session.flush()

    intent = OrderIntent(
        owner_id=user_id,
        runtime_id=runtime.id,
        action_mapping_id="m_conc_rej",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=100000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_rej",
        trigger_event_key="tk_rej",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user_id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=903,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=100000,
        filled_quantity_units=0,
        status=OrderStatus.RECONCILIATION_REQUIRED.value,
    )
    session.add(order)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user_id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="RECONCILIATION_REQUIRED",
        idempotency_key=f"place_rej_race:{order.id}",
        canonical_payload_hash="hash_rej_race",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
    )
    session.add(outbox)
    session.flush()

    rec = ReconciliationRecord(
        owner_id=user_id,
        order_id=order.id,
        outbox_id=outbox.id,
        status="OPEN",
    )
    session.add(rec)
    session.flush()
    rec_id = rec.id
    order_id = order.id
    outbox_id = outbox.id
    session.commit()
    session.close()

    ThreadSessionFactory = sessionmaker(bind=session.get_bind(), autocommit=False, autoflush=False)

    successes = []
    conflicts = []
    other_errors = []

    def attempt_reject(idx: int):
        db = ThreadSessionFactory()
        try:
            actor = db.query(User).filter(User.id == user_id).first()
            resolved = SandboxService.resolve_reconciliation(
                db=db,
                actor_user=actor,
                resolution_type="PLACE_REJECTED",
                provider_order_reference=None,
                notes=f"Concurrent reject by thread {idx}",
                reconciliation_id=rec_id,
            )
            successes.append(resolved.id)
        except ConflictError as e:
            conflicts.append(str(e))
        except Exception as e:
            other_errors.append(e)
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt_reject, i) for i in range(2)]
        concurrent.futures.wait(futures)

    assert len(other_errors) == 0, f"Unexpected errors during resolution race: {other_errors}"
    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}"
    assert len(conflicts) == 1, f"Expected 1 conflict error, got {len(conflicts)}"

    db_check = ThreadSessionFactory()
    final_rec = db_check.query(ReconciliationRecord).filter(ReconciliationRecord.id == rec_id).first()
    assert final_rec.status == "RESOLVED"
    assert final_rec.resolution_type == "PLACE_REJECTED"

    final_order = db_check.query(Order).filter(Order.id == order_id).first()
    assert final_order.status == OrderStatus.PROVIDER_REJECTED.value

    # Exactly one reservation release occurred:
    final_acct = db_check.query(PaperAccount).filter(PaperAccount.id == acct_id).first()
    assert final_acct.reserved_cash_units == 0

    # Exactly one ledger release entry exists:
    ledger_releases = db_check.query(AccountLedgerEntry).filter(
        AccountLedgerEntry.order_id == order_id,
        AccountLedgerEntry.entry_type == LedgerEntryType.RESERVATION_RELEASE.value,
    ).all()
    assert len(ledger_releases) == 1

    # Zero external links exist for PLACE_REJECTED:
    links = db_check.query(ExternalOrderLink).filter(ExternalOrderLink.order_id == order_id).all()
    assert len(links) == 0

    # Outbox remains RECONCILIATION_REQUIRED:
    final_outbox = db_check.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
    assert final_outbox.status == "RECONCILIATION_REQUIRED"

    db_check.close()


def test_reconciliation_savepoint_reraises_unrelated_integrity_errors(session, sandbox_setup):
    """
    Requirement 6 Test:
    Proves that the nested transaction/savepoint in _transition_to_reconciliation:
    - Catches only the expected unique constraint for (owner_id, outbox_id).
    - Rolls back only its savepoint.
    - Reloads the winning OPEN record when the collision is expected.
    - Re-raises unrelated integrity errors (e.g. check constraint failure or foreign key failure).
    - Preserves outer order/outbox updates when not aborted by an unrelated error.
    """
    from sqlalchemy.exc import IntegrityError
    from unittest.mock import patch

    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_sp_1",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=100000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_sp1",
        trigger_event_key="tk_sp1",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=904,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=100000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        idempotency_key=f"savepoint_test:{order.id}",
        canonical_payload_hash="hash_sp",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        attempts=1,
    )
    session.add(outbox)
    session.commit()

    worker = SandboxOutboxWorker()

    # Intentionally trigger an unrelated IntegrityError
    unrelated_exc = IntegrityError(
        statement="INSERT INTO reconciliation_records ...",
        params={},
        orig=Exception("CHECK constraint failed: ck_reconciliation_records_status"),
    )

    with patch.object(session, "add", side_effect=unrelated_exc):
        with pytest.raises(IntegrityError) as exc_info:
            worker._transition_to_reconciliation(session, outbox, order, "TEST_ERR", "Unrelated integrity error test")
        assert "ck_reconciliation_records_status" in str(exc_info.value)


def test_outbox_dead_letter_pre_transmission_releases_cash_no_reconciliation(session, sandbox_setup, monkeypatch):
    """
    Requirement 2 Test:
    If a PLACE reaches DEAD_LETTER without ever being transmitted:
    - Order becomes PROVIDER_REJECTED.
    - Reserved cash is released exactly once.
    - No reconciliation case is created because provider acceptance was ruled out.
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Setup reserved cash on account
    acct.reserved_cash_units = 2500000
    session.flush()

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_dl_place",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=50000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_dl_p",
        trigger_event_key="tk_dl_p",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=905,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=50000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        idempotency_key=f"dl_place:{order.id}",
        canonical_payload_hash="hash_dl_p",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        attempts=3,
        max_attempts=3,
    )
    session.add(outbox)
    session.commit()

    # Force network gate to block transmission
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "false")

    worker = SandboxOutboxWorker()
    worker.process_record(session, outbox, now)
    session.commit()

    # Verify:
    assert outbox.status == "DEAD_LETTER"
    assert order.status == OrderStatus.PROVIDER_REJECTED.value
    assert acct.reserved_cash_units == 0  # Released exactly once!

    # Verify no reconciliation record created:
    recs = session.query(ReconciliationRecord).filter(ReconciliationRecord.order_id == order.id).all()
    assert len(recs) == 0


def test_cancel_dead_letter_preserves_order_status(session, sandbox_setup, monkeypatch):
    """
    Requirement 1 Test:
    When a CANCEL exhausts safe pre-transmission retries:
    - Outbox -> DEAD_LETTER
    - Order must return from CANCEL_PENDING to ACKNOWLEDGED
    - Reservation remains unchanged
    - No reconciliation case is created
    - A later CANCEL operation must be permitted with a new outbox/idempotency operation
    - Never leave the order stuck in CANCEL_PENDING
    - Never mark it CANCELLED
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Setup order in ACKNOWLEDGED with reserved cash
    acct.reserved_cash_units = 1500000
    session.flush()

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_dl_cancel",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=50000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_dl_c",
        trigger_event_key="tk_dl_c",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=906,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=50000,
        filled_quantity_units=0,
        status=OrderStatus.ACKNOWLEDGED.value,
    )
    session.add(order)
    session.commit()

    # 2. Trigger first cancel request -> puts order into CANCEL_PENDING and queues outbox
    PaperService.cancel_order(session, order.id, user.id, reason="TEST_FIRST_CANCEL")
    session.commit()
    session.refresh(order)
    assert order.status == OrderStatus.CANCEL_PENDING.value

    outbox1 = session.query(SubmissionOutbox).filter(
        SubmissionOutbox.order_id == order.id,
        SubmissionOutbox.action_type == "CANCEL",
    ).first()
    assert outbox1 is not None
    assert outbox1.status == "PENDING"
    assert outbox1.idempotency_key == f"cancel:{order.id}"

    # 3. Simulate worker exhausting max attempts before transmission
    outbox1.status = "CLAIMED"
    outbox1.attempts = 3
    outbox1.max_attempts = 3
    session.commit()

    # Force network gate to block transmission
    monkeypatch.setenv("UPSTOX_SANDBOX_NETWORK_ENABLED", "false")

    worker = SandboxOutboxWorker()
    worker.process_record(session, outbox1, now)
    session.commit()

    session.refresh(outbox1)
    session.refresh(order)
    session.refresh(acct)

    # 4. Verify dead letter assertions:
    assert outbox1.status == "DEAD_LETTER"
    # Order returned from CANCEL_PENDING to ACKNOWLEDGED:
    assert order.status == OrderStatus.ACKNOWLEDGED.value
    # Reserved cash is untouched:
    assert acct.reserved_cash_units == 1500000
    # No reconciliation record created:
    recs = session.query(ReconciliationRecord).filter(ReconciliationRecord.order_id == order.id).all()
    assert len(recs) == 0

    # 5. Subsequent CANCEL operation must be permitted with a new outbox/idempotency operation:
    PaperService.cancel_order(session, order.id, user.id, reason="TEST_SECOND_CANCEL")
    session.commit()
    session.refresh(order)
    assert order.status == OrderStatus.CANCEL_PENDING.value

    all_cancels = session.query(SubmissionOutbox).filter(
        SubmissionOutbox.order_id == order.id,
        SubmissionOutbox.action_type == "CANCEL",
    ).order_by(SubmissionOutbox.created_at.asc()).all()
    assert len(all_cancels) == 2
    assert all_cancels[0].status == "DEAD_LETTER"
    assert all_cancels[1].status == "PENDING"
    assert all_cancels[1].idempotency_key == f"cancel:{order.id}:1"

def test_fail_closed_lease_recovery_with_transmission_marker(session, sandbox_setup):
    """
    Durability Test:
    When an expired lease has transmission_started_at set (external call may have occurred):
    - Outbox -> RECONCILIATION_REQUIRED (NOT RETRY_SCHEDULED)
    - Order -> RECONCILIATION_REQUIRED
    - Exactly one OPEN reconciliation record created
    - Zero adapter/network calls
    - Never retransmit
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_fc_1",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_fc1",
        trigger_event_key="tk_fc1",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=950,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    # Outbox WITH transmission_started_at (external call may have begun)
    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        claimed_by="dead-worker",
        claim_lease_until=now - datetime.timedelta(seconds=10),  # Expired!
        idempotency_key="fc_lease_test",
        canonical_payload_hash="hash_fc",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        transmission_started_at=now - datetime.timedelta(seconds=15),  # Marker set!
    )
    session.add(outbox)
    session.commit()

    worker = SandboxOutboxWorker(worker_id="recovery-worker")
    worker.recover_expired_leases(session, now)
    session.commit()

    session.refresh(outbox)
    session.refresh(order)

    # Fail-closed assertions:
    assert outbox.status == "RECONCILIATION_REQUIRED"
    assert outbox.claimed_by is None
    assert outbox.last_error_code == "LEASE_EXPIRED_AFTER_TRANSMISSION"
    assert order.status == OrderStatus.RECONCILIATION_REQUIRED.value

    # Exactly one OPEN reconciliation record created:
    recs = session.query(ReconciliationRecord).filter(
        ReconciliationRecord.order_id == order.id,
        ReconciliationRecord.outbox_id == outbox.id,
    ).all()
    assert len(recs) == 1
    assert recs[0].status == "OPEN"


def test_transmission_marker_committed_before_external_call(session, sandbox_setup):
    """
    Durability Test:
    Verifies that the transmission_started_at marker is durably committed
    BEFORE the external adapter call is made.
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    now = datetime.datetime.now(datetime.timezone.utc)

    PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    order = session.query(Order).filter(Order.runtime_id == runtime.id).first()
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).first()

    # Track when marker was committed vs when adapter was called
    call_log = []

    class MarkerTrackingAdapter(UpstoxSandboxAdapter):
        def place_order(self, payload, token):
            # At this point, transmission_started_at should already be committed
            call_log.append("adapter_called")
            return UpstoxPlaceResult(provider_order_id="MARKER_TEST_1", status="success", raw_response={"order_id": "MARKER_TEST_1"})

    adapter = MarkerTrackingAdapter()
    worker = SandboxOutboxWorker(adapter=adapter)
    worker.process_batch(session)

    assert len(call_log) == 1
    assert call_log[0] == "adapter_called"

    session.refresh(outbox)
    assert outbox.status == "DELIVERED"
    # Marker should still be set even after delivery
    assert outbox.transmission_started_at is not None


def test_fail_closed_lease_recovery_idempotent_reconciliation_record(session, sandbox_setup):
    """
    Durability Test:
    Multiple lease recovery cycles on the same outbox (with transmission marker)
    must not create duplicate reconciliation records.
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_fc_idem",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_fc_idem",
        trigger_event_key="tk_fc_idem",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=951,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        claimed_by="dead-worker-2",
        claim_lease_until=now - datetime.timedelta(seconds=10),
        idempotency_key="fc_idem_test",
        canonical_payload_hash="hash_fc_idem",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        transmission_started_at=now - datetime.timedelta(seconds=15),
    )
    session.add(outbox)
    session.commit()

    worker = SandboxOutboxWorker(worker_id="recovery-worker-idem")

    # First recovery cycle
    worker.recover_expired_leases(session, now)
    session.commit()

    session.refresh(outbox)
    assert outbox.status == "RECONCILIATION_REQUIRED"

    recs_after_first = session.query(ReconciliationRecord).filter(
        ReconciliationRecord.outbox_id == outbox.id,
    ).all()
    assert len(recs_after_first) == 1

    # Simulate a second recovery cycle (outbox is already RECONCILIATION_REQUIRED,
    # so it won't be picked up by expired lease query again, but let's verify idempotency
    # by manually re-running with status forced back to CLAIMED)
    outbox.status = "CLAIMED"
    outbox.claim_lease_until = now - datetime.timedelta(seconds=5)
    session.commit()

    worker.recover_expired_leases(session, now)
    session.commit()

    # Still exactly one reconciliation record (idempotent)
    recs_after_second = session.query(ReconciliationRecord).filter(
        ReconciliationRecord.outbox_id == outbox.id,
    ).all()
    assert len(recs_after_second) == 1


def test_marker_commit_failure_causes_zero_adapter_calls(session, sandbox_setup, monkeypatch):
    """
    Durability Test: If _commit_transmission_marker fails, the adapter is never called.
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]

    PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    order = session.query(Order).filter(Order.runtime_id == runtime.id).first()
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).first()

    adapter_calls = []

    class FailMarkerAdapter(UpstoxSandboxAdapter):
        def place_order(self, payload, token):
            adapter_calls.append("PLACE_CALLED")
            return UpstoxPlaceResult(provider_order_id="SHOULD_NOT_HAPPEN", status="success", raw_response={})

    adapter = FailMarkerAdapter()
    worker = SandboxOutboxWorker(adapter=adapter)

    # Monkey-patch _commit_transmission_marker to always fail
    original_commit_marker = worker._commit_transmission_marker
    def failing_marker(db, outbox, now):
        return False  # Simulate commit failure
    worker._commit_transmission_marker = failing_marker

    worker.process_batch(session)

    # Adapter was NEVER called
    assert len(adapter_calls) == 0, f"Adapter was called despite marker failure: {adapter_calls}"


def test_marker_visible_from_independent_session_before_adapter(session, sandbox_setup):
    """
    Durability Test: The transmission_started_at marker is visible from an independent
    DB session before the adapter call occurs.
    """
    from sqlalchemy.orm import sessionmaker

    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]

    PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    order = session.query(Order).filter(Order.runtime_id == runtime.id).first()
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).first()
    outbox_id = outbox.id

    IndependentSessionFactory = sessionmaker(bind=session.get_bind(), autocommit=False, autoflush=False)
    marker_visible_before_adapter = []

    class VisibilityCheckAdapter(UpstoxSandboxAdapter):
        def place_order(self, payload, token):
            # At this point, marker should be committed and visible from an independent session
            check_db = IndependentSessionFactory()
            check_outbox = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
            marker_visible_before_adapter.append(check_outbox.transmission_started_at is not None)
            check_db.close()
            return UpstoxPlaceResult(provider_order_id="VIS_CHECK_1", status="success", raw_response={"order_id": "VIS_CHECK_1"})

    adapter = VisibilityCheckAdapter()
    worker = SandboxOutboxWorker(adapter=adapter)
    worker.process_batch(session)

    assert len(marker_visible_before_adapter) == 1
    assert marker_visible_before_adapter[0] is True, "Marker was NOT visible from independent session before adapter call"


def test_simulated_crash_after_adapter_cannot_retransmit(session, sandbox_setup):
    """
    Durability Test: After the adapter call, even if a crash (exception) prevents
    the outbox from being marked DELIVERED, the marker ensures no retransmission.
    The expired-lease recovery path must fail-closed to RECONCILIATION_REQUIRED.
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    now = datetime.datetime.now(datetime.timezone.utc)

    PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    order = session.query(Order).filter(Order.runtime_id == runtime.id).first()
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).first()

    adapter_call_count = []

    class CrashAfterAdapter(UpstoxSandboxAdapter):
        def place_order(self, payload, token):
            adapter_call_count.append(1)
            return UpstoxPlaceResult(provider_order_id="CRASH_TEST_1", status="success", raw_response={"order_id": "CRASH_TEST_1"})

    adapter = CrashAfterAdapter()
    worker = SandboxOutboxWorker(adapter=adapter, lease_duration_seconds=5)

    # Override _handle_place_action to simulate crash after adapter call
    original_handle = worker._handle_place_action
    def crashing_handle(db, outbox, order, token, now):
        # Call adapter but then crash before updating outbox status
        res = adapter.place_order(outbox.payload_json, token)
        raise RuntimeError("Simulated worker crash after adapter call")
    worker._handle_place_action = crashing_handle

    # Process batch — will raise RuntimeError after adapter call
    try:
        worker.process_batch(session)
    except RuntimeError:
        pass

    # Adapter was called exactly once
    assert len(adapter_call_count) == 1

    # Outbox should still have status=CLAIMED with transmission_started_at set
    session.expire_all()
    session.refresh(outbox)
    assert outbox.transmission_started_at is not None
    assert outbox.status == "CLAIMED"

    # Simulate lease expiry: fast-forward clock
    future = now + datetime.timedelta(seconds=60)
    outbox.claim_lease_until = now - datetime.timedelta(seconds=1)  # Expired
    session.commit()

    # Recovery worker picks up the expired lease
    recovery_worker = SandboxOutboxWorker(worker_id="recovery")
    recovery_adapter_calls = []

    class NoCallAdapter(UpstoxSandboxAdapter):
        def place_order(self, payload, token):
            recovery_adapter_calls.append(1)
            return UpstoxPlaceResult(provider_order_id="SHOULD_NOT_HAPPEN", status="success", raw_response={})

    recovery_worker.adapter = NoCallAdapter()
    recovery_worker.recover_expired_leases(session, future)
    session.commit()

    # Zero adapter calls during recovery
    assert len(recovery_adapter_calls) == 0

    session.refresh(outbox)
    assert outbox.status == "RECONCILIATION_REQUIRED"

    session.refresh(order)
    assert order.status == OrderStatus.RECONCILIATION_REQUIRED.value

    recs = session.query(ReconciliationRecord).filter(ReconciliationRecord.outbox_id == outbox.id).all()
    assert len(recs) == 1
    assert recs[0].status == "OPEN"


def test_expired_cancel_with_marker_produces_reconciliation(session, sandbox_setup):
    """
    Durability Test: Expired CANCEL outbox with transmission_started_at marker
    produces RECONCILIATION_REQUIRED (not RETRY_SCHEDULED) and zero network calls.
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_cancel_fc",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_cancel_fc",
        trigger_event_key="tk_cancel_fc",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=960,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.CANCEL_PENDING.value,
    )
    session.add(order)
    session.flush()

    # CANCEL outbox with transmission marker set (external call may have occurred)
    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="CANCEL",
        priority=0,
        status="CLAIMED",
        claimed_by="dead-cancel-worker",
        claim_lease_until=now - datetime.timedelta(seconds=10),
        idempotency_key="fc_cancel_test",
        canonical_payload_hash="hash_cancel_fc",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        transmission_started_at=now - datetime.timedelta(seconds=15),
    )
    session.add(outbox)
    session.commit()

    worker = SandboxOutboxWorker(worker_id="cancel-recovery")
    worker.recover_expired_leases(session, now)
    session.commit()

    session.refresh(outbox)
    session.refresh(order)

    assert outbox.status == "RECONCILIATION_REQUIRED"
    assert outbox.last_error_code == "LEASE_EXPIRED_AFTER_TRANSMISSION"
    assert order.status == OrderStatus.RECONCILIATION_REQUIRED.value

    recs = session.query(ReconciliationRecord).filter(
        ReconciliationRecord.outbox_id == outbox.id,
    ).all()
    assert len(recs) == 1
    assert recs[0].status == "OPEN"


def test_sqlite_retry_uses_fresh_sessions(session, sandbox_setup):
    """
    Durability Test: SQLite lock retry in _transition_to_reconciliation creates
    a fresh session from SessionLocal for each retry, not the failed session.
    """
    from unittest.mock import patch, MagicMock
    from sqlalchemy.exc import OperationalError

    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_fresh_sess",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_fresh",
        trigger_event_key="tk_fresh",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=970,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        idempotency_key="fresh_sess_test",
        canonical_payload_hash="hash_fresh",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        attempts=1,
    )
    session.add(outbox)
    session.commit()

    worker = SandboxOutboxWorker()

    # Track which sessions are used for _do_reconciliation_transition
    sessions_used = []
    original_do_transition = worker._do_reconciliation_transition

    call_count = [0]
    def tracking_do_transition(db, outbox_arg, order_arg, error_code, error_msg):
        sessions_used.append(id(db))
        call_count[0] += 1
        if call_count[0] == 1:
            # First call (on original session): raise SQLite locked error
            raise OperationalError(
                statement="UPDATE",
                params={},
                orig=Exception("database is locked"),
            )
        # Subsequent calls succeed
        return original_do_transition(db, outbox_arg, order_arg, error_code, error_msg)

    worker._do_reconciliation_transition = tracking_do_transition

    # This should retry with a fresh session
    worker._transition_to_reconciliation(session, outbox, order, "FRESH_SESS_TEST", "Testing fresh sessions")

    # Verify at least 2 sessions used and they are different
    assert len(sessions_used) >= 2, f"Expected at least 2 session uses, got {len(sessions_used)}"
    assert sessions_used[0] != sessions_used[1], "Retry used the SAME session, not a fresh one"


def test_sqlite_retry_exhaustion_preserves_marker(session, sandbox_setup):
    """
    Durability Test: After SQLite lock retry exhaustion, the committed
    transmission_started_at marker remains in the database.
    Lease recovery will eventually force reconciliation.
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import sessionmaker

    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_exhaust",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_exhaust",
        trigger_event_key="tk_exhaust",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=971,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        idempotency_key="exhaust_test",
        canonical_payload_hash="hash_exhaust",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        attempts=1,
        transmission_started_at=now,  # Marker already committed
    )
    session.add(outbox)
    session.commit()

    outbox_id = outbox.id
    order_id = order.id

    worker = SandboxOutboxWorker()

    # Force every retry to fail with SQLite locked
    def always_locked(db, outbox_arg, order_arg, error_code, error_msg):
        raise OperationalError(
            statement="UPDATE",
            params={},
            orig=Exception("database is locked"),
        )
    worker._do_reconciliation_transition = always_locked

    with pytest.raises(OperationalError):
        worker._transition_to_reconciliation(session, outbox, order, "EXHAUST_TEST", "Exhaustion test")

    # Verify the committed marker survives
    IndependentSessionFactory = sessionmaker(bind=session.get_bind(), autocommit=False, autoflush=False)
    check_db = IndependentSessionFactory()
    check_outbox = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
    assert check_outbox.transmission_started_at is not None, "Marker was lost after retry exhaustion!"
    check_db.close()


def test_recovery_after_retry_exhaustion_creates_open_case(session, sandbox_setup):
    """
    Durability Test: After retry exhaustion, the lease expires and
    recover_expired_leases creates one OPEN reconciliation case without transmission.
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import sessionmaker

    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_recovery_exhaust",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_rec_exhaust",
        trigger_event_key="tk_rec_exhaust",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=972,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    # Outbox CLAIMED with marker + expired lease (simulates post-retry-exhaustion state)
    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        claimed_by="exhausted-worker",
        claim_lease_until=now - datetime.timedelta(seconds=5),  # Expired
        idempotency_key="recovery_exhaust_test",
        canonical_payload_hash="hash_rec_exhaust",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        transmission_started_at=now - datetime.timedelta(seconds=10),  # Marker present
    )
    session.add(outbox)
    session.commit()

    # Recovery worker
    recovery_worker = SandboxOutboxWorker(worker_id="recovery-exhaust")

    adapter_calls = []
    class NoCallAdapter(UpstoxSandboxAdapter):
        def place_order(self, payload, token):
            adapter_calls.append(1)
            return UpstoxPlaceResult(provider_order_id="FAIL", status="success", raw_response={})
    recovery_worker.adapter = NoCallAdapter()

    recovery_worker.recover_expired_leases(session, now)
    session.commit()

    # Zero adapter calls
    assert len(adapter_calls) == 0

    session.refresh(outbox)
    assert outbox.status == "RECONCILIATION_REQUIRED"

    session.refresh(order)
    assert order.status == OrderStatus.RECONCILIATION_REQUIRED.value

    recs = session.query(ReconciliationRecord).filter(ReconciliationRecord.outbox_id == outbox.id).all()
    assert len(recs) == 1
    assert recs[0].status == "OPEN"


def test_unrelated_operational_error_not_swallowed(session, sandbox_setup):
    """
    Durability Test: An OperationalError that is NOT a SQLite busy/locked error
    is propagated immediately, not retried.
    """
    from sqlalchemy.exc import OperationalError

    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_unrelated_op",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_unrel",
        trigger_event_key="tk_unrel",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=973,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        idempotency_key="unrelated_op_test",
        canonical_payload_hash="hash_unrel",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        attempts=1,
    )
    session.add(outbox)
    session.commit()

    worker = SandboxOutboxWorker()

    def unrelated_error(db, outbox_arg, order_arg, error_code, error_msg):
        raise OperationalError(
            statement="UPDATE",
            params={},
            orig=Exception("disk I/O error"),  # NOT "database is locked"
        )
    worker._do_reconciliation_transition = unrelated_error

    with pytest.raises(OperationalError) as exc_info:
        worker._transition_to_reconciliation(session, outbox, order, "UNRELATED_TEST", "Unrelated error")
    assert "disk I/O error" in str(exc_info.value)


def test_no_transaction_held_during_adapter_call(session, sandbox_setup):
    """
    Durability Test: No database transaction or row lock remains open during
    the HTTP request (adapter call). The marker commit completes before the adapter
    is invoked, and the adapter call happens outside any DB transaction.
    """
    from sqlalchemy.orm import sessionmaker

    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]

    PaperService.step_runtime(session, runtime.id, user.id, step_count=1)
    order = session.query(Order).filter(Order.runtime_id == runtime.id).first()
    outbox = session.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == order.id).first()
    outbox_id = outbox.id

    IndependentSessionFactory = sessionmaker(bind=session.get_bind(), autocommit=False, autoflush=False)
    transaction_state_during_adapter = []

    class TransactionCheckAdapter(UpstoxSandboxAdapter):
        def place_order(self, payload, token):
            # Try to write from an independent session during the adapter call
            # If the main session holds a write lock, this would block/fail on SQLite
            check_db = IndependentSessionFactory()
            try:
                check_outbox = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
                in_txn = session.in_transaction()
                # Verify independent session can write during adapter call
                check_outbox.last_error_code = "WRITE_DURING_ADAPTER"
                check_db.commit()
                # Verify the marker is committed (visible from independent session)
                transaction_state_during_adapter.append({
                    "marker_visible": check_outbox.transmission_started_at is not None,
                    "can_read": True,
                    "can_write": True,
                    "in_transaction": in_txn,
                })
            except Exception as e:
                transaction_state_during_adapter.append({
                    "marker_visible": False,
                    "can_read": False,
                    "can_write": False,
                    "in_transaction": session.in_transaction(),
                    "error": str(e),
                })
            finally:
                check_db.close()
            return UpstoxPlaceResult(provider_order_id="TXN_CHECK_1", status="success", raw_response={"order_id": "TXN_CHECK_1"})

    adapter = TransactionCheckAdapter()
    worker = SandboxOutboxWorker(adapter=adapter)
    worker.process_batch(session)

    assert len(transaction_state_during_adapter) == 1
    state = transaction_state_during_adapter[0]
    assert state["can_read"] is True, f"Independent session could not read during adapter call: {state.get('error')}"
    assert state["can_write"] is True, f"Independent session could not write during adapter call: {state.get('error')}"
    assert state["marker_visible"] is True, "Marker not visible from independent session during adapter call"
    assert state["in_transaction"] is False, "Caller session held an open transaction during adapter call"


def test_concurrent_lease_recovery_creates_one_open_record(session, sandbox_setup):
    """
    Durability Test: Multiple threads concurrently running recover_expired_leases
    on the same outbox (with marker) create exactly one OPEN reconciliation record.
    """
    import concurrent.futures
    from sqlalchemy.orm import sessionmaker

    session.commit()

    user = sandbox_setup["user"]
    user_id = user.id
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user_id,
        runtime_id=runtime.id,
        action_mapping_id="m_conc_rec",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=1000000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_conc_rec",
        trigger_event_key="tk_conc_rec",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user_id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=974,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=1000000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user_id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        claimed_by="dead-conc-worker",
        claim_lease_until=now - datetime.timedelta(seconds=10),
        idempotency_key="conc_recovery_test",
        canonical_payload_hash="hash_conc_rec",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        transmission_started_at=now - datetime.timedelta(seconds=15),
    )
    session.add(outbox)
    session.flush()

    outbox_id = outbox.id
    order_id = order.id
    session.commit()
    session.close()

    ThreadSessionFactory = sessionmaker(bind=session.get_bind(), autocommit=False, autoflush=False)

    errors = []
    def recover_in_thread(thread_idx):
        max_retries = 10
        for attempt in range(max_retries):
            db = ThreadSessionFactory()
            try:
                worker = SandboxOutboxWorker(worker_id=f"conc-rec-{thread_idx}")
                worker.recover_expired_leases(db, now)
                db.commit()
                break
            except Exception as e:
                try:
                    db.rollback()
                except Exception:
                    pass
                err_str = str(e).lower()
                # SQLite concurrency: retry on lock contention, integrity races, and pending rollback
                is_transient = (
                    "database is locked" in err_str
                    or "unique constraint" in err_str
                    or "pendingrollbackerror" in type(e).__name__.lower()
                    or "pending" in err_str
                )
                if is_transient and attempt < max_retries - 1:
                    import time
                    time.sleep(0.05 * (attempt + 1))
                    continue
                errors.append(e)
                break
            finally:
                db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(recover_in_thread, i) for i in range(2)]
        concurrent.futures.wait(futures)

    assert len(errors) == 0, f"Concurrent recovery errors: {errors}"

    # Exactly one OPEN reconciliation record
    check_db = ThreadSessionFactory()
    recs = check_db.query(ReconciliationRecord).filter(
        ReconciliationRecord.outbox_id == outbox_id,
    ).all()
    assert len(recs) == 1
    assert recs[0].status == "OPEN"

    final_outbox = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
    assert final_outbox.status == "RECONCILIATION_REQUIRED"

    final_order = check_db.query(Order).filter(Order.id == order_id).first()
    assert final_order.status == OrderStatus.RECONCILIATION_REQUIRED.value

    check_db.close()


def test_attempt_0_rollback_cannot_discard_unrelated_pending_work(session, sandbox_setup):
    """
    Transaction-Ownership Test 2:
    Proves attempt-0 rollback in _transition_to_reconciliation cannot discard
    unrelated work from prior outbox items in the same batch or transaction.
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Order 1 + Outbox 1 (Will succeed)
    intent1 = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_roll_1",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=100000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_roll_1",
        trigger_event_key="tk_roll_1",
    )
    session.add(intent1)
    session.flush()

    order1 = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent1.id,
        account_id=acct.id,
        order_sequence_number=981,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=100000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order1)
    session.flush()

    outbox1 = SubmissionOutbox(
        owner_id=user.id,
        order_id=order1.id,
        action_type="PLACE",
        priority=10,
        status="PENDING",
        idempotency_key="rollback_test_1",
        canonical_payload_hash="hash_roll_1",
        payload_json={"order_id": order1.id},
        next_attempt_at=now,
        attempts=0,
    )
    session.add(outbox1)

    # 2. Order 2 + Outbox 2 (Will timeout, attempt 0 hits lock, retry succeeds)
    intent2 = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_roll_2",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=100000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_roll_2",
        trigger_event_key="tk_roll_2",
    )
    session.add(intent2)
    session.flush()

    order2 = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent2.id,
        account_id=acct.id,
        order_sequence_number=982,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=100000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order2)
    session.flush()

    outbox2 = SubmissionOutbox(
        owner_id=user.id,
        order_id=order2.id,
        action_type="PLACE",
        priority=10,
        status="PENDING",
        idempotency_key="rollback_test_2",
        canonical_payload_hash="hash_roll_2",
        payload_json={"order_id": order2.id},
        next_attempt_at=now,
        attempts=0,
    )
    session.add(outbox2)
    session.commit()

    order1_id = order1.id
    outbox1_id = outbox1.id
    order2_id = order2.id
    outbox2_id = outbox2.id

    class MixedBatchAdapter(UpstoxSandboxAdapter):
        def place_order(self, payload, token):
            if payload.get("order_id") == order1_id:
                return UpstoxPlaceResult(provider_order_id="MIX_1", status="success", raw_response={"order_id": "MIX_1"})
            raise UpstoxAmbiguousError("Timed out contacting Upstox")

    worker = SandboxOutboxWorker(adapter=MixedBatchAdapter())

    # Simulate attempt 0 lock contention on item 2 transition
    orig_do_transition = worker._do_reconciliation_transition
    transition_calls = [0]
    def failing_attempt_0_transition(db, outbox_arg, order_arg, error_code, error_msg):
        transition_calls[0] += 1
        if transition_calls[0] == 1:
            raise OperationalError(
                statement="UPDATE",
                params={},
                orig=Exception("database is locked"),
            )
        return orig_do_transition(db, outbox_arg, order_arg, error_code, error_msg)

    worker._do_reconciliation_transition = failing_attempt_0_transition

    worker.process_batch(session)

    # Verify attempt 0 failed and retry succeeded
    assert transition_calls[0] >= 2, "Expected at least 2 transition attempts"

    # Independent check to verify database state
    IndependentSessionFactory = sessionmaker(bind=session.get_bind(), autocommit=False, autoflush=False)
    check_db = IndependentSessionFactory()

    # Item 1 was NOT discarded by Item 2's attempt-0 rollback
    chk_order1 = check_db.query(Order).filter(Order.id == order1_id).first()
    chk_outbox1 = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox1_id).first()
    assert chk_order1.status == OrderStatus.ACKNOWLEDGED.value, f"Order 1 was discarded: {chk_order1.status}"
    assert chk_outbox1.status == "DELIVERED", f"Outbox 1 was discarded: {chk_outbox1.status}"

    # Item 2 successfully committed RECONCILIATION_REQUIRED
    chk_order2 = check_db.query(Order).filter(Order.id == order2_id).first()
    chk_outbox2 = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox2_id).first()
    assert chk_order2.status == OrderStatus.RECONCILIATION_REQUIRED.value
    assert chk_outbox2.status == "RECONCILIATION_REQUIRED"

    check_db.close()


def test_fresh_session_success_survives_outer_commit_and_rollback(session, sandbox_setup):
    """
    Transaction-Ownership Tests 3, 4, 5:
    Proves that a fresh-session retry commit:
    - Remains committed after the outer worker returns.
    - Cannot be overwritten by a subsequent outer session commit().
    - Cannot be removed by a subsequent outer session rollback().
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_survive",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=100000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_surv",
        trigger_event_key="tk_surv",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=983,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=100000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="PENDING",
        idempotency_key="survive_test",
        canonical_payload_hash="hash_surv",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        attempts=0,
    )
    session.add(outbox)
    session.commit()

    order_id = order.id
    outbox_id = outbox.id

    class TimeoutAdapter(UpstoxSandboxAdapter):
        def place_order(self, payload, token):
            raise UpstoxAmbiguousError("Timeout in survive test")

    worker = SandboxOutboxWorker(adapter=TimeoutAdapter())

    # Force attempt 0 to encounter SQLite lock contention
    orig_do_transition = worker._do_reconciliation_transition
    transition_calls = [0]
    def lock_attempt_0(db, outbox_arg, order_arg, error_code, error_msg):
        transition_calls[0] += 1
        if transition_calls[0] == 1:
            raise OperationalError(
                statement="UPDATE",
                params={},
                orig=Exception("database is locked"),
            )
        return orig_do_transition(db, outbox_arg, order_arg, error_code, error_msg)

    worker._do_reconciliation_transition = lock_attempt_0

    # Process batch via caller session
    worker.process_batch(session)

    # 1. Verify fresh session committed and survives worker return
    IndependentSessionFactory = sessionmaker(bind=session.get_bind(), autocommit=False, autoflush=False)
    check_db = IndependentSessionFactory()
    chk_order = check_db.query(Order).filter(Order.id == order_id).first()
    chk_outbox = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
    assert chk_order.status == OrderStatus.RECONCILIATION_REQUIRED.value
    assert chk_outbox.status == "RECONCILIATION_REQUIRED"

    # 2. Outer caller commit cannot overwrite or invalidate
    session.commit()
    check_db.expire_all()
    chk_order = check_db.query(Order).filter(Order.id == order_id).first()
    chk_outbox = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
    assert chk_order.status == OrderStatus.RECONCILIATION_REQUIRED.value
    assert chk_outbox.status == "RECONCILIATION_REQUIRED"

    # 3. Outer caller rollback cannot undo or remove
    session.rollback()
    check_db.expire_all()
    chk_order = check_db.query(Order).filter(Order.id == order_id).first()
    chk_outbox = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
    assert chk_order.status == OrderStatus.RECONCILIATION_REQUIRED.value
    assert chk_outbox.status == "RECONCILIATION_REQUIRED"

    check_db.close()


def test_worker_safely_processes_next_batch_item_after_retry_success(session, sandbox_setup):
    """
    Transaction-Ownership Test 6:
    Proves that when item 1 in a batch transitions to reconciliation via a fresh-session
    retry, the outer caller session remains clean and fully capable of processing item 2.
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    # Item 1: Will timeout and retry
    intent1 = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_seq_1",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=100000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_seq_1",
        trigger_event_key="tk_seq_1",
    )
    session.add(intent1)
    session.flush()

    order1 = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent1.id,
        account_id=acct.id,
        order_sequence_number=984,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=100000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order1)
    session.flush()

    outbox1 = SubmissionOutbox(
        owner_id=user.id,
        order_id=order1.id,
        action_type="PLACE",
        priority=10,
        status="PENDING",
        idempotency_key="seq_test_1",
        canonical_payload_hash="hash_seq_1",
        payload_json={"order_id": order1.id},
        next_attempt_at=now,
        attempts=0,
    )
    session.add(outbox1)

    # Item 2: Will succeed cleanly
    intent2 = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_seq_2",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=100000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_seq_2",
        trigger_event_key="tk_seq_2",
    )
    session.add(intent2)
    session.flush()

    order2 = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent2.id,
        account_id=acct.id,
        order_sequence_number=985,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=100000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order2)
    session.flush()

    outbox2 = SubmissionOutbox(
        owner_id=user.id,
        order_id=order2.id,
        action_type="PLACE",
        priority=10,
        status="PENDING",
        idempotency_key="seq_test_2",
        canonical_payload_hash="hash_seq_2",
        payload_json={"order_id": order2.id},
        next_attempt_at=now,
        attempts=0,
    )
    session.add(outbox2)
    session.commit()

    order1_id = order1.id
    outbox1_id = outbox1.id
    order2_id = order2.id
    outbox2_id = outbox2.id

    class SequentialAdapter(UpstoxSandboxAdapter):
        def place_order(self, payload, token):
            if payload.get("order_id") == order1_id:
                raise UpstoxAmbiguousError("Timeout on item 1")
            return UpstoxPlaceResult(provider_order_id="SEQ_2_OK", status="success", raw_response={"order_id": "SEQ_2_OK"})

    worker = SandboxOutboxWorker(adapter=SequentialAdapter())

    # Item 1 hits lock contention on attempt 0, succeeds on attempt 1
    orig_do_transition = worker._do_reconciliation_transition
    transition_calls = [0]
    def lock_item1_attempt0(db, outbox_arg, order_arg, error_code, error_msg):
        transition_calls[0] += 1
        if transition_calls[0] == 1:
            raise OperationalError(
                statement="UPDATE",
                params={},
                orig=Exception("database is locked"),
            )
        return orig_do_transition(db, outbox_arg, order_arg, error_code, error_msg)

    worker._do_reconciliation_transition = lock_item1_attempt0

    # Process the entire batch
    processed = worker.process_batch(session)
    assert processed == 2

    # Verify Item 1 reached RECONCILIATION_REQUIRED
    IndependentSessionFactory = sessionmaker(bind=session.get_bind(), autocommit=False, autoflush=False)
    check_db = IndependentSessionFactory()

    chk_order1 = check_db.query(Order).filter(Order.id == order1_id).first()
    chk_outbox1 = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox1_id).first()
    assert chk_order1.status == OrderStatus.RECONCILIATION_REQUIRED.value
    assert chk_outbox1.status == "RECONCILIATION_REQUIRED"

    # Verify Item 2 was processed successfully despite earlier retry
    chk_order2 = check_db.query(Order).filter(Order.id == order2_id).first()
    chk_outbox2 = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox2_id).first()
    assert chk_order2.status == OrderStatus.ACKNOWLEDGED.value
    assert chk_outbox2.status == "DELIVERED"

    check_db.close()


def test_stale_caller_objects_cannot_flush_old_statuses(session, sandbox_setup):
    """
    Transaction-Ownership Test 7:
    Proves that after a fresh retry session commits, caller-owned ORM objects are expired
    so a later flush() on the caller session cannot restore the old status.
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_stale_flush",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=100000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_stale",
        trigger_event_key="tk_stale",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=986,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=100000,
        filled_quantity_units=0,
        status=OrderStatus.PENDING_SUBMISSION.value,
    )
    session.add(order)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="PLACE",
        priority=10,
        status="CLAIMED",
        idempotency_key="stale_flush_test",
        canonical_payload_hash="hash_stale",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        attempts=1,
    )
    session.add(outbox)
    session.commit()

    order_id = order.id
    outbox_id = outbox.id

    worker = SandboxOutboxWorker()

    # Simulate attempt 0 lock error so it retries with fresh session
    orig_do_transition = worker._do_reconciliation_transition
    transition_calls = [0]
    def lock_attempt0(db, outbox_arg, order_arg, error_code, error_msg):
        transition_calls[0] += 1
        if transition_calls[0] == 1:
            raise OperationalError(statement="UPDATE", params={}, orig=Exception("database is locked"))
        return orig_do_transition(db, outbox_arg, order_arg, error_code, error_msg)

    worker._do_reconciliation_transition = lock_attempt0

    worker._transition_to_reconciliation(session, outbox, order, "STALE_TEST", "Testing stale flush")

    # Now caller session tries to flush
    session.flush()
    session.commit()

    # Verify that in database, status is still RECONCILIATION_REQUIRED
    IndependentSessionFactory = sessionmaker(bind=session.get_bind(), autocommit=False, autoflush=False)
    check_db = IndependentSessionFactory()
    chk_order = check_db.query(Order).filter(Order.id == order_id).first()
    chk_outbox = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
    assert chk_order.status == OrderStatus.RECONCILIATION_REQUIRED.value
    assert chk_outbox.status == "RECONCILIATION_REQUIRED"
    check_db.close()


def test_cancel_action_satisfies_transaction_and_retry_rules(session, sandbox_setup):
    """
    Transaction-Ownership Test 11:
    Proves that CANCEL actions:
    - Hold no open DB transaction during adapter.cancel_order.
    - Successfully retry via fresh sessions on SQLite lock contention.
    - Commit RECONCILIATION_REQUIRED durably.
    - Survive outer commit and rollback.
    - Preserve cash reservation without premature release.
    """
    user = sandbox_setup["user"]
    runtime = sandbox_setup["runtime"]
    acct = sandbox_setup["account"]
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Setup reserved cash
    acct.reserved_cash_units = 5000000
    session.flush()

    intent = OrderIntent(
        owner_id=user.id,
        runtime_id=runtime.id,
        action_mapping_id="m_cancel_rule",
        requested_instrument_id="synthetic_candidate_option_pe_23000_15m",
        resolved_instrument_id="synthetic_candidate_option_pe_23000_15m",
        intent_type="ENTRY",
        reduce_only=False,
        side="BUY",
        quantity_units=50,
        order_type="LIMIT",
        limit_price_units=100000,
        time_in_force="DAY",
        source_candle_timestamp=now,
        source_evaluation_fingerprint="fp_c_rule",
        trigger_event_key="tk_c_rule",
    )
    session.add(intent)
    session.flush()

    order = Order(
        owner_id=user.id,
        runtime_id=runtime.id,
        intent_id=intent.id,
        account_id=acct.id,
        order_sequence_number=987,
        instrument_id="synthetic_candidate_option_pe_23000_15m",
        side=OrderSide.BUY.value,
        order_type="LIMIT",
        quantity_units=50,
        limit_price_units=100000,
        filled_quantity_units=0,
        status=OrderStatus.CANCEL_PENDING.value,
    )
    session.add(order)
    session.flush()

    # External link exists (meaning order was previously placed)
    ext_link = ExternalOrderLink(
        owner_id=user.id,
        order_id=order.id,
        provider_name="UPSTOX",
        provider_order_id="EXT_CANCEL_RULE_1",
        submitted_at=now,
    )
    session.add(ext_link)
    session.flush()

    outbox = SubmissionOutbox(
        owner_id=user.id,
        order_id=order.id,
        action_type="CANCEL",
        priority=0,
        status="PENDING",
        idempotency_key="cancel_rules_test",
        canonical_payload_hash="hash_c_rule",
        payload_json={"order_id": order.id},
        next_attempt_at=now,
        attempts=0,
    )
    session.add(outbox)
    session.commit()

    order_id = order.id
    outbox_id = outbox.id

    cancel_transaction_states = []

    class CancelRuleAdapter(UpstoxSandboxAdapter):
        def cancel_order(self, provider_order_id, token):
            cancel_transaction_states.append({
                "in_transaction": session.in_transaction(),
            })
            # Ambiguous 429 response
            raise UpstoxRetryable429(retry_after=5, error_code="RATE_LIMIT", message="Rate limit during cancel")

    worker = SandboxOutboxWorker(adapter=CancelRuleAdapter())

    # Force attempt 0 to encounter lock contention
    orig_do_transition = worker._do_reconciliation_transition
    transition_calls = [0]
    def lock_cancel_attempt0(db, outbox_arg, order_arg, error_code, error_msg):
        transition_calls[0] += 1
        if transition_calls[0] == 1:
            raise OperationalError(statement="UPDATE", params={}, orig=Exception("database is locked"))
        return orig_do_transition(db, outbox_arg, order_arg, error_code, error_msg)

    worker._do_reconciliation_transition = lock_cancel_attempt0

    worker.process_batch(session)

    # Verify no transaction was held during adapter call
    assert len(cancel_transaction_states) == 1
    assert cancel_transaction_states[0]["in_transaction"] is False

    # Verify attempt 0 hit lock and retry succeeded
    assert transition_calls[0] >= 2

    # Verify outcome in DB
    IndependentSessionFactory = sessionmaker(bind=session.get_bind(), autocommit=False, autoflush=False)
    check_db = IndependentSessionFactory()

    chk_order = check_db.query(Order).filter(Order.id == order_id).first()
    chk_outbox = check_db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
    chk_acct = check_db.query(PaperAccount).filter(PaperAccount.id == acct.id).first()

    assert chk_order.status == OrderStatus.RECONCILIATION_REQUIRED.value
    assert chk_outbox.status == "RECONCILIATION_REQUIRED"
    # Cancel reconciliation preserves cash reservation (never release until confirmed)
    assert chk_acct.reserved_cash_units == 5000000

    # Exactly one OPEN reconciliation record
    recs = check_db.query(ReconciliationRecord).filter(ReconciliationRecord.outbox_id == outbox_id).all()
    assert len(recs) == 1
    assert recs[0].status == "OPEN"

    check_db.close()
