"""Error types and the exit-code convention.

The convention matches the rest of the workspace (DiscoverySpec, StateDiff,
MCP Contract Canary):

- 0: the release is cleared (GO)
- 1: a quality failure the evidence proves (NO_GO)
- 2: the tool could not decide, or could not run at all (INCOMPLETE or
  infrastructure failure)

INCOMPLETE and infrastructure failure deliberately share exit 2. A caller that
needs to tell them apart reads ``decision.json``; a caller that only needs to
know whether to ship treats "cannot decide" and "cannot run" identically,
because neither is permission to release.
"""

from __future__ import annotations


class RevpackError(Exception):
    """Base class. ``exit_code`` is always 2: something could not be done."""

    exit_code = 2

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or []


class InputError(RevpackError):
    """An input file is missing, unreadable, or not the format it claims."""


class PolicyError(RevpackError):
    """The policy is invalid, including when it tries to weaken the floor."""


class PackError(RevpackError):
    """The pack is structurally wrong, or the filesystem refused it.

    Closure, chain, and stage ordering faults, and also the reads and writes
    that build one. A pack that cannot be written is an infrastructure failure
    and belongs on exit 2; letting the OSError escape would exit 1, which this
    convention reserves for a NO_GO the evidence proves.
    """


class AttestationError(RevpackError):
    """A signing or verification operation failed."""
