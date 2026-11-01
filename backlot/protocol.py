"""The HTTP/1.1 protocol `backlot serve` hands uvicorn: uvicorn's own httptools protocol, which
counts each request head against its front door's bound as it arrives (:mod:`backlot.head_bounds`)
and reads a request line that starts a read from the socket before its parser does
(:class:`Protocol` says which).

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
    """uvicorn's httptools protocol, reading a request line before its parser does.

    The line is read where a request starts a read from the socket: the first request on the
    connection, and each one after a request that has been read to its end. That is every request a
    client sends after the answer to the one before; a request that rides in the same read as the
    end of the one before it goes to the parser as it came.

    A read with no LF is held for the reads after it while :func:`_starts_a_request_line` holds for
    what is held. Once it does not, the held bytes go to the parser as they came, which refuses
    them, and uvicorn answers its 400 at once: `hello` with no LF is answered so.

    Each read is counted by :class:`_Head` first, whatever it holds, against the head of the request
    it carries: from the connection's first byte, and from the first read after a request that has
    been read to its end. A read that takes the head to its bound draws the front door's answer and
    the close, after which the transport reads nothing more.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._between_requests = True
        self._held = b""
        self._sent: bytes | None = None
        self._head = _Head()

    def data_received(self, data: bytes) -> None:
        answer = self._head.read(data)
        if answer is not None:
            self.transport.write(answer)
            self.transport.close()
            return
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
                return
        super().data_received(data)

    def on_message_begin(self) -> None:
        super().on_message_begin()
        self._between_requests = False
        if self._sent is not None:
            self.scope[request_target.SENT] = self._sent  # type: ignore[literal-required]
            self._sent = None

    def on_message_complete(self) -> None:
        super().on_message_complete()
        self._between_requests = True
        self._head = _Head()


class _Head:
    """The head of one request as it arrives, read only as far as :mod:`backlot.head_bounds` needs:
    how many bytes it holds, how many the line it is in holds before its LF, and the request line,
    whose first bytes name the vendor and which is kept only where one does."""

    def __init__(self) -> None:
        self.size = 0
        self.line = 0
        self.first = b""
        self.request_line = bytearray()
        self.in_request_line = True
        self.ended = False
        self.bound: head_bounds.Bound | None = None
        self.named = False

    def read(self, data: bytes) -> bytes | None:
        """The vendor's answer where ``data`` takes the head to its front door's bound, else None.
        Nothing after the empty line that ends the head is read."""
        pos = 0
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
            if self.in_request_line:
                self.in_request_line = False
            elif empty:
                self.ended = True
            if (answer := self._over()) is not None:
                return answer
            pos = lf + 1
        return None

    def _over(self) -> bytes | None:
        """The answer once the head has reached its bound, the front door's refusal of the request
        line where it refuses the line ahead of the bound
        (:meth:`backlot.head_bounds.Bound.refused`). The vendor is read off the request line once it
        ends or holds :data:`backlot.head_bounds.NAMED_WITHIN` bytes."""
        if not self.named and (
            not self.in_request_line or len(self.request_line) >= head_bounds.NAMED_WITHIN
        ):
            self.named = True
            self.bound = head_bounds.bound(bytes(self.request_line[: head_bounds.NAMED_WITHIN]))
        if self.bound is None:
            return None
        answer = self.bound.answer(self.size, self.line, self.in_request_line, self.ended)
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
