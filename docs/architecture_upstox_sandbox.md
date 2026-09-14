# TradePro Milestone 6B: Upstox Sandbox & Outbox Queue Architecture

## 1. Overview & Operational Boundary
Milestone 6B introduces an educational broker sandbox execution mode integrating with Upstox API v3. All broker integration operates behind an offline-first, fail-safe security boundary with transactional outbox queueing, strict fail-safe gates, and explicit manual reconciliation.

## 2. Credential Architecture & Shared-Host Risk
- **Environment-Only Token**: The database never stores raw or encrypted tokens, ciphertext, token hashes, token fingerprints, or Authorization headers. The access token is read exclusively from the server environment (`UPSTOX_SANDBOX_ACCESS_TOKEN`) at transmission time.
- **Provider Connections Schema**: The `provider_connections` table persists only non-secret metadata:
  - `id`: String(36) UUID
  - `owner_id`: String(36) Foreign key to `users.id`
  - `provider_name`: "UPSTOX"
  - `environment`: "SANDBOX"
  - `credential_reference`: Non-secret logical reference key (e.g. "env:UPSTOX_SANDBOX_ACCESS_TOKEN"), kept strictly server-side and never returned in ordinary API responses
  - `credential_version`: Non-secret version string (e.g. "v1")
  - `status`: Honest configuration status ("CONFIGURED", "DISABLED", "ERROR")
  - `last_successful_transmission_at`: Nullable UTC timestamp, updated only after an unambiguous successful Place or Cancel response. Not interpreted as current connectivity or proof of sandbox scope. Dynamic readiness is computed via `GET /api/v1/sandbox/readiness?runtime_id=<uuid>`.
  - `sanitized_error_code`: Bounded diagnostic code
  - `created_at`, `updated_at`: UTC timestamps
- **Shared Host & Token Scope Risk**: Because TradePro runs on shared infrastructure without direct custody of the broker's auth server, TradePro cannot cryptographically verify that a provided Upstox token is strictly constrained to sandbox scopes. If an operator mistakenly provisions a live production token, real-market exposure could occur. Operators must ensure credentials provisioned in `UPSTOX_SANDBOX_ACCESS_TOKEN` originate strictly from the Upstox developer sandbox portal.
- **Single-Owner Limitation**: Upstox sandbox connections are isolated strictly to a single configured sandbox owner (`UPSTOX_SANDBOX_OWNER_ID`). Cross-owner connection requests receive an identical 404 (ResourceNotFoundError) to prevent enumeration.

## 3. Default-Offline Safety Gates & Instrument Mapping Lifecycle
External transmission to Upstox is disabled by default. Transmission requires all runtime safety gates evaluated immediately before each HTTP request:
1. `APP_ENV` must be `local` or `test` (production and staging fail fast at startup if sandbox networking is enabled).
2. `UPSTOX_SANDBOX_NETWORK_ENABLED` environment flag must be explicitly `"true"`.
3. Valid `ProviderConnection` exists for the runtime owner with provider `UPSTOX` and environment `SANDBOX`.
4. Configured owner matches runtime owner.
5. Runtime trading mode is `BROKER_SANDBOX` or `BROKER_SANDBOX_RECORDED_FIXTURE`.
6. Verified, non-expired instrument mapping snapshot is frozen in the runtime.
7. Account status is active.
8. System kill-switch is inactive.
9. Pre-trade risk checks pass.
10. Order side, quantity, and price limits are verified.
11. Outbox entry is successfully claimed with an active lease.

If any gate fails, external network calls are blocked and the outbox entry transitions to `RETRY_SCHEDULED` (or `DEAD_LETTER` once max attempts are reached) without external transmission.

### Distinct Instrument Mapping Lifecycle & Semantics
Instrument mappings maintain 4 distinct verification states:
- `UNVERIFIED`: Initial state upon creation by EDITOR/ADMIN.
- `VERIFIED`: Approved by ADMIN after verifying instrument details. Required for creating sandbox runtimes.
- `REJECTED`: Rejected by ADMIN during verification. Unusable by runtimes.
- `DISABLED`: Explicitly deactivated by mapping owner or ADMIN. Unusable by runtimes.

Rules:
- Verification rejection persists `REJECTED` in the database, with the audit record's `new_status` matching `"REJECTED"`.
- Explicit disable operation persists `DISABLED`, with audit record `new_status` matching `"DISABLED"`.
- Both `REJECTED` and `DISABLED` mappings are unusable by sandbox runtimes.
- Invalid state transitions (e.g. attempting to verify a `REJECTED` or `DISABLED` mapping, or disabling an already `DISABLED` mapping) return HTTP 409 `ConflictError`.
- A rejected mapping cannot silently become verified; it requires creating a new mapping version.

## 4. LIMIT-Only Place and Cancel Order Support
Part 1 of the broker sandbox supports LIMIT orders only:
- **Supported Order Type**: `LIMIT` with Time-In-Force `DAY`.
- **Supported Actions**:
  - `PLACE` (`POST /v3/order/place`, priority 10)
  - `CANCEL` (`DELETE /v3/order/cancel`, priority 0)
- **Unsupported Orders**: `MARKET`, `SL`, and `SL-M` are rejected locally at the risk engine level with reason code `RISK_UNSUPPORTED_SANDBOX_ORDER_TYPE` before creating any outbox or network operation. `PAPER` mode continues to support educational market orders locally.
- **Order Slicing**: All orders enforce `slice=false`.

## 5. Canonical Outbox State Machine
The submission outbox implements exactly 6 canonical states:
- `PENDING`: Initial state upon creation; ready for first claim by worker.
- `CLAIMED`: Temporarily leased to an outbox worker with an active lease expiration.
- `DELIVERED`: Unambiguous provider success only (contains valid `provider_order_id` or cancel confirmation). Updates `conn.last_successful_transmission_at`.
- `RETRY_SCHEDULED`: Failure proven to occur before network transmission (e.g. temporary gate blockage); bounded retry scheduled.
- `RECONCILIATION_REQUIRED`: Ambiguous outcome where provider acceptance cannot be ruled out (HTTP 429, 5xx post-transmission, timeout, socket drop, malformed payload). Never automatically retried.
- `DEAD_LETTER`: Bounded safe retry count exhausted before any ambiguous transmission.

### Transition Rules & Dead-Letter Handling
- **PLACE Dead-Letter**: When a PLACE outbox entry exhausts retries without transmission, the associated order transitions to `PROVIDER_REJECTED` and reserved cash is released exactly once. No reconciliation record is created because provider acceptance was ruled out.
- **CANCEL Dead-Letter**: When a CANCEL outbox entry exhausts safe pre-transmission retries:
  - Outbox transitions to `DEAD_LETTER`.
  - Order returns from `CANCEL_PENDING` to `ACKNOWLEDGED`.
  - Reservation remains unchanged.
  - No reconciliation record is created.
  - A later CANCEL operation is permitted with a new outbox/idempotency key.
  - Order is never left stuck in `CANCEL_PENDING` and is never marked `CANCELLED`.
- **Priorities**:
  - `CANCEL`: Priority 0 (highest priority, processed before placements)
  - `PLACE`: Priority 10

## 6. Reconciliation Case Lifecycle
A reconciliation record tracks cases where network or provider transport outcome is ambiguous:
- `status`: `OPEN` or `RESOLVED`
- Lifecycle Check Constraint:
  ```sql
  (status = 'OPEN' AND resolution_type IS NULL AND resolved_by IS NULL AND resolved_at IS NULL)
  OR
  (status = 'RESOLVED' AND resolution_type IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
  ```
- Allowed Manual Resolutions:
  - `PLACE_CONFIRMED`: Place confirmed at provider. Order becomes `ACKNOWLEDGED`, external link created, reservation retained, 0 fill/fee fabricated.
  - `PLACE_REJECTED`: Place rejected by provider. Order becomes `PROVIDER_REJECTED`, reservation released idempotently.
  - `CANCEL_CONFIRMED`: Cancel confirmed at provider. Order becomes `CANCELLED`, reservation released idempotently.
  - `CANCEL_NOT_CONFIRMED`: Cancel unconfirmed/failed. Order returns to `ACKNOWLEDGED`, reservation retained. Subsequent cancel permitted.
- Outbox Status: When a case is resolved, `outbox.status` permanently remains `RECONCILIATION_REQUIRED` because its transport outcome was ambiguous. It is never set to `DELIVERED`.

## 7. Concurrency & Ordering Safety
- **Manual Resolution Claim Ordering**: The winning transition `OPEN -> RESOLVED` via atomic conditional update (`WHERE id = :id AND status = 'OPEN'`) executes before any reservation release, ledger insertion, ExternalOrderLink creation, or order status mutation. Losing transactions return HTTP 409 `ConflictError` before producing side effects.
- **Savepoint Handling on Ambiguity**: When transitioning to reconciliation, worker uses nested transactions (`begin_nested()`) catching only the expected `(owner_id, outbox_id)` collision. Unrelated integrity errors are re-raised. Outer order/outbox state is preserved.

## 8. Fixture Transmission Warning
The runtime mode `BROKER_SANDBOX_RECORDED_FIXTURE` executes recorded historical candle fixtures while transmitting order signals to the Upstox sandbox API if safety gates are enabled. The UI and documentation prominently warn:
> Recorded fixture signals in sandbox mode may transmit external order requests to the configured broker sandbox. Use caution and ensure sandbox credentials are valid.

## 9. Environment Configuration
- `UPSTOX_SANDBOX_NETWORK_ENABLED`: `"false"` (default offline; startup in prod/staging with `"true"` fails fast)
- `UPSTOX_SANDBOX_ACCESS_TOKEN`: read directly from environment at runtime
- `UPSTOX_SANDBOX_OWNER_ID`: single configured sandbox operator user ID
- `UPSTOX_SANDBOX_BASE_URL`: `"https://sandbox-api.upstox.com"`

## 10. PostgreSQL CI Requirement
- CI execution runs on clean PostgreSQL 16 Alpine (`ci.yml`).
- SQLite in local development uses strict table definitions, foreign keys, and atomic check constraints.
- In reporting: `PostgreSQL: CI-PENDING`.
