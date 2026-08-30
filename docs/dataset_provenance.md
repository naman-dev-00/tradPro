# Dataset Provenance & Fixture Verification

## Overview
All datasets inspected by TradePro are strictly packaged synthetic educational fixtures. Dataset provenance tracks origin, immutability, server manifest metadata, and SHA-256 fixture checksums.

## Strict Provenance Model
- `is_synthetic: Literal[True]` (guaranteed synthetic data)
- `immutable: Literal[True]` (fixtures cannot be mutated)
- `source_type: Literal["PACKAGED_SYNTHETIC_FIXTURE"]`
- `manifest_version: "1.0.0"`
- `category: DatasetCategory` (`REFERENCE` for underlying indices, `SUBJECT` for option series)

## Packaged Fixtures
1. `synthetic_underlying_nifty_15m`: Reference underlying series (35 completed candles). Expected status: **PASS**.
2. `synthetic_candidate_option_ce_23000_15m`: Subject call option candidate (10 completed candles). Expected status: **WARN** (`INSUFFICIENT_DATA_FOR_WARMUP`).
3. `synthetic_candidate_option_pe_23000_15m`: Subject put option candidate (10 completed candles). Expected status: **WARN** (`INSUFFICIENT_DATA_FOR_WARMUP`).
4. `synthetic_candidate_option_ce_23500_15m`: Subject call option candidate (10 completed candles). Expected status: **WARN** (`INSUFFICIENT_DATA_FOR_WARMUP`).
5. `synthetic_short_insufficient_5m`: Short subject series for timeframe testing (3 completed candles). Expected status: **WARN** (`INSUFFICIENT_DATA_FOR_WARMUP`).
6. `synthetic_with_incomplete_candle_15m`: Subject series containing 1 incomplete candle (`is_closed=false`). Expected status: **WARN** (`INCOMPLETE_CANDLE_PRESENT`, `INSUFFICIENT_DATA_FOR_WARMUP`).
