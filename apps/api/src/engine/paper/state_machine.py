from typing import Set, Dict, Tuple, Optional, Any
from src.engine.paper.models import OrderStatus, RuntimeStatus

class InvalidOrderTransitionError(Exception):
    def __init__(self, current_status: OrderStatus, requested_status: OrderStatus, reason: str = ""):
        super().__init__(f"Invalid order transition from {current_status.value} to {requested_status.value}. {reason}")
        self.current_status = current_status
        self.requested_status = requested_status
        self.reason = reason

class InvalidRuntimeTransitionError(Exception):
    def __init__(self, current_status: RuntimeStatus, requested_status: RuntimeStatus, reason: str = ""):
        super().__init__(f"Invalid runtime transition from {current_status.value} to {requested_status.value}. {reason}")
        self.current_status = current_status
        self.requested_status = requested_status
        self.reason = reason

# Allowed Order Transitions
VALID_ORDER_TRANSITIONS: Dict[OrderStatus, Set[OrderStatus]] = {
    OrderStatus.CREATED: {
        OrderStatus.ACCEPTED,
        OrderStatus.PENDING_SUBMISSION,
        OrderStatus.RISK_REJECTED,
        OrderStatus.REJECTED,
        OrderStatus.ERROR,
    },
    OrderStatus.PENDING_SUBMISSION: {
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.PROVIDER_REJECTED,
        OrderStatus.RECONCILIATION_REQUIRED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.ERROR,
    },
    OrderStatus.ACCEPTED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.ERROR,
    },
    OrderStatus.ACKNOWLEDGED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.RECONCILIATION_REQUIRED,
        OrderStatus.ERROR,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.ERROR,
    },
    OrderStatus.CANCEL_PENDING: {
        OrderStatus.ACKNOWLEDGED,  # Safe dead-letter revert when cancel fails before transmission
        OrderStatus.CANCELLED,
        OrderStatus.FILLED,  # Race: fill occurred while cancel was pending
        OrderStatus.RECONCILIATION_REQUIRED,
        OrderStatus.ERROR,
    },
    OrderStatus.RECONCILIATION_REQUIRED: {
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.CANCELLED,
        OrderStatus.PROVIDER_REJECTED,
        OrderStatus.ERROR,
    },
    # Terminal states have empty sets
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.PROVIDER_REJECTED: set(),
    OrderStatus.EXPIRED: set(),
    OrderStatus.RISK_REJECTED: set(),
    OrderStatus.ERROR: set(),
}

def validate_order_transition(
    current_status: OrderStatus,
    new_status: OrderStatus,
    actor: str,
    reason_code: str
) -> None:
    if not actor:
        raise ValueError("Actor must be specified for an order transition.")
    if not reason_code:
        raise ValueError("Reason code must be specified for an order transition.")

    allowed_next = VALID_ORDER_TRANSITIONS.get(current_status, set())
    if new_status not in allowed_next:
        raise InvalidOrderTransitionError(
            current_status=current_status,
            requested_status=new_status,
            reason=f"Permitted next states from {current_status.value}: {[s.value for s in allowed_next]}"
        )

# Allowed Runtime Transitions
VALID_RUNTIME_TRANSITIONS: Dict[RuntimeStatus, Set[RuntimeStatus]] = {
    RuntimeStatus.DRAFT: {
        RuntimeStatus.READY,
    },
    RuntimeStatus.READY: {
        RuntimeStatus.RUNNING,
    },
    RuntimeStatus.RUNNING: {
        RuntimeStatus.PAUSED,
        RuntimeStatus.HALTED,
        RuntimeStatus.STOPPED,
        RuntimeStatus.COMPLETED,
        RuntimeStatus.ERROR,
    },
    RuntimeStatus.PAUSED: {
        RuntimeStatus.RUNNING,
        RuntimeStatus.HALTED,
        RuntimeStatus.STOPPED,
        RuntimeStatus.ERROR,
    },
    RuntimeStatus.HALTED: {
        RuntimeStatus.READY,   # Authorized reset restores to READY
        RuntimeStatus.STOPPED, # Terminating a halted runtime
        RuntimeStatus.ERROR,
    },
    RuntimeStatus.STOPPED: set(),    # Terminal
    RuntimeStatus.COMPLETED: set(),  # Terminal after final candle
    RuntimeStatus.ERROR: set(),      # Terminal
}

def validate_runtime_transition(
    current_status: RuntimeStatus,
    new_status: RuntimeStatus,
    actor: str,
    reason_code: str
) -> None:
    if not actor:
        raise ValueError("Actor must be specified for a runtime transition.")
    if not reason_code:
        raise ValueError("Reason code must be specified for a runtime transition.")

    allowed_next = VALID_RUNTIME_TRANSITIONS.get(current_status, set())
    if new_status not in allowed_next:
        raise InvalidRuntimeTransitionError(
            current_status=current_status,
            requested_status=new_status,
            reason=f"Permitted next states from {current_status.value}: {[s.value for s in allowed_next]}"
        )
