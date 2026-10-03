"""AWS Signature Version 2 verification for the S3 router — standard library only.

Real S3 still verifies it, in the header (``Authorization: AWS <key>:<signature>``) and in the query
(``AWSAccessKeyId``, ``Expires`` and ``Signature``): botocore's ``HmacV1Auth`` and
``HmacV1QueryAuth`` were served a listing, a bucket's ``?versioning``, an object and ListBuckets on
a bucket created that day in us-east-1, and a listing of the public bucket ``noaa-ghcn-pds``
(measured 2026-09-29). The signature is base64 HMAC-SHA1 over a string of the method,
``Content-MD5``, ``Content-Type``, the date, the ``x-amz-*`` headers and the resource, each spelled
here the way real spells them in the ``StringToSign`` it returns on a mismatch.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

# The query parameters real names in the resource it signs. Measured 2026-09-29: every query
# parameter botocore's S3 model names, 72 of them, and `x-id`, `ACL`, `Versioning`, `x-amz-foo` and
# `X-Amz-Foo`, each sent as `?<name>=v` at a bucket's path and at a key's over a bad secret, and
# read out of the `StringToSign` real answered with; `website`, which refuses a value before
# anything is signed, was sent without one. These are the ones it named; the rest it left out, the
# listing's and ListMultipartUploads' own parameters among them, and names are matched with their
# case, so `ACL` and `Versioning` are left out too.
SUBRESOURCES = frozenset(
    {
        "abac",
        "accelerate",
        "acl",
        "analytics",
        "annotation",
        "annotationName",
        "attributes",
        "cors",
        "delete",
        "encryption",
        "intelligent-tiering",
        "inventory",
        "legal-hold",
        "lifecycle",
        "location",
        "logging",
        "metadataAnnotationTable",
        "metadataConfiguration",
        "metadataInventoryTable",
        "metadataJournalTable",
        "metadataTable",
        "metrics",
        "notification",
        "object-lock",
        "ownershipControls",
        "partNumber",
        "policy",
        "policyStatus",
        "publicAccessBlock",
        "replication",
        "requestPayment",
        "response-cache-control",
        "response-content-disposition",
        "response-content-encoding",
        "response-content-language",
        "response-content-type",
        "response-expires",
        "restore",
        "retention",
        "select",
        "select-type",
        "tagging",
        "torrent",
        "uploadId",
        "uploads",
        "versionId",
        "versioning",
        "versions",
        "website",
    }
)
# The ones real names with their value, decoded (`?response-content-type=a%2Fb` is signed as
# `response-content-type=a/b`); it names every other one alone whatever value was sent, `?acl=v`
# and `?acl=` both as `acl`, and a name sent twice once (same measurement).
VALUED = frozenset(
    {
        "partNumber",
        "response-cache-control",
        "response-content-disposition",
        "response-content-encoding",
        "response-content-language",
        "response-content-type",
        "response-expires",
        "select-type",
        "uploadId",
        "versionId",
    }
)


class DateOutOfRange(ValueError):
    """A date past the year 9999, which real answered with a 500 `InternalError` (2026-09-29)."""


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
# The zone names real read beside `GMT`, without case, and the offset east of UTC it read each at,
# in minutes: each was sent at the offset below and served, and `HDT`, `IST` and `ACDT`, which were
# skewed at theirs, were swept half an hour at a time and served at these alone. `UT`, `Z`, `A`,
# `UCT`, `MET`, `MEST`, `IRST`, `GST`, `NPT`, `BDT`, `ICT`, `SGT`, `PHT`, `BRT`, `ART`, `CLT`,
# `CLST`, `UYT`, `PET`, `COT`, `ECT`, `VET`, `BOT`, `PYT`, `GFT`, `SRT`, `AZOT`, `CVT`, `WITA`,
# `MYT`, `CST6CDT`, `EST5EDT`, `PRC`, `ROK`, `Japan`, `GMT0`, `Zulu`, `Universal`, `Greenwich` and
# `Asia/Seoul` were no date (2026-09-29).
_ZONES = {
    "utc": 0,
    "wet": 0,
    "west": 60,
    "bst": 60,
    "cet": 60,
    "wat": 60,
    "cest": 120,
    "eet": 120,
    "sast": 120,
    "cat": 120,
    "ist": 120,
    "eest": 180,
    "msk": 180,
    "eat": 180,
    "pkt": 300,
    "wib": 420,
    "hkt": 480,
    "awst": 480,
    "jst": 540,
    "kst": 540,
    "wit": 540,
    "acst": 570,
    "acdt": 570,
    "aest": 600,
    "chst": 600,
    "aedt": 660,
    "nzst": 720,
    "nzdt": 780,
    "sst": -660,
    "hst": -600,
    "hdt": -600,
    "akst": -540,
    "akdt": -480,
    "pst": -480,
    "pdt": -420,
    "mst": -420,
    "mdt": -360,
    "cst": -360,
    "cdt": -300,
    "est": -300,
    "edt": -240,
    "ast": -240,
    "adt": -180,
    "nst": -210,
    "ndt": -150,
}
# The patterns real reads a `Date` and an `x-amz-date` with besides the ISO 8601 basic form: RFC
# 1123, RFC 850 and asctime, as `java.text.SimpleDateFormat` reads `EEE, dd MMM yyyy HH:mm:ss zzz`,
# `EEE, dd-MMM-yy HH:mm:ss zzz` and `EEE MMM d HH:mm:ss yyyy`, leniently. Every name below and each
# lenient reading was measured on 2026-09-29 against a V2 header: a weekday or a month is its full
# or its short name without case, and a weekday is not checked against the date (`Mon` for a Tuesday
# was served); a separator is matched character for character, so `Tue,29`, `Tue , 29`, a tab after
# the comma and two spaces before the month were no date; a number or the zone may follow extra
# spaces (`Tue,  29` and two before `GMT` or before asctime's year were served); a number is any run
# of digits read over its range (`5:51:47` was read as 05:51 and skewed, `32 Sep` and `25:10:13`
# skewed, `:60` and a day of `029` served); `yyyy` reads a year as written (`02026` served, `26` a
# year long past), and `yy` reads two digits into the century around now and more as written (`026`,
# again a year long past); and nothing may follow (`GMT xyz`, and asctime with a zone, were no
# date).
_TIME = ("hour", ":", "minute", ":", "second")
_PATTERNS = (
    ("weekday", ", ", "day", " ", "month", " ", "year", " ", *_TIME, " ", "zone"),
    ("weekday", ", ", "day", "-", "month", "-", "yy", " ", *_TIME, " ", "zone"),
    ("weekday", " ", "month", " ", "day", " ", *_TIME, " ", "year"),
)
_NAMED = {"weekday": _WEEKDAYS, "month": _MONTHS}
_NUMBERS = frozenset({"day", "year", "yy", "hour", "minute", "second"})
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_date(value: str) -> datetime | None:
    """A ``Date`` or ``x-amz-date`` value as a UTC time, or ``None`` where real reads no date: the
    ISO 8601 basic form or one of ``_PATTERNS``, signed as sent. A date before 1970 is no date (1969
    was, and 1970 skewed), and one past the year 9999 raises ``DateOutOfRange`` (2026-09-29). Real
    reads a V4 header's date the way it reads V2's: `Date` alone, `x-amz-date` in RFC 1123, asctime,
    RFC 850, `UTC`, `EST` and `PDT`, and a weekday the date does not fall on, were each read as a
    date for a V4 header on the same day, and text after the zone was no date."""
    try:
        moment = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        moment = None
        for pattern in _PATTERNS:
            fields = _fields(value, pattern)
            if fields is not None:
                moment = _moment(fields)
                break
    if moment is None or moment < _EPOCH:
        return None
    return moment


def _fields(value: str, pattern: tuple[str, ...]) -> dict[str, int | str] | None:
    """What ``value`` holds for each of ``pattern``'s fields, or ``None`` where it does not fit."""
    fields: dict[str, int | str] = {}
    pos = 0
    for token in pattern:
        if token in _NAMED:
            rest = value[pos:].lower()
            found = max(
                (
                    (len(name), index)
                    for index, full in enumerate(_NAMED[token])
                    for name in (full, full[:3])
                    if rest.startswith(name)
                ),
                default=None,
            )
            if found is None:
                return None
            pos += found[0]
            fields[token] = found[1]
        elif token in _NUMBERS or token == "zone":
            while pos < len(value) and value[pos] in " \t":
                pos += 1
            if token == "zone":
                offset = _zone(value[pos:])
                if offset is None:
                    return None
                fields[token] = offset
                pos = len(value)
                continue
            end = pos
            while end < len(value) and value[end] in "0123456789":
                end += 1
            if end == pos:
                return None
            fields[token] = value[pos:end]
            pos = end
        elif value.startswith(token, pos):
            pos += len(token)
        else:
            return None
    return fields if pos == len(value) else None


def _zone(text: str) -> int | None:
    """A zone's offset east of UTC in minutes: `GMT`, `GMT` then a sign and `h:mm` or `hh:mm`, a
    sign and `hhmm`, or a name in ``_ZONES``. `GMT+0:00`, `GMT-00:00`, `GMT+09:30`, `+0930` and
    `+1400` were read; `GMT+9`, `GMT+0900`, `+09:00`, `+9`, `+000`, `+00000`, `+2400`, `+0060` and
    `UTC+9` were no date (2026-09-29)."""
    match = re.fullmatch(r"(?i:gmt)(?:([+-])([0-9]{1,2}):([0-9]{2}))?", text) or re.fullmatch(
        r"([+-])([0-9]{2})([0-9]{2})", text
    )
    if match is None:
        return _ZONES.get(text.lower())
    if match[1] is None:
        return 0
    hours, minutes = int(match[2]), int(match[3])
    if hours > 23 or minutes > 59:
        return None
    return (hours * 60 + minutes) * (1 if match[1] == "+" else -1)


def _moment(fields: dict[str, int | str]) -> datetime | None:
    """The instant ``fields`` name, each number carried over its range as a lenient calendar
    does."""
    try:
        numbers = {name: int(fields[name]) for name in _NUMBERS if name in fields}
    except ValueError:
        return None
    if "yy" in fields:
        year = numbers["yy"]
        if len(fields["yy"]) == 2:
            start = datetime.now(timezone.utc).year - 80
            year += start // 100 * 100
            if year < start:
                year += 100
    else:
        year = numbers["year"]
    if year > 9999:
        raise DateOutOfRange(year)
    if year < 1:
        return None
    try:
        first = datetime(year, int(fields["month"]) + 1, 1, tzinfo=timezone.utc)
        return first + timedelta(
            days=numbers["day"] - 1,
            hours=numbers["hour"],
            minutes=numbers["minute"] - int(fields.get("zone", 0)),
            seconds=numbers["second"],
        )
    except OverflowError as exc:
        raise DateOutOfRange(year) from exc


def _resource(path: str, query: str) -> str:
    """The wire path, then the signed parameters sorted by name (see ``SUBRESOURCES``)."""
    named: dict[str, str] = {}
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name in SUBRESOURCES:
            named.setdefault(name, value)
    parts = [f"{n}={named[n]}" if n in VALUED else n for n in sorted(named)]
    return path + ("?" + "&".join(parts) if parts else "")


def _amz_headers(headers: dict[str, str], query: str) -> str:
    """The ``x-amz-*`` headers and query parameters, lower-cased and sorted, one ``name:value`` a
    line. A query parameter real signs as a header too, and over a header of the same name: a GET
    carrying `x-amz-foo: 2` and `?x-amz-foo=1` was signed as `x-amz-foo:1` (2026-09-29), and a
    query-signed one's `X-Amz-Signature` as `x-amz-signature`."""
    named = {k: v.strip() for k, v in headers.items() if k.startswith("x-amz-")}
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name.lower().startswith("x-amz-"):
            named[name.lower()] = value
    return "".join(f"{name}:{named[name]}\n" for name in sorted(named))


def string_to_sign(method: str, headers: dict[str, str], date: str, path: str, query: str) -> str:
    """What real signs: ``headers`` lower-cased, ``date`` the line it signs for the date (the
    ``Date`` header, empty beside an ``x-amz-date``, or a query's ``Expires``)."""
    return (
        f"{method}\n{headers.get('content-md5', '')}\n{headers.get('content-type', '')}\n{date}\n"
        + _amz_headers(headers, query)
        + _resource(path, query)
    )


def sign(secret: str, to_sign: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha1).digest()
    ).decode()
