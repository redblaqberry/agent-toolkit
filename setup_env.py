"""Build the venv and install the Python components, editable, in one place.

    python setup_env.py           build it
    python setup_env.py --check   verify an existing one, install nothing

Four of the five components are Python and are installed here. The fifth,
`components/2-silobench`, is the TypeScript environment the fixtures were
captured from: it is not pip-installable, and the demo replays committed
captures rather than starting it, so it needs no install step.

Those four pin compatible dependencies (pydantic, typer, pyyaml), which is what
makes a single environment possible and is worth checking rather than assuming:
if a future version of one of them diverges, this is where it shows up, loudly,
instead of as a confusing failure three stages into the demo.

`ledger-agent` is deliberately not installed. It pulls google-adk and litellm,
which are heavy and which nothing in this demo calls. The agent's behaviour here
comes from committed fixtures rather than from a live model, so the loop is
deterministic and needs no API key.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOBS = ROOT / "components"
VENV = ROOT / ".venv"
BIN = VENV / ("Scripts" if sys.platform == "win32" else "bin")
PY = BIN / ("python.exe" if sys.platform == "win32" else "python")
EXE = ".exe" if sys.platform == "win32" else ""

# (directory under components/, import name, CLI name)
COMPONENTS = [
    ("1-discoveryspec", "discoveryspec", "discoveryspec"),
    ("3-statediff", "statediff", "statediff"),
    ("4-blastradius", "blastradius", "blast"),
    ("5-release-evidence-pack", "revpack", "revpack"),
]

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(errors="replace", line_buffering=True)
    except Exception:
        pass


def check() -> int:
    if not PY.exists():
        print(f"no venv at {VENV}. Run without --check to build it.")
        return 1
    bad = 0
    for directory, module, cli in COMPONENTS:
        done = subprocess.run([str(PY), "-c", f"import {module}"],
                              capture_output=True, text=True)
        has_cli = (BIN / f"{cli}{EXE}").exists()
        ok = done.returncode == 0 and has_cli
        bad += not ok
        why = "" if ok else (
            f"  import: {'ok' if done.returncode == 0 else 'FAIL'}, "
            f"cli: {'ok' if has_cli else 'missing'}")
        print(f"  {'ok  ' if ok else 'FAIL'} {directory:<26} {module:<22} {cli}{why}")
    print()
    print("environment is complete" if not bad else f"{bad} component(s) broken")
    return 1 if bad else 0


def build() -> int:
    missing = [d for d, _, _ in COMPONENTS if not (JOBS / d).is_dir()]
    if missing:
        print("these component directories are missing, so the demo cannot be "
              f"assembled: {missing}", file=sys.stderr)
        print("They are part of this repository under components/, so this "
              "checkout is incomplete.", file=sys.stderr)
        return 1

    if not PY.exists():
        print(f"creating {VENV}")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)

    subprocess.run([str(PY), "-m", "pip", "install", "-q", "--upgrade", "pip"],
                   check=False)
    for directory, _, _ in COMPONENTS:
        print(f"  installing {directory}")
        done = subprocess.run(
            [str(PY), "-m", "pip", "install", "-q", "-e", str(JOBS / directory)],
            capture_output=True, text=True)
        if done.returncode != 0:
            print(done.stdout[-2000:], file=sys.stderr)
            print(done.stderr[-2000:], file=sys.stderr)
            print(f"failed to install {directory}", file=sys.stderr)
            return 1
    print()
    return check()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify an existing environment, install nothing")
    args = parser.parse_args(argv)
    code = check() if args.check else build()
    if code == 0 and not args.check:
        print("\nnow run:  python run_loop.py")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
