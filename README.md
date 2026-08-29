# TradePro Option Strategy & Analytics Workspace

TradePro is an educational options strategy-building, backtesting, and paper-trading workspace.

This repository covers **Milestone 1 (Strategy Builder Foundation)** and **Milestone 2A (Candle Data & Deterministic Indicator Engine)**.

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

## Running Automated Test Suites

### Full Backend API Test Suite (`pytest`)
Runs all Milestone 1, 2A, and dataset backend tests:
```bash
cd apps/api
python -m pytest
```

### Frontend Web Test Suite (`vitest`)
Runs all graph serialization and Indicator Lab component tests:
```bash
cd apps/web
npm run test
```
