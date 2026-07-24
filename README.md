# DiscoverySpec

**Compile technical discovery transcripts into reviewed, executable deployment contracts.** Agent deployments rarely fail on model quality first; they fail on requirements nobody wrote down, stakeholder statements that contradict each other, and questions nobody answered before go-live. DiscoverySpec addresses that with a versioned `deployment-contract.v1` document (KPIs, roles, allowed actions, escalation rules, security constraints, latency and cost ceilings) in which every field traces back to a numbered customer statement, plus an acceptance suite that runs through [agent-eval-gate](https://github.com/redblaqberry/agent-eval-gate).

The full pipeline ships today: the contract format, a complete example discovery call, a fail-closed validator, the `compile` pipeline with a deterministic stub adapter and a structured Claude extraction adapter, the `approve` sign-off step that stamps a fully resolved draft, the scenario compiler that exports the ten-scenario acceptance suite in agent-eval-gate's native format, `run`, which executes that suite through agent-eval-gate and links every verdict back to the originating customer statement, and `report`, which renders a run as a manager-facing page under the customer's own brand rules.

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

pending fields: slo.cost_per_invoice_eur, data_governance

verdict: VALID
```

Exit codes are fail closed:

| Code | Meaning |
|---|---|
| 0 | structurally valid; draft findings (conflicts, open questions, pending fields) are reported, they are the product working |
| 1 | approval violation: the contract claims approved but has open conflicts, blocking questions, missing sign-off, no transcript pin, unfilled required fields, or an adopted requirement wired into nothing; also returned for a draft under `--require-approved` |
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

This writes `scenarios.yaml` (ten Given/When/Then scenarios in agent-eval-gate's native format, provenance carried in tags like `REQ-005` and `T15`) and `gate-config.json` (SLOs plus the scenario-to-requirement map). Drafts are refused: no unreviewed contract can run.

Then `run` executes the suite through [agent-eval-gate](https://github.com/redblaqberry/agent-eval-gate) (installed separately, e.g. `pip install -e ../agent-eval-gate`):

```bash
discoveryspec run --contract approved-contract.json \
  --mode replay --fixtures fixtures/ --prices prices.json --out gate-run \
  --verify-key approval-key.pub --signing-key approval-key.pem
```

In the signed workflow, `--verify-key` refuses a contract whose approval attestation is missing or invalid, and `--signing-key` (the same authority key here; a separate run key also works) attests the run report so `report --verify-key` can later prove the verdict was not edited. Both flags are optional; without them the run is structural only. The suite is compiled from the approved contract at run time (never a stale export) and loaded back with the gate's own scenario loader, so what runs is exactly what any agent-eval-gate user would run. Replay mode executes recorded trajectory fixtures deterministically with no API key; live mode (`--mode live --agent <module>`) runs the agent under test against the Messages API, with the gate's LLM judge on rubric scenarios. Two SLOs from the contract are enforced across the run: statistical p95 latency (nearest-rank, deliberately pessimistic at small n) and the per-invoice cost ceiling checked against every scenario, priced from a EUR price table (`--prices` is mandatory; a run that cannot verify a promised ceiling refuses instead of skipping the check). `run` writes `run.json` in agent-eval-gate's report format plus `run-report.json`, where every verdict carries the requirement it enforces, the transcript turns behind it, and the quoted customer statements, so a failing scenario reads as a broken promise with the stakeholder's words attached. The report also records the content hash of the exact contract the run executed, and a run with harness errors carries the verdict INCOMPLETE, never a plain pass or fail. Exit codes stay fail closed: harness or judge errors are exit 2, a failing scenario or breached SLO is exit 1, and there is no non-strict mode.

Finally, `report` renders the run for the people who signed the contract:

```bash
discoveryspec report --contract approved-contract.json --run gate-run \
  --brand examples/brands/nordlicht-strict.json --out report.html \
  --verify-key approval-key.pub
```

One self-contained HTML page for a non-technical decision maker: the verdict up front, every broken promise with the stakeholder's quoted words from the call, all scenarios in plain language (the labels live in the scenario templates, so friendly text cannot drift from what a test checks), and the agreed limits against what was observed. The brand file supplies identity, palette, and font stacks as data, never CSS, and its policy flags are hard constraints: if the brand says no shadows, no gradients, a corner-radius cap, or no emoji, the renderer's typed style serializer is audited against those rules and the report REFUSES to render on any violation, exactly as it refuses a palette that fails WCAG AA contrast or a contract that is not byte-identical to the one the run executed.

## The bundled example

`examples/invoice_automation/` contains a complete fictional discovery call for supplier-invoice automation at a furniture retailer: a 41-turn transcript with three seeded stakeholder conflicts (autonomous posting vs named human approval on every write, an approver threshold of EUR 5000 vs EUR 500, best-model-regardless vs a EUR 0.08 per-invoice ceiling) and one blocking data-governance question that nobody in the room could answer. Two golden contracts show both ends of the pipeline: the draft as a correct extraction would produce it, and the approved contract after human resolution, including a recorded post-call answer from Legal and an amendment reconciling the touchless KPI with the mandatory approval rule.

## Status and roadmap

Implemented: transcript parser (numbered turns, strict format, SHA-256 capture), `deployment-contract.v1` JSON Schema plus mirrored Pydantic models, the fail-closed validator, the `validate` CLI, the `compile` pipeline with its extraction-adapter interface, the deterministic stub adapter, and the structured Claude adapter, the `approve` sign-off command, the `export-gate` scenario compiler (ten Given/When/Then acceptance scenarios, each linked to transcript turn ids and requirement ids), the `run` command with replay and live modes and run-level SLO enforcement, the `report` renderer with the brand.v1 policy system, `keygen` for the approval key pair, golden contracts, example brand files, and a 237-test suite that includes loading the export with agent-eval-gate's own scenario loader and executing the full suite through its checks. The original bar, 10/10 runnable scenarios retaining requirement and transcript provenance with all three seeded conflicts surfaced before execution, is enforced by tests.

Roadmap: recorded live trajectory fixtures for the bundled example (so the replay run in CI exercises real model behavior, not synthetic trajectories) and judge calibration for the rubric scenarios.

Beyond that, the next major step is extending DiscoverySpec past supplier-invoice automation: numbered business documents as provenance sources alongside spoken transcripts, and contract-driven acceptance scenarios built from a small versioned set of typed rule primitives (required action, forbidden action, escalation under a structured condition, measurable latency and cost limits) instead of domain-bound templates. Scenario export stays fail closed throughout: a requirement without sufficient test inputs or an observable outcome is reported as non-executable and blocks the deployment gate rather than becoming a weak generic test. Initial support covers a defined set of acceptance-rule types proven against a second business domain; further domain packs or rule types are added only when they preserve executable checks and requirement-level provenance.

## Non-goals

Audio transcription, a collaborative requirements editor, free-form document ingestion (a document must carry numbered, citable units before it can be a provenance source; see the roadmap), autonomous conflict resolution (conflicts are surfaced for humans, never auto-decided), a requirements ontology, dashboards.

## Stack

Python, Pydantic, Typer, JSON Schema, pytest; the live extractor uses the Anthropic SDK (optional `[llm]` extra), and `run` executes through agent-eval-gate.

The `report` command and cryptographic attestation add `jinja2` and `cryptography` as dependencies.

MIT © Mateusz Śliwiński
