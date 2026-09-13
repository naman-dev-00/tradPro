import pytest
from decimal import Decimal
from src.engine.paper.units import (
    decimal_to_units,
    units_to_decimal,
    quantize_decimal,
    check_int64_bounds,
    INT64_MIN,
    INT64_MAX,
)

def test_decimal_to_units_and_back():
    val = Decimal("234.55")
    scale = 2
    units = decimal_to_units(val, scale)
    assert units == 23455
    back = units_to_decimal(units, scale)
    assert back == val

def test_float_rejection():
    with pytest.raises(TypeError, match="Floating-point values are forbidden"):
        decimal_to_units(234.55, 2)

    with pytest.raises(TypeError, match="Floating-point values are forbidden"):
        quantize_decimal(234.55, 2)

def test_quantization_rounding():
    # Positive half up rounding
    val = Decimal("10.005")
    units = decimal_to_units(val, 2)
    assert units == 1001  # 10.01

    val2 = Decimal("10.004")
    units2 = decimal_to_units(val2, 2)
    assert units2 == 1000  # 10.00

    # Negative half up rounding
    neg_val = Decimal("-10.005")
    neg_units = decimal_to_units(neg_val, 2)
    assert neg_units == -1001

    neg_val2 = Decimal("-10.004")
    neg_units2 = decimal_to_units(neg_val2, 2)
    assert neg_units2 == -1000

def test_zero_and_large_integers():
    assert decimal_to_units(Decimal("0.00"), 2) == 0
    assert units_to_decimal(0, 2) == Decimal("0.00")

    large_val = Decimal("1000000000.50")
    large_units = decimal_to_units(large_val, 2)
    assert large_units == 100000000050
    assert units_to_decimal(large_units, 2) == large_val

def test_signed_64bit_bounds_and_overflow():
    # Max accepted value
    assert check_int64_bounds(INT64_MAX) == INT64_MAX
    assert check_int64_bounds(INT64_MIN) == INT64_MIN

    # One unit beyond limits raises OverflowError
    with pytest.raises(OverflowError, match="exceeds signed 64-bit database bounds"):
        check_int64_bounds(INT64_MAX + 1)

    with pytest.raises(OverflowError, match="exceeds signed 64-bit database bounds"):
        check_int64_bounds(INT64_MIN - 1)

    # Decimal to units overflow
    huge_decimal = Decimal("92233720368547758.08")  # at scale 2, would be 9223372036854775808 > INT64_MAX
    with pytest.raises(OverflowError):
        decimal_to_units(huge_decimal, 2)

def test_large_price_times_quantity_and_fees():
    # 5,000,000 price units (50,000.00) * 100,000 quantity units (100,000 contracts)
    price_units = 5000000
    quantity_units = 100000
    notional = price_units * quantity_units
    assert notional == 500000000000
    assert check_int64_bounds(notional) == notional

    # Fee calculation near bounds
    fee_bps = 5
    fee = (notional * fee_bps) // 10000
    assert fee == 250000000
    assert check_int64_bounds(fee) == fee
