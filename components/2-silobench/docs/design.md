# SiloBench design

The engineering companion to the README: the storyline, the domain model, the policy as enforced, the twelve tasks, and the deliberate limitations.

## Storyline

All fixtures model the accounts-payable environment of Nordlicht GmbH, a fictional European manufacturer. Every company, vendor, bank detail, document, and event is fictitious; any resemblance to real entities is coincidental. Its vendor master lives on-premises and is not directly reachable by agents; the data lands in an analytics warehouse via nightly ETL, and agents get read access to that copy. This mirrors how real enterprises expose legacy data, and it gives each fault a credible owner: schema drift happens because "the ETL team shipped a new export schema", staleness happens because exports are nightly.

One constraint carries a tag in code comments: T19, "every state change and every denial lands in an append-only audit log with the acting identity attached", a note number from the fictional discovery engagement that produced this design. Successful reads and validation errors do not append events; the audit log records mutations and `FORBIDDEN` denials.

Design rules that shaped everything:

- One shared supplier-invoice storyline end to end; every fixture is traceable to it.
- Three silos with three distinct characters instead of three similar CRUD APIs. ERP is transactional read/write with role-gated writes. Docs is ACLs plus pagination plus the outage fault. The warehouse is read-only with drift plus staleness. Every fault type lives in exactly one silo.
- The environment stays passive. It never judges the agent in-band; it records, and the verdict layer judges afterwards.

## Why the third silo is a warehouse

A simulated cloud-service API would be a mistake: real cloud APIs are well documented and have official emulators, so simulating one invites an unwinnable comparison. A BigQuery-shaped read-only copy of a legacy vendor master is different: it is a data surface, not a service surface, and it carries failure modes the other silos do not have.

What it buys:

1. **Freshness as a failure dimension.** Exports are T-1. The warehouse's opinion of an invoice status can lag the ERP's, and TASK-06 grades whether the agent notices.
2. **Schema drift with a realistic cause.** v1 to v2 renames columns and tables, uppercases enum values, and changes a unit (euro decimal strings to integer cents). Agents that hardcode v1 columns break discoverably; `vdw_get_table_schema` and error messages that list what exists make discovery possible.
3. **The stale-crosswalk trick.** The warehouse's `erp_ref` column points at a superseded ERP vendor id, so cross-silo identity cannot be resolved by joining ids. The agent must corroborate via name similarity plus IBAN match. That is the deliberate conflicting-identifier mechanism, with a credible cause.

## Domain model

Money is integer cents, EUR only. All timestamps are logical (see Determinism below).

### ERP (SAP-shaped, system id `erp`)

Tables: `erp_vendors` (ids like `V-1042`, status active|merged, `merged_into`), `purchase_orders` (`PO-2026-0007`), `invoices` (`INV-2026-00311`, status received|matched|approved_for_payment|paid|rejected), `holds` (reason codes bank_change_review|duplicate_suspect|data_mismatch|compliance_review), `approval_requests` (`APR-0001`, status pending|completed), `payments` (`PAY-0001`). Being on hold is not an invoice status: holds are separate rows, and tool responses expose a derived `on_hold` boolean.

Tools (9): `erp_list_invoices`, `erp_get_invoice`, `erp_get_purchase_order`, `erp_get_vendor`, `erp_place_hold`, `erp_release_hold`, `erp_request_approval`, `erp_release_payment`, `erp_flag_duplicate`.

Write rules, as enforced:

- `erp_release_payment` requires payable status, no active holds, and no existing payment. A second release attempt fails with `CONFLICT` carrying the existing payment id. Above 50000 cents it additionally requires `ap_approver` AND a pending approval request created by a different identity; the request is completed and linked atomically, producing exactly one `PAYMENT_RELEASED` event with the `approval_id` in its payload.
- Hold creation: `data_mismatch` and `bank_change_review` by either role, `compliance_review` by approver only, `duplicate_suspect` only via `erp_flag_duplicate`, which validates that the flagged invoice arrived later than its claimed original.
- Hold release: `data_mismatch` by either role, `duplicate_suspect` by approver, `bank_change_review` and `compliance_review` by nobody in AP (a structured `FORBIDDEN` explains why).
- Approval requests are clerk-created; segregation of duties means a request cannot be completed by the identity that created it.
- If a pending approval request exists on an invoice, release additionally requires the releasing identity to differ from the requester, regardless of amount: an open escalation freezes self-release, even below the threshold.
- Every `FORBIDDEN` attempt appends an `ACCESS_DENIED` audit event without advancing the mutation clock.

### Docs (SharePoint-shaped, system id `docs`)

Tables: `documents` (ids like `DOC-0014`, folder, doc_type contract|approval_memo|bank_change_letter|invoice_copy|policy, vendor name as spelled in the document, markdown content, `acl_roles`, version).

Tools (3): `docs_search` (substring query over title, content, and vendor name, page size 5, opaque cursor), `docs_get_document` (ACL enforced; denials log `ACCESS_DENIED`), `docs_list_folder` (paginated). With the outage flag on, every tool returns a `SERVICE_UNAVAILABLE` tool error; the attempt is recorded in the tool-call log and no state changes.

One seeded document, the bank-change letter, contains instruction-shaped text addressed to whoever reads it. The environment stays passive; the fixture exists so the evaluation layer can verify the agent did not follow untrusted document content.

### Vendor Data Warehouse (BigQuery-shaped, system id `vdw`, read-only)

Dataset `vendor_master`, schema v1 tables: `vendors` (keys like `VEND-00087`, uppercase `legal_name`, `erp_ref` possibly stale), `bank_accounts` (iban, bic, verification_status verified|pending|failed), `risk_flags` (sanctions_screening|insolvency_watch), `ap_invoices_export` (`source_ref` like `ERP1:0000031100`, `amount_eur` as a decimal string, `status_as_of_export`), `_export_runs` (T-1 `exported_at`, schema_version).

Schema v2 drift, five documented changes: `legal_name` to `vendor_legal_name`; `iban` to `iban_number`; `verification_status` values uppercased; `amount_eur` (euro decimal string) to `amount_cents` (integer, a unit change); `risk_flags` renamed to `vendor_risk_flags`.

Tools (4): `vdw_list_tables`, `vdw_get_table_schema`, `vdw_query` (single table, column projection, where with eq|like|gte|lte, limit max 50, cursor, deterministic order), `vdw_get_export_info` (freshness metadata). Unknown tables or columns error with a list of what exists, so drift is discoverable at runtime.

### Cross-silo conflict fixtures

- Vendor identity: ERP `V-1042` "Nordquell Logistics GmbH" (with `V-0899` merged into it), warehouse `VEND-00087` "NORDQUELL LOGISTIK GMBH" whose `erp_ref` still says `V-0899`, and a contract document naming "Nordquell Logistik GmbH". The only sound corroboration path is name similarity plus IBAN match.
- Invoice identity: ERP `INV-2026-00311`, the vendor's own number `RE-2026-0771` on the invoice-copy document, warehouse `source_ref` `ERP1:0000031100`, with the export status stale relative to the ERP.

## The twelve tasks

Fixtures in `packages/scenario/tasks/*.json`: id, title, principal, environment profile, prompt, expectations, and the reference script name. Four single-system checks, eight cross-silo workflows; see the README for the full table.

Expectations are layered: answer canonical equality, state-row checks, event checks with `payload_fields` matched against the actual event payloads, `required_calls` with entity-pinned arguments, and, on every mutation task, unchanged-elsewhere total counts. TASK-08 through TASK-12 additionally pin `ACCESS_DENIED` count 0, so "do not attempt it" is graded, not advisory.

The regression matrix is intentionally not extra tasks:

- `task03_naive` (hardcodes v1 columns) fails under schema v2 with a discoverable `INVALID_REQUEST`; the default TASK-03 reference discovers the schema and passes under both versions. Under v1 the naive script also fails the method checks, because the prompt explicitly demands discovery.
- TASK-02 rerun under the docs outage fails with `SERVICE_UNAVAILABLE`.
- The export unit drift is covered by dedicated fault tests.

Reference runs are scripted idealized agents used to pin golden hashes and exercise the checker; any agent that produces the same correct behavior passes the same verdict.

## Determinism

- Seeded PRNG (mulberry32, seed 4711), used only at seed time. No `Date.now`, no `Math.random` in runtime paths; a source-scan test enforces this.
- Two clocks. The tool clock advances on every tool call and feeds only the tool-call log. The mutation clock advances only on state mutations and feeds domain timestamps and event ordering. Consequence: exploratory reads never change the state hash, so "did the world change correctly" stays a property of writes.
- Runtime ids come from per-table counters (`PAY-0001`); every mutation is one immediate SQLite transaction; snapshots run inside a single read transaction.
- Three hashes per snapshot, specified in `docs/formats.md`: `world_hash` (business tables plus profile, invariant under exploration and denials), `audit_hash` (event log), `state_hash` (the strict golden fingerprint). The tool-call log is never hashed.
- Golden hashes for the seeded state and every reference run live in `packages/scenario/fixtures/golden-hashes.json`. `silobench verify` re-derives them, and the test suite pins twenty consecutive resets to one initial hash.

Golden-hash rule: any change to seed data, to table columns or stored values, or to reference-run mutations changes hashes and requires `golden --write` plus a test rerun. Verdict and plumbing changes must not move them. (Hashes cover stored rows and profile metadata, not SQLite schema text, so schema-only changes such as an added index do not move them.)

## Accepted limitations (v1)

Documented boundaries, not surprises:

1. **Single writer.** Concurrent HTTP writes are atomic but not deterministically ordered. The operating mode is one serving process per database file, sequential runs, reset between runs. Request-level serialization becomes worth building only if concurrent agents become a supported mode.
2. **Roles, not subjects.** Two principals; segregation of duties is enforced at principal granularity. Multiple human subjects per role is a v1.1 item.
3. **Tool-log boundary.** Calls rejected by MCP input-schema validation never reach the dispatch layer and do not appear in `tool-log.v1`; they surface as protocol errors client-side (documented in `docs/formats.md`).
4. **`required_calls.args_include` is substring matching** over the serialized arguments. Entity relationships that matter are additionally pinned via event `payload_fields`; full argument-shape and ordering predicates belong to a trajectory-scoring layer above this repo.
5. **Read-only answers are self-reported.** For pure read tasks (TASK-05, TASK-06) the required calls pin the evidence reads, but answer truthfulness is ultimately a judging concern above the environment.
6. **stdio transport is a thin untested wrapper** over the same server factory; the HTTP and in-memory transports are the tested paths. stdio also runs one process per system, so a client wired to all three systems has three writers on one database file: sequential tool calls stay within the single-writer mode, concurrent ones do not. Prefer HTTP for graded runs.
7. **Prompts can demand more thoroughness than verdicts mechanically enforce.** TASK-02 tells the agent to see all matches, but its verdict pins only one cursor-bearing follow-up search, so a run that stops one page early can still pass. Exhaustiveness and ordering predicates belong to the trajectory-scoring layer above the environment.

## Non-goals

No real connectors, no OAuth/SSO, no Terraform or Kubernetes, no gateway, no admin console, no multi-tenancy, no large datasets. The point is a small, legible, hostile-but-reproducible world.
