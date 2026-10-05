import hashlib
from typing import Sequence
from .contracts import MarketDataCandle


def compute_market_data_fingerprint(candles: Sequence[MarketDataCandle]) -> str:
    """
    Computes a deterministic SHA-256 fingerprint from a sequence of normalized candles.
    Ensures identical candle inputs yield identical digests, and any modification or reordering
    alters the digest.
    """
    if not candles:
        return hashlib.sha256(b"EMPTY_SERIES").hexdigest()

    lines = []
    for c in candles:
        line = f"{c.timestamp.isoformat()}:{c.open_units}:{c.high_units}:{c.low_units}:{c.close_units}:{c.volume}"
        lines.append(line)

    payload = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
