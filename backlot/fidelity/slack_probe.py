"""What a Slack response carries is asked of slack.com, because no document states it.

The reference documentation answers which methods exist and which arguments they take
(:mod:`backlot.fidelity.slack_docs`). It cannot answer what comes back. ``docs/fidelity.md``,
"What a response carries is asked of slack.com", carries the measurement and this probe's bounds —
the list-empty and conditional-field rules :func:`diff_shapes` and :func:`shape_of` apply, and why
some of its findings are verified by hand instead of by this probe.

Backlot's side of the probe runs on ``PROBE_CORPUS`` below rather than the bundled demo corpus, so
a finding reproduces with ``backlot.serve(records=...)`` and does not move when that corpus does.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import httpx

from backlot.fidelity.errors import CredentialsMissing, FidelityError
from backlot.fidelity.findings import BREAKING, GAP, Finding

# A response key that is a FIELD name. Measured 2026-09-22 across every method Backlot serves:
# every key in Slack's answers matches this, except the conversation ids keying `shares.public` and
# `shares.private` on a file. Those are map entries, not fields, and are collapsed to `{}` — left
# alone they write one path per channel a file was shared into, which says nothing about either
# server's shape and changes whenever the workspace does.
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

# Seconds between two calls to the vendor. Slack applies its limits "per API method per
# workspace/team per app" (`docs.slack.dev/apis/web-api/rate-limits`), and every method here is
# asked once or twice except `conversations.history`, which discovery asks once per channel until
# it finds a thread. Eleven of the twelve are Tier 2 at worst (20+ per minute) and that one is
# Tier 3 (50+); `auth.test` names no tier at all, only "Special rate limits apply". So the pace is
# set by the published floor rather than by a method's own budget, and one call every 1.2s stays
# inside it without a retry loop to get wrong.
_PACE = 1.2

PROBE_CORPUS: tuple[dict[str, Any], ...] = (
    {
        "source_type": "slack",
        "doc_id": "probe-thread",
        "channel": "probe-general",
        "group": "probe",
        "visibility": "public",
        "author_email": "ava@probe.invalid",
        "author_name": "Ava Probe",
        "created": "2026-01-05T10:00:00Z",
        "content": "release checklist for the probe corpus",
        "reactions": [{"name": "eyes", "users": ["bob@probe.invalid"]}],
        "edited": {"user": "ava@probe.invalid", "ts": "1767607260.000000"},
        # A message carrying a file, because this is the only projection through which Backlot
        # serves one: `search.files` answers an empty match list by construction.
        "files": [
            {
                "id": "F0PROBE0001",
                "name": "checklist.txt",
                "mimetype": "text/plain",
                "title": "checklist",
            }
        ],
        "replies": [
            {
                "content": "checklist reviewed",
                "author_email": "bob@probe.invalid",
                "author_name": "Bob Probe",
                "created": "2026-01-05T10:05:00Z",
            }
        ],
    },
    {
        "source_type": "slack",
        "doc_id": "probe-searchable",
        "channel": "probe-random",
        "group": "probe",
        "visibility": "public",
        "author_email": "bob@probe.invalid",
        "author_name": "Bob Probe",
        "created": "2026-01-06T10:00:00Z",
        # A second channel, so a listing has more than one of anything — and the only record
        # carrying the word the search is asked for. Deliberately NOT the threaded one: Backlot
        # adds `thread_ts` to a match inside a thread and omits it otherwise, and a workspace
        # offers no way to guarantee a threaded match to pair it with, so the two sides would be
        # compared across a field only one of them could have.
        "content": "the probe corpus keeps one drosophila here to be searched for",
    },
)


# What Backlot's side is searched for: a word only the unthreaded record above carries, so its
# match is unthreaded too. The vendor's side has no such lever and its query is derived from the
# history the probe just read (see `_searchable_words`). Slack has no query that means "everything"
# to lean on instead — `docs/fidelity.md`, "What a response carries is asked of slack.com", carries
# the measurement for why.
_BACKLOT_QUERY = "drosophila"

# A word worth searching for: four characters or more, out of text with Slack's own markup taken
# off first, so `<@U0BCVV6G3M1>` contributes no token and neither does a link's target.
_WORD = re.compile(r"\w{4,}")
_MARKUP = re.compile(r"<[^>]*>")
# How many of those the probe will try before giving up. Search is the vendor's most restricted
# tier here (Tier 2), and a word drawn from a message that Slack's index does have should match on
# the first or second try.
_QUERY_ATTEMPTS = 3


class LiveProbeTarget(Protocol):
    """What this module needs of a source. Structural, so the registry can import this module
    without this module importing the registry."""

    live_url: str


@dataclass(frozen=True)
class Shape:
    """One response, reduced to what a client can read off it.

    ``fields`` maps a field path to the JSON types seen at it; ``containers`` is every path this
    walk actually looked inside. The second is what makes an absence readable: a field missing
    because the other side returned an empty list is not a divergence, and without recording which
    containers were entered the two cases are indistinguishable.
    """

    fields: dict[str, frozenset[str]]
    containers: frozenset[str]


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        # int and float are one type to a JSON reader, and Slack's own numbers arrive as both:
        # `tz_offset` is an integer and a file's `timestamp` can be either.
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


def shape_of(body: Any) -> Shape:
    """Every field path a response carries, with the JSON types seen at each.

    An array contributes one path for its members rather than one per member, so two pages of the
    same listing read the same. A field present on ANY member counts as present: Slack drops
    fields per object — a deactivated member carries thirteen fewer — and intersecting would
    report the vendor as missing what it serves on every other member.

    An OBJECT is a container whether or not it holds anything, and an EMPTY ARRAY is not. The
    difference is what each answer says: an object with no keys is a shape that was looked at and
    found to carry no fields, so the other side's fields under it are a real divergence, while an
    array with no elements is nothing to look at and says only that this sample had none. Counting
    an empty object as unmeasured hides them — measured 2026-09-22, Backlot answers a channel's
    ``properties`` as ``{}`` where the real API answers ``use_case``, ``tabs``, ``tabz`` and
    ``is_dormant``, and the 31 gaps that makes across ``conversations.list`` and
    ``conversations.info`` were the whole of what those two methods had to report.
    """
    fields: dict[str, set[str]] = {}
    containers: set[str] = set()

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            containers.add(path)
            for key, value in node.items():
                name = key if _FIELD_NAME.match(key) else "{}"
                child = f"{path}.{name}" if path else name
                fields.setdefault(child, set()).add(_json_type(value))
                walk(value, child)
        elif isinstance(node, list):
            for item in node:
                walk(item, f"{path}[]")

    walk(body, "")
    return Shape({k: frozenset(v) for k, v in fields.items()}, frozenset(containers))


def _container(path: str) -> str:
    """The path of the object a field sits in — everything before its own name."""
    return path.rsplit(".", 1)[0] if "." in path else ""


def diff_shapes(method: str, backlot: Shape, real: Shape) -> list[Finding]:
    """Every divergence between two answers to one method, both directions.

    Restricted to paths whose container BOTH sides entered. A field the vendor serves under a list
    Backlot answered empty is not a gap in Backlot's shape, and Backlot's own fields under a list
    the workspace answered empty are not surface it invented — reported as either, an empty result
    on one side turns the whole object into findings. An empty OBJECT is a container all the same;
    see :func:`shape_of` for why the two are not the same answer.
    """
    out: list[Finding] = []
    for path in sorted(set(backlot.fields) - set(real.fields)):
        if _container(path) in real.containers:
            out.append(
                Finding(
                    "extra_field",
                    BREAKING,
                    f"{method} {path}",
                    "Backlot serves it; the real API's answer has no such field",
                )
            )
    for path in sorted(set(real.fields) - set(backlot.fields)):
        if _container(path) in backlot.containers:
            out.append(
                Finding(
                    "missing_field",
                    GAP,
                    f"{method} {path}",
                    "the real API serves it; Backlot does not",
                )
            )
    for path in sorted(set(backlot.fields) & set(real.fields)):
        # `null` is dropped from both sides first: a field this workspace happens to leave null and
        # this corpus fills in is one field at one type, and reporting it would make the comparison
        # a report on two sets of contents.
        mine = backlot.fields[path] - {"null"}
        theirs = real.fields[path] - {"null"}
        if mine and theirs and not (mine & theirs):
            out.append(
                Finding(
                    "type_mismatch",
                    BREAKING,
                    f"{method} {path}",
                    f"real: {'|'.join(sorted(theirs))}, Backlot: {'|'.join(sorted(mine))}",
                )
            )
    return out


class _Caller:
    """One side of the probe, asked with its own base URL and its own token."""

    def __init__(self, base_url: str, token: str, *, pace: float, timeout: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._pace = pace
        self._timeout = timeout
        self._last = 0.0

    def __call__(self, method: str, **arguments: Any) -> dict:
        wait = self._pace - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = f"{self._base_url}/{method}"
        try:
            response = httpx.get(
                url,
                params=arguments,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as e:
            raise FidelityError(f"{url} went unanswered, so nothing was probed: {e}") from e
        finally:
            self._last = time.monotonic()
        try:
            body = response.json()
        except ValueError as e:
            raise FidelityError(f"{url} answered {response.status_code}, not JSON: {e}") from e
        if not isinstance(body, dict) or not body.get("ok"):
            # Not a divergence: a method that could not be called was not compared, and a run that
            # reported it as one would file a bug against Backlot for a scope the token lacks.
            raise FidelityError(
                f"{url} answered {body.get('error', body) if isinstance(body, dict) else body!r}, "
                f"so {method} could not be compared"
            )
        return body


@dataclass(frozen=True)
class Sample:
    """The ids one side's calls are aimed at, discovered through that side's own API.

    Discovered rather than configured, for the reason the S3 probe discovers its bucket: an id the
    probe cannot reach as a client is an id it cannot ask questions about either. The two sides
    hold different values — a live workspace's channel is not this corpus's — which is the whole
    point: what is compared is the shape of each server's answer about its own data.
    """

    channel: str
    thread_ts: str
    user: str
    query: str


def _searchable_words(messages: Any) -> list[str]:
    """Candidate queries, out of the text of messages the probe has already read.

    Longest first, because a long word is the one most likely to be indexed as itself rather than
    swallowed by a stop list, and deterministic for a given history so two runs against an
    unchanged workspace ask the same question.
    """
    words = {
        w for m in messages or () for w in _WORD.findall(_MARKUP.sub(" ", m.get("text") or ""))
    }
    return sorted(words, key=lambda w: (-len(w), w))


def discover(call: _Caller, query: str = "", *, require_admin: bool = False) -> Sample:
    """Find a channel, a threaded message, a person and a query to aim the rest of the probe at.

    Discovered rather than configured: an id the probe cannot reach as a client is one it cannot
    ask questions about either, and each of the four is a condition this workspace either meets or
    does not. Where it does not, that is reported as a contract the probe could not read rather
    than as a clean comparison, because a search that matches nothing leaves a match's own fields
    compared against nothing at all — and that is the whole of what the three search methods add
    over their envelopes.

    ``query`` is passed only for Backlot's side, where the corpus plants a word; the vendor's is
    derived, because Slack has no query that means "everything".

    ``require_admin`` checks the caller's own identity before anything else, by asking `users.info`
    about `auth.test`'s own `user_id` rather than paging `users.list` to find it — a workspace with
    more than one page of members would otherwise put an admin's own entry on a page this probe
    never reads. Backlot's side of the probe is always its admin/service token; a real caller who
    is not an admin gets `has_2fa` on their own member object only, not on every person the way
    Backlot's does, which is a difference in what the *caller* is, not one this probe's shape
    comparison can see. This repository's own misconfiguration — the wrong kind of token set, not
    the vendor's problem — so it raises :class:`~backlot.fidelity.errors.CredentialsMissing`
    rather than a plain contract-unreadable error, the same as a credential nobody set at all.
    """
    if require_admin:
        me = call("auth.test")
        mine = call("users.info", user=me["user_id"]).get("user") or {}
        if not mine.get("is_admin"):
            raise CredentialsMissing(
                "the token behind SLACK_USER_TOKEN is not a workspace admin, so `users.info`'s "
                "`has_2fa` cannot be probed the way Backlot's admin/service token serves it"
            )
    members = call("users.list", limit=200).get("members") or []
    channels = call("conversations.list", limit=200).get("channels") or []
    # A channel the caller has joined first: a joined channel is the one most likely to hold a
    # thread this caller has actually taken part in, which is what the rest of the probe needs.
    for channel in sorted(channels, key=lambda c: not c.get("is_member")):
        messages = call("conversations.history", channel=channel["id"], limit=200).get("messages")
        parents = [m for m in messages or () if m.get("reply_count")]
        if parents:
            break
    else:
        raise FidelityError(
            "no channel readable by this caller holds a message with replies, so "
            "conversations.replies cannot be probed"
        )
    people = [
        m
        for m in members
        if not m.get("is_bot") and not m.get("deleted") and m.get("id") != "USLACKBOT"
    ]
    if not people:
        raise FidelityError("this workspace lists no active person, so users.info cannot be probed")
    candidates = [query] if query else _searchable_words(messages)[:_QUERY_ATTEMPTS]
    for candidate in candidates:
        found = call("search.messages", query=candidate, count=1).get("messages") or {}
        if found.get("matches"):
            return Sample(channel["id"], parents[0]["ts"], people[0]["id"], candidate)
    raise FidelityError(
        f"none of the {len(candidates)} candidate words drawn from the discovered channel's "
        "history matched a search, so a search match cannot be probed"
    )


def _calls(sample: Sample) -> list[tuple[str, dict[str, Any]]]:
    """Every method Backlot serves, with the arguments this side is asked for."""
    return [
        ("api.test", {}),
        ("auth.test", {}),
        ("conversations.list", {"limit": 200}),
        ("conversations.info", {"channel": sample.channel}),
        ("conversations.history", {"channel": sample.channel, "limit": 200}),
        ("conversations.members", {"channel": sample.channel}),
        ("conversations.replies", {"channel": sample.channel, "ts": sample.thread_ts}),
        ("search.all", {"query": sample.query, "count": 20}),
        ("search.files", {"query": sample.query, "count": 20}),
        ("search.messages", {"query": sample.query, "count": 20}),
        ("users.info", {"user": sample.user}),
        ("users.list", {"limit": 200}),
    ]


def probe(backlot: _Caller, real: _Caller) -> list[Finding]:
    """Ask both servers the same twelve methods and compare the shapes that come back."""
    theirs = dict(_calls(discover(real, require_admin=True)))
    out: list[Finding] = []
    for method, ours in _calls(discover(backlot, _BACKLOT_QUERY)):
        out += diff_shapes(
            method, shape_of(backlot(method, **ours)), shape_of(real(method, **theirs[method]))
        )
    return sorted(out, key=lambda f: (f.severity != BREAKING, f.path, f.kind))


def divergences(
    source: LiveProbeTarget, credentials: Mapping[str, str], *, timeout: float = 120.0
) -> list[Finding]:
    """This module's entry point: start a Backlot on the probe corpus and ask both servers."""
    import backlot

    with backlot.serve(records=list(PROBE_CORPUS)) as server:
        return probe(
            _Caller(f"{server.base_url}/slack/api", server.token, pace=0.0, timeout=timeout),
            _Caller(source.live_url, credentials["user_token"], pace=_PACE, timeout=timeout),
        )
