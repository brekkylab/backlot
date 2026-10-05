"""S3: the two bucket listings, object reads, the XML shapes, and SigV4.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, unquote, urlencode

import pytest
import yaml
from starlette.requests import Request

from backlot import auth, sigv2, sigv4a, synth
from backlot.acl import Acl, Caller
from backlot.sigv4 import (
    canonical_request,
    expected_signature,
    is_skewed,
    parse_amz_date,
    parse_authorization,
    sign,
    split_credential,
)
from tests._helpers import client_for, complete, tiny_corpus

# ------------------------------------------------------------------------ S3 (SigV4/404/416 edges)


def _s3_signer(ak, sk):
    """botocore's S3 signer, which over http hashes the body into `x-amz-content-sha256` as boto3
    does, except where the request already names one: then it signs that value, as a client
    sending a trailer's keyword or a hash of its own does."""
    from botocore.auth import S3SigV4Auth
    from botocore.credentials import Credentials

    class _Signer(S3SigV4Auth):
        def _modify_request_before_signing(self, request):
            sent = request.headers.get("x-amz-content-sha256")
            super()._modify_request_before_signing(request)
            if sent is not None:
                del request.headers["x-amz-content-sha256"]
                request.headers["x-amz-content-sha256"] = sent

    return _Signer(Credentials(ak, sk), "s3", "us-east-1")


def _sign_get(base_url, path, token, *, tamper=False, extra_headers=None, method="GET", body=None):
    """Return (url, headers) for a SigV4-signed GET (or ``method``), using botocore (the real
    signer) over ``body``. The signature covers ``x-amz-date``, so it changes from one run to the
    next, and ``tamper`` swaps the signature's last hex digit for a different one: the header never
    verifies, whatever the signature ends in."""
    pytest.importorskip("botocore")
    from urllib.parse import parse_qsl, quote, urlencode

    from botocore.awsrequest import AWSRequest

    from backlot import synth

    # URL-encode the path: split on ? to preserve the path part, then properly encode query params.
    # Use quote_via=quote (not the default quote_plus) so a space becomes %20, matching the server's
    # canonicalization (backlot.sigv4._canonical_query uses quote); quote_plus would emit '+' and mismatch.
    if "?" in path:
        path_part, query_part = path.split("?", 1)
        params = parse_qsl(query_part, keep_blank_values=True)
        query_part = urlencode(params, safe="-_.~", quote_via=quote)
        path = f"{path_part}?{query_part}"

    ak = synth.s3_access_key_id(token)
    sk = synth.s3_secret_access_key(token)
    url = f"{base_url}{path}"
    req = AWSRequest(method=method, url=url, headers=dict(extra_headers or {}), data=body)
    _s3_signer(ak, sk).add_auth(req)
    headers = dict(req.headers)
    if tamper:
        signed = headers["Authorization"]
        headers["Authorization"] = signed[:-1] + ("1" if signed.endswith("0") else "0")
    return url, headers


def _signed(base_url, path, token, method="GET", extra_headers=None, body=None):
    """The response to a signed request, whatever its status — the refusals below are the subject,
    so an exception for a 4xx would hide them."""
    import httpx

    url, headers = _sign_get(
        base_url, path, token, method=method, extra_headers=extra_headers, body=body
    )
    return httpx.request(method, url, headers=headers, content=body)


# A payload hash naming a trailing checksum (``backlot.routers.s3._TRAILERS``).
_TRAILER = "STREAMING-UNSIGNED-PAYLOAD-TRAILER"
_TRAILER_SENT = {"x-amz-content-sha256": _TRAILER}

# The pair real puts on every answer, and the refusal it gives a method this router does not serve.
# The pair measured 2026-09-22 at ap-northeast-2 over twenty-five response shapes; the refusals
# 2026-09-23 against `s3.us-east-1.amazonaws.com`, the region this server presents, path-style,
# against a bucket name nobody owns.

# The id real's own answers carry, and the one its parse 400 carries: 16 of the 32 symbols it uses,
# and hex that never starts with `0` (`backlot.routers.s3.request_ids`,
# `backlot.errors.s3.method_not_allowed`).
_S3_ID = r"[0-9A-HJKMNP-TV-Z]{16}"
_PARSE_ID = r"[1-9A-F][0-9A-F]{0,15}"

_ID_ROWS = [
    ("GET", "/s3/", 200, _S3_ID),
    ("HEAD", "/s3/", 405, _S3_ID),
    ("HEAD", "/s3/eng-artifacts", 200, _S3_ID),
    ("GET", "/s3/eng-artifacts?list-type=2", 200, _S3_ID),
    ("GET", "/s3/eng-artifacts?location", 200, _S3_ID),
    ("GET", "/s3/eng-artifacts?uploads", 200, _S3_ID),
    ("GET", "/s3/no-such-bucket", 404, _S3_ID),
    ("GET", "/s3/eng-artifacts?versioning", 200, _S3_ID),
    ("GET", "/s3/eng-artifacts/runbooks/oncall.md?tagging", 200, _S3_ID),
    ("DELETE", "/s3/eng-artifacts", 501, _S3_ID),
    ("GET", "/s3/eng-artifacts?acl&versioning", 400, _S3_ID),
    ("PATCH", "/s3/eng-artifacts", 405, _S3_ID),
    ("TRACE", "/s3/eng-artifacts", 400, _PARSE_ID),
]


@pytest.mark.parametrize(
    "method, path, status, shape", _ID_ROWS, ids=[f"{r[0]}-{r[1]}" for r in _ID_ROWS]
)
def test_s3_every_answer_carries_the_request_id_pair_in_reals_shape(
    live_server, method, path, status, shape
):
    """Measured: real sends `x-amz-request-id` and `x-amz-id-2` on every response measured, a
    success and a refusal alike, and botocore reads both into `ResponseMetadata`. The id's shape is
    the answer's: S3's own and the parse 400's differ. The extended id's width varies from one real
    answer to the next, the parser's 400 included, so this server sends one width everywhere
    (`backlot.routers.s3.request_ids`)."""
    base_url, settings = live_server
    r = _signed(base_url, path, settings.admin_token, method=method)
    assert r.status_code == status
    assert re.fullmatch(shape, r.headers["x-amz-request-id"])
    assert len(r.headers["x-amz-id-2"]) == 96


def test_s3_a_request_id_uses_every_symbol_real_does_and_the_parse_400_is_unpadded_hex():
    """Over two hundred requests every one of real's 32 symbols turns up and no other, which a hex
    id never would, and the parse 400's id, never starting with `0`, is sometimes shorter than 16:
    the two shapes measured (`backlot.routers.s3.request_ids`,
    `backlot.errors.s3.method_not_allowed`)."""
    from backlot.errors import s3 as s3_errors
    from backlot.routers import s3 as s3_router

    pairs = [s3_router.request_ids("GET", f"/s3/bucket-{i}", "") for i in range(200)]
    assert {len(request_id) for request_id, _ in pairs} == {16}
    assert set("".join(request_id for request_id, _ in pairs)) == set(
        "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    )
    parse_ids = []
    for pair in pairs:
        token = s3_router.REQUEST_IDS.set(pair)
        try:
            refusal = s3_errors.method_not_allowed("/s3/bucket", "TRACE")
        finally:
            s3_router.REQUEST_IDS.reset(token)
        parse_ids.append(refusal.headers["x-amz-request-id"])
    assert all(re.fullmatch(_PARSE_ID, request_id) for request_id in parse_ids)
    assert min(len(request_id) for request_id in parse_ids) < 16


def test_s3_an_error_body_ends_with_the_pair_the_headers_carry(live_server):
    """Measured: an error body repeats the pair as its last two members, so a caller reading the
    body and a caller reading the headers report the same id for support."""
    base_url, settings = live_server
    r = _signed(base_url, "/s3/no-such-bucket", settings.admin_token)
    body = r.text
    assert body.rstrip().endswith(
        f"<RequestId>{r.headers['x-amz-request-id']}</RequestId>"
        f"<HostId>{r.headers['x-amz-id-2']}</HostId></Error>"
    )
    # A success has no `Error` document to repeat them in, and real puts them in no other body.
    served = _signed(base_url, "/s3/eng-artifacts?location", settings.admin_token)
    assert served.status_code == 200 and "<RequestId>" not in served.text


def test_s3_a_path_outside_the_mount_carries_no_request_id(live_server):
    """The pair rides what this server answers as S3, and `/s3x` is not one of those: a path that
    only starts with the same three characters is a 404 from the app, not an S3 answer."""
    import httpx

    base_url, _ = live_server
    r = httpx.get(f"{base_url}/s3x")
    assert r.status_code == 404 and "x-amz-request-id" not in r.headers


def test_s3_the_same_request_gets_the_same_pair_and_another_request_a_different_one(live_server):
    """The pair is seeded from the request, so a corpus served twice answers the same id, as its
    ETags and its synthesised ids already do."""
    base_url, settings = live_server
    first = _signed(base_url, "/s3/eng-artifacts?location", settings.admin_token)
    again = _signed(base_url, "/s3/eng-artifacts?location", settings.admin_token)
    other = _signed(base_url, "/s3/eng-artifacts?uploads", settings.admin_token)
    assert first.headers["x-amz-request-id"] == again.headers["x-amz-request-id"]
    assert first.headers["x-amz-id-2"] == again.headers["x-amz-id-2"]
    assert first.headers["x-amz-request-id"] != other.headers["x-amz-request-id"]


_KEY = "/s3/eng-artifacts/docs/runbook.md"

# fmt: off
_REFUSAL_ROWS = [
    # path, method, status, code, a member of the body, the Allow this server sends, and any
    # headers the request carries beyond the signature's
    ("/s3/eng-artifacts", "PATCH", 405, "MethodNotAllowed", "<ResourceType>BUCKET</", "GET, HEAD"),
    ("/s3/eng-artifacts", "POST", 412, "PreconditionFailed", "multipart/form-data</Condition>",
     None),
    ("/s3/eng-artifacts", "OPTIONS", 400, "BadRequest", "Origin request header needed.", None),
    (_KEY, "PATCH", 405, "MethodNotAllowed", "<ResourceType>OBJECT</", "GET, HEAD"),
    (_KEY, "POST", 405, "MethodNotAllowed", "<Method>POST</Method>", "GET, HEAD"),
    (_KEY, "OPTIONS", 400, "BadRequest", "Origin request header needed.", None),
    ("/s3/", "PATCH", 405, "MethodNotAllowed", "<ResourceType>SERVICE</", "GET"),
    ("/s3/", "PUT", 405, "MethodNotAllowed", "<ResourceType>SERVICE</", "GET"),
    ("/s3/", "OPTIONS", 400, "BadRequest", "Origin request header needed.", None),
    # An empty `Origin` is none on all three paths (``backlot.routers.s3._cors_preflight``).
    *[
        (path, "OPTIONS", 400, "BadRequest", "Origin request header needed.", None, {"Origin": ""})
        for path in ("/s3/eng-artifacts", _KEY, "/s3/")
    ],
    # A selector the method is not an operation of: the 405 names the selector's own type, and the
    # `Allow` is `GET` only where this server answers that selector on a GET.
    ("/s3/eng-artifacts?acl", "PATCH", 405, "MethodNotAllowed", "<ResourceType>ACL</", "GET"),
    ("/s3/eng-artifacts?location", "PUT", 405, "MethodNotAllowed", ">LOCATION</", "GET"),
    ("/s3/eng-artifacts?delete", "PUT", 405, "MethodNotAllowed", ">MULTI_OBJECT_DELETE</", None),
    ("/s3/eng-artifacts?versioning", "DELETE", 405, "MethodNotAllowed", ">VERSIONING</", "GET"),
    (f"{_KEY}?tagging", "POST", 405, "MethodNotAllowed", ">OBJECT_TAGGING</", "GET"),
    (f"{_KEY}?restore", "DELETE", 405, "MethodNotAllowed", ">RESTORE</", None),
    (f"{_KEY}?partNumber=1&uploadId=u", "PATCH", 405, "MethodNotAllowed", ">PART</", None),
    ("/s3/?acl", "PATCH", 405, "MethodNotAllowed", "<ResourceType>SERVICE</", "GET"),
    # A GET naming a selector whose operations are all on another method, before the bucket.
    ("/s3/eng-artifacts?delete", "GET", 405, "MethodNotAllowed", ">MULTI_OBJECT_DELETE</", None),
    ("/s3/eng-artifacts?restore", "GET", 405, "MethodNotAllowed", ">RESTORE</", None),
    ("/s3/no-such-bucket?delete", "GET", 405, "MethodNotAllowed", ">MULTI_OBJECT_DELETE</", None),
    # A bucket's three `metadata*Table` selectors, which only a `PUT` takes, on a GET, a POST, a
    # DELETE and a PATCH.
    *[
        (f"/s3/no-such-bucket?{selector}", method, 405, "MethodNotAllowed", f">{resource}</", None)
        for selector, resource in (
            ("metadataAnnotationTable", "BUCKET_METADATA_ANNOTATION_TABLE_CONFIGURATION"),
            ("metadataInventoryTable", "BUCKET_METADATA_INVENTORY_TABLE_CONFIGURATION"),
            ("metadataJournalTable", "BUCKET_METADATA_JOURNAL_TABLE_CONFIGURATION"),
        )
        for method in ("GET", "POST", "DELETE", "PATCH")
    ],
    ("/s3/eng-artifacts?metadataJournalTable=v", "GET", 405, "MethodNotAllowed",
     ">BUCKET_METADATA_JOURNAL_TABLE_CONFIGURATION</", None),
    (f"{_KEY}?delete", "GET", 405, "MethodNotAllowed", ">MULTI_OBJECT_DELETE</", None),
    (f"{_KEY}?encryption", "GET", 405, "MethodNotAllowed", ">OBJECT_ENCRYPTION</", None),
    (f"{_KEY}?restore", "GET", 405, "MethodNotAllowed", ">RESTORE</", None),
    (f"{_KEY}?select", "GET", 405, "MethodNotAllowed", "<Method>GET</Method><ResourceType>SELECT</",
     None),
    ("/s3/eng-artifacts?acl&delete", "GET", 400, "InvalidArgument", "parameters: acl, delete",
     None),
    (f"{_KEY}?uploads&acl", "GET", 400, "InvalidArgument", "parameters: acl, uploads", None),
    # `partNumber` at a bucket's path, on any method, and beside `uploadId` or a selector.
    ("/s3/eng-artifacts?partNumber=1", "PATCH", 400, "InvalidRequest", "valid key name.", None),
    ("/s3/no-such-bucket?partNumber=abc", "DELETE", 400, "InvalidRequest", "valid key name.", None),
    ("/s3/eng-artifacts?partNumber=1&uploadId=u", "GET", 405, "MethodNotAllowed", ">PART</", None),
    ("/s3/eng-artifacts?partNumber=1&acl", "GET", 400, "InvalidArgument", "acl, partNumber", None),
    (f"{_KEY}?partNumber=1&uploadId=u", "GET", 405, "MethodNotAllowed", ">PART</", None),
    # The write methods of the selectors that rows above refuse on a GET.
    ("/s3/eng-artifacts?restore", "DELETE", 405, "MethodNotAllowed", ">RESTORE</", None),
    (f"{_KEY}?delete", "PUT", 405, "MethodNotAllowed", ">MULTI_OBJECT_DELETE</", None),
    (f"{_KEY}?encryption", "POST", 405, "MethodNotAllowed", ">OBJECT_ENCRYPTION</", None),
    # A bucket's `torrent` and `uploadId`, and a bucket's selectors at a key, on the write methods.
    ("/s3/eng-artifacts?torrent", "POST", 405, "MethodNotAllowed", ">TORRENT</", None),
    ("/s3/eng-artifacts?uploadId=x", "PATCH", 405, "MethodNotAllowed", ">UPLOAD</", None),
    (f"{_KEY}?versioning", "PATCH", 405, "MethodNotAllowed", ">VERSIONING</", "GET"),
    (f"{_KEY}?location", "PUT", 405, "MethodNotAllowed", ">LOCATION</", "GET"),
    (f"{_KEY}?cors&acl", "PUT", 400, "InvalidArgument", "parameters: acl, cors", None),
    ("/s3/eng-artifacts?acl&versioning", "PATCH", 400, "InvalidArgument",
     "Conflicting query string parameters: acl, versioning", None),
]
# fmt: on


@pytest.mark.parametrize(
    "path, method, status, code, member, allow, headers",
    [(*r, {}) if len(r) == 6 else r for r in _REFUSAL_ROWS],
    ids=[
        f"{r[1]}-{r[0].rsplit('/', 1)[-1] or 'root'}" + ("-" + "-".join(r[6]) if r[6:] else "")
        for r in _REFUSAL_ROWS
    ],
)
def test_s3_a_method_this_router_does_not_serve_answers_reals_own_refusal(
    live_server, path, method, status, code, member, allow, headers
):
    """Each row measured. The body is XML on every one, and the `Allow` names what this server
    serves rather than real's own methods, which is the line the sub-resource 405 already draws."""
    base_url, settings = live_server
    r = _signed(base_url, path, settings.admin_token, method=method, extra_headers=headers or None)
    assert r.status_code == status
    assert r.headers["content-type"] == "application/xml"
    assert f"<Code>{code}</Code>" in r.text and member in r.text
    assert r.headers.get("allow") == allow


@pytest.mark.parametrize("path", ["/s3", "/s3/", "/s3/?acl"])
def test_s3_a_head_at_the_service_root_is_the_405_without_its_body(live_server, path):
    """Measured 2026-09-23, signed and unsigned, bare and with `?acl` and `?versioning`: real
    answers a `HEAD` at the service root 405 with `Allow: GET` and an empty `application/xml` body,
    not the parse 400 a method S3 defines nothing for gets, and sends it chunked
    (``backlot.routers.s3._head``). The GET on the same path is the listing, so the refusal is the
    method's."""
    import httpx

    base_url, settings = live_server
    for r in (
        _signed(base_url, path, settings.admin_token, method="HEAD"),
        httpx.head(f"{base_url}{path}"),
    ):
        assert r.status_code == 405
        assert r.headers["allow"] == "GET"
        assert r.headers["content-type"] == "application/xml"
        assert r.content == b""
        assert "content-length" not in r.headers and r.headers["transfer-encoding"] == "chunked"
    assert _signed(base_url, path, settings.admin_token).status_code == 200


def test_s3_every_bucket_selector_a_get_answers_is_declared():
    """What a bucket's GET answers is what its OpenAPI declares, so the tool `backlot mcp` hands an
    agent offers each operation the route serves (``backlot.openapi.qp``)."""
    from backlot.main import app
    from backlot.routers import s3 as s3_router

    declared = {p["name"] for p in app.openapi()["paths"]["/s3/{bucket}"]["get"]["parameters"]}
    assert s3_router._BUCKET_GETS <= declared
    assert {"key-marker", "version-id-marker", "id"} <= declared


def test_s3_every_selector_a_get_reads_has_an_answer_for_the_other_methods():
    """A selector added to what a GET reads without a row in the write tables would reach the bare
    path's refusal on `PUT`, `POST`, `DELETE` and `PATCH`, which is the answer real gives no
    selector."""
    from backlot.routers import s3 as s3_router

    assert s3_router._BUCKET_READ_SELECTORS <= set(s3_router._BUCKET_WRITE_SELECTORS)
    assert s3_router._OBJECT_READ_SELECTORS <= set(s3_router._OBJECT_PATH_WRITE_SELECTORS)


# A DeleteObjects body real's schema takes, sent with its `Content-MD5`, so the write is what is
# left to refuse (``backlot.routers.s3._delete_objects_refusal``).
_DELETE_BODY = b"<Delete><Object><Key>runbooks/oncall.md</Key></Object></Delete>"

_WRITE_ROWS = [
    ("/s3/eng-artifacts", "DELETE", None),
    ("/s3/eng-artifacts", "PUT", None),
    ("/s3/eng-artifacts", "PUT", b"<CreateBucketConfiguration/>"),
    ("/s3/eng-artifacts?delete", "POST", _DELETE_BODY),
    (f"{_KEY}?delete", "POST", _DELETE_BODY),
    ("/s3/eng-artifacts?acl", "PUT", None),
    ("/s3/eng-artifacts?cors", "DELETE", None),
    (_KEY, "DELETE", None),
    (_KEY, "PUT", b"new bytes"),
    (f"{_KEY}?uploads", "POST", None),
    (f"{_KEY}?partNumber=1&uploadId=u", "PUT", b"part"),
    (f"{_KEY}?uploadId=u", "DELETE", None),
    (f"{_KEY}?tagging", "DELETE", None),
    (f"{_KEY}?cors", "PUT", None),
]


@pytest.mark.parametrize(
    "path, method, body",
    _WRITE_ROWS,
    ids=[f"{r[1]}-{r[0].rsplit('/', 1)[-1]}{'-body' if r[2] else ''}" for r in _WRITE_ROWS],
)
def test_s3_a_write_is_refused_as_not_implemented(live_server, path, method, body):
    """Real answers each of these by doing the write, a selector's own method among them
    (`POST ?delete` is DeleteObjects, a key's `POST ?uploads` the CreateMultipartUpload boto3's
    `upload_file` sends). This server serves a corpus it does not change, so they get the code it
    already gives an operation it does not implement rather than a status that claims the write
    happened."""
    base_url, settings = live_server
    md5 = {"Content-MD5": base64.b64encode(hashlib.md5(body).digest()).decode()} if body else None
    r = _signed(base_url, path, settings.admin_token, method=method, body=body, extra_headers=md5)
    assert r.status_code == 501
    assert "<Code>NotImplemented</Code>" in r.text


@pytest.mark.parametrize(
    "path, message",
    [
        ("/s3/eng-artifacts", "CORS is not enabled for this bucket."),
        (_KEY, "CORS is not enabled for this bucket."),
        ("/s3/", "Bucket not found"),
    ],
)
@pytest.mark.parametrize("asked", [None, "", "GET", "DELETE"])
def test_s3_an_options_carrying_an_origin_answers_the_cors_refusal(
    live_server, path, message, asked
):
    """Measured: with an `Origin` the answer is a 403 whose message says which way the CORS lookup
    failed, whose `ResourceType` is `BUCKET` on all three paths, the service root included, and
    whose `Method` is the method the preflight asks about, or `OPTIONS` when it names none or an
    empty one (2026-09-30)."""
    base_url, settings = live_server
    headers = {"Origin": "https://example.invalid"}
    if asked is not None:
        headers["Access-Control-Request-Method"] = asked
    r = _signed(base_url, path, settings.admin_token, method="OPTIONS", extra_headers=headers)
    assert r.status_code == 403
    assert "<Code>AccessForbidden</Code>" in r.text and message in r.text
    assert f"<Method>{asked or 'OPTIONS'}</Method><ResourceType>BUCKET</ResourceType>" in r.text


@pytest.mark.parametrize("asked", ["put", "Get", "TRACE"])
def test_s3_a_preflight_asking_about_a_method_real_does_not_accept_is_a_400(live_server, asked):
    """Measured: real's preflight takes `GET`, `HEAD`, `POST`, `PUT`, `DELETE`, `PATCH` and
    `OPTIONS` as written, and answers any other value, a lowercase one included, with a 400 naming
    it. The same preflight asking about `PUT` is the 403, so the value is what is refused."""
    base_url, settings = live_server
    headers = {"Origin": "https://example.invalid", "Access-Control-Request-Method": asked}
    r = _signed(base_url, _KEY, settings.admin_token, method="OPTIONS", extra_headers=headers)
    assert r.status_code == 400
    assert f"<Message>Invalid Access-Control-Request-Method: {asked}</Message>" in r.text
    headers["Access-Control-Request-Method"] = "PUT"
    control = _signed(base_url, _KEY, settings.admin_token, method="OPTIONS", extra_headers=headers)
    assert control.status_code == 403


@pytest.mark.parametrize("method", ["TRACE", "LINK", "PROPFIND"])
@pytest.mark.parametrize("path", ["/s3/", "/s3/eng-artifacts", "/s3/eng-artifacts/docs/runbook.md"])
def test_s3_a_method_s3_defines_nothing_for_is_the_parse_400(live_server, method, path):
    """Measured 2026-09-22 with `TRACE`, `LINK` and `PROPFIND` at the service root, and 2026-09-23
    with `TRACE` on all three paths, `LINK` on a bucket and `PROPFIND` on a key: real answers each
    the same 400 `BadRequest`, `application/xml`, with no `Allow`. No route can be declared for a
    method that is not named, so `backlot.errors.s3` answers these."""
    import httpx

    base_url, _ = live_server
    r = httpx.request(method, f"{base_url}{path}")
    assert r.status_code == 400
    assert r.headers["content-type"] == "application/xml"
    assert "<Code>BadRequest</Code>" in r.text
    assert "parsing the HTTP request." in r.text
    assert "allow" not in r.headers
    assert r.text.rstrip().endswith(
        f"<RequestId>{r.headers['x-amz-request-id']}</RequestId>"
        f"<HostId>{r.headers['x-amz-id-2']}</HostId></Error>"
    )


def test_s3_a_write_names_its_bucket_before_the_501_and_createbucket_names_none(live_server):
    """Measured: real answers a write naming an absent bucket — a `DELETE`, a selector's own method
    such as `POST ?delete` or `PUT ?acl`, a key's `POST ?uploads` — with `NoSuchBucket` at 404,
    where its method refusals answer an absent bucket exactly as they answer a present one. A bare
    bucket `PUT` is CreateBucket, which real answered with the bucket for a free name and with
    `BucketAlreadyExists` for a taken one, so its 501 does not depend on the name."""
    base_url, settings = live_server
    absent = "/s3/no-such-bucket-xyz"
    for method, path in (
        ("DELETE", absent),
        ("DELETE", f"{absent}/a/b.txt"),
        ("POST", f"{absent}?delete"),
        ("PUT", f"{absent}?acl"),
        ("POST", f"{absent}/a/b.txt?uploads"),
    ):
        r = _signed(base_url, path, settings.admin_token, method=method)
        assert r.status_code == 404 and "<Code>NoSuchBucket</Code>" in r.text, (method, path)
    for method, path, status in (
        ("PATCH", absent, 405),
        ("POST", absent, 412),
        ("PATCH", f"{absent}?acl", 405),
        ("PUT", absent, 501),
    ):
        r = _signed(base_url, path, settings.admin_token, method=method)
        assert r.status_code == status, (method, path, r.text)
    present = _signed(base_url, "/s3/eng-artifacts", settings.admin_token, method="DELETE")
    assert present.status_code == 501 and "<Code>NotImplemented</Code>" in present.text


def test_s3_a_request_in_a_bucket_the_caller_cannot_see_is_nosuchbucket(live_server):
    """A write, a read of a key and a read of the bucket's configuration all look the bucket up
    first, so each is scoped as a listing is: `people-vault` holds one group-visible object, so an
    engineer is told the bucket does not exist where the admin gets the write's own answer, the
    object itself and the configuration. CreateBucket reads no bucket and a method refusal comes
    first, so both answer the two callers alike."""
    base_url, settings = live_server
    tokens = {
        u["email"]: u["token"] for u in yaml.safe_load(settings.tokens_path.read_text())["users"]
    }
    scoped_token = tokens["ava@acme.com"]
    for method, path, admin_status, *extra in (
        ("DELETE", "/s3/people-vault", 501),
        ("POST", "/s3/people-vault?delete", 400),
        ("POST", "/s3/people-vault?restore", 400),
        # A write's checks come after the bucket, so the scoped caller is told there is none
        # whatever the request carries, where the admin gets the check's 400.
        ("POST", "/s3/people-vault/comp/bands.csv?delete", 400, {"Content-MD5": "garbage"}),
        ("PUT", "/s3/people-vault/comp/bands.csv?encryption", 400, {"Content-MD5": "garbage"}),
        ("GET", "/s3/people-vault?versioning", 200),
        ("GET", "/s3/people-vault?acl", 200),
        ("GET", "/s3/people-vault?versions", 200),
        ("GET", "/s3/people-vault/comp/bands.csv?cors", 404),
        ("PUT", "/s3/people-vault/x.txt", 501),
        ("DELETE", "/s3/people-vault/x.txt?tagging", 501),
        ("GET", "/s3/people-vault/comp/bands.csv", 200),
        ("HEAD", "/s3/people-vault/comp/bands.csv", 200),
        ("HEAD", "/s3/people-vault", 200),
    ):
        headers = extra[0] if extra else None
        scoped = _signed(base_url, path, scoped_token, method=method, extra_headers=headers)
        admin = _signed(base_url, path, settings.admin_token, method=method, extra_headers=headers)
        assert scoped.status_code == 404, path
        if method != "HEAD":
            assert "<BucketName>people-vault</BucketName>" in scoped.text, path
        assert admin.status_code == admin_status, path
        # HeadBucket's ARN rides its 200 alone, so a caller who cannot see the bucket gets none.
        assert "x-amz-bucket-arn" not in scoped.headers, path
    # The same caller reads a bucket it can see.
    visible = _signed(base_url, "/s3/eng-artifacts/runbooks/oncall.md", scoped_token)
    assert visible.status_code == 200
    for method, status in (("PUT", 501), ("PATCH", 405)):
        scoped = _signed(base_url, "/s3/people-vault", scoped_token, method=method)
        admin = _signed(base_url, "/s3/people-vault", settings.admin_token, method=method)
        assert scoped.status_code == admin.status_code == status, method


@pytest.mark.parametrize(
    "method, path, write",
    [
        ("PATCH", "/s3/eng-artifacts", "DELETE"),
        ("PATCH", _KEY, "DELETE"),
        ("PATCH", "/s3/", None),
        ("PATCH", "/s3/eng-artifacts?acl", "PUT"),
        ("POST", "/s3/eng-artifacts?tagging", "DELETE"),
        ("OPTIONS", _KEY, "DELETE"),
        ("GET", "/s3/eng-artifacts?delete", "POST"),
        ("GET", f"{_KEY}?encryption", "PUT"),
        ("GET", "/s3/eng-artifacts?metadataAnnotationTable", "PUT"),
        ("GET", "/s3/eng-artifacts?acl&versioning", None),
        ("HEAD", "/s3/eng-artifacts?delete", "POST"),
        ("HEAD", "/s3/eng-artifacts?acl", "PUT"),
        ("HEAD", f"{_KEY}?select", "POST"),
        ("HEAD", f"{_KEY}?acl&tagging", None),
    ],
)
def test_s3_the_method_is_refused_before_the_credential(live_server, method, path, write):
    """The order ``backlot.routers.s3._method_refusal`` and ``backlot.routers.s3._read_refusal``
    keep: an unsigned request answers each of these as a signed one does, the method and a selector
    a GET or a HEAD cannot take coming ahead of the credential. The control is the same path
    unsigned under the method that writes there, which resolves the caller and the bucket first and
    so names a bucket the anonymous caller cannot see; the service root and a conflict have no such
    method."""
    import httpx

    base_url, settings = live_server
    unsigned = httpx.request(method, f"{base_url}{path}")
    signed = _signed(base_url, path, settings.admin_token, method=method)
    assert unsigned.status_code == signed.status_code != 403
    if method != "HEAD":
        code = re.search(r"<Code>([^<]+)</Code>", signed.text)[1]
        assert f"<Code>{code}</Code>" in unsigned.text
    if write:
        refused = httpx.request(write, f"{base_url}{path}")
        assert refused.status_code == 404, write
        assert "<Code>NoSuchBucket</Code>" in refused.text


# A credential refusal as it goes out, status, code, message and members: the headers of each row
# built when it runs, so the date is the clock's (measured 2026-09-29).
# fmt: off
_WIRE_REFUSAL_ROWS = [
    ("unknown key",
     lambda now: {"authorization": _v4("AKIABOGUS0000000BOGUS", now), "x-amz-date": now}, 403,
     "InvalidAccessKeyId", "The AWS Access Key Id you provided does not exist in our records.",
     "<AWSAccessKeyId>AKIABOGUS0000000BOGUS</AWSAccessKeyId>"),
    (
        "a date past 9999",
        lambda now: {
            "authorization": "AWS AKIABOGUS0000000BOGUS:abc",
            "date": "Tue, 29 Sep 10000 16:00:00 GMT",
        },
        500,
        "InternalError",
        "We encountered an internal error. Please try again.",
        "",
    ),
    (
        "a region set without the region",
        lambda now: {
            "authorization": _v4a("AKIABOGUS0000000BOGUS", now),
            "x-amz-date": now,
            "x-amz-region-set": "us-west-2",
        },
        400,
        "RegionSetMismatch",
        "The provided X-Amz-Region-Set doesn't match against the requested S3 region.",
        "<RequestedRegion>us-east-1</RequestedRegion><RegionSet>us-west-2</RegionSet>",
    ),
]
# fmt: on


@pytest.mark.parametrize(
    "headers, status, code, message, members",
    [r[1:] for r in _WIRE_REFUSAL_ROWS],
    ids=[r[0] for r in _WIRE_REFUSAL_ROWS],
)
def test_s3_a_credential_refusal_goes_out_with_reals_status(
    live_server, headers, status, code, message, members
):
    import httpx

    base_url, _ = live_server
    now = datetime.now(timezone.utc).strftime(AMZ_DATE_FORMAT)
    r = httpx.get(
        f"{base_url}/s3/eng-artifacts?list-type=2",
        headers={"x-amz-content-sha256": "UNSIGNED-PAYLOAD", **headers(now)},
    )
    assert r.status_code == status
    named = re.search(r"<Code>([^<]+)</Code><Message>([^<]*)</Message>(.*)<RequestId>", r.text)
    assert (named[1], named[2], named[3]) == (code, message, members)


# What a signature that does not verify gets beside each kind of refusal: its 403 ahead of the 405s
# and of what the listing judges after the bucket, and after the conflict, a bucket's
# `partNumber`, the listing's own parses, ListParts' and the refusals real's CORS front end and
# its form-upload check give (all measured 2026-09-29, over a bad secret and an unknown key).
_TAMPERED_ROWS = [
    ("GET", "/s3/eng-artifacts?list-type=2", 403),
    ("HEAD", "/s3/eng-artifacts", 403),
    ("GET", "/s3/eng-artifacts?delete", 403),
    ("HEAD", "/s3/eng-artifacts?delete", 403),
    ("GET", f"{_KEY}?restore", 403),
    ("HEAD", f"{_KEY}?acl", 403),
    ("GET", "/s3/eng-artifacts?encoding-type=bogus", 403),
    ("PATCH", "/s3/eng-artifacts", 403),
    ("PATCH", _KEY, 403),
    ("POST", _KEY, 403),
    ("PATCH", "/s3/", 403),
    ("PUT", "/s3/", 403),
    ("HEAD", "/s3/", 403),
    ("PATCH", "/s3/eng-artifacts?acl", 403),
    ("PUT", "/s3/eng-artifacts?delete", 403),
    ("GET", "/s3/eng-artifacts?acl&versioning", 400),
    ("HEAD", f"{_KEY}?acl&tagging", 400),
    ("PATCH", "/s3/eng-artifacts?acl&versioning", 400),
    ("GET", "/s3/eng-artifacts?partNumber=1", 400),
    ("GET", "/s3/eng-artifacts?max-keys=abc", 400),
    ("HEAD", "/s3/eng-artifacts?max-keys=abc", 400),
    ("GET", "/s3/eng-artifacts?start-after=x", 400),
    ("GET", "/s3/eng-artifacts?uploads&max-uploads=abc", 400),
    ("GET", f"{_KEY}?uploadId=x&max-parts=abc", 400),
    ("GET", f"{_KEY}?uploadId=x&part-number-marker=abc", 400),
    ("GET", "/s3/eng-artifacts?torrent", 403),
    ("GET", "/s3/eng-artifacts?metadataInventoryTable", 403),
    ("GET", f"{_KEY}?partNumber=abc", 403),
    ("GET", f"{_KEY}?partNumber=0", 403),
    ("HEAD", f"{_KEY}?versionId=", 400),
    ("POST", "/s3/eng-artifacts", 412),
    ("OPTIONS", "/s3/eng-artifacts", 400),
]


@pytest.mark.parametrize(
    "method, path, status", _TAMPERED_ROWS, ids=[f"{m}-{p[4:]}" for m, p, _ in _TAMPERED_ROWS]
)
def test_s3_tampered_signature_rejected(live_server, method, path, status):
    """Each row measured. Where the answer is the 403 it is the signature's own refusal, and the
    same request signed as sent is not a 403 (`backlot.routers.s3._signature_refusal`)."""
    import httpx

    base_url, settings = live_server
    url, headers = _sign_get(base_url, path, settings.admin_token, tamper=True, method=method)
    r = httpx.request(method, url, headers=headers)
    assert r.status_code == status
    if status == 403:
        if method != "HEAD":
            assert "<Code>SignatureDoesNotMatch</Code>" in r.text
        assert _signed(base_url, path, settings.admin_token, method=method).status_code != 403


@pytest.mark.parametrize("last", "0123456789abcdef")
def test_s3_a_tampered_signature_is_never_the_one_botocore_signed(monkeypatch, last):
    """Against a signature ending in each hex digit in turn, ``dead`` among them, the tamper in
    `_sign_get` sends 64 hex digits that are not that signature."""
    from botocore.auth import SigV4Auth

    signature = "0" * 60 + "dea" + last
    monkeypatch.setattr(SigV4Auth, "signature", lambda self, string_to_sign, request: signature)
    _, headers = _sign_get("http://127.0.0.1:8000", _KEY, TOKEN, tamper=True)
    sent = headers["Authorization"].rsplit("Signature=", 1)[1]
    assert sent != signature and re.fullmatch(r"[0-9a-f]{64}", sent)


# Each row is real's answer for a name nobody owns (2026-09-29), which every corpus bucket gets here
# too, an unsigned request seeing no bucket (``backlot.routers.s3._auth``).
# fmt: off
_ANONYMOUS_ROWS = [
    ("GET", "/s3/eng-artifacts?list-type=2", 404, "NoSuchBucket"),
    ("GET", "/s3/eng-artifacts?location", 404, "NoSuchBucket"),
    ("GET", "/s3/eng-artifacts/runbooks/oncall.md", 404, "NoSuchBucket"),
    ("GET", "/s3/eng-artifacts/runbooks/oncall.md?uploadId=x", 404, "NoSuchBucket"),
    ("DELETE", "/s3/eng-artifacts", 404, "NoSuchBucket"),
    ("GET", "/s3/eng-artifacts?list-type=2&max-keys=abc", 400, "InvalidArgument"),
    ("GET", "/s3/eng-artifacts?start-after=x", 400, "InvalidArgument"),
    ("PUT", "/s3/eng-artifacts", 403, "AccessDenied"),
    # An `X-Amz-Signature` without `X-Amz-Algorithm` and an `AWSAccessKeyId` without `Signature`
    # carry no credential (``backlot.auth.resolve_sigv4``).
    ("GET", "/s3/eng-artifacts?list-type=2&X-Amz-Signature=00", 404, "NoSuchBucket"),
    # The admin's own key, which the test puts in for `{admin_key}`.
    ("GET", "/s3/eng-artifacts?list-type=2&AWSAccessKeyId={admin_key}", 404, "NoSuchBucket"),
    ("GET", "/s3/eng-artifacts?list-type=2&Expires=9999999999&AWSAccessKeyId=x", 404,
     "NoSuchBucket"),
    # Nor does an empty `Authorization` with no credential in the query
    # (``backlot.auth.resolve_sigv4``).
    *[
        ("GET", f"/s3/eng-artifacts?list-type=2{query}", 404, "NoSuchBucket", {"Authorization": ""})
        for query in ("", "&X-Amz-Signature=00", "&AWSAccessKeyId=x")
    ],
    # ListBuckets, which real answers with a 307 to the product page and no body.
    ("GET", "/s3/", 307, None),
]
# fmt: on


@pytest.mark.parametrize(
    "method, path, status, code, headers",
    [(*r, {}) if len(r) == 4 else r for r in _ANONYMOUS_ROWS],
    ids=[f"{r[0]}-{r[1][4:]}" + ("-empty-authorization" if r[4:] else "") for r in _ANONYMOUS_ROWS],
)
def test_s3_an_unsigned_request_is_an_anonymous_caller_who_sees_no_bucket(
    live_server, method, path, status, code, headers
):
    """Every bucket gets the answer for one the caller cannot see (``backlot.routers.s3._auth``).
    The listing's parses come first, as on real. CreateBucket names no bucket, and real's refusal of
    an anonymous one is its own, and ListBuckets is the 307 ``backlot.routers.s3.list_buckets``
    describes. The admin, signed, reads the same bucket and the same list."""
    import httpx

    base_url, settings = live_server
    path = path.replace("{admin_key}", synth.s3_access_key_id(settings.admin_token))
    r = httpx.request(method, f"{base_url}{path}", headers=headers)
    assert r.status_code == status
    if code is None:
        assert r.headers["location"] == "https://aws.amazon.com/s3/" and r.content == b""
        assert _signed(base_url, path, settings.admin_token).status_code == 200
        return
    assert f"<Code>{code}</Code>" in r.text
    if code == "NoSuchBucket":
        assert "<BucketName>eng-artifacts</BucketName>" in r.text
    if code == "AccessDenied":
        assert "Anonymous users cannot invoke this API. Please authenticate." in r.text
    signed = _signed(base_url, "/s3/eng-artifacts?list-type=2", settings.admin_token)
    assert signed.status_code == 200


def test_s3_a_signature_mismatch_names_what_this_server_signed(live_server):
    """Real names the access key, the string it signed, the signature sent and the canonical
    request, the two strings as bytes too, in that order (measured 2026-09-29). Here they are what
    this server signed, which for a request signed by botocore is botocore's own canonical
    request."""
    import httpx
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    base_url, settings = live_server
    token = settings.admin_token
    access_key = synth.s3_access_key_id(token)
    url = f"{base_url}/s3/eng-artifacts?list-type=2&prefix=run%20books"
    req = AWSRequest(method="GET", url=url, headers={"x-amz-content-sha256": "UNSIGNED-PAYLOAD"})
    signer = S3SigV4Auth(
        Credentials(access_key, synth.s3_secret_access_key(token) + "x"), "s3", "us-east-1"
    )
    signer.add_auth(req)
    r = httpx.get(url, headers=dict(req.headers))
    assert r.status_code == 403
    root = ET.fromstring(r.content)
    assert [child.tag for child in root] == [
        "Code",
        "Message",
        "AWSAccessKeyId",
        "StringToSign",
        "SignatureProvided",
        "StringToSignBytes",
        "CanonicalRequest",
        "CanonicalRequestBytes",
        "RequestId",
        "HostId",
    ]
    members = {child.tag: child.text for child in root}
    assert members["AWSAccessKeyId"] == access_key
    # What botocore signed is the request as it stood before its own `Authorization` was added.
    as_signed = copy.deepcopy(req)
    del as_signed.headers["Authorization"]
    assert members["CanonicalRequest"] == signer.canonical_request(as_signed)
    assert members["StringToSign"] == signer.string_to_sign(req, members["CanonicalRequest"])
    assert members["SignatureProvided"] == req.headers["Authorization"].rsplit("=", 1)[1]
    for text, as_bytes in (
        ("StringToSign", "StringToSignBytes"),
        ("CanonicalRequest", "CanonicalRequestBytes"),
    ):
        assert members[as_bytes] == " ".join(f"{b:02x}" for b in members[text].encode())


def test_s3_key_containing_a_question_mark_verifies(live_server):
    """`?` is a legal character in a key, and a client sends it as `%3F`. Starlette rebuilds
    `request.url` from the DECODED path, so for `/q%3Fx.txt` its `.query` is `x.txt` — a query the
    client never sent or signed. The verifier canonicalises the wire query string instead, and an
    absent key with a `?` in it answers NoSuchKey like any absent key in a bucket that exists, not
    SignatureDoesNotMatch. Signed by botocore, the way boto3 sends it."""
    import urllib.request

    base_url, settings = live_server
    url, headers = _sign_get(base_url, "/s3/eng-artifacts/q%3Fx.txt", settings.admin_token)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url, headers=headers))
    assert e.value.code == 404 and b"NoSuchKey" in e.value.read()


def test_s3_unsatisfiable_range_is_416(live_server):
    import urllib.request

    base_url, settings = live_server
    url, headers = _sign_get(
        base_url,
        "/s3/eng-artifacts/runbooks/oncall.md",
        settings.admin_token,
        extra_headers={"Range": "bytes=99999-100000"},
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url, headers=headers))
    body = e.value.read()
    total = len("Check dashboards, roll back, page on-call.")
    assert e.value.code == 416 and b"<Code>InvalidRange</Code>" in body
    # The range as sent and the object's size, which is what real names this refusal with.
    members = f"<RangeRequested>bytes=99999-100000</RangeRequested><ActualObjectSize>{total}"
    assert f"</Message>{members}</ActualObjectSize><RequestId>".encode() in body
    # The size is named in the body alone, with no `Content-Range`
    # (``backlot.routers.s3.object_get``).
    assert e.value.headers.get("Content-Range") is None
    assert e.value.headers.get("Content-Type") == "application/xml"


# ---------------------------------------------------- S3 large-bucket perf (SQL-pushed listing)


def _s3_big_corpus(n=3000):
    """~3000 objects in one bucket: 12 month-prefixes x 25 day-prefixes, split 50/50 across two
    ACL groups so month-01 alone (250 objects, still nested by day) exercises prefix filtering,
    keyset pagination, delimiter rollup, and ACL scoping all at once — without needing to touch
    (or slow down) the shared SAMPLE corpus every other test in this module depends on."""
    for i in range(n):
        month = (i % 12) + 1
        day = ((i // 12) % 25) + 1
        key = f"logs/2026/{month:02d}/{day:02d}/obj-{i:05d}.json"
        group = "engineering" if (i // 12) % 2 == 0 else "people"
        author = "eng-bulk@acme.com" if group == "engineering" else "people-bulk@acme.com"
        yield {
            "source_type": "s3",
            "doc_id": f"s3-big-{i:05d}",
            "bucket": "big-bucket",
            "group": group,
            "key": key,
            "title": key,
            "content": f"payload-{i}",
            "author_email": author,
            "author_groups": [group],
            "visibility": "group",
        }
    # A second, dedicated bucket for the CommonPrefixes-straddling regression (Fix 3): one
    # "folder" (150 objects) bigger than a max-keys=100 page, plus a small trailing folder — the
    # exact shape that made a rolled-up CommonPrefixes group straddle a page cutoff and get
    # emitted twice before the fix.
    for i in range(150):
        key = f"grp/big/f-{i:04d}.json"
        yield {
            "source_type": "s3",
            "doc_id": f"s3-straddle-big-{i:04d}",
            "bucket": "straddle-bucket",
            "group": "engineering",
            "key": key,
            "title": key,
            "content": f"big-payload-{i}",
            "author_email": "eng-bulk@acme.com",
            "author_groups": ["engineering"],
            "visibility": "public",
        }
    for i in range(5):
        key = f"grp/small/f-{i:02d}.json"
        yield {
            "source_type": "s3",
            "doc_id": f"s3-straddle-small-{i:02d}",
            "bucket": "straddle-bucket",
            "group": "engineering",
            "key": key,
            "title": key,
            "content": f"small-payload-{i}",
            "author_email": "eng-bulk@acme.com",
            "author_groups": ["engineering"],
            "visibility": "public",
        }

    # A third, dedicated bucket for what `encoding-type=url` is for: keys holding a space, a
    # literal `+`, a `%` and a non-ASCII character, plus a "folder" whose own name holds a space so
    # CommonPrefixes is encoded too. The bundled corpus has none of these — every key in it comes
    # back the same encoded or not, so it cannot tell the two apart. The pair `a b.txt`/`a+b.txt`
    # is the reason the encoding exists: decoded they are the same string.
    for doc_id, key in (
        ("space", "a b.txt"),
        ("plus", "a+b.txt"),
        ("percent", "100%.csv"),
        ("hangul", "한글/x.txt"),
        ("folder", "run books/x.txt"),
        ("plain", "zz.txt"),
    ):
        yield {
            "source_type": "s3",
            "doc_id": f"s3-encoded-{doc_id}",
            "bucket": "encoded-bucket",
            "group": "engineering",
            "key": key,
            "title": key,
            "content": f"payload-{doc_id}",
            "author_email": "eng-bulk@acme.com",
            "author_groups": ["engineering"],
            "visibility": "public",
        }

    # A fourth bucket for the one group that has no successor: every key rolls up under the last
    # code point, so `key_successor` of the group is None and there is no bound to resume past.
    # Two keys, so one of them is still unfetched when the page holds the group.
    for doc_id in ("a", "b"):
        key = f"\U0010ffff{doc_id}.txt"
        yield {
            "source_type": "s3",
            "doc_id": f"s3-edge-{doc_id}",
            "bucket": "edge-bucket",
            "group": "engineering",
            "key": key,
            "title": key,
            "content": f"payload-{doc_id}",
            "author_email": "eng-bulk@acme.com",
            "author_groups": ["engineering"],
            "visibility": "public",
        }


@pytest.fixture(scope="module")
def big_bucket_settings(tmp_path_factory):
    """A DB of its own (not the shared SAMPLE) holding one bucket with ~3000 S3 objects."""
    from backlot.config import Settings
    from backlot.importer.byo import load

    data_dir = tmp_path_factory.mktemp("s3_big")
    settings = Settings(data_dir=data_dir)
    corpus = data_dir / "_big_corpus.jsonl"
    corpus.write_text("\n".join(json.dumps(complete(**r)) for r in _s3_big_corpus()))
    load(corpus, settings)
    return settings


@pytest.fixture(scope="module")
def big_bucket_tokens(big_bucket_settings):
    data = yaml.safe_load(big_bucket_settings.tokens_path.read_text())
    return {u["email"]: u["token"] for u in data["users"]}


@pytest.fixture(scope="module")
def big_bucket_client(big_bucket_settings):
    """The dedicated big-bucket DB, in-process: SigV4 only cares that the Host it sees matches what
    was signed, which holds for TestClient's base_url as much as a real port. ``reload=True``
    because the ``client`` fixture above still holds the module-level app — see ``client_for``."""
    with client_for(big_bucket_settings, reload=True) as c:
        yield c


def _s3_get(client, path, token, headers=None):
    return _s3_send(client, "GET", path, token, headers)


def _s3_send(client, method, path, token, headers=None, body=None):
    """SigV4-sign a request (same signer as the module-level ``_sign_get``) and issue it through an
    in-process TestClient instead of a live socket."""
    from urllib.parse import parse_qsl, quote, urlencode

    from botocore.awsrequest import AWSRequest

    from backlot import synth

    if "?" in path:
        path_part, query_part = path.split("?", 1)
        params = parse_qsl(query_part, keep_blank_values=True)
        query_part = urlencode(params, safe="-_.~", quote_via=quote)
        path = f"{path_part}?{query_part}"
    base_url = str(client.base_url)
    url = f"{base_url}{path}"
    ak = synth.s3_access_key_id(token)
    sk = synth.s3_secret_access_key(token)
    req = AWSRequest(method=method, url=url, headers=dict(headers or {}), data=body)
    _s3_signer(ak, sk).add_auth(req)
    return client.request(method, url, headers=dict(req.headers), content=body)


S3NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def _s3_keys(root) -> list[str]:
    return [e.text for e in root.findall(f"{{{S3NS}}}Contents/{{{S3NS}}}Key")]


def test_s3_large_bucket_prefix_filters_and_sorts(big_bucket_client, big_bucket_settings):
    pytest.importorskip("botocore")
    r = _s3_get(
        big_bucket_client,
        "/s3/big-bucket?list-type=2&prefix=logs/2026/01/&max-keys=1000",
        big_bucket_settings.admin_token,
    )
    assert r.status_code == 200
    root = ET.fromstring(r.text)
    keys = _s3_keys(root)
    assert len(keys) == 250  # 3000 / 12 months
    assert keys == sorted(keys)
    assert all(k.startswith("logs/2026/01/") for k in keys)
    assert root.findtext(f"{{{S3NS}}}IsTruncated") == "false"


def test_s3_large_bucket_pagination_round_trips(big_bucket_client, big_bucket_settings):
    pytest.importorskip("botocore")
    admin = big_bucket_settings.admin_token
    r1 = _s3_get(big_bucket_client, "/s3/big-bucket?list-type=2&max-keys=100", admin)
    root1 = ET.fromstring(r1.text)
    keys1 = _s3_keys(root1)
    assert len(keys1) == 100 and keys1 == sorted(keys1)
    assert root1.findtext(f"{{{S3NS}}}IsTruncated") == "true"
    token = root1.findtext(f"{{{S3NS}}}NextContinuationToken")
    assert token

    from urllib.parse import quote

    r2 = _s3_get(
        big_bucket_client,
        f"/s3/big-bucket?list-type=2&max-keys=100&continuation-token={quote(token)}",
        admin,
    )
    root2 = ET.fromstring(r2.text)
    keys2 = _s3_keys(root2)
    assert len(keys2) == 100 and keys2 == sorted(keys2)
    assert not (set(keys1) & set(keys2))  # no overlap between pages
    assert keys1[-1] < keys2[0]  # contiguous keyset order, no gap/dup
    assert root2.findtext(f"{{{S3NS}}}ContinuationToken") == token


def test_s3_large_bucket_delimiter_returns_common_prefixes(big_bucket_client, big_bucket_settings):
    pytest.importorskip("botocore")
    # Under a single month (250 objects, well within one SQL page) every "day" folder rolls up
    # into one CommonPrefixes entry, computed over that bounded page — see the comment on
    # backlot.routers.s3._list_objects for why this only holds a page's worth of raw rows at once.
    r = _s3_get(
        big_bucket_client,
        "/s3/big-bucket?list-type=2&prefix=logs/2026/01/&delimiter=/&max-keys=1000",
        big_bucket_settings.admin_token,
    )
    root = ET.fromstring(r.text)
    prefixes = {
        cp.findtext(f"{{{S3NS}}}Prefix") for cp in root.findall(f"{{{S3NS}}}CommonPrefixes")
    }
    assert prefixes == {f"logs/2026/01/{d:02d}/" for d in range(1, 26)}
    assert root.findall(f"{{{S3NS}}}Contents") == []  # every key continues past the delimiter
    assert root.findtext(f"{{{S3NS}}}IsTruncated") == "false"


@pytest.mark.parametrize("listing", ["", "list-type=2&", "versions&"], ids=["v1", "v2", "versions"])
def test_s3_large_bucket_acl_scopes_listing(
    big_bucket_client, big_bucket_settings, big_bucket_tokens, listing
):
    """The three served bodies scope the same way. The V1 one and ListObjectVersions carry a
    per-object ``Owner`` a scoped caller can read, so the ACL has to be proved on them and not only
    on the V2 shape."""
    pytest.importorskip("botocore")

    def keys_for(token):
        r = _s3_get(
            big_bucket_client,
            f"/s3/big-bucket?{listing}prefix=logs/2026/01/&max-keys=1000",
            token,
        )
        entry = "Version" if listing == "versions&" else "Contents"
        return {e.text for e in ET.fromstring(r.text).findall(f"{{{S3NS}}}{entry}/{{{S3NS}}}Key")}

    admin_keys = keys_for(big_bucket_settings.admin_token)
    eng_keys = keys_for(big_bucket_tokens["eng-bulk@acme.com"])
    people_keys = keys_for(big_bucket_tokens["people-bulk@acme.com"])

    assert len(admin_keys) == 250
    assert eng_keys and people_keys
    assert eng_keys < admin_keys and people_keys < admin_keys  # proper, non-empty subsets
    assert eng_keys.isdisjoint(people_keys)
    assert eng_keys | people_keys == admin_keys
    # And the scoped caller gets the body it asked for, per-object `Owner` and all.
    scoped = _s3_get(
        big_bucket_client,
        f"/s3/big-bucket?{listing}prefix=logs/2026/01/&max-keys=1",
        big_bucket_tokens["eng-bulk@acme.com"],
    )
    entry = "Version" if listing == "versions&" else "Contents"
    owner = ET.fromstring(scoped.text).find(f"{{{S3NS}}}{entry}/{{{S3NS}}}Owner/{{{S3NS}}}ID")
    assert (owner is not None) == (listing != "list-type=2&")


@pytest.mark.parametrize(
    "method, query, headers, body, status",
    [
        ("GET", "acl", None, None, 200),
        ("GET", "tagging", None, None, 200),
        ("GET", "attributes", {"x-amz-object-attributes": "ETag,ObjectSize"}, None, 200),
        ("GET", "annotation", None, None, 200),
        ("GET", "partNumber=1", None, None, 206),
        ("GET", "partNumber=2", None, None, 416),
        ("GET", "versionId=null", None, None, 200),
        ("GET", "", {"x-amz-checksum-mode": "ENABLED"}, None, 200),
        ("PUT", "encryption", None, b"<ObjectEncryption><SSE-S3/></ObjectEncryption>", 400),
    ],
)
def test_s3_an_objects_subresource_answers_only_a_caller_who_can_read_the_object(
    big_bucket_client, big_bucket_settings, big_bucket_tokens, method, query, headers, body, status
):
    """`big-bucket` holds objects of two groups in one bucket. Each of these answers from the
    object, UpdateObjectEncryption's checks among them, so a caller outside its group is told the
    key does not exist, as its listing tells it, where the admin and the object's own group get
    the answer, and so does that caller for an object of its own group."""
    people = f"/s3/big-bucket/logs/2026/01/02/obj-00012.json?{query}"
    engineering = f"/s3/big-bucket/logs/2026/01/01/obj-00000.json?{query}"
    eng, admin = big_bucket_tokens["eng-bulk@acme.com"], big_bucket_settings.admin_token
    client = big_bucket_client
    for token in (admin, big_bucket_tokens["people-bulk@acme.com"]):
        assert _s3_send(client, method, people, token, headers, body).status_code == status
    assert _s3_send(client, method, engineering, eng, headers, body).status_code == status
    hidden = _s3_send(client, method, people, eng, headers, body)
    code = "NoSuchVersion" if "versionId" in query else "NoSuchKey"
    assert hidden.status_code == 404 and f"<Code>{code}</Code>" in hidden.text


def test_s3_delimiter_common_prefix_not_duplicated_across_pages(
    big_bucket_client, big_bucket_settings
):
    """Fix 3 (correctness): "straddle-bucket" has one 150-object folder ("grp/big/") — bigger
    than a max-keys=100 page — plus a small trailing folder ("grp/small/"). Before the fix, the
    "grp/big/" CommonPrefixes group straddled the page cutoff and was emitted on BOTH the page
    where it started and the page where it resumed. Traverse every page and assert each
    CommonPrefixes/Content appears exactly once, with no gaps."""
    pytest.importorskip("botocore")
    admin = big_bucket_settings.admin_token
    from urllib.parse import quote

    seen_prefixes: list[str] = []
    seen_keys: list[str] = []
    url = "/s3/straddle-bucket?list-type=2&prefix=grp/&delimiter=/&max-keys=100"
    pages = 0
    while True:
        pages += 1
        assert pages <= 10, "too many pages — pagination isn't converging"
        r = _s3_get(big_bucket_client, url, admin)
        assert r.status_code == 200
        root = ET.fromstring(r.text)
        seen_prefixes += [
            cp.findtext(f"{{{S3NS}}}Prefix") for cp in root.findall(f"{{{S3NS}}}CommonPrefixes")
        ]
        seen_keys += _s3_keys(root)
        token = root.findtext(f"{{{S3NS}}}NextContinuationToken")
        if root.findtext(f"{{{S3NS}}}IsTruncated") != "true":
            assert token is None
            break
        assert token
        url = f"/s3/straddle-bucket?list-type=2&prefix=grp/&delimiter=/&max-keys=100&continuation-token={quote(token)}"

    # every CommonPrefixes appears EXACTLY once across all pages (no dup)...
    assert seen_prefixes == ["grp/big/", "grp/small/"]
    # ...and no plain Contents at all — both "folders" fully roll up under the delimiter (no gap)
    assert seen_keys == []


def test_s3_max_keys_zero_returns_empty_page_safely(big_bucket_client, big_bucket_settings):
    """max-keys=0 is an empty page that says so: KeyCount 0, IsTruncated false and no cursor.

    False whatever is in the bucket, which is what real S3 answers with keys in it (measured
    2026-09-14 against a bucket holding seven). A client that pages on IsTruncated is told there
    is no next page, and either way it is given no cursor to fetch one with. The page must also
    not crash: nothing indexes into it."""
    pytest.importorskip("botocore")
    r = _s3_get(
        big_bucket_client, "/s3/big-bucket?list-type=2&max-keys=0", big_bucket_settings.admin_token
    )
    assert r.status_code == 200
    root = ET.fromstring(r.text)
    assert root.findtext(f"{{{S3NS}}}KeyCount") == "0"
    assert root.findall(f"{{{S3NS}}}Contents") == []
    assert root.findall(f"{{{S3NS}}}CommonPrefixes") == []
    assert root.findtext(f"{{{S3NS}}}IsTruncated") == "false"
    assert root.findtext(f"{{{S3NS}}}NextContinuationToken") is None


# --- S3 --------------------------------------------------------------------------

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'


def _get_xml(base_url, path, token):
    url, headers = _sign_get(base_url, path, token)
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
        return ET.fromstring(r.read())


def test_list_buckets_xml_shape(live_server):
    """The bare page ``backlot.routers.s3.list_buckets`` describes, the owner as its 64-hex id
    alone; `max-directory-buckets`, not one of its parameters, leaves it as it is (measured
    2026-09-29)."""
    base_url, settings = live_server
    for path in ("/s3/", "/s3/?max-directory-buckets=1"):
        root = _get_xml(base_url, path, settings.admin_token)
        assert root.tag == f"{NS}ListAllMyBucketsResult"
        assert [c.tag for c in root] == [f"{NS}Owner", f"{NS}Buckets"], path
        assert [c.tag for c in root.find(f"{NS}Owner")] == [f"{NS}ID"], path
        assert re.fullmatch("[0-9a-f]{64}", root.findtext(f"{NS}Owner/{NS}ID")), path
        for b in root.iter(f"{NS}Bucket"):
            assert [c.tag for c in b] == [f"{NS}Name", f"{NS}CreationDate", f"{NS}BucketArn"]
            assert b.findtext(f"{NS}BucketArn") == "arn:aws:s3:::" + b.findtext(f"{NS}Name")
        names = {b.findtext(f"{NS}Name") for b in root.iter(f"{NS}Bucket")}
        assert "eng-artifacts" in names


def _bucket_page(base_url, query, token):
    """A ListBuckets page as its raw text, the names on it, each bucket's elements and what follows
    the buckets."""
    r = _signed(base_url, f"/s3/?{query}", token)
    assert r.status_code == 200, query
    root = ET.fromstring(r.content)
    buckets = root.find(f"{NS}Buckets")
    shapes = [[c.tag.removeprefix(NS) for c in b] for b in buckets]
    tail = [(c.tag.removeprefix(NS), c.text or "") for c in root][2:]
    return r.text, [b.findtext(f"{NS}Name") for b in buckets], shapes, tail


def test_list_buckets_pages_as_real_does_once_one_of_its_parameters_is_sent(live_server, tokens):
    """The paging ``backlot.routers.s3.list_buckets`` describes, followed to the end: any of its
    four parameters, an empty one too, puts each bucket's region in, the prefix follows the buckets
    escaped as real escapes it, and a truncated page's continuation token resumes after it. A scoped
    caller pages through the buckets it can see and no other."""
    from urllib.parse import quote

    base_url, settings = live_server
    admin = settings.admin_token
    names = [b.findtext(f"{NS}Name") for b in _get_xml(base_url, "/s3/", admin).iter(f"{NS}Bucket")]
    assert len(names) >= 3 and names == sorted(names)
    region = ["Name", "CreationDate", "BucketRegion", "BucketArn"]
    for query in (
        "max-buckets=",
        "max-buckets=05",
        "prefix=",
        "continuation-token=",
        "bucket-region=us-east-1",
        "bucket-region=Us-East-1",
        "bucket-region=us-east-1&bucket-region=zz",
        "max-buckets=1&max-buckets=abc",
    ):
        text, listed, shapes, _ = _bucket_page(base_url, query, admin)
        assert listed and shapes == [region] * len(listed), query
        assert "<BucketRegion>us-east-1</BucketRegion>" in text, query
    text, listed, _, tail = _bucket_page(base_url, "prefix=", admin)
    assert listed == names and tail == [("Prefix", "")] and "<Prefix></Prefix>" in text
    text, listed, _, tail = _bucket_page(base_url, "prefix=zzzz", admin)
    assert listed == [] and "<Buckets/>" in text and tail == [("Prefix", "zzzz")]
    text, listed, _, tail = _bucket_page(base_url, "max-buckets=1&prefix=x", admin)
    assert listed == [] and tail == [("Prefix", "x")]
    assert "<Prefix>&lt;&amp;</Prefix>" in _bucket_page(base_url, "prefix=%3C%26", admin)[0]
    _, listed, _, tail = _bucket_page(base_url, "prefix=eng&prefix=people", admin)
    assert listed == [n for n in names if n.startswith("eng")] != [] and tail == [("Prefix", "eng")]
    _, listed, _, tail = _bucket_page(base_url, "prefix=&max-buckets=1", admin)
    assert listed == names[:1] and [name for name, _ in tail] == ["Prefix", "ContinuationToken"]
    _, listed, _, tail = _bucket_page(base_url, "max-buckets=1&max-buckets=abc", admin)
    assert listed == names[:1] and [name for name, _ in tail] == ["ContinuationToken"]
    scoped = tokens["ava@acme.com"]
    seen_by = {}
    for token in (admin, scoped):
        visible = [
            b.findtext(f"{NS}Name") for b in _get_xml(base_url, "/s3/", token).iter(f"{NS}Bucket")
        ]
        seen, cursor = [], None
        for _hop in range(len(visible) + 1):
            query = "max-buckets=1" + (
                f"&continuation-token={quote(cursor, safe='')}" if cursor else ""
            )
            _, listed, _, tail = _bucket_page(base_url, query, token)
            seen += listed
            cursor = dict(tail).get("ContinuationToken")
            if cursor is None:
                break
        assert seen == visible, token
        seen_by[token] = seen
    assert set(seen_by[scoped]) < set(seen_by[admin])


_BUCKET_PARSE = "Provided max-buckets not an integer or within integer range"
_BUCKET_RANGE = "Argument max-buckets must be an integer between 1 and 10000"
_BUCKET_ENDPOINT = (
    "Requests with bucket-region specified must be made to the corresponding regional endpoint"
)
_BUCKET_REGION = "<ArgumentName>bucket-region</ArgumentName>"
_BUCKET_TOKEN = "The continuation token provided is incorrect"
_TOKEN_ARGUMENT = "<ArgumentName>continuation-token</ArgumentName>"


def _max_buckets(value):
    return f"<ArgumentName>max-buckets</ArgumentName><ArgumentValue>{value}</ArgumentValue>"


# ListBuckets' refusals as real answered them signed (2026-09-29, us-east-1): the query, the message
# and the members after it (None: the listing), and the status real answered the request with
# unsigned and over a bad secret, where it was sent so (same date).
# fmt: off
_LIST_BUCKETS_REFUSAL_ROWS = [
    ("max-buckets=abc", _BUCKET_PARSE, _max_buckets("abc"), 400, 400),
    ("max-buckets=%201", _BUCKET_PARSE, _max_buckets(" 1"), None, None),
    ("max-buckets=1%20", _BUCKET_PARSE, _max_buckets("1 "), None, None),
    ("max-buckets=abc&max-buckets=1", _BUCKET_PARSE, _max_buckets("abc"), None, None),
    ("max-buckets=abc&continuation-token=garbage", _BUCKET_PARSE, _max_buckets("abc"), None, None),
    ("max-buckets=0", _BUCKET_RANGE, _max_buckets("0"), 400, 400),
    ("max-buckets=-01", _BUCKET_RANGE, _max_buckets("-1"), None, None),
    ("max-buckets=010001", _BUCKET_RANGE, _max_buckets("10001"), None, None),
    ("max-buckets=0&bucket-region=zz", _BUCKET_RANGE, _max_buckets("0"), None, None),
    ("bucket-region=zz", "Argument value zz is not a valid AWS Region", _BUCKET_REGION, 400, 400),
    ("bucket-region=", "Argument value  is not a valid AWS Region", _BUCKET_REGION, None, None),
    ("bucket-region=Z%26Z", "Argument value Z&amp;Z is not a valid AWS Region", _BUCKET_REGION,
     None, None),
    ("bucket-region=us-east-1%20", "Argument value us-east-1  is not a valid AWS Region",
     _BUCKET_REGION, None, None),
    ("bucket-region=%20us-east-1", "Argument value  us-east-1 is not a valid AWS Region",
     _BUCKET_REGION, None, None),
    ("bucket-region=us-east-1%09", "Argument value us-east-1\t is not a valid AWS Region",
     _BUCKET_REGION, None, None),
    ("bucket-region=zz&bucket-region=us-east-1", "Argument value zz is not a valid AWS Region",
     _BUCKET_REGION, None, None),
    ("continuation-token=garbage&bucket-region=zz", "Argument value zz is not a valid AWS Region",
     _BUCKET_REGION, None, None),
    ("prefix=&bucket-region=zz", "Argument value zz is not a valid AWS Region", _BUCKET_REGION,
     None, None),
    ("bucket-region=ap-northeast-2", _BUCKET_ENDPOINT, _BUCKET_REGION, 400, 400),
    ("bucket-region=US-WEST-2", _BUCKET_ENDPOINT, _BUCKET_REGION, None, None),
    ("bucket-region=eu-west-1", _BUCKET_ENDPOINT, _BUCKET_REGION, None, None),
    # A continuation token, read after the credential: unsigned the 307 and over a bad secret the
    # 403, as a `prefix` gets them in the last row; a listing's group token is none of ListBuckets'.
    ("continuation-token=garbage", _BUCKET_TOKEN, _TOKEN_ARGUMENT, 307, 403),
    ("continuation-token=Zzp4", _BUCKET_TOKEN, _TOKEN_ARGUMENT, 307, 403),
    ("prefix=x", None, None, 307, 403),
]
# fmt: on


@pytest.mark.parametrize(
    "query, message, members, unsigned, tampered",
    _LIST_BUCKETS_REFUSAL_ROWS,
    ids=[r[0] for r in _LIST_BUCKETS_REFUSAL_ROWS],
)
def test_list_buckets_refuses_its_parameters_where_real_does(
    live_server, query, message, members, unsigned, tampered
):
    """Each refusal byte for byte signed; `max-buckets` and `bucket-region` the same 400 unsigned
    and over a bad secret, before the credential, where a `continuation-token` is the 307 and the
    403 a `prefix` gets, after it."""
    import httpx

    base_url, settings = live_server
    path = f"/s3/?{query}"
    signed = _signed(base_url, path, settings.admin_token)
    arms = [(signed, 200 if message is None else 400)]
    if unsigned is not None:
        url, headers = _sign_get(base_url, path, settings.admin_token, tamper=True)
        arms += [
            (httpx.get(f"{base_url}{path}"), unsigned),
            (httpx.get(url, headers=headers), tampered),
        ]
    for r, status in arms:
        assert r.status_code == status
        if status == 400:
            assert r.headers["content-type"] == "application/xml"
            assert _without_request_ids(r.text) == (
                _DECL + f"<Error><Code>InvalidArgument</Code><Message>{message}</Message>"
                f"{members}<RequestId/><HostId/></Error>"
            )
        elif status == 307:
            assert r.content == b""
        elif status == 403:
            assert "<Code>SignatureDoesNotMatch</Code>" in r.text


def test_list_objects_v2_xml_shape(live_server):
    base_url, settings = live_server
    root = _get_xml(base_url, "/s3/eng-artifacts?list-type=2", settings.admin_token)
    assert root.tag == f"{NS}ListBucketResult"
    assert root.findtext(f"{NS}Name") == "eng-artifacts"
    assert root.findtext(f"{NS}IsTruncated") in ("true", "false")
    c = next(root.iter(f"{NS}Contents"))
    assert c.findtext(f"{NS}Key") and c.findtext(f"{NS}ETag").startswith('"')
    assert c.findtext(f"{NS}LastModified").endswith("Z")


def test_list_objects_v2_delimiter_common_prefixes(live_server):
    base_url, settings = live_server
    root = _get_xml(base_url, "/s3/eng-artifacts?list-type=2&delimiter=/", settings.admin_token)
    prefixes = {cp.findtext(f"{NS}Prefix") for cp in root.iter(f"{NS}CommonPrefixes")}
    assert {"runbooks/", "design/"} <= prefixes


# ------------------------------------------------------------ sub-resources
# S3 dispatches on the query string: `?versioning`, `?acl`, `?tagging` and the rest each select an
# operation of their own at a bucket's or an object's path. Backlot answers every one of a bucket's
# as real answers a bucket nobody configured, and an object's as real answers an object with none of
# them. Every claim about real S3 below was measured against a general purpose bucket: each selector
# is answered as its own operation, an unknown key (`?foo=bar`, `?x-id=…`) is ignored, the match is
# case-sensitive, two selectors conflict, and HEAD with a selector is 405.

# What real answered each selector's GET with on a bucket nobody configured: status, Content-Type
# and the body as sent, request ids aside (2026-09-29, us-east-1).
# fmt: off
_CONFIGURATION_ROWS = [
    ("abac", 200, None,
     '<?xml version="1.0" encoding="UTF-8"?>\n<AbacStatus xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Status>Disabled</Status></AbacStatus>'),
    ("accelerate", 200, None,
     '<?xml version="1.0" encoding="UTF-8"?>\n<AccelerateConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>'),
    ("acl", 200, "application/xml",
     '<?xml version="1.0" encoding="UTF-8"?>\n<AccessControlPolicy xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Owner><ID>{owner}</ID></Owner><AccessControlList><Grant><Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="CanonicalUser"><ID>{owner}</ID></Grantee><Permission>FULL_CONTROL</Permission></Grant></AccessControlList></AccessControlPolicy>'),
    ("analytics", 200, None,
     '<ListBucketAnalyticsConfigurationsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated></ListBucketAnalyticsConfigurationsResult>'),
    ("cors", 404, "application/xml",
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>NoSuchCORSConfiguration</Code><Message>The CORS configuration does not exist</Message><BucketName>{bucket}</BucketName><RequestId/><HostId/></Error>'),
    ("encryption", 200, None,
     '<?xml version="1.0" encoding="UTF-8"?>\n<ServerSideEncryptionConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Rule><BucketKeyEnabled>false</BucketKeyEnabled><ApplyServerSideEncryptionByDefault><SSEAlgorithm>AES256</SSEAlgorithm></ApplyServerSideEncryptionByDefault><BlockedEncryptionTypes><EncryptionType>SSE-C</EncryptionType></BlockedEncryptionTypes></Rule></ServerSideEncryptionConfiguration>'),
    ("intelligent-tiering", 200, None,
     '<ListIntelligentTieringConfigurationsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated></ListIntelligentTieringConfigurationsResult>'),
    ("inventory", 200, None,
     '<?xml version="1.0" encoding="UTF-8"?><ListInventoryConfigurationsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated></ListInventoryConfigurationsResult>'),
    ("lifecycle", 404, "application/xml",
     "<Error><Code>NoSuchLifecycleConfiguration</Code><Message>The lifecycle configuration does not exist</Message><BucketName>{bucket}</BucketName><RequestId/><HostId/></Error>"),
    ("location", 200, "application/xml",
     '<?xml version="1.0" encoding="UTF-8"?>\n<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>'),
    ("logging", 200, "application/xml",
     '<?xml version="1.0" encoding="UTF-8"?>\n\n<BucketLoggingStatus xmlns="http://s3.amazonaws.com/doc/2006-03-01/">\n  <!--<LoggingEnabled><TargetBucket>myLogsBucket</TargetBucket><TargetPrefix>add/this/prefix/to/my/log/files/access_log-</TargetPrefix></LoggingEnabled>-->\n</BucketLoggingStatus>\n'),
    ("metadataConfiguration", 404, "application/xml",
     "<Error><Code>MetadataConfigurationNotFound</Code><Message>The metadata configuration was not found</Message><BucketName>{bucket}</BucketName><RequestId/><HostId/></Error>"),
    ("metadataTable", 405, "application/xml",
     "<Error><Code>V1APIsNotAllowed</Code><Message>The V1 GetBucketMetadataTableConfiguration API operation isn't available for this account. Use the corresponding V2 GetBucketMetadataConfiguration API operation instead.</Message><Method>GET</Method><RequestId/><HostId/></Error>"),
    ("metrics", 200, None,
     '<?xml version="1.0" encoding="UTF-8"?><ListMetricsConfigurationsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated></ListMetricsConfigurationsResult>'),
    ("notification", 200, None,
     '<?xml version="1.0" encoding="UTF-8"?>\n<NotificationConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>'),
    ("object-lock", 404, "application/xml",
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>ObjectLockConfigurationNotFoundError</Code><Message>Object Lock configuration does not exist for this bucket</Message><BucketName>{bucket}</BucketName><RequestId/><HostId/></Error>'),
    ("ownershipControls", 200, None,
     '<?xml version="1.0" encoding="UTF-8"?>\n<OwnershipControls xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Rule><ObjectOwnership>BucketOwnerEnforced</ObjectOwnership></Rule></OwnershipControls>'),
    ("policy", 404, "application/xml",
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>NoSuchBucketPolicy</Code><Message>The bucket policy does not exist</Message><BucketName>{bucket}</BucketName><RequestId/><HostId/></Error>'),
    ("policyStatus", 404, "application/xml",
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>NoSuchBucketPolicy</Code><Message>The bucket policy does not exist</Message><BucketName>{bucket}</BucketName><RequestId/><HostId/></Error>'),
    ("publicAccessBlock", 200, None,
     '<?xml version="1.0" encoding="UTF-8"?>\n<PublicAccessBlockConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><BlockPublicAcls>true</BlockPublicAcls><IgnorePublicAcls>true</IgnorePublicAcls><BlockPublicPolicy>true</BlockPublicPolicy><RestrictPublicBuckets>true</RestrictPublicBuckets></PublicAccessBlockConfiguration>'),
    ("replication", 404, "application/xml",
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>ReplicationConfigurationNotFoundError</Code><Message>The replication configuration was not found</Message><BucketName>{bucket}</BucketName><RequestId/><HostId/></Error>'),
    ("requestPayment", 200, None,
     '<?xml version="1.0" encoding="UTF-8"?>\n<RequestPaymentConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Payer>BucketOwner</Payer></RequestPaymentConfiguration>'),
    ("tagging", 404, "application/xml",
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>NoSuchTagSet</Code><Message>The TagSet does not exist</Message><BucketName>{bucket}</BucketName><RequestId/><HostId/></Error>'),
    ("versioning", 200, None,
     '<?xml version="1.0" encoding="UTF-8"?>\n<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>'),
    ("website", 404, "application/xml",
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>NoSuchWebsiteConfiguration</Code><Message>The specified bucket does not have a website configuration</Message><BucketName>{bucket}</BucketName><RequestId/><HostId/></Error>'),
]
# fmt: on


def _without_request_ids(body: str) -> str:
    return re.sub(r"<(RequestId|HostId)>[^<]*</\1>", r"<\1/>", body)


@pytest.mark.parametrize(
    "selector, status, content_type, body",
    _CONFIGURATION_ROWS,
    ids=[r[0] for r in _CONFIGURATION_ROWS],
)
def test_a_bucket_configuration_is_what_real_answers_for_a_bucket_nobody_configured(
    live_server, selector, status, content_type, body
):
    """Each configuration a bucket has, at the bucket's path, byte for byte what real sent for one
    nobody configured, the prolog and the `Content-Type` among them; and a HEAD naming it is the
    405 whose `Allow` names the GET that answers it."""
    base_url, settings = live_server
    owner = _get_xml(base_url, "/s3/eng-artifacts", settings.admin_token).findtext(
        f"{NS}Contents/{NS}Owner/{NS}ID"
    )
    r = _signed(base_url, f"/s3/eng-artifacts?{selector}", settings.admin_token)
    assert r.status_code == status
    assert r.headers.get("content-type") == content_type
    assert _without_request_ids(r.text) == body.format(bucket="eng-artifacts", owner=owner)
    head = _signed(base_url, f"/s3/eng-artifacts?{selector}", settings.admin_token, method="HEAD")
    assert head.status_code == 405 and head.headers.get("allow") == "GET"


@pytest.mark.parametrize(
    "selector",
    [
        "accelerate",
        "cors",
        "inventory",
        "lifecycle",
        "location",
        "notification",
        "policy",
        "replication",
        "requestPayment",
        "versioning",
        "website",
    ],
)
def test_a_bucket_selector_at_a_key_is_the_buckets_own_answer(live_server, selector):
    """The bucket's own answer, byte for byte, at a key it has and one it does not
    (``backlot.routers.s3._KEY_BUCKET_SELECTORS``), the bucket named where the answer names one."""
    base_url, settings = live_server
    at_bucket = _signed(base_url, f"/s3/eng-artifacts?{selector}", settings.admin_token)
    for path in (OBJECT_PATH, "/s3/eng-artifacts/no/such.md"):
        at_key = _signed(base_url, f"{path}?{selector}", settings.admin_token)
        assert at_key.status_code == at_bucket.status_code, path
        assert at_key.headers.get("content-type") == at_bucket.headers.get("content-type"), path
        assert _without_request_ids(at_key.text) == _without_request_ids(at_bucket.text), path


OBJECT_PATH = "/s3/eng-artifacts/runbooks/oncall.md"
OBJECT_TEXT = b"Check dashboards, roll back, page on-call."


def _refused(base_url, path, token, method="GET") -> urllib.error.HTTPError:
    url, headers = _sign_get(base_url, path, token, method=method)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method))
    return e.value


_OBJECT_LOCK = (
    _DECL
    + "<Error><Code>InvalidRequest</Code><Message>Bucket is missing Object Lock Configuration</Message><RequestId/><HostId/></Error>"
)
_TORRENT = (
    _DECL
    + "<Error><Code>MethodNotAllowed</Code><Message>The specified method is not allowed against this resource.</Message><Method>GET</Method><ResourceType>TORRENT</ResourceType><RequestId/><HostId/></Error>"
)
# An object's sub-resources as real answered them on a GET (2026-09-29, us-east-1), at a key the
# bucket has and at one it does not: the query, the headers sent, the status, the Content-Type and
# the body with the request ids aside. `{owner}`, `{etag}` and `{size}` are the object's own.
# fmt: off
_OBJECT_SUBRESOURCE_ROWS = [
    (
        "acl",
        {},
        200,
        "application/xml",
        _DECL
        + '<AccessControlPolicy xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Owner><ID>{owner}</ID></Owner><AccessControlList><Grant><Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="CanonicalUser"><ID>{owner}</ID></Grantee><Permission>FULL_CONTROL</Permission></Grant></AccessControlList></AccessControlPolicy>',
        404,
        _DECL
        + "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{missing}</Key><RequestId/><HostId/></Error>",
    ),
    (
        "tagging",
        {},
        200,
        None,
        _DECL + '<Tagging xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><TagSet/></Tagging>',
        404,
        _DECL
        + "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{bucket}/{missing}</Key><RequestId/><HostId/></Error>",
    ),
    ("attributes", {}, 400, "application/xml",
     "<Error><Code>InvalidRequest</Code><Message>The x-amz-object-attributes header specifying the attributes to be retrieved is either missing or empty</Message><RequestId/><HostId/></Error>",
     400,
     "<Error><Code>InvalidRequest</Code><Message>The x-amz-object-attributes header specifying the attributes to be retrieved is either missing or empty</Message><RequestId/><HostId/></Error>"),
    (
        "attributes",
        {"x-amz-object-attributes": "ETag,Checksum,ObjectParts,StorageClass,ObjectSize"},
        200,
        None,
        _DECL
        + '<GetObjectAttributesResponse xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><ETag>{etag}</ETag><Checksum><ChecksumCRC64NVME>{crc}</ChecksumCRC64NVME><ChecksumType>FULL_OBJECT</ChecksumType></Checksum><StorageClass>STANDARD</StorageClass><ObjectSize>{size}</ObjectSize></GetObjectAttributesResponse>',
        404,
        "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{missing}</Key><RequestId/><HostId/></Error>",
    ),
    *[
        (
            "attributes",
            {"x-amz-object-attributes": names},
            200,
            None,
            _DECL
            + '<GetObjectAttributesResponse xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Checksum><ChecksumCRC64NVME>{crc}</ChecksumCRC64NVME><ChecksumType>FULL_OBJECT</ChecksumType></Checksum></GetObjectAttributesResponse>',
            404,
            "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{missing}</Key><RequestId/><HostId/></Error>",
        )
        for names in ("Checksum", "Checksum,ObjectParts")
    ],
    (
        "attributes",
        {"x-amz-object-attributes": "ObjectSize, ETag"},
        200,
        None,
        _DECL
        + '<GetObjectAttributesResponse xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><ETag>{etag}</ETag><ObjectSize>{size}</ObjectSize></GetObjectAttributesResponse>',
        404,
        "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{missing}</Key><RequestId/><HostId/></Error>",
    ),
    (
        "attributes",
        {"x-amz-object-attributes": "ETag,"},
        200,
        None,
        _DECL
        + '<GetObjectAttributesResponse xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><ETag>{etag}</ETag></GetObjectAttributesResponse>',
        404,
        "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{missing}</Key><RequestId/><HostId/></Error>",
    ),
    ("attributes", {"x-amz-object-attributes": "ObjectParts"}, 200, None,
     _DECL + '<GetObjectAttributesResponse xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>', 404,
     "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{missing}</Key><RequestId/><HostId/></Error>"),
    ("legal-hold", {}, 400, "application/xml", _OBJECT_LOCK, 400, _OBJECT_LOCK),
    ("retention", {}, 400, "application/xml", _OBJECT_LOCK, 400, _OBJECT_LOCK),
    ("torrent", {}, 405, "application/xml", _TORRENT, 405, _TORRENT),
    (
        "annotation",
        {},
        200,
        None,
        _DECL
        + '<ListObjectAnnotationsOutput xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Annotations/><Bucket>{bucket}</Bucket><Key>{key}</Key><AnnotationCount>0</AnnotationCount></ListObjectAnnotationsOutput>',
        404,
        _DECL
        + "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{missing}</Key><RequestId/><HostId/></Error>",
    ),
    (
        "annotation&annotation-prefix=x&max-annotation-results=5",
        {},
        200,
        None,
        _DECL
        + '<ListObjectAnnotationsOutput xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Annotations/><Bucket>{bucket}</Bucket><Key>{key}</Key><AnnotationPrefix>x</AnnotationPrefix><MaxAnnotationResults>5</MaxAnnotationResults><AnnotationCount>0</AnnotationCount></ListObjectAnnotationsOutput>',
        404,
        _DECL
        + "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{missing}</Key><RequestId/><HostId/></Error>",
    ),
    (
        "annotation&annotation-prefix=",
        {},
        200,
        None,
        _DECL
        + '<ListObjectAnnotationsOutput xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Annotations/><Bucket>{bucket}</Bucket><Key>{key}</Key><AnnotationPrefix></AnnotationPrefix><AnnotationCount>0</AnnotationCount></ListObjectAnnotationsOutput>',
        404,
        _DECL
        + "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{missing}</Key><RequestId/><HostId/></Error>",
    ),
    (
        "annotation&max-annotation-results=",
        {},
        200,
        None,
        _DECL
        + '<ListObjectAnnotationsOutput xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Annotations/><Bucket>{bucket}</Bucket><Key>{key}</Key><MaxAnnotationResults>1000</MaxAnnotationResults><AnnotationCount>0</AnnotationCount></ListObjectAnnotationsOutput>',
        404,
        _DECL
        + "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{missing}</Key><RequestId/><HostId/></Error>",
    ),
    (
        "annotation&continuation-token=",
        {},
        400,
        "application/xml",
        _DECL
        + "<Error><Code>InvalidArgument</Code><Message>The continuation token provided is incorrect</Message><ArgumentName>continuation-token</ArgumentName><RequestId/><HostId/></Error>",
        404,
        _DECL
        + "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{missing}</Key><RequestId/><HostId/></Error>",
    ),
    (
        "annotation&annotationName=x",
        {},
        404,
        "application/xml",
        _DECL
        + "<Error><Code>NoSuchAnnotation</Code><Message>The specified annotation does not exist.</Message><AnnotationName>x</AnnotationName><RequestId/><HostId/></Error>",
        404,
        _DECL
        + "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{missing}</Key><RequestId/><HostId/></Error>",
    ),
    (
        "annotation&annotationName=",
        {},
        400,
        "application/xml",
        _DECL
        + "<Error><Code>UserAnnotationNameMustBeSpecified</Code><Message>You must specify a user annotation name.</Message><RequestId/><HostId/></Error>",
        400,
        _DECL
        + "<Error><Code>UserAnnotationNameMustBeSpecified</Code><Message>You must specify a user annotation name.</Message><RequestId/><HostId/></Error>",
    ),
    (
        "tagging&versionId=null",
        {},
        200,
        None,
        _DECL + '<Tagging xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><TagSet/></Tagging>',
        404,
        _DECL
        + "<Error><Code>NoSuchVersion</Code><Message>The specified version does not exist.</Message><Key>{bucket}/{missing}</Key><VersionId>null</VersionId><RequestId/><HostId/></Error>",
    ),
    (
        "acl&versionId=garbage",
        {},
        400,
        "application/xml",
        _DECL
        + "<Error><Code>InvalidArgument</Code><Message>Invalid version id specified</Message><ArgumentName>versionId</ArgumentName><ArgumentValue>garbage</ArgumentValue><RequestId/><HostId/></Error>",
        400,
        _DECL
        + "<Error><Code>InvalidArgument</Code><Message>Invalid version id specified</Message><ArgumentName>versionId</ArgumentName><ArgumentValue>garbage</ArgumentValue><RequestId/><HostId/></Error>",
    ),
    (
        "versionId=garbage",
        {},
        400,
        "application/xml",
        _DECL
        + "<Error><Code>InvalidArgument</Code><Message>Invalid version id specified</Message><ArgumentName>versionId</ArgumentName><ArgumentValue>garbage</ArgumentValue><RequestId/><HostId/></Error>",
        400,
        _DECL
        + "<Error><Code>InvalidArgument</Code><Message>Invalid version id specified</Message><ArgumentName>versionId</ArgumentName><ArgumentValue>garbage</ArgumentValue><RequestId/><HostId/></Error>",
    ),
    (
        "versionId=NULL",
        {},
        400,
        "application/xml",
        _DECL
        + "<Error><Code>InvalidArgument</Code><Message>Invalid version id specified</Message><ArgumentName>versionId</ArgumentName><ArgumentValue>NULL</ArgumentValue><RequestId/><HostId/></Error>",
        400,
        _DECL
        + "<Error><Code>InvalidArgument</Code><Message>Invalid version id specified</Message><ArgumentName>versionId</ArgumentName><ArgumentValue>NULL</ArgumentValue><RequestId/><HostId/></Error>",
    ),
]
# fmt: on


@pytest.mark.parametrize(
    "query, headers, status, content_type, body, missing_status, missing_body",
    _OBJECT_SUBRESOURCE_ROWS,
    ids=[
        r[0] + ("-" + next(iter(r[1].values())) if r[1] else "") for r in _OBJECT_SUBRESOURCE_ROWS
    ],
)
def test_an_objects_subresource_is_what_real_answers_for_an_object_with_none_of_it(
    live_server, query, headers, status, content_type, body, missing_status, missing_body
):
    """Each byte for byte, the prolog and the `Content-Type` among them, and the same at a key the
    bucket does not hold; a HEAD naming the selector is the 405 whose `Allow` names the GET that
    answers it, `?torrent`'s none."""
    base_url, settings = live_server
    owner = _get_xml(base_url, "/s3/eng-artifacts", settings.admin_token).findtext(
        f"{NS}Contents/{NS}Owner/{NS}ID"
    )
    from backlot.routers import s3 as s3_router

    fill = dict(
        owner=owner,
        etag=hashlib.md5(OBJECT_TEXT).hexdigest(),
        crc=s3_router._crc64nvme(OBJECT_TEXT),
        size=len(OBJECT_TEXT),
        bucket="eng-artifacts",
        key="runbooks/oncall.md",
        missing="no/such.md",
    )
    r = _signed(base_url, f"{OBJECT_PATH}?{query}", settings.admin_token, extra_headers=headers)
    assert r.status_code == status
    assert r.headers.get("content-type") == content_type
    assert _without_request_ids(r.text) == body.format(**fill)
    missing = _signed(
        base_url,
        f"/s3/eng-artifacts/no/such.md?{query}",
        settings.admin_token,
        extra_headers=headers,
    )
    assert missing.status_code == missing_status
    assert _without_request_ids(missing.text) == missing_body.format(**fill)
    selector = query.split("&")[0].split("=")[0]
    head = _signed(base_url, f"{OBJECT_PATH}?{query}", settings.admin_token, method="HEAD")
    assert head.content == b""
    if selector == "versionId":
        # A HEAD of the object, with the GET's status (same date).
        assert head.status_code == status
    else:
        assert head.status_code == 405
        assert head.headers.get("allow") == (None if selector == "torrent" else "GET")


_NO_VERSION_ID = "This operation does not accept a version-id."
_NO_WEBSITE_VALUE = "The website parameter must not have a value"
_BAD_VERSION = "Invalid version id specified"


_ANNOTATION_RANGE = "Argument max-annotation-results must be an integer between 1 and 1000"
_ANNOTATION_PARSE = "Provided max-annotation-results not an integer or within integer range"
_VERSION_EMPTY = "Version id cannot be the empty string"
_BAD_NAME = "Invalid attribute name specified."
_MISMATCH = "The request signature we calculated does not match"
_PART_RANGE = "Part number must be an integer between 1 and 10000, inclusive"
_PART_AND_RANGE = "Cannot specify both Range header and partNumber query parameter"
_BAD_CHECKSUM_MODE = "Value for x-amz-checksum-mode header is invalid."
_NO_SUCH_PART = (
    "The requested partnumber is not satisfiable</Message><PartNumberRequested>{}"
    "</PartNumberRequested><ActualPartCount>1</ActualPartCount>"
)
_NO_BUCKET = "The specified bucket does not exist"


def _argued(name, value=None):
    """`InvalidArgument`'s members: the name, and the value where real names one."""
    value = "" if value is None else f"<ArgumentValue>{value}</ArgumentValue>"
    return f"<ArgumentName>{name}</ArgumentName>{value}"


def _argument_value(value):
    """An `InvalidArgument` asserted by the value member it names."""
    return (400, "InvalidArgument", f"<ArgumentValue>{value}</ArgumentValue>")


def _attributes(value):
    return {"x-amz-object-attributes": value}


# The answers the rows below repeat.
_ANNOTATION_COUNT_RANGE = (400, "InvalidArgument", _ANNOTATION_RANGE)
_ANNOTATION_COUNT_PARSE = (400, "InvalidArgument", _ANNOTATION_PARSE)
_BUCKET_ABSENT = (404, "NoSuchBucket", _NO_BUCKET)
_SIGNATURE_MISMATCH = (403, "SignatureDoesNotMatch", _MISMATCH)
_VERSION_ID_REFUSED = (400, "InvalidArgument", _NO_VERSION_ID)
_VERSION_ID_EMPTY = (400, "InvalidArgument", _VERSION_EMPTY)
_BAD_ATTRIBUTE = (400, "InvalidArgument", _BAD_NAME)
_VERSION_ID_INVALID = (400, "InvalidArgument", _BAD_VERSION)
_ARGUMENT_00 = _argument_value("00")
_ARGUMENT_ETAG = _argument_value("etag")
_ARGUMENT_EMPTY = _argument_value("")
_PART_OUT_OF_RANGE = (400, "InvalidArgument", _PART_RANGE)
_PART_BESIDE_RANGE = (400, "InvalidRequest", _PART_AND_RANGE)
_KEY_MISSING = (404, "NoSuchKey", "<Key>no/such.md</Key>")
_PART_2_MISSING = (416, "InvalidPartNumber", _NO_SUCH_PART.format(2))
_NULL_VERSION_MISSING = (404, "NoSuchVersion", "<VersionId>null</VersionId>")
_CHECKSUM_MODE_REFUSED = (400, "InvalidRequest", _BAD_CHECKSUM_MODE)
_GARBAGE_MODE = {"x-amz-checksum-mode": "garbage"}

# Where each of an object's refusals sits against the bucket and the credential, as real answered
# (2026-09-29, us-east-1): at a key in a bucket that does not exist (`absent`), at the object over a
# bad secret (`tampered`), at the object signed (`present`) and at a key the bucket does not hold
# (`missing`), two refusals at once among them; the query, the headers, and the status, code and
# message fragment.
# fmt: off
_OBJECT_ORDER_ROWS = [
    ("absent", "annotation&max-annotation-results=0", {}, *_ANNOTATION_COUNT_RANGE),
    ("tampered", "annotation&max-annotation-results=0", {}, *_ANNOTATION_COUNT_RANGE),
    ("absent", "annotation&max-annotation-results=abc", {}, *_ANNOTATION_COUNT_PARSE),
    ("tampered", "annotation&max-annotation-results=abc", {}, *_ANNOTATION_COUNT_PARSE),
    ("absent", "annotation&continuation-token=garbage", {}, *_BUCKET_ABSENT),
    ("tampered", "annotation&continuation-token=garbage", {}, *_SIGNATURE_MISMATCH),
    ("absent", "annotation&annotationName=", {}, *_BUCKET_ABSENT),
    ("tampered", "annotation&annotationName=", {}, *_SIGNATURE_MISMATCH),
    ("absent", "torrent&versionId=garbage", {}, *_VERSION_ID_REFUSED),
    ("tampered", "torrent&versionId=garbage", {}, *_VERSION_ID_REFUSED),
    ("absent", "versionId=", {}, *_VERSION_ID_EMPTY),
    ("tampered", "versionId=", {}, *_VERSION_ID_EMPTY),
    ("absent", "tagging&versionId=", {}, *_VERSION_ID_EMPTY),
    ("absent", "versionId=garbage", {}, *_BUCKET_ABSENT),
    ("tampered", "versionId=garbage", {}, *_SIGNATURE_MISMATCH),
    ("absent", "versionId=null", {}, *_BUCKET_ABSENT),
    ("tampered", "versionId=null", {}, *_SIGNATURE_MISMATCH),
    ("absent", "attributes", _attributes("garbage"), *_BAD_ATTRIBUTE),
    ("tampered", "attributes", _attributes("garbage"), *_BAD_ATTRIBUTE),
    ("absent", "attributes", {}, *_BUCKET_ABSENT),
    ("absent", "acl", {}, *_BUCKET_ABSENT),
    ("absent", "tagging", {}, *_BUCKET_ABSENT),
    ("absent", "legal-hold", {}, *_BUCKET_ABSENT),
    ("absent", "retention", {}, *_BUCKET_ABSENT),
    ("absent", "torrent", {}, *_BUCKET_ABSENT),
    ("absent", "annotation", {}, *_BUCKET_ABSENT),
    ("present", "legal-hold&versionId=garbage", {}, *_VERSION_ID_INVALID),
    ("present", "attributes&versionId=garbage", {}, *_VERSION_ID_INVALID),
    ("present", "annotation&annotationName=x&max-annotation-results=abc", {},
     404, "NoSuchAnnotation", ">x<"),
    ("present", "annotation&max-annotation-results=abc&continuation-token=garbage", {},
     *_ANNOTATION_COUNT_PARSE),
    ("present", "annotation&max-annotation-results=0&continuation-token=garbage", {},
     *_ANNOTATION_COUNT_RANGE),
    ("present", "annotation&max-annotation-results=1001", {}, *_argument_value("1001")),
    ("present", "annotation&max-annotation-results=-01", {}, *_argument_value("-01")),
    ("present", "annotation&max-annotation-results=00", {}, *_ARGUMENT_00),
    ("present", "attributes", _attributes("etag"), *_ARGUMENT_ETAG),
    ("present", "attributes", _attributes(""), *_ARGUMENT_EMPTY),
    # GetObject's `partNumber`: after the bucket and before the key, a `Range` beside it first and
    # a `versionId` before that, and a part the object does not have after the key.
    ("present", "partNumber", {},
     400, "InvalidArgument", _PART_RANGE + "</Message>" + _argued("partNumber", "")),
    ("present", "partNumber=00", {}, *_ARGUMENT_00),
    ("present", "partNumber=%201", {}, *_argument_value(" 1")),
    ("present", "partNumber=1.0", {}, *_argument_value("1.0")),
    ("present", "partNumber=-1", {}, *_argument_value("-1")),
    ("present", "partNumber=10001", {}, *_argument_value("10001")),
    ("missing", "partNumber=abc", {}, *_PART_OUT_OF_RANGE),
    ("absent", "partNumber=abc", {}, *_BUCKET_ABSENT),
    ("tampered", "partNumber=abc", {}, *_SIGNATURE_MISMATCH),
    ("present", "partNumber=1", {"Range": "bytes=0-1"}, *_PART_BESIDE_RANGE),
    ("present", "partNumber=1", {"Range": ""}, *_PART_BESIDE_RANGE),
    ("present", "partNumber=abc", {"Range": "bytes=0-1"}, *_PART_BESIDE_RANGE),
    ("missing", "partNumber=1", {"Range": "bytes=0-1"}, *_PART_BESIDE_RANGE),
    ("absent", "partNumber=1", {"Range": "bytes=0-1"}, *_BUCKET_ABSENT),
    ("present", "partNumber=1&versionId=garbage", {"Range": "bytes=0-1"}, *_VERSION_ID_INVALID),
    ("present", "partNumber=abc&versionId=garbage", {}, *_VERSION_ID_INVALID),
    ("present", "partNumber=abc&versionId=", {}, *_VERSION_ID_EMPTY),
    ("missing", "partNumber=2", {}, *_KEY_MISSING),
    ("present", "partNumber=2", {}, *_PART_2_MISSING),
    ("present", "partNumber=02", {}, *_PART_2_MISSING),
    ("present", "partNumber=010000", {}, 416, "InvalidPartNumber", _NO_SUCH_PART.format(10000)),
    # A `versionId` beside a selector whose operation takes none
    # (``backlot.routers.s3._KEY_VERSIONLESS``).
    ("absent", "versioning&versionId=x", {}, *_VERSION_ID_REFUSED),
    ("tampered", "location&versionId=x", {}, *_VERSION_ID_REFUSED),
    ("present", "cors&versionId=x", {}, *_VERSION_ID_REFUSED),
    ("present", "logging&versionId=x", {}, *_VERSION_ID_REFUSED),
    ("present", "uploads&versionId=x", {}, *_VERSION_ID_REFUSED),
    ("present", "uploadId=u&versionId=null", {}, *_VERSION_ID_REFUSED),
    ("present", "versioning&versionId=null", {}, *_VERSION_ID_REFUSED),
    ("absent", "acl&versionId=x", {}, *_BUCKET_ABSENT),
    # `x-amz-object-attributes` read as `_attribute_names` reads it.
    ("present", "attributes", _attributes(",ETag"), *_ARGUMENT_EMPTY),
    ("present", "attributes", _attributes("ETag,,ObjectSize"), *_ARGUMENT_EMPTY),
    ("absent", "attributes", _attributes("ETag, ,ObjectSize"), *_ARGUMENT_EMPTY),
    ("tampered", "attributes", _attributes("etag,garbage"), *_ARGUMENT_ETAG),
    ("present", "attributes", _attributes("garbage,etag"), *_argument_value("garbage")),
    ("present", "attributes", _attributes("ETag, etag"), *_ARGUMENT_ETAG),
    ("present", "attributes", _attributes(","), 400, "InvalidRequest", "either missing or empty"),
    ("absent", "attributes", _attributes(","), *_BUCKET_ABSENT),
    ("tampered", "attributes", _attributes(",,"), *_SIGNATURE_MISMATCH),
    # A key the bucket does not hold, asked for by version or by part.
    ("missing", "versionId=null", {}, *_NULL_VERSION_MISSING),
    ("missing", "partNumber=1", {}, *_KEY_MISSING),
    ("missing", "partNumber=1&versionId=null", {}, *_NULL_VERSION_MISSING),
    # A checksum mode other than `ENABLED`: after the bucket, the credential, the version, the
    # part's refusals and a range or a part the object lacks, and ahead of a missing key.
    ("present", "", _GARBAGE_MODE, *_CHECKSUM_MODE_REFUSED),
    ("present", "", {"x-amz-checksum-mode": ""}, *_CHECKSUM_MODE_REFUSED),
    ("missing", "", _GARBAGE_MODE, *_CHECKSUM_MODE_REFUSED),
    ("missing", "versionId=null", _GARBAGE_MODE, *_CHECKSUM_MODE_REFUSED),
    ("absent", "", _GARBAGE_MODE, *_BUCKET_ABSENT),
    ("tampered", "", _GARBAGE_MODE, *_SIGNATURE_MISMATCH),
    ("present", "versionId=garbage", _GARBAGE_MODE, *_VERSION_ID_INVALID),
    ("present", "partNumber=abc", _GARBAGE_MODE, *_PART_OUT_OF_RANGE),
    ("present", "partNumber=2", _GARBAGE_MODE, *_PART_2_MISSING),
    ("present", "", {**_GARBAGE_MODE, "Range": "bytes=999-"},
     416, "InvalidRange", "not satisfiable"),
    ("present", "partNumber=1", {**_GARBAGE_MODE, "Range": "bytes=0-1"}, *_PART_BESIDE_RANGE),
]
# fmt: on


@pytest.mark.parametrize(
    "where, query, headers, status, code, fragment",
    _OBJECT_ORDER_ROWS,
    ids=[
        f"{r[0]}-{r[1]}" + ("-" + next(iter(r[2].values())) if r[2] else "")
        for r in _OBJECT_ORDER_ROWS
    ],
)
def test_an_objects_subresource_refusals_sit_where_real_puts_them(
    live_server, where, query, headers, status, code, fragment
):
    """The parses before the bucket and the credential, the version and the header checks after
    them and before the key, and the rest after the key, as in the table above."""
    import httpx

    base_url, settings = live_server
    path = {
        "absent": "/s3/no-such-bucket/k.txt",
        "missing": "/s3/eng-artifacts/no/such.md",
    }.get(where, OBJECT_PATH) + (f"?{query}" if query else "")
    url, signed = _sign_get(
        base_url, path, settings.admin_token, tamper=where == "tampered", extra_headers=headers
    )
    r = httpx.get(url, headers=signed)
    assert r.status_code == status
    assert f"<Code>{code}</Code>" in r.text and fragment in r.text


_FULL_RANGE = f"bytes 0-{len(OBJECT_TEXT) - 1}/{len(OBJECT_TEXT)}"
_FIRST_TEN = f"bytes 0-9/{len(OBJECT_TEXT)}"
# The object's own answer to a GET and a HEAD, as real answered its object (2026-09-29 and
# 2026-09-30): the query, the headers sent, the status, the range the answer names (None: none) and
# whether it carries the object's checksum.
_OBJECT_ANSWER_ROWS = [
    ("", {}, 200, None, False),
    ("versionId=null", {}, 200, None, False),
    ("partNumber=1", {}, 206, _FULL_RANGE, False),
    ("partNumber=01", {}, 206, _FULL_RANGE, False),
    ("partNumber=1&versionId=null", {}, 206, _FULL_RANGE, False),
    ("", {"Range": "bytes=0-9"}, 206, _FIRST_TEN, False),
    ("", {"x-amz-checksum-mode": "ENABLED"}, 200, None, True),
    ("", {"x-amz-checksum-mode": "enabled"}, 200, None, True),
    ("versionId=null", {"x-amz-checksum-mode": "ENABLED"}, 200, None, True),
    ("partNumber=1", {"x-amz-checksum-mode": "ENABLED"}, 206, _FULL_RANGE, True),
    ("", {"x-amz-checksum-mode": "ENABLED", "Range": "bytes=0-"}, 206, _FULL_RANGE, True),
    ("", {"x-amz-checksum-mode": "ENABLED", "Range": "bytes=0-9"}, 206, _FIRST_TEN, False),
    ("", {"x-amz-content-sha256": "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"}, 200, None, False),
    ("", {"x-amz-content-sha256": "0" * 64}, 200, None, False),
]


@pytest.mark.parametrize(
    "query, headers, status, content_range, checksum",
    _OBJECT_ANSWER_ROWS,
    ids=[
        "-".join([r[0] or "bare", *(f"{k}={v}" for k, v in r[1].items())])
        for r in _OBJECT_ANSWER_ROWS
    ],
)
def test_an_objects_own_answer_is_what_real_sends_for_its_object(
    live_server, query, headers, status, content_range, checksum
):
    """On a GET and a HEAD, the bytes of the range named or of the whole object, its one part the
    206 of its whole range, and in checksum mode the object's CRC-64/NVME beside an answer that
    holds all of it and none beside one that holds less; a signed payload hash that is not the
    empty body's, or that names an aws-chunked one, is not read."""
    from backlot.routers import s3 as s3_router

    base_url, settings = live_server
    path = f"{OBJECT_PATH}?{query}" if query else OBJECT_PATH
    etag = _signed(base_url, OBJECT_PATH, settings.admin_token).headers["etag"]
    first, last = 0, len(OBJECT_TEXT) - 1
    if content_range is not None:
        first, last = map(int, content_range.split()[1].split("/")[0].split("-"))
    for method in ("GET", "HEAD"):
        r = _signed(base_url, path, settings.admin_token, method=method, extra_headers=headers)
        assert r.status_code == status, method
        assert r.headers.get("content-range") == content_range, method
        assert r.headers["content-length"] == str(last - first + 1), method
        assert r.content == (OBJECT_TEXT[first : last + 1] if method == "GET" else b""), method
        assert r.headers["etag"] == etag, method
        crc = s3_router._crc64nvme(OBJECT_TEXT) if checksum else None
        assert r.headers.get("x-amz-checksum-crc64nvme") == crc, method
        assert r.headers.get("x-amz-checksum-type") == ("FULL_OBJECT" if checksum else None), method


def test_an_objects_attributes_size_is_its_bytes(tmp_path):
    """`ObjectSize` counts the object's bytes, which a key the GET sends with a `Content-Length`
    of its own agrees with, so an object whose text is not ASCII is larger than its length in
    characters."""
    text = "Café — 3€"
    record = complete(
        source_type="s3",
        doc_id="s3-cafe",
        bucket="eng-artifacts",
        group="engineering",
        key="notes/cafe.md",
        title="cafe.md",
        content=text,
        content_type="text/markdown",
        author_email="ava@acme.com",
        author_groups=["engineering"],
        visibility="public",
    )
    tiny = tiny_corpus(tmp_path, [record])
    # `reload=True`, as `big_bucket_client` does: a second lifespan on the app another client holds
    # would overwrite that client's store.
    with client_for(tiny, reload=True) as client:
        path = "/s3/eng-artifacts/notes/cafe.md"
        body = _s3_get(client, path, tiny.admin_token)
        attributes = _s3_get(
            client,
            f"{path}?attributes",
            tiny.admin_token,
            {"x-amz-object-attributes": "ObjectSize"},
        )
    size = len(text.encode("utf-8"))
    assert size > len(text) and body.headers["content-length"] == str(size)
    assert f"<ObjectSize>{size}</ObjectSize>" in attributes.text


def test_the_listing_location_and_object_still_answer_and_an_unknown_key_is_ignored(live_server):
    base_url, settings = live_server
    token = settings.admin_token
    # A bare bucket GET is ListObjects and `?list-type=2` its v2 form; `?foo=bar` and an `x-id` key
    # (the AWS SDK for JavaScript names the operation with one) are not selectors; `?Versioning` is
    # not `?versioning`; `?session` (CreateSession, directory buckets only) lists on a general
    # purpose bucket; and so does `?annotationName=v`, which real signs (2026-09-29).
    for query in (
        "",
        "?list-type=2",
        "?foo=bar",
        "?x-id=ListObjects",
        "?Versioning",
        "?session",
        "?annotationName=v",
    ):
        root = _get_xml(base_url, f"/s3/eng-artifacts{query}", token)
        assert root.tag == f"{NS}ListBucketResult", query
    assert _get_xml(base_url, "/s3/eng-artifacts?location", token).tag == f"{NS}LocationConstraint"
    # At a key's path too (``backlot.routers.s3._KEY_BUCKET_SELECTORS``).
    for path in (f"{OBJECT_PATH}?location", "/s3/eng-artifacts/no/such.md?location"):
        assert _get_xml(base_url, path, token).tag == f"{NS}LocationConstraint", path
    for query in ("", "?x-id=GetObject", "?foo=bar"):
        url, headers = _sign_get(base_url, f"{OBJECT_PATH}{query}", token)
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
            assert r.status == 200 and r.read() == OBJECT_TEXT, query


def test_two_subresources_at_once_conflict_the_way_real_s3_conflicts_them(live_server):
    base_url, settings = live_server
    # Named alphabetically whatever order they were sent in, and the first is the ArgumentValue.
    for path in ("/s3/eng-artifacts?versioning&acl", "/s3/eng-artifacts?acl&versioning"):
        err = _refused(base_url, path, settings.admin_token)
        body = err.read()
        assert err.code == 400
        assert b"<Code>InvalidArgument</Code>" in body
        assert b"<Message>Conflicting query string parameters: acl, versioning</Message>" in body
        assert (
            b"<ArgumentName>ResourceType</ArgumentName><ArgumentValue>acl</ArgumentValue>" in body
        )
    # `location` and `uploads` conflict like any other.
    err = _refused(base_url, "/s3/eng-artifacts?location&versioning", settings.admin_token)
    assert err.code == 400 and b"location, versioning" in err.read()
    err = _refused(base_url, "/s3/eng-artifacts?versioning&uploads", settings.admin_token)
    assert err.code == 400 and b"uploads, versioning" in err.read()
    err = _refused(base_url, f"{OBJECT_PATH}?tagging&acl", settings.admin_token)
    assert err.code == 400 and b"acl, tagging" in err.read()
    # A bucket's `metadata*Table`, and at a key `partNumber` beside a selector (2026-09-29).
    err = _refused(base_url, "/s3/eng-artifacts?metadataAnnotationTable&acl", settings.admin_token)
    assert err.code == 400 and b"acl, metadataAnnotationTable" in err.read()
    err = _refused(base_url, f"{OBJECT_PATH}?partNumber=abc&acl", settings.admin_token)
    assert err.code == 400 and b"acl, partNumber</Message>" in err.read()
    err = _refused(base_url, f"{OBJECT_PATH}?partNumber=1&tagging", settings.admin_token)
    body = err.read()
    assert err.code == 400 and b"partNumber, tagging" in body
    assert b"<ArgumentValue>partNumber</ArgumentValue>" in body
    # The conflict is reported before the bucket or the key is looked up.
    err = _refused(base_url, "/s3/no-such-bucket?acl&versioning", settings.admin_token)
    assert err.code == 400 and b"InvalidArgument" in err.read()
    err = _refused(base_url, "/s3/eng-artifacts/no/such.md?acl&tagging", settings.admin_token)
    assert err.code == 400 and b"InvalidArgument" in err.read()


def test_head_with_two_subresources_is_the_conflicts_400_with_an_empty_body(live_server):
    base_url, settings = live_server
    # Real S3 keeps the conflict's status for a HEAD and, as for any HEAD, sends no body; the
    # bucket and the key are not looked up first.
    for path in (
        "/s3/eng-artifacts?versioning&acl",
        f"{OBJECT_PATH}?acl&tagging",
        "/s3/no-such-bucket?acl&versioning",
        "/s3/eng-artifacts/no/such.md?acl&tagging",
    ):
        err = _refused(base_url, path, settings.admin_token, method="HEAD")
        assert err.code == 400 and err.read() == b"", path
        assert err.headers.get("Content-Type") == "application/xml"
        # Real sends no `Allow` on the conflict's 400 (measured).
        assert err.headers.get("Allow") is None, path


def test_what_does_not_exist_is_reported_before_the_subresource_except_for_list_parts(live_server):
    base_url, settings = live_server
    token = settings.admin_token
    err = _refused(base_url, "/s3/no-such-bucket?versioning", token)
    assert err.code == 404 and b"NoSuchBucket" in err.read()
    err = _refused(base_url, "/s3/eng-artifacts/no/such.md?acl", token)
    assert err.code == 404 and b"NoSuchKey" in err.read()
    # ListParts is about an upload, not the object under the key: real S3 answers NoSuchUpload for
    # a missing key rather than NoSuchKey, so Backlot answers it before looking the key up.
    err = _refused(base_url, "/s3/eng-artifacts/no/such.md?uploadId=abc123", token)
    assert err.code == 404 and b"<Code>NoSuchUpload</Code>" in err.read()
    # The bucket comes before all of that (``backlot.routers.s3.object_get``), with or without a
    # selector, ListParts' and a key's `?uploads` included.
    for query in ("", "?acl", "?uploadId=abc123", "?uploads"):
        err = _refused(base_url, f"/s3/no-such-bucket/no/such.md{query}", token)
        assert err.code == 404 and b"<Code>NoSuchBucket</Code>" in err.read(), query


def test_head_with_a_subresource_names_what_a_get_serves_and_a_bare_head_still_answers(live_server):
    base_url, settings = live_server
    token = settings.admin_token
    # Every bucket selector is served on a GET and still has no HEAD form, so each of their 405s
    # names GET (and each is asserted beside its GET above), and so does each of an object's that
    # its GET answers.
    for path, allow in (
        ("/s3/eng-artifacts?location", "GET"),
        ("/s3/eng-artifacts?uploads", "GET"),
        # Before the bucket or the key is looked up, as on real S3 — the header with it: a bucket
        # that does not exist answers `Allow: GET` for `?location` on real too (measured
        # 2026-09-17, ap-northeast-2).
        ("/s3/no-such-bucket?location", "GET"),
        ("/s3/no-such-bucket?versioning", "GET"),
        ("/s3/eng-artifacts/no/such.md?acl", "GET"),
        # The selectors a GET is refused for are 405s on a HEAD too
        # (``backlot.routers.s3._read_refusal``).
        ("/s3/eng-artifacts?delete", None),
        ("/s3/no-such-bucket?restore", None),
        (f"{OBJECT_PATH}?delete", None),
        (f"{OBJECT_PATH}?encryption", None),
        (f"{OBJECT_PATH}?select", None),
        (f"{OBJECT_PATH}?uploads", None),
        # A bucket's selectors at a key (``backlot.routers.s3._KEY_BUCKET_SELECTORS``) and two of an
        # object's at a bucket (``backlot.routers.s3._BUCKET_OBJECT_SELECTORS``), which the GET at
        # the same path answers after the bucket.
        (f"{OBJECT_PATH}?versioning", "GET"),
        (f"{OBJECT_PATH}?location", "GET"),
        ("/s3/no-such-bucket/a.txt?website", "GET"),
        (f"{OBJECT_PATH}?logging", None),
        (f"{OBJECT_PATH}?versions", None),
        ("/s3/eng-artifacts?torrent", None),
        ("/s3/eng-artifacts?uploadId=x", None),
        (f"{OBJECT_PATH}?partNumber=1&uploadId=x", None),
        # ListParts at a key, which the GET there answers, and a bucket's `metadata*Table`, which
        # it does not (2026-09-29).
        (f"{OBJECT_PATH}?uploadId=x", "GET"),
        ("/s3/no-such-bucket/a.txt?uploadId=x", "GET"),
        ("/s3/no-such-bucket?metadataAnnotationTable", None),
        ("/s3/eng-artifacts?metadataJournalTable", None),
    ):
        err = _refused(base_url, path, token, method="HEAD")
        assert err.code == 405 and err.read() == b"", path
        assert err.headers.get("Content-Type") == "application/xml", path
        assert err.headers.get("Allow") == allow, path
    # The bucket selectors real ignores at a key's path, `metrics` among them, leave its HEAD the
    # object's (same date).
    for path in (
        "/s3/eng-artifacts",
        OBJECT_PATH,
        f"{OBJECT_PATH}?metrics",
        f"{OBJECT_PATH}?metadataInventoryTable",
    ):
        url, headers = _sign_get(base_url, path, token, method="HEAD")
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers, method="HEAD")
        ) as r:
            assert r.status == 200, path


# The answers the rows below repeat.
_NO_BUCKET_NAMED = (404, "NoSuchBucket", "<BucketName>no-such-bucket</BucketName>")
_INVALID_REQUEST = (400, "InvalidRequest", "")


def _bad_argument(name, value=None):
    """An `InvalidArgument` naming the parameter, and its value where real names one."""
    return (400, "InvalidArgument", _argued(name, value))


def _not_allowed(method, resource):
    """A `MethodNotAllowed` naming the method and real's name for the resource."""
    return (
        405,
        "MethodNotAllowed",
        f"<Method>{method}</Method><ResourceType>{resource}</ResourceType>",
    )


# What each refusal names between its message and the request id pair: the member real names that
# code with, and never a `Resource` (`backlot.routers.s3._error`). InvalidRange's pair is asserted
# beside its 416 above.
# fmt: off
_MEMBER_ROWS = [
    ("GET", "/s3/no-such-bucket?list-type=2", *_NO_BUCKET_NAMED),
    ("GET", "/s3/no-such-bucket/a/b.txt", *_NO_BUCKET_NAMED),
    ("DELETE", "/s3/no-such-bucket", *_NO_BUCKET_NAMED),
    ("GET", "/s3/eng-artifacts/does/not/exist.md",
     404, "NoSuchKey", "<Key>does/not/exist.md</Key>"),
    ("GET", "/s3/eng-artifacts?list-type=2&max-keys=-1", *_bad_argument("maxKeys", "-1")),
    ("GET", "/s3/eng-artifacts?list-type=2&marker=x", *_bad_argument("marker")),
    ("GET", "/s3/eng-artifacts?acl&versioning", *_bad_argument("ResourceType", "acl")),
    ("GET", "/s3/eng-artifacts?delete", *_not_allowed("GET", "MULTI_OBJECT_DELETE")),
    ("GET", f"{OBJECT_PATH}?uploads", *_INVALID_REQUEST),
    ("GET", f"{OBJECT_PATH}?uploadId=abc123", 404, "NoSuchUpload", "<UploadId>abc123</UploadId>"),
    ("GET", "/s3/eng-artifacts/no/such.md?uploadId=", 404, "NoSuchUpload", "<UploadId></UploadId>"),
    ("GET", "/s3/no-such-bucket/a.txt?uploadId=x&max-parts=abc",
     *_bad_argument("max-parts", "abc")),
    ("GET", "/s3/eng-artifacts?partNumber=1", *_INVALID_REQUEST),
    ("GET", "/s3/eng-artifacts/runbooks/oncall.md?uploadId=x&part-number-marker=abc",
     *_bad_argument("part-number-marker", "abc")),
    # Two object selectors at a bucket's path, after the bucket
    # (``backlot.routers.s3._BUCKET_OBJECT_SELECTORS``).
    ("GET", "/s3/eng-artifacts?torrent", *_not_allowed("GET", "TORRENT")),
    ("GET", "/s3/eng-artifacts?uploadId=x", *_INVALID_REQUEST),
    ("POST", "/s3/eng-artifacts?uploadId=x", *_INVALID_REQUEST),
    ("GET", "/s3/no-such-bucket?torrent", *_NO_BUCKET_NAMED),
    ("DELETE", "/s3/no-such-bucket?uploadId=x", *_NO_BUCKET_NAMED),
    # The bucket selectors real answers at a key's path are the bucket's own operations, whatever
    # the key, and name the bucket; `?logging` and `?versions` are 400s of their own there.
    ("GET", "/s3/eng-artifacts/runbooks/oncall.md?cors",
     404, "NoSuchCORSConfiguration", "<BucketName>eng-artifacts</BucketName>"),
    ("GET", "/s3/eng-artifacts/no/such.md?policy",
     404, "NoSuchBucketPolicy", "<BucketName>eng-artifacts</BucketName>"),
    ("GET", "/s3/eng-artifacts/runbooks/oncall.md?logging", 400, "NoLoggingStatusForKey", ""),
    ("GET", "/s3/eng-artifacts/runbooks/oncall.md?versions", *_INVALID_REQUEST),
    ("GET", "/s3/no-such-bucket/a.txt?versioning", *_NO_BUCKET_NAMED),
    # This server's own refusal, which has no real body to copy.
    ("DELETE", "/s3/eng-artifacts", 501, "NotImplemented", ""),
    # A payload hash naming a trailer, on an operation that takes no upload: after the bucket and
    # what is refused before the credential, ahead of what is refused after the bucket.
    *[
        ("GET", path, *_INVALID_REQUEST, {"x-amz-content-sha256": trailer})
        for trailer in (_TRAILER, "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER",
                        "STREAMING-AWS4-ECDSA-P256-SHA256-PAYLOAD-TRAILER")
        for path in ("/s3/eng-artifacts?list-type=2", "/s3/eng-artifacts/runbooks/oncall.md")
    ],
    *[
        ("GET", path, *_INVALID_REQUEST, _TRAILER_SENT)
        for path in (
            "/s3/eng-artifacts?versioning",
            "/s3/eng-artifacts?acl",
            "/s3/eng-artifacts?list-type=2&continuation-token=garbage",
            "/s3/eng-artifacts/does/not/exist.md",
            "/s3/eng-artifacts/runbooks/oncall.md?partNumber=abc",
            "/s3/eng-artifacts/runbooks/oncall.md?versionId=garbage",
            "/s3/",
        )
    ],
    ("GET", "/s3/no-such-bucket?list-type=2", *_NO_BUCKET_NAMED, _TRAILER_SENT),
    ("GET", "/s3/eng-artifacts?max-keys=abc", *_bad_argument("max-keys", "abc"), _TRAILER_SENT),
    ("GET", "/s3/eng-artifacts?acl&versioning", *_bad_argument("ResourceType", "acl"),
     _TRAILER_SENT),
]
# fmt: on


@pytest.mark.parametrize(
    "method, path, status, code, members, headers",
    [r if len(r) == 6 else (*r, {}) for r in _MEMBER_ROWS],
    ids=[f"{i}-{r[0]}-{r[3]}-{r[1].rsplit('/', 1)[-1]}" for i, r in enumerate(_MEMBER_ROWS)],
)
def test_s3_a_refusal_names_what_it_refused_with_the_member_real_uses_for_its_code(
    live_server, method, path, status, code, members, headers
):
    """The member each code names, as ``backlot.routers.s3._error`` records it, whether the path
    names a key or not; nothing more for a key's GET `?uploads`, and none for a payload hash naming
    a trailer (2026-09-29)."""
    base_url, settings = live_server
    r = _signed(base_url, path, settings.admin_token, method=method, extra_headers=headers)
    assert r.status_code == status
    named = re.search(r"<Code>([^<]+)</Code><Message>[^<]*</Message>(.*)<RequestId>", r.text)
    assert (named[1], named[2]) == (code, members)


# The four configuration lists' own parameters, as real answered them (2026-09-29).
# fmt: off
_CONFIGURATION_LIST_ROWS = [
    ("analytics&id=x", 404,
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>NoSuchConfiguration</Code><Message>The specified configuration does not exist.</Message><RequestId/><HostId/></Error>'),
    ("analytics&id=", 200,
     '<ListBucketAnalyticsConfigurationsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated></ListBucketAnalyticsConfigurationsResult>'),
    ("analytics&continuation-token=garbage", 400,
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>MalformedContinuationToken</Code><Message>The continuation-token you provided invalid.</Message><RequestId/><HostId/></Error>'),
    ("analytics&continuation-token=", 200,
     '<ListBucketAnalyticsConfigurationsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated><ContinuationToken></ContinuationToken></ListBucketAnalyticsConfigurationsResult>'),
    ("analytics&id=x&continuation-token=garbage", 404,
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>NoSuchConfiguration</Code><Message>The specified configuration does not exist.</Message><RequestId/><HostId/></Error>'),
    ("intelligent-tiering&id=x", 404,
     "<Error><Code>NoSuchConfiguration</Code><Message>The specified configuration does not exist.</Message><RequestId/><HostId/></Error>"),
    ("intelligent-tiering&id=", 200,
     '<ListIntelligentTieringConfigurationsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated></ListIntelligentTieringConfigurationsResult>'),
    ("intelligent-tiering&continuation-token=garbage", 400,
     "<Error><Code>MalformedContinuationToken</Code><Message>The continuation-token you provided invalid.</Message><RequestId/><HostId/></Error>"),
    ("intelligent-tiering&continuation-token=", 200,
     '<ListIntelligentTieringConfigurationsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated><ContinuationToken></ContinuationToken></ListIntelligentTieringConfigurationsResult>'),
    ("intelligent-tiering&id=x&continuation-token=garbage", 404,
     "<Error><Code>NoSuchConfiguration</Code><Message>The specified configuration does not exist.</Message><RequestId/><HostId/></Error>"),
    ("inventory&id=x", 404,
     "<Error><Code>NoSuchConfiguration</Code><Message>The specified configuration does not exist.</Message><RequestId/><HostId/></Error>"),
    ("inventory&id=", 200,
     '<?xml version="1.0" encoding="UTF-8"?><ListInventoryConfigurationsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated></ListInventoryConfigurationsResult>'),
    ("inventory&continuation-token=garbage", 400,
     "<Error><Code>MalformedContinuationToken</Code><Message>The continuation-token you provided invalid.</Message><RequestId/><HostId/></Error>"),
    ("inventory&continuation-token=", 200,
     '<?xml version="1.0" encoding="UTF-8"?><ListInventoryConfigurationsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated><ContinuationToken></ContinuationToken></ListInventoryConfigurationsResult>'),
    ("inventory&id=x&continuation-token=garbage", 404,
     "<Error><Code>NoSuchConfiguration</Code><Message>The specified configuration does not exist.</Message><RequestId/><HostId/></Error>"),
    ("metrics&id=x", 404,
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>NoSuchConfiguration</Code><Message>The specified configuration does not exist.</Message><RequestId/><HostId/></Error>'),
    ("metrics&id=", 200,
     '<?xml version="1.0" encoding="UTF-8"?><ListMetricsConfigurationsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated></ListMetricsConfigurationsResult>'),
    ("metrics&continuation-token=garbage", 400,
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>MalformedContinuationToken</Code><Message>The continuation-token you provided invalid.</Message><RequestId/><HostId/></Error>'),
    ("metrics&continuation-token=", 200,
     '<?xml version="1.0" encoding="UTF-8"?><ListMetricsConfigurationsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated><ContinuationToken></ContinuationToken></ListMetricsConfigurationsResult>'),
    ("metrics&id=x&continuation-token=garbage", 404,
     '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>NoSuchConfiguration</Code><Message>The specified configuration does not exist.</Message><RequestId/><HostId/></Error>'),
]
# fmt: on


@pytest.mark.parametrize(
    "query, status, body", _CONFIGURATION_LIST_ROWS, ids=[r[0] for r in _CONFIGURATION_LIST_ROWS]
)
def test_a_configuration_list_reads_its_id_and_token_as_real_does(live_server, query, status, body):
    """Each list empty, as on a bucket nobody configured: an `id` names a configuration there is
    none of, a token is one it never handed out, and each is refused with the prolog real sent."""
    base_url, settings = live_server
    r = _signed(base_url, f"/s3/eng-artifacts?{query}", settings.admin_token)
    assert r.status_code == status
    assert _without_request_ids(r.text) == body


# The answers the rows below repeat.
_VERSION_ID_X = (400, _NO_VERSION_ID, _argued("versionId", "x"))
_BAD_VERSION_ID_X = (400, _BAD_VERSION, _argued("versionId", "x"))
_MAX_KEYS_ABC = (
    400,
    "Provided max-keys not an integer or within integer range",
    _argued("max-keys", "abc"),
)
_WEBSITE_V = (400, _NO_WEBSITE_VALUE, _argued("website", "v"))
_PARAMETER_NO_BUCKET = (404, _NO_BUCKET, "<BucketName>no-such-bucket</BucketName>")
_ANNOTATION_NAME_UNEXPECTED = (
    400,
    "Unexpected query string parameter",
    _argued("ResourceType", "annotationName"),
)
_MARKER_WITHOUT_KEY = (
    400,
    "A version-id marker cannot be specified without a key marker.",
    _argued("version-id-marker", "x"),
)
_BOGUS_ENCODING = (
    400,
    "Invalid Encoding Method specified in Request",
    _argued("encoding-type", "bogus"),
)

# A query parameter real refuses, and which of two it refuses when both are sent: the request, and
# the status, code, message and members of real's answer (measured 2026-09-29 against us-east-1).
# fmt: off
_PARAMETER_ROWS = [
    # `versionId` at a bucket's path, on each operation there.
    ("GET", "/s3/eng-artifacts?versionId=x", *_VERSION_ID_X),
    ("GET", "/s3/eng-artifacts?list-type=2&versionId=x", *_VERSION_ID_X),
    ("GET", "/s3/eng-artifacts?session&versionId=x", *_VERSION_ID_X),
    ("GET", "/s3/eng-artifacts?versions&versionId=x", *_VERSION_ID_X),
    ("GET", "/s3/eng-artifacts?uploads&versionId=x", *_VERSION_ID_X),
    ("GET", "/s3/eng-artifacts?versioning&versionId=", 400, _NO_VERSION_ID,
     _argued("versionId", "")),
    ("GET", "/s3/eng-artifacts?versioning&versionId=null", 400, _NO_VERSION_ID,
     _argued("versionId", "null")),
    ("GET", "/s3/eng-artifacts?acl&versionId=x", *_BAD_VERSION_ID_X),
    ("GET", "/s3/no-such-bucket?versioning&versionId=x", *_VERSION_ID_X),
    ("PUT", "/s3/no-such-bucket?versioning&versionId=x", *_VERSION_ID_X),
    ("DELETE", "/s3/no-such-bucket?versionId=x", *_VERSION_ID_X),
    # What comes before it, and what after.
    ("GET", "/s3/eng-artifacts?max-keys=abc&versionId=x", *_MAX_KEYS_ABC),
    ("GET", "/s3/eng-artifacts?versions&versionId=x&max-keys=abc", *_MAX_KEYS_ABC),
    ("GET", "/s3/eng-artifacts?uploads&max-uploads=abc&versionId=x", 400,
     "Provided max-uploads not an integer or within integer range", _argued("max-uploads", "abc")),
    ("GET", "/s3/no-such-bucket?versionId=x&start-after=a", 400,
     "startAfter only supported in REST.GET.BUCKET with list-type=2", _argued("start-after")),
    ("GET", "/s3/eng-artifacts?versionId=x&acl&versioning", 400,
     "Conflicting query string parameters: acl, versioning", _argued("ResourceType", "acl")),
    ("GET", "/s3/eng-artifacts?versionId=x&partNumber=1", 400, "Object must have a valid key name.",
     ""),
    ("GET", "/s3/eng-artifacts?versionId=x&delete", *_VERSION_ID_X),
    ("GET", "/s3/eng-artifacts?versionId=x&website=v", *_VERSION_ID_X),
    ("GET", "/s3/eng-artifacts?versionId=x&max-keys=-1", *_VERSION_ID_X),
    ("GET", "/s3/eng-artifacts?versionId=x&encoding-type=bogus", *_VERSION_ID_X),
    # A valued `website`, at a bucket's path and a key's.
    ("GET", "/s3/no-such-bucket?website=v", *_WEBSITE_V),
    ("PUT", "/s3/no-such-bucket?website=v", *_WEBSITE_V),
    # A key's writes read a valued `website` before the bucket too; an empty one is the write's
    # (2026-09-30).
    *[
        (method, "/s3/no-such-bucket/k.txt?website=v", *_WEBSITE_V)
        for method in ("PUT", "DELETE", "POST", "PATCH")
    ],
    *[
        (method, "/s3/no-such-bucket/k.txt?website=", *_PARAMETER_NO_BUCKET)
        for method in ("PUT", "DELETE")
    ],
    # A bucket's writes and a `versionId` (``backlot.routers.s3._VERSION_AFTER_THE_BUCKET``).
    ("POST", "/s3/eng-artifacts?versionId=x", 412,
     "At least one of the pre-conditions you specified did not hold",
     "<Condition>Bucket POST must be of the enclosure-type multipart/form-data</Condition>"),
    *[
        (method, f"/s3/no-such-bucket?{selector}&versionId=x", *_PARAMETER_NO_BUCKET)
        for method, selector in (("PUT", "acl"), ("POST", "restore"))
    ],
    *[
        (method, f"/s3/eng-artifacts?{selector}&versionId=x", *_BAD_VERSION_ID_X)
        for method, selector in (("PUT", "acl"), ("POST", "restore"))
    ],
    ("DELETE", "/s3/no-such-bucket?acl&versionId=x", *_VERSION_ID_X),
    ("GET", "/s3/eng-artifacts/no/such.md?website=v", *_WEBSITE_V),
    ("GET", "/s3/no-such-bucket?website=v&website", *_WEBSITE_V),
    ("GET", "/s3/no-such-bucket?website=v&max-keys=abc", *_WEBSITE_V),
    ("GET", "/s3/no-such-bucket?website=v&versioning", 400,
     "Conflicting query string parameters: versioning, website",
     _argued("ResourceType", "versioning")),
    # `annotationName` at a key without `annotation`.
    ("GET", "/s3/eng-artifacts/runbooks/oncall.md?annotationName=v", *_ANNOTATION_NAME_UNEXPECTED),
    ("GET", "/s3/no-such-bucket/k.txt?annotationName=v", *_ANNOTATION_NAME_UNEXPECTED),
    ("GET", "/s3/eng-artifacts/runbooks/oncall.md?annotationName=v&acl", 400,
     "Conflicting query string parameters: acl, annotationName", _argued("ResourceType", "acl")),
    ("GET", "/s3/eng-artifacts/runbooks/oncall.md?annotationName=v&website=v", 400,
     "Conflicting query string parameters: annotationName, website",
     _argued("ResourceType", "annotationName")),
    # ListObjectVersions' own, before the bucket and after it.
    ("GET", "/s3/no-such-bucket?versions&max-keys=abc", *_MAX_KEYS_ABC),
    ("GET", "/s3/no-such-bucket?versions&version-id-marker=x", *_MARKER_WITHOUT_KEY),
    ("GET", "/s3/no-such-bucket?versions&key-marker=a&version-id-marker=", 400,
     "A version-id marker cannot be empty.", _argued("version-id-marker", "")),
    ("GET", "/s3/no-such-bucket?versions&key-marker=a&version-id-marker=x", *_PARAMETER_NO_BUCKET),
    ("GET", "/s3/eng-artifacts?versions&key-marker=a&version-id-marker=x", 400, _BAD_VERSION,
     _argued("version-id-marker", "x")),
    ("GET", "/s3/eng-artifacts?versions&key-marker=a.txt&version-id-marker=NULL", 400, _BAD_VERSION,
     _argued("version-id-marker", "NULL")),
    ("GET", "/s3/no-such-bucket?versions&encoding-type=bogus", *_PARAMETER_NO_BUCKET),
    ("GET", "/s3/eng-artifacts?versions&encoding-type=bogus", *_BOGUS_ENCODING),
    ("GET", "/s3/eng-artifacts?versions&encoding-type=", 400,
     "Invalid Encoding Method specified in Request", _argued("encoding-type", "")),
    ("GET", "/s3/no-such-bucket?versions&max-keys=-1", *_PARAMETER_NO_BUCKET),
    ("GET", "/s3/eng-artifacts?versions&max-keys=-1", 400, "max-keys cannot be negative",
     _argued("max-keys")),
    ("GET", "/s3/eng-artifacts?versions&max-keys=abc&encoding-type=bogus", *_MAX_KEYS_ABC),
    ("GET", "/s3/eng-artifacts?versions&encoding-type=bogus&version-id-marker=x",
     *_MARKER_WITHOUT_KEY),
    ("GET", "/s3/eng-artifacts?versions&max-keys=abc&version-id-marker=x", *_MAX_KEYS_ABC),
    ("GET", "/s3/eng-artifacts?versions&max-keys=-1&encoding-type=bogus", *_BOGUS_ENCODING),
    ("GET", "/s3/eng-artifacts?versions&max-keys=-1&version-id-marker=x", *_MARKER_WITHOUT_KEY),
    ("GET", "/s3/eng-artifacts?versions&key-marker=a.txt&version-id-marker=garbage&max-keys=abc",
     *_MAX_KEYS_ABC),
    ("GET",
     "/s3/eng-artifacts?versions&key-marker=a.txt&version-id-marker=garbage&encoding-type=bogus",
     400, _BAD_VERSION, _argued("version-id-marker", "garbage")),
    ("GET", "/s3/eng-artifacts?versions&key-marker=a.txt&version-id-marker=&max-keys=abc",
     *_MAX_KEYS_ABC),
    ("GET", "/s3/eng-artifacts?versions&max-keys=abc&acl", 400,
     "Conflicting query string parameters: acl, versions", _argued("ResourceType", "acl")),
]
# fmt: on


@pytest.mark.parametrize(
    "method, path, status, message, members",
    _PARAMETER_ROWS,
    ids=[f"{r[0]}-{r[1].rsplit('/', 1)[-1]}" for r in _PARAMETER_ROWS],
)
def test_s3_a_parameter_real_refuses_is_refused_with_reals_message_and_members(
    live_server, method, path, status, message, members
):
    base_url, settings = live_server
    r = _signed(base_url, path, settings.admin_token, method=method)
    assert r.status_code == status
    named = re.search(r"<Message>([^<]*)</Message>(.*)<RequestId>", r.text)
    assert (named[1], named[2]) == (message, members)


@pytest.mark.parametrize(
    "path",
    [
        "/s3/eng-artifacts?versionId=x",
        "/s3/eng-artifacts?website=v",
        "/s3/eng-artifacts/runbooks/oncall.md?website=v",
        "/s3/eng-artifacts/runbooks/oncall.md?annotationName=v",
        "/s3/eng-artifacts?versions&max-keys=abc",
        "/s3/eng-artifacts?versions&version-id-marker=x",
        "/s3/eng-artifacts?versions&key-marker=a&version-id-marker=",
    ],
)
def test_s3_what_a_parameter_is_refused_for_comes_before_the_credential(live_server, path):
    """Real gave each of these its 400 unsigned and over a bad secret as well as signed (measured
    2026-09-29); a HEAD naming it is the 400 without its body, `?versions`' own a HEAD reads as the
    selector's 405 first."""
    import httpx

    base_url, settings = live_server
    signed = _signed(base_url, path, settings.admin_token)
    tampered_url, tampered = _sign_get(base_url, path, settings.admin_token, tamper=True)
    for other in (httpx.get(f"{base_url}{path}"), httpx.get(tampered_url, headers=tampered)):
        assert other.status_code == signed.status_code == 400, path
        assert _without_request_ids(other.text) == _without_request_ids(signed.text), path
    head = _signed(base_url, path, settings.admin_token, method="HEAD")
    assert (head.status_code, head.content) == ((405, b"") if "versions" in path else (400, b""))


@pytest.mark.parametrize(
    "path",
    [
        "/s3/eng-artifacts?versions&key-marker=a&version-id-marker=x",
        "/s3/eng-artifacts?versions&encoding-type=bogus",
        "/s3/eng-artifacts?versions&max-keys=-1",
    ],
)
def test_s3_what_list_object_versions_refuses_after_the_bucket_comes_after_the_credential(
    live_server, path
):
    """The other three of ListObjectVersions' refusals are real's after the bucket, so a signature
    that does not verify is refused first, and an unsigned caller, who sees no bucket here, is told
    there is none."""
    import httpx

    base_url, settings = live_server
    assert _signed(base_url, path, settings.admin_token).status_code == 400
    tampered_url, tampered = _sign_get(base_url, path, settings.admin_token, tamper=True)
    assert "<Code>SignatureDoesNotMatch</Code>" in httpx.get(tampered_url, headers=tampered).text
    assert "<Code>NoSuchBucket</Code>" in httpx.get(f"{base_url}{path}").text


# Every kind of answer a HEAD gets, the token it is sent with ("tampered": signed, then the
# signature spoiled; None: unsigned), and the length it declares, which only an object's own
# answers do.
# fmt: off
_HEAD_ROWS = [
    ("/s3/eng-artifacts", "admin", {}, 200, None),
    ("/s3/no-such-bucket", "admin", {}, 404, None),
    ("/s3/eng-artifacts/no/such.md", "admin", {}, 404, None),
    ("/s3/no-such-bucket/a/b.txt", "admin", {}, 404, None),
    ("/s3/eng-artifacts?versioning", "admin", {}, 405, None),
    ("/s3/eng-artifacts?acl&versioning", "admin", {}, 400, None),
    (OBJECT_PATH, "admin", {"Range": "bytes=99999-"}, 416, None),
    ("/s3/eng-artifacts", "tampered", {}, 403, None),
    ("/s3/eng-artifacts", None, {}, 404, None),
    (OBJECT_PATH, "tampered", {}, 403, None),
    (OBJECT_PATH, None, {}, 404, None),
    ("/s3/eng-artifacts?max-keys=abc", "admin", {}, 400, None),
    ("/s3/eng-artifacts", "admin", {"x-amz-content-sha256": _TRAILER}, 400, None),
    ("/s3/eng-artifacts/no/such.md", "admin", {"x-amz-content-sha256": _TRAILER}, 400, None),
    ("/s3/no-such-bucket", "admin", {"x-amz-content-sha256": _TRAILER}, 404, None),
    ("/s3/eng-artifacts", "admin", {"x-amz-content-sha256": "garbage"}, 400, None),
    ("/s3/eng-artifacts", None, {"Authorization": "Bearer abc"}, 400, None),
    ("/s3/no-such-bucket?list-type=2&marker=x", "admin", {}, 400, None),
    ("/s3/eng-artifacts?encoding-type=bogus", "admin", {}, 400, None),
    ("/s3/eng-artifacts?list-type=2&continuation-token=garbage", "admin", {}, 400, None),
    ("/s3/no-such-bucket?encoding-type=bogus", "admin", {}, 404, None),
    ("/s3/eng-artifacts?max-keys=-1", "admin", {}, 200, None),
    ("/s3/eng-artifacts?partNumber=1", "admin", {}, 400, None),
    (OBJECT_PATH, "admin", {}, 200, len(OBJECT_TEXT)),
    (OBJECT_PATH, "admin", {"Range": "bytes=0-9"}, 206, 10),
    # GetObject's `partNumber` and the parses a HEAD at a key makes before the bucket, each as real
    # answered it (2026-09-29): the object's one part, a part it has none of, a number out of range,
    # one beside a `Range`, and in a bucket that does not exist the parses and not the rest.
    (f"{OBJECT_PATH}?partNumber=1", "admin", {}, 206, len(OBJECT_TEXT)),
    (f"{OBJECT_PATH}?partNumber=2", "admin", {}, 416, None),
    (f"{OBJECT_PATH}?partNumber=abc", "admin", {}, 400, None),
    (f"{OBJECT_PATH}?partNumber=1", "admin", {"Range": "bytes=0-1"}, 400, None),
    ("/s3/eng-artifacts/no/such.md?partNumber=abc", "admin", {}, 400, None),
    ("/s3/eng-artifacts/no/such.md?partNumber=2", "admin", {}, 404, None),
    ("/s3/no-such-bucket/a.txt?partNumber=abc", "admin", {}, 404, None),
    ("/s3/no-such-bucket/a.txt?versionId=", "admin", {}, 400, None),
    ("/s3/no-such-bucket/a.txt?versionId", "admin", {}, 400, None),
    ("/s3/no-such-bucket/a.txt?versionId=garbage", "admin", {}, 404, None),
    ("/s3/no-such-bucket/a.txt?torrent&versionId=x", "admin", {}, 400, None),
    ("/s3/no-such-bucket/a.txt?attributes", "admin", {"x-amz-object-attributes": "garbage"}, 400,
     None),
    ("/s3/no-such-bucket/a.txt?attributes", "admin", {}, 405, None),
    ("/s3/no-such-bucket/a.txt?annotation&max-annotation-results=abc", "admin", {}, 405, None),
    # A checksum mode, and two parses a HEAD at a key makes before the bucket (2026-09-30).
    (OBJECT_PATH, "admin", {"x-amz-checksum-mode": "ENABLED"}, 200, len(OBJECT_TEXT)),
    (OBJECT_PATH, "admin", {"x-amz-checksum-mode": "garbage"}, 400, None),
    ("/s3/eng-artifacts/no/such.md", "admin", {"x-amz-checksum-mode": "garbage"}, 404, None),
    (f"{OBJECT_PATH}?acl", "admin", {"x-amz-checksum-mode": "garbage"}, 405, None),
    ("/s3/no-such-bucket/a.txt?versioning&versionId=x", "admin", {}, 400, None),
    ("/s3/no-such-bucket/a.txt?attributes", "admin", {"x-amz-object-attributes": ","}, 405, None),
]
# fmt: on


@pytest.mark.parametrize(
    "path, token, headers, status, length",
    _HEAD_ROWS,
    ids=[f"{r[3]}-{r[1]}-{r[0].rsplit('/', 1)[-1]}" for r in _HEAD_ROWS],
)
def test_s3_a_head_is_sent_chunked_as_xml_unless_it_is_the_objects_own(
    live_server, path, token, headers, status, length
):
    """Every answer but an object's 200 and 206 is framed as ``backlot.routers.s3._head`` frames
    it, and a HEAD at a bucket reads the listing's parameters as ``backlot.routers.s3.head_bucket``
    says. Read off a real uvicorn server, since the framing is what it writes; the service root's
    is asserted above."""
    import httpx

    base_url, settings = live_server
    if token is None:
        r = httpx.head(f"{base_url}{path}", headers=headers)
    else:
        url, signed = _sign_get(
            base_url,
            path,
            settings.admin_token,
            tamper=token == "tampered",
            extra_headers=headers,
            method="HEAD",
        )
        r = httpx.head(url, headers=signed)
    assert r.status_code == status and r.content == b""
    if length is None:
        assert "content-length" not in r.headers and r.headers["transfer-encoding"] == "chunked"
        assert r.headers["content-type"] == "application/xml"
        # Nor a range, on a HEAD as on a GET (``backlot.routers.s3.object_get``).
        assert "content-range" not in r.headers
    else:
        assert r.headers["content-length"] == str(length) and "transfer-encoding" not in r.headers
        assert r.headers["content-type"] == "text/markdown"


# ------------------------------------------------------------------------ ListMultipartUploads
# Every real S3 answer below was measured on 2026-09-10 (the negative and the repeated values on
# 2026-09-11) against a general purpose bucket in ap-northeast-2 with no upload in progress,
# path-style, SigV4, the query encoded the way `_sign_get` encodes it (form-decoded, then `quote`d),
# so a `+` or `%25` in a value reached real the way it reaches the server here.

_EMPTY_UPLOADS_PAGE = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<ListMultipartUploadsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<Bucket>eng-artifacts</Bucket><KeyMarker></KeyMarker><UploadIdMarker></UploadIdMarker>"
    b"<NextKeyMarker></NextKeyMarker><NextUploadIdMarker></NextUploadIdMarker>"
    b"<MaxUploads>1000</MaxUploads><IsTruncated>false</IsTruncated></ListMultipartUploadsResult>"
)


def _get_raw(base_url, path, token):
    url, headers = _sign_get(base_url, path, token)
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
        return r.status, r.headers, r.read()


def test_list_multipart_uploads_is_the_empty_page_real_serves_byte_for_byte(live_server):
    """A browsing client sends `?uploads` after every ListObjectsV2, unprompted, and real answers it
    with a 200. The body is real's for a bucket with no upload in progress, with the bucket's name
    swapped in: the two markers and the two next-markers present and empty, MaxUploads at the
    default, IsTruncated false, no Upload element — and no Prefix, Delimiter or EncodingType when
    none was sent."""
    base_url, settings = live_server
    for query in ("?uploads", "?uploads="):  # with and without the `=`; real treats both alike
        status, headers, body = _get_raw(
            base_url, f"/s3/eng-artifacts{query}", settings.admin_token
        )
        assert status == 200 and headers.get("Content-Type") == "application/xml", query
        assert body == _EMPTY_UPLOADS_PAGE, query


def _uploads_fields(base_url, query, token):
    """The result's children as (tag, text) in document order, tags without the namespace."""
    root = _get_xml(base_url, f"/s3/eng-artifacts?{query}", token)
    assert root.tag == f"{NS}ListMultipartUploadsResult"
    return [(child.tag[len(NS) :], child.text or "") for child in root]


def test_list_multipart_uploads_echoes_what_was_sent_in_reals_order(live_server):
    base_url, settings = live_server
    token = settings.admin_token
    fixed_head = [
        ("Bucket", "eng-artifacts"),
        ("KeyMarker", ""),
        ("UploadIdMarker", ""),
        ("NextKeyMarker", ""),
        ("NextUploadIdMarker", ""),
    ]
    # The two queries a browsing client sends: a folder listing's `?uploads` carries the delimiter,
    # and a prefix's carries both. Delimiter comes before Prefix on real, and both come before
    # MaxUploads.
    assert _uploads_fields(base_url, "delimiter=%2F&uploads=", token) == fixed_head + [
        ("Delimiter", "/"),
        ("MaxUploads", "1000"),
        ("IsTruncated", "false"),
    ]
    assert _uploads_fields(
        base_url, "encoding-type=url&prefix=runbooks%2F&delimiter=%2F&uploads=", token
    ) == fixed_head + [
        ("Delimiter", "/"),
        ("Prefix", "runbooks/"),
        ("MaxUploads", "1000"),
        ("EncodingType", "url"),
        ("IsTruncated", "false"),
    ]
    # An empty prefix, delimiter or key-marker is not echoed, as if it had not been sent.
    assert _uploads_fields(base_url, "uploads&prefix=&delimiter=&key-marker=", token) == (
        fixed_head + [("MaxUploads", "1000"), ("IsTruncated", "false")]
    )
    # key-marker is echoed; upload-id-marker alone is ignored, as the API reference says.
    assert _uploads_fields(base_url, "uploads&key-marker=abc", token)[1] == ("KeyMarker", "abc")
    assert _uploads_fields(base_url, "uploads&upload-id-marker=xyz", token) == fixed_head + [
        ("MaxUploads", "1000"),
        ("IsTruncated", "false"),
    ]
    # max-uploads: read for its value and served at 1000 past it. Real judges the value and not
    # the length of the digits or the sign, so any run of leading zeros comes off first — twenty of
    # them ahead of a 5 is 5, five thousand of them alone is 0, `-0` and `-00` are 0 where `-1` is
    # refused below, and `00002147483647` is in range where `00002147483648` is refused below (all
    # measured).
    for sent, echoed in (
        ("5", "5"),
        ("05", "5"),
        ("0", "0"),
        ("-0", "0"),
        ("-00", "0"),
        ("00000000005", "5"),
        ("0" * 20 + "5", "5"),
        ("0" * 5000, "0"),
        ("0000000000", "0"),
        ("00002147483647", "1000"),
        ("1001", "1000"),
        ("2000", "1000"),
    ):
        fields = dict(_uploads_fields(base_url, f"uploads&max-uploads={sent}", token))
        assert fields["MaxUploads"] == echoed, sent


def test_list_multipart_uploads_encodes_under_encoding_type_url_as_real_does(live_server):
    base_url, settings = live_server
    token = settings.admin_token
    # Without encoding-type the values come back as received, `+` and `%25` decoded by the server.
    fields = dict(
        _uploads_fields(base_url, "uploads&prefix=run books/x+y%25z&key-marker=k m", token)
    )
    assert fields["Prefix"] == "run books/x y%z" and fields["KeyMarker"] == "k m"
    # With it: space is `+`, `/` `-` `_` `.` `*` stay, the other punctuation sent is `%XX` in upper
    # case, and `URL` is taken like `url` and echoed as sent.
    fields = dict(
        _uploads_fields(
            base_url,
            "uploads&prefix=run books/x+y%25z!*'()~-_.,;:@=&key-marker=k m&delimiter=|"
            "&encoding-type=URL",
            token,
        )
    )
    assert fields["Prefix"] == "run+books/x+y%25z%21*%27%28%29%7E-_.%2C%3B%3A%40%3D"
    assert fields["KeyMarker"] == "k+m"
    assert fields["Delimiter"] == "%7C"
    assert fields["EncodingType"] == "URL"
    fields = dict(_uploads_fields(base_url, "uploads&prefix=한글/&encoding-type=url", token))
    assert fields["Prefix"] == "%ED%95%9C%EA%B8%80/"


def _invalid_argument(err: urllib.error.HTTPError, message: str, name: str, value: str) -> None:
    body = err.read()
    assert err.code == 400, body
    assert b"<Code>InvalidArgument</Code>" in body
    assert f"<Message>{message}</Message>".encode() in body, body
    assert (
        f"<ArgumentName>{name}</ArgumentName><ArgumentValue>{value}</ArgumentValue>".encode()
        in body
    ), body


def test_list_multipart_uploads_refuses_what_real_refuses_with_reals_messages(live_server):
    base_url, settings = live_server
    token = settings.admin_token
    uploads = "/s3/eng-artifacts?uploads"
    # max-uploads: not `int()`, which would take ` 5` and any size. (A `+5` on the wire is ` 5` by
    # the time it is parsed, on real and here alike: `_sign_get` decodes it as the form encoding.)
    # The two messages split on whether the value fits an int32: `-2147483648` does and is out of
    # range, `-2147483649` does not and is "not an integer", like `2147483648` on the other side.
    not_an_integer = "Provided max-uploads not an integer or within integer range"
    for value in (
        "abc",
        "2147483648",
        "00002147483648",
        " 5",
        "9" * 5000,
        "-2147483649",
        "-" + "9" * 20,
        "-abc",
        "-",
    ):
        err = _refused(base_url, f"{uploads}&max-uploads={value}", token)
        _invalid_argument(err, not_an_integer, "max-uploads", value)
    # The range message names the value as parsed, `-01` as `-1`, where the other names it as sent.
    out_of_range = "Argument max-uploads must be an integer between 0 and 2147483647"
    for sent, named in (("-1", "-1"), ("-2147483648", "-2147483648"), ("-01", "-1")):
        err = _refused(base_url, f"{uploads}&max-uploads={sent}", token)
        _invalid_argument(err, out_of_range, "max-uploads", named)
    # encoding-type: anything but `url`, the empty value included.
    for value in ("bogus", ""):
        err = _refused(base_url, f"{uploads}&encoding-type={value}", token)
        _invalid_argument(
            err, "Invalid Encoding Method specified in Request", "encoding-type", value
        )
    # upload-id-marker beside a key-marker: no id names an upload here, so every one is refused.
    err = _refused(base_url, f"{uploads}&key-marker=abc&upload-id-marker=xyz", token)
    _invalid_argument(err, "Invalid uploadId marker", "upload-id-marker", "xyz")
    # In real's order: max-uploads is parsed before the bucket is looked up, and its range,
    # encoding-type and the markers are checked after it — encoding-type first, then the range,
    # then the markers.
    err = _refused(base_url, "/s3/no-such-bucket?uploads&max-uploads=abc", token)
    _invalid_argument(err, not_an_integer, "max-uploads", "abc")
    for query in ("max-uploads=-1", "encoding-type=bogus", "key-marker=a&upload-id-marker=b"):
        err = _refused(base_url, f"/s3/no-such-bucket?uploads&{query}", token)
        assert err.code == 404 and b"NoSuchBucket" in err.read(), query
    err = _refused(base_url, f"{uploads}&max-uploads=abc&encoding-type=bogus", token)
    _invalid_argument(err, not_an_integer, "max-uploads", "abc")
    for query in (
        "encoding-type=bogus&key-marker=a&upload-id-marker=b",
        "max-uploads=-1&encoding-type=bogus",
        "max-uploads=-1&encoding-type=bogus&key-marker=a&upload-id-marker=b",
    ):
        err = _refused(base_url, f"{uploads}&{query}", token)
        _invalid_argument(
            err, "Invalid Encoding Method specified in Request", "encoding-type", "bogus"
        )
    err = _refused(base_url, f"{uploads}&max-uploads=-1&key-marker=a&upload-id-marker=b", token)
    _invalid_argument(err, out_of_range, "max-uploads", "-1")


def test_list_multipart_uploads_reads_the_first_of_a_repeated_parameter_as_real_does(live_server):
    """Real reads the first value of a parameter sent twice, so the same two values in opposite
    order land on opposite statuses; Starlette's `QueryParams.get` would read the last and land
    each on the other status."""
    base_url, settings = live_server
    token = settings.admin_token
    uploads = "/s3/eng-artifacts?uploads"
    fields = dict(_uploads_fields(base_url, "uploads&max-uploads=1&max-uploads=abc", token))
    assert fields["MaxUploads"] == "1"
    err = _refused(base_url, f"{uploads}&max-uploads=abc&max-uploads=1", token)
    _invalid_argument(
        err, "Provided max-uploads not an integer or within integer range", "max-uploads", "abc"
    )
    fields = dict(_uploads_fields(base_url, "uploads&encoding-type=url&encoding-type=bogus", token))
    assert fields["EncodingType"] == "url"
    err = _refused(base_url, f"{uploads}&encoding-type=bogus&encoding-type=url", token)
    _invalid_argument(err, "Invalid Encoding Method specified in Request", "encoding-type", "bogus")
    # An empty first upload-id-marker is the ignored one; the `x` sent after it is not read.
    fields = dict(
        _uploads_fields(
            base_url, "uploads&key-marker=k&upload-id-marker=&upload-id-marker=x", token
        )
    )
    assert fields["KeyMarker"] == "k"
    err = _refused(base_url, f"{uploads}&key-marker=k&upload-id-marker=x&upload-id-marker=", token)
    _invalid_argument(err, "Invalid uploadId marker", "upload-id-marker", "x")
    fields = dict(
        _uploads_fields(
            base_url,
            "uploads&prefix=a&prefix=b&delimiter=/&delimiter=|&key-marker=a&key-marker=b",
            token,
        )
    )
    assert (fields["Prefix"], fields["Delimiter"], fields["KeyMarker"]) == ("a", "/", "a")


def test_a_continuation_token_is_refused_after_the_bucket_lookup_not_before_it(live_server):
    """The refusal cannot be read as "this bucket exists", for any caller.

    Real puts the check below the lookup — `?list-type=2&continuation-token=garbage` on a bucket
    that does not exist is NoSuchBucket rather than the 400, and an empty value goes the same way
    (measured 2026-09-17). Here the bucket a caller cannot see is the one that tells them apart:
    `people-vault` holds one group-visible object, so an engineer is told it does not exist while
    the admin gets the 400 the token earns.
    """
    base_url, settings = live_server
    tokens = {
        u["email"]: u["token"] for u in yaml.safe_load(settings.tokens_path.read_text())["users"]
    }
    for value in ("garbage", ""):
        path = f"/s3/people-vault?list-type=2&continuation-token={value}"
        scoped = _refused(base_url, path, tokens["ava@acme.com"])
        assert scoped.code == 404 and b"<Code>NoSuchBucket</Code>" in scoped.read(), value
        admin = _refused(base_url, path, settings.admin_token)
        body = admin.read()
        assert admin.code == 400, value
        assert b"<Message>The continuation token provided is incorrect</Message>" in body, value


def _boto3_client(live_server):
    """An admin boto3 client at the live server, path-addressed, the way every boto3 test here
    dials it."""
    boto3 = pytest.importorskip("boto3")
    from botocore.config import Config

    base_url, settings = live_server
    return boto3.client(
        "s3",
        endpoint_url=f"{base_url}/s3",
        aws_access_key_id=synth.s3_access_key_id(settings.admin_token),
        aws_secret_access_key=synth.s3_secret_access_key(settings.admin_token),
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )


def test_boto3_gets_one_client_error_for_a_bad_continuation_token_not_page_one(live_server):
    """The same refusal, from the client side.

    A paging loop that stores its cursor between runs, or passes it through a URL or a queue, gets
    one `ClientError` for a mangled cursor and no page: botocore reads the 400 as that error and
    does not retry it, which is what real answers the cursor with.
    """
    s3 = _boto3_client(live_server)
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError) as raised:
        s3.list_objects_v2(Bucket="eng-artifacts", ContinuationToken="garbage")
    error = raised.value.response
    assert error["ResponseMetadata"]["HTTPStatusCode"] == 400
    assert error["ResponseMetadata"]["RetryAttempts"] == 0
    assert error["Error"]["Code"] == "InvalidArgument"
    assert error["Error"]["Message"] == "The continuation token provided is incorrect"
    # The cursor a page hands out still walks, so what was refused is the mangling and not paging.
    first = s3.list_objects_v2(Bucket="eng-artifacts", MaxKeys=1)
    second = s3.list_objects_v2(
        Bucket="eng-artifacts", MaxKeys=1, ContinuationToken=first["NextContinuationToken"]
    )
    assert second["ContinuationToken"] == first["NextContinuationToken"]
    assert second["Contents"][0]["Key"] != first["Contents"][0]["Key"]


def test_list_multipart_uploads_on_a_bucket_the_caller_cannot_see_is_no_such_bucket(live_server):
    """The listing and `?uploads` agree about which buckets exist: `people-vault` holds one
    group-visible object, so an engineer is told it does not exist, as the listing tells them."""
    base_url, settings = live_server
    tokens = {
        u["email"]: u["token"] for u in yaml.safe_load(settings.tokens_path.read_text())["users"]
    }
    assert _get_xml(base_url, "/s3/people-vault?uploads", settings.admin_token).tag == (
        f"{NS}ListMultipartUploadsResult"
    )
    err = _refused(base_url, "/s3/people-vault?uploads", tokens["ava@acme.com"])
    assert err.code == 404 and b"NoSuchBucket" in err.read()
    assert _get_xml(base_url, "/s3/eng-artifacts?uploads", tokens["ava@acme.com"]).tag == (
        f"{NS}ListMultipartUploadsResult"
    )


def test_boto3_head_bucket_reads_the_bucket_arn_and_that_it_is_no_access_point_alias(live_server):
    """The two headers ``backlot.routers.s3.head_bucket`` sends beside the region, which boto3
    returns as its own keys."""
    head = _boto3_client(live_server).head_bucket(Bucket="eng-artifacts")
    assert head["BucketArn"] == "arn:aws:s3:::eng-artifacts"
    assert head["BucketRegion"] == "us-east-1" and head["AccessPointAlias"] is False


def test_boto3_list_multipart_uploads_is_an_empty_page_not_a_client_error(live_server):
    s3 = _boto3_client(live_server)
    page = s3.list_multipart_uploads(Bucket="eng-artifacts", Prefix="runbooks/", Delimiter="/")
    assert page["ResponseMetadata"]["HTTPStatusCode"] == 200
    assert page["Bucket"] == "eng-artifacts" and page["Prefix"] == "runbooks/"
    assert page["Delimiter"] == "/" and page["MaxUploads"] == 1000
    assert page["IsTruncated"] is False and "Uploads" not in page


def test_boto3_list_objects_paginator_walks_the_bucket_and_keeps_marker_and_owner(live_server):
    """The two listings, from the client side.

    Against one body for both listings `list_objects` died on the first page — botocore's V1
    paginator falls back to the last key as the next `Marker`, the server ignored it and sent the
    same page again, and the walk raised `PaginationError: The same next token was received twice`.
    It also dropped `Marker` and every `Owner` from the output, because botocore keeps only the
    members the V1 output shape declares. Both listings now walk the bucket, and `list_objects`
    carries the two members again."""
    s3 = _boto3_client(live_server)
    walks = {}
    for operation in ("list_objects", "list_objects_v2"):
        pages = list(
            s3.get_paginator(operation).paginate(
                Bucket="eng-artifacts", PaginationConfig={"PageSize": 1}
            )
        )
        walks[operation] = [o["Key"] for page in pages for o in page.get("Contents", [])]
        assert len(pages) == len(walks[operation]), operation
    assert walks["list_objects"] == walks["list_objects_v2"] != []
    page = s3.list_objects(Bucket="eng-artifacts", MaxKeys=1)
    assert page["Marker"] == "" and page["Contents"][0]["Owner"]["ID"]
    assert "Owner" not in s3.list_objects_v2(Bucket="eng-artifacts", MaxKeys=1)["Contents"][0]


def test_boto3_reads_a_buckets_configuration_and_an_objects_and_gets_one_client_error_for_a_write(
    live_server,
):
    """What boto3 makes of each: a bucket's configurations and an object's sub-resources parse to
    what they parse to where nobody configured them, the absent ones as the error botocore models
    for each, and a write, which this server does not serve, is one ClientError at once — 501 is
    not a status botocore retries."""
    s3 = _boto3_client(live_server)
    from botocore.exceptions import ClientError

    bucket, key = "eng-artifacts", "runbooks/oncall.md"
    assert "Status" not in s3.get_bucket_versioning(Bucket=bucket)
    assert s3.get_bucket_acl(Bucket=bucket)["Grants"][0]["Permission"] == "FULL_CONTROL"
    rule = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]["Rules"][0]
    assert rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    assert s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True,
    }
    assert s3.get_bucket_request_payment(Bucket=bucket)["Payer"] == "BucketOwner"
    assert s3.list_bucket_inventory_configurations(Bucket=bucket)["IsTruncated"] is False
    versions = s3.list_object_versions(Bucket=bucket)["Versions"]
    assert {(v["Key"], v["VersionId"], v["IsLatest"]) for v in versions} == {
        (o["Key"], "null", True) for o in s3.list_objects_v2(Bucket=bucket)["Contents"]
    }
    for call, code in (
        (lambda: s3.get_bucket_policy(Bucket=bucket), "NoSuchBucketPolicy"),
        (lambda: s3.get_bucket_tagging(Bucket=bucket), "NoSuchTagSet"),
        (lambda: s3.get_bucket_cors(Bucket=bucket), "NoSuchCORSConfiguration"),
        (
            lambda: s3.get_bucket_lifecycle_configuration(Bucket=bucket),
            "NoSuchLifecycleConfiguration",
        ),
    ):
        with pytest.raises(ClientError) as e:
            call()
        assert e.value.response["Error"]["Code"] == code
        assert e.value.response["ResponseMetadata"]["RetryAttempts"] == 0
    assert s3.get_object_tagging(Bucket=bucket, Key=key)["TagSet"] == []
    assert s3.get_object_acl(Bucket=bucket, Key=key)["Grants"][0]["Permission"] == "FULL_CONTROL"
    attributes = s3.get_object_attributes(
        Bucket=bucket, Key=key, ObjectAttributes=["ETag", "ObjectSize", "StorageClass"]
    )
    assert attributes["ETag"] == hashlib.md5(OBJECT_TEXT).hexdigest()
    assert (attributes["ObjectSize"], attributes["StorageClass"]) == (len(OBJECT_TEXT), "STANDARD")
    assert attributes["LastModified"] == s3.head_object(Bucket=bucket, Key=key)["LastModified"]
    for call, code in (
        (lambda: s3.get_object_legal_hold(Bucket=bucket, Key=key), "InvalidRequest"),
        (lambda: s3.get_object_retention(Bucket=bucket, Key=key), "InvalidRequest"),
        (lambda: s3.get_object_torrent(Bucket=bucket, Key=key), "MethodNotAllowed"),
        (lambda: s3.get_object_tagging(Bucket=bucket, Key="no/such.md"), "NoSuchKey"),
    ):
        with pytest.raises(ClientError) as e:
            call()
        assert e.value.response["Error"]["Code"] == code
        assert e.value.response["ResponseMetadata"]["RetryAttempts"] == 0
    with pytest.raises(ClientError) as e:
        s3.put_object_tagging(Bucket=bucket, Key=key, Tagging={"TagSet": []})
    assert e.value.response["Error"]["Code"] == "NotImplemented"
    assert e.value.response["ResponseMetadata"]["HTTPStatusCode"] == 501
    assert e.value.response["ResponseMetadata"]["RetryAttempts"] == 0
    # No upload is ever in progress, so ListParts is real's answer for an upload id it lacks.
    with pytest.raises(s3.exceptions.NoSuchUpload) as e:
        s3.list_parts(Bucket=bucket, Key=key, UploadId="abc123")
    assert e.value.response["Error"]["UploadId"] == "abc123"
    assert e.value.response["ResponseMetadata"]["RetryAttempts"] == 0
    assert s3.get_bucket_location(Bucket=bucket)["LocationConstraint"] is None  # us-east-1
    assert key in {o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"]}
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == OBJECT_TEXT


# --- what real checks of a write before it performs it -----------------------------------------


def _md5(body: bytes) -> str:
    return base64.b64encode(hashlib.md5(body).digest()).decode()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


_GOOD_DELETE = b"<Delete><Object><Key>backlot-no-such-key</Key></Object></Delete>"
_EMPTY_KEY = b"<Delete><Object><Key></Key></Object></Delete>"
_EMPTY_SECOND_KEY = b"<Delete><Object><Key>x</Key></Object><Object><Key></Key></Object></Delete>"
_SSE_S3 = b"<ObjectEncryption><SSE-S3/></ObjectEncryption>"
_NO_TAGS = b"<Tagging><TagSet/></Tagging>"
_KMS_KEY_X = "arn:aws:kms:us-east-1:111111111111:key/x"
_MALFORMED_XML = (
    "The XML you provided was not well-formed or did not validate against our published schema"
)
_MISSING_CHECKSUM = "Missing required header for this request: Content-MD5 OR x-amz-checksum-*"
_USER_KEY = "User key must be specified."
_KMS = "Requests modifying object encryption configuration to SSE-KMS require a"
_BAD_KMS_FORMAT = (
    "Invalid KMS Key ARN format. You must provide the full KMS Key ARN to make an "
    "UpdateObjectEncryption request"
)


_CHECKSUM_NAMES = (
    "crc32",
    "crc32c",
    "crc64nvme",
    "md5",
    "sha1",
    "sha256",
    "sha512",
    "xxhash64",
    "xxhash3",
    "xxhash128",
)


def _right_checksum(name: str, body: bytes) -> bytes:
    """Each checksum of ``body`` as real took it (2026-09-29): the standard library's, the
    xxhash library's digest, and the router's two CRCs, which the test below checks on their own."""
    import xxhash

    from backlot.routers import s3 as s3_router

    if name == "crc32":
        return zlib.crc32(body).to_bytes(4, "big")
    if name in ("crc32c", "crc64nvme"):
        return s3_router._checksum(name, body)
    xx = {"xxhash64": xxhash.xxh64, "xxhash3": xxhash.xxh3_64, "xxhash128": xxhash.xxh3_128}
    return xx[name](body).digest() if name in xx else hashlib.new(name, body).digest()


_ZEROS = "0" * 64
_ZEROS_SENT = {"x-amz-content-sha256": _ZEROS}
_NEW = "/s3/eng-artifacts/backlot-new.txt"
_WRITTEN = "A method you provided writes to the corpus, which this server does not implement: "
_TRAILER_REFUSED = "The value of x-amz-content-sha256 header is invalid."
_NO_LENGTH = "You must provide the Content-Length HTTP header."
_BAD_MD5 = "The Content-MD5 you specified was invalid."
_MD5_MISMATCH = "The Content-MD5 you specified did not match what we received."
_SDK_ALONE = (
    "x-amz-sdk-checksum-algorithm specified, but no corresponding x-amz-checksum-* or "
    "x-amz-trailer headers were found."
)


# Each builder fills the template real answered with, and nothing else: the part of the request the
# answer names comes from the row, never a choice of code or message from it.
def _invalid_request(message: str) -> tuple[int, str, str, str]:
    return (400, "InvalidRequest", message, "")


def _unread_checksum(name: str) -> tuple[int, str, str, str]:
    """A checksum header real could not read."""
    return _invalid_request(f"Value for x-amz-checksum-{name} header is invalid.")


def _wrong_checksum(name: str) -> tuple[int, str, str, str]:
    """A checksum the body does not match, named in capitals and without members."""
    return (
        400,
        "BadDigest",
        f"The {name.upper()} you specified did not match the calculated checksum.",
        "",
    )


def _invalid_digest(sent: str) -> tuple[int, str, str, str]:
    """A `Content-MD5` real could not read, named as sent."""
    return (400, "InvalidDigest", _BAD_MD5, f"<Content-MD5>{sent}</Content-MD5>")


def _bad_digest(expected: str, body: bytes = _GOOD_DELETE) -> tuple[int, str, str, str]:
    """A `Content-MD5` the body does not match: the body's digest, and the one real names as
    expected, which is the header as sent, or its hex for PutObject (2026-09-29)."""
    return (
        400,
        "BadDigest",
        _MD5_MISMATCH,
        f"<CalculatedDigest>{_md5(body)}</CalculatedDigest>"
        f"<ExpectedDigest>{expected}</ExpectedDigest>",
    )


def _sha_mismatch(body: bytes, sent: str = _ZEROS) -> tuple[int, str, str, str]:
    """`XAmzContentSHA256Mismatch`: the hash sent, and the body's."""
    return (
        400,
        "XAmzContentSHA256Mismatch",
        "The provided 'x-amz-content-sha256' header does not match what was computed.",
        f"<ClientComputedContentSHA256>{sent}</ClientComputedContentSHA256>"
        f"<S3ComputedContentSHA256>{hashlib.sha256(body).hexdigest()}</S3ComputedContentSHA256>",
    )


def _wrote(method: str) -> tuple[int, str, str, str]:
    """This server's 501 where real performed the write."""
    return (501, "NotImplemented", _WRITTEN + method, "")


def _crc32(body: bytes) -> str:
    return _b64(zlib.crc32(body).to_bytes(4, "big"))


def _chunked(data: bytes) -> bytes:
    """An aws-chunked body of one chunk, trailed by its CRC32."""
    return (
        f"{len(data):x}\r\n".encode()
        + data
        + f"\r\n0\r\nx-amz-checksum-crc32:{_crc32(data)}\r\n\r\n".encode()
    )


def _encryption(arn=None, bucket_key=None, kind="SSE-KMS") -> bytes:
    inner = "" if arn is None else f"<KMSKeyArn>{arn}</KMSKeyArn>"
    inner += "" if bucket_key is None else f"<BucketKeyEnabled>{bucket_key}</BucketKeyEnabled>"
    return f"<ObjectEncryption><{kind}>{inner}</{kind}></ObjectEncryption>".encode()


# The requests, headers and answers the rows below repeat.
_RESTORE = ("POST", "/s3/eng-artifacts?restore")
_DELETE = ("POST", "/s3/eng-artifacts?delete")
_DELETE_AT_KEY = ("POST", "/s3/eng-artifacts/runbooks/oncall.md?delete")
_ENCRYPT = ("PUT", "/s3/eng-artifacts/runbooks/oncall.md?encryption")
_ENCRYPT_MISSING = ("PUT", "/s3/eng-artifacts/no/such.md?encryption")
_PUT_NEW = ("PUT", _NEW)
_PUT_TAGGING = ("PUT", "/s3/eng-artifacts/runbooks/oncall.md?tagging")
_DELETE_OBJECT = ("DELETE", "/s3/eng-artifacts/runbooks/oncall.md")
_WITH_MD5 = {"Content-MD5": _md5(_GOOD_DELETE)}
_TWO_CHECKSUMS = {"x-amz-checksum-crc32": "AAAAAA==", "x-amz-checksum-crc32c": "AAAAAA=="}
# The headers of an aws-chunked body of eleven bytes.
_CHUNKED = {
    "x-amz-content-sha256": _TRAILER,
    "content-encoding": "aws-chunked",
    "x-amz-decoded-content-length": "11",
}
_NO_USER_KEY = (400, "UserKeyMustBeSpecified", _USER_KEY, "")
_NO_SUCH_BUCKET = (
    404,
    "NoSuchBucket",
    "The specified bucket does not exist",
    "<BucketName>no-such-bucket</BucketName>",
)
_NO_SUCH_KEY = (
    404,
    "NoSuchKey",
    "The specified key does not exist.",
    "<Key>eng-artifacts/no/such.md</Key>",
)
_NO_CHECKSUM = _invalid_request(_MISSING_CHECKSUM)
_EMPTY_BODY = (400, "MissingRequestBodyError", "Request Body is empty", "")
_GARBAGE_MD5 = _invalid_digest("garbage")
_BAD_CRC32 = _unread_checksum("crc32")
_BAD_ALGORITHM = _invalid_request(
    "The algorithm type you specified in x-amz-checksum- header is invalid."
)
_MANY_CHECKSUMS = _invalid_request(
    "Expecting a single x-amz-checksum- header. Multiple checksum Types are not allowed."
)
_SDK_WITHOUT_CHECKSUM = _invalid_request(_SDK_ALONE)
_SDK_ALGORITHM_WRONG = _invalid_request("Value for x-amz-sdk-checksum-algorithm header is invalid.")
_NOT_WELL_FORMED = (400, "MalformedXML", _MALFORMED_XML, "")
_WROTE_POST = _wrote("POST")
_WROTE_PUT = _wrote("PUT")
_ENCRYPTION_MD5_MISMATCH = _bad_digest(_md5(b"o"), _SSE_S3)
_NO_KMS_ARN = _invalid_request(f"{_KMS} target kms key arn.")
_BAD_KMS = _invalid_request(_BAD_KMS_FORMAT)
_LENGTH_REQUIRED = (411, "MissingContentLength", _NO_LENGTH, "")
_TRAILER_REFUSAL = _invalid_request(_TRAILER_REFUSED)

# The request, and real's answer: status, code, message and members (measured 2026-09-29 on a
# bucket this account created, the public bucket, and a name nobody owns). A row whose answer is
# 501 is one real performed, which is the write this server does not do.
# fmt: off
_WRITE_CHECK_ROWS = [
    # A bucket's `POST ?restore`.
    (*_RESTORE, None, {}, *_NO_USER_KEY),
    (*_RESTORE, b"<RestoreRequest><Days>1</Days></RestoreRequest>", {}, *_NO_USER_KEY),
    ("POST", "/s3/no-such-bucket?restore", None, {}, *_NO_SUCH_BUCKET),
    # DeleteObjects, at a bucket's path: the headers.
    (*_DELETE, None, {}, *_NO_CHECKSUM),
    (*_DELETE, _GOOD_DELETE, {}, *_NO_CHECKSUM),
    (*_DELETE, None, {"Content-MD5": _md5(b"")}, *_EMPTY_BODY),
    (*_DELETE, None, {"x-amz-checksum-crc32": "AAAAAA=="}, *_EMPTY_BODY),
    (*_DELETE, None, {"Content-MD5": "garbage"}, *_GARBAGE_MD5),
    (*_DELETE, _GOOD_DELETE, {"Content-MD5": _b64(b"x" * 15)}, *_invalid_digest(_b64(b"x" * 15))),
    (*_DELETE, _GOOD_DELETE, {"Content-MD5": ""}, *_invalid_digest("")),
    (*_DELETE, _GOOD_DELETE, {"x-amz-checksum-crc32": "garbage"}, *_BAD_CRC32),
    (*_DELETE, _GOOD_DELETE, {"x-amz-checksum-crc32": _b64(b"xxx")}, *_BAD_CRC32),
    (*_DELETE, _GOOD_DELETE, {"x-amz-checksum-crc32": ""}, *_BAD_CRC32),
    *[
        (*_DELETE, _GOOD_DELETE, {f"x-amz-checksum-{name}": "garbage"}, *_unread_checksum(name))
        for name in ("crc32c", "crc64nvme", "sha1", "sha256", "sha512", "md5", "xxhash64",
                     "xxhash3")
    ],
    (*_DELETE, _GOOD_DELETE, {"x-amz-checksum-foo": "abc"}, *_BAD_ALGORITHM),
    (*_DELETE, None, {"x-amz-checksum-foo": "x"}, *_BAD_ALGORITHM),
    (*_DELETE, _GOOD_DELETE, {"x-amz-checksum-type": "FULL_OBJECT"}, *_NO_CHECKSUM),
    (*_DELETE, _GOOD_DELETE, {"x-amz-checksum-algorithm": "CRC32"}, *_NO_CHECKSUM),
    (*_DELETE, _GOOD_DELETE, {"x-amz-sdk-checksum-algorithm": "CRC32"}, *_SDK_WITHOUT_CHECKSUM),
    (*_DELETE, _GOOD_DELETE,
     {"x-amz-checksum-crc32": _crc32(_GOOD_DELETE), "x-amz-checksum-sha256": _b64(b"x" * 32)},
     *_MANY_CHECKSUMS),
    # ... and the pairs among them.
    (*_DELETE, None, {"x-amz-checksum-crc32": "garbage"}, *_BAD_CRC32),
    (*_DELETE, b"garbage", {"x-amz-checksum-crc32": "garbage"}, *_BAD_CRC32),
    (*_DELETE, _GOOD_DELETE, {"x-amz-checksum-crc32": "garbage", "Content-MD5": "garbage"},
     *_BAD_CRC32),
    ("POST", "/s3/eng-artifacts?delete&acl", _GOOD_DELETE, {"Content-MD5": "garbage"},
     400, "InvalidArgument", "Conflicting query string parameters: acl, delete",
     _argued("ResourceType", "acl")),
    ("POST", "/s3/no-such-bucket?delete", _GOOD_DELETE, {"Content-MD5": "garbage"},
     *_NO_SUCH_BUCKET),
    # The body.
    *[
        (*_DELETE, body, {"Content-MD5": _md5(body)}, *_NOT_WELL_FORMED)
        for body in (
            b"<Delete/>",
            b"<Delete><Object><Key>k</Key><VersionId>a</VersionId><VersionId>b</VersionId></Object></Delete>",
            b"<Delete><Object><Key>k</Key><ETag>a</ETag><ETag>b</ETag></Object></Delete>",
            b"<Delete><Quiet>true</Quiet></Delete>",
            b"<Delete><Object/></Delete>",
            b"<Delete><Object><Key>k</Key></Object><Nope/></Delete>",
            b"<Delete><Object><Key>k</Key><Nope/></Object></Delete>",
            b"<Delete><Object><Key>k</Key><Key>j</Key></Object></Delete>",
            b"<Delete><Object><Key>k</Key><Size>1</Size></Object></Delete>",
            b"<Delete><Object><Key>k</Key><LastModifiedTime>2020-01-01T00:00:00Z</LastModifiedTime></Object></Delete>",
            b"<Delete><Object><Key>k</Key></Object><Quiet>true</Quiet><Quiet>true</Quiet></Delete>",
            b"<Delete>" + b"<Object><Key>x</Key></Object>" * 1001 + b"</Delete>",
            b"garbage",
        )
    ],
    (*_DELETE, b"garbage", {"Content-MD5": _md5(b"other")}, *_NOT_WELL_FORMED),
    (*_DELETE, _EMPTY_KEY, {"Content-MD5": _md5(b"other")}, *_NO_USER_KEY),
    (*_DELETE, _EMPTY_SECOND_KEY, {"Content-MD5": _md5(_EMPTY_SECOND_KEY)}, *_NO_USER_KEY),
    (*_DELETE, _GOOD_DELETE, {"Content-MD5": _md5(b"other")}, *_bad_digest(_md5(b"other"))),
    (*_DELETE, _GOOD_DELETE,
     {"x-amz-checksum-crc32": _crc32(_GOOD_DELETE), "Content-MD5": _md5(b"o")},
     *_bad_digest(_md5(b"o"))),
    *[
        (*_DELETE, _GOOD_DELETE, {f"x-amz-checksum-{name}": _b64(b"x" * width)},
         *_wrong_checksum(name))
        for name, width in (
            ("crc32", 4), ("crc32c", 4), ("crc64nvme", 8), ("sha1", 20), ("sha256", 32),
            ("sha512", 64), ("md5", 16), ("xxhash64", 8), ("xxhash3", 8), ("xxhash128", 16),
        )
    ],
    # What real performed.
    *[
        (*_DELETE, body, {"Content-MD5": _md5(body)}, *_WROTE_POST)
        for body in (
            _GOOD_DELETE,
            b'<Delete xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Object><Key>k</Key></Object></Delete>',
            b'<Delete xmlns="urn:x"><Object><Key>k</Key></Object></Delete>',
            b"<Delete><Quiet>true</Quiet><Object><Key>k</Key></Object></Delete>",
            b"<Delete><Object><Key>k</Key></Object><Quiet>maybe</Quiet></Delete>",
            b"<Delete><Object><VersionId>null</VersionId><Key>k</Key><ETag>x</ETag></Object></Delete>",
            b'<?xml version="1.0" encoding="UTF-8"?><Delete><Object><Key>k</Key></Object></Delete>',
            b"<Delete>" + b"<Object><Key>k</Key></Object>" * 1000 + b"</Delete>",
        )
    ],
    (*_DELETE, _GOOD_DELETE,
     {"x-amz-checksum-crc32": _crc32(_GOOD_DELETE), "x-amz-checksum-type": "FULL_OBJECT"},
     *_WROTE_POST),
    # Each algorithm's right value for the body, and the SDK's algorithm beside it, which has to
    # name that algorithm, compared without case, or be empty, which is none
    # (``backlot.routers.s3._checksum_headers``).
    *[
        (*_DELETE, _GOOD_DELETE,
         {f"x-amz-checksum-{name}": _b64(_right_checksum(name, _GOOD_DELETE)), **sdk},
         *_WROTE_POST)
        for name, sdk in [
            *((name, {}) for name in _CHECKSUM_NAMES),
            *(("crc32", {"x-amz-sdk-checksum-algorithm": alg})
              for alg in ("crc32", "Crc32", "CRC32", "")),
            ("sha256", {"x-amz-sdk-checksum-algorithm": ""}),
        ]
    ],
    (*_DELETE, _GOOD_DELETE,
     {"x-amz-checksum-crc32": _crc32(_GOOD_DELETE), "x-amz-sdk-checksum-algorithm": "SHA256"},
     *_SDK_ALGORITHM_WRONG),
    # An empty SDK algorithm beside a `Content-MD5` alone is none, and so the write; an empty
    # `x-amz-trailer` is none too, so a named algorithm beside it is refused
    # (``_checksum_headers``).
    (*_DELETE, _GOOD_DELETE, {**_WITH_MD5, "x-amz-sdk-checksum-algorithm": ""}, *_WROTE_POST),
    (*_DELETE, _GOOD_DELETE,
     {**_WITH_MD5, "x-amz-sdk-checksum-algorithm": "CRC32", "x-amz-trailer": ""},
     *_SDK_WITHOUT_CHECKSUM),
    # A key's `POST ?delete` is the bucket's DeleteObjects, whatever the key.
    (*_DELETE_AT_KEY, None, {}, *_NO_CHECKSUM),
    ("POST", "/s3/eng-artifacts/no/such.md?delete", None, {}, *_NO_CHECKSUM),
    (*_DELETE_AT_KEY, b"<x/>", {"Content-MD5": _md5(b"<x/>")}, *_NOT_WELL_FORMED),
    # A key's `PUT ?encryption`.
    (*_ENCRYPT, None, {}, *_EMPTY_BODY),
    (*_ENCRYPT_MISSING, None, {}, *_EMPTY_BODY),
    (*_ENCRYPT, None, {"Content-MD5": "garbage"}, *_GARBAGE_MD5),
    *[
        (*_ENCRYPT, body, {}, *_NOT_WELL_FORMED)
        for body in (
            b"<x/>",
            b"garbage",
            b"<ObjectEncryption/>",
            b"<ObjectEncryption><Nope/></ObjectEncryption>",
            _encryption(),
            _encryption(kind="SSE-C"),
            b"<ObjectEncryption><SSE-S3/><SSE-KMS><KMSKeyArn>x</KMSKeyArn></SSE-KMS></ObjectEncryption>",
            b"<ObjectEncryption><SSE-KMS><KMSKeyArn>a</KMSKeyArn></SSE-KMS><SSE-KMS><KMSKeyArn>b</KMSKeyArn></SSE-KMS></ObjectEncryption>",
            b"<ObjectEncryption><SSE-KMS><KMSKeyArn>a</KMSKeyArn><KMSKeyArn>a</KMSKeyArn></SSE-KMS></ObjectEncryption>",
            b"<ObjectEncryption><SSE-S3><x/></SSE-S3></ObjectEncryption>",
        )
    ],
    (*_ENCRYPT_MISSING, b"<ObjectEncryption/>", {}, *_NOT_WELL_FORMED),
    (*_ENCRYPT, b"<x/>", {"Content-MD5": _md5(b"o")}, *_NOT_WELL_FORMED),
    (*_ENCRYPT, _SSE_S3, {"Content-MD5": _md5(b"o")}, *_ENCRYPTION_MD5_MISMATCH),
    (*_ENCRYPT_MISSING, _SSE_S3, {"Content-MD5": _md5(b"o")}, *_ENCRYPTION_MD5_MISMATCH),
    (*_ENCRYPT_MISSING, _SSE_S3, {}, *_NO_SUCH_KEY),
    (*_ENCRYPT_MISSING, _encryption(_KMS_KEY_X), {}, *_NO_SUCH_KEY),
    (*_ENCRYPT, _SSE_S3, {},
     *_invalid_request("Target encryption type 'SSE-S3' is not supported.")),
    (*_ENCRYPT, _encryption(""), {}, *_NO_KMS_ARN),
    (*_ENCRYPT, _encryption("", "maybe"), {}, *_NO_KMS_ARN),
    *[
        (*_ENCRYPT, _encryption(arn, bucket_key), {},
         *_invalid_request(f"{_KMS} valid target kms key arn: {fault}"))
        for arn, bucket_key, fault in (
            ("garbage", None, "Malformed ARN - doesn't start with 'arn:'"),
            ("garbage", "maybe", "Malformed ARN - doesn't start with 'arn:'"),
            (f" {_KMS_KEY_X} ", None, "Malformed ARN - doesn't start with 'arn:'"),
            ("arn:", None, "Malformed ARN - no AWS partition specified"),
            ("arn:aws:kms", None, "Malformed ARN - no service specified"),
            ("arn:aws:kms:", None, "Malformed ARN - no AWS region partition specified"),
            ("arn:aws:kms:us-east-1:", None, "Malformed ARN - no AWS account specified"),
            ("arn:aws:kms:us-east-1:111:", None, "Malformed ARN - no resource specified"),
            ("arn:aws:kms:us-east-1:111111111111:key/", None, "resource cannot be empty"),
        )
    ],
    (*_ENCRYPT, _encryption("arn:aws:kms:us-east-1:111111111111:alias/x", "maybe"), {}, *_BAD_KMS),
    (*_ENCRYPT, _encryption("arn:aws:s3:::x"), {}, *_BAD_KMS),
    # A key's id after `key/` (``backlot.routers.s3._is_kms_key``).
    *[
        (*_ENCRYPT, _encryption(f"arn:aws:kms:us-east-1:111111111111:{resource}"), {}, *_BAD_KMS)
        for resource in ("key:abc", "key/abc/def", "key/abc:def", "keys/abc", "key/a b")
    ],
    (*_ENCRYPT, _encryption(_KMS_KEY_X, "maybe"), {},
     *_invalid_request("BucketKeyEnabled must be 'true' or 'false'. Invalid value: maybe")),
    # Past everything ``backlot.routers.s3._object_encryption_refusal`` checks, the write: `TRUE`
    # and a key id.
    *[
        (*_ENCRYPT, _encryption(arn, bucket_key), {}, *_WROTE_PUT)
        for arn, bucket_key in (
            (_KMS_KEY_X, "TRUE"),
            ("arn:aws:kms:us-east-1:111111111111:key/abc", None),
            ("arn:aws:kms:us-east-1:111111111111:key/1234abcd-12ab-34cd-56ef-1234567890ab", None),
        )
    ],
    # PutObject: a `Content-MD5` that is not one ahead of every checksum header.
    *[
        (*_PUT_NEW, b"hello", {"Content-MD5": "garbage", **extra}, *_GARBAGE_MD5)
        for extra in (
            {},
            {"x-amz-checksum-crc32": "garbage"},
            {"x-amz-checksum-foo": "AAAAAA=="},
            {"x-amz-sdk-checksum-algorithm": "CRC32"},
            _TWO_CHECKSUMS,
            {"x-amz-content-sha256": _TRAILER},
            _ZEROS_SENT,
        )
    ],
    # Its checksum headers, ahead of an aws-chunked payload hash and of a payload hash that the body
    # does not match.
    *[
        (*_PUT_NEW, b"hello", {**extra, **sha}, *answer)
        for extra, answer in (
            ({"x-amz-checksum-foo": "AAAAAA=="}, _BAD_ALGORITHM),
            (_TWO_CHECKSUMS, _MANY_CHECKSUMS),
            ({"x-amz-checksum-crc32": "garbage"}, _BAD_CRC32),
            ({"x-amz-sdk-checksum-algorithm": "CRC32"}, _SDK_WITHOUT_CHECKSUM),
            ({"x-amz-sdk-checksum-algorithm": "CRC32", "x-amz-checksum-crc32c": "AAAAAA=="},
             _SDK_ALGORITHM_WRONG),
        )
        for sha in ({}, _ZEROS_SENT, {"x-amz-content-sha256": _TRAILER})
    ],
    # An aws-chunked payload hash with no decoded length, trailer's and signed alike.
    *[
        (*_PUT_NEW, b"hello", {"x-amz-content-sha256": sha}, *_LENGTH_REQUIRED)
        for sha in (_TRAILER, "STREAMING-AWS4-HMAC-SHA256-PAYLOAD")
    ],
    (*_PUT_NEW, b"", {"x-amz-content-sha256": _TRAILER}, *_LENGTH_REQUIRED),
    # An aws-chunked body naming its checksum in `x-amz-trailer`, which real wrote, and the same
    # without the `x-amz-trailer` the algorithm needs.
    (*_PUT_NEW, _chunked(b"hello world"),
     {**_CHUNKED, "x-amz-trailer": "x-amz-checksum-crc32", "x-amz-sdk-checksum-algorithm": "CRC32"},
     *_WROTE_PUT),
    (*_PUT_NEW, _chunked(b"hello world"), {**_CHUNKED, "x-amz-sdk-checksum-algorithm": "CRC32"},
     *_SDK_WITHOUT_CHECKSUM),
    # A payload hash the body does not match, ahead of a `Content-MD5` or a checksum it does not
    # match, an empty body too; a right one, in either case, is the write.
    *[
        (*_PUT_NEW, body, {**_ZEROS_SENT, **extra}, *_sha_mismatch(body))
        for body, extra in (
            (b"hello", {}),
            (b"", {}),
            (b"hello", {"Content-MD5": _md5(b"other")}),
            (b"hello", {"x-amz-checksum-crc32": _crc32(b"other")}),
        )
    ],
    *[
        (*_PUT_NEW, b"hello", {"x-amz-content-sha256": sha}, *_WROTE_PUT)
        for sha in (
            hashlib.sha256(b"hello").hexdigest(),
            hashlib.sha256(b"hello").hexdigest().upper(),
            "UNSIGNED-PAYLOAD",
        )
    ],
    # A `Content-MD5` the body does not match, named in hex, ahead of a checksum it does not match.
    *[
        (*_PUT_NEW, b"hello", {"Content-MD5": _md5(b"other"), **extra},
         *_bad_digest(hashlib.md5(b"other").hexdigest(), b"hello"))
        for extra in ({}, {"x-amz-checksum-crc32": _crc32(b"other")})
    ],
    (*_PUT_NEW, b"hello", {"x-amz-checksum-crc32": _crc32(b"other")}, *_wrong_checksum("crc32")),
    *[
        (*_PUT_NEW, b"hello", extra, *_WROTE_PUT)
        for extra in (
            {"Content-MD5": _md5(b"hello"), "x-amz-checksum-crc32": _crc32(b"hello")},
            {"x-amz-sdk-checksum-algorithm": "crc32", "x-amz-checksum-crc32": _crc32(b"hello")},
        )
    ],
    # The bucket comes first.
    *[
        ("PUT", path, b"hello", {"x-amz-content-sha256": sha}, *_NO_SUCH_BUCKET)
        for path in ("/s3/no-such-bucket/k.txt", "/s3/no-such-bucket/k.txt?tagging")
        for sha in (_ZEROS, _TRAILER)
    ],
    # DeleteObjects checks the payload hash after its body, its keys and its digests.
    (*_DELETE, _GOOD_DELETE, {**_ZEROS_SENT, **_WITH_MD5}, *_sha_mismatch(_GOOD_DELETE)),
    (*_DELETE, b"<x/>", {**_ZEROS_SENT, "Content-MD5": _md5(b"<x/>")}, *_NOT_WELL_FORMED),
    (*_DELETE, b"", {**_ZEROS_SENT, "Content-MD5": _md5(b"")}, *_EMPTY_BODY),
    (*_DELETE, _GOOD_DELETE, _ZEROS_SENT, *_NO_CHECKSUM),
    (*_DELETE, _GOOD_DELETE, {**_ZEROS_SENT, "Content-MD5": _md5(b"other")},
     *_bad_digest(_md5(b"other"))),
    (*_DELETE, _GOOD_DELETE, {**_ZEROS_SENT, "Content-MD5": "garbage"}, *_GARBAGE_MD5),
    (*_DELETE, _EMPTY_KEY, {**_ZEROS_SENT, "Content-MD5": _md5(_EMPTY_KEY)}, *_NO_USER_KEY),
    (*_DELETE, _GOOD_DELETE, {**_TRAILER_SENT, **_WITH_MD5}, *_TRAILER_REFUSAL),
    # UpdateObjectEncryption: after the schema and a `Content-MD5` the body does not match, ahead of
    # the key.
    (*_ENCRYPT, b"<x/>", _ZEROS_SENT, *_NOT_WELL_FORMED),
    (*_ENCRYPT, b"", _ZEROS_SENT, *_EMPTY_BODY),
    (*_ENCRYPT, _encryption(kind="SSE-S3"), {**_ZEROS_SENT, "Content-MD5": _md5(b"other")},
     *_bad_digest(_md5(b"other"), _encryption(kind="SSE-S3"))),
    *[
        ("PUT", f"/s3/eng-artifacts/{key}?encryption", _encryption(kind="SSE-S3"),
         {**_ZEROS_SENT, **extra}, *_sha_mismatch(_encryption(kind="SSE-S3")))
        for key, extra in (
            ("runbooks/oncall.md", {"Content-MD5": _md5(_encryption(kind="SSE-S3"))}),
            ("backlot-no-such-key", {}),
        )
    ],
    # The other writes measured: a key's `PUT ?tagging` and a bucket's `PUT ?versioning` check the
    # payload hash once the bucket is found, a `DELETE` does not, and the tagging and the `DELETE`
    # refuse a trailer's.
    (*_PUT_TAGGING, _NO_TAGS, _ZEROS_SENT, *_sha_mismatch(_NO_TAGS)),
    ("PUT", "/s3/eng-artifacts?versioning", b"<VersioningConfiguration/>", _ZEROS_SENT,
     *_sha_mismatch(b"<VersioningConfiguration/>")),
    (*_PUT_TAGGING, _NO_TAGS, _TRAILER_SENT, *_TRAILER_REFUSAL),
    (*_DELETE_OBJECT, None, _TRAILER_SENT, *_TRAILER_REFUSAL),
    *[
        (*_DELETE_OBJECT, body, {"x-amz-content-sha256": sha}, *_wrote("DELETE"))
        for body, sha in ((b"hello", _ZEROS), (None, "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"))
    ],
    (*_RESTORE, b"<RestoreRequest/>", _ZEROS_SENT, *_NO_USER_KEY),
]
# fmt: on


@pytest.mark.parametrize(
    "method, path, body, headers, status, code, message, members",
    _WRITE_CHECK_ROWS,
    ids=[f"{i}-{r[0]}-{r[1].rsplit('?', 1)[-1]}-{r[5]}" for i, r in enumerate(_WRITE_CHECK_ROWS)],
)
def test_s3_a_write_is_checked_as_real_checks_it_before_the_501(
    live_server, method, path, body, headers, status, code, message, members
):
    base_url, settings = live_server
    r = _signed(
        base_url, path, settings.admin_token, method=method, body=body, extra_headers=headers
    )
    assert r.status_code == status
    named = re.search(r"<Code>([^<]+)</Code><Message>([^<]*)</Message>(.*)<RequestId>", r.text)
    assert (named[1], named[2], named[3]) == (code, message, members)


def test_s3_an_encryption_write_signed_with_signature_version_2_is_refused_for_it(live_server):
    """The V2 refusal ``backlot.routers.s3._object_encryption_refusal`` makes first, with a body and
    without, where V4 is checked for its body. Signed here by hand: botocore's V2 signer leaves
    `?encryption` out of the string it signs, and real, which signs it, refused botocore's signature
    as a mismatch (2026-09-29)."""
    import hmac as _hmac

    import httpx

    base_url, settings = live_server
    secret = synth.s3_secret_access_key(settings.admin_token)
    for body, content_type in ((None, ""), (b"<x/>", "application/xml")):
        date = _http_date()
        to_sign = f"PUT\n\n{content_type}\n{date}\n{OBJECT_PATH}?encryption"
        sig = base64.b64encode(_hmac.new(secret.encode(), to_sign.encode(), hashlib.sha1).digest())
        headers = {
            "Date": date,
            "Authorization": f"AWS {synth.s3_access_key_id(settings.admin_token)}:{sig.decode()}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        r = httpx.put(f"{base_url}{OBJECT_PATH}?encryption", headers=headers, content=body)
        assert r.status_code == 400, r.text
        assert (
            "<Message>Requests modifying object encryption configuration require AWS Signature "
            "Version 4.</Message>" in r.text
        )


@pytest.mark.parametrize(
    "crc, check",
    [("_CRC32C", "e3069283"), ("_CRC64NVME", "ae8b14860a799888")],
)
def test_the_crcs_s3_checks_a_delete_with_are_the_standard_ones(crc, check):
    """CRC-32C and CRC-64/NVME's published check values over `123456789`."""
    from backlot.routers import s3 as s3_router

    assert s3_router._reflected_crc(b"123456789", getattr(s3_router, crc)).hex() == check


# --- the SigV4 verifier (backlot/sigv4.py) — S3 is its only caller --------------------------------
botocore = pytest.importorskip("botocore")
from botocore.auth import S3SigV4Auth  # noqa: E402
from botocore.awsrequest import AWSRequest  # noqa: E402
from botocore.credentials import Credentials  # noqa: E402

TOKEN = "usr-7d0022af43df72b74a89"
AK = synth.s3_access_key_id(TOKEN)
SK = synth.s3_secret_access_key(TOKEN)


def _sign(method, url, region="us-east-1"):
    """Sign a request exactly as boto3 would; return (headers, path, query)."""
    from urllib.parse import urlsplit

    req = AWSRequest(method=method, url=url)
    req.headers["x-amz-content-sha256"] = "UNSIGNED-PAYLOAD"
    S3SigV4Auth(Credentials(AK, SK), "s3", region).add_auth(req)
    parts = urlsplit(url)
    headers = dict(req.headers)
    # A bare AWSRequest never gets a Host header (real HTTP clients add it at the wire
    # layer, not on the request object) but botocore's signer still folds it into the
    # canonical request via the URL. A real request arriving over HTTP always carries
    # Host, so reproduce that here rather than skip verifying it.
    headers.setdefault("host", parts.netloc)
    return headers, parts.path, parts.query


def _verify(headers, method, path, query):
    hdrs = {k.lower(): v for k, v in headers.items()}
    parsed = parse_authorization(hdrs["authorization"])
    ak, date_stamp, region = split_credential(parsed["credential"])
    assert ak == AK
    return expected_signature(
        SK,
        method,
        path,
        query,
        hdrs,
        parsed["signed_headers"],
        hdrs.get("x-amz-content-sha256", "UNSIGNED-PAYLOAD"),
        hdrs["x-amz-date"],
        date_stamp,
        region,
    ), parsed["signature"]


def test_verifier_accepts_a_real_botocore_signature():
    headers, path, query = _sign("GET", "http://127.0.0.1:8000/s3/eng-artifacts?list-type=2")
    expected, provided = _verify(headers, "GET", path, query)
    assert expected == provided


def test_verifier_accepts_a_signed_object_get():
    headers, path, query = _sign("GET", "http://127.0.0.1:8000/s3/eng-artifacts/runbooks/oncall.md")
    expected, provided = _verify(headers, "GET", path, query)
    assert expected == provided


def test_verifier_rejects_a_tampered_signature():
    headers, path, query = _sign("GET", "http://127.0.0.1:8000/s3/eng-artifacts/runbooks/oncall.md")
    expected, provided = _verify(headers, "GET", path, "list-type=2")  # query changed after signing
    assert expected != provided


def test_acl_resolve_access_key(tmp_path):
    import yaml

    tokens = tmp_path / "tokens.yaml"
    tokens.write_text(
        yaml.safe_dump(
            {
                "admin_token": "admin-service-token",
                "users": [{"email": "ava@acme.com", "name": "Ava", "token": TOKEN}],
            }
        )
    )
    acl = Acl.load(tokens, "admin-service-token", "acme")
    caller, secret = acl.resolve_access_key(AK)
    assert caller == Caller(email="ava@acme.com", is_admin=False) and secret == SK
    admin_caller, admin_secret = acl.resolve_access_key(
        synth.s3_access_key_id("admin-service-token")
    )
    assert admin_caller.is_admin and admin_secret == synth.s3_secret_access_key(
        "admin-service-token"
    )
    assert acl.resolve_access_key("AKIADOESNOTEXIST0000") is None


# ---------------------------------------------------------------- request-time fidelity
# real S3 rejects header-auth requests whose x-amz-date has drifted more than 15
# minutes from the server clock (RequestTimeTooSkewed), and rejects presigned URLs once
# X-Amz-Date + X-Amz-Expires has elapsed (AccessDenied). These tests build self-consistent
# requests (signed via `expected_signature` with the real derived secret) so they're
# deterministic regardless of wall-clock — no dependency on when the suite happens to run.

AMZ_DATE_FORMAT = "%Y%m%dT%H%M%SZ"


def _acl():
    return Acl({TOKEN: "ava@acme.com"}, "admin-service-token", "acme")


def _request(method, path, query, headers) -> Request:
    """A minimal Starlette Request mirroring what `resolve_sigv4` reads: headers,
    query_params, method, scope['query_string'] and scope['raw_path'] — plus a fake app.state.acl
    so `auth.acl(request)` resolves without a real ASGI app. `path` is the wire path; uvicorn
    hands it over verbatim as `raw_path` and percent-decoded as `path`, and so does this."""
    scope = {
        "type": "http",
        "method": method,
        "path": unquote(path),
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "headers": [(k.lower().encode("ascii"), v.encode("ascii")) for k, v in headers.items()],
        "scheme": "http",
        "server": ("backlot", 80),
        "app": SimpleNamespace(state=SimpleNamespace(acl=_acl())),
    }
    return Request(scope)


V4 = "AWS4-HMAC-SHA256"
V4A = "AWS4-ECDSA-P256-SHA256"


def _signature(algorithm, date_line, scope, canonical, secret=SK):
    """The signature over the string real signs, spelt out here: SigV4's HMAC under the scope's
    signing key, or SigV4a's ECDSA under the key ``backlot.sigv4a`` derives, which
    ``test_sigv4a_derives_the_key_botocores_crt_signer_signs_with`` pins."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    digest = hashlib.sha256(canonical.encode()).hexdigest()
    to_sign = f"{algorithm}\n{date_line}\n{scope}\n{digest}"
    if algorithm.upper() == V4A:
        key = sigv4a.private_key(AK, secret)
        return key.sign(to_sign.encode(), ec.ECDSA(hashes.SHA256())).hex()
    date_stamp, region = scope.split("/")[:2]
    return sign(secret, date_stamp, region, to_sign)


def _header_auth_request(
    amz_date: str,
    path="/s3/eng-artifacts",
    query="list-type=2",
    region="us-east-1",
    algorithm=V4,
    dates=None,
    scope_date=None,
    region_set="*",
):
    """Build a header-auth GET with a genuinely valid signature, dated `amz_date` in `x-amz-date`
    unless ``dates`` names the date headers sent, the scope dated ``scope_date`` or the date's
    first eight characters. A SigV4a one signs its ``region_set`` as well."""
    headers = {"host": "backlot", "x-amz-content-sha256": "UNSIGNED-PAYLOAD"}
    headers.update(dates or {"x-amz-date": amz_date})
    if algorithm == V4A:
        headers["x-amz-region-set"] = region_set
    signed_headers = ";".join(sorted(k for k in headers if k != "x-amz-content-sha256"))
    date_line = headers["x-amz-date"] if "x-amz-date" in headers else headers["date"]
    date_stamp = scope_date or amz_date[:8]
    scope = (
        f"{date_stamp}/s3/aws4_request"
        if algorithm == V4A
        else (f"{date_stamp}/{region}/s3/aws4_request")
    )
    canonical = canonical_request("GET", path, query, headers, signed_headers, "UNSIGNED-PAYLOAD")
    sig = _signature(algorithm, date_line, scope, canonical)
    headers["authorization"] = (
        f"{algorithm} Credential={AK}/{scope}, SignedHeaders={signed_headers}, Signature={sig}"
    )
    return _request("GET", path, query, headers)


def _presigned_request(
    amz_date: str,
    expires: int,
    path="/s3/eng-artifacts",
    region="us-east-1",
    algorithm=V4,
    payload_hash=None,
):
    """Build a presigned-query GET signed for `amz_date`/`expires` with a valid signature; a
    SigV4a one names `*` as its region set, and one sending ``payload_hash`` as its
    `x-amz-content-sha256` signs it as the payload line."""
    date_stamp = amz_date[:8]
    signed_headers = "host"
    headers = {"host": "backlot"}
    if payload_hash is not None:
        headers["x-amz-content-sha256"] = payload_hash
    scope = (
        f"{date_stamp}/s3/aws4_request"
        if algorithm == V4A
        else (f"{date_stamp}/{region}/s3/aws4_request")
    )
    params = {
        "X-Amz-Algorithm": algorithm,
        "X-Amz-Credential": f"{AK}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": signed_headers,
    }
    if algorithm == V4A:
        params["X-Amz-Region-Set"] = "*"
    query = urlencode(params, safe="-_.~", quote_via=quote)
    payload = payload_hash or "UNSIGNED-PAYLOAD"
    canonical = canonical_request("GET", path, query, headers, signed_headers, payload)
    sig = _signature(algorithm, amz_date, scope, canonical)
    query = f"{query}&X-Amz-Signature={sig}"
    return _request("GET", path, query, headers)


def test_parse_amz_date_and_is_skewed_are_pure():
    now = datetime.now(timezone.utc)
    assert parse_amz_date("garbage") is None
    assert parse_amz_date("") is None
    parsed = parse_amz_date("20260101T000000Z")
    assert parsed == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert not is_skewed(now, now)
    assert not is_skewed(now - timedelta(minutes=14), now)
    assert is_skewed(now - timedelta(minutes=16), now)
    assert is_skewed(now + timedelta(minutes=16), now)  # skew is bidirectional


def test_header_auth_rejects_skewed_date():
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(AMZ_DATE_FORMAT)
    req = _header_auth_request(stale)
    caller, err = auth.resolve_sigv4(req)
    assert caller is None
    assert err.code == "RequestTimeTooSkewed"
    members = dict(err.members)
    assert members["RequestTime"] == stale and members["MaxAllowedSkewMilliseconds"] == "900000"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", members["ServerTime"])


def test_header_auth_skew_check_precedes_signature_check():
    # A stale date with a BROKEN signature must still report RequestTimeTooSkewed — proving the
    # time check runs BEFORE signature verification (a signature-first order would instead return
    # SignatureDoesNotMatch). The access key is valid, so key-lookup passes and the time check wins.
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(AMZ_DATE_FORMAT)
    date_stamp = stale[:8]
    signed_headers = "host;x-amz-date"
    headers = {"host": "backlot", "x-amz-date": stale, "x-amz-content-sha256": "UNSIGNED-PAYLOAD"}
    credential = f"{AK}/{date_stamp}/us-east-1/s3/aws4_request"
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={credential}, "
        f"SignedHeaders={signed_headers}, Signature=deadbeef"
    )
    caller, err = auth.resolve_sigv4(_request("GET", "/s3/eng-artifacts", "list-type=2", headers))
    assert caller is None
    assert err.code == "RequestTimeTooSkewed"


# A `%3F` in the key decodes to a `?` that splits Starlette's rebuilt `request.url`, so the
# canonical request has to come off the wire — see ``backlot.auth._wire``.
SIGNED_PATHS = ["/s3/eng-artifacts", "/s3/eng-artifacts/q%3Fx.txt"]


def _date_headers(form: str, now: datetime) -> dict[str, str]:
    """The date headers a header request sends in ``form``, each measured on 2026-09-29: real read
    `Date` without an `x-amz-date`, an `x-amz-date` over a `Date` it cannot read, and an
    `x-amz-date` in RFC 1123, asctime or RFC 850, each signed as sent, and a `+0900` one beside a
    scope dated in UTC."""
    rfc = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
    return {
        "x-amz-date": {"x-amz-date": now.strftime(AMZ_DATE_FORMAT)},
        "Date": {"date": rfc},
        "x-amz-date-over-a-bad-Date": {"x-amz-date": now.strftime(AMZ_DATE_FORMAT), "date": "x"},
        "rfc1123-x-amz-date": {"x-amz-date": rfc},
        "asctime": {"x-amz-date": now.strftime("%a %b %d %H:%M:%S %Y")},
        "rfc850": {"x-amz-date": now.strftime("%A, %d-%b-%y %H:%M:%S GMT")},
        "+0900": {"date": (now + timedelta(hours=9)).strftime("%a, %d %b %Y %H:%M:%S +0900")},
    }[form]


@pytest.mark.parametrize("path", SIGNED_PATHS)
@pytest.mark.parametrize("algorithm", [V4, V4A])
@pytest.mark.parametrize(
    "form",
    ["x-amz-date", "Date", "x-amz-date-over-a-bad-Date", "rfc1123-x-amz-date", "asctime", "rfc850"]
    + ["+0900"],
)
def test_header_auth_accepts_current_date(path, algorithm, form):
    now = datetime.now(timezone.utc)
    current = now.strftime(AMZ_DATE_FORMAT)
    req = _header_auth_request(
        current, path=path, algorithm=algorithm, dates=_date_headers(form, now)
    )
    caller, err = auth.resolve_sigv4(req)
    assert err is None
    assert caller == Caller(email="ava@acme.com", is_admin=False)


def test_presigned_expired_is_access_denied():
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(AMZ_DATE_FORMAT)
    req = _presigned_request(stale, expires=60)
    caller, err = auth.resolve_sigv4(req)
    assert caller is None
    assert (err.code, err.message) == ("AccessDenied", "Request has expired")
    assert [name for name, _ in err.members] == ["X-Amz-Expires", "Expires", "ServerTime"]
    # `Expires` is the request's date plus its lifetime, as real's was (2026-09-29).
    expires = datetime.strptime(stale, AMZ_DATE_FORMAT) + timedelta(seconds=60)
    assert dict(err.members)["Expires"] == expires.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("path", SIGNED_PATHS)
@pytest.mark.parametrize("algorithm", [V4, V4A])
@pytest.mark.parametrize("payload_hash", [None, "garbage", "0" * 64])
def test_presigned_unexpired_ok(path, algorithm, payload_hash):
    """A presign sending an `x-amz-content-sha256` is signed over it, which real served for
    `garbage` and 64 zeros (2026-09-29)."""
    current = datetime.now(timezone.utc).strftime(AMZ_DATE_FORMAT)
    req = _presigned_request(
        current, expires=3600, path=path, algorithm=algorithm, payload_hash=payload_hash
    )
    caller, err = auth.resolve_sigv4(req)
    assert err is None
    assert caller == Caller(email="ava@acme.com", is_admin=False)


def _now(minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(AMZ_DATE_FORMAT)


def _dated(authorization: str, minutes: int = 0) -> dict[str, str]:
    """A V4 header's two headers: its `x-amz-date`, ``minutes`` from now, and the header itself."""
    return {"x-amz-date": _now(minutes), "authorization": authorization}


def _v4(
    akid: str,
    date: str,
    service: str = "s3",
    terminal: str = "aws4_request",
    region="us-east-1",
    scope_date=None,
    signed="host;x-amz-date",
) -> str:
    stamp = date[:8] if scope_date is None else scope_date
    return (
        f"AWS4-HMAC-SHA256 Credential={akid}/{stamp}/{region}/{service}/{terminal}, "
        f"SignedHeaders={signed}, Signature=00"
    )


def _v4a(akid: str, date: str, scope=None, signed="host;x-amz-date;x-amz-region-set") -> str:
    scope = f"{date[:8]}/s3/aws4_request" if scope is None else scope
    return f"AWS4-ECDSA-P256-SHA256 Credential={akid}/{scope}, SignedHeaders={signed}, Signature=00"


def _another_day() -> tuple[str, str]:
    """A `Date` of now in a zone whose day is not UTC's, and that day as a scope date: `+1400`
    from ten o'clock UTC on, and `HST`, ten hours behind, before it."""
    now = datetime.now(timezone.utc)
    zone, hours = ("+1400", 14) if now.hour >= 10 else ("HST", -10)
    local = now + timedelta(hours=hours)
    return local.strftime(f"%a, %d %b %Y %H:%M:%S {zone}"), local.strftime("%Y%m%d")


def _http_date(minutes: int = 0) -> str:
    """A `Date` header's value, RFC 1123, ``minutes`` from now."""
    from email.utils import formatdate

    return formatdate(
        (datetime.now(timezone.utc) + timedelta(minutes=minutes)).timestamp(), usegmt=True
    )


def _epoch(minutes: int = 0) -> str:
    return str(int((datetime.now(timezone.utc) + timedelta(minutes=minutes)).timestamp()))


def _query(**params) -> str:
    return urlencode(
        {k.replace("_", "-"): v for k, v in params.items()}, safe="-_.~", quote_via=quote
    )


_UNKNOWN = "AKIAIOSFODNN7EXAMPLE"
_MALFORMED = "The authorization header is malformed; "
_SHAPE = (
    'the Credential is mal-formed; expecting "<YOUR-AKID>/YYYYMMDD/REGION/SERVICE/aws4_request".'
)
_NO_DATE = "AWS authentication requires a valid Date or x-amz-date header"
_SKEWED = "The difference between the request time and the current time is too large."
_NO_KEY = "The AWS Access Key Id you provided does not exist in our records."
_WEEK = (
    "X-Amz-Expires must be less than a week (in seconds); that is, the given X-Amz-Expires must "
    "be less than 604800 seconds"
)
_WRONG_REGION = "the region 'us-west-2' is wrong; expecting 'us-east-1'"
_NO_REGION = "a non-empty region must be provided in the credential."
_QUERY_CREDENTIAL = "Error parsing the X-Amz-Credential parameter; "
_ONE_MECHANISM = (
    "Only one auth mechanism allowed; only the X-Amz-Algorithm query parameter, Signature query "
    "string parameter or the Authorization header should be specified"
)
_NO_SPACE = "Authorization header is invalid -- one and only one ' ' (space) required"
_V2_FORMAT = "AWS authorization header is invalid.  Expected AwsAccessKeyId:signature"
_V2_QUERY_PARAMETERS = (
    "Query-string authentication requires the Signature, Expires and AWSAccessKeyId parameters"
)
_NOT_A_DATE = "Invalid date (should be seconds since epoch): "
_NO_PAYLOAD_HASH = "Missing required header for this request: x-amz-content-sha256"
_BAD_PAYLOAD_HASH = (
    "x-amz-content-sha256 must be UNSIGNED-PAYLOAD, STREAMING-UNSIGNED-PAYLOAD-TRAILER, "
    "STREAMING-AWS4-HMAC-SHA256-PAYLOAD, STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER, "
    "STREAMING-AWS4-ECDSA-P256-SHA256-PAYLOAD, STREAMING-AWS4-ECDSA-P256-SHA256-PAYLOAD-TRAILER or "
    "a valid sha256 value."
)
_NOT_SIGNED = "There were headers present in the request which were not signed"
_DATE_FORMAT = 'This date in the credential must be in the format "yyyyMMdd".'
_SHAPE_4A = 'the Credential is mal-formed; expecting "<YOUR-AKID>/YYYYMMDD/SERVICE/aws4_request".'
_REGION_SET = "The provided X-Amz-Region-Set doesn't match against the requested S3 region."
_INTERNAL = "We encountered an internal error. Please try again."
_A_NUMBER = "X-Amz-Expires should be a number"


def _presign(
    date: str,
    expires="3600",
    credential=None,
    algorithm=V4,
    akid=AK,
    region="us-east-1",
    service="s3",
    scope_date=None,
    **extra,
) -> str:
    """A presign's query with every parameter present: ``credential`` unless given, which is
    ``akid`` over a scope dated `date`'s day or ``scope_date``, with no region when the
    ``algorithm`` is SigV4a's. The rows below are built when the module is imported, so the
    default lifetime is an hour, long enough for a serial run to reach them unexpired; a row that
    is to expire says so."""
    stamp = date[:8] if scope_date is None else scope_date
    if credential is None:
        regions = "" if algorithm == V4A else f"{region}/"
        credential = f"{akid}/{stamp}/{regions}{service}/aws4_request"
    return _query(
        X_Amz_Algorithm=algorithm,
        X_Amz_Credential=credential,
        X_Amz_Date=date,
        X_Amz_Expires=expires,
        X_Amz_SignedHeaders="host",
        X_Amz_Signature="00",
        **extra,
    )


def _v2_query(expires: str, akid: str = _UNKNOWN, **extra) -> str:
    """A V2 query's three parameters, `Expires` as given."""
    return _query(Signature="abc", AWSAccessKeyId=akid, Expires=expires, **extra)


def _http_dated(authorization: str, minutes: int = 0) -> dict[str, str]:
    """The `_http_date` header and the `Authorization` beside it."""
    return {"date": _http_date(minutes), "authorization": authorization}


def _meta_dated(authorization: str) -> dict[str, str]:
    """A V4 header's two headers with an `x-amz-meta-foo` it did not sign between them."""
    return {"x-amz-date": _now(), "x-amz-meta-foo": "bar", "authorization": authorization}


def _region_dated(region_set: str, authorization: str) -> dict[str, str]:
    """A SigV4a header's three headers: its `x-amz-date`, the region set, and the header."""
    return {"x-amz-date": _now(), "x-amz-region-set": region_set, "authorization": authorization}


# The answer builders, under the rule stated at `_invalid_request`: the code, the message as real
# spelt it and the members where the row names them.
def _header_malformed(reason: str, members=None):
    """`AuthorizationHeaderMalformed`: the fixed prefix, then ``reason``."""
    return ("AuthorizationHeaderMalformed", _MALFORMED + reason, members)


def _query_credential(reason: str, members=None):
    """`AuthorizationQueryParametersError` for an `X-Amz-Credential` real could not read."""
    return ("AuthorizationQueryParametersError", _QUERY_CREDENTIAL + reason, members)


def _query_refused(message: str, members=None):
    """`AuthorizationQueryParametersError` for any other query parameter."""
    return ("AuthorizationQueryParametersError", message, members)


def _date_format(value: str):
    """A header's scope date real could not read, with no members."""
    return _header_malformed(f'incorrect date format "{value}". ' + _DATE_FORMAT, ())


def _query_date_format(value: str):
    """The query's spelling of `_date_format`."""
    return _query_credential(f'incorrect date format "{value}". ' + _DATE_FORMAT, ())


def _date_differs(value: str):
    """A query's scope date real read and compared with its `X-Amz-Date`'s day."""
    return _query_refused(
        f'Invalid credential date "{value}". This date is not the same as X-Amz-Date: '
        f'"{_now()[:8]}".'
    )


def _not_a_date(value: str):
    """A V2 query's `Expires` real could not read as seconds."""
    return ("AccessDenied", _NOT_A_DATE + value, None)


def _expired_at(expires: str):
    """A V2 query's expiry as real named it; the server time is the clock's (2026-09-29)."""
    return ("AccessDenied", "Request has expired", (("Expires", expires),))


def _unsigned(names: str):
    """`AccessDenied` naming the sent headers left out of `SignedHeaders`, as real lists them."""
    return ("AccessDenied", _NOT_SIGNED, (("HeadersNotSigned", names),))


def _bad_hash(value: str):
    """An `x-amz-content-sha256` real could not read, named as sent."""
    return (
        "InvalidArgument",
        _BAD_PAYLOAD_HASH,
        (("ArgumentName", "x-amz-content-sha256"), ("ArgumentValue", value)),
    )


def _region_set(value: str):
    """A SigV4a region set the region this server presents is not in."""
    return (
        "RegionSetMismatch",
        _REGION_SET,
        (("RequestedRegion", "us-east-1"), ("RegionSet", value)),
    )


# The headers and answers the rows below repeat.
_GARBAGE_SCOPE = f"AWS4-HMAC-SHA256 Credential={AK}/garbage, SignedHeaders=host, Signature=00"
_HASHLESS = {"x-amz-content-sha256": None}
_GARBAGE_HASH = {"x-amz-content-sha256": "garbage"}
_Y10K = "Tue, 29 Sep 10000 16:00:00 GMT"
_TWO_MECHANISMS = ("InvalidArgument", _ONE_MECHANISM)
_UNSUPPORTED_SCHEME = ("InvalidArgument", "Unsupported Authorization Type")
_NOT_ONE_SPACE = ("InvalidArgument", _NO_SPACE)
_DATELESS = ("AccessDenied", _NO_DATE)
_TOO_SKEWED = ("RequestTimeTooSkewed", _SKEWED)
_WRONG_SERVICE = _header_malformed('incorrect service "ec2". This endpoint belongs to "s3".')
_WRONG_TERMINAL = _header_malformed(
    'incorrect terminal "aws5_request". This endpoint uses "aws4_request".'
)
_SCOPE_DATE_DIFFERS = _header_malformed(
    "Invalid credential date. Date is not the same as X-Amz-Date."
)
_UNKNOWN_KEY = ("InvalidAccessKeyId", _NO_KEY)
_BAD_QUERY_ALGORITHM = _query_refused(
    'X-Amz-Algorithm only supports "AWS4-HMAC-SHA256 and AWS4-ECDSA-P256-SHA256"'
)
_NEGATIVE_EXPIRES = _query_refused("X-Amz-Expires must be non-negative")
_OVER_A_WEEK = _query_refused(_WEEK)
_NOT_A_NUMBER = _query_refused(_A_NUMBER)
_EXPIRED = ("AccessDenied", "Request has expired")
_HEADER_WRONG_REGION = _header_malformed(_WRONG_REGION)
_QUERY_WRONG_REGION = _query_credential(_WRONG_REGION)
_V2_MALFORMED = ("InvalidArgument", _V2_FORMAT)
_V2_QUERY_INCOMPLETE = ("AccessDenied", _V2_QUERY_PARAMETERS)
_PAYLOAD_HASH_MISSING = ("InvalidRequest", _NO_PAYLOAD_HASH, ())
_GARBAGE_PAYLOAD_HASH = _bad_hash("garbage")
_INTERNAL_500 = ("InternalError", _INTERNAL, ())
_EMPTY_SCOPE_DATE = _date_format("")
_META_NOT_SIGNED = _unsigned("x-amz-meta-foo")
_DATE_NOT_SIGNED = _unsigned("x-amz-date")
_NO_REGION_SET_QUERY = _query_refused(
    "SigV4a query auth requires a non empty x-amz-region-set parameter.", ()
)

# One fault each, and each pair whose order was measured, 2026-09-29 against us-east-1.
# fmt: off
_REFUSAL_ROWS_UNIT = [
    ("header and query", {"authorization": "Bearer abc"}, _query(X_Amz_Algorithm="bogus"),
     *_TWO_MECHANISMS),
    # An empty header beside either query form, a valid presign and a bad algorithm alike
    # (``backlot.auth.resolve_sigv4`` has the measurement).
    ("empty header and a presign", {"authorization": ""}, _presign(_now()), *_TWO_MECHANISMS),
    ("empty header and X-Amz-Algorithm", {"authorization": ""}, _query(X_Amz_Algorithm="zz"),
     *_TWO_MECHANISMS),
    ("empty header and Signature", {"authorization": ""}, _v2_query("9999999999", akid=AK),
     *_TWO_MECHANISMS),
    ("bearer", {"authorization": "Bearer abc"}, "", *_UNSUPPORTED_SCHEME),
    ("sha512", {"authorization": "AWS4-HMAC-SHA512 Credential=x"}, "", *_UNSUPPORTED_SCHEME),
    ("no space", {"authorization": "AWS4-HMAC-SHA256"}, "", *_NOT_ONE_SPACE),
    ("no space before scheme", {"authorization": "AWS4-HMAC-SHA512"}, "", *_NOT_ONE_SPACE),
    ("no date", {"authorization": _v4(_UNKNOWN, _now())}, "", *_DATELESS),
    ("bad date", {"x-amz-date": "garbage", "authorization": _v4(_UNKNOWN, _now())}, "", *_DATELESS),
    ("date before parts", {"authorization": "AWS4-HMAC-SHA256 nonsense"}, "", *_DATELESS),
    ("skew first", _dated(_v4(_UNKNOWN, _now(-30)), -30), "", *_TOO_SKEWED),
    ("skew before parts", _dated("AWS4-HMAC-SHA256 nonsense", -30), "", *_TOO_SKEWED),
    ("skew before scope", _dated(_GARBAGE_SCOPE, -30), "", *_TOO_SKEWED),
    ("no parts", _dated("AWS4-HMAC-SHA256 nonsense"), "",
     *_header_malformed("the authorization header requires three components: Credential, "
                        "SignedHeaders, and Signature.")),
    ("scope", _dated(_GARBAGE_SCOPE), "", *_header_malformed(_SHAPE)),
    ("service", _dated(_v4(_UNKNOWN, _now(), service="ec2")), "", *_WRONG_SERVICE),
    ("terminal", _dated(_v4(AK, _now(), terminal="aws5_request")), "", *_WRONG_TERMINAL),
    ("scope date", _dated(_v4(_UNKNOWN, "20200101")), "", *_SCOPE_DATE_DIFFERS),
    ("unknown key", _dated(_v4(_UNKNOWN, _now())), "", *_UNKNOWN_KEY),
    ("query algorithm", {}, _query(X_Amz_Algorithm="AWS4-HMAC-SHA512", X_Amz_Signature="00"),
     *_BAD_QUERY_ALGORITHM),
    ("query parameters", {}, _query(X_Amz_Algorithm="AWS4-HMAC-SHA256", X_Amz_Signature="00"),
     *_query_refused("Query-string authentication version 4 requires the X-Amz-Algorithm, "
                     "X-Amz-Credential, X-Amz-Signature, X-Amz-Date, X-Amz-SignedHeaders, and "
                     "X-Amz-Expires parameters.")),
    ("query date", {}, _presign("garbage", expires="abc", scope_date="20260929"),
     *_query_refused("X-Amz-Date must be in the ISO8601 Long Format \"yyyyMMdd'T'HHmmss'Z'\"")),
    ("query expires", {}, _presign(_now(), expires="abc"), *_NOT_A_NUMBER),
    ("query negative", {}, _presign(_now(60), expires="-1"), *_NEGATIVE_EXPIRES),
    ("query a week", {}, _presign(_now(-60 * 24 * 9), expires="604801"), *_OVER_A_WEEK),
    ("not yet valid", {}, _presign(_now(60), credential="garbage"),
     "AccessDenied", "Request is not yet valid"),
    ("expiry first", {}, _presign(_now(-600), expires="60", credential="garbage"), *_EXPIRED),
    ("query credential", {}, _presign(_now(), credential="garbage"), *_query_credential(_SHAPE)),
    ("query service first", {}, _presign(_now(), service="ec2", scope_date="20200101"),
     *_query_credential('incorrect service "ec2". This endpoint belongs to "s3".')),
    ("query scope date", {}, _presign(_now(), akid=_UNKNOWN, scope_date="20200101"),
     *_date_differs("20200101")),
    ("query key", {}, _presign(_now(), akid=_UNKNOWN), *_UNKNOWN_KEY),
    # The region, which is the one this server presents, header and query alike.
    ("region", _dated(_v4(AK, _now(), region="us-west-2")), "", *_HEADER_WRONG_REGION),
    ("region in capitals", _dated(_v4(AK, _now(), region="US-EAST-1")), "",
     *_header_malformed("the region 'US-EAST-1' is wrong; expecting 'us-east-1'")),
    ("region empty", _dated(_v4(AK, _now(), region="")), "", *_header_malformed(_NO_REGION)),
    ("region before the key", _dated(_v4(_UNKNOWN, _now(), region="us-west-2")), "",
     *_HEADER_WRONG_REGION),
    ("region before the service", _dated(_v4(AK, _now(), service="ec2", region="us-west-2")), "",
     *_HEADER_WRONG_REGION),
    ("region before the scope date", _dated(_v4(AK, "20200101", region="us-west-2")), "",
     *_HEADER_WRONG_REGION),
    ("skew before the region", _dated(_v4(AK, _now(-30), region="us-west-2"), -30), "",
     *_TOO_SKEWED),
    ("date before the region", {"authorization": _v4(AK, _now(), region="us-west-2")}, "",
     *_DATELESS),
    ("query region", {}, _presign(_now(), region="us-west-2"), *_QUERY_WRONG_REGION),
    ("query region before the key", {}, _presign(_now(), akid=_UNKNOWN, region="us-west-2"),
     *_QUERY_WRONG_REGION),
    ("query region before the service", {}, _presign(_now(), region="us-west-2", service="ec2"),
     *_QUERY_WRONG_REGION),
    ("query region before the scope date", {},
     _presign(_now(), region="us-west-2", scope_date="20200101"), *_QUERY_WRONG_REGION),
    ("query expiry before the region", {}, _presign(_now(-120), expires="60", region="us-west-2"),
     *_EXPIRED),
    ("query a week before the region", {}, _presign(_now(), expires="604801", region="us-west-2"),
     *_OVER_A_WEEK),
    ("query region empty", {}, _presign(_now(), region=""), *_query_credential(_NO_REGION)),
    # Signature Version 2 in the header, one fault at a time and the pairs measured.
    ("v2 no colon", _http_dated("AWS garbage"), "", *_V2_MALFORMED),
    ("v2 empty signature", _http_dated(f"AWS {AK}:"), "", *_V2_MALFORMED),
    ("v2 two colons", _http_dated(f"AWS {AK}:a:b"), "", *_V2_MALFORMED),
    ("v2 format before the date", {"authorization": "AWS garbage"}, "", *_V2_MALFORMED),
    ("v2 two spaces", _http_dated(f"AWS  {AK}:abc"), "", *_NOT_ONE_SPACE),
    ("v2 no date", {"authorization": f"AWS {_UNKNOWN}:abc"}, "", *_DATELESS),
    ("v2 empty key after the date", {"authorization": "AWS :abc"}, "", *_DATELESS),
    ("v2 bad date", {"date": "garbage", "authorization": f"AWS {_UNKNOWN}:abc"}, "", *_DATELESS),
    ("v2 x-amz-date read over Date",
     {"x-amz-date": "garbage", **_http_dated(f"AWS {_UNKNOWN}:abc")}, "", *_DATELESS),
    ("v2 skew before the key", _http_dated(f"AWS {_UNKNOWN}:abc", -30), "", *_TOO_SKEWED),
    ("v2 x-amz-date skewed over Date",
     {"x-amz-date": _http_date(-30), **_http_dated(f"AWS {_UNKNOWN}:abc")}, "", *_TOO_SKEWED),
    ("v2 unknown key", _http_dated(f"AWS {_UNKNOWN}:abc"), "", *_UNKNOWN_KEY),
    ("v2 beside X-Amz-Algorithm", {"authorization": "AWS garbage"}, "X-Amz-Algorithm=x",
     *_TWO_MECHANISMS),
    ("v2 beside Signature", _http_dated(f"AWS {AK}:abc"), "Signature=abc", *_TWO_MECHANISMS),
    ("v4 beside Signature", {"authorization": "AWS4-HMAC-SHA256 x"}, "Signature=abc",
     *_TWO_MECHANISMS),
    # And in the query.
    ("v2 query beside X-Amz-Algorithm", {},
     _v2_query("9999999999", akid=AK, X_Amz_Algorithm="AWS4-HMAC-SHA256"), *_TWO_MECHANISMS),
    ("v2 query without Expires", {}, _query(Signature="abc", AWSAccessKeyId=_UNKNOWN),
     *_V2_QUERY_INCOMPLETE),
    ("v2 query without a key", {}, _query(Signature="abc", Expires=_epoch(60)),
     *_V2_QUERY_INCOMPLETE),
    ("v2 query Expires a word", {}, _v2_query("garbage"), *_not_a_date("garbage")),
    ("v2 query Expires past an int32", {}, _v2_query("2147483648"), *_not_a_date("2147483648")),
    ("v2 query Expires 1e9", {}, _v2_query("1e9"), *_not_a_date("1e9")),
    ("v2 query Expires with a space", {}, _v2_query(" 1"), *_not_a_date(" 1")),
    ("v2 query expiry before the key", {}, _v2_query(_epoch(-2)), *_EXPIRED),
    ("v2 query Expires -1", {}, _v2_query("-1"), *_EXPIRED),
    ("v2 query Expires with a leading zero", {}, _v2_query("0" + _epoch(60)), *_UNKNOWN_KEY),
    ("v2 query unknown key", {}, _v2_query(_epoch(60)), *_UNKNOWN_KEY),
    # The header's `x-amz-content-sha256`, after its one space and its scheme and ahead of its date.
    ("no payload hash", {**_HASHLESS, **_dated(_v4(_UNKNOWN, _now()))}, "", *_PAYLOAD_HASH_MISSING),
    ("payload hash before the date", {**_HASHLESS, "authorization": _v4(_UNKNOWN, _now())}, "",
     *_PAYLOAD_HASH_MISSING),
    ("payload hash before the parts", {**_HASHLESS, "authorization": "AWS4-HMAC-SHA256 nonsense"},
     "", *_PAYLOAD_HASH_MISSING),
    ("scheme before the payload hash", {**_HASHLESS, "authorization": "Bearer abc"}, "",
     *_UNSUPPORTED_SCHEME),
    ("one space before the payload hash", {**_HASHLESS, "authorization": "AWS4-HMAC-SHA256"}, "",
     *_NOT_ONE_SPACE),
    ("v2 reads no payload hash", {**_HASHLESS, **_http_dated(f"AWS {_UNKNOWN}:a")}, "",
     *_UNKNOWN_KEY),
    *[
        (f"payload hash {value!r}", {"x-amz-content-sha256": value, **_dated(_v4(AK, _now()))}, "",
         *_bad_hash(value))
        for value in ("garbage", "unsigned-payload", "a" * 63, "a" * 65, "g" * 64, "")
    ],
    ("payload hash before the skew", {**_GARBAGE_HASH, **_dated(_v4(AK, _now(-30)), -30)}, "",
     *_GARBAGE_PAYLOAD_HASH),
    ("payload hash before the region",
     {**_GARBAGE_HASH, **_dated(_v4(AK, _now(), region="us-west-2"))}, "", *_GARBAGE_PAYLOAD_HASH),
    *[
        (f"payload hash {value[:24]!r} is read",
         {"x-amz-content-sha256": value, **_dated(_v4(_UNKNOWN, _now()))}, "", *_UNKNOWN_KEY)
        for value in ("A" * 64, _TRAILER, "STREAMING-AWS4-HMAC-SHA256-PAYLOAD")
    ],
    # The header's date, as ``backlot.auth._resolve_v4_header`` reads it.
    ("Date alone", _http_dated(_v4(_UNKNOWN, _now())), "", *_UNKNOWN_KEY),
    ("x-amz-date over Date", {"x-amz-date": "garbage", **_http_dated(_v4(_UNKNOWN, _now()))}, "",
     *_DATELESS),
    ("an empty x-amz-date over Date", {"x-amz-date": "", **_http_dated(_v4(_UNKNOWN, _now()))}, "",
     *_DATELESS),
    ("Date skewed", _http_dated(_v4(_UNKNOWN, _now(-30)), -30), "", *_TOO_SKEWED),
    ("Date before 1970",
     {"date": "Tue, 29 Sep 1969 16:00:00 GMT", "authorization": _v4(_UNKNOWN, _now())}, "",
     *_DATELESS),
    ("Date past 9999", {"date": _Y10K, "authorization": _v4(_UNKNOWN, _now())}, "", *_INTERNAL_500),
    ("x-amz-date past 9999", {"x-amz-date": _Y10K, "authorization": _v4(_UNKNOWN, _now())}, "",
     *_INTERNAL_500),
    ("x-amz-date in RFC 1123", {"x-amz-date": _http_date(), "authorization": _v4(_UNKNOWN, _now())},
     "", *_UNKNOWN_KEY),
    ("scope date in the zone sent",
     {"date": _another_day()[0], "authorization": _v4(_UNKNOWN, _another_day()[1])}, "",
     *_SCOPE_DATE_DIFFERS),
    ("scope date in UTC", {"date": _another_day()[0], "authorization": _v4(_UNKNOWN, _now())}, "",
     *_UNKNOWN_KEY),
    # The scope date's format, ahead of the region and read before it is compared with the date.
    *[
        (f"credential date {value!r}", _dated(_v4(AK, _now(), scope_date=value)), "",
         *_date_format(value))
        for value in ("", "2026", "20261329", "20260230", "2026090", "20260900", "2026929",
                      "202609291", "2026-09-29", "+2026092", "2026O929")
    ],
    *[
        (f"credential date {value!r} is a date", _dated(_v4(AK, _now(), scope_date=value)), "",
         *_SCOPE_DATE_DIFFERS)
        for value in ("2026092", _now()[:8] + "x")
    ],
    ("credential date before the region",
     _dated(_v4(AK, _now(), scope_date="", region="us-west-2")), "", *_EMPTY_SCOPE_DATE),
    ("credential date before an empty region", _dated(_v4(AK, _now(), scope_date="", region="")),
     "", *_EMPTY_SCOPE_DATE),
    ("query credential date", {}, _presign(_now(), scope_date="2026"), *_query_date_format("2026")),
    ("query credential date before the region", {},
     _presign(_now(), scope_date="", region="us-west-2"), *_query_date_format("")),
    ("query credential date read and compared", {}, _presign(_now(), scope_date=_now()[:8] + "x"),
     *_date_differs(_now()[:8] + "x")),
    # A header that had to be signed and was not (``backlot.auth._unsigned``), where
    # ``backlot.auth._resolve_v4_header`` puts it.
    ("x-amz-meta-foo unsigned", _meta_dated(_v4(_UNKNOWN, _now())), "", *_META_NOT_SIGNED),
    ("content-md5 unsigned",
     {"x-amz-date": _now(), "content-md5": _md5(b""), "authorization": _v4(_UNKNOWN, _now())}, "",
     *_unsigned("content-md5")),
    ("host unsigned", _dated(_v4(_UNKNOWN, _now(), signed="x-amz-date")), "", *_unsigned("host")),
    ("x-amz-date unsigned", _dated(_v4(_UNKNOWN, _now(), signed="host")), "", *_DATE_NOT_SIGNED),
    ("unsigned in real's order",
     {"x-amz-date": _now(), "x-amz-meta-b": "1", "content-md5": _md5(b""), "x-amz-meta-a": "2",
      "authorization": _v4(_UNKNOWN, _now())},
     "", *_unsigned("x-amz-meta-a, x-amz-meta-b, content-md5")),
    ("headers real takes unsigned",
     {"x-amz-date": _now(), "content-type": "text/plain", "range": "bytes=0-1",
      "date": _http_date(), "if-none-match": '"x"', "x-amzn-trace-id": "Root=1-x",
      "authorization": _v4(_UNKNOWN, _now())},
     "", *_UNKNOWN_KEY),
    ("scope date before unsigned", _meta_dated(_v4(AK, "20200101")), "", *_SCOPE_DATE_DIFFERS),
    ("region before unsigned", _meta_dated(_v4(AK, _now(), region="us-west-2")), "",
     *_HEADER_WRONG_REGION),
    ("payload hash before unsigned",
     {"x-amz-date": _now(), "x-amz-meta-foo": "bar", **_GARBAGE_HASH,
      "authorization": _v4(AK, _now())},
     "", *_GARBAGE_PAYLOAD_HASH),
    ("query unsigned", {"x-amz-meta-foo": "bar"}, _presign(_now(), akid=_UNKNOWN),
     *_META_NOT_SIGNED),
    ("query x-amz-date header unsigned", {"x-amz-date": _now()}, _presign(_now(), akid=_UNKNOWN),
     *_DATE_NOT_SIGNED),
    ("query expiry before unsigned", {"x-amz-meta-foo": "bar"}, _presign(_now(-600), expires="60"),
     *_EXPIRED),
    # `X-Amz-Expires` as a Java long.
    *[
        (f"query X-Amz-Expires {value!r}", {}, _presign(_now(), expires=value), *_NOT_A_NUMBER)
        for value in (" 300", "300 ", "3_00", "3e2", "0x12c", "300.0", "", "-", "+",
                      "9223372036854775808", "-9223372036854775809")
    ],
    ("query X-Amz-Expires the least long", {}, _presign(_now(), expires="-9223372036854775808"),
     *_NEGATIVE_EXPIRES),
    ("query X-Amz-Expires the greatest long", {}, _presign(_now(), expires="9223372036854775807"),
     *_OVER_A_WEEK),
    *[
        (f"query X-Amz-Expires {value!r} is 300", {},
         _presign(_now(), expires=value, akid=_UNKNOWN), *_UNKNOWN_KEY)
        for value in ("+300", "0300", "٣٠٠", "３００", "0" * 40 + "300")
    ],
    # A V2 query's `Expires`: every negative long is a date, a positive one to an int32's largest,
    # and the milliseconds it makes wrap as a long's.
    ("v2 query Expires with a plus", {}, _v2_query("+2147483647"), *_UNKNOWN_KEY),
    *[
        (f"v2 query Expires {value}", {}, _v2_query(value), *_expired_at(expires))
        for value, expires in (
            ("-2147483649", "1901-12-13T20:45:51Z"),
            ("-9999999999", "1653-02-10T06:13:21Z"),
            ("-62135596800", "0001-01-03T00:00:00Z"),
            ("-62135596801", "0001-01-02T23:59:59Z"),
            ("-62167219200", "0001-01-03T00:00:00Z"),
            ("-30610224000", "0999-12-27T00:00:00Z"),
            ("-50000000000", "0385-07-24T07:06:40Z"),
            ("-9223372036854775807", "1970-01-01T00:00:01Z"),
            ("-9223372036854775808", "1970-01-01T00:00:00Z"),
        )
    ],
    *[
        (f"v2 query Expires {value} is real's 500", {}, _v2_query(value), *_INTERNAL_500)
        for value in ("-62167219201", "-70000000000", "-9223372036854775")
    ],
    ("v2 query Expires that wraps into the future", {}, _v2_query("-9223372036854776"),
     *_UNKNOWN_KEY),
    ("v2 query Expires below a long", {}, _v2_query("-9223372036854775809"),
     *_not_a_date("-9223372036854775809")),
    ("v2 Date past 9999", {"date": _Y10K, "authorization": f"AWS {_UNKNOWN}:abc"}, "",
     *_INTERNAL_500),
    # SigV4a in the header: its scope has no region, and it signs a region set.
    ("v4a scope with a region",
     _dated(_v4a(AK, _now(), scope=f"{_now()[:8]}/us-east-1/s3/aws4_request")), "",
     *_header_malformed(_SHAPE_4A, ())),
    ("v4a no payload hash", {**_HASHLESS, "authorization": _v4a(AK, _now())}, "",
     *_PAYLOAD_HASH_MISSING),
    ("v4a service",
     _region_dated("us-west-2", _v4a(AK, _now(), scope=f"{_now()[:8]}/ec2/aws4_request")), "",
     *_WRONG_SERVICE),
    ("v4a terminal", _dated(_v4a(AK, _now(), scope=f"{_now()[:8]}/s3/aws5_request")), "",
     *_WRONG_TERMINAL),
    ("v4a credential date", _dated(_v4a(AK, _now(), scope="/ec2/aws4_request")), "",
     *_EMPTY_SCOPE_DATE),
    ("v4a scope date before the region set",
     _dated(_v4a(AK, _now(), scope="20200101/s3/aws4_request")), "", *_SCOPE_DATE_DIFFERS),
    ("v4a no region set", _dated(_v4a(_UNKNOWN, _now(), signed="host;x-amz-date")), "",
     "InvalidRequest", "Missing required header for this request: x-amz-region-set", ()),
    ("v4a region set unsigned",
     _region_dated("us-west-2", _v4a(AK, _now(), signed="host;x-amz-date")), "",
     *_unsigned("x-amz-region-set")),
    ("v4a unsigned before the region set", _meta_dated(_v4a(AK, _now(), signed="host;x-amz-date")),
     "", *_META_NOT_SIGNED),
    *[
        (f"v4a region set {value!r}", _region_dated(value, _v4a(_UNKNOWN, _now())), "",
         *_region_set(value))
        for value in ("us-west-2", "")
    ],
    ("v4a region set before the key", _region_dated("us-*", _v4a(_UNKNOWN, _now())), "",
     *_UNKNOWN_KEY),
    # And in the query.
    ("v4a query scope with a region", {},
     _presign(_now(), credential=f"{AK}/{_now()[:8]}/us-east-1/s3/aws4_request", algorithm=V4A),
     *_query_credential(_SHAPE_4A, ())),
    ("v4a query no region set", {}, _presign(_now(), akid=_UNKNOWN, algorithm=V4A),
     *_NO_REGION_SET_QUERY),
    ("v4a query region set in lower case is none", {},
     _presign(_now(), akid=_UNKNOWN, algorithm=V4A, x_amz_region_set="*"), *_NO_REGION_SET_QUERY),
    ("v4a query empty region set", {},
     _presign(_now(), akid=_UNKNOWN, algorithm=V4A, X_Amz_Region_Set=""), *_NO_REGION_SET_QUERY),
    ("v4a query region set", {},
     _presign(_now(), akid=_UNKNOWN, algorithm=V4A, X_Amz_Region_Set="us-west-2"),
     *_region_set("us-west-2")),
    ("v4a query unsigned before the region set", {"x-amz-meta-foo": "bar"},
     _presign(_now(), akid=_UNKNOWN, algorithm=V4A), *_META_NOT_SIGNED),
    ("v4a query algorithm in lower case", {}, _presign(_now(), algorithm="aws4-ecdsa-p256-sha256"),
     *_BAD_QUERY_ALGORITHM),
]
# fmt: on


@pytest.mark.parametrize(
    "headers, query, code, message, members",
    [(*r[1:5], r[5] if len(r) > 5 else None) for r in _REFUSAL_ROWS_UNIT],
    ids=[r[0] for r in _REFUSAL_ROWS_UNIT],
)
def test_a_credential_real_refuses_is_refused_with_reals_code_and_message(
    headers, query, code, message, members
):
    """Each row is real's answer to that request, measured 2026-09-29 against us-east-1
    (``backlot.auth.resolve_sigv4`` has the order), and the members real named where the row names
    them. Each carries the payload hash boto3 sends, and a row that sends none names it as
    ``None``."""
    sent = {"host": "backlot", "x-amz-content-sha256": "UNSIGNED-PAYLOAD", **headers}
    caller, err = auth.resolve_sigv4(
        _request(
            "GET", "/s3/eng-artifacts", query, {k: v for k, v in sent.items() if v is not None}
        )
    )
    assert caller is None and (err.code, err.message) == (code, message)
    if members is not None:
        # The members the row names, the server's clock apart.
        assert tuple(m for m in err.members if m[0] != "ServerTime") == members
        return
    if code == "InvalidArgument" and "authorization" in headers:
        assert dict(err.members) == {
            "ArgumentName": "Authorization",
            "ArgumentValue": headers["authorization"],
        }
    elif code == "InvalidArgument":
        # The two query forms beside each other, with no header to name (2026-09-29).
        assert err.members == (("ArgumentName", "Authorization"),)
    if code == "InvalidAccessKeyId":
        assert err.members == (("AWSAccessKeyId", _UNKNOWN),)
    if "is wrong; expecting" in message:
        assert err.members == (("Region", "us-east-1"),)
    if message == "Request has expired" and "AWSAccessKeyId=" in query:
        # A V2 query names its `Expires` as a time and the server's, and no lifetime (2026-09-29).
        expires = datetime.fromtimestamp(int(dict(parse_qsl(query))["Expires"]), timezone.utc)
        assert err.members[0] == ("Expires", expires.strftime("%Y-%m-%dT%H:%M:%SZ"))
        assert [name for name, _ in err.members] == ["Expires", "ServerTime"]
    if message == "Request is not yet valid":
        # Real names the request's date in epoch milliseconds (2026-09-29).
        sent = datetime.strptime(dict(parse_qsl(query))["X-Amz-Date"], AMZ_DATE_FORMAT)
        expected_ms = str(int(sent.replace(tzinfo=timezone.utc).timestamp() * 1000))
        assert [name for name, _ in err.members] == ["X-Amz-Date", "Expires", "ServerTime"]
        assert dict(err.members)["X-Amz-Date"] == expected_ms


# --- Signature Version 2 (backlot/sigv2.py) ----------------------------------------------------


def _v2_request(method, path, query, headers, secret=SK, date_line=None):
    """A V2 header request signed over the string real signs, built here by hand: ``date_line`` is
    the line for the date (the `Date` header unless given), then the `x-amz-*` lines, then the
    path and ``query``'s signed parameters, which the caller spells out as ``signed``."""
    import hmac as _hmac

    headers = {"host": "backlot", **headers}
    amz = "".join(f"{k}:{v}\n" for k, v in sorted(headers.items()) if k.startswith("x-amz-"))
    date = headers.get("date", "") if date_line is None else date_line
    signed = headers.pop("_signed", "")
    to_sign = (
        f"{method}\n{headers.get('content-md5', '')}\n{headers.get('content-type', '')}\n{date}\n"
        f"{amz}{path}{signed}"
    )
    sig = base64.b64encode(_hmac.new(secret.encode(), to_sign.encode(), hashlib.sha1).digest())
    headers["authorization"] = f"AWS {AK}:{sig.decode()}"
    return _request(method, path, query, headers), to_sign


@pytest.mark.parametrize(
    "headers, date_line",
    [
        ({"date": "Tue, 29 Sep 2026 09:00:00 GMT"}, None),
        ({"date": "Tuesday, 29-Sep-26 09:00:00 GMT"}, None),
        ({"date": "20260929T090000Z"}, None),
        ({"date": "Tue, 29 Sep 2026 09:00:00 -0000"}, None),
        ({"date": "Tue, 29 Sep 2026 18:00:00 +0900"}, None),
        ({"date": "Tue Sep 29 09:00:00 2026"}, None),
        ({"x-amz-date": "Tue, 29 Sep 2026 09:00:00 +0000"}, ""),
        ({"x-amz-date": "Tue Sep 29 09:00:00 2026"}, ""),
        ({"x-amz-date": "Tue, 29 Sep 2026 09:00:00 GMT"}, ""),
        (
            {
                "x-amz-date": "Tue, 29 Sep 2026 09:00:00 GMT",
                "date": "Mon, 01 Jan 2001 00:00:00 GMT",
            },
            "",
        ),
        (
            {
                "date": "Tue, 29 Sep 2026 09:00:00 GMT",
                "x-amz-meta-a": "1",
                "content-type": "text/csv",
            },
            None,
        ),
    ],
    ids=[
        "rfc1123",
        "rfc850",
        "iso8601",
        "-0000",
        "+0900",
        "asctime",
        "x-amz-date-+0000",
        "x-amz-date-asctime",
        "x-amz-date",
        "x-amz-date-over-date",
        "headers",
    ],
)
def test_a_signature_version_2_header_verifies_over_the_string_real_signs(
    monkeypatch, headers, date_line
):
    """Date forms real read (``test_a_date_is_read_as_real_read_it`` has the rest), each signed as
    sent (the `StringToSign` real returned for a bad secret named each as sent, 2026-09-29); an
    `x-amz-date` over a `Date`, signed as ``backlot.auth._resolve_v2_header`` signs it; and the
    `Content-Type` and `x-amz-meta-*` lines real signs. Real served the RFC 1123, `-0000`, `+0900`
    and asctime forms, every `x-amz-date` row and the `x-amz-meta-*` one."""
    now = datetime(2026, 9, 29, 9, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(
        auth, "datetime", SimpleNamespace(now=lambda tz: now, fromtimestamp=datetime.fromtimestamp)
    )
    req, _ = _v2_request("GET", "/s3/eng-artifacts", "list-type=2", headers, date_line=date_line)
    caller, err = auth.resolve_sigv4(req)
    assert err is None and caller == Caller(email="ava@acme.com", is_admin=False)


# Each date form real was sent at 2026-09-29T15:51:47Z against a V2 header, and what it made of
# it: served, skewed, no date, or its 500.
_DATE_ROWS = [
    *[
        (value, "served")
        for value in (
            "Tue, 29 Sep 2026 15:51:47 GMT",
            "Tue, 29 Sep 2026 15:51:47 -0000",
            "Tue, 29 Sep 2026 15:51:47 +0000",
            "Tue, 29 Sep 2026 15:51:47 UTC",
            "Wed, 30 Sep 2026 00:51:47 +0900",
            "Tue Sep 29 15:51:47 2026",
            "Tue, 29 Sep 2026 10:51:47 EST",
            "tue, 29 sep 2026 15:51:47 gmt",
            "TUE, 29 SEP 2026 15:51:47 GMT",
            "Tue, 29 Sep 2026 17:51:47 IST",
            "Tue, 29 Sep 2026 05:51:47 HDT",
            "Wed, 30 Sep 2026 01:21:47 ACDT",
            "Tue, 29 Sep 2026 12:21:47 NST",
            "Tue, 29 Sep 2026 07:51:47 pst",
            "Wed, 30 Sep 2026 01:51:47 CHST",
            "Wed, 30 Sep 2026 00:51:47 GMT+09:00",
            "Tuesday, 29-Sep-26 15:51:47 +0000",
            "Tue, 29-Sep-26 15:51:47 GMT",
            "Tuesday, 29 Sep 2026 15:51:47 GMT",
            "Tue, 29 September 2026 15:51:47 GMT",
            "Mon, 29 Sep 2026 15:51:47 GMT",
            "Tue,  29 Sep 2026 15:51:47 GMT",
            "Tue, 29 Sep 02026 15:51:47 GMT",
            "Tue, 29 Sep 2026 15:51:60 GMT",
            "Tuesday, 29-Sep-2026 15:51:47 GMT",
            "Tue, 29 Sep 2026 15:51:47 GMT+00:00",
            "Tue, 29 Sep 2026 15:51:47 GMT-00:00",
            "Tue, 29 Sep 2026 15:51:47 GMT+0:00",
            "Wed, 30 Sep 2026 01:21:47 GMT+09:30",
            "Wed, 30 Sep 2026 01:21:47 +0930",
            "Wed, 30 Sep 2026 05:51:47 +1400",
            "Tue, 29 Sep 2026 15:51:47  GMT",
            "Tue Sep 29 15:51:47  2026",
            "Tue Sep 029 15:51:47 2026",
            "Tuesday, 29-Sep- 26 15:51:47 GMT",
            "20260929T155147Z",
        )
    ],
    *[
        (value, "skewed")
        for value in (
            "Tue, 29 Sep 2026 15:51:47 +0900",
            "Tue, 29 Sep 2026 21:21:47 IST",
            "Tue, 29 Sep 2026 5:51:47 GMT",
            "Tue, 29 Sep 1970 15:51:47 GMT",
            "Tue, 29 Sep 9999 15:51:47 GMT",
            "Tue, 29 Sep 2026 25:51:47 GMT",
            "Tue, 32 Sep 2026 15:51:47 GMT",
        )
    ],
    *[
        (value, None)
        for value in (
            "",
            "Tue, 29 Sep 2026 15:51:47",
            "Tue, 29 Sep 2026 15:51:47 Z",
            "Tue, 29 Sep 2026 15:51:47 UT",
            "29 Sep 2026 15:51:47 GMT",
            "Wed, 30 Sep 2026 00:51:47 GMT+9",
            "Wed, 30 Sep 2026 00:51:47 GMT+0900",
            "Wed, 30 Sep 2026 00:51:47 +09:00",
            "Wed, 30 Sep 2026 00:51:47 Asia/Seoul",
            "Tue, 29 Sep 2026 15:51:47 GMT xyz",
            "Tue Sep 29 15:51:47 2026 GMT",
            "2026-09-29T15:51:47Z",
            "Xyz, 29 Sep 2026 15:51:47 GMT",
            "Tue, 29 09 2026 15:51:47 GMT",
            "Tue , 29 Sep 2026 15:51:47 GMT",
            "Tue,29 Sep 2026 15:51:47 GMT",
            "Tue,\t29 Sep 2026 15:51:47 GMT",
            "Tue, 29 Sep 26 15:51:47 GMT",
            "Tue, 29 Sep 2026 15:51 GMT",
            "Tue  Sep 29 15:51:47 2026",
            "Tue, 29 Sep 2026 15:51:47 +00:00",
            "Tue, 29 Sep 2026 15:51:47 +000",
            "Tue, 29 Sep 2026 15:51:47 +00000",
            "Wed, 30 Sep 2026 15:51:47 +2400",
            "Tue, 29 Sep 2026 16:51:47 +0060",
            "Tue, 29 Sep 1969 15:51:47 GMT",
            "Tue, 29 Sep 1000 15:51:47 GMT",
            "Tue, 29 Sep 999 15:51:47 GMT",
            "Tuesday, 29-Sep-026 15:51:47 GMT",
        )
    ],
    ("Tue, 29 Sep 10000 15:51:47 GMT", "500"),
]


@pytest.mark.parametrize("value, answer", _DATE_ROWS)
def test_a_date_is_read_as_real_read_it(value, answer):
    """``backlot.sigv2.parse_date`` over each form, a skew being a date more than fifteen minutes
    from the one it was sent at."""
    sent = datetime(2026, 9, 29, 15, 51, 47, tzinfo=timezone.utc)
    if answer == "500":
        with pytest.raises(sigv2.DateOutOfRange):
            sigv2.parse_date(value)
        return
    read = sigv2.parse_date(value)
    if answer is None:
        assert read is None
    else:
        assert read is not None and is_skewed(read, sent) is (answer == "skewed")


# What real named in a Signature Version 2 string to sign for `?<name>=v`, after the path, at a
# bucket's path and a key's (every query parameter botocore's S3 model uses and five more,
# 2026-09-29; `website` sent without a value). A name real refused before signing at one of the two
# paths has no row there.
_V2_SIGNED_ROWS = [
    ("bucket", "abac", "?abac"),
    ("key", "abac", "?abac"),
    ("bucket", "accelerate", "?accelerate"),
    ("key", "accelerate", "?accelerate"),
    ("bucket", "acl", "?acl"),
    ("key", "acl", "?acl"),
    ("bucket", "analytics", "?analytics"),
    ("key", "analytics", "?analytics"),
    ("bucket", "annotation", "?annotation"),
    ("key", "annotation", "?annotation"),
    ("bucket", "annotation-prefix", ""),
    ("key", "annotation-prefix", ""),
    ("bucket", "annotationName", "?annotationName"),
    ("bucket", "attributes", "?attributes"),
    ("key", "attributes", "?attributes"),
    ("bucket", "bucket-region", ""),
    ("key", "bucket-region", ""),
    ("key", "continuation-token", ""),
    ("bucket", "cors", "?cors"),
    ("key", "cors", "?cors"),
    ("bucket", "delete", "?delete"),
    ("key", "delete", "?delete"),
    ("bucket", "delimiter", ""),
    ("key", "delimiter", ""),
    ("bucket", "encoding-type", ""),
    ("key", "encoding-type", ""),
    ("bucket", "encryption", "?encryption"),
    ("key", "encryption", "?encryption"),
    ("bucket", "fetch-owner", ""),
    ("key", "fetch-owner", ""),
    ("bucket", "id", ""),
    ("key", "id", ""),
    ("bucket", "intelligent-tiering", "?intelligent-tiering"),
    ("key", "intelligent-tiering", "?intelligent-tiering"),
    ("bucket", "inventory", "?inventory"),
    ("key", "inventory", "?inventory"),
    ("bucket", "key-marker", ""),
    ("key", "key-marker", ""),
    ("bucket", "legal-hold", "?legal-hold"),
    ("key", "legal-hold", "?legal-hold"),
    ("bucket", "lifecycle", "?lifecycle"),
    ("key", "lifecycle", "?lifecycle"),
    ("bucket", "list-type", ""),
    ("key", "list-type", ""),
    ("bucket", "location", "?location"),
    ("key", "location", "?location"),
    ("bucket", "logging", "?logging"),
    ("key", "logging", "?logging"),
    ("bucket", "marker", ""),
    ("key", "marker", ""),
    ("bucket", "max-annotation-results", ""),
    ("key", "max-annotation-results", ""),
    ("bucket", "max-buckets", ""),
    ("key", "max-buckets", ""),
    ("bucket", "max-directory-buckets", ""),
    ("key", "max-directory-buckets", ""),
    ("key", "max-keys", ""),
    ("bucket", "max-parts", ""),
    ("key", "max-parts", ""),
    ("bucket", "max-uploads", ""),
    ("key", "max-uploads", ""),
    ("bucket", "metadataAnnotationTable", "?metadataAnnotationTable"),
    ("key", "metadataAnnotationTable", "?metadataAnnotationTable"),
    ("bucket", "metadataConfiguration", "?metadataConfiguration"),
    ("key", "metadataConfiguration", "?metadataConfiguration"),
    ("bucket", "metadataInventoryTable", "?metadataInventoryTable"),
    ("key", "metadataInventoryTable", "?metadataInventoryTable"),
    ("bucket", "metadataJournalTable", "?metadataJournalTable"),
    ("key", "metadataJournalTable", "?metadataJournalTable"),
    ("bucket", "metadataTable", "?metadataTable"),
    ("key", "metadataTable", "?metadataTable"),
    ("bucket", "metrics", "?metrics"),
    ("key", "metrics", "?metrics"),
    ("bucket", "notification", "?notification"),
    ("key", "notification", "?notification"),
    ("bucket", "object-lock", "?object-lock"),
    ("key", "object-lock", "?object-lock"),
    ("bucket", "ownershipControls", "?ownershipControls"),
    ("key", "ownershipControls", "?ownershipControls"),
    ("bucket", "part-number-marker", ""),
    ("key", "part-number-marker", ""),
    ("key", "partNumber", "?partNumber=v"),
    ("bucket", "policy", "?policy"),
    ("key", "policy", "?policy"),
    ("bucket", "policyStatus", "?policyStatus"),
    ("key", "policyStatus", "?policyStatus"),
    ("bucket", "prefix", ""),
    ("key", "prefix", ""),
    ("bucket", "publicAccessBlock", "?publicAccessBlock"),
    ("key", "publicAccessBlock", "?publicAccessBlock"),
    ("bucket", "renameObject", ""),
    ("key", "renameObject", ""),
    ("bucket", "replication", "?replication"),
    ("key", "replication", "?replication"),
    ("bucket", "requestPayment", "?requestPayment"),
    ("key", "requestPayment", "?requestPayment"),
    ("bucket", "response-cache-control", "?response-cache-control=v"),
    ("key", "response-cache-control", "?response-cache-control=v"),
    ("bucket", "response-content-disposition", "?response-content-disposition=v"),
    ("key", "response-content-disposition", "?response-content-disposition=v"),
    ("bucket", "response-content-encoding", "?response-content-encoding=v"),
    ("key", "response-content-encoding", "?response-content-encoding=v"),
    ("bucket", "response-content-language", "?response-content-language=v"),
    ("key", "response-content-language", "?response-content-language=v"),
    ("bucket", "response-content-type", "?response-content-type=v"),
    ("key", "response-content-type", "?response-content-type=v"),
    ("bucket", "response-expires", "?response-expires=v"),
    ("key", "response-expires", "?response-expires=v"),
    ("bucket", "restore", "?restore"),
    ("key", "restore", "?restore"),
    ("bucket", "retention", "?retention"),
    ("key", "retention", "?retention"),
    ("bucket", "select", "?select"),
    ("key", "select", "?select"),
    ("bucket", "select-type", "?select-type=v"),
    ("key", "select-type", "?select-type=v"),
    ("bucket", "session", ""),
    ("key", "session", ""),
    ("key", "start-after", ""),
    ("bucket", "tagging", "?tagging"),
    ("key", "tagging", "?tagging"),
    ("bucket", "torrent", "?torrent"),
    ("key", "torrent", "?torrent"),
    ("bucket", "upload-id-marker", ""),
    ("key", "upload-id-marker", ""),
    ("bucket", "uploadId", "?uploadId=v"),
    ("key", "uploadId", "?uploadId=v"),
    ("bucket", "uploads", "?uploads"),
    ("key", "uploads", "?uploads"),
    ("bucket", "version-id-marker", ""),
    ("key", "version-id-marker", ""),
    ("key", "versionId", "?versionId=v"),
    ("bucket", "versioning", "?versioning"),
    ("key", "versioning", "?versioning"),
    ("bucket", "versions", "?versions"),
    ("key", "versions", "?versions"),
    ("bucket", "x-id", ""),
    ("key", "x-id", ""),
    ("bucket", "ACL", ""),
    ("key", "ACL", ""),
    ("bucket", "Versioning", ""),
    ("key", "Versioning", ""),
    ("bucket", "x-amz-foo", ""),
    ("key", "x-amz-foo", ""),
    ("bucket", "X-Amz-Foo", ""),
    ("key", "X-Amz-Foo", ""),
    ("bucket", "website", "?website"),
    ("key", "website", "?website"),
]


@pytest.mark.parametrize(
    "where, name, signed", _V2_SIGNED_ROWS, ids=[f"{r[0]}-{r[1]}" for r in _V2_SIGNED_ROWS]
)
def test_signature_version_2_signs_the_query_parameters_real_signs(where, name, signed):
    """Each row is what real's `StringToSign` named for that parameter, and a signature over it
    verifies here where a signature over anything else is the mismatch that names it."""
    path = "/s3/eng-artifacts" if where == "bucket" else "/s3/eng-artifacts/runbooks/oncall.md"
    query = "website" if name == "website" else f"{quote(name)}=v"
    headers = {"date": _http_date(), "_signed": signed}
    if name.lower().startswith("x-amz-"):
        headers[name.lower()] = "v"
    req, _ = _v2_request("GET", path, query, headers)
    caller, err = auth.resolve_sigv4(req)
    assert err is None, (name, err and dict(err.members).get("StringToSign"))


@pytest.mark.parametrize(
    "query, signed, amz, headers, form",
    [
        (*row, "header")
        for row in (
            ("versionId=v&acl", "?acl&versionId=v", "", {}),
            ("uploads&prefix=x", "?uploads", "", {}),
            ("acl=", "?acl", "", {}),
            (
                "response-content-type=a%2Fb&response-expires=x",
                "?response-content-type=a/b&response-expires=x",
                "",
                {},
            ),
            ("x-amz-foo=1&acl", "?acl", "x-amz-foo:1\n", {}),
            ("X-Amz-Foo=1&acl", "?acl", "x-amz-foo:1\n", {}),
            ("partNumber=2&uploadId=a%20b", "?partNumber=2&uploadId=a b", "", {}),
            ("tagging&tagging", "?tagging", "", {}),
            ("acl=a&acl=b", "?acl", "", {}),
            # A query's `x-amz-*` parameter signed over a header of its name.
            ("x-amz-foo=1", "", "x-amz-foo:1\n", {"x-amz-foo": "2"}),
        )
    ]
    + [
        # A query's date line is its `Expires` as sent, and its `x-amz-*` parameters are signed.
        ("X-Amz-Signature=00", "", "x-amz-signature:00\n", {}, "query"),
    ],
)
def test_signature_version_2_sorts_names_and_decodes_values_as_real_does(
    query, signed, amz, headers, form
):
    """The strings ``backlot.sigv2._resource`` and ``backlot.sigv2._amz_headers`` build, with a
    value decoded where one is signed and a name sent twice signed once (measured 2026-09-29 at a
    key over a bad secret). The mismatch names the string this server signed, which is real's, over
    the path under the mount (``backlot.auth._verify_v2``): the access key, the string, the
    signature sent and the string's bytes, and no canonical request, which V2 has none of, in the
    order real named them for a header over thirty samples at a bucket's path (same date)."""
    path = "/s3/eng-artifacts/runbooks/oncall.md"
    if form == "header":
        date = _http_date()
        req, _ = _v2_request("GET", path, query, {"date": date, **headers}, secret="x" * 40)
    else:
        date = _epoch(60)
        credential = _query(AWSAccessKeyId=AK, Expires=date, Signature="abc")
        req = _request("GET", path, f"{credential}&{query}", {"host": "backlot"})
    caller, err = auth.resolve_sigv4(req)
    assert caller is None and err.code == "SignatureDoesNotMatch"
    members = dict(err.members)
    under = path.removeprefix("/s3")
    assert members["StringToSign"] == f"GET\n\n\n{date}\n{amz}{under}{signed}"
    assert members["StringToSignBytes"] == " ".join(
        f"{b:02x}" for b in members["StringToSign"].encode()
    )
    if form == "header":
        assert [name for name, _ in err.members] == [
            "AWSAccessKeyId",
            "StringToSign",
            "SignatureProvided",
            "StringToSignBytes",
        ]
    else:
        assert members["SignatureProvided"] == "abc"


@pytest.mark.parametrize("algorithm", [V4, V4A])
def test_a_lower_case_v4_scheme_is_v4_and_is_signed_as_sent(algorithm):
    """A lower-case scheme is V4 or SigV4a, matched without case (``backlot.auth.resolve_sigv4``)
    and signed as sent (``backlot.auth._verify_v4``), so a signature over the upper-case line is the
    mismatch, named with the members a V4 mismatch has and SigV4a's scope, which has no region
    (2026-09-29)."""
    amz_date = datetime.now(timezone.utc).strftime(AMZ_DATE_FORMAT)
    req = _header_auth_request(amz_date, algorithm=algorithm)
    authz = dict(req.headers)["authorization"].replace(algorithm, algorithm.lower())
    headers = {**dict(req.headers), "authorization": authz}
    _, err = auth.resolve_sigv4(_request("GET", "/s3/eng-artifacts", "list-type=2", headers))
    assert err.code == "SignatureDoesNotMatch"
    assert [name for name, _ in err.members] == [
        "AWSAccessKeyId",
        "StringToSign",
        "SignatureProvided",
        "StringToSignBytes",
        "CanonicalRequest",
        "CanonicalRequestBytes",
    ]
    lines = dict(err.members)["StringToSign"].split("\n")
    scope = (
        f"{amz_date[:8]}/s3/aws4_request"
        if algorithm == V4A
        else (f"{amz_date[:8]}/us-east-1/s3/aws4_request")
    )
    assert lines[:3] == [algorithm.lower(), amz_date, scope]


# What botocore's CRT signers sent over the key this module signs with, `AK` written in where they
# name it, header and query, at the date they name (awscrt, 2026-09-29): the derivation
# ``backlot.sigv4a`` makes is the one real served requests signed with, and these pin it to the
# CRT's.
_CRT_HEADERS = {
    "host": "backlot",
    "x-amz-date": "20260929T164424Z",
    "x-amz-region-set": "*",
    "x-amz-content-sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "authorization": (
        f"AWS4-ECDSA-P256-SHA256 Credential={AK}/20260929/s3/aws4_request, "
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-region-set, "
        "Signature=304502206d7fd0a1310db3d34713b117c2230e5b47b3d882e7434da9302ef25dd742e2f002210"
        "09a6e8bcac270c6a8b1b85bc833a91adb75fb294700852370908143b0a2fab52b"
    ),
}
_CRT_QUERY = (
    f"list-type=2&X-Amz-Algorithm=AWS4-ECDSA-P256-SHA256&X-Amz-Credential={AK}%2F"
    "20260929%2Fs3%2Faws4_request&X-Amz-Date=20260929T164424Z&X-Amz-SignedHeaders=host&X-Amz-Exp"
    "ires=3600&X-Amz-Region-Set=%2A&X-Amz-Signature=30450220009bb7d3648ca5ca91936a326c794f8e9708"
    "3ae64b567c0229d6d95deaf47034022100ed134677908751f962bb0f648f3caeb534db767e3e4515e97718b3350"
    "6514214"
)


@pytest.mark.parametrize("form", ["header", "query"])
def test_sigv4a_derives_the_key_botocores_crt_signer_signs_with(monkeypatch, form):
    """Each verifies at a minute past its date, and fails once a byte of its signature changes."""
    now = datetime(2026, 9, 29, 16, 45, 24, tzinfo=timezone.utc)
    monkeypatch.setattr(
        auth, "datetime", SimpleNamespace(now=lambda tz: now, fromtimestamp=datetime.fromtimestamp)
    )
    headers, query = (
        (_CRT_HEADERS, "list-type=2") if form == "header" else ({"host": "backlot"}, _CRT_QUERY)
    )
    caller, err = auth.resolve_sigv4(_request("GET", "/s3/eng-artifacts", query, headers))
    assert err is None and caller == Caller(email="ava@acme.com", is_admin=False)
    if form == "header":
        headers = {**headers, "authorization": headers["authorization"][:-2] + "00"}
    else:
        query = query[:-2] + "00"
    _, err = auth.resolve_sigv4(_request("GET", "/s3/eng-artifacts", query, headers))
    assert err.code == "SignatureDoesNotMatch"


# The region sets real took for us-east-1 and the ones it refused (2026-09-29).
_REGION_SET_ROWS = [
    *[
        (value, True)
        for value in (
            "*",
            "us-east-1",
            "US-EAST-1",
            "us-*",
            "US-*",
            "us-east-*",
            "*-1",
            "u*1",
            "us*",
            "*east*",
            "us-east-1*",
            "*1",
            "**",
            "us-east-1,us-west-2",
            "us-west-2,us-east-1",
            "us-east-1,",
            ",us-east-1",
            "us-east-1,*",
            "*,us-west-2",
            "us-east-1,us-east-1",
        )
    ],
    *[
        (value, False)
        for value in (
            "us-west-2",
            "",
            "us-east-?",
            "aws-global",
            "us-east-10",
            "us-east-",
            "us-west-2, us-east-1",
            "us-east-1;us-west-2",
            "us-east-1 us-west-2",
        )
    ],
]


@pytest.mark.parametrize("region_set, matches", _REGION_SET_ROWS)
def test_a_region_set_names_the_region_as_real_matches_it(region_set, matches):
    assert sigv4a.region_set_matches(region_set, "us-east-1") is matches


# The unsigned headers real named, in the order it named them (2026-09-29).
# fmt: off
_NOT_SIGNED_ROWS = [
    ("x-amz-meta-a, content-md5, host", ["x-amz-meta-a", "host", "content-md5"]),
    ("x-amz-meta-a, content-md5, x-amz-meta-z", ["content-md5", "x-amz-meta-z", "x-amz-meta-a"]),
    ("x-amz-z, x-amz-a", ["x-amz-z", "x-amz-a"]),
    ("x-amz-meta-a, x-amz-meta-b", ["x-amz-meta-b", "x-amz-meta-a"]),
    ("x-amz-meta-a, x-amz-meta-b, content-md5", ["x-amz-meta-b", "x-amz-meta-a", "content-md5"]),
    ("x-amz-meta-a, x-amz-checksum-mode, x-amz-acl",
     ["x-amz-checksum-mode", "x-amz-meta-a", "x-amz-acl"]),
    ("x-amz-meta-a, x-amz-meta-a2, x-amz-meta-a-b",
     ["x-amz-meta-a", "x-amz-meta-a2", "x-amz-meta-a-b"]),
    ("host, x-amz-meta-foo", ["host", "x-amz-meta-foo"]),
    ("x-amz-meta-a2, x-amz-z", ["x-amz-z", "x-amz-meta-a2"]),
    ("host, x-amz-meta-a-b", ["x-amz-meta-a-b", "host"]),
    (
        "x-amz-meta-k2, x-amz-meta-k1, x-amz-meta-k0, x-amz-meta-k6, x-amz-meta-k5, x-amz-meta-k4, "
        "x-amz-meta-k3, x-amz-meta-k9, x-amz-meta-k12, x-amz-meta-k8, x-amz-meta-k7, "
        "x-amz-meta-k10, x-amz-meta-k11",
        [f"x-amz-meta-k{i}" for i in range(13)],
    ),
    (
        "x-amz-meta-q, x-amz-meta-p, x-amz-meta-s, x-amz-meta-r, x-amz-meta-o, x-amz-meta-i, "
        "x-amz-meta-h, x-amz-meta-e, x-amz-meta-d, x-amz-meta-g, x-amz-meta-f, x-amz-meta-a, "
        "x-amz-meta-y, x-amz-meta-u, x-amz-meta-t, x-amz-meta-w",
        [f"x-amz-meta-{c}" for c in "qwertyuiopasdfgh"],
    ),
    (
        "x-amz-acl, content-md5, x-amz-tagging, x-amz-server-side-encryption, x-amz-storage-class",
        ["x-amz-acl", "x-amz-tagging", "x-amz-storage-class", "x-amz-server-side-encryption"]
        + ["content-md5"],
    ),
]
# fmt: on


@pytest.mark.parametrize("named, sent", _NOT_SIGNED_ROWS, ids=[r[0][:40] for r in _NOT_SIGNED_ROWS])
def test_unsigned_headers_are_named_in_reals_order_whatever_order_they_came_in(named, sent):
    """Sent in the order given and in the reverse, which real named alike."""
    for order in (sent, sent[::-1]):
        assert ", ".join(auth._hash_set_order(order)) == named


# Where the payload hash a V4 or SigV4a signature covers comes from, and a request it does not.
_PAYLOAD_HASH_ROWS = [
    ({"authorization": "AWS4-HMAC-SHA256 x", "x-amz-content-sha256": "v"}, "", "v"),
    ({"authorization": "aws4-ecdsa-p256-sha256 x", "x-amz-content-sha256": "v"}, "", "v"),
    ({"x-amz-content-sha256": "v"}, "X-Amz-Algorithm=AWS4-HMAC-SHA256", "v"),
    ({}, "X-Amz-Algorithm=AWS4-HMAC-SHA256", None),
    ({"authorization": "AWS a:b", "x-amz-content-sha256": "v"}, "", None),
    ({"authorization": "Bearer x", "x-amz-content-sha256": "v"}, "", None),
    ({"x-amz-content-sha256": "v"}, "Signature=x&AWSAccessKeyId=y&Expires=1", None),
    ({"x-amz-content-sha256": "v"}, "", None),
]


@pytest.mark.parametrize("headers, query, payload_hash", _PAYLOAD_HASH_ROWS)
def test_the_payload_hash_is_the_one_a_v4_signature_covers(headers, query, payload_hash):
    """A V2 header was served over a payload hash real refuses in a V4 one, and a presign over one
    it signed as its payload line (2026-09-29)."""
    request = _request("GET", "/s3/eng-artifacts", query, {"host": "backlot", **headers})
    assert auth.signed_payload_hash(request) == payload_hash


@pytest.mark.parametrize(
    "signed_path, verifies",
    [
        ("/eng-artifacts/runbooks/oncall.md", True),
        ("/s3/eng-artifacts/runbooks/oncall.md", True),
        ("/eng-artifacts/runbooks/oncall.md/", False),
    ],
)
def test_signature_version_2_signs_the_path_under_the_mount_or_the_whole_path(
    signed_path, verifies
):
    """Either path ``backlot.auth._verify_v2`` verifies, what is under `/s3` (boto3's `auth_path`)
    or the whole of what was sent (a signer handed the URL), and a path that is neither, boto3's
    bucket paths with their slash, is the mismatch."""
    import hmac as _hmac

    date = _http_date()
    to_sign = f"GET\n\n\n{date}\n{signed_path}"
    sig = base64.b64encode(_hmac.new(SK.encode(), to_sign.encode(), hashlib.sha1).digest()).decode()
    headers = {"host": "backlot", "date": date, "authorization": f"AWS {AK}:{sig}"}
    caller, err = auth.resolve_sigv4(
        _request("GET", "/s3/eng-artifacts/runbooks/oncall.md", "", headers)
    )
    assert (err is None) == verifies


def test_signature_version_2_is_served_as_real_serves_it(live_server):
    """botocore's own V2 signers, header and query, against the served routes: the listings, a
    bucket's configuration, an object and its HEAD, ListBuckets, a query ten years ahead and the
    scheme in lower case (each served on real, 2026-09-29), and a scoped caller still scoped. Then
    boto3's own V2 client, path-style: ListBuckets and an object's GET and HEAD are served, as real
    served them, and a bucket's own operations are the mismatch real answered them with (same
    date), for the slash ``backlot.auth._verify_v2`` describes."""
    import httpx
    from botocore.auth import HmacV1Auth, HmacV1QueryAuth
    from botocore.config import Config
    from botocore.exceptions import ClientError

    boto3 = pytest.importorskip("boto3")

    base_url, settings = live_server
    admin = Credentials(
        synth.s3_access_key_id(settings.admin_token),
        synth.s3_secret_access_key(settings.admin_token),
    )

    def v2(method, path, cred=admin, lower=False):
        req = AWSRequest(method=method, url=f"{base_url}{path}")
        HmacV1Auth(cred).add_auth(req)
        headers = dict(req.headers)
        if lower:
            headers["Authorization"] = "aws" + headers["Authorization"][3:]
        return httpx.request(method, f"{base_url}{path}", headers=headers)

    def v2_query(path, expires=60):
        req = AWSRequest(method="GET", url=f"{base_url}{path}")
        HmacV1QueryAuth(admin, expires=expires).add_auth(req)
        return httpx.get(req.url)

    assert "<ListBucketResult" in v2("GET", "/s3/eng-artifacts").text
    assert "<KeyCount>" in v2("GET", "/s3/eng-artifacts?list-type=2").text
    assert "<VersioningConfiguration" in v2("GET", "/s3/eng-artifacts?versioning").text
    assert v2("GET", OBJECT_PATH).content == OBJECT_TEXT
    assert v2("HEAD", OBJECT_PATH).status_code == 200
    assert "<ListAllMyBucketsResult" in v2("GET", "/s3/").text
    assert v2("GET", "/s3/eng-artifacts", lower=True).status_code == 200
    assert "<ListBucketResult" in v2_query("/s3/eng-artifacts").text
    assert v2_query("/s3/eng-artifacts", expires=10 * 365 * 86400).status_code == 200
    tokens = {
        u["email"]: u["token"] for u in yaml.safe_load(settings.tokens_path.read_text())["users"]
    }
    ava = Credentials(
        synth.s3_access_key_id(tokens["ava@acme.com"]),
        synth.s3_secret_access_key(tokens["ava@acme.com"]),
    )
    assert "<Code>NoSuchBucket</Code>" in v2("GET", "/s3/people-vault/comp/bands.csv", ava).text
    assert v2("GET", "/s3/people-vault/comp/bands.csv").status_code == 200
    bad = v2("HEAD", "/s3/eng-artifacts", Credentials(admin.access_key, "x" * 40))
    assert (bad.status_code, bad.content) == (403, b"")
    s3 = boto3.client(
        "s3",
        endpoint_url=f"{base_url}/s3",
        aws_access_key_id=admin.access_key,
        aws_secret_access_key=admin.secret_key,
        region_name="us-east-1",
        config=Config(signature_version="s3", s3={"addressing_style": "path"}),
    )
    assert "eng-artifacts" in {b["Name"] for b in s3.list_buckets()["Buckets"]}
    assert (
        s3.get_object(Bucket="eng-artifacts", Key="runbooks/oncall.md")["Body"].read()
        == OBJECT_TEXT
    )
    assert s3.head_object(Bucket="eng-artifacts", Key="runbooks/oncall.md")["ContentLength"] == len(
        OBJECT_TEXT
    )
    for call in (
        lambda: s3.list_objects_v2(Bucket="eng-artifacts"),
        lambda: s3.get_bucket_versioning(Bucket="eng-artifacts"),
    ):
        with pytest.raises(ClientError) as e:
            call()
        assert e.value.response["Error"]["Code"] == "SignatureDoesNotMatch"


# --- the two listings ------------------------------------------------------------


def _children(root) -> list[str]:
    """The result's child tags in document order, without the namespace."""
    return [c.tag[len(f"{{{S3NS}}}") :] for c in root]


def _listing(client, query, token):
    r = _s3_get(client, f"/s3/encoded-bucket?{query}", token)
    assert r.status_code == 200, r.text
    return ET.fromstring(r.text)


def _versions(client, query, token):
    r = _s3_get(client, f"/s3/encoded-bucket?versions&{query}", token)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/xml"
    return ET.fromstring(r.text)


def _version_entries(root) -> list[str]:
    """Each Version's key and each CommonPrefixes' prefix, in the order they were served."""
    return [
        c.findtext(f"{{{S3NS}}}Key")
        if c.tag.endswith("Version")
        else c.findtext(f"{{{S3NS}}}Prefix")
        for c in root
        if c.tag.endswith("Version") or c.tag.endswith("CommonPrefixes")
    ]


def test_list_object_versions_is_each_key_as_its_one_null_version(
    big_bucket_client, big_bucket_settings
):
    """Each key as its one version, in the shape ``backlot.routers.s3._list_object_versions``
    records, the elements in real's order."""
    token = big_bucket_settings.admin_token
    root = _versions(big_bucket_client, "", token)
    assert root.tag == f"{{{S3NS}}}ListVersionsResult"
    assert _children(root) == [
        "Name",
        "Prefix",
        "KeyMarker",
        "VersionIdMarker",
        "MaxKeys",
        "IsTruncated",
        *["Version"] * 6,
    ]
    assert [
        root.findtext(f"{{{S3NS}}}{n}")
        for n in ("Name", "Prefix", "KeyMarker", "VersionIdMarker", "MaxKeys", "IsTruncated")
    ] == ["encoded-bucket", "", "", "", "1000", "false"]
    v1 = {
        c.findtext(f"{{{S3NS}}}Key"): c
        for c in _listing(big_bucket_client, "", token).findall(f"{{{S3NS}}}Contents")
    }
    versions = root.findall(f"{{{S3NS}}}Version")
    assert [v.findtext(f"{{{S3NS}}}Key") for v in versions] == list(v1)
    for version in versions:
        assert _children(version) == [
            "Key",
            "VersionId",
            "IsLatest",
            "LastModified",
            "ETag",
            "Size",
            "Owner",
            "StorageClass",
        ]
        assert version.findtext(f"{{{S3NS}}}VersionId") == "null"
        assert version.findtext(f"{{{S3NS}}}IsLatest") == "true"
        listed = v1[version.findtext(f"{{{S3NS}}}Key")]
        for name in ("LastModified", "ETag", "Size", "StorageClass"):
            assert version.findtext(f"{{{S3NS}}}{name}") == listed.findtext(f"{{{S3NS}}}{name}")
        assert version.findtext(f"{{{S3NS}}}Owner/{{{S3NS}}}ID") == listed.findtext(
            f"{{{S3NS}}}Owner/{{{S3NS}}}ID"
        )
        assert version.find(f"{{{S3NS}}}Owner/{{{S3NS}}}DisplayName") is None


@pytest.mark.parametrize(
    "extra", ["", "&delimiter=/", "&prefix=a", "&delimiter=/&encoding-type=url"]
)
def test_list_object_versions_pages_by_its_markers_to_the_end(
    big_bucket_client, big_bucket_settings, extra
):
    """Following `NextKeyMarker` and `NextVersionIdMarker` a page at a time walks every entry once,
    in key order, as ``backlot.routers.s3._list_object_versions`` records real's four keys doing.
    Under `encoding-type=url` a marker comes back encoded and goes back decoded."""
    token = big_bucket_settings.admin_token
    whole = _version_entries(_versions(big_bucket_client, extra.lstrip("&"), token))
    walked, query, pages = [], "max-keys=1" + extra, 0
    while True:
        page = _versions(big_bucket_client, query, token)
        pages += 1
        entries = _version_entries(page)
        assert len(entries) == 1, query
        walked += entries
        if page.findtext(f"{{{S3NS}}}IsTruncated") == "false":
            assert page.find(f"{{{S3NS}}}NextKeyMarker") is None
            break
        next_key = page.findtext(f"{{{S3NS}}}NextKeyMarker")
        assert next_key == entries[-1]
        is_group = page.find(f"{{{S3NS}}}CommonPrefixes") is not None
        assert (page.findtext(f"{{{S3NS}}}NextVersionIdMarker")) == (None if is_group else "null")
        sent = unquote(next_key.replace("+", " ")) if "encoding-type" in extra else next_key
        query = f"max-keys=1{extra}&key-marker={quote(sent, safe='')}"
        if not is_group:
            query += "&version-id-marker=null"
        echoed = _versions(big_bucket_client, query, token)
        assert echoed.findtext(f"{{{S3NS}}}KeyMarker") == next_key
        assert pages < 10
    # A page of one holds a key or a group alone, so the walk is key order, where a whole page puts
    # its groups after its keys (see the delimiter test below).
    assert sorted(walked) == sorted(whole) and len(walked) == len(set(walked)) > 1
    if "encoding-type" not in extra:
        assert walked == sorted(walked, key=str.encode)


def test_list_object_versions_rolls_up_by_delimiter_and_encodes_under_url(
    big_bucket_client, big_bucket_settings
):
    """Versions and then CommonPrefixes, `Delimiter` and `EncodingType` as
    ``backlot.routers.s3._list_object_versions`` records them, and every key, prefix, marker and the
    delimiter encoded under `encoding-type=url`."""
    token = big_bucket_settings.admin_token
    rolled = _versions(big_bucket_client, "delimiter=/", token)
    assert _version_entries(rolled) == [
        "100%.csv",
        "a b.txt",
        "a+b.txt",
        "zz.txt",
        "run books/",
        "한글/",
    ]
    assert rolled.findtext(f"{{{S3NS}}}Delimiter") == "/"
    assert _versions(big_bucket_client, "", token).find(f"{{{S3NS}}}Delimiter") is None
    assert _versions(big_bucket_client, "delimiter=", token).findtext(f"{{{S3NS}}}Delimiter") == ""
    encoded = _versions(
        big_bucket_client, "encoding-type=URL&delimiter=%20&key-marker=100%25.csv&prefix=", token
    )
    assert encoded.findtext(f"{{{S3NS}}}EncodingType") == "URL"
    assert encoded.findtext(f"{{{S3NS}}}Delimiter") == "+"
    assert encoded.findtext(f"{{{S3NS}}}KeyMarker") == "100%25.csv"
    assert _version_entries(encoded) == [
        "a%2Bb.txt",
        "zz.txt",
        "%ED%95%9C%EA%B8%80/x.txt",
        "a+",
        "run+",
    ]
    page = _versions(big_bucket_client, "encoding-type=url&max-keys=1&key-marker=100%25.csv", token)
    assert page.findtext(f"{{{S3NS}}}NextKeyMarker") == "a+b.txt"
    assert (
        _versions(big_bucket_client, "prefix=a%20&encoding-type=url", token).findtext(
            f"{{{S3NS}}}Prefix"
        )
        == "a+"
    )


@pytest.mark.parametrize(
    "query, entries, max_keys, truncated",
    [
        ("max-keys=0", [], "0", "false"),
        (
            "max-keys=05",
            ["100%.csv", "a b.txt", "a+b.txt", "run books/x.txt", "zz.txt"],
            "5",
            "true",
        ),
        ("max-keys=-0", [], "0", "false"),
        ("max-keys=1001", None, "1001", "false"),
        ("max-keys=", None, "1000", "false"),
        ("key-marker=zz.txt", ["한글/x.txt"], "1000", "false"),
        ("key-marker=zzz&prefix=", ["한글/x.txt"], "1000", "false"),
        ("prefix=run%20", ["run books/x.txt"], "1000", "false"),
        ("prefix=run%20&key-marker=a", ["run books/x.txt"], "1000", "false"),
        ("prefix=run%20&key-marker=run%20books%2Fx.txt", [], "1000", "false"),
        ("key-marker=run%20books%2F&delimiter=/", ["zz.txt", "한글/"], "1000", "false"),
        ("key-marker=&version-id-marker=null", [], "1000", "false"),
        (
            "key-marker=a%20b.txt&version-id-marker=null",
            ["a+b.txt", "run books/x.txt", "zz.txt", "한글/x.txt"],
            "1000",
            "false",
        ),
        ("start-after=zz.txt&marker=zz.txt&list-type=2", None, "1000", "false"),
    ],
)
def test_list_object_versions_reads_its_bounds_as_real_does(
    big_bucket_client, big_bucket_settings, query, entries, max_keys, truncated
):
    """The bounds ``backlot.routers.s3._versions_parse`` and
    ``backlot.routers.s3._list_object_versions`` record: `max-keys` echoed uncapped, `key-marker`
    past a key or its group, `version-id-marker=null` where `key-marker` alone resumes and beside an
    empty `key-marker` a page of nothing; and the listings' own parameters ignored (measured on
    real's four keys, 2026-09-29)."""
    root = _versions(big_bucket_client, query, big_bucket_settings.admin_token)
    whole = ["100%.csv", "a b.txt", "a+b.txt", "run books/x.txt", "zz.txt", "한글/x.txt"]
    assert _version_entries(root) == (whole if entries is None else entries)
    assert root.findtext(f"{{{S3NS}}}MaxKeys") == max_keys
    assert root.findtext(f"{{{S3NS}}}IsTruncated") == truncated


def _entries(root) -> list[str]:
    """Keys and CommonPrefixes together, in the order they were served."""
    return [
        c.findtext(f"{{{S3NS}}}Key")
        if c.tag.endswith("Contents")
        else c.findtext(f"{{{S3NS}}}Prefix")
        for c in root
        if c.tag.endswith("Contents") or c.tag.endswith("CommonPrefixes")
    ]


def test_a_bare_bucket_get_is_list_objects_and_only_list_type_2_is_its_v2_form(
    big_bucket_client, big_bucket_settings
):
    """#188: the two listings are different bodies, and `list-type` alone chooses between them.

    Measured against a bucket in ap-northeast-2 on 2026-09-14: V1 carries `Marker` and a per-object
    `Owner` and no `KeyCount`; V2 carries `KeyCount` and a `NextContinuationToken` and no `Owner`.
    Every value of `list-type` other than `2` — `1`, `0`, a word — answers V1, the same as sending
    none at all.
    """
    token = big_bucket_settings.admin_token
    v1 = _listing(big_bucket_client, "max-keys=1", token)
    assert _children(v1) == ["Name", "Prefix", "Marker", "MaxKeys", "IsTruncated", "Contents"]
    # Present and empty when none was sent, which is what real answers and not what the reference
    # says ("Marker is included in the response if it was sent with the request").
    assert v1.findtext(f"{{{S3NS}}}Marker") == ""
    sent = _listing(big_bucket_client, "marker=" + quote("a b.txt"), token)
    assert sent.findtext(f"{{{S3NS}}}Marker") == "a b.txt"
    v2 = _listing(big_bucket_client, "list-type=2&max-keys=1", token)
    # V2's echo goes the other way: the element is there when the parameter was sent, an empty
    # value included, and absent when it was not — `?list-type=2&start-after=` answers
    # `<StartAfter></StartAfter>` (measured 2026-09-17).
    assert v2.find(f"{{{S3NS}}}StartAfter") is None
    assert (
        _listing(big_bucket_client, "list-type=2&start-after=", token).findtext(
            f"{{{S3NS}}}StartAfter"
        )
        == ""
    )
    assert _children(v2) == [
        "Name",
        "Prefix",
        "NextContinuationToken",
        "KeyCount",
        "MaxKeys",
        "IsTruncated",
        "Contents",
    ]
    owner = f"{{{S3NS}}}Contents/{{{S3NS}}}Owner"
    assert v1.find(owner) is not None and list(v1.find(owner)) != []
    assert [c.tag[len(f"{{{S3NS}}}") :] for c in v1.find(owner)] == ["ID"]
    assert v2.find(owner) is None
    for value in ("1", "0", "bogus"):
        other = _listing(big_bucket_client, f"list-type={value}&max-keys=1", token)
        assert _children(other) == _children(v1), value
    # The order with every element present, which the two pages above cannot show. Real's, measured
    # 2026-09-14: the cursors sit between Prefix and KeyCount on V2 and before MaxKeys on V1, and
    # Delimiter and EncodingType sit between MaxKeys and IsTruncated on both.
    slash = quote("/")
    full_v2 = _listing(
        big_bucket_client,
        f"list-type=2&delimiter={slash}&start-after={quote('100%.csv')}&encoding-type=url"
        "&max-keys=1",
        token,
    )
    assert _children(full_v2)[:9] == [
        "Name",
        "Prefix",
        "StartAfter",
        "NextContinuationToken",
        "KeyCount",
        "MaxKeys",
        "Delimiter",
        "EncodingType",
        "IsTruncated",
    ]
    full_v1 = _listing(
        big_bucket_client,
        f"delimiter={slash}&marker={quote('100%.csv')}&encoding-type=url&max-keys=1",
        token,
    )
    assert _children(full_v1)[:8] == [
        "Name",
        "Prefix",
        "Marker",
        "NextMarker",
        "MaxKeys",
        "Delimiter",
        "EncodingType",
        "IsTruncated",
    ]


def test_every_contents_is_written_before_every_common_prefixes(
    big_bucket_client, big_bucket_settings
):
    """Real groups the two rather than interleaving them by key.

    Measured 2026-09-14 and again 2026-09-16 over these same six keys: `?delimiter=/` comes back
    with `100%.csv`, `a b.txt`, `a+b.txt` and `zz.txt` as `Contents` and then `run books/` and
    `한글/` as `CommonPrefixes`, so `run books/` follows `zz.txt` although it sorts before it. Both
    listings answer that way, and `encoding-type=url` does not move anything.

    The set of elements is the same either way, which is why the probe's child-set comparison
    cannot see this and `backlot diff` reports nothing: only the document order says it.
    """
    token = big_bucket_settings.admin_token
    grouped = ["100%.csv", "a b.txt", "a+b.txt", "zz.txt", "run books/", "한글/"]
    for query in ("delimiter=/", "list-type=2&delimiter=/"):
        root = _listing(big_bucket_client, query, token)
        assert _entries(root) == grouped, query
        kinds = [
            c.tag.split("}")[1] for c in root if c.tag.endswith(("Contents", "CommonPrefixes"))
        ]
        assert kinds == ["Contents"] * 4 + ["CommonPrefixes"] * 2, query
    encoded = _listing(
        big_bucket_client, "list-type=2&encoding-type=url&delimiter=" + quote("/"), token
    )
    assert _entries(encoded) == [
        "100%25.csv",
        "a+b.txt",
        "a%2Bb.txt",
        "zz.txt",
        "run+books/",
        "%ED%95%9C%EA%B8%80/",
    ]
    # A page with no delimiter has no CommonPrefixes to move, and stays in key order.
    assert _entries(_listing(big_bucket_client, "max-keys=3", token)) == [
        "100%.csv",
        "a b.txt",
        "a+b.txt",
    ]


def test_list_objects_names_a_next_marker_only_under_a_delimiter_and_pages_to_the_end(
    big_bucket_client, big_bucket_settings
):
    """#188: V1's cursor is `NextMarker`, and real sends one only when a `delimiter` is set.

    Without one a truncated page carries no cursor at all and botocore falls back to the last key
    it saw, which is the fallback this has to leave intact. With one, `NextMarker` names the page's
    last entry BY KEY — a key or a rolled-up prefix, and not always the element the body ends on,
    since every CommonPrefixes is written after every Contents — and sending it back as `marker`
    resumes past that whole entry: real answers `?delimiter=/&marker=docs/` and
    `?delimiter=/&marker=docs/a.txt` alike with what follows `docs/`, never `docs/` again, so the
    walk terminates (measured 2026-09-14).
    """
    token = big_bucket_settings.admin_token
    flat = _listing(big_bucket_client, "max-keys=1", token)
    assert flat.findtext(f"{{{S3NS}}}IsTruncated") == "true"
    assert flat.find(f"{{{S3NS}}}NextMarker") is None
    rolled = _listing(big_bucket_client, "delimiter=/&max-keys=1", token)
    assert rolled.findtext(f"{{{S3NS}}}NextMarker") == _entries(rolled)[-1]
    # The page above ends on a plain key, where the last entry and the last raw key are the same
    # string. This one ends on a rolled-up prefix, which is what real names — `run books/`, not the
    # `run books/x.txt` underneath it.
    on_a_group = _listing(big_bucket_client, "delimiter=/&max-keys=4", token)
    assert _entries(on_a_group) == ["100%.csv", "a b.txt", "a+b.txt", "run books/"]
    assert on_a_group.findtext(f"{{{S3NS}}}NextMarker") == "run books/"
    # One key further in, the entry real names is no longer the element the body ends on: the page
    # carries four Contents and then `run books/`, and `NextMarker` is `zz.txt`, the last entry by
    # key (measured 2026-09-16). Reading the last element of the body instead would send the walk
    # back over `zz.txt`.
    past_the_group = _listing(big_bucket_client, "delimiter=/&max-keys=5", token)
    assert _entries(past_the_group) == ["100%.csv", "a b.txt", "a+b.txt", "zz.txt", "run books/"]
    assert past_the_group.findtext(f"{{{S3NS}}}NextMarker") == "zz.txt"

    seen, marker, pages = [], None, 0
    while pages < 10:
        query = "delimiter=/&max-keys=1" + (f"&marker={quote(marker)}" if marker else "")
        page = _listing(big_bucket_client, query, token)
        seen += _entries(page)
        pages += 1
        if page.findtext(f"{{{S3NS}}}IsTruncated") != "true":
            break
        marker = page.findtext(f"{{{S3NS}}}NextMarker")
    assert seen == ["100%.csv", "a b.txt", "a+b.txt", "run books/", "zz.txt", "한글/"]
    assert len(seen) == len(set(seen))
    # A marker inside a group skips the rest of it, exactly as one naming the group does.
    inside = _listing(big_bucket_client, "delimiter=/&marker=" + quote("run books/x.txt"), token)
    assert _entries(inside) == ["zz.txt", "한글/"]


def test_a_parameter_of_the_other_listing_is_refused_before_the_bucket_is_looked_up(
    big_bucket_client, big_bucket_settings
):
    """#188: `marker` under V2 and `start-after`/`continuation-token` without it are refused.

    Each carries its own message and an `ArgumentName` with no `ArgumentValue` beside it, an empty
    value is refused like any other, and a bucket that does not exist still takes the 400 rather
    than NoSuchBucket. On a V1 request carrying both V2 parameters real names `continuation-token`
    (all measured 2026-09-14).
    """
    token = big_bucket_settings.admin_token
    cases = (
        (
            "list-type=2&marker=x",
            "Marker unsupported with REST.GET.BUCKET in list-type=2",
            "marker",
        ),
        (
            "start-after=x",
            "startAfter only supported in REST.GET.BUCKET with list-type=2",
            "start-after",
        ),
        (
            "continuation-token=x",
            "continuation-token only supported in REST.GET.BUCKET with list-type=2",
            "continuation-token",
        ),
    )
    for query, message, name in cases:
        for bucket in ("encoded-bucket", "no-such-bucket-here"):
            r = _s3_get(big_bucket_client, f"/s3/{bucket}?{query}", token)
            assert r.status_code == 400, (bucket, query, r.text)
            assert f"<Message>{message}</Message>" in r.text, query
            assert f"<ArgumentName>{name}</ArgumentName>" in r.text, query
            assert "<ArgumentValue>" not in r.text, query
        empty = _s3_get(big_bucket_client, f"/s3/encoded-bucket?{query[:-1]}", token)
        assert empty.status_code == 400, query
    both = _s3_get(
        big_bucket_client, "/s3/encoded-bucket?start-after=x&continuation-token=y", token
    )
    assert "<ArgumentName>continuation-token</ArgumentName>" in both.text


def test_the_listing_encodes_under_encoding_type_url_as_real_does(
    big_bucket_client, big_bucket_settings
):
    """#178: `encoding-type=url` is read, echoed and applied to every key and prefix.

    boto3 sets it on every `list_objects`/`list_objects_v2` the caller did not
    (`botocore/handlers.py`, `set_list_objects_encoding_type_url`) and Cyberduck puts it on every
    listing it issues, so this is the query the common clients send. Measured 2026-09-14 on a
    bucket holding these keys: a space is `+`, a literal `+` is `%2B`, `%` is `%25` and the UTF-8
    of a non-ASCII character is `%XX` per byte, in `Key` and `CommonPrefixes/Prefix` alike. The
    continuation tokens are left alone. Ordering is on the stored key, not the encoded one, which
    is why `a b.txt` comes back before `a+b.txt` where encoding first would swap them (`%` is 0x25
    and `+` is 0x2B).
    """
    token = big_bucket_settings.admin_token
    plain = _listing(big_bucket_client, "list-type=2&max-keys=3", token)
    assert _entries(plain) == ["100%.csv", "a b.txt", "a+b.txt"]
    assert plain.find(f"{{{S3NS}}}EncodingType") is None
    encoded = _listing(big_bucket_client, "list-type=2&encoding-type=url&max-keys=3", token)
    assert _entries(encoded) == ["100%25.csv", "a+b.txt", "a%2Bb.txt"]
    assert encoded.findtext(f"{{{S3NS}}}EncodingType") == "url"
    # The token is opaque and stays as it is, `=` padding included.
    assert encoded.findtext(f"{{{S3NS}}}NextContinuationToken") == plain.findtext(
        f"{{{S3NS}}}NextContinuationToken"
    )
    rolled = _listing(
        big_bucket_client, "list-type=2&encoding-type=url&delimiter=" + quote("/"), token
    )
    assert "run+books/" in _entries(rolled) and "%ED%95%9C%EA%B8%80/" in _entries(rolled)
    # The echoes of what was sent go through the encoder too, not just the keys.
    echoes = _listing(
        big_bucket_client,
        "list-type=2&encoding-type=url&prefix="
        + quote("run books/")
        + "&delimiter="
        + quote("|")
        + "&start-after="
        + quote("a b.txt"),
        token,
    )
    assert echoes.findtext(f"{{{S3NS}}}Prefix") == "run+books/"
    assert echoes.findtext(f"{{{S3NS}}}Delimiter") == "%7C"
    assert echoes.findtext(f"{{{S3NS}}}StartAfter") == "a+b.txt"
    # V1's own echoes go through the same encoder, `NextMarker` included.
    v1 = _listing(
        big_bucket_client,
        "encoding-type=url&delimiter=" + quote("/") + "&marker=" + quote("a b.txt") + "&max-keys=1",
        token,
    )
    assert v1.findtext(f"{{{S3NS}}}Marker") == "a+b.txt"
    assert v1.findtext(f"{{{S3NS}}}NextMarker") == "a%2Bb.txt"
    # `URL` is taken like `url` and echoed as sent; anything else is refused, empty included.
    assert (
        _listing(big_bucket_client, "list-type=2&encoding-type=URL&max-keys=1", token).findtext(
            f"{{{S3NS}}}EncodingType"
        )
        == "URL"
    )
    for value in ("bogus", ""):
        r = _s3_get(
            big_bucket_client, f"/s3/encoded-bucket?list-type=2&encoding-type={value}", token
        )
        assert r.status_code == 400, value
        assert "<Message>Invalid Encoding Method specified in Request</Message>" in r.text, value
        assert (
            f"<ArgumentName>encoding-type</ArgumentName><ArgumentValue>{value}</ArgumentValue>"
            in r.text
        ), value


def test_a_continuation_token_that_does_not_decode_is_refused_not_answered_with_page_one(
    big_bucket_client, big_bucket_settings
):
    """#205: a `continuation-token` Backlot cannot read is a 400, and an empty one is the same 400.

    Real answers both "The continuation token provided is incorrect" under `ArgumentName`
    `continuation-token` with no `ArgumentValue` beside it, and answers neither with a page
    (measured 2026-09-17 against a bucket in ap-northeast-2). Where the refusal sits is measured
    too: `encoding-type` is judged before it, the `max-keys` range after it, the `max-keys` parse
    before the bucket is looked up at all, and a bucket that does not exist is NoSuchBucket for an
    unreadable token and an empty one alike.

    A token that decodes is served, which is what keeps paging working — and it is served even when
    this listing never handed it out, where real refuses it. Why the two cannot be told apart here
    is in `_list_objects`'s docstring. Real's own check is not a check on a token's shape: one it
    issued with its final character replaced comes back 200, echoed as sent (measured the same
    day).
    """
    token = big_bucket_settings.admin_token
    message = "The continuation token provided is incorrect"

    def refused(query):
        r = _s3_get(big_bucket_client, f"/s3/encoded-bucket?{query}", token)
        assert r.status_code == 400, (query, r.text)
        return r.text

    # Sent and unreadable, an empty value among them: not base64, base64 of bytes that are not
    # UTF-8, and base64 of a string this listing does not spell its bounds with.
    for value in ("garbage", "", "!!!!", "a" * 200, base64.urlsafe_b64encode(b"x:zz").decode()):
        body = refused(f"list-type=2&continuation-token={quote(value)}")
        assert f"<Message>{message}</Message>" in body, value
        assert "<ArgumentName>continuation-token</ArgumentName>" in body, value
        assert "<ArgumentValue>" not in body, value
        # No page came back with it, and nothing echoed the token as if one had.
        assert "<Contents>" not in body and "<ContinuationToken>" not in body, value

    # The order among the listing's refusals, with the bucket lookup in the middle of
    # it: the `max-keys` parse is judged above the lookup, this refusal below it.
    assert "<ArgumentName>encoding-type</ArgumentName>" in refused(
        "list-type=2&continuation-token=garbage&encoding-type=bogus"
    )
    assert f"<Message>{message}</Message>" in refused(
        "list-type=2&continuation-token=garbage&max-keys=-1"
    )
    assert "<ArgumentName>max-keys</ArgumentName>" in refused(
        "list-type=2&continuation-token=garbage&max-keys=abc"
    )
    for value in ("garbage", ""):
        missing = _s3_get(
            big_bucket_client, f"/s3/no-such-bucket?list-type=2&continuation-token={value}", token
        )
        assert missing.status_code == 404, value
        assert "<Code>NoSuchBucket</Code>" in missing.text, value
    # `start-after` is not reached once the token is refused, an empty token included.
    assert f"<Message>{message}</Message>" in refused(
        "list-type=2&continuation-token=garbage&start-after=zz.txt"
    )
    assert f"<Message>{message}</Message>" in refused(
        "list-type=2&continuation-token=&start-after="
    )

    # The first of a repeated parameter is the one read, here as everywhere (see `_first`).
    issued = _listing(big_bucket_client, "list-type=2&max-keys=1", token).findtext(
        f"{{{S3NS}}}NextContinuationToken"
    )
    assert f"<Message>{message}</Message>" in refused(
        f"list-type=2&continuation-token=garbage&continuation-token={quote(issued)}"
    )
    good_first = _listing(
        big_bucket_client,
        f"list-type=2&max-keys=1&continuation-token={quote(issued)}&continuation-token=garbage",
        token,
    )
    assert good_first.findtext(f"{{{S3NS}}}ContinuationToken") == issued

    # The parameter name is matched as sent: a capitalised one selects nothing, so the listing is
    # page one and echoes no token at all.
    capitalised = _listing(big_bucket_client, "list-type=2&Continuation-Token=garbage", token)
    assert capitalised.find(f"{{{S3NS}}}ContinuationToken") is None
    assert _entries(capitalised) == _entries(_listing(big_bucket_client, "list-type=2", token))

    # A token displaces `start-after` rather than being weighed against it, and displaces its echo
    # too: real sends no `StartAfter` element at all when both were sent, where `start-after` alone
    # is echoed (measured 2026-09-17).
    displaced = _listing(
        big_bucket_client,
        f"list-type=2&max-keys=1&start-after=zz.txt&continuation-token={quote(issued)}",
        token,
    )
    assert displaced.find(f"{{{S3NS}}}StartAfter") is None
    assert displaced.findtext(f"{{{S3NS}}}ContinuationToken") == issued
    assert _entries(displaced) == ["a b.txt"]

    # A token this listing handed out still pages, and so does one a caller spelled by hand.
    page_two = _listing(
        big_bucket_client, f"list-type=2&max-keys=1&continuation-token={quote(issued)}", token
    )
    assert page_two.findtext(f"{{{S3NS}}}ContinuationToken") == issued
    assert _entries(page_two) == ["a b.txt"]
    hand_written = base64.urlsafe_b64encode(b"k:zz.txt").decode()
    served = _listing(
        big_bucket_client, f"list-type=2&continuation-token={quote(hand_written)}", token
    )
    assert served.findtext(f"{{{S3NS}}}ContinuationToken") == hand_written
    assert _entries(served) == ["한글/x.txt"]


def test_max_keys_is_read_by_value_and_refused_with_the_two_messages_real_sends(
    big_bucket_client, big_bucket_settings
):
    """#192: `max-keys` is parsed the way `max-uploads` is, and its refusals spell it two ways.

    A value that does not parse as an int32 is "Provided max-keys not an integer or within integer
    range" under `ArgumentName` `max-keys`; one that parses but is below zero is "Argument maxKeys
    must be an integer between 0 and 2147483647" under `maxKeys`, camel-cased. The split is also a
    split around the bucket lookup: the parse happens before it and the range after, which a bucket
    that does not exist tells apart. What is served is capped at 1000 but the echo is the value as
    parsed, and `max-keys=0` is an empty page whose IsTruncated is false (all measured 2026-09-14,
    the spellings on both forms of the listing).
    """
    token = big_bucket_settings.admin_token
    not_an_integer = "Provided max-keys not an integer or within integer range"
    out_of_range = f"Argument maxKeys must be an integer between 0 and {2147483647}"
    for prefix in ("", "list-type=2&"):
        for value, message, name in (
            ("abc", not_an_integer, "max-keys"),
            (" 5", not_an_integer, "max-keys"),
            ("2147483648", not_an_integer, "max-keys"),
            ("-2147483649", not_an_integer, "max-keys"),
            ("-1", out_of_range, "maxKeys"),
            ("-2147483648", out_of_range, "maxKeys"),
        ):
            r = _s3_get(
                big_bucket_client, f"/s3/encoded-bucket?{prefix}max-keys={quote(value)}", token
            )
            assert r.status_code == 400, (prefix, value, r.text)
            assert f"<Message>{message}</Message>" in r.text, (prefix, value)
            assert f"<ArgumentName>{name}</ArgumentName>" in r.text, (prefix, value)
    # `-01` is reported as `-1`: the value as parsed, not as sent.
    r = _s3_get(big_bucket_client, "/s3/encoded-bucket?max-keys=-01", token)
    assert "<ArgumentValue>-1</ArgumentValue>" in r.text
    # The parse is before the bucket lookup and the range after it.
    assert _s3_get(big_bucket_client, "/s3/no-such-bucket?max-keys=abc", token).status_code == 400
    missing = _s3_get(big_bucket_client, "/s3/no-such-bucket?max-keys=-1", token)
    assert missing.status_code == 404 and "<Code>NoSuchBucket</Code>" in missing.text
    # Empty is the default, leading zeros come off, `-0` is 0, and the echo is uncapped.
    assert (
        _listing(big_bucket_client, "list-type=2&max-keys=", token).findtext(f"{{{S3NS}}}MaxKeys")
        == "1000"
    )
    assert (
        _listing(big_bucket_client, "list-type=2&max-keys=05", token).findtext(f"{{{S3NS}}}MaxKeys")
        == "5"
    )
    past_cap = _listing(big_bucket_client, "list-type=2&max-keys=1001", token)
    assert past_cap.findtext(f"{{{S3NS}}}MaxKeys") == "1001"
    assert len(_entries(past_cap)) == 6
    zero = _listing(big_bucket_client, "list-type=2&max-keys=0", token)
    assert zero.findtext(f"{{{S3NS}}}MaxKeys") == "0"
    assert zero.findtext(f"{{{S3NS}}}IsTruncated") == "false" and _entries(zero) == []


def test_a_repeated_parameter_is_read_as_its_first_value_including_list_type(
    big_bucket_client, big_bucket_settings
):
    """#178: real reads the first of a repeated parameter, and so does the listing.

    Both go through `_first`, which `?uploads` already used (#176), where Starlette's
    `QueryParams.get` gives the last. Measured 2026-09-14: `?prefix=a&prefix=zz.txt` lists under
    `a`, and `?max-keys=1&max-keys=abc` is a page of one rather than the refusal `abc` would be.
    `list-type` is read the same way, so the first of two spellings picks the shape.
    """
    token = big_bucket_settings.admin_token
    assert (
        _listing(big_bucket_client, "list-type=2&max-keys=1&max-keys=abc", token).findtext(
            f"{{{S3NS}}}KeyCount"
        )
        == "1"
    )
    assert (
        _listing(big_bucket_client, "list-type=2&prefix=a&prefix=zz.txt", token).findtext(
            f"{{{S3NS}}}Prefix"
        )
        == "a"
    )
    assert _children(_listing(big_bucket_client, "list-type=1&list-type=2", token))[2] == "Marker"
    assert "KeyCount" in _children(_listing(big_bucket_client, "list-type=2&list-type=1", token))


def test_a_page_whose_trailing_group_has_no_successor_is_complete_not_truncated(
    big_bucket_client, big_bucket_settings
):
    """A rolled-up group under the last code point has no bound to resume past, and a page ending
    on it carries every entry there is: a key sorting after that group cannot be spelled, so every
    row still unfetched rolls up into the CommonPrefixes entry already on the page. It reports
    itself complete, rather than truncated with no cursor to leave it by, which real never sends.

    Both listings reach it — V2 through the group token and V1 through `NextMarker` — and the walk
    ends on the first page either way."""
    token = big_bucket_settings.admin_token
    last = quote("\U0010ffff")
    for query in (f"list-type=2&delimiter={last}&max-keys=1", f"delimiter={last}&max-keys=1"):
        r = _s3_get(big_bucket_client, f"/s3/edge-bucket?{query}", token)
        assert r.status_code == 200, r.text
        root = ET.fromstring(r.text)
        assert _entries(root) == ["\U0010ffff"], query
        assert root.findtext(f"{{{S3NS}}}IsTruncated") == "false", query
        assert root.find(f"{{{S3NS}}}NextContinuationToken") is None, query
        assert root.find(f"{{{S3NS}}}NextMarker") is None, query
    # The guard is only for the group with no successor: a group that has one still pages.
    ordinary = _listing(big_bucket_client, "delimiter=/&max-keys=1", token)
    assert ordinary.findtext(f"{{{S3NS}}}IsTruncated") == "true"


@pytest.mark.parametrize("edge", ["\U0010ffff", "\ud7ff"])
def test_a_prefix_or_marker_no_character_steps_past_is_a_page_not_a_500(
    big_bucket_client, big_bucket_settings, edge
):
    """Both parameters take any character a client sends, and two of them have no character the
    key-range helper can step onto: the last code point has nothing above it at all, and U+D7FF
    has only the surrogate block, which UTF-8 cannot encode and sqlite3 cannot bind. The listing's
    own `marker` reaches the same helper through the CommonPrefixes group it resumes past. Neither
    is a listing anyone wants; both are answered.

    What real S3 does with these is unmeasured — a delimiter of `\U0010ffff` is not a query worth
    a bucket — so the only claim here is that the server answers rather than crashes."""
    token = big_bucket_settings.admin_token
    for query in (
        f"prefix=a{quote(edge)}",
        f"list-type=2&prefix=a{quote(edge)}",
        f"delimiter={quote(edge)}&marker=a{quote(edge)}",
        f"delimiter={quote(edge)}",
        f"prefix={quote(edge)}",
    ):
        r = _s3_get(big_bucket_client, f"/s3/encoded-bucket?{query}", token)
        assert r.status_code == 200, (query, r.status_code, r.text[:200])
        assert ET.fromstring(r.text).tag == f"{{{S3NS}}}ListBucketResult", query
