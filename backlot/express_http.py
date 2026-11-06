"""What Express does alike for each vendor whose answers it writes."""

from __future__ import annotations

import base64
import hashlib
import re


def weak_etag(body: bytes) -> str:
    """Express's weak `ETag` for ``body``, as the `etag` package writes it: the body's length in
    bytes, in hex, and the first 27 characters of the base64 SHA-1 of the body."""
    digest = base64.b64encode(hashlib.sha1(body).digest()).decode("ascii")[:27]
    return f'W/"{len(body):x}-{digest}"'


#: ``fresh``'s test for `no-cache` among the comma-separated parts of a request's `Cache-Control`,
#: the same in 0.5.2, which Express 4 asks, and in 2.0.0, which Express 5 asks, its `\s` written
#: out as the characters JavaScript's `\s` holds in latin-1, which a header value is read as.
NO_CACHE = re.compile(r"(?:^|,)[\t\n\v\f\r \xa0]*?no-cache[\t\n\v\f\r \xa0]*?(?:,|$)")
