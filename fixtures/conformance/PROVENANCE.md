# Conformance corpus: real producer output, vendored

Every file here was emitted by the tool named below, not written by hand. They
exist so the conformance suite runs everywhere, including CI, where the sibling
repositories are not checked out. Without them the tests that matter most, the
ones that check this project against reality rather than against its own
assumptions, would silently skip and the suite would still report green.

Vendored 2026-07-18.

| File | Producer | Origin | How it was produced |
|---|---|---|---|
| `discoveryspec-approved-contract.json` | DiscoverySpec 0.1.0 | `01-discoveryspec/examples/invoice_automation/approved-contract.json` | committed example, `deployment-contract.v1` |
| `gate-run-clerk.json` | agent-eval-gate 0.1.0 | `04-mcp-contract-canary/tests/data/golden-run-clerk.json` | real replay of the 10 clerk tasks against captured SiloBench traces |
| `gate-run-approver.json` | agent-eval-gate 0.1.0 | `04-mcp-contract-canary/tests/data/golden-run-approver.json` | real replay of the 2 approver tasks |
| `gate-run-v2-clerk.json` | agent-eval-gate 0.1.0 | `04-mcp-contract-canary/tests/data/golden-run-v2-clerk.json` | real replay against the v2 candidate contracts, genuinely failing |
| `canary-report.json` | mcp-contract-canary 0.1.0 | `04-mcp-contract-canary/report.json` | real `mcp-canary verify` run, 2026-07-18 18:41 |
| `silobench-golden-hashes.json` | SiloBench 0.1.0 | `02-silobench/packages/scenario/fixtures/golden-hashes.json` | committed golden, `silobench-golden.v1` |
| `statediff-verdict-SD-PAY-01.json` | StateDiff 0.1.0 | generated | `statediff check --scenario scenarios/payment-release.yaml --before fixtures/baseline/payment/before-snapshot.json --after ... --json` |

## Honesty notes

- **The canary report and the v2 gate run describe an authored scenario.** The
  v2 contracts are a hypothetical vendor update written as fixtures in repo 04,
  not a change SiloBench actually made. The *artifacts* are genuine tool output;
  the *situation* they describe is constructed. Both facts matter.
- **The DiscoverySpec contract is approved but unsigned.** Its `approved` status
  is a structural claim, and the conformance suite asserts that this project
  reports it as unsigned rather than treating approval as authenticated.
- **The canary report's consumer map was semantically wrong and has been
  corrected upstream.** It cited requirement ids that resolved but described
  something else, so referential integrity passed every one of them and hid the
  mismatch. One conformance test now asserts the corrected mapping. It is kept
  rather than deleted because the defect class returns whenever a requirement id
  is chosen by position instead of by what the requirement says.
- **Invocation paths in these artifacts are redacted.** Producer output records
  the argv it was invoked with, which embeds an absolute workspace layout that
  no parser or rule here reads. `redact_machine_paths` in `tests/conftest.py`
  replaces those with `<workspace>` and `<tmp>` placeholders. Every verdict,
  finding, count, and hash is exactly as the producer emitted it, and the drift
  guard redacts both sides before comparing, so redaction can never mask an
  upstream change. Re-vendor by applying that function to the upstream file.

## Staleness

Vendored copies rot. `test_conformance.py::test_vendored_corpus_matches_upstream`
compares each file against its origin whenever the sibling workspace is present,
and skips when it is not. A drift there means an upstream tool changed its
output and this project has not caught up yet.
