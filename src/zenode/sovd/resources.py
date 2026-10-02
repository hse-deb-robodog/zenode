"""One node's record, as SOVD data items.

Three sources and five groups, all declared by the node or the runtime, none
scraped (design note §5). The ids cannot collide: runtime ids are
``NodeHealth`` field names, measure ids match ``[a-z][a-z0-9_]*``, service ids
always contain a ``.``, and the two sidecar-derived presence items carry the
``x-zenode-`` prefix SOVD reserves for vendors.

Values are wrapped as ``{"value": …}`` because that is what upstream's bundled
provider emits for a scalar, and a client written against it expects the
wrapper. A service reply is an object already and is served as-is by the
router; this module only lists it.
"""

from __future__ import annotations

from typing import Any, get_args

from ..msgs import NodeState, ServiceInfo
from ..msgs.health import RUNTIME_BY_ID
from .model import DataCategoryInformation, Group, Metadata
from .topology import NodeView

IDENT_DATA = "identData"
SYS_INFO = "sysInfo"
CURRENT_DATA = "currentData"

GROUP_IDENTITY = "identity"
GROUP_RUNTIME = "runtime"
GROUP_PRESENCE = "presence"
GROUP_APP = "app"
GROUP_SVC = "svc"

JSON = "application/json"

_IDENTITY = ("node", "host", "zenode")
_PRESENCE = ("x-zenode-presence", "x-zenode-last-seen")
_STATE_SCHEMA: dict[str, Any] = {"type": "string", "enum": list(get_args(NodeState))}


def service_id(key: str, namespace: str) -> str:
    """``robodog/state/get_pose`` → ``state.get_pose``.

    The descriptor carries resolved keys; the namespace is the sidecar's own
    and says nothing about the service, so it is stripped. A key outside the
    namespace keeps its full path, dotted, so it stays distinct.
    """
    if namespace and key.startswith(f"{namespace}/"):
        key = key[len(namespace) + 1 :]
    return key.replace("/", ".")


def readable_services(view: NodeView, namespace: str) -> dict[str, ServiceInfo]:
    """Data id → descriptor for every service the sidecar may GET.

    JSON both ways, because the sidecar passes bytes through and cannot
    translate anything else; a read with another codec is listed nowhere
    rather than served wrongly.
    """
    if view.info is None:
        return {}
    return {
        service_id(s.key, namespace): s
        for s in view.info.serves
        if s.diagnostic == "read" and s.request_encoding == JSON and s.reply_encoding == JSON
    }


def _wrap(value: Any, schema: dict[str, Any], description: str = "") -> tuple[Any, dict[str, Any]]:
    wrapped: dict[str, Any] = {
        "type": "object",
        "properties": {"value": schema},
        "required": ["value"],
    }
    if description:
        wrapped["description"] = description
    return {"value": value}, wrapped


def data_items(view: NodeView, namespace: str) -> list[Metadata]:
    items: list[Metadata] = [
        Metadata(id=i, name=i, category=IDENT_DATA, groups=[GROUP_IDENTITY]) for i in _IDENTITY
    ]
    if view.health is not None:
        items.extend(
            Metadata(id=d.id, name=d.id, category=SYS_INFO, groups=[GROUP_RUNTIME])
            for d in RUNTIME_BY_ID.values()
            if getattr(view.health, d.id) is not None
        )
        items.append(Metadata(id="state", name="state", category=SYS_INFO, groups=[GROUP_RUNTIME]))
    items.extend(
        Metadata(id=i, name=i, category=SYS_INFO, groups=[GROUP_PRESENCE]) for i in _PRESENCE
    )
    if view.info is not None and view.health is not None:
        items.extend(
            Metadata(id=d.id, name=d.id, category=CURRENT_DATA, groups=[GROUP_APP])
            for d in view.info.measures
            if d.id in view.health.measures
        )
    items.extend(
        Metadata(id=i, name=s.description or i, category=CURRENT_DATA, groups=[GROUP_SVC])
        for i, s in readable_services(view, namespace).items()
    )
    return items


def read_value(view: NodeView, data_id: str, namespace: str) -> tuple[Any, dict[str, Any]] | None:
    """The wrapped value and its schema for any non-service item, else ``None``.

    ``None`` for an unknown id and for a known one whose value is unknown
    right now (``cpu_percent`` before ``/proc`` answered, a measure absent from
    this heartbeat): unknown is not zero, and 404 is the honest status.
    """
    if data_id in _IDENTITY:
        value = _identity(view, data_id)
        return None if value is None else _wrap(value, {"type": "string"})
    if data_id == "x-zenode-presence":
        return _wrap(view.presence, {"type": "string", "enum": ["live", "silent", "gone"]})
    if data_id == "x-zenode-last-seen":
        if view.last_seen_s is None:
            return None
        return _wrap(view.last_seen_s, {"type": "number"}, "Seconds since the last heartbeat.")
    if view.health is None:
        return None
    if data_id == "state":
        return _wrap(view.health.state, _STATE_SCHEMA)
    descriptor = RUNTIME_BY_ID.get(data_id)
    if descriptor is not None:
        value = getattr(view.health, data_id)
        if value is None:
            return None
        kind = "integer" if descriptor.integral else "number"
        return _wrap(value, {"type": kind}, descriptor.description)
    if view.info is not None:
        for d in view.info.measures:
            if d.id == data_id:
                measured = view.health.measures.get(data_id)
                if measured is None:
                    return None
                kind = "integer" if d.integral else "number"
                return _wrap(measured, {"type": kind}, d.description)
    return None


def _identity(view: NodeView, data_id: str) -> str | None:
    if data_id == "node":
        return view.name
    if data_id == "host":
        if view.info is not None and view.info.host:
            return view.info.host
        return view.health.host if view.health is not None and view.health.host else None
    if data_id == "zenode":
        return view.info.zenode if view.info is not None and view.info.zenode else None
    return None


def categories(view: NodeView, namespace: str) -> list[DataCategoryInformation]:
    seen = sorted({m.category for m in data_items(view, namespace)})
    return [DataCategoryInformation(item=c) for c in seen]


def groups(view: NodeView, namespace: str) -> list[Group]:
    pairs = sorted({(m.groups[0], m.category) for m in data_items(view, namespace) if m.groups})
    return [Group(id=g, category=c) for g, c in pairs]
