"""S3: ListObjectsV2, object reads, the XML shapes, and SigV4.

One file per router, so a provider's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import quote, urlencode

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
from tests._helpers import client_for


# ------------------------------------------------------------------------ S3 (SigV4/404/416 edges)


def _sign_get(base_url, path, token, *, tamper=False, extra_headers=None):
    """Return (url, headers) for a SigV4-signed GET, using botocore (the real signer)."""
    pytest.importorskip("botocore")
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    from urllib.parse import parse_qsl, quote, urlencode
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


@pytest.fixture(scope="module")
def big_bucket_settings(tmp_path_factory):
    """A DB of its own (not the shared SAMPLE) holding one bucket with ~3000 S3 objects."""
    from backlot.importer.byo import load
    from backlot.config import Settings

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
    # backlot.routers.s3._list_objects_v2 for why this only holds a page's worth of raw rows at once.
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


def test_s3_large_bucket_acl_scopes_listing(
    big_bucket_client, big_bucket_settings, big_bucket_tokens
):
    pytest.importorskip("botocore")

    def keys_for(token):
        r = _s3_get(
            big_bucket_client,
            "/s3/big-bucket?list-type=2&prefix=logs/2026/01/&max-keys=1000",
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
    """Fix 4: max-keys=0 must not crash (no indexing into an empty page) and must report
    IsTruncated based on whether more data exists, with KeyCount 0 and no NextContinuationToken."""
    pytest.importorskip("botocore")
    r = _s3_get(
        big_bucket_client, "/s3/big-bucket?list-type=2&max-keys=0", big_bucket_settings.admin_token
    )
    assert r.status_code == 200
    root = ET.fromstring(r.text)
    assert root.findtext(f"{{{S3NS}}}KeyCount") == "0"
    assert root.findall(f"{{{S3NS}}}Contents") == []
    assert root.findall(f"{{{S3NS}}}CommonPrefixes") == []
    assert root.findtext(f"{{{S3NS}}}IsTruncated") == "true"  # big-bucket has 3000 objects
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
    query_params, method, url.query, and scope['raw_path'] — plus a fake app.state.acl
    so `auth.acl(request)` resolves without a real ASGI app."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "headers": [(k.lower().encode("ascii"), v.encode("ascii")) for k, v in headers.items()],
        "scheme": "http",
        "server": ("mock", 80),
        "app": SimpleNamespace(state=SimpleNamespace(acl=_acl())),
    }
    return Request(scope)


def _header_auth_request(
    amz_date: str, path="/s3/eng-artifacts", query="list-type=2", region="us-east-1"
):
    """Build a header-auth GET signed for `amz_date` with a genuinely valid signature."""
    date_stamp = amz_date[:8]
    signed_headers = "host;x-amz-date"
    headers = {"host": "mock", "x-amz-date": amz_date, "x-amz-content-sha256": "UNSIGNED-PAYLOAD"}
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
    headers = {"host": "mock"}
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
    headers = {"host": "mock", "x-amz-date": stale, "x-amz-content-sha256": "UNSIGNED-PAYLOAD"}
    credential = f"{AK}/{date_stamp}/us-east-1/s3/aws4_request"
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={credential}, "
        f"SignedHeaders={signed_headers}, Signature=deadbeef"
    )
    caller, err = auth.resolve_sigv4(_request("GET", "/s3/eng-artifacts", "list-type=2", headers))
    assert caller is None
    assert err == "RequestTimeTooSkewed"


def test_header_auth_accepts_current_date():
    current = datetime.now(timezone.utc).strftime(AMZ_DATE_FORMAT)
    req = _header_auth_request(current)
    caller, err = auth.resolve_sigv4(req)
    assert err is None
    assert caller == Caller(email="ava@acme.com", is_admin=False)


def test_presigned_expired_is_access_denied():
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(AMZ_DATE_FORMAT)
    req = _presigned_request(stale, expires=60)
    caller, err = auth.resolve_sigv4(req)
    assert caller is None
    assert err == "AccessDenied"


def test_presigned_unexpired_ok():
    current = datetime.now(timezone.utc).strftime(AMZ_DATE_FORMAT)
    req = _presigned_request(current, expires=3600)
    caller, err = auth.resolve_sigv4(req)
    assert err is None
    assert caller == Caller(email="ava@acme.com", is_admin=False)
