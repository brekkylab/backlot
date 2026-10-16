"""What a comparison reports.

Every kind of comparison — a GraphQL schema walk, a published-spec path diff, a behavioural
probe — answers in these terms, so the vocabulary lives apart from any one of them. A finding says
what diverged and how much it matters; ``backlot.fidelity.surface`` says where in the contract it
sits, and ``backlot.fidelity.baselines`` which findings have already been read and accepted.
"""

from __future__ import annotations

from dataclasses import dataclass

BREAKING, GAP = "breaking", "gap"


@dataclass(frozen=True)
class Finding:
    """One divergence, identified by ``key`` so a baseline can acknowledge it across runs."""

    kind: str
    severity: str
    path: str
    detail: str
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.path}"

    def as_dict(self) -> dict[str, str]:
        d = {"kind": self.kind, "severity": self.severity, "path": self.path, "detail": self.detail}
        if self.note:
            d["note"] = self.note
        return d
