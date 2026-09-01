"""Measuring what Backlot serves against what the vendor serves.

Fidelity is the point of this project, and until now it was a policy — measure the real service,
record the measurement beside the fix — carried out by hand, one pull request at a time. That
catches a divergence when someone goes looking. It does not catch the vendor changing something
next March.

This package is the same measurement, run on a schedule and checked in. Pick a source out of
``COMPARISONS``, call ``divergences`` on it, read the result as ``Finding`` objects graded ``BREAKING``
or ``GAP``, and acknowledge what you accept through ``Baseline`` — so a run reports only what is
new, and accepting one is a file change that goes through review. ``FidelityError`` separates the
one case that is not a finding at all: the vendor could not be asked.

That is the whole public surface. The submodules are not part of it: each exposes a ``divergences``
of its own that loads both sides for its kind of source, and re-exporting the steps in between — a
schema loader, a spec fetcher, a path walker — would invite a caller to assemble a comparison by
hand and get a different answer than the command does.
"""

from backlot.fidelity.errors import FidelityError
from backlot.fidelity.findings import BREAKING, GAP, Baseline, Finding
from backlot.fidelity.comparisons import COMPARISONS, baseline_path, divergences

__all__ = [
    "BREAKING",
    "GAP",
    "COMPARISONS",
    "Baseline",
    "FidelityError",
    "Finding",
    "baseline_path",
    "divergences",
]
