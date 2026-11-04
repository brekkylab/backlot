"""The HTTP/1.1 protocol `backlot serve` hands uvicorn: uvicorn's own httptools protocol, which
counts each request head against its front door's bound as it arrives (:mod:`backlot.head_bounds`)
and reads each request line before its parser does (:class:`Protocol`).

httptools reads a header of 262,144 bytes and hands its request to the app, where each front door
has answered at its bound, which is shorter, and closed the connection. :class:`Protocol` answers
there as the front door does, before the parser is handed the byte that reached it. A head that ends
within its bound goes to the parser; one whose target holds 65,536 bytes or more is answered
uvicorn's 400 reading `Invalid HTTP request received.`, the target being longer than
``httptools.parse_url`` reads.

httptools refuses a request target holding one of the bytes :mod:`backlot.request_target` names,
in the path or in the query, and uvicorn answers the refusal itself with a 400 reading
`Invalid HTTP request received.`, before the app is called (uvicorn 0.52.4 with httptools 0.9.0;
none of the ten ``set_dangerous_leniencies`` options lets one through). :class:`Protocol` hands
the parser the line with those bytes percent-encoded and leaves the target as sent on the scope,
where ``backlot.main.answer_a_raw_byte_in_the_request_target`` answers it.

httptools reads a `#` in the target as the start of a fragment, and the app sees the path and the
query without it and what comes after it. :class:`Protocol` leaves a target holding one on the
scope as sent too (:data:`backlot.request_target.WITH_A_FRAGMENT`), and marks each request whose
target holds a `?` or a `#` after its path (:data:`backlot.request_target.FOLLOWED`), which the
scope does not keep for a `?` with nothing after it.

What else httptools refuses, uvicorn answers with that 400 as well; where the front door has an
answer of its own, :class:`Protocol` writes that answer in its place
(:mod:`backlot.parser_refusals`).
"""

from __future__ import annotations

import sys

import httptools
from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

from backlot import head_bounds, parser_refusals, request_target


class Protocol(HttpToolsProtocol):
    """uvicorn's httptools protocol, reading each request's head before its parser does.

    A request that starts in the same read as the end of the one before it is read as one that
    starts a read is (:meth:`_read`).

    A request's line is read before the parser reads it. The CRs and LFs ahead of it go to the
    parser, which skips them, and the rest of a read with no LF after them is held for the reads
    after it while :func:`_starts_a_request_line` holds for what is held. Once it does not, the
    held bytes go to the parser as they came, which refuses them, and :meth:`send_400_response`
    answers: `hello` with no LF is answered so.

    Each request's head is counted by :class:`_Head` from its own first byte, and the front door's
    answer to one that reaches its bound goes out once each request ahead of it on the connection
    has been answered. On api.github.com and on S3 (2026-11-02), a request past the bound behind a
    short one on the same connection drew the short one's answer and then the bound's, written in
    the same write as the short one, in a write of its own, or with its first 30 bytes in the short
    one's write. A refusal httptools makes is answered by :meth:`send_400_response`.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._between_requests = True
        self._held = b""
        self._sent: bytes | None = None
        self._with_a_fragment: bytes | None = None
        self._no_version = False
        self._head = _Head()
        self._body: _Body | None = None
        self._refusal: bytes | None = None
        self._refused: tuple[str, str, bytes, str] | None = None
        self._handed_on = False

    def data_received(self, data: bytes) -> None:
        while data and self._refusal is None and not self.transport.is_closing():
            data = self._read(data)

    def _read(self, data: bytes) -> bytes:
        """Hand the parser ``data`` as far as the end of the request it is in, where its head ends
        or, for a body after it, where :class:`_Body` says the body ends, and return the bytes after
        that end."""
        if self._refused is not None:
            answer = self._head.read(data)
            if answer is not None:
                self._hold(answer)
            else:
                self._answer_the_refusal()
            return b""
        if self._body is not None:
            start = 0
            for end in self._body.ends(data):
                super().data_received(data[start:end])
                start = end
                if self._body is None:
                    return data[end:]
            super().data_received(data[start:])
            return b""
        answer = self._head.read(data)
        if answer is not None:
            self._hold(answer)
            return b""
        data, rest = data[: self._head.read_to], data[self._head.read_to :]
        if self._between_requests:
            data = self._held + data
            self._held = b""
            at = len(data) - len(data.lstrip(b"\r\n"))
            end = data.find(b"\n", at)
            if end >= 0:
                self._with_a_fragment = request_target.with_a_fragment(data[at : end + 1])
                line, self._sent = request_target.encode_line(data[at : end + 1])
                self._no_version = _has_no_version(line)
                data = data[:at] + line + data[end + 1 :]
            elif _starts_a_request_line(data[at:]):
                data, self._held = data[:at], data[at:]
                self._unset_keepalive_if_required()
        super().data_received(data)
        if self.parser is None or self.parser.should_upgrade():
            return b""
        return rest

    def _hold(self, answer: bytes) -> None:
        """Write ``answer`` and close the connection once the answer ahead of it on the connection
        has gone out: at once where none is still to go out, or where the request ``answer``
        refuses has reached the app itself, as one refused in its body has."""
        self._refusal = answer
        if self.cycle is None or self.cycle.response_complete or self._handed_on:
            self._refuse()

    def _refuse(self) -> None:
        self._unset_keepalive_if_required()
        self.transport.write(self._refusal)
        self.transport.close()

    def on_message_begin(self) -> None:
        super().on_message_begin()
        self._between_requests = False
        if self._sent is not None:
            self.scope[request_target.SENT] = self._sent  # type: ignore[literal-required]
            self._sent = None
        if self._with_a_fragment is not None:
            self.scope[request_target.WITH_A_FRAGMENT] = self._with_a_fragment  # type: ignore[literal-required]
            self._with_a_fragment = None

    def on_headers_complete(self) -> None:
        if b"?" in self.url or b"#" in self.url:
            self.scope[request_target.FOLLOWED] = True  # type: ignore[literal-required]
        super().on_headers_complete()
        self._handed_on = True
        self._body = _Body.framed_by(self.headers)

    def on_message_complete(self) -> None:
        super().on_message_complete()
        self._between_requests = True
        self._handed_on = False
        self._head = _Head()
        self._body = None

    def on_response_complete(self) -> None:
        super().on_response_complete()
        if self._refusal is not None and self.cycle.response_complete:
            if not self.transport.is_closing():
                self._refuse()

    def send_400_response(self, msg: str) -> None:
        """Answer the request httptools refused, as :meth:`_answer_the_refusal` does. uvicorn calls
        this as it handles the parser's refusal, which :func:`sys.exception` gives."""
        method = self.parser.get_method().decode("latin-1")
        self._refused = (str(sys.exception()), method, self.url, msg)
        self._answer_the_refusal()

    def _answer_the_refusal(self) -> None:
        """Hold the front door's answer to what httptools refused where ``backlot.parser_refusals``
        has one, else uvicorn's 400, as :meth:`_hold` holds it. A `control` refusal waits for the
        rest of the head where the front door does
        (:func:`backlot.parser_refusals.waits_for_the_head`): the bytes after it go to
        :class:`_Head` alone, the parser having refused them."""
        reason, method, url, msg = self._refused
        cause = self._cause(reason)
        if cause == "control" and not self._head.ended and parser_refusals.waits_for_the_head(url):
            return
        answer = None
        if cause is not None:
            refused = parser_refusals.Refused(
                cause,
                method,
                url,
                frozenset(self._head.names),
                self._head.coding_after_length,
            )
            answer = parser_refusals.answer(refused)
        if answer is None:
            kept, self.transport = self.transport, _Kept()
            try:
                super().send_400_response(msg)
                answer = self.transport.written
            finally:
                self.transport = kept
        self._hold(answer)

    def _cause(self, reason: str) -> parser_refusals.Cause | None:
        """Which of ``backlot.parser_refusals.Cause`` httptools refused the request for, read off
        the ``reason`` it gave and the head as far as it has arrived, or ``None`` for any other
        refusal."""
        head = self._head
        if reason == "Invalid header value char":
            return "nul" if head.nul else "control"
        if reason == "Invalid header token" and self._no_version:
            return "versionless"
        if reason == "Empty Transfer-Encoding":
            return "blank"
        if reason not in (
            "Content-Length can't be present with Transfer-Encoding",
            "Transfer-Encoding can't be present with Content-Length",
        ):
            return None
        if head.codings and all(coding.lower() == b"identity" for coding in head.codings):
            return "identity"
        if head.coding_after_length and head.codings[-1:] == [b""]:
            return "blank"
        return None


class _Kept:
    """A transport that keeps what is written to it, for :meth:`Protocol._answer_the_refusal` to
    hold the 400 uvicorn writes."""

    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    def close(self) -> None:
        pass


class _Body:
    """Where the body after a request's head can end, as its framing headers say: after its
    `Content-Length`, or for a chunked one, after a CRLF CRLF, which ends the last chunk or the
    trailer after it."""

    def __init__(self, length: int | None) -> None:
        self.left = length
        self.tail = b""

    @classmethod
    def framed_by(cls, headers: list[tuple[bytes, bytes]]) -> _Body | None:
        """The body after a head with ``headers`` as the parser read them, names in lower case, or
        ``None`` for a head that frames none: the parser ends such a request at its head or refuses
        it. httptools refuses a `Content-Length` that is not digits, and a second one, before it
        hands the head on."""
        length, chunked = None, False
        for name, value in headers:
            if name == b"content-length":
                length = int(value)
            elif name == b"transfer-encoding":
                chunked = value.rpartition(b",")[2].strip().lower() == b"chunked"
        if chunked:
            return cls(None)
        return None if length is None else cls(length)

    def ends(self, data: bytes):
        """The offsets in ``data``, the body's next bytes, at which it can end, in order."""
        if self.left is not None:
            left, self.left = self.left, max(self.left - len(data), 0)
            if left <= len(data):
                yield left
            return
        joined = self.tail + data
        before = len(self.tail)
        self.tail = joined[-3:]
        at = joined.find(b"\r\n\r\n")
        while at >= 0:
            if at + 4 > before:
                yield at + 4 - before
            at = joined.find(b"\r\n\r\n", at + 1)


class _Head:
    """The head of one request as it arrives, counted as :meth:`backlot.head_bounds.Bound.answer`
    reads it, its request line, whose first bytes name the vendor and which is kept only where
    they name one, and of the header lines what :meth:`_read_header` keeps."""

    def __init__(self) -> None:
        self.size = 0
        self.line = 0
        self.first = b""
        self.request_line = bytearray()
        self.in_request_line = True
        self.blank = 0
        self.ended = False
        self.bound: head_bounds.Bound | None = None
        self.named = False
        self.name = b""
        self.in_value = False
        self.value = b""
        self.names: list[bytes] = []
        self.codings: list[bytes] = []
        self.coding_after_length = False
        self.nul = False
        self.read_to = 0

    def read(self, data: bytes) -> bytes | None:
        """The vendor's answer where ``data`` takes the head to its front door's bound, else None.
        Nothing after the empty line that ends the head is read: :attr:`read_to` is how many bytes
        of ``data`` were."""
        pos = 0
        self.read_to = len(data)
        while not self.ended and pos < len(data):
            lf = data.find(b"\n", pos)
            end = len(data) if lf < 0 else lf
            if self.line == 0:
                self.first = data[pos : pos + 1]
            if self.in_request_line and (not self.named or self.bound is not None):
                self.request_line += data[pos:end]
            elif not self.in_request_line:
                self._read_header(data[pos:end])
            self.line += end - pos
            self.size += end - pos
            if (answer := self._over()) is not None or lf < 0:
                return answer
            empty = self.line == 0 or (self.line == 1 and self.first == b"\r")
            self.size += 1
            self.line = 0
            if not self.in_request_line:
                self._end_header()
            if self.in_request_line and empty:
                self.blank = self.size
                self.request_line = bytearray()
            elif self.in_request_line:
                self.in_request_line = False
            elif empty:
                self.ended = True
            if (answer := self._over()) is not None:
                return answer
            pos = lf + 1
        if self.ended:
            self.read_to = pos
        return None

    def _read_header(self, part: bytes) -> None:
        """Read ``part``, the next bytes of a header line: its name up to the colon, whether its
        value holds 0x00, and the value of a `Transfer-Encoding`. A refusal for a byte in a value
        is `nul` where any value holds 0x00, as real answered a 0x00 whether another byte
        httptools refuses in a value came before it or after it (:mod:`backlot.parser_refusals`)."""
        if not self.in_value:
            colon = part.find(b":")
            self.name = (self.name + part[: len(part) if colon < 0 else colon])[:_NAMES_READ]
            if colon < 0:
                return
            self.names.append(self.name.lower())
            self.in_value = True
            part = part[colon + 1 :]
        if b"\x00" in part:
            self.nul = True
        if self.names[-1] == b"transfer-encoding":
            self.value = (self.value + part)[: _NAMES_READ + 1]

    def _end_header(self) -> None:
        """Keep a `Transfer-Encoding` the header line that has ended holds, without the spaces,
        tabs and CR around it where it fits in :data:`_NAMES_READ`, and whether a `Content-Length`
        came ahead of it."""
        if self.in_value and self.names[-1] == b"transfer-encoding":
            fits = len(self.value) <= _NAMES_READ
            self.codings.append(self.value.strip(b" \t\r") if fits else self.value)
            self.coding_after_length = b"content-length" in self.names
        self.name = b""
        self.in_value = False
        self.value = b""

    def _over(self) -> bytes | None:
        """The answer once the head has reached its bound, the front door's refusal of the request
        line where it refuses the line ahead of the bound
        (:meth:`backlot.head_bounds.Bound.refused`)."""
        if not self.named and (
            not self.in_request_line or len(self.request_line) >= head_bounds.NAMED_WITHIN
        ):
            self.named = True
            self.bound = head_bounds.bound(bytes(self.request_line[: head_bounds.NAMED_WITHIN]))
        if self.bound is None:
            return None
        answer = self.bound.answer(
            self.size, self.blank, self.line, self.in_request_line, self.ended
        )
        if answer is None:
            return None
        line = bytes(self.request_line)
        return self.bound.refused(line, not self.in_request_line, self.blank) or answer


#: How much of a header line's name, and of a `Transfer-Encoding`'s value, :class:`_Head` keeps:
#: enough for the names it reads (`authorization`, `content-length`) and the codings
#: (``backlot.parser_refusals.Cause``) to be told from any longer one.
_NAMES_READ = 32


def _has_no_version(line: bytes) -> bool:
    """Whether ``line``, which ends at its LF, holds no `HTTP/` version after its last space, as a
    line an LF in the target ends holds none."""
    return not line.rstrip(b"\r\n").rpartition(b" ")[2].startswith(b"HTTP/")


def _starts_a_request_line(held: bytes) -> bool:
    """Whether httptools reads ``held`` up to its first space, that space included, as the start of
    a request line: a method it knows, or the start of one. What comes after the space is the
    target, whose bytes :func:`backlot.request_target.encode_line` encodes once the line ends."""
    method, space, _ = held.partition(b" ")
    try:
        httptools.HttpRequestParser(None).feed_data(method + space)
    except httptools.HttpParserError:
        return False
    return True
