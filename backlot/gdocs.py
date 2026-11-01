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
import warnings
from collections.abc import Iterable
from pathlib import Path

# `regex` asks `locale.getpreferredencoding()` as it compiles each new pattern, which warns under
# UTF-8 mode; the encoding it reads plays no part in a `str` pattern.
warnings.filterwarnings("ignore", category=EncodingWarning, module="regex._main")
import regex  # noqa: E402

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

# An extended grapheme cluster of Unicode's text segmentation (UAX 29): measured, an insert is
# refused exactly where one has no boundary, on 33 sequences of combining and spacing marks, ZWJ
# and ZWNJ, skin tones, flags of regional indicators and of tag characters, Hangul jamo of every
# block, a Prepend and a variation selector, and taken after a ZWJ that follows a letter.
_CLUSTER = regex.compile(r"\X")

# What `replaceAllText` compares in place of each character it changes, matched case or not and
# in a regular expression alike: measured by replacing each ASCII letter and ten pairs (`ae`,
# `dz`, `ss`, ...) in a Doc of every letter in U+00C0-U+024F, U+0250-U+02AF and U+1E00-U+1EFF.
# A letter with a mark compares as its base letter, and 102 others as the letters in this table
# (`ø` as `o`, `æ` as `ae`, `ß` as `ss`); `ﬁ`, `ẞ`, fullwidth, Greek and Cyrillic letters, and
# every character outside the table, compare as themselves.
_FOLD: dict[str, str] = json.loads(
    (Path(__file__).parent / "data" / "gdocs_fold.json").read_text(encoding="utf-8")
)


def _fold(text: str) -> tuple[str, list[tuple[int, int]]]:
    """``text`` as `replaceAllText` compares it, with the ``[start, end)`` in ``text`` of each
    character of the result. A combining mark after a Latin letter folds into that letter:
    measured, `e` and U+0301 were replaced as one by a match of `e`, and `e` U+0301 matched `é`."""
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    for pos, c in enumerate(text):
        if unicodedata.category(c) == "Mn" and spans and spans[-1][1] == pos:
            start = spans[-1][0]
            if unicodedata.name(text[start], "").startswith("LATIN "):
                n = len(_FOLD.get(text[start], text[start]))
                spans[-n:] = [(start, pos + 1)] * n
                continue
        folded = _FOLD.get(c, c)
        out.append(folded)
        spans += [(pos, pos + 1)] * len(folded)
    return "".join(out), spans


def _unfold(spans: list[tuple[int, int]], start: int, end: int) -> tuple[int, int]:
    """The characters a match of ``[start, end)`` in a `_fold` result replaces. One that ends
    inside a character's fold stops before that character, unless it lies wholly within the fold
    from its first letter, when it takes the character and the one before it: measured on `æ` and
    `Ǆ` after `x`, `ß` after `x`, `x\\n` and a space, `xa` in `xæ` leaving `æ` and `a` in `xæ`
    replacing both."""
    begin, finish = spans[start][0], spans[end - 1][1]
    if end < len(spans) and spans[end] == spans[end - 1]:
        if spans[start] == spans[end - 1] and (start == 0 or spans[start - 1] != spans[start]):
            begin = max(begin - 1, 0)
        else:
            finish = max(spans[end - 1][0], begin)
    return begin, finish


def _caseless(c: str) -> str:
    """One character as a search without `matchCase` compares it, by its simple case mappings:
    measured, `s` matched `ſ` and `i` the `I` that `İ` folds to."""
    upper = c.upper() if len(c.upper()) == 1 else c
    lower = upper.lower()
    return lower if len(lower) == 1 else upper


# `searchByRegex` reads a pattern as `java.util.regex` does, with `MULTILINE` and `UNIX_LINES`,
# and `CASE_INSENSITIVE` and `UNICODE_CASE` without `matchCase`: measured on 60 patterns, of which
# these are the classes Java reads in ASCII unless a `(?U)` asks otherwise.
_JAVA_CLASSES = {
    "d": "0-9",
    "s": " \\t\\n\\x0b\\f\\r",
    "w": "a-zA-Z_0-9",
    "h": " \\t\\xa0\\u1680\\u180e\\u2000-\\u200a\\u202f\\u205f\\u3000",
    "v": "\\n\\x0b\\f\\r\\x85\\u2028\\u2029",
}
_JAVA_POSIX = {
    "Lower": "a-z",
    "Upper": "A-Z",
    "ASCII": "\\x00-\\x7f",
    "Alpha": "a-zA-Z",
    "Digit": "0-9",
    "Alnum": "a-zA-Z0-9",
    "Punct": "!-/:-@\\[-`{-~",
    "Graph": "!-~",
    "Print": " -~",
    "Blank": " \\t",
    "Cntrl": "\\x00-\\x1f\\x7f",
    "XDigit": "0-9a-fA-F",
    "Space": " \\t\\n\\x0b\\f\\r",
}
_JAVA_WORD = "[a-zA-Z_0-9]"
_JAVA_BOUNDARY = f"(?:(?<={_JAVA_WORD})(?!{_JAVA_WORD})|(?<!{_JAVA_WORD})(?={_JAVA_WORD}))"
_JAVA_INSIDE = f"(?:(?<={_JAVA_WORD})(?={_JAVA_WORD})|(?<!{_JAVA_WORD})(?!{_JAVA_WORD}))"
_JAVA_FLAGS = regex.compile(r"\(\?([idmsuxU]*)(?:-([idmsuxU]*))?([:)])")
_JAVA_COUNT = regex.compile(r"\{\d+(?:,\d*)?\}")


def _java_pattern(java: str) -> tuple[str, bool] | None:
    """A `java.util.regex` pattern as the `regex` module reads it in its version-1 syntax, and
    whether it names a `u` or `U` flag, or None for one Java does not compile: measured, `{,2}`
    and `(?#c)` matched nothing, `[[:alpha:]]` matched `:` and `\\0101` `A`."""
    named = "".join(m[1] for m in _JAVA_FLAGS.finditer(java))
    ascii_classes = "U" not in named
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(java):
        c = java[i]
        if c == "\\" and i + 1 < len(java):
            e = java[i + 1]
            i += 2
            if e == "Q":
                end = java.find("\\E", i)
                literal = java[i:] if end < 0 else java[i:end]
                i = len(java) if end < 0 else end + 2
                out.append(regex.escape(literal))
            elif e in "hHvV" or (ascii_classes and e in "dDsSwW"):
                members = _JAVA_CLASSES[e.lower()]
                out.append(f"[{'^' if e.isupper() else ''}{members}]")
            elif e == "R" and not depth:
                out.append(f"(?:\\r\\n|[{_JAVA_CLASSES['v']}])")
            elif e in "bB" and ascii_classes and not depth:
                out.append(_JAVA_BOUNDARY if e == "b" else _JAVA_INSIDE)
            elif e == "z" and not depth:
                out.append("\\Z")
            elif e == "Z" and not depth:
                out.append("(?=\\n?\\Z)")
            elif e == "0":
                digits = regex.match(r"[0-3][0-7]{2}|[0-7]{1,2}", java[i:])
                if not digits:
                    return None
                out.append(regex.escape(chr(int(digits[0], 8))))
                i += len(digits[0])
            elif e == "x" and java[i : i + 1] == "{":
                end = java.find("}", i)
                if end < 0:
                    return None
                out.append(regex.escape(chr(int(java[i + 1 : end], 16))))
                i = end + 1
            elif e == "c" and i < len(java):
                out.append(regex.escape(chr(ord(java[i]) ^ 64)))
                i += 1
            elif e == "e":
                out.append("\\x1b")
            elif e == "k" and java[i : i + 1] == "<":
                end = java.find(">", i)
                if end < 0:
                    return None
                out.append(f"(?P={java[i + 1 : end]})")
                i = end + 1
            elif e in "pPN" and java[i : i + 1] == "{":
                end = java.find("}", i)
                if end < 0:
                    return None
                name = java[i + 1 : end]
                if e != "N" and ascii_classes and name in _JAVA_POSIX:
                    out.append(f"[{'^' if e == 'P' else ''}{_JAVA_POSIX[name]}]")
                else:
                    out.append(f"\\{e}{{{name}}}")
                i = end + 1
            else:
                out.append("\\" + e)
            continue
        if depth:
            if c == "[":
                depth += 1
                out.append("[\\:" if java[i + 1 : i + 2] == ":" else "[")
                i += 2 if java[i + 1 : i + 2] == ":" else 1
                continue
            if c == "]":
                depth -= 1
            elif c in "|~-" and java[i + 1 : i + 2] == c:
                out.append(c + "\\" + c)
                i += 2
                continue
            out.append(c)
            i += 1
            continue
        if c == "[":
            depth = 1
            out.append(c)
            if java[i + 1 : i + 2] == "^":
                out.append("^")
                i += 1
            if java[i + 1 : i + 2] == "]":
                out.append("\\]")
                i += 1
            i += 1
            continue
        if c == "(" and java.startswith("(?#", i):
            return None
        if c == "(" and (flags := _JAVA_FLAGS.match(java, i)):
            on = flags[1].translate(str.maketrans("", "", "duU"))
            off = (flags[2] or "").translate(str.maketrans("", "", "duU"))
            group = on + (f"-{off}" if off else "")
            if group or flags[3] == ":":
                out.append(f"(?{group}{flags[3]}")
            i = flags.end()
            continue
        if c == "{" and not _JAVA_COUNT.match(java, i):
            return None
        out.append(c)
        i += 1
    return "".join(out), not ascii_classes or "u" in named


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


def _join_runs(left: tuple[str, dict, int], chunks: Iterable[tuple[list, int]]) -> None:
    """The run that starts ``chunks`` (lists of characters in body order, each from an index)
    relabelled as the run of ``left``, the character before it, when the two are of one style: a
    delete that brings them together joins them. Only as far as the paragraph's newline, since a
    run is served per paragraph and a later delete of that newline joins what follows it anew."""
    right = None
    for chunk, begin in chunks:
        for i in range(begin, len(chunk)):
            c, style, run = chunk[i]
            if right is None:
                if style != left[1]:
                    return
                right = run
            if run != right:
                return
            chunk[i] = (c, style, left[2])
            if c == "\n":
                return


_REGEX = regex
# Version 1 reads Java's nested and intersected classes; simple case folding is Java's.
_REGEX_FLAGS = regex.V1 | regex.MULTILINE
_REGEX_CASELESS = regex.IGNORECASE
# A `(?i)` with `matchCase` is ASCII's alone unless `(?u)` says otherwise: measured, `(?i)ж`
# missed `Ж` and `(?iu)ж` matched it.
_REGEX_ASCII_CASE = regex.ASCII


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
        """Whether an insert before the character at ``pos`` would land inside a grapheme cluster
        (`_CLUSTER`). Inserting U+0301 after a letter composes a cluster and was taken."""
        if pos == 0 or pos >= len(self.chars):
            return False
        start = pos
        while start and self.chars[start - 1][0] != "\n":
            start -= 1
        end = pos
        while self.chars[end][0] != "\n":
            end += 1
        at = start
        for cluster in _CLUSTER.finditer("".join(c for c, *_ in self.chars[start : end + 1])):
            if at >= pos:
                break
            at += len(cluster[0])
        return at != pos

    def _paragraph_of(self, pos: int) -> int:
        return sum(1 for c, *_ in self.chars[:pos] if c == "\n")

    def _starts_paragraph(self, pos: int) -> bool:
        return pos == 0 or self.chars[pos - 1][0] == "\n"

    def _style_at(self, pos: int, text: str) -> dict:
        """The style ``text`` inserted before ``pos`` takes: the character before it's, or at the
        start of a paragraph the first character's, the newline's in an empty one. A link is kept
        only between two characters of that link, and there only by text that does not start with
        a newline, which is unstyled; at a link's end the text takes the next character's style.
        Measured on inserts at both ends of and inside a bold run and a link, a bold link, a link
        inside bold text and an underlined paragraph, at the start of an italic paragraph after a
        struck-through newline, and into an empty paragraph."""
        if self._starts_paragraph(pos):
            style = self.chars[pos][1]
            return _unlinked(style) if "link" in style else style
        style, after = self.chars[pos - 1][1], self.chars[pos][1]
        if "link" not in style:
            return style
        if after.get("link") == style["link"]:
            return {} if text.startswith("\n") else style
        return _unlinked(after) if "link" in after else after

    def _run_at(self, pos: int, style: dict) -> int:
        """The run text in ``style`` inserted before ``pos`` joins. Text that lacks a member the
        character before it has (the newline before a paragraph's first character included) is a
        run of its own; otherwise it joins the run before it in the same paragraph when that is its
        style, else the run after it when that is. Measured: text at a link's end, italic text after
        a struck-through newline, and an unstyled character replaced after a bold one each stayed a
        run of their own, while text at the start of a heading, of an empty paragraph and of a bold
        paragraph after an unstyled newline, and a bold character replaced after an unstyled one,
        joined the run after it."""
        before = self.chars[pos - 1] if pos else None
        return self._run_between(before, self.chars[pos], style, self._starts_paragraph(pos))

    def _run_between(
        self,
        before: tuple[str, dict, int] | None,
        after: tuple[str, dict, int],
        style: dict,
        at_start: bool,
    ) -> int:
        if before is not None and not before[1].keys() <= style.keys():
            return next(self._runs)
        if not at_start and before is not None and before[1] == style:
            return before[2]
        if after[1] == style:
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
        style = self._style_at(pos, text) if style is None else style
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
        if 0 < start < len(self.chars):
            _join_runs(self.chars[start - 1], [(self.chars, start)])

    def find(self, text: str, *, match_case: bool, regex: bool) -> list[tuple[int, int]]:
        """Each match of ``text``, left to right, as the ``[start, end)`` characters it replaces,
        compared as `_fold` folds them: measured, a match may cross paragraphs, `aa` matches `aaa`
        once, and without `matchCase` the comparison is `_caseless` (`STRASSE` matching `straße`).
        A match is counted however much of a character it covers, and one that covers part of a
        character's fold reaches as `_unfold` says, so two ranges may overlap: measured, `s` in
        `ßs` was three matches. A regular expression reads as
        `_java_pattern` says, and one that does not compile matches nothing; an empty match takes
        the character it stands before, measured on `z*`, which replaced every character of a
        Doc."""
        folded, spans = _fold("".join(c for c, *_ in self.chars))
        needle = _fold(text)[0]
        found: list[tuple[int, int]] = []
        if regex:
            java = _java_pattern(needle)
            flags = _REGEX_FLAGS | (0 if match_case else _REGEX_CASELESS)
            if java is not None and match_case and not java[1]:
                flags |= _REGEX_ASCII_CASE
            try:
                pattern = None if java is None else _REGEX.compile("(?-f)" + java[0], flags)
            except _REGEX.error:
                pattern = None
            for m in pattern.finditer(folded) if pattern else ():
                if m.start() < len(folded):
                    found.append((m.start(), max(m.end(), m.start() + 1)))
        else:
            if not match_case:
                folded, needle = (
                    "".join(map(_caseless, folded)),
                    "".join(map(_caseless, needle)),
                )
            at = folded.find(needle)
            while at >= 0:
                found.append((at, at + len(needle)))
                at = folded.find(needle, at + len(needle))
        return [_unfold(spans, start, end) for start, end in found]

    def replace(self, matches: list[tuple[int, int]], text: str) -> None:
        """Each match replaced by ``text`` in the style of its first character, measured on matches
        starting either side of a bold run's edge, and an empty one by nothing. The body's last
        newline stays: a match ending with it keeps it after the replacement, measured on `last\\n`
        and on `\\n`.

        Ranges that overlap are replaced one at a time from the last, each over what the ones after
        it left: measured, `a` in `aæ` replaced both characters by one `R`. Otherwise, one pass
        from the last match to the first, each a delete and an insert as `delete` and
        `insert` make them, with what follows the match already built: the characters and
        paragraphs after the cut are kept back to front, so a replace costs the body's length once
        rather than once per match."""
        if any(a[1] > b[0] for a, b in itertools.pairwise(matches)):
            for start, end in reversed(matches):
                style = self.chars[start][1]
                end = min(end, len(self.chars) - 1)
                if end > start:
                    self.delete(start, end)
                if text:
                    self.insert(start, text, style)
            return
        newlines = list(itertools.accumulate((c == "\n" for c, *_ in self.chars), initial=0))
        cut = len(self.chars)
        tail: list[list[tuple[str, dict, int]]] = []  # chunks, the last one first in the body
        tail_paragraphs: list[dict] = []  # back to front: the paragraph holding the cut last
        for start, end in reversed(matches):
            if start == end:
                continue
            end = min(end, len(self.chars) - 1)
            tail.append(self.chars[end:cut])
            tail_paragraphs += reversed(self.paragraphs[newlines[end] : newlines[cut]])
            cut = start
            at_start = self._starts_paragraph(start)
            if end > start:
                joined = newlines[end] - newlines[start]
                if joined and not at_start:
                    tail_paragraphs[-1] = self.paragraphs[newlines[start]]
                if start:
                    _join_runs(self.chars[start - 1], ((chunk, 0) for chunk in reversed(tail)))
            if not text:
                continue
            style = self.chars[start][1]
            before = self.chars[start - 1] if start else None
            after = next(chunk[0] for chunk in reversed(tail) if chunk)
            run = self._run_between(before, after, style, at_start)
            own = tail_paragraphs.pop()
            made = [self._split_style(own) for _ in range(text.count("\n"))]
            tail_paragraphs += reversed([*made, own] if at_start else [own, *made])
            tail.append([(c, style, run) for c in text])
        tail.append(self.chars[:cut])
        tail_paragraphs += reversed(self.paragraphs[: newlines[cut]])
        self.chars = [c for chunk in reversed(tail) for c in chunk]
        self.paragraphs = tail_paragraphs[::-1]

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
