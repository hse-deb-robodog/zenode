"""The HTTP surface: upstream's route set, upstream's error bodies, and the
design note's status mapping.

A ``ThreadingHTTPServer`` from the standard library, like ``zenode.exporter``:
one thread per connection, each of which may block on a service call. The
bridge it calls through is a protocol so this module is tested with a fake and
no zenoh. Nothing here is cached: a read reaches the node or fails with a
status that says why, and the only state it consults is the topology.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, urlsplit

from ..errors import ServiceError, ServiceTimeout
from .model import (
    SOVD_VERSION,
    DataCategoryInformation,
    EntityCapabilities,
    EntityReference,
    Group,
    Items,
    Metadata,
    ReadResponse,
    SovdInfo,
    VendorInfo,
    VersionInfo,
    Wire,
    dumps,
    error_body,
)
from .resources import categories, data_items, groups, read_value, readable_services
from .topology import NodeView, Topology

CONTENT_TYPE = "application/json"


class CallsSaturated(Exception):
    """More in-flight service calls than ``--max-calls`` allows."""


class CallBridge(Protocol):
    """What the router needs from the sidecar: the topology, and one call."""

    topology: Topology
    namespace: str
    vendor_version: str

    def call(self, key: str) -> bytes:
        """Call ``key`` with an empty JSON request; return the raw JSON reply.

        Raises :class:`~zenode.errors.ServiceTimeout`,
        :class:`~zenode.errors.ServiceError` or :class:`CallsSaturated`.
        """
        ...


def normalize_base(path: str) -> str:
    """``""``/``"/"`` → mount at root; ``sovd`` or ``/sovd/`` → ``/sovd``."""
    path = path.strip("/")
    return f"/{path}" if path else ""


_V1 = re.compile(r"^/v1(?:/(?P<rest>.*))?$")
_COMPONENT = re.compile(
    r"^components/(?P<id>[^/]+)"
    r"(?:/(?P<sub>hosts|belongs-to|data-categories|data-groups|data|x-zenode-logs)"
    r"(?:/(?P<data_id>[^/]+))?)?$"
)

# The list schemas, once: `Items[T]` is parametrised at import so the handler
# never builds a pydantic generic per request.
_ITEMS_SCHEMA: dict[type[Wire], dict[str, Any]] = {
    EntityReference: Items[EntityReference].schema_of(),
    Metadata: Items[Metadata].schema_of(),
    DataCategoryInformation: Items[DataCategoryInformation].schema_of(),
    Group: Items[Group].schema_of(),
}


def _segment(value: str) -> str:
    """Percent-encode one path segment the way upstream does."""
    return quote(value, safe="")


class _Handler(BaseHTTPRequestHandler):
    bridge: CallBridge
    base: str

    protocol_version = "HTTP/1.1"

    # ----------------------------------------------------------- plumbing

    def log_message(self, format: str, *args: object) -> None:
        """Silence per-request logging; a traversal is hundreds of requests."""

    def _send(self, status: int, body: bytes | None, **headers: str) -> None:
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body or b"")))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, model: Wire) -> None:
        self._send(200, model.to_json())

    def _error(self, status: int, code: str, message: str, vendor_code: str | None = None) -> None:
        self._send(status, error_body(code, message, vendor_code))

    def _v1(self) -> str:
        host = self.headers.get("Host", "localhost")
        return f"http://{host}{self.base}/v1"

    # ------------------------------------------------------------ routing

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_POST(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        if self._route_exists():
            self._send(405, None, Allow="GET")
        else:
            self._send(404, None)

    def _strip_base(self) -> str | None:
        path = urlsplit(self.path).path
        if path == self.base or path.startswith(self.base + "/"):
            return path[len(self.base) :]
        return None

    def _route_exists(self) -> bool:
        rest = self._strip_base()
        return rest is not None and (rest == "/version-info" or _V1.match(rest) is not None)

    def do_GET(self) -> None:
        rest = self._strip_base()
        if rest is None:
            self._send(404, None)
            return
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        include_schema = query.get("include-schema", ["false"])[-1]
        if include_schema not in ("true", "false"):
            self._error(400, "incomplete-request", "Bad request")
            return
        with_schema = include_schema == "true"

        if rest == "/version-info":
            self._version_info(with_schema)
            return
        m = _V1.match(rest)
        if m is None:
            self._send(404, None)
            return
        sub = m.group("rest") or ""
        if sub == "":
            self._root(with_schema)
        elif sub == "components":
            self._components(with_schema)
        elif sub in ("apps", "areas"):
            self._items([], with_schema, EntityReference)
        elif (cm := _COMPONENT.match(sub)) is not None:
            self._component(
                cm.group("id"), cm.group("sub"), cm.group("data_id"), query, with_schema
            )
        else:
            self._send(404, None)

    # ----------------------------------------------------------- handlers

    def _version_info(self, with_schema: bool) -> None:
        info = VersionInfo(
            sovd_info=[
                SovdInfo(
                    version=SOVD_VERSION,
                    base_uri=self._v1(),
                    vendor_info=VendorInfo(version=self.bridge.vendor_version, name="zenode"),
                )
            ],
            schema_=VersionInfo.schema_of() if with_schema else None,
        )
        self._json(info)

    def _root(self, with_schema: bool) -> None:
        caps = EntityCapabilities(
            id="",
            name="",
            components=f"{self._v1()}/components" if self.bridge.topology.names() else None,
            schema_=EntityCapabilities.schema_of() if with_schema else None,
        )
        self._json(caps)

    def _components(self, with_schema: bool) -> None:
        refs = [
            EntityReference(id=n, name=n, href=f"{self._v1()}/components/{_segment(n)}")
            for n in self.bridge.topology.names()
        ]
        self._items(refs, with_schema, EntityReference)

    def _items(self, items: list[Any], with_schema: bool, item_type: type[Wire]) -> None:
        body: Items[Any] = Items(items=items)
        if with_schema:
            body.schema_ = _ITEMS_SCHEMA[item_type]
        self._json(body)

    def _component(
        self,
        name: str,
        sub: str | None,
        data_id: str | None,
        query: dict[str, list[str]],
        with_schema: bool,
    ) -> None:
        view = self.bridge.topology.get(name)
        if view is None:
            self._error(404, "vendor-specific", f"Entity not found: {name}", "entity-not-found")
            return
        ns = self.bridge.namespace
        here = f"{self._v1()}/components/{_segment(name)}"
        if sub is None:
            self._json(
                EntityCapabilities(
                    id=name,
                    name=name,
                    data=f"{here}/data",
                    hosts=f"{here}/hosts",
                    x_zenode_logs=f"{here}/x-zenode-logs",
                    schema_=EntityCapabilities.schema_of() if with_schema else None,
                )
            )
        elif sub in ("hosts", "belongs-to"):
            self._items([], with_schema, EntityReference)
        elif sub == "data-categories":
            self._items(categories(view, ns), False, DataCategoryInformation)
        elif sub == "data-groups":
            wanted = query.get("category", [""])[-1]
            rows = [g for g in groups(view, ns) if not wanted or g.category == wanted]
            self._items(rows, False, Group)
        elif sub == "data" and data_id is None:
            self._data_list(view, query, with_schema)
        elif sub == "data" and data_id is not None:
            self._data_read(view, data_id, with_schema)
        elif sub == "x-zenode-logs":
            self._send(200, dumps({"items": [r.model_dump() for r in view.logs]}))
        else:
            self._send(404, None)

    def _data_list(self, view: NodeView, query: dict[str, list[str]], with_schema: bool) -> None:
        items = data_items(view, self.bridge.namespace)
        wanted_groups = [g for g in query.get("groups", []) if g]
        wanted_categories = [c for c in query.get("categories", []) if c]
        if wanted_groups:  # groups beat categories, as upstream
            items = [m for m in items if m.groups and m.groups[0] in wanted_groups]
        elif wanted_categories:
            items = [m for m in items if m.category in wanted_categories]
        self._items(items, with_schema, Metadata)

    def _data_read(self, view: NodeView, data_id: str, with_schema: bool) -> None:
        ns = self.bridge.namespace
        services = readable_services(view, ns)
        if data_id in services:
            service = services[data_id]
            self._service_read(view, data_id, service.key, service.reply_schema, with_schema)
            return
        found = read_value(view, data_id, ns)
        if found is None:
            self._error(404, "error-response", f"not found: {data_id}")
            return
        value, schema = found
        self._json(ReadResponse(id=data_id, data=value, schema_=schema if with_schema else None))

    def _service_read(
        self, view: NodeView, data_id: str, key: str, schema: dict[str, Any], with_schema: bool
    ) -> None:
        if view.presence == "gone":
            self._error(504, "not-responding", f"{view.name} is gone")
            return
        try:
            raw = self.bridge.call(key)
        except ServiceTimeout as e:
            self._error(504, "not-responding", str(e))
            return
        except ServiceError as e:
            self._error(502, "error-response", str(e))
            return
        except CallsSaturated:
            self._error(503, "sovd-server-failure", "too many in-flight service calls")
            return
        try:
            data = json.loads(raw)
        except ValueError:
            self._error(502, "error-response", f"{key}: reply is not JSON")
            return
        self._json(ReadResponse(id=data_id, data=data, schema_=schema if with_schema else None))


def make_server(
    bridge: CallBridge, host: str, port: int, *, base_uri: str = "/sovd"
) -> ThreadingHTTPServer:
    """An HTTP server serving ``bridge`` under ``base_uri``.

    The handler class is built per call so the bridge binds as a class
    attribute, exactly as :func:`zenode.exporter.make_server` does.
    """
    handler = type(
        "_BoundHandler", (_Handler,), {"bridge": bridge, "base": normalize_base(base_uri)}
    )
    return ThreadingHTTPServer((host, port), handler)
