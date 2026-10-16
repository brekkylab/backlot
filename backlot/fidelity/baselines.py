"""What a baseline remembers: the surface a source was last measured as, and which findings about
it have already been read and accepted. How the file spells both is ``backlot.fidelity.surface``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from backlot.fidelity.findings import BREAKING, Finding
from backlot.fidelity.surface import OperationEntry, Surface, surface_from_json


def _reject_one_url(value) -> None:
    """A str is iterable, so one URL passed unwrapped spreads: `list("https://…")` writes one
    list element per character, which loads back the same way with nothing downstream shaped
    wrongly enough to complain."""
    if isinstance(value, str):
        raise TypeError(f"endpoints is a tuple of URLs, not one URL: pass ({value!r},)")


def _endpoints(raw) -> tuple[str, ...]:
    """What the file said, as a tuple — refusing a bare string BEFORE converting it, because
    `tuple("https://…")` is that per-character spread rather than the one URL it looks like."""
    _reject_one_url(raw)
    return tuple(raw)


@dataclass(frozen=True)
class Baseline:
    """The surface a source was last measured as, and the divergences already read and accepted
    on it, so a run reports only what is new.

    Without this the first run against Fireflies reports fourteen root fields Backlot never claimed
    to serve, and by the third run nobody reads the output. Acknowledging is a file change, which
    means it goes through review like any other.
    """

    source: str
    endpoints: tuple[str, ...]
    measured: str
    acknowledged: dict[str, Finding]
    # What the file was measured against, which the acknowledged findings are read off. None for
    # a source no run has written yet.
    surface: Surface | None = None

    def __post_init__(self) -> None:
        # Covers `empty` and `identified_as`, which construct from what a caller passed. `load`
        # converts before constructing, so it checks the raw value itself — see `_endpoints`.
        _reject_one_url(self.endpoints)

    @classmethod
    def empty(cls, source: str, endpoints: tuple[str, ...] = ()) -> "Baseline":
        return cls(source=source, endpoints=endpoints, measured="", acknowledged={})

    @property
    def operations(self) -> tuple[OperationEntry, ...] | None:
        """Every operation the vendor names and every one Backlot serves, offline — the surface's
        operations, or a GraphQL schema's root query fields."""
        return None if self.surface is None else self.surface.operations

    @classmethod
    def load(cls, path: Path) -> "Baseline":
        raw = json.loads(path.read_text(encoding="utf-8"))
        surface, findings = surface_from_json(raw)
        if "acknowledged" in raw:
            # The list is never read, alone or beside a surface, so the next `--update-baseline`
            # would rewrite the file without its notes and print nothing about them.
            raise ValueError(
                f"{path} lists its accepted findings under `acknowledged` rather than on the "
                f"elements they name, so none of them would be read: rerun `backlot diff -s "
                f"{raw.get('source', '<source>')} --update-baseline` against a copy in the surface "
                "format, then move each note onto the element it names"
            )
        return cls(
            source=raw["source"],
            endpoints=_endpoints(raw.get("endpoints", ())),
            measured=raw.get("measured", ""),
            acknowledged={f.key: f for f in findings},
            surface=surface,
        )

    def write(
        self, path: Path, findings: Iterable[Finding], *, measured: str, surface: Surface
    ) -> None:
        """Rewrite the file as ``surface``, acknowledging exactly ``findings`` on the elements they
        name and keeping existing notes.

        An entry for a ``breaking`` finding is kept whole rather than refreshed. Its note was
        written about the detail beside it — ``vendor: Int, Backlot: Float`` — so refreshing the
        detail alone would leave prose explaining a divergence that is no longer the one recorded,
        and it would do it under a flag that is documented never to acknowledge a breaking finding.
        Which findings are breaking is the measurement's to say, not the file's.
        """
        kept = []
        for f in findings:
            known = self.acknowledged.get(f.key)
            if known is None:
                kept.append(f)
            else:
                kept.append(
                    replace(known, severity=BREAKING)
                    if f.severity == BREAKING
                    else replace(f, note=known.note)
                )
        doc = {
            "source": self.source,
            "endpoints": list(self.endpoints),
            "measured": measured,
            **surface.to_json(kept),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")

    def identified_as(self, source: str, endpoints: tuple[str, ...]) -> "Baseline":
        """The same acknowledgements, relabelled for the comparison actually being run.

        A loaded baseline reports whatever the file last said it was about. That is fine until a
        source is renamed or repointed, at which point the stale name would be written back
        forever, because the file is the only thing that ever set it.
        """
        return replace(self, source=source, endpoints=endpoints)

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
