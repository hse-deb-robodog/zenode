"""Declarative wiring: bind handlers and publishers at class-definition time.

Instead of wiring everything imperatively in ``on_start``, a node can declare
its I/O where it lives::

    class Talker(Node):
        name = "talker"
        cmd = publish(Topics.cmd_vel)              # typed Publisher once started

        @subscribe(Topics.pose, mode="latest")
        async def on_pose(self, msg: Pose) -> None: ...

        @serve(Services.get_map)
        async def on_get_map(self, req: MapRequest) -> CostMap: ...

        @every(0.1)                                   # or @every("rate_hz", unit="hz")
        async def tick(self) -> None: ...

        @metric("battery_soc", unit="1")               # rides on the heartbeat
        def _soc(self) -> float | None: ...

Semantics:

- The decorators only stamp metadata and return the function unchanged, so
  handlers stay directly callable in tests (``await node.on_pose(msg)``).
- ``publish()`` descriptors are materialized when the node starts, *before*
  ``on_start`` runs (so ``on_start`` may use them). Reading one earlier
  raises; assigning to one always raises.
- Decorated bindings are activated *after* ``on_start`` returns, so handlers
  and timers never observe a half-initialized node. ``@every`` intervals are
  resolved against ``self.config`` at that point, so they can come from the
  deployment's config file.
- Inheritance: a subclass that overrides a decorated method *without*
  re-decorating inherits the binding (the override is called). Re-decorating
  replaces the binding. Overriding a ``publish()`` attribute with anything
  else removes that publisher.
- The imperative API (``self.subscribe(...)`` in ``on_start``) remains the
  escape hatch for wiring that is only known at runtime.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Generic, Literal, TypeVar, get_args, overload

from .errors import ContractError
from .msgs.health import NodeHealth
from .msgs.info import MeasureDescriptor, MeasureKind
from .pubsub import OnDeadline, Publisher, SubscriptionMode
from .timers import IntervalSpec, IntervalUnit, OnTimerError
from .topic import Service, Topic

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])

BINDINGS_ATTR = "__zenode_bindings__"
METRICS_ATTR = "__zenode_metric__"

MEASURE_KINDS: tuple[MeasureKind, ...] = get_args(MeasureKind)
"""Derived from the wire model's Literal, so this check and what a
:class:`~zenode.msgs.MeasureDescriptor` accepts are one spelling."""

_MEASURE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
"""What survives being a Prometheus name fragment and an OTLP name segment
unchanged. Validated at decoration so a typo fails at import, not on the first
heartbeat — the stance ``NodeConfig(extra="forbid")`` already takes."""


@dataclass(frozen=True)
class Binding:
    """One declarative wiring instruction stamped onto a method."""

    kind: Literal["subscribe", "serve", "every", "on_silence", "on_resume", "on_matching"]
    target: Topic[Any] | Service[Any, Any] | None = None
    interval: IntervalSpec | None = None
    opts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Measurement:
    """One ``@metric`` declaration, stamped onto a method."""

    descriptor: MeasureDescriptor
    """The static half — id, unit, kind, integral, description — as the wire
    model itself. The node's descriptor publishes this object verbatim, so what
    ``@metric`` declared and what ``zenode info`` shows cannot drift."""
    attr: str = ""
    """The runtime half, filled in by :func:`collect_metrics`. The value is
    resolved with ``getattr(self, attr)`` at sample time, exactly as
    ``_wire_bindings`` resolves a handler, so an undecorated override is the
    one called."""


def _stamp(fn: F, binding: Binding) -> F:
    existing: tuple[Binding, ...] = getattr(fn, BINDINGS_ATTR, ())
    setattr(fn, BINDINGS_ATTR, (*existing, binding))
    return fn


def subscribe(
    topic: Topic[Any],
    *,
    mode: SubscriptionMode = "queue",
    queue_size: int = 64,
    deadline: float | None = None,
    on_deadline: OnDeadline = "log",
) -> Callable[[F], F]:
    """Bind this method as a subscription handler for ``topic``.

    The method keeps the normal handler signature: ``(self, msg)`` or
    ``(self, msg, envelope)``, sync or async. Stack multiple ``@subscribe``
    decorators to feed several topics into one handler.

    ``deadline`` (seconds) reacts to *silence* — see :meth:`zenode.Node.subscribe`.
    Give it a named reaction with :func:`on_silence` / :func:`on_resume`, or a
    policy with ``on_deadline``.
    """
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool) and deadline <= 0:
        raise ContractError("@subscribe deadline must be positive")

    def deco(fn: F) -> F:
        return _stamp(
            fn,
            Binding(
                kind="subscribe",
                target=topic,
                opts={
                    "mode": mode,
                    "queue_size": queue_size,
                    "deadline": deadline,
                    "on_deadline": on_deadline,
                },
            ),
        )

    return deco


def serve(service: Service[Any, Any]) -> Callable[[F], F]:
    """Bind this method as the request handler for ``service``:
    ``(self, request) -> reply``, sync or async."""

    def deco(fn: F) -> F:
        return _stamp(fn, Binding(kind="serve", target=service))

    return deco


def on_silence(topic: Topic[Any]) -> Callable[[F], F]:
    """React when ``topic`` stops arriving for longer than its ``deadline``.

    Signature ``(self, silent_for: float)``, sync or async. Fires **once** per
    outage, on the edge — the reaction is latching, so re-firing it while the
    producer stays gone would be noise. Pair with :func:`on_resume` to learn
    when it is safe to run again.

    The topic must be subscribed with ``deadline=`` somewhere on the node,
    decoratively or in ``on_start``; otherwise the node fails at ``start()``.
    Stackable, so one method can cover several topics.
    """

    def deco(fn: F) -> F:
        return _stamp(fn, Binding(kind="on_silence", target=topic))

    return deco


def on_resume(topic: Topic[Any]) -> Callable[[F], F]:
    """React when ``topic`` starts arriving again after a silence.

    Signature ``(self, silent_for: float)`` — how long the outage lasted, which
    is the number worth logging. Without this edge a node that safed itself has
    no way to learn it may run again, and you are back to polling.
    """

    def deco(fn: F) -> F:
        return _stamp(fn, Binding(kind="on_resume", target=topic))

    return deco


def on_matching(topic: Topic[Any]) -> Callable[[F], F]:
    """React when ``topic`` gains its first subscriber or loses its last.

    Signature ``(self, matching: bool)``, sync or async. The counterpart to
    ``Publisher.matching`` for producers that are expensive to *run*, not just
    to encode — a camera, a lidar spin-up::

        frames = publish(Topics.frames)

        @on_matching(Topics.frames)
        async def on_viewers(self, matching: bool) -> None:
            await (self.camera.start() if matching else self.camera.stop())

    Fires once with the current state when the node starts, then only on a
    change, so the hook alone is enough to decide — there is nothing to poll.
    The node must publish that topic (decoratively or from ``on_start``), and
    the topic must not be latched: a latched publisher always matches, so the
    falling edge would never arrive. Both are errors at ``start()``.

    Matching is a view of the routing graph, not a delivery receipt — good for
    "don't bother producing this", wrong for "the data definitely arrived".
    Stackable, so one method can cover several topics.
    """

    def deco(fn: F) -> F:
        return _stamp(fn, Binding(kind="on_matching", target=topic))

    return deco


def every(
    interval: IntervalSpec,
    *,
    unit: IntervalUnit = "s",
    on_error: OnTimerError = "log",
) -> Callable[[F], F]:
    """Run this method periodically once the node is running.

    The interval may be a literal, a config field name, or a callable —
    resolved against ``self.config`` when the node starts::

        @every(0.1)                             # 10 Hz, fixed
        @every("control_rate_hz", unit="hz")    # self.config.control_rate_hz
        @every(lambda self: 1 / self.config.control_rate_hz)

    A name that is not a field of ``self.config``, or a value that is not a
    positive number, raises :class:`~zenode.ConfigError` at ``start()`` — not
    at the first tick.

    ``on_error`` is the policy for a raising body (see
    :data:`~zenode.timers.OnTimerError`). Scheduling and counters are those of
    :meth:`zenode.Node.every`.
    """
    if isinstance(interval, (int, float)) and not isinstance(interval, bool) and interval <= 0:
        raise ContractError("@every interval must be positive")

    def deco(fn: F) -> F:
        return _stamp(
            fn,
            Binding(kind="every", interval=interval, opts={"unit": unit, "on_error": on_error}),
        )

    return deco


def metric(
    id: str,
    *,
    unit: str = "",
    kind: MeasureKind = "gauge",
    integral: bool = False,
    description: str = "",
) -> Callable[[F], F]:
    """Put one of this node's own numbers on its health heartbeat.

    The decorated method takes no arguments and returns a number, or ``None``
    for *unknown* — which is not zero, the promise ``cpu_percent`` already
    makes::

        @metric("battery_soc", unit="1", description="Pack state of charge.")
        def _soc(self) -> float | None:
            return self._driver.soc

        @metric("frames_processed", unit="{frame}", kind="counter", integral=True)
        def _frames(self) -> int:
            return self._count

    Like every decorator here it only stamps metadata, so the method stays
    directly callable in a test. It is evaluated from the health timer, so the
    same rule applies as to any timer body: **it must not block**, and anything
    that would belongs in :meth:`~zenode.Node.blocking`. Read a value the node
    already cached rather than going to fetch one.

    A raising body is logged, counted in ``handler_errors``, and omits that one
    value; the heartbeat itself is never taken down with it.

    Why declared rather than a dictionary a handler writes into: the set of ids
    is fixed once the class body executes, and that is what keeps the exported
    cardinality bounded — a node keying by detected-object id would otherwise
    defeat it in an afternoon, in someone else's time-series database.

    The fence: **scalars that describe a node's health go on the heartbeat;
    application state goes behind a read-only service.** A pose, a costmap or a
    ``BatteryState`` is not a measurement. ``docs/conventions.md`` is normative
    for ``unit`` — SI on the wire, SoC as 0.0 to 1.0, radians never ``*_deg``.

    Args:
        id: ``[a-z][a-z0-9_]*``, exported as ``zenode_app_<id>``. It means the
            same thing fleet-wide, so two nodes declaring it must agree.
        unit: UCUM, as OTLP expects — ``s``, ``By``, ``1``, ``{frame}``.
        kind: ``"gauge"`` or ``"counter"`` (cumulative since node start).
        integral: The value is a whole number, which OTLP encodes differently.
        description: One line, used as the exported ``HELP``.
    """
    if not _MEASURE_ID.match(id):
        raise ContractError(
            f"@metric({id!r}): id must match {_MEASURE_ID.pattern} — it becomes a "
            f"metric name in Prometheus and OTLP, which neither can escape for you"
        )
    if id in NodeHealth.model_fields:
        raise ContractError(
            f"@metric({id!r}): {id!r} is already a NodeHealth field computed by the "
            f"runtime; pick a name of your own so the two cannot be confused"
        )
    if kind not in MEASURE_KINDS:
        raise ContractError(f"@metric({id!r}): kind must be one of {', '.join(MEASURE_KINDS)}")

    def deco(fn: F) -> F:
        if getattr(fn, METRICS_ATTR, None) is not None:
            # Unlike @subscribe, stacking has no sensible meaning: one callable
            # produces one number, so a second decorator silently discards one
            # of the two declarations.
            raise ContractError(
                f"@metric({id!r}): {getattr(fn, '__name__', fn)!r} already declares a "
                f"measurement; one method reports one value"
            )
        setattr(
            fn,
            METRICS_ATTR,
            Measurement(
                MeasureDescriptor(
                    id=id, unit=unit, kind=kind, integral=integral, description=description
                )
            ),
        )
        return fn

    return deco


class publish(Generic[T]):
    """Class-level publisher declaration, materialized at node start.

    ``cmd = publish(Topics.cmd_vel)`` makes ``self.cmd`` a typed
    :class:`~zenode.Publisher` once the node has started. Reading it earlier
    raises; assigning to it always raises; class-level access returns the
    descriptor itself.
    """

    def __init__(self, topic: Topic[T]) -> None:
        self.topic = topic
        self._name = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name

    @property
    def storage_key(self) -> str:
        return f"__zenode_pub_{self._name}"

    @overload
    def __get__(self, obj: None, objtype: type) -> publish[T]: ...

    @overload
    def __get__(self, obj: object, objtype: type | None = None) -> Publisher[T]: ...

    def __get__(self, obj: object | None, objtype: type | None = None) -> Publisher[T] | publish[T]:
        if obj is None:
            return self
        pub = obj.__dict__.get(self.storage_key)
        if pub is None:
            raise RuntimeError(
                f"publisher {self._name!r} is not available before the node has started"
            )
        return pub

    def __set__(self, obj: object, value: Any) -> None:
        raise AttributeError(f"{self._name!r} is a zenode-managed publisher; it cannot be assigned")


def collect_bindings(cls: type) -> dict[str, tuple[Binding, ...]]:
    """All decorated bindings of a class, attribute name → bindings.

    Walks the MRO base-first so the most-derived decoration wins; an
    undecorated override keeps the inherited binding (the wiring resolves the
    handler via ``getattr``, which finds the override).
    """
    out: dict[str, tuple[Binding, ...]] = {}
    for klass in reversed(cls.__mro__):
        for name, member in vars(klass).items():
            bindings = getattr(member, BINDINGS_ATTR, None)
            if bindings:
                out[name] = tuple(bindings)
    return out


def collect_metrics(cls: type) -> dict[str, Measurement]:
    """All ``@metric`` declarations of a class, **id** → measurement.

    Keyed by id rather than by attribute name, which is what lets a subclass
    replace a parent's measurement by redeclaring its id on a method of its
    own. Walks the MRO base-first like :func:`collect_bindings`, so an
    undecorated override still inherits the declaration and is the one called.

    Two attributes of *one* class body claiming the same id is a
    :class:`~zenode.ContractError`: only one of them could ever be reported,
    and silently picking is how a measurement goes missing without a message.
    """
    out: dict[str, Measurement] = {}
    for klass in reversed(cls.__mro__):
        declared_here: dict[str, str] = {}
        for name, member in vars(klass).items():
            measurement: Measurement | None = getattr(member, METRICS_ATTR, None)
            if measurement is None:
                continue
            measure_id = measurement.descriptor.id
            clash = declared_here.get(measure_id)
            if clash is not None:
                raise ContractError(
                    f"{klass.__name__}.{name} and {klass.__name__}.{clash} both declare "
                    f"the measurement {measure_id!r}; ids are unique per node"
                )
            declared_here[measure_id] = name
            # A re-decorated attribute replaces its own inherited declaration
            # outright — otherwise changing an id would leave the old one
            # behind, still pointing at the same method.
            for inherited_id, inherited in list(out.items()):
                if inherited.attr == name and inherited_id != measure_id:
                    del out[inherited_id]
            out[measure_id] = replace(measurement, attr=name)
    return out


def collect_publishers(cls: type) -> dict[str, publish[Any]]:
    """All ``publish()`` descriptors of a class, attribute name → descriptor."""
    out: dict[str, publish[Any]] = {}
    for klass in reversed(cls.__mro__):
        for name, member in vars(klass).items():
            if isinstance(member, publish):
                out[name] = member
            elif name in out:
                del out[name]  # overridden by something that is not a publisher
    return out
