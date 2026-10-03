"""SigV4a (``AWS4-ECDSA-P256-SHA256``), the asymmetric form of Signature Version 4.

The canonical request is SigV4's (``backlot.sigv4.canonical_request``); what differs is the scope,
which names no region (`<date>/s3/aws4_request`), the region set the request signs instead
(`x-amz-region-set` in the header, `X-Amz-Region-Set` in the query), and the signature: an ECDSA
signature over P-256 and SHA-256, hex-encoded DER, under a key derived from the secret access key.
The derivation is the NIST SP 800-108 counter-mode KDF with HMAC-SHA256 that botocore's CRT signer
uses; a key derived here verified that signer's signatures, and real S3 served requests signed with
it, in the header and in the query (2026-09-29, us-east-1).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from functools import lru_cache

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

ALGORITHM = "AWS4-ECDSA-P256-SHA256"
# The order of P-256's base point: the derived scalar has to fall below it, less one.
_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


@lru_cache(maxsize=256)
def private_key(access_key: str, secret: str) -> ec.EllipticCurvePrivateKey:
    """The P-256 key a SigV4a signature is made with: each counter from 1 feeds one HMAC-SHA256 of
    the fixed input under `AWS4A` + the secret, and the first output below the order less one, plus
    one, is the scalar."""
    for counter in range(1, 255):
        fixed = (
            (1).to_bytes(4, "big")
            + ALGORITHM.encode()
            + b"\x00"
            + access_key.encode()
            + bytes([counter])
            + (256).to_bytes(4, "big")
        )
        digest = hmac.new(("AWS4A" + secret).encode(), fixed, hashlib.sha256).digest()
        candidate = int.from_bytes(digest, "big")
        if candidate <= _ORDER - 2:
            return ec.derive_private_key(candidate + 1, ec.SECP256R1())
    raise ValueError("no SigV4a key under 255 counters")


def string_to_sign(algorithm: str, amz_date: str, scope: str, canonical: str) -> str:
    """``algorithm`` as the header spells it, as SigV4's is: real signed a lower-case scheme's line
    as sent (2026-09-29)."""
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "\n".join([algorithm, amz_date, scope, digest])


def verify(access_key: str, secret: str, to_sign: str, signature: str) -> bool:
    """Whether ``signature`` is hex DER that verifies over ``to_sign``: upper-case hex verified on
    real, and `zz`, odd-length hex and 32 bytes that are not DER were each the mismatch (same
    date)."""
    try:
        raw = bytes.fromhex(signature)
    except ValueError:
        return False
    try:
        private_key(access_key, secret).public_key().verify(
            raw, to_sign.encode("utf-8"), ec.ECDSA(hashes.SHA256())
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def region_set_matches(region_set: str, region: str) -> bool:
    """Whether a comma-separated region set names ``region``. Each entry is matched whole and
    without case, `*` standing for any run of characters and `?` for itself, and an empty entry is
    skipped: real took `*`, `us-*`, `*-1`, `u*1`, `**`, `US-*`, `us-east-1*`, `,us-east-1` and
    `us-west-2,us-east-1` for us-east-1, and refused `us-east-?`, `us-east-10`, `us-east-`,
    `aws-global`, an empty set, `us-west-2, us-east-1`, `us-east-1;us-west-2` and
    `us-east-1 us-west-2` (2026-09-29)."""
    for entry in region_set.split(","):
        if not entry:
            continue
        pattern = ".*".join(re.escape(part) for part in entry.split("*"))
        if re.fullmatch(pattern, region, re.IGNORECASE):
            return True
    return False
