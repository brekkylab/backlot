"""S3: the two bucket listings, object reads, the XML shapes, and SigV4.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import base64
import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlencode

import pytest
import yaml
from starlette.requests import Request

from backlot import auth, synth
from backlot.acl import Acl, Caller
from backlot.sigv4 import (
    expected_signature,
    is_skewed,
    parse_amz_date,
    parse_authorization,
    split_credential,
)
from tests._helpers import client_for, complete

# ------------------------------------------------------------------------ S3 (SigV4/404/416 edges)


def _sign_get(base_url, path, token, *, tamper=False, extra_headers=None, method="GET"):
    """Return (url, headers) for a SigV4-signed GET (or ``method``), using botocore (the real
    signer)."""
    pytest.importorskip("botocore")
    from urllib.parse import parse_qsl, quote, urlencode

    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

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
    req = AWSRequest(method=method, url=url, headers=dict(extra_headers or {}))
    req.headers["x-amz-content-sha256"] = "UNSIGNED-PAYLOAD"
    S3SigV4Auth(Credentials(ak, sk), "s3", "us-east-1").add_auth(req)
    headers = dict(req.headers)
    if tamper:
        headers["Authorization"] = headers["Authorization"][:-4] + "dead"
    return url, headers


def test_s3_unknown_access_key_rejected(live_server):
    import urllib.request

    base_url, settings = live_server
    url = f"{base_url}/s3/eng-artifacts?list-type=2"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": (
                "AWS4-HMAC-SHA256 Credential=AKIABOGUS0000000BOGUS/"
                "20260720/us-east-1/s3/aws4_request, "
                "SignedHeaders=host, Signature=00"
            ),
            "x-amz-date": "20260720T000000Z",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403 and b"InvalidAccessKeyId" in e.value.read()


def test_s3_tampered_signature_rejected(live_server):
    import urllib.request

    base_url, settings = live_server
    url, headers = _sign_get(
        base_url, "/s3/eng-artifacts?list-type=2", settings.admin_token, tamper=True
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url, headers=headers))
    assert e.value.code == 403 and b"SignatureDoesNotMatch" in e.value.read()


def test_s3_missing_key_is_nosuchkey(live_server):
    import urllib.request

    base_url, settings = live_server
    url, headers = _sign_get(base_url, "/s3/eng-artifacts/does/not/exist.md", settings.admin_token)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url, headers=headers))
    assert e.value.code == 404 and b"NoSuchKey" in e.value.read()


def test_s3_key_containing_a_question_mark_verifies(live_server):
    """`?` is a legal character in a key, and a client sends it as `%3F`. Starlette rebuilds
    `request.url` from the DECODED path, so for `/q%3Fx.txt` its `.query` is `x.txt` — a query the
    client never sent or signed. The verifier canonicalises the wire query string instead, and an
    absent key with a `?` in it answers NoSuchKey like every other absent key, not
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
    assert e.value.code == 416 and b"InvalidRange" in e.value.read()
    total = len("Check dashboards, roll back, page on-call.")
    assert e.value.headers.get("Content-Range") == f"bytes */{total}"
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


def _s3_get(client, path, token):
    """SigV4-sign a GET (same signer as the module-level ``_sign_get``) and issue it through an
    in-process TestClient instead of a live socket."""
    from urllib.parse import parse_qsl, quote, urlencode

    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

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
    req = AWSRequest(method="GET", url=url)
    req.headers["x-amz-content-sha256"] = "UNSIGNED-PAYLOAD"
    S3SigV4Auth(Credentials(ak, sk), "s3", "us-east-1").add_auth(req)
    return client.get(url, headers=dict(req.headers))


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


@pytest.mark.parametrize("listing", ["", "list-type=2&"], ids=["v1", "v2"])
def test_s3_large_bucket_acl_scopes_listing(
    big_bucket_client, big_bucket_settings, big_bucket_tokens, listing
):
    """Both served bodies scope the same way. The V1 one carries a per-object ``Owner`` a scoped
    caller can read, so the ACL has to be proved on it and not only on the V2 shape."""
    pytest.importorskip("botocore")

    def keys_for(token):
        r = _s3_get(
            big_bucket_client,
            f"/s3/big-bucket?{listing}prefix=logs/2026/01/&max-keys=1000",
            token,
        )
        return {e.text for e in ET.fromstring(r.text).findall(f"{{{S3NS}}}Contents/{{{S3NS}}}Key")}

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
    owner = ET.fromstring(scoped.text).find(f"{{{S3NS}}}Contents/{{{S3NS}}}Owner/{{{S3NS}}}ID")
    assert (owner is not None) == (listing == "")


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


def _get_xml(base_url, path, token):
    url, headers = _sign_get(base_url, path, token)
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
        return ET.fromstring(r.read())


def test_list_buckets_xml_shape(live_server):
    base_url, settings = live_server
    root = _get_xml(base_url, "/s3/", settings.admin_token)
    assert root.tag == f"{NS}ListAllMyBucketsResult"
    assert root.find(f"{NS}Owner/{NS}ID") is not None
    names = {b.findtext(f"{NS}Name") for b in root.iter(f"{NS}Bucket")}
    assert "eng-artifacts" in names


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


# ------------------------------------------------------------ sub-resources Backlot does not serve
# S3 dispatches on the query string: `?versioning`, `?acl`, `?tagging` and the rest each select an
# operation of their own at a bucket's or an object's path. Backlot implements two of them at a
# bucket's path (`?location` and `?uploads`, below), refuses the rest, and used to answer every one
# with the listing or the object's bytes under a 200. Every claim about real S3 below was measured
# against a general purpose bucket: each selector is answered as its own operation, an unknown key
# (`?foo=bar`, `?x-id=…`) is ignored, the match is case-sensitive, two selectors conflict, and HEAD
# with a selector is 405.

BUCKET_SUBRESOURCES = [
    "abac",
    "accelerate",
    "acl",
    "analytics",
    "cors",
    "encryption",
    "intelligent-tiering",
    "inventory",
    "lifecycle",
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
    "versioning",
    "versions",
    "website",
]
OBJECT_SUBRESOURCES = [
    "acl",
    "annotation",
    "attributes",
    "legal-hold",
    "retention",
    "tagging",
    "torrent",
    "uploadId=abc123",  # ListParts: selected by a required querystring member, not a bare key
]
OBJECT_PATH = "/s3/eng-artifacts/runbooks/oncall.md"
OBJECT_TEXT = b"Check dashboards, roll back, page on-call."


def _refused(base_url, path, token, method="GET") -> urllib.error.HTTPError:
    url, headers = _sign_get(base_url, path, token, method=method)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method))
    return e.value


@pytest.mark.parametrize("selector", BUCKET_SUBRESOURCES)
def test_an_unimplemented_bucket_subresource_is_refused_not_answered_with_the_listing(
    live_server, selector
):
    base_url, settings = live_server
    err = _refused(base_url, f"/s3/eng-artifacts?{selector}", settings.admin_token)
    body = err.read()
    assert err.code == 501
    assert b"<Code>NotImplemented</Code>" in body
    assert b"ListBucketResult" not in body
    assert err.headers.get("Content-Type") == "application/xml"
    # The HEAD at the same path names no method, the `Allow` naming only what a GET serves.
    head = _refused(base_url, f"/s3/eng-artifacts?{selector}", settings.admin_token, method="HEAD")
    assert head.code == 405 and head.headers.get("Allow") is None


@pytest.mark.parametrize("selector", OBJECT_SUBRESOURCES)
def test_an_unimplemented_object_subresource_is_refused_not_answered_with_the_object(
    live_server, selector
):
    base_url, settings = live_server
    err = _refused(base_url, f"{OBJECT_PATH}?{selector}", settings.admin_token)
    body = err.read()
    assert err.code == 501
    assert b"<Code>NotImplemented</Code>" in body
    assert OBJECT_TEXT not in body
    assert err.headers.get("Content-Type") == "application/xml"
    # As above, and no object sub-resource is served at all, so none of these names a method.
    head = _refused(base_url, f"{OBJECT_PATH}?{selector}", settings.admin_token, method="HEAD")
    assert head.code == 405 and head.headers.get("Allow") is None


def test_the_listing_location_and_object_still_answer_and_an_unknown_key_is_ignored(live_server):
    base_url, settings = live_server
    token = settings.admin_token
    # A bare bucket GET is ListObjects and `?list-type=2` its v2 form; `?foo=bar` and an `x-id` key
    # (the AWS SDK for JavaScript names the operation with one) are not selectors; `?Versioning` is not `?versioning`; and
    # `?session` (CreateSession, directory buckets only) lists on a general purpose bucket.
    for query in ("", "?list-type=2", "?foo=bar", "?x-id=ListObjects", "?Versioning", "?session"):
        root = _get_xml(base_url, f"/s3/eng-artifacts{query}", token)
        assert root.tag == f"{NS}ListBucketResult", query
    assert _get_xml(base_url, "/s3/eng-artifacts?location", token).tag == f"{NS}LocationConstraint"
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
    # `location` and `uploads`, the two bucket sub-resources Backlot serves, conflict like any other.
    err = _refused(base_url, "/s3/eng-artifacts?location&versioning", settings.admin_token)
    assert err.code == 400 and b"location, versioning" in err.read()
    err = _refused(base_url, "/s3/eng-artifacts?versioning&uploads", settings.admin_token)
    assert err.code == 400 and b"uploads, versioning" in err.read()
    err = _refused(base_url, f"{OBJECT_PATH}?tagging&acl", settings.admin_token)
    assert err.code == 400 and b"acl, tagging" in err.read()
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
    # a missing key rather than NoSuchKey, so Backlot refuses it before looking the key up.
    err = _refused(base_url, "/s3/eng-artifacts/no/such.md?uploadId=abc123", token)
    assert err.code == 501 and b"NotImplemented" in err.read()


def test_head_with_a_subresource_names_what_a_get_serves_and_a_bare_head_still_answers(live_server):
    base_url, settings = live_server
    token = settings.admin_token
    # `location` and `uploads` are served on a GET and still have no HEAD form, so theirs are the
    # two 405s that name GET; every selector the GET refuses is asserted beside its own 501 above.
    for path, allow in (
        ("/s3/eng-artifacts?location", "GET"),
        ("/s3/eng-artifacts?uploads", "GET"),
        # Before the bucket or the key is looked up, as on real S3 — the header with it: a bucket
        # that does not exist answers `Allow: GET` for `?location` on real too (measured
        # 2026-09-17, ap-northeast-2).
        ("/s3/no-such-bucket?location", "GET"),
        ("/s3/no-such-bucket?versioning", None),
        ("/s3/eng-artifacts/no/such.md?acl", None),
    ):
        err = _refused(base_url, path, token, method="HEAD")
        assert err.code == 405 and err.read() == b"", path
        assert err.headers.get("Content-Type") == "application/xml", path
        assert err.headers.get("Allow") == allow, path
    for path in ("/s3/eng-artifacts", OBJECT_PATH):
        url, headers = _sign_get(base_url, path, token, method="HEAD")
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers, method="HEAD")
        ) as r:
            assert r.status == 200, path


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
    """#169: a browsing client sends `?uploads` after every ListObjectsV2, unprompted, and got a
    501 where real answers 200. The body is real's for a bucket with no upload in progress, with the bucket's
    name swapped in: the two markers and the two next-markers present and empty, MaxUploads at the
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
    # The two queries in #169's trace: a folder listing's `?uploads` carries the delimiter, and a
    # prefix's carries both. Delimiter comes before Prefix on real, and both come before MaxUploads.
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
    """#205: the refusal cannot be read as "this bucket exists", for any caller.

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
    """#205's own reproduction, from the client side.

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


def test_boto3_list_multipart_uploads_is_an_empty_page_not_a_client_error(live_server):
    s3 = _boto3_client(live_server)
    page = s3.list_multipart_uploads(Bucket="eng-artifacts", Prefix="runbooks/", Delimiter="/")
    assert page["ResponseMetadata"]["HTTPStatusCode"] == 200
    assert page["Bucket"] == "eng-artifacts" and page["Prefix"] == "runbooks/"
    assert page["Delimiter"] == "/" and page["MaxUploads"] == 1000
    assert page["IsTruncated"] is False and "Uploads" not in page


def test_boto3_list_objects_paginator_walks_the_bucket_and_keeps_marker_and_owner(live_server):
    """#188's own reproduction, from the client side.

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


def test_boto3_gets_one_client_error_instead_of_an_empty_answer_or_a_retried_500(live_server):
    """The issue's own reproduction. Before the fix `get_bucket_versioning` returned `{}` and
    `get_bucket_policy` returned the XML listing as the policy string, because botocore parsed the
    listing as each operation's output; `get_object_tagging` and `list_parts` surfaced as a 500
    after botocore's retries, because botocore's `_handle_200_error` could not parse the object's bytes
    as XML. 501 is not a status botocore retries, so each is now one ClientError, at once."""
    s3 = _boto3_client(live_server)
    from botocore.exceptions import ClientError

    bucket, key = "eng-artifacts", "runbooks/oncall.md"
    for call in (
        lambda: s3.get_bucket_versioning(Bucket=bucket),
        lambda: s3.get_bucket_policy(Bucket=bucket),
        lambda: s3.get_bucket_tagging(Bucket=bucket),
        lambda: s3.get_object_tagging(Bucket=bucket, Key=key),
        lambda: s3.list_parts(Bucket=bucket, Key=key, UploadId="abc123"),
    ):
        with pytest.raises(ClientError) as e:
            call()
        assert e.value.response["Error"]["Code"] == "NotImplemented"
        assert e.value.response["ResponseMetadata"]["HTTPStatusCode"] == 501
        assert e.value.response["ResponseMetadata"]["RetryAttempts"] == 0
    assert s3.get_bucket_location(Bucket=bucket)["LocationConstraint"] is None  # us-east-1
    assert key in {o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"]}
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == OBJECT_TEXT


# --- the SigV4 verifier (backlot/sigv4.py) — S3 is its only caller ------------------------------------
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


def _header_auth_request(
    amz_date: str, path="/s3/eng-artifacts", query="list-type=2", region="us-east-1"
):
    """Build a header-auth GET signed for `amz_date` with a genuinely valid signature."""
    date_stamp = amz_date[:8]
    signed_headers = "host;x-amz-date"
    headers = {
        "host": "backlot",
        "x-amz-date": amz_date,
        "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
    }
    sig = expected_signature(
        SK,
        "GET",
        path,
        query,
        headers,
        signed_headers,
        "UNSIGNED-PAYLOAD",
        amz_date,
        date_stamp,
        region,
    )
    credential = f"{AK}/{date_stamp}/{region}/s3/aws4_request"
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={credential}, SignedHeaders={signed_headers}, Signature={sig}"
    )
    return _request("GET", path, query, headers)


def _presigned_request(amz_date: str, expires: int, path="/s3/eng-artifacts", region="us-east-1"):
    """Build a presigned-query GET signed for `amz_date`/`expires` with a valid signature."""
    date_stamp = amz_date[:8]
    signed_headers = "host"
    headers = {"host": "backlot"}
    credential = f"{AK}/{date_stamp}/{region}/s3/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": credential,
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": signed_headers,
    }
    query = urlencode(params, safe="-_.~", quote_via=quote)
    sig = expected_signature(
        SK,
        "GET",
        path,
        query,
        headers,
        signed_headers,
        "UNSIGNED-PAYLOAD",
        amz_date,
        date_stamp,
        region,
    )
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
    assert err == "RequestTimeTooSkewed"


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
    assert err == "RequestTimeTooSkewed"


# A `%3F` in the key decodes to a `?` that splits Starlette's rebuilt `request.url`, so the
# canonical request has to come off the wire — see the comment in `resolve_sigv4`.
SIGNED_PATHS = ["/s3/eng-artifacts", "/s3/eng-artifacts/q%3Fx.txt"]


@pytest.mark.parametrize("path", SIGNED_PATHS)
def test_header_auth_accepts_current_date(path):
    current = datetime.now(timezone.utc).strftime(AMZ_DATE_FORMAT)
    req = _header_auth_request(current, path=path)
    caller, err = auth.resolve_sigv4(req)
    assert err is None
    assert caller == Caller(email="ava@acme.com", is_admin=False)


def test_presigned_expired_is_access_denied():
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(AMZ_DATE_FORMAT)
    req = _presigned_request(stale, expires=60)
    caller, err = auth.resolve_sigv4(req)
    assert caller is None
    assert err == "AccessDenied"


@pytest.mark.parametrize("path", SIGNED_PATHS)
def test_presigned_unexpired_ok(path):
    current = datetime.now(timezone.utc).strftime(AMZ_DATE_FORMAT)
    req = _presigned_request(current, expires=3600, path=path)
    caller, err = auth.resolve_sigv4(req)
    assert err is None
    assert caller == Caller(email="ava@acme.com", is_admin=False)


# --- the two listings ------------------------------------------------------------


def _children(root) -> list[str]:
    """The result's child tags in document order, without the namespace."""
    return [c.tag[len(f"{{{S3NS}}}") :] for c in root]


def _listing(client, query, token):
    r = _s3_get(client, f"/s3/encoded-bucket?{query}", token)
    assert r.status_code == 200, r.text
    return ET.fromstring(r.text)


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
    resumes past that whole entry: real answers `?delimiter=/&marker=docs/` and `?delimiter=/&marker=docs/a.txt`
    alike with what follows `docs/`, never `docs/` again, so the walk terminates (measured
    2026-09-14).
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

    # The order among the refusals this path already had, with the bucket lookup in the middle of
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
