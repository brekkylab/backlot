"""The collation Drive's ``orderBy=name_natural`` sorts an English-language account's names by:
ICU's root collation at tertiary strength, with numeric ordering on, over the name with its ASCII
capitals lowered, so ``A`` ties ``a`` where ``Ä`` sorts after ``ä`` and ``ⓐ`` after ``a``.

Measured against Drive v3 on 2026-10-20, real collates by the language the caller's Google account
is set to. Two Korean-language accounts listed their 21,872 files in ICU's ``ko`` order, Hangul and
Han ahead of Latin, apart from rows a page boundary repeated; one of them, set to English, then
listed its 228 files, and seven folders of 58, 32, 6, 6, 16, 19 and 9 names made for it, in the root
order. Backlot collates as the English-language account did. Names the collation ties, such as ``A``
and ``a``, keep the order ``backlot.routers.google._drive_sort`` gives ties.

``backlot/data/drive_collation.json`` holds ICU's weights for every code point but the Hangul
syllables, whose jamo carry theirs, and for every sequence CLDR's root collation weighs as one,
made by ``scripts/gen_drive_collation.py``; :func:`sort_key` reads them, so Backlot does not need
ICU. Each pair it ordered apart from ICU 78.3, over every string of up to four of ten Tibetan
letters and vowel signs, every one of up to four of eleven Cyrillic letters and marks, 64,485 random
strings of cased letters, compatibility forms and digits, and 50,936 random strings of mixed
scripts, held marks out of their canonical order or one of the seven code points whose
decomposition starts with a mark (U+0340, U+0341, U+0343, U+0344, U+0F73, U+0F75, U+0F81)."""

from __future__ import annotations

import bisect
import functools
import json
from pathlib import Path

_TABLE = Path(__file__).resolve().parent / "data" / "drive_collation.json"

# ICU reads at most 254 digits of a run as one number and the rest as the next.
_NUMBER_DIGITS = 254
# Real lowers the ASCII capitals of a name before it collates it, and no other.
_ASCII_LOWER = {cp: cp + 32 for cp in range(ord("A"), ord("Z") + 1)}
# How many marks past a sequence Backlot looks for one that completes it: the 30 a run of marks
# holds at most in the stream-safe text of Unicode's UAX #15. ICU looks further; this bound keeps a
# name of a starter and a long run of marks from costing the square of its length.
_MARKS_LOOKED_PAST = 30


class _Weights:
    """The table, read once, on the first sort that needs it. A code point in a run has one
    element, of the run's next primary and the common secondary and tertiary; every other code
    point the table holds, and every sequence that collates as one, has its elements listed."""

    def __init__(self, table: dict) -> None:
        self.common, self.tcommon, self.zero = table["common"], table["tcommon"], table["zero"]
        self.starts, self.counts, self.firsts = [], [], []
        end = next_rank = 0
        for gap, count, skip in table["runs"]:
            self.starts.append(end + gap)
            self.counts.append(count)
            self.firsts.append(next_rank + skip)
            end, next_rank = end + gap + count, next_rank + skip + count
        self.listed = {
            tuple(cps) if isinstance(cps, list) else (cps,): tuple(
                zip(flat[::3], flat[1::3], flat[2::3])
            )
            for cps, *flat in table["chars"] + table["contractions"]
        }
        self.longest = max(len(k) for k in self.listed)
        self.starters = {k[0] for k in self.listed if len(k) > 1}
        self.digits = {start + v: v for start in table["digits"] for v in range(10)}
        self.ccc = {cp + k: ccc for cp, count, ccc in table["non_starters"] for k in range(count)}

    def at(self, cps: list[int], i: int, taken: set[int]):
        """Where the code points from ``i`` that collate as one end, and their elements, past the
        positions in ``taken``. The longest sequence the table lists from ``i`` also takes a mark
        further on, past marks of a lower combining class, when the two are listed as one, and that
        mark's position joins ``taken``: the Unicode Collation Algorithm's S2.1, as ICU matches, as
        far as ``_MARKS_LOOKED_PAST`` marks."""
        key, end = (cps[i],), i + 1
        if cps[i] in self.starters:
            ahead, j = [], i + 1
            while j < len(cps) and len(ahead) < self.longest - 1:
                if j not in taken:
                    ahead.append(j)
                j += 1
            for n in range(len(ahead), 0, -1):
                if (longer := (cps[i], *(cps[a] for a in ahead[:n]))) in self.listed:
                    key, end = longer, ahead[n - 1] + 1
                    break
            j, passed, looked = end, 0, 0
            while j < len(cps) and looked < _MARKS_LOOKED_PAST:
                if j in taken:
                    j += 1
                    continue
                if not (ccc := self.ccc.get(cps[j], 0)):
                    break
                looked += 1
                if passed < ccc and key + (cps[j],) in self.listed:
                    key += (cps[j],)
                    taken.add(j)
                else:
                    passed = max(passed, ccc)
                j += 1
        if (found := self.listed.get(key)) is not None:
            return end, found
        run = bisect.bisect_right(self.starts, cps[i]) - 1
        return end, ((self.firsts[run] + cps[i] - self.starts[run], self.common, self.tcommon),)


@functools.cache
def _weights() -> _Weights:
    return _Weights(json.loads(_TABLE.read_text(encoding="utf-8")))


def _jamo(cp: int) -> tuple[int, ...]:
    """A Hangul syllable as its two or three jamo, which carry its collation elements, by the
    arithmetic Unicode defines the syllables with; any other code point as itself."""
    index = cp - 0xAC00
    if not 0 <= index < 11172:
        return (cp,)
    lead, vowel, tail = 0x1100 + index // 588, 0x1161 + index % 588 // 28, index % 28
    return (lead, vowel, 0x11A7 + tail) if tail else (lead, vowel)


@functools.lru_cache(maxsize=1 << 16)
def sort_key(name: str) -> tuple[tuple, tuple, tuple]:
    """``name``'s place in the collation, as a key ``sorted`` compares: every element's primary
    in turn, then every secondary, then every tertiary. A run of decimal digits, in any script,
    compares by its value, as one element for each ``_NUMBER_DIGITS`` digits of it, ahead of the
    characters ICU weighs as digits without reading a number from them, such as ``²`` and ``①``; its
    leading zeros do not count, so ``v01`` ties ``v1``. A precomposed character and its
    decomposition collate alike without normalizing, as they do in ICU, since the table gives the
    one the other's elements, but for the seven the module's docstring names."""
    w = _weights()
    cps = [j for ch in name.translate(_ASCII_LOWER) for j in _jamo(ord(ch))]
    primary: list[tuple] = []
    secondary: list[int] = []
    tertiary: list[int] = []
    taken: set[int] = set()
    i = 0
    while i < len(cps):
        if i in taken:
            i += 1
            continue
        if cps[i] in w.digits:
            j, digits = i, []
            while j < len(cps) and (j in taken or cps[j] in w.digits):
                if j not in taken:
                    digits.append(cps[j])
                j += 1
            run = "".join(str(w.digits[cp]) for cp in digits)
            at = 0
            while at < len(run):
                while at < len(run) - 1 and run[at] == "0":
                    at += 1
                number = run[at : at + _NUMBER_DIGITS]
                primary.append((w.zero - 0.5, len(number), number))
                secondary.append(w.common)
                tertiary.append(w.tcommon)
                at += len(number)
            i = j
            continue
        i, elements = w.at(cps, i, taken)
        for p, s, t in elements:
            if p:
                primary.append((p, 0, ""))
            if s:
                secondary.append(s)
            if t:
                tertiary.append(t)
    return tuple(primary), tuple(secondary), tuple(tertiary)
