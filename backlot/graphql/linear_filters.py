"""Compile a Linear ``IssueFilter`` / ``CommentFilter`` into a SQL fragment.

Linear's filter inputs are comparator objects nested under field names —
``{state: {name: {eq: "Done"}}, priority: {lte: 2}}`` — combined with ``and`` / ``or``. This
turns one into ``("(...)", [params])`` that :mod:`backlot.store` pushes into the WHERE clause, so a
filtered query stays an indexed scan instead of materializing the whole team and filtering in
Python.

Two design rules make the result trustworthy rather than merely convenient:

- **Declared means implemented.** ``schemas``-side, ``backlot/graphql/linear.graphql`` declares only
  the filter fields compiled here, so anything Linear accepts and this does not is a GraphQL
  validation error naming the field — never a filter that is silently dropped and answered with
  a full, wrong result set. :func:`compile_issue_filter` still raises on an unknown key, so the
  SDL and this module cannot drift apart unnoticed.
- **Derived fields are expanded, not faked.** ``state.type`` and ``team.key`` have no column:
  both are pure functions of a column whose distinct values are a handful of rows, so they
  compile to an ``IN`` over the names that satisfy the predicate. That is exact, not approximate.

The date comparators take Linear's ``DateTimeOrDuration`` / ``TimelessDateOrDuration`` scalars, whose
grammar (ISO 8601, year and year-month shortcuts, and ISO 8601 durations relative to now) is
implemented at the top of this module and bound onto the scalars themselves, so a bad operand is
the same validation error Linear answers.
"""

from __future__ import annotations

import calendar
import datetime as _dt
import re

from graphql import GraphQLError
from graphql.language import StringValueNode

from backlot import store, synth

# --- the date operands: Linear's `DateTimeOrDuration` and `TimelessDateOrDuration` ------------
# Every date comparator in Linear's schema takes one of these two scalars, not a plain `DateTime`.
# Linear's own description of `DateTimeOrDuration`: "Represents a date and time in ISO 8601 format.
# Accepts shortcuts like `2021` to represent midnight Fri Jan 01 2021. Also accepts ISO 8601
# durations strings which are added to the current date to create the represented date (e.g
# '-P2W1D' represents the date that was two weeks and 1 day ago)". `TimelessDate` carries the
# identical description.
#
# Measured against api.linear.app on 2026-09-03, `createdAt: {gt: X}` for each X:
#   accepted  "2021"  "2021-03"  "2021-03-05 10:00"  "2026-09-01"  "2026-09-01T05:30:31.786Z"
#             "1"  "P1D"  "-P1D"  "PT1H"  "-PT1H"  "P1M"  "-P1Y2M3W4DT5H6M7S"
#   rejected  "1700000000"  "20210305"  "P"  "2021-13-01"  ""   -- each a 400 validation error:
#             `Expected value of type "DateTimeOrDuration", found "P"; Unable to parse value 'P'
#             into a valid date`
#   rejected  a non-string, literal or variable -- `DateTimeOrDuration supports only string values`
# So a bare epoch is NOT accepted (an earlier version of this module took one), digits alone are a
# YEAR, and the rejection happens at validation, before any resolver runs. The parsers below are
# wired in as the scalars' own coercion (``SCALARS``, bound by ``linear_resolvers.build_engine``) so
# a bad operand is the same 400 here. They raise ``ValueError``, not ``GraphQLError``, on purpose:
# graphql-core (like graphql-js, which Linear runs) reports a GraphQLError from a scalar verbatim
# but wraps any other exception as `Expected value of type 'DateTimeOrDuration', found "P"; <message>`
# -- the envelope the measurement above shows.
#
# `TimelessDateOrDuration` (2026-09-03, against an issue due 2026-03-15 created for the purpose and
# deleted after): the same grammar, read down to the UTC day. `eq` matched for "2026-03-15",
# "2026-03-15T00:00:00Z", "2026-03-15T23:59:59Z" and "2026-03-16T02:00:00+09:00" (17:00Z on the
# 15th); it did NOT match for "2026-03-15T23:59:59-05:00" (04:59Z on the 16th) or
# "2026-03-15T02:00:00+09:00" (17:00Z on the 14th). `gte: "2026-03-15T12:00:00Z"` matched and
# `gt: "2026-03-15T00:00:00Z"` did not, so the operand is truncated before the comparison rather
# than compared as an instant. "2026-03" is the first of the month (`eq` misses, `gte` hits), a
# duration is relative to today (`lt: "P0D"` hits a past due date). The scalar names itself
# `TimelessDate` in its errors: `Unable to parse literal value of kind 'IntValue'. TimelessDate
# supports only 'StringValue' ones`, and for a variable `Unable to parse value '1'. TimelessDate
# supports only string values` (quoted, where DateTimeOrDuration prints the value bare).

_DURATION = re.compile(
    r"^(?P<sign>[+-])?P"
    r"(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)
_DURATION_PARTS = ("years", "months", "weeks", "days", "hours", "minutes", "seconds")
_TIME = re.compile(r"[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?")


def _now() -> _dt.datetime:
    """The instant a duration operand is relative to. A function, so a test can pin it."""
    return _dt.datetime.now(_dt.timezone.utc)


def _add_months(when: _dt.datetime, months: int) -> _dt.datetime:
    """Calendar months, clamping the day the way every date library does (Jan 31 + 1M = Feb 28)."""
    index = when.year * 12 + (when.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    day = min(when.day, calendar.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


def _from_duration(m: re.Match, value: str) -> _dt.datetime:
    sign = -1 if m.group("sign") == "-" else 1
    part = {k: float(m.group(k) or 0) for k in _DURATION_PARTS}
    try:
        when = _add_months(_now(), sign * int(part["years"] * 12 + part["months"]))
        return when + sign * _dt.timedelta(
            weeks=part["weeks"],
            days=part["days"],
            hours=part["hours"],
            minutes=part["minutes"],
            seconds=part["seconds"],
        )
    except (ValueError, OverflowError):
        # A duration that leaves the calendar ("P9999Y", "P99999999999D") is not a date either;
        # the answer is the scalar's own message, not the interpreter's.
        raise ValueError(f"Unable to parse value {value!r} into a valid date") from None


def parse_datetime_or_duration(value, scalar: str = "DateTimeOrDuration") -> _dt.datetime:
    """One ``DateTimeOrDuration`` operand as an aware ``datetime``, or a ``ValueError`` whose
    message is the one Linear's scalar produces (see the measurements above). ``scalar`` is the
    name the message carries: ``TimelessDateOrDuration`` calls itself ``TimelessDate`` there."""
    if not isinstance(value, str):
        # Measured: DateTimeOrDuration prints the value bare (`value 1700000000.`), TimelessDate
        # quotes it (`value '1'.`).
        shown = repr(value) if scalar == "DateTimeOrDuration" else f"'{value}'"
        raise ValueError(f"Unable to parse value {shown}. {scalar} supports only string values")
    s = value.strip()
    m = _DURATION.match(s)
    if m is not None:
        if not any(m.group(k) for k in _DURATION_PARTS):
            raise ValueError(f"Unable to parse value {value!r} into a valid date")  # bare "P"
        return _from_duration(m, value)
    # The extended (hyphenated) form only: a year ("2021", and Linear also takes "1"), a
    # year-month, or a full date with an optional time after `T` or a space. Linear rejected the
    # basic form "20210305" and the hour-only time "2021-03-05T10" (measured), so those and the week
    # form Python's parser would accept ("20210305T100000", "2021-W10") are refused before it sees
    # them, as is an epoch. A time is at least HH:MM; seconds, a fraction, `Z` or an offset
    # ("2021-03-05T10:00:00+09:00" was accepted) may follow.
    date = re.fullmatch(r"(\d{1,4})(?:-(\d{2})(?:-(\d{2}))?)?(.*)", s, re.S)
    if date is None:
        raise ValueError(f"Unable to parse value {value!r} into a valid date")
    if date.group(4) and not (date.group(3) and _TIME.fullmatch(date.group(4))):
        raise ValueError(f"Unable to parse value {value!r} into a valid date")
    if date.group(2) is None:
        s = f"{int(date.group(1)):04d}-01-01"
    elif date.group(3) is None:
        s = s + "-01"
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Unable to parse value {value!r} into a valid date") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)


def parse_timeless_date_or_duration(value) -> str:
    """One ``TimelessDateOrDuration`` operand as the bare ``YYYY-MM-DD`` the ``due_date`` column
    holds. Linear documents the scalar with the same text as ``DateTimeOrDuration``, accepts the
    same spellings, and reads an operand that carries a time down to its UTC day (measured, see the
    module comment: "2026-03-16T02:00:00+09:00" is the 15th, "2026-03-15T23:59:59-05:00" is not)."""
    when = parse_datetime_or_duration(value, "TimelessDate")
    return when.astimezone(_dt.timezone.utc).date().isoformat()


def _parse_literal(parse, scalar: str, node, _variables=None):
    """The literal half of a scalar's coercion. Linear's scalars take a string literal only, and
    say so: `Unable to parse literal value of kind 'IntValue'. DateTimeOrDuration supports only
    'StringValue' ones` (measured 2026-09-03; the other scalar says `TimelessDate`)."""
    if not isinstance(node, StringValueNode):
        raise ValueError(
            f"Unable to parse literal value of kind '{type(node).__name__.removesuffix('Node')}'. "
            f"{scalar} supports only 'StringValue' ones"
        )
    return parse(node.value)


# What ``linear_resolvers.build_engine`` binds onto the two scalars: parse the operand up front, so
# a filter reaches the compiler below carrying a ``datetime`` / a ``YYYY-MM-DD`` string, and a bad
# operand never reaches it at all.
SCALARS = {
    "DateTimeOrDuration": {
        "parse_value": parse_datetime_or_duration,
        "parse_literal": lambda node, variables=None: _parse_literal(
            parse_datetime_or_duration, "DateTimeOrDuration", node, variables
        ),
    },
    "TimelessDateOrDuration": {
        "parse_value": parse_timeless_date_or_duration,
        "parse_literal": lambda node, variables=None: _parse_literal(
            parse_timeless_date_or_duration, "TimelessDate", node, variables
        ),
    },
}


def _to_epoch(value) -> int | None:
    """A ``DateTimeOrDuration`` operand -> unix seconds, to compare against a ``*_ts`` column.
    Normally already a ``datetime`` (the scalar coerced it); a string is parsed the same way, for a
    caller that hands this module a filter without going through the schema."""
    if value is None or value == "":
        return None
    if not isinstance(value, _dt.datetime):
        try:
            value = parse_datetime_or_duration(value)
        except ValueError as e:
            raise GraphQLError(str(e)) from None
    return int(value.timestamp())


def _to_timeless(value) -> str | None:
    """A ``TimelessDateOrDuration`` operand -> the ``YYYY-MM-DD`` the column holds."""
    if value is None or value == "":
        return None
    try:
        return parse_timeless_date_or_duration(value)
    except ValueError as e:
        raise GraphQLError(str(e)) from None


# A UUID as Linear's id comparators validate it: 8-4-4-4-12 hex with an RFC 4122 variant nibble
# (8, 9, a or b). Measured 2026-09-03 on both `IssueFilter.id` and `NullableProjectFilter.id`: the nil
# UUID, `1111…1111` (variant 1) and `ffff…ffff` (variant f) answer `Argument Validation Error`
# (code INVALID_INPUT), while `…-4111-8111-…` (v4), `…-1111-8111-…` (v1) and `…-5111-9111-…` (v5)
# are looked up -- so the version nibble is not checked, the variant is. Every id Backlot serves is
# a v4 with variant 8-b (`synth._uuid_from`), so nothing of its own is refused.
UUID_SHAPE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_UUID_LIKE = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def argument_validation_error() -> GraphQLError:
    """The error Linear's id comparators answer for a value they refuse: a 200 whose `errors` carry
    `Argument Validation Error` with code INVALID_INPUT (measured 2026-09-03)."""
    return GraphQLError("Argument Validation Error", extensions={"code": "INVALID_INPUT"})


def _refuse_malformed_uuids(spec: dict | None) -> None:
    """`EntityIdentifierIDComparator` (``project.id``): any string is looked up -- `"PROJ-1"`,
    `"not-a-uuid"` and an unknown UUID each answered an empty page -- EXCEPT something shaped like a
    UUID with a variant Linear's validator rejects, which is refused before the lookup."""
    for op, raw in (spec or {}).items():
        for value in raw if isinstance(raw, list) else [raw]:
            if isinstance(value, str) and _UUID_LIKE.match(value) and not UUID_SHAPE.match(value):
                raise argument_validation_error()


def _refuse_malformed_project_ids(spec: dict | None) -> None:
    """:func:`_refuse_malformed_uuids` over every ``id`` a ``ProjectFilter`` reaches, the nested ones
    included: ``_sub_filter`` follows ``and`` / ``or`` down to the same lookup, so a value refused at
    the top level was quietly looked up (and matched nothing) one level down. The issue-id side,
    ``_resolve_issue_ids``, already recurses this way."""
    for sub in _through_and_or(spec):
        _refuse_malformed_uuids(sub.get("id"))


def _through_and_or(spec):
    """``spec`` and every filter object nested under its ``and`` / ``or``, depth first."""
    if not isinstance(spec, dict):
        return
    yield spec
    for key in ("and", "or"):
        for sub in spec.get(key) or []:
            yield from _through_and_or(sub)


# A comparator key -> how it becomes SQL, given a column expression and the value.
# `%` / `_` in a LIKE needle are escaped so a user-supplied value stays literal.
_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def _like(value: str) -> str:
    return str(value).translate(_LIKE_ESCAPE)


class _Comparator:
    """Renders one comparator object (``{eq: …, contains: …}``) against a column.

    NULL under a negative operator follows what api.linear.app answered on 2026-09-03, over issues
    whose ``estimate``, ``dueDate`` and ``completedAt`` were null:

    * on a FIELD of the issue, ``neq`` DROPS the null rows (`estimate: {neq: 99}` over four null
      estimates answered nothing) and ``nin`` KEEPS them (`estimate: {nin: [99]}` answered all four;
      `dueDate: {nin: ["2026-03-15"]}` answered exactly the issues with no due date);
    * on a RELATION (``project: {name: {neq: …}}``, ``assignee: {name: {neq: …}}``,
      ``assignee: {id: {neq: …}}``, and ``nin`` alike) issues WITHOUT the relation are kept.

    ``relation=True`` is the second rule, for the columns ``_sub_filter`` maps a nested filter onto.
    Neither rule is SQL's own three-valued one (`<>` and `NOT IN` both drop nulls), so both are
    spelled out here rather than left to the engine.
    """

    def __init__(
        self,
        col: str,
        *,
        text: bool = False,
        epoch: bool = False,
        timeless: bool = False,
        relation: bool = False,
    ):
        self.col, self.text, self.epoch, self.timeless = col, text, epoch, timeless
        self.relation = relation

    def _not_equal(self, expr: str) -> str:
        """``expr`` is a ``<>`` test; on a relation the absent rows pass it too."""
        return f"({self.col} IS NULL OR {expr})" if self.relation else expr

    def _value(self, v):
        # A DateComparator's operand is a DateTimeOrDuration but the column is unix seconds; a
        # NullableTimelessDateComparator's is a TimelessDateOrDuration and the column a YYYY-MM-DD.
        if self.epoch:
            return _to_epoch(v)
        if self.timeless:
            return _to_timeless(v)
        return v

    def render(self, spec: dict) -> tuple[str, list]:
        parts: list[str] = []
        params: list = []
        for op, raw in spec.items():
            if raw is None and op in ("and", "or"):
                continue
            if raw is None and op == "null":
                # Measured 2026-09-03: on a comparator, an explicit null operand on `null` itself
                # reads as `null: true`, not as `false` and not as the condition-nothing-passes
                # below. `dueDate: {null: null}` answered every issue over four with no due date
                # (so not "nothing passes"), `priority: {null: null}` none over four that all carry
                # a priority, and `{or: [{priority: {null: null}}, {title: {eq: T}}]}` the title
                # match alone (so not a vacuous key either). Beside a sibling it still reads as
                # true: `priority: {null: null, eq: 0}` answered none over those four, as
                # `{null: true, eq: 0}` does and `{null: false, eq: 0}` (all four) does not. This
                # is the comparator's rule only -- on a relation (`_sub_filter`) and on the label
                # collection (`_labels_filter`) the same key is dropped.
                raw = True
            if raw is None and op != "null":
                # A null operand is a condition, not an absent one. Measured against
                # api.linear.app on 2026-09-03: a comparison with null (`eq`, `neq`, `lt`, `lte`, `gt`, `gte`)
                # matches nothing, on a nullable field too -- `dueDate: {eq: null}` answered none
                # over issues with no due date, `estimate: {gte: null}` none -- while a string or
                # list operator with null (`contains`, `startsWith`, `eqIgnoreCase`, `in`, `nin`,
                # ...) is a condition every row passes: `title: {contains: null}` answered every
                # issue, and `labels: {some: {name: {contains: null}}}` the labelled ones only,
                # which is what tells a passing condition from no condition (`{}`) at all.
                parts.append("0" if op in _NULL_IS_FALSE else "1")
                continue
            if op in ("and", "or"):
                # `EstimateComparator` nests plain comparators under `and` / `or`. Each is
                # rendered against the same column; an empty list constrains nothing.
                subs = [self.render(x) for x in raw]
                frags = [f for f, _ in subs if f]
                for _, p in subs:
                    params.extend(p)
                if frags:
                    parts.append("(" + (" AND " if op == "and" else " OR ").join(frags) + ")")
                continue
            # `in` / `nin` carry a list and coerce per element below, and `null` carries a
            # Boolean; running either through a date operand's coercion hands the scalar something
            # that is not a date (the old code did, so `completedAt: {null: true}` was an error).
            v = raw if op in ("in", "nin", "null") else self._value(raw)
            if op == "eq":
                parts.append(f"{self.col} = ?")
                params.append(v)
            elif op == "neq":
                parts.append(self._not_equal(f"{self.col} <> ?"))
                params.append(v)
            elif op == "lt":
                parts.append(f"{self.col} < ?")
                params.append(v)
            elif op == "lte":
                parts.append(f"{self.col} <= ?")
                params.append(v)
            elif op == "gt":
                parts.append(f"{self.col} > ?")
                params.append(v)
            elif op == "gte":
                parts.append(f"{self.col} >= ?")
                params.append(v)
            elif op in ("in", "nin"):
                vals = [self._value(x) for x in (raw or [])]
                if not vals:
                    parts.append("0" if op == "in" else "1")
                    continue
                marks = ",".join("?" for _ in vals)
                if op == "in":
                    parts.append(f"{self.col} IN ({marks})")
                else:
                    # Null rows pass `nin` on a field and on a relation alike (measured; see the
                    # class docstring), which a bare `NOT IN` would drop.
                    parts.append(f"({self.col} IS NULL OR {self.col} NOT IN ({marks}))")
                params += vals
            elif op == "contains":
                parts.append(f"{self.col} LIKE ? ESCAPE '\\'")
                params.append(f"%{_like(v)}%")
            elif op == "containsIgnoreCase":
                # SQLite LIKE is already ASCII case-insensitive; lower() on both sides keeps it
                # correct for non-ASCII too, at the cost of the index (a contains scan anyway).
                parts.append(f"lower({self.col}) LIKE lower(?) ESCAPE '\\'")
                params.append(f"%{_like(v)}%")
            elif op == "startsWith":
                parts.append(f"{self.col} LIKE ? ESCAPE '\\'")
                params.append(f"{_like(v)}%")
            elif op == "endsWith":
                parts.append(f"{self.col} LIKE ? ESCAPE '\\'")
                params.append(f"%{_like(v)}")
            elif op == "eqIgnoreCase":
                parts.append(f"lower({self.col}) = lower(?)")
                params.append(v)
            elif op == "neqIgnoreCase":
                parts.append(self._not_equal(f"lower({self.col}) <> lower(?)"))
                params.append(v)
            elif op == "null":
                parts.append(f"{self.col} IS {'NULL' if raw else 'NOT NULL'}")
            else:
                raise GraphQLError(f"unsupported comparator {op!r} on this filter field")
        return _join(parts, "AND"), params


# The comparison operators, which a null operand turns into a condition nothing passes (see
# `_Comparator.render`); every other operator with a null operand is one every row passes.
_NULL_IS_FALSE = frozenset({"eq", "neq", "lt", "lte", "gt", "gte"})


def _join(parts: list[str], op: str) -> str:
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return "(" + f" {op} ".join(parts) + ")"


def _distinct(conn, column: str) -> list[str]:
    """The distinct values of a low-cardinality column (``state``, ``team``, 6 and 3 rows in the
    corpora). Read once per filter compile — cheap, and it is what makes the derived-field
    expansion below exact."""
    return [
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT {column} FROM linear_issues WHERE {column} IS NOT NULL"
        )
    ]


def _derived_in(conn, column: str, derive, spec: dict) -> tuple[str, list]:
    """Compile a comparator over a *derived* value (``state.type``, ``team.key``) by evaluating
    the derivation over the column's distinct values and emitting an ``IN`` over the names that
    match. Exact, because the derivation is a pure function of the column."""
    names = _distinct(conn, column)
    # Reuse the comparator machinery by testing each derived value against it in Python.
    matched = [n for n in names if _matches(derive(n), spec)]
    # A NEGATIVE predicate ("not this project") must keep rows whose column is NULL — they have
    # no relation, so they cannot be the excluded one, and that is what Linear answers for
    # `project: {name: {neq: …}}` (measured 2026-09-03; see the `_Comparator` docstring). The
    # relation-mode comparator spells it out (`IS NULL OR <> ?`); an IN-list over distinct non-NULL
    # names silently dropped them, so `project:{id:{neq:X}}` and `project:{name:{neq:X}}` disagreed
    # on 24 real rows.
    negative = any(op in spec for op in ("neq", "nin", "neqIgnoreCase"))
    null_ok = f" OR {column} IS NULL" if negative else ""
    if not matched:
        return (f"{column} IS NULL", []) if negative else ("0", [])
    marks = ",".join("?" for _ in matched)
    return f"({column} IN ({marks}){null_ok})", list(matched)


def _matches(value: str, spec: dict) -> bool:
    """Evaluate a StringComparator against a single Python value (used only by
    :func:`_derived_in`, over a handful of distinct names)."""
    for op, raw in spec.items():
        if op == "eq" and value != raw:
            return False
        if op == "neq" and value == raw:
            return False
        if op == "in" and value not in (raw or []):
            return False
        if op == "nin" and value in (raw or []):
            return False
        if op == "contains" and str(raw) not in value:
            return False
        if op == "containsIgnoreCase" and str(raw).lower() not in value.lower():
            return False
        if op == "startsWith" and not value.startswith(str(raw)):
            return False
        if op == "endsWith" and not value.endswith(str(raw)):
            return False
        if op == "eqIgnoreCase" and value.lower() != str(raw).lower():
            return False
        if op == "neqIgnoreCase" and value.lower() == str(raw).lower():
            return False
    return True


# What one `IssueLabelFilter` compiles to, besides a fragment. Four kinds, because Linear answers
# them differently and the difference only shows inside an `and` / `or` (see `_label_predicate`):
_NONE = "none"  # nothing here: `{}`, `and: []`, every key null -- dropped from any list
_QUANT = "quant"  # `or: []`: no condition, but a predicate -- the quantifier still asks for a label
_VACUOUS = "vacuous"  # a key that constrains nothing (`name: {}`) -- makes the whole `or` constrain nothing
_REAL = "real"  # a condition


def _label_predicate(spec: dict) -> tuple[str, list, str]:
    """One ``IssueLabelFilter`` against the ``value`` column of a ``json_each`` row, INCLUDING its
    ``and`` / ``or``. Returns ``(fragment, params, kind)``; the kind is one of the four above.

    An empty fragment is right in two places and wrong in a third, which is what the kind is for.
    At the top of a quantifier it is right: ``some: {}`` and ``some: {name: {}}`` each answered every
    issue on api.linear.app, label-less ones included, so ``_labels_predicate`` compiles them to
    nothing. Inside an ``and`` it is right too: ``some: {and: [{}, {name: {eq: A}}]}`` and
    ``some: {and: [{name: {}}, {name: {eq: A}}]}`` each answered exactly what ``{name: {eq: A}}``
    does. Inside an ``or`` it is wrong, and which way depends on WHY the branch is empty, measured on
    2026-09-03 over two labelled issues (``[a, z]``, ``[a]``) beside four with no labels:

    * a branch that constrains nothing (``{name: {}}``, ``{and: [{}]}``) makes the WHOLE ``or``
      constrain nothing: ``every: {or: [{name: {}}, {name: {eq: z}}]}`` answered every issue.
    * a branch with nothing in it (``{}``, ``{and: []}``) is dropped, and the ``or`` reads as a
      NEGATIVE predicate (see ``_reads_as_negation``): ``some: {or: [{}, {name: {eq: z}}]}``
      answered ``[a, z]`` and the four label-less issues, ``every: {or: [{}, {name: {eq: z}}]}``
      the label-less four alone -- the answers ``some`` / ``every: {name: {neq: a}}`` give.
    * a branch that is a literally empty ``or`` is dropped too, and reads POSITIVE:
      ``some: {or: [{or: []}, {name: {eq: z}}]}`` answered ``[a, z]`` alone.
    * a literally empty ``or`` at the top is a predicate every label satisfies, and the quantifier
      still asks for one: ``some: {or: []}`` and ``every: {or: []}`` answered the labelled issues.
    * an ``or`` whose every branch is dropped (``or: [{}]``) constrains nothing.
    """
    parts: list[str] = []
    params: list = []
    kinds: list[str] = []
    for key, sub in (spec or {}).items():
        if sub is None:
            continue
        if key == "name":
            frag, p = _Comparator("value").render(sub)
            kind = _REAL if frag else _VACUOUS
        elif key in ("and", "or"):
            frag, p, kind = _label_branches(key, sub)
        else:
            raise GraphQLError(f"unsupported label filter field {key!r}")
        kinds.append(kind)
        if kind == _REAL:
            parts.append(frag)
            params.extend(p)
    if parts:
        return _join(parts, "AND"), params, _REAL
    for kind in (_QUANT, _VACUOUS):
        if kind in kinds:
            return "", [], kind
    return "", [], _NONE


def _label_branches(key: str, sub: list) -> tuple[str, list, str]:
    """The ``and`` / ``or`` of an ``IssueLabelFilter``, by the rules in ``_label_predicate``."""
    if not sub:
        return "", [], _QUANT if key == "or" else _NONE
    subs = [_label_predicate(x) for x in sub]
    if key == "or" and any(kind == _VACUOUS for _, _, kind in subs):
        return "", [], _VACUOUS
    real = [(f, p) for f, p, kind in subs if kind == _REAL]
    if not real:
        return "", [], _VACUOUS
    frag = "(" + (" AND " if key == "and" else " OR ").join(f for f, _ in real) + ")"
    return frag, [x for _, p in real for x in p], _REAL


# The operators that read as a negation. What the quantifier does with an issue that has NO labels
# depends on this (see `_labels_predicate`), so a compound predicate has to carry a polarity too:
# `and` is negative when every branch is, `or` when any branch is, and a comparator when every
# operator in it is -- `{eq: A, neq: B}` reads positive (measured 2026-09-03; see the test matrix).
# "Every operator of none" is true, so an empty branch reads as negative and `or: [{}, P]` is P
# read negatively, while `or: []` (any branch of none) reads positive -- both measured the same
# day, see `_label_predicate`. A null operand keeps its operator's polarity: `{neqIgnoreCase: null}` under
# `some` answered every issue (a negative predicate every label passes), `{eq: null}` none.
_NEGATIVE_OPS = frozenset(
    {
        "neq",
        "nin",
        "neqIgnoreCase",
        "notContains",
        "notContainsIgnoreCase",
        "notStartsWith",
        "notEndsWith",
    }
)
_LABEL_COUNT = "json_array_length(COALESCE(labels, '[]'))"
_LABEL_EACH = "json_each(COALESCE(labels, '[]'))"


def _reads_as_negation(spec: dict | None) -> bool:
    """Whether an ``IssueLabelFilter`` is a negative predicate, by the rule above."""
    parts = []
    for key, sub in (spec or {}).items():
        if sub is None:
            continue
        if key == "name":
            parts.append(all(op in _NEGATIVE_OPS for op in sub))
        elif key == "and":
            parts.append(all(_reads_as_negation(x) for x in sub))
        elif key == "or":
            parts.append(any(_reads_as_negation(x) for x in sub))
    return all(parts)


def _labels_predicate(spec: dict, *, every: bool) -> tuple[str, list]:
    """``labels: {some|every: {…}}`` over the JSON ``labels`` column, the way api.linear.app answers
    it (measured 2026-09-03 against two labelled issues, ``[a, b]`` and ``[a]``, beside four with
    no labels -- the full matrix is ``tests/test_linear.py::test_labels_quantifiers_answer_as_linear``).

    Linear's quantifiers are not the textbook ones. The polarity of the predicate decides what an
    issue with NO labels answers:

    * ``some`` with a positive predicate (``eq``, ``in``, ``contains``, …) is an EXISTS: an issue
      with no labels never matches.
    * ``every`` with a positive predicate is a NON-vacuous "all": the issue has to have at least one
      label, and every one of them satisfies the predicate. ``every: {name: {eq: A}}`` answered the
      ``[a]`` issue alone -- not the label-less ones, which a vacuous "all" would include.
    * ``some`` with a NEGATIVE predicate (``neq``, ``nin``, ``notContains``, …) is the complement of
      that: the issue has no labels, OR one of its labels satisfies it. ``some: {name: {neq: A}}``
      answered ``[a, b]`` and every label-less issue, and not ``[a]``.
    * ``every`` with a negative predicate is the textbook, vacuously true "all": no label fails it,
      and an issue with no labels qualifies. ``every: {name: {neq: A}}`` answered the label-less
      issues alone.

    So each negative form is the negation of the positive form of the other quantifier -- Linear
    pushes the ``not`` outside the quantifier -- and :func:`_reads_as_negation` says which form a
    compound predicate takes. A predicate that constrains nothing (``some: {}``, ``some: {and: []}``,
    ``some: {name: {}}``) is no predicate: every issue answered, label-less ones included, so it
    compiles to nothing here too. A literally empty ``or`` is a predicate every label satisfies
    (see :func:`_label_predicate`), so the quantifier still applies to it."""
    inner, params, kind = _label_predicate(spec)
    if kind in (_NONE, _VACUOUS):
        return "", []
    if kind == _QUANT:
        inner = "1"
    negative = _reads_as_negation(spec)
    if every:
        no_label_fails = f"NOT EXISTS (SELECT 1 FROM {_LABEL_EACH} WHERE NOT {inner})"
        return (
            no_label_fails if negative else f"({_LABEL_COUNT} > 0 AND {no_label_fails})"
        ), params
    a_label_matches = f"EXISTS (SELECT 1 FROM {_LABEL_EACH} WHERE {inner})"
    return (f"({_LABEL_COUNT} = 0 OR {a_label_matches})" if negative else a_label_matches), params


# The keys of an `IssueLabelCollectionFilter` are not ANDed: one of them answers and the rest are
# ignored, in this order (see `_labels_filter`).
_LABELS_PRECEDENCE = ("and", "or", "length", "every", "some", "name")


def _labels_filter(spec: dict) -> tuple[str, list]:
    """One ``IssueLabelCollectionFilter``, the way api.linear.app answered it on 2026-09-03 over
    two labelled issues (``[a, z]``, ``[a]``) beside four with no labels.

    Its keys do not AND together. ``and`` answers if present, else ``or``, else the first present of
    ``length``, ``every``, ``some``, ``name``, and the others are ignored whatever their order:
    ``{and: [{length: {eq: 2}}], or: [{length: {eq: 1}}]}`` answered ``[a, z]`` (an AND would be
    empty), ``{or: [{length: {eq: 0}}], length: {eq: 2}}`` the four label-less issues,
    ``{length: {eq: 1}, some: {name: {eq: z}}}`` ``[a]``, ``{some: {name: {eq: z}}, every: {name:
    {eq: a}}}`` ``[a]``, and ``{name: {eq: z}, some: {name: {eq: a}}}`` both labelled issues. The one
    key that does AND is ``null``, and only beside those four: ``null: true`` answers nothing (an
    issue's label collection is never null, not even an empty one), ``null: false`` everything, and
    ``{or: [{length: {eq: 0}}], null: true}`` answered the label-less four -- the ``or`` alone.
    ``null: null`` is no key at all, here as on a relation: ``{null: null}`` answered every issue
    and ``{null: null, length: {eq: 0}}`` the label-less four (measured 2026-09-03; a comparator
    reads the same operand as ``null: true``, see ``_Comparator.render``).

    ``length`` is a ``NumberComparator`` over the label count (``{eq: 0}`` answered the label-less
    issues, ``{eq: 2}`` the two-label one); a bare ``name`` answers as ``some`` does; an empty
    object constrains nothing. Inside ``and`` a branch that constrains nothing is dropped
    (``{and: [{}, {length: {eq: 2}}]}`` answered ``[a, z]``); inside ``or`` it makes the whole
    filter constrain nothing (``{or: [{}, {length: {eq: 2}}]}`` and ``{or: [{some: {}}, {length:
    {eq: 2}}]}`` each answered every issue), and so does an ``and`` / ``or`` with nothing left in
    it (``{and: [{}], length: {eq: 2}}``, ``{or: [{length: {eq: 2}}], and: []}``)."""
    spec = {k: v for k, v in (spec or {}).items() if v is not None}
    for key in spec:
        if key not in _LABELS_PRECEDENCE and key != "null":
            raise GraphQLError(f"unsupported labels filter field {key!r}")
    for key in ("and", "or"):
        if key in spec:
            return _labels_branches(key, spec[key])
    parts: list[str] = []
    params: list = []
    if spec.get("null"):
        parts.append("0")
    for key in ("length", "every", "some", "name"):
        if key not in spec:
            continue
        if key == "length":
            frag, p = _Comparator(_LABEL_COUNT).render(spec[key])
        elif key == "name":
            frag, p = _labels_predicate({"name": spec[key]}, every=False)
        else:
            frag, p = _labels_predicate(spec[key], every=key == "every")
        if frag:
            parts.append(frag)
            params.extend(p)
        break
    return _join(parts, "AND"), params


def _labels_branches(key: str, sub: list) -> tuple[str, list]:
    """The ``and`` / ``or`` of an ``IssueLabelCollectionFilter``, by the rules in ``_labels_filter``."""
    subs = [_labels_filter(x) for x in sub]
    if key == "or" and any(not f for f, _ in subs):
        return "", []
    kept = [(f, p) for f, p in subs if f]
    if not kept:
        return "", []
    frag = "(" + (" AND " if key == "and" else " OR ").join(f for f, _ in kept) + ")"
    return frag, [x for _, p in kept for x in p]


# field name on IssueFilter -> how it compiles.
#   ("col", column, kwargs)    a comparator straight onto a column
#   ("nested", {sub: spec})    an object filter whose sub-fields map onto columns
def compile_issue_filter(
    conn, flt: dict | None, team_keys: dict | None = None
) -> tuple[str, list] | None:
    """``IssueFilter`` -> ``(sql_fragment, params)``, or None when there is nothing to filter."""
    sql, params = _issue_filter(conn, flt or {}, team_keys)
    return (sql, params) if sql else None


def _issue_filter(conn, flt: dict, team_keys: dict | None = None) -> tuple[str, list]:
    frag, params, _ = _issue_parts(conn, flt, team_keys)
    return frag, params


def _issue_parts(conn, flt: dict, team_keys: dict | None) -> tuple[str, list, bool]:
    """``(fragment, params, vacuous)`` for one ``IssueFilter`` object.

    ``vacuous`` is whether a key of this object constrains nothing -- a field with an empty
    comparator (``title: {}``), a nested filter that compiles to nothing (``labels: {}``,
    ``labels: {some: {}}``, ``state: {name: {}}``), or an ``and`` / ``or`` with branches but no
    condition left (``or: [{}]``). It matters inside an ``or``, where api.linear.app answers a
    vacuous branch by making the WHOLE ``or`` constrain nothing, its other keys and branches
    included: ``{or: [{labels: {}}, {title: {eq: T}}]}`` and ``{or: [{labels: {}, title: {eq:
    "no such title"}}, {title: {eq: T}}]}`` each answered every issue, where the ``title`` branch
    alone answers one. A branch with nothing in it is dropped instead -- ``{or: [{}, {title: {eq:
    T}}]}``, ``{or: [{and: []}, …]}``, ``{or: [{or: []}, …]}`` and ``{or: [{title: null}, …]}`` each
    answered the title match alone -- and inside an ``and`` a vacuous branch is dropped too:
    ``{and: [{labels: {}}, {title: {eq: T}}]}`` answered the title match, ``{or: [{and: [{labels:
    {}}, {title: {eq: "no such title"}}]}, {title: {eq: T}}]}`` the same. The flag is how the
    empty fragment of a dropped branch is told from the empty fragment of a vacuous one."""
    parts: list[str] = []
    params: list = []
    vacuous = False

    def add(frag, p):
        nonlocal vacuous
        if frag:
            parts.append(frag)
            params.extend(p)
        else:
            vacuous = True

    for key, spec in (flt or {}).items():
        if spec is None:
            continue
        if key in ("and", "or"):
            # `team_keys` rides down the recursion: without it a `team.key` nested under and/or
            # compiled against the name-derived key while the same filter at top level compiled
            # against the corpus's, so the two spellings of one query disagreed.
            branches = spec
            if key == "or":
                # api.linear.app reads the keys of one `or` branch as alternatives, not as one
                # conjunction: `{or: [{number: {eq: 1}, title: {eq: T2}}]}` answered the issue
                # numbered 1 and the one titled T2, where `{and: [{number: {eq: 1}, title: {eq:
                # T2}}]}` answered none, and `projects(filter: {or: [{name: {eq: A}, id: {eq:
                # B}}]})` both projects (measured 2026-09-04). So a branch with n keys is n
                # branches; one with none is still dropped, as measured above.
                branches = [{k: v} for branch in spec for k, v in (branch or {}).items()]
            subs = [_issue_parts(conn, s, team_keys) for s in branches]
            if key == "or" and any(v for _, _, v in subs):
                vacuous = True
                continue
            kept = [(f, p) for f, p, _ in subs if f]
            if not kept:
                vacuous = vacuous or bool(spec)
                continue
            parts.append("(" + (" AND " if key == "and" else " OR ").join(f for f, _ in kept) + ")")
            params.extend(x for _, p in kept for x in p)
            continue
        if key == "id":
            # The filter speaks a Linear UUID or a human identifier; the column is the issue
            # `id` either is resolved to, so the caller translates every value to its id (a
            # served_id column lookup, falling back to identifier) before it gets here — see
            # linear_resolvers._resolve_issue_ids / _resolve_one_issue_id.
            add(*_Comparator("id").render(spec))
        elif key == "title":
            add(*_Comparator("title").render(spec))
        elif key == "description":
            add(*_Comparator("content").render(spec))
        # No `branchName`: Linear's `IssueFilter` has no such field (measured 2026-09-03 -- the
        # real API answers `Field "branchName" is not defined by type "IssueFilter"`), so this
        # compiled a predicate no client written against Linear could ever send.
        elif key == "priority":
            # Against the COALESCE the field is served with, so `priority: {eq: 0}` finds an issue
            # whose column is NULL and `Issue.priority` reads 0 (see store.LINEAR_PRIORITY_EXPR).
            add(*_Comparator(store.LINEAR_PRIORITY_EXPR).render(spec))
        elif key == "estimate":
            # `EstimateComparator`: the number comparators plus `and` / `or` over them.
            add(*_Comparator("estimate").render(spec))
        elif key == "dueDate":
            # `NullableTimelessDateComparator`: operands are TimelessDateOrDuration, the column a
            # bare YYYY-MM-DD, so the comparison is lexical over the ISO date.
            add(*_Comparator("due_date", timeless=True).render(spec))
        elif key in ("createdAt", "updatedAt", "completedAt", "canceledAt"):
            col = {
                "createdAt": "created_ts",
                # Against the COALESCE the field is served with, as `priority` above: Linear's
                # `updatedAt` is non-null, so an issue with no recorded edit has to be found at
                # the creation time it reports, and `neq` must not drop it (see
                # store.LINEAR_UPDATED_EXPR). `completedAt` / `canceledAt` are nullable in Linear
                # too (`NullableDateComparator`), so the raw column is right for them.
                "updatedAt": store.LINEAR_UPDATED_EXPR,
                "completedAt": "completed_ts",
                "canceledAt": "canceled_ts",
            }[key]
            add(*_Comparator(col, epoch=True).render(spec))
        elif key == "state":
            # No `id` here on purpose: a workflow-state id is derived from (team, name), not
            # from the `state` column alone, so it cannot be expanded over one column's distinct
            # values the way `team.key` and `project.id` can. It is therefore absent from
            # `WorkflowStateFilter` in the SDL too — declared means implemented.
            add(
                *_sub_filter(
                    conn,
                    spec,
                    {
                        "name": ("col", "state"),
                        "type": ("derived", "state", synth.linear_state_type),
                    },
                )
            )
        elif key in ("assignee", "creator"):
            email_col = "assignee_email" if key == "assignee" else "author_email"
            name_col = "assignee_display" if key == "assignee" else "owner_display"
            add(
                *_sub_filter(
                    conn,
                    spec,
                    {
                        "email": ("col", email_col),
                        "name": ("col", name_col),
                        "displayName": ("col", name_col),
                        "id": ("derived", email_col, synth.linear_user_id),
                    },
                )
            )
        elif key == "team":
            add(
                *_sub_filter(
                    conn,
                    spec,
                    {
                        "name": ("col", "team"),
                        # The key a team's own identifiers carry, read off `linear_teams`
                        # (see `store.linear_team_keys`); name-derived for a team with no
                        # row there.
                        "key": (
                            "derived",
                            "team",
                            lambda name: (team_keys or {}).get(name) or synth.linear_team_key(name),
                        ),
                        "id": ("derived", "team", synth.linear_team_id),
                    },
                )
            )
        elif key == "project":
            _refuse_malformed_project_ids(spec)
            add(
                *_sub_filter(
                    conn,
                    spec,
                    {
                        "name": ("col", "project"),
                        "id": ("derived", "project", synth.linear_project_id),
                    },
                )
            )
        elif key == "labels":
            add(*_labels_filter(spec))
        else:
            raise GraphQLError(f"unsupported issue filter field {key!r}")
    return _join(parts, "AND"), params, vacuous


def _relation_items(spec: dict, nested: bool = False) -> list[tuple[str, object, bool]]:
    """The keys of a nested object filter with its ``and`` branches read as more keys of the same
    object, each tagged with whether it came from a branch. ``null: null`` is no key at all, unlike
    on a comparator (see `_Comparator.render`): measured 2026-09-03 with one assigned issue beside
    three unassigned, `assignee: {null: null}` answered all four where `null: true` answered the
    three and `null: false` the one, `{null: null, name: {eq: ME}}` the assigned one alone,
    `{null: null, name: {eq: "nobody"}}` none, and `{or: [{assignee: {null: null}}, {title: {eq:
    "no such title"}}]}` all four -- the branch left as `{}` is a key that constrains nothing, as
    `{assignee: {}}` is."""
    out: list[tuple[str, object, bool]] = []
    for key, sub in (spec or {}).items():
        if sub is None:
            continue
        if key == "and":
            for branch in sub:
                out.extend(_relation_items(branch, nested=True))
        else:
            out.append((key, sub, nested))
    return out


def _without_null_values(spec: dict) -> dict:
    """``spec`` with every null-valued key dropped, ``and`` / ``or`` branches included, so that a
    branch written ``{null: null}`` is the ``{}`` that `_relation_items` reads it as."""
    out: dict = {}
    for key, sub in (spec or {}).items():
        if sub is None:
            continue
        out[key] = [_without_null_values(b) for b in sub] if key in ("and", "or") else sub
    return out


def _under(spec: dict, *keys: str):
    for key in keys:
        yield from spec.get(key) or []


def _carries_null_true(spec: dict) -> bool:
    """A ``null: true`` on ``spec`` or anywhere under its ``and`` / ``or`` branches."""
    return spec.get("null") is True or any(_carries_null_true(b) for b in _under(spec, "or", "and"))


def _carries_null_false(spec: dict) -> bool:
    return spec.get("null") is False or any(
        _carries_null_false(b) for b in _under(spec, "or", "and")
    )


def _carries_empty(spec: dict) -> bool:
    """``{}`` itself, ``{and: []}`` (the same object), or an ``or`` with such a branch."""
    return spec in ({}, {"and": []}) or any(_carries_empty(b) for b in _under(spec, "or"))


def _is_bare_null_false(spec: dict) -> bool:
    """``{null: false}`` and nothing else -- written so, or as ``and`` branches that merge to it, or
    as a branch of an ``or``."""
    if any(key not in ("null", "or", "and") for key in spec):
        return False
    if spec.get("null") is False and "or" not in spec and not spec.get("and"):
        return True
    if spec.get("and") and "or" not in spec and spec.get("null") is None:
        items = _relation_items(spec)
        nulls = [sub for key, sub, _ in items if key == "null"]
        return bool(nulls) and not any(nulls) and len(nulls) == len(items)
    return any(_is_bare_null_false(b) for b in _under(spec, "or"))


_NEGATIVE_OPS = ("neq", "nin", "neqIgnoreCase")


def _carries_negative(spec: dict) -> bool:
    """A comparator that keeps the issues without the relation (see `_Comparator`), anywhere in
    ``spec``."""
    for key, sub in spec.items():
        if key in ("or", "and"):
            if any(_carries_negative(b) for b in sub):
                return True
        elif key != "null" and isinstance(sub, dict) and any(op in sub for op in _NEGATIVE_OPS):
            return True
    return False


def _related_row_term(conn, key: str, sub, mapping: dict) -> tuple[str | None, list]:
    """One key of an ``or`` or ``and`` branch, read against the related row. ``null`` says nothing
    about that row and renders nothing (None); a comparator with no operator is a condition every
    row passes."""
    if key == "null":
        return None, []
    if key == "or":
        return _related_row_or(conn, sub, mapping, nested=True)
    if key == "and":
        return _related_row_and(conn, sub, mapping)
    target = mapping.get(key)
    if target is None:
        raise GraphQLError(f"unsupported nested filter field {key!r}")
    if target[0] == "col":
        frag, p = _Comparator(target[1], relation=True).render(sub)
    else:
        frag, p = _derived_in(conn, target[1], target[2], sub)
    return frag or "1", p


def _related_row_terms(conn, spec: dict, mapping: dict, op: str) -> tuple[str | None, list]:
    frags: list[str] = []
    params: list = []
    for key, sub in spec.items():
        frag, p = _related_row_term(conn, key, sub, mapping)
        if frag is not None:
            frags.append(frag)
            params.extend(p)
    return (_join(frags, op) if frags else None), params


def _related_row_or(conn, branches: list, mapping: dict, nested: bool) -> tuple[str | None, list]:
    """The related-row side of an ``or``: the keys of one branch are alternatives, a branch that
    renders nothing is skipped, and a group with nothing left is a condition every row passes.
    ``or: []`` nested in a branch renders nothing at all."""
    if not branches:
        return (None if nested else "1"), []
    frags: list[str] = []
    params: list = []
    for branch in branches:
        frag, p = _related_row_terms(conn, branch, mapping, "OR")
        if frag is not None:
            frags.append(frag)
            params.extend(p)
    return (_join(frags, "OR") if frags else "1"), params


def _related_row_and(conn, branches: list, mapping: dict) -> tuple[str | None, list]:
    """The related-row side of an ``and`` written inside an ``or`` branch: the keys of one branch
    AND, as do the branches; ``and: []`` renders nothing."""
    if not branches:
        return None, []
    frags: list[str] = []
    params: list = []
    for branch in branches:
        frag, p = _related_row_terms(conn, branch, mapping, "AND")
        if frag is not None:
            frags.append(frag)
            params.extend(p)
    return (_join(frags, "AND") if frags else "1"), params


def _is_vacuous_branch(spec: dict) -> bool:
    """A branch that is nothing but comparators with no operator (``{name: {}}``), ``null`` aside."""
    comparators = [sub for key, sub in spec.items() if key not in ("null", "or", "and")]
    return (
        bool(comparators)
        and all(sub == {} for sub in comparators)
        and not ("or" in spec or "and" in spec)
    )


def _relation_or(
    conn, branches: list, mapping: dict, col: str, own: bool
) -> tuple[str, list, bool]:
    """An ``or`` on a nullable relation, as api.linear.app reads it (the rule is spelled out on
    `_sub_filter`), WITHOUT the issues a ``null: true`` below it adds: that is the object's
    business (see `_sub_filter`). ``own`` is whether the ``or`` is the object's own key rather
    than one lifted out of an ``and`` branch. The third value says whether a bare ``{null:
    false}`` branch requires the relation for the whole object."""
    if not branches:
        return f"{col} IS NOT NULL", [], False
    if all(_carries_null_true(b) for b in branches):
        return f"{col} IS NULL", [], False
    has_null_true = any(_carries_null_true(b) for b in branches)
    every_null_false = all(_carries_null_false(b) for b in branches)
    if any(_is_vacuous_branch(b) for b in branches):
        return (f"{col} IS NOT NULL" if every_null_false else ""), [], False
    bare = (
        not has_null_true and not every_null_false and any(_is_bare_null_false(b) for b in branches)
    )
    without = any(_carries_empty(b) for b in branches) or (bare and own)
    requires = bare and not own
    related, params = _related_row_or(conn, branches, mapping, nested=False)
    if without:
        return ("" if related == "1" else f"({col} IS NULL OR {related})"), params, requires
    if requires:
        return ("" if related == "1" else related), params, True
    if every_null_false or not any(_carries_negative(b) for b in branches):
        if related == "1":
            return f"{col} IS NOT NULL", [], False
        return f"({col} IS NOT NULL AND {related})", params, False
    return related, params, False


def _sub_filter(conn, spec: dict, mapping: dict) -> tuple[str, list]:
    """A nested object filter (``state``, ``assignee``, ``team``, ``project``).

    On a nullable relation ``null: true`` is not one condition among the keys: api.linear.app
    answers the issues without the relation and reads nothing else in the object, so
    ``assignee: {null: true, name: {eq: X}}`` is the unassigned issues, not none. Measured
    2026-09-04 on ``project`` with two issues in two throwaway projects beside two in none (every
    sibling tried -- ``name``, ``id``, both, an ``and`` or ``or`` of them -- answered the two
    without a project) and on ``assignee`` with one issue assigned to the viewer beside three
    unassigned; ``creator`` takes the same path and answered every issue of a workspace whose
    issues have no creator, where an AND would have answered none. ``null: false`` does AND with
    its siblings: ``{null: false, name: {eq: X}}`` is the issues with the relation named X.

    ``and`` branches are read as more keys of the same object, so ``{and: [{null: true}], name:
    {eq: X}}`` and ``{and: [{null: true}, {name: {eq: X}}]}`` are ``null: true`` too. A ``null``
    written at the object's own level wins over one inside its ``and`` branches (``{null: false,
    and: [{null: true}]}`` is the issues with the relation); between branches ``true`` wins
    (``{and: [{null: false}, {null: true}]}`` is the issues without it). An ``and`` at the level
    of ``IssueFilter`` is not this object: ``{and: [{assignee: {null: true}}, {assignee: {name:
    {eq: X}}}]}`` is two relation filters ANDed and answers none, as measured.

    An ``or`` is not the union of its branches each read by that rule. Measured 2026-09-04, 429
    filters in eight rounds over the same four issues (two in two projects, one of them assigned
    to the viewer, two in none); from the third round on each round was predicted from the rule
    before it was sent, 105 of 112 right, and the misses fixed corners until every row fit:

    * A ``null: true`` anywhere below the object -- in an ``or`` branch, an ``and`` branch, or
      nested deeper -- adds the issues without the relation to whatever the REST of the object
      answers, ``IS NULL OR (rest)``: ``{or: [{null: true}, {name: {eq: X}}], name: {eq: Y}}`` is
      the issues without a project plus those in X that are also in Y, and ``{and: [{or: [{null:
      true}, {name: {eq: X}}]}], or: [{name: {eq: X}}]}`` the issues without plus X's. The
      object's own ``null: false`` wins over it (``{null: false, or: [{null: true}, {name: {eq:
      X}}]}`` is X's), and so does a bare ``{null: false}`` branch of an ``or`` lifted out of an
      ``and`` branch (below).
    * An ``or`` whose every branch carries a ``null: true`` somewhere is the issues without the
      relation, comparators unread: ``{or: [{null: true, name: {eq: X}}, {null: true}]}``.
    * Otherwise the ``or`` is the issues whose relation satisfies its branches read against the
      related row, where THE KEYS OF ONE BRANCH ARE ALTERNATIVES -- ``{or: [{name: {eq: X}, id:
      {eq: Y}}]}`` is X's and Y's where ``{name: {eq: X}, id: {eq: Y}}`` is none, as Linear's
      ``or`` reads a branch on ``IssueFilter`` and ``ProjectFilter`` too -- a branch that says
      nothing about that row (``{}``, ``{null: true}``, ``{null: false}``) skipped, and nothing
      left meaning every related row; PLUS the issues without the relation when some branch is
      ``{}`` (``{and: []}``, or an ``or`` with such a branch), or when the ``or`` is the object's
      own and some branch is a bare ``{null: false}`` beside one with no ``null: false`` anywhere
      in it and no ``null: true`` in the ``or``. So ``{or: [{null: false}, {name: {eq:
      "nobody"}}]}``, ``{or: [{}, {name: {eq: "nobody"}}]}`` and ``{or: [{null: true}, {name: {eq:
      "nobody"}}]}`` are each the issues without a project, ``{or: [{null: true, name: {eq: X}},
      {name: {eq: "nobody"}}]}`` those plus X's, and ``{or: [{null: false}, {null: false, name:
      {eq: "nobody"}}]}`` none. When every branch carries a ``null: false`` the relation must
      exist, which only a negative comparator can show.
    * The same bare ``{null: false}`` in an ``or`` lifted out of an ``and`` branch does the
      opposite: it requires the relation for the whole object, as the object's own ``null:
      false`` would, so ``{and: [{or: [{null: false}, {name: {neq: X}}]}]}`` is the issues with a
      project other than X and ``{and: [{or: [{null: false}, {}]}]}`` the issues with one; a
      ``null: true`` in that ``or`` (``{and: [{or: [{null: false}, {null: true}]}]}``, every issue)
      turns it off.
    * A branch that is nothing but comparators with no operator (``{name: {}}``) makes the
      ``or`` constrain nothing, ``{or: [{name: {}}, {name: {eq: "nobody"}}]}`` being every issue,
      unless every branch carries ``null: false``, when the relation is still required; beside a
      real key the same comparator is TRUE inside its branch, so ``{or: [{name: {}, id: {eq:
      Y}}]}`` is the issues with a project.
    * An ``or`` inside a branch renders its related-row side only, its ``null`` keys counting
      toward the rules above for the ``or`` that holds it; a nested ``or: []`` and an ``and: []``
      render nothing, and the object's own ``or: []`` is the issues with the relation, where
      ``{}`` and ``{and: []}`` are every issue.

    This is the vendor's arithmetic as measured, not its code: the first two clauses read like a
    scan for ``null: true`` that decides whether rows without the relation are admitted, the
    rest like flags hoisted out of the branches before the related-row side compiles, but no
    single query shape was found that yields all of it. The one piece of Linear's own filter
    code that is public, the matcher its web client runs over synced models (the ``or`` and
    ``and`` operators of the ModelMatcher in ``static.linear.app/client/assets/store.*.js``,
    read 2026-09-04), does answer a missing relation by scanning: an ``or`` there is "some
    branch says ``null: true``", an ``and`` "every branch does". It is not the API's compiler,
    though -- it ANDs the keys of a branch and reads ``{and: [{null: true}, {name: {eq: X}}]}``
    as none where the API answers the issues without the relation -- so the clauses above stay
    measured."""
    spec = _without_null_values(spec)
    items = _relation_items(spec)
    own = [sub for key, sub, nested in items if key == "null" and not nested]
    inner = [sub for key, sub, nested in items if key == "null" and nested]
    if own:
        null = own[0]
    elif inner:
        null = any(inner)
    else:
        null = None
    # Which column carries "no such relation" is mapping-specific, so use the first mapped column.
    col = next(m[1] for m in mapping.values())
    if null is True:
        return f"{col} IS NULL", []
    parts: list[str] = []
    params: list = []
    requires = null is False
    for key, sub, nested in items:
        if key == "or":
            frag, p, req = _relation_or(conn, sub, mapping, col, own=not nested)
            requires = requires or req
            if frag:
                parts.append(frag)
                params.extend(p)
    for key, sub, _ in items:
        if key in ("null", "or") or sub == {}:
            continue
        target = mapping.get(key)
        if target is None:
            raise GraphQLError(f"unsupported nested filter field {key!r}")
        if target[0] == "col":
            frag, p = _Comparator(target[1], relation=True).render(sub)
        else:
            frag, p = _derived_in(conn, target[1], target[2], sub)
        if frag:
            parts.append(frag)
            params.extend(p)
    rest = _join(parts, "AND")
    if requires:
        return _join([f"{col} IS NOT NULL", rest], "AND"), params
    if _carries_null_true(spec):
        return ("" if not rest else f"({col} IS NULL OR {rest})"), params
    return rest, params


def _map_comment_ids(conn, spec: dict) -> dict:
    """Rewrite a comparator over ``Comment.id`` from served UUIDs back to stored row ids.

    Unlike an issue, a comment has no id table of its own (there are 165k at the scale measured and
    they are only ever reached through their parent), so the mapping is built on demand for just
    the ids the comparator names. An unresolvable id becomes a sentinel that matches nothing,
    which is what "no such comment" should return."""
    out = {}
    for op, val in spec.items():
        values = val if isinstance(val, list) else [val]
        wanted = {str(v) for v in values}
        lookup: dict[str, str] = {}
        for (row_id,) in conn.execute("SELECT id FROM linear_comments"):
            served = synth.linear_comment_id(row_id)
            if served in wanted:
                lookup[served] = row_id
                if len(lookup) == len(wanted):
                    break
        mapped = [lookup.get(str(v), "\x00none") for v in values]
        out[op] = mapped if isinstance(val, list) else mapped[0]
    return out


def compile_comment_filter(conn, flt: dict | None) -> tuple[str, list] | None:
    """``CommentFilter`` -> ``(sql_fragment, params)``. Columns are on the aliased ``c`` table,
    matching :func:`backlot.store.list_linear_comments`'s join."""
    sql, params = _comment_filter(conn, flt or {})
    return (sql, params) if sql else None


def _comment_filter(conn, flt: dict) -> tuple[str, list]:
    parts: list[str] = []
    params: list = []
    for key, spec in (flt or {}).items():
        if spec is None:
            continue
        if key in ("and", "or"):
            subs = [_comment_filter(conn, s) for s in spec]
            frags = [f for f, _ in subs if f]
            for _, p in subs:
                params.extend(p)
            if frags:
                parts.append("(" + (" AND " if key == "and" else " OR ").join(frags) + ")")
            continue
        if key == "id":
            # `Comment.id` is served as synth.linear_comment_id(row id), so a filter written from
            # a served id must be translated back or it can never match what the client just saw.
            frag, p = _Comparator("c.id").render(_map_comment_ids(conn, spec))
        elif key == "body":
            frag, p = _Comparator("c.body").render(spec)
        elif key in ("createdAt", "updatedAt"):
            frag, p = _Comparator("c.created_ts", epoch=True).render(spec)
        else:
            raise GraphQLError(f"unsupported comment filter field {key!r}")
        if frag:
            parts.append(frag)
            params.extend(p)
    return _join(parts, "AND"), params
