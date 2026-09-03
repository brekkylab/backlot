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
            if raw is None and op != "null":
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


def _label_predicate(spec: dict) -> tuple[str, list]:
    """One ``IssueLabelFilter`` against the ``value`` column of a ``json_each`` row, INCLUDING its
    ``and`` / ``or``. Reading only ``name`` makes a nested and/or compile to nothing, and an empty
    fragment drops the WHOLE filter — ``labels:{some:{and:[{name:{eq:"nonexistent"}}]}}`` would
    return the unfiltered corpus, the silent wrong answer this module exists to prevent."""
    parts: list[str] = []
    params: list = []
    for key, sub in (spec or {}).items():
        if sub is None:
            continue
        if key == "name":
            frag, p = _Comparator("value").render(sub)
        elif key in ("and", "or"):
            subs = [_label_predicate(x) for x in sub]
            frags = [f for f, _ in subs if f]
            for _, sp in subs:
                params.extend(sp)
            frag, p = (
                (("(" + (" AND " if key == "and" else " OR ").join(frags) + ")") if frags else ""),
                [],
            )
        else:
            raise GraphQLError(f"unsupported label filter field {key!r}")
        if frag:
            parts.append(frag)
            params.extend(p)
    return _join(parts, "AND"), params


def _labels_predicate(spec: dict, *, every: bool) -> tuple[str, list]:
    """``labels: {some|every: {…}}`` over the JSON ``labels`` column.

    ``some`` is an EXISTS over ``json_each``; ``every`` is "no element fails", which also holds
    for an issue with no labels — the same semantics Linear's collection filters use."""
    inner, params = _label_predicate(spec)
    if not inner:
        # An empty inner predicate must NOT silently vanish: dropping it turns a narrowing filter
        # into a full-corpus answer. Nothing legitimate produces one, so it is a client error.
        raise GraphQLError("a labels filter must constrain something (e.g. labels.some.name)")
    if every:
        return (
            f"NOT EXISTS (SELECT 1 FROM json_each(COALESCE(labels, '[]')) WHERE NOT {inner})",
            params,
        )
    return (f"EXISTS (SELECT 1 FROM json_each(COALESCE(labels, '[]')) WHERE {inner})", params)


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
    parts: list[str] = []
    params: list = []

    def add(frag, p):
        if frag:
            parts.append(frag)
            params.extend(p)

    for key, spec in (flt or {}).items():
        if spec is None:
            continue
        if key in ("and", "or"):
            # `team_keys` rides down the recursion: without it a `team.key` nested under and/or
            # compiled against the name-derived key while the same filter at top level compiled
            # against the corpus's, so the two spellings of one query disagreed.
            subs = [_issue_filter(conn, s, team_keys) for s in spec]
            frags = [f for f, _ in subs if f]
            for _, p in subs:
                params.extend(p)
            if frags:
                parts.append("(" + (" AND " if key == "and" else " OR ").join(frags) + ")")
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
                "updatedAt": "updated_ts",
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
            for sub, every in (("some", False), ("every", True)):
                if sub in spec and spec[sub] is not None:
                    add(*_labels_predicate(spec[sub], every=every))
            for sub in ("and", "or"):
                if spec.get(sub):
                    raise GraphQLError(
                        f"labels.{sub} is not supported by Backlot; use labels.some / labels.every"
                    )
            # Key PRESENCE, not truthiness: `some: {}` is present-but-empty and deserves the
            # more precise "must constrain something" from _labels_predicate, not this one.
            if not any(k in spec for k in ("some", "every")):
                raise GraphQLError("a labels filter needs `some` or `every`")
        else:
            raise GraphQLError(f"unsupported issue filter field {key!r}")
    return _join(parts, "AND"), params


def _sub_filter(conn, spec: dict, mapping: dict) -> tuple[str, list]:
    """A nested object filter (``state``, ``assignee``, ``team``, ``project``)."""
    parts: list[str] = []
    params: list = []
    for key, sub in (spec or {}).items():
        if sub is None and key != "null":
            continue
        if key in ("and", "or"):
            subs = [_sub_filter(conn, s, mapping) for s in sub]
            frags = [f for f, _ in subs if f]
            for _, p in subs:
                params.extend(p)
            if frags:
                parts.append("(" + (" AND " if key == "and" else " OR ").join(frags) + ")")
            continue
        if key == "null":
            # `null: true` on a nullable relation means "no such relation on this issue". Which
            # column carries that is mapping-specific, so use the first mapped column.
            col = next(m[1] for m in mapping.values())
            parts.append(f"{col} IS {'NULL' if sub else 'NOT NULL'}")
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
    return _join(parts, "AND"), params


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
