# Strategy orchestration: Milestone 6C, Phase 1

Phase 1 defines pure contracts, identities, three persistence tables and migration
`0006_strategy_orchestrator`. It does not implement a worker, importer, API,
activation, scheduled evaluation, order creation, frontend, or broker transmission.
TradePro remains a sandbox/pre-production validation platform in this milestone.
Live trading is prohibited.

## Approved execution boundary

The only supported source is `FIXTURE_REPLAY`. Its execution policy is always
`INTERNAL_MOCK_ONLY`; the immutable snapshot explicitly records
`external_transmission_allowed=false`. `REALTIME_PROVIDER` is unsupported and
rejected, pending authoritative real-time integration in Milestone 6D.

Automatic historical signals must never reach the real Upstox HTTP adapter, even
when `UPSTOX_SANDBOX_NETWORK_ENABLED=true`. Phase 1 models this prohibition; later
phases MUST enforce it at both orchestration and outbox transmission boundaries
before enabling any automated producer. Existing 6B manual execution is unchanged.
No existing worker consumes these tables in Phase 1. This schema alone is not a
runtime transport gate. Tests use disposable databases and no real provider calls.

Fixture authority means an approved, checksum-pinned replay source, not authoritative
exchange data. Runtime-scoped candles are intentional: each runtime pins immutable
source configuration, so identical data across runtimes remains isolated.

## Pure contracts and candle acceptance

`OrchestrationSnapshot` freezes owner/runtime, strategy version and content,
action/risk policies, instrument specification, exact mapping ID/version,
verification/expiry, source namespace, ordered dataset provenance, alignment,
replay bounds, engine versions and execution policy. Structured snapshot sections
are bounded canonical JSON object strings, so nested mutation cannot bypass a
frozen Pydantic model. Dataset/series collections are tuples of frozen contracts.
No runtime version is used as a replacement for this full snapshot identity.

`CompletedCandle` contains source namespace/event/type, dataset/checksum,
reference/subject role, instrument, timeframe, UTC open/close/receipt timestamps,
integer OHLCV and scales, explicit closed flag and revision 1. Its
`content_fingerprint` property derives the canonical fingerprint; callers cannot
supply a mismatching hash to the domain constructor.

`accept_completed_candle(payload, clock=...)` is the time-sensitive acceptance
boundary. The clock is injected outside the payload. Structural constructors alone
do not assert current wall time; callers MUST use the acceptance boundary before
persistence. It rejects future receipt/close and receipt before close.

The approved manifest contains only 5m and 15m; Phase 1 supports only those
interval contracts. The only 5m dataset is a short SUBJECT series, so this does
not claim that a complete 5m REFERENCE-driven runtime can currently be configured.
`packaged_alignment_v1` explicitly approves the six packaged datasets, each with
`alignment_offset_seconds=0`. This is a fixture observation, not an exchange rule.
The internal snapshot factory derives the offset from that server policy and
rejects request-supplied owner/runtime/source-policy fields. ORM insertion checks
manifest provenance and policy again. Source policy/version/offset are frozen.
A new source-policy version is required to expand operational timeframes.

All timestamps are UTC-aware and normalized before arithmetic. The offset must
be a strict integer with `0 <= offset < timeframe_seconds`. Acceptance requires
`open_epoch_seconds % timeframe_seconds == offset`, whole-second boundaries and
`close_at == open_at + timeframe_duration`. The acceptance boundary requires the
frozen snapshot and checks its owner/runtime/source/alignment. Nonzero offsets
are supported by pure contracts; the 30m/1h arithmetic tests do not approve those
intervals operationally. No exchange-calendar interpretation is implied.

Prices are positive, volume nonnegative, each at most 9,000,000,000,000,000 units;
price/volume scales are 0 through 8. High must bound open/close/low and low must
bound open/close. `scaled_units` accepts exact decimal strings, Decimal or integer
inputs, rejects binary floats/non-finite values and excess precision, and never
rounds. Conversion is independent of the ambient Decimal context. Legacy fixture
float loading must not be reused as an exact ingestion representation.

Source identifiers are 1–100 restricted ASCII characters; resource IDs are 1–36;
checksums are 64 lowercase hexadecimal characters. Domain contracts forbid extra
fields. Revision 1 and completed candles only are accepted. Corrections would
require a future versioned event/revision contract; 6C does not reevaluate them.

## Canonical identities

All algorithms hash UTF-8 canonical JSON containing `algorithm` and `payload`.
Keys are sorted, separators compact, Unicode ASCII-escaped, and aware timestamps
normalized to UTC with six fractional digits and `Z`. Lists retain order. Floats,
Decimals, non-string object keys and implicit conversions are rejected. Exact
decimal values in snapshot material must be explicitly represented as units or
decimal strings before constructing the contract. Existing fingerprint algorithms
and their golden vectors are unchanged.

* `orchestration_snapshot_v1`: the complete frozen snapshot, decoding its canonical
  object strings into JSON objects in the hash payload.
* `completed_candle_v1`: all candle contract fields except delivery event ID and
  receipt time. Re-delivery aliases/timing cannot change content identity. Owner,
  runtime, source namespace, scales and provenance remain material.
* `runtime_evaluation_v1`: owner/runtime, complete snapshot hash, mapping ID/version,
  timeframe, close boundary and ordered required-series identities/fingerprints.
  Reference and subject ordering is material, never silently sorted.

Golden vectors in `tests/test_orchestration_contracts.py`, for the fixed fixture in
`tests/orchestration_support.py`:

| Algorithm | SHA-256 |
|---|---|
| orchestration_snapshot_v1 | `44ac537bfb45f26564d11f4c47bf0688f4644239b4945658df10a73502cb0568` |
| completed_candle_v1 | `a0806f39e230d998541a1f6fc585606493e0bdc3951cf7b30463fc07b88ad3ef` |
| runtime_evaluation_v1 | `6f5a5eaa49b076dca1521cd1e18a0583a5340ee1c565bcc2c89a72202728972a` |

The exact snapshot hash preimage is committed as
[orchestration_snapshot_v1_golden.json](orchestration_snapshot_v1_golden.json)
(excluding its final newline); the golden test compares it byte-for-byte. Snapshot
and candle material replace `alignment_policy: UTC_EPOCH` with
`source_policy_version: packaged_alignment_v1` and `alignment_offset_seconds: 0`.
Both hashes therefore change. Evaluation material keeps its schema but embeds
the changed snapshot and candle hashes, so its hash changes transitively.
No pre-6C fingerprint implementation or expected hash is edited.

Exactly once means one committed evaluation and its database effects per immutable
runtime snapshot/interval. Pure computation may repeat after a pre-commit crash.
No claim is made about exactly-once network delivery. Existing 6B ambiguity and
reconciliation behavior remains authoritative.

## Persistence and constraints

`runtime_orchestration_configs` has one row per runtime and no activation status.
It stores source policy, snapshot/hash, structured consent, approved replay
bounds, checkpoint and lease/retry metadata. Existing `StrategyRuntime.status` is
the sole lifecycle. Source is constrained to FIXTURE_REPLAY and execution policy
to INTERNAL_MOCK_ONLY. Fencing generation is positive; retries are bounded 0–100;
lease owner/expiry must both be present or both absent; checkpoint must be within
the approved bounds. Snapshot text is at most 262,144 ASCII characters/bytes; reason codes are 64.
Consent contains policy version `fixture_consent_v1`, a UTC confirmation timestamp
and canonical SHA-256 fingerprint bound to source, replay bounds, execution policy
and full snapshot fingerprint. The owner is also the confirming user: an explicit
`owner_id -> users.id` RESTRICT FK persists that identity without a second,
potentially contradictory user column. `prepare_configuration` derives it from
the authenticated server user and owned runtime; it returns an unsaved record.
No request dump, notes, tokens, cookies or authorization fields are stored. Retry and lease-expiry indexes support a future scheduler.

`completed_candle_events` stores immutable accepted candle content. Unique keys
protect (owner,runtime,content hash), source/series/dataset/event ID, and
(owner,runtime,role,instrument,timeframe,close). A differing event ID or content
cannot bypass the interval constraint. Future ingestion must compare conflicts and
return an existing row only for proven identical content; a database uniqueness
error itself is not proof of a benign duplicate. Series/close indexing supports
bounded historical reads.

`runtime_evaluations` stores only finalized rule statuses TRUE/FALSE/UNAVAILABLE/
INVALID, action outcome NO_ACTION/REJECTED/ACCEPTED_INTERNAL, and risk outcome
NOT_RUN/REJECTED/ACCEPTED. Nonaccepted actions require a bounded no-order reason;
accepted internal actions require accepted risk and an available boolean rule
result. ON_FALSE policies remain possible. There is no broker/order status here.
Ordered required-candle evidence is bounded to 2,048 characters, audit and risk
summary to 65,536 each. Unique evaluation hash and owner/runtime/timeframe/close
keys prevent duplicate effects. A history index supports owner/runtime inspection.

All three tables require ownership and UTCDateTime fields. All new FKs begin with owner_id and use ON UPDATE/DELETE RESTRICT.
The confirming-user FK references the users primary key; resource FKs are composites. Configuration points to
the runtime; candles to the same runtime's source namespace/timeframe/policy/offset; evaluation
to the exact owner/runtime/config/snapshot/timeframe and owned runtime candle IDs.
Parent composite unique constraints precede dependent FKs. `0004` already supplies
the owner-first runtime unique key; ORM metadata now also declares that key.

Database checks enforce sizes, supported states, ownership references, finality,
numeric bounds, OHLC geometry and timestamp ordering. Pure domain validation and ORM insertion guards enforce exact duration/alignment,
hexadecimal syntax, canonical JSON and snapshot/candle/consent hash agreement.
Snapshot sections accept a bounded allowlist of domain keys and bounded strings,
rejecting transport metadata, control characters and credential/trace patterns.
Audit evidence is a typed projection (`result`, bounded `condition_ids`); risk
evidence permits `outcome` and bounded `reason_codes`. Required-candle evidence
uses the frozen identity contract. Unknown fields are rejected, never silently
redacted or copied. These are internal storage contracts, not public responses.
Later services must verify evaluation-to-candle semantics, mapping readiness and
rule execution. Public responses must explicitly project fields and exclude owner
and confirming-user metadata; ORM/domain serialization must never be a response. SQL constraints alone cannot certify source authority or calculate
canonical hashes. No arbitrary provider/request payload is stored.

ActionDecision/OrderIntent associations are deliberately deferred until used.
RiskDecision's mandatory OrderIntent relationship is untouched. Pre-intent risk
evidence resides in runtime_evaluations, not a nullable or dummy intent.

### Integer storage portability

New integral columns use ExactInteger: SQLAlchemy binds only exact Python ints
(no strings, floats or bools). SQLite uses BLOB affinity to retain the input
storage class, plus dialect-specific `typeof(column) = 'integer'` checks. Thus
raw numeric text cannot be converted silently by INTEGER affinity. Stored valid
values are native SQLite integers. PostgreSQL uses unconstrained NUMERIC, so a
fraction survives assignment until the portable integral-equality CHECK rejects
it; native BIGINT assignment could round a numeric fraction before that CHECK.
Both dialects enforce the same explicit bounds. Result processors return Python
ints. PostgreSQL DDL omits SQLite typeof checks and uses the shared bounds/integral
checks as its equivalent enforcement. No SQLite-only function reaches PostgreSQL.

Application SQLite engines enable foreign keys on every new connection; the
shared SQLite test engine does too. Alembic explicitly enables them before its
migration transaction. Migration DDL does not invoke ORM mapper guards.

## Immutability and limitations

There are no update APIs or database-specific triggers. ORM updates/deletes of
candles and finalized evaluations are rejected. Configuration source, identity,
snapshot, replay bounds and consent are immutable. Only checkpoint, lease,
fencing generation, retry schedule/count, last reason and updated timestamp can
change. ORM guards reject decreasing generation/checkpoints, including expired
ORM attributes. Later claim services must also enforce fencing with atomic
conditional database updates; Phase 1 guards are not a concurrency scheduler.

Raw SQL/bulk ORM DML bypasses mapper guards. Database constraints still apply but
do not enforce complete append-only privileges. Later services must use insert/read
operations for audit rows, and operational DB access must be appropriately limited.
Snapshots never store bearer tokens or credential references. Evidence must be
server-generated and sanitized; bounded strings are not a secret scrubber.

## Migration and verification

Graph: 0005_upstox_sandbox → 0006_strategy_orchestrator (sole head, 26-character ID,
within Alembic's installed 32-character version column). New tables are created in
configuration/candle/evaluation order. No merged migration is edited. Downgrade
checks every new table before DDL and refuses if any has rows; an empty downgrade
and re-upgrade are supported. No existing runtime becomes automated.

Tests use temporary SQLite files or one reusable isolated schema in the existing
CI PostgreSQL database. They never create per-test databases. Rows are cleared in
child-first order between tests, and only that generated schema is dropped.
A missing PostgreSQL URL skips PostgreSQL parameters; connection, setup or DDL
failures fail the suite. Every configured PostgreSQL parameter runs in CI,
including fractional-integer cases (there are no SQLite-only skip branches). They check schema parity, all CHECK
definitions and behavior, identity collisions, owner/config/candle consistency,
immutability and downgrade safety. PostgreSQL execution is CI-PENDING locally.
The development database must retain its SHA-256, size and mtime throughout checks.

## Mandatory later-phase work

* Revalidate owner/role/account/mapping/expiry/risk/source readiness at activation
  and execution; explicitly bind consent to the full frozen configuration.
* Enforce the fixture-only transmission prohibition before creating automatic
  execution work and again before any real adapter call. Environment toggles must
  never override this source gate.
* PAUSE stops new claims; RESUME revalidates prerequisites; STOP permanently stops
  new evaluations. Existing intents/outbox operations continue under their gates.
  STOP must NOT automatically cancel broker orders. Any stop-and-cancel operation
  needs a separate future contract and confirmation.
* Make GET /api/v1/sandbox/connection strictly read-only. Move metadata creation
  into an authorized, CSRF-protected mutation.
* Inspect POST /strategies/validate cost and limits; require authentication or a
  strict bounded anonymous rate limit. It must never activate/persist automation.
* Implement bounded ingestion, ordering/missing-interval policy, leases/fencing,
  evaluator integration, transactional auditing and internal/mock execution.
* Add activation/evaluation APIs and Paper Trading Lab UI only in later phases.
