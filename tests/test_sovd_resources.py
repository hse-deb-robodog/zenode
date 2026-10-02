"""One node, rendered as SOVD data items.

Ids are collision-free by construction: runtime ids are NodeHealth field
names, measure ids match [a-z][a-z0-9_]*, service ids contain a dot, and the
two presence items carry the vendor prefix.
"""

from __future__ import annotations

from typing import Any

from zenode.msgs import Empty, MeasureDescriptor, NodeHealth, NodeInfo, ServiceInfo
from zenode.sovd.resources import (
    GROUP_APP,
    GROUP_SVC,
    categories,
    data_items,
    groups,
    read_value,
    readable_services,
    service_id,
)
from zenode.sovd.topology import NodeView


def _view(**kw: Any) -> NodeView:
    base: dict[str, Any] = {
        "name": "nav",
        "presence": "live",
        "info": None,
        "health": None,
        "last_seen_s": None,
        "logs": [],
    }
    return NodeView(**{**base, **kw})


def _info(**kw: Any) -> NodeInfo:
    return NodeInfo(node="nav", host="jetson", zenode="0.1.0", **kw)


def _health(**kw: Any) -> NodeHealth:
    return NodeHealth.model_validate(
        {"node": "nav", "host": "jetson", "state": "running", "uptime_s": 6.0, "ts_ns": 1, **kw}
    )


def _read(view: NodeView, data_id: str, namespace: str = "") -> tuple[Any, dict[str, Any]]:
    found = read_value(view, data_id, namespace)
    assert found is not None, data_id
    return found


READ = ServiceInfo(
    key="robodog/state/get_pose",
    request="Empty",
    reply="Pose",
    diagnostic="read",
    description="Where am I.",
    request_schema=Empty.model_json_schema(),
    reply_schema={"type": "object"},
    request_encoding="application/json",
    reply_encoding="application/json",
)
OPERATION = READ.model_copy(update={"key": "robodog/motion/home", "diagnostic": "operation"})
HIDDEN = READ.model_copy(update={"key": "robodog/state/reset", "diagnostic": None})
RAW = READ.model_copy(
    update={"key": "robodog/state/blob", "reply_encoding": "application/octet-stream"}
)


def test_service_ids_strip_the_namespace_and_dot_the_key():
    assert service_id("robodog/state/get_pose", "robodog") == "state.get_pose"
    assert service_id("state/get_pose", "") == "state.get_pose"
    assert service_id("other/state/get_pose", "robodog") == "other.state.get_pose"


def test_only_json_read_services_are_readable():
    view = _view(info=_info(serves=[READ, OPERATION, HIDDEN, RAW]))
    assert list(readable_services(view, "robodog")) == ["state.get_pose"]


def test_data_items_cover_identity_runtime_presence_measures_and_services():
    view = _view(
        info=_info(measures=[MeasureDescriptor(id="battery_soc", unit="1")], serves=[READ]),
        health=_health(measures={"battery_soc": 0.5}),
        last_seen_s=0.4,
    )
    items = {m.id: m for m in data_items(view, "robodog")}
    assert items["node"].category == "identData"
    assert items["sent"].category == "sysInfo" and items["sent"].groups == ["runtime"]
    assert items["state"].groups == ["runtime"]
    assert items["x-zenode-presence"].groups == ["presence"]
    assert items["battery_soc"].groups == [GROUP_APP]
    assert items["state.get_pose"].groups == [GROUP_SVC]
    assert items["state.get_pose"].name == "Where am I."


def test_without_a_heartbeat_only_identity_presence_and_services_are_listed():
    view = _view(info=_info(serves=[READ]))
    ids = {m.id for m in data_items(view, "robodog")}
    assert "sent" not in ids and "node" in ids and "x-zenode-presence" in ids


def test_values_are_wrapped_like_upstream():
    view = _view(
        info=_info(measures=[MeasureDescriptor(id="battery_soc", unit="1", description="SoC")]),
        health=_health(sent=3, measures={"battery_soc": 0.5}),
        last_seen_s=0.4,
        presence="silent",
    )
    assert _read(view, "sent")[0] == {"value": 3}
    assert _read(view, "battery_soc")[0] == {"value": 0.5}
    assert _read(view, "x-zenode-presence")[0] == {"value": "silent"}
    assert _read(view, "x-zenode-last-seen")[0] == {"value": 0.4}
    assert _read(view, "node")[0] == {"value": "nav"}
    _, schema = _read(view, "battery_soc")
    assert schema["properties"]["value"]["type"] == "number"
    assert schema["description"] == "SoC"


def test_unknown_and_absent_values_are_none():
    view = _view(info=_info(), health=_health(cpu_percent=None))
    assert read_value(view, "nope", "") is None
    assert read_value(view, "cpu_percent", "") is None  # unknown is not zero


def test_categories_and_groups_follow_the_items():
    view = _view(info=_info(serves=[READ]), health=_health())
    assert [c.item for c in categories(view, "robodog")] == ["currentData", "identData", "sysInfo"]
    assert [(g.id, g.category) for g in groups(view, "robodog")] == [
        ("identity", "identData"),
        ("presence", "sysInfo"),
        ("runtime", "sysInfo"),
        ("svc", "currentData"),
    ]
