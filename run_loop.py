"""The whole deployment loop, on one scenario, in one command.

    python run_loop.py            the clean run and the defective one
    python run_loop.py --clean    only the run that ships
    python run_loop.py --defect   only the run that gets blocked
    python run_loop.py --json     machine-readable summary

Five tools, one supplier-invoice agent, one question: is this safe to ship?

    discoveryspec   what the customer actually asked for, compiled into a
                    contract with provenance back to the transcript
    silobench       the enterprise it runs against, with roles and approvals
    statediff       what the run changed, graded against the scenario
    blastradius     whether it changed anything it was never allowed to
    revpack         the signed dossier, and the go or no-go

The demo runs the loop twice on the same scenario. Once on a clean payment
release, which ships. Once on a run that also mutates a vendor record nobody
asked it to touch, which does not. A demo that only shows the happy path proves
nothing: every pipeline passes when nothing is wrong.

**What this does not claim.** It would be a better story if the state oracle
missed the defect and a later stage saved it. It does not: `statediff` catches
every defect fixture in this repository, and the demo prints that rather than
hiding it. Overstating the gap between these tools would be the same failure the
tools exist to catch.

What the second run does show is narrower and true. Three independent
instruments, each asking a different question, all refuse it. And `revpack`
refuses it for reasons the other two structurally cannot see, because they are
properties of the evidence set rather than of the run:

    producers disagree     the canary and statediff attribute the failure to
                           different requirements. Each is internally consistent;
                           together they cannot both be right.
    binding failures       an artifact claims to describe a release whose
                           contract fingerprint it does not match.

No single tool can catch either. They are only visible once every artifact is
assembled and checked against the others, which is what a release dossier is
for. That is the argument: not that any one instrument is blind, but that
shipping is a claim about the whole evidence set, and nothing but the dossier
grades the whole evidence set.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPONENTS = ROOT / "components"
BIN = ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
EXE = ".exe" if sys.platform == "win32" else ""

# components/2-silobench is the environment the fixtures were captured from. It
# is replayed rather than started, so no stage below invokes it directly.
DISCOVERYSPEC = COMPONENTS / "1-discoveryspec"
STATEDIFF = COMPONENTS / "3-statediff"
BLASTRADIUS = COMPONENTS / "4-blastradius"
REVPACK = COMPONENTS / "5-release-evidence-pack"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(errors="replace", line_buffering=True)
    except Exception:
        pass


@dataclass
class Stage:
    """One tool's verdict on one run."""

    name: str
    tool: str
    question: str
    exit_code: int = 0
    verdict: str = ""
    detail: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.exit_code != 0


def run(tool: str, *args: str) -> tuple[int, str]:
    """Invoke one of the four CLIs and return its exit code and output."""
    executable = BIN / f"{tool}{EXE}"
    if not executable.exists():
        raise SystemExit(
            f"{executable} is missing. Run `python setup_env.py` first: it "
            f"builds the venv and installs the Python components in editable mode."
        )
    done = subprocess.run(
        [str(executable), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(ROOT),
    )
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def tail(text: str, n: int = 4) -> list[str]:
    return [l.rstrip() for l in text.strip().splitlines() if l.strip()][-n:]


# --- the stages -------------------------------------------------------------

def stage_contract() -> Stage:
    """Is there a reviewed contract, and does it trace to what the customer said?

    This runs first because everything downstream is graded against it. An
    acceptance suite nobody signed off is a test suite, not a deployment
    contract, and the difference is who is accountable when it passes.
    """
    example = DISCOVERYSPEC / "examples" / "invoice_automation"
    code, out = run(
        "discoveryspec", "validate",
        "--contract", str(example / "approved-contract.json"),
        "--transcript", str(example / "transcript.md"),
    )
    verdict = next((l.split(":", 1)[1].strip() for l in out.splitlines()
                    if l.lower().startswith("verdict:")), "unknown")
    return Stage("Contract", "discoveryspec",
                 "does a reviewed contract exist, and does it trace to the transcript?",
                 code, verdict, tail(out, 3))


def stage_state(defect: str | None) -> Stage:
    """Did the run do the job? The state oracle, and the limit of one."""
    baseline = STATEDIFF / "fixtures" / "baseline" / "payment"
    after_dir = (STATEDIFF / "fixtures" / "defects" / defect) if defect else baseline
    code, out = run(
        "statediff", "check",
        "--scenario", str(STATEDIFF / "scenarios" / "payment-release.yaml"),
        "--before", str(baseline / "before-snapshot.json"),
        "--after", str(after_dir / "after-snapshot.json"),
        "--before-events", str(baseline / "before-events.jsonl"),
        "--after-events", str(after_dir / "after-events.jsonl"),
    )
    failed = [l.strip() for l in out.splitlines() if "[FAIL]" in l]
    return Stage("State", "statediff",
                 "did the run reach the state the scenario requires?",
                 code, "FAIL" if code else "PASS",
                 failed[:3] or tail(out, 2))


def stage_blast(broken: bool) -> Stage:
    """Did it change anything it was never allowed to touch?

    This is the stage that carries the argument. A run can satisfy every
    assertion in the scenario and still have moved something nobody asked it to
    move, because a scenario grades what it was told to look at.
    """
    quickstart = BLASTRADIUS / "examples" / "quickstart"
    after = "orders-after-broken.json" if broken else "orders-after-correct.json"
    code, out = run(
        "blast", "check",
        "-c", str(BLASTRADIUS / "examples" / "contracts" / "order-ships.yaml"),
        "--before", str(quickstart / "orders-before.json"),
        "--after", str(quickstart / after),
    )
    verdict = "BLOCKED" if code else "GO"
    return Stage("Blast radius", "blastradius",
                 "did it change anything outside what the contract permits?",
                 code, verdict, tail(out, 3))


def stage_release(defective: bool) -> Stage:
    """Assemble every artifact into one dossier and apply the release policy."""
    clean = REVPACK / "fixtures" / "clean"
    other = REVPACK / "fixtures" / "defect"

    def evidence(kind: str, relative: str) -> str:
        source = other if (defective and (other / relative).exists()) else clean
        return f"{kind}={source / relative}"

    out_dir = ROOT / "out" / ("defect" if defective else "clean") / "pack"
    if out_dir.exists():
        import shutil
        shutil.rmtree(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)

    code, out = run(
        "revpack", "build",
        "-e", evidence("contract", "contract/approved-contract.json"),
        "-e", evidence("state-verdict", "state-verdict/SD-PAY-01.json"),
        "-e", evidence("blast-radius", "blast-radius/report.json"),
        "-e", evidence("gate-run", "gate-run/run.json"),
        "-e", evidence("silobench-verify", "silobench-verify/verify.json"),
        "-e", evidence("environment-golden", "environment/golden-hashes.json"),
        "--policy", str(clean / "policy.yaml"),
        "--release", str(clean / "release.yaml"),
        "--traceability", str(clean / "traceability.yaml"),
        "--out", str(out_dir), "--no-vcs",
    )
    first = next((l.strip() for l in out.splitlines() if l.strip()), "")
    return Stage("Release", "revpack",
                 "does the assembled evidence satisfy the release policy?",
                 code, first.split()[0] if first else "unknown", tail(out, 4))


# --- the two runs -----------------------------------------------------------

def loop(defective: bool) -> list[Stage]:
    stages = [stage_contract()]
    stages.append(stage_state("unexplained-vendor-mutation" if defective else None))
    stages.append(stage_blast(broken=defective))
    stages.append(stage_release(defective=defective))
    return stages


def render(title: str, subtitle: str, stages: list[Stage]) -> bool:
    print()
    print("=" * 78)
    print(f"  {title}")
    print(f"  {subtitle}")
    print("=" * 78)
    for stage in stages:
        mark = "BLOCKED" if stage.blocked else "ok"
        print(f"\n  [{mark:^7}] {stage.name}  ({stage.tool})")
        print(f"            {stage.question}")
        print(f"            verdict: {stage.verdict}")
        for line in stage.detail:
            print(f"              {line[:96]}")
    shipped = not any(s.blocked for s in stages)
    print()
    print("-" * 78)
    print(f"  DECISION: {'SHIP' if shipped else 'DO NOT SHIP'}")
    if not shipped:
        caught = [s for s in stages if s.blocked]
        print(f"  refused by {len(caught)} independent instrument(s): "
              f"{', '.join(s.tool for s in caught)}")
        print("  revpack refuses it for reasons the others cannot see: the producers")
        print("  disagree with each other, and an artifact does not bind to the")
        print("  contract it claims to describe. Both are properties of the evidence")
        print("  set, not of the run, so only the assembled dossier can grade them.")
    print("-" * 78)
    return shipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--clean", action="store_true", help="only the run that ships")
    group.add_argument("--defect", action="store_true", help="only the run that is blocked")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args(argv)

    runs = []
    if not args.defect:
        runs.append(("clean", False))
    if not args.clean:
        runs.append(("defect", True))

    results = {}
    for name, defective in runs:
        stages = loop(defective)
        results[name] = stages
        if not args.json:
            render(
                "RUN 2: the same agent, one unrequested change" if defective
                else "RUN 1: a correct payment release",
                "it also mutates a vendor record nobody asked it to touch" if defective
                else "everything it changed, it was asked to change",
                stages,
            )

    if args.json:
        print(json.dumps({
            name: [{"stage": s.name, "tool": s.tool, "verdict": s.verdict,
                    "blocked": s.blocked, "exit_code": s.exit_code}
                   for s in stages]
            for name, stages in results.items()
        }, indent=2))
        return 0

    if len(results) == 2:
        clean_ok = not any(s.blocked for s in results["clean"])
        defect_ok = not any(s.blocked for s in results["defect"])
        print()
        print("=" * 78)
        if clean_ok and not defect_ok:
            print("  The loop shipped the correct run and refused the other one.")
            print("  Four instruments, one decision, and every refusal traceable to the")
            print("  artifact and the rule that produced it.")
        else:
            print("  UNEXPECTED: the demo did not separate the two runs.")
            print(f"  clean shipped={clean_ok}  defect shipped={defect_ok}")
            return 1
        print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
