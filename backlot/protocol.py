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

What else httptools refuses, uvicorn answers with that 400 as well, which :class:`Protocol` writes
in place of where the front door has an answer of its own (:mod:`backlot.parser_refusals`).
"""

from __future__ import annotations

import re
import sys

import httptools
from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

from backlot import head_bounds, parser_refusals, request_target

#: The bytes httptools refuses in a header value as `Invalid header value char`: every byte from
#: 0x00 to 0x1F but the tab, the LF and the CR, and 0x7F. A lone LF or CR in a value it refuses for
#: the line's ending instead.
REFUSED_IN_A_VALUE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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
    the close, after which the transport reads nothing more. What :class:`_Head` has read of a head
    when httptools refuses it names the refusal (:meth:`_cause`); a request whose head it has not
    read, one that rides in the same read as the end of the one before it, is answered uvicorn's
    400.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._between_requests = True
        self._held = b""
        self._sent: bytes | None = None
        self._no_version = False
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
                self._no_version = _has_no_version(line)
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

    def send_400_response(self, msg: str) -> None:
        """The front door's answer to what httptools refused (``backlot.parser_refusals``) where it
        has one, else uvicorn's 400. uvicorn calls this as it handles the parser's refusal, which
        :func:`sys.exception` gives."""
        cause = self._cause(str(sys.exception()))
        answer = None
        if cause is not None:
            refused = parser_refusals.Refused(
                cause,
                self.parser.get_method().decode("latin-1"),
                self.url,
                frozenset(self._head.names),
                self._head.coding_after_length,
            )
            answer = parser_refusals.answer(refused)
        if answer is None:
            super().send_400_response(msg)
            return
        self.transport.write(answer)
        self.transport.close()

    def _cause(self, reason: str) -> parser_refusals.Cause | None:
        """Which of ``backlot.parser_refusals.Cause`` httptools refused the request for, read off
        the ``reason`` it gave and the head as far as it has arrived, or ``None`` for any other
        refusal."""
        head = self._head
        if head.size == 0:
            return None
        if reason == "Invalid header value char" and head.value_byte is not None:
            return "nul" if head.value_byte == 0 else "control"
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


class _Head:
    """The head of one request as it arrives, read only as far as :mod:`backlot.head_bounds` and
    :data:`backlot.parser_refusals.Cause` need: how many bytes it holds, how many the line it is in
    holds before its LF, the request line's first bytes, which name the vendor, and of the header
    lines what :meth:`_read_header` keeps."""

    def __init__(self) -> None:
        self.size = 0
        self.line = 0
        self.first = b""
        self.start = b""
        self.in_request_line = True
        self.ended = False
        self.bound: head_bounds.Bound | None = None
        self.named = False
        self.name = b""
        self.in_value = False
        self.value = b""
        self.names: list[bytes] = []
        self.codings: list[bytes] = []
        self.coding_after_length = False
        self.value_byte: int | None = None

    def read(self, data: bytes) -> bytes | None:
        """The vendor's answer where ``data`` takes the head to its front door's bound, else None.
        Nothing after the empty line that ends the head is read."""
        pos = 0
        while not self.ended and pos < len(data):
            lf = data.find(b"\n", pos)
            end = len(data) if lf < 0 else lf
            if self.line == 0:
                self.first = data[pos : pos + 1]
            if self.in_request_line:
                self.start += data[pos:end][: head_bounds.NAMED_WITHIN - len(self.start)]
            else:
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
            if self.in_request_line:
                self.in_request_line = False
            elif empty:
                self.ended = True
            if (answer := self._over()) is not None:
                return answer
            pos = lf + 1
        return None

    def _read_header(self, part: bytes) -> None:
        """Read ``part``, the next bytes of a header line: its name up to the colon, the bytes of
        :data:`REFUSED_IN_A_VALUE` its value holds, and the value of a `Transfer-Encoding`. Of those
        bytes the head keeps 0x00 where a value holds one, as real answered a 0x00 whether another
        of them came before it or after it (:mod:`backlot.parser_refusals`), and else the first."""
        if not self.in_value:
            colon = part.find(b":")
            self.name = (self.name + part[: len(part) if colon < 0 else colon])[:_NAMES_READ]
            if colon < 0:
                return
            self.names.append(self.name.lower())
            self.in_value = True
            part = part[colon + 1 :]
        if self.value_byte != 0 and (refused := REFUSED_IN_A_VALUE.search(part)):
            self.value_byte = 0 if b"\x00" in part else (self.value_byte or refused[0][0])
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
        """The answer once the head is past its bound. The vendor is read off the request line once
        it ends or holds :data:`backlot.head_bounds.NAMED_WITHIN` bytes."""
        if not self.named and (
            not self.in_request_line or len(self.start) >= head_bounds.NAMED_WITHIN
        ):
            self.named = True
            self.bound = head_bounds.bound(self.start)
        if self.bound is None:
            return None
        return self.bound.answer(self.size, self.line, self.in_request_line, self.ended)


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
