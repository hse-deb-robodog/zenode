"""The standard node-health heartbeat message.

Every node publishes this on ``<ns>/node/<name>/health`` (see
:class:`zenode.node.Node`); liveliness answers *whether* a node is up, health
answers *how well* it is doing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from ..topic import resolve_key
from .info import MeasureDescriptor

NodeState = Literal["starting", "running", "stopping", "stopped"]


def health_key(name: str) -> str:
    """The key, relative to the namespace, that ``name`` publishes health on."""
    return f"node/{name}/health"


def health_pattern(namespace: str) -> str:
    """Key expression matching every node's health in ``namespace``.

    Derived from :func:`health_key` so the publisher and the CLI cannot drift.
    """
    return resolve_key(health_key("*"), namespace)


class NodeHealth(BaseModel):
    """One node's health heartbeat.

    Counters are cumulative since start; the ``*_ms`` latencies cover the
    interval since the previous heartbeat.
    """

    node: str
    host: str = ""
    """The machine this node runs on. A deployment label, not an identity:
    nothing is addressed by it and no key contains it, so two nodes sharing a
    host is a fact about where the CPU is burning, never a lookup. Defaults to
    ``socket.gethostname()``; set ``[transport] host`` where that is a random
    container id rather than a name a person recognizes."""
    state: NodeState
    uptime_s: float
    sent: int = 0
    received: int = 0
    dropped: int = 0
    stale: int = 0
    handler_errors: int = 0
    """Exceptions raised inside subscription handlers, service handlers, timer
    bodies and ``@on_matching`` hooks — all the places zenode catches and keeps
    going."""
    timer_overruns: int = 0
    """Timer periods missed because a body ran longer than its interval.
    Sustained overruns mean a periodic loop cannot hold its rate."""
    deadline_misses: int = 0
    """Subscriptions that went silent — a producer that stopped, never started,
    or lost its link. Cumulative, one per transition rather than one per second,
    so a rising number is the alert."""
    logs_dropped: int = 0
    """Log records dropped instead of published on this node's log topic,
    because the publish queue was full. A log transport that loses records
    silently is worse than one that says so."""

    shm_fallbacks: int = 0
    """Messages on a ``shm=True`` topic that published through the normal path
    instead — shared memory unavailable, or the pool exhausted. Silently taking
    a seven-times-slower path is how a robot misses its deadline."""

    cpu_percent: float | None = None
    """This process's CPU since the last heartbeat, as a percentage of *one*
    core — a node saturating two cores reports 200. ``None`` where ``/proc`` is
    unavailable, and on the first heartbeat, which has no interval to divide by.
    Unknown and zero are different answers."""
    rss_bytes: int | None = None
    """Resident set size. ``None`` where ``/proc`` is unavailable."""
    queue_max_depth: int = 0
    """Deepest any subscription queue got since the last heartbeat. ``dropped``
    says a queue overflowed; this says one is at 60 of 64 and about to."""

    age_mean_ms: float = 0.0
    """Publish-to-dequeue delay, averaged over subscriptions by message count.
    Measures the network plus this node's queue; relies on synchronized
    clocks between machines, as :meth:`~zenode.Envelope.age_s` does."""
    age_max_ms: float = 0.0
    handler_mean_ms: float = 0.0
    """Time spent inside handlers — subscription and service alike."""
    handler_max_ms: float = 0.0

    measures: dict[str, float] = {}
    """This node's own ``@metric`` declarations, sampled for this heartbeat.

    An id absent from the dict is **unknown, not zero** — the measurement
    returned ``None``, raised, or is not declared by this node at all. Unit,
    kind and description live on the latched
    :class:`~zenode.msgs.info.NodeInfo` descriptor instead of here, because
    they never change and this message goes out every ``health_interval``.

    Bounded by declaration: the ids are fixed once the node class body has
    executed, which is what keeps the exported series countable."""

    ts_ns: int


RUNTIME_MEASURES: tuple[MeasureDescriptor, ...] = (
    MeasureDescriptor(
        id="uptime_s",
        unit="s",
        description="Time since this node started.",
    ),
    MeasureDescriptor(
        id="sent",
        unit="{message}",
        kind="counter",
        integral=True,
        description="Messages published.",
    ),
    MeasureDescriptor(
        id="received",
        unit="{message}",
        kind="counter",
        integral=True,
        description="Messages received.",
    ),
    MeasureDescriptor(
        id="dropped",
        unit="{message}",
        kind="counter",
        integral=True,
        description="Messages dropped by a full queue.",
    ),
    MeasureDescriptor(
        id="stale",
        unit="{message}",
        kind="counter",
        integral=True,
        description="Messages dropped past max_age.",
    ),
    MeasureDescriptor(
        id="handler_errors",
        unit="{error}",
        kind="counter",
        integral=True,
        description="Exceptions raised inside subscription, service and timer handlers.",
    ),
    MeasureDescriptor(
        id="timer_overruns",
        unit="{overrun}",
        kind="counter",
        integral=True,
        description="Timer deadlines missed because a body outran its interval.",
    ),
    MeasureDescriptor(
        id="deadline_misses",
        unit="{miss}",
        kind="counter",
        integral=True,
        description="Subscriptions that went silent past their deadline.",
    ),
    MeasureDescriptor(
        id="logs_dropped",
        unit="{record}",
        kind="counter",
        integral=True,
        description="Log records dropped before publishing, leaving `zenode logs` incomplete.",
    ),
    MeasureDescriptor(
        id="shm_fallbacks",
        unit="{message}",
        kind="counter",
        integral=True,
        description="Messages on a shm=True topic that published through the normal path.",
    ),
    MeasureDescriptor(
        id="cpu_percent",
        unit="%",
        description="Process CPU since the last heartbeat, as a percentage of one core.",
    ),
    MeasureDescriptor(
        id="rss_bytes",
        unit="By",
        integral=True,
        description="Process resident set size.",
    ),
    MeasureDescriptor(
        id="queue_max_depth",
        unit="{message}",
        integral=True,
        description="Deepest any subscription queue got since the last heartbeat.",
    ),
    MeasureDescriptor(
        id="age_mean_ms",
        unit="ms",
        description="Publish-to-dequeue delay, mean over the last heartbeat interval.",
    ),
    MeasureDescriptor(
        id="age_max_ms",
        unit="ms",
        description="Publish-to-dequeue delay, worst case over the last heartbeat interval.",
    ),
    MeasureDescriptor(
        id="handler_mean_ms",
        unit="ms",
        description="Time spent inside handlers, mean over the last heartbeat interval.",
    ),
    MeasureDescriptor(
        id="handler_max_ms",
        unit="ms",
        description="Time spent inside handlers, worst case over the last heartbeat interval.",
    ),
)
"""The heartbeat's own numeric fields, described the way ``@metric`` describes
an application's. Ids are the :class:`NodeHealth` field names, so a consumer
reads a value by ``getattr(health, id)`` and formats it by descriptor, in any
language. Published on :attr:`zenode.msgs.NodeInfo.runtime` and rendered by
``zenode export``, which keeps its own series names (``sent_total``, seconds
instead of ``ms``) as rendering rules on top of this one table. Units are the
*wire* units: ``ms`` here, because that is what the field holds."""

RUNTIME_BY_ID: dict[str, MeasureDescriptor] = {d.id: d for d in RUNTIME_MEASURES}
"""The same catalog, indexed; ``tests/test_runtime_measures.py`` holds it to
exactly the numeric fields of :class:`NodeHealth`."""
