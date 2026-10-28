"""Google Sheets' cells, as the importer, the store and `routers.google_sheets` all hold them.

Outside the router because the importer is the other caller and the two layers do not import each
other -- an importer that reached into a router would pull the whole web layer into ``backlot
import``. A vendor-specific pure helper that more than one layer needs lives at this level:
``sigv4`` is the S3 router's, and ``synth`` is every source's.

In four parts:

* display -- how real renders a number: General, and the number formats a cell carries;
* cells -- a sheet's grid and the states a write leaves a cell in, serialised as Drive's export
  serves it;
* input -- what a value written through the ``values`` API stores, RAW or USER_ENTERED;
* workbook -- a spreadsheet's sheets and settings, as a write changes them.

Measured against the live Sheets v4 API on 2026-10-26, on an ``en_US`` spreadsheet, unless a
docstring says otherwise.
"""

from __future__ import annotations

import copy
import json
import math
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import NamedTuple

from backlot import synth

# --- display --------------------------------------------------------------------------------------
#
# Rounding is half away from zero on the number's shortest decimal spelling, not on its binary
# value: ``2.675`` under ``#,##0.00`` displays ``2.68`` where ``round(2.675, 2)`` is ``2.67``.

# The serial-number epoch: day 0 is 1899-12-30, so day 60 is 1900-02-28 and day 61 1900-03-01.
_EPOCH_DAYS = 693594  # date(1899, 12, 30).toordinal()

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAYS = tuple(name[:3] for name in WEEKDAY_NAMES)

# The family a number format belongs to, by its `type`. A USER_ENTERED write keeps a cell's format
# when what it infers is of the same family, so this grouping is what decides inheritance
# (`apply`). NUMBER, CURRENCY and SCIENTIFIC are one family: `$5` into a `#,##0`
# cell keeps `#,##0`, and `1,234` into a `"$"#,##0` cell keeps the currency.
FAMILY = {
    "NUMBER": "number",
    "CURRENCY": "number",
    "SCIENTIFIC": "number",
    "PERCENT": "percent",
    "DATE": "date",
    "TIME": "time",
    "DATE_TIME": "date_time",
    "TEXT": "text",
}

# The types `dateTimeRenderOption=FORMATTED_STRING` renders as their display string.
DATE_TIME_TYPES = frozenset({"DATE", "TIME", "DATE_TIME"})


def _dec(x) -> Decimal:
    """``x`` as the decimal it is spelled as: ``repr`` of a float is its shortest round-trip form."""
    return Decimal(repr(x)) if isinstance(x, float) else Decimal(x)


def _fixed(d: Decimal, places: int) -> str:
    """``d`` rounded half away from zero to ``places`` decimals, a negative zero shown unsigned."""
    q = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)
    return format(q, "f")


def _strip(s: str) -> str:
    return s.rstrip("0").rstrip(".") if "." in s else s


def _sci(d: Decimal, places: int, *, strip: bool) -> str:
    """Scientific notation with a ``places``-decimal mantissa and an exponent of at least two
    digits — ``1.23E+05``, ``1.00E+100`` — or the mantissa's trailing zeros removed (``1E+15``)."""
    if d == 0:
        mantissa, exp = Decimal(0), 0
    else:
        exp = d.adjusted()
        mantissa = (d.scaleb(-exp)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if abs(mantissa) >= 10:
            mantissa, exp = mantissa / 10, exp + 1
    m = _fixed(mantissa, places)
    if strip:
        m = _strip(m)
    return f"{m}E{'+' if exp >= 0 else '-'}{abs(exp):02d}"


def general(x) -> str:
    """A number's display with no number format.

    Ten significant digits, more when the integer part needs them (``123456789012`` shows whole),
    trailing zeros removed. A magnitude under ``1e-10`` shows ``0``. From the point where
    ``floor(log10(|x|))`` reaches 15 it is scientific with six significant digits
    (``1.23457E+15``, ``1E+21``), and that boundary is the floating-point logarithm's:
    ``999999999999998`` already shows ``1E+15`` while ``999999999999990`` shows whole.
    """
    if x == 0 or abs(x) < 1e-10:
        return "0"
    d = _dec(x)
    exp = math.floor(math.log10(abs(x)))
    if exp >= 15:
        return _sci(d, 5, strip=True)
    return _strip(_fixed(d, max(0, 9 - exp)))


def _sections(pattern: str) -> list[str]:
    """A pattern's `;`-separated sections, a `;` inside quotes or after a backslash kept."""
    out, cur, quoted, escaped = [], "", False, False
    for ch in pattern:
        if escaped:
            cur, escaped = cur + ch, False
        elif ch == "\\":
            cur, escaped = cur + ch, True
        elif ch == '"':
            cur, quoted = cur + ch, not quoted
        elif ch == ";" and not quoted:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return [*out, cur]


class _Numeric(NamedTuple):
    prefix: str
    suffix: str
    int_zeros: int  # `0`s before the decimal point: digits always shown
    grouping: bool
    scale: int  # trailing commas, each dividing by 1000
    frac_zeros: int  # `0`s after the point: decimals always shown
    frac_optional: int  # `#`/`?` after the point: shown when not zero
    point: bool
    percent: int
    exp_sign: str | None  # "+" for `E+`, "-" for `E-`, None for no exponent
    exp_zeros: int


def _numeric_section(section: str) -> _Numeric | None:
    """One section read into its parts, or None for one this cannot display -- a condition
    (`[>100]`), a literal between digits, a text placeholder."""
    prefix, suffix, digits_seen, after_digits = [], [], False, False
    int_zeros = frac_zeros = frac_optional = percent = scale = exp_zeros = 0
    grouping = point = False
    exp_sign = None
    part = "int"
    i = 0
    while i < len(section):
        ch = section[i]
        literal = None
        if ch == '"':
            end = section.find('"', i + 1)
            literal, i = (
                section[i + 1 : end if end >= 0 else len(section)],
                (end if end >= 0 else len(section)),
            )
        elif ch == "\\" and i + 1 < len(section):
            literal, i = section[i + 1], i + 1
        elif ch == "_" and i + 1 < len(section):
            literal, i = " ", i + 1
        elif ch == "*" and i + 1 < len(section):
            i += 1
        elif ch == "[":
            end = section.find("]", i)
            if end < 0 or not section[i + 1 : end].isalnum():
                return None  # a condition, not a colour
            i = end
        elif ch in "0#?":
            if after_digits and part != "exp":
                return None
            digits_seen = True
            if part == "int":
                int_zeros += ch == "0"
            elif part == "frac":
                frac_zeros += ch == "0"
                frac_optional += ch != "0"
            else:
                exp_zeros += 1
        elif ch == "," and digits_seen and part == "int":
            if i + 1 < len(section) and section[i + 1] in "0#?":
                grouping = True
            else:
                scale += 1
        elif ch == "." and part == "int" and not point:
            point, part, digits_seen = True, "frac", True
        elif ch in "Ee" and i + 1 < len(section) and section[i + 1] in "+-" and digits_seen:
            exp_sign, part, i = section[i + 1], "exp", i + 1
        elif ch == "@":
            return None
        else:
            percent += ch == "%"
            literal = ch
        if literal is not None:
            if digits_seen:
                after_digits = True
                suffix.append(literal)
            else:
                prefix.append(literal)
        i += 1
    if not digits_seen:
        return None
    return _Numeric(
        "".join(prefix),
        "".join(suffix),
        int_zeros,
        grouping,
        scale,
        frac_zeros,
        frac_optional,
        point,
        percent,
        exp_sign,
        exp_zeros,
    )


def _digits(d: Decimal, spec: _Numeric) -> str:
    """``|d|`` under ``spec``, without its sign."""
    places = spec.frac_zeros + spec.frac_optional
    if spec.exp_sign is not None:
        width = max(spec.int_zeros, 1)
        if d == 0:
            mantissa, exp = Decimal(0), 0
        else:
            exp = d.adjusted() - (width - 1)
            mantissa = d.scaleb(-exp).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
            if mantissa >= Decimal(10) ** width:
                exp += 1
                mantissa = d.scaleb(-exp).quantize(
                    Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP
                )
        body = _decimal_text(abs(mantissa), spec, places)
        sign = "-" if exp < 0 else ("+" if spec.exp_sign == "+" else "")
        return f"{body}E{sign}{abs(exp):0{max(spec.exp_zeros, 1)}d}"
    return _decimal_text(
        abs(d).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), spec, places
    )


def _decimal_text(d: Decimal, spec: _Numeric, places: int) -> str:
    whole, _, frac = format(d, "f").partition(".")
    whole = whole.lstrip("0")
    whole = whole.rjust(spec.int_zeros, "0")
    if spec.grouping and whole:
        whole = f"{int(whole):,}".rjust(len(whole), "0") if whole.lstrip("0") else whole
    frac = frac.ljust(places, "0")[:places]
    keep = spec.frac_zeros + len(frac[spec.frac_zeros :].rstrip("0"))
    frac = frac[:keep]
    return whole + ("." + frac if spec.point and (frac or spec.frac_optional) else "")


def _numeric(x, pattern: str) -> str:
    """A number under a numeric pattern: digit placeholders (`0` always shown, `#` when not zero),
    grouping, a point, `%`, an exponent (`E+00`), literals around them (quoted, escaped, or a
    plain `$`), and up to three sections, positive;negative;zero. Measured patterns:
    `0.0`, `0.000`, `#,##0.0`, `0`, `0.0%`, `0.000E+00`, `0E+0`, `$#,##0`, `#,##0;(#,##0)` and
    `0.00"kg"` beside the patterns a write infers. A pattern this cannot read displays as General."""
    sections = _sections(pattern)
    d = _dec(x)
    negative_section = len(sections) > 1 and d < 0
    if len(sections) > 2 and d == 0:
        section = sections[2]
    elif negative_section:
        section = sections[1]
    else:
        section = sections[0]
    spec = _numeric_section(section)
    if spec is None:
        return general(x)
    d = d * Decimal(100) ** spec.percent / Decimal(1000) ** spec.scale
    text = _digits(d, spec)
    shown_negative = d < 0 and not negative_section and any(c in "123456789" for c in text)
    return f"{'-' if shown_negative else ''}{spec.prefix}{text}{spec.suffix}"


# Date and time tokens, longest first so `mmmm` is not read as `mm` twice.
_TOKEN = re.compile(r'"[^"]*"|\[h\]|am/pm|yyyy|yy|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|ss|s', re.I)


def _civil(days: int) -> tuple[int, int, int]:
    """``(year, month, day)`` of a day count from 0001-01-01 = 1, past year 9999 as well: real
    displays serial 2958466 as ``10000-01-01`` where :mod:`datetime` stops at 9999."""
    # Howard Hinnant's days_from_civil inverse, shifted from the 1970 epoch to ordinal 1.
    z = days - 719163 + 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return (y + 1 if m <= 2 else y), m, d


def _date_time(x, pattern: str) -> str:
    """A serial number under a DATE, TIME or DATE_TIME pattern.

    The value is rounded half up to the whole second first and only then cut to what the pattern
    shows: ``10:30:45`` under ``h:mm`` is ``10:30``, while ``0.999999`` of a day (86399.91 s) is
    ``0:00``. A time of day wraps (``-0.25`` is ``18:00``), and ``[h]`` counts elapsed hours,
    keeping a sign (``-0.5`` is ``-12:00:00``).
    """
    # The product is taken in floating point, as real takes it: `0.937505787037037` of a day is
    # 81000.5 s there and rounds to 10:30:01, where the exact decimal product falls just short.
    total = int(_dec(float(x) * 86400).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    days, secs = divmod(total, 86400)
    year, month, day = _civil(_EPOCH_DAYS + days)
    hour, rem = divmod(secs, 3600)
    minute, second = divmod(rem, 60)
    ampm = "am/pm" in pattern.lower()
    out, prev = [], None
    pos = 0
    for tok in _TOKEN.finditer(pattern):
        out.append(pattern[pos : tok.start()])
        pos = tok.end()
        t = tok.group()
        if t.startswith('"'):
            out.append(t[1:-1])
            continue
        t = t.lower()
        if t == "[h]":
            hours, rest = divmod(abs(total), 3600)
            out.append(("-" if total < 0 else "") + str(hours))
            minute, second = divmod(rest, 60)
        elif t in ("h", "hh"):
            shown = (hour % 12 or 12) if ampm else hour
            out.append(f"{shown:02d}" if t == "hh" else str(shown))
        elif t in ("mm", "m") and prev in ("h", "hh", "[h]"):
            out.append(f"{minute:02d}" if t == "mm" else str(minute))
        elif t in ("ss", "s"):
            out.append(f"{second:02d}" if t == "ss" else str(second))
        elif t == "am/pm":
            out.append("AM" if hour < 12 else "PM")
        elif t == "yyyy":
            out.append(f"{year:04d}")
        elif t == "yy":
            out.append(f"{year % 100:02d}")
        elif t == "mmmm":
            out.append(MONTHS[month - 1])
        elif t == "mmm":
            out.append(MONTHS[month - 1][:3])
        elif t == "mm":
            out.append(f"{month:02d}")
        elif t == "m":
            out.append(str(month))
        elif t == "dddd":
            out.append(WEEKDAY_NAMES[(_EPOCH_DAYS + days - 1) % 7])
        elif t == "ddd":
            out.append(WEEKDAYS[(_EPOCH_DAYS + days - 1) % 7])
        elif t == "dd":
            out.append(f"{day:02d}")
        else:  # "d"
            out.append(str(day))
        prev = t
    out.append(pattern[pos:])
    return "".join(out)


# The pattern a number format that names a type and no pattern displays with. Measured on an
# `en_US` spreadsheet: CURRENCY as dollars to the cent, DATE, TIME and DATE_TIME as these; NUMBER,
# SCIENTIFIC and TEXT as General, and PERCENT as General a hundred times over with a `%`.
_TYPE_PATTERNS = {
    "CURRENCY": '"$"#,##0.00',
    "DATE": "m/d/yyyy",
    "TIME": "h:mm:ss am/pm",
    "DATE_TIME": "m/d/yyyy h:mm:ss",
}


def render(x, number_format: dict) -> str:
    """A number's display under a number format: its pattern, or its type's when it states none."""
    kind = number_format["type"]
    pattern = number_format.get("pattern") or _TYPE_PATTERNS.get(kind)
    if not pattern:
        return general(_dec(x) * 100) + "%" if kind == "PERCENT" else general(x)
    if kind in DATE_TIME_TYPES:
        return _date_time(x, pattern)
    return _numeric(x, pattern)


# --- cells ----------------------------------------------------------------------------------------
#
# The importer derives ``gdrive_files.content`` from the first sheet and the export route serves
# TSV from the grid, so the two agree by sharing this code.

# A stored cell. A bare scalar unless a served write made it something a scalar cannot say
# (`apply`): then an object with `v` (the value, absent when empty), `e` (the error
# type) and `nf` (the `numberFormat`). A corpus states scalars alone, so a grid that was never
# written holds nothing else, and a corpus `""` or `None` is a cell nobody wrote.
#
# Two objects are empty and still there, measured 2026-10-26: `{"v": ""}` is a cell written an
# empty string (a RAW `""`, a lone apostrophe), served with a string's format and no value, and
# `{"nf": {}}` a cell whose value a USER_ENTERED `""` took away, served with a format and no
# alignment. A never-written cell is neither: it is served as nothing at all.
Cell = str | int | float | bool | None | dict

# Each error type by the code it displays as.
ERROR_CODES = {
    "N_A": "#N/A",
    "DIVIDE_BY_ZERO": "#DIV/0!",
    "REF": "#REF!",
    "VALUE": "#VALUE!",
    "NAME": "#NAME?",
    "NUM": "#NUM!",
    "NULL_VALUE": "#NULL!",
    "ERROR": "#ERROR!",
}


EMPTY_STRING = {"v": ""}
CLEARED = {"nf": {}}


def cell(value, *, error: str | None = None, number_format: dict | None = None) -> Cell:
    """A cell as stored, from its parts."""
    if error is None and not number_format:
        return value
    out: dict = {}
    if value is not None:
        out["v"] = value
    if error is not None:
        out["e"] = error
    if number_format is not None:
        out["nf"] = number_format
    return out


def cell_value(c: Cell):
    """A cell's value: a string (`""` for an empty string), a number, a boolean, or None for a
    cleared, never-written or error cell."""
    return c.get("v") if isinstance(c, dict) else c


def cell_error(c: Cell) -> str | None:
    return c.get("e") if isinstance(c, dict) else None


def cell_format(c: Cell) -> dict | None:
    """A cell's `numberFormat` in real's member order, `type` and then `pattern` (measured
    2026-10-27 on CURRENCY, PERCENT and TIME cells), or None for one that has none. A stored grid
    is JSON with sorted keys, so the order is set here."""
    nf = c.get("nf") if isinstance(c, dict) else None
    return {k: nf[k] for k in ("type", "pattern") if k in nf} if nf else None


def written(c: Cell) -> bool:
    """Whether a cell is there to serve: it holds something, or a write left it empty."""
    return isinstance(c, dict) or (c is not None and c != "")


def _scalar(v):
    """One cell as it is stored. An integral float collapses to an int: measured, writing 3.0 to a
    real sheet stores ``numberValue: 3`` and formats as ``"3"``, so keeping the float would serve a
    value the real API cannot hold.

    Narrow to ``float`` on purpose. Widening this to ``(int, float)`` would swallow ``bool``, which
    is a subclass of ``int`` — the hazard ``routers.google_sheets._sheets_value`` guards against."""
    if not isinstance(v, float):
        return v
    return int(v) if v.is_integer() else v


collapse = _scalar


def normalise_grid(grid: list[list]) -> list[list[Cell]]:
    """A rectangular grid: short rows padded with empty cells to the width of the widest row.

    Everything downstream may assume rectangularity, which is what lets the serving layer index a
    row by column without a bounds check on every cell."""
    width = max((len(r) for r in grid), default=0)
    return [[_scalar(r[i]) if i < len(r) else None for i in range(width)] for r in grid]


def formatted(c: Cell) -> str:
    """A cell's ``formattedValue`` -- its display string, which is also what export serialises.

    Measured: a boolean displays as TRUE/FALSE whatever its number format, an error as its code,
    an empty cell as the empty string, and a number under its number format or, with none, in
    General (:func:`general`)."""
    if (error := cell_error(c)) is not None:
        return ERROR_CODES[error]
    value = cell_value(c)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return value
    nf = cell_format(c)
    return render(value, nf) if nf else general(value)


def used_extent(grid: list[list[Cell]]) -> tuple[int, int]:
    """``(rows, cols)`` counting only as far as the last row and column holding anything.

    Measured: export pads its rows out to the sheet's last USED column, so an all-empty trailing
    column contributes no field at all."""
    rows = cols = 0
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if formatted(cell) != "":
                rows = r + 1
                cols = max(cols, c + 1)
    return rows, cols


def _rows_used(grid: list[list[Cell]]) -> list[list[str]]:
    rows, cols = used_extent(grid)
    return [[formatted(grid[r][c]) for c in range(cols)] for r in range(rows)]


def _csv_field(v: str) -> str:
    if any(ch in v for ch in (",", '"', "\n")):
        return '"' + v.replace('"', '""') + '"'
    return v


def to_csv(grid: list[list[Cell]]) -> str:
    """The grid as ``files.export?mimeType=text/csv`` serialises it.

    Measured: a field is quoted for a comma, a double quote or a newline but NOT for a tab; an
    embedded newline stays a bare ``\\n`` inside the quotes while rows are separated by
    ``\\r\\n``; leading and trailing spaces are preserved unquoted; and there is no trailing
    newline. Hand-rolled because :mod:`csv` cannot express that dialect.

    Rows are rectangular to the last used column, unlike ``values.get``, which trims each row
    independently and so answers ragged."""
    return "\r\n".join(",".join(_csv_field(v) for v in row) for row in _rows_used(grid))


def to_tsv(grid: list[list[Cell]]) -> str:
    """The grid as ``files.export?mimeType=text/tab-separated-values`` serialises it.

    Measured: TSV has NO quoting mechanism. An embedded newline and an embedded tab each collapse
    to a single space, and a double quote passes through bare -- a lossy conversion, reproduced
    rather than escaped around."""
    return "\r\n".join(
        "\t".join(v.replace("\n", " ").replace("\t", " ") for v in row) for row in _rows_used(grid)
    )


# --- input ----------------------------------------------------------------------------------------
#
# ``valueInputOption=RAW`` keeps a value as sent, and ``USER_ENTERED`` reads a string the way the
# Sheets UI reads typing. Each rule was measured by writing an input into a cell and reading back
# its ``userEnteredValue``, ``formattedValue`` and ``effectiveFormat.numberFormat``. Inputs outside
# what was measured fall through to a string, which is what real does for anything it cannot read.
#
# A formula is stored as the text typed: evaluating one needs a formula engine, and storing its
# text keeps a later read honest about what was written.

# Error literals a USER_ENTERED string is read as, by their displayed code, matched without
# regard to case and after trimming: `#n/a` and `#N/A ` are both N_A, `#N/A!` is text.
ERRORS = {code: kind for kind, code in ERROR_CODES.items()}

# What trimming removes before a number, a date or an error is read. A boolean is NOT trimmed:
# `TRUE ` and ` FALSE` stay text.
_SPACE = " \t\n\r"

_CURRENCIES = "$€"

# The digits of a number: an integer part that may group by commas (each group after a comma at
# least three digits, so `1,2345` reads and `12,34` does not, and `,123` reads with nothing before
# the comma), an optional fraction, an optional exponent. ASCII digits only: `٣` and `１２` are text.
_DIGITS = re.compile(
    r"(?P<int>\d*(?:,\d{3,})+|\d+)?(?P<frac>\.\d*)?(?P<exp>[eE][+-]?\d+)?\Z", re.ASCII
)


def _nf(type_: str, pattern: str) -> dict:
    return {"type": type_, "pattern": pattern}


def _truncate15(text: str) -> Decimal:
    """A decimal kept to 15 significant digits by truncation: `0.1234567890123456789` stores
    `0.123456789012345` and `1234567890123456.5` stores `1234567890123450`."""
    d = Decimal(text)
    if d == 0:
        return Decimal(0)
    exp = d.adjusted()
    q = Decimal(1).scaleb(exp - 14)
    return d.quantize(q, rounding="ROUND_DOWN") if exp - 14 < 0 else (d // q) * q


def _number(text: str):
    """``(value, number_format)`` for a numeric spelling, or None.

    One negation at most -- parentheses or a minus, before or after a leading currency (`($5)`,
    `$(5)`, `-$5`, `$-5`); two are text (`-(5)`, `(-5)`, `$(-5)`, `-$(5)`). A currency trails a
    percent sign but does not precede one (`5%$` reads, `5$%` is text), and a percent sign may lead
    (`%5`). A space, tab, newline or carriage return may follow a leading sign or currency and
    precede a trailing one (`-\t5`, `5\t$`), where a no-break space may not. All measured."""
    s, negations, cur, cur_after, pct = text, 0, None, False, False
    if s[:1] == "(" and s[-1:] == ")":
        s, negations = s[1:-1].strip(_SPACE), 1
    elif s[:1] == "-":
        s, negations = s[1:].lstrip(_SPACE), 1
    if s and s[0] in _CURRENCIES:
        cur, s = s[0], s[1:].lstrip(_SPACE)
        if s[:1] == "-":
            s, negations = s[1:], negations + 1
        elif s[:1] == "(" and s[-1:] == ")":
            s, negations = s[1:-1], negations + 1
    elif s[:1] == "%":
        s, pct = s[1:], True
    if cur is None and s and s[-1] in _CURRENCIES:
        cur, cur_after, s = s[-1], True, s[:-1].rstrip(_SPACE)
    if not pct and s[-1:] == "%":
        s, pct = s[:-1], True
    if negations > 1:
        return None
    neg = negations == 1
    m = _DIGITS.match(s)
    if not s or m is None or not re.search(r"[0-9]", (m["int"] or "") + (m["frac"] or "")):
        return None
    whole, frac, exp = m["int"] or "", m["frac"] or "", m["exp"]
    if exp and cur:
        return None
    digits = whole.replace(",", "")
    if not frac and not exp and len(digits.lstrip("0")) > 15:
        return None  # a long integer stays text, so an id is not rounded
    try:
        d = _truncate15((digits or "0") + (frac if frac != "." else "") + (exp or ""))
    except InvalidOperation:
        return None
    if math.isinf(float(d)):
        return None  # past a double's range, measured: `1.8e308` is text where `1.7…e308` reads
    if pct:
        d /= 100
    if neg:
        d = -d
    value = float(d)
    value = int(value) if value.is_integer() and abs(value) < 2**53 else value
    places = ".00" if frac else ""
    if exp:
        fmt = _nf("SCIENTIFIC", "0.00E+00")
    elif cur:
        body = f"#,##0{places}"
        fmt = _nf("CURRENCY", f'{body}"{cur}"' if cur_after else f'"{cur}"{body}')
    elif pct:
        fmt = _nf("PERCENT", f"0{places}%")
    elif "," in whole:
        fmt = _nf("NUMBER", f"#,##0{places}")
    else:
        fmt = None
    return value, fmt


def _month(word: str):
    """``(month, token)`` for a month name: its three-letter abbreviation or its full name, any
    case. `Sept` is neither."""
    w = word.casefold()
    for i, name in enumerate(MONTHS):
        if w == name.casefold():
            return i + 1, "mmmm"
        if w == name[:3].casefold():
            return i + 1, "mmm"
    return None


def _year(text: str, today: date) -> tuple[int, str]:
    """A typed year and its token: two digits are `yy`, anything else `yyyy`. A year under 100,
    however many digits spell it, pivots at 30 — `29` is 2029, `30` is 1930, and `0024` 2024 —
    while `124` is the year 124."""
    y = int(text)
    if y < 100:
        y += 2000 if y < 30 else 1900
    return y, ("yy" if len(text) == 2 else "yyyy")


# The latest year a typed date reaches, measured: `1/1/99999` reads, `1/1/100000` is text.
_MAX_YEAR = 99999


def _serial(y: int, m: int, d: int):
    """The serial number of a calendar date, or None for one that does not exist. Computed rather
    than taken from :mod:`datetime`, whose years stop at 9999 where a typed date's do not."""
    if not (1 <= m <= 12 and 1 <= y <= _MAX_YEAR):
        return None
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    if not 1 <= d <= (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[m - 1]:
        return None
    # days from 0001-01-01, the inverse of `_civil`
    yy, mm = (y - 1, m + 9) if m <= 2 else (y, m - 3)
    era = yy // 400
    doy = (153 * mm + 2) // 5 + d - 1
    doe = (yy - era * 400) * 365 + (yy - era * 400) // 4 - (yy - era * 400) // 100 + doy
    return era * 146097 + doe - 719468 + 719163 - _EPOCH_DAYS


_NUMERIC_DATE = re.compile(r"(\d+)([/-])(\d+)(?:\2(\d+))?\Z", re.ASCII)


def _numeric_date(s: str, today: date):
    """`2024-01-15`, `2024/1`, `1/15/2024`, `1/15/24`, `1/15`, `1/2024`, `01-15-2024`: ``(serial,
    pattern)`` or None. Month and day are zero-padded in the pattern when EITHER was typed with a
    leading zero (`1/05/2024` is `mm/dd/yyyy`). A dot is not a date separator."""
    m = _NUMERIC_DATE.match(s)
    if m is None:
        return None
    a, sep, b, c = m[1], m[2], m[3], m[4]
    if len(a) == 4:  # year first: y-m-d or y-m
        y, ytok = int(a), "yyyy"
        mo, dd = b, c
        if dd is None:
            serial = _serial(y, int(mo), 1)
            mtok = "mm" if mo.startswith("0") else "m"
            return (serial, f"yyyy{sep}{mtok}") if serial is not None else None
        pad = mo.startswith("0") or dd.startswith("0")
        serial = _serial(y, int(mo), int(dd))
        return (
            (serial, f"yyyy{sep}{'mm' if pad else 'm'}{sep}{'dd' if pad else 'd'}")
            if serial is not None
            else None
        )
    if c is None and len(b) == 4:  # m/yyyy
        serial = _serial(int(b), int(a), 1)
        return (
            (serial, f"{'mm' if a.startswith('0') else 'm'}{sep}yyyy")
            if serial is not None
            else None
        )
    if c is None:  # m/d, this year
        serial = _serial(today.year, int(a), int(b))
        pad = a.startswith("0") or b.startswith("0")
        return (
            (serial, f"{'mm' if pad else 'm'}{sep}{'dd' if pad else 'd'}")
            if serial is not None
            else None
        )
    y, ytok = _year(c, today)
    pad = a.startswith("0") or b.startswith("0")
    serial = _serial(y, int(a), int(b))
    if serial is None:
        return None
    return serial, f"{'mm' if pad else 'm'}{sep}{'dd' if pad else 'd'}{sep}{ytok}"


_WEEKDAYS = {w.casefold() for w in WEEKDAYS}
_WORDY = re.compile(r"[A-Za-z]+\.?|\d+|[ ,\-]+", re.ASCII)


# The parts a month-name date may have, by the order of month (M) and numbers (N), each to the
# roles its numbers play given the first: a day (D) or a year (Y).
_WORDY_SHAPES = {
    "MN": lambda first: "Y" if len(first) == 4 else "D",
    "MNN": lambda first: "DY",
    "NM": lambda first: "Y" if len(first) == 4 else "D",
    "NMN": lambda first: "YD" if len(first) == 4 else "DY",
}


def _wordy_date(s: str, today: date):
    """A date with a month name — `Jan 15, 2024`, `15 Jan 2024`, `January 2024`, `Jan15`,
    `2024 Jan 15`, `Mon Jan 15 2024` — as ``(serial, pattern)`` or None.

    The pattern is the input with each part replaced by its token and every separator kept as
    typed: `Jan. 15, 2024` is `mmm. d, yyyy` and `Jan 15,2024` is `mmm d,yyyy`.

    Measured, the orders that read are month-day-year, month-year, day-month-year, year-month-day
    and year-month, a year after a day being two or four digits and any other year four; a day is
    one or two digits up to 31. Anything else is text: `Jan 32`, `Jan 99`, `Jan 100`,
    `Jan 2024 15`, `15 2024 Jan`."""
    parts = _WORDY.findall(s)
    if "".join(parts) != s:
        return None
    kinds, pattern, numbers = [], [], []
    month = None
    for p in parts:
        if p[0].isalpha():
            word, dot = (p[:-1], ".") if p.endswith(".") else (p, "")
            got = _month(word)
            if got is not None and month is None:
                month = got[0]
                kinds.append("M")
                pattern.append(got[1] + dot)
            elif not pattern and not dot and word.casefold() in _WEEKDAYS:
                pattern.append("ddd")
            else:
                return None
        elif p[0].isdigit():
            kinds.append("N")
            numbers.append(p)
            pattern.append(p)  # replaced by its token below
        else:
            pattern.append(p)
    shape = "".join(kinds)
    if month is None or shape not in _WORDY_SHAPES:
        return None
    roles = _WORDY_SHAPES[shape](numbers[0])
    day = year = None
    tokens = []
    for role, text in zip(roles, numbers):
        if role == "D":
            if len(text) > 2 or not 1 <= int(text) <= 31:
                return None
            day = int(text)
            tokens.append("dd" if text.startswith("0") else "d")
        else:
            if len(text) not in ((2, 4) if roles == "DY" else (4,)):
                return None
            year = _year(text, today)
            tokens.append(year[1])
    out, k = [], 0
    for piece in pattern:
        if piece and piece[0].isdigit():
            out.append(tokens[k])
            k += 1
        else:
            out.append(piece)
    serial = _serial(year[0] if year else today.year, month, day or 1)
    if serial is None:
        return None
    return serial, "".join(out)


_TIME = re.compile(
    r"(?P<sign>-)?(?P<h>\d+)(?::(?P<m>\d{2})(?::(?P<s>\d{2})(?P<f>\.\d+)?)?)?"
    r"(?: *(?P<ampm>[AaPp][Mm]))?\Z",
    re.ASCII,
)


def _time(s: str):
    """``(day_fraction, pattern)`` for a time of day or a duration, or None.

    Minutes and seconds are two digits and may overflow (`10:60` is 11:00); an hour alone needs
    AM/PM (`10 PM`), which spaces alone may set apart (`10:30\tPM` is text). A total of a day or
    more, or a negative one, is a duration shown as `[h]:mm:ss`. With AM/PM the hour is 1 to 12."""
    m = _TIME.match(s)
    if m is None or (m["m"] is None and m["ampm"] is None):
        return None
    h = int(m["h"])
    if m["ampm"]:
        if m["sign"] or not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if m["ampm"].casefold() == "pm" else 0)
    secs = Decimal(h * 3600 + int(m["m"] or 0) * 60 + int(m["s"] or 0)) + Decimal(m["f"] or 0)
    if m["sign"]:
        secs = -secs
    value = secs / 86400
    if m["ampm"]:
        pattern = "h:mm:ss am/pm" if m["s"] else "h:mm am/pm"
    elif value >= 1 or value < 0:
        pattern = "[h]:mm:ss"
    else:
        pattern = "h:mm:ss" if m["s"] else "h:mm"
    return value, pattern


def _date_or_time(s: str, today: date):
    """``(value, number_format)`` for a date, a time or both, or None.

    A date and a time are split at a run of spaces, tried from the right so `2024-01-15 10:30 PM`
    keeps `10:30 PM` whole, or at the `T` of `2024-01-15T10:30`; the pattern joins the two with a
    space either way. Measured: `1/15/2024  10:30` is a date-time, and a tab between them text."""
    t = _time(s)
    if t is not None:
        return t[0], _nf("TIME", t[1])
    d = _numeric_date(s, today) or _wordy_date(s, today)
    if d is not None:
        return d[0], _nf("DATE", d[1])
    splits = [(m.start(), m.end()) for m in reversed(list(re.finditer(" +", s))) if m.start()]
    iso = s.find("T")
    if iso > 0 and re.fullmatch(r"[0-9]{4}-[0-9]+-[0-9]+", s[:iso]):
        splits.append((iso, iso + 1))
    for i, j in splits:
        d = _numeric_date(s[:i], today) or _wordy_date(s[:i], today)
        t = _time(s[j:])
        if d is not None and t is not None:
            # A duration after a date adds to it, and keeps its `[h]`: measured, `1/15/2024 25:00`
            # is 1/16 01:00 as `m/d/yyyy [h]:mm:ss`, shown with the hours since the epoch.
            return float(d[0] + t[0]), _nf("DATE_TIME", f"{d[1]} {t[1]}")
    return None


def _number_value(v):
    v = float(v)
    return int(v) if v.is_integer() and abs(v) < 2**53 else v


def read(text: str, today: date):
    """What USER_ENTERED makes of a string: ``(kind, value, number_format)``.

    ``kind`` is how the result takes a cell's existing format (:func:`apply`): ``"empty"``,
    ``"apostrophe"``, ``"formula"``, ``"error"`` (``value`` is the error type), ``"bool"``,
    ``"number"`` or ``"string"``."""
    if text == "":
        return "empty", None, None
    if text[0] == "'":
        return "apostrophe", (text[1:] or None), None
    if text[0] in "=+":
        return "formula", text, None
    # ASCII letters alone fold, measured: `falſe` is a string
    if text.isascii() and text.lower() in ("true", "false"):
        return "bool", text.lower() == "true", None
    s = text.strip(_SPACE)
    if s.upper() in ERRORS:
        return "error", ERRORS[s.upper()], None
    if s:
        num = _number(s)
        if num is not None:
            return "number", num[0], num[1]
        dt = _date_or_time(s, today)
        if dt is not None:
            return "number", _number_value(dt[0]), dt[1]
    return "string", text, None


def _family(number_format: dict | None):
    return number_format and FAMILY[number_format["type"]]


def apply(before, entered, option: str, today: date):
    """The cell ``entered`` leaves behind over ``before`` under ``option``.

    Measured, a write keeps the cell's number format in these cases and otherwise replaces or
    drops it:

    * a JSON number or boolean, under either option, keeps it;
    * RAW: any string, ``""`` included, drops it, and ``""`` leaves an empty string
      (:data:`EMPTY_STRING`), as a lone apostrophe does;
    * USER_ENTERED: ``""`` clears the value and keeps it -- a cell that had none keeps an empty
      format (:data:`CLEARED`), and one never written stays so; an apostrophe drops
      it; a formula keeps
      it; an input that infers a format keeps the existing one when the two are of one family
      (:data:`FAMILY`) and takes the inferred one otherwise; a plain number, a
      boolean, an error or text keeps a number-family format and drops any other.
    """
    current = cell_format(before)
    if isinstance(entered, bool) or isinstance(entered, (int, float)):
        return cell(collapse(entered), number_format=current)
    if option == "RAW":
        return cell(entered) if entered != "" else dict(EMPTY_STRING)
    kind, value, inferred = read(entered, today)
    if kind == "empty":
        if current:
            return cell(None, number_format=current)
        return dict(CLEARED) if written(before) else None
    if kind == "apostrophe":
        return cell(value) if value is not None else dict(EMPTY_STRING)
    if kind == "formula":
        return cell(value, number_format=current)
    if inferred is not None:
        keep = _family(current) == _family(inferred)
        return cell(value, number_format=current if keep else inferred)
    keep = current if _family(current) == "number" else None
    if kind == "error":
        return cell(None, error=value, number_format=keep)
    return cell(value, number_format=keep)


# --- workbook -------------------------------------------------------------------------------------
#
# A spreadsheet as a write sees it: its sheets, each with the properties a write may state, and
# the rules for adding one. Every Sheets write loads a workbook, changes it here in memory, and
# saves it whole (`routers.google_sheets._sheets_save`), so an operation is a method on
# :class:`Workbook` rather than a query. `spreadsheets.create` and `sheets.copyTo` add sheets
# through :meth:`Workbook.add_sheet`, as the requests `spreadsheets.batchUpdate` carries would.

# A sheet's grid when nothing states another: measured, a new sheet and one added with only a
# title are both 1000 x 26.
GRID_ROWS = 1000
GRID_COLS = 26

# A new workbook's settings, which a workbook with none stated takes (`store`'s
# `gdrive_spreadsheets`). Measured: `ON_CHANGE` and `Etc/GMT` are what a freshly created spreadsheet
# answered with.
DEFAULT_PROPERTIES = {"locale": "en_US", "autoRecalc": "ON_CHANGE", "timeZone": "Etc/GMT"}

# Every locale a spreadsheet accepts, each with what a sheet is called when nothing names it
# (`{n}` its number) and what a copy is called (`{title}` the source's), in the language of the
# spreadsheet it lands in. Measured 2026-10-26 by setting each spelling Python's `locale_alias`
# knows on one spreadsheet, adding an unnamed sheet and copying one: a spelling is matched exactly,
# so `en_us`, `ko-KR` and `es_419` are refused, and `iw_IL` is accepted where `he_IL` is not.
LOCALES: dict[str, tuple[str, str]] = {
    locale: tuple(words)
    for locale, words in json.loads(
        (Path(__file__).parent / "data" / "sheets_locales.json").read_text(encoding="utf-8")
    ).items()
}

# Every `timeZone` a spreadsheet keeps a name for, as the name it serves back. Measured 2026-10-27
# by setting 640 spellings on one spreadsheet (every name Python's zone database and CLDR's alias
# table know, and case, offset and padding variants) and reading each back, and 20 of them through
# `spreadsheets.create` too, which agreed: a name is matched exactly and served under its CLDR
# name (`US/Pacific` as `America/Los_Angeles`, `Asia/Kolkata` as `Asia/Calcutta`, `UTC` as
# `Etc/UTC`). The rest are `Etc/GMT` -- `utc`, `asia/seoul`, `Factory`, `GMT+9`, `GMT+0900` --
# but for an offset spelled `GMT+9:00` (:data:`_CUSTOM_ZONE`), which is `Etc/Unknown`.
TIME_ZONES: dict[str, str] = json.loads(
    (Path(__file__).parent / "data" / "sheets_time_zones.json").read_text(encoding="utf-8")
)
_CUSTOM_ZONE = re.compile(r"GMT[+-](?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\Z")


def time_zone(name: str) -> str:
    """The `timeZone` a spreadsheet stated as ``name`` serves (:data:`TIME_ZONES`)."""
    if name in TIME_ZONES:
        return TIME_ZONES[name]
    return "Etc/Unknown" if _CUSTOM_ZONE.match(name) else DEFAULT_PROPERTIES["timeZone"]


# The `SheetProperties` and `GridProperties` fields a sheet keeps, in proto field order, which is
# the order real serves them in.
SHEET_FIELDS = ("gridProperties", "hidden", "tabColor", "rightToLeft", "tabColorStyle")
GRID_FIELDS = (
    "rowCount",
    "columnCount",
    "frozenRowCount",
    "frozenColumnCount",
    "hideGridlines",
    "rowGroupControlAfter",
    "columnGroupControlAfter",
)


def _rgb_order(color: dict) -> dict:
    return {k: color[k] for k in ("red", "green", "blue", "alpha") if k in color}


class Sheet(NamedTuple):
    """One sheet. ``properties`` is what a write stated past the id, title and index -- empty for
    a sheet a corpus stated, which is 1000 x 26 unless its grid is larger."""

    sheet_id: int
    index: int
    title: str
    grid: list[list]
    properties: dict = {}

    @property
    def rows(self) -> int:
        """The grid's height, never smaller than the data: as stated, else at least the default."""
        stated = self.properties.get("gridProperties", {}).get("rowCount")
        return max(stated or GRID_ROWS, len(self.grid))

    @property
    def cols(self) -> int:
        stated = self.properties.get("gridProperties", {}).get("columnCount")
        return max(stated or GRID_COLS, max((len(r) for r in self.grid), default=0))

    def served(self) -> dict:
        """The `SheetProperties` `spreadsheets.get` serves, in real's key order."""
        grid = self.properties.get("gridProperties", {})
        out: dict = {
            "sheetId": self.sheet_id,
            "title": self.title,
            "index": self.index,
            "sheetType": "GRID",
            "gridProperties": {
                "rowCount": self.rows,
                "columnCount": self.cols,
                **{k: grid[k] for k in GRID_FIELDS[2:] if grid.get(k)},
            },
        }
        out.update({k: self.properties[k] for k in SHEET_FIELDS[1:] if self.properties.get(k)})
        # a stored sheet's properties are JSON with sorted keys; a colour serves in field order
        if "tabColor" in out:
            out["tabColor"] = _rgb_order(out["tabColor"])
        if "rgbColor" in out.get("tabColorStyle", {}):
            out["tabColorStyle"] = {"rgbColor": _rgb_order(out["tabColorStyle"]["rgbColor"])}
        return out

    def stated(self) -> dict:
        """``properties`` with the grid's size written in, which is what a saved sheet keeps."""
        props = copy.deepcopy(self.properties)
        grid = props.setdefault("gridProperties", {})
        grid["rowCount"], grid["columnCount"] = self.rows, self.cols
        return props


def _one(mapped: str, c: str) -> str:
    """A case mapping that stays one character, as Java's per-character one does: `ß` upper-cases
    to itself there, not to `SS`."""
    return mapped if len(mapped) == 1 else c


def same_title(a: str, b: str) -> bool:
    """Whether two sheet titles collide: of one length, and character by character equal, or
    equal upper-cased, or equal lower-cased, as Java's `equalsIgnoreCase` compares. Measured
    2026-10-27 on pairs of titles in one create: `Same`/`same`, `ſheet`/`Sheet` and `ς`/`σ`
    collide, while `Straße`/`STRASSE`, `ﬁle`/`FILE` and `İ`/`i̇`, whose case mapping changes
    their length, do not."""
    return len(a) == len(b) and all(
        x == y
        or _one(x.upper(), x) == _one(y.upper(), y)
        or _one(x.lower(), x) == _one(y.lower(), y)
        for x, y in zip(a, b)
    )


class Workbook:
    """A spreadsheet's sheets in index order and its settings (:data:`DEFAULT_PROPERTIES`)."""

    def __init__(self, file_id: str, sheets: list[Sheet], properties: dict | None = None):
        self.file_id = file_id
        self.sheets = list(sheets)
        self.properties = {**DEFAULT_PROPERTIES, **(properties or {})}

    @property
    def locale(self) -> str:
        return self.properties["locale"]

    def sheet_by_id(self, sheet_id: int) -> Sheet | None:
        return next((s for s in self.sheets if s.sheet_id == sheet_id), None)

    def title_taken(self, title: str) -> bool:
        """Whether a sheet already has this title (:func:`same_title`)."""
        return any(same_title(s.title, title) for s in self.sheets)

    def default_title(self) -> str:
        """The first `Sheet<n>` in the workbook's language (:data:`LOCALES`) no sheet has.
        Measured: the first sheet of a new workbook is `Sheet1` under `en_US`, `시트1` under
        `ko_KR`, `Hoja 1` under `es`."""
        word = LOCALES[self.locale][0]
        n = 1
        while self.title_taken(word.format(n=n)):
            n += 1
        return word.format(n=n)

    def copy_title(self, title: str) -> str:
        """What a copy of a sheet titled ``title`` is called here. Measured: `Copy of Other` in an
        `en_US` workbook, then `Copy of Other 1`, `Copy of Other 2`; `Other의 사본` and `Other의 사본
        1` in a `ko_KR` one -- the destination's language, not the source's."""
        base = LOCALES[self.locale][1].format(title=title)
        if not self.title_taken(base):
            return base
        n = 1
        while self.title_taken(f"{base} {n}"):
            n += 1
        return f"{base} {n}"

    def new_sheet_id(self, title: str) -> int:
        """A pseudo-random id no sheet here has, as real gives every sheet but a new workbook's
        only one (:func:`synth.sheet_id`)."""
        taken = {s.sheet_id for s in self.sheets}
        salt = ""
        while (sid := synth.sheet_id(self.file_id, title, salt)) in taken:
            salt += "+"
        return sid

    def add_sheet(self, sheet: Sheet, index: int | None = None) -> Sheet:
        """Put ``sheet`` at ``index`` (last when None), renumbering the sheets after it, and return
        it as placed. Its id and title are the caller's to have checked."""
        at = len(self.sheets) if index is None else index
        placed = sheet._replace(index=at)
        self.sheets.insert(at, placed)
        self.sheets = [s._replace(index=i) for i, s in enumerate(self.sheets)]
        return self.sheets[at]
