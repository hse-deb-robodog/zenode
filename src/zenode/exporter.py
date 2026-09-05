"""``NodeHealth`` on the bus, re-served as Prometheus metrics.

Every node already publishes the four golden signals and its resource usage on
``<ns>/node/<name>/health``. This turns that into something standard
infrastructure can scrape, without any node knowing about it.

A **sidecar**, deliberately, rather than instruments inside each node:

- Nodes stay dependency-free. Where metrics go is a deployment decision, the
  same boundary :mod:`zenode.otel` draws for spans.
- One process to configure and one connection to a collector, instead of one
  per node — which is the connectivity argument that kept the OpenTelemetry SDK
  out of the nodes in the first place.
- The exposition format is a string join, so this costs no dependency at all.
  No protobuf, no gRPC, nothing compiled, on hardware where that matters.

Cardinality is bounded by *declaration*: one series set per live node, labelled
only by host, node and namespace. The runtime's own fields are a fixed table
below; an application's :func:`~zenode.metric` measurements are fixed once its
node class body has executed, which is what keeps them countable — a free
``dict[str, float]`` keyed by detected-object id would not be, and the damage
would land in someone else's time-series database.

Only pull is implemented. A robot behind NAT cannot be scraped and wants OTLP
push instead; that needs an SDK and an exporter, so it belongs behind the
``otel`` extra rather than here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .msgs.health import NodeHealth
from .msgs.info import MeasureDescriptor, NodeInfo

logger = logging.getLogger(__name__)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

APP_PREFIX = "zenode_app_"
"""Prefix for application measurements, keeping them clear of the runtime's own
``zenode_node_`` names — so a field added to ``NodeHealth`` in a later release
can never collide with someone's ``@metric``."""

DEFAULT_STALE_AFTER = 60.0
"""Seconds without a heartbeat before a node's series are dropped entirely.

Long enough that ``zenode_node_last_seen_seconds`` can carry an alert about a
node that went quiet, short enough that a decommissioned node stops being
reported as if it were merely slow."""


@dataclass(frozen=True, slots=True)
class Sample:
    """One node's most recent heartbeat, and when it arrived."""

    health: NodeHealth
    at: float
    """``time.monotonic()`` on arrival — wall clocks are the sender's problem."""


@dataclass(frozen=True, slots=True)
class Metric:
    """One exported field of ``NodeHealth``, in both wire formats.

    Shared by the Prometheus and OTLP paths on purpose: two tables would drift,
    and a field exported by one and not the other is the kind of gap nobody
    notices until a dashboard is silently missing a series.
    """

    name: str
    """Prometheus name, without the ``zenode_node_`` prefix."""
    kind: str
    help: str
    value: Callable[[NodeHealth], float | None]
    """``None`` omits the series rather than reporting zero: a node with no
    ``/proc`` has unknown CPU, and unknown is not idle."""
    otlp: str
    """OTLP name, dotted per OpenTelemetry convention. Collectors normalise it
    back to the Prometheus form, so both paths land on one series."""
    unit: str
    """UCUM, as OTLP expects: ``s``, ``By``, ``%``, or ``{thing}`` for a count."""
    integral: bool = False
    """Whether the value is a whole number, which OTLP encodes differently."""


# Counters are cumulative since node start. Prometheus detects the reset when a
# node restarts, which is exactly the semantics these have.
COUNTERS: tuple[Metric, ...] = (
    Metric(
        "sent_total",
        "counter",
        "Messages published.",
        lambda h: h.sent,
        "zenode.node.sent",
        "{message}",
        integral=True,
    ),
    Metric(
        "received_total",
        "counter",
        "Messages received.",
        lambda h: h.received,
        "zenode.node.received",
        "{message}",
        integral=True,
    ),
    Metric(
        "dropped_total",
        "counter",
        "Messages dropped by a full queue.",
        lambda h: h.dropped,
        "zenode.node.dropped",
        "{message}",
        integral=True,
    ),
    Metric(
        "stale_total",
        "counter",
        "Messages dropped past max_age.",
        lambda h: h.stale,
        "zenode.node.stale",
        "{message}",
        integral=True,
    ),
    Metric(
        "handler_errors_total",
        "counter",
        "Exceptions raised inside subscription, service and timer handlers.",
        lambda h: h.handler_errors,
        "zenode.node.handler_errors",
        "{error}",
        integral=True,
    ),
    Metric(
        "timer_overruns_total",
        "counter",
        "Timer deadlines missed because a body outran its interval.",
        lambda h: h.timer_overruns,
        "zenode.node.timer_overruns",
        "{overrun}",
        integral=True,
    ),
    Metric(
        "shm_fallbacks_total",
        "counter",
        "Messages on a shm=True topic that published through the normal path.",
        lambda h: h.shm_fallbacks,
        "zenode.node.shm_fallbacks",
        "{message}",
        integral=True,
    ),
    Metric(
        "logs_dropped_total",
        "counter",
        "Log records dropped before publishing, leaving `zenode logs` incomplete.",
        lambda h: h.logs_dropped,
        "zenode.node.logs_dropped",
        "{record}",
        integral=True,
    ),
)

# Base units, per Prometheus convention: seconds and bytes, never milliseconds.
GAUGES: tuple[Metric, ...] = (
    Metric(
        "uptime_seconds",
        "gauge",
        "Time since this node started.",
        lambda h: h.uptime_s,
        "zenode.node.uptime",
        "s",
    ),
    Metric(
        "cpu_percent",
        "gauge",
        "Process CPU since the last heartbeat, as a percentage of one core.",
        lambda h: h.cpu_percent,
        "zenode.node.cpu",
        "%",
    ),
    Metric(
        "rss_bytes",
        "gauge",
        "Process resident set size.",
        lambda h: h.rss_bytes,
        "zenode.node.rss",
        "By",
        integral=True,
    ),
    Metric(
        "queue_max_depth",
        "gauge",
        "Deepest any subscription queue got since the last heartbeat.",
        lambda h: h.queue_max_depth,
        "zenode.node.queue_max_depth",
        "{message}",
        integral=True,
    ),
    Metric(
        "message_age_mean_seconds",
        "gauge",
        "Publish-to-dequeue delay, mean over the last heartbeat interval.",
        lambda h: h.age_mean_ms / 1000.0,
        "zenode.node.message_age_mean",
        "s",
    ),
    Metric(
        "message_age_max_seconds",
        "gauge",
        "Publish-to-dequeue delay, worst case over the last heartbeat interval.",
        lambda h: h.age_max_ms / 1000.0,
        "zenode.node.message_age_max",
        "s",
    ),
    Metric(
        "handler_duration_mean_seconds",
        "gauge",
        "Time spent inside handlers, mean over the last heartbeat interval.",
        lambda h: h.handler_mean_ms / 1000.0,
        "zenode.node.handler_duration_mean",
        "s",
    ),
    Metric(
        "handler_duration_max_seconds",
        "gauge",
        "Time spent inside handlers, worst case over the last heartbeat interval.",
        lambda h: h.handler_max_ms / 1000.0,
        "zenode.node.handler_duration_max",
        "s",
    ),
)


_SELF_HELP = {
    "log_records": "Log records handled by this exporter, by outcome.",
    "metric_pushes": "Metric pushes attempted by this exporter, by outcome.",
}
"""The exporter's own counters.

Nothing else watches the sidecar. When it cannot reach a collector it logs to
its own stderr and every signal stops, which from a dashboard is
indistinguishable from a quiet robot — so the counters it already keeps are
published alongside the nodes' own.
"""


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(namespace: str, node: str, host: str = "", **extra: str) -> str:
    pairs = {"namespace": namespace, "node": node, **extra}
    # An empty host is omitted rather than emitted as host="": a node published
    # by an older zenode has no host, and `host=""` would read as one.
    if host:
        pairs["host"] = host
    return ",".join(f'{key}="{_escape(value)}"' for key, value in sorted(pairs.items()))


def format_value(value: float) -> str:
    """One number, rendered for a human or a scraper. Shared with ``zenode health``.

    Integers print in full and without a trailing ``.0``, so a counter reads as
    a count: ``%g`` alone would turn 1204567 frames into ``1.20457e+06``, which
    a 30 Hz camera reaches in nine hours and these nodes run for a week. Floats
    get six significant digits, because ``6.0023076990000845`` seconds of uptime
    is bytes on every scrape in exchange for nothing.
    """
    return str(int(value)) if float(value).is_integer() else f"{value:.6g}"


_kind_conflicts_logged: set[str] = set()
"""Measure ids already reported as declared with two different kinds. Bounded by
the number of declared ids, and it exists so a scrape every 15 seconds does not
turn one deployment mistake into a log every 15 seconds."""


def app_series_name(descriptor: MeasureDescriptor) -> str:
    """The Prometheus series name for one measurement.

    ``_total`` on counters, because that is the suffix a collector's
    OTLP-to-Prometheus normalisation produces for a monotonic sum — the two
    export paths have to land on one series name.
    """
    suffix = "_total" if descriptor.kind == "counter" else ""
    return f"{APP_PREFIX}{descriptor.id}{suffix}"


def _app_help(descriptor: MeasureDescriptor) -> str:
    text = descriptor.description or f"Application measurement {descriptor.id}."
    return f"{text} [{descriptor.unit}]" if descriptor.unit else text


def app_descriptors(catalogs: dict[str, NodeInfo]) -> dict[str, MeasureDescriptor]:
    """The exported descriptor per measure id, resolving disagreements.

    One Prometheus name cannot carry two types. Where two nodes declare the same
    id with a different ``kind``, the first node in sorted order wins and the
    others' points are omitted by :func:`render` — an arbitrary but *stable*
    rule, which matters more than which one wins, because a series whose type
    flips between scrapes is worse than a missing one. An id is meant to mean
    the same thing fleet-wide; a conflict is a deployment mistake, so it is
    logged (once).
    """
    chosen: dict[str, MeasureDescriptor] = {}
    for node in sorted(catalogs):
        for descriptor in catalogs[node].measures:
            first = chosen.setdefault(descriptor.id, descriptor)
            if first.kind != descriptor.kind and descriptor.id not in _kind_conflicts_logged:
                _kind_conflicts_logged.add(descriptor.id)
                logger.warning(
                    "measurement %r is declared as %s by one node and %s by %r; "
                    "exporting the first and omitting the rest — an id must mean "
                    "the same thing fleet-wide",
                    descriptor.id,
                    first.kind,
                    descriptor.kind,
                    node,
                )
    return chosen


def render_app(
    live: list[tuple[str, Sample]], namespace: str, catalogs: dict[str, NodeInfo]
) -> list[str]:
    """Application measurements, as exposition lines.

    A value whose node has published no descriptor for it is omitted rather than
    guessed at: latched delivery makes that gap transient, and a series whose
    ``TYPE`` flips mid-history is worse than a short hole.

    Conflicts are resolved over the **live** node set only, which is what keeps
    a scrape and a push agreeing. ``Registry.offer_info`` keeps a descriptor
    after its node dies, so without this a node decommissioned an hour ago would
    still win the sorted-first tie-break here — and take a live node's series
    off ``/metrics`` — while :meth:`Registry.snapshot` had already dropped it
    from the push.
    """
    live_names = {name for name, _ in live}
    catalogs = {node: info for node, info in catalogs.items() if node in live_names}
    if not catalogs:
        return []
    declared = {node: {d.id: d for d in info.measures} for node, info in catalogs.items()}
    chosen = app_descriptors(catalogs)
    lines: list[str] = []
    for measure_id in sorted(chosen):
        descriptor = chosen[measure_id]
        points = [
            (name, sample.health.host, sample.health.measures[measure_id])
            for name, sample in live
            if measure_id in sample.health.measures
            # `declared[name]` must have it: a value from a node whose own
            # descriptor has not arrived says nothing about its type.
            and declared.get(name, {}).get(measure_id) is not None
            and declared[name][measure_id].kind == descriptor.kind
        ]
        if not points:
            continue
        name = app_series_name(descriptor)
        lines.append(f"# HELP {name} {_app_help(descriptor)}")
        lines.append(f"# TYPE {name} {descriptor.kind}")
        lines.extend(
            f"{name}{{{_labels(namespace, node, host)}}} {format_value(value)}"
            for node, host, value in points
        )
    return lines


def render_self(stats: dict[str, dict[str, int]]) -> list[str]:
    """The exporter's own counters, as exposition lines."""
    lines: list[str] = []
    for family, outcomes in sorted(stats.items()):
        name = f"zenode_exporter_{family}_total"
        lines.append(f"# HELP {name} {_SELF_HELP.get(family, family)}")
        lines.append(f"# TYPE {name} counter")
        lines.extend(
            f'{name}{{outcome="{_escape(outcome)}"}} {value}'
            for outcome, value in sorted(outcomes.items())
        )
    return lines


def render(
    samples: dict[str, Sample],
    namespace: str,
    *,
    now: float,
    stale_after: float = DEFAULT_STALE_AFTER,
    self_stats: dict[str, dict[str, int]] | None = None,
    catalogs: dict[str, NodeInfo] | None = None,
) -> str:
    """The full exposition, as one string. Pure — no clock, no socket.

    Nodes not heard from in ``stale_after`` seconds are omitted entirely, so
    their series go absent rather than freezing at their last value and reading
    as a healthy node that stopped doing anything.

    ``catalogs`` are the nodes' :class:`~zenode.msgs.info.NodeInfo` descriptors,
    which supply the ``TYPE`` and ``HELP`` for their ``@metric`` measurements —
    without one for a node, that node contributes no ``zenode_app_`` series.
    """
    live = sorted(
        (name, sample) for name, sample in samples.items() if now - sample.at <= stale_after
    )
    lines: list[str] = []

    lines.append("# HELP zenode_node_info Node identity and lifecycle state.")
    lines.append("# TYPE zenode_node_info gauge")
    for name, sample in live:
        labels = _labels(namespace, name, sample.health.host, state=sample.health.state)
        lines.append(f"zenode_node_info{{{labels}}} 1")

    lines.append("# HELP zenode_node_last_seen_seconds Age of this node's newest heartbeat.")
    lines.append("# TYPE zenode_node_last_seen_seconds gauge")
    for name, sample in live:
        lines.append(
            f"zenode_node_last_seen_seconds{{{_labels(namespace, name, sample.health.host)}}} "
            f"{format_value(round(now - sample.at, 3))}"
        )

    for metric in (*COUNTERS, *GAUGES):
        rendered = [
            (name, sample.health.host, value)
            for name, sample in live
            if (value := metric.value(sample.health)) is not None
        ]
        if not rendered:
            continue
        lines.append(f"# HELP zenode_node_{metric.name} {metric.help}")
        lines.append(f"# TYPE zenode_node_{metric.name} {metric.kind}")
        lines.extend(
            f"zenode_node_{metric.name}{{{_labels(namespace, name, host)}}} {format_value(value)}"
            for name, host, value in rendered
        )

    lines.extend(render_app(live, namespace, catalogs or {}))

    if self_stats:
        lines.extend(render_self(self_stats))

    return "\n".join(lines) + "\n"


class Registry:
    """Latest heartbeat and descriptor per node.

    Written from a zenoh thread, read by HTTP.
    """

    def __init__(self, namespace: str, *, stale_after: float = DEFAULT_STALE_AFTER) -> None:
        self.namespace = namespace
        self.stale_after = stale_after
        self.self_stats: Callable[[], dict[str, dict[str, int]]] | None = None
        """Set by the caller to also publish the exporter's own counters."""
        self._samples: dict[str, Sample] = {}
        self._catalogs: dict[str, NodeInfo] = {}
        self._lock = threading.Lock()

    def offer(self, payload: bytes) -> None:
        """Accept a heartbeat; ignore anything that is not one.

        A key expression is not a promise about what is published on it, so a
        foreign payload on a matching key is skipped rather than fatal — the
        same tolerance ``zenode health`` applies.
        """
        try:
            health = NodeHealth.model_validate_json(payload)
        except ValueError:
            return
        with self._lock:
            self._samples[health.node] = Sample(health, time.monotonic())

    def offer_info(self, payload: bytes) -> None:
        """Accept a node descriptor; ignore anything that is not one.

        Kept even when the node goes stale, unlike a sample: the descriptor is
        static, and a node that restarts republishes it before its first
        heartbeat arrives anyway. Bounded by the number of nodes that have ever
        been seen, which is the same bound the samples carry.
        """
        try:
            info = NodeInfo.model_validate_json(payload)
        except ValueError:
            return
        with self._lock:
            self._catalogs[info.node] = info

    def snapshot(self) -> tuple[dict[str, Sample], dict[str, NodeInfo]]:
        """Every node heard from recently enough to still count, and its catalog.

        Both under one lock, and shared by both export paths, so a scrape and a
        push made a moment apart report the same nodes and the same types.
        """
        now = time.monotonic()
        with self._lock:
            samples = {
                name: sample
                for name, sample in self._samples.items()
                if now - sample.at <= self.stale_after
            }
            catalogs = {n: self._catalogs[n] for n in samples if n in self._catalogs}
            return samples, catalogs

    def render(self) -> str:
        with self._lock:
            samples = dict(self._samples)
            catalogs = dict(self._catalogs)
        return render(
            samples,
            self.namespace,
            now=time.monotonic(),
            stale_after=self.stale_after,
            self_stats=self.self_stats() if self.self_stats else None,
            catalogs=catalogs,
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._samples)


class _Handler(BaseHTTPRequestHandler):
    registry: Registry

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        body = self.registry.render().encode()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Silence per-request logging: a scrape every 15s is not news."""


def make_server(registry: Registry, host: str, port: int) -> ThreadingHTTPServer:
    """An HTTP server exposing ``registry`` at ``/metrics``.

    The handler class is built per call so the registry is bound to it as a
    class attribute — :class:`~http.server.BaseHTTPRequestHandler` is
    instantiated per request and takes no arguments of its own.
    """
    handler = type("_BoundHandler", (_Handler,), {"registry": registry})
    return ThreadingHTTPServer((host, port), handler)
