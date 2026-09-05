"""What a node declared, published once on ``<ns>/node/<name>/info``.

The heartbeat carries the numbers that change; this carries the half that does
not — the entities a node wired up and the catalog of its ``@metric``
declarations. Units, kinds and descriptions never change, so repeating them
every ``health_interval`` forever would be bytes for nothing on a robot that
runs for a week.

**Latched, not a queryable**, and that is the whole point. :meth:`Node.serve`
dispatches to the node's event loop, so a node wedged in a synchronous driver
call answers no query — which is exactly when "who was supposed to publish this
key" gets asked. The descriptor was published at startup and is served from
zenoh's own threads by the zenoh-ext cache, so it survives the wedge. A sidecar
started an hour later gets it from the history query, with no discovery loop,
timeout or retry of its own.

Consumers must declare an *advanced* subscriber
(``zext.HistoryConfig(detect_late_publishers=True, max_samples=1)``); a plain
subscriber receives no cache and would simply never see a descriptor.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from ..topic import resolve_key


def info_key(name: str) -> str:
    """The key, relative to the namespace, that ``name`` publishes its descriptor on."""
    return f"node/{name}/info"


def info_pattern(namespace: str) -> str:
    """Key expression matching every node's descriptor in ``namespace``.

    Derived from :func:`info_key` so the publisher and the CLI cannot drift.
    """
    return resolve_key(info_key("*"), namespace)


class EntityInfo(BaseModel):
    """One topic a node publishes or subscribes."""

    key: str
    """Resolved and absolute — what actually went on the wire, not the
    contract-relative form. A namespace mismatch is one of the things this
    exists to make visible, so the namespace has to be *in* the value."""
    schema_name: str = ""
    """The payload model's class name. A label for a human reading a table, not
    a type identity: two contracts may both call something ``Pose``, so a tool
    that decoded by name would eventually decode wrongly and confidently.
    ``zenode echo --contract`` stays the only route to a typed decode.

    Spelled ``schema_name`` rather than ``schema`` because pydantic v2 warns
    that the latter shadows an attribute of ``BaseModel``."""
    flags: list[str] = []
    """``latched(1)``, ``max_age=0.2``, ``shm``, ``trace@0.1``, ``prio=real_time``
    — built by :func:`zenode.topic.topic_flags`, the same function
    ``zenode topics`` renders from a ``Topic``."""


class ServiceInfo(BaseModel):
    """One service a node serves."""

    key: str
    request: str = ""
    reply: str = ""


class MeasureDescriptor(BaseModel):
    """The static half of one ``@metric`` declaration.

    There is no separate ``name``: the ``id`` *is* the name, and two identifiers
    for one thing is exactly the drift that keeping a single table prevents.
    """

    id: str
    unit: str = ""
    """UCUM, as OTLP expects: ``s``, ``By``, ``1``, ``{frame}``.
    :doc:`../conventions` is normative — SI on the wire, SoC as 0.0 to 1.0."""
    kind: Literal["gauge", "counter"] = "gauge"
    integral: bool = False
    """Whether the value is a whole number, which OTLP encodes differently."""
    description: str = ""


class NodeInfo(BaseModel):
    """One node's descriptor: what it wired up, and what it measures."""

    node: str
    host: str = ""
    """The machine it runs on — see :attr:`zenode.msgs.NodeHealth.host`."""
    zenode: str = ""
    """The zenode version the node is running. A fleet mid-rollout is a thing
    that happens, and a descriptor that disagrees with the bus is usually it."""
    publishes: list[EntityInfo] = []
    subscribes: list[EntityInfo] = []
    serves: list[ServiceInfo] = []
    measures: list[MeasureDescriptor] = []
    """The catalog behind :attr:`zenode.msgs.NodeHealth.measures`. A value on a
    heartbeat whose descriptor has not arrived is not exported: a series whose
    ``TYPE`` flips mid-history is worse than a short hole, and latched delivery
    makes the hole transient."""
