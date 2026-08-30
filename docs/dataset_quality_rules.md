# Dataset Quality Rules & Status Precedence

## Overall Quality Status Evaluation
Status evaluation follows a strict deterministic precedence:

` FAIL > WARN > PASS `

- **FAIL**: Any issue with severity `ERROR` exists across the entire dataset.
- **WARN**: No `ERROR` exists, but at least one issue with severity `WARNING` exists (e.g. incomplete candle, missing interval, or completed rows < 34).
- **PASS**: Zero `ERROR` and zero `WARNING` issues detected. (Issues with severity `INFO` do not alter PASS status).

## Issue Codes and Invariant Severities

| Issue Code | Severity | Description |
| :--- | :--- | :--- |
| `FILE_UNAVAILABLE` | `ERROR` | Packaged fixture file is missing from filesystem. |
| `CSV_HEADER_INVALID` | `ERROR` | CSV header is empty, has missing columns, or duplicate column names. |
| `ROW_MALFORMED` | `ERROR` | Row cannot form a structurally valid record (wrong column count or unparseable numbers). |
| `TIMESTAMP_INVALID` | `ERROR` | Timestamp string cannot be parsed as an ISO 8601 datetime. |
| `TIMESTAMP_NOT_UTC` | `ERROR` | Timestamp is naive (missing UTC offset `Z` or `+00:00`). |
| `TIMESTAMP_OUT_OF_ORDER` | `ERROR` | Consecutive timestamp is earlier than previous timestamp. |
| `DUPLICATE_TIMESTAMP` | `ERROR` | Multiple rows share the exact same timestamp. |
| `TIMEFRAME_UNSUPPORTED` | `ERROR` | Timeframe string is not among supported intervals. |
| `TIMEFRAME_INTERVAL_MISMATCH` | `ERROR` | Timestamp difference is not a multiple of expected timeframe interval. |
| `MISSING_INTERVAL` | `WARNING` | Timestamp gap detected that is a positive multiple of expected interval. |
| `INSTRUMENT_ID_MISMATCH` | `ERROR` | Row `instrument_id` differs from dataset provenance. |
| `TIMEFRAME_VALUE_MISMATCH` | `ERROR` | Row `timeframe` differs from dataset provenance. |
| `NON_FINITE_VALUE` | `ERROR` | Value is `NaN` or `Infinity`. |
| `NEGATIVE_PRICE` | `ERROR` | Price field (`open`, `high`, `low`, `close`) is $\le 0$. |
| `NEGATIVE_VOLUME` | `ERROR` | Volume field is $< 0$. |
| `OHLC_HIGH_BOUND_INVALID` | `ERROR` | High price is strictly less than $\max(\text{open}, \text{close}, \text{low})$. |
| `OHLC_LOW_BOUND_INVALID` | `ERROR` | Low price is strictly greater than $\min(\text{open}, \text{close}, \text{high})$. |
| `INCOMPLETE_CANDLE_PRESENT` | `WARNING` | Row contains `is_closed=false`. |
| `MANIFEST_COUNT_MISMATCH` | `ERROR` | Completed/total row count does not match manifest entry. |
| `COMPLETED_COUNT_MISMATCH` | `ERROR` | Completed candle count does not match manifest entry. |
| `CHECKSUM_MISMATCH` | `ERROR` | Exact file byte SHA-256 does not match manifest checksum. |
| `MANIFEST_METADATA_MISMATCH` | `ERROR` | Manifest metadata differs from dataset definition. |
| `INSUFFICIENT_DATA_FOR_WARMUP`| `WARNING` | Completed row count is $< 34$ (recommended warm-up for default MACD). |
| `DATASET_ROW_LIMIT_EXCEEDED` | `ERROR` | Total CSV rows exceed maximum limit ($5,000$). |
