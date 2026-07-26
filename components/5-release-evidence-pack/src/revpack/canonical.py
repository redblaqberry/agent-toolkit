"""Canonical encoding.

Two encodings live here and they are not interchangeable:

- ``canonical_bytes`` / ``digest`` operate on a *parsed object* and produce the
  compact, key-sorted form that every signature and content digest is taken
  over. This is byte-compatible with DiscoverySpec's ``attest.canonical_bytes``
  (sorted keys, ``(",", ":")`` separators, ``ensure_ascii=False``, UTF-8), so a
  document signed by either project verifies under the other. That
  compatibility is pinned by a test, not by hope.

- ``file_sha256`` operates on *raw bytes on disk*. Manifest entries use it, so
  tamper detection never depends on a parser round-tripping a document the same
  way it was written. A file that fails to parse still has a stable hash.

``dump_json`` is the on-disk writer: sorted keys and two-space indent so a
human can read a pack and a diff is meaningful. Because digests are taken over
the parsed object rather than the file text, changing the indent would not
silently change any signature.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import PackError


def canonical_bytes(payload: Any) -> bytes:
    """Deterministic encoding of a parsed object: sorted keys, no whitespace."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest(payload: Any) -> str:
    """SHA-256 hex of the canonical encoding of ``payload``."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def file_sha256(path: str | Path) -> str:
    """SHA-256 hex of a file's raw bytes, read in chunks."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def bytes_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def dump_json(payload: Any) -> str:
    """On-disk JSON: sorted keys, indent 2, trailing newline."""
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_text_atomic(path: str | Path, text: str) -> None:
    """Write via a temp sibling then replace, with LF newlines on every platform.

    Newlines are pinned because manifest entries hash raw bytes: a file written
    with CRLF on Windows and LF elsewhere would produce two different hashes for
    the same logical content, and a pack built on one machine would fail
    verification on another.

    A full disk, a read-only mount, or a vanished directory raises ``PackError``
    rather than a bare ``OSError``. An escaping OSError reaches the CLI as an
    unhandled exception and exits 1, and 1 is the code this project reserves for
    a NO_GO the evidence proves, so a disk-full error would be read as a proven
    release failure.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        )
    except OSError as exc:
        raise PackError(f"cannot write {path}: {exc}") from exc
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except OSError as exc:
        Path(handle.name).unlink(missing_ok=True)
        raise PackError(f"cannot write {path}: {exc}") from exc
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def inputs_digest(entries: list[tuple[str, str]]) -> str:
    """Digest over a derived artifact's inputs.

    ``entries`` is a list of ``(path, sha256)`` pairs. Sorting makes the result
    independent of collection order, so the same inputs always yield the same
    digest. This is what makes a stale derived artifact detectable: its recorded
    inputs digest stops matching a recomputation over the pack it sits in.
    """
    return digest([[path, sha] for path, sha in sorted(entries)])
