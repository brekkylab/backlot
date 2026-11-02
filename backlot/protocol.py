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
"""

from __future__ import annotations

import httptools
from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

from backlot import head_bounds, request_target


class Protocol(HttpToolsProtocol):
    """uvicorn's httptools protocol, reading each request's head before its parser does.

    A request that starts in the same read as the end of the one before it is read as one that
    starts a read is (:meth:`_read`).

    A request's line is read before the parser reads it. A read with no LF is held for the reads
    after it while :func:`_starts_a_request_line` holds for what is held. Once it does not, the
    held bytes go to the parser as they came, which refuses them, and uvicorn answers its 400 at
    once: `hello` with no LF is answered so.

    Each request's head is counted by :class:`_Head` from its own first byte, and the front door's
    answer to one that reaches its bound goes out once each request ahead of it on the connection
    has been answered.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._between_requests = True
        self._held = b""
        self._sent: bytes | None = None
        self._head = _Head()
        self._body: _Body | None = None
        self._refusal: bytes | None = None

    def data_received(self, data: bytes) -> None:
        while data and self._refusal is None and not self.transport.is_closing():
            data = self._read(data)

    def _read(self, data: bytes) -> bytes:
        """Hand the parser ``data`` as far as the end of the request it is in, where its head ends
        or, for a body after it, where :class:`_Body` says the body ends, and return the bytes after
        that end."""
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
            self._refusal = answer
            if self.cycle is None or self.cycle.response_complete:
                self._refuse()
            return b""
        data, rest = data[: self._head.read_to], data[self._head.read_to :]
        if self._between_requests:
            data = self._held + data
            self._held = b""
            end = data.find(b"\n")
            if end >= 0:
                line, self._sent = request_target.encode_line(data[: end + 1])
                data = line + data[end + 1 :]
            elif _starts_a_request_line(data):
                self._held = data
                self._unset_keepalive_if_required()
                return b""
        super().data_received(data)
        if self.parser is None or self.parser.should_upgrade():
            return b""
        return rest

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

    def on_headers_complete(self) -> None:
        super().on_headers_complete()
        self._body = _Body.framed_by(self.headers)

    def on_message_complete(self) -> None:
        super().on_message_complete()
        self._between_requests = True
        self._head = _Head()
        self._body = None

    def on_response_complete(self) -> None:
        super().on_response_complete()
        if self._refusal is not None and self.cycle.response_complete:
            if not self.transport.is_closing():
                self._refuse()


class _Body:
    """Where the body after a request's head can end, as its framing headers say: after its
    `Content-Length`, or for a chunked one, after a CRLF CRLF, which ends the last chunk or the
    trailer after it. A body framed neither way is not read here."""

    def __init__(self, length: int | None, chunked: bool) -> None:
        self.left = length
        self.chunked = chunked
        self.tail = b""

    @classmethod
    def framed_by(cls, headers: list[tuple[bytes, bytes]]) -> _Body:
        """The body after a head with ``headers`` as the parser read them, names in lower case."""
        length, chunked = None, False
        for name, value in headers:
            if name == b"content-length":
                try:
                    length = int(value)
                except ValueError:
                    length = None
            elif name == b"transfer-encoding":
                chunked = value.rpartition(b",")[2].strip().lower() == b"chunked"
        return cls(None if chunked else length, chunked)

    def ends(self, data: bytes):
        """The offsets in ``data``, the body's next bytes, at which it can end, in order."""
        if self.left is not None:
            left, self.left = self.left, max(self.left - len(data), 0)
            if left <= len(data):
                yield left
            return
        if not self.chunked:
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
    reads it, and its request line, whose first bytes name the vendor and which is kept only where
    they name one."""

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
            self.line += end - pos
            self.size += end - pos
            if (answer := self._over()) is not None or lf < 0:
                return answer
            empty = self.line == 0 or (self.line == 1 and self.first == b"\r")
            self.size += 1
            self.line = 0
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
        return self.bound.refused(bytes(self.request_line), not self.in_request_line) or answer


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
