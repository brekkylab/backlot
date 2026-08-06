"""S3: ListObjectsV2, object reads, the XML shapes, and SigV4.

One file per router, so a provider's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""
from __future__ import annotations

import json
import urllib.request
import xml.etree.ElementTree as ET

import yaml

import pytest

from app import synth
from tests._helpers import client_for


# ------------------------------------------------------------------------ S3 (SigV4/404/416 edges)

def _sign_get(base_url, path, token, *, tamper=False, extra_headers=None):
    """Return (url, headers) for a SigV4-signed GET, using botocore (the real signer)."""
    pytest.importorskip("botocore")
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    from urllib.parse import parse_qsl, quote, urlencode
    from app import synth

    # URL-encode the path: split on ? to preserve the path part, then properly encode query params.
    # Use quote_via=quote (not the default quote_plus) so a space becomes %20, matching the server's
    # canonicalization (app.sigv4._canonical_query uses quote); quote_plus would emit '+' and mismatch.
    if "?" in path:
        path_part, query_part = path.split("?", 1)
        params = parse_qsl(query_part, keep_blank_values=True)
        query_part = urlencode(params, safe="-_.~", quote_via=quote)
        path = f"{path_part}?{query_part}"

    ak = synth.s3_access_key_id(token)
    sk = synth.s3_secret_access_key(token)
    url = f"{base_url}{path}"
    req = AWSRequest(method="GET", url=url, headers=dict(extra_headers or {}))
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
    req = urllib.request.Request(url, headers={
        "Authorization": ("AWS4-HMAC-SHA256 Credential=AKIABOGUS0000000BOGUS/"
                          "20260720/us-east-1/s3/aws4_request, "
                          "SignedHeaders=host, Signature=00"),
        "x-amz-date": "20260720T000000Z"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403 and b"InvalidAccessKeyId" in e.value.read()


def test_s3_tampered_signature_rejected(live_server):
    import urllib.request
    base_url, settings = live_server
    url, headers = _sign_get(base_url, "/s3/eng-artifacts?list-type=2",
                             settings.admin_token, tamper=True)
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


def test_s3_unsatisfiable_range_is_416(live_server):
    import urllib.request
    base_url, settings = live_server
    url, headers = _sign_get(base_url, "/s3/eng-artifacts/runbooks/oncall.md",
                             settings.admin_token, extra_headers={"Range": "bytes=99999-100000"})
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
        yield {"source_type": "s3", "doc_id": f"s3-big-{i:05d}", "bucket": "big-bucket",
               "group": group, "key": key, "title": key, "content": f"payload-{i}",
               "author_email": author, "author_groups": [group], "visibility": "group"}
    # A second, dedicated bucket for the CommonPrefixes-straddling regression (Fix 3): one
    # "folder" (150 objects) bigger than a max-keys=100 page, plus a small trailing folder — the
    # exact shape that made a rolled-up CommonPrefixes group straddle a page cutoff and get
    # emitted twice before the fix.
    for i in range(150):
        key = f"grp/big/f-{i:04d}.json"
        yield {"source_type": "s3", "doc_id": f"s3-straddle-big-{i:04d}", "bucket": "straddle-bucket",
               "group": "engineering", "key": key, "title": key, "content": f"big-payload-{i}",
               "author_email": "eng-bulk@acme.com", "author_groups": ["engineering"],
               "visibility": "public"}
    for i in range(5):
        key = f"grp/small/f-{i:02d}.json"
        yield {"source_type": "s3", "doc_id": f"s3-straddle-small-{i:02d}", "bucket": "straddle-bucket",
               "group": "engineering", "key": key, "title": key, "content": f"small-payload-{i}",
               "author_email": "eng-bulk@acme.com", "author_groups": ["engineering"],
               "visibility": "public"}


@pytest.fixture(scope="module")
def big_bucket_settings(tmp_path_factory):
    """A DB of its own (not the shared SAMPLE) holding one bucket with ~3000 S3 objects."""
    from app.importer.byo import load
    from app.config import Settings

    data_dir = tmp_path_factory.mktemp("s3_big")
    settings = Settings(data_dir=data_dir)
    corpus = data_dir / "_big_corpus.jsonl"
    corpus.write_text("\n".join(json.dumps(r) for r in _s3_big_corpus()))
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
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    from urllib.parse import parse_qsl, quote, urlencode
    from app import synth

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
    r = _s3_get(big_bucket_client,
               "/s3/big-bucket?list-type=2&prefix=logs/2026/01/&max-keys=1000",
               big_bucket_settings.admin_token)
    assert r.status_code == 200
    root = ET.fromstring(r.text)
    keys = _s3_keys(root)
    assert len(keys) == 250                                   # 3000 / 12 months
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
    r2 = _s3_get(big_bucket_client,
                f"/s3/big-bucket?list-type=2&max-keys=100&continuation-token={quote(token)}",
                admin)
    root2 = ET.fromstring(r2.text)
    keys2 = _s3_keys(root2)
    assert len(keys2) == 100 and keys2 == sorted(keys2)
    assert not (set(keys1) & set(keys2))               # no overlap between pages
    assert keys1[-1] < keys2[0]                        # contiguous keyset order, no gap/dup
    assert root2.findtext(f"{{{S3NS}}}ContinuationToken") == token


def test_s3_large_bucket_delimiter_returns_common_prefixes(big_bucket_client, big_bucket_settings):
    pytest.importorskip("botocore")
    # Under a single month (250 objects, well within one SQL page) every "day" folder rolls up
    # into one CommonPrefixes entry, computed over that bounded page — see the comment on
    # app.routers.s3._list_objects_v2 for why this only holds a page's worth of raw rows at once.
    r = _s3_get(big_bucket_client,
               "/s3/big-bucket?list-type=2&prefix=logs/2026/01/&delimiter=/&max-keys=1000",
               big_bucket_settings.admin_token)
    root = ET.fromstring(r.text)
    prefixes = {cp.findtext(f"{{{S3NS}}}Prefix")
               for cp in root.findall(f"{{{S3NS}}}CommonPrefixes")}
    assert prefixes == {f"logs/2026/01/{d:02d}/" for d in range(1, 26)}
    assert root.findall(f"{{{S3NS}}}Contents") == []      # every key continues past the delimiter
    assert root.findtext(f"{{{S3NS}}}IsTruncated") == "false"


def test_s3_large_bucket_acl_scopes_listing(big_bucket_client, big_bucket_settings, big_bucket_tokens):
    pytest.importorskip("botocore")

    def keys_for(token):
        r = _s3_get(big_bucket_client,
                   "/s3/big-bucket?list-type=2&prefix=logs/2026/01/&max-keys=1000", token)
        return {e.text for e in ET.fromstring(r.text).findall(f"{{{S3NS}}}Contents/{{{S3NS}}}Key")}

    admin_keys = keys_for(big_bucket_settings.admin_token)
    eng_keys = keys_for(big_bucket_tokens["eng-bulk@acme.com"])
    people_keys = keys_for(big_bucket_tokens["people-bulk@acme.com"])

    assert len(admin_keys) == 250
    assert eng_keys and people_keys
    assert eng_keys < admin_keys and people_keys < admin_keys      # proper, non-empty subsets
    assert eng_keys.isdisjoint(people_keys)
    assert eng_keys | people_keys == admin_keys


def test_s3_delimiter_common_prefix_not_duplicated_across_pages(big_bucket_client, big_bucket_settings):
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
        seen_prefixes += [cp.findtext(f"{{{S3NS}}}Prefix")
                          for cp in root.findall(f"{{{S3NS}}}CommonPrefixes")]
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
    """Fix 4: max-keys=0 must not crash (no indexing into an empty page) and must report
    IsTruncated based on whether more data exists, with KeyCount 0 and no NextContinuationToken."""
    pytest.importorskip("botocore")
    r = _s3_get(big_bucket_client, "/s3/big-bucket?list-type=2&max-keys=0",
               big_bucket_settings.admin_token)
    assert r.status_code == 200
    root = ET.fromstring(r.text)
    assert root.findtext(f"{{{S3NS}}}KeyCount") == "0"
    assert root.findall(f"{{{S3NS}}}Contents") == []
    assert root.findall(f"{{{S3NS}}}CommonPrefixes") == []
    assert root.findtext(f"{{{S3NS}}}IsTruncated") == "true"          # big-bucket has 3000 objects
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
