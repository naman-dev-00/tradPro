from decimal import Decimal, ROUND_HALF_UP
from typing import Union

INT64_MIN = -9223372036854775808  # -2**63
INT64_MAX = 9223372036854775807   # 2**63 - 1

def check_int64_bounds(value: int) -> int:
    """
    Validates that a scaled integer falls within the signed 64-bit database bounds.
    Raises OverflowError if outside [-2**63, 2**63 - 1].
    """
    if not isinstance(value, int):
        raise TypeError(f"Units must be integer, got {type(value)}")
    if value < INT64_MIN or value > INT64_MAX:
        raise OverflowError(f"Scaled integer unit {value} exceeds signed 64-bit database bounds [{INT64_MIN}, {INT64_MAX}].")
    return value

def decimal_to_units(value: Union[Decimal, str, int], scale: int) -> int:
    """
    Converts a Decimal (or numeric string / int) to a scaled integer representation.
    Rejects float values to prevent binary floating-point inaccuracies.
    Enforces signed 64-bit bounds.
    """
    if isinstance(value, float):
        raise TypeError("Floating-point values are forbidden for exact monetary/quantity units. Use Decimal or string.")

    if not isinstance(value, Decimal):
        value = Decimal(str(value))

    if scale < 0:
        raise ValueError("Scale cannot be negative.")

    quantizer = Decimal("10") ** (-scale)
    quantized = value.quantize(quantizer, rounding=ROUND_HALF_UP)

    # Scale to integer
    multiplier = 10 ** scale
    scaled = int(quantized * multiplier)
    return check_int64_bounds(scaled)

def units_to_decimal(units: int, scale: int) -> Decimal:
    """
    Converts a scaled integer unit back to a Decimal quantized to scale.
    Enforces signed 64-bit bounds.
    """
    if not isinstance(units, int):
        raise TypeError(f"Units must be integer, got {type(units)}")
    check_int64_bounds(units)
    if scale < 0:
        raise ValueError("Scale cannot be negative.")

    divisor = Decimal(10 ** scale)
    val = Decimal(units) / divisor
    quantizer = Decimal("10") ** (-scale)
    return val.quantize(quantizer, rounding=ROUND_HALF_UP)

def quantize_decimal(value: Union[Decimal, str, int], scale: int) -> Decimal:
    """
    Quantizes a Decimal to the specified scale using ROUND_HALF_UP.
    """
    if isinstance(value, float):
        raise TypeError("Floating-point values are forbidden. Use Decimal or string.")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    quantizer = Decimal("10") ** (-scale)
    return value.quantize(quantizer, rounding=ROUND_HALF_UP)
