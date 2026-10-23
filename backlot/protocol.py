"""The HTTP/1.1 protocol `backlot serve` hands uvicorn: uvicorn's own httptools protocol, which
reads a request line that starts a read from the socket before its parser does (:class:`Protocol`
says which).

httptools refuses a request target holding one of the bytes :mod:`backlot.request_target` names,
in the path or in the query, and uvicorn answers the refusal itself with a 400 reading
`Invalid HTTP request received.`, before the app is called (uvicorn 0.52.4 with httptools 0.9.0;
none of the ten ``set_dangerous_leniencies`` options lets one through). :class:`Protocol` hands
the parser the line with those bytes percent-encoded and leaves the target as sent on the scope,
where ``backlot.main.answer_a_raw_byte_in_the_request_target`` answers it.
"""

from __future__ import annotations

from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

from backlot import request_target


class Protocol(HttpToolsProtocol):
    """uvicorn's httptools protocol, reading a request line before its parser does.

    The line is read where a request starts a read from the socket: the first request on the
    connection, and each one after a request that has been read to its end. That is every request a
    client sends after the answer to the one before; a request that rides in the same read as the
    end of the one before it goes to the parser as it came.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._between_requests = True
        self._held = b""
        self._sent: bytes | None = None

    def data_received(self, data: bytes) -> None:
        if self._between_requests:
            data = self._held + data
            end = data.find(b"\n")
            if end < 0:
                self._held = data
                self._unset_keepalive_if_required()
                return
            self._held = b""
            line, self._sent = request_target.encode_line(data[: end + 1])
            data = line + data[end + 1 :]
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
