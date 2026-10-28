"""Google Docs' body, as the importer, the store and the Docs routes all hold it.

Outside the router for the reason `gsheets` is: the importer is the other caller.

A body is stored as real serves `body.content` with every `startIndex` and `endIndex` left out, and
:func:`indexed` puts them back at read time, so the stored form is the vendor's own. A document
that states no structure is stored as nothing and read through :func:`from_text`.

Measured against the live Docs v1 API on 2026-10-26, as a Workspace member on documents it owns and
as a second account on the Docs it can read.
"""

from __future__ import annotations

import copy
import json
import re
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


def heading_id(file_id: str, ordinal: int) -> str:
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
