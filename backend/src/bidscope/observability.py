"""Small, process-local observability primitives for BidScope.

The module deliberately keeps telemetry bounded: request IDs are correlation
metadata only, and metric labels come from fixed vocabularies. Structured log
records are allowlisted by their callers and recursively redacted as a final
safety net before they leave the process.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Mapping
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Any, ClassVar, Literal, TextIO, cast

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
_MAX_REQUEST_ID_BYTES = 128
_MAX_STRING_LENGTH = 1000
_MAX_NESTED_DEPTH = 2
_MAX_LIST_ITEMS = 32
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:token|authorization|cookie|secret|api[_-]?key|password)", re.IGNORECASE
)
_BEARER_RE = re.compile(r"(\bBearer\s+)[^\s,;]+", re.IGNORECASE)
_SENSITIVE_VALUE_RE = re.compile(
    r"((?:token|api[_-]?key|secret|password)\s*[:=]\s*)[^\s,;]+",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """The safe, request-scoped fields available to application code."""

    request_id: str
    method: str
    normalized_path: str


_request_context: ContextVar[RequestContext | None] = ContextVar(
    "bidscope_request_context", default=None
)


def get_request_context() -> RequestContext | None:
    """Return the current request context, if execution is inside a request."""
    return _request_context.get()


def valid_request_id(value: str | None) -> bool:
    """Return whether a client-supplied request ID is safe to echo and log."""
    if value is None:
        return False
    try:
        if len(value.encode("ascii")) > _MAX_REQUEST_ID_BYTES:
            return False
    except UnicodeEncodeError:
        return False
    return _REQUEST_ID_RE.fullmatch(value) is not None


def _request_id(request: Request) -> str:
    supplied = request.headers.get(REQUEST_ID_HEADER)
    return supplied if valid_request_id(supplied) and supplied is not None else str(uuid.uuid4())


def _normalized_path(request: Request) -> str:
    """Use a route template, never a raw path containing user-controlled IDs."""
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path:
        normalized = re.sub(r"\{[^}/]+\}", ":id", route_path)
        return normalized[:200]
    return "/unmatched"


def _status_class(status_code: int) -> str:
    return f"{status_code // 100}xx" if 100 <= status_code <= 599 else "unknown"


class _DynamicStderrHandler(logging.StreamHandler[TextIO]):
    """Write to the current stderr so test and process capture both work."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def _logger() -> logging.Logger:
    logger = logging.getLogger("bidscope.observability")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(handler, "_bidscope_json", False) for handler in logger.handlers):
        handler = _DynamicStderrHandler()
        handler._bidscope_json = True  # type: ignore[attr-defined]
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


def _redact_string(value: str) -> str:
    value = value[:_MAX_STRING_LENGTH]
    value = _BEARER_RE.sub(r"\1[REDACTED]", value)
    return _SENSITIVE_VALUE_RE.sub(r"\1[REDACTED]", value)


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= _MAX_NESTED_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:_MAX_LIST_ITEMS]:
            key = str(raw_key)[:100]
            result[key] = "[REDACTED]" if _SENSITIVE_KEY_RE.search(key) else _sanitize(
                raw_value, depth=depth + 1
            )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize(item, depth=depth + 1) for item in list(value)[:_MAX_LIST_ITEMS]]
    return _redact_string(str(value))


class JsonFormatter(logging.Formatter):
    """Serialize structured records as one redacted JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        raw_structured = getattr(record, "structured", None)
        if isinstance(raw_structured, Mapping):
            payload = cast(dict[str, Any], _sanitize(raw_structured))
            payload.setdefault("event", _redact_string(record.getMessage()))
        else:
            payload = {"event": _redact_string(record.getMessage())}
        payload.setdefault("level", record.levelname.lower())
        payload.setdefault("logger", record.name)
        if record.exc_info is not None and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def log_event(level: int, event: str, **fields: Any) -> None:
    """Emit a redacted structured event without accepting arbitrary log text."""
    structured = {"event": event, **fields}
    _logger().log(level, event, extra={"structured": structured})


@dataclass(frozen=True, slots=True)
class _MetricDefinition:
    kind: Literal["counter", "histogram", "gauge"]
    labels: tuple[str, ...]
    values: Mapping[str, frozenset[str]]


_METRIC_DEFINITIONS: dict[str, _MetricDefinition] = {
    "bidscope_http_requests_total": _MetricDefinition(
        "counter",
        ("status",),
        {"status": frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "unknown"})},
    ),
    "bidscope_http_request_duration_seconds": _MetricDefinition(
        "histogram",
        ("status",),
        {"status": frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "unknown"})},
    ),
    "bidscope_runs_total": _MetricDefinition(
        "counter",
        ("outcome",),
        {
            "outcome": frozenset(
                {
                    "pending",
                    "running",
                    "completed",
                    "retryable",
                    "awaiting_confirmation",
                    "cancelled",
                }
            )
        },
    ),
    "bidscope_run_node_duration_seconds": _MetricDefinition(
        "histogram",
        ("node",),
        {"node": frozenset({"intent", "duplicate", "retrieval", "report", "persist", "unknown"})},
    ),
    "bidscope_run_failures_total": _MetricDefinition(
        "counter",
        ("error_code",),
        {
            "error_code": frozenset(
                {
                    "validation",
                    "graph_node_error",
                    "ownership_lost",
                    "dependency_unavailable",
                    "unknown",
                }
            )
        },
    ),
    "bidscope_sse_connections": _MetricDefinition("gauge", (), {}),
    "bidscope_scheduler_ticks_total": _MetricDefinition(
        "counter",
        ("outcome",),
        {"outcome": frozenset({"due", "ran", "skipped", "failed"})},
    ),
    "bidscope_snapshot_imports_total": _MetricDefinition(
        "counter",
        ("outcome", "source"),
        {
            "outcome": frozenset({"success", "failed", "skipped"}),
            "source": frozenset({"ccgp", "ggzy", "synthetic_demo", "unknown"}),
        },
    ),
    "bidscope_report_delivery_duration_seconds": _MetricDefinition(
        "histogram",
        ("outcome",),
        {"outcome": frozenset({"success", "failed"})},
    ),
    "bidscope_dependency_failures_total": _MetricDefinition(
        "counter",
        ("source",),
        {"source": frozenset({"database", "checkpoint", "object_store", "unknown"})},
    ),
    "bidscope_acquisition_runs_total": _MetricDefinition(
        "counter",
        ("source", "outcome"),
        {
            "source": frozenset({"ccgp"}),
            "outcome": frozenset({"success", "failed", "quarantined", "rate_limited"}),
        },
    ),
    "bidscope_acquisition_duration_seconds": _MetricDefinition(
        "histogram",
        ("source",),
        {"source": frozenset({"ccgp"})},
    ),
    "bidscope_source_freshness_seconds": _MetricDefinition(
        "gauge",
        ("source",),
        {"source": frozenset({"ccgp"})},
    ),
    "bidscope_acquisition_records_total": _MetricDefinition(
        "counter",
        ("source",),
        {"source": frozenset({"ccgp"})},
    ),
}



class MetricsRegistry:
    """Thread-safe process-local metrics with fixed names and label vocabularies."""

    HISTOGRAM_BUCKETS: ClassVar[tuple[float, ...]] = (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
    )

    def __init__(self) -> None:
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._histograms: dict[
            tuple[str, tuple[tuple[str, str], ...]], list[float]
        ] = defaultdict(list)
        self._lock = Lock()

    @staticmethod
    def _labels_key(name: str, labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
        definition = _METRIC_DEFINITIONS.get(name)
        if definition is None:
            raise ValueError(f"unknown metric: {name}")
        supplied = dict(labels or {})
        if set(supplied) != set(definition.labels):
            raise ValueError(f"invalid labels for {name}")
        for label_name, value in supplied.items():
            if not isinstance(value, str) or value not in definition.values[label_name]:
                raise ValueError(f"invalid value for label {label_name}")
        return tuple(sorted(supplied.items()))

    def counter(self, name: str, labels: Mapping[str, str] | None = None) -> None:
        definition = _METRIC_DEFINITIONS.get(name)
        if definition is None or definition.kind != "counter":
            raise ValueError(f"metric is not a counter: {name}")
        key = (name, self._labels_key(name, labels))
        with self._lock:
            self._values[key] += 1

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        definition = _METRIC_DEFINITIONS.get(name)
        if definition is None or definition.kind != "histogram":
            raise ValueError(f"metric is not a histogram: {name}")
        if not 0 <= value <= 3600:
            raise ValueError("histogram value is outside the supported bound")
        key = (name, self._labels_key(name, labels))
        with self._lock:
            self._histograms[key].append(value)

    def gauge(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        definition = _METRIC_DEFINITIONS.get(name)
        if definition is None or definition.kind != "gauge":
            raise ValueError(f"metric is not a gauge: {name}")
        if not -1_000_000 <= value <= 1_000_000:
            raise ValueError("gauge value is outside the supported bound")
        key = (name, self._labels_key(name, labels))
        with self._lock:
            self._values[key] = value

    def increment_gauge(self, name: str, labels: Mapping[str, str] | None = None) -> None:
        key = (name, self._labels_key(name, labels))
        definition = _METRIC_DEFINITIONS.get(name)
        if definition is None or definition.kind != "gauge":
            raise ValueError(f"metric is not a gauge: {name}")
        with self._lock:
            self._values[key] += 1

    def decrement_gauge(self, name: str, labels: Mapping[str, str] | None = None) -> None:
        definition = _METRIC_DEFINITIONS.get(name)
        if definition is None or definition.kind != "gauge":
            raise ValueError(f"metric is not a gauge: {name}")
        key = (name, self._labels_key(name, labels))
        with self._lock:
            self._values[key] -= 1

    @staticmethod
    def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        escaped = (
            f'{key}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
            for key, value in labels
        )
        return "{" + ",".join(escaped) + "}"

    def render_prometheus(self) -> str:
        with self._lock:
            values = dict(self._values)
            histograms = {key: tuple(items) for key, items in self._histograms.items()}
        lines: list[str] = []
        for name, definition in _METRIC_DEFINITIONS.items():
            lines.append(f"# TYPE {name} {definition.kind}")
            if definition.kind == "histogram":
                for (metric_name, labels), observations in sorted(histograms.items()):
                    if metric_name != name:
                        continue
                    for bucket in self.HISTOGRAM_BUCKETS:
                        count = sum(observation <= bucket for observation in observations)
                        bucket_labels = tuple(
                            label for label in labels if label[0] != "le"
                        ) + (("le", _format_number(bucket)),)
                        lines.append(
                            f"{name}_bucket{self._render_labels(bucket_labels)} {count}"
                        )
                    inf_labels = tuple(
                        label for label in labels if label[0] != "le"
                    ) + (("le", "+Inf"),)
                    lines.append(
                        f"{name}_bucket{self._render_labels(inf_labels)} "
                        f"{len(observations)}"
                    )
                    lines.append(
                        f"{name}_count{self._render_labels(labels)} "
                        f"{len(observations)}"
                    )
                    lines.append(
                        f"{name}_sum{self._render_labels(labels)} "
                        f"{_format_number(sum(observations))}"
                    )
            else:
                for (metric_name, labels), value in sorted(values.items()):
                    if metric_name == name:
                        lines.append(f"{name}{self._render_labels(labels)} {_format_number(value)}")
        return "\n".join(lines) + "\n"


def _format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return format(value, ".12g")


METRICS_REGISTRY = MetricsRegistry()
metrics_registry = METRICS_REGISTRY


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a bounded correlation ID and emit one RED record per request."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request_id = _request_id(request)
        started = time.monotonic()
        context = RequestContext(
            request_id=request_id,
            method=request.method,
            normalized_path="/unmatched",
        )
        token = _request_context.set(context)
        response: Response | None = None
        exception_type: str | None = None
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            context = RequestContext(
                request_id=request_id,
                method=request.method,
                normalized_path=_normalized_path(request),
            )
            _request_context.set(context)
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        except Exception as error:
            exception_type = type(error).__name__
            raise
        finally:
            duration_seconds = min(max(time.monotonic() - started, 0.0), 3600.0)
            normalized_path = _normalized_path(request)
            status = _status_class(status_code)
            registry = getattr(request.app.state, "metrics", METRICS_REGISTRY)
            with suppress(Exception):
                registry.counter("bidscope_http_requests_total", {"status": status})
                registry.observe(
                    "bidscope_http_request_duration_seconds",
                    duration_seconds,
                    {"status": status},
                )
            log_event(
                logging.INFO,
                "http_request",
                request_id=request_id,
                method=request.method,
                normalized_path=normalized_path,
                status=status_code,
                duration_ms=round(duration_seconds * 1000, 3),
                exception_type=exception_type,
            )
            _request_context.reset(token)
