# Release Evidence Pack

A signed, independently verifiable release decision pack whose verdict can be recomputed from its own evidence.

Five tools produce evidence about an agent deployment: an approved requirements contract, an evaluation run bundle, a state oracle's verdicts, a contract blast-radius report, and a golden environment fingerprint. Each proves something about its own slice, and each validates only its own file. The decision to ship is made across all of them, which is exactly where nothing checks anything.

`revpack` collects those artifacts, binds each one to the declared release environment and contract (an environment-level tie, not a run-level one: see "What it refuses to claim"), evaluates a written policy against them, and produces `decision.json`: `GO`, `NO_GO`, or `INCOMPLETE`. It then seals the pack with an Ed25519 signature over a canonical manifest, so someone who did not run any of the tools can check the result later.

## Quickstart

Requires Python >= 3.10.

```bash
pip install -e ".[dev]" -c constraints.txt
pytest -q          # 455 tests. On a standalone checkout 7 conformance cases
                   # skip (6 upstream drift guards and 1 live producer test,
                   # all needing the sibling repositories), plus 5 that need
                   # symlink privileges, POSIX mode bits, or POSIX filename
                   # semantics.
```

Build, seal, and verify a pack from the committed fixtures:

```bash
revpack keygen --private sign.pem --public verify.pem

revpack build \
  -e contract=fixtures/clean/contract/approved-contract.json \
  -e gate-run=fixtures/clean/gate-run/run.json \
  -e state-verdict=fixtures/clean/state-verdict/SD-PAY-01.json \
  -e blast-radius=fixtures/clean/blast-radius/report.json \
  -e environment-golden=fixtures/clean/environment/golden-hashes.json \
  -e silobench-verify=fixtures/clean/silobench-verify/verify.json \
  --policy fixtures/clean/policy.yaml \
  --release fixtures/clean/release.yaml \
  --traceability fixtures/clean/traceability.yaml \
  --out pack/

revpack seal   --pack pack/ --key sign.pem
revpack verify --pack pack/ --key verify.pem
```

That prints `GO`, then `decision GO recomputed and matches` and `verified verdict: GO`. Swapping `gate-run`, `blast-radius`, and `silobench-verify` for their `fixtures/defect/` counterparts produces `NO_GO` and exit 1 from `build`, and sealing that pack and verifying it also exits 1.

Exit codes: `0` GO, `1` NO_GO, `2` INCOMPLETE, infrastructure failure, or a verification that could not be completed. `verify` exits on the verdict it confirmed, not merely on whether it could read the pack, so `revpack verify && ship` refuses a sealed `NO_GO` instead of accepting it. An infrastructure fault never borrows a decision's exit code: a full disk or an unreadable pack exits 2, never 1, because reporting a crash as `NO_GO` would fabricate a proven refusal to ship. Unexpected exceptions still print a traceback, since swallowing it would trade a debugging problem for a worse one, but the exit code stays 2.

## The property worth reading the code for

`verify` does not only recompute hashes. It re-evaluates the policy against the sealed evidence and compares the result to the sealed `decision.json`.

This matters because a pack whose decision was forged *before* signing is cryptographically perfect. Every hash matches, the signature verifies, nothing changed after sealing. Hash checks prove a pack was not altered; they say nothing about whether its verdict ever followed from its evidence. `tests/test_pack.py::test_b8_decision_forged_before_sealing_is_caught` builds exactly that pack, and its companion test proves the forgery passes every hash and signature check first.

The recomputation can legitimately be skipped: a pack sealed under older rule semantics cannot be re-judged by a build that implements different ones, and calling that tampering would be a false accusation. That skip is decided by the semantics version recorded in the **signed manifest**, not by the copy inside `decision.json`, and a disagreement between the two is itself refused. One build writes both, so they cannot differ unless the decision was edited. `seal` refuses to sign a decision computed under different rule semantics in the first place, so the mismatch is named once at signing rather than discovered at every future verification.

The exit code follows the same logic. When the decision cannot be rechecked, `verify` exits 2 rather than 0, because "these are the bytes that were signed" is a weaker claim than "and the verdict follows from them" and the two must not return the same answer. When it can be rechecked, `verify` exits on the verdict it just confirmed. A tool that returned success for every pack it could parse would tell a release gate the same thing about a proven GO and a proven refusal.

## What it refuses to claim

Matching `before_state_hash` proves a common seeded environment, not that the state oracle graded the same agent run the evaluation harness recorded. Six of SiloBench's twelve golden task hashes are identical, asserted against the real golden file, and no producer emits a shared run id. That gap is reported in the decision and pinned by a test rather than papered over. Likewise `signature_present` is named for what it checks, because verifying a producer's own attestation would need that producer's public key, which this version does not take.

The same discipline runs through the parsers. A producer's `"passed"` must be an actual JSON boolean, because `"false"` is a non-empty string and every truthiness test reads it as success. A missing flag is refused rather than defaulted, since a producer that did not say is not a producer that said no. Cost and latency must be finite numbers, because `NaN > limit` is false and would otherwise satisfy a blocking ceiling. A blocking rule that resolves to "not applicable" blocks the release instead of passing quietly, because a requirement nobody evaluated is not a requirement that was met.

The list of cases that report rather than pass is long, and every one of them used to yield `GO`: a run the committed golden cannot be applied to (a release descriptor that declares it expects non-default runs makes the gap attributable inside the signed pack rather than a switch the graded party controls, but the final state is still ungraded, so the run reports `INCOMPLETE` rather than clearing), a check status outside the state oracle's vocabulary, a change severity outside the canary's, a traceability id that resolves to no collected artifact, a trajectory recording no timing at all (absent timing is not zero timing, and zero satisfies any ceiling), a `clean` canary report that states no replay counters, a task recording no environment profile, a scenario naming no model, a golden that does not identify itself or whose task hashes are not digests, and two fingerprints that are equal without being digests.

One case is different in kind. A duplicate JSON object key or YAML mapping key is refused outright, because every parser in reach is last-value-wins: `{"passed": false, "passed": true}` reads as failing to the person who opens the file and as passing to the tool, and hashing, sealing, and recomputation all run over the parsed structure, so that reading survives the entire pipeline with every signature intact. This matters most for `policy.yaml`, which is not evidence but the terms of the decision: a rule carrying `blocking: true` and then `blocking: false` is advisory to the engine and blocking to its reader, and the forged policy is copied into the pack and covered by the signature.

## Tamper boundaries

Verification covers each of these, with a test per case:

| Boundary | Attack it refuses |
|---|---|
| Evidence bytes | An edited or truncated evidence file |
| Policy | An edited policy after sealing |
| Decision | An edited `decision.json` after sealing |
| Directory closure | A file added to the pack, nested inside it, or deleted from it |
| Manifest and attestation | A stripped signature, an edited hash, an unknown field, the wrong key |
| Derivation | A decision forged before sealing, which every hash and signature accepts |
| Semantics binding | A forged semantics version used to disarm the recomputation |
| Parsed meaning | A repeated JSON or YAML key, where the file reads one way and parses another |

Packs contain no symlinks. The walk refuses them rather than skipping them, because a directory symlink is not descended into while a file symlink is followed and hashed, so silently skipping one would let unlisted content sit inside a pack that verifies clean.

## Durability

A pack sealed today must still verify under a later build, so verification compares the verdict and every status while ignoring the wording that explains them, and a change in rule *meaning* is reported as "integrity confirmed, decision not rechecked" rather than as tampering. `docs/durability.md` is the full inventory, including the upstream exposures that are not defended and why.

## Conformance fixtures

Most tests run against fixtures written to match this project's own understanding of five schemas, which proves internal consistency and nothing about whether that understanding is right. A separate conformance suite parses artifacts the five producers actually emitted, vendored under `fixtures/conformance/` with recorded provenance, plus a drift guard that re-checks them against upstream and one test that executes StateDiff's real CLI.

Invocation paths in those vendored artifacts are redacted to `<workspace>` and `<tmp>` placeholders, because producer output records the argv it was invoked with and no parser here reads a command string. Every verdict, finding, count, and hash is exactly as the producer emitted it, and the drift guard redacts both sides before comparing, so redaction can never mask an upstream change. `fixtures/conformance/PROVENANCE.md` documents each file's origin and the re-vendoring procedure.

## Project structure

```
src/revpack/
  parsers.py     five producer families, five different ideas of a verdict
  binding.py     ties each artifact to the declared release identity
  policy.py      the rule engine and the evidence-class reductions
  lattice.py     the status lattice and its reductions
  pack.py        collect, decide, seal, verify
  canonical.py   canonical JSON, hashing, atomic writes
  attest.py      Ed25519 keygen, signing, verification
  cli.py         keygen / collect / decide / build / seal / verify
fixtures/clean/       a release that should ship
fixtures/defect/      the same release with real failures
fixtures/conformance/ genuine producer output, vendored
docs/durability.md    what breaks across versions, and what is not defended
```

## Not built

- No rendered dossier. The pack is JSON and a manifest; there is no HTML or Markdown report generator.
- No EU AI Act Annex IV export. The mapping was designed and is not implemented, and the honest version of that feature is a coverage report showing how little of Annex IV an evaluation stack can actually populate, not a generated document.
- No release comparison. Packs are verified individually; `compare` does not exist.
- No artifact here has come from a live end-to-end run of the full producing stack. The vendored artifacts are genuine producer output, but they were produced separately rather than by one continuous execution.
- Signing uses a user-supplied Ed25519 key. This is explicitly not a qualified electronic signature, and the tool makes no legal determination of any kind.

## License

[MIT](LICENSE)
