"""Amazon S3 API (read-only, object storage).

Path-style endpoint for a client: ``http://<host>/s3`` (boto3: ``endpoint_url=".../s3"`` with
``addressing_style=path``; mirage: ``S3Config(endpoint_url=".../s3", path_style=True)``). Auth is
full AWS SigV4 (``backlot.auth.resolve_sigv4``) against a per-caller access-key/secret derived from a
bearer token; the admin/service token's key sees everything, a user's key is ACL-filtered.
Responses are S3 XML (namespace ``http://s3.amazonaws.com/doc/2006-03-01/``) or raw object bytes;
errors use the S3 ``<Error>`` envelope.

S3 dispatches on the query string: ``?acl``, ``?versioning``, ``?tagging`` and the rest each select a
different operation at the same path. Two of them are answered at a bucket's path, ``?location``
(GetBucketLocation) and ``?uploads`` (ListMultipartUploads, always the empty page, since data
enters through ``backlot import`` and no upload is ever in progress). The ones Backlot does not
implement are refused with ``NotImplemented`` (501) rather than answered with the listing or the
object's bytes, so a caller gets an error to handle instead of another operation's body to parse.

Object model: a bucket is the grouping/ACL unit (``s3_buckets``); an object is one doc
(``s3_objects``), ``key`` is its address and ``content`` its verbatim body. "Folders" are pure
key-prefix convention surfaced via ListObjectsV2's ``delimiter``/``CommonPrefixes``.
"""

from __future__ import annotations

import base64
import re
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request, Response

from backlot import auth, store, synth
from backlot.openapi import qp

router = APIRouter(prefix="/s3", tags=["s3"])
# A signed S3 request's canonical path is exact; letting Starlette 307-redirect a bare "/s3" ->
# "/s3/" would both break SigV4 (the redirected request no longer matches what was signed) and
# send botocore's bucket-region-redirect logic haywire. ListBuckets is registered for both
# "/s3" and "/s3/" below so the exact path always matches directly and no redirect is ever
# triggered (setting `router.redirect_slashes = False` here would be a no-op once this
# sub-router is flattened into the app by `include_router`).

NS = "http://s3.amazonaws.com/doc/2006-03-01/"
_MAX_KEYS = 1000
# Only this value of `list-type` selects the V2 shape. Real S3 answers the V1 shape for the
# parameter absent and for every other value alike — `list-type=1`, `list-type=0` and
# `list-type=bogus` all come back with `Marker` and without `KeyCount` (measured 2026-09-14).
_LIST_TYPE_V2 = "2"
# A parameter that belongs to the other version is refused, not ignored, and before the bucket is
# looked up: each of these on a bucket that does not exist is the 400 and not NoSuchBucket. The
# message is its own, the error carries `ArgumentName` and NO `ArgumentValue`, and an empty value
# is refused the same as any other. A V1 request carrying both of the V2 parameters is refused for
# `continuation-token`, which is why it comes first here (all measured 2026-09-14).
_V2_ONLY = (
    ("continuation-token", "continuation-token only supported in REST.GET.BUCKET with list-type=2"),
    ("start-after", "startAfter only supported in REST.GET.BUCKET with list-type=2"),
)
_V1_ONLY = (("marker", "Marker unsupported with REST.GET.BUCKET in list-type=2"),)
# ListMultipartUploads' own ceiling, which is also its default: "The limit of 1,000 multipart uploads
# is also the default value" (the S3 API reference on ListMultipartUploads). A larger `max-uploads`
# is served at the cap rather than refused (measured: 1001 and 2000 both echo 1000).
_MAX_UPLOADS = 1000
# The widest `max-uploads` real S3 parses: "Argument max-uploads must be an integer between 0 and
# 2147483647" is its own message for a negative value (measured).
_INT32_MAX = 2147483647
# What `encoding-type=url` leaves as it is. Measured on real S3 against ListMultipartUploads: letters,
# digits, `-`, `_`, `.`, `*` and `/` come back unchanged, a space comes back as `+`, and each of the
# other bytes sent — `!'()~,;:@="<>|%` and the UTF-8 of `한글` — as `%XX` in upper case (`!` is `%21`,
# `~` is `%7E`, `|` is `%7C`, `한글` is `%ED%95%9C%EA%B8%80`). That is Java's URLEncoder with `/` added
# to its safe set, which is the rule applied to the bytes not sent; Python's `quote_plus` is not it,
# keeping `~` and encoding `*`.
_URL_ENCODING_SAFE = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.*/"
)

# Read off the raw request rather than through FastAPI signatures, so each has to be declared by
# hand (see openapi.qp). What is declared is what selects an operation or decides which keys come
# back, which is why `list-type`, `marker` and `encoding-type` are declared below: the first
# chooses between the two listings, the second pages the V1 one, and the third changes how every
# key comes back.
# ListMultipartUploads' own parameters (`max-uploads`, `key-marker`, `upload-id-marker`,
# `encoding-type`) are absent for a different reason. They are not inert: _int32_param and
# _list_multipart_uploads read all four, `max-uploads`, `key-marker` and `encoding-type` come back
# echoed, and `max-uploads`, `encoding-type` and `upload-id-marker` can each turn the 200 into an
# InvalidArgument. What none of them does is decide which uploads a caller gets, because there are
# never any — all they shape is an echo of the caller's own input on a page that is always empty.
# Declaring them would advertise a paging surface, a marker to resume from and a page size, over a
# listing that never has a second page.
_P_BUCKET_GET = [
    qp("prefix"),
    qp(
        "delimiter",
        description="roll keys sharing a prefix up to this separator into CommonPrefixes; "
        "unset lists every key flat",
    ),
    qp(
        "list-type",
        description="2 selects ListObjectsV2 (KeyCount, continuation tokens); any other value, "
        "and the parameter absent, selects ListObjects (Marker, NextMarker, per-object Owner)",
    ),
    qp(
        "marker",
        description="ListObjects only: resume after this key, or past the CommonPrefixes group "
        "holding it — refused under list-type=2",
    ),
    qp(
        "start-after",
        description="ListObjectsV2 only: seeds the FIRST page; continuation-token wins over it — "
        "refused without list-type=2",
    ),
    qp(
        "encoding-type",
        description="url: every key, prefix, delimiter and marker in the response comes back "
        "URL-encoded, under an EncodingType element; any other value is refused",
    ),
    qp(
        "max-keys",
        "integer",
        description=f"keys per page, served at most {_MAX_KEYS}, which is also the default; "
        "MaxKeys echoes the value as parsed, uncapped",
    ),
    qp(
        "continuation-token",
        description="ListObjectsV2 only: NextContinuationToken from a page whose IsTruncated was "
        "true — refused without list-type=2",
    ),
    qp(
        "location",
        description="present, at any value: answer the bucket's LocationConstraint "
        "instead of a listing",
    ),
    qp(
        "uploads",
        description="present, at any value: answer ListMultipartUploads instead of a listing — "
        "always the empty page, since data enters through backlot import and no upload is "
        "ever in progress",
    ),
]
_ERR_STATUS = {
    "MissingSecurityHeader": 403,
    "AuthorizationHeaderMalformed": 400,
    "InvalidAccessKeyId": 403,
    "SignatureDoesNotMatch": 403,
    "RequestTimeTooSkewed": 403,
    "AccessDenied": 403,
    "NoSuchBucket": 404,
    "NoSuchKey": 404,
    "InvalidRange": 416,
    "InvalidArgument": 400,
    # "A header you provided implies functionality that is not implemented. HTTP Status Code: 501"
    # — S3 API reference, the Error data type's code table.
    "NotImplemented": 501,
}

# The query keys that select an operation other than the listing at a bucket's path, and other
# than the object's bytes at an object's path. botocore's S3 service model (2006-03-01) declares
# each as its own operation — `GetBucketVersioning: GET /{Bucket}?versioning`, `GetObjectTagging:
# GET /{Bucket}/{Key+}?tagging` — and `backlot diff --source s3` asks a running server every one.
# Real S3 dispatches on exactly these keys and ignores any other query key (measured against a
# general purpose bucket: `?foo=bar` and `?x-id=ListObjects` both answer the listing, the match is
# case-sensitive so `?Versioning` lists too, and the value is ignored so `?versioning=1` is still
# GetBucketVersioning). The `x-id` key is what the AWS SDK for JavaScript adds to name the operation
# where several share a path — `?x-id=GetObject`, `?analytics&x-id=GetBucketAnalyticsConfiguration`
# — and it must stay ignorable here as it is there. So the refusal is keyed on this set
# rather than on an allow-list of the listing's own parameters, which would refuse what real S3
# ignores.
#
# `session` is absent on purpose: CreateSession exists for directory buckets only, and real S3
# answers `GET /{bucket}?session` on a general purpose bucket with the listing (same measurement),
# so the listing IS the faithful answer. `location` and `uploads` are implemented below; they are in
# the set so that pairing either with another selector conflicts the way real S3 conflicts it
# (`?uploads&versioning` is "Conflicting query string parameters: uploads, versioning", measured).
_BUCKET_SELECTORS = frozenset(
    {
        "abac",
        "accelerate",
        "acl",
        "analytics",
        "cors",
        "encryption",
        "intelligent-tiering",
        "inventory",
        "lifecycle",
        "location",
        "logging",
        "metadataConfiguration",
        "metadataTable",
        "metrics",
        "notification",
        "object-lock",
        "ownershipControls",
        "policy",
        "policyStatus",
        "publicAccessBlock",
        "replication",
        "requestPayment",
        "tagging",
        "uploads",
        "versioning",
        "versions",
        "website",
    }
)
_OBJECT_SELECTORS = frozenset(
    {"acl", "annotation", "attributes", "legal-hold", "retention", "tagging", "torrent", "uploadId"}
)


# --------------------------------------------------------------------------- helpers


def _xml(body: str, status: int = 200, headers: dict | None = None) -> Response:
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?>\n' + body,
        media_type="application/xml",
        status_code=status,
        headers=headers,
    )


def _error(
    code: str, message: str, resource: str = "", extra: str = "", headers: dict | None = None
) -> Response:
    body = (
        f"<Error><Code>{code}</Code><Message>{escape(message)}</Message>"
        f"<Resource>{escape(resource)}</Resource>{extra}</Error>"
    )
    return _xml(body, status=_ERR_STATUS.get(code, 400), headers=headers)


def _selected(q, selectors: frozenset[str]) -> list[str]:
    """The sub-resource selectors a query string carries, in the order real S3 names them.

    Sorted, because real S3 refuses a request carrying two with an ``InvalidArgument`` that names
    them alphabetically whatever order they were sent in — ``?versioning&acl`` is "Conflicting
    query string parameters: acl, versioning" — and reports the first as the ``ArgumentValue``.
    """
    return sorted(k for k in q.keys() if k in selectors)


def _first(q, name: str, default: str | None = "") -> str | None:
    """The FIRST of a repeated query parameter, which is the one real S3 reads (measured
    2026-09-11): ``?uploads&max-uploads=1&max-uploads=abc`` is the page at 1 and the same two
    values the other way round the refusal of ``abc``, and ``encoding-type``, ``upload-id-marker``
    and ``prefix`` go the same way. Starlette's ``QueryParams.get`` returns the last."""
    values = q.getlist(name)
    return values[0] if values else default


def _argument_error(message: str, name: str, value: str, resource: str) -> Response:
    """Real S3's ``InvalidArgument`` for one query parameter: the message beside the parameter's
    name and the value as sent, in ``ArgumentName`` and ``ArgumentValue`` (measured)."""
    return _error(
        "InvalidArgument",
        message,
        resource,
        extra=(
            f"<ArgumentName>{escape(name)}</ArgumentName>"
            f"<ArgumentValue>{escape(value)}</ArgumentValue>"
        ),
    )


def _argument_name(name: str) -> str:
    """``ArgumentName`` with no ``ArgumentValue`` beside it.

    Real S3 leaves the value out of the three cross-version refusals — the body is ``Code``,
    ``Message`` and ``ArgumentName`` alone, whatever was sent and for an empty value too (measured
    2026-09-14) — where every other ``InvalidArgument`` here carries both (see
    ``_argument_error``)."""
    return f"<ArgumentName>{escape(name)}</ArgumentName>"


def _conflict(selected: list[str], resource: str) -> Response:
    """Two selectors at once, refused as real S3 refuses them (measured).

    The name real reports here is ``ResourceType`` rather than a parameter's, and the value is the
    first selector it names in the message."""
    return _argument_error(
        "Conflicting query string parameters: " + ", ".join(selected),
        "ResourceType",
        selected[0],
        resource,
    )


def _head_refusal(selected: list[str]) -> Response:
    """HEAD with a sub-resource selector, refused before the bucket or the key is looked up.

    No sub-resource has a HEAD form: real S3 answers ``HEAD /{bucket}?versioning`` and
    ``HEAD /{key}?acl`` 405 with an empty ``application/xml`` body, whether or not the bucket or the
    key exists, and two selectors at once with the conflict's 400 and the same empty body
    (measured). Real S3 also sends an ``Allow`` naming the methods that sub-resource takes; Backlot
    takes none of them, so it sends none.
    """
    return Response(status_code=400 if len(selected) > 1 else 405, media_type="application/xml")


def _not_implemented(selector: str, resource: str) -> Response:
    """Refuse an operation Backlot does not implement, instead of answering it with another's body.

    ``NotImplemented`` is the S3 error code for "functionality that is not implemented" (the API
    reference's Error code table), and 501 is not a status botocore retries, so a boto3 caller gets one ``ClientError`` straight away.
    What the catch-all used to answer was worse in both directions: a bucket sub-resource got the
    listing, which botocore parsed as an empty result (``get_bucket_versioning`` -> ``{}``), and an
    object sub-resource got the object's bytes, which botocore could not parse as XML and reported
    as a 500 after retrying.

    Real S3 answers each of these operations rather than refusing it — measured against a general
    purpose bucket, ``?versioning`` is an empty ``<VersioningConfiguration>``, a bucket's
    ``?tagging`` is ``NoSuchTagSet`` and an object's an empty ``<Tagging>`` — so every refusal here
    is a gap the S3 baseline records, with the answer real S3 gives written beside it.
    """
    return _error(
        "NotImplemented",
        "A query string parameter you provided selects an operation that is not implemented: "
        + selector,
        resource,
    )


def _auth(request: Request):
    """Returns ``(caller, visible_ids, None)`` on success or ``(None, None, error_response)``."""
    caller, err = auth.resolve_sigv4(request)
    if err:
        return None, None, _error(err, err)
    visible = auth.visible_ids(request, caller)
    return caller, visible, None


def _owner_id(request: Request) -> str:
    """A stable canonical-user-style id for the org that owns every bucket here."""
    return synth._digest("s3-owner:" + request.app.state.acl.org_name)[:16]


def _owner_xml(request: Request) -> str:
    org = request.app.state.acl.org_name
    return f"<Owner><ID>{_owner_id(request)}</ID><DisplayName>{escape(org)}</DisplayName></Owner>"


def _bucket_visible(conn, bucket: str, visible) -> bool:
    if store.get_container(conn, "s3", bucket) is None:
        return False
    if visible is None:
        return True
    return bool(store.list_documents(conn, "s3", container=bucket, visible_ids=visible, limit=1))


def _object_row(conn, bucket: str, key: str, visible):
    return store.s3_by_bucket_key(conn, bucket, key, visible_ids=visible)


def _encode_key_token(key: str) -> str:
    """ListObjectsV2's ``NextContinuationToken`` is opaque on real S3; here it's just the last
    key of the page, base64'd — a keyset cursor, not an offset, so resuming never re-scans (or
    re-counts) the rows already returned. Resumes EXCLUSIVE of ``key`` (``key > key``)."""
    return base64.urlsafe_b64encode(("k:" + key).encode()).decode()


def _encode_group_token(group_successor: str) -> str:
    """A cursor that resumes past a WHOLE rolled-up CommonPrefixes group at once, rather than
    past just the last raw key seen — used when the last entry on a page is a CommonPrefix whose
    raw keys aren't all fetched yet (see ``_list_objects``). ``group_successor`` is already
    ``store.key_successor(group)``; resumes INCLUSIVE of it (``key >= group_successor``), since
    that's the smallest key that could possibly fall outside the group."""
    return base64.urlsafe_b64encode(("g:" + group_successor).encode()).decode()


def _decode_token(token: str) -> tuple[str, str] | None:
    """Decode a continuation token to ``(mode, value)`` — ``mode`` is ``"after"`` (exclusive,
    from ``_encode_key_token``) or ``"at"`` (inclusive, from ``_encode_group_token``). ``None``
    if the token is malformed."""
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if raw.startswith("k:"):
        return "after", raw[2:]
    if raw.startswith("g:"):
        return "at", raw[2:]
    return None


# --------------------------------------------------------------------------- endpoints


@router.get("")
@router.get("/")
async def list_buckets(request: Request):
    """ListBuckets — every bucket the caller can see.

    A bucket is visible when any object in it is, so a caller with no readable object in a bucket
    does not learn the bucket exists."""
    caller, visible, err = _auth(request)
    if err:
        return err
    conn = auth.conn(request)
    buckets = [
        b["name"]
        for b in store.list_containers(conn, "s3")
        if _bucket_visible(conn, b["name"], visible)
    ]
    items = "".join(
        f"<Bucket><Name>{escape(b)}</Name>"
        f"<CreationDate>{synth.s3_iso(synth.epoch('s3-bucket:' + b))}</CreationDate></Bucket>"
        for b in buckets
    )
    return _xml(
        f'<ListAllMyBucketsResult xmlns="{NS}">{_owner_xml(request)}'
        f"<Buckets>{items}</Buckets></ListAllMyBucketsResult>"
    )


@router.head("/{bucket}")
async def head_bucket(request: Request, bucket: str):
    """HeadBucket — 200 if the caller can see this bucket, 404 if they cannot, and the signature's
    own status (403, or 400 for a malformed header) when the request does not authenticate at all.

    Headers alone in every case, which is why it carries no MCP tool (see ``backlot.openapi``)."""
    caller, visible, err = _auth(request)
    if err:
        return Response(status_code=err.status_code)
    conn = auth.conn(request)
    selected = _selected(request.query_params, _BUCKET_SELECTORS)
    if selected:
        return _head_refusal(selected)
    if not _bucket_visible(conn, bucket, visible):
        return Response(status_code=404)
    return Response(status_code=200, headers={"x-amz-bucket-region": "us-east-1"})


@router.get("/{bucket}", openapi_extra={"parameters": _P_BUCKET_GET})
async def bucket_get(request: Request, bucket: str):
    """ListObjects or ListObjectsV2 — one page of the keys in this bucket that the caller can read.

    ``list-type=2`` selects the V2 shape and anything else the V1 one; the two differ in what they
    page with and in the elements they carry (see ``_list_objects``). Both are filtered by
    ``prefix``, rolled up by ``delimiter`` and bounded by ``max-keys``. Two other GETs share this
    path and are selected the same way: with ``location`` present it answers GetBucketLocation, and
    with ``uploads`` present ListMultipartUploads, always the empty page because no upload is ever
    in progress."""
    caller, visible, err = _auth(request)
    if err:
        return err
    conn = auth.conn(request)
    q = request.query_params
    resource = f"/{bucket}"
    selected = _selected(q, _BUCKET_SELECTORS)
    if len(selected) > 1:
        # Before the bucket is looked up: real S3 reports the conflict for a bucket that does not
        # exist too (measured).
        return _conflict(selected, resource)
    max_uploads, max_keys = _MAX_UPLOADS, _MAX_KEYS
    v2 = _first(q, "list-type", None) == _LIST_TYPE_V2
    if selected == ["uploads"]:
        # Also before the bucket is looked up: `?uploads&max-uploads=abc` on a bucket that does not
        # exist is the 400, not NoSuchBucket, where `?uploads&max-uploads=-1` on it is NoSuchBucket:
        # the value is parsed here and judged against the range after the lookup (measured). The
        # other parameters are read after it too.
        max_uploads, err = _int32_param(q, "max-uploads", _MAX_UPLOADS, resource)
        if err:
            return err
    elif not selected:
        # The listing straddles the lookup the same way, and in this order: `max-keys` parses first
        # (`?start-after=x&max-keys=abc` on a V1 request is the max-keys refusal, not start-after's),
        # then the other version's parameters are refused, and both happen before the bucket is
        # looked up. The range and `encoding-type` are judged after it, in _list_objects (measured
        # 2026-09-14).
        max_keys, err = _int32_param(q, "max-keys", _MAX_KEYS, resource)
        if err:
            return err
        for name, message in _V1_ONLY if v2 else _V2_ONLY:
            if _first(q, name, None) is not None:
                return _error("InvalidArgument", message, resource, extra=_argument_name(name))
    if not _bucket_visible(conn, bucket, visible):
        return _error("NoSuchBucket", "The specified bucket does not exist", bucket)
    if selected == ["location"]:
        # us-east-1 is represented by an *empty* LocationConstraint element on real S3.
        return _xml(f'<LocationConstraint xmlns="{NS}"></LocationConstraint>')
    if selected == ["uploads"]:
        return _list_multipart_uploads(request, bucket, max_uploads)
    if selected:
        return _not_implemented(selected[0], resource)
    return _list_objects(request, conn, bucket, visible, v2=v2, max_keys=max_keys)


def _group_of(key: str, prefix: str, delimiter: str) -> str | None:
    """The CommonPrefixes group ``key`` rolls up into, or ``None`` when it is listed on its own."""
    if not delimiter or not key.startswith(prefix):
        return None
    rest = key[len(prefix) :]
    idx = rest.find(delimiter)
    return prefix + rest[: idx + len(delimiter)] if idx != -1 else None


def _list_objects(
    request: Request, conn, bucket: str, visible, *, v2: bool, max_keys: int
) -> Response:
    """One page of a bucket's keys, in whichever of the two listings the caller asked for.

    The rows, the ``prefix`` filter and the ``delimiter`` rollup are the same for both. What
    differs is measured, and it is what a client pages with:

    V1 carries ``Marker`` always, empty when none was sent and echoing it when one was — the API
    reference says otherwise ("Marker is included in the response if it was sent with the
    request"), and what real does is send the empty element either way (measured). It carries
    ``NextMarker`` only when a ``delimiter`` is set and the page is truncated, which the reference
    states and the measurement agrees with: "This element is returned only if you have the
    delimiter request parameter specified. If the response does not include the NextMarker element
    and it is truncated, you can use the value of the last Key element in the response as the
    marker parameter in the subsequent request". That fallback is botocore's V1 paginator, so
    sending a cursor where real sends none would make Backlot easier to page than the thing it
    stands in for. Its ``Contents`` carry an ``Owner``, which in this region is an ``ID`` with no
    ``DisplayName``. It has no ``KeyCount``.

    V2 carries ``KeyCount``, ``NextContinuationToken`` when truncated, ``ContinuationToken``
    echoed when one was sent, and ``StartAfter`` echoed when one was and no continuation token
    displaced it. No ``Owner``.

    A ``marker`` whose own group has already been listed skips that whole group rather than
    walking back into it: real answers ``?delimiter=/&marker=docs/`` and
    ``?delimiter=/&marker=docs/a.txt`` alike with the entries after ``docs/``, never ``docs/``
    again, so paging a delimited V1 listing terminates (measured 2026-09-14 — one page each of
    ``docs/``, ``notes/`` and a plain key, with no repeat). That is the same bound V2 reaches
    through ``_encode_group_token``.

    ``MaxKeys`` echoes the value as parsed, uncapped — real answers ``max-keys=1001`` with
    ``<MaxKeys>1001</MaxKeys>`` and at most ``_MAX_KEYS`` keys, and ``max-keys=05`` with
    ``<MaxKeys>5</MaxKeys>`` — and ``max-keys=0`` is a page of nothing whose ``IsTruncated`` is
    false with keys in the bucket (all measured 2026-09-14).

    ``encoding-type`` is judged here, after the bucket lookup and before the range, and refused
    unless it is ``url`` compared without case. Under it ``Prefix``, ``Delimiter``, ``StartAfter``,
    ``Marker``, ``NextMarker``, every ``Key`` and every ``CommonPrefixes/Prefix`` come back encoded
    and the continuation tokens do not — each of those measured, since the reference names only
    four ("returns encoded key name values in the following response elements: Delimiter, Prefix,
    Key, and StartAfter", the ListObjectsV2 page) and says nothing about the V1 pair or the tokens.
    A token keeps its ``/``, ``+`` and ``=``.

    The body carries every ``Contents`` and then every ``CommonPrefixes``, real's order on both
    listings and not the key order the entries are collected in. The two part company on a page
    holding both: real answers ``?delimiter=/&max-keys=5`` over ``100%.csv``, ``a b.txt``,
    ``a+b.txt``, ``run books/x.txt`` and ``zz.txt`` with four ``Contents`` and then
    ``run books/``, and names ``zz.txt`` as the ``NextMarker`` — the last entry by key, not the
    element the body ends on (measured 2026-09-16).

    Every parameter is read as real reads it, the first value when one is sent twice (see
    ``_first``): ``?prefix=a&prefix=zz.txt`` lists under ``a``, and ``?list-type=1&list-type=2``
    answers V1 where the two the other way round answer V2 (measured).
    """
    q = request.query_params
    resource = f"/{bucket}"
    encoding_type = _first(q, "encoding-type", None)
    if encoding_type is not None and encoding_type.lower() != "url":
        return _argument_error(
            "Invalid Encoding Method specified in Request", "encoding-type", encoding_type, resource
        )
    err = _range_refusal(max_keys, "maxKeys", resource)
    if err:
        return err
    enc = _url_encode if encoding_type is not None else (lambda v: v)
    prefix = _first(q, "prefix")
    delimiter = _first(q, "delimiter")
    marker = _first(q, "marker") if not v2 else ""
    start_after = _first(q, "start-after") if v2 else ""

    # A continuation-token (opaque, from a previous page) wins over start-after, exactly like
    # real S3 — start-after only seeds the very first page of a listing. Its mode (exclusive
    # "after" a raw key, vs inclusive "at" a CommonPrefixes-group successor — see
    # _encode_group_token) picks which of list_s3_objects' two independent lower bounds to use.
    # V1 reaches the same two bounds through `marker` alone: inside a group it resumes past the
    # whole group, and anywhere else past the key itself.
    continuation = _first(q, "continuation-token", None) if v2 else None
    after, at, past_the_end = None, None, False
    if continuation:
        decoded = _decode_token(continuation)
        if decoded is not None:
            mode, value = decoded
            if mode == "after":
                after = value
            else:
                at = value
    elif start_after:
        after = start_after
    elif marker:
        group = _group_of(marker, prefix, delimiter)
        if group:
            at = store.key_successor(group)
            # No successor means the group runs to the end of what a key can spell, so nothing
            # sorts after it and this page is empty rather than the whole listing over again.
            past_the_end = at is None
        else:
            after = marker

    # The one SQL query that replaces the old 100k-row materialize: prefix + keyset (`key >
    # after` / `key >= at`) + ACL all pushed down, walking idx_s3_key(bucket, key) directly in
    # sorted order. Ask for one extra row so IsTruncated is a plain length check (and so we can
    # tell, below, whether a trailing rolled-up group extends past this page) — no separate
    # COUNT(*) query. `served` is what this page may hold: the echo is uncapped but what comes
    # back is not. served=0 is its own case — `rows` is empty after trimming, IsTruncated is false
    # the way real answers it, and nothing below reads the overflow row.
    served = min(max_keys, _MAX_KEYS)
    rows = (
        []
        if past_the_end
        else store.list_s3_objects(
            conn,
            bucket,
            prefix=prefix,
            start_after=after,
            start_at=at,
            visible_ids=visible,
            limit=served + 1,
        )
    )
    is_truncated = served > 0 and len(rows) > served
    overflow_row = rows[served] if is_truncated else None  # first not-yet-returned raw row
    rows = rows[:served]
    by_key = {r["key"]: r for r in rows}

    # Split into (CommonPrefixes, Contents) using the delimiter, S3-style. `rows` is already
    # key-ascending (straight off idx_s3_key), so a first-seen dedup below puts `entries` in key
    # order for free — no second sort. Key order is what KeyCount counts and what both cursors are
    # cut from; the body is written in real's document order further down, which is not this one.
    #
    # Bounded rollup: CommonPrefixes are computed only over THIS page (<= served+1 raw rows),
    # never the whole bucket/prefix. Real S3 can afford to enumerate every CommonPrefixes for a
    # huge delimited listing in one response because it skips whole key ranges internally without
    # reading every key under them; a plain SQL range scan can't do that skip, so a "folder" (a
    # rolled-up CommonPrefixes group) can hold more raw keys than fit in one page.
    entries: list[tuple[str, str]] = []
    seen_prefix: set[str] = set()
    for r in rows:
        k = r["key"]
        group = _group_of(k, prefix, delimiter)
        if group is not None:
            if group not in seen_prefix:
                seen_prefix.add(group)
                entries.append(("cp", group))
            continue
        entries.append(("obj", k))

    # NextContinuationToken: normally the last *raw* key fetched (exclusive keyset bound), same
    # as before. But if the LAST entry on this page is a CommonPrefixes group and that group's raw
    # keys extend past this page (the one overflow row we fetched is still inside it — keys
    # sharing a prefix are always lexicographically contiguous, so if the very next raw key isn't
    # in the group, nothing further out is either), resuming at "key > last raw key" would walk
    # straight back into the SAME group and re-emit its already-returned CommonPrefixes on the
    # next page. Instead resume at "key >= key_successor(group)" — past the group's entire key
    # range in one bounded index seek, never re-scanning its rows — so each CommonPrefixes is
    # emitted at most once across all pages, and plain keys still use the last-key cursor.
    # V1 needs no such split: its cursor IS the last entry, group or key, and a marker naming a
    # group is read back as that same bound above.
    last_kind, last_val = entries[-1] if entries else (None, None)
    group_runs_on = (
        is_truncated
        and last_kind == "cp"
        and overflow_row is not None
        and overflow_row["key"].startswith(last_val)
    )
    group_successor = store.key_successor(last_val) if group_runs_on else None
    if group_runs_on and group_successor is None:
        # That group's key range runs to the end of what a key can spell (see
        # ``store.key_successor``), so every row still unfetched rolls up into the CommonPrefixes
        # entry this page already carries: the page holds every entry there is, and saying
        # truncated would leave a client a page it cannot page out of — there is no cursor to give
        # it and nothing left to fetch with one.
        is_truncated = False
    next_token = None
    if is_truncated and rows and v2:
        next_token = (
            _encode_group_token(group_successor)
            if group_successor is not None
            else _encode_key_token(rows[-1]["key"])
        )
    next_marker = last_val if (is_truncated and entries and not v2 and delimiter) else None

    body = [
        f'<ListBucketResult xmlns="{NS}"><Name>{escape(bucket)}</Name>',
        f"<Prefix>{escape(enc(prefix))}</Prefix>",
    ]
    if v2:
        if start_after and not continuation:
            body.append(f"<StartAfter>{escape(enc(start_after))}</StartAfter>")
        if continuation:
            body.append(f"<ContinuationToken>{escape(continuation)}</ContinuationToken>")
        if next_token:
            body.append(f"<NextContinuationToken>{next_token}</NextContinuationToken>")
        body.append(f"<KeyCount>{len(entries)}</KeyCount>")
    else:
        body.append(f"<Marker>{escape(enc(marker))}</Marker>")
        if next_marker is not None:
            body.append(f"<NextMarker>{escape(enc(next_marker))}</NextMarker>")
    body.append(f"<MaxKeys>{max_keys}</MaxKeys>")
    if delimiter:
        body.append(f"<Delimiter>{escape(enc(delimiter))}</Delimiter>")
    if encoding_type is not None:
        body.append(f"<EncodingType>{escape(encoding_type)}</EncodingType>")
    body.append(f"<IsTruncated>{'true' if is_truncated else 'false'}</IsTruncated>")
    # Every `Contents` first and every `CommonPrefixes` after, which is real's document order and
    # not the key order `entries` is in: a page of `100%.csv`, `a b.txt`, `a+b.txt`, `run books/`
    # and `zz.txt` comes back with `run books/` last, after `zz.txt`, on both listings (measured
    # 2026-09-14 and 2026-09-16). `entries` keeps key order because that is what the cursors are
    # cut from — `NextMarker` on that page is `zz.txt`, the last entry by key, not the element the
    # body ends on.
    owner = "" if v2 else f"<Owner><ID>{_owner_id(request)}</ID></Owner>"
    for val in [v for kind, v in entries if kind == "obj"]:
        r = by_key[val]
        ts = r["updated_ts"] or r["created_ts"]
        body.append(
            f"<Contents><Key>{escape(enc(val))}</Key>"
            f"<LastModified>{synth.s3_iso(ts)}</LastModified>"
            f"<ETag>{escape(synth.s3_etag(r['key'], r['content']))}</ETag>"
            f"<Size>{len(r['content'].encode())}</Size>{owner}"
            f"<StorageClass>{escape(r['subtype'] or 'STANDARD')}</StorageClass></Contents>"
        )
    for val in [v for kind, v in entries if kind == "cp"]:
        body.append(f"<CommonPrefixes><Prefix>{escape(enc(val))}</Prefix></CommonPrefixes>")
    body.append("</ListBucketResult>")
    return _xml("".join(body))


def _url_encode(value: str) -> str:
    """``value`` as real S3 echoes it under ``encoding-type=url`` (see ``_URL_ENCODING_SAFE``)."""
    out = []
    for b in value.encode("utf-8"):
        if b in _URL_ENCODING_SAFE:
            out.append(chr(b))
        elif b == 0x20:
            out.append("+")
        else:
            out.append(f"%{b:02X}")
    return "".join(out)


def _int32_param(q, name: str, default: int, resource: str) -> tuple[int, Response | None]:
    """``max-uploads`` and ``max-keys``, parsed the way real S3 parses both (measured).

    Absent or empty is the default; a run of digits, with or without a leading ``-``, is read for
    its value, leading zeros and all (``05`` and ``00000000005`` are both 5, twenty zeros and a 5 is
    5, five thousand zeros is 0, ``-0`` is 0) and returned as parsed, uncapped and with the range
    left to ``_range_refusal``: a value that fits an int32 but comes to less than 0 (``-1``,
    ``-2147483648``) is refused there, after the bucket lookup, and anything else — a word, a
    leading space, a value whose digits do not fit an int32 in either direction (``2147483648``,
    ``-2147483649``) — is refused here, before the lookup, with "Provided <name> not an integer or
    within integer range". Not ``int()``, which accepts `` 5`` and ``+5`` and has no ceiling.

    The two parameters take the same parser because real parses them the same way, which was
    measured on each separately: the listing refuses ``max-keys=abc``, ``max-keys= 5``,
    ``max-keys=2147483648`` and ``max-keys=-2147483649`` with this message and serves ``max-keys=``,
    ``max-keys=05`` and ``max-keys=-0``, as ListMultipartUploads does for its own (2026-09-11 and
    2026-09-14). Where they differ is the range refusal, which spells the name differently — see
    ``_range_refusal``.
    """
    raw = _first(q, name)
    if raw == "":
        return default, None
    negative = raw.startswith("-")
    # Leading zeros come off before the range is judged, because real judges the value and not the
    # length or the sign: `00002147483647` is served and `00002147483648` refused, five thousand
    # zeros is 0 where five thousand nines is refused, and `-0` is 0 where `-1` is out of range
    # (measured). Stripping them is also what keeps `int()` off a run of digits it will not parse
    # at all, which it refuses past 4300 of them.
    digits = (raw[1:] if negative else raw).lstrip("0") or "0"
    # An int32 reaches one further below zero than above it, so `-2147483648` is a value out of
    # range where `-2147483649` is not an integer at all (measured 2026-09-11).
    ceiling = _INT32_MAX + 1 if negative else _INT32_MAX
    if not re.fullmatch(r"-?[0-9]+", raw) or len(digits) > 10 or int(digits) > ceiling:
        return 0, _argument_error(
            f"Provided {name} not an integer or within integer range", name, raw, resource
        )
    return -int(digits) if negative else int(digits), None


def _range_refusal(value: int, name: str, resource: str) -> Response | None:
    """A parsed ``max-uploads``/``max-keys`` below zero, refused as real refuses it (measured).

    Real judges the range after the bucket lookup and after ``encoding-type``, and names the value
    as parsed rather than as sent — ``-01`` is reported as ``-1`` (2026-09-11 for ``max-uploads``,
    2026-09-14 for ``max-keys``). ``name`` is separate from the parse refusal's because the listing
    spells it differently on either side of that split: ``max-keys=abc`` is refused for ``max-keys``
    and ``max-keys=-1`` for ``maxKeys``, on both forms of the listing, where ListMultipartUploads
    spells ``max-uploads`` in both of its messages (measured).
    """
    if value >= 0:
        return None
    return _argument_error(
        f"Argument {name} must be an integer between 0 and {_INT32_MAX}", name, str(value), resource
    )


def _list_multipart_uploads(request: Request, bucket: str, max_uploads: int) -> Response:
    """ListMultipartUploads — the page real S3 serves for a bucket with no upload in progress.

    Backlot never has one: data enters through ``backlot import`` and never through the served API,
    so the faithful answer is always the empty page, and a browsing client that sends ``?uploads``
    after every ListObjectsV2 gets the 200 it gets from real.

    The shape is real's, measured against a general purpose bucket with no upload in progress:
    ``Bucket``, then ``KeyMarker``, ``UploadIdMarker``, ``NextKeyMarker`` and ``NextUploadIdMarker``
    present and empty, then ``Delimiter`` and ``Prefix`` (in that order) each only when sent
    non-empty, ``MaxUploads``, ``EncodingType`` only when sent, and ``IsTruncated`` false. No
    ``Upload`` and no ``CommonPrefixes``: "A response can contain zero or more Upload elements"
    (the S3 API reference on ListMultipartUploads).

    ``key-marker`` is echoed. ``upload-id-marker`` alone is ignored, as the API reference says ("If
    key-marker is not specified, the upload-id-marker parameter is ignored"); beside a ``key-marker``
    real refuses it with "Invalid uploadId marker" when it names no upload in the bucket, which was
    measured with two ids on a bucket with none in progress — whether real would also refuse an id
    that does name one could not be told apart there, and on Backlot no id ever does, so every such
    request takes the refusal. ``encoding-type`` is checked before the markers and refused unless it
    is ``url``, compared without case since real takes ``URL`` too and echoes it as ``URL`` (the two
    spellings measured); under it ``KeyMarker``, ``Delimiter`` and ``Prefix`` come back encoded (see
    ``_url_encode``); ``Bucket`` as it is, a bucket name holding nothing the encoding touches. Each
    parameter is read as real reads it, the first value when one is sent twice (see ``_first``).

    ``max_uploads`` arrives parsed from ``_int32_param``, negative included: real judges the range
    after the bucket lookup and after ``encoding-type``, before the markers, and names the value as
    parsed, ``-01`` as ``-1`` (measured 2026-09-11). Past ``_MAX_UPLOADS`` it is served at the cap.
    """
    q = request.query_params
    resource = f"/{bucket}"
    encoding_type = _first(q, "encoding-type", None)
    if encoding_type is not None and encoding_type.lower() != "url":
        return _argument_error(
            "Invalid Encoding Method specified in Request", "encoding-type", encoding_type, resource
        )
    err = _range_refusal(max_uploads, "max-uploads", resource)
    if err:
        return err
    key_marker = _first(q, "key-marker")
    upload_id_marker = _first(q, "upload-id-marker")
    if key_marker and upload_id_marker:
        return _argument_error(
            "Invalid uploadId marker", "upload-id-marker", upload_id_marker, resource
        )
    prefix = _first(q, "prefix")
    delimiter = _first(q, "delimiter")
    enc = _url_encode if encoding_type is not None else (lambda v: v)
    body = [
        f'<ListMultipartUploadsResult xmlns="{NS}"><Bucket>{escape(bucket)}</Bucket>',
        f"<KeyMarker>{escape(enc(key_marker))}</KeyMarker><UploadIdMarker></UploadIdMarker>",
        "<NextKeyMarker></NextKeyMarker><NextUploadIdMarker></NextUploadIdMarker>",
        f"<Delimiter>{escape(enc(delimiter))}</Delimiter>" if delimiter else "",
        f"<Prefix>{escape(enc(prefix))}</Prefix>" if prefix else "",
        f"<MaxUploads>{min(max_uploads, _MAX_UPLOADS)}</MaxUploads>",
        f"<EncodingType>{escape(encoding_type)}</EncodingType>"
        if encoding_type is not None
        else "",
        "<IsTruncated>false</IsTruncated></ListMultipartUploadsResult>",
    ]
    return _xml("".join(body))


@router.api_route("/{bucket}/{key:path}", methods=["GET", "HEAD"])
async def object_get(request: Request, bucket: str, key: str):
    """GetObject — the object's bytes, or its metadata headers alone for a HEAD.

    ``key`` is a whole path, slashes and all. A ``Range`` header returns 206 over that slice; an
    object the caller cannot read is NoSuchKey, not AccessDenied, so a listing and a read agree
    about what exists."""
    caller, visible, err = _auth(request)
    if err:
        return err if request.method == "GET" else Response(status_code=err.status_code)
    conn = auth.conn(request)
    selected = _selected(request.query_params, _OBJECT_SELECTORS)
    if selected and request.method == "HEAD":
        return _head_refusal(selected)
    if len(selected) > 1:
        return _conflict(selected, f"/{bucket}/{key}")
    if selected == ["uploadId"]:
        # ListParts is about an upload, not about the object under the key: real S3 answers
        # `NoSuchUpload` for a key that does not exist, not `NoSuchKey` (measured), so the refusal
        # comes before the key is looked up.
        return _not_implemented("uploadId", f"/{bucket}/{key}")
    row = _object_row(conn, bucket, key, visible)
    if row is None:
        if request.method == "HEAD":
            return Response(status_code=404)
        return _error("NoSuchKey", "The specified key does not exist.", f"/{bucket}/{key}")
    if selected:
        # The key exists, so the sub-resource is what is being refused — real S3 checks the key
        # first too: `GET /{missing}?acl` is NoSuchKey (same measurement).
        return _not_implemented(selected[0], f"/{bucket}/{key}")

    data = row["content"].encode("utf-8")
    total = len(data)
    ts = row["updated_ts"] or row["created_ts"]
    headers = {
        "ETag": synth.s3_etag(row["key"], row["content"]),
        "Last-Modified": synth.s3_http_date(ts),
        "Accept-Ranges": "bytes",
        # Seeded from the object's own address, which is what identifies the object being served.
        "x-amz-request-id": synth._digest(f"s3-req:{row['bucket']}/{row['key']}")[:16].upper(),
    }
    ctype = row["content_type"] or "text/plain"
    # Set Content-Type via the headers dict, not the `media_type=` kwarg: Starlette auto-appends
    # "; charset=utf-8" to any bare text/* media_type, but a real S3 object's Content-Type is
    # returned byte-for-byte as stored — no charset ever added.
    headers["Content-Type"] = ctype

    rng = request.headers.get("range")
    status, start, end = 200, 0, total - 1
    if rng:
        parsed = _parse_range(rng, total)
        if parsed is None:
            range_headers = {**headers, "Content-Range": f"bytes */{total}"}
            if request.method == "GET":
                return _error(
                    "InvalidRange",
                    "The requested range is not satisfiable",
                    f"/{bucket}/{key}",
                    extra=f"<ActualObjectSize>{total}</ActualObjectSize>",
                    headers={"Content-Range": f"bytes */{total}"},
                )
            return Response(status_code=416, headers={**range_headers, "Content-Length": "0"})
        start, end = parsed
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"

    length = end - start + 1
    headers["Content-Length"] = str(length)
    if request.method == "HEAD":
        return Response(status_code=200 if status == 200 else 206, headers=headers)
    return Response(content=data[start : end + 1], status_code=status, headers=headers)


def _parse_range(header: str, total: int):
    """Parse a single-range ``bytes=…`` header -> (start, end) inclusive, or None if unsatisfiable."""
    if not header.startswith("bytes="):
        return None
    spec = header[len("bytes=") :].split(",")[0].strip()
    lo, _, hi = spec.partition("-")
    try:
        if lo == "":  # suffix: bytes=-N (last N bytes)
            n = int(hi)
            if n <= 0:
                return None
            return max(0, total - n), total - 1
        start = int(lo)
        end = int(hi) if hi else total - 1
    except ValueError:
        return None
    if start >= total or start > end:
        return None
    return start, min(end, total - 1)
