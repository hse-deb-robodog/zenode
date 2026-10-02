"""The reporter, tested through its interface: fake sources in, messages out.

Every test here constructs a :class:`~zenode.reporting.Reporter` with plain
fakes and asserts on what it builds or puts — no node, no session, and no
reaching into name-mangled privates. The node-side wiring (that ``start()``
attaches the real publishers and the health timer calls ``beat()``) is what
``test_integration.py`` proves.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel

from zenode import Service, Topic
from zenode.declarative import Measurement
from zenode.metrics import Latency, ProcessStats
from zenode.msgs import Empty, MeasureDescriptor
from zenode.msgs.health import RUNTIME_MEASURES
from zenode.reporting import Reporter, ReporterSources, _entity_info
from zenode.topic import topic_flags


class Ping(BaseModel):
    value: int = 0


CMD = Topic("cmd/vel", Ping, latched=True, history=3, priority="real_time")
STATE = Topic("state/x", Ping, max_age=0.2)
SUM = Service("svc/sum", request=Ping, reply=Ping)


# --------------------------------------------------------------------- fakes


class _FakePublisher:
    def __init__(self, topic: Topic[Any], key: str) -> None:
        self.topic = topic
        self.key = key
        self.sent = 0
        self.errors = 0


class _FakeSubscription:
    def __init__(self, topic: Topic[Any], key: str) -> None:
        self.topic = topic
        self.key = key
        self.received = 0
        self.dropped = 0
        self.stale = 0
        self.errors = 0
        self.deadline_misses = 0
        self.queue_peak = 0
        self.age = Latency()
        self.handler_time = Latency()


class _FakeServer:
    def __init__(self, service: Service[Any, Any], key: str) -> None:
        self.service = service
        self.key = key
        self.errors = 0
        self.handler_time = Latency()


class _FakeTimer:
    def __init__(self, *, errors: int = 0, overruns: int = 0) -> None:
        self.errors = errors
        self.overruns = overruns


class _Put:
    """Stands in for a publisher's ``put``: the reporter only ever calls it.

    ``fail`` makes the first N calls raise — a session that went away and
    came back, which is exactly the situation the retry protocol exists for.
    """

    def __init__(self, *, fail: int = 0) -> None:
        self.sent: list[Any] = []
        self._fail = fail

    def __call__(self, msg: Any) -> None:
        if self._fail:
            self._fail -= 1
            raise RuntimeError("the session went away")
        self.sent.append(msg)


def sources(
    *, measured: dict[str, Callable[[], Any]] | None = None, **overrides: Any
) -> ReporterSources:
    """Minimal fully-faked sources; ``measured`` maps attr names to callables."""
    values = measured or {}
    defaults: dict[str, Any] = {
        "name": "nav",
        "host": "jetson",
        "zenode_version": "0.0-test",
        "health_interval": 2.0,
        "state": lambda: "running",
        "publishers": [],
        "subscriptions": [],
        "servers": [],
        "timers": [],
        "log_dropped": lambda: 0,
        "shm_fallbacks": lambda: 0,
        "process": ProcessStats(),
        "metrics": {},
        "resolve": lambda attr: values[attr],
        "log": logging.getLogger("test.reporter"),
    }
    defaults.update(overrides)
    return ReporterSources(**defaults)


def reporter(**kwargs: Any) -> Reporter:
    return Reporter(sources(**kwargs))


# ------------------------------------------------------------------ sampling


def test_sampled_values_reach_the_heartbeats_measures():
    r = reporter(
        metrics={
            "battery_soc": Measurement(MeasureDescriptor(id="battery_soc"), attr="_soc"),
            "frames": Measurement(MeasureDescriptor(id="frames"), attr="_frames"),
        },
        measured={"_soc": lambda: 0.5, "_frames": lambda: 7},
    )
    assert r.build_health().measures == {"battery_soc": 0.5, "frames": 7.0}


def test_none_is_absent_rather_than_zero():
    """Unknown is not zero — the promise `cpu_percent` already makes."""
    r = reporter(
        metrics={"soc": Measurement(MeasureDescriptor(id="soc"), attr="_soc")},
        measured={"_soc": lambda: None},
    )
    health = r.build_health()
    assert health.measures == {}
    assert health.handler_errors == 0


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_value_is_omitted_but_is_not_an_error(bad):
    """A NaN serializes as invalid OTLP JSON and takes a whole push with it."""
    r = reporter(
        metrics={"wobble": Measurement(MeasureDescriptor(id="wobble"), attr="_wobble")},
        measured={"_wobble": lambda: bad},
    )
    health = r.build_health()
    assert health.measures == {}
    assert health.handler_errors == 0  # not finite is unknown, not a bug


def _raising() -> float:
    raise RuntimeError("the BMS is not answering")


def test_a_raising_measurement_is_counted_omitted_and_cumulative():
    r = reporter(
        metrics={
            "soc": Measurement(MeasureDescriptor(id="soc"), attr="_soc"),
            "frames": Measurement(MeasureDescriptor(id="frames"), attr="_frames"),
        },
        measured={"_soc": _raising, "_frames": lambda: 3},
    )
    health = r.build_health()
    assert health.measures == {"frames": 3.0}  # one bad measurement, not all
    assert health.handler_errors == 1
    assert r.build_health().handler_errors == 2  # cumulative, like every counter


def test_a_non_numeric_return_is_counted_rather_than_crashing_the_beat():
    r = reporter(
        metrics={"soc": Measurement(MeasureDescriptor(id="soc"), attr="_soc")},
        measured={"_soc": lambda: "0.87 volts"},
    )
    health = r.build_health()
    assert health.measures == {}
    assert health.handler_errors == 1


# --------------------------------------------------------------- aggregation


def test_counters_are_summed_across_everything_the_node_wired():
    pub = _FakePublisher(CMD, "cmd/vel")
    pub.sent, pub.errors = 11, 1
    sub = _FakeSubscription(STATE, "state/x")
    sub.received, sub.dropped, sub.stale = 5, 2, 1
    sub.errors, sub.deadline_misses, sub.queue_peak = 1, 3, 9
    other = _FakeSubscription(STATE, "state/y")
    other.received, other.queue_peak = 4, 4
    srv = _FakeServer(SUM, "svc/sum")
    srv.errors = 1
    timer = _FakeTimer(errors=1, overruns=6)

    health = reporter(
        publishers=[pub],
        subscriptions=[sub, other],
        servers=[srv],
        timers=[timer],
        log_dropped=lambda: 8,
        shm_fallbacks=lambda: 2,
    ).build_health()

    assert (health.node, health.host, health.state) == ("nav", "jetson", "running")
    assert health.sent == 11
    assert health.received == 9
    assert health.dropped == 2
    assert health.stale == 1
    assert health.handler_errors == 4  # sub + server + timer + publisher
    assert health.timer_overruns == 6
    assert health.deadline_misses == 3
    assert health.logs_dropped == 8
    assert health.shm_fallbacks == 2
    assert health.queue_max_depth == 9


def test_latencies_are_summarized_and_the_beat_resets_the_window():
    """Each heartbeat covers the interval since the last, so `beat()` resets."""
    sub = _FakeSubscription(STATE, "state/x")
    sub.age.observe(0.010)
    sub.age.observe(0.030)
    sub.handler_time.observe(0.002)
    sub.queue_peak = 5
    srv = _FakeServer(SUM, "svc/sum")
    srv.handler_time.observe(0.004)

    r = reporter(subscriptions=[sub], servers=[srv])
    put_health, put_info = _Put(), _Put()
    r.attach(put_health, put_info)
    r.beat()

    health = put_health.sent[0]
    assert (health.age_mean_ms, health.age_max_ms) == (20.0, 30.0)
    assert (health.handler_mean_ms, health.handler_max_ms) == (3.0, 4.0)
    assert health.queue_max_depth == 5
    assert sub.age.count == 0
    assert sub.handler_time.count == 0
    assert srv.handler_time.count == 0
    assert sub.queue_peak == 0


# ---------------------------------------------------------------- descriptor


def test_the_descriptor_names_the_node_its_host_and_its_version():
    info = reporter().build_info()
    assert (info.node, info.host, info.zenode) == ("nav", "jetson", "0.0-test")


def test_the_host_reaches_the_heartbeat_and_the_descriptor():
    """Both messages carry it, so neither consumer has to join against the other."""
    r = reporter()
    assert r.build_health().host == "jetson"
    assert r.build_info().host == "jetson"


def test_the_measure_catalog_is_the_declared_descriptors_verbatim():
    """No field-by-field copy: what ``@metric`` stamped is what goes out."""
    soc = MeasureDescriptor(id="battery_soc", unit="1", description="Pack state of charge.")
    frames = MeasureDescriptor(id="frames_processed", kind="counter", integral=True)
    r = reporter(
        metrics={
            "battery_soc": Measurement(soc, attr="_soc"),
            "frames_processed": Measurement(frames, attr="_frames"),
        }
    )
    assert r.build_info().measures == [soc, frames]
    assert r.build_info().measures[0] is soc


def test_the_descriptor_lists_what_the_node_wired():
    info = reporter(
        publishers=[_FakePublisher(CMD, "cmd/vel")],
        subscriptions=[_FakeSubscription(STATE, "state/x")],
        servers=[_FakeServer(SUM, "svc/sum")],
    ).build_info()
    assert [e.key for e in info.publishes] == ["cmd/vel"]
    assert [e.key for e in info.subscribes] == ["state/x"]
    assert info.serves[0].key == "svc/sum"
    assert (info.serves[0].request, info.serves[0].reply) == ("Ping", "Ping")


def test_an_entity_carries_the_resolved_key_the_schema_and_the_flags():
    """The resolved key is the point: a namespace mismatch is what this shows."""
    entity = _entity_info(CMD, CMD.resolve("robodog"))
    assert entity.key == "robodog/cmd/vel"
    assert entity.schema_name == "Ping"
    assert entity.flags == ["latched(3)", "prio=real_time"]


def test_entity_flags_come_from_the_same_function_zenode_topics_renders():
    """Two renderings of one topic's semantics would drift; there is only one."""
    assert _entity_info(STATE, "state/x").flags == topic_flags(STATE) == ["max_age=0.2"]


# ------------------------------------------------------------------ protocol


def test_an_unattached_reporter_does_nothing_and_does_not_raise():
    """`health_interval = None`: constructed, never attached, never publishes."""
    r = reporter()
    r.mark_declared()
    r.beat()
    r.publish_info()


def test_the_descriptor_is_not_published_before_start_finishes_wiring():
    """The health timer already ticks while `on_start` runs; a descriptor taken
    then would be missing every binding `_wire_bindings` has not created yet."""
    r = reporter()
    put_health, put_info = _Put(), _Put()
    r.attach(put_health, put_info)
    r.mark_declared()
    r.beat()
    assert put_health.sent  # the heartbeat is deliberately already ticking
    assert put_info.sent == []
    r.publish_info()  # start()'s first snapshot
    assert len(put_info.sent) == 1


def test_a_failed_publish_does_not_propagate_and_is_retried_by_the_beat():
    """A descriptor is diagnostics; "degrade, never crash" applies — but the
    failure `start()` swallows must not silence the node for its lifetime."""
    r = reporter()
    put_health, put_info = _Put(), _Put(fail=2)
    r.attach(put_health, put_info)

    r.publish_info()  # start()'s first snapshot: fails, swallowed
    assert put_info.sent == []

    r.beat()  # still failing: swallowed, the heartbeat is unaffected
    assert len(put_health.sent) == 1
    assert put_info.sent == []

    r.beat()  # the session came back: the retry lands
    assert len(put_info.sent) == 1

    r.beat()
    assert len(put_info.sent) == 1  # and it does not republish for ever after


def test_a_wiring_change_republishes_exactly_once():
    """`subscribe()` is public API a handler may call long after start."""
    r = reporter()
    put_health, put_info = _Put(), _Put()
    r.attach(put_health, put_info)
    r.publish_info()

    r.mark_declared()
    r.beat()
    assert len(put_info.sent) == 2

    r.beat()
    assert len(put_info.sent) == 2  # nothing changed, so nothing is republished


# ------------------------------------------------------------- descriptor: services


GET_POSE = Service(
    "state/get_pose", request=Empty, reply=Ping, diagnostic="read", description="Where am I."
)


def test_the_descriptor_says_how_a_service_may_be_called():
    r = reporter(
        servers=[
            _FakeServer(GET_POSE, "robodog/state/get_pose"),
            _FakeServer(SUM, "robodog/svc/sum"),
        ]
    )
    by_key = {s.key: s for s in r.build_info().serves}
    assert by_key["robodog/state/get_pose"].diagnostic == "read"
    assert by_key["robodog/state/get_pose"].description == "Where am I."
    assert by_key["robodog/svc/sum"].diagnostic is None


def test_the_descriptor_carries_both_schemas():
    """A consumer that never imports the contract still knows the shapes."""
    r = reporter(servers=[_FakeServer(SUM, "robodog/svc/sum")])
    (info,) = r.build_info().serves
    assert info.request_schema == Ping.model_json_schema()
    assert info.reply_schema == Ping.model_json_schema()
    assert info.request_encoding == "application/json"
    assert info.reply_encoding == "application/json"


def test_a_raw_service_has_no_schema_but_names_its_encoding():
    raw = Service("svc/blob", request=bytes, reply=bytes)
    r = reporter(servers=[_FakeServer(raw, "robodog/svc/blob")])
    (info,) = r.build_info().serves
    assert info.request_schema == {}
    assert info.request_encoding == "application/octet-stream"


# -------------------------------------------------------- descriptor: runtime catalog


def test_the_descriptor_carries_the_runtime_catalog():
    """Every heartbeat field is described on the bus, like `measures` already is."""
    info = reporter().build_info()
    assert info.runtime == list(RUNTIME_MEASURES)


def test_the_descriptor_carries_the_health_interval():
    assert reporter(health_interval=2.0).build_info().health_interval == 2.0
    assert reporter(health_interval=None).build_info().health_interval is None
