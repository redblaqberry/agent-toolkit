"""Release Evidence Pack: a signed, independently verifiable release dossier.

Reads completed artifacts from five independent evaluation tools, normalizes
their mutually incompatible verdicts, checks that they describe the same
release, evaluates a declarative policy, and seals the result so that anyone
holding the public key can recompute the decision from the evidence.

It never reruns an upstream test and never overrides a producer's verdict. It
does recompute values that producers decline to serialize, and it labels every
one of those as derived.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .errors import AttestationError, InputError, PackError, PolicyError, RevpackError
from .lattice import Status, reduce_statuses, worst
from .models import Decision, Envelope, Manifest, Policy, ReleaseDescriptor, TraceabilityMap
from .pack import collect, compute_decision, decide, seal, verify

__all__ = [
    "AttestationError",
    "Decision",
    "Envelope",
    "InputError",
    "Manifest",
    "PackError",
    "Policy",
    "PolicyError",
    "ReleaseDescriptor",
    "RevpackError",
    "Status",
    "TraceabilityMap",
    "__version__",
    "collect",
    "compute_decision",
    "decide",
    "reduce_statuses",
    "seal",
    "verify",
    "worst",
]
