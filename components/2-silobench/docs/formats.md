# SiloBench export formats and determinism contract

These are the interfaces downstream tooling consumes: snapshot-diff layers
read `snapshot.v1` and `event-log.v1`; evaluation harnesses read the task
fixtures and `tool-log.v1`. Versioned: breaking changes bump the format name.

## snapshot.v1 (`silobench snapshot --out file.json`)

```json
{
  "format": "snapshot.v1",
  "meta": { "seed": 4711, "schema_version": 1, "docs_outage": false, "mutation_seq": 3 },
  "tables": { "<table>": [ { "...columns": "..." } ] },
  "world_hash": "sha256 hex",
  "audit_hash": "sha256 hex",
  "state_hash": "sha256 hex"
}
```

- `tables` holds every table except `tool_calls`, rows in insertion (rowid)
  order, values exactly as stored.
- Three hashes, three meanings:
  - `world_hash`: business tables plus profile (`seed`, `schema_version`,
    `docs_outage`). Excludes `events`, `counters`, `environment`. Two runs
    that produce the same business outcome get the same world hash even if
    one explored more or was denied more often. Use this to compare
    trajectories.
  - `audit_hash`: the stored event rows exactly as they appear in this
    snapshot's `tables.events` (with `payload_json` as the stored string). It
    is recomputable from `snapshot.v1`, but NOT from `event-log.v1`, whose
    lines carry the parsed `payload` object form instead.
  - `state_hash`: meta plus all snapshot tables. The strict fingerprint; the
    committed golden hashes (`packages/scenario/fixtures/golden-hashes.json`)
    pin it for the seeded state and every scripted reference run.
- Hashing: recursive canonical JSON (object keys sorted by UTF-16 code unit
  order, JavaScript's default string comparison; arrays in order; no
  whitespace; UTF-8 encoded before sha256). All key names in v1 are ASCII, so
  this coincides with bytewise ordering today; cross-language consumers must
  replicate UTF-16 code unit ordering if non-ASCII keys ever appear.
  Implementation: `packages/domain/src/hash.ts`.

## event-log.v1 (`silobench events --out file.jsonl`)

JSONL. First line is a header:

```json
{"format":"event-log.v1","seed":4711,"schema_version":1,"docs_outage":false,"events":2}
```

Each following line is one domain event, ordered by (`mutation_seq`, insertion
order): `event_id`, `mutation_seq`, `ts`, `actor`, `system`, `type`,
`entity_type`, `entity_id`, and `payload` (a JSON object, already parsed).
Audit-only events (ACCESS_DENIED) carry the mutation_seq current at the time
of the attempt without advancing it. Event ids are numerically monotonic, but
ordering uses insertion order, not lexical id comparison.
Event types: `HOLD_PLACED`, `HOLD_RELEASED`,
`APPROVAL_REQUESTED`, `PAYMENT_RELEASED`, `DUPLICATE_FLAGGED`,
`ACCESS_DENIED`. The log is append-only and starts empty at seed time; seeded
rows (holds, the pending approval) predate the log by design.

## tool-log.v1 (`silobench calls --out file.jsonl`)

Same JSONL shape with a `tool-log.v1` header. One line per dispatched tool
call: `call_seq`, `ts` (logical), `system`, `principal`, `tool`, `args_json`,
`outcome` (`ok`|`error`), `error_code`, `result_digest` (sha256 of the
canonical JSON result). This is the observability layer: it is excluded from
every state hash, and it is the data trajectory-level scoring runs on.

Known boundary: calls rejected by MCP input-schema validation never reach the
dispatch layer and therefore do not appear here; they surface as protocol
errors on the client side. Logging is also best-effort by design: a logging
failure never masks the tool outcome, so in pathological cases (a disk error
mid-run) a row can be missing. The log is observability, never evidence for
the state hashes.

## Determinism contract

- Logical time only. Epoch 2026-01-05T09:00:00Z (UTC), tick 1 second,
  ISO-8601 UTC serialization everywhere. The nightly export ran at
  2026-01-04T02:00:00Z. No wall clock, no unseeded randomness (enforced by a
  source-scan test).
- The mutation clock ticks only on agent-caused state mutations inside one
  immediate SQLite transaction per mutation; reads never move any hash.
  Controller actions (fault toggles, reseeds) are environment configuration,
  not agent mutations: they change the world and state hashes but do not
  advance the mutation clock.
- Snapshots are taken inside a single read transaction, so every table, the
  profile, and the counters describe one committed state.
- Deterministic orderings: ERP lists by `invoice_id`; docs search by
  `created_date` DESC then `doc_id`; folder listings by `doc_id`; warehouse
  queries by `rowid` (or requested column then `rowid`). Cursors are opaque
  and bound to a digest of the query shape, rejecting replay against
  different arguments; docs and warehouse cursors carry offsets, the ERP
  invoice list carries a keyset (the last ordering key).
- Single-writer assumption: one serving process per database file. The
  scenario controller resets between runs; concurrent multi-process writes
  are out of scope for v1.

## Verdict boundary

SiloBench owns the per-task oracle: fixture expectations (answer canonical
equality, state-row checks, event checks, required-call checks over the
tool-call log) evaluated by `packages/scenario/src/checker.ts`, one verdict
per run. Layers above it own aggregation across runs, richer trajectory
scoring (ordering and full argument-shape predicates) over `tool-log.v1`,
and judging; cross-snapshot invariants belong to whatever diffs
`snapshot.v1` files.
