"""Mock Amazon S3 API (read-only, object storage).

Path-style endpoint for a client: ``http://<host>/s3`` (boto3: ``endpoint_url=".../s3"`` with
``addressing_style=path``; mirage: ``S3Config(endpoint_url=".../s3", path_style=True)``). Auth is
full AWS SigV4 (``backlot.auth.resolve_sigv4``) against a per-caller access-key/secret derived from a
bearer token; the admin/service token's key sees everything, a user's key is ACL-filtered.
Responses are S3 XML (namespace ``http://s3.amazonaws.com/doc/2006-03-01/``) or raw object bytes;
errors use the S3 ``<Error>`` envelope.

Object model: a bucket is the grouping/ACL unit (``s3_buckets``); an object is one doc
(``s3_objects``), ``key`` is its address and ``content`` its verbatim body. "Folders" are pure
key-prefix convention surfaced via ListObjectsV2's ``delimiter``/``CommonPrefixes``.
"""

from __future__ import annotations

import base64

from xml.sax.saxutils import escape

from fastapi import APIRouter, Request, Response

from backlot import auth, store, synth

router = APIRouter(prefix="/s3", tags=["s3"])
# A signed S3 request's canonical path is exact; letting Starlette 307-redirect a bare "/s3" ->
# "/s3/" would both break SigV4 (the redirected request no longer matches what was signed) and
# send botocore's bucket-region-redirect logic haywire. ListBuckets is registered for both
# "/s3" and "/s3/" below so the exact path always matches directly and no redirect is ever
# triggered (setting `router.redirect_slashes = False` here would be a no-op once this
# sub-router is flattened into the app by `include_router`).

NS = "http://s3.amazonaws.com/doc/2006-03-01/"
_MAX_KEYS = 1000
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
}


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


def _auth(request: Request):
    """Returns ``(caller, visible_ids, None)`` on success or ``(None, None, error_response)``."""
    caller, err = auth.resolve_sigv4(request)
    if err:
        return None, None, _error(err, err)
    visible = auth.visible_ids(request, caller)
    return caller, visible, None


def _owner_xml(request: Request) -> str:
    org = request.app.state.acl.org_name
    oid = synth._digest("s3-owner:" + org)[:16]  # a stable canonical-user-style id
    return f"<Owner><ID>{oid}</ID><DisplayName>{escape(org)}</DisplayName></Owner>"


def _bucket_visible(conn, bucket: str, visible) -> bool:
    if store.get_container(conn, "s3", bucket) is None:
        return False
    if visible is None:
        return True
    return bool(store.list_documents(conn, "s3", container=bucket, visible_ids=visible, limit=1))


def _object_row(request: Request, conn, bucket: str, key: str, visible):
    doc_id = request.app.state.index["s3"].get(f"{bucket}/{key}")
    if doc_id is None:
        return None
    return store.get_document(conn, "s3", doc_id, visible)


def _encode_key_token(key: str) -> str:
    """ListObjectsV2's ``NextContinuationToken`` is opaque on real S3; here it's just the last
    key of the page, base64'd — a keyset cursor, not an offset, so resuming never re-scans (or
    re-counts) the rows already returned. Resumes EXCLUSIVE of ``key`` (``key > key``)."""
    return base64.urlsafe_b64encode(("k:" + key).encode()).decode()


def _encode_group_token(group_successor: str) -> str:
    """A cursor that resumes past a WHOLE rolled-up CommonPrefixes group at once, rather than
    past just the last raw key seen — used when the last entry on a page is a CommonPrefix whose
    raw keys aren't all fetched yet (see ``_list_objects_v2``). ``group_successor`` is already
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
    caller, visible, err = _auth(request)
    if err:
        return Response(status_code=err.status_code)
    conn = auth.conn(request)
    if not _bucket_visible(conn, bucket, visible):
        return Response(status_code=404)
    return Response(status_code=200, headers={"x-amz-bucket-region": "us-east-1"})


@router.get("/{bucket}")
async def bucket_get(request: Request, bucket: str):
    caller, visible, err = _auth(request)
    if err:
        return err
    conn = auth.conn(request)
    if not _bucket_visible(conn, bucket, visible):
        return _error("NoSuchBucket", "The specified bucket does not exist", bucket)
    q = request.query_params
    if "location" in q:
        # us-east-1 is represented by an *empty* LocationConstraint element on real S3.
        return _xml(f'<LocationConstraint xmlns="{NS}"></LocationConstraint>')
    return _list_objects_v2(request, conn, bucket, visible)


def _list_objects_v2(request: Request, conn, bucket: str, visible) -> Response:
    q = request.query_params
    prefix = q.get("prefix", "")
    delimiter = q.get("delimiter", "")
    start_after = q.get("start-after", "")
    try:
        max_keys = min(int(q.get("max-keys", _MAX_KEYS)), _MAX_KEYS)
    except ValueError:
        return _error("InvalidArgument", "max-keys must be an integer")

    # A continuation-token (opaque, from a previous page) wins over start-after, exactly like
    # real S3 — start-after only seeds the very first page of a listing. Its mode (exclusive
    # "after" a raw key, vs inclusive "at" a CommonPrefixes-group successor — see
    # _encode_group_token) picks which of list_s3_objects' two independent lower bounds to use.
    continuation = q.get("continuation-token")
    after, at = None, None
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

    # The one SQL query that replaces the old 100k-row materialize: prefix + keyset (`key >
    # after` / `key >= at`) + ACL all pushed down, walking idx_s3_key(bucket, key) directly in
    # sorted order. Ask for one extra row so IsTruncated is a plain length check (and so we can
    # tell, below, whether a trailing rolled-up group extends past this page) — no separate
    # COUNT(*) query. max_keys=0: the LIMIT is still 1 (so IsTruncated is still computed), but
    # `rows` ends up empty after trimming, and every access below is guarded on it.
    rows = store.list_s3_objects(
        conn,
        bucket,
        prefix=prefix,
        start_after=after,
        start_at=at,
        visible_ids=visible,
        limit=max_keys + 1,
    )
    is_truncated = len(rows) > max_keys
    overflow_row = rows[max_keys] if is_truncated else None  # first not-yet-returned raw row
    rows = rows[:max_keys]
    by_key = {r["key"]: r for r in rows}

    # Split into (CommonPrefixes, Contents) using the delimiter, S3-style. `rows` is already
    # key-ascending (straight off idx_s3_key), so a first-seen dedup below reproduces the final
    # sorted order for free — no second sort.
    #
    # Bounded rollup: CommonPrefixes are computed only over THIS page (<= max_keys+1 raw rows),
    # never the whole bucket/prefix. Real S3 can afford to enumerate every CommonPrefixes for a
    # huge delimited listing in one response because it skips whole key ranges internally without
    # reading every key under them; a plain SQL range scan can't do that skip, so a "folder" (a
    # rolled-up CommonPrefixes group) can hold more raw keys than fit in one page.
    entries: list[tuple[str, str]] = []
    seen_prefix: set[str] = set()
    for r in rows:
        k = r["key"]
        if delimiter:
            rest = k[len(prefix) :]
            idx = rest.find(delimiter)
            if idx != -1:
                cp = prefix + rest[: idx + len(delimiter)]
                if cp not in seen_prefix:
                    seen_prefix.add(cp)
                    entries.append(("cp", cp))
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
    next_token = None
    if is_truncated and rows:
        last_kind, last_val = entries[-1] if entries else (None, None)
        if (
            last_kind == "cp"
            and overflow_row is not None
            and overflow_row["key"].startswith(last_val)
        ):
            next_token = _encode_group_token(store.key_successor(last_val))
        else:
            next_token = _encode_key_token(rows[-1]["key"])

    body = [
        f'<ListBucketResult xmlns="{NS}"><Name>{escape(bucket)}</Name>',
        f"<Prefix>{escape(prefix)}</Prefix>",
        f"<KeyCount>{len(entries)}</KeyCount><MaxKeys>{max_keys}</MaxKeys>",
        f"<Delimiter>{escape(delimiter)}</Delimiter>" if delimiter else "",
        f"<StartAfter>{escape(start_after)}</StartAfter>" if start_after else "",
        f"<IsTruncated>{'true' if is_truncated else 'false'}</IsTruncated>",
    ]
    if continuation:
        body.append(f"<ContinuationToken>{escape(continuation)}</ContinuationToken>")
    if next_token:
        body.append(f"<NextContinuationToken>{next_token}</NextContinuationToken>")
    for kind, val in entries:
        if kind == "cp":
            body.append(f"<CommonPrefixes><Prefix>{escape(val)}</Prefix></CommonPrefixes>")
        else:
            r = by_key[val]
            ts = r["updated_ts"] or r["created_ts"]
            body.append(
                f"<Contents><Key>{escape(val)}</Key>"
                f"<LastModified>{synth.s3_iso(ts)}</LastModified>"
                f"<ETag>{escape(synth.s3_etag(r['doc_id'], r['content']))}</ETag>"
                f"<Size>{len(r['content'].encode())}</Size>"
                f"<StorageClass>{escape(r['subtype'] or 'STANDARD')}</StorageClass></Contents>"
            )
    body.append("</ListBucketResult>")
    return _xml("".join(body))


@router.api_route("/{bucket}/{key:path}", methods=["GET", "HEAD"])
async def object_get(request: Request, bucket: str, key: str):
    caller, visible, err = _auth(request)
    if err:
        return err if request.method == "GET" else Response(status_code=err.status_code)
    conn = auth.conn(request)
    row = _object_row(request, conn, bucket, key, visible)
    if row is None:
        if request.method == "HEAD":
            return Response(status_code=404)
        return _error("NoSuchKey", "The specified key does not exist.", f"/{bucket}/{key}")

    data = row["content"].encode("utf-8")
    total = len(data)
    ts = row["updated_ts"] or row["created_ts"]
    headers = {
        "ETag": synth.s3_etag(row["doc_id"], row["content"]),
        "Last-Modified": synth.s3_http_date(ts),
        "Accept-Ranges": "bytes",
        "x-amz-request-id": synth._digest("s3-req:" + row["doc_id"])[:16].upper(),
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
