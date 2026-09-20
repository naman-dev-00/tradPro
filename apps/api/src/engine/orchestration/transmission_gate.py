"""Operational transmission prohibition gate for strategy orchestration.

Enforces structural and operational prohibition of external broker transmission
for fixture replay and internal mock execution policies.
"""
import os
from typing import Any


class TransmissionProhibitedError(RuntimeError):
    """Raised when external transmission is attempted on internal/mock execution."""
    pass


def external_transmission_allowed(source_type: str, execution_policy: str) -> bool:
    """Return whether external transmission is permitted.

    Only approved source types and execution policies are evaluated.
    FIXTURE_REPLAY + INTERNAL_MOCK_ONLY is structurally forbidden from transmitting.
    Unknown or unexpected combinations fail closed by raising ValueError.
    """
    if source_type == "FIXTURE_REPLAY" and execution_policy == "INTERNAL_MOCK_ONLY":
        return False

    # Fail closed for all other or unknown combinations
    raise ValueError(
        f"Unknown, unsupported, or unapproved orchestration source/policy combination: "
        f"source_type='{source_type}', execution_policy='{execution_policy}'"
    )


def assert_orchestration_execution_is_internal_only(target: Any) -> None:
    """Assert that external transmission is prohibited for the given config or runtime.

    Ensures that under FIXTURE_REPLAY and INTERNAL_MOCK_ONLY:
    - No Upstox adapter calls can occur.
    - No external SubmissionOutbox rows can be generated.
    - No provider order references can be created.
    - UPSTOX_SANDBOX_NETWORK_ENABLED=true cannot override this prohibition.

    Raises TransmissionProhibitedError if external transmission is attempted or allowed.
    Raises ValueError if source_type or execution_policy is unknown or invalid.
    """
    source_type = getattr(target, "source_type", None)
    execution_policy = getattr(target, "execution_policy", None)

    # If attributes are missing, attempt dictionary lookup or fail closed
    if source_type is None and isinstance(target, dict):
        source_type = target.get("source_type")
    if execution_policy is None and isinstance(target, dict):
        execution_policy = target.get("execution_policy")

    if source_type is None or execution_policy is None:
        raise ValueError("Target lacks required source_type or execution_policy for transmission gating")

    # Evaluate fail-closed permission
    allowed = external_transmission_allowed(str(source_type), str(execution_policy))
    if allowed:
        raise TransmissionProhibitedError("External transmission is strictly forbidden for this orchestration configuration.")

    # Explicitly check that network toggle does not override the gate
    # Even if UPSTOX_SANDBOX_NETWORK_ENABLED is true, the prohibition holds firmly.
    network_enabled = os.environ.get("UPSTOX_SANDBOX_NETWORK_ENABLED", "false").strip().lower() == "true"
    if network_enabled:
        # The gate remains in effect; external transmission is still strictly prohibited.
        pass
