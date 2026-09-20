"""Fixture-only orchestration contracts; no scheduler or broker integration."""

from .transmission_gate import (
    TransmissionProhibitedError,
    external_transmission_allowed,
    assert_orchestration_execution_is_internal_only,
)

__all__ = [
    "TransmissionProhibitedError",
    "external_transmission_allowed",
    "assert_orchestration_execution_is_internal_only",
]
