"""The one error the comparison raises, in a module a prober can import without a cycle."""

from __future__ import annotations


class FidelityError(RuntimeError):
    """A vendor's contract could not be read, so there is nothing to compare against."""
