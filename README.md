# TradePro Option Strategy & Analytics Workspace

[![CI](https://github.com/naman-dev-00/tradPro/actions/workflows/ci.yml/badge.svg)](https://github.com/naman-dev-00/tradPro/actions/workflows/ci.yml)

TradePro is an educational options strategy-building, indicator analysis, and rule evaluation workspace.

This repository covers **Milestone 1 (Strategy Builder Foundation)**, **Milestone 2A (Candle Data & Indicator Engine)**, and **Milestone 2B (Educational Rule Evaluation Engine & Rule Lab UI)**.

---

## Repository Structure

```
tradepro/
  apps/
    web/             # Next.js, React Flow, TypeScript, Tailwind CSS
    api/             # Python, FastAPI, SQLAlchemy, Pydantic, Indicator Engine
      src/
        engine/      # Pure deterministic indicator functions & candle loader
        fixtures/    # Synthetic educational OHLCV datasets
        routes/      # API endpoints (strategies, health, indicators)
  contracts/
    strategy.schema.json  # Source of truth JSON Schema strategy contract
  docker-compose.yml # Dev orchestration stack
  README.md          # Setup, Indicator Engine specification & CLI guidelines
```

---

## Technical Architecture & Safety Rules

### 1. Database Safety & Fallback Rules
- **Explicit Connection Verification**: Database connection verification occurs during FastAPI startup (`verify_database_connection()`), NOT at module import time.
- **Strict PostgreSQL Errors**: If `DATABASE_URL` is configured (e.g. PostgreSQL), the backend tests the connection on startup. If the database connection fails, the API raises a `RuntimeError` and **never silently falls back to SQLite**.
- **SQLite Fallback**: Automatic SQLite fallback (`sqlite:///./tradepro.db`) occurs **ONLY** when `DATABASE_URL` is absent AND `APP_ENV` is set to `"local"` or `"test"`.
- **Environment Enforcements**: In `"staging"` or `"production"`, missing `DATABASE_URL` immediately raises a `RuntimeError`.
- **Test Isolation**: Automated unit test suites use isolated temporary SQLite databases, preventing state leaks across runs.

---

## Candle Model Specification

Completed OHLCV candles adhere to strict validation rules:
- `open`, `high`, `low`, `close` must be strictly $> 0$.
- `volume` must be $\ge 0$.
- `high` $\ge \max(\text{open}, \text{close}, \text{low})$.
- `low` $\le \min(\text{open}, \text{close}, \text{high})$.
- Timestamps must be timezone-aware (normalized to UTC). Naive timestamps are rejected.
- Timeframe must belong to supported set: `["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w"]`.
- Preprocessing (`preprocess_candle_series`) enforces strict chronological ordering and excludes incomplete candles (`is_closed == False`).

---

## Indicator Engine & Calculation Formulas

All indicators are pure, deterministic functions operating strictly on completed candles without future data leakage:

| Indicator | Formula / Method | Warm-Up Period | Notes |
| :--- | :--- | :--- | :--- |
| `PRICE` | Closing price $C_t$ | 1 candle | Available immediately on 1st completed candle |
| `SMA(period)` | $\frac{1}{n} \sum_{i=0}^{n-1} C_{t-i}$ | `period` candles | Simple moving average |
| `EMA(period)` | $C_t \cdot k + \text{EMA}_{t-1} \cdot (1-k)$, $k = \frac{2}{n+1}$ | `period` candles | Initialized with SMA of first `period` candles |
| `RSI(period)` | Wilder's RSI smoothing. If $\text{loss}=0, \text{gain}>0 \Rightarrow 100$. If $\text{loss}=0, \text{gain}=0 \Rightarrow 50$. | `period + 1` candles | Requires `period` price change intervals |
| `MACD(fast, slow, signal)` | $\text{MACD} = \text{EMA}_{fast} - \text{EMA}_{slow}$; $\text{Signal} = \text{EMA}_{signal}(\text{MACD})$ | MACD Line: `slow` candles.<br>Signal/Hist: `slow + signal - 1` | Component-level warm-up alignment |
| `PIVOT` | $P = \frac{H_{t-1} + L_{t-1} + C_{t-1}}{3}$; $S_1 = 2P - H_{t-1}$; $R_1 = 2P - L_{t-1}$ | 2 candles | Uses immediately preceding completed candle's $H, L, C$ |
| `VOLUME` | Candle volume $V_t$ | 1 candle | Available immediately |
| `AVERAGE_VOLUME(period)` | $\frac{1}{n} \sum_{i=0}^{n-1} V_{t-i}$ | `period` candles | Moving average of volume |

---

## Synthetic Educational Datasets

Located under [`apps/api/src/fixtures/`](file:///c:/Users/Naman/OneDrive/Desktop/MouseWithoutBorders/tradpro/apps/api/src/fixtures/):
- `synthetic_underlying_nifty_15m.csv`: Underlying series (NIFTY 15m)
- `synthetic_candidate_option_ce_23000_15m.csv`: Option candidate 1 (Call CE 23000)
- `synthetic_candidate_option_pe_23000_15m.csv`: Option candidate 2 (Put PE 23000)
- `synthetic_candidate_option_ce_23500_15m.csv`: Option candidate 3 (Call CE 23500)
- `synthetic_short_insufficient_5m.csv`: Short series (3 candles) for warm-up testing
- `synthetic_with_incomplete_candle_15m.csv`: Contains an incomplete candle (`is_closed=False`) to test exclusion

*All datasets are clearly labeled as synthetic educational test data.*

---

## Development & Inspection API Endpoints

### 1. `GET /indicators/supported`
Returns supported indicators, parameter requirements, and defaults.

### 2. `POST /indicators/calculate`
Calculates timestamp-aligned indicator results with warm-up remaining status.
*Request limit*: Max 5,000 candles per request.

**Example Request:**
```json
{
  "candles": [
    {
      "timestamp": "2026-08-28T09:15:00Z",
      "instrument_id": "NIFTY",
      "timeframe": "15m",
      "open": 22000.0,
      "high": 22050.0,
      "low": 21980.0,
      "close": 22040.0,
      "volume": 1500.0,
      "is_closed": true
    }
  ],
  "indicator": "SMA",
  "params": { "period": 20 }
}
```

---

## Synthetic Indicator Inspection Lab (`/indicator-lab`)

The **Indicator Lab** provides a visual educational workspace for inspecting indicators against synthetic OHLCV datasets:
- **Disclaimers**: Prominent educational notice explicitly stating that data is synthetic and no trading recommendations or orders are generated.
- **Dataset Selection**: Inspect packaged synthetic fixtures (`NIFTY`, Option CEs/PEs, short series, incomplete candle series) with total, completed, and excluded candle counts, plus a raw OHLCV data table viewer.
- **Interactive Charting**: Powered by TradingView Lightweight Charts (`lightweight-charts`), rendering price overlays (`PRICE`, `SMA`, `EMA`, `PIVOT`) on main price chart, and separate panels for `RSI`, `MACD`, and `VOLUME`/`AVERAGE_VOLUME`.
- **Comparison Mode**: Compare up to 3 indicator configurations simultaneously with distinct color coding and parameter tracking.
- **Results Table**: Timestamp-aligned results table with status indicators (`Available` vs `Warm-up`) and pagination.

---

---

## Educational Rule Inspection Lab (`/rule-lab`)

The **Rule Inspection Lab** provides deterministic Boolean rule evaluation of options strategies:
- **Disclaimers**: Prominent educational notice stating that results show Boolean rule evaluation and are not trading recommendations or execution signals.
- **Neutral Evaluation States**: Evaluates rules to strictly neutral statuses: `TRUE`, `FALSE`, `UNAVAILABLE`, `INVALID`.
- **Interactive Tree Viewers**: Expandable Reference and Subject scope condition trees rendering timestamps, evaluated left/right values, operator tolerances, and warm-up/expiration reasons.
- **Strategy Integration**: Jump directly from Rule Lab to Strategy Builder to modify strategy definitions.

---

## Educational Multi-Series Rule Inspection Lab (`/multi-series-lab`)

The **Multi-Series Lab** evaluates strategy rules independently across 1 to 20 packaged synthetic subject datasets:
- **Safe Synthetic Manifest (`GET /multi-series/datasets`)**: Server-side whitelisted manifest categorizing datasets into `REFERENCE` (Underlying Index) and `SUBJECT` (Option Candidates).
- **Independent Inspection (`POST /multi-series/evaluate`)**: Evaluates rules across selected subject datasets in deterministic input order without ranking, picking winners, or recommending actions.
- **Status Count Invariants**: Summary cards displaying `TRUE`, `FALSE`, `UNAVAILABLE`, `INVALID`, and Total Evaluated counts.
- **Accessibility & Mobile Layout**: Keyboard-accessible multi-select, visible focus rings, text+icon status badges, screen-reader live announcements (`role="status"`), and 375px mobile responsive layout.

---

---

## Historical Boolean Rule Replay Lab (`/historical-replay-lab`) & Inspection History (`/inspection-history`)

The **Historical Replay Lab** and **Inspection History** provide persistent, audit-trailed, reproducible Boolean rule evaluation across historical synthetic candle timestamps:
- **Persistent Database Models (`inspection_runs`)**: Alembic migration `0002_inspection_history` adds immutable inspection history storage compatible with SQLite and PostgreSQL.
- **Dataset Reproducibility & Fixture Checksums**: Dynamically computes SHA-256 fixture checksums server-side, captures stored snapshots, and displays mismatch reproducibility warnings.
- **Race-Safe Deduplication & Fingerprinting**: Generates SHA-256 request fingerprints to safely reuse completed identical runs without re-evaluation or race condition duplicates.
- **Categorical Status Timelines & Exports**: Displays status timeline sequences, transition counts, consecutive run lengths, and neutral JSON / CSV exports (with formula injection escaping for cells starting with `=`, `+`, `-`, or `@`).
- **Educational Safety & Boundaries**: Strict prohibition of trade simulation, profitability calculations, ranking, signals, recommendations, or broker connectivity. Local single-user educational architecture.

---

## GitHub Actions CI Workflow

Automated CI workflow runs on pull requests targeting `main`, pushes to `main`, and manual dispatch:
- **Backend Job**: Sets up Python 3.12, installs `apps/api/requirements.txt`, runs `pytest` with warnings treated as errors.
- **Frontend Job**: Sets up Node 22.x, runs `npm ci`, Vitest (`npm run test`), ESLint (`npm run lint`), TypeScript check (`npx tsc --noEmit`), and Next.js build (`npm run build`).

### Running Automated Test Suites Locally

```bash
# 1. Full Backend API Test Suite (pytest with warning enforcement)
cd apps/api
python -m pytest

# 2. Full Frontend Web Test Suite (vitest, lint, tsc, build)
cd apps/web
npm run test
npm run lint
npx tsc --noEmit
npm run build
```
