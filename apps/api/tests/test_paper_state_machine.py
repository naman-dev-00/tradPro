import pytest
from src.engine.paper.models import OrderStatus, RuntimeStatus
from src.engine.paper.state_machine import (
    validate_order_transition,
    validate_runtime_transition,
    InvalidOrderTransitionError,
    InvalidRuntimeTransitionError,
    VALID_ORDER_TRANSITIONS,
    VALID_RUNTIME_TRANSITIONS,
)

def test_valid_order_transitions():
    # CREATED -> ACCEPTED, REJECTED, RISK_REJECTED, ERROR
    validate_order_transition(OrderStatus.CREATED, OrderStatus.ACCEPTED, actor="OMS", reason_code="RISK_PASSED")
    validate_order_transition(OrderStatus.CREATED, OrderStatus.REJECTED, actor="OMS", reason_code="EXCHANGE_REJECTED")
    validate_order_transition(OrderStatus.CREATED, OrderStatus.RISK_REJECTED, actor="RISK", reason_code="EXCEEDED")
    validate_order_transition(OrderStatus.CREATED, OrderStatus.ERROR, actor="OMS", reason_code="SYSTEM_ERROR")

    # ACCEPTED -> PARTIALLY_FILLED, FILLED, CANCEL_PENDING, REJECTED, EXPIRED, ERROR
    validate_order_transition(OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED, actor="BROKER", reason_code="FILL")
    validate_order_transition(OrderStatus.ACCEPTED, OrderStatus.FILLED, actor="BROKER", reason_code="FULL_FILL")
    validate_order_transition(OrderStatus.ACCEPTED, OrderStatus.CANCEL_PENDING, actor="USER", reason_code="USER_REQ")
    validate_order_transition(OrderStatus.ACCEPTED, OrderStatus.REJECTED, actor="BROKER", reason_code="REJECTED")
    validate_order_transition(OrderStatus.ACCEPTED, OrderStatus.EXPIRED, actor="CLOCK", reason_code="EXPIRED")
    validate_order_transition(OrderStatus.ACCEPTED, OrderStatus.ERROR, actor="OMS", reason_code="ERROR")

    # PARTIALLY_FILLED -> PARTIALLY_FILLED (multi-fill) and FILLED, CANCEL_PENDING, EXPIRED, ERROR
    validate_order_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED, actor="BROKER", reason_code="ADDITIONAL_FILL")
    validate_order_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, actor="BROKER", reason_code="FILL")
    validate_order_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCEL_PENDING, actor="USER", reason_code="CANCEL_REQ")
    validate_order_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.EXPIRED, actor="CLOCK", reason_code="EXPIRED")
    validate_order_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.ERROR, actor="OMS", reason_code="ERROR")

    # CANCEL_PENDING -> CANCELLED, FILLED (race condition), ERROR
    validate_order_transition(OrderStatus.CANCEL_PENDING, OrderStatus.CANCELLED, actor="BROKER", reason_code="CONFIRMED")
    validate_order_transition(OrderStatus.CANCEL_PENDING, OrderStatus.FILLED, actor="BROKER", reason_code="FILL_WON_RACE")
    validate_order_transition(OrderStatus.CANCEL_PENDING, OrderStatus.ERROR, actor="OMS", reason_code="ERROR")

def test_invalid_order_transitions():
    # Terminal state transitions must fail
    terminal_order_states = [
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.RISK_REJECTED,
        OrderStatus.ERROR,
    ]
    for term_state in terminal_order_states:
        assert len(VALID_ORDER_TRANSITIONS[term_state]) == 0, f"Terminal state {term_state} must have no outgoing transitions"
        with pytest.raises(InvalidOrderTransitionError):
            validate_order_transition(term_state, OrderStatus.ACCEPTED, actor="USER", reason_code="INVALID")

    # CREATED cannot jump directly to FILLED
    with pytest.raises(InvalidOrderTransitionError):
        validate_order_transition(OrderStatus.CREATED, OrderStatus.FILLED, actor="BROKER", reason_code="INVALID")

def test_valid_runtime_transitions():
    validate_runtime_transition(RuntimeStatus.DRAFT, RuntimeStatus.READY, actor="USER", reason_code="VALIDATED")
    validate_runtime_transition(RuntimeStatus.READY, RuntimeStatus.RUNNING, actor="USER", reason_code="STARTED")
    validate_runtime_transition(RuntimeStatus.RUNNING, RuntimeStatus.PAUSED, actor="USER", reason_code="PAUSED")
    validate_runtime_transition(RuntimeStatus.PAUSED, RuntimeStatus.RUNNING, actor="USER", reason_code="RESUMED")
    validate_runtime_transition(RuntimeStatus.RUNNING, RuntimeStatus.HALTED, actor="KILL_SWITCH", reason_code="HALTED")
    validate_runtime_transition(RuntimeStatus.HALTED, RuntimeStatus.READY, actor="ADMIN", reason_code="RESET")
    validate_runtime_transition(RuntimeStatus.RUNNING, RuntimeStatus.STOPPED, actor="USER", reason_code="STOPPED")
    # RUNNING -> COMPLETED (explicit end of dataset state, Item 10)
    validate_runtime_transition(RuntimeStatus.RUNNING, RuntimeStatus.COMPLETED, actor="CLOCK", reason_code="DATASET_END")

def test_invalid_runtime_transitions():
    # Terminal runtime states must have no outgoing transitions
    terminal_runtime_states = [
        RuntimeStatus.STOPPED,
        RuntimeStatus.COMPLETED,
        RuntimeStatus.ERROR,
    ]
    for term_state in terminal_runtime_states:
        assert len(VALID_RUNTIME_TRANSITIONS[term_state]) == 0, f"Terminal state {term_state} must have no outgoing transitions"
        with pytest.raises(InvalidRuntimeTransitionError):
            validate_runtime_transition(term_state, RuntimeStatus.RUNNING, actor="USER", reason_code="INVALID")

    with pytest.raises(InvalidRuntimeTransitionError):
        validate_runtime_transition(RuntimeStatus.DRAFT, RuntimeStatus.RUNNING, actor="USER", reason_code="INVALID")
