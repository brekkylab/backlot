"""Validate BYO corpus records against the per-service JSON Schemas in ``backlot/schemas/``.

The schemas (``backlot/schemas/<source_type>.schema.json``, Draft 2020-12) are the source of
truth for the record shape the loader accepts — they define the app's ingest contract, so this
lives on the application side. ``backlot/importer/byo.py`` calls :func:`record_errors` to fail
fast on load, and its ``--dry-run`` validates a whole file via :func:`validate_file` without
touching the DB.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import re

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema import validators as _js_validators
from jsonschema.exceptions import ValidationError

# __file__-relative, not cwd-relative: these are resources the package SHIPS (see the
# [tool.setuptools.package-data] entry in pyproject.toml), unlike backlot.config's data_dir,
# which is user data and must resolve against the cwd instead. `.parent`, not
# `.parent.parent` — schemas/ lives inside the backlot/ package, not the repo root, precisely so
# it is included in the wheel.
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def _load_schemas() -> dict[str, dict]:
    """Load every ``*.schema.json`` in ``SCHEMA_DIR``, keyed by its ``source_type`` const."""
    schemas: dict[str, dict] = {}
    for p in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schema = json.loads(p.read_text())
        const = schema.get("properties", {}).get("source_type", {}).get("const")
        schemas[const or p.name.split(".")[0]] = schema
    if not schemas:
        # Loading zero schemas is a packaging bug, not empty user data — every BYO record would
        # then fail validation with a confusing "source_type must be one of []" that points at the
        # data instead of the missing schemas/. Announce it here instead.
        raise RuntimeError(
            f"no *.schema.json files found under {SCHEMA_DIR} — the package is missing its "
            "bundled schemas (check [tool.setuptools.package-data] in pyproject.toml)"
        )
    return schemas


SERVICE_SCHEMAS: dict[str, dict] = _load_schemas()


def _ecma_end_anchors(patrn: str) -> str:
    """``patrn`` with every ``$`` METACHARACTER rewritten to ``\\Z``.

    JSON Schema defines ``pattern`` against the ECMA-262 dialect, where ``$`` (no ``m`` flag)
    matches only at the end of the input. Python's ``$`` matches there *or* immediately before a
    final newline, so an anchored pattern silently stops being anchored for exactly one character:
    ``re.search(r"^[a-z]+$", "incidents\\n")`` matches, and every pattern in ``backlot/schemas/``
    is anchored ``^…$``. A corpus could therefore state a container name the vendor could not hold
    -- a Slack ``channel`` is served verbatim as a conversation's ``name`` -- past a schema that
    already writes the refusal down.

    Rewriting ``$`` rather than matching with :func:`re.fullmatch` keeps the other half of the
    contract intact: the spec's ``pattern`` is an UNANCHORED partial match, so a full match would
    trade this divergence for a second one the moment a schema wants a pattern anchored at neither
    end. It also leaves the shipped schemas readable by a non-Python validator, which they have to
    be -- they are the documented contract for a BYO corpus, not an implementation detail.

    A ``$`` is only a metacharacter when it is unescaped and outside a character class, so a
    literal ``[$]`` or ``\\$`` is left exactly as written.
    """
    out: list[str] = []
    i, in_class = 0, False
    while i < len(patrn):
        c = patrn[i]
        if c == "\\" and i + 1 < len(patrn):
            out.append(patrn[i : i + 2])
            i += 2
            continue
        if in_class:
            # A `]` in the first position of a class is a literal, so `[]]` and `[^]]` close on
            # their SECOND `]`. Reading the first one as the close would put the rest of the
            # pattern "outside" a class it is still inside.
            if c == "]" and not (out[-1] == "[" or (out[-1] == "^" and out[-2] == "[")):
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "$":
            out.append("\\Z")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


@lru_cache(maxsize=None)
def _ecma_regex(patrn: str) -> re.Pattern:
    return re.compile(_ecma_end_anchors(patrn))


def _pattern_ecma(validator, patrn, instance, schema):
    """``pattern``, applied with ECMA-262's end anchor (see :func:`_ecma_end_anchors`).

    Still a `search`, and still reported against the pattern the schema WROTE rather than the
    rewritten one, so an author reads back the rule they can look up in the schema file.
    """
    if not isinstance(instance, str):
        return
    if not _ecma_regex(patrn).search(instance):
        yield ValidationError(f"{instance!r} does not match {patrn!r}")


# Only `pattern` is overridden; every other keyword stays the library's. `check_schema` and the
# error paths `record_errors` renders come through unchanged, because this IS Draft 2020-12 with
# one keyword's implementation corrected.
_BacklotValidator = _js_validators.extend(Draft202012Validator, {"pattern": _pattern_ecma})


@lru_cache(maxsize=None)
def _validator(source_type: str) -> Draft202012Validator:
    return _BacklotValidator(SERVICE_SCHEMAS[source_type], format_checker=FormatChecker())


def _subschema(schema: dict, parts: list):
    """Walk into ``schema`` by a schema-path prefix, through objects and arrays alike."""
    node = schema
    try:
        for p in parts:
            node = node[int(p)] if isinstance(node, list) else node[p]
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return node


# Keywords whose value is a MAP of subschemas, keyed by names the schema author chose. A
# `then` sitting directly under one of these is such a name — a field a corpus may carry — and
# the `if` beside it is another one, so reading the pair as a conditional would invent a clause
# out of two ordinary fields.
_SUBSCHEMA_MAPS = frozenset(
    {"properties", "patternProperties", "dependentSchemas", "$defs", "definitions"}
)


def _branch_index(parts: list) -> int | None:
    """Index of the first ``then``/``else`` that stands where a keyword can stand."""
    for i, p in enumerate(parts):
        if p in ("then", "else") and (i == 0 or parts[i - 1] not in _SUBSCHEMA_MAPS):
            return i
    return None


def _when_clause(schema: dict, schema_path, failing) -> str:
    """`" when subtype is \\"file\\""` for a rule a condition put in force, else ``""``.

    A conditional requirement reports at the document root with a bare message: a 23,000-record
    corpus was rejected with dozens of identical ``<root>: 'path' is a required property`` lines,
    none of which said that ``path`` is required of source files and of nothing else. The
    condition is recoverable — the failing branch's schema path runs through a ``then`` (or an
    ``else``), and the ``if`` beside it is the predicate — so it is stated instead of left for the
    reader to find in the schema. Shapes this does not recognise add no clause rather than a
    guessed one.

    An ``else`` reads "unless", not a field-by-field negation: it is in force when the whole
    predicate fails, so a two-field condition rules out the pair together, and negating each
    field separately would claim something the schema does not say.

    A predicate field is reported as ``(or absent)`` unless the ``if`` requires it, because
    ``properties`` alone constrains a value without demanding the field.

    ``failing`` is the subschema that actually reported the error, and the walk is only trusted
    when it arrives there. No shipped schema uses ``$ref`` yet, so this guard changes nothing
    today; it is what keeps the clause honest once one does.
    """
    parts = list(schema_path)
    branch = _branch_index(parts)
    if branch is None:
        return ""
    # A schema path is reported WITHOUT the `$ref` hops it passed through, so a path from inside a
    # referenced subschema reads as a root-relative one and this walk lands on whatever conditional
    # happens to sit at those keys. The last element is the failing keyword, so its parent is the
    # schema that failed; anything else means the walk went somewhere other than the real branch.
    # `is not`, not `!=`: two structurally identical branches under different conditionals must not
    # count as a match, and jsonschema hands the subschema object through rather than copying it.
    if _subschema(schema, parts[:-1]) is not failing:
        return ""
    owner = _subschema(schema, parts[:branch])
    cond = owner.get("if") if isinstance(owner, dict) else None
    if not isinstance(cond, dict):
        return ""
    clauses = []
    # Guarded like `owner`, `cond` and `spec` are: `_load_schemas` only json.loads the files, and
    # `check_schema` runs in a test rather than at import, so a hand-edited schema reaches here
    # unvalidated. A non-object `properties` must not turn "report this bad record" into a
    # traceback on every record -- least of all on `--dry-run`, whose whole job is to report.
    props = cond.get("properties")
    for field, spec in (props if isinstance(props, dict) else {}).items():
        if not isinstance(spec, dict):
            continue
        # A NEGATED predicate — `{"not": {"const": "file"}}` — is how a schema says "of everything
        # except". Read through the negation rather than skipping it: unrendered, the rule reported
        # with no condition at all, which is the bare `'state' is a required property` this
        # function exists to replace.
        negated = isinstance(spec.get("not"), dict)
        if negated:
            spec = spec["not"]
        if "const" in spec:
            allowed = [spec["const"]]
        elif isinstance(spec.get("enum"), list) and spec["enum"]:
            allowed = spec["enum"]
        else:
            continue
        verb = "is not" if negated else "is"
        dumped = [json.dumps(v, ensure_ascii=False) for v in allowed]
        # Not `x or y`: the fields are joined by " and ", so a bare `or` between values reads as
        # `x or (y and z)` once a second field follows. A single value needs no bracketing.
        shown = dumped[0] if len(dumped) == 1 else "one of [" + ", ".join(dumped) + "]"
        req = cond.get("required")
        if isinstance(req, list) and field in req:
            clauses.append(f"{field} {verb} {shown}")
        else:
            # `properties` constrains a field's value, it does not require the field. An `if` with
            # no sibling `required` therefore SUCCEEDS for a record that omits the field, so `then`
            # is in force there too -- and saying only `when subtype is "file"` sends the author
            # looking through a record for a field they never set. `unless` needs it for the mirror
            # reason: an absent field satisfies the predicate, so it does not lift the rule.
            clauses.append(f"{field} {verb} {shown} (or absent)")
    # `{"not": {"required": [...]}}` predicates on a field's ABSENCE — how a schema says "one of
    # these two, and here is the one to require when the other is missing". Left unrendered, a
    # record that states neither was told only that the second is required and never learned that
    # stating the first would do.
    absent = cond.get("not")
    if isinstance(absent, dict) and isinstance(absent.get("required"), list):
        clauses += [f"{field} is absent" for field in absent["required"]]
    lead = " unless " if parts[branch] == "else" else " when "
    return lead + " and ".join(clauses) if clauses else ""


def _record_label(rec: dict) -> str:
    """How the author finds this record again: the id it wrote, else its title.

    A line number is not that on its own — a sharded artifact numbers each shard from one, and
    the id is what the author's own build wrote down and can grep for.

    One line, and truncation says so. ``--dry-run`` prints one problem per line and a title is
    free text that may hold a newline; a silently cut label, meanwhile, looks like a shorter id
    that the author would search for and never find.
    """
    for key in ("doc_id", "title"):
        value = rec.get(key)
        if isinstance(value, str) and value.strip():
            label = " ".join(value.split())
            return label if len(label) <= 60 else label[:59] + "…"
    return ""


def _alternatives(err) -> str | None:
    """The message for an ``anyOf`` whose every branch requires ONE field — a child row saying
    "state either of these", which is how ``content``/``body`` and ``text``/``content`` are spelled.

    jsonschema reports that failure as ``is not valid under any of the given schemas``: it names
    neither field, and the record it quotes is the child row rather than the missing key. Since the
    branches ARE the alternatives, the message can say which ones. None for any other ``anyOf`` —
    a union of types, say — where the branches name no fields to list.
    """
    if err.validator != "anyOf" or not isinstance(err.validator_value, list):
        return None
    fields: list[str] = []
    for branch in err.validator_value:
        if not isinstance(branch, dict) or set(branch) != {"required"}:
            return None
        req = branch["required"]
        if not isinstance(req, list) or len(req) != 1 or not isinstance(req[0], str):
            return None
        fields.append(req[0])
    if len(fields) < 2:
        return None
    quoted = [f"'{f}'" for f in fields]
    return f"{', '.join(quoted[:-1])} or {quoted[-1]} is a required property"


def record_errors(rec: dict) -> list[str]:
    """Return human-readable validation errors for one BYO record ([] if valid).

    Each message names the record, where in it the problem is, and — for a rule that only
    applies to some records — the condition that put the rule in force.
    """
    if not isinstance(rec, dict):
        return ["record must be a JSON object"]
    st = rec.get("source_type")
    if st not in SERVICE_SCHEMAS:
        return [f"source_type must be one of {list(SERVICE_SCHEMAS)}, got {st!r}"]
    label = _record_label(rec)
    msgs: list[str] = []
    for err in sorted(_validator(st).iter_errors(rec), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path)
        # The record's own name says "the whole record" better than a placeholder does, so
        # `<root>` is only there for a record that gave nothing to be called by. The path is
        # bracketed rather than set off by a space, because a label is free text and a space does
        # not close: a record titled `subtype` with a bad `subtype` field read
        # "subtype subtype: ...", naming the field twice and marking neither.
        #
        # A nameless record's path is bracketed too, against `<root>`, so that brackets always mean
        # "field path" and an unbracketed head always names the record. Left bare, a path from a
        # nameless record and a label from a named one produce the same one-token head: `subtype: `
        # was both "the subtype field is wrong" and "this record called subtype is wrong".
        if label:
            head = f"{label} [{loc}]" if loc else label
        else:
            head = f"<root> [{loc}]" if loc else "<root>"
        message = _alternatives(err) or err.message
        msgs.append(
            f"{head}: {message}{_when_clause(SERVICE_SCHEMAS[st], err.schema_path, err.schema)}"
        )
    return msgs


def jsonl_lines(text: str) -> list[str]:
    """Split a JSONL document into records on ``\\n`` — and ONLY on ``\\n``.

    Not ``str.splitlines()``, which also breaks on U+2028/U+2029, U+0085 and the vertical tab.
    Those are ordinary characters inside a JSON string, and JSON Lines separates records by ``\\n``,
    so splitting on them tears one valid record into two invalid halves. Real text contains them:
    one U+2028 showed up in a real 500k-document corpus, and it was enough to make a converted
    artifact fail to load with "Unterminated string"."""
    return text.split("\n")


def validate_file(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, message), ...] for every problem in a JSONL corpus ([] == all valid)."""
    problems: list[tuple[int, str]] = []
    for lineno, raw in enumerate(jsonl_lines(Path(path).read_text()), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            problems.append((lineno, f"invalid JSON: {e}"))
            continue
        for msg in record_errors(rec):
            problems.append((lineno, msg))
    return problems
