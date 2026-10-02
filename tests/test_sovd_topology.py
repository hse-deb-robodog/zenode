"""The sidecar's one piece of state, and its three presence answers.

Liveliness admits a node; heartbeat age says whether it is answering;
departure is retained for a bounded window so a crashed node is still
diagnosable. Every container has a cap, because a sidecar runs as long as the
robot does.
"""

from __future__ import annotations

from typing import Any

import pytest

from zenode.msgs import LogRecordMsg, NodeHealth, NodeInfo
from zenode.sovd.topology import SILENT_AFTER_BEATS, NodeView, Topology


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _health(node: str = "nav", **kw: Any) -> bytes:
    return (
        NodeHealth.model_validate(
            {"node": node, "state": "running", "uptime_s": 1.0, "ts_ns": 1, **kw}
        )
        .model_dump_json()
        .encode()
    )


def _info(node: str = "nav", **kw: Any) -> bytes:
    return NodeInfo(node=node, **kw).model_dump_json().encode()


def _log(node: str = "nav", message: str = "m") -> bytes:
    return (
        LogRecordMsg(node=node, level="WARNING", logger="x", message=message, ts_ns=1)
        .model_dump_json()
        .encode()
    )


def _view(t: Topology, name: str = "nav") -> NodeView:
    view = t.get(name)
    assert view is not None
    return view


def test_liveliness_admits_a_node(clock):
    t = Topology("", clock=clock)
    assert t.names() == []
    t.presence("nav", True)
    assert t.names() == ["nav"]
    view = _view(t)
    assert view.presence == "live" and view.health is None


def test_a_descriptor_that_arrives_before_liveliness_is_kept_but_not_listed(clock):
    """Latched delivery races the liveliness replay; dropping it would lose it for good."""
    t = Topology("", clock=clock)
    t.offer_info(_info(host="jetson"))
    assert t.names() == []
    assert t.get("nav") is None
    t.presence("nav", True)
    info = _view(t).info
    assert info is not None and info.host == "jetson"


def test_foreign_payloads_are_ignored(clock):
    t = Topology("", clock=clock)
    t.offer_info(b"not json")
    t.offer_health(b'{"unrelated": 1}')
    t.offer_log(b"{}")
    assert t.names() == []


def test_silence_is_three_missed_beats(clock):
    t = Topology("", clock=clock)
    t.presence("nav", True)
    t.offer_info(_info(health_interval=2.0))
    t.offer_health(_health())
    assert _view(t).presence == "live"
    clock.now += SILENT_AFTER_BEATS * 2.0 - 0.1
    assert _view(t).presence == "live"
    clock.now += 0.2
    assert _view(t).presence == "silent"
    assert _view(t).last_seen_s == pytest.approx(6.1)


def test_a_node_without_a_heartbeat_is_never_silent(clock):
    t = Topology("", clock=clock)
    t.presence("nav", True)
    t.offer_info(_info(health_interval=None))
    clock.now += 1000
    assert _view(t).presence == "live"


def test_departure_is_retained_then_dropped(clock):
    t = Topology("", clock=clock, retain_gone=10.0)
    t.presence("nav", True)
    t.offer_health(_health(sent=5))
    t.offer_log(_log())
    t.presence("nav", False)
    view = _view(t)
    assert view.presence == "gone"
    assert view.health is not None and view.health.sent == 5
    assert len(view.logs) == 1
    assert t.names() == ["nav"]
    clock.now += 10.1
    assert t.names() == [] and t.get("nav") is None


def test_a_returning_node_is_live_again_with_its_history(clock):
    t = Topology("", clock=clock)
    t.presence("nav", True)
    t.offer_log(_log())
    t.presence("nav", False)
    t.presence("nav", True)
    assert _view(t).presence == "live" and len(_view(t).logs) == 1


def test_the_departed_set_is_capped_oldest_first(clock):
    t = Topology("", clock=clock, max_gone=2)
    for name in ["a", "b", "c"]:
        t.presence(name, True)
        clock.now += 1
        t.presence(name, False)
    assert t.names() == ["b", "c"]


def test_log_ring_is_bounded(clock):
    t = Topology("", clock=clock, log_buffer=3)
    t.presence("nav", True)
    for i in range(5):
        t.offer_log(_log(message=str(i)))
    assert [r.message for r in _view(t).logs] == ["2", "3", "4"]


def test_views_are_snapshots(clock):
    t = Topology("", clock=clock)
    t.presence("nav", True)
    view = _view(t)
    t.offer_log(_log())
    assert view.logs == []
