"""Google Docs' body, as the importer, the store and the Docs routes all hold it.

Outside the router for the reason `gsheets` is: the importer is the other caller.

A body is stored as real serves `body.content` with every `startIndex` and `endIndex` left out, and
:func:`indexed` puts them back at read time, so the stored form is the vendor's own. A document
that states no structure is stored as nothing and read through :func:`from_text`, until its first
write stores that reading (:class:`Body`).

Measured against the live Docs v1 API on 2026-10-26, as a Workspace member on documents it owns and
as a second account on the Docs it can read.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import unicodedata
from pathlib import Path

from backlot import synth

# `documentStyle`, `namedStyles` and the first section break's `sectionStyle` of a document
# `documents.create` makes for a Workspace member whose account is in English. Of 43 Docs read,
# 42 had these `namedStyles`, and 37 differed from this `documentStyle` in an A4 `pageSize` alone.
_DEFAULTS = json.loads(
    (Path(__file__).parent / "data" / "docs_defaults.json").read_text(encoding="utf-8")
)

# `TextStyle`'s members in the order real serves them, in `namedStyles` and in every run read.
_TEXT_STYLE_ORDER = (
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "smallCaps",
    "backgroundColor",
    "foregroundColor",
    "fontSize",
    "weightedFontFamily",
    "baselineOffset",
    "link",
)

# The characters `insertText` strips from what it inserts: U+0000-U+0008, U+000C-U+001F (`\r`
# among them) and U+E000-U+F8FF. A tab, `\x0b`, `\x7f`, U+2028 and a character past the BMP stay.
STRIPPED = re.compile("[\x00-\x08\x0c-\x1f\ue000-\uf8ff]")

# The characters `insertText` refuses with a 500, U+FDD0-U+FDEF, keeping U+FDCF and U+FDF0 beside
# them; a `.txt` holding one fails its conversion to a Doc as well, and Drive keeps an unconverted
# `.txt` holding U+FDD0 and U+FDEF byte for byte.
UNWRITABLE = re.compile("[\ufdd0-\ufdef]")

# The colour a link gives its run, through the editor or `updateTextStyle` alike.
_LINK_COLOR = {"color": {"rgbColor": {"red": 0.06666667, "green": 0.33333334, "blue": 0.8}}}


def document_style() -> dict:
    return copy.deepcopy(_DEFAULTS["documentStyle"])


def named_styles() -> dict:
    return copy.deepcopy(_DEFAULTS["namedStyles"])


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, the unit every index counts in: a character outside the BMP
    is two."""
    return len(text.encode("utf-16-le")) // 2


def text_style(marks: dict) -> dict:
    """A corpus run's marks as the `TextStyle` real serves for them. A false mark is no mark."""
    style = {k: True for k in ("bold", "italic", "underline", "strikethrough") if marks.get(k)}
    if marks.get("link"):
        style["underline"] = True
        style["foregroundColor"] = copy.deepcopy(_LINK_COLOR)
        style["link"] = {"url": marks["link"]}
    return {k: style[k] for k in _TEXT_STYLE_ORDER if k in style}


def _newline_style(marks: dict) -> dict:
    """The style of a paragraph's newline, which takes its last run's: a run styled up to the end
    of its paragraph styles the newline too, except for the link, which stops before it along with
    the underline and colour it brought. An underline the run states itself stays."""
    return text_style({k: v for k, v in marks.items() if k != "link"})


def heading_id(file_id: str, ordinal: int | str) -> str:
    """A heading paragraph's `headingId`: ``h.`` and twelve base-36 characters, the length 262 of
    281 measured ids have (the rest are one or two shorter). Real gave one to every TITLE,
    SUBTITLE and HEADING_n paragraph in all but one Doc read, and none to NORMAL_TEXT."""
    n = synth.hnum(f"{file_id}\0{ordinal}", length=16)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    for _ in range(12):
        n, r = divmod(n, 36)
        out += digits[r]
    return "h." + out


def _paragraph(runs: list[tuple[str, dict]], newline: dict, style: str, hid: str | None) -> dict:
    """One `paragraph` structural element, adjacent runs of one style merged as real merges them
    and the newline joined to the last run when its style matches."""
    elements: list[dict] = []
    for text, ts in [*runs, ("\n", newline)]:
        if not text:
            continue
        if elements and elements[-1]["textRun"]["textStyle"] == ts:
            elements[-1]["textRun"]["content"] += text
        else:
            elements.append({"textRun": {"content": text, "textStyle": ts}})
    pstyle = {"headingId": hid} if hid else {}
    pstyle |= {"namedStyleType": style, "direction": "LEFT_TO_RIGHT"}
    return {"paragraph": {"elements": elements, "paragraphStyle": pstyle}}


def _section_break() -> dict:
    return {"sectionBreak": {"sectionStyle": copy.deepcopy(_DEFAULTS["sectionStyle"])}}


def from_paragraphs(file_id: str, paragraphs: list[dict]) -> list[dict]:
    """A corpus's `paragraphs` as a stored body: the section break every body opens with, then one
    `paragraph` per entry. A heading's `headingId` is drawn here, once."""
    content = [_section_break()]
    for i, p in enumerate(paragraphs):
        style = p.get("style") or "NORMAL_TEXT"
        stated = p["runs"] if "runs" in p else [{"text": p.get("text", "")}]
        runs = [(r["text"], text_style(r)) for r in stated]
        newline = _newline_style(stated[-1])
        hid = heading_id(file_id, i) if style != "NORMAL_TEXT" else None
        content.append(_paragraph(runs, newline, style, hid))
    return content


def from_text(text: str) -> list[dict]:
    """A document that states no structure: one unstyled paragraph per line of its text, read as
    `insertText` would write it, so without the characters it strips."""
    return [_section_break()] + [
        _paragraph([(STRIPPED.sub("", line), {})], {}, "NORMAL_TEXT", None)
        for line in text.split("\n")
    ]


def text_of(paragraphs: list[dict]) -> str:
    """A corpus's `paragraphs` as the text Drive's search and export read: one line each."""
    return "\n".join(
        "".join(r["text"] for r in p["runs"]) if "runs" in p else p.get("text", "")
        for p in paragraphs
    )


def indexed(content: list[dict]) -> list[dict]:
    """A stored body as real serves it, each structural element and paragraph element carrying
    its `startIndex` and `endIndex` in UTF-16 code units, those two first. The body opens at 0,
    and a zero `startIndex` is left out, as proto3 leaves out every zero."""
    out = []
    at = 0
    for el in content:
        if "sectionBreak" in el:
            out.append(_span(at, at + 1) | el)
            at += 1
            continue
        start = at
        elements = []
        for pe in el["paragraph"]["elements"]:
            end = at + utf16_len(pe["textRun"]["content"])
            elements.append(_span(at, end) | pe)
            at = end
        paragraph = {
            "elements": elements,
            **{k: v for k, v in el["paragraph"].items() if k != "elements"},
        }
        out.append(_span(start, at) | {"paragraph": paragraph})
    return out


def _span(start: int, end: int) -> dict:
    return ({"startIndex": start} if start else {}) | {"endIndex": end}


# --- a body opened for a write --------------------------------------------------------------------
#
# Measured against the live Docs v1 API on 2026-10-29, as a Workspace member writing a Doc it owns,
# through `documents.batchUpdate`'s `insertText`, `deleteContentRange` and `replaceAllText`.

# The characters a grapheme cluster continues with, beyond a combining mark: the zero-width joiner
# and the five emoji skin tones (Extend, in Unicode's text segmentation).
_ZWJ = chr(0x200D)
_SKIN_TONES = ("\U0001f3fb", "\U0001f3ff")
_REGIONAL = ("\U0001f1e6", "\U0001f1ff")
# The Hangul jamo pairs one syllable joins, by text segmentation's L, V, T, LV and LVT.
_HANGUL_JOINS = frozenset(
    {
        ("L", "L"),
        ("L", "V"),
        ("L", "LV"),
        ("L", "LVT"),
        ("LV", "V"),
        ("LV", "T"),
        ("V", "V"),
        ("V", "T"),
        ("LVT", "T"),
        ("T", "T"),
    }
)


def _hangul(ch: str) -> str | None:
    cp = ord(ch)
    if 0x1100 <= cp <= 0x115F or 0xA960 <= cp <= 0xA97C:
        return "L"
    if 0x1160 <= cp <= 0x11A7 or 0xD7B0 <= cp <= 0xD7C6:
        return "V"
    if 0x11A8 <= cp <= 0x11FF or 0xD7CB <= cp <= 0xD7FB:
        return "T"
    if 0xAC00 <= cp <= 0xD7A3:
        return "LV" if (cp - 0xAC00) % 28 == 0 else "LVT"
    return None


def _pictographic(ch: str) -> bool:
    """Whether a character is an emoji a zero-width joiner joins to the one before it: the
    pictographic blocks, without the full Extended_Pictographic table."""
    cp = ord(ch)
    return 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2300 <= cp <= 0x23FF


def _unlinked(style: dict) -> dict:
    """A run's style with its link taken off, along with the underline and colour a link brings:
    what text written next to a link, and not inside it, is given."""
    return {
        k: v
        for k, v in style.items()
        if k not in ("link", "underline") and not (k == "foregroundColor" and v == _LINK_COLOR)
    }


def _heading(style: dict) -> bool:
    return style.get("namedStyleType", "NORMAL_TEXT") != "NORMAL_TEXT"


class Body:
    """A document's body opened for a write: each character with the `TextStyle` of the run it sits
    in and that run, and each paragraph's `paragraphStyle`, every paragraph ending at a newline. A
    write changes it in memory, and :meth:`content` is what is stored back.

    A run is kept apart from its neighbour of the same style until a delete brings the two
    together: measured, text inserted at a link's end, unstyled, stayed a run of its own before the
    unstyled text after the link, and so did italic text inserted at the start of an italic run,
    while deleting a bold run joined the unstyled runs either side of it.

    Positions here count characters; :meth:`position` turns a request's index, in UTF-16 code
    units, into one. The checks a request's refusal names are the router's
    (`routers.google_docs`), which asks :meth:`position` and :meth:`splits_cluster`."""

    def __init__(self, file_id: str, content: list[dict], revision: int):
        self.file_id = file_id
        self.revision = revision
        self._section = content[0]
        self._runs = itertools.count()
        self.chars: list[tuple[str, dict, int]] = []
        self.paragraphs: list[dict] = []
        for el in content[1:]:
            for pe in el["paragraph"]["elements"]:
                run, n = pe["textRun"], next(self._runs)
                self.chars += [(c, run.get("textStyle", {}), n) for c in run["content"]]
            self.paragraphs.append(el["paragraph"].get("paragraphStyle", {}))
        self._split = 0

    @property
    def end(self) -> int:
        """The body's end index: one past its last newline."""
        return 1 + sum(utf16_len(c) for c, *_ in self.chars)

    def position(self, index: int) -> int | None:
        """The character a body index falls before, or None when it falls between the two halves
        of a surrogate pair. ``index`` is at least 1 and at most :attr:`end`."""
        at = 1
        for pos, (c, *_) in enumerate(self.chars):
            if at == index:
                return pos
            if at > index:
                return None
            at += utf16_len(c)
        return len(self.chars) if at == index else None

    def splits_cluster(self, pos: int) -> bool:
        """Whether an insert before the character at ``pos`` would land inside a grapheme cluster:
        measured, between a letter and U+0301, an emoji and a skin tone, two regional indicators of
        a flag, a Hangul leading and vowel jamo, a heart and U+FE0F, and a man and the zero-width
        joiner before a woman in a family emoji. Inserting U+0301 after a letter composes a cluster
        and was taken."""
        if pos == 0 or pos >= len(self.chars):
            return False
        prev, nxt = self.chars[pos - 1][0], self.chars[pos][0]
        if prev == "\n":
            return False
        if unicodedata.category(nxt) in ("Mn", "Mc", "Me") or nxt == _ZWJ:
            return True
        if _SKIN_TONES[0] <= nxt <= _SKIN_TONES[1]:
            return True
        if prev == _ZWJ and _pictographic(nxt):
            return True
        if _REGIONAL[0] <= prev <= _REGIONAL[1] and _REGIONAL[0] <= nxt <= _REGIONAL[1]:
            run = 0
            while (
                pos - 1 - run >= 0 and _REGIONAL[0] <= self.chars[pos - 1 - run][0] <= _REGIONAL[1]
            ):
                run += 1
            return run % 2 == 1
        return (_hangul(prev), _hangul(nxt)) in _HANGUL_JOINS

    def _paragraph_of(self, pos: int) -> int:
        return sum(1 for c, *_ in self.chars[:pos] if c == "\n")

    def _starts_paragraph(self, pos: int) -> bool:
        return pos == 0 or self.chars[pos - 1][0] == "\n"

    def _style_at(self, pos: int) -> tuple[dict, bool]:
        """The style text inserted before ``pos`` takes, and whether a link was taken off it: the
        character before it's, or at the start of a paragraph the first character's, the newline's
        in an empty one; a link only between two characters of that link. Measured on inserts at
        both ends of and inside a bold run and a link, at the start of an italic paragraph after a
        struck-through newline, and into an empty paragraph."""
        if self._starts_paragraph(pos):
            style, inside = self.chars[pos][1], False
        else:
            style = self.chars[pos - 1][1]
            inside = self.chars[pos][1].get("link") == style.get("link")
        if inside or "link" not in style:
            return style, False
        return _unlinked(style), True

    def _run_at(self, pos: int, style: dict) -> int:
        """The run text in ``style`` inserted before ``pos`` joins: the one before it in the same
        paragraph when that is its style; at a paragraph's start the one after it, when the newline
        before is in that style too, measured on inserts at the start of a heading, into an empty
        paragraph and at the start of an italic paragraph after a struck-through newline, which
        alone stayed a run of its own; and a run of its own otherwise."""
        if not self._starts_paragraph(pos):
            before = self.chars[pos - 1]
            return before[2] if before[1] == style else next(self._runs)
        after = self.chars[pos]
        if after[1] == style and (pos == 0 or self.chars[pos - 1][1] == style):
            return after[2]
        return next(self._runs)

    def _split_style(self, style: dict) -> dict:
        """The `paragraphStyle` of a paragraph an inserted newline makes: the one it was cut from,
        a heading with a `headingId` of its own."""
        out = {k: v for k, v in style.items() if k != "headingId"}
        if not _heading(style):
            return out
        self._split += 1
        return {"headingId": heading_id(self.file_id, f"r{self.revision}.{self._split}")} | out

    def insert(self, pos: int, text: str, style: dict | None = None) -> None:
        """``text`` before the character at ``pos``, every character of it in one style (`_style_at`
        unless given). Each newline in it ends a paragraph styled as the one it was inserted in;
        at a paragraph's start the new paragraphs come before the one they were cut from, which
        keeps its `headingId`, and anywhere else after it. Measured on newlines inserted at the
        start, middle and end of a heading."""
        at_start = self._starts_paragraph(pos)
        unlinked = False
        if style is None:
            style, unlinked = self._style_at(pos)
        # text a link was taken off stays a run of its own, measured beside unstyled text after
        # the link
        run = next(self._runs) if unlinked else self._run_at(pos, style)
        p = self._paragraph_of(pos)
        self.chars[pos:pos] = [(c, style, run) for c in text]
        own = self.paragraphs[p]
        made = [self._split_style(own) for _ in range(text.count("\n"))]
        if made:
            self.paragraphs[p : p + 1] = [*made, own] if at_start else [own, *made]

    def delete(self, start: int, end: int) -> None:
        """The characters in ``[start, end)``. The paragraphs a deleted newline joins take the first
        one's style, or, when the range starts at that paragraph's start, the last one's. Measured
        on ranges from inside and from the start of a heading into the paragraph after it, and on
        whole paragraphs."""
        first = self._paragraph_of(start)
        joined = sum(1 for c, *_ in self.chars[start:end] if c == "\n")
        keep = first + joined if self._starts_paragraph(start) else first
        del self.chars[start:end]
        if joined:
            self.paragraphs[first : first + joined + 1] = [self.paragraphs[keep]]
        if 0 < start < len(self.chars) and self.chars[start - 1][1] == self.chars[start][1]:
            left, right, at = self.chars[start - 1][2], self.chars[start][2], start
            while at < len(self.chars) and self.chars[at][2] == right:
                self.chars[at] = (*self.chars[at][:2], left)
                at += 1

    def find(self, text: str, *, match_case: bool, regex: bool) -> list[tuple[int, int]]:
        """Where ``text`` matches, as ``[start, end)`` character ranges, left to right and not
        overlapping, newlines included: measured, a match may cross paragraphs, `aa` matches
        `aaa` once, and without `matchCase` the comparison folds case fully, `STRASSE` matching
        `straße` and `ÉTÉ` `été`. A regular expression reads `^` at each paragraph's start and
        `.` as no newline, and one that does not compile matches nothing; an empty match takes the
        character it stands before, measured on `z*`, which replaced every character of a Doc."""
        body = "".join(c for c, *_ in self.chars)
        if regex:
            try:
                pattern = re.compile(text, re.MULTILINE | (0 if match_case else re.IGNORECASE))
            except re.error:
                return []
            return [
                (m.start(), max(m.end(), m.start() + 1))
                for m in pattern.finditer(body)
                if m.start() < len(body)
            ]
        if match_case:
            folded, origin = body, list(range(len(body)))
            needle = text
        else:
            parts: list[str] = []
            origin = []
            for pos, c in enumerate(body):
                parts.append(c.casefold())
                origin += [pos] * len(parts[-1])
            folded, needle = "".join(parts), text.casefold()
        out = []
        at = folded.find(needle)
        while at >= 0:
            last = at + len(needle) - 1
            whole = (at == 0 or origin[at - 1] != origin[at]) and (
                last + 1 == len(origin) or origin[last + 1] != origin[last]
            )
            if whole:
                out.append((origin[at], origin[last] + 1))
                at = folded.find(needle, last + 1)
            else:
                at = folded.find(needle, at + 1)
        return out

    def replace(self, matches: list[tuple[int, int]], text: str) -> None:
        """Each match replaced by ``text`` in the style of its first character, measured on matches
        starting either side of a bold run's edge. The body's last newline stays: a match ending
        with it keeps it after the replacement, measured on `last\\n` and on `\\n`."""
        for start, end in reversed(matches):
            style = self.chars[start][1]
            end = min(end, len(self.chars) - 1)
            if end > start:
                self.delete(start, end)
            if text:
                self.insert(start, text, style)

    def text(self) -> str:
        """The body as the text Drive's search and export read: one line per paragraph."""
        return "".join(c for c, *_ in self.chars)[:-1]

    def content(self) -> list[dict]:
        """The body as it is stored, one `textRun` per run in each paragraph."""
        out = [self._section]
        runs: list[tuple[str, dict, int]] = []
        p = 0
        for c, style, run in self.chars:
            if runs and runs[-1][2] == run:
                runs[-1] = (runs[-1][0] + c, style, run)
            else:
                runs.append((c, style, run))
            if c == "\n":
                elements = [{"textRun": {"content": t, "textStyle": s}} for t, s, _ in runs]
                out.append(
                    {"paragraph": {"elements": elements, "paragraphStyle": self.paragraphs[p]}}
                )
                runs, p = [], p + 1
        return out
