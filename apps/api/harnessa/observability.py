"""Bounded-cardinality HTTP metrics for the API process."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock

_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_KNOWN_METHODS = frozenset({"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"})


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class RequestMetrics:
    """Process-local request counters and latency histogram using route templates."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts: dict[tuple[str, str, int], int] = defaultdict(int)
        self._duration_sum: dict[tuple[str, str, int], float] = defaultdict(float)
        self._buckets: dict[tuple[str, str, int], list[int]] = {}

    def observe(self, method: str, route: str, status: int, duration_seconds: float) -> None:
        # ASGI accepts arbitrary method tokens. Collapse extensions to a stable label
        # so an unauthenticated caller cannot grow the process-local metric maps.
        method = method if method in _KNOWN_METHODS else "OTHER"
        key = (method, route, status)
        with self._lock:
            self._counts[key] += 1
            self._duration_sum[key] += max(0.0, duration_seconds)
            buckets = self._buckets.setdefault(key, [0] * len(_BUCKETS))
            for index, boundary in enumerate(_BUCKETS):
                if duration_seconds <= boundary:
                    buckets[index] += 1

    def render(self) -> str:
        lines = [
            "# HELP harnessa_http_requests_total Total HTTP requests handled by this API process.",
            "# TYPE harnessa_http_requests_total counter",
            "# HELP harnessa_http_request_duration_seconds HTTP request latency in seconds.",
            "# TYPE harnessa_http_request_duration_seconds histogram",
        ]
        with self._lock:
            for (method, route, status), count in sorted(self._counts.items()):
                labels = f'method="{_escape_label(method)}",route="{_escape_label(route)}",status="{status}"'
                lines.append(f"harnessa_http_requests_total{{{labels}}} {count}")
                buckets = self._buckets[(method, route, status)]
                for boundary, bucket_count in zip(_BUCKETS, buckets, strict=True):
                    lines.append(
                        f'harnessa_http_request_duration_seconds_bucket{{{labels},le="{boundary}"}} {bucket_count}'
                    )
                lines.append(
                    f'harnessa_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {count}'
                )
                lines.append(
                    f"harnessa_http_request_duration_seconds_sum{{{labels}}} {self._duration_sum[(method, route, status)]:.9f}"
                )
                lines.append(f"harnessa_http_request_duration_seconds_count{{{labels}}} {count}")
        return "\n".join(lines) + "\n"


request_metrics = RequestMetrics()
