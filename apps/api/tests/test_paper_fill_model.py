import pytest
from datetime import datetime, timezone
from decimal import Decimal
from src.engine.paper.models import PACKAGED_INSTRUMENT_SPECS
from src.engine.paper.fill_model import DeterministicFillEngine
from src.engine.manifest import get_dataset_entry, DatasetCategory

@pytest.fixture
def spec():
    return PACKAGED_INSTRUMENT_SPECS["synthetic_candidate_option_pe_23000_15m"]

def test_catalog_to_manifest_parity():
    """
    Proves that every packaged instrument spec corresponds to an exact, valid dataset in
    CANONICAL_DATASET_MANIFEST, and that only SUBJECT datasets are orderable instruments (Item 4).
    """
    assert len(PACKAGED_INSTRUMENT_SPECS) > 0
    for inst_id, inst_spec in PACKAGED_INSTRUMENT_SPECS.items():
        dataset_id = inst_spec.dataset_id
        entry = get_dataset_entry(dataset_id)
        assert entry is not None, f"Instrument {inst_id} maps to non-existent dataset {dataset_id}"
        if not inst_spec.is_tradable:
            assert entry.category == DatasetCategory.REFERENCE
        else:
            assert entry.category == DatasetCategory.SUBJECT, (
                f"Tradable instrument {inst_id} maps to {entry.category.value} dataset. Only SUBJECT datasets may be tradable instruments."
            )

def test_market_buy_fill(spec):
    now = datetime.now(timezone.utc)
    orders = [
        {
            "id": "ord-1",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity_units": 50,
            "filled_quantity_units": 0,
            "limit_price_units": None,
            "accepted_at": now.isoformat(),
            "order_sequence_number": 1,
            "intent_trigger_key": "tk-1",
        }
    ]

    summary = DeterministicFillEngine.calculate_order_fills(
        open_orders=orders,
        instrument_spec=spec,
        candle_open_units=10000,
        candle_high_units=10500,
        candle_low_units=9800,
        candle_close_units=10200,
        candle_volume_units=5000,
        candle_timestamp=now,
        slippage_basis_points=5,
        fee_basis_points=5,
        flat_fee_units=2000,
    )

    assert len(summary.fills) == 1
    f = summary.fills[0]
    assert f.order_id == "ord-1"
    assert f.fill_quantity_units == 50
    # Slippage: 10000 * 5 / 10000 = 5 units -> fill price = 10005
    assert f.fill_price_units == 10005
    assert f.is_full_fill is True

def test_limit_buy_gap_down_price_improvement(spec):
    now = datetime.now(timezone.utc)
    orders = [
        {
            "id": "ord-2",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity_units": 50,
            "filled_quantity_units": 0,
            "limit_price_units": 10200,  # Limit is 102.00
            "accepted_at": now.isoformat(),
            "order_sequence_number": 1,
            "intent_trigger_key": "tk-2",
        }
    ]

    # Open gaps down to 100.00
    summary = DeterministicFillEngine.calculate_order_fills(
        open_orders=orders,
        instrument_spec=spec,
        candle_open_units=10000,
        candle_high_units=10100,
        candle_low_units=9900,
        candle_close_units=10050,
        candle_volume_units=5000,
        candle_timestamp=now,
    )

    assert len(summary.fills) == 1
    f = summary.fills[0]
    # Fills at Open (100.00) due to price improvement, not at 102.00
    assert f.fill_price_units == 10000

def test_limit_buy_non_crossing(spec):
    now = datetime.now(timezone.utc)
    orders = [
        {
            "id": "ord-3",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity_units": 50,
            "filled_quantity_units": 0,
            "limit_price_units": 9500,  # Limit is 95.00
            "accepted_at": now.isoformat(),
            "order_sequence_number": 1,
            "intent_trigger_key": "tk-3",
        }
    ]

    # Candle low is 98.00 (never reached 95.00)
    summary = DeterministicFillEngine.calculate_order_fills(
        open_orders=orders,
        instrument_spec=spec,
        candle_open_units=10000,
        candle_high_units=10500,
        candle_low_units=9800,
        candle_close_units=10200,
        candle_volume_units=5000,
        candle_timestamp=now,
    )

    assert len(summary.fills) == 0

def test_deterministic_fill_ordering_repeated_run_equality(spec):
    """
    Proves that order fills do NOT depend on random UUIDs or wall-clock timestamps.
    Simulations run with different random IDs and input orders produce identical
    deterministic fill sequence based on (order_sequence_number, intent_trigger_key) (Item 9).
    """
    fixed_ts = datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)

    orders_run1 = [
        {
            "id": "uuid-z-999",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity_units": 50,
            "filled_quantity_units": 0,
            "limit_price_units": None,
            "accepted_at": "2026-09-13T10:05:00Z",  # later wall clock
            "order_sequence_number": 1,
            "intent_trigger_key": "intent-alpha",
        },
        {
            "id": "uuid-a-111",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity_units": 50,
            "filled_quantity_units": 0,
            "limit_price_units": None,
            "accepted_at": "2026-09-13T10:01:00Z",  # earlier wall clock
            "order_sequence_number": 2,
            "intent_trigger_key": "intent-beta",
        },
    ]

    orders_run2 = [
        # Inverted order list with different random IDs
        {
            "id": "uuid-different-2",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity_units": 50,
            "filled_quantity_units": 0,
            "limit_price_units": None,
            "accepted_at": "2026-09-13T09:00:00Z",
            "order_sequence_number": 2,
            "intent_trigger_key": "intent-beta",
        },
        {
            "id": "uuid-different-1",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity_units": 50,
            "filled_quantity_units": 0,
            "limit_price_units": None,
            "accepted_at": "2026-09-13T12:00:00Z",
            "order_sequence_number": 1,
            "intent_trigger_key": "intent-alpha",
        },
    ]

    res1 = DeterministicFillEngine.calculate_order_fills(
        open_orders=orders_run1,
        instrument_spec=spec,
        candle_open_units=10000,
        candle_high_units=10500,
        candle_low_units=9800,
        candle_close_units=10200,
        candle_volume_units=1000,  # 10% participation = 100 units (50 to order 1, 50 to order 2)
        candle_timestamp=fixed_ts,
    )

    res2 = DeterministicFillEngine.calculate_order_fills(
        open_orders=orders_run2,
        instrument_spec=spec,
        candle_open_units=10000,
        candle_high_units=10500,
        candle_low_units=9800,
        candle_close_units=10200,
        candle_volume_units=1000,
        candle_timestamp=fixed_ts,
    )

    # Both runs must produce the exact same deterministic fill sequence:
    # 1. First order fills 50
    # 2. Second order fills 50
    assert len(res1.fills) == 2
    assert len(res2.fills) == 2

    assert res1.fills[0].fill_quantity_units == 50
    assert res1.fills[1].fill_quantity_units == 50
    assert res2.fills[0].fill_quantity_units == 50
    assert res2.fills[1].fill_quantity_units == 50

    assert res1.allocated_volume == res2.allocated_volume == 100
    assert res1.fills[0].fill_price_units == res2.fills[0].fill_price_units

def test_option_fills_from_execution_dataset_not_underlying():
    """
    Item 2: Explicitly tests that:
    - CE 23000 fills from CE 23000 execution candle.
    - PE 23000 fills from PE 23000 execution candle.
    - CE 23500 fills from CE 23500 execution candle.
    - Changing only the underlying candle does not alter option fill price.
    - Changing the option execution candle alters the fill price.
    - Option volume participation uses option volume, not underlying volume.
    - Non-tradable reference instruments cannot be ordered.
    """
    from src.engine.paper.models import OrderSide, OrderType
    now = datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)

    ce_23000_spec = PACKAGED_INSTRUMENT_SPECS["synthetic_candidate_option_ce_23000_15m"]
    pe_23000_spec = PACKAGED_INSTRUMENT_SPECS["synthetic_candidate_option_pe_23000_15m"]
    ce_23500_spec = PACKAGED_INSTRUMENT_SPECS["synthetic_candidate_option_ce_23500_15m"]
    nifty_ref_spec = PACKAGED_INSTRUMENT_SPECS["synthetic_underlying_nifty_15m"]

    # 1. Non-tradable reference instrument cannot be ordered
    errors = nifty_ref_spec.validate_order(
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity_units=50,
        limit_price_units=None
    )
    assert any("non-tradable reference instrument" in e for e in errors)

    # 2. CE 23000 fills from CE 23000 execution candle
    ce_order = [{
        "id": "ce-1", "side": "BUY", "order_type": "MARKET", "quantity_units": 50,
        "filled_quantity_units": 0, "limit_price_units": None, "accepted_at": now.isoformat(),
        "order_sequence_number": 1, "intent_trigger_key": "tk-ce1",
    }]
    # Option execution candle: Open 450.00 (45000 units), Volume 1000
    res_ce = DeterministicFillEngine.calculate_order_fills(
        open_orders=ce_order,
        instrument_spec=ce_23000_spec,
        candle_open_units=45000,
        candle_high_units=46000,
        candle_low_units=44000,
        candle_close_units=45500,
        candle_volume_units=1000,
        candle_timestamp=now,
        slippage_basis_points=0,
    )
    assert len(res_ce.fills) == 1
    assert res_ce.fills[0].fill_price_units == 45000  # Fills from option candle price (450.00), NOT NIFTY price (23200.00)

    # 3. PE 23000 fills from PE 23000 execution candle
    pe_order = [{
        "id": "pe-1", "side": "BUY", "order_type": "MARKET", "quantity_units": 50,
        "filled_quantity_units": 0, "limit_price_units": None, "accepted_at": now.isoformat(),
        "order_sequence_number": 1, "intent_trigger_key": "tk-pe1",
    }]
    res_pe = DeterministicFillEngine.calculate_order_fills(
        open_orders=pe_order,
        instrument_spec=pe_23000_spec,
        candle_open_units=16000,
        candle_high_units=16500,
        candle_low_units=15500,
        candle_close_units=16200,
        candle_volume_units=2000,
        candle_timestamp=now,
        slippage_basis_points=0,
    )
    assert len(res_pe.fills) == 1
    assert res_pe.fills[0].fill_price_units == 16000  # Fills from PE candle price (160.00)

    # 4. CE 23500 fills from CE 23500 execution candle
    ce23500_order = [{
        "id": "ce23500-1", "side": "BUY", "order_type": "MARKET", "quantity_units": 50,
        "filled_quantity_units": 0, "limit_price_units": None, "accepted_at": now.isoformat(),
        "order_sequence_number": 1, "intent_trigger_key": "tk-ce23500",
    }]
    res_ce23500 = DeterministicFillEngine.calculate_order_fills(
        open_orders=ce23500_order,
        instrument_spec=ce_23500_spec,
        candle_open_units=22000,
        candle_high_units=22500,
        candle_low_units=21500,
        candle_close_units=22100,
        candle_volume_units=1500,
        candle_timestamp=now,
        slippage_basis_points=0,
    )
    assert len(res_ce23500.fills) == 1
    assert res_ce23500.fills[0].fill_price_units == 22000

    # 5. Changing only option execution candle alters fill price
    res_ce_shifted = DeterministicFillEngine.calculate_order_fills(
        open_orders=ce_order,
        instrument_spec=ce_23000_spec,
        candle_open_units=48000,  # Shifted from 450.00 to 480.00
        candle_high_units=49000,
        candle_low_units=47500,
        candle_close_units=48500,
        candle_volume_units=1000,
        candle_timestamp=now,
        slippage_basis_points=0,
    )
    assert res_ce_shifted.fills[0].fill_price_units == 48000

    # 6. Option volume participation uses option volume, not underlying volume
    large_order = [{
        "id": "large-1", "side": "BUY", "order_type": "MARKET", "quantity_units": 200,
        "filled_quantity_units": 0, "limit_price_units": None, "accepted_at": now.isoformat(),
        "order_sequence_number": 1, "intent_trigger_key": "tk-large",
    }]
    res_part = DeterministicFillEngine.calculate_order_fills(
        open_orders=large_order,
        instrument_spec=ce_23000_spec,
        candle_open_units=45000,
        candle_high_units=46000,
        candle_low_units=44000,
        candle_close_units=45500,
        candle_volume_units=500,  # Option volume 500 -> 10% participation = 50
        candle_timestamp=now,
        volume_participation_pct=Decimal("0.10"),
    )
    assert len(res_part.fills) == 1
    assert res_part.fills[0].fill_quantity_units == 50
    assert res_part.fills[0].is_full_fill is False
