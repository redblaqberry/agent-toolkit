# DiscoverySpec

**Compile technical discovery transcripts into reviewed, executable deployment contracts.** Agent deployments rarely fail on model quality first; they fail on requirements nobody wrote down, stakeholder statements that contradict each other, and questions nobody answered before go-live. DiscoverySpec addresses that with a versioned `deployment-contract.v2` document (KPIs, roles, allowed actions, escalation rules, security constraints, latency and cost ceilings) in which every field traces back to a numbered customer statement, plus an acceptance suite that runs through [agent-eval-gate](https://github.com/redblaqberry/agent-eval-gate).

The full pipeline ships today: the contract format, a complete example discovery call, a fail-closed validator, the `compile` pipeline with a deterministic stub adapter and a structured Claude extraction adapter, the `approve` sign-off step that stamps a fully resolved draft, the scenario compiler that renders the contract's own typed acceptance rules into a suite in agent-eval-gate's native format, `run`, which executes that suite through agent-eval-gate and links every verdict back to the originating customer statement, and `report`, which renders a run as a manager-facing page under the customer's own brand rules.

Most eval tooling starts from datasets, tasks, and scorers the user authors. DiscoverySpec starts one step earlier: it preserves provenance from customer statement to requirement to acceptance test, flags contradictory stakeholder requirements before anything runs, and refuses to let a contract with unresolved conflicts or unanswered blocking questions reach execution.

## How it works

```
transcript.md -> draft contract -> human approval -> contract JSON
             -> gate fixtures/config -> run results (with provenance links)
```

The contract is only executable through its requirements:

- Every KPI, role, action, escalation rule, security constraint, SLO, and data-governance entry references a requirement id.
- The reverse direction is checked too, because that is where promises actually get lost: a requirement that came out of review adopted, but that no executable section references, is a commitment the deployment would silently drop. An approved contract carrying one is refused, and `approve` will not stamp it. Requirements still in open conflict are exempt (which side gets wired in is exactly what resolving them decides), and a rejected one must never be wired.
- Every requirement cites the numbered transcript turns it was extracted from, or an explicitly recorded follow-up. This is positional traceability: the tool verifies the cited turns exist in the pinned transcript, not that they semantically support the requirement. Confirming that each requirement faithfully reflects its cited turns is the job of the human approval step, which is what the sign-off attests to.
- The contract pins the SHA-256 of the exact transcript bytes its provenance came from; validating against a different transcript fails, and a contract cannot be approved without the pin.
- `approve --signing-key` cryptographically attests the approval with an Ed25519 signature over a canonical digest of the whole contract; `validate`/`export-gate`/`run`/`report --verify-key` then require a valid signature, so editing any byte (including the approver names or `status`) after signing fails the gate. The trust boundary is stated plainly: this protects against anyone who does not hold the signing key, under a single approval-authority model. Per-approver identities, threshold signatures, and an attestation registry are roadmap. Without a verify key, validation is structural only and cannot prove authentic approval.
- Contradictory stakeholder statements become explicit conflict pairs that must be resolved, with a named rationale, before approval.
- Required fields the discovery call did not answer become blocking open questions with a named owner; an approved contract cannot carry them.
- Requirements resolved as rejected can never be wired into an executable section.

## Try it

```bash
pip install -e .[dev]
discoveryspec validate --contract examples/invoice_automation/draft-contract.json
```

Output for the bundled draft:

```
contract: nordlicht-invoice-automation (draft)
requirements: 18  open questions: 1

CONFLICTS (3) - resolve before approval:
  REQ-004 vs REQ-005
    REQ-004 [T13] Anna Lindqvist (Operations): Autonomous posting under EUR 1000
    REQ-005 [T15, T18] Jonas Weber (Security): Named human approval for every ERP write
  REQ-006 vs REQ-007
    REQ-006 [T21] Anna Lindqvist (Operations): Approver threshold EUR 5000
    REQ-007 [T23] Priya Nair (Finance): Approver threshold EUR 500
  REQ-008 vs REQ-009
    REQ-008 [T26] Anna Lindqvist (Operations): Best model regardless of cost
    REQ-009 [T28] Priya Nair (Finance): Cost ceiling EUR 0.08 per invoice

BLOCKING OPEN QUESTION UNK-001 [T35, T36, T37, T38] owner Nordlicht Legal:
    Who owns the extracted invoice data, and what is the retention period for source documents and model outputs?

pending fields: slo.cost_per_task_eur, data_governance

NO ACCEPTANCE RULE (7) - write one, or record how each is verified out of band:
  REQ-003 (allowed_action) Jonas Weber (Security): Autonomous agent scope
  REQ-010 (latency) Tomas Keller (IT): Interactive latency
  REQ-011 (security) Jonas Weber (Security): EU data residency
  REQ-012 (security) Jonas Weber (Security): Append-only audit log
  REQ-013 (security) Jonas Weber (Security): Untrusted document content
  REQ-016 (escalation) Priya Nair (Finance): Duplicate-invoice escalation
  REQ-017 (escalation) Jonas Weber (Security): Vendor bank-change escalation

verdict: VALID
```

A draft is allowed to be incomplete; that list is the reviewer's remaining work, not a failure. The same gaps in an approved contract are exit 1.

Exit codes are fail closed:

| Code | Meaning |
|---|---|
| 0 | structurally valid; draft findings (conflicts, open questions, pending fields) are reported, they are the product working |
| 1 | approval violation: the contract claims approved but has open conflicts, blocking questions, missing sign-off, no transcript pin, unfilled required fields, an adopted requirement wired into nothing, or a behavioral requirement with neither an acceptance rule nor a recorded out-of-band verification; also returned for a draft under `--require-approved` |
| 2 | structural failure: schema or model violation, broken provenance, transcript mismatch, dangling references |

For CI and deploy gates, `--require-approved` makes exit 0 possible only for an approved, clean contract, so a draft can never pass a pipeline.

The bundled draft is itself the output of the `compile` pipeline. `compile` runs an extraction adapter over the transcript and owns the trust envelope around whatever the adapter returns: the result is always an unsigned draft, pinned to the SHA-256 of the parsed transcript, and validated end to end (including turn provenance) before anything is written, so an extraction that cites turns that do not exist is refused, and no adapter can mint an approved contract or hand back an attestation it did not have the key to produce. The `stub` adapter replays a recorded extraction result, which keeps the whole compile-approve-export-run pipeline deterministic and testable without model access; the `claude` adapter extracts live with one structured model call (`pip install 'discoveryspec[llm]'` and SDK credentials in the environment), and its output receives exactly the same distrust.

```bash
discoveryspec compile --transcript examples/invoice_automation/transcript.md \
  --extractor stub --fixture examples/invoice_automation/draft-contract.json \
  --out draft-contract.json

# or live extraction:
discoveryspec compile --transcript examples/invoice_automation/transcript.md \
  --extractor claude --out draft-contract.json
```

Once every conflict is resolved, every blocking question answered, and every required field filled, `approve` records the human sign-off. The signed workflow runs on one Ed25519 key pair, which `keygen` produces:

```bash
discoveryspec keygen --out approval-key
# private key -> approval-key.pem  (secret; never commit it)
# public key  -> approval-key.pub
# fingerprint: a29f6e2b032b7042

discoveryspec approve --contract resolved-draft.json \
  --by "Anna Lindqvist (Operations), Jonas Weber (Security), Priya Nair (Finance)" \
  --out approved-contract.json --signing-key approval-key.pem
```

`keygen` writes the private key owner-readable where the platform enforces file modes, and refuses to overwrite an existing pair without `--force`, because replacing a key silently invalidates every contract and run report ever signed with it. The private key is the whole trust anchor: whoever holds it can mint approvals that every downstream gate accepts, so it stays out of the repository and only the `.pub` is handed around.

`approve` is the intended way a contract gains `status: approved`. It refuses a draft with open conflicts, blocking questions, pending fields, or an adopted requirement wired into nothing (exit 1), refuses to re-stamp an existing sign-off, refuses a draft that already carries an attestation (approve is what signs, so a signature on an unapproved document did not come from this pipeline), and never silently overwrites an output file. On success it pins the SHA-256 of the verified transcript, changes nothing outside `metadata`, and re-validates the stamped document end to end before writing it, so its output always passes `validate --require-approved`. With `--signing-key` it also attests the approval cryptographically; a downstream gate that runs `validate --require-approved --verify-key approval-key.pub` then rejects any contract whose `status` was flipped to `approved` by hand, because structural validation alone cannot tell an authentic approval from an edited one.

Then compile the acceptance suite:

```bash
discoveryspec export-gate --contract examples/invoice_automation/approved-contract.json --out gate-export
```

This writes `scenarios.yaml` (Given/When/Then scenarios in agent-eval-gate's native format, provenance carried in tags like `REQ-005` and `T15`) and `gate-config.json` (SLOs, the scenario-to-requirement map, and what the suite does not cover). Drafts are refused: no unreviewed contract can run.

The suite is a function of the contract, not of the compiler. Each scenario is rendered from one `acceptance_rules` entry: a typed primitive that states the situation, the message to send the agent under test, and the outcome that must be observable. There are five, and nothing in the compiler knows what business a contract is for:

| Primitive | Checks |
|---|---|
| `required_action` | the agent must take these actions, optionally in order and with argument subsets |
| `forbidden_action` | the agent must never take these actions |
| `escalation` | under a stated condition, the agent must hand off instead of acting |
| `latency` | exercises the interactive path, with the budget derived from the contract's own p95 |
| `cost` | exercises the path the per-task cost ceiling covers |

Coverage is the part that matters. A requirement describing agent behavior must carry an acceptance rule, or record `out_of_band_verification`: a named reason it cannot be observed in a trajectory, and who verifies it instead. Data residency and an append-only audit log are properties of where the system is hosted, so no sequence of agent actions can demonstrate them; writing a scenario for either would produce a test that passes without proving anything. Silence is what gets refused. An approved contract where a behavioral promise has neither a rule nor a recorded excuse does not validate, does not export, and cannot be approved. What the suite does not cover then travels with the export, into the run record, and into a section of the manager report, so a clean verdict can never be read as broader than it is.

Rules are checked as strictly as the rest of the contract: every action a rule names must exist in `allowed_actions`, a rule enforcing a rejected requirement is a contradiction, a requirement cannot be both tested and excused, and a rule with no deterministic outcome (no expected or forbidden actions, no call limits, no output constraints) is an error rather than a weak generic test, because a scenario that cannot fail still adds a passing row to the report. A rubric does not satisfy that requirement: replay mode never invokes the judge, so a rule carrying only a rubric would pass unconditionally in the one mode that runs without credentials.

Then `run` executes the suite through [agent-eval-gate](https://github.com/redblaqberry/agent-eval-gate) (installed separately, e.g. `pip install -e ../agent-eval-gate`):

```bash
discoveryspec run --contract examples/invoice_automation/approved-contract.json \
  --mode replay --fixtures examples/invoice_automation/fixtures \
  --prices examples/prices.json --out gate-run \
  --verify-key approval-key.pub --signing-key approval-key.pem
```

In the signed workflow, `--signing-key` (the same authority key; a separate run key also works) attests the run report so `report --verify-key` can later prove the verdict was not edited. Both flags are optional; without them the run is structural only. The suite is compiled from the approved contract at run time (never a stale export) and loaded back with the gate's own scenario loader, so what runs is exactly what any agent-eval-gate user would run. Replay mode executes recorded trajectory fixtures deterministically with no API key (both bundled examples ship a fixture set and a shared EUR price table, so this command runs from a fresh clone); live mode (`--mode live --agent <module>`) runs the agent under test against the Messages API, with the gate's LLM judge on rubric scenarios. Two SLOs from the contract are enforced across the run: statistical p95 latency (nearest-rank, deliberately pessimistic at small n) and the per-task cost ceiling checked against every scenario, priced from a EUR price table (`--prices` is mandatory; a run that cannot verify a promised ceiling refuses instead of skipping the check). `run` writes `run.json` in agent-eval-gate's report format plus `run-report.json`, where every verdict carries the requirement it enforces, the transcript turns behind it, and the quoted customer statements, so a failing scenario reads as a broken promise with the stakeholder's words attached. The report also records the content hash of the exact contract the run executed, and a run with harness errors carries the verdict INCOMPLETE, never a plain pass or fail. Exit codes stay fail closed: harness or judge errors are exit 2, a failing scenario or breached SLO is exit 1, and there is no non-strict mode.

Finally, `report` renders the run for the people who signed the contract:

```bash
discoveryspec report --contract approved-contract.json --run gate-run \
  --brand examples/brands/nordlicht-strict.json --out report.html \
  --verify-key approval-key.pub
```

One self-contained HTML page for a non-technical decision maker: the verdict up front, every broken promise with the stakeholder's quoted words from the call, all scenarios in plain language (every label is the `label` field of the acceptance rule it came from, so friendly text cannot drift from what a test checks), what the suite did not check and who verifies it instead, and the agreed limits against what was observed. The brand file supplies identity, palette, and font stacks as data, never CSS, and its policy flags are hard constraints: if the brand says no shadows, no gradients, a corner-radius cap, or no emoji, the renderer's typed style serializer is audited against those rules and the report REFUSES to render on any violation, exactly as it refuses a palette that fails WCAG AA contrast or a contract that is not byte-identical to the one the run executed.

## The bundled examples

`examples/invoice_automation/` contains a complete fictional discovery call for supplier-invoice automation at a furniture retailer: a 41-turn transcript with three seeded stakeholder conflicts (autonomous posting vs named human approval on every write, an approver threshold of EUR 5000 vs EUR 500, best-model-regardless vs a EUR 0.08 per-invoice ceiling) and one blocking data-governance question that nobody in the room could answer. Two golden contracts show both ends of the pipeline: the draft as a correct extraction would produce it, and the approved contract after human resolution, including a recorded post-call answer from Legal and an amendment reconciling the touchless KPI with the mandatory approval rule.

`examples/refund_handling/` exists to keep the previous paragraph honest. It is a second contract from a different business: refund handling at an online electronics retailer, with its own transcript, its own actions (`read_order`, `check_return_window`, `check_fraud_signals`, `issue_refund`, `request_approval`, `send_customer_message`), its own roles, its own escalation triggers, its own unit of work (a refund request, not an invoice), and its own out-of-band promise (an immutable decision log). It compiles to ten scenarios with no change to any code. `tests/test_second_domain.py` serializes that export and fails if a single word from the invoice example (`invoice`, `erp`, `vendor`, `ap_approver`, `posting`, and the rest) appears anywhere in it, so the compiler cannot quietly acquire a template again.

Both examples ship replay fixtures and share a EUR price table, so `run` and `report` execute from a fresh clone and are exercised end to end in CI. The fixtures are synthetic, written to satisfy each scenario's checks; they prove the harness works, not that any particular agent behaves. `examples/README.md` says so plainly, and recording real trajectories from an agent under test remains a roadmap item that changes nothing in the pipeline when it happens.

## Status and roadmap

Implemented: transcript parser (numbered turns, strict format, SHA-256 capture), `deployment-contract.v2` JSON Schema plus mirrored Pydantic models, the fail-closed validator, the `validate` CLI, the `compile` pipeline with its extraction-adapter interface, the deterministic stub adapter, and the structured Claude adapter, the `approve` sign-off command, the `export-gate` compiler that renders typed acceptance rules into Given/When/Then scenarios linked to requirement ids and transcript turns, the `run` command with replay and live modes and run-level SLO enforcement, the `report` renderer with the brand.v1 policy system, `keygen` for the approval key pair, two complete example domains with golden contracts and replay fixtures, example brand files, and a 280-test suite (233 of which need no optional dependency; the rest exercise the gate integration and are skipped, and separately run in CI, when agent-eval-gate is absent) that includes loading both exports with agent-eval-gate's own scenario loader and executing both suites through its checks. The original bar, 10/10 runnable scenarios retaining requirement and transcript provenance with all three seeded conflicts surfaced before execution, is enforced by tests, and now so is the harder one: the same compiler produces a second domain's suite with no code path that knows either business.

`deployment-contract.v2` supersedes v1 and v1 documents are not accepted. To be precise about what that cost: the v1 schema was committed and public on this repository for about a week, so the honest claim is that no downstream code outside this repository ever integrated against it, not that it was never published. v2 adds `acceptance_rules` and `requirements[].out_of_band_verification`, adds `metadata.system`, and renames the cost SLO from `cost_per_invoice_eur` to `cost_per_task_eur` with an explicit `unit`, because a deployment contract is not an invoicing document.

Roadmap: recorded live trajectory fixtures for both examples (so the replay runs in CI exercise real model behavior rather than synthetic trajectories), judge calibration for the rubric scenarios, and numbered business documents as provenance sources alongside spoken transcripts. Further rule types are added only when they preserve executable checks and requirement-level provenance; the current five cover both bundled domains without a sixth.

## Non-goals

Audio transcription, a collaborative requirements editor, free-form document ingestion (a document must carry numbered, citable units before it can be a provenance source; see the roadmap), autonomous conflict resolution (conflicts are surfaced for humans, never auto-decided), a requirements ontology, dashboards.

## Stack

Python, Pydantic, Typer, JSON Schema, pytest; the live extractor uses the Anthropic SDK (optional `[llm]` extra), and `run` executes through agent-eval-gate.

The `report` command and cryptographic attestation add `jinja2` and `cryptography` as dependencies.

MIT © Mateusz Śliwiński
