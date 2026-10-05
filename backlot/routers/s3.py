"""Amazon S3 API (read-only, object storage).

Path-style endpoint for a client: ``http://<host>/s3`` (boto3: ``endpoint_url=".../s3"`` with
``addressing_style=path``; mirage: ``S3Config(endpoint_url=".../s3", path_style=True)``). Auth is
AWS SigV4, SigV4a or Signature Version 2, in the header or the query
(``backlot.auth.resolve_sigv4``), against a per-caller access-key/secret derived from a bearer
token; the admin/service token's key sees everything, a user's key is ACL-filtered, and an unsigned
request is the anonymous caller's, who sees no bucket (``_auth``). A method this router does not
serve, and a GET or a HEAD naming a selector it cannot take, is refused before the bucket and
whether or not a credential is sent, as real refuses them, but after a signature that was sent and
does not verify (``_method_refusal``, ``_read_refusal``); a write resolves the credential and the
bucket first. Responses are S3 XML (namespace ``http://s3.amazonaws.com/doc/2006-03-01/``) or raw
object bytes; errors use the S3 ``<Error>`` envelope.

S3 dispatches on the query string: ``?acl``, ``?versioning``, ``?tagging`` and the rest each select
a different operation at the same path. Every one of a bucket's is answered, as real answers a
bucket nobody configured, which is what every bucket here is (``_bucket_configuration``), and
``?versions`` is ListObjectVersions over the bucket's keys; ``?uploads`` (ListMultipartUploads) is
always the empty page, since data enters through ``backlot import`` and no upload is ever in
progress, and ``?uploadId`` at a key (ListParts) is ``NoSuchUpload`` for the same reason. A selector
whose operations are all on another method is the 405 a GET gets on real (``_BUCKET_READ_REFUSED``,
``_OBJECT_READ_REFUSED``), and a few more are refused at one path the way real refuses them there
(``_BUCKET_OBJECT_SELECTORS``, ``_KEY_BUCKET_REFUSALS``). An object's own are answered as real
answers an object with no tags and no annotations, written with no checksum header, in a bucket
without Object Lock (``_object_subresource``), ``?torrent`` being the 405 real gives its GET. The
writes are refused with ``NotImplemented`` (501), after what real checks of four of them first
(``_WRITE_CHECKS``, ``_KEY_WRITE_CHECKS``, ``_put_object_refusal``).

Object model: a bucket is the grouping/ACL unit (``s3_buckets``); an object is one doc
(``s3_objects``), ``key`` is its address and ``content`` its verbatim body. "Folders" are pure
key-prefix convention surfaced via ListObjectsV2's ``delimiter``/``CommonPrefixes``.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import re
import zlib
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import xxhash
from fastapi import APIRouter, Request, Response

from backlot import auth, sigv4, store, synth
from backlot.openapi import qp

router = APIRouter(prefix="/s3", tags=["s3"])
# A signed S3 request's canonical path is exact; letting Starlette 307-redirect a bare "/s3" ->
# "/s3/" would both break SigV4 (the redirected request no longer matches what was signed) and
# send botocore's bucket-region-redirect logic haywire. ListBuckets is registered for both
# "/s3" and "/s3/" below so the exact path always matches directly and no redirect is ever
# triggered (setting `router.redirect_slashes = False` here would be a no-op once this
# sub-router is flattened into the app by `include_router`).

NS = "http://s3.amazonaws.com/doc/2006-03-01/"
_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'
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
# back, which is why `list-type`, `marker` and `encoding-type` are declared below: the first chooses
# between the two listings, the second pages the V1 one, and the third changes how every key comes
# back. ListMultipartUploads' own `max-uploads` and `upload-id-marker` are absent for a different
# reason. They are not inert: _int32_param and _list_multipart_uploads read them, `max-uploads`
# comes back echoed, and either can turn the 200 into an InvalidArgument. What neither does is
# decide which uploads a caller gets, because there are never any — all they shape is an echo of the
# caller's own input on a page that is always empty. Declaring them would advertise a paging
# surface, a page size and a marker to resume from, over a listing that never has a second page.
# `key-marker` is declared for ListObjectVersions, whose pages it does resume.
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
        "fetch-owner",
        description="ListObjectsV2: true, exactly, adds each object's Owner, which ListObjects "
        "always carries; any other value leaves it out",
    ),
    qp(
        "start-after",
        description="ListObjectsV2 only: seeds the FIRST page; continuation-token wins over it — "
        "refused without list-type=2",
    ),
    qp(
        "encoding-type",
        description="url: every key and every prefix in the response comes back URL-encoded, and "
        "so do the echoes of delimiter, start-after and marker, under an EncodingType element; "
        "any other value is refused",
    ),
    qp(
        "max-keys",
        "integer",
        description=f"keys per page, served at most {_MAX_KEYS}, which is also the default; "
        "MaxKeys echoes the value as parsed, uncapped",
    ),
    qp(
        "continuation-token",
        description="ListObjectsV2: NextContinuationToken from a page whose IsTruncated was "
        "true — refused without list-type=2, and refused as incorrect when it does not decode, "
        "an empty value included. The four configuration lists read it too, and hand none out",
    ),
    qp(
        "versions",
        description="present, at any value: answer ListObjectVersions instead of a listing — "
        "every key as its one version, `null`, since no bucket here is versioned",
    ),
    qp(
        "key-marker",
        description="ListObjectVersions: resume after this key, or past the CommonPrefixes group "
        "holding it (NextKeyMarker from a truncated page)",
    ),
    qp(
        "version-id-marker",
        description="ListObjectVersions: with key-marker, the only version id there is, `null`; "
        "any other is refused, and so is one without a key-marker",
    ),
    qp(
        "id",
        description="with analytics, intelligent-tiering, inventory or metrics: the one "
        "configuration to answer instead of the list; none is configured, so any is "
        "NoSuchConfiguration",
    ),
    *(
        qp(
            selector,
            description=f"present, at any value: answer the bucket's {selector} configuration "
            "instead of a listing, as real answers a bucket nobody configured",
        )
        for selector in sorted(
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
            }
        )
    ),
    qp(
        "website",
        description="present with no value: answer the bucket's website configuration instead of "
        "a listing, as real answers a bucket nobody configured; a value is refused",
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
    "AuthorizationHeaderMalformed": 400,
    "AuthorizationQueryParametersError": 400,
    "InvalidAccessKeyId": 403,
    "SignatureDoesNotMatch": 403,
    "RequestTimeTooSkewed": 403,
    "AccessDenied": 403,
    "NoSuchBucket": 404,
    "NoSuchKey": 404,
    "NoSuchUpload": 404,
    "InvalidRange": 416,
    "InvalidArgument": 400,
    "InvalidRequest": 400,
    # What a bucket nobody configured answers its configurations with (``_UNCONFIGURED``,
    # ``_CONFIGURATION_LISTS``), and the refusals of a key's sub-resources, its parts and the write
    # checks.
    "NoSuchCORSConfiguration": 404,
    "NoSuchLifecycleConfiguration": 404,
    "MetadataConfigurationNotFound": 404,
    "ObjectLockConfigurationNotFoundError": 404,
    "NoSuchBucketPolicy": 404,
    "ReplicationConfigurationNotFoundError": 404,
    "NoSuchTagSet": 404,
    "NoSuchWebsiteConfiguration": 404,
    "NoSuchConfiguration": 404,
    "V1APIsNotAllowed": 405,
    "MalformedContinuationToken": 400,
    "NoLoggingStatusForKey": 400,
    "UserKeyMustBeSpecified": 400,
    "MissingRequestBodyError": 400,
    "MalformedXML": 400,
    "InvalidDigest": 400,
    "BadDigest": 400,
    "NoSuchVersion": 404,
    "InvalidPartNumber": 416,
    "NoSuchAnnotation": 404,
    "UserAnnotationNameMustBeSpecified": 400,
    # A SigV4a region set, a date past the year 9999 or a V2 query's `Expires` before the year zero
    # (``backlot.auth``), and a payload hash (``_payload_mismatch``, ``_put_object_refusal``).
    "RegionSetMismatch": 400,
    "InternalError": 500,
    "XAmzContentSHA256Mismatch": 400,
    "MissingContentLength": 411,
    # "A header you provided implies functionality that is not implemented. HTTP Status Code: 501"
    # — S3 API reference, the Error data type's code table.
    "NotImplemented": 501,
    # The four a method this router does not serve answers with, each measured.
    "MethodNotAllowed": 405,
    "PreconditionFailed": 412,
    "BadRequest": 400,
    "AccessForbidden": 403,
}

# The query keys that select an operation other than the listing at a bucket's path, and other than
# the object's bytes at an object's path. botocore's S3 service model (2006-03-01) declares each as
# its own operation — GetBucketVersioning as `GET /{Bucket}?versioning`, GetObjectTagging as
# `GET /{Bucket}/{Key+}?tagging` — and `backlot diff --source s3` asks a running server every one.
# Real S3 dispatches a GET on these keys and on the ones a GET cannot take (`_BUCKET_READ_REFUSED`,
# `_OBJECT_READ_REFUSED`), and ignores the other query keys measured (against a general purpose
# bucket: `?foo=bar` and `?x-id=ListObjects` both answer the listing, the match is case-sensitive so
# `?Versioning` lists too, and the value is ignored so `?versioning=1` is still
# GetBucketVersioning). The `x-id` key is what the AWS SDK for JavaScript adds to name the operation
# where several share a path — `?x-id=GetObject`, `?analytics&x-id=GetBucketAnalyticsConfiguration`
# — and it must stay ignorable here as it is there. So the refusal is keyed on this set rather than
# on an allow-list of the listing's own parameters, which would refuse what real S3 ignores.
#
# `session` is absent on purpose: CreateSession exists for directory buckets only, and real S3
# answers `GET /{bucket}?session` on a general purpose bucket with the listing (same measurement),
# so the listing IS the faithful answer. Every selector in the set is answered below
# (``_BUCKET_GETS``), and any two conflict the way real S3 conflicts them (`?uploads&versioning` is
# "Conflicting query string parameters: uploads, versioning", measured).
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
# The bucket selectors `bucket_get` answers, every one of them, written once so that the `Allow` on
# the HEAD refusal and the GET it names cannot drift apart (``_bucket_configuration``,
# ``_list_object_versions``, ``_list_multipart_uploads``).
_BUCKET_GETS = _BUCKET_SELECTORS
# Two object selectors a bucket's path answers, after the bucket: `?torrent` is the 405 naming
# `TORRENT` on a GET, and `?uploadId` is 400 `InvalidRequest`, "A key must be specified", on a GET,
# a `POST` and a `DELETE`, where each is `NoSuchBucket` for a bucket that does not exist (measured
# 2026-09-29 against us-east-1, the public bucket and a name nobody owns). A HEAD naming either is
# the 405.
_BUCKET_OBJECT_SELECTORS = frozenset({"torrent", "uploadId"})
_KEY_REQUIRED_FOR_UPLOAD = "A key must be specified"
# The bucket selectors a key's path answers as the bucket's own operation, whatever the key: real
# answered each of these but `?logging` and `?versions` on a GET at a key with the bucket's answer,
# the same for a key the bucket has and one it does not, and `NoSuchBucket` in a bucket that does
# not exist, where the other bucket selectors are ignored there and the key answered (measured
# 2026-09-29, every bucket selector on a GET and a HEAD at a key, and again on a bucket this account
# created, whose answers at a key it has and one it does not were the bucket's own, byte for byte).
# Here they are what the bucket's path answers them with (``_bucket_configuration``), apart from
# `?logging` and `?versions`, which real refuses at a key with a 400 of their own.
_KEY_BUCKET_SELECTORS = frozenset(
    {
        "accelerate",
        "cors",
        "inventory",
        "lifecycle",
        "location",
        "logging",
        "notification",
        "policy",
        "replication",
        "requestPayment",
        "versioning",
        "versions",
        "website",
    }
)
_KEY_BUCKET_REFUSALS = {
    "logging": (
        "NoLoggingStatusForKey",
        "There is no such thing as the ?logging sub-resource for a key",
    ),
    "versions": (
        "InvalidRequest",
        "There is no such thing as the ?versions sub-resource for a key",
    ),
}
# The selectors `object_get` answers rather than refusing, which the `Allow` at a key names: the
# bucket's it answers there, the object's own, `?torrent` apart (``_object_subresource``), and
# ListParts' `?uploadId`.
_OBJECT_GETS = (_KEY_BUCKET_SELECTORS - _KEY_BUCKET_REFUSALS.keys()) | {
    "acl",
    "annotation",
    "attributes",
    "legal-hold",
    "retention",
    "tagging",
    "uploadId",
}

# What real answers each of a bucket's configurations with on a bucket nobody configured, which is
# every bucket here: measured 2026-09-29 on a bucket this account created in us-east-1 that day,
# with its defaults, signed by its owner, a GET of each selector. Each is copied as real sent it,
# the prolog before the root element included — the declaration and a newline, the declaration
# alone, or nothing — and the `Content-Type`, which real left off every 200 here but `?acl`,
# `?location`, `?logging`, `?uploads` and `?versions`. The defaults are the ones S3 gives a new
# bucket: SSE-S3 with SSE-C blocked, `BucketOwnerEnforced`, every public-access block on.
_ANSWERED = {
    "abac": (f'<AbacStatus xmlns="{NS}"><Status>Disabled</Status></AbacStatus>', None),
    "accelerate": (f'<AccelerateConfiguration xmlns="{NS}"/>', None),
    "encryption": (
        f'<ServerSideEncryptionConfiguration xmlns="{NS}"><Rule>'
        "<BucketKeyEnabled>false</BucketKeyEnabled><ApplyServerSideEncryptionByDefault>"
        "<SSEAlgorithm>AES256</SSEAlgorithm></ApplyServerSideEncryptionByDefault>"
        "<BlockedEncryptionTypes><EncryptionType>SSE-C</EncryptionType></BlockedEncryptionTypes>"
        "</Rule></ServerSideEncryptionConfiguration>",
        None,
    ),
    "location": (f'<LocationConstraint xmlns="{NS}"/>', "application/xml"),
    "notification": (f'<NotificationConfiguration xmlns="{NS}"/>', None),
    "ownershipControls": (
        f'<OwnershipControls xmlns="{NS}"><Rule><ObjectOwnership>BucketOwnerEnforced'
        "</ObjectOwnership></Rule></OwnershipControls>",
        None,
    ),
    "publicAccessBlock": (
        f'<PublicAccessBlockConfiguration xmlns="{NS}"><BlockPublicAcls>true</BlockPublicAcls>'
        "<IgnorePublicAcls>true</IgnorePublicAcls><BlockPublicPolicy>true</BlockPublicPolicy>"
        "<RestrictPublicBuckets>true</RestrictPublicBuckets></PublicAccessBlockConfiguration>",
        None,
    ),
    "requestPayment": (
        f'<RequestPaymentConfiguration xmlns="{NS}"><Payer>BucketOwner</Payer>'
        "</RequestPaymentConfiguration>",
        None,
    ),
    "versioning": (f'<VersioningConfiguration xmlns="{NS}"/>', None),
}
# `?logging` is the one whose prolog is two newlines and whose body carries real's own comment.
_LOGGING = (
    f'<BucketLoggingStatus xmlns="{NS}">\n'
    "  <!--<LoggingEnabled><TargetBucket>myLogsBucket</TargetBucket><TargetPrefix>add/this/prefix"
    "/to/my/log/files/access_log-</TargetPrefix></LoggingEnabled>-->\n</BucketLoggingStatus>\n"
)
# The configurations a new bucket has none of: real's 404 naming the bucket, and for
# `?metadataTable` the 405 real gives this account's buckets in place of that V1 operation. The
# prolog is the declaration and a newline unless ``_NO_PROLOG`` names the code.
_UNCONFIGURED = {
    "cors": ("NoSuchCORSConfiguration", "The CORS configuration does not exist"),
    "lifecycle": ("NoSuchLifecycleConfiguration", "The lifecycle configuration does not exist"),
    "metadataConfiguration": (
        "MetadataConfigurationNotFound",
        "The metadata configuration was not found",
    ),
    "metadataTable": (
        "V1APIsNotAllowed",
        "The V1 GetBucketMetadataTableConfiguration API operation isn't available for this "
        "account. Use the corresponding V2 GetBucketMetadataConfiguration API operation instead.",
    ),
    "object-lock": (
        "ObjectLockConfigurationNotFoundError",
        "Object Lock configuration does not exist for this bucket",
    ),
    "policy": ("NoSuchBucketPolicy", "The bucket policy does not exist"),
    "policyStatus": ("NoSuchBucketPolicy", "The bucket policy does not exist"),
    "replication": (
        "ReplicationConfigurationNotFoundError",
        "The replication configuration was not found",
    ),
    "tagging": ("NoSuchTagSet", "The TagSet does not exist"),
    "website": (
        "NoSuchWebsiteConfiguration",
        "The specified bucket does not have a website configuration",
    ),
}
_NO_PROLOG = frozenset(
    {"NoSuchLifecycleConfiguration", "MetadataConfigurationNotFound", "V1APIsNotAllowed"}
)
# The four configurations that are lists, each empty on a new bucket: the root real names, the
# prolog of its 200, and the prolog of its two refusals, `id` naming a configuration there is none
# of and a `continuation-token` it cannot read (same measurement).
_CONFIGURATION_LISTS = {
    "analytics": ("ListBucketAnalyticsConfigurationsResult", "", _DECLARATION + "\n"),
    "intelligent-tiering": ("ListIntelligentTieringConfigurationsResult", "", ""),
    "inventory": ("ListInventoryConfigurationsResult", _DECLARATION, ""),
    "metrics": ("ListMetricsConfigurationsResult", _DECLARATION, _DECLARATION + "\n"),
}


# --------------------------------------------------------------------------- helpers


def _xml(
    body: str,
    status: int = 200,
    headers: dict | None = None,
    *,
    prolog: str = _DECLARATION + "\n",
    media_type: str | None = "application/xml",
) -> Response:
    """An XML answer. ``prolog`` and ``media_type`` are what real sends before the body and as its
    type, which is the declaration and a newline as `application/xml` for most answers and something
    else for a few (``_ANSWERED``, ``_NO_PROLOG`` and ``_CONFIGURATION_LISTS`` name which)."""
    return Response(
        content=prolog + body, media_type=media_type, status_code=status, headers=headers
    )


# One request id pair per request, so the headers and the error body name the same one. A context
# variable rather than an argument because `_error` is reached from helpers that read the query
# string and never the request (`_argument_error`, `_int32_param`), and the pair is the
# request's rather than any one refusal's. `backlot.main.answer_s3_with_request_ids` sets it and
# puts the same pair on the headers of every answer this router gives.
REQUEST_IDS: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "s3_request_ids", default=None
)

# Every symbol real S3's own answers put in a request id: `0`-`9` and `A`-`Z` without `I`, `L`, `O`
# and `U` (`request_ids` has the measurement).
_REQUEST_ID_SYMBOLS = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def request_ids(method: str, path: str, query: str) -> tuple[str, str]:
    """``(x-amz-request-id, x-amz-id-2)`` for one request, in the shape real S3's own answers give.

    Measured 2026-09-22 at ap-northeast-2 over twenty-five response shapes, a success and a refusal
    alike: both rode every one. Their shape was measured 2026-09-23 over forty each of a 200, a GET
    404, a HEAD 404 and a 405, in each of us-east-1 and ap-northeast-2: the id was 16 characters on
    all 320, and `_REQUEST_ID_SYMBOLS` is every symbol those 320 used. The parse 400 carries an id of
    another shape (``backlot.errors.s3.method_not_allowed``). The extended id is base64 whose width
    is not a property of the answer: over those 320 and eighty parse 400s it was 96 characters 240
    times, 76 108 times, 128 46 times, 120 five times and 108 once, and every kind of answer came at
    more than one width, so this sends the 96. Seeded from the request rather than randomised, so a
    corpus served twice answers the same pair, which is what an ETag and a synthesised id already
    do here.
    """
    raw = hashlib.shake_256(f"s3-req:{method} {path}?{query}".encode()).digest(82)
    n = int.from_bytes(raw[:10])
    request_id = "".join(_REQUEST_ID_SYMBOLS[(n >> shift) & 31] for shift in range(75, -1, -5))
    return request_id, base64.b64encode(raw[10:]).decode("ascii")


def _error(
    code: str,
    message: str,
    extra: str = "",
    headers: dict | None = None,
    *,
    prolog: str = _DECLARATION + "\n",
) -> Response:
    """Real S3's `<Error>`: the code, the message, ``extra``, then the request id pair.

    ``extra`` is whatever real names the failure with, which is a member of the code's own rather
    than one element every code shares: `BucketName` for `NoSuchBucket`, the key as `Key` for
    `NoSuchKey`, alone on GetObject and with its bucket on `?tagging` and UpdateObjectEncryption,
    `ArgumentName` and mostly `ArgumentValue` for `InvalidArgument`, `RangeRequested` and
    `ActualObjectSize` for `InvalidRange`, and `Method` and `ResourceType` for `MethodNotAllowed`.
    Measured 2026-09-29 and 2026-09-30 against `s3.us-east-1.amazonaws.com`: none of the 267
    distinct error bodies of 34 codes those days' captures hold, request ids set aside, carried a
    `Resource`, a key in an absent bucket and a missing key under a prefix among them.
    `NotImplemented` is this server's own refusal, with no real body to copy, and names nothing. A
    credential refusal's members are the ones ``backlot.auth.resolve_sigv4`` names
    (``_refused_credential``)."""
    ids = REQUEST_IDS.get()
    # Real names them last, after the members that describe the failure (measured over seven error
    # bodies: `NoSuchBucket`, `NoSuchKey`, `InvalidArgument`, `MethodNotAllowed`, `BadRequest`,
    # `PreconditionFailed` and `AccessForbidden`).
    tail = f"<RequestId>{ids[0]}</RequestId><HostId>{ids[1]}</HostId>" if ids else ""
    body = f"<Error><Code>{code}</Code><Message>{escape(message)}</Message>{extra}{tail}</Error>"
    return _xml(body, status=_ERR_STATUS.get(code, 400), headers=headers, prolog=prolog)


def _no_such_bucket(bucket: str) -> Response:
    return _error(
        "NoSuchBucket",
        "The specified bucket does not exist",
        f"<BucketName>{escape(bucket)}</BucketName>",
    )


def _no_such_key(key: str) -> Response:
    return _error("NoSuchKey", "The specified key does not exist.", f"<Key>{escape(key)}</Key>")


def _head(status: int, headers: dict | None = None) -> Response:
    """An answer to a `HEAD` other than an object's 200 or 206: no body, framed as real frames it.

    Real sends each of them `Transfer-Encoding: chunked` with no `Content-Length`, and as
    `application/xml` — the service root's 405, a bucket's 200, the 404s, a selector's 405 and the
    conflict's 400, a 416 and the credential's 403s alike — where an object's 200 and 206 carry the
    object's type and the length a GET would send (measured 2026-09-29 against
    `s3.us-east-1.amazonaws.com`, twenty-two `HEAD`s, signed and unsigned). Starlette writes a
    `Content-Length` on each of these, `0` for the empty body, so it is taken off here and the
    framing named instead: uvicorn's httptools protocol writes no body for a `HEAD` whatever its
    framing header says, and its h11 protocol keeps a chunked framing it is given, so
    `backlot serve` sends what real sends on either.
    """
    response = Response(status_code=status, media_type="application/xml", headers=headers)
    del response.headers["content-length"]
    response.headers["transfer-encoding"] = "chunked"
    return response


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


def _argument_error(message: str, name: str, value: str) -> Response:
    """Real S3's ``InvalidArgument`` for one query parameter: the message beside the parameter's
    name and the value as sent, in ``ArgumentName`` and ``ArgumentValue`` (measured)."""
    return _error(
        "InvalidArgument",
        message,
        extra=(
            f"<ArgumentName>{escape(name)}</ArgumentName>"
            f"<ArgumentValue>{escape(value)}</ArgumentValue>"
        ),
    )


def _argument_name(name: str) -> str:
    """``ArgumentName`` with no ``ArgumentValue`` beside it.

    Real S3 leaves the value out of the three cross-version refusals and of the unreadable
    ``continuation-token`` refusal — the body is ``Code``, ``Message`` and ``ArgumentName`` alone,
    whatever was sent and for an empty value too (measured 2026-09-14 and 2026-09-17). The other
    ``InvalidArgument`` refusals here carry both (see ``_argument_error``)."""
    return f"<ArgumentName>{escape(name)}</ArgumentName>"


def _conflict(selected: list[str]) -> Response:
    """Two selectors at once, refused as real S3 refuses them (measured).

    The name real reports here is ``ResourceType`` rather than a parameter's, and the value is the
    first selector it names in the message."""
    return _argument_error(
        "Conflicting query string parameters: " + ", ".join(selected),
        "ResourceType",
        selected[0],
    )


def _refused_credential(refusal: auth.SigV4Refusal, head: bool = False) -> Response:
    """Real's answer for a credential it refuses, members and all; on a `HEAD`, its status alone."""
    if head:
        return _head(_ERR_STATUS[refusal.code])
    members = "".join(f"<{name}>{escape(value)}</{name}>" for name, value in refusal.members)
    return _error(refusal.code, refusal.message, members)


# The payload hashes that name a trailing checksum, which an upload's aws-chunked body carries
# (``_put_object_refusal``). Real refused each on a GET with "The value of x-amz-content-sha256
# header is invalid.", and `STREAMING-UNSIGNED-PAYLOAD-TRAILER` likewise on a GET of a listing, of a
# bucket's `?versioning` and `?acl`, of an object and of a key the bucket does not hold, on a HEAD,
# a `DELETE`, a `POST ?delete`, a key's `PUT ?tagging`, a presign's GET and the service root's. It
# came after a signature that fails, after a bucket that does not exist and after what is refused
# before the credential, a `max-keys` of `abc` and two selectors, and ahead of a
# `continuation-token`, a `partNumber` and a `versionId` that are refused after the bucket
# (2026-09-29).
_TRAILERS = frozenset(
    {
        "STREAMING-UNSIGNED-PAYLOAD-TRAILER",
        "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER",
        "STREAMING-AWS4-ECDSA-P256-SHA256-PAYLOAD-TRAILER",
    }
)


def _trailer_refusal(request: Request, head: bool = False) -> Response | None:
    """The 400 for a payload hash in ``_TRAILERS`` on an operation that takes no upload, or
    ``None``."""
    if auth.signed_payload_hash(request) not in _TRAILERS:
        return None
    if head:
        return _head(400)
    return _error("InvalidRequest", "The value of x-amz-content-sha256 header is invalid.")


def _signature_refusal(request: Request) -> auth.SigV4Refusal | None:
    """The refusal of a credential that was sent and does not verify, or ``None``.

    ``None`` as well when no credential is sent: that is an anonymous caller's request, on real and
    here (``backlot.auth.resolve_sigv4``), and real gives it the method and selector refusals a
    signed request gets, where a signature that fails and an access key AWS does not know are each
    refused first. Measured 2026-09-29 against `s3.us-east-1.amazonaws.com`: twelve selector
    refusals across a GET and a HEAD at a bucket and at a key, each sent signed, unsigned, signed
    over a bad secret and with an unknown key, and ten method refusals at a bucket, a key and the
    service root, which the bad secret and the unknown key each turned into their 403.
    """
    return auth.resolve_sigv4(request)[1]


def _method_refusal(request: Request, resource_type: str, headers: dict | None = None) -> Response:
    """The 405 naming the method and the resource type, once a signature that was sent verifies."""
    head = request.method == "HEAD"
    refusal = _signature_refusal(request)
    if refusal is not None:
        return _refused_credential(refusal, head)
    if head:
        return _head(405, headers)
    return _error(
        "MethodNotAllowed",
        _METHOD_NOT_ALLOWED,
        extra=_method_type(request.method, resource_type),
        headers=headers,
    )


def _bucket_selected(q, selectors: frozenset[str]) -> list[str]:
    """The selectors a request at a bucket's path carries, `partNumber` among them, with `PART`
    standing for `partNumber` beside `uploadId`, which is UploadPart (``_UPLOAD_PART``).

    `partNumber` names a part of an object, which a bucket's path has none of: real answered
    `?partNumber=1` there with 400 `InvalidRequest`, "Object must have a valid key name.", on a
    GET, a HEAD, a `PUT`, a `POST`, a `DELETE` and a `PATCH`, and for `abc` and an empty value as
    for `1`, before the credential (signed, unsigned and over a bad secret alike) and before the
    listing's own parameters (``?partNumber=1&max-keys=abc`` is still this 400); beside another
    selector it is the conflict (``acl, partNumber``), and beside `uploadId` the 405 naming `PART`
    with `Allow: PUT` (measured 2026-09-29, us-east-1).
    """
    selected = _selected(q, selectors | {"partNumber"})
    if "partNumber" in selected and "uploadId" in q and set(selected) <= {"partNumber", "uploadId"}:
        return ["PART"]
    return selected


def _read_refusal(
    request: Request,
    selected: list[str],
    refused: dict[str, str],
    served_on_get: frozenset[str],
    first: Response | None = None,
) -> Response | None:
    """What a GET or a HEAD naming a selector is refused with before the bucket is looked up, or
    ``None`` when the request goes on to be answered.

    Two selectors are the conflict before any credential is read: real answered seven such requests,
    at a bucket and at a key, on a GET and a HEAD, and in a bucket that does not exist, with the 400
    whether each was signed, unsigned, signed over a bad secret or sent with an unknown key
    (2026-09-29). So is a bucket's `partNumber` (``_bucket_selected``). Then ``first``, what the
    caller refuses before the credential — the parses, a `versionId`, a valued `website`
    (``_version_id_refusal``, ``_website_refusal``) — and after it one selector is refused on a
    HEAD whatever it is (``_head_refusal``) and on a GET when the GET cannot take it (``refused``,
    the type its 405 names), once a signature that was sent verifies (``_signature_refusal``).
    """
    head = request.method == "HEAD"
    if len(selected) > 1:
        return _head_refusal(selected, served_on_get) if head else _conflict(selected)
    if selected == ["partNumber"]:
        return _head(400) if head else _error("InvalidRequest", _KEY_REQUIRED)
    if first is not None:
        return _head(first.status_code) if head else first
    if not selected or not (head or selected[0] in refused):
        return None
    if head:
        refusal = _signature_refusal(request)
        return (
            _refused_credential(refusal, head)
            if refusal
            else _head_refusal(selected, served_on_get)
        )
    return _method_refusal(request, refused[selected[0]])


_NO_VERSION_ID = "This operation does not accept a version-id."


def _version_id_refusal(q, selected: list[str], method: str = "GET") -> Response | None:
    """A `versionId` at a bucket's path, which names a version of an object and no operation there
    takes one: real refused it on every GET at a bucket's path — both listings, each of the bucket's
    selectors and `?session` — and on a `DELETE` and a selector's `PUT` (`?versioning`, `?tagging`),
    naming the value as sent, an empty one and `null` alike, where a GET's `?acl` calls it an
    invalid id instead; before the credential and the bucket, and after the conflict, a bucket's
    `partNumber` and the listings' parses, so that `?delete&versionId=x` is this 400 and not the 405
    (measured 2026-09-29 and 2026-09-30). The writes that read it after the bucket are
    ``_VERSION_AFTER_THE_BUCKET``'s."""
    if "versionId" not in q:
        return None
    message = _BAD_VERSION if selected == ["acl"] and method in ("GET", "HEAD") else _NO_VERSION_ID
    return _argument_error(message, "versionId", _first(q, "versionId"))


def _website_refusal(q) -> Response | None:
    """`?website` with a value, which real refused at a bucket's path and at a key's, on a GET, a
    HEAD and a `PUT`, before the credential and the bucket, naming the first value; an empty one is
    `?website`, and `versionId` comes first (measured 2026-09-29)."""
    value = _first(q, "website", None)
    if value:
        return _argument_error("The website parameter must not have a value", "website", value)
    return None


def _key_selected(q) -> list[str]:
    """The selectors a GET or a HEAD at a key's path carries, `annotationName` among them when no
    `annotation` names the operation it belongs to: alone, real refused it at a key with "Unexpected
    query string parameter" under `ResourceType` on a GET and with the 400 on a HEAD, and beside
    another selector it is the conflict (`acl, annotationName`), before the credential and the
    bucket (measured 2026-09-29). Not every front end of real's refuses it on a GET: asked one
    address of `s3.us-east-1.amazonaws.com` at a time on 2026-09-30, about four in five refused it
    on every request and most of the rest answered every one with the object, as if it were absent,
    while 90 HEADs spread over whichever address each reached were all the 400 (2026-09-29). The
    refusal is what most of them give and what a HEAD gets. At a bucket's path it is left to the
    listing, as real leaves it. `partNumber` beside a selector other than `uploadId` is the conflict
    too (`?partNumber=1&tagging` is "partNumber, tagging", same date); alone it is GetObject's
    (``_part_refusal``), and beside `uploadId` UploadPart."""
    selected = _selected(q, _OBJECT_READ_SELECTORS)
    if "annotationName" in q and "annotation" not in q:
        selected = sorted([*selected, "annotationName"])
    if "partNumber" in q and selected and selected != ["uploadId"]:
        selected = sorted([*selected, "partNumber"])
    return selected


def _head_refusal(selected: list[str], served_on_get: frozenset[str]) -> Response:
    """HEAD with a sub-resource selector, refused before the bucket or the key is looked up.

    No sub-resource has a HEAD form: real S3 answers ``HEAD /{bucket}?versioning`` and
    ``HEAD /{key}?acl`` 405 with an empty ``application/xml`` body, whether or not the bucket or the
    key exists, and two selectors at once with the conflict's 400 and the same empty body (measured;
    ``_head`` frames both). Real S3 also sends an ``Allow`` naming the methods that sub-resource
    takes, GET and PUT and DELETE among them (measured), and what an ``Allow`` names is the resource
    answering rather than S3: "a list of the target resource's currently supported methods" (RFC
    9110, Section 15.5.6). ``served_on_get`` is that list — the selectors this same path answers on
    a GET, which is a different set at a bucket's path and at a key's — so each of them gets
    ``GET`` and every other selector gets no header, naming a method there being as false a claim
    as repeating real's PUT and DELETE.

    No header leaves Section 15.5.6's MUST unmet, and Section 10.2.1's "An empty Allow field value
    indicates that the resource allows no methods" would meet it and does survive this stack. Real
    S3 sends an empty ``Allow`` on none of these rows (measured), so the absent header is preferred
    to a value real never sends.
    """
    if len(selected) > 1:
        return _head(400)
    return _head(405, {"Allow": "GET"} if selected[0] in served_on_get else None)


def _auth(request: Request):
    """``(caller, visible_ids, None)``, or ``(None, None, refusal)`` for a credential real refuses.

    An unsigned request is the anonymous caller's, who can see nothing (``backlot.acl``), so every
    bucket is one it cannot see: `NoSuchBucket`, the answer a user gets for a bucket it holds no
    readable object in. For a name no bucket has, that is real's own answer to an unsigned request
    (measured 2026-09-29); for one this corpus holds, real says `AccessDenied`, and saying
    `NoSuchBucket` instead keeps an anonymous caller from learning which names the corpus holds.
    """
    caller, refusal = auth.resolve_sigv4(request)
    if refusal is not None:
        return None, None, _refused_credential(refusal, request.method == "HEAD")
    return caller, auth.visible_ids(request, caller), None


def _owner_id(request: Request) -> str:
    """A stable canonical user id for the org that owns every bucket here, in real's shape: 64
    lower-case hex digits, which real's own owner id was (2026-09-29)."""
    return synth._digest("s3-owner:" + request.app.state.acl.org_name)


def _owner_xml(request: Request) -> str:
    """The owner as ListBuckets names it, the id and no `DisplayName` (same date)."""
    return f"<Owner><ID>{_owner_id(request)}</ID></Owner>"


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
    if the token is malformed, which the listing refuses."""
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
    """ListBuckets — every bucket the caller can see, in name order.

    A bucket is visible when any object in it is, so a caller with no readable object in a bucket
    does not learn the bucket exists. An unsigned request is real's 307 to the product page, with no
    body, where it answers a signed one with the listing.

    Each bucket is its name, its creation date and its ARN, and with any of ListBuckets' own
    parameters sent (``_LIST_BUCKETS_PARAMETERS``), an empty one too, its region as well, between
    the date and the ARN; after the buckets come the prefix, when one was sent, an empty one
    included, and a continuation token, when the page is truncated, which resumes after the page's
    last name. An empty page is `<Buckets/>`. `max-buckets`, `prefix` and `bucket-region` are each
    read where they are first sent. `max-buckets` is parsed as `max-keys` is and has to be 1 to
    10000, a value outside that named as parsed (`-01` as `-1`), and `bucket-region` has to be
    us-east-1, compared without case: another region (``_AWS_REGIONS``) is refused as one this
    endpoint does not serve, and any other value, a region with a space beside it among them, as no
    region, named as sent. Those two come before the credential, unsigned and over a bad secret
    alike, and `max-buckets` first; a `continuation-token` that does not decode after it, an
    unsigned request being the 307 then (all measured 2026-09-29 on this account's buckets in
    us-east-1)."""
    q = request.query_params
    paged = any(name in q for name in _LIST_BUCKETS_PARAMETERS)
    max_buckets = _MAX_BUCKETS
    if "max-buckets" in q:
        max_buckets, err = _int32_param(q, "max-buckets", _MAX_BUCKETS)
        if err:
            return err
        if not 1 <= max_buckets <= _MAX_BUCKETS:
            return _argument_error(
                f"Argument max-buckets must be an integer between 1 and {_MAX_BUCKETS}",
                "max-buckets",
                str(max_buckets),
            )
    region = _first(q, "bucket-region", None)
    if region is not None and region.lower() != sigv4.REGION:
        message = (
            "Requests with bucket-region specified must be made to the corresponding regional "
            "endpoint"
            if region.lower() in _AWS_REGIONS
            else f"Argument value {region} is not a valid AWS Region"
        )
        return _error("InvalidArgument", message, extra=_argument_name("bucket-region"))
    caller, visible, err = _auth(request)
    if err:
        return err
    if caller.is_anonymous:
        return Response(status_code=307, headers={"Location": _ANONYMOUS_LIST_BUCKETS})
    trailer = _trailer_refusal(request)
    if trailer is not None:
        return trailer
    after = None
    token = _first(q, "continuation-token", None)
    if token:
        decoded = _decode_token(token)
        if decoded is None or decoded[0] != "after":
            return _error(
                "InvalidArgument",
                "The continuation token provided is incorrect",
                extra=_argument_name("continuation-token"),
            )
        after = decoded[1]
    prefix = _first(q, "prefix", None)
    conn = auth.conn(request)
    names = sorted(
        b["name"]
        for b in store.list_containers(conn, "s3")
        if b["name"].startswith(prefix or "")
        and (after is None or b["name"] > after)
        and _bucket_visible(conn, b["name"], visible)
    )
    page, truncated = names[:max_buckets], len(names) > max_buckets
    region_xml = f"<BucketRegion>{sigv4.REGION}</BucketRegion>" if paged else ""
    items = "".join(
        f"<Bucket><Name>{escape(b)}</Name>"
        f"<CreationDate>{synth.s3_iso(synth.epoch('s3-bucket:' + b))}</CreationDate>"
        f"{region_xml}<BucketArn>arn:aws:s3:::{escape(b)}</BucketArn></Bucket>"
        for b in page
    )
    tail = f"<Prefix>{escape(prefix)}</Prefix>" if prefix is not None else ""
    if truncated:
        tail += f"<ContinuationToken>{_encode_key_token(page[-1])}</ContinuationToken>"
    buckets = f"<Buckets>{items}</Buckets>" if items else "<Buckets/>"
    return _xml(
        f'<ListAllMyBucketsResult xmlns="{NS}">{_owner_xml(request)}{buckets}{tail}'
        "</ListAllMyBucketsResult>"
    )


# ListBuckets' own parameters, any of which turns on the paged form of its answer (same date).
_LIST_BUCKETS_PARAMETERS = ("max-buckets", "prefix", "continuation-token", "bucket-region")
_MAX_BUCKETS = 10000
# The regions `bucket-region` takes as regions: every region botocore 1.40.61's endpoint partitions
# name, 46 of them. Real served us-east-1 and refused each of the other 45 as needing its own
# endpoint, where `us-east-9`, `eu-west-9`, `global`, `local`, `us-east-1a` and an empty value were
# refused as no region at all, and so were `us-east-1` with a space before or after it or a tab
# after it and `us-west-2` with a space before it or a tab after it (2026-09-29).
_AWS_REGIONS = frozenset(
    """af-south-1 ap-east-1 ap-east-2 ap-northeast-1 ap-northeast-2 ap-northeast-3 ap-south-1
    ap-south-2 ap-southeast-1 ap-southeast-2 ap-southeast-3 ap-southeast-4 ap-southeast-5
    ap-southeast-6 ap-southeast-7 ca-central-1 ca-west-1 cn-north-1 cn-northwest-1 eu-central-1
    eu-central-2 eu-isoe-west-1 eu-north-1 eu-south-1 eu-south-2 eu-west-1 eu-west-2 eu-west-3
    eusc-de-east-1 il-central-1 me-central-1 me-south-1 mx-central-1 sa-east-1 us-east-1 us-east-2
    us-gov-east-1 us-gov-west-1 us-iso-east-1 us-iso-west-1 us-isob-east-1 us-isob-west-1
    us-isof-east-1 us-isof-south-1 us-west-1 us-west-2""".split()
)


# What this server serves at each path, which is what its `Allow` names — real names its own
# methods there (`HEAD, DELETE, POST, GET, PUT` on a bucket), and naming those would tell a client
# about methods this server refuses. The sub-resource 405 already draws that line.
_ALLOW_BUCKET = "GET, HEAD"
_ALLOW_OBJECT = "GET, HEAD"


@router.head("/{bucket}")
async def head_bucket(request: Request, bucket: str):
    """HeadBucket — 200 if the caller can see this bucket, 404 if they cannot (an unsigned caller
    sees none), and a refused credential's own status, 403 or 400, as its status alone.

    Headers alone in every case, which is why it carries no MCP tool (see ``backlot.openapi``). The
    200 carries the two real sends beside the region, the bucket's ARN and that the name is not an
    access point alias, which boto3's ``head_bucket`` returns as ``BucketArn`` and
    ``AccessPointAlias`` (measured 2026-09-29 on the public bucket `noaa-ghcn-pds` in us-east-1).

    A HEAD reads the listing's parameters the way the listing's GET does, and answers each refusal
    as its status alone: `max-keys` that does not parse and the other version's parameters before
    the credential and the bucket, then an `encoding-type` or a `continuation-token` it cannot read
    after the bucket. The one it leaves is the `max-keys` range: `?max-keys=-1` is the 200 (all
    measured 2026-09-29, beside the GET, on the public bucket and a name nobody owns). A `versionId`
    and a valued `website` are the 400 ahead of a selector's 405, as on real (same date)."""
    q = request.query_params
    selected = _bucket_selected(q, _BUCKET_READ_SELECTORS)
    first = _version_id_refusal(q, selected) or _website_refusal(q)
    refused = _read_refusal(request, selected, _BUCKET_GET_REFUSALS, _BUCKET_GETS, first)
    if refused is not None:
        return refused
    v2 = _first(q, "list-type", None) == _LIST_TYPE_V2
    _, err = _listing_parse(q, v2)
    if err is not None:
        return _head(err.status_code)
    caller, visible, err = _auth(request)
    if err:
        return err
    if not _bucket_visible(auth.conn(request), bucket, visible):
        return _head(404)
    err = _trailer_refusal(request, head=True) or _listing_checks(q, v2)[-1]
    if err is not None:
        return _head(err.status_code)
    return _head(
        200,
        {
            "x-amz-bucket-region": "us-east-1",
            "x-amz-access-point-alias": "false",
            "x-amz-bucket-arn": f"arn:aws:s3:::{bucket}",
        },
    )


@router.get("/{bucket}", openapi_extra={"parameters": _P_BUCKET_GET})
async def bucket_get(request: Request, bucket: str):
    """ListObjects or ListObjectsV2 — one page of the keys in this bucket that the caller can read.

    ``list-type=2`` selects the V2 shape and anything else the V1 one; the two differ in what they
    page with and in the elements they carry (see ``_list_objects``). Both are filtered by
    ``prefix``, rolled up by ``delimiter`` and bounded by ``max-keys``. The bucket's other GETs
    share this path and are selected the same way, by a selector's presence: ``versions`` answers
    ListObjectVersions, ``uploads`` ListMultipartUploads, always the empty page because no upload
    is ever in progress, and each of the rest the configuration it names, as real answers a bucket
    nobody configured (``_bucket_configuration``). A ``versionId`` is refused on every one of them
    and a ``website`` with a value too (``_version_id_refusal``, ``_website_refusal``)."""
    q = request.query_params
    selected = _bucket_selected(q, _BUCKET_READ_SELECTORS)
    max_uploads, max_keys = _MAX_UPLOADS, _MAX_KEYS
    v2 = _first(q, "list-type", None) == _LIST_TYPE_V2
    # The parses come before the credential as well as the bucket: an unsigned request and one
    # signed over a bad secret get the same 400 for `?uploads&max-uploads=abc`, `?max-keys=abc` and
    # `?versions&max-keys=abc` (measured 2026-09-29), and so do the conflict, a bucket's
    # `partNumber` and, after the parses, a `versionId` and a valued `website` (``_read_refusal``).
    # What is refused once the bucket is found comes after the signature.
    first = None
    if selected == ["uploads"]:
        # Before the bucket is looked up: `?uploads&max-uploads=abc` on a bucket that does not exist
        # is the 400, not NoSuchBucket, where `?uploads&max-uploads=-1` on it is NoSuchBucket: the
        # value is parsed here and judged against the range after the lookup (measured). The other
        # parameters are read after it too.
        max_uploads, first = _int32_param(q, "max-uploads", _MAX_UPLOADS)
    elif selected == ["versions"]:
        max_keys, first = _versions_parse(q)
    elif not selected:
        max_keys, first = _listing_parse(q, v2)
    first = first or _version_id_refusal(q, selected) or _website_refusal(q)
    refused = _read_refusal(request, selected, _BUCKET_GET_REFUSALS, _BUCKET_GETS, first)
    if refused is not None:
        return refused
    caller, visible, err = _auth(request)
    if err:
        return err
    conn = auth.conn(request)
    if not _bucket_visible(conn, bucket, visible):
        return _no_such_bucket(bucket)
    trailer = _trailer_refusal(request)
    if trailer is not None:
        return trailer
    if selected == ["torrent"]:
        return _method_refusal(request, _BUCKET_WRITE_SELECTORS["torrent"][0])
    if selected == ["uploadId"]:
        return _error("InvalidRequest", _KEY_REQUIRED_FOR_UPLOAD)
    if selected == ["uploads"]:
        return _list_multipart_uploads(request, bucket, max_uploads)
    if selected == ["versions"]:
        return _list_object_versions(request, conn, bucket, visible, max_keys)
    if selected:
        return _bucket_configuration(request, bucket, selected[0])
    return _list_objects(request, conn, bucket, visible, v2=v2, max_keys=max_keys)


def _listing_parse(q, v2: bool) -> tuple[int, Response | None]:
    """What the listing refuses before the credential and the bucket, with ``max-keys`` as parsed.

    In this order: `max-keys` parses first (`?start-after=x&max-keys=abc` on a V1 request is the
    max-keys refusal, not start-after's), then the other version's parameters are refused. What is
    judged after the bucket is in ``_listing_checks`` and ``_list_objects``, in the order the
    latter's docstring gives.
    """
    max_keys, err = _int32_param(q, "max-keys", _MAX_KEYS)
    if err:
        return max_keys, err
    for name, message in _V1_ONLY if v2 else _V2_ONLY:
        if _first(q, name, None) is not None:
            return max_keys, _error("InvalidArgument", message, extra=_argument_name(name))
    return max_keys, None


def _listing_checks(q, v2: bool) -> tuple[str | None, str | None, tuple | None, Response | None]:
    """The listing's ``encoding-type`` and ``continuation-token``, judged after the bucket lookup,
    as ``(encoding_type, continuation, decoded, refusal)``."""
    encoding_type = _first(q, "encoding-type", None)
    if encoding_type is not None and encoding_type.lower() != "url":
        return (
            encoding_type,
            None,
            None,
            _argument_error(
                "Invalid Encoding Method specified in Request", "encoding-type", encoding_type
            ),
        )
    # What a readable token bounds is at `after, at` in ``_list_objects``.
    continuation = _first(q, "continuation-token", None) if v2 else None
    decoded = None
    if continuation is not None:
        decoded = _decode_token(continuation)
        if decoded is None:
            # Sent and unreadable is its own input, neither absent nor a token. Judged here because
            # real does: `?continuation-token=garbage&encoding-type=bogus` is the encoding-type
            # refusal, `&max-keys=-1` beside it is this one, and a bucket that does not exist is
            # NoSuchBucket for an unreadable token and an empty one both (measured 2026-09-17; the
            # shape is in ``_list_objects``'s docstring).
            return (
                encoding_type,
                continuation,
                None,
                _error(
                    "InvalidArgument",
                    "The continuation token provided is incorrect",
                    extra=_argument_name("continuation-token"),
                ),
            )
    return encoding_type, continuation, decoded, None


def _group_of(key: str, prefix: str, delimiter: str) -> str | None:
    """The CommonPrefixes group ``key`` rolls up into, or ``None`` when it is listed on its own."""
    if not delimiter or not key.startswith(prefix):
        return None
    rest = key[len(prefix) :]
    idx = rest.find(delimiter)
    return prefix + rest[: idx + len(delimiter)] if idx != -1 else None


def _resume_after(marker: str, prefix: str, delimiter: str) -> tuple[str | None, str | None, bool]:
    """The bound a V1 ``marker`` resumes from, and ListObjectVersions' ``key-marker``, as
    ``(after, at, past_the_end)``: inside a group it resumes past the whole group, and anywhere
    else past the key itself (see ``_list_objects``)."""
    group = _group_of(marker, prefix, delimiter)
    if group:
        at = store.key_successor(group)
        # No successor means the group runs to the end of what a key can spell, so nothing
        # sorts after it and this page is empty rather than the whole listing over again.
        return None, at, at is None
    return marker, None, False


def _page(
    conn,
    bucket: str,
    visible,
    prefix: str,
    delimiter: str,
    after: str | None,
    at: str | None,
    past_the_end: bool,
    served: int,
) -> tuple[list, dict, list[tuple[str, str]], bool, str | None]:
    """One page of rows from the bound ``after``/``at``, rolled up by ``delimiter``, as ``(rows,
    by_key, entries, is_truncated, group_successor)``: ``entries`` in key order, ``kind`` ``obj``
    or ``cp``, and ``group_successor`` set when the page ends on a group whose keys run on past it
    (both listings and ListObjectVersions page this way)."""
    # The one SQL query: prefix + keyset (`key > after` / `key >= at`) + ACL all pushed down,
    # walking idx_s3_key(bucket, key) directly in sorted order. Ask for one extra row so IsTruncated
    # is a plain length check (and so we can tell, below, whether a trailing rolled-up group extends
    # past this page) — no separate COUNT(*) query. `served` is what this page may hold: the echo is
    # uncapped but what comes back is not. served=0 is its own case — `rows` is empty after
    # trimming, IsTruncated is false the way real answers it, and nothing below reads the overflow
    # row.
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
    # cut from; the caller writes the body in real's document order, which is not this one.
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
    # V1 and ListObjectVersions need no such split: their cursor IS the last entry, group or key,
    # and a marker naming a group is read back as that same bound (``_resume_after``).
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
    return rows, by_key, entries, is_truncated, group_successor


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
    displaced it. Its ``Contents`` carry the V1 ``Owner`` only under ``fetch-owner=true``, spelled
    exactly so and read off the first ``fetch-owner`` sent: measured 2026-10-03 and 2026-10-05,
    ``true`` added it between ``Size`` and ``StorageClass``, and ``TRUE``, ``True``, ``false``,
    ``bogus``, ``1``, `` true`` and an empty value left it out without a refusal. On V1 the
    parameter changes nothing.

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

    The refusals here come after the bucket lookup, in this order: ``encoding-type``, an
    unreadable ``continuation-token``, the ``max-keys`` range (measured 2026-09-14 and
    2026-09-17). ``encoding-type`` is refused unless it is ``url`` compared without case. Under it
    ``Prefix``, ``Delimiter``, ``StartAfter``, ``Marker``, ``NextMarker``, every ``Key`` and every
    ``CommonPrefixes/Prefix`` come back encoded
    and the continuation tokens do not — each of those measured, since the reference names only
    four ("returns encoded key name values in the following response elements: Delimiter, Prefix,
    Key, and StartAfter", the ListObjectsV2 page) and says nothing about the V1 pair or the tokens.
    A token keeps its ``/``, ``+`` and ``=``.

    A ``continuation-token`` that does not decode is refused, and an empty one the same way: real
    answers both "The continuation token provided is incorrect" under ``ArgumentName``
    ``continuation-token``, with no ``ArgumentValue`` (measured 2026-09-17). What is left is a token
    that decodes to a bound this listing never handed out — real refuses that too, where Backlot
    pages from the bound it spells. Backlot's tokens are derived from the bound rather than issued
    and recorded, so one a caller wrote and one a previous page returned are the same bytes, and
    refusing either refuses both.

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
    encoding_type, continuation, decoded, err = _listing_checks(q, v2)
    if err is not None:
        return err
    err = _range_refusal(max_keys, "maxKeys")
    if err:
        return err
    enc = _url_encode if encoding_type is not None else (lambda v: v)
    prefix = _first(q, "prefix")
    delimiter = _first(q, "delimiter")
    marker = _first(q, "marker") if not v2 else ""
    # `None` for absent and `""` for sent empty, which the echo below has to tell apart the way
    # `continuation` does: real answers `?list-type=2&start-after=` with `<StartAfter></StartAfter>`
    # and sends no element when the parameter is absent (measured 2026-09-17).
    start_after = _first(q, "start-after", None) if v2 else None

    # A continuation-token wins over start-after, exactly like real S3 — start-after only seeds the
    # very first page of a listing. Its mode (exclusive "after" a raw key, vs inclusive "at" a
    # CommonPrefixes-group successor — see _encode_group_token) picks which of list_s3_objects' two
    # independent lower bounds to use. V1 reaches the same two bounds through `marker` alone
    # (``_resume_after``).
    after, at, past_the_end = None, None, False
    if decoded is not None:
        mode, value = decoded
        if mode == "after":
            after = value
        else:
            at = value
    elif start_after:
        after = start_after
    elif marker:
        after, at, past_the_end = _resume_after(marker, prefix, delimiter)
    served = min(max_keys, _MAX_KEYS)
    rows, by_key, entries, is_truncated, group_successor = _page(
        conn, bucket, visible, prefix, delimiter, after, at, past_the_end, served
    )
    last_val = entries[-1][1] if entries else None
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
        # Both echoes turn on whether the parameter was sent, not on whether it has a value: an
        # empty `start-after` is echoed as the empty element, and an empty token was refused above,
        # so `is None` is the only reading with an input behind it.
        if start_after is not None and continuation is None:
            body.append(f"<StartAfter>{escape(enc(start_after))}</StartAfter>")
        if continuation is not None:
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
    # Every `Contents`, then every `CommonPrefixes` — real's document order, not the key order
    # `entries` holds (see this function's docstring).
    fetch_owner = not v2 or _first(q, "fetch-owner") == "true"
    owner = f"<Owner><ID>{_owner_id(request)}</ID></Owner>" if fetch_owner else ""
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


def _int32_param(q, name: str, default: int) -> tuple[int, Response | None]:
    """An integer query parameter, parsed the way real S3 parses ``max-keys`` (measured).

    Absent or empty is the default; a run of digits, with or without a leading ``-``, is read for
    its value, leading zeros and all (``05`` and ``00000000005`` are both 5, twenty zeros and a 5 is
    5, five thousand zeros is 0, ``-0`` is 0) and returned as parsed, uncapped and with the range
    left to ``_range_refusal``: a value that fits an int32 but comes to less than 0 (``-1``,
    ``-2147483648``) is refused there, after the bucket lookup, and anything else — a word, a
    leading space, a value whose digits do not fit an int32 in either direction (``2147483648``,
    ``-2147483649``) — is refused here, before the lookup, with "Provided <name> not an integer or
    within integer range". Not ``int()``, which accepts `` 5`` and ``+5`` and has no ceiling.

    Each parameter takes the same parser because real parses them the same way, which was
    measured on each separately: the listing refuses ``max-keys=abc``, ``max-keys= 5``,
    ``max-keys=2147483648`` and ``max-keys=-2147483649`` with this message and serves ``max-keys=``,
    ``max-keys=05`` and ``max-keys=-0``, as ListMultipartUploads does for its own (2026-09-11 and
    2026-09-14); ListParts refuses ``max-parts=abc``, ListBuckets ``max-buckets=abc``,
    ``max-buckets= 1`` and ``max-buckets=1 `` and serves ``max-buckets=05``, and
    ListObjectAnnotations refuses ``max-annotation-results=abc`` and serves
    ``max-annotation-results=005`` (2026-09-29). Where they differ is the range each is held to
    and how its refusal names the value — see ``_range_refusal``, ``list_buckets`` and
    ``_object_parse``.
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
            f"Provided {name} not an integer or within integer range", name, raw
        )
    return -int(digits) if negative else int(digits), None


def _range_refusal(value: int, name: str) -> Response | None:
    """A parsed ``max-uploads``/``max-keys`` below zero, refused as real refuses it (measured).

    Real judges the range after the bucket lookup and after ``encoding-type`` (the listing's full
    order is in ``_list_objects``'s docstring), and names the value as parsed rather than as sent —
    ``-01`` is reported as ``-1`` (2026-09-11 for ``max-uploads``, 2026-09-14 for ``max-keys``).
    ``name`` is separate from the parse refusal's because the listing spells it differently on
    either side of that split: ``max-keys=abc`` is refused for ``max-keys`` and ``max-keys=-1`` for
    ``maxKeys``, on both forms of the listing, where ListMultipartUploads spells ``max-uploads`` in
    both of its messages (measured)."""
    if value >= 0:
        return None
    return _argument_error(
        f"Argument {name} must be an integer between 0 and {_INT32_MAX}", name, str(value)
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
    encoding_type = _first(q, "encoding-type", None)
    if encoding_type is not None and encoding_type.lower() != "url":
        return _argument_error(
            "Invalid Encoding Method specified in Request", "encoding-type", encoding_type
        )
    err = _range_refusal(max_uploads, "max-uploads")
    if err:
        return err
    key_marker = _first(q, "key-marker")
    upload_id_marker = _first(q, "upload-id-marker")
    if key_marker and upload_id_marker:
        return _argument_error("Invalid uploadId marker", "upload-id-marker", upload_id_marker)
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


def _versions_parse(q) -> tuple[int, Response | None]:
    """What ListObjectVersions refuses before the credential and the bucket, with ``max-keys`` as
    parsed: ``max-keys`` as the listing parses it, then a ``version-id-marker`` without a
    ``key-marker`` and an empty one beside it, each naming the marker as sent (measured 2026-09-29,
    on a bucket nobody owns, unsigned and over a bad secret). A ``key-marker`` sent empty is a
    ``key-marker``: ``?versions&key-marker=&version-id-marker=null`` is served."""
    max_keys, err = _int32_param(q, "max-keys", _MAX_KEYS)
    if err:
        return max_keys, err
    marker = _first(q, "version-id-marker", None)
    if marker is not None and _first(q, "key-marker", None) is None:
        message = "A version-id marker cannot be specified without a key marker."
        return max_keys, _argument_error(message, "version-id-marker", marker)
    if marker == "":
        return max_keys, _argument_error(
            "A version-id marker cannot be empty.", "version-id-marker", ""
        )
    return max_keys, None


def _list_object_versions(request: Request, conn, bucket: str, visible, max_keys: int) -> Response:
    """ListObjectVersions — the bucket's keys as their versions, one each, since no bucket here is
    versioned: every one is the version real names `null` and the latest.

    The shape is real's, measured 2026-09-29 on two buckets this account created that day, one of
    four keys and one of three: ``Name``, ``Prefix``, ``KeyMarker`` and ``VersionIdMarker`` always,
    echoing what was sent; ``NextKeyMarker`` whenever the page is truncated, the last entry by key
    whether a key or a group, and ``NextVersionIdMarker`` beside it, `null`, unless that entry is a
    group; ``MaxKeys`` as parsed, uncapped; ``Delimiter`` whenever one was sent, an empty one
    included; ``EncodingType`` as sent; ``IsTruncated``; each ``Version`` and then each
    ``CommonPrefixes``. A ``Version`` carries the listing's fields with ``VersionId`` and
    ``IsLatest`` after the key and the ``Owner`` a V1 listing carries. Paging is V1's:
    ``key-marker`` resumes past the key, or past the whole group holding it (``_resume_after``), and
    following the markers walked the four keys a page each and a delimited listing past its group.
    `version-id-marker` can only be `null`, which resumes where ``key-marker`` alone does; beside an
    empty ``key-marker`` real served a page of nothing.

    What is refused before the bucket is in ``_versions_parse``. After it, in this order: a
    ``version-id-marker`` that is not `null` ("Invalid version id specified", `NULL` included), an
    ``encoding-type`` that is not ``url`` compared without case, a ``max-keys`` below zero
    ("max-keys cannot be negative", with no ``ArgumentValue``). Under ``encoding-type=url`` every
    key and group, ``Prefix``, ``Delimiter``, ``KeyMarker`` and ``NextKeyMarker`` come back encoded.
    """
    q = request.query_params
    marker = _first(q, "version-id-marker", None)
    if marker is not None and marker != "null":
        return _argument_error("Invalid version id specified", "version-id-marker", marker)
    encoding_type = _first(q, "encoding-type", None)
    if encoding_type is not None and encoding_type.lower() != "url":
        return _argument_error(
            "Invalid Encoding Method specified in Request", "encoding-type", encoding_type
        )
    if max_keys < 0:
        return _error(
            "InvalidArgument", "max-keys cannot be negative", extra=_argument_name("max-keys")
        )
    enc = _url_encode if encoding_type is not None else (lambda v: v)
    prefix = _first(q, "prefix")
    delimiter = _first(q, "delimiter", None)
    key_marker = _first(q, "key-marker")
    if marker is not None and key_marker == "":
        after, at, past_the_end = None, None, True
    elif key_marker:
        after, at, past_the_end = _resume_after(key_marker, prefix, delimiter or "")
    else:
        after, at, past_the_end = None, None, False
    _, by_key, entries, is_truncated, _ = _page(
        conn,
        bucket,
        visible,
        prefix,
        delimiter or "",
        after,
        at,
        past_the_end,
        min(max_keys, _MAX_KEYS),
    )
    body = [
        f'<ListVersionsResult xmlns="{NS}"><Name>{escape(bucket)}</Name>',
        f"<Prefix>{escape(enc(prefix))}</Prefix><KeyMarker>{escape(enc(key_marker))}</KeyMarker>",
        f"<VersionIdMarker>{escape(marker or '')}</VersionIdMarker>",
    ]
    if is_truncated and entries:
        kind, last = entries[-1]
        body.append(f"<NextKeyMarker>{escape(enc(last))}</NextKeyMarker>")
        if kind == "obj":
            body.append("<NextVersionIdMarker>null</NextVersionIdMarker>")
    body.append(f"<MaxKeys>{max_keys}</MaxKeys>")
    if delimiter is not None:
        body.append(f"<Delimiter>{escape(enc(delimiter))}</Delimiter>")
    if encoding_type is not None:
        body.append(f"<EncodingType>{escape(encoding_type)}</EncodingType>")
    body.append(f"<IsTruncated>{'true' if is_truncated else 'false'}</IsTruncated>")
    owner = f"<Owner><ID>{_owner_id(request)}</ID></Owner>"
    for val in [v for kind, v in entries if kind == "obj"]:
        r = by_key[val]
        body.append(
            f"<Version><Key>{escape(enc(val))}</Key><VersionId>null</VersionId>"
            f"<IsLatest>true</IsLatest>"
            f"<LastModified>{synth.s3_iso(r['updated_ts'] or r['created_ts'])}</LastModified>"
            f"<ETag>{escape(synth.s3_etag(r['key'], r['content']))}</ETag>"
            f"<Size>{len(r['content'].encode())}</Size>{owner}"
            f"<StorageClass>{escape(r['subtype'] or 'STANDARD')}</StorageClass></Version>"
        )
    for val in [v for kind, v in entries if kind == "cp"]:
        body.append(f"<CommonPrefixes><Prefix>{escape(enc(val))}</Prefix></CommonPrefixes>")
    body.append("</ListVersionsResult>")
    return _xml("".join(body))


def _bucket_configuration(request: Request, bucket: str, selector: str) -> Response:
    """A bucket's own configuration, the one ``selector`` names, as real answers it for a bucket
    nobody configured (``_ANSWERED``, ``_UNCONFIGURED``, ``_CONFIGURATION_LISTS``), at the
    bucket's path and at a key's alike (``_KEY_BUCKET_SELECTORS``). ``?acl`` grants the owner
    ``FULL_CONTROL`` and nothing else, naming it by the id a V1 listing names it by."""
    if selector in _CONFIGURATION_LISTS:
        return _configuration_list(request.query_params, selector)
    if selector in _UNCONFIGURED:
        code, message = _UNCONFIGURED[selector]
        extra = (
            "<Method>GET</Method>"
            if code == "V1APIsNotAllowed"
            else f"<BucketName>{escape(bucket)}</BucketName>"
        )
        prolog = "" if code in _NO_PROLOG else _DECLARATION + "\n"
        return _error(code, message, extra, prolog=prolog)
    if selector == "logging":
        return _xml(_LOGGING, prolog=_DECLARATION + "\n\n")
    if selector == "acl":
        return _xml(_access_control_policy(request))
    body, media_type = _ANSWERED[selector]
    return _xml(body, media_type=media_type)


def _access_control_policy(request: Request) -> str:
    """The owner's `FULL_CONTROL` and nothing else, which real answered a bucket's `?acl` and an
    object's with on a bucket whose objects its owner owns (`BucketOwnerEnforced`, 2026-09-29)."""
    owner = _owner_id(request)
    grantee = (
        '<Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:type="CanonicalUser"><ID>{owner}</ID></Grantee>'
    )
    return (
        f'<AccessControlPolicy xmlns="{NS}"><Owner><ID>{owner}</ID></Owner>'
        f"<AccessControlList><Grant>{grantee}<Permission>FULL_CONTROL</Permission></Grant>"
        "</AccessControlList></AccessControlPolicy>"
    )


def _configuration_list(q, selector: str) -> Response:
    """One of the four configuration lists, empty: an ``id`` naming one is ``NoSuchConfiguration``
    and a ``continuation-token`` a list never handed out ``MalformedContinuationToken``, in that
    order, an empty ``id`` read as none and an empty token echoed (measured 2026-09-29)."""
    root, prolog, refusal_prolog = _CONFIGURATION_LISTS[selector]
    if _first(q, "id"):
        return _error(
            "NoSuchConfiguration",
            "The specified configuration does not exist.",
            prolog=refusal_prolog,
        )
    token = _first(q, "continuation-token", None)
    if token:
        return _error(
            "MalformedContinuationToken",
            "The continuation-token you provided invalid.",
            prolog=refusal_prolog,
        )
    echo = "<ContinuationToken></ContinuationToken>" if token is not None else ""
    return _xml(
        f'<{root} xmlns="{NS}"><IsTruncated>false</IsTruncated>{echo}</{root}>',
        prolog=prolog,
        media_type=None,
    )


@router.api_route("/{bucket}/{key:path}", methods=["GET", "HEAD"])
async def object_get(request: Request, bucket: str, key: str):
    """GetObject — the object's bytes, or its metadata headers alone for a HEAD.

    ``key`` is a whole path, slashes and all. A ``Range`` header returns 206 over that slice. The
    bucket is looked up before the key, as real looks them up: a key in a bucket that does not exist
    is NoSuchBucket, with ``?acl`` and ``?uploadId`` as without them (measured 2026-09-29), and so
    is one in a bucket the caller cannot see. An object the caller cannot read is NoSuchKey, not
    AccessDenied, so a listing and a read agree about what exists.

    In checksum mode (``_checksum_mode``) an answer holding the whole object carries its CRC-64/NVME
    (``_crc64nvme``) and a range short of it none, and another mode is real's 400: after the
    `versionId`, the part's refusals and a range or a part the object does not have, and on a GET,
    not a HEAD, ahead of a key the bucket does not hold. A sub-resource reads no mode. Real's front
    ends disagree on a bad `versionId` beside a bad mode or a bad part: 11 of 16 requests of each
    pair refused the version first (all measured 2026-09-30)."""
    head = request.method == "HEAD"
    q = request.query_params
    selected = _key_selected(q)
    if selected == ["uploadId"] and "partNumber" in q:
        # UploadPart, which real refuses a GET and a HEAD of with the 405 naming `PART`, before the
        # bucket (measured 2026-09-29).
        selected = ["PART"]
    first = _website_refusal(q)
    if selected == ["annotationName"]:
        first = _argument_error("Unexpected query string parameter", "ResourceType", selected[0])
    first = first or _object_parse(request, selected, head)
    # `_OBJECT_GETS`, not `_BUCKET_GETS`: on a GET a key's path serves only the bucket selectors
    # `_KEY_BUCKET_SELECTORS` names, `?logging` and `?versions` apart, so its HEAD refusal names no
    # method for the others.
    refused = _read_refusal(request, selected, _OBJECT_GET_REFUSALS, _OBJECT_GETS, first)
    if refused is not None:
        return refused
    if selected == ["uploadId"]:
        # ListParts' own integers parse before the credential and the bucket, as the listing's do:
        # `?uploadId=x&max-parts=abc` is the 400 in a bucket that does not exist, unsigned and over
        # a bad secret, where `max-parts=-1` is left to the upload (measured 2026-09-29).
        for name in ("max-parts", "part-number-marker"):
            _, err = _int32_param(q, name, 0)
            if err:
                return err
    caller, visible, err = _auth(request)
    if err:
        return err
    conn = auth.conn(request)
    if not _bucket_visible(conn, bucket, visible):
        return _head(404) if head else _no_such_bucket(bucket)
    trailer = _trailer_refusal(request, head)
    if trailer is not None:
        return trailer
    if selected and selected[0] in _KEY_BUCKET_SELECTORS:
        # The bucket's own operation, whatever the key (`_KEY_BUCKET_SELECTORS`).
        if selected[0] in _KEY_BUCKET_REFUSALS:
            return _error(*_KEY_BUCKET_REFUSALS[selected[0]])
        return _bucket_configuration(request, bucket, selected[0])
    if selected == ["uploads"]:
        return _error("InvalidRequest", _UPLOADS_ON_A_KEY)
    if selected == ["uploadId"]:
        # ListParts is about an upload, not about the object under the key, and no upload is ever
        # in progress here: real answers `NoSuchUpload` naming the id for a key that exists and for
        # one that does not, an empty id included (measured 2026-09-29), so the answer comes
        # before the key is looked up.
        return _error(
            "NoSuchUpload",
            "The specified upload does not exist. The upload ID may be invalid, or the upload may "
            "have been aborted or completed.",
            f"<UploadId>{escape(_first(q, 'uploadId'))}</UploadId>",
        )
    if selected == ["torrent"]:
        return _method_refusal(request, _OBJECT_WRITE_SELECTORS["torrent"][0])
    version = _first(q, "versionId", None)
    if version is not None and version != "null":
        return _head(400) if head else _argument_error(_BAD_VERSION, "versionId", version)
    part, number = _first(q, "partNumber", None), None
    if part is not None:
        refused = _part_refusal(request, part)
        if refused is not None:
            return _head(refused.status_code) if head else refused
        number = int(part.lstrip("0"))
    if selected in (["legal-hold"], ["retention"]):
        return _error("InvalidRequest", "Bucket is missing Object Lock Configuration")
    if selected == ["attributes"] and not _attribute_names(
        request.headers.get("x-amz-object-attributes")
    ):
        return _error("InvalidRequest", _NO_ATTRIBUTES, prolog="")
    if selected == ["annotation"] and _first(q, "annotationName", None) == "":
        return _error(
            "UserAnnotationNameMustBeSpecified", "You must specify a user annotation name."
        )
    row = _object_row(conn, bucket, key, visible)
    if row is None:
        if head:
            return _head(404)
        if not selected and _checksum_mode(request) is None:
            return _error("InvalidRequest", _BAD_CHECKSUM_MODE)
        # A key's `?tagging` names what is missing with its bucket, the rest the key alone, and
        # `?attributes` sends no declaration; `versionId=null` of a key there is none of is the
        # version that is missing (measured 2026-09-29).
        missing = escape(f"{bucket}/{key}" if selected == ["tagging"] else key)
        prolog = "" if selected == ["attributes"] else _DECLARATION + "\n"
        if version is not None:
            return _error(
                "NoSuchVersion",
                "The specified version does not exist.",
                f"<Key>{missing}</Key><VersionId>null</VersionId>",
                prolog=prolog,
            )
        return _error(
            "NoSuchKey", "The specified key does not exist.", f"<Key>{missing}</Key>", prolog=prolog
        )
    if selected:
        return _object_subresource(request, bucket, key, row, selected[0])

    data = row["content"].encode("utf-8")
    total = len(data)
    if number is not None and number > 1:
        # Every object here is one part. The part is named as parsed, `02` as `2` (2026-09-29).
        if head:
            return _head(416)
        return _error(
            "InvalidPartNumber",
            "The requested partnumber is not satisfiable",
            f"<PartNumberRequested>{number}</PartNumberRequested>"
            "<ActualPartCount>1</ActualPartCount>",
        )
    ts = row["updated_ts"] or row["created_ts"]
    headers = {
        "ETag": synth.s3_etag(row["key"], row["content"]),
        "Last-Modified": synth.s3_http_date(ts),
        "Accept-Ranges": "bytes",
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
            # No `Content-Range` beside it, on a GET or a HEAD (measured 2026-09-29): real names
            # the size in the body alone.
            if head:
                return _head(416)
            return _error(
                "InvalidRange",
                "The requested range is not satisfiable",
                f"<RangeRequested>{escape(rng)}</RangeRequested>"
                f"<ActualObjectSize>{total}</ActualObjectSize>",
            )
        start, end = parsed
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"

    if number is not None:
        # The one part is the whole object, sent as the 206 of its range (2026-09-29).
        status = 206
        headers["Content-Range"] = f"bytes 0-{total - 1}/{total}"
    mode = _checksum_mode(request)
    if mode is None:
        return _head(400) if head else _error("InvalidRequest", _BAD_CHECKSUM_MODE)
    if mode and (start, end) == (0, total - 1):
        headers["x-amz-checksum-crc64nvme"] = _crc64nvme(data)
        headers["x-amz-checksum-type"] = "FULL_OBJECT"

    length = end - start + 1
    headers["Content-Length"] = str(length)
    if head:
        return Response(status_code=status, headers=headers)
    return Response(content=data[start : end + 1], status_code=status, headers=headers)


_BAD_VERSION = "Invalid version id specified"
_BAD_CHECKSUM_MODE = "Value for x-amz-checksum-mode header is invalid."
_MAX_PARTS = 10000
_NO_ATTRIBUTES = (
    "The x-amz-object-attributes header specifying the attributes to be retrieved is either "
    "missing or empty"
)
# The names `x-amz-object-attributes` takes, in the order GetObjectAttributes answers them whatever
# order they were asked in (2026-09-29).
_OBJECT_ATTRIBUTES = ("ETag", "Checksum", "ObjectParts", "StorageClass", "ObjectSize")
# A key's selectors whose operation takes no `versionId`: real refused one beside each of them,
# `null` included, before the bucket and the credential, where `?acl`, `?tagging`, `?attributes`
# and `?legal-hold` read it as a version after them (2026-09-29 and 2026-09-30).
_KEY_VERSIONLESS = _KEY_BUCKET_SELECTORS | {"torrent", "uploadId", "uploads"}
_MAX_ANNOTATIONS = 1000


def _part_refusal(request: Request, part: str) -> Response | None:
    """GetObject's `partNumber`, which real refuses after the credential and the bucket and before
    the key, a HEAD as its status alone: beside a `Range` header, an empty or unreadable one
    included, and then anything but a run of digits whose value is 1 to 10000, leading zeros and
    all, named as sent (`00`, ` 1`, `1.0`, an empty value); a `versionId` it cannot read comes
    first (measured 2026-09-29 on an object this account wrote)."""
    if "range" in request.headers:
        return _error(
            "InvalidRequest", "Cannot specify both Range header and partNumber query parameter"
        )
    digits = part.lstrip("0")
    if (
        not re.fullmatch(r"[0-9]+", part)
        or not digits
        or len(digits) > 5
        or int(digits) > _MAX_PARTS
    ):
        return _argument_error(
            f"Part number must be an integer between 1 and {_MAX_PARTS}, inclusive",
            "partNumber",
            part,
        )
    return None


def _object_parse(request: Request, selected: list[str], head: bool) -> Response | None:
    """What a GET at a key refuses before the credential and the bucket, as real refused it,
    unsigned, over a bad secret and in a bucket nobody owns (measured 2026-09-29 and 2026-09-30):
    a `versionId`, `null` included, beside a selector whose operation takes none
    (``_KEY_VERSIONLESS``), "This operation does not accept a version-id."; an empty `versionId`,
    "Version id cannot be the empty string"; an `x-amz-object-attributes` naming an attribute there
    is none of, read as ``_attribute_names`` reads it and naming the first such name; and on a GET
    ListObjectAnnotations' `max-annotation-results`, parsed as `max-keys` is and then held to 1 to
    1000, naming it as sent. A HEAD is refused each of the others as its status alone, where
    `?annotation&max-annotation-results=abc` is the selector's 405."""
    q = request.query_params
    version = _first(q, "versionId", None)
    if version is not None and selected and selected[0] in _KEY_VERSIONLESS:
        return _argument_error(_NO_VERSION_ID, "versionId", version)
    if version == "":
        return _argument_error("Version id cannot be the empty string", "versionId", "")
    sent = request.headers.get("x-amz-object-attributes")
    if selected == ["attributes"] and sent is not None:
        for name in _attribute_names(sent):
            if name not in _OBJECT_ATTRIBUTES:
                return _argument_error(
                    "Invalid attribute name specified.", "x-amz-object-attributes", name
                )
    if not head and selected == ["annotation"] and _first(q, "annotationName", None) is None:
        count, err = _int32_param(q, "max-annotation-results", _MAX_ANNOTATIONS)
        if err:
            return err
        if not 1 <= count <= _MAX_ANNOTATIONS:
            return _argument_error(
                f"Argument max-annotation-results must be an integer between 1 and "
                f"{_MAX_ANNOTATIONS}",
                "max-annotation-results",
                _first(q, "max-annotation-results"),
            )
    return None


def _attribute_names(sent: str | None) -> list[str]:
    """`x-amz-object-attributes` as real reads it: split at each comma, the empty pieces at the end
    dropped and the rest trimmed, so that `ETag,` and `ETag,,` are `ETag` and `,` is no name at
    all, the missing header's 400 after the bucket, where `,ETag`, `ETag,,ObjectSize` and
    `ETag, ,ObjectSize` name an empty one and an empty header is one empty name (measured
    2026-09-29 and 2026-09-30)."""
    if sent is None:
        return []
    if sent == "":
        return [""]
    pieces = sent.split(",")
    while pieces and pieces[-1] == "":
        pieces.pop()
    return [piece.strip() for piece in pieces]


def _checksum_mode(request: Request) -> bool | None:
    """Whether a GET or a HEAD of an object asks for its checksum: `x-amz-checksum-mode: ENABLED`,
    compared without case, is `True`, no header `False`, and any other value, an empty one
    included, `None`, which real refused (measured 2026-09-30)."""
    sent = request.headers.get("x-amz-checksum-mode")
    if sent is None:
        return False
    return True if sent.strip().lower() == "enabled" else None


def _crc64nvme(data: bytes) -> str:
    """The CRC-64/NVME real reports for an object written with no checksum of its own, as the
    `Checksum` of `?attributes` and the `x-amz-checksum-crc64nvme` of a GET or a HEAD in checksum
    mode, `FULL_OBJECT` its type (measured 2026-09-30: real's value for the bundled corpus's
    CHANGELOG.md was this one)."""
    return base64.b64encode(_checksum("crc64nvme", data)).decode()


def _object_subresource(request: Request, bucket: str, key: str, row, selector: str) -> Response:
    """An object's own sub-resource, as real answered it for an object written with no tags and no
    checksum header in a bucket with no Object Lock (2026-09-29 and 2026-09-30, on this account's
    objects): `?acl` the owner's grant, `?tagging` an empty `TagSet`, `?attributes` the attributes
    asked for that the object has, in real's order, its ETag unquoted, its `Checksum` the
    CRC-64/NVME real computed for an object sent without one (``_crc64nvme``) and its
    `Last-Modified` beside it (no `ObjectParts`, which a single part is none of), and `?annotation`
    the empty list or, with an `annotationName`, `NoSuchAnnotation`. The `?tagging`, `?attributes`
    and `?annotation` 200s carry no `Content-Type`, as real's did."""
    q = request.query_params
    if selector == "acl":
        return _xml(_access_control_policy(request))
    if selector == "tagging":
        return _xml(f'<Tagging xmlns="{NS}"><TagSet/></Tagging>', media_type=None)
    if selector == "attributes":
        asked = set(_attribute_names(request.headers.get("x-amz-object-attributes")))
        data = row["content"].encode("utf-8")
        elements = {
            "ETag": escape(synth.s3_etag(row["key"], row["content"]).strip('"')),
            "Checksum": f"<ChecksumCRC64NVME>{_crc64nvme(data)}</ChecksumCRC64NVME>"
            "<ChecksumType>FULL_OBJECT</ChecksumType>",
            "StorageClass": escape(row["subtype"] or "STANDARD"),
            "ObjectSize": str(len(data)),
        }
        inner = "".join(
            f"<{name}>{elements[name]}</{name}>"
            for name in _OBJECT_ATTRIBUTES
            if name in asked and name in elements
        )
        root = "GetObjectAttributesResponse"
        body = f'<{root} xmlns="{NS}">{inner}</{root}>' if inner else f'<{root} xmlns="{NS}"/>'
        ts = row["updated_ts"] or row["created_ts"]
        return _xml(body, media_type=None, headers={"Last-Modified": synth.s3_http_date(ts)})
    name = _first(q, "annotationName", None)
    if name is not None:
        return _error(
            "NoSuchAnnotation",
            "The specified annotation does not exist.",
            f"<AnnotationName>{escape(name)}</AnnotationName>",
        )
    if "continuation-token" in q:
        # After the key, where the count's refusals come before the bucket (same date): there are
        # never any annotations, so no token names a page of them, an empty one included.
        return _error(
            "InvalidArgument",
            "The continuation token provided is incorrect",
            extra=_argument_name("continuation-token"),
        )
    echo = ""
    prefix = _first(q, "annotation-prefix", None)
    if prefix is not None:
        echo += f"<AnnotationPrefix>{escape(prefix)}</AnnotationPrefix>"
    if "max-annotation-results" in q:
        count, _ = _int32_param(q, "max-annotation-results", _MAX_ANNOTATIONS)
        echo += f"<MaxAnnotationResults>{count}</MaxAnnotationResults>"
    root = "ListObjectAnnotationsOutput"
    return _xml(
        f'<{root} xmlns="{NS}"><Annotations/><Bucket>{escape(bucket)}</Bucket>'
        f"<Key>{escape(key)}</Key>{echo}<AnnotationCount>0</AnnotationCount></{root}>",
        media_type=None,
    )


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


# --- methods this router does not serve -----------------------------------------------------
#
# Real answers each method its own way, so each is declared here and answered with the error real
# sends. Measured 2026-09-23 against `s3.us-east-1.amazonaws.com`, the region this server presents
# (`x-amz-bucket-region`, the empty `LocationConstraint`), path-style, signed and unsigned, against
# a bucket name nobody owns:
#
#   request                          real
#   ---------------------------------|---------------------------------------------------------
#   PATCH, POST on a key             | 405 MethodNotAllowed, `<Method>`, `<ResourceType>OBJECT`
#   PATCH on a bucket                | 405 MethodNotAllowed, `<ResourceType>BUCKET`
#   any of the four at the root      | 405 MethodNotAllowed, `<ResourceType>SERVICE`, `Allow: GET`
#   HEAD at the root                 | 405, `Allow: GET`, an empty `application/xml` body
#   POST on a bucket                 | 412 PreconditionFailed, `<Condition>` naming
#                                    | multipart/form-data
#   a selector, a method it lacks    | 405 MethodNotAllowed naming the selector's own resource type
#   two selectors                    | 400 InvalidArgument, the conflict a GET gets
#   OPTIONS, no `Origin`             | 400 BadRequest, "Insufficient information..."
#   OPTIONS with an `Origin`         | 403 AccessForbidden, the CORS message for that path
#   PUT on a bucket, no selector     | CreateBucket: 409 BucketAlreadyExists, or 200 and the bucket
#   any other write                  | 404 NoSuchBucket for an absent bucket, else the write itself
#   a method S3 defines nothing for  | 400 BadRequest, "An error occurred when parsing the HTTP
#                                    | request." — `backlot.errors.s3` answers those, since no
#                                    | route here can be declared for a method that is not named
#
# The methods real answers by doing the write are the ones this server does not serve, so they
# answer `NotImplemented` (501), once what real checks of four of them first finds nothing to refuse
# (``_WRITE_CHECKS``, ``_KEY_WRITE_CHECKS``, ``_put_object_refusal``). The rest are real's own
# answers. Three more deliberate differences, each stated where it is made: the `Allow` names what
# Backlot serves rather than what real serves, which is what the sub-resource 405 already does; a
# multipart `POST` on a bucket is refused as a non-multipart one is, since an upload is a write; and
# a preflight names every bucket path as present (`_cors_preflight`).
#
# An unsigned request gets each method refusal a signed one gets, because real answers the method
# first: an unsigned `PATCH` on a bucket, on a key and at the root, an unsigned `HEAD` at the root,
# and an unsigned `PATCH` or `POST` naming a selector that lacks it, answered the same 405 as a
# signed one, and an unsigned `OPTIONS` the same 400 and 403. A signature that is sent and does not
# verify is refused first, ahead of each 405 (`_method_refusal`), and after the 412, both `OPTIONS`
# answers and the conflict, which answered a bad secret and an unknown access key as they answer a
# signed request (measured 2026-09-29). A write resolves the credential and the bucket before its
# 501 (`_refuse_write`).

_METHOD_NOT_ALLOWED = "The specified method is not allowed against this resource."
_CORS_NEEDS_ORIGIN = "Insufficient information. Origin request header needed."
_CORS_DISABLED = "CORSResponse: CORS is not enabled for this bucket."
_CORS_NO_BUCKET = "CORSResponse: Bucket not found"
_WRITE_IS_NOT_SERVED = (
    "A method you provided writes to the corpus, which this server does not implement: "
)

# What real answers a sub-resource selector with on the three methods a write can take: the resource
# type its 405 names, and the methods that are an operation there. Measured 2026-09-23 as above,
# every selector below on each of `PUT`, `POST`, `DELETE` and `PATCH`, 156 requests: a method in the
# set answered `NoSuchBucket` for the absent bucket, which is the write resolving it, and every
# other method the 405 naming the type, before the bucket, and with an `Allow` that is exactly the
# set plus `GET` where the selector has a GET form. A bucket's `restore` and a key's `delete` and
# `encryption` were measured 2026-09-29 the same way, at the absent name and the public bucket, 23
# requests, and answered by the same rule, and so were a bucket's three `metadata*Table` selectors,
# which only a `PUT` takes, on each method at the absent name and on a GET and a HEAD at a bucket
# this account created (same date). A key's `tagging` is a different type from a bucket's, so the
# two paths keep their own tables. The selectors here with no GET form, a bucket's `torrent` and
# `uploadId` and a key's `uploads` apart, are the ones a GET is refused for (`_BUCKET_READ_REFUSED`,
# `_OBJECT_READ_REFUSED`); `session` and `renameObject` answered as the bare path does and are left
# out.
_BUCKET_WRITE_SELECTORS: dict[str, tuple[str, frozenset[str]]] = {
    "abac": ("BUCKET_ABAC", frozenset({"PUT"})),
    "accelerate": ("ACCELERATE", frozenset({"PUT"})),
    "acl": ("ACL", frozenset({"PUT"})),
    "analytics": ("ANALYTICS", frozenset({"DELETE", "PUT"})),
    "cors": ("CORS", frozenset({"DELETE", "PUT"})),
    "delete": ("MULTI_OBJECT_DELETE", frozenset({"POST"})),
    "encryption": ("ENCRYPTION", frozenset({"DELETE", "PUT"})),
    "intelligent-tiering": ("INTELLIGENT_TIERING", frozenset({"DELETE", "PUT"})),
    "inventory": ("INVENTORY", frozenset({"DELETE", "PUT"})),
    "lifecycle": ("LIFECYCLE", frozenset({"DELETE", "PUT"})),
    "location": ("LOCATION", frozenset()),
    "logging": ("LOGGING_STATUS", frozenset({"PUT"})),
    "metadataAnnotationTable": (
        "BUCKET_METADATA_ANNOTATION_TABLE_CONFIGURATION",
        frozenset({"PUT"}),
    ),
    "metadataConfiguration": ("BUCKET_METADATA_CONFIGURATION", frozenset({"DELETE", "POST"})),
    "metadataInventoryTable": ("BUCKET_METADATA_INVENTORY_TABLE_CONFIGURATION", frozenset({"PUT"})),
    "metadataJournalTable": ("BUCKET_METADATA_JOURNAL_TABLE_CONFIGURATION", frozenset({"PUT"})),
    "metadataTable": ("BUCKET_METADATA_TABLE_CONFIGURATION", frozenset({"DELETE", "POST"})),
    "metrics": ("METRICS", frozenset({"DELETE", "PUT"})),
    "notification": ("NOTIFICATION", frozenset({"PUT"})),
    "object-lock": ("OBJECT_LOCK_CONFIGURATION", frozenset({"PUT"})),
    "ownershipControls": ("OWNERSHIP_CONTROLS", frozenset({"DELETE", "PUT"})),
    "policy": ("BUCKETPOLICY", frozenset({"DELETE", "PUT"})),
    "policyStatus": ("POLICY_STATUS", frozenset()),
    "publicAccessBlock": ("PUBLIC_ACCESS_BLOCK", frozenset({"DELETE", "PUT"})),
    "replication": ("REPLICATION", frozenset({"DELETE", "PUT"})),
    "requestPayment": ("REQUEST_PAYMENT", frozenset({"PUT"})),
    "restore": ("RESTORE", frozenset({"POST"})),
    "tagging": ("TAGGING", frozenset({"DELETE", "PUT"})),
    # Two of an object's, which a bucket's path answers too: each write method on 2026-09-29, 14
    # requests at the absent name and the public bucket (`_BUCKET_OBJECT_SELECTORS`).
    "torrent": ("TORRENT", frozenset()),
    "uploadId": ("UPLOAD", frozenset({"DELETE", "POST"})),
    "uploads": ("UPLOADS", frozenset({"POST"})),
    "versioning": ("VERSIONING", frozenset({"PUT"})),
    "versions": ("BUCKETVERSIONS", frozenset()),
    "website": ("WEBSITE", frozenset({"DELETE", "PUT"})),
}
_OBJECT_WRITE_SELECTORS: dict[str, tuple[str, frozenset[str]]] = {
    "acl": ("ACL", frozenset({"PUT"})),
    "annotation": ("OBJECT_ANNOTATIONS", frozenset()),
    "attributes": ("OBJECT_ATTRIBUTES", frozenset()),
    "delete": ("MULTI_OBJECT_DELETE", frozenset({"POST"})),
    "encryption": ("OBJECT_ENCRYPTION", frozenset({"PUT"})),
    "legal-hold": ("OBJECT_LOCK_LEGALHOLD", frozenset({"PUT"})),
    "restore": ("RESTORE", frozenset({"POST"})),
    "retention": ("OBJECT_LOCK_RETENTION", frozenset({"PUT"})),
    "select": ("SELECT", frozenset({"POST"})),
    "tagging": ("OBJECT_TAGGING", frozenset({"DELETE", "PUT"})),
    "torrent": ("TORRENT", frozenset()),
    "uploadId": ("UPLOAD", frozenset({"DELETE", "POST"})),
    "uploads": ("UPLOADS", frozenset({"POST"})),
}
# `uploadId` beside a `partNumber` is UploadPart, a type of its own that only `PUT` is (same date:
# `PATCH ?partNumber=1&uploadId=…` is the 405 naming `PART` with `Allow: PUT`).
_UPLOAD_PART = ("PART", frozenset({"PUT"}))

# The selectors a GET and a HEAD naming one are refused for with the 405 naming its type, before the
# bucket is looked up: those whose operations are all on the methods above, which is every selector
# of a write table outside `_BUCKET_SELECTORS` and `_OBJECT_SELECTORS`, a key's `uploads` and a
# bucket's `torrent` and `uploadId` apart. Measured 2026-09-29 against `s3.us-east-1.amazonaws.com`:
# the selectors of both tables but the three `metadata*Table` ones, and `partNumber`,
# `renameObject`, `select-type` and `session`, 40 in all, each on a GET and a HEAD at a bucket
# nobody owns and at a key in it, 160 requests. These are the ones a GET answered with a 405, which
# named the type and carried an `Allow` of the write methods, and a HEAD answered each with the 405
# and the same `Allow`; the public bucket `noaa-ghcn-pds` and one of its objects answered them the
# same, and so did a bucket's three `metadata*Table` selectors in a later sweep of every selector at
# both paths (same date). The `Allow` is not repeated, since this server serves no method at these
# selectors, which is the line `_head_refusal` draws.
_BUCKET_READ_REFUSED = {
    selector: resource_type
    for selector, (resource_type, _) in _BUCKET_WRITE_SELECTORS.items()
    if selector not in _BUCKET_SELECTORS | _BUCKET_OBJECT_SELECTORS
}
_OBJECT_READ_REFUSED = {
    selector: resource_type
    for selector, (resource_type, _) in _OBJECT_WRITE_SELECTORS.items()
    if selector not in _OBJECT_SELECTORS | {"uploads"}
}
# A key's `uploads` is a `POST` too, but what a GET naming it gets is a 400 of its own, and after
# the bucket is looked up: `NoSuchBucket` for the absent bucket, this for the public one's object
# and for a key it does not have (same date). A HEAD naming it is the 405 the others are.
_UPLOADS_ON_A_KEY = "Key is not expected for the GET method ?uploads subresource"
# What a GET and a HEAD at each path read as a selector, and so what a pair of them conflicts
# over: `?acl&delete`, `?restore&location` and a key's `?uploads&acl` are each the conflict, and a
# HEAD naming any one of them is the 405 (same date).
_BUCKET_READ_SELECTORS = (
    _BUCKET_SELECTORS | frozenset(_BUCKET_READ_REFUSED) | _BUCKET_OBJECT_SELECTORS
)
_OBJECT_READ_SELECTORS = (
    _OBJECT_SELECTORS | frozenset(_OBJECT_READ_REFUSED) | {"uploads"} | _KEY_BUCKET_SELECTORS
)
# What a GET's 405 names for each selector it is refused for, UploadPart's pair among them.
_BUCKET_GET_REFUSALS = {**_BUCKET_READ_REFUSED, "PART": _UPLOAD_PART[0]}
_OBJECT_GET_REFUSALS = {**_OBJECT_READ_REFUSED, "PART": _UPLOAD_PART[0]}
# What a method at a key's path naming a selector gets: the object's own table, and for a selector
# of `_KEY_BUCKET_SELECTORS` the bucket's row, which is what each of those answered at a key in a
# bucket nobody owns on `PUT`, `POST`, `DELETE` and `PATCH` (52 requests, same date).
_OBJECT_PATH_WRITE_SELECTORS = {
    **_OBJECT_WRITE_SELECTORS,
    **{selector: _BUCKET_WRITE_SELECTORS[selector] for selector in _KEY_BUCKET_SELECTORS},
}
_KEY_REQUIRED = "Object must have a valid key name."
# What real answers an unsigned ListBuckets with: a 307 to here (measured 2026-09-29).
_ANONYMOUS_LIST_BUCKETS = "https://aws.amazon.com/s3/"

# The `Access-Control-Request-Method` values real's preflight accepts, case and all: any other is
# "Invalid Access-Control-Request-Method: <value>" at 400 (same date, `put`, `Get`, `FOO`, `TRACE`,
# `CONNECT`, `PROPFIND` and `LINK` among them).
_CORS_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"})


def _method_type(method: str, resource_type: str) -> str:
    return f"<Method>{escape(method)}</Method><ResourceType>{resource_type}</ResourceType>"


def _cors_preflight(request: Request, message: str) -> Response:
    """Real's answer to an `OPTIONS`, which its CORS front end gives before anything reads the path.

    Without an `Origin`, or with an empty one, it is the same 400 on a bucket, a key and the service
    root; with one it is a 403 whose message says which way the lookup failed, whose `ResourceType`
    is `BUCKET` on all three, and whose `Method` is the `Access-Control-Request-Method` the
    preflight asks about, or `OPTIONS` without one or with an empty one (measured, the service root
    included, and the two empty values 2026-09-30). Real says "Bucket not found" for a bucket that
    does not exist; the preflight carries no credential, so this server names every bucket path as
    present rather than tell an anonymous caller which names the corpus holds.
    """
    if not request.headers.get("origin"):
        return _error("BadRequest", _CORS_NEEDS_ORIGIN)
    asked = request.headers.get("access-control-request-method") or "OPTIONS"
    if asked not in _CORS_METHODS:
        return _error("BadRequest", "Invalid Access-Control-Request-Method: " + asked)
    return _error("AccessForbidden", message, extra=_method_type(asked, "BUCKET"))


# --- what real checks of a write before it performs it -----------------------------------------
#
# Four writes real refuses on what the request carries before doing anything, and this server can
# answer them the same way before its 501: a bucket's `POST ?restore`, which names no object, a
# `POST ?delete` at a bucket's path or a key's, DeleteObjects on the bucket either way, a key's
# `PUT ?encryption`, UpdateObjectEncryption, and a key's bare `PUT`, PutObject
# (``_put_object_refusal``). Measured 2026-09-29 against `s3.us-east-1.amazonaws.com` on a bucket
# this account created for it, the public bucket `noaa-ghcn-pds`, another account's bucket and a
# name nobody owns, the refusals one at a time and two at once. Each comes after the credential and
# the bucket, as the rest of a write's answer does (``_refuse_write``). What a valid request would
# then do is the write, which is the 501.
_MALFORMED_XML = (
    "The XML you provided was not well-formed or did not validate against our published schema"
)
_NO_BODY = "Request Body is empty"
_USER_KEY = "User key must be specified."
# The `x-amz-checksum-*` algorithms real takes, and the width of each one's value in bytes: a name
# not here is "The algorithm type you specified in x-amz-checksum- header is invalid.", a value that
# is not base64 of that width (a word for each but `xxhash128`, three bytes and an empty value for
# `crc32`) "Value for x-amz-checksum-<name> header is invalid.", and `type` and `algorithm` are not
# algorithms, a request carrying only them lacking the header. Two of them at once are refused as
# well.
_CHECKSUM_WIDTHS = {
    "crc32": 4,
    "crc32c": 4,
    "crc64nvme": 8,
    "md5": 16,
    "sha1": 20,
    "sha256": 32,
    "sha512": 64,
    "xxhash3": 8,
    "xxhash64": 8,
    "xxhash128": 16,
}
_NOT_CHECKSUMS = frozenset({"type", "algorithm"})


def _reflected_crc_table(poly: int) -> list[int]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
        table.append(crc)
    return table


# CRC-32C and CRC-64/NVME, each reflected with every bit set before and after, as S3 computes them
# (``tests/test_s3.py`` checks both against their standard check values). Real took the value of
# each checksum ``_checksum`` computes, the two here among them, for a DeleteObjects body sent to
# the public bucket (2026-09-29).
_CRC32C = (_reflected_crc_table(0x82F63B78), 32)
_CRC64NVME = (_reflected_crc_table(0x9A6C9329AC4BC9B5), 64)


def _reflected_crc(data: bytes, crc: tuple[list[int], int]) -> bytes:
    table, bits = crc
    mask = (1 << bits) - 1
    value = mask
    for byte in data:
        value = table[(value ^ byte) & 0xFF] ^ (value >> 8)
    return (value ^ mask).to_bytes(bits // 8, "big")


# What each xxhash checksum names: real took `xxhash64` as XXH64, `xxhash3` as XXH3's 64 bits and
# `xxhash128` as its 128, each in the byte order the library's digest has, and refused each reversed
# (2026-09-29, the same DeleteObjects).
_XXHASHES = {"xxhash64": xxhash.xxh64, "xxhash3": xxhash.xxh3_64, "xxhash128": xxhash.xxh3_128}


def _checksum(name: str, body: bytes) -> bytes:
    """The checksum ``name`` (one of ``_CHECKSUM_WIDTHS``) of ``body``."""
    if name == "crc32":
        return zlib.crc32(body).to_bytes(4, "big")
    if name == "crc32c":
        return _reflected_crc(body, _CRC32C)
    if name == "crc64nvme":
        return _reflected_crc(body, _CRC64NVME)
    if name in _XXHASHES:
        return _XXHASHES[name](body).digest()
    return hashlib.new(name, body).digest()


def _decoded(value: str, width: int) -> bytes | None:
    """``value`` as the ``width`` bytes it is the base64 of, or ``None``."""
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        return None
    return raw if len(raw) == width else None


def _local(tag: str) -> str:
    """An element's name without its namespace, which real does not read: a `Delete` in S3's own
    namespace, in `urn:x` and in none were each performed, and an `ObjectEncryption` in `urn:x` read
    as one (2026-09-29)."""
    return tag.rpartition("}")[2]


def _xml_root(body: bytes):
    try:
        return ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None


def _delete_keys(body: bytes) -> list[str] | None:
    """The keys a DeleteObjects body names, or ``None`` when real's schema refuses it: a `Delete`
    holding one to 1000 `Object`s and at most one `Quiet`, in any order and whatever `Quiet` says,
    each `Object` holding one `Key` and at most a `VersionId` and an `ETag`. `<Delete/>`, a `Quiet`
    alone, two of them, an empty `Object`, two `Key`s, two `VersionId`s, two `ETag`s, a `Size` or a
    `LastModifiedTime` in one, an unknown element anywhere and 1001 `Object`s were `MalformedXML`,
    and 1000 were performed (2026-09-29 and 2026-09-30)."""
    root = _xml_root(body)
    if root is None or _local(root.tag) != "Delete":
        return None
    objects, quiet = [], 0
    for child in root:
        name = _local(child.tag)
        if name == "Object":
            objects.append(child)
        elif name == "Quiet":
            quiet += 1
        else:
            return None
    if quiet > 1 or not 1 <= len(objects) <= 1000:
        return None
    keys = []
    for obj in objects:
        names = [_local(c.tag) for c in obj]
        if set(names) - {"Key", "VersionId", "ETag"} or names.count("Key") != 1:
            return None
        if names.count("VersionId") > 1 or names.count("ETag") > 1:
            return None
        keys.append(next(c for c in obj if _local(c.tag) == "Key").text or "")
    return keys


def _content_md5(request: Request) -> tuple[str | None, bytes | None, Response | None]:
    """The `Content-MD5` sent, the digest it names, and its refusal when it names none (16 bytes of
    base64, an empty value refused as any other), naming it as sent."""
    sent = request.headers.get("content-md5")
    if sent is None:
        return None, None, None
    digest = _decoded(sent, 16)
    if digest is None:
        refusal = _error(
            "InvalidDigest",
            "The Content-MD5 you specified was invalid.",
            f"<Content-MD5>{escape(sent)}</Content-MD5>",
        )
        return sent, None, refusal
    return sent, digest, None


def _md5_mismatch(expected: str, digest: bytes, body: bytes) -> Response | None:
    """`BadDigest` for a `Content-MD5` ``body`` does not match, naming the digest computed in base64
    and ``expected`` as the operation names the one sent: DeleteObjects and UpdateObjectEncryption
    as sent, and PutObject in hex (2026-09-29)."""
    actual = hashlib.md5(body).digest()
    if actual == digest:
        return None
    return _error(
        "BadDigest",
        "The Content-MD5 you specified did not match what we received.",
        f"<CalculatedDigest>{base64.b64encode(actual).decode()}</CalculatedDigest>"
        f"<ExpectedDigest>{escape(expected)}</ExpectedDigest>",
    )


def _payload_mismatch(request: Request, body: bytes) -> Response | None:
    """`XAmzContentSHA256Mismatch` for a signed payload hash of 64 hex digits that ``body`` does not
    match without case, naming it as sent and the body's own, or ``None``. Real checked it on
    PutObject, a key's `PUT ?tagging`, a bucket's `PUT ?versioning`, DeleteObjects and
    UpdateObjectEncryption, PutObject's empty body included, and not on a `DELETE` with a body or a
    GET (2026-09-29); where each checks it among its other refusals is in its own function."""
    sent = auth.signed_payload_hash(request)
    if sent is None or not re.fullmatch(r"[0-9a-fA-F]{64}", sent):
        return None
    actual = hashlib.sha256(body).hexdigest()
    if sent.lower() == actual:
        return None
    return _error(
        "XAmzContentSHA256Mismatch",
        "The provided 'x-amz-content-sha256' header does not match what was computed.",
        f"<ClientComputedContentSHA256>{escape(sent)}</ClientComputedContentSHA256>"
        f"<S3ComputedContentSHA256>{actual}</S3ComputedContentSHA256>",
    )


def _checksum_headers(headers) -> tuple[tuple[str, bytes] | None, Response | None]:
    """The `x-amz-checksum-*` header a write names and the digest it decodes to, or the refusal of
    the headers, in real's order: an algorithm ``_CHECKSUM_WIDTHS`` does not name, more than one, a
    value that is not one; then an `x-amz-sdk-checksum-algorithm` with none of them and no
    `x-amz-trailer`, which names the checksum an aws-chunked body carries after it, and one naming
    another algorithm, compared without case. PutObject and DeleteObjects read them alike, and an
    aws-chunked PutObject naming its checksum in `x-amz-trailer` was served (2026-09-29). An empty
    `x-amz-sdk-checksum-algorithm` or `x-amz-trailer` is none, where an empty checksum value is one
    that is not one: real took DeleteObjects with the first empty beside a checksum and beside none,
    and refused the second empty beside a named algorithm (2026-09-30)."""
    named = {
        name[len("x-amz-checksum-") :]: value
        for name, value in headers.items()
        if name.startswith("x-amz-checksum-")
        and name[len("x-amz-checksum-") :] not in _NOT_CHECKSUMS
    }
    if set(named) - _CHECKSUM_WIDTHS.keys():
        return None, _error(
            "InvalidRequest",
            "The algorithm type you specified in x-amz-checksum- header is invalid.",
        )
    if len(named) > 1:
        return None, _error(
            "InvalidRequest",
            "Expecting a single x-amz-checksum- header. Multiple checksum Types are not allowed.",
        )
    checksum = None
    for name, value in named.items():
        checksum = (name, _decoded(value, _CHECKSUM_WIDTHS[name]))
        if checksum[1] is None:
            message = f"Value for x-amz-checksum-{name} header is invalid."
            return None, _error("InvalidRequest", message)
    algorithm = headers.get("x-amz-sdk-checksum-algorithm") or None
    if algorithm is not None and checksum is None and not headers.get("x-amz-trailer"):
        return None, _error(
            "InvalidRequest",
            "x-amz-sdk-checksum-algorithm specified, but no corresponding x-amz-checksum-* or "
            "x-amz-trailer headers were found.",
        )
    if algorithm is not None and checksum is not None and algorithm.lower() != checksum[0]:
        return None, _error(
            "InvalidRequest", "Value for x-amz-sdk-checksum-algorithm header is invalid."
        )
    return checksum, None


def _checksum_mismatch(checksum: tuple[str, bytes] | None, body: bytes) -> Response | None:
    if checksum is None or _checksum(checksum[0], body) == checksum[1]:
        return None
    message = f"The {checksum[0].upper()} you specified did not match the calculated checksum."
    return _error("BadDigest", message)


async def _restore_without_a_key(request: Request, bucket: str, key: str, visible) -> Response:
    """A bucket's `POST ?restore` names no object to restore: real answered it with this 400, with a
    body and without, signed with either version, on the public bucket, another's bucket being
    `AccessDenied`, and on this account's own (2026-09-29)."""
    return _error("UserKeyMustBeSpecified", _USER_KEY)


async def _delete_objects_refusal(
    request: Request, bucket: str, key: str, visible
) -> Response | None:
    """DeleteObjects' refusals, in real's order: the `x-amz-checksum-*` headers and
    `x-amz-sdk-checksum-algorithm` (``_checksum_headers``); a `Content-MD5` that is not one;
    neither a `Content-MD5` nor a checksum ("Missing required header for this request: Content-MD5
    OR x-amz-checksum-*"); no body; a body the schema refuses (``_delete_keys``); an empty `Key`; a
    `Content-MD5` or a checksum the body does not match; and last a payload hash the body does not
    match, which ``_refuse_write`` checks once these pass: real named each of those before it
    (2026-09-29). A request past all of them is the delete itself."""
    checksum, refusal = _checksum_headers(request.headers)
    if refusal is not None:
        return refusal
    sent, digest, refusal = _content_md5(request)
    if refusal is not None:
        return refusal
    if sent is None and checksum is None:
        return _error(
            "InvalidRequest",
            "Missing required header for this request: Content-MD5 OR x-amz-checksum-*",
        )
    body = await request.body()
    if not body:
        return _error("MissingRequestBodyError", _NO_BODY)
    keys = _delete_keys(body)
    if keys is None:
        return _error("MalformedXML", _MALFORMED_XML)
    if "" in keys:
        return _error("UserKeyMustBeSpecified", _USER_KEY)
    if digest is not None and (mismatch := _md5_mismatch(sent, digest, body)) is not None:
        return mismatch
    return _checksum_mismatch(checksum, body)


async def _put_object_refusal(request: Request, bucket: str, key: str, visible) -> Response | None:
    """PutObject's refusals, in real's order: a `Content-MD5` that is not one; the checksum headers
    (``_checksum_headers``); an aws-chunked payload hash (`STREAMING-…`) without an
    `x-amz-decoded-content-length`, the 411 real answered a plain body with, a trailer's value and
    a signed one alike; a payload hash the body does not match (``_payload_mismatch``); a
    `Content-MD5` the body does not match, naming the digest sent in hex; a checksum the body does
    not match (all measured 2026-09-29). A chunked body is not decoded here, so one that declares
    its length is taken on to the write this server does not do."""
    sent, digest, refusal = _content_md5(request)
    if refusal is not None:
        return refusal
    checksum, refusal = _checksum_headers(request.headers)
    if refusal is not None:
        return refusal
    payload_hash = auth.signed_payload_hash(request) or ""
    if payload_hash.startswith("STREAMING-"):
        if "x-amz-decoded-content-length" in request.headers:
            return None
        return _error("MissingContentLength", "You must provide the Content-Length HTTP header.")
    body = await request.body()
    mismatch = _payload_mismatch(request, body)
    if mismatch is not None:
        return mismatch
    if digest is not None and (mismatch := _md5_mismatch(digest.hex(), digest, body)) is not None:
        return mismatch
    return _checksum_mismatch(checksum, body)


def _object_encryption(body: bytes) -> tuple[str, str, str | None] | None:
    """What an UpdateObjectEncryption body asks for, as ``(type, kms_key_arn, bucket_key_enabled)``,
    or ``None`` when real's schema refuses it: an `ObjectEncryption` holding exactly one of an empty
    `SSE-S3` and an `SSE-KMS`, which holds one `KMSKeyArn` and at most one `BucketKeyEnabled` in any
    order. `<x/>`, a body that is not XML, `<ObjectEncryption/>`, an unknown element, `SSE-C`, both,
    two `SSE-KMS`, an `SSE-KMS` without a `KMSKeyArn` or with two, and an `SSE-S3` with an element
    in it were `MalformedXML` (2026-09-29)."""
    root = _xml_root(body)
    if root is None or _local(root.tag) != "ObjectEncryption" or len(root) != 1:
        return None
    (choice,) = root
    kind = _local(choice.tag)
    if kind == "SSE-S3":
        return (kind, "", None) if len(choice) == 0 else None
    if kind != "SSE-KMS":
        return None
    names = [_local(c.tag) for c in choice]
    if set(names) - {"KMSKeyArn", "BucketKeyEnabled"} or names.count("KMSKeyArn") != 1:
        return None
    if names.count("BucketKeyEnabled") > 1:
        return None
    text = {_local(c.tag): c.text or "" for c in choice}
    return kind, text["KMSKeyArn"], text.get("BucketKeyEnabled")


def _arn_fault(arn: str) -> str | None:
    """What real finds wrong with a KMS key ARN, each part taken to the next colon: the six messages
    below are real's, for `garbage` and an ARN with a leading space, `arn:`, `arn:aws:kms`,
    `arn:aws:kms:`, `arn:aws:kms:us-east-1:` and `arn:aws:kms:us-east-1:111:`, and a `key/` with no
    id after it is "resource cannot be empty" (2026-09-29)."""
    first = arn.find(":")
    if first < 0 or arn[:first] != "arn":
        return "Malformed ARN - doesn't start with 'arn:'"
    marks = [first]
    for missing in (
        "no AWS partition specified",
        "no service specified",
        "no AWS region partition specified",
        "no AWS account specified",
    ):
        mark = arn.find(":", marks[-1] + 1)
        if mark < 0:
            return "Malformed ARN - " + missing
        marks.append(mark)
    resource = arn[marks[-1] + 1 :]
    if not resource:
        return "Malformed ARN - no resource specified"
    if re.fullmatch(r"[^/:]+[/:]", resource):
        return "resource cannot be empty"
    return None


def _is_kms_key(arn: str) -> bool:
    """A `kms` ARN naming a `key/<id>`, the id letters, digits and hyphens: `key/abc` and a key's
    UUID were taken on to the account, where `key:abc`, `key/abc/def`, `key/abc:def`, `key/a b`,
    `keys/abc`, an `alias/` and another service's ARN were "Invalid KMS Key ARN format"; the
    partition and the region were not read (`aws-cn` and `us-west-2` were both taken on to the
    account; measured 2026-09-29 and 2026-09-30)."""
    parts = arn.split(":", 5)
    return parts[2] == "kms" and re.fullmatch(r"key/[A-Za-z0-9-]+", parts[5]) is not None


async def _object_encryption_refusal(
    request: Request, bucket: str, key: str, visible
) -> Response | None:
    """UpdateObjectEncryption's refusals, in real's order: a request signed with Signature Version 2
    (``auth.signed_with_v2``); a `Content-MD5` that is not one; no body; a body the schema refuses
    (``_object_encryption``); a `Content-MD5` the body does not match; a payload hash the body does
    not match (``_payload_mismatch``); the key, named with its bucket; `SSE-S3`, which the operation
    does not take; the KMS key's ARN, empty, malformed (``_arn_fault``) or not a key's
    (``_is_kms_key``); a `BucketKeyEnabled` that is not `true` or `false` without case. What real
    checks after those is the key's account, which this server has none of to compare, and then the
    write."""
    if auth.signed_with_v2(request):
        return _error(
            "InvalidRequest",
            "Requests modifying object encryption configuration require AWS Signature Version 4.",
        )
    sent, digest, refusal = _content_md5(request)
    if refusal is not None:
        return refusal
    body = await request.body()
    if not body:
        return _error("MissingRequestBodyError", _NO_BODY)
    asked = _object_encryption(body)
    if asked is None:
        return _error("MalformedXML", _MALFORMED_XML)
    if digest is not None and (mismatch := _md5_mismatch(sent, digest, body)) is not None:
        return mismatch
    mismatch = _payload_mismatch(request, body)
    if mismatch is not None:
        return mismatch
    if _object_row(auth.conn(request), bucket, key, visible) is None:
        return _no_such_key(f"{bucket}/{key}")
    kind, arn, bucket_key = asked
    if kind == "SSE-S3":
        return _error("InvalidRequest", "Target encryption type 'SSE-S3' is not supported.")
    needs = "Requests modifying object encryption configuration to SSE-KMS require a"
    if not arn:
        return _error("InvalidRequest", f"{needs} target kms key arn.")
    fault = _arn_fault(arn)
    if fault is not None:
        return _error("InvalidRequest", f"{needs} valid target kms key arn: {fault}")
    if not _is_kms_key(arn):
        return _error(
            "InvalidRequest",
            "Invalid KMS Key ARN format. You must provide the full KMS Key ARN to make an "
            "UpdateObjectEncryption request",
        )
    if bucket_key is not None and bucket_key.lower() not in ("true", "false"):
        message = f"BucketKeyEnabled must be 'true' or 'false'. Invalid value: {bucket_key}"
        return _error("InvalidRequest", message)
    return None


# Which of those each path's writes run, by selector and method.
_WRITE_CHECKS = {
    ("restore", "POST"): _restore_without_a_key,
    ("delete", "POST"): _delete_objects_refusal,
}
_KEY_WRITE_CHECKS = {
    ("delete", "POST"): _delete_objects_refusal,
    ("encryption", "PUT"): _object_encryption_refusal,
}


async def _refuse_write(
    request: Request,
    bucket: str,
    method: str,
    *,
    resolve: bool = True,
    instead: Response | None = None,
    validate=None,
    key: str = "",
    upload: bool = False,
) -> Response:
    """The 501 for a write, or ``instead``, once the credential and, unless ``resolve`` is false,
    the bucket it names resolve, and once ``validate`` — what real checks of the request before it
    performs the write (``_WRITE_CHECKS``, ``_KEY_WRITE_CHECKS``, ``_put_object_refusal``) — finds
    nothing to refuse.

    Real answers a write naming a bucket that does not exist — a `DELETE`, a key's `PUT`, a
    selector's own method such as `POST ?delete` or `PUT ?acl` — with `NoSuchBucket` at 404, where
    its method refusals answer an absent bucket as they answer a present one (measured 2026-09-23).
    So the method refusals precede resolution and this one does not: a caller asking to delete
    something that is not there learns it is not there, and one asking to delete something that is
    learns this server does not write. A bare bucket `PUT` is CreateBucket, which real answered with
    `BucketAlreadyExists` for a taken name and by creating a free one, not with `NoSuchBucket`, so
    it is refused with ``resolve=False`` and says nothing about the name. An unsigned one is real's
    `AccessDenied` for an anonymous caller (measured 2026-09-29), and any other unsigned write
    names a bucket that caller cannot see.

    Past the bucket, a payload hash naming a trailer is refused (``_trailer_refusal``) unless the
    write is an ``upload``, whose own check reads it; and a `PUT`'s and a `POST`'s payload hash is
    checked against the body (``_payload_mismatch``), where real checked it after the bucket:
    PutObject and UpdateObjectEncryption check it among their own refusals, and a write ``instead``
    does not answer once ``validate`` finds nothing, which is where DeleteObjects has it.
    """
    caller, visible, err = _auth(request)
    if err is not None:
        return err
    if not resolve and caller.is_anonymous:
        return _error(
            "AccessDenied", "Anonymous users cannot invoke this API. Please authenticate."
        )
    if resolve and not _bucket_visible(auth.conn(request), bucket, visible):
        return _no_such_bucket(bucket)
    if not upload and (trailer := _trailer_refusal(request)) is not None:
        return trailer
    if validate is not None:
        refused = await validate(request, bucket, key, visible)
        if refused is not None:
            return refused
    if instead is None and method in ("PUT", "POST"):
        mismatch = _payload_mismatch(request, await request.body())
        if mismatch is not None:
            return mismatch
    return instead or _error("NotImplemented", _WRITE_IS_NOT_SERVED + method)


async def _selector_refusal(
    request: Request,
    bucket: str,
    selected: list[str],
    table: dict[str, tuple[str, frozenset[str]]],
    served_on_get: frozenset[str],
    *,
    checks: dict | None = None,
    key: str = "",
) -> Response | None:
    """The answer to a method carrying the sub-resource selectors ``selected``, or ``None`` when it
    carries none.

    Two selectors are the conflict a GET gets, and a bucket's `partNumber` the 400 a GET gets
    (``_bucket_selected``), both before the credential. One is a write when the method is an
    operation of that selector, checked as real checks it when ``checks`` names the pair
    (``_WRITE_CHECKS``, ``_KEY_WRITE_CHECKS``), and otherwise the 405 naming the selector's type,
    whose `Allow` is `GET` for a selector this server answers on a GET and absent otherwise — the
    line ``_head_refusal`` draws.
    """
    if not selected:
        return None
    if len(selected) > 1:
        return _conflict(selected)
    if selected == ["partNumber"]:
        return _error("InvalidRequest", _KEY_REQUIRED)
    if selected == ["PART"]:
        resource_type, writes = _UPLOAD_PART
    else:
        resource_type, writes = table[selected[0]]
    if request.method in writes:
        return await _refuse_write(
            request,
            bucket,
            request.method,
            validate=(checks or {}).get((selected[0], request.method)),
            key=key,
        )
    return _method_refusal(
        request, resource_type, {"Allow": "GET"} if selected[0] in served_on_get else None
    )


@router.api_route(
    "", methods=["HEAD", "PUT", "POST", "DELETE", "PATCH", "OPTIONS"], include_in_schema=False
)
@router.api_route(
    "/", methods=["HEAD", "PUT", "POST", "DELETE", "PATCH", "OPTIONS"], include_in_schema=False
)
async def service_method_refusal(request: Request) -> Response:
    """The service root serves `ListBuckets` alone, and real answers `PUT`, `POST`, `DELETE` and
    `PATCH` there with one 405 naming `SERVICE`, each measured, and the same with a selector on
    `PATCH`, `POST` and `PUT`. A `HEAD` is the same 405, with or without `?acl` or `?versioning`
    (measured 2026-09-23), framed as ``_head`` frames every `HEAD` refusal; it is declared here
    because a method no route here takes is answered by ``backlot.errors.s3`` as the parse 400.
    Each comes after a signature that was sent verifies (``_method_refusal``)."""
    if request.method == "OPTIONS":
        return _cors_preflight(request, _CORS_NO_BUCKET)
    return _method_refusal(request, "SERVICE", {"Allow": "GET"})


# A bucket's writes that read a `versionId` as a version after the bucket rather than refusing it
# before: real answered `PUT ?acl&versionId=x` and `POST ?restore&versionId=x` with `NoSuchBucket`
# for a name nobody owns and "Invalid version id specified" on the public bucket, where
# `DELETE ?acl`, `PUT ?versioning`, `DELETE ?cors`, `PUT ?tagging` and a bare `DELETE` refused it
# before the bucket, and a bare `POST ?versionId=x` was the 412 (2026-09-30).
_VERSION_AFTER_THE_BUCKET = frozenset({("PUT", "acl"), ("POST", "restore")})


@router.api_route(
    "/{bucket}", methods=["PUT", "POST", "DELETE", "PATCH", "OPTIONS"], include_in_schema=False
)
async def bucket_method_refusal(request: Request, bucket: str) -> Response:
    """Without a selector, `PUT` creates the bucket on real, `DELETE` removes it and `POST` is a
    form upload, and `PATCH` is a refusal of real's own; with one, the selector decides. Real's
    refusals are answered as real answers them, and its writes are the ones this server refuses
    instead."""
    method = request.method
    if method == "OPTIONS":
        return _cors_preflight(request, _CORS_DISABLED)
    q = request.query_params
    selected = _bucket_selected(q, frozenset(_BUCKET_WRITE_SELECTORS))
    late = len(selected) == 1 and (method, selected[0]) in _VERSION_AFTER_THE_BUCKET
    if len(selected) <= 1 and selected != ["partNumber"]:
        skip = late or (method == "POST" and not selected)
        refused = (None if skip else _version_id_refusal(q, selected, method)) or _website_refusal(
            q
        )
        if refused is not None:
            return refused
    if late and "versionId" in q:
        # Resolved as a write, and then refused as an id real cannot read.
        instead = _argument_error(_BAD_VERSION, "versionId", _first(q, "versionId"))
        return await _refuse_write(request, bucket, method, instead=instead)
    if selected == ["uploadId"] and method in _BUCKET_WRITE_SELECTORS["uploadId"][1]:
        # Resolved as a write, and then real's own 400 for the bucket that exists.
        instead = _error("InvalidRequest", _KEY_REQUIRED_FOR_UPLOAD)
        return await _refuse_write(request, bucket, method, instead=instead)
    by_selector = await _selector_refusal(
        request, bucket, selected, _BUCKET_WRITE_SELECTORS, _BUCKET_GETS, checks=_WRITE_CHECKS
    )
    if by_selector is not None:
        return by_selector
    if method == "PATCH":
        return _method_refusal(request, "BUCKET", {"Allow": _ALLOW_BUCKET})
    if method == "POST":
        # Real refuses a bucket POST that is not a form upload before reading anything else; a
        # multipart one is the upload itself, which is a write, so it is refused here as well.
        return _error(
            "PreconditionFailed",
            "At least one of the pre-conditions you specified did not hold",
            extra="<Condition>Bucket POST must be of the enclosure-type multipart/form-data</Condition>",
        )
    return await _refuse_write(request, bucket, method, resolve=method != "PUT")


@router.api_route(
    "/{bucket}/{key:path}",
    methods=["PUT", "POST", "DELETE", "PATCH", "OPTIONS"],
    include_in_schema=False,
)
async def object_method_refusal(request: Request, bucket: str, key: str) -> Response:
    """Without a selector a key path takes `PUT` and `DELETE` on real, both writes, and refuses
    `PATCH` and `POST` with the 405 that names `OBJECT`; with one, the selector decides."""
    method = request.method
    if method == "OPTIONS":
        return _cors_preflight(request, _CORS_DISABLED)
    q = request.query_params
    selected = _selected(q, frozenset(_OBJECT_PATH_WRITE_SELECTORS))
    if selected == ["uploadId"] and "partNumber" in q:
        selected = ["PART"]
    refused = _website_refusal(q)
    if refused is not None:
        return refused
    by_selector = await _selector_refusal(
        request,
        bucket,
        selected,
        _OBJECT_PATH_WRITE_SELECTORS,
        _OBJECT_GETS,
        checks=_KEY_WRITE_CHECKS,
        key=key,
    )
    if by_selector is not None:
        return by_selector
    if method in ("PATCH", "POST"):
        return _method_refusal(request, "OBJECT", {"Allow": _ALLOW_OBJECT})
    if method == "PUT":
        return await _refuse_write(
            request, bucket, method, validate=_put_object_refusal, key=key, upload=True
        )
    return await _refuse_write(request, bucket, method)
