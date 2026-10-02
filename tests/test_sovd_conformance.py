"""Every response the sidecar emits, validated against opensovd-models.

The schemas under tests/sovd/schemas were generated once from the Rust models
at the commit named in tests/sovd/PIN. The traversal mirrors upstream's
test_api.py: version-info, root, components, each component's capabilities,
categories, groups, data list, each data item; then apps and areas. It never
follows hrefs; it rebuilds paths from ids, as upstream does. The harness's own
probe node is a second component with no heartbeat, which is a useful extra:
every listed item must still read.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import BaseModel

from zenode import Node, Service, TopicSet, metric, serve
from zenode.msgs import Empty
from zenode.sovd import Sidecar, make_server

SCHEMAS = Path(__file__).parent / "sovd" / "schemas"


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.json").read_text())


class Pose(BaseModel):
    x: float = 0.0


class Svc(TopicSet):
    get_pose = Service("conf/state/get_pose", request=Empty, reply=Pose, diagnostic="read")


class Probe(Node):
    name = "probe"
    health_interval = 0.2

    @metric("frames", unit="{frame}", kind="counter", integral=True)
    def _frames(self) -> int:
        return 7

    @serve(Svc.get_pose)
    async def _get_pose(self, _: Empty) -> Pose:
        return Pose(x=2.0)


def _get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as r:
        assert r.status == 200, url
        return json.loads(r.read())


def _validate(body: dict[str, Any], name: str) -> None:
    jsonschema.validate(body, _schema(name))


@pytest.mark.integration
async def test_every_response_validates_against_the_pinned_models(zen):
    await zen.start_node(Probe)
    sidecar = Sidecar(zen.session, zen.namespace)
    sidecar.start()
    server = make_server(sidecar, "127.0.0.1", 0)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}/sovd"
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            view = sidecar.topology.get("probe")
            if view is not None and view.health is not None and view.info is not None:
                break
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)

        def traverse() -> None:
            _validate(_get(f"{root}/version-info"), "version_info")
            caps = _get(f"{root}/v1")
            _validate(caps, "entity_capabilities")
            assert "components" in caps
            components = _get(f"{root}/v1/components?include-schema=true")
            _validate(components, "entities")
            for ref in components["items"]:
                cid = ref["id"]
                here = f"{root}/v1/components/{cid}"
                caps = _get(here)
                _validate(caps, "entity_capabilities")
                assert caps["id"] == cid and "data" in caps
                _validate(_get(f"{here}/data-categories"), "data_categories")
                _validate(_get(f"{here}/data-groups"), "data_groups")
                data = _get(f"{here}/data?include-schema=true")
                _validate(data, "data_list")
                if cid == "probe":
                    ids = {m["id"] for m in data["items"]}
                    assert ids >= {"frames", "conf.state.get_pose", "sent"}
                for item in data["items"]:
                    body = _get(f"{here}/data/{item['id']}?include-schema=true")
                    _validate(body, "read_response")
                    jsonschema.validate(body["data"], body["schema"])  # upstream's is_data_item leg
                _validate(_get(f"{here}/hosts"), "entities")
                _validate(_get(f"{here}/belongs-to"), "entities")
            _validate(_get(f"{root}/v1/apps"), "entities")
            _validate(_get(f"{root}/v1/areas"), "entities")

        await asyncio.to_thread(traverse)

        def error_shape() -> None:
            try:
                urllib.request.urlopen(f"{root}/v1/components/ghost", timeout=5)
            except urllib.error.HTTPError as e:
                _validate(json.loads(e.read()), "generic_error")
                return
            raise AssertionError("expected 404")

        await asyncio.to_thread(error_shape)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        sidecar.stop()
