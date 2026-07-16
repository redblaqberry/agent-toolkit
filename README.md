# DiscoverySpec

**Compile technical discovery transcripts into reviewed, executable deployment contracts.** Agent deployments rarely fail on model quality first; they fail on requirements nobody wrote down, stakeholder statements that contradict each other, and questions nobody answered before go-live. DiscoverySpec addresses that with a versioned `deployment-contract.v1` document (KPIs, roles, allowed actions, escalation rules, security constraints, latency and cost ceilings) in which every field traces back to a numbered customer statement, plus an acceptance suite that runs through [agent-eval-gate](https://github.com/redblaqberry/agent-eval-gate).

What ships today: the contract format, a complete example discovery call, a fail-closed validator, and the scenario compiler that exports the ten-scenario acceptance suite in agent-eval-gate's native format. The LLM extraction (`compile`) and gate execution (`run`) stages are on the roadmap below and build on the same contract.

Most eval tooling starts from datasets, tasks, and scorers the user authors. DiscoverySpec starts one step earlier: it preserves provenance from customer statement to requirement to acceptance test, flags contradictory stakeholder requirements before anything runs, and refuses to let a contract with unresolved conflicts or unanswered blocking questions reach execution.

## How it works

```
transcript.md -> draft contract -> human approval -> contract JSON
             -> gate fixtures/config -> run results (with provenance links)
```

The contract is only executable through its requirements:

- Every KPI, role, action, escalation rule, security constraint, SLO, and data-governance entry references a requirement id.
- Every requirement cites the numbered transcript turns it was extracted from, or an explicitly recorded follow-up.
- The contract pins the SHA-256 of the exact transcript bytes its provenance came from; validating against a different transcript fails, and a contract cannot be approved without the pin.
- Contradictory stakeholder statements become explicit conflict pairs that must be resolved, with a signed rationale, before approval.
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
| 1 | approval violation: the contract claims approved but has open conflicts, blocking questions, missing sign-off, no transcript pin, or unfilled required fields; also returned for a draft under `--require-approved` |
| 2 | structural failure: schema or model violation, broken provenance, transcript mismatch, dangling references |

For CI and deploy gates, `--require-approved` makes exit 0 possible only for an approved, clean contract, so a draft can never pass a pipeline.

Once a contract is approved, compile its acceptance suite:

```bash
discoveryspec export-gate --contract examples/invoice_automation/approved-contract.json --out gate-export
```

This writes `scenarios.yaml` (ten Given/When/Then scenarios in agent-eval-gate's native format, provenance carried in tags like `REQ-005` and `T15`) and `gate-config.json` (SLOs plus the scenario-to-requirement map). Drafts are refused: no unreviewed contract can run.

## The bundled example

`examples/invoice_automation/` contains a complete fictional discovery call for supplier-invoice automation at a furniture retailer: a 41-turn transcript with three seeded stakeholder conflicts (autonomous posting vs named human approval on every write, an approver threshold of EUR 5000 vs EUR 500, best-model-regardless vs a EUR 0.08 per-invoice ceiling) and one blocking data-governance question that nobody in the room could answer. Two golden contracts show both ends of the pipeline: the draft as a correct extraction would produce it, and the approved contract after human resolution, including a recorded post-call answer from Legal and an amendment reconciling the touchless KPI with the mandatory approval rule.

## Status and roadmap

Implemented: transcript parser (numbered turns, strict format, SHA-256 capture), `deployment-contract.v1` JSON Schema plus mirrored Pydantic models, the fail-closed validator, the `validate` CLI, the `export-gate` scenario compiler (ten Given/When/Then acceptance scenarios, each linked to transcript turn ids and requirement ids), golden contracts, and an 84-test suite that includes loading the export with agent-eval-gate's own scenario loader.

Roadmap: `compile` (structured LLM extraction of the draft contract, followed by mandatory human approval; no unreviewed contract can run), `approve`, and `run`, which executes the exported scenarios through agent-eval-gate and links every verdict back to the originating customer statement. The roadmap is done when 10/10 runnable scenarios retain requirement and transcript provenance and all three seeded conflicts are surfaced before execution; the compiler and validator halves of that bar are enforced by tests today.

## Non-goals

Audio transcription, a collaborative requirements editor, arbitrary document ingestion, autonomous conflict resolution (conflicts are surfaced for humans, never auto-decided), a requirements ontology, dashboards.

## Stack

Python, Pydantic, Typer, JSON Schema, pytest.

MIT © Mateusz Śliwiński
