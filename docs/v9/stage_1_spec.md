# Stage 1 engineering specification — proposed, not implemented

Audited source: `7a022d3688e7576ccbeff9997b1a633f35d77f21`. The current user-supplied reconciliation of master §§6.1–6.3 and Stage 1 scope governs the stated lifecycle and G1 scope. The complete Notion corpus and its policy values have not been reviewed. Paths below are proposals unless marked existing; no code, tests, migrations or production activation are authorized by this document.

## Scope and boundaries

Stage 1A defines a read-only adapter over existing registry records, metadata validation, contracts, lifecycle, dependency graph and a disabled fixture builder. Stage 1B adds compatible collector/normalization changes, chronology and coverage integration, manifests, restart/gap handling, durable atomic publication, volume/load assessment and an isolated archive/restore demonstration. G1 is the combined gate: 1A types alone cannot pass it. Adaptive estimation is Stage 2. Legacy decisions, position management, risk, execution, notifications and journal behavior must remain unchanged. READY is eligibility for downstream evaluation only; it never grants account or universe permissions or changes portfolio limits.

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

`Identity = (venue, instrument_id, market_type, quote_currency)` (schema_version is representation metadata) uses nonempty canonical strings and an enumerated market type, with source-specific identity retained. `InstrumentMapping` contains separate explicit `spot_identity` and `derivative_identity`, mapping version, provenance, validity interval and validation status; mismatched venue/quote or ambiguous mappings fail validation unless a versioned approved policy explicitly permits them.

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

Stage 0: eight document texts reviewed and principal corrections resolved, but full audit acceptance, baseline evidence and runtime remain unverified; checkpoint 251eef7 was independently confirmed on GitHub. This is not evidence of production readiness.

## Stage 1A continuation evidence and allocation

Source `7a022d3688e7576ccbeff9997b1a633f35d77f21`; documentation commits
`6f04eb2ec51fe2d1a665895211c9c5573f0aeed5` and
`3ca53a6274a83808e5945c6fe703ef95291b8917`; implementation checkpoint/base
`251eef7b9a4c62732d9a0853516e57594355597c`. The continuation commit is the
commit containing this paragraph. Remote publication of that new commit must
be verified separately. The old checkpoint was independently confirmed on GitHub.

The adapter consumes caller-supplied rows from the existing instruments table,
with positive integer database IDs, explicit symbols and explicit mappings; source
identities in mappings must match the corresponding registry symbols. It never
loads configuration or reads the database itself. Decimal serialization strips
trailing zeros without consulting the ambient context. Fixture freshness takes an
explicit `evaluated_at` and `max_age`; no production freshness value is supplied.
Optional graph diagnostics can be inspected without becoming mandatory vetoes.
`Profile.available_at` is the fixture packaging/evaluation time; source and
computation availability must precede it. `Publisher` is an interface, not a
persistence implementation. Recovery and shadow evidence are fixture inputs;
READY grants no account, universe or trading permissions.

| Case | Allocation and evidence |
|---|---|
| S1-01 | 1A fixture covered/missing zero (`test_quality_scoped_and_zero`); real coverage 1B. |
| S1-02 | 1A field-scoped side/unit (`test_quality_scoped_and_zero`); live normalization 1B. |
| S1-03 | 1A graph, freshness and policy (`test_optional_defect_is_reported_without_veto`, `test_time_and_freshness_are_explicit`); coverage integration 1B. |
| S1-04 | Deferred 1B: immutable correction and source coverage. |
| S1-05 | Partial 1A fixture closed flag (`test_quality_scoped_and_zero`); collector chronology 1B. |
| S1-06 | Partial 1A time checks (`test_times_late_and_unknown`, `test_time_and_freshness_are_explicit`); historical integration 1B. |
| S1-07 | Partial 1A digest and decimal context (`test_decimal_digest_ignores_ambient_context`); manifest/replay 1B. |
| S1-08 | Partial 1A lifecycle (`test_lifecycle_no_automatic_authorization`, `test_recovery_requires_full_evidence`); integrated evidence 1B, system shadow Stage 7. |
| S1-09 | Deferred 1B: restart and gap recovery. |
| S1-10 | Partial 1A protocol and pinned fixture (`test_six_mapped_instruments_and_pinned_profiles`); durable atomic concurrent publication 1B. |
| S1-11 | Partial 1A six registry/profile fixtures and immutable supplied limits (`test_six_mapped_instruments_and_pinned_profiles`); portfolio integration later. |
| S1-12 | Partial 1A guarded import and selected existing registry regressions; comprehensive compatibility 1B. |
| S1-13 | Partial 1A graph/registry validation (`test_graph_validation_and_optional`, `test_mapping_ambiguity`, `test_registry_rejects_malformed_and_source_conflicts`); integration 1B. |
| S1-14 | Deferred 1B: isolated archive and restore. |
| S1-15 | Deferred 1B: capability inventory against source evidence. |
| S1-16 | Deferred 1B: measured fixture load. |

Validation environment: Python 3.12.14, pytest 8.3.4, Ruff 0.8.4.
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=tests/v9/offline_guard:. /workspace/analagentai/.venv/bin/python -m pytest -q tests/v9 tests/test_stage_8_1.py -k 'v9 or pairs_are_parsed or five_tokens_are_parsed or symbol_without_contract or duplicate_pair or horizon_parsing'`
reported 20 passed, 22 deselected, two warnings about disabled pytest-asyncio
configuration. `ruff check src/v9 tests/v9` passed. The sitecustomize guard
blocks socket connections before collection/import; it is a Python socket guard,
not an OS-level network namespace or proof about native subprocesses. No DB,
exchange, production service, deployment or credentials were used.

The above was the earlier partial checkpoint; the continuation below addresses
its quality provenance, policy validation and actual fixture import/build isolation. Historical coverage, production state, full source-audit
acceptance, persistence, load and restore remain unverified. G1 is unpassed.


## Completion continuation — isolated fixture evidence

`FixturePolicy` is frozen and contains a name, version, three explicit boolean
requirements, and an explicitly supplied optional nonnegative freshness age in
seconds. It estimates nothing. `assess` requires UTC evaluation time, a completed
analysis window and evidence references, and records those along with input refs,
policy identity, version and content digest in every quality result. Missing policy
produces `POLICY_MISSING`; invalid policy values are rejected. Known historical
arrival and availability are never synthesized; unknown times remain unknown and
produce `TIME_UNKNOWN`. The graph requires typed policies and checks the content
digest as well as name and version, scoped to the relevant mandatory dependency.
Conflicting policy names are rejected. The builder checks quality context and
policy content against the profile and retains that immutable policy in its digest.
No threshold, window or production policy is inferred.

The fixture subprocess test installs an audit hook before importing v9 and executing
the real builder. It rejects Python audit events for sockets, process creation and
dynamic native loading, blocks thread and scheduler starts, compares environment
and live threads, and checks that DB, exchange, config and execution modules were
not imported. Its parent interpreter starts with the socket guard in sitecustomize,
including before test collection. The v9 source imports only stdlib and local v9
modules; stdlib may use already-loaded native extensions. This is targeted evidence
for these imports and builder, not an operating-system sandbox or proof against
arbitrary malicious native code.

S1 allocation in the preceding table remains applicable: S1-01, 02, 03, 05, 06,
07, 08, 10, 11, 12 and 13 have fixture-level 1A evidence and integration or durable
portions assigned to 1B (system shadow is Stage 7); S1-04, 09, 14, 15 and 16 are
deferred to 1B. `test_quality_provenance_and_policy_validation` and
`test_policy_content_conflict_is_scoped` add policy and provenance evidence;
`test_fresh_import_and_build_without_side_effects` adds A10 evidence.
Independent Stage 1A acceptance is pending. Full G1 remains unpassed; no claims
are made for live coverage, recovery, durable publication, restore or load.

## Independent review corrections R1–R4

The reviewed base is `4b82a9792806a7def5b1a660d71156985b87647c`.
`InputContext` binds a quality result to one business identity, input role and
unit. Every supplied observation must match this context; unknown units remain
explicit and fail only when required. A measurement retains its primary and
optional additional quality results and requires compatible units. All input
references from each quality result must occur in the measurement's source
references; additional source references are allowed for explicit derivation.
Coverage evidence references remain separate. A profile permits a different
input identity only when a supplied immutable spot/derivative mapping links it
to the profile identity, has the declared mapping version, and is valid at the
evaluation instant. No symbol or unit conversion is inferred. This is a
fixture-level provenance rule, not verification that a source itself is true.

When `max_age_seconds` is supplied, covered-window freshness is measured from
`window.end`, including an empty window. Unknown coverage remains MISSING; an
expired covered interval is STALE, never a valid zero. Nonempty observations
also retain their event-time freshness check. No production age is provided.
Mapping identities and validity, observation identity/window, quality context,
measurement children, profile mappings and graph nodes are validated as their
immutable contract types. Graph policy conflicts are reported by
`policy_diagnostics`; only consumers whose mandatory closure requires a
conflicted name are denied. Optional and unrelated conflicts do not veto them.

A1–A10 fixture evidence remains as above, with R1–R4 covered by
`tests/v9/test_review_regressions.py`. S1-01/02/03/05/06/07/08/10/11/12/13
remain partially covered at fixture level; their integration/durability portions
remain in 1B or later, as listed in the prior table. S1-04/09/14/15/16 remain
deferred to 1B. Neither independent acceptance nor G1 is claimed.

## Stage 1B.1 offline foundation — fixture scope

Base: `dae740873776e2c03899599efb99f104205ca599`, tree
`3d0dcdb11b1e496de94609ab9f2ad9ff10e843ba`. The pure opt-in adapter in
`src/v9/offline_data.py` accepts explicit immutable fixture fields; it never
loads legacy collectors, configuration or a database. It preserves raw values,
units, source references, source IDs and optional mapping version. It rejects
unavailable inputs at cutoff and marks side, units, OI field semantics and
mapping failures separately. There is no currency/unit conversion. A full
last-N poll, rollup or order-book snapshot is not proof of continuity.
`Coverage` accepts caller-supplied fixture evidence scoped to stream and window;
its independently-verified marker is an assertion by the fixture, not an
external verification or recovery of historical coverage.

`offline_replay.py` records canonical checksums, versions, exclusions, cutoff,
coverage, a validated fixture-policy digest and output digest. Event time/source ID/reference form a deterministic
tie-breaker, not an assertion about true exchange sequence. A later revision of
an already available source ID is invisible before its availability time.
`Revision` and `correct` model in-memory immutable fixture corrections only;
input availability and complete coverage must be supplied, and the original is
retained. `Checkpoint` encodes and reloads stable IDs and checksums. Resume marks
conflicting duplicates, unsupported cursor and repair intervals; it cannot move
the verified boundary over a gap. Recovery of SUSPENDED requires explicit valid
evidence, policy and freshness and returns at most DATA_CHECK.

| Case | Current evidence and remaining work |
|---|---|
| S1-01 | Fixture covered zero, unknown and gap: `test_coverage_quality_and_optional_scope`; real evidence integration remains. |
| S1-02 | Side/unit and zero OI fixtures: `test_explicit_normalization_and_capabilities`, `test_open_interest_zero_and_mapping_diagnostics`; exchange semantics remain unverified. |
| S1-03 | Mandatory/optional gap: `test_coverage_quality_and_optional_scope`; live propagation remains. |
| S1-04 | Immutable correction: `test_immutable_revision_cutoff`; durable revision persistence is 1B.2. |
| S1-05 | Candle flag and cutoff: `test_time_cutoff_unknown_and_candle`; real source chronology remains. |
| S1-06 | Unknown and late availability: `test_time_cutoff_unknown_and_candle`, `test_manifest_replay_reproducibility_and_revision_cutoff`. |
| S1-07 | Deterministic fixture manifest/replay and changed input/version: `test_manifest_replay_reproducibility_and_revision_cutoff`. |
| S1-08 | Fixture suspension and recovery: `test_restart_overlap_conflict_gap_and_recovery`; system shadow is Stage 7. |
| S1-09 | Serialized checkpoint, overlap, conflict and gap: `test_restart_overlap_conflict_gap_and_recovery`; actual source recovery remains unverified. |
| S1-10 | Deferred 1B.2: durable atomic publication and pinned reads. |
| S1-11 | Six fixture identities: `test_six_generic_instruments_scoped` and Stage 1A profile regression; portfolio integration is later. |
| S1-12 | Disabled adapter and legacy parser: `test_legacy_parser_unchanged_when_adapter_disabled`; broad consumer/runtime side effects remain unverified. |
| S1-13 | Existing Stage 1A registry/mapping/cycle regressions; no collector routing changed. |
| S1-14 | Deferred 1B.2: isolated archive/restore. |
| S1-15 | Explicit unsupported snapshot/liquidation/OI capability reasons: `test_explicit_normalization_and_capabilities`; live capabilities unverified. |
| S1-16 | Deferred 1B.2: measured load and volume. |

Offline sample at fixed fixture times: manifest digest
`39a23a8336672c893da76460cf26d6a140412c6f41eccd67197737a80500e924`;
output digest `8a1f0b4907054db1ffdd4a89948874f0371e0def11066528654774c799129fd9`.
Changing the fixture value yields manifest
`3c1829ef151e8e4fa67e6e6def32df7eb05751553b4bd7648db7b2061b57eefe` and output
`b0e1253af5ddf527a775f2dd32d3c5fc91392ff9577da88ad519d00667d67f10`.
These are fixture evidence, not production or full G1 acceptance.

## Stage 1B.1 independent-review corrections

Review base: `ee5aa188ffeb1cb127d6f3e285a54e1033320849`, tree
`de23caa9d54a9eb6dc31f4842d1f78fade27c0e1`. Findings and fixture evidence:

| Finding | Implementation and test evidence | Remaining scope |
|---|---|---|
| S1-02/03/13 mapping | `MappingRequirement` binds consumer identity, source identity, role and explicit mapping. `sample_replay` excludes absent, wrong or expired required mapping and records source quality; `evaluate_replays` feeds that quality through the existing Graph. `test_required_mapping_and_graph_from_actual_replay`, `test_graph_uses_replay_quality_for_exclusions_and_optional_gaps`. | Source metadata is supplied by the fixture; live mapping and collector integration remain unverified. |
| S1-06/07 determinism | Conflicts are checked for every visible `(source ID, availability)` group before selecting a revision; tied exclusions use complete canonical digest as a tie-breaker. Manifest retains field declaration, closed-candle rule and mapping provenance. `test_replay_visible_conflicts_and_tied_exclusions_are_order_independent`. | Exchange sequencing is not inferred; sample data and replay are offline. |
| S1-03/08/09 restart | Checkpoint `semantic-v1` fingerprints source semantics independently of capture reference and receipt times; capture provenance is retained separately. Conflicts are resolved before emitting any candidate, out-of-interval events are excluded, and explicit gapped coverage returns repair bounds without moving the boundary. `test_restart_deduplication_conflicts_and_scoped_repairs` and `test_legacy_checkpoint_requires_explicit_migration`. | Old checkpoints lacking a fingerprint format with nonempty seen state fail closed and require separately justified migration; no durable store or exchange repair is implemented. |
| S1-04/05/06 chronology | Candle kind and close instant are explicit; closed candles available before close are excluded, ticks do not use candle-close rules. Revision completion must be strictly later than predecessor availability. `test_candle_close_and_strict_revision_chronology`, `test_closed_candle_at_and_after_close_and_valid_revision`. | Correctness of upstream candle close times remains an external source-evidence obligation. |

The Stage 1B.1 sample's manifest format changed to include quality and the
normalization/mapping policy. For the same fixture, current manifest digest is
`56ec893e4f565e3d7af2ec87a1084501be8d8220cc327660f025049c723440d6`, output
`8a1f0b4907054db1ffdd4a89948874f0371e0def11066528654774c799129fd9`;
changing the input gives manifest
`c9a7523fa8f6036e470d4e0bcfdc81193cfe3e10d98d6d4b8edc76dada2645e5` and output
`b0e1253af5ddf527a775f2dd32d3c5fc91392ff9577da88ad519d00667d67f10`.
Previously documented digests describe the review base, not this new format.
S1-10/14/16 and durable revisions remain in 1B.2; G1 remains open.

## Stage 1B.1 integration-review corrections

Base commit `f2f67b2cf3a1d220bc8186ff34a292e02e70d4af`, tree
`97a2a27cf393d9884bd2a19905dd3e00ae58c54f`. Fixture traceability:

- S1-02/03/13: `evaluate_replays` checks each mandatory edge in its actual
  transitive consumer closure. Different instrument identities require the
  source manifest's mapping to name the actual consumer and source, retain the
  role/source provenance and be valid at the consumer cutoff. Optional edge
  failures are diagnostics, not vetoes. `test_graph_binds_mapping_to_each_actual_edge`
  and `test_mapping_revalidated_at_consumer_cutoff` cover these paths.
- S1-03/08/09: `Checkpoint.unresolved` persists ID, interval, candidate
  fingerprints, evidence references and detection time; conflict captures are
  retained separately. Empty retries, time passage and unrelated records do not
  clear it. `RepairEvidence` requires an explicit chosen observed candidate,
  matching ID/interval and a distinct, later independently-verified coverage
  proof with its own evidence reference. Proof timestamps are checked at the
  evaluation cutoff. Repair and ordinary records for the same ID must be
  processed separately. `test_conflict_persists_until_explicit_verified_repair`
  and `test_repair_requires_matching_id_interval_candidate_and_new_evidence`.
- Existing serialized checkpoints lacking conflict-state format are labelled
  `legacy-untracked-v0` and cannot resume automatically; they require an
  independently justified migration/revalidation. Prior raw-digest format
  remains separately blocked. `test_prior_checkpoint_without_conflict_state_fails_closed`.

This is fixture-level evidence; supplied repair proofs are assertions, not
verification of a real source. Durable storage and integration remain deferred.
S1-10/14/16 remain in separately assigned 1B.2; Stage 1B.1 requires independent
review and G1 remains open. No production or trading permissions are granted.

### S1-03/08/09: repair of previously emitted fixture events

Review base `c55d8ca4c87433bc75285728f42543a2dae272c4`: a repair that
selects a different semantic fingerprint for an already emitted source ID is
rejected as `EMITTED_RECORD_CORRECTION_REQUIRED`. The immutable checkpoint and
unresolved conflict remain available to keep lifecycle SUSPENDED. Confirmation
of the previously emitted payload clears the conflict without another emission;
selection for a never-emitted ID emits the selected observed payload once.
`tests/v9/test_emitted_repair.py` exercises all three cases across checkpoint
reload and retry. Durable correction or retraction for already consumed records
requires separately assigned persistence work; this fixture does not perform it.
