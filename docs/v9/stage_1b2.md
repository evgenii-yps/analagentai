# Stage 1B.2: isolated fixture persistence

This is an opt-in fixture adapter, not runtime wiring or a production migration.
`PostgresPublisher` implements the synchronous `Publisher` protocol using a
caller-owned psycopg 3 autocommit connection. No connection is created on import;
no environment variables or credentials are read by the adapter. Never supply a
shared or production database. `initialize` creates the `stage1b2` schema and
fails if it already exists. A missing current key has generation 0; its first
publication returns 1. The business key excludes identity schema version.
Profile versions are globally unique, as `read_version(version)` requires.

A transaction-scoped advisory lock and row lock serialize writers per key,
including the initially absent key. The version insert and pointer advance commit
together; a stale generation, duplicate version or exception rolls back both.
History and evidence reject UPDATE and DELETE by triggers. The schema owner can
still drop or truncate tables or bypass its own controls: these are fixture
isolation protections, not an adversarial security boundary. Caller-provided
connections must have autocommit enabled so each method's transaction is the
commit boundary. Do not share a connection between concurrent workers.

Serialization uses a versioned, explicit allowlist of immutable contract types,
including Decimal, UTC timestamps, nested quality, policy and mappings. Each
version stores its canonical content digest and serialized-payload SHA-256;
reads validate both. Fixture evidence is append-only by `(kind, item_key)` and
stores raw fields, revisions, coverage, policies, manifests and checkpoints.
The caller supplies evidence and identifiers; this layer does not assert that
fixture coverage proves real exchange coverage or automatically enforce revision
chain semantics. Validate a revision chain with the accepted `revision_at`
contract before treating it as usable. Legacy checkpoint formats are refused.

After an ambiguous COMMIT, reconnect and read both the globally unique version
and the current key/generation. Compare the pinned version's content digest and
business identity with the intended publication. If it exists and matches, the
version committed even if a later writer has since advanced the pointer. If it
is absent, inspect the current generation and reconcile with the caller's
operation record before retrying with an appropriate expected generation. A
mismatched existing version is a conflict. Never blindly republish after a lost
connection; this adapter provides no distributed exactly-once guarantee.

`archive` reads one repeatable-read snapshot and returns an uncompressed JSON
archive with a format version, checksums for payloads and an overall SHA-256.
The checksum detects accidental corruption; it is not a signature or an
independent trust anchor. Preserve a trusted external checksum and protect the
archive separately. `restore` validates format, types, checksums, profile digests
and pointer bindings, then creates and populates a *new* schema in one database
transaction. Duplicate entries and SQL errors roll back schema creation. Only
expose a restored database after restore returns and the caller independently
checks expected inventory and replay digests. No durable retraction or correction
of already consumed events is implemented. Keep old archives and pinned versions;
rollback of this fixture adapter means stop its callers and retain the isolated
database/archive for investigation. Drop a test schema only after explicitly
confirming that it is disposable; never use this as a legacy-history cleanup.

## Repeatable local drill

Both explicit targets are inspected read-only before any fixture writes; an
existing `stage1b2` schema on either rejects the pair. The targets must be
loopback databases named `stage1b2_source` and `stage1b2_restore`. Each drill
and benchmark repeat then creates its own PostgreSQL container and fresh pair
of databases. Cleanup removes only that container by its captured 64-digit ID,
including its anonymous volumes; it never drops a schema in a supplied target.
The local Docker socket and cached `postgres:16-alpine` image are required.
Install the optional `stage1b2` dependency and pytest in an isolated Python 3.12
environment. The following preflight server is also owned by this invocation:

```sh
STAGE1B2_DOCKER='--host=unix:///var/run/docker.sock'
STAGE1B2_PREFLIGHT_ID=$(docker "$STAGE1B2_DOCKER" create --pull=never -p 127.0.0.1::5432 \
  -e POSTGRES_PASSWORD=fixture-only postgres:16-alpine)
docker "$STAGE1B2_DOCKER" start "$STAGE1B2_PREFLIGHT_ID"
# Wait for PostgreSQL readiness, then create the two disposable preflight databases.
docker "$STAGE1B2_DOCKER" exec "$STAGE1B2_PREFLIGHT_ID" createdb -U postgres stage1b2_source
docker "$STAGE1B2_DOCKER" exec "$STAGE1B2_PREFLIGHT_ID" createdb -U postgres stage1b2_restore
docker "$STAGE1B2_DOCKER" port "$STAGE1B2_PREFLIGHT_ID" 5432/tcp
# Replace PORT with the displayed local port.
export STAGE1B2_SOURCE='host=127.0.0.1 port=PORT dbname=stage1b2_source user=postgres password=fixture-only'
export STAGE1B2_RESTORE='host=127.0.0.1 port=PORT dbname=stage1b2_restore user=postgres password=fixture-only'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=tests/v9:tests/v9_postgres:. \
  python -m pytest -q tests/v9_postgres
PYTHONPATH=tests/v9:tests/v9_postgres:. python scripts/v9/benchmark_stage1b2.py
docker "$STAGE1B2_DOCKER" rm --force --volumes "$STAGE1B2_PREFLIGHT_ID"
```

The integration drill tests stale and duplicate publications, rollback after
insertion, concurrent independent connections, a fresh-process pinned read,
immutable rows, a fresh restore, repeated replay, unresolved conflicts and the
refusal to replace an emitted payload. It rejects unsupported checkpoints,
corrupt and incompatible archives, and an internally inconsistent archive whose
failure occurs after schema creation; the schema is absent after rollback.

## Fixture measurement (2026-11-14)

Python 3.12.14, psycopg 3.3.9, PostgreSQL 16.16 Alpine container, local TCP,
Linux x86_64; three sequential repeats, same generated five instruments and
200 trade-like rows per instrument, 729,780 serialized input bytes total.
Each row is committed separately; five profiles are published separately. Replay
is in-process and archive/restore use a repeatable-read snapshot and a fresh
schema. No warm-up, concurrency or production traffic is modeled. Times are
wall-clock medians; relation size includes table indexes and TOAST, not WAL or
container overhead. Archive is uncompressed JSON and contains 1,000 raw rows
and five profile versions, rather than the larger evidence inventory used in the
integration drill.

| Operation | Median | Throughput |
|---|---:|---:|
| Ingest 1,000 rows | 1.9869 s | 503 rows/s |
| Publish five profiles | 0.02218 s | 225 profiles/s |
| Replay 1,000 rows | 0.1403 s | 7,130 rows/s |
| Archive 968,925 bytes | 0.02433 s | 39.8 MB/s |
| Restore 968,925 bytes | 0.4780 s | 2.03 MB/s |

Table/index size was 1,073,152 bytes; archive size 968,925 bytes on each run.
One output digest was `afd48c0159cee3c2f85b3bd17dc1c5ed8de309c6e05b6d87d0eefd5450a64ea2`;
all five outputs share this digest because the values and IDs are identical.
Five manifest digests differ by instrument and remain stable across repeats;
the script prints them. The fixture supports only replay of explicitly supplied
trade-like values and declared coverage. It does not establish continuous
order-book depth, market liquidity, exchange sequence or liquidation coverage;
those require future forward capture and independent source validation.

A conservative fixture retention boundary is to retain all versions, revisions,
raw rows, manifests, coverage and checkpoints needed by every pinned replay,
plus an independently checked archive and restore evidence, until an explicit
retention policy is approved. The measured archive is about 969 bytes per input
row in this small fixture, but fixed profile/index overhead, compression, WAL,
updates and real source rates make linear extrapolation unreliable. No duration
or production capacity is inferred, and legacy retention remains unchanged.

## Combined Stage 1 evidence

| Requirement | Evidence and remaining limits |
|---|---|
| S1-01 | Covered zero versus missing fixtures; live coverage unknown. |
| S1-02 | Side/unit and mapping fixture validation; live source semantics unknown. |
| S1-03 | Mandatory gap and conflict block fixture paths; runtime propagation unverified. |
| S1-04 | Immutable fixture revisions stored and restored; real source correction unverified. |
| S1-05 | Candle closure and chronology fixtures; collector chronology unverified. |
| S1-06 | Cutoff, known/unknown availability fixtures; real historical capture unverified. |
| S1-07 | Manifest and output replay digests match after fixture restore; production replay unverified. |
| S1-08 | Fixture suspension and evidence-based recovery; system shadow remains Stage 7. |
| S1-09 | Conflict and emitted-payload refusal survive fixture restore; real source repair unverified. |
| S1-10 | Isolated transaction, CAS, rollback, global version uniqueness and pinned process read tests; no production integration. |
| S1-11 | Generic fixture identities and supplied limits; portfolio integration is later. |
| S1-12 | Guarded baseline, legacy parsers, read-only target preflight, sentinel replacement, pinned worker addresses and validated owned-container cleanup; runtime compatibility open. |
| S1-13 | Fixture graph, registry, edge mapping and cycle checks; live routing unverified. |
| S1-14 | Checksummed fixture archive, fresh transactional restore and negative tests; production backup not claimed. |
| S1-15 | Source capability limitations explicit; actual exchange capability unverified. |
| S1-16 | Synthetic local measurements above; real forward volume and resources open. |

Independent review and combined G1 acceptance remain open. Stage 0 full audit
and runtime verification remain open. Stage 2 requires G1 and a separate
assignment; stages 3–6 cover scenarios, decision/risk, execution/recovery and
calibration/replay; stages 7–9 require separately authorized forward shadow,
demo/Sheets and controlled rollout/cleanup/Telegram.

## Corrective review: isolation and commit boundaries

PostgreSQL tests remain collectable without the optional psycopg dependency and
are skipped at execution until it is installed. Both the integration fixture and
benchmark use `tests/v9_postgres/fixture_targets.py` to preflight both targets
read-only before provisioning an isolated server. Both explicit DSNs must name the two distinct disposable
databases above on the same explicit numeric loopback host and port. The helper
rejects all inherited `PG*` settings and all DSN keys other than host, port,
dbname, user and password, including `hostaddr`, service and connection options.
It then pins the connection's host address to `127.0.0.1`. Each test and benchmark repeat has its own server; the supplied preflight databases
are only read.

All adapter operations that own a transaction require both autocommit and an
idle libpq transaction state, checked before their SQL; an outer transaction is
not a commit boundary. Archive v1 restore validates before creating the schema
that each key with retained versions has exactly one matching current pointer,
and that its generation equals the number of retained versions. Duplicate
version IDs, missing pointers and mismatched generations are rejected. This v1
rule assumes all published versions are retained; a future retention policy that
removes versions requires a new archive contract rather than relaxing the check.

The corrective run on Python 3.12.14, PostgreSQL 16.16, psycopg 3.3.9 and Linux
x86_64 repeated the same 5-instrument, 1,000-row synthetic fixture three times:
median ingest 2.17861 s, publication 0.03299 s, replay 0.23096 s, archive
0.02470 s, restore 0.54915 s; 729,780 input bytes, 1,073,152 relation bytes and
968,925 archive bytes. Methodology and liquidity/retention limits above still
apply. Measurements are not production capacity evidence.

The combined S1-01–16 matrix above remains applicable with these corrections:
S1-10 now additionally rejects writes under an outer transaction and verifies
visibility from an independent connection; S1-12 now includes full CI test
collection without psycopg and shared fail-closed endpoint validation; S1-14 now
rejects missing or inconsistent archive pointers before schema creation and
rejects restore under an outer transaction. All live-source, runtime, acceptance
and later-stage limits in that matrix remain open.


## Corrective review: fixture cleanup race

The earlier OID-then-DROP cleanup had a race and is superseded. Supplied targets
are never mutated by fixture setup or cleanup. Their schemas are checked before
any worker server is created. A worker is a fresh local PostgreSQL container
with fresh source and restore databases. Its captured container ID, not a schema
name, is used for cleanup on success or failure; anonymous volumes are removed.
The container create command writes a cidfile so partial create failures can be
cleaned too. This is an isolated fixture, not permission to use shared databases.

Both source and restore occupied-target tests preserve their sentinel and the
other database. `test_replacement_during_cleanup_survives` deterministically
replaces the caller schema when worker cleanup starts and checks that the new
sentinel survives. Partial setup, successive fresh workers and transactional DDL
rollback are covered. S1-12 includes these checks; other S1 rows retain the
limits above. Independent acceptance, live-source evidence and G1 remain open.

## Corrective review: worker endpoint and cleanup ID

All owned-server DSNs embed both `host=127.0.0.1` and `hostaddr=127.0.0.1`,
including those used by direct test connections and fresh processes. Caller
preflight still rejects inherited `PG*` settings and caller-supplied
`hostaddr`; it pins the address itself after validation. The worker DSN stays
pinned even if `PGHOSTADDR` changes after preflight.

Cleanup only uses a validated, complete lowercase 64-character hexadecimal ID
captured from this run's cidfile. A malformed cidfile raises without running
`docker rm`; a valid ID left after a partial create or start failure is removed.
The regression suite tests both cases, the late address change, preflight
rejection before side effects, and the earlier replacement-sentinel scenario.
S1-12 covers this isolation evidence; S1-10 and S1-14 retain their isolated
fixture limits and S1-16 remains a synthetic measurement. Independent review
and G1 acceptance remain open.
