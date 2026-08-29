import os
from enum import Enum
from typing import List, Dict, Optional, Any
from pydantic import BaseModel, ConfigDict
from src.engine.models import Candle
from src.engine.csv_loader import load_candles_from_csv

class DatasetCategory(str, Enum):
    REFERENCE = "REFERENCE"
    SUBJECT = "SUBJECT"

class DatasetManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    display_name: str
    description: str
    instrument_id: str
    timeframe: str
    candle_count: int
    completed_candle_count: int
    category: DatasetCategory
    is_synthetic: bool = True

FIXTURES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures"))

MANIFEST_METADATA: List[Dict[str, Any]] = [
    {
        "dataset_id": "synthetic_underlying_nifty_15m",
        "display_name": "NIFTY Underlying Index (15m)",
        "description": "Synthetic underlying NIFTY index 15-minute OHLCV reference series.",
        "filename": "synthetic_underlying_nifty_15m.csv",
        "instrument_id": "NIFTY",
        "timeframe": "15m",
        "category": DatasetCategory.REFERENCE,
    },
    {
        "dataset_id": "synthetic_candidate_option_ce_23000_15m",
        "display_name": "NIFTY 23000 Call Option (15m)",
        "description": "Synthetic 23000 CE option candidate 15-minute OHLCV subject series.",
        "filename": "synthetic_candidate_option_ce_23000_15m.csv",
        "instrument_id": "NIFTY_23000_CE",
        "timeframe": "15m",
        "category": DatasetCategory.SUBJECT,
    },
    {
        "dataset_id": "synthetic_candidate_option_pe_23000_15m",
        "display_name": "NIFTY 23000 Put Option (15m)",
        "description": "Synthetic 23000 PE option candidate 15-minute OHLCV subject series.",
        "filename": "synthetic_candidate_option_pe_23000_15m.csv",
        "instrument_id": "NIFTY_23000_PE",
        "timeframe": "15m",
        "category": DatasetCategory.SUBJECT,
    },
    {
        "dataset_id": "synthetic_candidate_option_ce_23500_15m",
        "display_name": "NIFTY 23500 Call Option (15m)",
        "description": "Synthetic 23500 CE option candidate 15-minute OHLCV subject series.",
        "filename": "synthetic_candidate_option_ce_23500_15m.csv",
        "instrument_id": "NIFTY_23500_CE",
        "timeframe": "15m",
        "category": DatasetCategory.SUBJECT,
    },
    {
        "dataset_id": "synthetic_short_insufficient_5m",
        "display_name": "Short Series - Insufficient Data (5m)",
        "description": "Short 3-candle 5m subject series for timeframe mismatch / insufficient data testing.",
        "filename": "synthetic_short_insufficient_5m.csv",
        "instrument_id": "SHORT_SERIES",
        "timeframe": "5m",
        "category": DatasetCategory.SUBJECT,
    },
    {
        "dataset_id": "synthetic_with_incomplete_candle_15m",
        "display_name": "Series with Incomplete Candle (15m)",
        "description": "Synthetic 15m subject series containing 1 incomplete candle (is_closed=False) for exclusion verification.",
        "filename": "synthetic_with_incomplete_candle_15m.csv",
        "instrument_id": "INCOMPLETE_SERIES",
        "timeframe": "15m",
        "category": DatasetCategory.SUBJECT,
    },
]

# Cache loaded candles and entries
_MANIFEST_REGISTRY: Dict[str, DatasetManifestEntry] = {}
_CANDLE_CACHE: Dict[str, List[Candle]] = {}
_FILENAME_MAP: Dict[str, str] = {}

def _initialize_manifest_registry():
    seen_ids = set()
    for meta in MANIFEST_METADATA:
        dataset_id = meta["dataset_id"]
        if dataset_id in seen_ids:
            raise ValueError(f"Duplicate manifest dataset ID detected: '{dataset_id}'")
        seen_ids.add(dataset_id)

        filename = meta["filename"]
        filepath = os.path.join(FIXTURES_DIR, filename)
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Packaged fixture CSV file missing for dataset '{dataset_id}'")

        candles = load_candles_from_csv(filepath)
        candle_count = len(candles)
        completed_candle_count = sum(1 for c in candles if c.is_closed)

        entry = DatasetManifestEntry(
            dataset_id=dataset_id,
            display_name=meta["display_name"],
            description=meta["description"],
            instrument_id=meta["instrument_id"],
            timeframe=meta["timeframe"],
            candle_count=candle_count,
            completed_candle_count=completed_candle_count,
            category=meta["category"],
            is_synthetic=True,
        )
        _MANIFEST_REGISTRY[dataset_id] = entry
        _CANDLE_CACHE[dataset_id] = candles
        _FILENAME_MAP[dataset_id] = filename

_initialize_manifest_registry()

def get_dataset_manifest() -> List[DatasetManifestEntry]:
    # Returns manifest entries in stable pre-defined order
    return [_MANIFEST_REGISTRY[meta["dataset_id"]] for meta in MANIFEST_METADATA]

def get_dataset_entry(dataset_id: str) -> Optional[DatasetManifestEntry]:
    return _MANIFEST_REGISTRY.get(dataset_id)

import copy

def load_dataset_candles(dataset_id: str) -> List[Candle]:
    if dataset_id not in _CANDLE_CACHE:
        raise KeyError(f"Unknown synthetic dataset ID '{dataset_id}'")
    # Return copies to prevent callers mutating cache
    return [copy.copy(c) for c in _CANDLE_CACHE[dataset_id]]
