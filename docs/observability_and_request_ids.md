# Observability & Request-ID Middleware

## Overview
TradePro implements secure structured access logging and request correlation via `ObservabilityMiddleware` (`src/middleware/observability.py`).

## Request-ID Security Model
- **Client Header**: `X-Request-ID`.
- **Validation**: Strict ASCII alphanumeric regex `^[A-Za-z0-9_.-]{1,64}$`.
- **Sanitization**: Any missing, oversized (>64 chars), whitespace, control character, or CRLF header is discarded and replaced with a newly generated UUID v4.
- **Propagation**: `X-Request-ID` is returned in all HTTP response headers (2xx, 4xx, 5xx).

## Safe Structured Logging
- Middleware never reads or logs request bodies, query values, file paths, credentials, or environment secrets.
- Logged fields:
  - `request_id`: Accepted or generated correlation ID.
  - `method`: HTTP method (`GET`, `POST`, etc.).
  - `route`: Normalized route template (e.g. `/api/v1/data-quality/datasets/{dataset_id}`), or `"unresolved"`.
  - `status_code`: Response status integer.
  - `duration_bucket`: Bounded bucket (`<10ms`, `10-50ms`, `50-200ms`, `>200ms`).
