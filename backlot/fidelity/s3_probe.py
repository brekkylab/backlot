"""S3 is compared by asking the server, not by reading a document.

Every other source declares one path per operation in the spec its vendor publishes, so listing
the paths on each side and diffing them says something. S3 does not: it dispatches on the QUERY STRING. ``GET /{Bucket}`` is
ListObjects, ``GET /{Bucket}?list-type=2`` is ListObjectsV2, ``GET /{Bucket}?location`` is
GetBucketLocation, and ``?acl``, ``?policy``, ``?versioning`` and ninety more are each a different
operation at the same path. Backlot serves all of them from four catch-all routes that declare no
query parameters and read the string themselves, so a path-and-parameter diff pairs every S3
operation with the same route and reports a clean match every time — a green check that means
nothing, which is worse than not checking at all.

What can be asked instead is what the server actually answers. An operation Backlot does not
implement should be REFUSED. The failure this looks for is the third possibility: answering it with
some other operation's body. A caller asking for a bucket's versioning configuration and receiving
an object listing under a 200 gets no error, no log line, and a parse that quietly produces
nonsense — which is the exact failure this project exists to prevent, and it cannot be seen in a
document.

An operation is judged indistinguishable when its response has the same status and the same XML
root element as the same path carrying no query at all. ListObjectsV2 legitimately answers that way
— ``?list-type=2`` selects the v2 form of what a bare bucket GET already means — so it is
acknowledged in the baseline like any other reviewed divergence, rather than special-cased here.
ListObjects is the bare form itself, so it carries no selector and is never asked.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

import httpx

from backlot import sigv4
from backlot.fidelity.findings import BREAKING, GAP, Finding
from backlot.fidelity.errors import FidelityError
from backlot.fidelity.fetch import fetch_json

_ROOT = re.compile(r"<\??[a-zA-Z]*[^>]*>\s*<([A-Za-z][\w.-]*)")
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()


class ProbeTarget(Protocol):
    """What this module needs of a source. Structural, so the registry can import this module
    without this module importing the registry."""

    spec_url: str

    def run(self, base_url: str, model: Mapping[str, Any], timeout: float) -> list[Finding]: ...


@dataclass(frozen=True)
class S3Operation:
    """One botocore operation, reduced to the request that selects it."""

    name: str
    method: str
    target: str  # "service" | "bucket" | "object"
    query: str

    def __str__(self) -> str:
        # The operation's own name leads, because a query literal does NOT identify one:
        # GetBucketAnalyticsConfiguration and ListBucketAnalyticsConfigurations are both
        # `?analytics`, and a baseline keyed on the request alone would silence whichever it saw
        # second.
        request = f"{self.method} /{{{self.target}}}" + (f"?{self.query}" if self.query else "")
        return f"{self.name}: {request}"


def _required_query_member(shapes: Mapping[str, Any], op: Mapping[str, Any]) -> str:
    """The querystring member that selects an operation whose ``requestUri`` carries no ``?``.

    ListParts is the case, and the only one: botocore gives it the same ``/{Bucket}/{Key+}`` as
    GetObject and selects it with a REQUIRED ``uploadId`` in the querystring. Read from the URI
    alone it comes out bare, is skipped as "the bare form", and never asked — while the server
    answers it with the object body, which is the silent fallthrough this module exists to catch.

    Required only. GetObject's ``versionId`` and ListObjects' ``prefix`` are optional refinements
    of the bare operation, not a different operation, and treating them as selectors would ask the
    same request twice under two names.
    """
    shape = shapes.get((op.get("input") or {}).get("shape")) or {}
    required = set(shape.get("required") or ())
    selectors = sorted(
        member.get("locationName") or name
        for name, member in (shape.get("members") or {}).items()
        if name in required and member.get("location") == "querystring"
    )
    return selectors[0] if selectors else ""


def operations(model: Mapping[str, Any]) -> list[S3Operation]:
    """The read operations a botocore S3 service model declares.

    Writes are not probed: Backlot serves reads, and a write it refuses is the correct answer
    rather than a divergence worth listing ninety times.
    """
    shapes = model.get("shapes") or {}
    out = []
    for name, op in (model.get("operations") or {}).items():
        http = op.get("http") or {}
        method = (http.get("method") or "").upper()
        if method not in ("GET", "HEAD"):
            continue
        uri = http.get("requestUri") or "/"
        path, _, query = urlsplit(uri).path, *uri.partition("?")[1:]
        query = query or _required_query_member(shapes, op)
        if "{Key" in path:
            target = "object"
        elif "{Bucket" in path:
            target = "bucket"
        else:
            target = "service"
        out.append(S3Operation(name, method, target, query))
    return sorted(out, key=lambda o: (o.target, o.query, o.name))


def _sign(method: str, url: str, access_key: str, secret: str, region: str = "us-east-1") -> dict:
    """A SigV4 Authorization header, built with the same module that verifies one."""
    parts = urlsplit(url)
    now = _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    headers = {"host": parts.netloc, "x-amz-date": amz_date, "x-amz-content-sha256": _EMPTY_SHA}
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    signature = sigv4.expected_signature(
        secret,
        method,
        parts.path,
        parts.query,
        headers,
        signed_headers,
        _EMPTY_SHA,
        amz_date,
        date_stamp,
        region,
    )
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


def _shape(response: httpx.Response) -> tuple[int, str]:
    """What a response looks like from the outside: its status and its XML root element."""
    match = _ROOT.match(response.text.lstrip())
    return response.status_code, match.group(1) if match else "(not xml)"


def probe(
    base_url: str,
    access_key: str,
    secret: str,
    ops: list[S3Operation],
    *,
    bucket: str,
    key: str,
    timeout: float = 20.0,
) -> list[Finding]:
    """Ask a running Backlot every read operation S3 declares, and classify what comes back."""
    targets = {"service": "/", "bucket": f"/{bucket}", "object": f"/{bucket}/{key}"}

    def call(method: str, path: str, query: str) -> httpx.Response:
        url = f"{base_url.rstrip('/')}/s3{path}" + (f"?{query}" if query else "")
        # A request that never got an answer is not an answer to classify. Left to propagate it
        # would leave the command exiting 1, which a scheduled run reads as a divergence.
        try:
            return httpx.request(
                method, url, headers=_sign(method, url, access_key, secret), timeout=timeout
            )
        except httpx.HTTPError as e:
            raise FidelityError(
                f"{method} {url} went unanswered, so nothing was probed: {e}"
            ) from e

    # What each path answers with nothing selecting an operation. Anything matching it is being
    # served by the catch-all rather than by an implementation of the operation asked for.
    fallthrough = {t: _shape(call("GET", p, "")) for t, p in targets.items()}

    out: list[Finding] = []
    for op in ops:
        if not op.query:  # the bare form IS the fallthrough; nothing to tell apart
            continue
        answer = _shape(call(op.method, targets[op.target], op.query))
        if answer[0] >= 400:
            out.append(
                Finding(
                    "missing_operation",
                    GAP,
                    str(op),
                    f"{op.name} is refused ({answer[0]}), which is an honest answer for an "
                    "operation Backlot does not serve",
                )
            )
        elif answer == fallthrough[op.target]:
            out.append(
                Finding(
                    "silent_fallthrough",
                    BREAKING,
                    str(op),
                    f"{op.name} is answered {answer[0]} <{answer[1]}>, which is what the same path "
                    f"answers with no operation selected: Backlot neither implements it nor "
                    f"refuses it, so a caller parses another operation's body",
                )
            )
    return sorted(out, key=lambda f: (f.severity != BREAKING, f.path, f.kind))


def run(base_url: str, model: Mapping[str, Any], timeout: float = 20.0) -> list[Finding]:
    """Probe a running Backlot for every read operation S3 declares.

    This is what ``ProbeComparison.run`` points at, and it holds everything S3-specific: which
    credentials the probe signs with, and which bucket and object it aims at. The dispatcher knows
    only that a probe source fetches a vendor model, gets a server, and returns findings — so a
    second probe source is a new module and one more entry in the registry, with nothing to change
    here or there.
    """
    # Everything below talks to the server this just started, and a server that cannot be asked is
    # the same class of outcome as a vendor that cannot be asked: no comparison ran, so `backlot
    # diff` must exit 2 rather than let the failure surface as a divergence a scheduled run files a
    # bug for.
    try:
        credentials = httpx.get(f"{base_url}/_meta/users", timeout=timeout).json()
        access_key = credentials["admin_s3_access_key_id"]
        secret = credentials["admin_s3_secret_access_key"]
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as e:
        raise FidelityError(
            f"{base_url}/_meta/users served no S3 credentials to probe with: {e}"
        ) from e

    def read(path: str, query: str = "") -> str:
        url = f"{base_url}/s3{path}" + (f"?{query}" if query else "")
        try:
            return httpx.get(
                url, headers=_sign("GET", url, access_key, secret), timeout=timeout
            ).text
        except httpx.HTTPError as e:
            raise FidelityError(f"{url} could not be read to aim the probe: {e}") from e

    # Discovered through the API being probed, rather than out of the corpus: a bucket the probe
    # cannot reach as a client is a bucket the probe cannot ask questions about either.
    buckets = re.findall(r"<Name>([^<]+)</Name>", read("/"))
    if not buckets:
        raise FidelityError("the corpus serves no S3 bucket, so there is nothing to probe")
    bucket = buckets[0]
    keys = re.findall(r"<Key>([^<]+)</Key>", read(f"/{bucket}", "list-type=2"))
    if not keys:
        raise FidelityError(f"bucket {bucket!r} holds no object, so the object probes cannot run")
    return probe(
        base_url, access_key, secret, operations(model), bucket=bucket, key=keys[0], timeout=timeout
    )


def divergences(source: "ProbeTarget", *, timeout: float = 120.0) -> list[Finding]:
    """This module's entry point: fetch the vendor model, start a server, and ask it.

    Fetching the model and starting the server happen here, which means they are shared by every
    probe: :class:`ProbeComparison` routes through this module, so a source needing a different
    corpus, a different port or no server at all would have to route somewhere else rather than
    only supply a different ``run``.
    """
    import backlot

    model = fetch_json(source.spec_url, timeout=timeout)
    with backlot.serve() as server:
        return source.run(server.base_url, model, timeout)
