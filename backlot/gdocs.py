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
    (Path(__file__).parent / "data" / "gdocs_defaults.json").read_text(encoding="utf-8")
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
    together (`_run_at` says when an insert starts one): measured, deleting a bold run joined the
    unstyled runs either side of it.

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

    def _style_at(self, pos: int) -> dict:
        """The style text inserted before ``pos`` takes: the character before it's, or at the start
        of a paragraph the first character's, the newline's in an empty one; a link only between
        two characters of that link. Measured on inserts at both ends of and inside a bold run and a
        link, at the start of an italic paragraph after a struck-through newline, and into an empty
        paragraph."""
        if self._starts_paragraph(pos):
            style, inside = self.chars[pos][1], False
        else:
            style = self.chars[pos - 1][1]
            inside = self.chars[pos][1].get("link") == style.get("link")
        return style if inside or "link" not in style else _unlinked(style)

    def _run_at(self, pos: int, style: dict) -> int:
        """The run text in ``style`` inserted before ``pos`` joins. Text that lacks a member the
        character before it has (the newline before a paragraph's first character included) is a
        run of its own; otherwise it joins the run before it in the same paragraph when that is its
        style, else the run after it when that is. Measured: text at a link's end, italic text after
        a struck-through newline, and an unstyled character replaced after a bold one each stayed a
        run of their own, while text at the start of a heading, of an empty paragraph and of a bold
        paragraph after an unstyled newline, and a bold character replaced after an unstyled one,
        joined the run after it."""
        before = self.chars[pos - 1][1] if pos else {}
        if not before.keys() <= style.keys():
            return next(self._runs)
        if not self._starts_paragraph(pos) and before == style:
            return self.chars[pos - 1][2]
        if self.chars[pos][1] == style:
            return self.chars[pos][2]
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
        style = self._style_at(pos) if style is None else style
        run = self._run_at(pos, style)
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


# --- a body as Drive's `files.export` renders it --------------------------------------------------
#
# Measured against Drive v3 from 2026-10-30 to 2026-11-01, as a Workspace member exporting Docs it
# wrote through `documents.batchUpdate`, each holding only what a corpus may state: named styles,
# marks, and the characters each rule below names.

# The `#`s a paragraph of each named style opens with in Markdown.
_MARKDOWN_LEVEL = {"TITLE": 1, "SUBTITLE": 2, **{f"HEADING_{n}": n for n in range(1, 7)}}

# Each named style's own text style, which its paragraphs' runs take where they state nothing:
# HEADING_6's italic is Markdown's `*`.
_TEXT_STYLES = {s["namedStyleType"]: s["textStyle"] for s in _DEFAULTS["namedStyles"]["styles"]}

# The space separators a plain export keeps: a figure space and a narrow no-break space. Every other
# one, U+2028 and U+2029 among them, is a space.
_KEPT_SPACES = "\u2007\u202f"

# What a plain export trims from both ends: every character up to the space, as Java's `trim`.
_TRIMMED = "".join(map(chr, range(0x21)))


def _paragraph_runs(paragraph: dict) -> list[tuple[str, dict]]:
    """A paragraph's runs without the newline that ends it."""
    runs = [
        (pe["textRun"]["content"], pe["textRun"].get("textStyle", {}))
        for pe in paragraph["elements"]
    ]
    text, style = runs[-1]
    runs[-1] = (text.removesuffix("\n"), style)
    return [r for r in runs if r[0]]


def _spans(paragraph: dict) -> list[tuple[str, dict]]:
    """A paragraph's runs with neighbours of one text style joined. A `documents.batchUpdate`
    insert beside a link leaves two such runs, and Markdown escapes them as one. A text style the
    paragraph's named style sets to the same value (HEADING_6's italic) is left out first, as
    real's body leaves it out: on 2026-11-01 an unstyled space and an italic tab in a HEADING_6
    read back as one unstyled run."""
    named = _TEXT_STYLES.get(_named(paragraph), {})
    out: list[tuple[str, dict]] = []
    for text, style in _paragraph_runs(paragraph):
        style = {k: v for k, v in style.items() if named.get(k) != v}
        if out and out[-1][1] == style:
            out[-1] = (out[-1][0] + text, style)
        else:
            out.append((text, style))
    return out


def _named(paragraph: dict) -> str:
    return paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")


def _plain_char(c: str) -> str:
    if c == "\x0b":
        return "\r\n"
    if c == "\t":
        return " " * 8
    if unicodedata.category(c) in ("Zs", "Zl", "Zp") and c not in _KEPT_SPACES:
        return " "
    return c


def _after_break(text: str) -> str:
    """One run's text without the separator right after each of its vertical tabs: a space, line or
    paragraph separator but a no-break one. Measured 2026-10-31: U+0020, U+1680, U+2002, U+200A,
    U+205F, U+3000, U+2028 and U+2029 dropped, one at a time; U+00A0, U+2007, U+202F, a tab, U+200B,
    U+FEFF and U+0085 kept, and so is a separator starting the next run, of one text style or
    not."""
    return "".join(
        c
        for i, c in enumerate(text)
        if not (i and text[i - 1] == "\x0b" and _plain_char(c) == " " and c != "\xa0")
    )


def _lead_breaks(paragraph: dict) -> int:
    """The CRLFs a plain export adds for the runs a paragraph starts with that hold nothing but
    vertical tabs once `_after_break` drops the separators after them: one for each such run another
    run follows, and one for each such run that is a link. Real's answers on 2026-11-01 match it on
    paragraphs starting with one to three runs of vertical tabs, before text or alone, in
    NORMAL_TEXT, TITLE and SUBTITLE, and on ones starting with runs of a vertical tab and a space,
    U+3000, a tab, U+00A0 or two spaces, or a space and a vertical tab; a HEADING_n takes none."""
    spans = _spans(paragraph)
    n = 0
    for i, (text, style) in enumerate(spans):
        if _after_break(text).strip("\x0b"):
            break
        n += i + 1 < len(spans)
        n += "link" in style
    return n


def plain_text(content: list[dict]) -> str:
    """A body as `files.export` serves it as `text/plain`: a byte-order mark, then one line per
    paragraph joined by CRLF, trimmed at both ends. A vertical tab is a CRLF and drops the separator
    after it (`_after_break`), a tab is eight spaces. A paragraph holding nothing but vertical tabs,
    or nothing, takes one CRLF more, and one other than a HEADING_n that starts with vertical tabs
    takes the CRLFs `_lead_breaks` counts; a HEADING_n holding nothing is left out, and one holding
    only vertical tabs is a CRLF fewer than them."""
    lines = []
    for el in content:
        if "paragraph" not in el:
            continue
        text = "".join(_after_break(t) for t, _ in _paragraph_runs(el["paragraph"]))
        if _named(el["paragraph"]).startswith("HEADING_"):
            if not text:
                continue
            if not text.strip("\x0b"):
                text = text[1:]
        else:
            if not text.strip("\x0b"):
                text += "\x0b"
            text = "\x0b" * _lead_breaks(el["paragraph"]) + text
        lines.append("".join(map(_plain_char, text)))
    return "\ufeff" + "\r\n".join(lines).strip(_TRIMMED)


# Markdown. A paragraph is a line, and two lines of NORMAL_TEXT are joined by a hard break (two
# spaces and a newline), as a vertical tab is; a paragraph with a `#` heading stands apart by a
# blank line. Two hard breaks in a row, or one beside a blank line, with nothing but empty
# paragraphs between them, are one blank line.
_BREAK, _BLOCK = object(), object()

# A paragraph of exactly two spaces before a heading: written as nothing, and the breaks on both
# sides of it as three newlines (real's answers on 2026-11-01).
_GAP = object()

# Escaped wherever they stand. `+`, `-` and `=` are escaped at a word's start, after a space, a tab
# or the start of a run; a run of digits there is escaped at the `.` or `)` that ends it before a
# space; and the `:` of `http://`, `https://` or `ftp://` and the `.` of `www.`, in any case (U+017F
# `ſ` reading as `s`), where `_word_before` finds no word character before them in the run. A `&` is
# escaped before an ASCII letter. A run is a span of one text style (`_spans`).
_ALWAYS = set("!#$*<>[\\]_`|~")
_WORD_START = re.compile(r"(?<![^ \t])")
_LIST_NUMBER = re.compile(r"(?<![^ \t])[0-9]+([.)])(?=[ \t]|$)")
_AUTOLINK = re.compile(r"(?:https?|ftp)(:)//|www(\.)", re.IGNORECASE)

# A URL keeps every character but these: a space, a tab, LF and CR are `%` and their code in two
# columns (`%20`, `% 9`), and the rest are backslash-escaped.
_URL_ESCAPED = set("()<>\\`")

# The marks Markdown spells, by rank: a link 0, italic 1, bold 2, strikethrough 3. Marks opening at
# one character open in rank order, a link outermost.
_MARK_ORDER = ("link", "italic", "bold", "strikethrough")
_DELIMITER = {"italic": "*", "bold": "**", "strikethrough": "~~"}


def _escape(text: str) -> list[str]:
    """Each character of one run as Markdown writes it."""
    out = [f"\\{c}" if c in _ALWAYS else c for c in text]
    for i, c in enumerate(text):
        if c in "+-=" and _WORD_START.match(text, i):
            out[i] = f"\\{c}"
        elif c == "&" and text[i + 1 : i + 2].isascii() and text[i + 1 : i + 2].isalpha():
            out[i] = "\\&"
    for m in _LIST_NUMBER.finditer(text):
        out[m.start(1)] = f"\\{text[m.start(1)]}"
    at = 0
    while m := _AUTOLINK.search(text, at):
        at = m.start() + 1
        if not _word_before(text, m.start()):
            g = m.start(1) if m.start(1) >= 0 else m.start(2)
            out[g] = f"\\{text[g]}"
            at = m.end()
    return out


def _word_before(text: str, at: int) -> bool:
    """Whether a word character stands before `text[at]` in a run, as `java.util.regex`'s `\\b`
    reads one: an ASCII letter, digit or `_`, or a nonspacing mark (Mn) after a letter or digit of
    any script with only nonspacing marks between. A character past U+FFFF ends that walk as no
    letter, since Java steps back one UTF-16 unit and reads the low surrogate. Real's answers on
    2026-11-01 match it with `0`, `A`, `e`, `w`, `é`, `α`, `_`, a space and `-` before each of
    U+00AD, U+0301, U+0345, U+034F, U+0903, U+1160, U+200C, U+200D, U+2060, U+20DD, U+3099, U+FE0F,
    U+FE20, U+1F3FB, U+E0100 and U+0301 U+0302."""
    if not at:
        return False
    c = text[at - 1]
    if c.isascii() and (c.isalnum() or c == "_"):
        return True
    if unicodedata.category(c) != "Mn":
        return False
    for c in reversed(text[:at]):
        if ord(c) > 0xFFFF:
            return False
        kind = unicodedata.category(c)
        if kind in ("Lu", "Ll", "Lt", "Lm", "Lo", "Nd"):
            return True
        if kind != "Mn":
            return False
    return False


def _url(url: str) -> str:
    return "".join(
        f"%{ord(c):2X}" if c in " \t\n\r" else f"\\{c}" if c in _URL_ESCAPED else c for c in url
    )


def _marks(style: dict) -> frozenset:
    out = {k for k in _DELIMITER if style.get(k)}
    if style.get("link", {}).get("url") is not None:
        out.add(("link", style["link"]["url"]))
    return frozenset(out)


def _rank(mark) -> int:
    return _MARK_ORDER.index(mark[0] if isinstance(mark, tuple) else mark)


def _delimiter(mark, closing: bool = False) -> str:
    if isinstance(mark, tuple):
        return f"]({_url(mark[1])})" if closing else "["
    return _DELIMITER[mark]


# Real's Markdown writer, read off its answers on 2026-10-31 and 2026-11-01 (Google documents none of
# it). Its delimiters do not nest: where marks end, the lowest-rank ending mark closes with every
# mark opened above it, in rounds; the marks that continue reopen in an order read off a copy of the
# stack, and a new mark of lower rank than a closing one takes a closer's place. An opener waits out
# spaces and tabs, and a closer that meets a waiting opener cancels it.


def _rounds(stack: list, marks: set) -> list:
    """The rounds that close `stack`'s ending marks, each (index, inner, target): the lowest-rank
    ending mark is the target, every mark above it the inner, and the next round runs below it."""
    rest, rounds = list(stack), []
    while ending := [m for m in rest if m not in marks]:
        target = min(ending, key=_rank)
        at = rest.index(target)
        rounds.append((at, rest[at + 1 :], target))
        rest = rest[:at]
    return rounds


def _transition(stack: list, marks: set, end: dict, now: int) -> tuple:
    """Where the marks open, `stack` (bottom first), give way to the next run's `marks`: the marks
    closed, the marks opened, the stack after, the rounds, and the (new mark, closer's mark) a
    substitution made. `end[m]` is the first later run without the mark m, `now` this run.

    A round prints the part not printed yet of one close list of its inner and the earlier rounds',
    sorted by (end, rank) with an ending mark's end `now`, then its target. The continuing marks
    reopen read off a copy of the stack from which the ending marks are swap-removed, highest rank
    first, each found from the copy's end; where link 1 gives way to link 2, link 2 enters the copy
    between the first two removals when the first is the stack's top and the second is not a link
    and not on top after it."""
    new = sorted((m for m in marks if m not in stack), key=_rank)
    rounds = _rounds(stack, marks)
    if not rounds:
        return [], new, stack + new, rounds, None
    base = stack[: rounds[-1][0]]
    ends = [m for m in stack if m not in marks]

    def key(m) -> tuple:
        return (end[m] if m in marks else now, _rank(m))

    sub = at_round = dropped = None
    for k, (_, inner, target) in enumerate(rounds):
        lower = [n for n in new if _rank(n) < _rank(target)]
        if lower and any(key(m) < key(max(lower, key=key)) for m in inner):
            sub, at_round = max(lower, key=key), k
            break
    closes, closed = [], []
    for k, (_, inner, target) in enumerate(rounds):
        printed = len(closes)
        closes = sorted(closes + inner, key=key)
        if k == at_round:
            dropped = next(m for m in closes if m in inner)
            closes = sorted([m for m in closes if m != dropped] + [sub], key=key)
        closed += closes[printed:] + [target]
    lead = [t for _, inner, t in itertools.takewhile(lambda r: not r[1], rounds)]
    copy, size = list(stack), len(stack)
    if dropped is not None:
        copy[stack.index(dropped)] = sub
    for m in lead:
        at = copy[:size].index(m)
        copy[at], size = copy[size - 1], size - 1
    removed = sorted((m for m in ends if m not in lead and m != dropped), key=lambda m: -_rank(m))
    link2 = [n for n in new if isinstance(n, tuple)]
    push = sub is None and link2 and any(isinstance(m, tuple) for m in ends) and len(removed) > 1
    push = push and removed[0] == stack[-1] and not isinstance(removed[1], tuple)
    for i, m in enumerate(removed):
        if push and i == 1 and copy[size - 1] != m:
            copy[size : size + 1], size = link2[:1], size + 1
        at = max(j for j, c in enumerate(copy) if c == m)
        copy[at], size = copy[size - 1], size - 1
    targets = {t for *_, t in rounds}
    slots = [i for i in range(rounds[-1][0] + 1, len(stack)) if stack[i] not in targets]
    top = max(map(_rank, ends))
    low = [n for n in new if _rank(n) < top]
    high = [n for n in new if _rank(n) >= top]
    if sub is not None:
        mid = []
        for j, m in enumerate(m for m in reversed(stack) if m in marks and m not in base):
            read = copy[slots[-1 - j]] if j < len(slots) else m
            mid.append(read if m != dropped else dropped if read == sub else sub)
        opened = low[::-1] + mid + high
    else:
        reads = [copy[slots[i]] for i in reversed(range(len(closes))) if closes[i] in marks]
        if any(m not in marks for m in reads):
            opened = reads + low[::-1] + high
        elif len(set(reads)) == len(reads):
            opened = low[::-1] + reads + high
        else:
            opened = reads[:1] + low[::-1] + reads[1:] + high
    after = base + [m for m in stack[len(base) :] if m in marks] + new
    return closed, opened, after, rounds, (sub, dropped) if sub is not None else None


def _commit_at(rounds, closed, held, before, after, swap) -> int | None:
    """The round before which a transition writes the pending spaces and tabs and the `held`
    openers ([mark, anchors] pairs), or None to keep holding them."""
    if not held:
        return None
    new, ended = after - before, before - after
    low = min(map(_rank, new), default=None)
    below = (
        None if low is None else next((k for k, r in enumerate(rounds) if low < _rank(r[2])), None)
    )
    took = min(len(closed), len(held))
    if any(anchors for _, anchors in held[len(held) - took :]):
        return 0
    if new and (not ended or low < min(map(_rank, ended))):
        return below
    marks = [m for m, _ in held]
    if any(m in marks for m in rounds[-1][1]):
        return 0 if any(r[1] for r in rounds[:-1]) else below
    if took < len(closed):
        end = 0
        for k, (_, inner, _) in enumerate(rounds):
            end += len(inner) + 1
            if took < end:
                break
        if k <= 1:
            pairs = list(zip(closed, reversed(marks)))[:took]
            mixed = pairs[0][0] == pairs[0][1] and any(c != m for c, m in pairs[1:])
            if mixed and isinstance(rounds[0][2], tuple):
                return below
            return 0 if swap is not None and swap[0] in marks[len(marks) - took :] else None
        if not rounds[0][1]:
            return 0 if new and low <= _rank("italic") else None
    return 0 if rounds[0][1] else below


def _stretch(pieces: list[tuple[frozenset, str, list[str]]]) -> str:
    """One stretch of a paragraph (the runs between two vertical tabs) as Markdown, from its
    (marks, text, escaped text) pieces. At each change of marks a closer takes the last held opener,
    else the opener written last, else is written, and before the round `_commit_at` picks the
    pending spaces and tabs and the held openers are written; the new openers are then held. A held
    opener's anchors count the changes it stayed held across that opened marks or took two openers."""
    sets = [m for i, (m, *_) in enumerate(pieces) if m != (pieces[i - 1][0] if i else frozenset())]
    sets.append(frozenset())
    out: list[tuple[str, bool]] = []
    held: list[list] = []
    pending, stack = "", []

    def commit() -> None:
        nonlocal pending
        out.extend([(pending, False)] if pending else [])
        out.extend((_delimiter(m), True) for m, _ in held)
        pending = ""
        held.clear()

    def transition(now: int, before: frozenset) -> None:
        nonlocal stack
        after = sets[now]
        end = {m: next(j for j in range(now, len(sets)) if m not in sets[j]) for m in after}
        closed, opened, stack_after, rounds, swap = _transition(stack, set(after), end, now)
        at = _commit_at(rounds, closed, held, before, after, swap)
        took = start = 0
        for k, (_, inner, _) in enumerate(rounds):
            if k == at:
                commit()
            for m in closed[start : start + len(inner) + 1]:
                if held or (out and out[-1][1]):
                    (held or out).pop()
                    took += 1
                else:
                    out.append((_delimiter(m, closing=True), False))
            start += len(inner) + 1
        for h in held:
            h[1] += bool(opened) or took >= 2
        held.extend([m, 0] for m in opened)
        stack = stack_after

    step, now = -1, frozenset()
    for marks, text, escaped in pieces:
        if marks != now:
            step += 1
            transition(step, now)
            now = marks
        core = [i for i, c in enumerate(text) if c not in " \t"]
        if not core:
            pending += "".join(escaped)
            continue
        pending += "".join(escaped[: core[0]])
        commit()
        out.append(("".join(escaped[core[0] : core[-1] + 1]), False))
        pending = "".join(escaped[core[-1] + 1 :])
    transition(step + 1, now)
    commit()
    return "".join(text for text, _ in out)


def _inline(runs: list[tuple[str, dict]], named: dict, heading: bool = False) -> list:
    """A paragraph's runs as Markdown: text and a `_BREAK` for each vertical tab, or in a heading a
    space. A vertical tab closes every mark; each stretch between two is `_stretch`, where runs
    whose marks are equal, as for text styles that part on underline alone, meet without a
    transition, each escaped as a run of its own. On 2026-11-01 real wrote such runs so, among them
    three italic, strikethrough runs (two spaces, a space and a tab, a tab and `a`) with the outer
    two underlined, as their spaces and tabs and then `*~~a~~*`."""
    stretches: list[list] = [[]]
    for text, style in runs:
        marks = _marks({**named, **style})
        for piece in re.split("(\x0b)", text):
            if piece == "\x0b":
                stretches.append([])
            elif piece:
                stretches[-1].append((marks, piece, _escape(piece)))
    out: list = []
    line = ""
    for n, stretch in enumerate(stretches):
        line += _stretch(stretch)
        if n < len(stretches) - 1:
            if heading:
                line += " "
                continue
            out += [line, _BREAK]
            line = ""
    return [*out, line]


def markdown(content: list[dict]) -> str:
    """A body as `files.export` serves it as `text/markdown` or `text/x-markdown`: TITLE as `#`,
    SUBTITLE as `##` and HEADING_n as n `#`s, a heading's vertical tab a space; bold, italic,
    strikethrough and links in a paragraph's named style or its runs' as `**`, `*`, `~~` and
    `[text](url)`, underline as nothing. Nothing is trimmed, and nothing ends the last line."""
    paragraphs = [el["paragraph"] for el in content if "paragraph" in el]
    levels = [_MARKDOWN_LEVEL.get(_named(p)) for p in paragraphs]
    tokens: list = []
    for i, (paragraph, level) in enumerate(zip(paragraphs, levels)):
        if i:
            tokens.append(_BLOCK if level or levels[i - 1] else _BREAK)
        runs = _spans(paragraph)
        named = _TEXT_STYLES.get(_named(paragraph), {})
        if level:
            tokens.append("#" * level + " " + "".join(_inline(runs, named, heading=True)))
            continue
        line = _inline(runs, named)
        if line == ["  "] and i + 1 < len(paragraphs) and levels[i + 1]:
            tokens.append(_GAP)
        else:
            tokens += line
    out, pending = "", []
    for t in tokens:
        if t is _BREAK or t is _BLOCK or t is _GAP:
            pending.append(t)
        elif t:
            out += _separator(pending) + t
            pending = []
    return out + _separator(pending)


def _separator(breaks: list) -> str:
    """What the breaks between two lines, with only empty paragraphs between them, are
    written as."""
    if not breaks:
        return ""
    if _GAP in breaks:
        return "\n\n\n"
    return "  \n" if breaks == [_BREAK] else "\n\n"
