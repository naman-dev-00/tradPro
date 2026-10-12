# TradePro: Remaining Roadmap & Delivery Architecture (Phases 5 – 8)

**Document Version:** 2.0.0
**Baseline Commit:** `646af3b6138473cb45cdbddaff8404f0a21fe46d` (Phase 6 Verified Clean Main)
**Contract Verification Date:** 2026-10-07

---

## 1. Executive Roadmap Overview

TradePro has completed foundational deterministic orchestration (Phases 1–4), read-only provider market data ingestion (Phase 5), and provider-driven paper and sandbox execution correctness (Phase 6). The roadmap now specifies Phase 7 split into Phase 7A (Secure Broker Connection and Live Readiness) and Phase 7B (Guarded Live Broker Execution & Order Transmission), leading into production deployment and observability in Phase 8:

```
[Phase 4: Verified Main Baseline] (a32802c)
       │
       ▼
[Phase 5: Read-Only Provider Market Data & Lab]  [COMPLETED - MERGED]
       │  • Bounded REST client (Historical & Intraday V3)
       │  • Network disabled by default; isolated credentials
       │  • Canonical validation & clock-injected candle completion
       │  • Authenticated Market Data Lab UI
       ▼
[Phase 6: Execution Correctness & Sandbox Sync]   [COMPLETED - MERGED]
       │  • Ingestion into active strategy runtimes
       │  • Outbox atomicity, pre-transmission lease markers
       │  • Fail-closed 429 & ambiguous outcome reconciliation
       │  • Exact trade ID verification & sandbox trade limits
       ▼
[Phase 7A: Secure Broker Connection & Readiness] <-- CURRENT IMPLEMENTATION
       │  • Upstox OAuth 2.0 authorization code flow with single-use state
       │  • Provider-confirmed UCC/account identity bound to local owner
       │  • Server-side authenticated token encryption (key outside DB)
       │  • Read-only broker snapshots (funds, margins, holdings, positions, orders)
       │  • Multi-gate readiness checks (Static IP, session, segment, CAS blockers)
       │  • Live execution strictly disabled (no live order placement/cancel)
       ▼
[Phase 7B: Guarded Live Broker Execution]
       │  • Dedicated BROKER_LIVE execution mode behind multi-tier administrative locks
       │  • Environment gating: APP_ENV=production, explicit operator consent workflow
       │  • Pre-trade risk engine, capital limits, software emergency kill-switches
       │  • Authoritative live fill reconciliation & position tracking
       ▼
[Phase 8: Deployment, Observability, Recovery & Acceptance]
          • Production containerization & automated health/readiness probes
          • OpenTelemetry distributed tracing & Prometheus metrics
          • Disaster recovery automation & final operational acceptance
```

---

## 2. Official Upstox Provider Contract Audit

### 2.1 Audit Scope and Documentation Sources
- **Provider:** Upstox API V2 / V3
- **Audit Retrieval Date:** 2026-10-07
- **Official Documentation Sources Consulted:**
  - Authentication: `https://upstox.com/developer/api-documentation/authentication/`
  - Sandbox Environment: `https://upstox.com/developer/api-documentation/sandbox/`
  - Rate Limiting: `https://upstox.com/developer/api-documentation/rate-limiting/`
  - Regulatory Circular (Algo & Static IP): `https://upstox.com/developer/api-documentation/announcements/algo-trading-circular/`
  - Closing Auction Session (CAS): `https://upstox.com/developer/api-documentation/announcements/closing-auction-session/`
  - Exchange Circular (NSE INVG67858): `https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf`

---

### 2.2 OAuth 2.0 Authentication & Token Lifecycle
- **Authorization URL:**
  `GET https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={API_KEY}&redirect_uri={REDIRECT_URI}&state={STATE}`
- **Token Exchange Endpoint:**
  `POST https://api.upstox.com/v2/login/authorization/token`
  - **Headers:** `Content-Type: application/x-www-form-urlencoded`, `Accept: application/json`
  - **Body Parameters:**
    - `code`: Authorization code returned from dialog redirect (single-use)
    - `client_id`: Registered App API Key
    - `client_secret`: Registered App Secret (server-side only)
    - `redirect_uri`: Must match exactly the configured allowlisted redirect URI
    - `grant_type`: `authorization_code`
  - **Token Response:**
    ```json
    {
      "email": "user@example.com",
      "user_name": "Trader Name",
      "user_id": "UCC12345",
      "user_type": "individual",
      "broker": "UPSTOX",
      "is_active": true,
      "access_token": "eyJhbGciOi...",
      "extended_token": null
    }
    ```
- **Token Lifetime & Refresh Behavior:**
  - Tokens expire daily at the morning reset (~03:30 AM IST).
  - **No refresh tokens exist in Upstox V2 API.** Re-authentication via OAuth authorization code flow is required every trading day.
  - Replay protection: Authorization codes are strictly single-use and consumed immediately upon exchange (succeed or fail).
- **Security Invariants for TradePro:**
  - `state` parameter must be cryptographically random (minimum 32 bytes entropy / 64 hex chars), single-use, bounded to a 5–10 minute TTL, tied to the authenticated `owner_id` and initiating session.
  - State consumption must be atomic (preventing concurrent callback replay attacks).
  - Client secrets and tokens must never appear in client-side code, cookies, browser storage, logs, API responses, or review bundles.
  - Server-side tokens must be encrypted at rest using authenticated symmetric encryption (e.g., AES-256-GCM / Fernet) with the encryption key supplied via environment configuration outside the database.

---

### 2.3 Account Identity & Read-Only Portfolio / Funds Endpoints
- **User Profile API:**
  `GET https://api.upstox.com/v2/user/profile`
  - Returns `user_id` (Unique Client Code / UCC), `user_name`, `email`, `user_type`, `broker`.
  - Used for authoritative verification that the connected broker account matches the configured local owner.
- **Funds and Margin API:**
  `GET https://api.upstox.com/v2/user/get-funds-and-margin`
  - Optional query parameter: `segment` (`SEC` for equity, `COM` for commodities).
  - Returns structured equity and commodity objects: `available_margin`, `used_margin`, `payin_amount`, `span_margin`, `adhoc_margin`, `notional_cash`, `exposure_margin`.
- **Holdings API:**
  `GET https://api.upstox.com/v2/portfolio/long-term-holdings`
  - Returns list of demat holdings (`isin`, `tradingsymbol`, `quantity`, `t1_quantity`, `average_price`, `last_price`, `pnl`).
- **Positions API:**
  `GET https://api.upstox.com/v2/portfolio/short-term-positions`
  - Returns open intraday/delivery positions (`instrument_token`, `tradingsymbol`, `quantity`, `buy_amount`, `sell_amount`, `realised`, `unrealised`).
- **Orders API:**
  `GET https://api.upstox.com/v2/order/retrieve-all`
  - Returns day order book with status, order ID, quantity, price, order type.
- **Isolation Invariant:**
  - Snapshots are read-only evidence. They are persisted separately from paper accounts, paper positions, internal reservations, and ledger entries. Reading a broker position or order must NEVER manufacture an internal fill.

---

### 2.4 Sandbox vs. Live Capabilities & Constraints
- **Host Allowlist:**
  - Live API: `https://api.upstox.com`
  - Sandbox API: `https://api-sandbox.upstox.com` (or `https://api.upstox.com` with sandbox tokens)
- **Documented Sandbox Capabilities:**
  - Order placement simulation (`/place-order`, `/v3/place-order`, `/place-multi-order`)
  - Order modification simulation (`/modify-order`, `/v3/modify-order`)
  - Order cancellation simulation (`/cancel-order`, `/v3/cancel-order`)
- **Documented Sandbox Limitations:**
  - Sandbox tokens are generated statically via Upstox Developer Apps portal (valid 30 days).
  - OAuth authorization dialog and redirect URIs have no functional purpose in Sandbox.
  - Read endpoints (`/user/profile`, `/user/get-funds-and-margin`, `/portfolio/long-term-holdings`, `/portfolio/short-term-positions`, `/order/details`, `/order/trades`) do NOT return authoritative sandbox state or are unsupported (`AUTHORITATIVE_TRADES_UNAVAILABLE`).
  - Mocked sandbox responses must NEVER be treated as evidence of real broker readiness or live trading capability.

---

### 2.5 Regulatory Circulars & Static IP Requirements
- **Circular:** NSE Circular `INVG67858` / Upstox Algo Trading Circular (Effective April 1, 2026).
- **Mandatory Static IP for Order Placement:**
  - All automated/API order requests (`Place Order`, `Modify Order`, `Cancel Order`, `Place Multi Order`, `Cancel Multi Order`, `GTT Orders`, `Exit All Positions`) MUST originate from a registered Static IP.
  - Each Upstox user account permits up to two Static IPs: one **Primary** and one **Secondary** (failover).
  - Static IPs are user-account scoped and unique (cannot be shared between accounts).
  - Updating Static IPs via API (`/update-app-static-ips`) immediately invalidates all existing access tokens and requires daily re-authentication.
  - **Exemption:** Standard Read-Only APIs (user profile, funds, holdings, positions, historical candles) are **NOT restricted** by Static IP.
- **Algo Name Requirement (`X-Algo-Name` Header):**
  - When placing orders via Algo Apps, requests must include the configured Algo Name in the `X-Algo-Name` HTTP header.
- **SEBI Algo Registration Threshold:**
  - Algorithms submitting <= 10 orders per second are classified as **Regular Algos** (no SEBI/exchange registration required).
  - Algorithms submitting > 10 orders per second require formal exchange/SEBI registration and an assigned **Algo ID**.

---

### 2.6 Rate Limits
- **Combined Order Placement APIs:**
  - Regular Algos (<= 10 orders/s): 10 req/s, 500 req/min, 2,000 req/30min.
  - SEBI-Registered Algos: 50 req/s, 500 req/min, 2,000 req/30min.
- **Other Standard APIs (Funds, Holdings, Positions, Historical Data):**
  - 50 requests / second
  - 500 requests / minute
  - 2,000 requests / 30 minutes
- **Payout APIs:** 10 req/s (Standard), 10 req/min (Restricted).
- **TOTP Login:** 1 req/s, 10 req/min, 60 req/30min.

---

### 2.7 Exchange Segments, Market Timings & Closing Auction Session (CAS)
- **Effective Date:** August 3, 2026
- **Derivatives Market Close:**
  - Equity segments (`NSE_EQ`, `BSE_EQ`) close at 15:30 IST (`NORMAL_CLOSE`).
  - Derivatives segments (`NSE_FO`, `BSE_FO`) close at 15:40 IST (`NORMAL_CLOSE`).
  - Assumption of a single 15:30 IST close across all segments is invalid. Session hours must be validated per segment.
- **Closing Auction Session (CAS) for F&O-Eligible Equities:**
  - Eligible equity instruments carry `cas_eligible = true` in instrument metadata.
  - Schedule for CAS-eligible securities:
    - ~15:15 IST: `CTS_CLOSE` — continuous trading closes; order entry paused.
    - ~15:20 IST: `CAS_LM_START` — closing auction opens; Limit and Market orders accepted.
    - ~15:25 IST: `CAS_M_STOP` — Market order entry restricted; Limit orders accepted.
    - 15:28–15:30 IST: `CAS_STOP` — closing auction order entry closed.
    - 15:30 IST: `NORMAL_CLOSE` — market close.
  - **CAS Order Restrictions:**
    - Stop-Loss orders (`SL`, `SL-M`) are strictly prohibited and rejected in all CAS phases.
    - Immediate-or-Cancel (`IOC`) orders are prohibited.
    - Order slicing is prohibited.
    - Price band: orders must fall within +/- 3% of the Reference Price.
  - **Readiness Invariant:** If session evidence indicates CAS or an unsupported session phase, live dispatch readiness must be blocked (`BLOCKED`).

---

## 3. Detailed Phase Specifications

### Phase 5: Read-Only Provider Market Data & Inspection [COMPLETED]
- **Delivered Scope:**
  - Read-only Upstox V3 Market Data Adapter for historical and intraday candles.
  - Network disabled by default (`UPSTOX_MARKET_DATA_ENABLED=false`).
  - Server-side credentials scoped strictly to configured operator (`UPSTOX_MARKET_DATA_OWNER_ID`).
  - Strict host allowlist (`https://api.upstox.com`), bounded timeouts, payload bounds, exponential backoff.
  - Complete error sanitization: tokens stripped from logs, diagnostics, and errors.
  - Canonical validation: UTC timezone normalization, scaled unit conversions (`BigInteger` 10^4 scale), OHLC geometry integrity, clock-injected completion check.
  - SHA-256 dataset provenance fingerprinting.
  - Authenticated owner-scoped APIs (`/api/v1/market-data/*`) and Market Data Lab UI.

---

### Phase 6: Execution Correctness & Sandbox Sync [COMPLETED]
- **Delivered Scope:**
  - Normalized candle ingestion into active strategy runtimes.
  - Outbox queueing for automated LIMIT order execution with atomic lease markers (`transmission_started_at`).
  - Multi-gate safety verification and conservative 429 reconciliation:
    - Mutating order submissions receiving 429 fail closed to `RECONCILIATION_REQUIRED`; blind retransmission is prohibited.
  - Order details and trade verification requiring exact match on `order_id`. Duplicate trades with conflicting details or inconsistent cumulative quantities are rejected.
  - Authoritative sandbox trade limitation documented and enforced (`AUTHORITATIVE_TRADES_UNAVAILABLE`).
  - Durable request-scoped reconciliation transactions with full rollback on error.
  - Zero live broker transmission enforced at all times.

---

### Phase 7A: Secure Broker Connection & Live Readiness (CURRENT IMPLEMENTATION)
- **Scope:**
  - Upstox OAuth 2.0 authorization code handling:
    - Cryptographically random, single-use, owner-bound, session-bound state token with 5-minute TTL.
    - Allowlisted callback URI validation (`/api/v1/broker/auth/callback`).
    - Atomic state consumption to prevent replay and concurrent callback races.
    - Server-side code exchange; provider-confirmed broker UCC/Client ID bound to local owner.
    - Connection classification: explicit `SANDBOX` vs `LIVE` environment separation.
    - Disconnection, expiry, and authorization failure handling.
  - Server-Side Secret Store & Authenticated Encryption:
    - Access tokens encrypted at rest via authenticated symmetric encryption (AES-256-GCM / Fernet) using environment master key (`BROKER_SECRET_KEY`).
    - Encryption key kept strictly outside the database.
    - Zero tokens, codes, or secrets in API responses, logs, cookies, or browser storage.
  - Read-Only Broker Adapter & Snapshots:
    - Allowlisted HTTPS endpoints for read-only snapshot retrieval: User Profile, Funds & Margins, Holdings, Positions, Open Orders.
    - Host allowlisting (`https://api.upstox.com`), bounded timeouts (5s), bounded payload size (2MB), retry bounds.
    - Exact unit conversions: funds/margins and positions converted to scaled units without floating-point drift.
    - Remote HTTP fetches performed outside database locks; optimistic version checking before snapshot persistence.
    - Snapshots isolated completely from internal paper accounting; positions read do not create fills or ledger entries.
    - Explicit authenticated refresh action (`POST /api/v1/broker/snapshots/refresh`).
    - GET endpoints (`/connection`, `/snapshots/latest`, `/readiness`) are strictly non-mutating.
  - Structured Readiness Checks:
    - Multi-gate structured evaluation:
      1. Valid connection and matching provider UCC.
      2. Token freshness and daily expiration status.
      3. Static IP registration evidence (check configured static IP vs required).
      4. Segment and session evidence (checks supported segments, active trading hours, CAS status; CAS / unsupported sessions block readiness).
      5. Fresh account/risk snapshot evidence.
      6. Kill switch and unresolved reconciliation status.
    - Unverified prerequisites fail closed to `UNKNOWN` or `BLOCKED`.
    - Live execution dispatch status is hardcoded to `live_dispatch_enabled: false` in Phase 7A.
  - Broker Connection & Readiness UI:
    - Connection panel with Connect, Reconnect, Disconnect controls.
    - Masked account identity (e.g. `UCC***345`) and environment badge (`LIVE` / `SANDBOX`).
    - Token freshness indicator and last refresh timestamp.
    - Read-only balances, positions, and open orders tables.
    - Actionable readiness blocker list.
    - Prominent banner: "Live execution disabled (Phase 7A: Read-Only Readiness)".
- **Acceptance Criteria:**
  - OAuth state replay, expiration, or cross-owner consumption returns 400/403.
  - Tokens are never exposed in plaintext in DB or responses.
  - Broker snapshots produce zero changes in `paper_accounts`, `account_ledger_entries`, `paper_positions`, or `fills`.
  - Zero live order submissions or cancellations can be triggered.
  - Protected database `tradepro.db` remains unmodified.

---

### Phase 7B: Guarded Live Broker Execution
- **Scope:**
  - Dedicated `BROKER_LIVE` execution mode behind multi-tier administrative locks.
  - Production environment gating: `APP_ENV=production` required; explicit dual-operator consent workflow.
  - Pre-trade risk engine: max daily loss limit, max position size, max orders per minute, static IP check before transmission.
  - Software emergency kill-switch with immediate outbox suppression (< 500ms).
  - Outbox integration with `X-Algo-Name` header inclusion.
  - Real-time margin validation against cached broker snapshots.
  - Authoritative live fill reconciliation and position tracking.
- **Dependencies:**
  - Phase 7A secure broker connection, authoritative UCC binding, and snapshot adapter.
  - Production Upstox API credentials and registered Static IP.

---

### Phase 8: Deployment, Observability, Recovery & Final Acceptance
- **Scope:**
  - Production containerization (Docker multi-stage builds) and Kubernetes manifests.
  - Prometheus metrics instrumentation (order latency, outbox queue lag, provider error rates, snapshot freshness).
  - OpenTelemetry distributed tracing with correlation IDs propagated across web, API, and workers.
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
