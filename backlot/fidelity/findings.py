"""What a comparison reports, and what a baseline remembers about it.

Every kind of comparison — a GraphQL schema walk, a published-spec path diff, a behavioural
probe — answers in these terms, so the vocabulary lives apart from any one of them. A finding says
what diverged and how much it matters; a baseline says which of them have already been read and
accepted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

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


@dataclass(frozen=True)
class Baseline:
    """Divergences already read and accepted, so a run reports only what is new.

    Without this the first run against Fireflies reports fourteen root fields Backlot never claimed
    to serve, and by the third run nobody reads the output. Acknowledging is a file change, which
    means it goes through review like any other.
    """

    source: str
    endpoint: str
    measured: str
    acknowledged: dict[str, Finding]

    @classmethod
    def empty(cls, source: str, endpoint: str = "") -> "Baseline":
        return cls(source=source, endpoint=endpoint, measured="", acknowledged={})

    @classmethod
    def load(cls, path: Path) -> "Baseline":
        raw = json.loads(path.read_text())
        ack = {}
        for e in raw.get("acknowledged", []):
            f = Finding(
                kind=e["kind"],
                severity=e.get("severity", GAP),
                path=e["path"],
                detail=e.get("detail", ""),
                note=e.get("note", ""),
            )
            ack[f.key] = f
        return cls(
            source=raw["source"],
            endpoint=raw.get("endpoint", ""),
            measured=raw.get("measured", ""),
            acknowledged=ack,
        )

    def write(self, path: Path, findings: Iterable[Finding], *, measured: str) -> None:
        """Rewrite the file so it acknowledges exactly ``findings``, keeping existing notes.

        A ``breaking`` entry is kept whole rather than refreshed. Its note was written about the
        detail beside it — ``vendor: Int, Backlot: Float`` — so refreshing the detail alone would
        leave prose explaining a divergence that is no longer the one recorded, and it would do it
        under a flag that is documented never to acknowledge a breaking finding.
        """
        kept = []
        for f in findings:
            known = self.acknowledged.get(f.key)
            if known is None:
                kept.append(f)
            else:
                kept.append(known if known.severity == BREAKING else replace(f, note=known.note))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "source": self.source,
                    "endpoint": self.endpoint,
                    "measured": measured,
                    "acknowledged": [f.as_dict() for f in kept],
                },
                indent=2,
            )
            + "\n"
        )

    def identified_as(self, source: str, endpoint: str) -> "Baseline":
        """The same acknowledgements, relabelled for the comparison actually being run.

        A loaded baseline reports whatever the file last said it was about. That is fine until a
        source is renamed or repointed, at which point the stale name would be written back
        forever, because the file is the only thing that ever set it.
        """
        return replace(self, source=source, endpoint=endpoint)

    def unacknowledged(self, findings: Iterable[Finding]) -> list[Finding]:
        """Findings this baseline does not already account for.

        A ``gap`` is accounted for by identity alone: the vendor has surface Backlot does not, and
        the vendor restating it at a new type does not change what was accepted. A ``breaking``
        finding is accounted for by identity AND detail, because there the detail IS the
        contradiction. An entry reading ``vendor: Int, Backlot: Float`` says nothing about a vendor
        that now serves ``String``, and going on silencing it is how the vendor changing something
        next March passes unread — which is the one outcome this whole command exists to prevent.
        """
        return [
            f
            for f in findings
            if (known := self.acknowledged.get(f.key)) is None
            or (f.severity == BREAKING and known.detail != f.detail)
        ]

    def resolved(self, findings: Iterable[Finding]) -> list[Finding]:
        """Acknowledged divergences the vendor no longer has — the baseline is now stale."""
        live = {f.key for f in findings}
        return [f for k, f in sorted(self.acknowledged.items()) if k not in live]
