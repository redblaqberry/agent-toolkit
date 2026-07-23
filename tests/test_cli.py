"""The command line surface, which is where the exit-code contract is kept.

Exit codes are the whole interface for a caller that automates a release: 0 GO,
1 a NO_GO the evidence proves, 2 could not decide or could not run. Anything
that produces the wrong one of those three is worse than a crash, because a
crash is visible and a wrong exit code is a green pipeline.

Two of the defects covered here were exactly that shape. `build` had no
`--no-vcs` option while a CI job passed one, so it exited 2, and the job that
wrapped it in `if build ...; then fail; fi` read that as a correctly refused
release and passed green while testing nothing.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import (
    CLEAN,
    CLEAN_EVIDENCE,
    DEFECT_EVIDENCE,
    STAMP,
    build_pack,
    read_json,
    sealed_pack,
    write_json,
)
from revpack.cli import app
from revpack.pack import seal

runner = CliRunner()


def build_args(out: Path, extra: list[str] | None = None) -> list[str]:
    args = ["build"]
    for kind, path in CLEAN_EVIDENCE.items():
        args += ["-e", f"{kind}={path}"]
    args += [
        "--policy", str(CLEAN / "policy.yaml"),
        "--release", str(CLEAN / "release.yaml"),
        "--traceability", str(CLEAN / "traceability.yaml"),
        "--out", str(out),
        "--collected-at", STAMP,
    ]
    return args + (extra or [])


# ---------------------------------------------------------------------------
# option parity between build and collect
# ---------------------------------------------------------------------------


def test_build_accepts_no_vcs(tmp_path):
    # `build` is `collect` plus `decide`, so an option that changes what
    # `collect` records has to exist on both. It did not, and the exit code for
    # an unknown option is 2, the same code a genuinely undecidable release
    # produces.
    result = runner.invoke(app, build_args(tmp_path / "pack", ["--no-vcs"]))
    assert result.exit_code == 0, result.output


def test_build_no_vcs_records_no_git_state(tmp_path):
    runner.invoke(app, build_args(tmp_path / "pack", ["--no-vcs"]))
    envelopes = read_json(tmp_path / "pack" / "envelopes.json")["envelopes"]
    assert envelopes
    # Not merely accepted: actually threaded through to collect. An option that
    # parses and changes nothing is worse than a missing one.
    assert all(envelope.get("vcs") is None for envelope in envelopes)


def test_build_threads_the_vcs_choice_into_collect(tmp_path, monkeypatch):
    # Accepted is not the same as honoured. An option that parses and changes
    # nothing is worse than a missing one: the missing one at least fails
    # loudly. This also pins the default, so adding the switch cannot quietly
    # turn provenance recording off for everyone who does not pass it.
    from revpack import cli
    from revpack.errors import PackError

    seen = []

    def spy(*args):
        seen.append(args[-1])
        raise PackError("stopped after recording the flag")

    monkeypatch.setattr(cli, "collect", spy)
    runner.invoke(app, build_args(tmp_path / "a", ["--no-vcs"]))
    runner.invoke(app, build_args(tmp_path / "b"))
    assert seen == [False, True]


def test_a_defective_release_exits_exactly_1(tmp_path):
    args = ["build"]
    for kind, path in DEFECT_EVIDENCE.items():
        args += ["-e", f"{kind}={path}"]
    args += [
        "--policy", str(CLEAN / "policy.yaml"),
        "--release", str(CLEAN / "release.yaml"),
        "--traceability", str(CLEAN / "traceability.yaml"),
        "--out", str(tmp_path / "pack"),
        "--collected-at", STAMP,
        "--no-vcs",
    ]
    result = runner.invoke(app, args)
    # Exactly 1, never merely nonzero: 2 means the tool could not run, and a
    # gate that treats any failure as a proven refusal proves nothing.
    assert result.exit_code == 1
    assert read_json(tmp_path / "pack" / "decision.json")["state"] == "NO_GO"


# ---------------------------------------------------------------------------
# verify reports the verdict it verified, not merely that it verified something
# ---------------------------------------------------------------------------


def verify_exit(pack, public) -> int:
    return runner.invoke(app, ["verify", "--pack", str(pack), "--key", str(public)]).exit_code


def test_verify_exits_0_on_a_sealed_go(tmp_path, keys):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    assert read_json(pack / "decision.json")["state"] == "GO"
    assert verify_exit(pack, public) == 0


def test_verify_exits_1_on_a_sealed_no_go(tmp_path, keys):
    """The strongest check in the tool used to end in `exit 0`.

    `verify` returned success for every pack it could read, so recomputing a
    sealed NO_GO printed "decision NO_GO recomputed and matches" and exited 0.
    A gate written as `revpack verify && ship` then shipped a release the
    evidence refused, and the more thoroughly the pack verified, the more
    confidently it shipped. Recomputation confirms a refusal; it does not lift
    one, and the exit code has to say which of the two happened.
    """
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private, evidence=DEFECT_EVIDENCE)
    assert read_json(pack / "decision.json")["state"] == "NO_GO"
    assert verify_exit(pack, public) == 1


def test_verify_exits_2_on_a_sealed_incomplete(tmp_path, keys):
    private, public = keys
    evidence = dict(CLEAN_EVIDENCE)
    del evidence["state-verdict"]
    pack = sealed_pack(tmp_path / "pack", private, evidence=evidence)
    assert read_json(pack / "decision.json")["state"] == "INCOMPLETE"
    assert verify_exit(pack, public) == 2


def test_verify_exits_2_when_the_verdict_could_not_be_rechecked(tmp_path, keys, monkeypatch):
    # Integrity established, verdict established by nobody. Still not a pass.
    import revpack.pack as pack_module
    from revpack.models import SEMANTICS_VERSION

    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    monkeypatch.setattr(pack_module, "SEMANTICS_VERSION", SEMANTICS_VERSION + 1)
    assert verify_exit(pack, public) == 2


def test_a_tampered_pack_exits_2_and_is_never_mistaken_for_a_refusal(tmp_path, keys):
    """2, not 1. Both are non-zero and they mean opposite things.

    A NO_GO is a verdict the evidence supports and the pack can defend in an
    audit. A tampered pack supports no verdict at all. Handing it the refusal
    code would credit it with a decision nothing established, and would read in
    a log as a release that was correctly refused rather than as a pack whose
    contents cannot be trusted. The edit here would recompute to NO_GO if it
    were ever evaluated, which is exactly why the codes must differ.
    """
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    target = pack / "evidence" / "state-verdict" / "SD-PAY-01.json"
    payload = read_json(target)
    payload["status"] = "fail"
    write_json(target, payload)
    assert verify_exit(pack, public) == 2


def test_a_forged_decision_exits_2_rather_than_either_verdict(tmp_path, keys):
    # Not 0 (the verdict the pack claims) and not 1 (the verdict the evidence
    # yields): the pack proved neither, so the honest code is the one that says
    # nothing could be established.
    private, public = keys
    pack = tmp_path / "pack"
    build_pack(pack, evidence=DEFECT_EVIDENCE)
    payload = read_json(pack / "decision.json")
    assert payload["state"] == "NO_GO"
    payload["state"] = "GO"
    write_json(pack / "decision.json", payload)
    seal(pack, str(private))
    assert verify_exit(pack, public) == 2


# ---------------------------------------------------------------------------
# keygen
# ---------------------------------------------------------------------------


def test_keygen_refuses_to_overwrite_an_existing_key(tmp_path):
    # Rerunning keygen silently destroyed a keypair, and for an archival tool
    # that makes every previously sealed pack unverifiable.
    private, public = tmp_path / "sign.pem", tmp_path / "verify.pem"
    assert runner.invoke(app, ["keygen", "--private", str(private), "--public", str(public)]).exit_code == 0
    original = private.read_bytes()

    result = runner.invoke(app, ["keygen", "--private", str(private), "--public", str(public)])
    assert result.exit_code == 2
    assert private.read_bytes() == original


def test_keygen_force_overwrites_deliberately(tmp_path):
    private, public = tmp_path / "sign.pem", tmp_path / "verify.pem"
    runner.invoke(app, ["keygen", "--private", str(private), "--public", str(public)])
    original = private.read_bytes()

    result = runner.invoke(
        app, ["keygen", "--private", str(private), "--public", str(public), "--force"]
    )
    assert result.exit_code == 0
    assert private.read_bytes() != original


def test_keygen_refuses_when_only_the_public_key_exists(tmp_path):
    """A refusal must leave the filesystem exactly as it found it.

    The private key was written first and the public one refused afterwards, so
    the command reported an error and exited having left a brand new private key
    on disk beside an unrelated old public key. Nothing in either file says they
    do not belong together: every pack sealed under the old key still verifies
    against the old public key, and the next `seal` signs with a key that public
    key cannot check. Asserting the exit code alone did not notice, which is why
    the absence of the private file is asserted here.
    """
    private, public = tmp_path / "sign.pem", tmp_path / "verify.pem"
    public.write_bytes(b"placeholder")
    result = runner.invoke(app, ["keygen", "--private", str(private), "--public", str(public)])
    assert result.exit_code == 2
    assert public.read_bytes() == b"placeholder"
    assert not private.exists(), "a refused keygen left an orphan private key behind"


def test_keygen_refuses_when_only_the_private_key_exists(tmp_path):
    # The mirror case, so the pre-check cannot be satisfied by testing one path.
    private, public = tmp_path / "sign.pem", tmp_path / "verify.pem"
    private.write_bytes(b"placeholder")
    result = runner.invoke(app, ["keygen", "--private", str(private), "--public", str(public)])
    assert result.exit_code == 2
    assert private.read_bytes() == b"placeholder"
    assert not public.exists()


def test_keygen_refuses_one_destination_for_both_halves_of_a_keypair(tmp_path):
    """A keypair is two files, and only one of them survives a shared path.

    The private key was written first and the public one refused on top of it, so
    the command reported an error while leaving a signing key sitting at the path
    the operator meant to publish.
    """
    both = tmp_path / "key.pem"
    result = runner.invoke(app, ["keygen", "--private", str(both), "--public", str(both)])
    assert result.exit_code == 2
    assert not both.exists(), "a refused keygen left a key behind at the shared path"


def test_keygen_force_does_not_write_the_public_key_over_the_private_one(tmp_path):
    # The destructive half. `--force` is permission to replace an old keypair,
    # never permission to destroy the new private key with the public one a line
    # later, which is what the second write did: the surviving file was the
    # public key and the key every pack sealed afterwards needed was gone.
    both = tmp_path / "key.pem"
    both.write_bytes(b"placeholder")
    result = runner.invoke(
        app, ["keygen", "--private", str(both), "--public", str(both), "--force"]
    )
    assert result.exit_code == 2
    assert both.read_bytes() == b"placeholder"


def test_a_failed_public_key_write_leaves_no_orphan_private_key(tmp_path, monkeypatch):
    """A keypair is two files, and a failure between them must roll the first back.

    The private key was written and the public write then failed on a full disk
    or a vanished directory, leaving a signing key with no verifier. Nothing in
    the file says it is half a pair, so it looks usable, and for an archival
    signing tool that is the worst outcome: every pack sealed with it is
    unverifiable. Write both or neither.
    """
    from revpack import cli

    private, public = tmp_path / "sign.pem", tmp_path / "verify.pem"
    real_write = cli._write_key

    def flaky(path, pem, mode, force):
        if Path(path) == public:
            raise cli.InputError("no space left on device")
        return real_write(path, pem, mode, force)

    monkeypatch.setattr(cli, "_write_key", flaky)
    result = runner.invoke(app, ["keygen", "--private", str(private), "--public", str(public)])
    assert result.exit_code == 2
    assert not private.exists(), "a failed public-key write left an orphan private key behind"
    assert not public.exists()


def test_a_failed_public_key_write_under_force_leaves_no_new_private_key(tmp_path, monkeypatch):
    # With --force the old pair may already be gone, so a lone new private key is
    # the exact half-written keypair this refuses to leave. The new private half
    # is rolled back too.
    from revpack import cli

    private, public = tmp_path / "sign.pem", tmp_path / "verify.pem"
    private.write_bytes(b"old-private")
    public.write_bytes(b"old-public")
    real_write = cli._write_key

    def flaky(path, pem, mode, force):
        if Path(path) == public:
            raise cli.InputError("no space left on device")
        return real_write(path, pem, mode, force)

    monkeypatch.setattr(cli, "_write_key", flaky)
    result = runner.invoke(
        app, ["keygen", "--private", str(private), "--public", str(public), "--force"]
    )
    assert result.exit_code == 2
    # The old private key was removed by --force before the write, and the new
    # one is rolled back, so neither a stale nor a half-new private key survives.
    assert not private.exists(), "a failed keygen left a new private key with no matching public"


@pytest.mark.skipif(sys.platform == "win32", reason="mode bits are advisory on Windows")
def test_the_private_key_is_created_owner_only(tmp_path):
    # write_bytes creates with the process umask, which on a shared or CI host
    # routinely leaves the signing key readable by every account on the box.
    private, public = tmp_path / "sign.pem", tmp_path / "verify.pem"
    runner.invoke(app, ["keygen", "--private", str(private), "--public", str(public)])
    assert stat.S_IMODE(private.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# infrastructure failures are exit 2, never exit 1
# ---------------------------------------------------------------------------


def test_a_filesystem_failure_exits_2_not_1(tmp_path, monkeypatch):
    """Exit 1 is a proven NO_GO. A full disk is not one.

    An OSError escaping as an unhandled exception exits 1, which tells a release
    gate that the evidence proved a failure. It proved nothing; the tool never
    ran.
    """
    import revpack.pack as pack_module

    def refuse(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pack_module.shutil, "copyfile", refuse)
    result = runner.invoke(app, build_args(tmp_path / "pack", ["--no-vcs"]))
    assert result.exit_code == 2


def test_a_write_failure_during_decide_exits_2_not_1(tmp_path, monkeypatch):
    import revpack.canonical as canonical_module

    def refuse(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(canonical_module.tempfile, "NamedTemporaryFile", refuse)
    result = runner.invoke(app, build_args(tmp_path / "pack", ["--no-vcs"]))
    assert result.exit_code == 2


def test_an_unexpected_exception_exits_2_not_1(monkeypatch, capsys):
    """The catch-all in main().

    Python exits 1 on an unhandled exception, and 1 is this project's code for a
    NO_GO the evidence proves, so a bug would be reported as a proven release
    failure. The traceback still reaches stderr; only the exit code changes.
    """
    from revpack import cli

    def explode() -> None:
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(cli, "app", explode)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
    assert "something nobody anticipated" in capsys.readouterr().err


def test_a_command_exit_code_still_reaches_the_caller(monkeypatch):
    # typer.Exit and SystemExit derive from BaseException, so the catch-all
    # cannot swallow a command's own decision code and turn every NO_GO into a 2.
    from revpack import cli

    def exit_one() -> None:
        raise SystemExit(1)

    monkeypatch.setattr(cli, "app", exit_one)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
