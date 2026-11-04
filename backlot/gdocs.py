"""A Google Doc, as the importer, the store and the Docs routes all hold it.

Outside the router for the reason `gsheets` is: the importer is the other caller.

A body is stored as real serves `body.content` with every `startIndex` and `endIndex` left out, and
:func:`indexed` puts them back at read time, so the stored form is the vendor's own. A document
that states no structure is stored as nothing and read through :func:`from_prose`, until its first
write stores that reading (:class:`Document`).

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
from collections.abc import Iterable, Iterator
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

# The characters `replaceAllText` reads a footnote reference and a page break as: measured
# 2026-11-02, a regular expression's class found U+001B where a reference stood and U+000C where a
# page break did, `.` matched either, and `xy` did not match across one. Both are characters
# `insertText` strips, so no text holds them, and here each stands for its element in a body.
FOOTNOTE = "\x1b"
PAGE_BREAK = "\x0c"
_ELEMENTS = FOOTNOTE + PAGE_BREAK

# A footnote's run style, as `createFootnote` makes one and the editor writes into it: measured
# 2026-11-02, its content opens with a space and every run is 10 PT.
_FOOTNOTE_TEXT = {"fontSize": {"magnitude": 10, "unit": "PT"}}


def document_style(header: str | None = None, footer: str | None = None) -> dict:
    """A tab's `documentStyle`: the defaults, and its header's and footer's ids after
    `background`, where real serves them (measured 2026-10-26 and 2026-11-02)."""
    style = copy.deepcopy(_DEFAULTS["documentStyle"])
    ids = {k: v for k, v in (("defaultHeaderId", header), ("defaultFooterId", footer)) if v}
    out: dict = {}
    for key, value in style.items():
        out[key] = value
        if key == "background":
            out |= ids
    return out


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


def _base36(seed: str) -> str:
    n = synth.hnum(seed, length=16)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    for _ in range(12):
        n, r = divmod(n, 36)
        out += digits[r]
    return out


def heading_id(file_id: str, ordinal: int | str) -> str:
    """A heading paragraph's `headingId`: ``h.`` and twelve base-36 characters, the length 262 of
    281 measured ids have (the rest are one or two shorter). Real gave one to every TITLE,
    SUBTITLE and HEADING_n paragraph in all but one Doc read, and none to NORMAL_TEXT."""
    return "h." + _base36(f"{file_id}\0{ordinal}")


def segment_id(file_id: str, seed: str) -> str:
    """A header's, footer's or footnote's id: ``kix.`` and twelve base-36 characters, as real's
    `createHeader`, `createFooter` and `createFootnote` answered (measured 2026-11-02; a few ids
    were one shorter)."""
    return "kix." + _base36(f"{file_id}\0kix\0{seed}")


def tab_id(file_id: str, seed: str) -> str:
    """A tab's id past the first, `t.0`: ``t.`` and twelve base-36 characters, as real's
    `addDocumentTab` answered (measured 2026-11-02; a few ids were one shorter)."""
    return "t." + _base36(f"{file_id}\0tab\0{seed}")


def _paragraph(
    runs: list[tuple[str, dict] | dict], newline: dict, pstyle: dict, hid: str | None = None
) -> dict:
    """One `paragraph` structural element, adjacent runs of one style merged as real merges them
    and the newline joined to the last run when its style matches. A run given as a dict is a
    footnote reference or a page break, an element of its own."""
    elements: list[dict] = []
    for run in [*runs, ("\n", newline)]:
        if isinstance(run, dict):
            elements.append(run)
            continue
        text, ts = run
        if not text:
            continue
        if elements and elements[-1].get("textRun", {}).get("textStyle") == ts:
            elements[-1]["textRun"]["content"] += text
        else:
            elements.append({"textRun": {"content": text, "textStyle": ts}})
    head = {"headingId": hid} if hid else {}
    return {"paragraph": {"elements": elements, "paragraphStyle": head | pstyle}}


def _paragraph_style(named: str, footnote: bool = False) -> dict:
    # measured 2026-11-02: every paragraph of a footnote has a `lineSpacing` of 100
    spacing = {"lineSpacing": 100} if footnote else {}
    return {"namedStyleType": named, **spacing, "direction": "LEFT_TO_RIGHT"}


def _section_break() -> dict:
    return {"sectionBreak": {"sectionStyle": copy.deepcopy(_DEFAULTS["sectionStyle"])}}


def blank_segment(footnote: bool = False) -> list[dict]:
    """A header or footer as `createHeader` and `createFooter` make it, one empty paragraph, or a
    footnote as `createFootnote` makes it (`_FOOTNOTE_TEXT`)."""
    if footnote:
        return [
            _paragraph(
                [(" ", dict(_FOOTNOTE_TEXT))],
                dict(_FOOTNOTE_TEXT),
                _paragraph_style("NORMAL_TEXT", footnote=True),
            )
        ]
    return [_paragraph([], {}, _paragraph_style("NORMAL_TEXT"))]


def _stated(
    file_id: str, paragraphs: list[dict], seed: str, notes: dict | None, footnote: bool = False
) -> list[dict]:
    """A corpus's paragraphs as stored content, a heading's `headingId` drawn here, once, from
    ``seed`` and its position. A stated footnote is added to ``notes``, formatted as
    `_FOOTNOTE_TEXT`."""
    content = []
    for i, p in enumerate(paragraphs):
        named = p.get("style") or "NORMAL_TEXT"
        stated = p["runs"] if "runs" in p else [{"text": p.get("text", "")}]
        if footnote and i == 0:
            stated = [{"text": " "}, *stated]
        runs: list[tuple[str, dict] | dict] = []
        for j, r in enumerate(stated):
            ts = text_style(r) | (_FOOTNOTE_TEXT if footnote else {})
            ts = {k: ts[k] for k in _TEXT_STYLE_ORDER if k in ts}
            if "footnote" in r:
                fid = segment_id(file_id, f"{seed}{i}.{j}")
                notes[fid] = _stated(file_id, r["footnote"], f"{seed}{i}.{j}.", None, True)
                runs.append({"footnoteReference": {"footnoteId": fid, "textStyle": ts}})
            elif r.get("pageBreak"):
                runs.append({"pageBreak": {"textStyle": ts}})
            else:
                runs.append((r["text"], ts))
        newline = _newline_style(stated[-1]) | (_FOOTNOTE_TEXT if footnote else {})
        newline = {k: newline[k] for k in _TEXT_STYLE_ORDER if k in newline}
        hid = heading_id(file_id, f"{seed}{i}") if named != "NORMAL_TEXT" else None
        content.append(_paragraph(runs, newline, _paragraph_style(named, footnote), hid))
    return content


def from_paragraphs(
    file_id: str, paragraphs: list[dict], seed: str = "", notes: dict | None = None
) -> list[dict]:
    """A corpus's `paragraphs` as a stored body: the section break every body opens with, then one
    `paragraph` per entry. A stated footnote is added to ``notes``."""
    return [_section_break(), *_stated(file_id, paragraphs, seed, notes)]


def from_text(text: str) -> list[dict]:
    """A document that states no structure: one unstyled paragraph per line of its text, read as
    `insertText` would write it, so without the characters it strips."""
    return [_section_break()] + [
        _paragraph([(STRIPPED.sub("", line), {})], {}, _paragraph_style("NORMAL_TEXT"))
        for line in text.split("\n")
    ]


# The emoji a tab icon may be: every sequence of Unicode's emoji-test.txt for Emoji 15.0, whatever
# its qualification, but `©`, `®` and `™` with or without U+FE0F, and without it `‼`, `⁉` and each
# arrow shown as text by default (one the list also holds with U+FE0F, `➡`). Measured 2026-11-02
# and 2026-11-03: Emoji 15.1's `🐦‍🔥`, 16.0's `🫩` and `🇨🇶`, unassigned pictographs, U+FE0F
# after an emoji shown as one by default (`⭐️`), a flag that is no RGI one and a ZWJ that ends a
# sequence were refused; 15.0's `🫨`, an unqualified `❤`, `1⃣` and `ℹ`, a skin tone alone, and
# arrows shown as emoji (`🏹`, `💘`, `🔄`) were taken.
_ICONS = frozenset(
    json.loads((Path(__file__).parent / "data" / "gdocs_icons.json").read_text(encoding="utf-8"))
)
_ICON_REFUSED = frozenset("\u00a9\u00ae\u2122")
_ICON_BARE = frozenset("\u203c\u2049")


def is_icon(text: str) -> bool:
    if text not in _ICONS or text[0] in _ICON_REFUSED:
        return False
    if len(text) > 1 or text + "\ufe0f" not in _ICONS:
        return True
    return text not in _ICON_BARE and "ARROW" not in unicodedata.name(text, "")


# --- a document: its tabs, and in each a body, a header, a footer and footnotes ------------------
#
# Stored as ``{"tabs": [tab, ...]}``, with ``"tabbed": true`` once the Doc has held more than one
# tab (`document_text`), each tab ``{"tabProperties": {tabId, title, iconEmoji?}, "untitled"?,
# "body": [...], "headers": {id: [...]}, "footers": {...}, "footnotes": {...}, "childTabs":
# [...]}``, its contents as real serves them without indices or footnote numbers. A tab's index,
# parent and depth are its place in the tree, read where it is served.


def new_tab(
    tab_id: str, title: str, body: list[dict] | None = None, untitled: bool = False
) -> dict:
    """A tab holding ``body``, or the empty paragraph a blank Doc and `addDocumentTab` make:
    measured 2026-11-02, a new tab's `documentStyle`, `namedStyles` and section style are a blank
    Doc's. An ``untitled`` tab is the first of a blank Doc that has held no other, whose ``title``
    is only the one it shows."""
    return {
        "tabProperties": {"tabId": tab_id, "title": title},
        **({"untitled": True} if untitled else {}),
        "body": body if body is not None else from_text(""),
        "headers": {},
        "footers": {},
        "footnotes": {},
        "childTabs": [],
    }


# The line breaks a tab title loses, each run of them to one space, and what a title blank once
# they have gone holds at most: the characters to U+0020.
_TITLE_BREAKS = re.compile("[\r\n\x0b\x0c\x85\u2028\u2029]+")
_TITLE_BLANK = "".join(map(chr, range(0x21)))


def tab_title(title: str) -> str | None:
    """A tab title as real stores it, or None for one that takes the default (`default_title`).
    Measured 2026-11-03: `a\r\nb` and `a\n\nb` were stored `a b` and `trail\n` was `trail `,
    while a tab, U+0000, U+001F, U+007F, U+00A0 and U+3000 were kept; `\t`, `\n`, U+0000, U+001F,
    U+0085 and ` \n ` alone each took the default, while U+00A0 alone did not."""
    stored = _TITLE_BREAKS.sub(" ", title)
    return stored if stored.strip(_TITLE_BLANK) else None


def from_prose(text: str) -> dict:
    """A document that states no structure: its one tab, `t.0`, holding `from_text`."""
    return {"tabs": [new_tab("t.0", "Tab 1", from_text(text), untitled=True)]}


def walk(tabs: list[dict], parent: dict | None = None, depth: int = 0):
    """Each tab, depth first as real lists them, with its parent and depth."""
    for tab in tabs:
        yield tab, parent, depth
        yield from walk(tab["childTabs"], tab, depth + 1)


def default_title(titles: Iterable[str], count: int) -> str:
    """The title real gives a tab that states none: ``Tab n`` for the ``count`` tabs the Doc then
    has, n climbing past each title already taken. Measured 2026-11-02: a Doc's eighth tab was
    `Tab 8`, and with `Tab 7` and `Tab 9` taken the next three were `Tab 8`, `Tab 10` and
    `Tab 11`; `TAB 7` took nothing."""
    taken = set(titles)
    while f"Tab {count}" in taken:
        count += 1
    return f"Tab {count}"


def from_record(file_id: str, rec: dict) -> dict:
    """A corpus record's `tabs`, or its `paragraphs`, `header` and `footer` as its one tab."""
    stated = rec.get("tabs") or [
        {k: rec[k] for k in ("paragraphs", "header", "footer") if rec.get(k) is not None}
    ]
    titles = [t["title"] for t in _stated_tabs(stated) if t.get("title")]
    built: list[str] = []

    def build(tab: dict, n: int) -> dict:
        seed = "" if n == 0 else f"t{n}."
        tid = "t.0" if n == 0 else tab_id(file_id, str(n))
        # as `addDocumentTab` takes a title, one taken once rewritten getting the default
        title = tab_title(tab.get("title") or "")
        untitled = n == 0 and not tab.get("title") and len(list(_stated_tabs(stated))) == 1
        if title is None or title in built:
            title = default_title(titles, n + 1)
        titles.append(title)
        built.append(title)
        notes: dict = {}
        body = from_paragraphs(file_id, tab["paragraphs"], seed, notes)
        out = new_tab(tid, title, body, untitled=untitled)
        if tab.get("icon"):
            out["tabProperties"]["iconEmoji"] = tab["icon"]
        for kind in ("header", "footer"):
            if tab.get(kind) is not None:
                sid = segment_id(file_id, f"{seed}{kind}")
                out[f"{kind}s"][sid] = _stated(file_id, tab[kind], f"{seed}{kind}.", None)
        out["footnotes"] = notes
        return out

    ordinals = itertools.count()

    def tree(tabs: list[dict]) -> list[dict]:
        out = []
        for tab in tabs:
            built = build(tab, next(ordinals))
            built["childTabs"] = tree(tab.get("tabs") or [])
            out.append(built)
        return out

    tabs = tree(stated)
    return {"tabs": tabs, **({"tabbed": True} if len(list(walk(tabs))) > 1 else {})}


def _stated_tabs(tabs: list[dict]):
    for tab in tabs:
        yield tab
        yield from _stated_tabs(tab.get("tabs") or [])


def record_text(rec: dict) -> str:
    """A corpus record's text as Drive's search reads it (`document_text`)."""
    return document_text(from_record("", rec))


def document_text(doc: dict) -> str:
    """A stored document's text as Drive's search reads it: each tab's title, but the default of a
    lone tab in a Doc that never held more than one, then its body, headers, footers and footnotes,
    depth first, one line per paragraph, a segment holding no text giving none. Measured 2026-11-03:
    `fullText contains` found a word only in a first or second tab's header, footer or footnote or a
    child tab's header or footer, in an added or a renamed tab's title and not the old one, and in a
    lone tab's title once renamed, and `Tab` in a Doc of several tabs, one untitled, and in one
    whose tabs were deleted back to one, but not in a Doc that only ever had one untitled tab; and a
    footnote reference or a page break parted the words either side of it as a line break does
    (`routers.google._in_a_line`)."""
    tabs = [tab for tab, *_ in walk(doc["tabs"])]
    lines = []
    for tab in tabs:
        title = tab["tabProperties"]["title"]
        if doc.get("tabbed") or title != default_title((), 1):
            lines.append(title)
        for segment in (
            tab["body"],
            *tab["headers"].values(),
            *tab["footers"].values(),
            *tab["footnotes"].values(),
        ):
            text = "".join(
                pe["textRun"]["content"] if "textRun" in pe else "\n"
                for el in segment
                if "paragraph" in el
                for pe in el["paragraph"]["elements"]
            )[:-1]
            if text.strip():
                lines.append(text)
    return "\n".join(lines)


def footnote_numbers(tab: dict) -> dict[str, str]:
    """Each footnote's `footnoteNumber`, by its reference's place in the tab's body: measured
    2026-11-02, a footnote made before another's reference became `1` and the other `2`, and a
    second tab's first footnote was `1`."""
    refs = [
        pe["footnoteReference"]["footnoteId"]
        for el in tab["body"]
        if "paragraph" in el
        for pe in el["paragraph"]["elements"]
        if "footnoteReference" in pe
    ]
    return {fid: str(n) for n, fid in enumerate(refs, 1)}


def indexed(content: list[dict], numbers: dict[str, str] | None = None) -> list[dict]:
    """Stored content as real serves it, each structural element and paragraph element carrying
    its `startIndex` and `endIndex` in UTF-16 code units, those two first, and a footnote
    reference its `footnoteNumber`. Content opens at 0, a body with its section break and a header,
    footer or footnote with its first paragraph, and a zero `startIndex` is left out, as proto3
    leaves out every zero."""
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
            if "textRun" in pe:
                end = at + utf16_len(pe["textRun"]["content"])
            else:
                end = at + 1
            if "footnoteReference" in pe:
                ref = pe["footnoteReference"]
                pe = {
                    "footnoteReference": {
                        "footnoteId": ref["footnoteId"],
                        "footnoteNumber": (numbers or {}).get(ref["footnoteId"], ""),
                        "textStyle": ref["textStyle"],
                    }
                }
            elements.append(_span(at, end) | pe)
            at = end
        paragraph = {
            "elements": elements,
            **{k: v for k, v in el["paragraph"].items() if k != "elements"},
        }
        out.append(_span(start, at) | {"paragraph": paragraph})
    return out


def document_tab(tab: dict) -> dict:
    """A stored tab as real serves its `documentTab`, and as a Doc's flat members when it is the
    first tab: the body, then the header, footer and footnotes it has, then its styles (measured
    2026-11-02)."""
    numbers = footnote_numbers(tab)
    out: dict = {"body": {"content": indexed(tab["body"], numbers)}}
    for kind, key in (
        ("headers", "headerId"),
        ("footers", "footerId"),
        ("footnotes", "footnoteId"),
    ):
        if tab[kind]:
            out[kind] = {sid: {key: sid, "content": indexed(c)} for sid, c in tab[kind].items()}
    out["documentStyle"] = document_style(
        next(iter(tab["headers"]), None), next(iter(tab["footers"]), None)
    )
    out["namedStyles"] = named_styles()
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
# A letter whose canonical decomposition starts with a base letter compares as that letter, and
# 104 others as the table says (`ø` as `o`, `æ` as `ae`, `ß` as `ss`, `×` as `x`); `ﬁ`, `ẞ`,
# fullwidth, Greek and Cyrillic letters, and every character outside the table, compare as
# themselves.
_FOLD: dict[str, str] = json.loads(
    (Path(__file__).parent / "data" / "gdocs_fold.json").read_text(encoding="utf-8")
)


def _fold(text: str) -> tuple[str, list[tuple[int, int]]]:
    """``text`` as `replaceAllText` compares it, with the ``[start, end)`` in ``text`` of each
    character of the result. A combining mark folds into the character before it, whatever that
    is: measured, `e` and U+0301 were replaced as one by a match of `e`, as were `ж`, `α`, `1` and
    `日` with it, and a search for U+0301 alone matched nothing. A mark that starts the text moves
    every character after it on by one: measured on three Docs, a match of `a` in U+0301 `ab`
    replaced the `b`."""
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    shift = 0
    for pos, c in enumerate(text):
        if unicodedata.category(c) == "Mn":
            if not spans:
                shift += 1
                continue
            start = spans[-1][0]
            n = len(_FOLD.get(text[start], text[start]))
            spans[-n:] = [(start, pos + 1)] * n
            continue
        folded = _FOLD.get(c, c)
        out.append(folded)
        at = min(pos + shift, len(text) - 1)
        spans += [(at, at + 1)] * len(folded)
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
    measured, `s` matched `ſ`, `k` the Kelvin sign and `i` the `I` that `İ` folds to."""
    upper = c.upper() if len(c.upper()) == 1 else c
    lower = upper.lower()
    return lower if len(lower) == 1 else upper


# `searchByRegex` reads a pattern as `java.util.regex` does, with `MULTILINE` and `UNIX_LINES`,
# and `CASE_INSENSITIVE` and `UNICODE_CASE` without `matchCase`: measured on 200 patterns. These
# are the classes Java reads in ASCII unless a `(?U)` asks otherwise, and whatever the case flags
# say: measured, without `matchCase` `\w` and `\p{Alpha}` took neither `ſ` nor the Kelvin sign.
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
# Java's property names past a general category, each as the members of a class: an `Is` binary
# property (its name in any case, with or without `_`), a `java` method of `Character`, and `all`,
# `L1` and `LD`.
# Measured: `\p{IsLetter}`, `\p{IsWhiteSpace}`, `\p{javaLowerCase}` and `\p{javaWhitespace}`
# took Java's characters, and `\p{isletter}`, `\p{Latin}` and `\p{Lowercase}` matched nothing.
_JAVA_CATEGORIES = frozenset(
    "L Lu Ll Lt Lm Lo LC M Mn Mc Me N Nd Nl No P Pc Pd Ps Pe Pi Pf Po S Sm Sc Sk So Z Zs Zl Zp C "
    "Cc Cf Co Cs Cn".split()
)
_JAVA_BINARY = {
    "ALPHABETIC": "\\p{Alphabetic}",
    "IDEOGRAPHIC": "\\p{Ideographic}",
    "LETTER": "\\p{L}",
    "LOWERCASE": "\\p{Lowercase}",
    "UPPERCASE": "\\p{Uppercase}",
    "TITLECASE": "\\p{Lt}",
    "PUNCTUATION": "\\p{P}",
    "CONTROL": "\\p{Cc}",
    "WHITESPACE": "\\p{White_Space}",
    "DIGIT": "\\p{Nd}",
    "HEXDIGIT": "\\p{Hex_Digit}",
    "JOINCONTROL": "\\p{Join_Control}",
    "NONCHARACTERCODEPOINT": "\\p{Noncharacter_Code_Point}",
    "ASSIGNED": "\\p{Assigned}",
    "EMOJI": "\\p{Emoji}",
    "EMOJIPRESENTATION": "\\p{Emoji_Presentation}",
    "EMOJIMODIFIER": "\\p{Emoji_Modifier}",
    "EMOJIMODIFIERBASE": "\\p{Emoji_Modifier_Base}",
    "EMOJICOMPONENT": "\\p{Emoji_Component}",
    "EXTENDEDPICTOGRAPHIC": "\\p{Extended_Pictographic}",
}
_JAVA_METHODS = {
    "javaLowerCase": "\\p{Lowercase}",
    "javaUpperCase": "\\p{Uppercase}",
    "javaTitleCase": "\\p{Lt}",
    "javaDigit": "\\p{Nd}",
    "javaDefined": "\\p{Assigned}",
    "javaLetter": "\\p{L}",
    "javaLetterOrDigit": "\\p{L}\\p{Nd}",
    "javaAlphabetic": "\\p{Alphabetic}",
    "javaIdeographic": "\\p{Ideographic}",
    "javaSpaceChar": "\\p{Z}",
    "javaWhitespace": "[\\t\\n\\x0b\\f\\r\\x1c-\\x1f\\p{Z}]--[\\xa0\\u2007\\u202f]",
    "javaISOControl": "\\x00-\\x1f\\x7f-\\x9f",
    "javaMirrored": "\\p{Bidi_Mirrored}",
    "javaJavaIdentifierStart": "\\p{L}\\p{Nl}\\p{Sc}\\p{Pc}",
    "javaJavaIdentifierPart": (
        "\\p{L}\\p{Nl}\\p{Sc}\\p{Pc}\\p{Nd}\\p{Mn}\\p{Mc}\\x00-\\x08\\x0e-\\x1b\\x7f-\\x9f\\p{Cf}"
    ),
    "javaUnicodeIdentifierStart": "\\p{ID_Start}",
    "javaUnicodeIdentifierPart": "\\p{ID_Continue}\\x00-\\x08\\x0e-\\x1b\\x7f-\\x9f\\p{Cf}",
    "javaIdentifierIgnorable": "\\x00-\\x08\\x0e-\\x1b\\x7f-\\x9f\\p{Cf}",
    "all": "\\x00-\\U0010ffff",
    "L1": "\\x00-\\xff",
    "LD": "\\p{L}\\p{Nd}",
}
# `\p{Lower}` and `\p{Upper}` under a case-insensitive flag take both ASCII cases, and never `ſ`
# or the Kelvin sign: measured, without `matchCase` `\p{Upper}` took `a` and `B`, and
# `\p{Lower}` neither `ſ` nor the Kelvin sign.
_JAVA_CASED = {
    "Lower": "(?:(?-i:[a-z])|(?=(?-i:[A-Z]))[a-z])",
    "Upper": "(?:(?-i:[A-Z])|(?=(?-i:[a-z]))[A-Z])",
}
_JAVA_KEYS = {
    "script": "Script",
    "sc": "Script",
    "block": "Block",
    "blk": "Block",
    "general_category": "General_Category",
    "gc": "General_Category",
}
# The letters Java reads after a backslash; any other is a pattern it refuses (measured, `\m`).
_JAVA_ESCAPES = frozenset("tnrfaecxu0123456789dDsSwWhHvVRXbBAGZzQEpPkN")
_JAVA_WORD = "(?-i:[a-zA-Z_0-9])"
_JAVA_BOUNDARY = f"(?:(?<={_JAVA_WORD})(?!{_JAVA_WORD})|(?<!{_JAVA_WORD})(?={_JAVA_WORD}))"
_JAVA_INSIDE = f"(?:(?<={_JAVA_WORD})(?={_JAVA_WORD})|(?<!{_JAVA_WORD})(?!{_JAVA_WORD}))"
_JAVA_FLAGS = regex.compile(r"\(\?([idmsuxU]*)(?:-([idmsuxU]*))?([:)])")
_JAVA_NAMED = regex.compile(r"\(\?<([a-zA-Z][a-zA-Z0-9]*)>")
_JAVA_COUNT = regex.compile(r"\{\d+(?:,\d*)?\}")
_JAVA_OCTAL = regex.compile(r"[0-3][0-7]{2}|[0-7]{1,2}")
# Measured: `\x{}`, `\x{zz}`, `\x{+41}` and `\x{110000}` matched nothing, `\x{10FFFF}` and
# `\x{0041}` compiled, and `\uD83D\uDE00` matched an emoji that `\uD83D` alone missed.
_JAVA_HEX = {
    "x": regex.compile(r"[0-9a-fA-F]{2}"),
    "u": regex.compile(r"[0-9a-fA-F]{4}"),
    "{": regex.compile(r"\{([0-9a-fA-F]+)\}"),
}
_JAVA_LOW = regex.compile(r"\\u([dD][c-fC-F][0-9a-fA-F]{2})")


def _java_property(name: str, ascii_classes: bool) -> str | None:
    """The members of the class Java's ``\\p{name}`` names, or None for a name it refuses."""
    if ascii_classes and name in _JAVA_POSIX:
        return _JAVA_POSIX[name]
    if name in _JAVA_CATEGORIES or name in _JAVA_POSIX:
        return f"\\p{{{name}}}"
    if name in _JAVA_METHODS:
        return _JAVA_METHODS[name]
    key, _, value = name.partition("=")
    if value:
        return f"\\p{{{_JAVA_KEYS[key.lower()]}={value}}}" if key.lower() in _JAVA_KEYS else None
    if name.startswith("Is"):
        rest = name[2:]
        binary = _JAVA_BINARY.get(rest.upper().replace("_", "").replace(" ", ""))
        return binary or (f"\\p{{{rest}}}" if rest in _JAVA_CATEGORIES else f"\\p{{Script={rest}}}")
    if name.startswith("In"):
        return f"\\p{{Block={name[2:]}}}"
    return None


def _java_pattern(java: str, match_case: bool) -> str | None:
    """A `java.util.regex` pattern as the `regex` module reads it in its version-1 syntax, or None
    for one Java does not compile. With ``match_case``, a `(?i)` with no `u` or `U` flag beside it
    folds ASCII's case alone, so a non-ASCII letter outside a class keeps its case, written as
    itself or as an escape: measured, `(?i)ж`, `(?i)\\x{436}` and `(?i)\\Qж\\E` missed `Ж`,
    `(?iu)ж` matched it, and `(?i)\\p{Lu}` took `a` and `Б`. Measured too: `{,2}`, `(?#c)`, `(?R)`,
    `(?1)`, `(?&n)`, `(?P<n>`, `(?|`, `(?(1)`, `\\K`, `\\m` and a group name used twice matched
    nothing, `[[:alpha:]]` matched `:`, `\\0101` matched `A`, and under `(?x)` a class drops its
    spaces and `[#a]` is a comment left open."""
    named = [m[1] for m in _JAVA_FLAGS.finditer(java)]
    ascii_classes = not any("U" in n for n in named)
    ascii_case = (
        match_case and any("i" in n for n in named) and not any("u" in n or "U" in n for n in named)
    )
    groups = [m[1] for m in _JAVA_NAMED.finditer(java)]
    if len(groups) != len(set(groups)):
        return None
    out: list[str] = []
    depth = 0
    spaced = False

    def literal(ch: str) -> str:
        if ascii_case and not depth and not ch.isascii() and ch.lower() != ch.upper():
            return f"(?-i:{regex.escape(ch)})"
        return regex.escape(ch)

    i = 0
    while i < len(java):
        c = java[i]
        if c == "\\":
            if i + 1 >= len(java):
                return None
            e = java[i + 1]
            i += 2
            if e.isalpha() and e not in _JAVA_ESCAPES:
                return None
            if e == "Q":
                end = java.find("\\E", i)
                quoted = java[i:] if end < 0 else java[i:end]
                i = len(java) if end < 0 else end + 2
                out.append("".join(map(literal, quoted)))
            elif e in "hHvV" or (ascii_classes and e in "dDsSwW"):
                members = _JAVA_CLASSES[e.lower()]
                made = f"[{'^' if e.isupper() else ''}{members}]"
                out.append(made if depth else f"(?-i:{made})")
            elif e == "R" and not depth:
                out.append(f"(?:\\r\\n|[{_JAVA_CLASSES['v']}])")
            elif e in "bB" and ascii_classes and not depth:
                out.append(_JAVA_BOUNDARY if e == "b" else _JAVA_INSIDE)
            elif e == "z" and not depth:
                out.append("\\Z")
            elif e == "Z" and not depth:
                out.append("(?=\\n?\\Z)")
            elif e == "0":
                digits = _JAVA_OCTAL.match(java, i)
                if not digits:
                    return None
                out.append(literal(chr(int(digits[0], 8))))
                i = digits.end()
            elif e == "x" and java[i : i + 1] == "{":
                digits = _JAVA_HEX["{"].match(java, i)
                if not digits or int(digits[1], 16) > 0x10FFFF:
                    return None
                out.append(literal(chr(int(digits[1], 16))))
                i = digits.end()
            elif e in "xu":
                digits = _JAVA_HEX[e].match(java, i)
                if not digits:
                    return None
                code = int(digits[0], 16)
                i = digits.end()
                low = _JAVA_LOW.match(java, i) if 0xD800 <= code < 0xDC00 else None
                if low:
                    code = 0x10000 + (code - 0xD800) * 0x400 + int(low[1], 16) - 0xDC00
                    i = low.end()
                out.append(literal(chr(code)))
            elif e == "c" and i < len(java):
                out.append(literal(chr(ord(java[i]) ^ 64)))
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
                i = end + 1
                if e == "N":
                    try:
                        out.append(literal(unicodedata.lookup(name)))
                    except KeyError:
                        return None
                    continue
                members = _java_property(name, ascii_classes)
                if members is None:
                    return None
                made = f"[{'^' if e == 'P' else ''}{members}]"
                if not depth and ascii_classes and name in _JAVA_CASED:
                    cased = _JAVA_CASED[name]
                    made = cased if e == "p" else f"(?:(?!{cased})[\\s\\S])"
                elif not depth and ascii_classes and name in _JAVA_POSIX:
                    made = f"(?-i:{made})"
                out.append(made)
            else:
                out.append("\\" + e)
            continue
        if depth:
            if spaced and c in " \t\n\x0b\f\r":
                i += 1
                continue
            if spaced and c == "#":
                end = java.find("\n", i)
                i = len(java) if end < 0 else end + 1
                continue
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
        if c == "(" and (flags := _JAVA_FLAGS.match(java, i)):
            spaced = ("x" in flags[1] or spaced) and "x" not in (flags[2] or "")
            on = flags[1].translate(str.maketrans("", "", "duU"))
            off = (flags[2] or "").translate(str.maketrans("", "", "duU"))
            group = on + (f"-{off}" if off else "")
            if group or flags[3] == ":":
                out.append(f"(?{group}{flags[3]}")
            i = flags.end()
            continue
        if c == "(" and java.startswith("(?", i):
            after = java[i + 2 : i + 4]
            if not (
                after[:1] in ("=", "!", ">") or after in ("<=", "<!") or _JAVA_NAMED.match(java, i)
            ):
                return None
        if c == "{" and not _JAVA_COUNT.match(java, i):
            return None
        out.append(f"(?-i:{c})" if ascii_case and not c.isascii() and c.lower() != c.upper() else c)
        i += 1
    return "".join(out)


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
    run is served per paragraph and a later delete of that newline joins what follows it anew. A
    footnote reference or page break is never one side of a join; once one is deleted, the runs
    either side join (measured 2026-11-02)."""
    if left[0] in _ELEMENTS:
        return
    right = None
    for chunk, begin in chunks:
        for i in range(begin, len(chunk)):
            c, style, run = chunk[i]
            if c in _ELEMENTS:
                return
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


class Body:
    """A body, header, footer or footnote opened for a write: each character with the `TextStyle`
    of the run it sits in and that run, and each paragraph's `paragraphStyle`, every paragraph
    ending at a newline. A footnote reference is the character `FOOTNOTE` and a page break
    `PAGE_BREAK`, and :attr:`footnotes` holds the footnote of each reference in order. A write
    changes it in memory, and :meth:`content` is what is stored back.

    A run is kept apart from its neighbour of the same style until a delete brings the two
    together (`_run_at` says when an insert starts one): measured, deleting a bold run joined the
    unstyled runs either side of it.

    Positions here count characters; :meth:`position` turns a request's index, in UTF-16 code
    units, into one. A body opens with its section break, at index 0, and its first character
    is at 1; a header, footer or footnote has none, and its first character is at 0. The checks a
    request's refusal names are the router's (`routers.google_docs`), which asks :meth:`position`
    and :meth:`splits_cluster`."""

    def __init__(self, file_id: str, content: list[dict], revision: int):
        self.file_id = file_id
        self.revision = revision
        self._section = content[0] if "sectionBreak" in content[0] else None
        self.base = 1 if self._section else 0
        self._runs = itertools.count()
        self.chars: list[tuple[str, dict, int]] = []
        self.paragraphs: list[dict] = []
        self.footnotes: list[str] = []
        for el in content[self.base :]:
            for pe in el["paragraph"]["elements"]:
                n = next(self._runs)
                if "textRun" in pe:
                    run = pe["textRun"]
                    self.chars += [(c, run.get("textStyle", {}), n) for c in run["content"]]
                elif "footnoteReference" in pe:
                    self.chars.append((FOOTNOTE, pe["footnoteReference"]["textStyle"], n))
                    self.footnotes.append(pe["footnoteReference"]["footnoteId"])
                else:
                    self.chars.append((PAGE_BREAK, pe["pageBreak"]["textStyle"], n))
            self.paragraphs.append(el["paragraph"].get("paragraphStyle", {}))
        self._split = 0

    @property
    def end(self) -> int:
        """The end index: one past the last newline."""
        return self.base + sum(utf16_len(c) for c, *_ in self.chars)

    def position(self, index: int) -> int | None:
        """The character an index falls before, or None when it falls between the two halves
        of a surrogate pair. ``index`` is at least :attr:`base` and at most :attr:`end`."""
        at = self.base
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
        joined the run after it. A footnote reference or page break is no run to join: measured
        2026-11-02, text inserted right after a reference joined the run after it, and right
        before one the run before it."""
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
        if not at_start and before is not None and before[0] not in _ELEMENTS:
            if before[1] == style:
                return before[2]
        if after[0] not in _ELEMENTS and after[1] == style:
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

    def insert_footnote(self, pos: int, footnote_id: str) -> None:
        """A reference to ``footnote_id`` before the character at ``pos``, in the style text
        inserted there takes: measured 2026-11-02, a reference made inside a bold run, and right
        after one, was bold, and one after an unstyled space was not."""
        self.footnotes.insert(self._footnotes_before(pos), footnote_id)
        self.insert(pos, FOOTNOTE, self._style_at(pos, FOOTNOTE))

    def _footnotes_before(self, pos: int) -> int:
        return sum(1 for c, *_ in self.chars[:pos] if c == FOOTNOTE)

    def _drop_footnotes(self, spans: list[tuple[int, int]]) -> None:
        """Forget the footnote of each reference inside ``spans``, which a write is about to
        remove: measured 2026-11-02, a reference deleted or replaced took its footnote with it."""
        kept, k = [], 0
        for pos, (c, *_) in enumerate(self.chars):
            if c != FOOTNOTE:
                continue
            if not any(start <= pos < end for start, end in spans):
                kept.append(self.footnotes[k])
            k += 1
        self.footnotes = kept

    def delete(self, start: int, end: int) -> None:
        """The characters in ``[start, end)``. The paragraphs a deleted newline joins take the first
        one's style, or, when the range starts at that paragraph's start, the last one's. Measured
        on ranges from inside and from the start of a heading into the paragraph after it, and on
        whole paragraphs."""
        first = self._paragraph_of(start)
        joined = sum(1 for c, *_ in self.chars[start:end] if c == "\n")
        keep = first + joined if self._starts_paragraph(start) else first
        self._drop_footnotes([(start, end)])
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
        `ßs` was three matches. A regular expression reads as `_java_pattern` says, and one that
        does not compile matches nothing; an empty match takes the character it stands before,
        measured on `z*`, which replaced every character of a Doc, and the search goes on one
        character after it, as Java's does: measured, `a??` in `aa` was three matches. A search
        that folds to nothing matches nothing."""
        folded, spans = _fold("".join(c for c, *_ in self.chars))
        needle = _fold(text)[0]
        found: list[tuple[int, int]] = []
        if not needle:
            return []
        if regex:
            java = _java_pattern(needle, match_case)
            flags = _REGEX_FLAGS | (0 if match_case else _REGEX_CASELESS)
            try:
                pattern = None if java is None else _REGEX.compile("(?-f)" + java, flags)
            except _REGEX.error:
                pattern = None
            at = 0
            while pattern and at <= len(folded) and (m := pattern.search(folded, at)):
                if m.start() < len(folded):
                    found.append((m.start(), max(m.end(), m.start() + 1)))
                at = m.end() + (m.end() == m.start())
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
        last = len(self.chars) - 1
        self._drop_footnotes([(start, min(end, last)) for start, end in matches])
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

    def content(self) -> list[dict]:
        """What is stored back, one `textRun` per run in each paragraph and an element of its own
        for each footnote reference and page break."""
        out = [self._section] if self._section else []
        elements: list[dict] = []
        notes = iter(self.footnotes)
        held = None  # the run the last text element is
        p = 0
        for c, style, run in self.chars:
            if c == FOOTNOTE:
                ref = {"footnoteId": next(notes), "textStyle": style}
                elements.append({"footnoteReference": ref})
                held = None
            elif c == PAGE_BREAK:
                elements.append({"pageBreak": {"textStyle": style}})
                held = None
            elif held == run:
                elements[-1]["textRun"]["content"] += c
            else:
                elements.append({"textRun": {"content": c, "textStyle": style}})
                held = run
            if c == "\n":
                out.append(
                    {"paragraph": {"elements": elements, "paragraphStyle": self.paragraphs[p]}}
                )
                elements, held, p = [], None, p + 1
        return out


class Document:
    """A stored document opened for a write: its tab tree, and each body, header, footer or
    footnote a request reaches opened as a :class:`Body` once and kept open until :meth:`stored`
    writes them back."""

    def __init__(self, file_id: str, stored: dict, revision: int):
        self.file_id = file_id
        self.revision = revision
        self.tabs: list[dict] = copy.deepcopy(stored["tabs"])
        # whether the Doc has held more than one tab, which keeps a lone tab's default title in
        # what Drive's search reads (`document_text`)
        self.tabbed = stored.get("tabbed", False)
        self._open: dict[tuple[str, str], Body] = {}
        self._made = itertools.count()
        # the requests `routers.google_docs.docs_batch_update` counts as no write when they are all
        # a batch holds
        self.unchanged = 0

    def flat(self) -> list[dict]:
        return [tab for tab, *_ in walk(self.tabs)]

    def tab(self, tab_id: str | None) -> dict | None:
        """The tab ``tab_id`` names, the first tab for none (measured 2026-11-02: a location with
        no or an empty `tabId` wrote the first tab, whatever its id)."""
        if not tab_id:
            return self.tabs[0]
        return next((t for t in self.flat() if t["tabProperties"]["tabId"] == tab_id), None)

    def kind(self, tab: dict, segment: str) -> str | None:
        """Whether ``segment`` names the tab's ``header``, ``footer`` or a ``footnote``, ``body``
        for the empty id, or None for none of them."""
        if not segment:
            return "body"
        for kind in ("header", "footer", "footnote"):
            if segment in tab[f"{kind}s"]:
                return kind
        return None

    def open(self, tab: dict, segment: str = "") -> Body:
        key = (tab["tabProperties"]["tabId"], segment)
        if key not in self._open:
            kind = self.kind(tab, segment)
            content = tab["body"] if kind == "body" else tab[f"{kind}s"][segment]
            self._open[key] = Body(self.file_id, content, self.revision)
        return self._open[key]

    def segments(self, tab: dict) -> list[Body]:
        """The tab's body, header, footer and footnotes, each opened."""
        ids = [""] + [sid for kind in ("headers", "footers", "footnotes") for sid in tab[kind]]
        return [self.open(tab, sid) for sid in ids]

    def settle(self) -> None:
        """Drop each footnote whose reference a write removed, so a later request in the batch no
        longer finds it."""
        for tab in self.flat():
            key = (tab["tabProperties"]["tabId"], "")
            if key not in self._open:
                continue
            kept = set(self._open[key].footnotes)
            for fid in [f for f in tab["footnotes"] if f not in kept]:
                del tab["footnotes"][fid]
                self._open.pop((key[0], fid), None)

    def new_id(self, make) -> str:
        """An id ``make`` draws from this write, one no tab, header, footer or footnote has."""
        taken = {t["tabProperties"]["tabId"] for t in self.flat()}
        taken |= {s for t in self.flat() for k in ("headers", "footers", "footnotes") for s in t[k]}
        while (made := make(self.file_id, f"r{self.revision}.{next(self._made)}")) in taken:
            pass
        return made

    def parent(self, tab: dict) -> dict | None:
        return next((p for t, p, _ in walk(self.tabs) if t is tab), None)

    def siblings(self, parent: dict | None) -> list[dict]:
        return parent["childTabs"] if parent else self.tabs

    def place(self, tab: dict, parent: dict | None, index: int | None, new: bool = False) -> None:
        """Put ``tab`` under ``parent`` (None for the root) before the sibling now at ``index``,
        or last. Measured 2026-11-02: a root tab moved from 0 to 4 among four became the last and
        from 1 to 2 stayed where it was."""
        siblings = self.siblings(parent)
        if not new:
            old = self.siblings(self.parent(tab))
            at = next(i for i, t in enumerate(old) if t is tab)
            old.pop(at)
            if old is siblings and index is not None and index > at:
                index -= 1
        siblings.insert(len(siblings) if index is None else min(index, len(siblings)), tab)

    def remove(self, tab: dict) -> None:
        """Take ``tab`` out of the tree, its child tabs with it."""
        gone = {t["tabProperties"]["tabId"] for t, *_ in walk([tab])}
        siblings = self.siblings(self.parent(tab))
        siblings.pop(next(i for i, t in enumerate(siblings) if t is tab))
        for key in [k for k in self._open if k[0] in gone]:
            del self._open[key]

    def stored(self) -> dict:
        for tab in self.flat():
            for (tid, sid), body in self._open.items():
                if tid != tab["tabProperties"]["tabId"]:
                    continue
                kind = self.kind(tab, sid)
                if kind == "body":
                    tab["body"] = body.content()
                elif kind is not None:
                    tab[f"{kind}s"][sid] = body.content()
        tabbed = self.tabbed or len(self.flat()) > 1
        return {"tabs": self.tabs, **({"tabbed": True} if tabbed else {})}


# --- a document as Drive's `files.export` renders it ----------------------------------------------
#
# Measured against Drive v3 from 2026-10-30 to 2026-11-03, as a Workspace member exporting Docs it
# wrote through `documents.batchUpdate`, each holding only what a corpus may state.

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


def _paragraph_runs(paragraph: dict, note=None) -> list[tuple[str, dict]]:
    """A paragraph's runs without the newline that ends it, a page break as `PAGE_BREAK` and a
    footnote reference as ``note`` writes its footnote's id, or as `FOOTNOTE`."""
    runs = []
    for pe in paragraph["elements"]:
        if "textRun" in pe:
            runs.append((pe["textRun"]["content"], pe["textRun"].get("textStyle", {})))
        elif "footnoteReference" in pe:
            ref = pe["footnoteReference"]
            runs.append((note(ref["footnoteId"]) if note else FOOTNOTE, ref["textStyle"]))
        else:
            runs.append((PAGE_BREAK, pe["pageBreak"]["textStyle"]))
    text, style = runs[-1]
    runs[-1] = (text.removesuffix("\n"), style)
    return [r for r in runs if r[0]]


def _spans(paragraph: dict) -> list[tuple[str, dict]]:
    """A paragraph's runs with neighbours of one text style joined, its page breaks left out. A
    `documents.batchUpdate` insert beside a link leaves two such runs, and Markdown escapes them as
    one. A text style the paragraph's named style sets to the same value (HEADING_6's italic) is
    left out first, as real's body leaves it out: on 2026-11-01 an unstyled space and an italic tab
    in a HEADING_6 read back as one unstyled run."""
    named = _TEXT_STYLES.get(_named(paragraph), {})
    out: list[tuple[str, dict]] = []
    for text, style in _paragraph_runs(paragraph):
        text = text.replace(PAGE_BREAK, "")
        if not text:
            continue
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


def _lead_breaks(paragraph: dict) -> list[tuple[int, int]]:
    """The CRLFs a plain export adds for the runs a paragraph starts with that hold nothing but
    vertical tabs once `_after_break` drops the separators after them, as pairs: how many of the
    paragraph's vertical tabs they follow, and how many they are: one for each such run another run
    follows, and one for each such run that is a link. Real's answers on 2026-11-01 match it on
    paragraphs starting with one to three runs of vertical tabs, before text or alone, in
    NORMAL_TEXT, TITLE and SUBTITLE, and on ones starting with runs of a vertical tab and a space,
    U+3000, a tab, U+00A0 or two spaces, or a space and a vertical tab. A footnote reference and a
    page break are runs of their own, and a page break is no run that follows, measured 2026-11-03:
    a vertical tab then a reference of one text style took the CRLF of two runs, a vertical tab then
    a page break none, and a vertical-tab link then a page break its link's one, before the rule."""
    named = _TEXT_STYLES.get(_named(paragraph), {})
    runs: list[tuple[str, dict]] = []
    for text, style in _paragraph_runs(paragraph):
        style = {k: v for k, v in style.items() if named.get(k) != v}
        if runs and runs[-1][1] == style and {text, runs[-1][0]}.isdisjoint(_ELEMENTS):
            runs[-1] = (runs[-1][0] + text, style)
        else:
            runs.append((text, style))
    out, tabs = [], 0
    for i, (text, style) in enumerate(runs):
        if text == PAGE_BREAK:
            continue
        if _after_break(text).strip("\x0b"):
            break
        tabs += text.count("\x0b")
        follows = i + 1 < len(runs) and runs[i + 1][0] != PAGE_BREAK
        if follows or "link" in style:
            out.append((tabs, follows + ("link" in style)))
    return out


def _with_lead_breaks(text: str, leads: list[tuple[int, int]]) -> str:
    """``text`` with each of `_lead_breaks`' CRLFs after the vertical tab it counts."""
    out, tabs = "", 0
    for c in text:
        out += c
        tabs += c == "\x0b"
        while leads and leads[0][0] == tabs:
            out += "\x0b" * leads.pop(0)[1]
    return out


# What a plain export writes for a page break, on a line of its own, and before the footnotes.
_RULE = "_" * 16


def _page_breaks(text: str, bare: str, heading: bool) -> str:
    """A paragraph's text with each page break a `_RULE` line: measured 2026-11-02, `ab`, a page
    break and `cd` were `ab`, the rule and `cd` on three lines; a page break opening a paragraph
    starts no line before it, and one closing a paragraph that holds text ends none after it, while
    the last of a paragraph holding page breaks alone ends a line after it, which `_plain_lines`
    doubles outside a HEADING_n and drops in one; measured 2026-11-03, two page breaks alone were
    two rules with no line between them. A page break with nothing but vertical tabs between it and
    the paragraph's start or the page break before it starts no line either, in a HEADING_n, or
    outside one once text comes before those vertical tabs: measured 2026-11-03, a vertical tab, a
    page break and `cd` in a HEADING_2 were an empty line, the rule and `cd` on three lines, and
    `ab`, a page break, a vertical tab, a page break and `cd` in NORMAL_TEXT had one empty line
    between the two rules. Vertical tabs after the last page break of a paragraph holding text end
    the rule's line themselves: `ab`, a page break and a vertical tab were `ab` and the rule on two
    lines, and no third."""
    out = ""
    pieces = text.split(PAGE_BREAK)
    for i, piece in enumerate(pieces):
        if i:
            final = i == len(pieces) - 1
            tabs = pieces[i - 1] and not pieces[i - 1].strip("\x0b")
            start = out and not (tabs and (heading or "".join(pieces[:i]).strip("\x0b")))
            closed = final and not piece.strip("\x0b") and bare.strip("\x0b")
            end = (piece or (final and not bare)) and not closed
            out += ("\x0b" if start else "") + _RULE + ("\x0b" if end else "")
        out += piece
    return out


def _plain_lines(content: list[dict], note) -> list[str]:
    """Stored content as `files.export` writes it as `text/plain`, a line per paragraph. A vertical
    tab is a CRLF and drops the separator after it (`_after_break`), a tab is eight spaces, a page
    break is `_page_breaks`' and a footnote reference ``note``'s. A paragraph holding no text but
    vertical tabs or page breaks, or nothing, takes one CRLF more, and one other than a HEADING_n
    takes the CRLFs `_lead_breaks` counts; a HEADING_n holding nothing is left out, and one holding
    no text but vertical tabs or page breaks that ends with a CRLF is one fewer (measured
    2026-11-03: a page break alone in HEADING_1 to HEADING_6 was the rule alone, and a page break
    then a vertical tab in a HEADING_2 the rule and one CRLF). Real's bytes and these agree on every
    paragraph of up to three of text, a vertical tab, a vertical-tab link, a page break and a
    footnote reference, and of four without the link, in NORMAL_TEXT, HEADING_2 and TITLE, each
    between two paragraphs of text, measured 2026-11-03."""
    lines = []
    for el in content:
        if "paragraph" not in el:
            continue
        text = "".join(_after_break(t) for t, _ in _paragraph_runs(el["paragraph"], note))
        bare = text.replace(PAGE_BREAK, "")
        heading = _named(el["paragraph"]).startswith("HEADING_")
        if not heading:
            text = _with_lead_breaks(text, _lead_breaks(el["paragraph"]))
        if PAGE_BREAK in text:
            text = _page_breaks(text, bare, heading)
        if heading:
            if not text:
                continue
            if not bare.strip("\x0b"):
                text = text.removesuffix("\x0b")
        else:
            if not bare.strip("\x0b"):
                text += "\x0b"
        lines.append("".join(map(_plain_char, text)))
    return lines


def _export_parts(doc: dict) -> tuple[list[dict], list[dict], list[dict], dict, list]:
    """What an export renders of a document: every tab's body one after another, depth first, the
    first tab's header and footer, and the footnotes numbered across the tabs by their references,
    each footnote's id with its content. Measured 2026-11-02: a second tab's body followed the
    first's with no title, its header and footer were left out, and its first footnote was `[2]`
    after the first tab's `[1]`."""
    tabs = [tab for tab, *_ in walk(doc["tabs"])]
    body = [el for tab in tabs for el in tab["body"]]
    first = doc["tabs"][0]
    header = next(iter(first["headers"].values()), [])
    footer = next(iter(first["footers"].values()), [])
    notes = [
        (
            pe["footnoteReference"]["footnoteId"],
            tab["footnotes"][pe["footnoteReference"]["footnoteId"]],
        )
        for tab in tabs
        for el in tab["body"]
        if "paragraph" in el
        for pe in el["paragraph"]["elements"]
        if "footnoteReference" in pe
    ]
    numbers = {fid: n for n, (fid, _) in enumerate(notes, 1)}
    return body, header, footer, numbers, notes


def plain_text(doc: dict) -> str:
    """A document as `files.export` serves it as `text/plain`: a byte-order mark, then the first
    tab's header, every tab's body, the first tab's footer, and when there are footnotes a
    `_RULE` and each footnote after its number in brackets, the number its reference is written
    as too (`_plain_lines`), all joined by CRLF and trimmed at both ends. Measured 2026-11-02."""
    body, header, footer, numbers, notes = _export_parts(doc)

    def note(fid: str) -> str:
        return f"[{numbers[fid]}]"

    lines = _plain_lines(header, note) + _plain_lines(body, note) + _plain_lines(footer, note)
    if notes:
        lines.append(_RULE)
        lines += [
            f"[{n}]" + "\r\n".join(_plain_lines(c, note)) for n, (_, c) in enumerate(notes, 1)
        ]
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


def _escape(text: str, notes: Iterator[int]) -> list[str]:
    """Each character of one run as Markdown writes it, a footnote reference as `[^n]` with the
    next of ``notes``."""
    out = [f"[^{next(notes)}]" if c == FOOTNOTE else f"\\{c}" if c in _ALWAYS else c for c in text]
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


# Real's Markdown writer, read off its answers on 2026-10-31 and 2026-11-01 (Google documents none
# of it). Its delimiters do not nest: where marks end, the lowest-rank ending mark closes with every
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
    opener's anchors count the changes it stayed held across that opened marks or took two or more
    openers."""
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


def _inline(runs: list[tuple[str, dict]], named: dict, notes: Iterator[int], heading=False) -> list:
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
                stretches[-1].append((marks, piece, _escape(piece, notes)))
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


def markdown(doc: dict) -> str:
    """A document as `files.export` serves it as `text/markdown` or `text/x-markdown`: every tab's
    body one after another (`_export_parts`), and then each footnote as `[^n]: ` and its content,
    the lines after its first indented four spaces, each two apart by a blank line. Measured
    2026-11-02: a page break wrote nothing, a reference `[^n]` inside the marks around it, and no
    header or footer was written; measured 2026-11-03, a body ending in a blank line, after two
    or more empty paragraphs, took no second one before the first footnote."""
    body, _, _, _, notes = _export_parts(doc)
    refs = iter(range(1, len(notes) + 1))
    out = _markdown(body, refs)
    for n, (_, content) in enumerate(notes, 1):
        lines = _markdown(content, refs).split("\n")
        out = (
            (out.removesuffix("\n\n") if n == 1 else out)
            + f"\n\n[^{n}]: "
            + "\n".join([lines[0]] + [f"    {x}" if x else x for x in lines[1:]])
        )
    return out


def _markdown(content: list[dict], notes: Iterator[int]) -> str:
    """Stored content as Markdown: TITLE as `#`, SUBTITLE as `##` and HEADING_n as n `#`s, a
    heading's vertical tab a space; bold, italic, strikethrough and links in a paragraph's named
    style or its runs' as `**`, `*`, `~~` and `[text](url)`, underline as nothing. Nothing is
    trimmed, and nothing ends the last line."""
    paragraphs = [el["paragraph"] for el in content if "paragraph" in el]
    levels = [_MARKDOWN_LEVEL.get(_named(p)) for p in paragraphs]
    tokens: list = []
    for i, (paragraph, level) in enumerate(zip(paragraphs, levels)):
        if i:
            tokens.append(_BLOCK if level or levels[i - 1] else _BREAK)
        runs = _spans(paragraph)
        named = _TEXT_STYLES.get(_named(paragraph), {})
        if level:
            tokens.append("#" * level + " " + "".join(_inline(runs, named, notes, heading=True)))
            continue
        line = _inline(runs, named, notes)
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
