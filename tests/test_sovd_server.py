"""The router, against a fake call bridge: routes, shapes, status codes.

No zenoh here. What is tested is that the HTTP surface matches upstream's at
the pinned commit and that every failure the design maps to a status lands on
that status with upstream's error body.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from zenode.errors import ServiceError, ServiceTimeout
from zenode.msgs import Empty, LogRecordMsg, MeasureDescriptor, NodeHealth, NodeInfo, ServiceInfo
from zenode.sovd.server import CallsSaturated, make_server, normalize_base
from zenode.sovd.topology import Topology

READ = ServiceInfo(
    key="robodog/state/get_pose",
    diagnostic="read",
    description="Where am I.",
    request_schema=Empty.model_json_schema(),
    reply_schema={"type": "object", "properties": {"x": {"type": "number"}}},
    request_encoding="application/json",
    reply_encoding="application/json",
)


class FakeBridge:
    def __init__(self, topology: Topology) -> None:
        self.topology = topology
        self.namespace = "robodog"
        self.vendor_version = "0.1-test"
        self.replies: dict[str, bytes | Exception] = {}
        self.calls: list[str] = []

    def call(self, key: str) -> bytes:
        self.calls.append(key)
        reply = self.replies[key]
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def world() -> Iterator[tuple[str, FakeBridge]]:
    topology = Topology("robodog", clock=lambda: 100.0)
    topology.presence("nav", True)
    topology.offer_info(
        NodeInfo(
            node="nav",
            host="jetson",
            zenode="0.1.0",
            health_interval=2.0,
            measures=[MeasureDescriptor(id="battery_soc", unit="1")],
            serves=[READ],
        )
        .model_dump_json()
        .encode()
    )
    topology.offer_health(
        NodeHealth.model_validate(
            {
                "node": "nav",
                "state": "running",
                "uptime_s": 6.0,
                "ts_ns": 1,
                "sent": 3,
                "measures": {"battery_soc": 0.5},
            }
        )
        .model_dump_json()
        .encode()
    )
    topology.offer_log(
        LogRecordMsg(node="nav", level="WARNING", logger="l", message="hot", ts_ns=1)
        .model_dump_json()
        .encode()
    )
    bridge = FakeBridge(topology)
    server = make_server(bridge, "127.0.0.1", 0, base_uri="/sovd")
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", bridge
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(url: str, **headers: str) -> tuple[int, Any, dict[str, str]]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None), dict(e.headers)


def test_normalize_base():
    assert normalize_base("") == "" and normalize_base("/") == ""
    assert normalize_base("sovd") == "/sovd" and normalize_base("/sovd/") == "/sovd"


def test_version_info_sits_outside_v1(world):
    base, _ = world
    status, body, headers = _get(f"{base}/sovd/version-info")
    assert status == 200 and headers["Content-Type"] == "application/json"
    (info,) = body["sovd_info"]
    assert info["version"] == "1.1"
    assert info["base_uri"] == f"{base}/sovd/v1"
    assert info["vendor_info"] == {"version": "0.1-test", "name": "zenode"}
    assert _get(f"{base}/sovd/v1/version-info")[0] == 404


def test_root_links_only_the_collections_that_exist(world):
    base, _ = world
    status, body, _ = _get(f"{base}/sovd/v1")
    assert status == 200
    assert body == {"id": "", "name": "", "components": f"{base}/sovd/v1/components"}
    assert _get(f"{base}/sovd/v1/")[1] == body


def test_components_list_and_capabilities(world):
    base, _ = world
    _, body, _ = _get(f"{base}/sovd/v1/components")
    assert body == {
        "items": [{"id": "nav", "name": "nav", "href": f"{base}/sovd/v1/components/nav"}]
    }
    _, caps, _ = _get(f"{base}/sovd/v1/components/nav")
    assert caps == {
        "id": "nav",
        "name": "nav",
        "data": f"{base}/sovd/v1/components/nav/data",
        "hosts": f"{base}/sovd/v1/components/nav/hosts",
        "x-zenode-logs": f"{base}/sovd/v1/components/nav/x-zenode-logs",
    }
    assert _get(f"{base}/sovd/v1/components/nav/hosts")[1] == {"items": []}
    assert _get(f"{base}/sovd/v1/components/nav/belongs-to")[1] == {"items": []}


def test_apps_and_areas_are_empty_collections(world):
    base, _ = world
    assert _get(f"{base}/sovd/v1/apps")[1] == {"items": []}
    assert _get(f"{base}/sovd/v1/areas")[1] == {"items": []}
    assert _get(f"{base}/sovd/v1/apps/x")[0] == 404


def test_unknown_component_is_a_vendor_specific_404(world):
    base, _ = world
    status, body, _ = _get(f"{base}/sovd/v1/components/ghost")
    assert status == 404
    assert body == {
        "error_code": "vendor-specific",
        "vendor_code": "entity-not-found",
        "message": "Entity not found: ghost",
    }


def test_data_list_categories_groups_and_filters(world):
    base, _ = world
    _, body, _ = _get(f"{base}/sovd/v1/components/nav/data")
    ids = {m["id"] for m in body["items"]}
    assert {"node", "sent", "state", "battery_soc", "state.get_pose", "x-zenode-presence"} <= ids
    _, by_group, _ = _get(f"{base}/sovd/v1/components/nav/data?groups=app")
    assert [m["id"] for m in by_group["items"]] == ["battery_soc"]
    _, by_cat, _ = _get(f"{base}/sovd/v1/components/nav/data?categories=identData")
    assert {m["id"] for m in by_cat["items"]} == {"node", "host", "zenode"}
    _, both, _ = _get(f"{base}/sovd/v1/components/nav/data?groups=app&categories=identData")
    assert [m["id"] for m in both["items"]] == ["battery_soc"]  # groups win, as upstream
    _, cats, _ = _get(f"{base}/sovd/v1/components/nav/data-categories?include-schema=true")
    assert cats == {"items": [{"item": "currentData"}, {"item": "identData"}, {"item": "sysInfo"}]}
    _, grps, _ = _get(f"{base}/sovd/v1/components/nav/data-groups?category=sysInfo")
    assert grps == {
        "items": [
            {"id": "presence", "category": "sysInfo"},
            {"id": "runtime", "category": "sysInfo"},
        ]
    }


def test_include_schema_is_a_sibling_and_must_be_boolean(world):
    base, _ = world
    _, body, _ = _get(f"{base}/sovd/v1/components?include-schema=true")
    assert set(body) == {"items", "schema"} and body["schema"]["type"] == "object"
    status, err, _ = _get(f"{base}/sovd/v1/components?include-schema=maybe")
    assert status == 400 and err == {"error_code": "incomplete-request", "message": "Bad request"}
    assert _get(f"{base}/sovd/v1/components?unknown=1")[0] == 200


def test_reading_a_runtime_value_and_a_measure(world):
    base, _ = world
    _, body, _ = _get(f"{base}/sovd/v1/components/nav/data/sent")
    assert body == {"id": "sent", "data": {"value": 3}}
    _, body, _ = _get(f"{base}/sovd/v1/components/nav/data/battery_soc?include-schema=true")
    assert body["data"] == {"value": 0.5}
    assert body["schema"]["properties"]["value"]["type"] == "number"


def test_unknown_data_id_is_an_error_response_404(world):
    base, _ = world
    status, body, _ = _get(f"{base}/sovd/v1/components/nav/data/nope")
    assert status == 404 and body == {"error_code": "error-response", "message": "not found: nope"}


def test_reading_a_service_calls_the_node(world):
    base, bridge = world
    bridge.replies["robodog/state/get_pose"] = b'{"x": 1.5}'
    _, body, _ = _get(f"{base}/sovd/v1/components/nav/data/state.get_pose?include-schema=true")
    assert body == {"id": "state.get_pose", "data": {"x": 1.5}, "schema": READ.reply_schema}
    assert bridge.calls == ["robodog/state/get_pose"]


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (ServiceTimeout("no reply"), 504, "not-responding"),
        (ServiceError("robodog/state/get_pose: boom"), 502, "error-response"),
        (CallsSaturated(), 503, "sovd-server-failure"),
    ],
)
def test_call_failures_map_to_statuses(world, failure, status, code):
    base, bridge = world
    bridge.replies["robodog/state/get_pose"] = failure
    got, body, _ = _get(f"{base}/sovd/v1/components/nav/data/state.get_pose")
    assert got == status and body["error_code"] == code


def test_an_undecodable_reply_is_a_502(world):
    base, bridge = world
    bridge.replies["robodog/state/get_pose"] = b"not json"
    assert _get(f"{base}/sovd/v1/components/nav/data/state.get_pose")[0] == 502


def test_a_gone_node_is_not_called(world):
    base, bridge = world
    bridge.topology.presence("nav", False)
    status, body, _ = _get(f"{base}/sovd/v1/components/nav/data/state.get_pose")
    assert status == 504 and body["error_code"] == "not-responding"
    assert bridge.calls == []
    _, body, _ = _get(f"{base}/sovd/v1/components/nav/data/x-zenode-presence")
    assert body["data"] == {"value": "gone"}


def test_logs_under_the_vendor_link(world):
    base, _ = world
    _, body, _ = _get(f"{base}/sovd/v1/components/nav/x-zenode-logs")
    assert body["items"][0]["message"] == "hot" and body["items"][0]["node"] == "nav"


def test_unknown_paths_and_methods(world):
    base, _ = world
    status, body, _ = _get(f"{base}/sovd/v1/nope")
    assert status == 404 and body is None
    assert _get(f"{base}/other/v1/components")[0] == 404
    req = urllib.request.Request(f"{base}/sovd/v1/components", method="PUT")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 405 and e.value.headers["Allow"] == "GET"


def test_hrefs_follow_the_host_header(world):
    base, _ = world
    _, body, _ = _get(f"{base}/sovd/v1/components", Host="robot:7690")
    assert body["items"][0]["href"] == "http://robot:7690/sovd/v1/components/nav"
