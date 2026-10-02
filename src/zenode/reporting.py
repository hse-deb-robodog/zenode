"""The reporter: a node's heartbeat and its latched descriptor, in one place.

One module owns everything a node says about itself — the hot half (the
heartbeat, every ``health_interval``) and the cold half (the descriptor,
republished when the wiring changes). The protocol between the two is the
part worth concentrating: the descriptor is a *snapshot*, ``subscribe()`` /
``publisher()`` / ``serve()`` are public API a handler may call long after
start, and a publish that failed must be retried by the next beat without
being mistaken for a node that never started. Before this module existed,
that one invariant was narrated in four separate comments across ``node.py``.

Like ``topic.py``, this file imports no zenoh. The publishers arrive as two
plain callables via :meth:`Reporter.attach`, and everything the heartbeat
observes arrives as :class:`ReporterSources` — which is also the honest,
written-down answer to "what does the heartbeat actually read?". Tests
construct both with fakes; no name-mangled attribute is part of the deal.

**The heartbeat itself never fails.** A broken measurement must not take the
liveness signal with it — the absence of a heartbeat is what a fleet alerts
on, and a node's own bug must not be able to forge it.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from .metrics import Latency, ProcessStats, summarize
from .msgs.health import RUNTIME_MEASURES, NodeHealth, NodeState
from .msgs.info import EntityInfo, NodeInfo, ServiceInfo
from .topic import Service, Topic, topic_flags

if TYPE_CHECKING:
    from .declarative import Measurement


def _entity_info(topic: Topic[Any], key: str) -> EntityInfo:
    """One publisher or subscription, as it appears on the descriptor."""
    return EntityInfo(key=key, schema_name=topic.schema.__name__, flags=topic_flags(topic))


def _json_schema(model: type[Any]) -> dict[str, Any]:
    """``model_json_schema()`` where there is one; ``{}`` for ``bytes``."""
    if isinstance(model, type) and issubclass(model, BaseModel):
        return model.model_json_schema()
    return {}


def _service_info(service: Service[Any, Any], key: str) -> ServiceInfo:
    """One served service, as it appears on the descriptor."""
    return ServiceInfo(
        key=key,
        request=service.request.__name__,
        reply=service.reply.__name__,
        diagnostic=service.diagnostic,
        description=service.description,
        request_schema=_json_schema(service.request),
        reply_schema=_json_schema(service.reply),
        request_encoding=str(service.request_codec.encoding),
        reply_encoding=str(service.reply_codec.encoding),
    )


class PublisherLike(Protocol):
    """The slice of a :class:`~zenode.pubsub.Publisher` the reporter reads."""

    topic: Topic[Any]
    key: str
    sent: int
    errors: int


class SubscriptionLike(Protocol):
    """The slice of a :class:`~zenode.pubsub.Subscription` the reporter reads.

    ``queue_peak`` and the two :class:`~zenode.metrics.Latency` accumulators
    are also *written*: each beat reports them and resets the window, so one
    startup spike cannot dominate the numbers for hours.
    """

    topic: Topic[Any]
    key: str
    received: int
    dropped: int
    stale: int
    errors: int
    deadline_misses: int
    queue_peak: int
    age: Latency
    handler_time: Latency


class ServerLike(Protocol):
    """The slice of a :class:`~zenode.service.ServiceServer` the reporter reads."""

    service: Service[Any, Any]
    key: str
    errors: int
    handler_time: Latency


class TimerLike(Protocol):
    """The slice of a :class:`~zenode.timers.Timer` the reporter reads."""

    errors: int
    overruns: int


@dataclass(frozen=True)
class ReporterSources:
    """Everything the heartbeat and the descriptor observe, spelled out.

    The entity sequences are the node's *live* lists — the reporter sees a
    wiring change without being told twice (``mark_declared`` is what triggers
    the republish; the lists are what make the snapshot current). Scalars that
    change or are created late (``state``, the log handler) come as callables;
    what is fixed at construction comes as plain values.
    """

    name: str
    host: str
    zenode_version: str
    health_interval: float | None
    """Published on the descriptor so a consumer can judge silence."""
    state: Callable[[], NodeState]
    publishers: Sequence[PublisherLike]
    subscriptions: Sequence[SubscriptionLike]
    servers: Sequence[ServerLike]
    timers: Sequence[TimerLike]
    log_dropped: Callable[[], int]
    shm_fallbacks: Callable[[], int]
    process: ProcessStats
    metrics: Mapping[str, Measurement]
    resolve: Callable[[str], Callable[[], Any]]
    """Resolves a :class:`~zenode.declarative.Measurement`'s ``attr`` to the
    callable to sample — ``getattr`` on the node, deferred to call time so an
    undecorated override is the one called, exactly as ``_wire_bindings``
    resolves a handler."""
    log: logging.Logger


class Reporter:
    """Builds and publishes one node's heartbeat and descriptor.

    Constructed with the node, attached when the publishers exist. Unattached,
    every method is a safe no-op — which is the whole ``health_interval = None``
    story. The node's health timer calls :meth:`beat`; the three wiring methods
    call :meth:`mark_declared`; ``start()`` calls :meth:`publish_info` once,
    after the wiring is complete.
    """

    def __init__(self, sources: ReporterSources) -> None:
        self._sources = sources
        self._measure_errors = 0
        self._declared_version = 0
        self._published_version = -1
        self._ready = False
        self._started_monotonic = time.monotonic()
        self._put_health: Callable[[NodeHealth], None] | None = None
        self._put_info: Callable[[NodeInfo], None] | None = None

    def mark_declared(self) -> None:
        """Note a wiring change; the next beat republishes the descriptor."""
        self._declared_version += 1

    def attach(
        self,
        put_health: Callable[[NodeHealth], None],
        put_info: Callable[[NodeInfo], None],
    ) -> None:
        """Hand over the two publishers and start the uptime clock."""
        self._started_monotonic = time.monotonic()
        self._put_health = put_health
        self._put_info = put_info

    def beat(self) -> None:
        """One heartbeat: publish health, catch the descriptor up, reset windows.

        The reset comes last and only after a successful health publish — the
        same order the inlined version kept, so a put that raises (and is
        logged by the timer) does not silently discard the interval's numbers.
        """
        if self._put_health is None:
            return
        self._put_health(self.build_health())
        # No second timer: a node whose wiring never changes pays one integer
        # comparison per beat, and `_ready` keeps the descriptor quiet while
        # the heartbeat already ticks during `on_start`.
        if self._ready and self._declared_version != self._published_version:
            self.publish_info()
        for sub in self._sources.subscriptions:
            sub.age.reset()
            sub.handler_time.reset()
            sub.queue_peak = 0
        for server in self._sources.servers:
            server.handler_time.reset()

    def publish_info(self) -> None:
        """Publish the descriptor; never raise.

        ``_ready`` is set on entry rather than on success, deliberately: it
        means "the wiring is complete enough to describe", so a *failed*
        publish stays distinguishable from a node that never finished
        ``start()`` — the version is what records success, and a mismatch is
        what makes the next beat retry.
        """
        self._ready = True
        if self._put_info is None:
            return
        version = self._declared_version
        try:
            self._put_info(self.build_info())
        except Exception:
            self._sources.log.exception("publishing the node descriptor failed")
            return
        self._published_version = version

    def build_health(self) -> NodeHealth:
        """The heartbeat, built from the live sources. No side effects beyond
        sampling (a raising measurement is counted; see :meth:`_sample`)."""
        s = self._sources
        age_mean_ms, age_max_ms = summarize(sub.age for sub in s.subscriptions)
        handler_mean_ms, handler_max_ms = summarize(
            [sub.handler_time for sub in s.subscriptions]
            + [server.handler_time for server in s.servers]
        )
        # Before the message is built, so a measurement that raises is already
        # counted in the `handler_errors` this same heartbeat reports.
        measures = self._sample()
        return NodeHealth(
            node=s.name,
            host=s.host,
            state=s.state(),
            uptime_s=time.monotonic() - self._started_monotonic,
            sent=sum(p.sent for p in s.publishers),
            received=sum(sub.received for sub in s.subscriptions),
            dropped=sum(sub.dropped for sub in s.subscriptions),
            stale=sum(sub.stale for sub in s.subscriptions),
            handler_errors=sum(sub.errors for sub in s.subscriptions)
            + sum(server.errors for server in s.servers)
            + sum(timer.errors for timer in s.timers)
            + sum(p.errors for p in s.publishers)
            + self._measure_errors,
            timer_overruns=sum(timer.overruns for timer in s.timers),
            deadline_misses=sum(sub.deadline_misses for sub in s.subscriptions),
            logs_dropped=s.log_dropped(),
            shm_fallbacks=s.shm_fallbacks(),
            cpu_percent=s.process.cpu_percent(),
            rss_bytes=s.process.rss_bytes(),
            queue_max_depth=max((sub.queue_peak for sub in s.subscriptions), default=0),
            age_mean_ms=age_mean_ms,
            age_max_ms=age_max_ms,
            handler_mean_ms=handler_mean_ms,
            handler_max_ms=handler_max_ms,
            measures=measures,
            ts_ns=time.time_ns(),
        )

    def build_info(self) -> NodeInfo:
        """The descriptor, built from the live sources."""
        s = self._sources
        return NodeInfo(
            node=s.name,
            host=s.host,
            zenode=s.zenode_version,
            publishes=[_entity_info(p.topic, p.key) for p in s.publishers],
            subscribes=[_entity_info(sub.topic, sub.key) for sub in s.subscriptions],
            serves=[_service_info(server.service, server.key) for server in s.servers],
            measures=[m.descriptor for m in s.metrics.values()],
            runtime=list(RUNTIME_MEASURES),
            health_interval=s.health_interval,
        )

    def _sample(self) -> dict[str, float]:
        """Evaluate the ``@metric`` declarations for one heartbeat.

        Three ways a value is left out, and each of them means *unknown*
        rather than zero — the promise ``cpu_percent`` already makes:

        - ``None``, which is what a measurement returns for "I do not know yet".
        - Not finite. A NaN would serialize as invalid OTLP JSON downstream and
          break a push carrying every other node's numbers with it.
        - The callable raised (or returned something that is not a number),
          which is logged and counted in ``handler_errors``.
        """
        sampled: dict[str, float] = {}
        for measurement in self._sources.metrics.values():
            try:
                value = self._sources.resolve(measurement.attr)()
                number = None if value is None else float(value)
            except Exception:
                self._measure_errors += 1
                self._sources.log.exception(
                    "measurement failed; omitting it from this heartbeat",
                    extra={"measure": measurement.descriptor.id},
                )
                continue
            if number is None or not math.isfinite(number):
                continue
            sampled[measurement.descriptor.id] = number
        return sampled
