from typing import List, Dict, Any, Optional, Union
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from src.engine.models import Candle, IndicatorResult, SUPPORTED_TIMEFRAMES
from src.engine import indicators

router = APIRouter(prefix="/indicators", tags=["indicators"])

MAX_CANDLES_PER_REQUEST = 5000

SUPPORTED_INDICATORS_METADATA = [
    {
        "name": "PRICE",
        "description": "Closing price of completed candle",
        "parameters": {}
    },
    {
        "name": "SMA",
        "description": "Simple Moving Average",
        "parameters": {
            "period": {"type": "integer", "default": 20, "minimum": 1}
        }
    },
    {
        "name": "EMA",
        "description": "Exponential Moving Average",
        "parameters": {
            "period": {"type": "integer", "default": 20, "minimum": 1}
        }
    },
    {
        "name": "RSI",
        "description": "Relative Strength Index (Wilder's RSI)",
        "parameters": {
            "period": {"type": "integer", "default": 14, "minimum": 1}
        }
    },
    {
        "name": "MACD",
        "description": "Moving Average Convergence Divergence",
        "parameters": {
            "fast_period": {"type": "integer", "default": 12, "minimum": 1},
            "slow_period": {"type": "integer", "default": 26, "minimum": 1},
            "signal_period": {"type": "integer", "default": 9, "minimum": 1}
        }
    },
    {
        "name": "PIVOT",
        "description": "Classic Floor Pivot Points (Pivot, S1, R1)",
        "parameters": {}
    },
    {
        "name": "VOLUME",
        "description": "Candle volume",
        "parameters": {}
    },
    {
        "name": "AVERAGE_VOLUME",
        "description": "Simple Moving Average of volume",
        "parameters": {
            "period": {"type": "integer", "default": 20, "minimum": 1}
        }
    }
]


class CandleInputSchema(BaseModel):
    timestamp: datetime
    instrument_id: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = True


class CalculateIndicatorRequestSchema(BaseModel):
    candles: List[CandleInputSchema]
    indicator: str
    params: Dict[str, Any] = Field(default_factory=dict)


class IndicatorResultOutputSchema(BaseModel):
    timestamp: datetime
    indicator: str
    value: Optional[Union[float, Dict[str, Optional[float]]]]
    available: bool
    warmup_remaining: int


class CalculateIndicatorResponseSchema(BaseModel):
    indicator: str
    params: Dict[str, Any]
    results: List[IndicatorResultOutputSchema]


import os
import csv
from src.engine.csv_loader import load_candles_from_csv

FIXTURES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures"))

PACKAGED_DATASETS = {
    "synthetic_underlying_nifty_15m": {
        "id": "synthetic_underlying_nifty_15m",
        "name": "NIFTY Underlying Index (15m)",
        "description": "Synthetic underlying index 15-minute OHLCV series (35 completed candles).",
        "filename": "synthetic_underlying_nifty_15m.csv",
        "instrument_id": "NIFTY",
        "timeframe": "15m"
    },
    "synthetic_candidate_option_ce_23000_15m": {
        "id": "synthetic_candidate_option_ce_23000_15m",
        "name": "NIFTY 23000 Call Option (15m)",
        "description": "Synthetic 23000 CE option candidate 15-minute OHLCV series.",
        "filename": "synthetic_candidate_option_ce_23000_15m.csv",
        "instrument_id": "NIFTY_23000_CE",
        "timeframe": "15m"
    },
    "synthetic_candidate_option_pe_23000_15m": {
        "id": "synthetic_candidate_option_pe_23000_15m",
        "name": "NIFTY 23000 Put Option (15m)",
        "description": "Synthetic 23000 PE option candidate 15-minute OHLCV series.",
        "filename": "synthetic_candidate_option_pe_23000_15m.csv",
        "instrument_id": "NIFTY_23000_PE",
        "timeframe": "15m"
    },
    "synthetic_candidate_option_ce_23500_15m": {
        "id": "synthetic_candidate_option_ce_23500_15m",
        "name": "NIFTY 23500 Call Option (15m)",
        "description": "Synthetic 23500 CE option candidate 15-minute OHLCV series.",
        "filename": "synthetic_candidate_option_ce_23500_15m.csv",
        "instrument_id": "NIFTY_23500_CE",
        "timeframe": "15m"
    },
    "synthetic_short_insufficient_5m": {
        "id": "synthetic_short_insufficient_5m",
        "name": "Short Series - Insufficient Data (5m)",
        "description": "Short 3-candle series intentionally having insufficient data for warm-up testing.",
        "filename": "synthetic_short_insufficient_5m.csv",
        "instrument_id": "SHORT_SERIES",
        "timeframe": "5m"
    },
    "synthetic_with_incomplete_candle_15m": {
        "id": "synthetic_with_incomplete_candle_15m",
        "name": "Series with Incomplete Candle (15m)",
        "description": "Dataset containing 10 candles with 1 incomplete candle (is_closed=False) to verify exclusion.",
        "filename": "synthetic_with_incomplete_candle_15m.csv",
        "instrument_id": "INCOMPLETE_SERIES",
        "timeframe": "15m"
    }
}


@router.get("/supported")
def get_supported_indicators():
    """
    Returns supported indicators, parameters, and defaults.
    For educational and development inspection only.
    """
    return {"indicators": SUPPORTED_INDICATORS_METADATA}


@router.get("/datasets")
def get_synthetic_datasets():
    """
    Returns list of packaged synthetic CSV datasets.
    For educational and development inspection only.
    """
    return {
        "datasets": [
            {
                "id": meta["id"],
                "name": meta["name"],
                "description": meta["description"],
                "instrument_id": meta["instrument_id"],
                "timeframe": meta["timeframe"]
            }
            for meta in PACKAGED_DATASETS.values()
        ]
    }


@router.get("/datasets/{dataset_id}")
def get_synthetic_dataset_detail(dataset_id: str):
    """
    Returns candle data for a specific packaged synthetic dataset by dataset_id.
    Never accepts arbitrary filesystem paths.
    """
    if dataset_id not in PACKAGED_DATASETS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Synthetic dataset '{dataset_id}' not found. Available datasets: {sorted(list(PACKAGED_DATASETS.keys()))}"
        )

    meta = PACKAGED_DATASETS[dataset_id]
    file_path = os.path.join(FIXTURES_DIR, meta["filename"])

    total_count = 0
    with open(file_path, "r", encoding="utf-8") as f:
        lines = [l for l in f if not l.strip().startswith("#")]
        reader = csv.DictReader(lines)
        total_count = sum(1 for _ in reader)

    completed_candles = load_candles_from_csv(file_path)
    excluded_count = total_count - len(completed_candles)

    return {
        "id": meta["id"],
        "name": meta["name"],
        "description": meta["description"],
        "instrument_id": meta["instrument_id"],
        "timeframe": meta["timeframe"],
        "total_candles": total_count,
        "completed_candles": len(completed_candles),
        "excluded_incomplete_candles": excluded_count,
        "candles": [
            {
                "timestamp": c.timestamp.isoformat(),
                "instrument_id": c.instrument_id,
                "timeframe": c.timeframe,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "is_closed": c.is_closed
            }
            for c in completed_candles
        ]
    }


@router.post("/calculate", response_model=CalculateIndicatorResponseSchema)
def calculate_indicator(payload: CalculateIndicatorRequestSchema):
    """
    Calculates deterministic indicator values for a given candle series.
    For educational and development inspection only.
    """
    if len(payload.candles) > MAX_CANDLES_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Candle count ({len(payload.candles)}) exceeds maximum limit of {MAX_CANDLES_PER_REQUEST} candles per request."
        )

    indicator_name = payload.indicator.upper().strip()
    valid_names = {item["name"] for item in SUPPORTED_INDICATORS_METADATA}

    if indicator_name not in valid_names:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown indicator '{payload.indicator}'. Supported indicators: {sorted(list(valid_names))}"
        )

    # Convert Pydantic CandleInputs to domain Candle models
    domain_candles: List[Candle] = []
    for idx, c_in in enumerate(payload.candles):
        try:
            ts = c_in.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)

            c = Candle(
                timestamp=ts,
                instrument_id=c_in.instrument_id,
                timeframe=c_in.timeframe,
                open=c_in.open,
                high=c_in.high,
                low=c_in.low,
                close=c_in.close,
                volume=c_in.volume,
                is_closed=c_in.is_closed
            )
            domain_candles.append(c)
        except ValueError as ve:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid candle at index {idx}: {str(ve)}"
            )

    # Dispatch to calculation engine
    try:
        if indicator_name == "PRICE":
            _check_unexpected_params(payload.params, set())
            res = indicators.calculate_price(domain_candles)

        elif indicator_name == "SMA":
            _check_unexpected_params(payload.params, {"period"})
            period = int(payload.params.get("period", 20))
            res = indicators.calculate_sma(domain_candles, period=period)

        elif indicator_name == "EMA":
            _check_unexpected_params(payload.params, {"period"})
            period = int(payload.params.get("period", 20))
            res = indicators.calculate_ema(domain_candles, period=period)

        elif indicator_name == "RSI":
            _check_unexpected_params(payload.params, {"period"})
            period = int(payload.params.get("period", 14))
            res = indicators.calculate_rsi(domain_candles, period=period)

        elif indicator_name == "MACD":
            _check_unexpected_params(payload.params, {"fast_period", "slow_period", "signal_period"})
            fast_p = int(payload.params.get("fast_period", 12))
            slow_p = int(payload.params.get("slow_period", 26))
            sig_p = int(payload.params.get("signal_period", 9))
            res = indicators.calculate_macd(
                domain_candles, fast_period=fast_p, slow_period=slow_p, signal_period=sig_p
            )

        elif indicator_name == "PIVOT":
            _check_unexpected_params(payload.params, set())
            res = indicators.calculate_pivot(domain_candles)

        elif indicator_name == "VOLUME":
            _check_unexpected_params(payload.params, set())
            res = indicators.calculate_volume(domain_candles)

        elif indicator_name == "AVERAGE_VOLUME":
            _check_unexpected_params(payload.params, {"period"})
            period = int(payload.params.get("period", 20))
            res = indicators.calculate_average_volume(domain_candles, period=period)

        else:
            raise HTTPException(status_code=400, detail=f"Unsupported indicator '{indicator_name}'")

    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Indicator parameter error: {str(ve)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to calculate indicator for provided series."
        )

    out_results = [
        IndicatorResultOutputSchema(
            timestamp=r.timestamp,
            indicator=r.indicator,
            value=r.value,
            available=r.available,
            warmup_remaining=r.warmup_remaining
        )
        for r in res
    ]

    return CalculateIndicatorResponseSchema(
        indicator=indicator_name,
        params=payload.params,
        results=out_results
    )


def _check_unexpected_params(provided_params: Dict[str, Any], allowed_keys: set):
    unexpected = set(provided_params.keys()) - allowed_keys
    if unexpected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unexpected parameters for indicator: {sorted(list(unexpected))}. Allowed parameters: {sorted(list(allowed_keys))}"
        )
