"""Command line interface.

Exit codes follow the workspace convention: 0 GO, 1 NO_GO, 2 INCOMPLETE or
infrastructure failure. ``decision.json`` is the machine channel that separates
the last two, because a caller deciding whether to ship treats "cannot decide"
and "cannot run" identically.

``build`` deliberately stops before sealing. Signing is a human act with a key,
and folding it into a convenience command invites signing something nobody
read.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Optional

import typer

from . import attest
from .errors import InputError, RevpackError
from .models import EvidenceKind
from .pack import collect, decide, seal, verify

app = typer.Typer(add_completion=False, no_args_is_help=True)

EXIT_BY_STATE = {"GO": 0, "NO_GO": 1, "INCOMPLETE": 2}

COLOUR_BY_STATE = {
    "GO": typer.colors.GREEN,
    "NO_GO": typer.colors.RED,
    "INCOMPLETE": typer.colors.YELLOW,
}

_KINDS = set(EvidenceKind.__args__)  # type: ignore[attr-defined]


def _fail(exc: RevpackError) -> None:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    for problem in exc.problems:
        typer.secho(f"  - {problem}", fg=typer.colors.RED, err=True)
    raise typer.Exit(exc.exit_code)


def _parse_evidence(items: list[str]) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for item in items:
        if "=" not in item:
            typer.secho(
                f"error: --evidence expects kind=path, got {item!r}", fg=typer.colors.RED, err=True
            )
            raise typer.Exit(2)
        kind, path = item.split("=", 1)
        if kind not in _KINDS:
            typer.secho(
                f"error: unknown evidence kind {kind!r}; expected one of {sorted(_KINDS)}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)
        parsed.append((kind, path))
    return parsed


def _report(decision) -> None:
    typer.secho(f"{decision.state}", fg=COLOUR_BY_STATE[decision.state], bold=True, err=True)
    typer.secho(
        f"  release {decision.release_id} under policy {decision.policy_id}", err=True
    )
    for outcome in decision.evidence_classes:
        if outcome.status.value != "PASS":
            typer.secho(f"  evidence {outcome.kind}: {outcome.status.value} ({outcome.detail})", err=True)
    for rule in decision.rules:
        # NOT_APPLICABLE is noise on an advisory rule and a gap on a blocking
        # one: the policy said the release must not clear without this check and
        # the check declined to run. Hiding it made the state INCOMPLETE for a
        # reason the operator could not see anywhere in the report.
        if rule.status.value == "PASS":
            continue
        if rule.status.value == "NOT_APPLICABLE" and not rule.blocking:
            continue
        marker = "blocking" if rule.blocking else "advisory"
        typer.secho(f"  rule {rule.id} [{marker}]: {rule.status.value} ({rule.detail})", err=True)
        for finding in rule.findings[:5]:
            typer.secho(f"      {finding}", err=True)
    for binding in decision.bindings:
        if binding.status.value != "PASS":
            typer.secho(
                f"  binding {binding.evidence_id} {binding.check}: {binding.status.value}", err=True
            )


KEY_OVERWRITE_PROBLEM = (
    "every pack ever sealed with the current private key becomes unverifiable the moment "
    "it is gone, and an archival tool that destroys its own history on a repeated command "
    "is not archival; pass --force only if no sealed pack depends on this key"
)


def _refuse_occupied_key_paths(paths: list[Path], force: bool) -> None:
    """Refuse before writing anything if any destination is already taken.

    ``_write_key`` refuses an existing file on its own, but it does so one file
    at a time, and a keypair is not written one file at a time in any sense that
    matters. Writing the private key and then refusing on the public one left a
    brand new private key on disk beside an unrelated old public key: every pack
    sealed under the old key still verifies against that public key, nothing in
    either file says which one it belongs to, and the command that produced the
    mismatch reported an error and exited. A refusal has to leave the filesystem
    as it found it.

    ``lexists`` rather than ``exists``: a dangling symbolic link is not a file
    this can write through, and ``exists`` follows the link and reports nothing
    there, which would put the check back out of step with ``O_EXCL``.
    """
    if force:
        return
    occupied = [str(p) for p in paths if os.path.lexists(p)]
    if occupied:
        raise InputError(
            f"refusing to overwrite the existing key(s) {', '.join(occupied)}",
            problems=[KEY_OVERWRITE_PROBLEM],
        )


def _refuse_one_destination_for_both_keys(private: Path, public: Path) -> None:
    """Refuse a keypair whose two halves would be written to one file.

    Only one of them can survive there, and neither outcome is recoverable.
    Without ``--force`` the private key was written first and the public one
    refused on top of it, so the command reported an error while leaving a
    signing key sitting at the path the operator meant to publish. With
    ``--force`` the public key was written over the private key, and the only
    copy of the key every pack sealed afterwards would be signed with was gone
    before the command finished.

    Compared after resolution and case folding, because ``k.pem`` and ``./K.PEM``
    are one file on the platform where the mode bits are advisory anyway, and a
    check that compares the two strings would wave that spelling through.
    """
    if os.path.normcase(str(private.resolve())) == os.path.normcase(str(public.resolve())):
        raise InputError(
            f"refusing to write both halves of a keypair to {private}",
            problems=[
                "a private key and a public key cannot share a destination: one is written "
                "over the other, and whichever survives is not the pair you can both seal "
                "and verify with"
            ],
        )


def _write_key(path: Path, pem: bytes, mode: int, force: bool) -> None:
    """Create a key file, refusing to write over one that already exists.

    ``Path.write_bytes`` creates with whatever the process umask allows, which
    on a shared host or a CI runner routinely leaves a signing key readable by
    every account on the box. Creating through ``os.open`` with an explicit mode
    fixes that, and ``O_EXCL`` closes the gap between testing for an existing
    file and writing over it.

    The mode bits are advisory on Windows: the file inherits the directory ACL
    and 0o600 is approximated at best. The flag is passed regardless, because
    the POSIX case is where signing keys sit on shared infrastructure, and a
    weaker platform is not a reason to write a weaker file everywhere else.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if force:
        # Removed rather than truncated, so the replacement is created fresh
        # under `mode` instead of inheriting the permissions of whatever was
        # there before.
        path.unlink(missing_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    except FileExistsError as exc:
        raise InputError(
            f"refusing to overwrite the existing key {path}",
            problems=[KEY_OVERWRITE_PROBLEM],
        ) from exc
    except OSError as exc:
        raise InputError(f"cannot create {path}: {exc}") from exc
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(pem)
    except OSError as exc:
        # O_EXCL means this call created the file, so a write that fails partway
        # has left a partial key nobody should keep. Remove it, so a single
        # failed write leaves nothing rather than a truncated PEM that looks like
        # a key and is not.
        path.unlink(missing_ok=True)
        raise InputError(f"cannot write {path}: {exc}") from exc


def _write_keypair(
    private_path: Path,
    private_pem: bytes,
    public_path: Path,
    public_pem: bytes,
    force: bool,
) -> None:
    """Write both key files, or leave neither half of the new pair behind.

    ``_write_key`` makes each file all-or-nothing on its own, but the two are
    halves of one object and the failure this closes is between them: the private
    key written, then the public write refused by a full disk or a vanished
    directory, leaving a signing key with no verifier. Nothing in the file says
    it is half a pair, so it looks usable, gets published, and every pack sealed
    with it is unverifiable. For an archival signing tool that is the worst
    outcome, worse than the command simply failing, so a failure on the second
    write rolls the first back.

    With ``--force`` the old pair may already be gone, because ``_write_key``
    removes an existing destination before creating the replacement, so rolling
    the new private key back can leave neither old nor new. That is the honest
    end state of an interrupted overwrite and it is still better than a lone new
    private key whose matching public key was never written: this keeps the
    invariant that the two halves ship together or not at all.
    """
    _write_key(private_path, private_pem, 0o600, force)
    try:
        _write_key(public_path, public_pem, 0o644, force)
    except BaseException:
        # Roll back the private half this call just wrote. Guarded so a failed
        # rollback cannot mask the original error a caller needs to see.
        try:
            private_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@app.command()
def keygen(
    private: str = typer.Option(..., "--private", help="path to write the PEM private key"),
    public: str = typer.Option(..., "--public", help="path to write the PEM public key"),
    force: bool = typer.Option(
        False, "--force", help="overwrite existing key files (destroys the old keypair)"
    ),
) -> None:
    """Generate an Ed25519 keypair for sealing packs."""
    private_path, public_path = Path(private), Path(public)
    try:
        # Both destinations first, then either write. The two files are halves of
        # one object and a half-written keypair is worse than no keypair at all.
        # `_write_key` keeps its own O_EXCL refusal underneath this: the check
        # here decides whether to start, and O_EXCL closes the race between
        # deciding and writing.
        #
        # The single-destination check runs whatever `force` says, because
        # `--force` is permission to replace an old keypair, never permission to
        # destroy the new private key with the public one a line later.
        _refuse_one_destination_for_both_keys(private_path, public_path)
        _refuse_occupied_key_paths([private_path, public_path], force)
        private_pem, public_pem = attest.generate_keypair()
        _write_keypair(private_path, private_pem, public_path, public_pem, force)
    except RevpackError as exc:
        _fail(exc)
    typer.secho(f"wrote {private} and {public}", err=True)


@app.command("collect")
def collect_cmd(
    evidence: list[str] = typer.Option(
        ..., "--evidence", "-e", help="kind=path, repeatable (e.g. -e contract=approved.json)"
    ),
    policy: str = typer.Option(..., "--policy"),
    release: str = typer.Option(..., "--release"),
    traceability: str = typer.Option(..., "--traceability"),
    out: str = typer.Option(..., "--out", help="pack directory to create"),
    collected_at: Optional[str] = typer.Option(
        None, "--collected-at", help="pin the collection timestamp (for reproducible packs)"
    ),
    record_vcs: bool = typer.Option(True, "--vcs/--no-vcs", help="record producing-tree git state"),
) -> None:
    """Copy evidence and inputs into a pack and index them."""
    try:
        pack = collect(
            _parse_evidence(evidence), policy, release, traceability, out, collected_at, record_vcs
        )
    except RevpackError as exc:
        _fail(exc)
    typer.secho(f"collected into {pack}", err=True)


@app.command("decide")
def decide_cmd(pack: str = typer.Option(..., "--pack")) -> None:
    """Evaluate the policy and write decision.json."""
    try:
        decision = decide(pack)
    except RevpackError as exc:
        _fail(exc)
    _report(decision)
    raise typer.Exit(EXIT_BY_STATE[decision.state])


@app.command("build")
def build_cmd(
    evidence: list[str] = typer.Option(..., "--evidence", "-e"),
    policy: str = typer.Option(..., "--policy"),
    release: str = typer.Option(..., "--release"),
    traceability: str = typer.Option(..., "--traceability"),
    out: str = typer.Option(..., "--out"),
    collected_at: Optional[str] = typer.Option(None, "--collected-at"),
    # Threaded through rather than hard-wired. `build` is `collect` plus
    # `decide`, so an option that changes what `collect` records has to exist on
    # both or the convenience command quietly cannot produce the same pack the
    # long form can. Recording VCS state reaches out to the producing tree,
    # which is exactly what a reproducibility run needs to be able to switch
    # off.
    record_vcs: bool = typer.Option(True, "--vcs/--no-vcs", help="record producing-tree git state"),
) -> None:
    """Collect then decide. Does not seal: signing needs a human and a key."""
    try:
        collect(
            _parse_evidence(evidence), policy, release, traceability, out, collected_at, record_vcs
        )
        decision = decide(out)
    except RevpackError as exc:
        _fail(exc)
    _report(decision)
    raise typer.Exit(EXIT_BY_STATE[decision.state])


@app.command("seal")
def seal_cmd(
    pack: str = typer.Option(..., "--pack"),
    key: str = typer.Option(..., "--key", help="Ed25519 private key, PEM"),
    pack_id: Optional[str] = typer.Option(None, "--pack-id"),
) -> None:
    """Build the manifest over the finished pack and sign it."""
    try:
        manifest = seal(pack, key, pack_id)
    except RevpackError as exc:
        _fail(exc)
    typer.secho(f"sealed {manifest.pack_id}: {len(manifest.entries)} file(s) indexed", err=True)


@app.command("verify")
def verify_cmd(
    pack: str = typer.Option(..., "--pack"),
    key: str = typer.Option(..., "--key", help="Ed25519 public key, PEM"),
) -> None:
    """Check closure, content, signature, and the verdict; exit on the verdict."""
    try:
        result = verify(pack, key)
    except RevpackError as exc:
        _fail(exc)
    for note in result.notes:
        typer.secho(f"ok: {note}", fg=typer.colors.GREEN, err=True)
    if not result.decision_rechecked:
        # Exit 2, not 0. The pack is intact and this is not an accusation, but
        # "the bytes are the ones that were signed" is a materially weaker
        # result than "and the verdict follows from them", and handing both the
        # same exit code means a gate written as `revpack verify && ship` cannot
        # tell them apart. Under this project's convention 2 is exactly the
        # right code: the tool could not decide.
        typer.secho(f"incomplete: {result.unrecheckable}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(2)
    # The verdict is reported through the same exit table `decide` uses. A pack
    # can be perfectly intact, perfectly signed, and carry a refusal: verifying
    # a sealed NO_GO establishes that the release must not ship, so exiting 0
    # because nothing was wrong with the pack would tell `revpack verify &&
    # ship` to ship it. Recomputation confirmed the refusal; it did not lift it.
    #
    # A pack that fails any integrity check never reaches this line: those raise
    # and leave through `_fail`, whose exit code is always 2, so a tamper or a
    # forgery can never be mistaken for the honest NO_GO of a pack that verified.
    typer.secho(
        f"verified verdict: {result.state}",
        fg=COLOUR_BY_STATE[result.state],
        bold=True,
        err=True,
    )
    raise typer.Exit(EXIT_BY_STATE[result.state])


def main() -> None:
    try:
        app()
    except RevpackError as exc:  # pragma: no cover - defensive
        _fail(exc)
        sys.exit(exc.exit_code)
    except Exception as exc:
        # An unhandled exception exits 1, and 1 is this tool's code for a NO_GO
        # the evidence proves. A crash reported as a proven quality failure is
        # the worst available answer: it tells a release gate the opposite of
        # the truth, and it is indistinguishable from a real refusal in a log.
        # Exit 2 says what actually happened, which is that the tool could not
        # run. The traceback is still printed, because swallowing it would trade
        # one debugging problem for another.
        #
        # `typer.Exit` and `SystemExit` derive from BaseException and are not
        # caught here, so a command's own exit code still reaches the caller.
        traceback.print_exc()
        typer.secho(
            f"error: unexpected failure: {type(exc).__name__}: {exc}",
            fg=typer.colors.RED,
            err=True,
        )
        sys.exit(2)


if __name__ == "__main__":  # pragma: no cover
    main()
