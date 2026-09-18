"""Pure acceptance boundary. This module does not import datasets or perform I/O."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from .models import CompletedCandle, MAX_UNITS, utc


def scaled_units(value: str | Decimal | int, scale: int) -> int:
    """Exact conversion: excess precision is rejected, never rounded."""
    if type(scale) is not int or not 0 <= scale <= 8:
        raise ValueError("Scale must be an integer between zero and eight")
    if isinstance(value, bool) or not isinstance(value, (str, Decimal, int)):
        raise ValueError("Use an exact decimal string, Decimal or integer; floats are forbidden")
    if len(str(value)) > 100:
        raise ValueError("Numeric input exceeds limit")
    try:
        number = Decimal(value)
        if not number.is_finite() or number.copy_abs() > MAX_UNITS:
            raise ValueError("Non-finite or oversized numeric input")
        sign, digits, exponent = number.as_tuple()
        coefficient = int("".join(str(digit) for digit in digits))
        power = exponent + scale
        if coefficient == 0:
            return 0
        if power < 0:
            if -power > len(digits) or coefficient % (10 ** -power):
                raise ValueError("Value has excess fractional precision")
            coefficient //= 10 ** -power
        else:
            if len(digits) + power > 16:
                raise ValueError("Value exceeds unit bounds")
            coefficient *= 10 ** power
        if coefficient > MAX_UNITS:
            raise ValueError("Value exceeds unit bounds")
        return -coefficient if sign else coefficient
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError("Invalid numeric input") from exc


def accept_completed_candle(payload: dict, *, snapshot, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> CompletedCandle:
    """Validate at acceptance time; future clocks cannot be supplied in the payload."""
    now = utc(clock())
    candle = CompletedCandle.model_validate(payload)
    if snapshot is not None:
        for field in ("owner_id", "runtime_id", "source_namespace", "source_type", "timeframe",
                      "source_policy_version", "alignment_offset_seconds"):
            if getattr(candle, field) != getattr(snapshot, field):
                raise ValueError("Candle disagrees with frozen source configuration")
    if candle.close_at > now or candle.received_at > now:
        raise ValueError("Future candle close or receipt is forbidden")
    return candle
