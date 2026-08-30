import time
import threading
from typing import Dict, List
from fastapi import HTTPException, status, Request

class InMemoryRateLimiter:
    """
    Thread-safe sliding window rate limiter with bounded memory and synchronous on-demand expiration pruning.

    Operational Characteristics & Limitations:
    - Process-Local Scope: Suitable for current single-process/local development and test configuration.
    - Multi-Worker Limitation: In a multi-worker deployment (e.g. multi-process Gunicorn/Uvicorn),
      each worker maintains independent counters. Production multi-worker deployments require a shared
      backing store (e.g., Redis) or gateway-level rate limiting.
    - Synchronous In-Band Pruning: Pruning runs synchronously inside `check_rate_limit` without spawning
      background daemon threads, ensuring clean shutdown and zero thread leaks during testing.
    """
    def __init__(self, max_keys: int = 10000):
        self._lock = threading.Lock()
        self._records: Dict[str, List[float]] = {}
        self._max_keys = max_keys
        self._last_prune = time.time()

    def check_rate_limit(self, key: str, max_requests: int, window_seconds: int = 60) -> None:
        """
        Enforces a sliding-window rate limit for a specific key.
        Raises 429 Too Many Requests with Retry-After header if exceeded.
        """
        now = time.time()
        window_start = now - window_seconds

        with self._lock:
            # Periodic pruning of expired keys if key count is high or every 60 seconds
            if len(self._records) > self._max_keys or (now - self._last_prune > 60):
                self._prune_expired(now)

            timestamps = self._records.get(key, [])
            valid_timestamps = [t for t in timestamps if t > window_start]

            if len(valid_timestamps) >= max_requests:
                oldest = valid_timestamps[0]
                retry_after = max(1, int(oldest + window_seconds - now))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too Many Requests. Please slow down.",
                    headers={"Retry-After": str(retry_after)}
                )

            valid_timestamps.append(now)
            self._records[key] = valid_timestamps

    def _prune_expired(self, now: float) -> None:
        """Removes expired entries across all keys to prevent unbounded memory growth."""
        self._last_prune = now
        stale_keys = []
        for k, ts in self._records.items():
            fresh = [t for t in ts if t > (now - 3600)]
            if not fresh:
                stale_keys.append(k)
            else:
                self._records[k] = fresh
        for k in stale_keys:
            self._records.pop(k, None)

    def clear(self) -> None:
        """Clears all rate limit state for test isolation."""
        with self._lock:
            self._records.clear()

rate_limiter = InMemoryRateLimiter()

def get_client_ip(request: Request) -> str:
    """
    Safely extracts client IP for rate limiting without blindly trusting spoofed headers.
    """
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"
