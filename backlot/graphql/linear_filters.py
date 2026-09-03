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
"""

from __future__ import annotations

import datetime as _dt

from graphql import GraphQLError

from backlot import synth


def _to_epoch(value) -> int | None:
    """An ISO-8601 ``DateTime`` operand -> unix seconds, to compare against a ``*_ts`` column.
    Deliberately NOT an importer's own date coercion: that exists to salvage a corpus's messy
    human date strings and would drag the whole importer into the serving path. A filter operand
    comes from a GraphQL client, so ISO-8601 (or a bare epoch) is the whole contract."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise GraphQLError(f"{value!r} is not a valid ISO-8601 DateTime") from None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)).timestamp())


# A comparator key -> how it becomes SQL, given a column expression and the value.
# `%` / `_` in a LIKE needle are escaped so a user-supplied value stays literal.
_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def _like(value: str) -> str:
    return str(value).translate(_LIKE_ESCAPE)


class _Comparator:
    """Renders one comparator object (``{eq: …, contains: …}``) against a column."""

    def __init__(self, col: str, *, text: bool = False, epoch: bool = False):
        self.col, self.text, self.epoch = col, text, epoch

    def _value(self, v):
        # A DateComparator's operand is an ISO-8601 string but the column is unix seconds.
        return _to_epoch(v) if self.epoch else v

    def render(self, spec: dict) -> tuple[str, list]:
        parts: list[str] = []
        params: list = []
        for op, raw in spec.items():
            if raw is None and op != "null":
                continue
            v = self._value(raw)
            if op == "eq":
                parts.append(f"{self.col} = ?")
                params.append(v)
            elif op == "neq":
                # NULL never equals anything, so a plain `<> ?` would silently drop NULL rows a
                # caller asking "not X" expects to see.
                parts.append(f"({self.col} IS NULL OR {self.col} <> ?)")
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
                parts.append(f"{self.col} {'IN' if op == 'in' else 'NOT IN'} ({marks})")
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
                parts.append(f"({self.col} IS NULL OR lower({self.col}) <> lower(?))")
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
    # no value, so they cannot be the excluded one. The SQL comparator's own `neq` already spells
    # this out (`IS NULL OR <> ?`); an IN-list over distinct non-NULL names silently dropped them,
    # so `project:{id:{neq:X}}` and `project:{name:{neq:X}}` disagreed on 24 real rows.
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
            if key == "or" and sub == []:
                # Measured 2026-09-03: a literally empty `or` is a predicate every label satisfies,
                # and the quantifier still applies -- `some: {or: []}` and `every: {or: []}` both
                # answered exactly the issues with at least one label. An empty `and`, and an `or`
                # whose branches are themselves empty (`or: [{}]`), are no predicate at all: every
                # issue answered, label-less ones included, which the empty fragment below gives.
                frags = ["1"]
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


# The operators that read as a negation. What the quantifier does with an issue that has NO labels
# depends on this (see `_labels_predicate`), so a compound predicate has to carry a polarity too:
# `and` is negative when every branch is, `or` when any branch is, and a comparator when every
# operator in it is -- `{eq: A, neq: B}` reads positive (measured 2026-09-03, see the test matrix).
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
            ops = [op for op, v in sub.items() if v is not None]
            parts.append(bool(ops) and all(op in _NEGATIVE_OPS for op in ops))
        elif key == "and":
            parts.append(bool(sub) and all(_reads_as_negation(x) for x in sub))
        elif key == "or":
            parts.append(any(_reads_as_negation(x) for x in sub))
    return bool(parts) and all(parts)


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
    compound predicate takes. An empty predicate (``some: {}``, ``some: {and: []}``) constrains
    nothing there: every issue answered, label-less ones included, so it compiles to nothing here
    too. The one exception is a literally empty ``or`` (see :func:`_label_predicate`)."""
    inner, params = _label_predicate(spec)
    if not inner:
        return "", []
    negative = _reads_as_negation(spec)
    if every:
        no_label_fails = f"NOT EXISTS (SELECT 1 FROM {_LABEL_EACH} WHERE NOT {inner})"
        return (
            no_label_fails if negative else f"({_LABEL_COUNT} > 0 AND {no_label_fails})"
        ), params
    a_label_matches = f"EXISTS (SELECT 1 FROM {_LABEL_EACH} WHERE {inner})"
    return (f"({_LABEL_COUNT} = 0 OR {a_label_matches})" if negative else a_label_matches), params


def _labels_filter(spec: dict) -> tuple[str, list]:
    """One ``IssueLabelCollectionFilter``. Besides the two quantifiers, measured on 2026-09-03:
    ``and`` / ``or`` compose collection filters; ``length`` is a ``NumberComparator`` over the
    label count (``{eq: 0}`` answered the label-less issues, ``{eq: 2}`` the two-label one);
    ``null: true`` answered NOTHING -- an issue's label collection is never null, not even an empty
    one -- and ``null: false`` everything; a bare ``name`` at this level answered as ``some`` does;
    and an empty object constrains nothing."""
    parts: list[str] = []
    params: list = []

    def add(frag, p):
        if frag:
            parts.append(frag)
            params.extend(p)

    for key, sub in (spec or {}).items():
        if sub is None and key != "null":
            continue
        if key in ("some", "every"):
            add(*_labels_predicate(sub, every=key == "every"))
        elif key in ("and", "or"):
            subs = [_labels_filter(x) for x in sub]
            frags = [f for f, _ in subs if f]
            for _, p in subs:
                params.extend(p)
            if frags:
                parts.append("(" + (" AND " if key == "and" else " OR ").join(frags) + ")")
        elif key == "length":
            add(*_Comparator(_LABEL_COUNT).render(sub))
        elif key == "null":
            if sub:
                parts.append("0")
        elif key == "name":
            add(*_labels_predicate({"name": sub}, every=False))
        else:
            raise GraphQLError(f"unsupported labels filter field {key!r}")
    return _join(parts, "AND"), params


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
        elif key == "branchName":
            add(*_Comparator("branch_name").render(spec))
        elif key == "priority":
            add(*_Comparator("priority").render(spec))
        elif key == "estimate":
            add(*_Comparator("estimate").render(spec))
        elif key == "dueDate":
            add(*_Comparator("due_date").render(spec))
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
            add(*_labels_filter(spec))
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
            frag, p = _Comparator(target[1]).render(sub)
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
