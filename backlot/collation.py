"""MEASUREMENT PROTOTYPE, not for merging as written: the collation ``orderBy=name_natural`` sorts
by, approximated with ``unicodedata`` alone, built to measure how close a stdlib-only key gets to
ICU's root collation at tertiary strength, numeric, over the name with its ASCII capitals lowered.

Against the ICU table this module replaced (``backlot/data/drive_collation.json`` at 8b5d00b9),
comparing every adjacent pair and random pairs: issue 593's eleven names in real's order; the
bundled corpus's 25,108 titles, 0 of 225,195 pairs apart; random Hangul, Greek and kana + Han
names, 0; random accented Latin names, 1.6% (the secondary weight of æ and œ); random Russian
names, 0.1% (й, which ICU weighs as its own letter). Of the eight folders
``test_drive_order_by_a_name_lists_a_folder_as_real_did`` replays, the first, fourth and sixth
match; the others hold Thai and Tibetan contractions and Cyrillic й."""

from __future__ import annotations

import functools
import unicodedata as ud

_ASCII_LOWER = {cp: cp + 32 for cp in range(ord("A"), ord("Z") + 1)}
# ICU root's order of the white space, punctuation and symbols a name is likely to hold, read off
# the ICU table by sorting each as a one-character name. Anything else of those categories sorts
# after these, by code point.
_VARIABLE = (
    "   \xa0 _‗-‐‑‒–—―,;:!¡?¿.․‥…·'‘’‚‛\"“”„‟«»()[]{}‖§¶@*/\\&#%†‡•‣‧`´^¯¨¸°©®"
    "←↚→↛↑↓↔↕↖↗↘↙↜↝↞↟+±÷×<=>¬|¦~¤¢$£¥€"
)
_VAR_RANK = {c: i for i, c in enumerate(_VARIABLE)}
# ICU root's secondary order of the common Latin accents; any other accent after these.
_ACCENTS = "̧̨́̀̆̂̌̊̈̋̃̇̄"
_ACC_RANK = {c: i + 1 for i, c in enumerate(_ACCENTS)}
# The Unicode Collation Algorithm's tertiary weights, by decomposition tag.
_TAG = {
    "<wide>": 1,
    "<compat>": 2,
    "<noBreak>": 2,
    "<font>": 3,
    "<circle>": 4,
    "<narrow>": 5,
    "<small>": 7,
    "<square>": 9,
    "<fraction>": 10,
    "<super>": 12,
    "<sub>": 13,
}
_WS = frozenset("\t\n\x0b\x0c\r\x85")
# Letters NFKD leaves whole that ICU weighs as two letters, apart at the tertiary level only.
_EXPAND = {"ß": "ss", "æ": "ae", "œ": "oe", "ĳ": "ij"}
# Letters NFKD leaves whole that ICU weighs as a base letter and an accent.
_STROKE = {"ø": "o", "ł": "l", "đ": "d", "ð": "d", "ħ": "h", "ŧ": "t", "ƀ": "b"}
# The blocks of marks ICU weighs as accents, at the secondary level; a mark of any other block, such
# as a Tibetan or Thai vowel sign, carries a primary of its own.
_ACCENT_BLOCKS = (
    (0x0300, 0x0370),
    (0x1AB0, 0x1B00),
    (0x1DC0, 0x1E00),
    (0x20D0, 0x2100),
    (0xFE20, 0xFE30),
    (0x3099, 0x309B),
)
_NUMBER_DIGITS = 254


def _tag(c: str) -> int:
    d = ud.decomposition(c)
    return _TAG.get(d.split()[0], 2) if d.startswith("<") else 0


def _is_accent(b: str) -> bool:
    return ud.category(b) in ("Mn", "Me") and any(lo <= ord(b) < hi for lo, hi in _ACCENT_BLOCKS)


def _primary(b: str) -> tuple[int, int]:
    """White space, then punctuation, symbols, currency, digits, and letters by code point."""
    cat = ud.category(b)
    if b in _WS or cat == "Zs":
        return 1, _VAR_RANK.get(b, 0) if b == " " else ord(b)
    if cat[0] == "P":
        return 2, _VAR_RANK.get(b, 100 + ord(b))
    if cat in ("Sk", "Sm", "So"):
        return 3, _VAR_RANK.get(b, 100 + ord(b))
    if cat == "Sc":
        return 4, _VAR_RANK.get(b, 100 + ord(b))
    if (v := ud.digit(b, None)) is not None:
        return 5, v
    return 6, ord(b.lower())


@functools.lru_cache(maxsize=1 << 16)
def sort_key(name: str) -> tuple[tuple, tuple, tuple]:
    """``name``'s place in the collation: every primary, then every secondary, then every
    tertiary."""
    p: list[tuple] = []
    s: list[int] = []
    t: list[int] = []
    chars = name.translate(_ASCII_LOWER)
    i = 0
    while i < len(chars):
        c = chars[i]
        if ud.category(c) == "Nd":  # a run of decimal digits compares by its value
            j = i
            while j < len(chars) and ud.category(chars[j]) == "Nd":
                j += 1
            run = "".join(str(ud.decimal(x)) for x in chars[i:j])
            at = 0
            while at < len(run):
                while at < len(run) - 1 and run[at] == "0":
                    at += 1
                num = run[at : at + _NUMBER_DIGITS]
                p.append((5, -1, len(num), num))
                s.append(0)
                t.append(0)
                at += len(num)
            i = j
            continue
        i += 1
        if ud.category(c) in ("Cf", "Cc") and c not in _WS:
            continue  # ignorable
        tag = _tag(c)
        if c in _EXPAND:
            c, tag = _EXPAND[c], 2
        elif c.lower() in _STROKE:
            base = _STROKE[c.lower()]
            c = (base.upper() if c != c.lower() else base) + "̸"
        if "ァ" <= c <= "ヶ":  # katakana ties hiragana at primary and secondary
            c, tag = chr(ord(c) - 0x60), tag + 1
        for b in ud.normalize("NFKD", c):
            if _is_accent(b):
                s.append(_ACC_RANK.get(b, 100 + ord(b)))
                continue
            p.append((*_primary(b), 0, ""))
            s.append(0)
            t.append(tag + (6 if b != b.lower() else 0))
    return tuple(p), tuple(s), tuple(t)
