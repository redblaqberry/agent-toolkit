# Durability: what breaks, when, and how you find out

This tool reads five upstream formats it does not control and produces an
artifact meant to be reopened years later. Both halves of that make change a
correctness problem rather than a maintenance chore. This document is the honest
inventory: what is defended, what fails safely, and what is still exposed.

The governing rule everywhere below: **when this project cannot establish
something, it must say so rather than assume it.** A wrong INCOMPLETE costs an
afternoon. A wrong GO is the failure the whole project exists to prevent.

---

## 1. Archival durability: can an old pack still be verified?

A sealed pack is the deliverable. If a later build of this tool cannot verify a
pack it sealed last year, the format is worthless.

| Change | Effect on an existing sealed pack | Status |
|---|---|---|
| Wording of an explanation improves | Verifies clean, with an informational note that the prose differs | Defended |
| A new field is added to `decision.v1` | Verifies clean (comparison covers the semantic core, not the document) | Defended |
| The meaning of a rule changes | Integrity confirmed; recomputation explicitly **skipped** and reported as skipped | Defended |
| Evidence, policy, or decision is edited | Fails, naming the file | Defended |
| `decide` under one build, `seal` under a later one | `seal` refuses to sign a decision whose semantics version is not this build's | Defended |
| Python or pydantic version drifts | Pinned by `constraints.txt` for CI and development installs only | **Partly** |

This was a real defect, not a hypothetical. Before the fix, verification
compared the entire decision document, so changing a single word in an
explanatory message made every previously sealed pack fail with "the sealed
decision does not follow from the sealed evidence": a false accusation of
tampering against an untouched artifact. A tool that cries wolf over its own
cosmetic edits will not be believed when it reports a real forgery.

The fix separates the verdict from its wording. `Decision.semantic_core()`
contains the state, every status, the input digest, and the coverage sets, and
excludes all prose. `semantics_version` distinguishes "this pack was decided
under rules I no longer implement" from "this pack was tampered with", and the
two get different answers. `tests/test_durability.py` pins both directions,
including that a status change is still caught even though prose is ignored.

**Which copy of the version is trusted.** A durability allowance that skips
recomputation is a way to switch the strongest check in the project off, so the
version that triggers it cannot be read from the document being checked.
`decision.json` is exactly the file a forger edits, and declaring a
`semantics_version` this build does not implement would otherwise buy a clean
verification for any verdict at all. `seal` stamps the current build's version
into the manifest, the signature covers it, and `verify` refuses any pack whose
decision and manifest disagree: one build wrote both files, so a disagreement
means the decision was edited afterwards.

**What the skip is worth.** When the decision cannot be rechecked, `verify`
exits 2, not 0. The pack is intact and nothing about it is being disputed, but
"the bytes are the ones that were signed" is a weaker claim than "and the
verdict follows from them", and a gate written as `revpack verify && ship`
cannot tell the two apart if they share an exit code. Exit 2 is the code this
project already uses for "could not decide", which is precisely what happened.

The companion half matters just as much: when the decision **is** rechecked,
`verify` exits on the verdict it confirmed rather than on whether the pack was
readable. Returning 0 for any parseable pack gave a release gate the same
answer for a proven GO and a proven refusal, which made the recomputation
described above worth nothing to the one caller most likely to rely on it.

**`SEMANTICS_VERSION` is now 5.** Every pack sealed by a build that stamped an
earlier version verifies under this build as "integrity verified, but the
decision was NOT rechecked", and exits 2. That is the designed behaviour of the
skip above, not a regression, but it is user-visible: a pack that used to verify
green now reports that its verdict cannot be re-established. The rule changes
that forced each bump are the fail-closed treatments listed in section 2, every
one of which can turn a former PASS into INCONCLUSIVE, so recomputing an older
pack under current semantics could contradict a decision that was correct when
sealed. The v4 changes are three more readings that stopped standing in for an
answer: a gate run whose `mode` is outside agent-eval-gate's own `live | replay`
vocabulary is refused rather than read as live (which had disarmed both replay
protections at once); a StateDiff verdict whose checks are empty or entirely
`not_applicable` is inconclusive rather than passing; and a cost ceiling is
unevidenced for a scenario the run put no price against, rather than satisfied by
the priced scenarios beside it.

The v5 changes are further fail-closed readings and one decision rule: a declared
but null SLO ceiling is read as unevidenced rather than absent; a `clean` canary
report carrying a breaking or risky change group is graded on its groups, not its
label; a behavioral check name with no identifier after its prefix, a StateDiff
check with no id, and an all-null approval signature each stop standing in for
evidence; and, at the decision, a collected reading that normalizes to FAIL blocks
the release whether or not the policy required its evidence class, so a proven
failure held in the pack can no longer disappear for want of a matching rule.

---

## 2. Upstream format drift

Two of the five producers emit **no version field at all**, which is the root
difficulty and not something this project can fix from the outside.

| Upstream change | How it is noticed | Result |
|---|---|---|
| StateDiff, canary, contract, or SiloBench golden bumps to `.v2` | Explicit format-string check | Refuses, exit 2, names the format. **Safe** |
| A field is added to `run.json` | Not noticed | Ignored. Harmless |
| `results[]` changes shape | Type guards | Clean `InputError`, exit 2. **Safe** |
| A nested field the parser reads changes type | Type guards on every field a verdict is derived from | Clean `InputError`, exit 2. **Safe** |
| agent-eval-gate adds a **new check name** | Not noticed | Counted as non-behavioral, so the scenario reads INCONCLUSIVE. **Fails safe, but wrong** |
| agent-eval-gate starts serializing `passed` | Not noticed | This project keeps deriving. Derivation could diverge from upstream. **Exposed** |
| DiscoverySpec changes `attest.v1` canonicalization | Not noticed | Signature incompatibility. **Exposed** |
| SiloBench adds tasks beyond TASK-12 | Golden lookup handles it | Works |
| The canary drops `hard_failures` or `state_failures` | Required on a `clean` report | Refuses, exit 2. **Fails safe** |
| SiloBench drops `profile` or `schema_version` | Required | Refuses, exit 2. **Fails safe** |
| The gate drops per-step `latency_s` | No usable measurement | Ceiling reported unevidenced, INCOMPLETE. **Fails safe** |
| The gate drops `trajectory.model` | Model claim cannot be established | Binding INCONCLUSIVE. **Fails safe** |
| A repeated JSON or YAML key in any input | Duplicate-key rejection at every parse site | Refuses, exit 2. **Safe** |
| A producer's semantics change without a format bump | Not noticed | **Exposed** |

The "nested field the parser reads" row is scoped deliberately. Every field a
verdict is derived from is now type-checked at the point it is read: a check name
that is not a string, a scenario or task id that is not a string, a requirement
or turn list that is a bare string rather than an array, a canary `affected`
entry that is not an object or names no scenario, and a `baseline`/`candidate`
side that is not an object each raise a clean `InputError` rather than reaching a
string method (which used to surface as an `AttributeError`) or being silently
skipped (which used to shrink a claim set the conflict rule compares). Fields the
parser only carries and never reads to decide are not in scope: a non-hash string
where a digest belongs is caught where it is compared, not where it is read, and
a field no rule consults can be any JSON type without changing a verdict.

### The exposures, stated plainly

Three were here from the start. Two more were added deliberately when the
parsers stopped ignoring values they did not recognise, and they belong on this
list because they trade one failure mode for another rather than eliminating it.

**The behavioral-check allowlist is a hardcoded guess at another tool's
vocabulary.** `classify_check` decides what counts as an assertion about agent
behaviour by matching prefixes copied from `agent-eval-gate/checks.py`. If that
file gains a check type, this project will not count it, the scenario will read
INCONCLUSIVE, and a healthy release will be reported incomplete. That direction
is deliberate (the alternative lets an unrecognized check stand in for evidence)
but it is still wrong, and it is invisible until someone investigates a puzzling
INCOMPLETE. The real fix belongs upstream: agent-eval-gate should classify its
own checks and say which ones assert behaviour. Until then the conformance suite
catches removals, not additions.

**Two producer vocabularies are now closed sets hardcoded here.** The state
oracle's per-check statuses (`pass`, `fail`, `not_applicable`) and the canary's
change severities (`info`, `risky`, `breaking`) used to be filtered: anything
unrecognised was quietly dropped, so a check status of `"unknown"` counted as
passing and a `catastrophic` change group vanished from a `clean` report. Both
are now validated against the vocabulary this project knows. The cost is the
same shape as the allowlist above: if either producer adds a member, this build
reports INCONCLUSIVE for the state oracle or refuses with exit 2 for the canary,
and a healthy release is reported as unverifiable. That is the safe direction
and it is still wrong, and the real fix is the same one, upstream.

**Fields this build now requires were previously defaulted.** Missing per-step
timing, absent canary replay counters, an empty SiloBench profile, and a
scenario naming no model each used to read as zero, clean, or fine. Each is
now a refusal or an INCONCLUSIVE. The trade is the same one as the two
vocabularies above: a producer that stops emitting one of these gets a loud
failure instead of a silent pass, which is the correct direction and is still
a healthy release reported as unverifiable. Same upstream fix.

**`attest.v1` compatibility with DiscoverySpec is asserted by this project's own
tests.** Both implement the same scheme, and `tests/test_canonical_attest.py`
pins the canonical form against fixed vectors, but nothing compares against a
document DiscoverySpec actually signed, because no signed example exists in that
repository yet. If its canonicalization drifts, the two will silently disagree.
The fix is one shared signed fixture, and it needs repo 01 to produce one.

**A producer can change meaning without changing format.** If StateDiff redefines
what `pass` implies while keeping `statediff.verdict.v1`, nothing here notices.
This is not solvable by a consumer; it is an argument for the conformance suite
below, which at least detects output that changes shape.

---

## 3. How this is tested

Four layers, weakest claim to strongest.

**Unit and integration, 426 tests.** Runs against fixtures written by hand to
match this project's understanding of the upstream schemas. Proves internal
consistency and nothing about whether that understanding is correct. A parser
can be perfectly self-consistent and still wrong about the format it reads.

**Conformance, 29 tests, 22 of which run anywhere.** Parses artifacts the real
producers actually emitted, vendored into `fixtures/conformance/` with
`PROVENANCE.md` recording where each came from. This is the layer that would
have caught a misunderstood schema. It confirms, against real output: the
contract carries 19 requirements and is correctly reported as unsigned; the gate
runs contain the real 12-task golden set with every scenario carrying at least
one behavioural assertion; `run.json` genuinely has no verdict field; six
SiloBench golden hashes really are identical; and StateDiff's recorded
before-state really does equal SiloBench's committed initial hash, two tools in
two languages agreeing on one value.

**Drift guard, 6 tests.** Compares each vendored copy against its upstream origin
whenever the sibling workspace is present, and skips otherwise. A failure means
an upstream tool changed its output and this project has not caught up.

**Live producer, 1 test.** Actually executes StateDiff's CLI and compares the
result to the vendored copy. The only test that notices a genuine format change
on the day it happens rather than at integration time.

### What the test suite still cannot tell you

- **Nothing here has consumed a live agent-eval-gate run against SiloBench**,
  because no such run has ever been recorded. The gate artifacts are real, but
  they came from a replay in MCP Contract Canary, not from an end-to-end
  execution of the producing stack.
- **CI verifies a Linux-built pack on Windows.** The roundtrip jobs build and
  seal on Linux, then verify the transported bytes on Windows, so the LF-newline
  and POSIX-path pinning is exercised rather than only asserted. What is still
  not covered is the reverse direction: packs are also built on Windows during
  development, and no job verifies a Windows-built pack on Linux.
- **No suite-wide mutation testing.** Each fix in the two fail-closed batches
  was mutation-tested individually: the fix was reverted, its regression test
  confirmed failing, then restored. That establishes those specific tests are
  not vacuous. It says nothing about the other several hundred, so the suite's
  overall ability to detect an injected bug is still unmeasured, and this
  remains the highest-value next test investment. The number that makes the
  case is not 158 tests passing over five vacuity holes; it is **324 tests
  passing while ten artifact-level forgeries still produced GO**. A suite can
  be large, green, and blind at the same time, and only mutation testing
  measures which.
- **No property-based testing** over generated evidence and policy combinations.
  The safety property is pinned by examples, not explored.

---

## 4. Honest summary

**Archival durability: solid.** A pack sealed today verifies under a later build,
and the two ways that could go wrong are distinguished and tested.

**Upstream durability: adequate, with five named exposures.** Format bumps fail
closed and loudly. Shape changes fail closed and loudly. Vocabulary additions and
silent semantic changes do not, and three of the five exposures need an upstream
change to close properly.

**Test durability: better than it looks from the count.** The 455 number is
misleading on its own; what matters is that 29 of them run against output five
real tools produced, and one re-runs a producer to check the corpus has not gone
stale. That live producer test previously could not run on Linux at all, because
it resolved a Windows-only executable path, so the drift signal this section
credited it with did not exist on CI until that was fixed. The gap that remains
is that no artifact in this project has yet come from a live end-to-end run of
the full producing stack, which is blocked on work in the upstream repositories.
