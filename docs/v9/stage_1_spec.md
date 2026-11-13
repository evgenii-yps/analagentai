# Stage 1 engineering specification — proposed, not implemented

Audited source: `7a022d3688e7576ccbeff9997b1a633f35d77f21`. The current user-supplied reconciliation of master §§6.1–6.3 and Stage 1 scope governs the stated lifecycle and G1 scope. The complete Notion corpus and its policy values have not been reviewed. Paths below are proposals unless marked existing; no code, tests, migrations or production activation are authorized by this document.

## Scope and boundaries

Stage 1A defines registry, metadata validation, contracts, lifecycle, dependency graph, compatible collector adapters and a disabled deterministic builder. Stage 1B adds fixture-driven chronology and coverage checks, manifests, restart/gap handling, volume/load assessment and an isolated archive/restore demonstration. G1 is the combined gate: 1A types alone cannot pass it. Adaptive estimation is Stage 2. Legacy decisions, position management, risk, execution, notifications and journal behavior must remain unchanged. READY is eligibility for downstream evaluation only; it never grants account or universe permissions or changes portfolio limits.

Existing integration points: `src/core/instruments.py`, `src/core/config.py`, `src/core/db.py`, `src/collectors/{trades,ohlcv,orderbook,futures}.py`, `db/init.sql`, `scripts/retention.py`, `backtest/`. They remain the authoritative legacy paths and must not be silently replaced.

## Proposed files and interfaces

- `src/v9/contracts.py`: immutable dataclasses/enums and validation for `Identity`, `InstrumentMapping`, `Observation`, `QualityResult`, `ProfileVersion`, `DatasetManifest` and `LifecycleState` below. UTC-aware instants; reject naive or inverted times and invalid identities. Serialization uses canonical field order and explicit schema version.
- `src/v9/registry.py`: `Registry.validate(entries, mappings) -> ValidationReport` and `Registry.resolve(identity) -> InstrumentMetadata | None`. A single logical registry owns versioned identities and mapping references. Duplicate keys, conflicting metadata and orphan or ambiguous mappings are errors; no symbols inferred from string concatenation. Adapters can read the existing registry without changing legacy consumers.
- `src/v9/normalization.py`: `normalize(raw, identity, metadata, received_at) -> Observation | QualityResult`. Preserve raw references, reject unknown units/side as UNSUPPORTED, never infer receipt time for legacy history. `received_at=None` is allowed only with an explicit unknown reason; it cannot qualify as a known replay availability.
- `src/v9/quality.py`: `assess(observations, window, policy, evaluated_at) -> QualityResult`. Return status and independent coverage/activity fields; `MISSING` is not numeric zero. Unknown policy values yield INSUFFICIENT_HISTORY or UNSUPPORTED with reason and prevent eligibility when mandatory. Boundaries use `[start,end)` in UTC.
- `src/v9/dependencies.py`: `validate_graph(nodes, edges) -> ValidationReport`, topological order, cycle rejection, independently specified module input windows and confirmation requirements. Missing mandatory upstream quality blocks dependent eligibility.
- `src/v9/profiles.py`: `build_profile(snapshot, policy, evaluated_at) -> ProfileVersion` is pure and deterministic and disabled by default; explicit fixture invocation is allowed. Inputs include stable source references and canonical policy. No adaptive calibration, live scheduler or external requests. `publish(profile, expected_generation) -> PublicationResult` is an interface only in 1A; 1B supplies an isolated test implementation using a transaction or atomic compare-and-swap. The transaction validates version uniqueness, persists immutable payload/provenance then advances a single current pointer; failures/conflicts leave neither partial visibility nor pointer changes. Prior versions are readable; never update them in place.
- `src/v9/lifecycle.py`: `transition(current, evidence, policy, evaluated_at) -> TransitionResult` implements the rules below, including denial reasons and audit references; no side effects and no authorization grant.
- `src/v9/manifests.py`: `create_manifest(inputs, bounds, versions) -> DatasetManifest` records source identity and stable input identifiers/checksums, window, units, coverage, event and availability bounds, unknowns, schema/calculation/policy versions and generation time. Unknown receipt/availability remains unknown and makes the affected point-in-time dataset unsuitable unless a separately approved, provable availability rule exists.
- `src/v9/recovery.py`: `assess_restart(checkpoint, observed_ranges, policy) -> RecoveryPlan` detects gaps/overlap and returns explicit repair requirements; it does not send requests, silently fill missing data, or mutate production. A restored profile remains suspended until its evidence passes recovery checks.
- Proposed tests: `tests/v9/test_{contracts,registry,normalization,quality,dependencies,profiles,lifecycle,manifests,recovery,compatibility}.py`; isolated restore integration test `tests/v9/test_restore.py` with temporary storage/DB only. These files do not yet exist.

## Data contracts

`Identity = (schema_version, venue, instrument_id, market_type, quote_currency)` uses nonempty canonical strings and an enumerated market type, with source-specific identity retained. `InstrumentMapping` contains separate explicit `spot_identity` and `derivative_identity`, mapping version, provenance, validity interval and validation status; mismatched venue/quote or ambiguous mappings fail validation unless a versioned approved policy explicitly permits them.

`Observation` contains identity, source, input reference, value (nullable), unit (explicit or unknown), side (buy/sell/unknown where applicable), event_time, received_at (nullable with reason), available_at (nullable with reason), and window bounds when aggregated. `event_time` is market event time; `received_at` is actually observed arrival; `available_at` is when this datum became usable, not a fabricated earlier value. For derived results `available_at >= max(actual input available_at, computation completed_at)`; unknown input availability propagates unknown. `evaluated_at` is the decision/evaluation time and cannot precede any input's availability. A historical dataset lacking receipt records must not invent them or claim point-in-time eligibility.

`QualityResult` contains `status` in `VALID, MISSING, STALE, GAP, INSUFFICIENT_HISTORY, UNSUPPORTED`, explicit reason codes, evaluated_at, requested/observed window bounds, source references, unit, coverage evidence and activity evidence as separate fields. Complete coverage with zero activity differs from missing or unknown coverage; confidence is never manufactured from zero-filled gaps. Incomplete mandatory coverage blocks readiness. Unsupported source capabilities remain explicit. Closed-candle policy is parameterized; unfinished candles are marked and excluded when closed inputs are required.

`ProfileVersion` contains identity and explicit mapping reference, `schema_version`, `policy_version`, immutable unique `profile_version`, `calculation_version`, `data_cutoff`, input windows and references, per-module windows and confirmation requirements, `available_at`, `evaluated_at`, `applicable_from`, `expires_at`, `sufficiency`, quality results, provenance, lifecycle evidence and deterministic content digest. Enforce availability no later than applicability, expiry after applicability and evaluation no earlier than all known inputs; unknown times fail eligibility. Episode/profile pinning is a future consumer requirement: consumers retain a specific immutable version, not the current pointer. Policy parameters are global or derived by approved automatic methods; no manual per-coin thresholds or automatic portfolio-risk expansion.

## Lifecycle and separate dimensions

Proposed state sequence: `DISCOVERED → DATA_CHECK → WARMUP → SHADOW_VALIDATION → READY`. Any stage can enter `SUSPENDED` on failed mandatory evidence, unsupported inputs, expired profile, revoked authorization evidence or data gap; lifecycle state does not replace quality, expiry or account/universe permission. Versioned policy determines numerical criteria without embedding unapproved values.

- DISCOVERED → DATA_CHECK: validated registry identity, metadata, explicit mappings, capability inventory and dependency graph.
- DATA_CHECK → WARMUP: units and source provenance known, chronology valid, mandatory coverage and configured freshness demonstrated; missing policy fails closed.
- WARMUP → SHADOW_VALIDATION: required history and independently defined module windows/confirmations satisfy configured sufficiency; immutable candidate and manifest available.
- SHADOW_VALIDATION → READY: deterministic sample replay matches its stored digest, configured shadow evidence passes, current profile is applicable and unexpired, and mandatory inputs remain valid. READY does not override account/universe permissions.
- Any → SUSPENDED: record failing quality, evidence and last safe state; no implicit restart. SUSPENDED recovery requires registry/mapping revalidation, gap/chronology and freshness checks, valid policy, compatible immutable profile, replay/checkpoint validation and renewed shadow evidence when policy requires it. Resume at the earliest stage whose evidence remains valid; never jump to READY solely from elapsed time.

## Implementation order, storage and recovery

1. Inventory adapter fields against existing registry and collectors; preserve legacy formats and make any normalization correction opt-in behind disabled configuration. Detect truncation, unknown side/units, missing timestamp and polling coverage limitations; do not claim recovery of missing historical data.
2. Implement contracts, deterministic validation, graph and disabled builder with fixture tests; no external service starts. Unresolved calibration thresholds are explicit policy inputs and do not block engineering of these checks.
3. In isolated fixtures implement chronology/coverage and manifests; verify restart with overlapping polls and missing intervals. Record counts, bytes, load assumptions, instrument count and extrapolation method; do not assume measured production capacity.
4. Design retention/archive formats, checksums, retention boundaries and provenance without changing current retention. Demonstrate archive and restore on an isolated copy and compare manifest, immutable profiles, data checksums and sample replay. Do not issue cleanup or production migrations. Existing `trade_flow_1m` conflict-do-nothing preserves aggregates when raw history is partially deleted. Never blindly UPSERT/recalculate. A later correction requires demonstrably complete source coverage, immutable revisions and explicit authorization.
5. Validate legacy consumers against unchanged fixtures. Any persisted schema design/migration and rollback are a separately reviewed implementation decision; no migration is executed in Stage 0. Failed publication leaves previous pointer and prior versions intact; rollback disables the new consumer and restores the previous pointer without deleting history.

## Acceptance tests — proposed, not executed

| ID | Input/action | Expected result |
|---|---|---|
| S1-01 | Covered interval with no events vs missing interval | valid zero activity with coverage vs MISSING/GAP, never equal |
| S1-02 | Trade with unknown side or OI with unknown unit | UNSUPPORTED with provenance/reason; never invent side/unit |
| S1-03 | Gap in mandatory source | GAP and no eligibility; optional missing source remains explicit |
| S1-04 | Late event after aggregate, including partly deleted raw | existing aggregate unchanged; correction rejected without complete-source evidence and immutable revision |
| S1-05 | Open candle under closed-candle policy | excluded/insufficient until closed and available |
| S1-06 | Input available after evaluated_at; historical receipt missing | rejected for point-in-time use; no invented receipt timestamp |
| S1-07 | Same canonical manifest, policy, inputs and evaluated_at twice | identical payload and digest; changed input/version changes provenance |
| S1-08 | Valid evidence for each transition; mandatory gap, expiry, then recovery | ordered lifecycle, SUSPENDED with reason, rechecks required; READY never grants permission |
| S1-09 | Restart with overlap and a missing segment | overlap deduplicated, missing segment recorded; no silent completeness or premature READY |
| S1-10 | Concurrent publish/failure and existing pinned episode | one atomic current pointer, no partial publication; old immutable version remains readable and pinned |
| S1-11 | Six instruments vs fewer with same approved portfolio limits | all six have independent identities/profiles; aggregate risk limits unchanged |
| S1-12 | Existing legacy consumer fixture before/after disabled adapter | identical interfaces, outputs and side effects; no external call |
| S1-13 | Registry collision, orphan mapping, dependency cycle | validation rejects with actionable reason |
| S1-14 | Archive isolated dataset and profiles, restore to fresh isolated copy | matching checksums, manifests, provenance and deterministic sample replay |
| S1-15 | Source lacks liquidations or sequenced order-book deltas | capability UNSUPPORTED, never represented as valid continuous data |
| S1-16 | Fixture load assessment with documented scale/size | measured counts/bytes/duration and assumptions recorded; no production-capacity claim |

G1 passes only if S1-01–16 pass at the applicable phases, known defects are detectable, mandatory gaps block eligibility, sample replay is deterministic, isolated restore succeeds, profile provenance survives, and unsupported capabilities remain explicit. Policy values required for eligibility must be approved and supplied; missing values block that eligibility, not offline contract development. The original Notion requirements and exact deployed state remain unverified and must be reconciled before acceptance.

## Full roadmap

0. Audit and independent review. 1. Data foundation, registry, quality/profile lifecycle and G1. 2. Analytical modules and adaptive estimation. 3. Scenarios, temporal confirmation, episodes and pinned versions. 4. Decisions, positions and portfolio risk. 5. Execution adaptation, exclusive ownership and recovery. 6. Calibration, chronological replay and independent validation. 7. Forward shadow without order-submission capability. 8. Separately authorized demo pilot and new Sheets journal. 9. Separately authorized rollout, verified cleanup and Telegram.

## Stage 1A implementation status (fixture-only)

Business identity is `(venue, instrument_id, market_type, quote_currency)`; `schema_version` is representation metadata and is excluded from the business key. Lifecycle, quality, expiry and permissions are independent. The fixture builder packages supplied measurements and policy identifiers without calibration; fixture shadow evidence is not Stage 7 system forward-shadow evidence. Optional defects affect only dependent consumers. This branch contains no runtime wiring, SQL or exchange operations.

S1-01 partly covered by `tests/v9/test_contracts.py` (zero versus missing); S1-02 partly covered (field-scoped unknown side/unit); S1-03 partly covered (fixture quality and graph); S1-04 deferred to 1B (source coverage and immutable corrections); S1-05 partly covered (fixture closed flag); S1-06 partly covered (timestamps; historical replay integration deferred); S1-07 partly covered (fixture digest; full sample replay 1B); S1-08 partly covered (fixture lifecycle; real shadow evidence later); S1-09 deferred 1B; S1-10 interface only, durable atomic publication 1B; S1-11 identity/unchanged supplied limits only, integration deferred; S1-12 partly covered by import/config regression, consumer compatibility 1B; S1-13 partly covered (cycles, unresolved nodes, collisions); S1-14–16 deferred 1B. Full G1 remains unpassed.

Stage 0: eight document texts reviewed and principal corrections resolved, but full audit acceptance, baseline evidence, remote publication and runtime remain unverified. This is not evidence of production readiness.
