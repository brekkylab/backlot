"""The collation Drive's ``orderBy=name_natural`` sorts an English-language account's names by:
ICU's root collation at tertiary strength, with numeric ordering on, over the name with its ASCII
capitals lowered, so ``A`` ties ``a`` where ``Ä`` sorts after ``ä`` and ``ⓐ`` after ``a``.

Measured against Drive v3 on 2026-10-20 and 2026-10-21, real collates by the language the caller's
Google account is set to. Two Korean-language accounts listed their 21,872 files in ICU's ``ko``
order, Hangul and Han ahead of Latin, apart from rows a page boundary repeated; one of them, set to
English, then listed its 228 files, and twelve folders of 58, 32, 6, 6, 16, 19, 9, 6, 366, 24, 3 and
11 names made for it, in the root order. Backlot collates as the English-language account did. Names
the collation ties, such as ``A`` and ``a``, keep the order ``backlot.routers.google._drive_sort``
gives ties.

:func:`sort_key` builds that order out of ``unicodedata`` and the tables below, each checked against
the root collation table ICU 78.3 implements, CLDR 48's ``allkeys_CLDR.txt``. It reads a name in
NFC, so names that are canonically equivalent tie. Where the tables stop, it sorts otherwise than
ICU, as in these:

- A letter sorts by its code point, where ICU orders some otherwise: Cyrillic ``ғ`` after ``г``,
  Greek ``ϝ`` after ``ε``, Latin ``ƀ`` after ``b``, and Han across its blocks, U+20000 between
  U+4E00 and U+3400.
- A mark outside ``_ACCENT_RANGES`` is weighed as a letter, where ICU weighs some as accents, such
  as Devanagari's anusvara, and an accent ``_ACCENTS`` does not list sorts after the listed ones,
  where ICU puts some among them, such as the kana voicing mark ahead of the short stroke.
- Punctuation or a symbol ``_VARIABLE`` does not list sorts after the listed ones of its kind,
  where ICU puts some among them, such as ``‰`` after ``%``.
- Two code points are weighed as one only where NFC or ``_JOINED`` joins them. ICU also joins a
  Tibetan vowel sign to an AA sign past another AA sign (U+0F71 U+0F71 U+0F72), and weighs marks
  written out of their canonical order as written, where NFC reorders them first.
- ``unicodedata`` is the running Python's, whose Unicode version moves with the release, and a
  character newer than that version sorts as a letter, by code point."""

from __future__ import annotations

import functools
import re
import unicodedata

# Real lowers the ASCII capitals of a name before it collates it, and no other.
_ASCII_LOWER = {cp: cp + 32 for cp in range(ord("A"), ord("Z") + 1)}
# ICU reads at most 254 digits of a run as one number and the rest as the next.
_NUMBER_DIGITS = 254
# Marks the table ignores wholly: the combining grapheme joiner, Hebrew cantillation, meteg and
# upper and lower dots, the Arabic Quranic marks, some of the combining half marks, and the
# variation selectors.
_IGNORED = re.compile(
    "[\u034f\u0591-\u05af\u05bd\u05c4\u05c5\u0610-\u061a"
    "\u06d6-\u06dc\u06df-\u06e4\u06e7\u06e8\u06ea-\u06ed"
    "\ufe00-\ufe0f\ufe21\ufe23-\ufe26\ufe28\ufe2a-\ufe2d\ufe2f\U000e0100-\U000e01ef]"
)

# Code points the table weighs as the one code point they spell, which NFC does not compose: Thai
# and Lao SARA AM written as NIKHAHIT and SARA AA, and Tibetan vowel signs written in parts.
_JOINED = {
    "\u0fb2\u0f71\u0f80": "\u0f77",
    "\u0fb3\u0f71\u0f80": "\u0f79",
    "\u0e4d\u0e32": "\u0e33",
    "\u0ecd\u0eb2": "\u0eb3",
    "\u0f71\u0f72": "\u0f73",
    "\u0f71\u0f74": "\u0f75",
    "\u0f71\u0f80": "\u0f81",
    "\u0fb2\u0f80": "\u0f76",
    "\u0fb3\u0f80": "\u0f78",
}
_JOIN = re.compile("|".join(_JOINED))
# Unicode's Logical_Order_Exception property (PropList.txt): the Thai, Lao, New Tai Lue and Tai
# Viet vowels written ahead of the consonant they follow. The table weighs each, with a consonant of
# its script after it, as that consonant and then the vowel.
_PREVOWEL = re.compile(
    "[\u0e40-\u0e44][\u0e01-\u0e2e]"
    "|[\u0ec0-\u0ec4][\u0e81\u0e82\u0e84\u0e86-\u0e8a\u0e8c-\u0ea3\u0ea5\u0ea7-\u0eae\u0edc-\u0edf]"
    "|[\u19b5-\u19b7\u19ba][\u1980-\u19ab]"
    "|[\uaab5\uaab6\uaab9\uaabb\uaabc][\uaa80-\uaaaf]"
)
# Letters NFKD splits that the table weighs whole: Cyrillic short I and the Arabic letters with
# madda or hamza, which its contractions name, and the ones ``_JOINED`` spells.
_WHOLE = frozenset("\u0419\u0439\u0622\u0623\u0624\u0625\u0626" + "".join(_JOINED.values()))

# The white space, punctuation and symbols a name is likely to hold, in the order of the table's
# primary weights, but for the ones it weighs as their decomposition. Other punctuation or a symbol
# sorts after the last of its kind here, by code point.
_VARIABLE = (
    "\t\n\x0b\x0c\r\x85\u2028\u2029 _\u2017-\u2010\u2012\u2013\u2014\u2015"
    "\u301c\u30fb,\u3001;:!\xa1?\xbf.\u3002\xb7'\u2018\u2019\u201a\u201b\""
    "\u201c\u201d\u201e\u201f\u301d\u301e\xab\xbb()[]{}\u3008\u3009\u300a\u300b\u300c\u300d"
    "\u300e\u300f\u3010\u3011\u3014\u3015\u2016\xa7\xb6@*/\\&#%\u2020\u2021\u2022\u2023\u2027"
    "\u3003`\xb4^\xaf\xa8\xb8\xb0\xa9\xae\u2190\u2192\u2191\u2193\u2194\u2195\u2196"
    "\u2197\u2198\u2199\u219c\u219d\u219e\u219f+\xb1\xf7\xd7<=>\xac|\xa6~\u0e46\u0ec6\u3005"
    "\u303b\u309d\u30fc\u30fd\xa4\xa2$\xa3\xa5\u20ac"
)
_VAR_RANK = {c: i for i, c in enumerate(_VARIABLE)}


def _kind(c: str) -> str | None:
    """Which of ``_VARIABLE``'s kinds ``c`` is, if it is punctuation or a symbol."""
    category = unicodedata.category(c)
    return {"P": "punctuation", "S": "currency" if category == "Sc" else "symbol"}.get(category[0])


_VAR_END = {kind: i for i, c in enumerate(_VARIABLE) if (kind := _kind(c))}

# The accents the table weighs at the secondary level, in the order of their weights, ending with
# the one it gives the join in æ, ð, ŀ, œ and ß. A mark of ``_ACCENT_RANGES`` not here sorts after
# these, by code point.
_JOIN_ACCENT = "\xb7"
_ACCENTS = (
    "\u0301\u0300\u0306\u0302\u030c\u030a\u0308\u030b\u0303\u0307\u0338\u0327\u0328\u0304\u0335"
    + _JOIN_ACCENT
)
_ACCENT_RANK = {c: i + 1 for i, c in enumerate(_ACCENTS)}
# Ranges of marks, and nothing else, every one of which the table weighs as an accent once
# ``_IGNORED`` has taken out the ones it ignores. A mark outside them, such as a Tibetan vowel sign,
# is a letter of its own.
_ACCENT_RANGES = (
    (0x0300, 0x0362),  # Combining Diacritical Marks, without the combining Latin letters
    (0x05B0, 0x05BC),  # Hebrew points
    (0x05BF, 0x05BF),
    (0x05C1, 0x05C2),
    (0x05C7, 0x05C7),
    (0x064B, 0x065F),  # Arabic
    (0x0670, 0x0670),
    (0x0E47, 0x0E4E),  # Thai
    (0x0EC8, 0x0ECE),  # Lao
    (0x20D0, 0x20F0),  # Combining Diacritical Marks for Symbols
    (0xFE20, 0xFE2F),  # Combining Half Marks
    (0x3099, 0x309A),  # kana voicing marks
)
_ACCENT_MARKS = frozenset(chr(cp) for lo, hi in _ACCENT_RANGES for cp in range(lo, hi + 1))

# The letters of the Latin-1 Supplement and Latin Extended-A blocks that no decomposition maps onto
# an ASCII letter, as the table weighs them: two letters joined, an ASCII letter and an accent, or
# a letter of its own right after an ASCII letter. The table puts þ after z, where its code point
# already sorts it.
_LIGATURES = {"\xe6": "ae", "\u0153": "oe", "\xdf": "ss"}
_WITH = {
    "\xf0": "d" + _JOIN_ACCENT,
    "\u0111": "d\u0335",
    "\u0127": "h\u0335",
    "\u0142": "l\u0335",
    "\xf8": "o\u0338",
}
_AFTER = {"\u0131": "i", "\u0138": "q", "\u014b": "n", "\u0167": "t"}
# A middle dot after an l is the accent of the join, and ŀ is weighed as l and that middle dot.
_MIDDLE_DOT = "\xb7"
_L_DOT = {0x0140: "l\xb7", 0x013F: "L\xb7"}

# Tibetan: a subjoined consonant sorts right after its consonant, and the vowel signs, in this
# order, after the block's consonants and sign letters, which end at U+0F8F.
_TIBETAN_SUBJOINED = range(0x0F90, 0x0FB9)
_TIBETAN_LETTERS_END = 0x0F8F
_TIBETAN_VOWELS = (
    "\u0f71\u0f72\u0f73\u0f80\u0f81\u0f74\u0f75\u0f76"
    "\u0f77\u0f78\u0f79\u0f7a\u0f7b\u0f7c\u0f7d\u0f84"
)
_TIBETAN_VOWEL_RANK = {c: i + 1 for i, c in enumerate(_TIBETAN_VOWELS)}

# The tertiary weight the table gives a lowercase letter of each decomposition tag. An uppercase
# letter's is six above it, or 0x1D for a superscript, subscript or squared one. The kana have
# weights of their own: (small, normal) for each kind.
_TERTIARY = {
    "": 0x02,
    "<wide>": 0x03,
    "<compat>": 0x04,
    "<font>": 0x05,
    "<circle>": 0x06,
    "<small>": 0x0F,
    "<narrow>": 0x12,
    "<super>": 0x14,
    "<sub>": 0x15,
    "<vertical>": 0x16,
    "<initial>": 0x17,
    "<medial>": 0x18,
    "<final>": 0x19,
    "<isolated>": 0x1A,
    "<noBreak>": 0x1B,
    "<square>": 0x1C,
    "<fraction>": 0x1E,
}
_KANA = {"HIRAGANA": (0x0D, 0x0E), "KATAKANA": (0x0F, 0x11), "<narrow>": (0x10, 0x12)}
_CIRCLED_KANA = 0x13
# Every kana letter, by its name; they are all in Hiragana and Katakana, Katakana Phonetic
# Extensions, and Kana Extended-B to Small Kana Extension.
_KANA_NAMES = {
    chr(cp): name
    for lo, hi in ((0x3041, 0x30FF), (0x31F0, 0x31FF), (0x1AFF0, 0x1B16F))
    for cp in range(lo, hi + 1)
    if (name := unicodedata.name(chr(cp), "")).startswith(("HIRAGANA LETTER ", "KATAKANA LETTER "))
}


def _primary(b: str) -> tuple:
    """Where ``b`` sorts at the primary level: white space, punctuation, symbols and currency
    signs as ``_VARIABLE`` orders them, then digits by value, then letters."""
    if (rank := _VAR_RANK.get(b)) is not None:
        return (1, rank, 0)
    if kind := _kind(b):
        return (1, _VAR_END[kind], ord(b))
    if (value := unicodedata.digit(b, None)) is not None:
        return (2, value, 0)
    if ord(b) in _TIBETAN_SUBJOINED:
        return (3, ord(b) - 0x50, 1)
    if b in _TIBETAN_VOWEL_RANK:
        return (3, _TIBETAN_LETTERS_END, _TIBETAN_VOWEL_RANK[b])
    return (3, ord(b.lower()), 0)


def _tertiary(tag: str, b: str) -> int:
    weight = _TERTIARY[tag]
    if b == b.lower():
        return weight
    return weight + 6 if weight <= 0x06 else 0x1D if weight in (0x14, 0x15, 0x1C) else weight


def _kana(b: str, tag: str) -> tuple[str, int] | None:
    """A kana letter as the hiragana it ties at the primary level, with its tertiary weight."""
    name = _KANA_NAMES.get(b)
    if name is None:
        return None
    script = name.split(" ")[0]
    small = " SMALL " in name
    if small:
        b = unicodedata.lookup(name.replace("SMALL ", ""))
    if script == "KATAKANA" and "\u30a1" <= b <= "\u30f6":
        b = chr(ord(b) - 0x60)
    if tag == "<circle>":
        return b, _CIRCLED_KANA
    return b, _KANA[tag if tag == "<narrow>" else script][0 if small else 1]


@functools.lru_cache(maxsize=1 << 16)
def _elements(c: str) -> tuple[tuple, ...]:
    """What one character adds to a key, in order: ``(primary, tertiary)`` for each letter,
    digit, space, punctuation mark or symbol it is weighed as, and ``(None, secondary)`` for each
    accent."""
    if unicodedata.category(c) in ("Cc", "Cf") and c not in _VAR_RANK:
        return ()  # ignorable
    lower = c.lower()
    if lower in _LIGATURES:
        first, second = _LIGATURES[lower]
        return (
            (_primary(first), _tertiary("<compat>", c)),
            (None, _ACCENT_RANK[_JOIN_ACCENT]),
            (_primary(second), _tertiary("<compat>", c)),
        )
    if lower in _WITH:
        base, accent = _WITH[lower]
        return (_primary(base), _tertiary("", c)), (None, _ACCENT_RANK[accent])
    if lower in _AFTER:
        return (((3, ord(_AFTER[lower]), 1), _tertiary("", c)),)
    decomposition = unicodedata.decomposition(c)
    tag = decomposition.split()[0] if decomposition.startswith("<") else ""
    # A spacing accent, a space and a mark under its decomposition, is a symbol of its own.
    whole = c in _WHOLE or decomposition.startswith("<compat> 0020 ")
    elements = []
    for b in c if whole else unicodedata.normalize("NFKD", c):
        if b in _ACCENT_MARKS:
            elements.append((None, _ACCENT_RANK.get(b, len(_ACCENTS) + 1 + ord(b))))
        elif kana := _kana(b, tag):
            elements.append((_primary(kana[0]), kana[1]))
        else:
            elements.append((_primary(b), _tertiary(tag, b)))
    return tuple(elements)


@functools.lru_cache(maxsize=1 << 16)
def sort_key(name: str) -> tuple[tuple, tuple, tuple]:
    """``name``'s place in the collation: every primary, then every secondary, then every
    tertiary."""
    primaries: list[tuple] = []
    secondaries: list[int] = []
    tertiaries: list[int] = []

    def letter(primary: tuple, tertiary: int) -> None:
        primaries.append(primary)
        secondaries.append(0)
        tertiaries.append(tertiary)

    text = unicodedata.normalize("NFC", name.translate(_ASCII_LOWER)).translate(_L_DOT)
    text = _IGNORED.sub("", text)
    text = _PREVOWEL.sub(lambda m: m.group()[::-1], _JOIN.sub(lambda m: _JOINED[m.group()], text))
    i = 0
    while i < len(text):
        c = text[i]
        if unicodedata.category(c) == "Nd":  # a run of decimal digits compares by its value
            j = i
            while j < len(text) and unicodedata.category(text[j]) == "Nd":
                j += 1
            run = "".join(str(unicodedata.decimal(d)) for d in text[i:j])
            at = 0
            while at < len(run):
                while at < len(run) - 1 and run[at] == "0":
                    at += 1
                number = run[at : at + _NUMBER_DIGITS]
                letter((2, -1, len(number), number), _TERTIARY[""])
                at += len(number)
            i = j
            continue
        i += 1
        if c == _MIDDLE_DOT and i >= 2 and text[i - 2] in "lL":
            secondaries.append(_ACCENT_RANK[_JOIN_ACCENT])
            continue
        for primary, weight in _elements(c):
            if primary is None:
                secondaries.append(weight)
            else:
                letter(primary, weight)
    return tuple(primaries), tuple(secondaries), tuple(tertiaries)
