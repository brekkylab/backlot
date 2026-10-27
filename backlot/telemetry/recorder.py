"""Counts calls in memory and sends them as OTLP/JSON delta metrics.

The recorder keeps one counter per combination of attributes, and one latency histogram per
combination without the answer's status, error code and class. It swaps them out on every export
and posts the swapped-out interval. Delivery is best effort: a payload that
cannot be sent is dropped, nothing here ever raises into the server, and an export at shutdown
waits a bounded time.
"""

from __future__ import annotations

import logging
import platform
import sys
import threading
import time
from typing import Any, Callable, Mapping

import httpx

from backlot.version import version as _version

ENDPOINT = "https://telemetry.brekkylab.com/v1/metrics"
# Seconds between exports. What was counted since the last one is also sent at shutdown.
INTERVAL = 60.0
# Attributes that describe how a call was answered rather than the call.
_ANSWER = ("http.response.status_code", "backlot.error_code", "backlot.error_class")
# Upper bounds, in ms, of the latency histogram's buckets.
BOUNDS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)
# Combinations kept per interval. A request that would add one more is counted as dropped instead,
# which bounds memory however varied the traffic is, and the export says how many were.
MAX_SERIES = 4096
_POST_TIMEOUT = 5.0
_log = logging.getLogger(__name__)


def resource() -> dict[str, str]:
    return {
        "service.name": "backlot",
        "service.version": _version(),
        "os.type": {"darwin": "darwin", "win32": "windows"}.get(sys.platform, sys.platform),
        "process.runtime.version": platform.python_version(),
    }


class Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self._start_ns = time.time_ns()
        self._counts: dict[tuple, int] = {}
        self._histograms: dict[tuple, list] = {}
        self.dropped = 0

    def record(self, attributes: Mapping[str, Any], duration_ms: float) -> None:
        key = tuple(sorted(attributes.items()))
        # The latency of a call does not depend on how it was answered.
        timing = tuple((k, v) for k, v in key if k not in _ANSWER)
        with self._lock:
            if key not in self._counts and len(self._counts) >= MAX_SERIES:
                self.dropped += 1
                return
            self._counts[key] = self._counts.get(key, 0) + 1
            hist = self._histograms.get(timing)
            if hist is None:
                hist = self._histograms[timing] = [0, 0.0, [0] * (len(BOUNDS) + 1)]
            hist[0] += 1
            hist[1] += duration_ms
            hist[2][sum(duration_ms > b for b in BOUNDS)] += 1

    def take(self) -> tuple[int, int, dict[tuple, int], dict[tuple, list], int]:
        """The interval so far, which starts a new one."""
        with self._lock:
            out = (self._start_ns, time.time_ns(), self._counts, self._histograms, self.dropped)
            self._reset()
        return out


def _attrs(pairs) -> list[dict]:
    return [
        {"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": str(v)}}
        for k, v in pairs
    ]


def encode(
    start_ns: int,
    end_ns: int,
    counts: dict,
    histograms: dict,
    dropped: int = 0,
    surface: Mapping[tuple[str, str], int] | None = None,
) -> dict | None:
    """The OTLP/JSON body for one interval, None for an interval with no calls.

    ``dropped`` adds the ``backlot.http.dropped`` count: the calls past :data:`MAX_SERIES` this
    interval.

    ``surface`` adds the ``backlot.surface.operations`` gauge: per source and support state, how
    many vendor operations this Backlot's baselines record, so coverage is read off the sender's own
    version."""
    if not counts:
        return None
    start, end = str(start_ns), str(end_ns)
    sums = [
        {
            "attributes": _attrs(key),
            "startTimeUnixNano": start,
            "timeUnixNano": end,
            "asInt": str(n),
        }
        for key, n in counts.items()
    ]
    hists = [
        {
            "attributes": _attrs(key),
            "startTimeUnixNano": start,
            "timeUnixNano": end,
            "count": str(count),
            "sum": total,
            "bucketCounts": [str(b) for b in buckets],
            "explicitBounds": list(BOUNDS),
        }
        for key, (count, total, buckets) in histograms.items()
    ]
    version = _version()
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": _attrs(resource().items())},
                "scopeMetrics": [
                    {
                        "scope": {"name": "backlot.server", "version": version},
                        "metrics": [
                            {
                                "name": "backlot.http.requests",
                                "unit": "{request}",
                                "sum": {
                                    "aggregationTemporality": 1,
                                    "isMonotonic": True,
                                    "dataPoints": sums,
                                },
                            },
                            {
                                "name": "backlot.http.duration",
                                "unit": "ms",
                                "histogram": {"aggregationTemporality": 1, "dataPoints": hists},
                            },
                            *_dropped_metric(dropped, start, end),
                            *_surface_metric(surface or {}, end),
                        ],
                    }
                ],
            }
        ]
    }


def _dropped_metric(dropped: int, start: str, end: str) -> list[dict]:
    if not dropped:
        return []
    point = {
        "attributes": [],
        "startTimeUnixNano": start,
        "timeUnixNano": end,
        "asInt": str(dropped),
    }
    return [
        {
            "name": "backlot.http.dropped",
            "unit": "{request}",
            "sum": {"aggregationTemporality": 1, "isMonotonic": True, "dataPoints": [point]},
        }
    ]


def _surface_metric(surface: Mapping[tuple[str, str], int], end: str) -> list[dict]:
    if not surface:
        return []
    points = [
        {
            "attributes": _attrs((("backlot.source", source), ("backlot.support", support))),
            "timeUnixNano": end,
            "asInt": str(n),
        }
        for (source, support), n in sorted(surface.items())
    ]
    return [
        {
            "name": "backlot.surface.operations",
            "unit": "{operation}",
            "gauge": {"dataPoints": points},
        }
    ]


class Exporter:
    """Posts the recorder's intervals from a daemon thread."""

    def __init__(
        self,
        recorder: Recorder,
        url: str,
        interval: float,
        surface: Callable[[], Mapping[tuple[str, str], int]] | None = None,
    ) -> None:
        self._recorder = recorder
        self._surface_of = surface
        self._surface: Mapping[tuple[str, str], int] | None = None
        self._url = url
        self._interval = max(interval, 1.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sent = 0
        self.failed = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="backlot-telemetry", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.flush()

    def flush(self, timeout: float = _POST_TIMEOUT) -> None:
        interval = self._recorder.take()
        if not interval[2]:
            return
        if self._surface is None and self._surface_of is not None:
            try:  # read once, on this thread rather than a request's
                self._surface = self._surface_of()
            except Exception:  # noqa: BLE001 — the counts go out without it
                self._surface = {}
        body = encode(*interval, surface=self._surface)
        try:
            response = httpx.post(
                self._url,
                json=body,
                timeout=timeout,
                headers={"User-Agent": f"backlot/{_version()}"},
            )
            if response.status_code >= 400:
                self.failed += 1
                _log.debug("telemetry export answered %s", response.status_code)
            else:
                self.sent += 1
        except Exception as e:  # noqa: BLE001 — delivery is best effort and must not reach the server
            self.failed += 1
            _log.debug("telemetry export failed: %s", type(e).__name__)

    def stop(self, timeout: float = 2.0) -> None:
        """Send what was counted since the last export, waiting at most ``timeout`` seconds in all:
        an export already in flight shares the budget, and what is left after it is dropped."""
        deadline = time.monotonic() + timeout
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        remaining = deadline - time.monotonic()
        if remaining > 0.05:
            # On a thread of its own: httpx resolves the host outside its timeout, so a stalled
            # resolver would hold the caller past the bound. The thread is left behind at it.
            final = threading.Thread(target=self.flush, kwargs={"timeout": remaining}, daemon=True)
            final.start()
            final.join(remaining)


# The process's one recorder: the middleware counts into it and the server's exporter drains it.
RECORDER = Recorder()
