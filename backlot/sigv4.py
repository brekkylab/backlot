"""AWS Signature Version 4 verification for the S3 router — standard library only.

Real S3 clients (boto3, aioboto3/mirage, the AWS CLI the awslabs MCP server drives) always
SigV4-sign their requests. This module rebuilds the canonical request → string-to-sign →
signature so the server can authenticate them without adding botocore as a runtime dependency.

Only read-only GET/HEAD is served, so the payload hash is taken verbatim from the client's
``x-amz-content-sha256`` header (empty body / UNSIGNED-PAYLOAD) — no body hashing here. S3's
signer uses the request path *verbatim* as the canonical URI (no normalization, no re-encoding),
which is why the router passes the raw wire path through unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote

ALGORITHM = "AWS4-HMAC-SHA256"
AMZ_DATE_FORMAT = "%Y%m%dT%H%M%SZ"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str = "s3") -> bytes:
    k_date = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def parse_authorization(header: str | None) -> dict | None:
    """Parse a SigV4 ``Authorization`` header into its three fields, or None if malformed."""
    if not header or not header.startswith(ALGORITHM):
        return None
    parts: dict[str, str] = {}
    for kv in header[len(ALGORITHM) :].strip().split(","):
        k, _, v = kv.strip().partition("=")
        if k:
            parts[k.strip()] = v.strip()
    if not {"Credential", "SignedHeaders", "Signature"} <= parts.keys():
        return None
    return {
        "credential": parts["Credential"],
        "signed_headers": parts["SignedHeaders"],
        "signature": parts["Signature"],
    }


def split_credential(credential: str) -> tuple[str, str, str] | None:
    """``<AK>/<yyyymmdd>/<region>/s3/aws4_request`` -> ``(access_key, date_stamp, region)``."""
    bits = (credential or "").split("/")
    if len(bits) != 5 or bits[3] != "s3" or bits[4] != "aws4_request":
        return None
    return bits[0], bits[1], bits[2]


def _canonical_query(query: str) -> str:
    """RFC-3986 encode + sort the query params, excluding the presigned signature itself."""
    pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "X-Amz-Signature"]
    enc = sorted((quote(k, safe="-_.~"), quote(v, safe="-_.~")) for k, v in pairs)
    return "&".join(f"{k}={v}" for k, v in enc)


def _canonical_headers(headers: dict, signed_headers: str) -> str:
    out = []
    for name in signed_headers.split(";"):
        value = headers.get(name, "")
        out.append(f"{name}:{' '.join(value.split())}\n")
    return "".join(out)


def canonical_request(method, path, query, headers, signed_headers, payload_hash) -> str:
    return "\n".join(
        [
            method,
            path,  # S3: verbatim path, no normalization/re-encoding
            _canonical_query(query),
            _canonical_headers(headers, signed_headers),
            signed_headers,
            payload_hash,
        ]
    )


def parse_amz_date(amz_date: str) -> datetime | None:
    """Parse an ``x-amz-date``/``X-Amz-Date`` value (``YYYYMMDDTHHMMSSZ``, UTC).

    Returns ``None`` if the value is missing or doesn't match the expected format.
    """
    if not amz_date:
        return None
    try:
        return datetime.strptime(amz_date, AMZ_DATE_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_skewed(request_time: datetime, now: datetime, max_skew: int = 900) -> bool:
    """True if ``request_time`` is more than ``max_skew`` seconds away from ``now`` (either
    direction) — a pure function so the 15-minute clock-skew window is testable without a
    real clock."""
    return abs((now - request_time).total_seconds()) > max_skew


def expected_signature(
    secret, method, path, query, headers, signed_headers, payload_hash, amz_date, date_stamp, region
) -> str:
    cr = canonical_request(method, path, query, headers, signed_headers, payload_hash)
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    sts = "\n".join([ALGORITHM, amz_date, scope, _sha256_hex(cr.encode("utf-8"))])
    return hmac.new(
        _signing_key(secret, date_stamp, region), sts.encode("utf-8"), hashlib.sha256
    ).hexdigest()
