"""The sidecar against real nodes in the harness.

One shared in-process session: the harness's. The sidecar runs its own loop
thread on that session, exactly as `zenode sovd` does on its own session.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel

from zenode import Node, Service, TopicSet, metric, serve
from zenode.msgs import Empty
from zenode.sovd import Sidecar, make_server
from zenode.sovd.server import CallsSaturated


class Pose(BaseModel):
    x: float = 0.0


class Svc(TopicSet):
    get_pose = Service("sovdtest/state/get_pose", request=Empty, reply=Pose, diagnostic="read")
    boom = Service("sovdtest/state/boom", request=Empty, reply=Pose, diagnostic="read")
    home = Service("sovdtest/motion/home", request=Empty, reply=Pose, diagnostic="operation")


class Nav(Node):
    name = "nav"
    health_interval = 0.2

    @metric("battery_soc", unit="1")
    def _soc(self) -> float:
        return 0.5

    @serve(Svc.get_pose)
    async def _get_pose(self, _: Empty) -> Pose:
        return Pose(x=1.5)

    @serve(Svc.boom)
    async def _boom(self, _: Empty) -> Pose:
        raise RuntimeError("no fix")

    @serve(Svc.home)
    async def _home(self, _: Empty) -> Pose:
        return Pose()


def _get(url: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else None


async def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def _described(sidecar: Sidecar, name: str) -> bool:
    view = sidecar.topology.get(name)
    return view is not None and view.health is not None and view.info is not None


@pytest.mark.integration
async def test_a_node_is_served_end_to_end(zen):
    nav = await zen.start_node(Nav)
    sidecar = Sidecar(zen.session, zen.namespace, call_timeout=1.0)
    sidecar.start()
    server = make_server(sidecar, "127.0.0.1", 0)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/sovd/v1"
    try:
        await _wait_for(lambda: _described(sidecar, "nav"))

        status, body = await asyncio.to_thread(_get, f"{base}/components")
        assert status == 200 and body["items"][0]["id"] == "nav"

        _, body = await asyncio.to_thread(_get, f"{base}/components/nav/data/battery_soc")
        assert body["data"] == {"value": 0.5}

        _, body = await asyncio.to_thread(
            _get, f"{base}/components/nav/data/sovdtest.state.get_pose"
        )
        assert body == {"id": "sovdtest.state.get_pose", "data": {"x": 1.5}}

        status, body = await asyncio.to_thread(
            _get, f"{base}/components/nav/data/sovdtest.state.boom"
        )
        assert status == 502 and "no fix" in body["message"]

        status, _ = await asyncio.to_thread(
            _get, f"{base}/components/nav/data/sovdtest.motion.home"
        )
        assert status == 404  # an operation is not a data resource

        await zen.stop_node(nav)
        await _wait_for(
            lambda: (v := sidecar.topology.get("nav")) is not None and v.presence == "gone"
        )
        _, body = await asyncio.to_thread(_get, f"{base}/components/nav/data/x-zenode-presence")
        assert body["data"] == {"value": "gone"}
        status, _ = await asyncio.to_thread(
            _get, f"{base}/components/nav/data/sovdtest.state.get_pose"
        )
        assert status == 504
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        sidecar.stop()


@pytest.mark.integration
async def test_the_call_cap_is_enforced(zen):
    await zen.start_node(Nav)
    sidecar = Sidecar(zen.session, zen.namespace, max_calls=0)
    sidecar.start()
    try:
        await _wait_for(lambda: "nav" in sidecar.topology.names())
        with pytest.raises(CallsSaturated):
            await asyncio.to_thread(sidecar.call, "sovdtest/state/get_pose")
    finally:
        sidecar.stop()
