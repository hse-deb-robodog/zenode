"""The latched node descriptor: keys, the node-side wiring, and late joiners.

What the descriptor *contains* and when it is republished is the reporter's
protocol, tested through its interface in ``test_reporting.py``. This module
keeps the contract half (key derivation) and the node half: that ``__init__``
feeds the reporter the right host, and that a real node's descriptor reaches
a late subscriber from the latched cache.
"""

from __future__ import annotations

import asyncio
import socket
import time

import pytest
from conftest import internals
from pydantic import BaseModel

from zenode import Node, Service, Topic, TransportConfig, metric, publish, subscribe
from zenode.msgs import NodeInfo, info_key, info_pattern
from zenode.msgs.health import health_key
from zenode.msgs.log import log_key
from zenode.msgs.trace import trace_key


class Ping(BaseModel):
    value: int = 0


CMD = Topic("cmd/vel", Ping, latched=True, history=3, priority="real_time")
STATE = Topic("state/x", Ping, max_age=0.2)
SUM = Service("svc/sum", request=Ping, reply=Ping)


# ------------------------------------------------------------------ key derivation


def test_info_key_is_relative():
    assert info_key("nav") == "node/nav/info"


def test_pattern_matches_the_key_it_derives_from():
    """The publisher and the CLI must not be able to drift apart."""
    assert info_pattern("") == "node/*/info"
    assert info_pattern("robodog") == "robodog/node/*/info"
    assert info_pattern("robodog").replace("*", "nav") == f"robodog/{info_key('nav')}"


def test_the_reserved_keys_do_not_collide():
    """`node/<name>/**` is one namespace shared by four builders."""
    keys = {health_key("nav"), log_key("nav"), trace_key("nav"), info_key("nav")}
    assert len(keys) == 4


# --------------------------------------------------------------------------- node


class Described(Node):
    name = "described"
    health_interval = 0.05

    out = publish(CMD)

    @subscribe(STATE)
    async def on_state(self, msg: Ping) -> None: ...

    async def on_sum(self, req: Ping) -> Ping:
        return req

    async def on_start(self) -> None:
        self.serve(SUM, self.on_sum)

    @metric("battery_soc", unit="1", description="Pack state of charge.")
    def _soc(self) -> float:
        return 0.87

    @metric("frames_processed", unit="{frame}", kind="counter", integral=True)
    def _frames(self) -> int:
        return 4


# These two read the reporter through the sanctioned `internals()` hatch: what
# they assert is `Node.__init__`'s wiring — which host reaches the sources —
# not the reporter's own behavior, which has its interface tests.


def test_host_defaults_to_the_machines_own_name():
    reporter = internals(Described()).reporter
    assert reporter.build_info().host == socket.gethostname()


def test_the_transport_section_overrides_the_hostname():
    reporter = internals(Described(transport=TransportConfig(host="jetson"))).reporter
    assert reporter.build_info().host == "jetson"
    assert reporter.build_health().host == "jetson"


# ------------------------------------------------------------------- late joiners


@pytest.mark.integration
async def test_a_late_joiner_receives_the_descriptor_from_the_latched_cache(zen):
    """The point of latching: subscribe an hour later, still get the catalog."""
    await zen.start_node(Described)
    await _settle()

    out = zen.collect(Topic(info_key("described"), NodeInfo, latched=True))
    info = await out.next()
    assert {m.id for m in info.measures} == {"battery_soc", "frames_processed"}
    assert any(e.key == "cmd/vel" for e in info.publishes)
    assert any(e.key == "state/x" for e in info.subscribes)
    assert any(s.key == "svc/sum" and s.request == "Ping" for s in info.serves)


@pytest.mark.integration
async def test_a_late_subscription_republishes_exactly_once(zen):
    """`subscribe()` is public API a handler may call long after start."""
    node = await zen.start_node(Described)
    collected: list[NodeInfo] = []
    zen.subscribe(Topic(info_key("described"), NodeInfo, latched=True), collected.append)
    await _settle()
    collected.clear()

    node.subscribe(Topic("late/topic", Ping), lambda msg: None)
    await _settle()
    assert len(collected) == 1
    assert any(e.key == "late/topic" for e in collected[0].subscribes)

    collected.clear()
    await _settle()
    assert collected == []  # nothing changed, so nothing is republished


async def _settle(beats: int = 4) -> None:
    """Long enough for `health_interval = 0.05` to tick a few times."""
    deadline = time.monotonic() + beats * 0.05 + 0.2
    while time.monotonic() < deadline:
        await asyncio.sleep(0.02)
