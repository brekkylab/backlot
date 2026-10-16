"""The one error the comparison raises, in a module a prober can import without a cycle."""

from __future__ import annotations


class FidelityError(RuntimeError):
    """A vendor's contract could not be read, so there is nothing to compare against."""


class CredentialsMissing(FidelityError):
    """A credential a comparison declares is not set anywhere, or is set to one it cannot use.

    Its own type because it is not a vendor problem: answering it like an outage leaves the three
    sources that declare a credential silently uncompared, night after night, with the run green.
    """
