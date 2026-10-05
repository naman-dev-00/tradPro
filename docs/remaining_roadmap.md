# TradePro: Remaining Roadmap & Delivery Architecture (Phases 5 – 8)

**Document Version:** 1.0.0  
**Baseline Commit:** `a32802c5b0c8c69baccc01fb2eda108201b1f19c` (Phase 4 Verified Clean Main)  
**Contract Verification Date:** 2026-10-05  

---

## 1. Executive Roadmap Overview

TradePro has completed foundational deterministic orchestration (Phase 1–4) through internal paper execution and fixture-based evaluation. The remaining roadmap transitions TradePro into read-only provider data ingestion, sandbox broker automated execution, guarded live trading, and production-grade observability and recovery:

```
[Phase 4: Verified Main Baseline] (a32802c)
       │
       ▼
[Phase 5: Read-Only Provider Market Data & Lab]  <-- CURRENT IMPLEMENTATION
       │  • Bounded REST client (Historical & Intraday V3)
       │  • Network disabled by default
       │  • Separate market data credentials & isolation
       │  • Canonical validation & clock-injected candle completion
       │  • Authenticated Market Data Lab UI
       ▼
[Phase 6: Provider-Driven Paper & Automated Broker Sandbox Execution]
       │  • Evaluator consumes live provider market data
       │  • Outbox queueing for automated LIMIT order execution
       │  • Multi-gate safety verification and conservative 429 reconciliation
       ▼
[Phase 7: Guarded Live Broker Execution]
       │  • Live broker environment gating & explicit operator consent
       │  • Capital limits, software kill-switches, strict risk engine barriers
       │  • Live fill reconciliation & position tracking
       ▼
[Phase 8: Deployment, Observability, Recovery & Acceptance]
          • Production containerization & automated health probes
          • OpenTelemetry / Prometheus metrics & audit logs
          • Disaster recovery runbooks & final operational acceptance
```

---

## 2. Upstox API V3 Market Data Contract Verification

### 2.1 Provider Documentation & Endpoint Contracts
- **Provider:** Upstox API V3
- **Official Documentation Portal:** [Upstox Developer Documentation](https://upstox.com/developer/api-documentation)
- **Historical Candle Data V3:** `GET https://api.upstox.com/v3/historical-candle/{instrumentKey}/{unit}/{interval}/{to_date}/{from_date}`
- **Intraday Candle Data V3:** `GET https://api.upstox.com/v3/historical-candle/intraday/{instrumentKey}/{unit}/{interval}`
- **Verification Date:** 2026-10-05

### 2.2 Parameters and Constraints
- **Timeframe / Unit Support:**
  - Supported units: `minutes`
  - Phase 5 supported intervals: `5` (5-minute candle) and `15` (15-minute candle)
  - Parameter format: `unit=minutes`, `interval=5` or `15`
- **Date Format:** `YYYY-MM-DD` (inclusive range)
- **Instrument Key Identity:** e.g., `NSE_INDEX|Nifty 50`, `NSE_EQ|INE002A01018`, `NSE_FO|...`
- **Response Format:**
  ```json
  {
    "status": "success",
    "data": {
      "candles": [
        [
          "2026-10-05T15:15:00+05:30",
          25200.50,
          25240.00,
          25180.25,
          25215.10,
          85420,
          0
        ]
      ]
    }
  }
  ```
  Field index map:
  - `[0]`: ISO-8601 timestamp string with provider timezone offset (e.g. `+05:30`)
  - `[1]`: Open price (float)
  - `[2]`: High price (float)
  - `[3]`: Low price (float)
  - `[4]`: Close price (float)
  - `[5]`: Volume (integer/float)
  - `[6]`: Open Interest (integer, optional/ignored in equity)

### 2.3 Provider Request Limits & Policies
- **Rate Limits:**
  - 50 requests / second
  - 500 requests / minute
  - 2,000 requests / 30 minutes
- **Authentication:** HTTP Header `Authorization: Bearer <access_token>`
- **Caching & Latency Semantics:**
  - Intraday endpoint is cached up to ~30s via CDN.
  - The latest in-progress candle can be incomplete or revising.
  - **TradePro Invariant:** Only *completed* candles are admitted. An injectable clock validates `candle_open + interval <= clock.now_utc()` before admitting candles. Incomplete candles are strictly excluded.
- **Scope & Credential Separation:**
  - Market data authorization is distinct from broker order placement authorization (`UPSTOX_MARKET_DATA_ACCESS_TOKEN` vs `UPSTOX_SANDBOX_ACCESS_TOKEN`).
  - No assumption is made that sandbox credentials permit market data access.

---

## 3. Detailed Phase Specifications

### Phase 5: Read-Only Provider Market Data & Inspection (CURRENT IMPLEMENTATION)
- **Scope:**
  - Read-only Upstox V3 Market Data Adapter for historical and current-day candles.
  - Explicit network enablement flag (`UPSTOX_MARKET_DATA_ENABLED=false` default).
  - Server-side credentials scoped strictly to configured operator (`UPSTOX_MARKET_DATA_OWNER_ID`, `UPSTOX_MARKET_DATA_ACCESS_TOKEN`).
  - Strict host whitelist (`https://api.upstox.com`) and GET endpoint paths.
  - Bounded timeouts, bounded response payload size, and bounded exponential backoff.
  - Complete error sanitization: authorization headers and tokens stripped from logs, diagnostics, and errors.
  - Canonical validation: UTC timezone normalization, exact numeric parsing and scaled unit conversions (`BigInteger` 10^4 scale for prices), OHLC geometry integrity (`high >= low`, `high >= max(open, close)`, `low <= min(open, close)`), volume non-negativity.
  - Injectable clock ensuring future or unclosed candles are never returned as completed inputs.
  - Deterministic ordering (chronological ascending) and deduplication.
  - Dataset provenance: SHA-256 fingerprinting of canonical payload, source labeled as `PROVIDER_UPSTOX_V3`, retrieval timestamp recorded.
  - Authenticated owner-scoped APIs (`/api/v1/market-data/*`).
  - Market Data Lab UI: honest connection status, instrument/timeframe selector, historical/intraday inspection, candle data table, provenance card, error/loading/disabled states.
- **Dependencies:**
  - Verified Phase 4 main baseline (`a32802c`).
  - Existing user authentication and session management.
- **Acceptance Criteria:**
  - When `UPSTOX_MARKET_DATA_ENABLED=false`, zero external network calls are attempted; readiness returns `NETWORK_DISABLED` status.
  - Cross-owner API calls return `403 Forbidden` (`FORBIDDEN_OPERATOR`).
  - Scaled integer conversion and UTC timestamps match exact canonical representation.
  - Unfinished or future candles are rejected deterministically.
  - Malformed, out-of-order, or duplicate candles are handled safely; conflicting revisions are quarantined.
  - Completeness is honestly reported (`COMPLETE`, `INCOMPLETE`, `UNKNOWN`); `UNKNOWN` is reported when range/session coverage cannot be verified without exchange trading calendar evidence.
  - No order endpoints, `submission_outbox` writes, or financial ledger mutations occur.
  - Protected database `tradepro.db` remains unmodified.
- **Outstanding Items / Limitations:**
  - Live end-to-end smoke test requires external operator-provided Upstox access token.
  - Real-time WebSocket streaming feed is deferred to Phase 6/7.

---

### Phase 6: Provider-Driven Paper & Automated Broker Sandbox Execution
- **Scope:**
  - Ingestion service feeding normalized Phase 5 candles into active strategy runtimes.
  - Automated signal generation based on real-market completed candles.
  - Integration with Milestone 6B transactional outbox queue (`submission_outbox`).
  - Execution mode `BROKER_SANDBOX` driven by incoming provider candle intervals.
  - Automated `PLACE` and `CANCEL` order submission for verified instrument mappings.
  - Conservative rate limiting and 429 reconciliation worker.
  - Polling-based order status verification and fill simulation against sandbox broker.
  - Conservative ambiguous-outcome handling: Any ambiguous transmission outcome (timeouts, network drops, unconfirmed 5xx) immediately transitions outbox entry to `RECONCILIATION_REQUIRED`. Automatic broker retransmission without status confirmation is strictly prohibited to prevent duplicate executions.
- **Dependencies:**
  - Phase 5 read-only market data adapter and normalizer.
  - Phase 4 strategy orchestrator worker and transition lifecycle.
  - Milestone 6B sandbox outbox worker and safety gates.
- **Acceptance Criteria:**
  - Evaluator processes completed provider candles without temporal leakage.
  - Outbox transitions adhere strictly to `PENDING` -> `CLAIMED` -> `DELIVERED` / `RECONCILIATION_REQUIRED`.
  - Rate limiting (429) triggers exponential backoff without dropping orders.
  - Ambiguous outcomes freeze further automatic transmission until authoritative reconciliation.
  - Sandbox broker rejections do not corrupt internal paper ledger.
- **Outstanding Items:**
  - Partial fill reconciliation and order modification handling.
  - Handling of market closure and holidays in provider feed.

---

### Phase 7: Guarded Live Broker Execution
- **Scope:**
  - Dedicated `BROKER_LIVE` execution mode behind multi-tier administrative locks.
  - Environment gating: `APP_ENV=production` required, explicit operator consent workflow for activation.
  - Real-time pre-trade risk engine: max daily loss limit, max position size, max orders per minute.
  - Integrated software emergency kill-switch with immediate outbox suppression.
  - Real-time margin checking and broker sync.
  - Immutable audit logging of all transmission decisions, responses, and manual interventions.
- **Dependencies:**
  - Phase 6 automated sandbox execution proven stable across stress test scenarios.
  - Dedicated production API credentials with Upstox.
- **Acceptance Criteria:**
  - Zero accidental live transmissions under any test, staging, or local configurations.
  - Software kill-switch activation aborts pending outbox submissions within < 500ms.
  - Exceeding any risk parameter immediately transitions runtime to `HALTED`.
  - Full audit trail captured in immutable security log.
- **Outstanding Items:**
  - Legal & regulatory compliance signoff for algorithmic execution.
  - Broker-side rate limit SLA agreement and leased line / low-latency connectivity options.

---

### Phase 8: Deployment, Observability, Recovery & Final Acceptance
- **Scope:**
  - Production containerization (Docker multi-stage builds) and Helm / Kubernetes deployment manifests.
  - Prometheus metrics instrumentation (request latencies, outbox lag, provider error rates, evaluation cycle duration).
  - OpenTelemetry distributed tracing with correlation IDs propagated across web, API, and background workers.
  - Automated health and readiness probes (`/healthz`, `/readyz`).
  - Disaster recovery automation: point-in-time database restoration drill, outbox reconciliation recovery script.
  - Operational runbooks for incident response, credential rotation, and emergency halting.
  - Final acceptance testing suite and signoff report.
- **Dependencies:**
  - Phases 5, 6, and 7 implemented and verified.
- **Acceptance Criteria:**
  - Automated deployment achieves zero-downtime rolling restart.
  - Mean Time to Detect (MTTD) < 30 seconds for provider feed disconnection.
  - Recovery Time Objective (RTO) < 5 minutes for worker crashes.
  - 100% of test suites (unit, integration, migration parity, E2E) passing on CI.
- **Outstanding Items:**
  - Provisioning of production cloud environment and secrets management infrastructure (e.g. AWS Secrets Manager or HashiCorp Vault).
  - Production alerting channels (PagerDuty, Slack).
