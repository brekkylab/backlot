"""The raw bytes, the escapes and the printable characters ``backlot.request_target`` answers, and
the `Host`s ``backlot.bracketed_host`` answers, over a real uvicorn.

A client library percent-encodes a raw byte or refuses to send it, and the TestClient would too;
the TestClient also escapes a `"`, a `<` and a `>` and leaves out a `#` and what follows it. So
each request here is written to a socket by hand, to the server ``live_server`` starts with
``backlot serve``. A single-byte row names the vendor host whose measured answer it holds; a row
of `_TARGETS`, `_ESCAPES`, `_HOSTS` or `_PRINTABLE` names its path, measured on that path's host.
"""

from __future__ import annotations

import re
import select
import socket
import time
import urllib.parse

import pytest

from backlot import bracketed_host, errors, request_target, store
from tests._helpers import raw_exchange, raw_request, read_answer, served_id

_UTF8, _LONE = b"\xc3\xa9", b"\x80"
_UVICORN = b"Invalid HTTP request received."
_KINDS = {
    "utf-8": _UTF8,
    "lone": _LONE,
    "control": b"\x01",
    "nul": b"\x00",
    "tab": b"\x09",
    "cr": b"\x0d",
    "space": b" ",
    "del": b"\x7f",
    "ff": b"\xff",
}
#: The kinds whose answer is another's: 0x7F is a `control`, 0xFF a lone `high`.
_AS = {"del": "control", "ff": "lone"}

# What the front door answers each kind with, in the path and in a query value. `encoded` is the
# answer to the request with the byte percent-encoded, `encoded, cut` the one with the byte
# percent-encoded and the `%` that ends the path taken off with the hex digit after it, `?` the one
# with each high byte in the path read as a `?` and the other bytes percent-encoded, and ` closed`
# after one of them that answer with the connection closed after it.
_GOOGLE = dict.fromkeys(("utf-8", "lone"), "encoded") | {
    "control": "google 54",
    "nul": "google 54",
    "tab": "google 1555",
    "cr": "google 1555",
    "space": "google 1555",
}
_ATLASSIAN = {
    "utf-8": "encoded",
    "lone": "encoded",
    "control": "edge invalid_characters",
    "nul": "cloudfront",
    "tab": "edge codec_error",
    "cr": "cloudfront",
    "space": "encoded",
}
_CLOUDFLARE_PATH = dict.fromkeys(("utf-8", "lone", "control", "tab"), "cloudflare empty") | {
    "nul": "cloudflare page",
    "cr": "cloudflare page",
    "space": "encoded",
}
_CLOUDFLARE_QUERY = _CLOUDFLARE_PATH | {"space": "cloudflare empty"}
_S3_QUERY = {
    "utf-8": "encoded",
    "lone": "encoded",
    "control": "encoded closed",
    "nul": "s3 empty",
    "tab": "s3 empty",
    "cr": "s3 empty",
    "space": "s3 505",
}
_GRAPH_QUERY = dict.fromkeys(("utf-8", "lone"), "encoded") | {
    "control": "graph invalid url",
    "nul": "graph invalid url",
    "tab": "graph badly formed",
    "cr": "graph badly formed",
    "space": "graph badly formed",
}
_MEASURED = {
    # vendor: (path the byte goes under, the path's answers, the query's answers)
    "api.github.com": (
        "/github/",
        dict.fromkeys(_KINDS, "github"),
        dict.fromkeys(_KINDS, "github"),
    ),
    "slack.com": ("/slack/api/", dict.fromkeys(_KINDS, "slack"), dict.fromkeys(_KINDS, "slack")),
    "www.googleapis.com": ("/drive/v3/files/", _GOOGLE, _GOOGLE),
    "sheets.googleapis.com": ("/sheets/v4/spreadsheets/", _GOOGLE, _GOOGLE),
    "Jira Cloud": ("/atlassian/rest/api/3/", _ATLASSIAN, _ATLASSIAN),
    "Confluence Cloud": ("/atlassian/wiki/rest/api/", _ATLASSIAN, _ATLASSIAN),
    "api.notion.com": ("/notion/v1/", _CLOUDFLARE_PATH, _CLOUDFLARE_QUERY),
    "api.hubapi.com": ("/hubspot/crm/v3/objects/contacts/", _CLOUDFLARE_PATH, _CLOUDFLARE_QUERY),
    "api.linear.app": ("/linear/", _CLOUDFLARE_PATH, _CLOUDFLARE_QUERY),
    "api.fireflies.ai": ("/fireflies/", _CLOUDFLARE_PATH, _CLOUDFLARE_QUERY),
    "s3.us-east-1.amazonaws.com": (
        "/s3/no-such-bucket/",
        _S3_QUERY | {"utf-8": "?", "lone": "?", "nul": "s3 empty"},
        _S3_QUERY,
    ),
    "graph.microsoft.com": ("/msgraph/v1.0/", _GRAPH_QUERY | {"lone": "graph empty"}, _GRAPH_QUERY),
}

# Targets the table above does not hold: raw bytes beside each other or beside an escape, both
# orders where the order decides, and paths outside a family's prefixes: (base, target after it,
# answer), each measured on the host whose path the base is.
_TARGETS = [
    ("/drive/v3/files/", b"nope\x09y\x01x", "google 54"),
    ("/drive/v3/files/", b"nope?b=\x01y\x20x", "google 54"),
    ("/drive/v3/files/", b"nope\x80x?b=\x20x", "google 1555"),
    ("/atlassian/rest/api/3/", b"nope\x01y\x00x", "cloudfront"),
    ("/atlassian/rest/api/3/", b"nope\x01y\x20x", "encoded"),
    ("/atlassian/rest/api/3/", b"nope\x20y\x09x", "encoded"),
    ("/atlassian/rest/api/3/", b"nope x?b=\x01x", "edge invalid_characters"),
    ("/atlassian/rest/api/3/", b"nope\x09x?b= x", "encoded"),
    ("/atlassian/rest/api/3/", b"nope?b=\x09y\x01x", "edge invalid_characters"),
    ("/atlassian/rest/api/3/", b"nope?b=\x01y\x20x", "edge invalid_characters"),
    ("/notion/v1/", b"nope\x01y\x20x", "encoded"),
    ("/notion/v1/", b"nope x?b=\x80x", "cloudflare empty"),
    ("/notion/v1/", b"nope\x01x?b= x", "cloudflare empty"),
    ("/notion/v1/", b"nope\x80y\x0dx", "cloudflare page"),
    # Linear and Fireflies: a path Cloudflare refuses beside a raw byte, its page
    # (`request_target.refused_path`), a broken escape in the query and one the end of the path
    # cuts short (`request_target.cut_escape`), the 400 the byte draws, and a cut one beside a
    # space, the path read as its percent-encoding without it
    *(
        (base, rest, "cloudflare page")
        for base in ("/linear/", "/fireflies/")
        for rest in (
            b"a\xc3\xa9%zz",
            b"a%zz\xc3\xa9",
            b"a\x01%zz",
            b"a\xc3\xa9%00",
            b"a%zz?b=\xc3\xa9",
            b"a%2?b=\xc3\xa9",
            b"a%2\xc3\xa9",
            b"../a\xc3\xa9",
            b"../a b",
        )
    ),
    *(
        (base, rest, "cloudflare empty")
        for base in ("/linear/", "/fireflies/")
        for rest in (b"a\xc3\xa9?b=%zz", b"a\x01?b=%00", b"a\xc3\xa9%2", b"a\x01%2")
    ),
    *((base, b"a b%2", "encoded, cut") for base in ("/linear/", "/fireflies/")),
    ("/s3/no-such-bucket/", b"nope\x0dy x", "s3 empty"),
    ("/s3/no-such-bucket/", b"nope\x09y x", "s3 505"),
    ("/s3/no-such-bucket/", b"nope y\x09x", "s3 505"),
    ("/msgraph/v1.0/", b"nope\x01y\x09x", "graph invalid url"),
    ("/msgraph/v1.0/", b"nope\x09y\x01x", "graph badly formed"),
    ("/msgraph/v1.0/", b"nope\x80x?b=\x01x", "graph invalid url"),
    ("/github", b"?b=\x80x", "github"),
    ("/s3/", b"no-such-bucket\xc3\xa9x/nope", "?"),
    ("/s3/", b"no-such-bucket\xc3\xa9\x01x/nope", "? closed"),
    ("/s3/", b"no-such-bucket\x01\xc3\xa9x/nope", "? closed"),
    ("/s3/", b"no-such-bucket\x01\x80x/nope", "? closed"),
    ("/s3/no-such-bucket/", b"nope\x01\x80x", "? closed"),
    # the CDN's own refusals ahead of the edge's, as Backlot answers them encoded
    ("/atlassian/rest/api/3/", b"nope%00x\x01y", "encoded"),
    ("/atlassian/rest/api/3/", b"nope%G1x\ty", "encoded"),
    ("/atlassian/rest/api/3/", b"a/../nope\x01y", "encoded"),
    # and the edge's own `;` and Tomcat's escaped slash behind it
    ("/atlassian/rest/api/3/", b"nope;x\x01y", "edge invalid_characters"),
    ("/atlassian/wiki/rest/api/", b"nope%2Fx\x01y", "edge invalid_characters"),
    # Google's batch mounts, outside its families' prefixes (www.googleapis.com, and
    # gmail.googleapis.com for Gmail's)
    ("/batch/drive/v3", b"\x01x", "google 54"),
    ("/batch/drive/v3", b"?b=\x00x", "google 54"),
    ("/batch", b"?b=\tx", "google 1555"),
    ("/batch/gmail/v1", b"\x01x", "google 54"),
    ("/batch/gmail/v1", b" x", "google 1555"),
    ("/batch/drive/v3", b"x\x80", "encoded"),
]

# A method the CDN does not pass on, whose refusal comes ahead of the edge's: (method, base,
# target after it, answer), on Jira Cloud but the one Confluence path.
_CDN_METHODS = [
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope\x01y", "encoded"),
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope?b=\x01y", "encoded"),
    (b"PROPFIND", "/atlassian/wiki/rest/api/", b"nope?b=\x01y", "encoded"),
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope\ty", "encoded"),
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope y", "encoded"),
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope\x80y", "encoded"),
    (b"TRACE", "/atlassian/rest/api/3/", b"nope\x01y", "encoded"),
    (b"TRACE", "/atlassian/rest/api/3/", b"nope\ty", "encoded"),
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope\x00y", "cloudfront"),
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope\ry", "cloudfront"),
]


# An escape in the path or the query, sent as an escape: (base, target after it, answer), the answer
# the front door's page or, where it read the target on, the status of the API behind it
# (``request_target.escape_answer`` dates the measurement).
_ESCAPES = [
    ("/slack/api/", b"auth.test%00", "slack empty"),
    ("/slack/api/", b"nope%00x", "slack empty"),
    ("/slack/api/", b"auth.test%01", 404),
    ("/slack/api/", b"auth.test?b=%00x", 200),
    ("/drive/v3/", b"about%00", "google 1555 kept"),
    ("/drive/v3/", b"nope%00x", "google 1555 kept"),
    ("/drive/v3", b"%00x", 404),
    ("/drive/v3/", b"nope%01x", 404),
    ("/batch/drive/v3", b"%00", "google 1555 kept"),
    ("/batch", b"%00x", 404),
    ("/gmail/v1/users/me/", b"profile%00", "google 1555 kept"),
    ("/gmail/v1", b"%00x", "google 1555 kept"),
    ("/sheets/v4/spreadsheets/", b"x%00", "google 1555 kept"),
    ("/sheets/v4", b"%00x", "google 1555 kept"),
    ("/docs/v1", b"%00x", "google 1555 kept"),
    ("/slides/v1/", b"nope%00x", "google 1555 kept"),
    ("/notion/v1/", b"users%00", "cloudflare page"),
    ("/notion/v1/", b"nope%00x", "cloudflare page"),
    ("/notion/v1/", b"nope%01x", 400),
    ("/hubspot/crm/v3/objects/", b"contacts%00", "cloudflare page"),
    ("/hubspot/crm/v3/objects/contacts/", b"nope%00x", "cloudflare page"),
    ("/hubspot/crm/v3/objects/contacts/", b"nope%01x", 404),
    ("/s3/", b"%00", "s3 empty"),
    ("/s3/no-such-bucket/", b"nope%00x", "s3 empty"),
    ("/s3/no-such-bucket/", b"nope%01x", 404),
    *(
        ("/msgraph/v1.0/", b"nope%s" % escape, "graph invalid url")
        for escape in (b"%00x", b"%01x", b"%0ax", b"%0Dx", b"%1Fx", b"%7fx")
    ),
    ("/msgraph/v1.0/", b"me%0A", "graph invalid url"),
    ("/msgraph/v1.0/", b"me?b=%01x", 401),
    # escapes past 0x7F, read as UTF-8 where they decode and byte by byte where they do not
    # (``request_target._graph_refuses``)
    *(
        ("/msgraph/v1.0/", b"nope%s" % escape, "graph invalid url")
        for escape in (
            *(b"%81x", b"%8Dx", b"%8Fx", b"%90x", b"%9Dx", b"%C2%81x", b"%C2%9Dx", b"%81%C3%A9x"),
            *(b"%C3%A9%81x", b"%F4%8Fx", b"%F4%90%80%80x", b"%F4%8F%BF%BEx", b"%F4%8F%BF%BFx"),
        )
    ),
    *(
        ("/msgraph/v1.0/", b"nope%s" % escape, 401)
        for escape in (
            *(b"%80x", b"%82x", b"%F4x", b"%ED%A0%80x", b"%C2%80x", b"%C3%81x", b"%E2%80%9Dx"),
            *(b"%EF%BF%BEx", b"%EF%BF%BFx", b"%F4%8F%BF%BDx"),
        )
    ),
    ("/msgraph/v1.0/", b"%81/nope", "graph invalid url"),
    ("/msgraph/v1.0/", b"me?b=%81x", 401),
    ("/msgraph/v1.0/", b"me?b=%F4%90%80%80x", 401),
    ("/github/", b"nope%00x", 404),
]
#: The hosts real was measured on with no credential, whose rows here send none.
_UNCREDENTIALED = ("/gmail/", "/sheets/", "/docs/", "/slides/", "/s3/", "/msgraph/")
#: A Signature Version 4 `Authorization` for an access key no account holds.
_AWS_AUTHORIZATION = (
    b"Authorization: AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20261030/us-east-1/s3/"
    b"aws4_request, SignedHeaders=host, Signature=" + b"0" * 64 + b"\r\n"
)

# A `Host` in brackets that holds no IPv6 address (``backlot.bracketed_host`` dates the
# measurement): (host, or ``None`` where the header lines carry it, method, base, target after it,
# answer, header lines or none), "served" the answer to the request with a `Host` of `x`. `[0:0]`
# on a path each vendor serves and on Confluence's search with a CQL it refuses, at GitHub's root
# and after another header, the other `Host`s on GitHub's path, `[v1.x]` on each host behind
# Cloudflare and on Google's batch endpoint, Cloudflare's reading of an IPvFuture on Notion, and on
# S3, which serves each as it serves its own `Host`, an IPv6 address in brackets, brackets left
# open, and `[0:0]` with an `Authorization` S3 cannot read.
_HOSTS = [
    *(
        (b"[0:0]", method, base, rest, answer)
        for method, base, rest, answer in (
            (b"GET", "/github/", b"rate_limit", "github"),
            (b"GET", "/slack/api/", b"auth.test", "slack 404 empty"),
            (b"GET", "/atlassian/rest/api/3/", b"serverInfo", "cloudfront 403"),
            (b"GET", "/atlassian/wiki/rest/api/", b"space", "cloudfront 403"),
            (
                b"GET",
                "/atlassian/wiki/rest/api/",
                b"search?cql=space.key%3D%22a%27b%22",
                "cloudfront 403",
            ),
            (b"GET", "/drive/v3/", b"about", "google 1555 kept"),
            (b"GET", "/gmail/v1/users/me/", b"profile", "google 1555 kept"),
            (b"GET", "/sheets/v4/spreadsheets/", b"x", "google 1555 kept"),
            (b"GET", "/docs/v1/documents/", b"x", "google 1555 kept"),
            (b"GET", "/slides/v1/presentations/", b"x", "google 1555 kept"),
            (b"POST", "/batch/drive/", b"v3", "google 1555 kept"),
            (b"GET", "/notion/v1/", b"users", "cloudflare page"),
            (b"GET", "/hubspot/crm/v3/objects/", b"contacts", "cloudflare page"),
            (b"POST", "/linear/", b"graphql", "cloudflare page"),
            (b"POST", "/fireflies/", b"graphql", "cloudflare page"),
            (b"GET", "/msgraph/v1.0/", b"me", "graph invalid hostname"),
            (b"GET", "/s3/", b"", "served"),
            (b"GET", "/s3/no-such-bucket/", b"x", "served"),
            (b"GET", "/github", b"", "github"),
        )
    ),
    *(
        (host, b"GET", "/github/", b"rate_limit", "github")
        for host in (b"[0:0]:443", b"[x]", b"[1.2.3.4]", b"[v1.x]", b"[:]", b"[g::1]")
    ),
    (None, b"GET", "/github/", b"rate_limit", "github", b"User-Agent: x\r\nHost: [0:0]\r\n"),
    *(
        (b"[v1.x]", method, base, rest, "cloudflare forbidden")
        for method, base, rest in (
            (b"GET", "/notion/v1/", b"users"),
            (b"GET", "/hubspot/crm/v3/objects/", b"contacts"),
            (b"POST", "/linear/", b"graphql"),
            (b"POST", "/fireflies/", b"graphql"),
        )
    ),
    (b"[v1.x]", b"POST", "/batch/drive/", b"v3", "google 1555 kept"),
    *(
        (host, b"GET", "/notion/v1/", b"users", answer)
        for host, answer in (
            (b"[vF.x]", "cloudflare forbidden"),
            (b"[v1.]", "cloudflare forbidden"),
            (b"[v1.:]", "cloudflare forbidden"),
            (b"[v1.x.y.z]", "cloudflare forbidden"),
            (b"[v1.-.x]", "cloudflare forbidden"),
            (b"[v1.x-.]", "cloudflare forbidden"),
            (b"[vAb.x]", "cloudflare page"),
            (b"[V1.x]", "cloudflare page"),
            (b"[vx.y]", "cloudflare page"),
            (b"[v1]", "cloudflare page"),
            (b"[v1..]", "cloudflare page"),
            (b"[v1.x..]", "cloudflare page"),
            (b"[v1.~]", "cloudflare page"),
        )
    ),
    *(
        (host, b"GET", "/s3/no-such-bucket/", b"x", "served")
        for host in (b"[::1]", b"[::1]:443", b"[::]", b"[ABCD::1]", b"[::ffff:1.2.3.4]", b"[0:0")
    ),
    (b"[0:0]", b"GET", "/s3/no-such-bucket/", b"x", "served", _AWS_AUTHORIZATION),
]

#: A broken escape or a `%00` in the path beside a raw byte, which Cloudflare in front of Notion
#: and HubSpot answers with its page.
_BESIDE_A_RAW_BYTE = (
    *(b"nope\x01y%zzx", b"nope%zzy\x01x", b"nope\xc3\xa9y%zzx", b"nope%zzy\xc3\xa9x"),
    *(b"nope\ty%zzx", b"nope%zzy\tx", b"nope\xe9y%zzx", b"nope%zzx?b=\x01", b"nope%zzx?b=\xc3\xa9"),
    *(b"nope\x01y%00x", b"nope%00y\x01x", b"nope\xc3\xa9y%00x"),
)
#: A `%` or a `%4` that ends the path after a raw byte, which that Cloudflare answers as the byte.
_CUT_AFTER_A_RAW_BYTE = (b"nope\x01y%", b"nope\x01y%4", b"nope\xc3\xa9y%", b"nope\xc3\xa9y%4")

# A printable character the front door reads otherwise or refuses, sent as itself, among them a `%`
# two hex digits do not follow and a `#` (``request_target.printable_answer`` and
# ``request_target.printable_reading`` date the measurements,
# ``backlot.main.refuse_a_broken_escape_in_a_github_query`` GitHub's empty 400,
# ``errors.atlassian.echoed_spelling`` Jira's and Confluence's echo, and
# ``errors.atlassian.front_door`` Confluence's Tomcat): (base, target after it, answer),
# `as <target>` the answer to the base with that target after it, which is how the front door reads
# this one.
_PRINTABLE = [
    # Slack: Apache's page for a broken escape in the path, envoy's empty 400 for a `#` anywhere,
    # ahead of a broken escape and behind a raw byte, and the one `%00` draws ahead of either
    *(
        ("/slack/api/", rest, "slack apache")
        for rest in (b"nope%x", b"nope%zzx", b"nope%4x", b"nope%", b"nope%4", b"auth.test%zz")
    ),
    ("/slack/api/", b"auth.test?b=%x", 200),
    *(
        ("/slack/api/", rest, "slack empty")
        for rest in (
            *(b"auth.test#x", b"auth.test?b=1#", b"#", b"nope#y%x", b"nope%y#x"),
            *(b"auth.test?b=#x%", b"nope%00y#x", b"nope#y%00x", b"nope%00y%x", b"nope%y%00x"),
        )
    ),
    *(
        ("/slack/api/", rest, "slack")
        for rest in (b"nope\x01y#x", b"nope#y\x01x", b"nope\x01y%x", b"nope%y\x01x")
    ),
    ("/slack/api/", b"auth.test?b=\x01y#x", "slack"),
    # GitHub: its page for a `#` anywhere, ahead of a broken escape; the empty 400 for a broken
    # escape in the query of a path a route serves; a broken escape in the path read on
    *(
        ("/github/", rest, "github")
        for rest in (b"rate_limit#x", b"#x", b"rate_limit?b=1#x", b"rate_limit?b=1#", b"nope#")
    ),
    ("/github/", b"nope#y%x", "github"),
    ("/github/", b"nope%y#x", "github"),
    *(
        ("/github/", rest, "github empty")
        for rest in (
            *(b"rate_limit?b=%x", b"rate_limit?b=%", b"rate_limit?b=%4", b"rate_limit?b=%4x"),
            *(b"rate_limit?b=%zz", b"rate_limit?%x=1", b"rate_limit?c=1&b=%"),
            *(b"rate_limit?c=1&b=%4", b"repos/octocat/Hello-World?b=%x"),
        )
    ),
    ("/github/", b"rate_limit?b=%25x", 200),
    *(
        ("/github/", rest, 404)
        for rest in (b"nope?b=%x", b"nope%x", b"nope%zzx", b"nope%", b"nope%4", b"rate_limit%x")
    ),
    # Graph: its page for a broken escape, a `"`, a `#`, a `<` or a `>` in the path, each read on
    # in the query
    *(
        ("/msgraph/v1.0/", rest, "graph invalid url")
        for rest in (
            *(b'nope"x', b"nope<x", b"nope>x", b"nope#x", b"nope%x", b"nope%zzx", b"nope%4x"),
            *(b"nope%", b"nope%4", b'me"x', b"me#x"),
        )
    ),
    *(("/msgraph/v1.0/", rest, 401) for rest in (b'me?b="x', b"me?b=#x", b"me?b=%x")),
    ("/msgraph/v1.0/", b'nope\x01y"x', "graph invalid url"),
    ("/msgraph/v1.0/", b'nope"y\x01x', "graph invalid url"),
    # S3: a `#` in the path one of its characters, and the empty 400 for a broken escape in the
    # path, a `#` before it or after it, where one in the query is read on
    ("/s3/", b"nope#x", "as nope%23x"),
    ("/s3/", b"nope#x/key", "as nope%23x/key"),
    ("/s3/no-such-bucket/", b"nope#x", "as nope%23x"),
    *(
        ("/s3/", rest, "s3 empty")
        for rest in (b"nope%x", b"nope%zzx", b"nope%4x", b"nope%", b"nope%4", b"nope%y#x")
    ),
    ("/s3/", b"nope#y%x", "s3 empty"),
    *(("/s3/no-such-bucket/", rest, "s3 empty") for rest in (b"nope%x", b"nope%zzx", b"nope%")),
    ("/s3/no-such-bucket/", b"x?b=%x", 404),
    # beside a raw byte, read on
    *(
        ("/s3/", rest, "s3 empty")
        for rest in (b"nope\x01y%x", b"nope%y\x01x", b"nope\xc3\xa9y%x", b"nope%y\xc3\xa9x")
    ),
    ("/s3/", b"nope\x01y#x", 400),
    ("/s3/", b"nope#y\x01x", 400),
    # Notion: Cloudflare's page for a broken escape in the path but where the end of the target
    # cuts it short, which Notion reads itself, and nothing read past a `#`
    *(
        ("/notion/v1/", rest, "cloudflare page")
        for rest in (b"nope%x", b"nope%zzx", b"nope%4x", b"users%zz", b"users/me%zz", b"nope%y#x")
    ),
    ("/notion/v1/", b"nope%", 400),
    ("/notion/v1/", b"nope%4", 400),
    ("/notion/v1/", b"nope#y%x", "as nope"),
    ("/notion/v1/", b"users?b=%x", 400),
    *(("/notion/v1/", rest, "cloudflare page") for rest in _BESIDE_A_RAW_BYTE),
    *(("/notion/v1/", rest, "cloudflare empty") for rest in _CUT_AFTER_A_RAW_BYTE),
    # HubSpot: Cloudflare's page for a broken escape in the path, HubSpot's where the end of the
    # target cuts it short, Jetty's for a `{` or a `}` under `/crm/v3/objects/`, behind
    # Cloudflare's; a `;` and what follows it up to the next `/` no part of the path; a broken
    # escape in the query dropping the credential; a `\` read as itself
    *(
        ("/hubspot/crm/v3/objects/contacts/", rest, "cloudflare page")
        for rest in (b"nope%x", b"nope%zzx", b"nope%4x", b"nope{y%x", b"nope%y{x")
    ),
    ("/hubspot/crm/v3/objects/contacts/", b"nope%", "hubspot error page"),
    ("/hubspot/crm/v3/objects/contacts/", b"nope%4", "hubspot error page"),
    *(
        ("/hubspot/crm/v3/objects/", rest, "hubspot jetty")
        for rest in (b"contacts/nope{x", b"contacts/nope}x", b"contacts{x", b"{x/contacts")
    ),
    ("/hubspot/crm/v3/objects/", b"contacts/nope{y;x", "hubspot jetty"),
    ("/hubspot/crm/v3/objects/", b"contacts?b={x", 200),
    ("/hubspot/crm/v3/objects/contacts/", b"nope;x", "as nope"),
    ("/hubspot/crm/v3/objects/contacts/", b"nope;x;y", "as nope"),
    ("/hubspot/crm/v3/objects/", b"contacts;x", "as contacts"),
    ("/hubspot/crm/v3/objects/contacts/", b";x", "as "),
    *(
        ("/hubspot/crm/v3/objects/", rest, 401)
        for rest in (
            *(b"contacts?b=%x", b"contacts?b=%", b"contacts?b=%4", b"contacts?b=%zz"),
            *(b"contacts?b=%4x", b"contacts?%x=1", b"contacts/nope?b=%x"),
        )
    ),
    ("/hubspot/crm/v3/objects/contacts/", b"nope\\x", "as nope%5Cx"),
    *(
        ("/hubspot/crm/v3/objects/contacts/", rest, "cloudflare page")
        for rest in _BESIDE_A_RAW_BYTE
    ),
    *(
        ("/hubspot/crm/v3/objects/contacts/", rest, "cloudflare empty")
        for rest in (
            *_CUT_AFTER_A_RAW_BYTE,
            *(b"nope\x01y{x", b"nope{y\x01x", b"nope\xc3\xa9y{x", b"nope{x?b=\x01"),
        )
    ),
    # Google: a `\` in the path a `/`, and on Drive a `+` in the path a space
    ("/drive/v3/files/", b"nope+x", "as nope%20x"),
    ("/drive/v3/files/", b"nope++x", "as nope%20%20x"),
    ("/drive/v3/files/", b"nope+x/permissions", "as nope%20x/permissions"),
    ("/drive/v3/", b"files\\nope", "as files/nope"),
    ("/drive/v3", b"\\about", "as /about"),
    ("/drive", b"\\v3/about", "as /v3/about"),
    ("/drive/v3/files/", b"nope\\x", "as nope/x"),
    ("/gmail/v1/users/me/", b"nope\\x", "as nope/x"),
    ("/docs/v1", b"\\documents/x", "as /documents/x"),
    ("/docs/v1/documents", b"\\x", "as /x"),
    ("/docs/v1/documents/", b"nope\\x", "as nope/x"),
    ("/sheets/v4", b"\\spreadsheets/x", "as /spreadsheets/x"),
    ("/sheets/v4/spreadsheets", b"\\x", "as /x"),
    ("/sheets/v4/spreadsheets/", b"nope\\x", "as nope/x"),
    ("/slides/v1/presentations/", b"nope\\x", "as nope/x"),
    # Jira and Confluence: a `\` in the path a `/`, and eight characters echoed as their escapes
    ("/atlassian/rest/api/3/", b"nope\\x", "as nope/x"),
    ("/atlassian/rest/api/3/", b"nope\\\\x", "as nope/x"),
    ("/atlassian/rest/api/", b"3\\serverInfo", "as 3/serverInfo"),
    ("/atlassian/rest/api", b"\\3/serverInfo", "as /3/serverInfo"),
    ("/atlassian/wiki/rest/api", b"\\space", "as /space"),
    ("/atlassian/wiki/rest/api/", b"nope\\x", "as nope/x"),
    *(
        (base, b"nope%cx" % c, "as nope%%%02Xx" % c)
        for base in ("/atlassian/rest/api/3/", "/atlassian/wiki/rest/api/")
        for c in b'"<>^`{|}'
    ),
    # Confluence's Tomcat: the three of the eleven characters it refuses in a query value that the
    # test client escapes (``tests.test_atlassian._FRONT_DOOR_ROWS`` holds the other eight)
    *(("/atlassian/wiki/rest/api/", b"nope?b=%cx" % c, "tomcat") for c in b'"<>'),
    # behind the edge's page for a raw control byte, and beside a raw UTF-8 one
    *(
        ("/atlassian/wiki/rest/api/", rest, "edge invalid_characters")
        for rest in (b"nope\x01y[x", b"nope[y\x01x", b"space?b=\x01y[x", b"space?b=[y\x01x")
    ),
    *(
        ("/atlassian/wiki/rest/api/", rest, "tomcat")
        for rest in (b"nope\xc3\xa9y[x", b"nope[y\xc3\xa9x", b"space?b=\xc3\xa9y[x")
    ),
]


def _rows():
    for host, (base, path, query) in _MEASURED.items():
        for kind, sent in _KINDS.items():
            answer = _AS.get(kind, kind)
            ids = (f"{host} path {kind}", f"{host} {kind}")
            yield pytest.param(
                b"GET", base, b"nope" + sent + b"x", path[answer], 1, b"", b"x", id=ids[0]
            )
            yield pytest.param(
                b"GET", base, b"nope?b=" + sent + b"x", query[answer], 1, b"", b"x", id=ids[1]
            )
    for base, rest, answer in _TARGETS:
        yield pytest.param(b"GET", base, rest, answer, 1, b"", b"x", id=f"{base} {rest!r}")
    for method, base, rest, answer in _CDN_METHODS:
        yield pytest.param(
            method, base, rest, answer, 1, b"", b"x", id=f"{method.decode()} {base} {rest!r}"
        )
    # `Authorization` twice, which Cloudflare refuses ahead of the bytes
    for base, sent in (("/linear/", b"\t"), ("/fireflies/", b"\x01"), ("/notion/v1/", b"\t")):
        rest = b"nope" + sent + b"x"
        yield pytest.param(b"GET", base, rest, "cloudflare page", 2, b"", b"x", id=f"{base} twice")
    yield pytest.param(
        b"GET", "/hubspot/crm/v3/objects/contacts/", b"nope\tx", "cloudflare page", 2, b"", b"x"
    )
    # a conditional header twice, which Cloudflare refuses ahead of the bytes, and once, which
    # draws the empty 400 the byte alone draws: on each vendor, with no credential, a `\x01` in
    # the query
    for base in ("/notion/v1/", "/linear/", "/fireflies/", "/hubspot/crm/v3/objects/contacts/"):
        for header, answer, times in (
            (b"If-None-Match: *\r\nIf-None-Match: *\r\n", "cloudflare page", "twice"),
            (b"If-Match: *\r\nIf-Match: *\r\n", "cloudflare page", "twice"),
            (b"If-None-Match: *\r\n", "cloudflare empty", "once"),
        ):
            name = header.split(b":")[0].decode()
            yield pytest.param(
                b"GET", base, b"nope?b=\x01x", answer, 0, header, b"x", id=f"{base} {name} {times}"
            )
    yield pytest.param(
        b"GET", "/_meta/", b"nope\x80x", "uvicorn", 1, b"", b"x", id="a path no vendor owns"
    )
    for base, rest, answer in _ESCAPES:
        authorizations = 0 if base.startswith(_UNCREDENTIALED) else 1
        yield pytest.param(
            b"GET", base, rest, answer, authorizations, b"", b"x", id=f"{base} {rest!r}"
        )
    for host, method, base, rest, answer, *header in _HOSTS:
        authorizations = 0 if base.startswith(_UNCREDENTIALED) else 1
        name = (host or b"(in the lines)").decode()
        id = f"{name} {method.decode()} {base}{rest.decode()}" + " +header" * len(header)
        yield pytest.param(
            method, base, rest, answer, authorizations, *header or [b""], host, id=id
        )
    for base, rest, answer in _PRINTABLE:
        authorizations = 0 if base.startswith(_UNCREDENTIALED) else 1
        yield pytest.param(
            b"GET", base, rest, answer, authorizations, b"", b"x", id=f"{base} {rest!r}"
        )
    # with no credential, and on GitHub with a token that names no one, which real reads on past a
    # broken escape in the query
    for base, rest, answer, header in (
        ("/hubspot/crm/v3/objects/contacts/", b"nope{x", "hubspot jetty", b""),
        ("/hubspot/crm/v3/objects/contacts/", b"nope%x", "cloudflare page", b""),
        ("/hubspot/crm/v3/objects/contacts/", b"nope%", "hubspot error page", b""),
        ("/hubspot/crm/v3/objects/", b"contacts?b=%x", 401, b""),
        ("/slack/api/", b"nope%x", "slack apache", b""),
        ("/slack/api/", b"auth.test?b=1#x", "slack empty", b""),
        ("/notion/v1/", b"nope%x", "cloudflare page", b""),
        ("/notion/v1/", b"nope%", 400, b""),
        ("/notion/v1/", b"nope%4", 400, b""),
        ("/github/", b"rate_limit?b=1#x", "github", b""),
        ("/github/", b"nope%x#y", "github", b""),
        ("/github/", b"rate_limit?b=%x", 200, b""),
        ("/github/", b"user?b=%x", 401, b""),
        ("/github/", b"rate_limit?b=%x", 401, b"Authorization: token nope\r\n"),
    ):
        kind = "a token that names no one" if header else "no credential"
        yield pytest.param(
            b"GET", base, rest, answer, 0, header, b"x", id=f"{base} {rest!r} {kind}"
        )
    # a read sent as a POST naming GET, which Google's API reads behind its front door
    override = b"X-HTTP-Method-Override: GET\r\n"
    for sent, answer in ((b"\x01", "google 54"), (b"\t", "google 1555"), (b"\x80", "encoded")):
        rest = b"?fields=kind&b=" + sent + b"x"
        yield pytest.param(
            b"POST", "/drive/v3/about", rest, answer, 1, override, b"x", id=f"override {sent!r}"
        )


def _send(
    base_url: str,
    method: bytes,
    target: bytes,
    admin: str | None,
    times: int = 1,
    header: bytes = b"",
    host: bytes = b"x",
) -> tuple[int, dict[str, str], bytes]:
    authorization = f"Bearer {admin}" if admin else None
    request = raw_request(method, target, authorization, times=times, headers=header, host=host)
    return raw_exchange(base_url, request)


def _encoded(target: bytes, question_marks: bool = False) -> bytes:
    path, mark, query = target.partition(b"?")
    if question_marks:
        path = re.sub(rb"[\x80-\xff]", b"%3F", path)
    raw = re.compile(rb"[\x00-\x20\x7f-\xff]")
    path, query = (raw.sub(lambda m: b"%%%02X" % m[0][0], part) for part in (path, query))
    return path + mark + query


#: The answers after which the connection reads another request; it is closed after every other one
#: (``backlot.request_target._page``).
_KEEPS_THE_CONNECTION = {
    "encoded",
    "encoded, cut",
    "?",
    "cloudflare empty",
    "graph empty",
    "google 1555 kept",
    "slack 404 empty",
    "cloudfront 403",
    "cloudflare forbidden",
    "served",
    "slack apache",
    "hubspot jetty",
    "hubspot error page",
    "github empty",
    "tomcat",
}


def _ids_left_out(base: str, body: bytes) -> object:
    """What two answers to one request share: S3's error and Graph's carry ids seeded from the
    request or drawn at random."""
    if base.startswith("/s3/"):
        return re.findall(rb"<(Code|BucketName|Key)>(.*?)</", body)
    if base.startswith("/msgraph/"):
        return re.findall(rb'"code":"(.*?)"', body)
    return body


_PAGES = {
    "github": (400, "text/html; charset=utf-8", request_target.GITHUB_PAGE),
    "slack": (400, "text/plain", b"Bad Request"),
    "slack empty": (400, None, b""),
    "slack 404 empty": (404, None, b""),
    "google 54": (400, "text/html; charset=UTF-8", request_target.GOOGLE_PAGE_54),
    "google 1555": (400, "text/html; charset=UTF-8", request_target.GOOGLE_PAGE_1555),
    "cloudflare page": (400, "text/html", request_target.CLOUDFLARE_PAGE),
    "cloudflare empty": (400, None, b""),
    "cloudflare forbidden": (403, "text/html", bracketed_host.CLOUDFLARE_FORBIDDEN),
    "s3 empty": (400, None, b""),
    "graph empty": (400, None, b""),
    "graph invalid url": (400, "text/html; charset=us-ascii", request_target.GRAPH_INVALID_URL),
    "graph invalid hostname": (
        400,
        "text/html; charset=us-ascii",
        request_target.GRAPH_INVALID_HOSTNAME,
    ),
    "graph badly formed": (
        400,
        "text/html; charset=us-ascii",
        request_target.GRAPH_BADLY_FORMED,
    ),
    "uvicorn": (400, "text/plain; charset=utf-8", _UVICORN),
    "slack apache": (400, "text/html; charset=iso-8859-1", request_target.SLACK_APACHE_PAGE),
    "hubspot jetty": (400, "text/html;charset=iso-8859-1", request_target.HUBSPOT_JETTY_PAGE),
    "hubspot error page": (400, "text/html; charset=utf-8", request_target.HUBSPOT_ERROR_PAGE),
    "github empty": (400, None, b""),
    "tomcat": (
        400,
        errors.atlassian.TOMCAT_MEDIA_TYPE,
        errors.atlassian.tomcat_page(400).encode(),
    ),
}


def _page(answer: str, method: bytes, target: bytes) -> tuple[int, str | None, bytes]:
    if answer == "cloudfront":
        raw_path = _encoded(target).partition(b"?")[0].decode("latin-1")
        page = errors.atlassian.cdn_bad_request(method.decode(), raw_path)
        return 400, "text/html", page.encode()
    if answer.startswith("edge "):
        return 400, "text/html", errors.atlassian.edge_page(f"http1.{answer[5:]}").encode()
    if answer == "cloudfront 403":
        raw_path = target.partition(b"?")[0].decode("latin-1")
        return (
            403,
            "text/html",
            errors.atlassian.cdn_refused_host(method.decode(), raw_path).encode(),
        )
    return _PAGES[answer]


@pytest.mark.parametrize("method, base, rest, answer, authorizations, header, host", list(_rows()))
def test_a_byte_an_escape_or_a_host_is_answered_as_the_vendors_front_door_answers_it(
    live_server, method, base, rest, answer, authorizations, header, host
):
    base_url, settings = live_server
    admin = None if base.startswith("/s3/") else settings.admin_token
    target = base.encode() + rest
    status, headers, body = _send(base_url, method, target, admin, authorizations, header, host)
    if isinstance(answer, int):  # the API behind the front door, which read the target on
        assert status == answer
        assert body not in {page for _, _, page in _PAGES.values() if page}
        return
    if answer.startswith("as "):  # the target the front door reads this one as
        twin_target = base.encode() + answer.removeprefix("as ").encode()
        twin = _send(base_url, method, twin_target, admin, authorizations, header, host)
        assert (status, headers.get("content-type"), headers.get("connection")) == (
            twin[0],
            twin[1].get("content-type"),
            twin[1].get("connection"),
        )
        assert _ids_left_out(base, body) == _ids_left_out(base, twin[2])
        return
    assert headers.get("connection") == (None if answer in _KEEPS_THE_CONNECTION else "close")
    reading = answer.removesuffix(" closed").removesuffix(" kept")
    if reading in ("encoded", "encoded, cut", "?", "served"):
        encoded = target if reading == "served" else _encoded(target, question_marks=reading == "?")
        if reading == "encoded, cut":
            encoded = re.sub(rb"%[0-9A-Fa-f]?\Z", b"", encoded)
        twin = _send(base_url, method, encoded, admin, header=header)
        assert status == twin[0]
        assert _ids_left_out(base, body) == _ids_left_out(base, twin[2])
        return
    if answer == "s3 505":
        assert (status, headers["content-type"]) == (505, "application/xml")
        assert b"<Code>HttpVersionNotSupported</Code>" in body
        return
    want_status, want_type, want_body = _page(reading, method, target)
    assert (status, headers.get("content-type"), body) == (want_status, want_type, want_body)
    if answer == "github empty":
        assert headers["vary"] == "Accept-Encoding, Accept, X-Requested-With"
        assert "x-github-request-id" in headers
    # the edge's page carries the edge's two headers beside its own, Tomcat's behind it the two
    # alone, CloudFront's neither
    edge = {"x-content-type-options": "nosniff", "x-xss-protection": "1; mode=block"}
    if answer.startswith("edge ") or answer == "tomcat":
        allow_origin = "*" if answer.startswith("edge ") else None
        assert {k: headers.get(k) for k in [*edge, "access-control-allow-origin"]} == edge | {
            "access-control-allow-origin": allow_origin
        }
    else:
        assert not edge.keys() & headers.keys()


@pytest.mark.parametrize(
    "spelled, record, made_up, outsider",
    [
        ("/atlassian/wiki/rest/api/content\\{}", "cf-comp", "999999999", "ava@acme.com"),
        ("/hubspot/crm/v3/objects/companies/{};x", "hs-co-secret", "999999999", "ava@acme.com"),
        ("/drive/v3/files\\{}", "Q1 Revenue Model", "nope", "mia@acme.com"),
    ],
)
def test_a_record_an_outsider_cannot_read_is_a_made_up_id_however_the_front_door_reads_it(
    live_server, tokens, spelled, record, made_up, outsider
):
    """A record named in a spelling the front door reads as another path
    (``request_target.printable_reading``) answers a caller who cannot read it what a made-up id in
    the same spelling answers, status and body byte for byte but for the id where the body names
    it. The admin's 200 on the same target is the control."""
    base_url, settings = live_server
    if record.startswith(("cf-", "hs-")):
        source = "confluence" if record.startswith("cf-") else "hubspot"
        id = str(served_id(source, record))
    else:
        conn = store.connect_ro(settings.db_path)
        table = store.table("google_drive")
        id = conn.execute(f"SELECT id FROM {table} WHERE title = ?", (record,)).fetchone()["id"]
        conn.close()
    target = spelled.format(id).encode()
    assert _send(base_url, b"GET", target, settings.admin_token)[0] == 200
    hidden = _send(base_url, b"GET", target, tokens[outsider])
    unknown = _send(base_url, b"GET", spelled.format(made_up).encode(), tokens[outsider])
    assert hidden[0] == unknown[0] == 404
    assert hidden[1]["content-type"] == unknown[1]["content-type"]
    assert hidden[2].replace(id.encode(), made_up.encode()) == unknown[2]


@pytest.mark.parametrize(
    "line, encoded, sent",
    [
        (b"GET /a\x80b HTTP/1.1\r\n", b"GET /a%80b HTTP/1.1\r\n", b"/a\x80b"),
        (b"GET /a\x80b HTTP/1.1\n", b"GET /a%80b HTTP/1.1\n", b"/a\x80b"),
        (b"GET /a b?c d HTTP/1.1\r\n", b"GET /a%20b?c%20d HTTP/1.1\r\n", b"/a b?c d"),
        (b"GET /a\rb HTTP/1.1\r\n", b"GET /a%0Db HTTP/1.1\r\n", b"/a\rb"),
        (b"GET /a%20b HTTP/1.1\r\n", b"GET /a%20b HTTP/1.1\r\n", None),
        (b"GET /a\x80b\r\n", b"GET /a\x80b\r\n", None),
        (b"GET /a\x80b HTTQ/1.1\r\n", b"GET /a\x80b HTTQ/1.1\r\n", None),
        (b" /a\x80b HTTP/1.1\r\n", b" /a\x80b HTTP/1.1\r\n", None),
    ],
)
def test_the_target_is_what_lies_between_the_first_space_and_the_last(line, encoded, sent):
    assert request_target.encode_line(line) == (encoded, sent)


@pytest.mark.parametrize(
    "head, rest, statuses, body, before_the_rest",
    [
        # Cloudflare's empty 400, after which the connection reads the next request
        (b"GET /notion/v1/no", b"pe\tx", [200, 400, 200], b"", False),
        # GitHub's page, after which it is closed
        (b"GET /github/no", b"pe\x80x", [200, 400], request_target.GITHUB_PAGE, False),
        # S3 closes it after the answer it gives a control read as its escape
        (b"GET /s3/no-such-bucket/no", b"pe\x01x", [200, 404], None, False),
        # a method httptools does not know, one past its end, and none: uvicorn's 400 to the
        # first read alone
        (b"hello", b" /github/nope\x80x", [200, 400], _UVICORN, True),
        (b"GETX", b" /github/nope\x80x", [200, 400], _UVICORN, True),
        (b" GET", b" /github/nope\x80x", [200, 400], _UVICORN, True),
    ],
)
def test_a_request_line_in_two_reads_after_another_on_one_connection(
    live_server, head, rest, statuses, body, before_the_rest
):
    """The request line arrives in two reads, after a request answered on the same connection. The
    first read waits for the second where it starts a request line, and is answered alone where it
    does not (``backlot.protocol._starts_a_request_line``)."""
    base_url, settings = live_server
    url = urllib.parse.urlsplit(base_url)
    auth = (
        b"" if b"/s3/" in head else b"Authorization: Bearer %s\r\n" % settings.admin_token.encode()
    )
    with socket.create_connection((url.hostname, url.port), timeout=10) as c:
        reader = c.makefile("rb")
        answers, early = [], []
        for first, second in (
            (b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n", b""),
            (head, rest + b" HTTP/1.1\r\nHost: x\r\n" + auth + b"\r\n"),
            (b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n", b""),
        ):
            try:
                c.sendall(first)
                if second:
                    early.append(bool(select.select([c], [], [], 1)[0]))
                    if not early[-1]:
                        c.sendall(second)
                answer = read_answer(reader)
            except OSError:  # closed after the answer before
                break
            if answer is None:
                break
            answers.append(answer)
    assert early == [before_the_rest]
    assert [status for status, _, _ in answers] == statuses
    if body is not None:
        assert answers[1][2] == body


@pytest.mark.parametrize(
    "form, answered",
    [
        (b"token={token}", b'"ok":true'),
        (b"GET", b'"error":"invalid_form_data"'),
    ],
)
def test_a_body_in_a_read_of_its_own_reaches_the_route(live_server, form, answered):
    """A read that starts no request is the parser's as it came: a body with no LF in it, sent after
    its headers, is answered, one that reads as the start of a request line too."""
    base_url, settings = live_server
    url = urllib.parse.urlsplit(base_url)
    form = form.replace(b"{token}", settings.admin_token.encode())
    head = (
        b"POST /slack/api/auth.test HTTP/1.1\r\nHost: x\r\nConnection: close\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\nContent-Length: %d\r\n\r\n" % len(form)
    )
    with socket.create_connection((url.hostname, url.port), timeout=10) as c:
        c.sendall(head)
        time.sleep(0.2)
        c.sendall(form)
        answer = b""
        while chunk := c.recv(65536):
            answer += chunk
    assert answer.startswith(b"HTTP/1.1 200 ")
    assert answered in answer
