# SOVD Phase 1b: the sidecar — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `zenode sovd`, a read-only, stdlib-only HTTP sidecar that serves live nodes as SOVD components, their heartbeat fields, declared measurements and `diagnostic="read"` services as data resources, and their log ring under a custom link, discovering everything from the bus.

**Architecture:** Four modules under `zenode.sovd`. `model.py` is the wire shapes, pydantic with kebab-case aliases and `exclude_none`, mirroring `opensovd-models` at commit `26953d97`. `topology.py` is the bounded state: one record per node fed by liveliness, the latched descriptor, heartbeats and logs, with the live/silent/gone classification. `resources.py` turns one node's record into SOVD data items. `server.py` is the `ThreadingHTTPServer` router with upstream's exact route set and status mapping. `sidecar.py` is the glue: one asyncio loop thread owning the zenoh session's subscriptions and the capped service-call bridge that HTTP handler threads submit to.

**Tech Stack:** Python 3.11+, `http.server`, `json`, `urllib.parse`, pydantic v2, eclipse-zenoh, pytest; `jsonschema` as a dev dependency for the conformance test; a Rust stable toolchain once, to dump schema fixtures from the cargo checkout.

**Spec:** `docs/design/opensovd-adapter.md` §8 "The mapping", §9 "Architecture", §10 "Presence", §11 "Conformance", §12 Phase 1. The runtime half landed in `docs/superpowers/plans/2026-10-02-sovd-runtime-changes.md`.

## Global Constraints

- Runtime dependencies stay `eclipse-zenoh` and `pydantic`. `jsonschema` goes in the `dev` group only.
- The subpackage imports only public zenode API plus `zenode.service.call_service`, `zenode.cli._declare_latched_subscriber` and `zenode.presence.PresenceWatcher`. Nothing name-mangled, no `_Probe`.
- Route set, JSON shapes and `version: "1.1"` are pinned to `opensovd-core` commit `26953d97da4335179083aff41ed52a82886499e7`.
- Upstream wire facts that must hold (from the checkout): `Response<T>` is flattened, so `schema` is a sibling of `items`/`id`, never a `data` envelope. Link keys are kebab-case: `belongs-to`, `bulk-data`, `is-located-on`. Empty optional lists are omitted, never `[]`. `data-categories` items use the key `item`. `data-categories` and `data-groups` never return a schema. `groups` beats `categories` when both are given. Unknown query keys are ignored; a malformed `include-schema` is 400 `incomplete-request` "Bad request". Unknown path is 404 with an empty body; wrong method is 405 with `Allow`. `/version-info` sits at `{base}/version-info`, outside `/v1`. Hrefs are `{scheme}://{Host header}{base}/v1/...`.
- Status mapping, from the spec: 404 entity not found (`error_code: "vendor-specific"`, `vendor_code: "entity-not-found"`), 404 unknown data id (`error-response`, `"not found: {id}"`), 504 `ServiceTimeout` or a gone node (`not-responding`), 502 `ServiceError` or an undecodable reply (`error-response`), 503 over the call cap (`sovd-server-failure`).
- Defaults, from the spec: `--listen 127.0.0.1:7690`, `--base-uri /sovd`, `--retain-gone 600`, `--max-gone 64`, `--log-buffer 1000`, `--max-calls 32`, `--call-timeout 2.0`. Silent after three missed `health_interval`s.
- Bounded by construction: one heartbeat, one descriptor and a `deque(maxlen=log_buffer)` per node; departed and never-admitted records capped at `max_gone`; in-flight calls capped at `max_calls`.
- Commit messages are one short subject line in the repo's `type(scope): summary` style, no body, no attribution.
- Checks that must pass before the final commit: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format src tests examples`, `uv run pyright`, `uv run ty check src tests`, `uv run sphinx-build -W -b html docs docs/_build/html`.

---

## File map

| File | Responsibility |
|---|---|
| `src/zenode/sovd/__init__.py` | exports `Sidecar`, `Topology`, `make_server` |
| `src/zenode/sovd/model.py` | wire shapes, `Wire.to_json()`, `GenericError`, `error_body()` |
| `src/zenode/sovd/topology.py` | `Topology`, `NodeView`, `Presence`; bounded per-node state and classification |
| `src/zenode/sovd/resources.py` | `service_id()`, `data_items()`, `read_value()`, `categories()`, `groups()` for one `NodeView` |
| `src/zenode/sovd/server.py` | `CallBridge` protocol, `make_server()`, `_Handler` routing and status mapping |
| `src/zenode/sovd/sidecar.py` | `Sidecar`: loop thread, subscriptions, presence watcher, capped `call()` |
| `src/zenode/cli.py` | `cmd_sovd`, `zenode sovd` subparser |
| `tests/sovd/schemas/*.json`, `tests/sovd/PIN` | schema fixtures from `opensovd-models` |
| `tests/test_sovd_model.py`, `tests/test_sovd_topology.py`, `tests/test_sovd_resources.py`, `tests/test_sovd_server.py`, `tests/test_sovd_integration.py`, `tests/test_sovd_conformance.py`, `tests/test_cli_commands.py` | tests |
| `docs/cli.md`, `docs/sovd.md` (new), `docs/index.md`, `docs/design/opensovd-adapter.md` | user docs and the status line |

---

### Task 1: Schema fixtures from `opensovd-models`

**Files:**
- Create: `tests/sovd/schemas/entities.json`, `entity_capabilities.json`, `data_list.json`, `read_response.json`, `data_categories.json`, `data_groups.json`, `version_info.json`, `generic_error.json`
- Create: `tests/sovd/PIN`
- Modify: `pyproject.toml` (dev group)

**Interfaces:**
- Produces: eight JSON Schema 2020-12 documents, one per response shape the sidecar emits, and `jsonschema` in the dev group. Task 9 validates every response against them.

The checkout lives at `/home/fabian/.cargo/git/checkouts/opensovd-core-7f30599978eede10/26953d9`. Nothing in it dumps schemas, so a throwaway crate in the scratchpad does it once. The crate is not committed; the JSON is.

- [ ] **Step 1: Write the throwaway crate in the scratchpad**

```toml
# $SCRATCH/dump-schemas/Cargo.toml
[package]
name = "dump-schemas"
version = "0.0.0"
edition = "2021"

[dependencies]
opensovd-models = { path = "/home/fabian/.cargo/git/checkouts/opensovd-core-7f30599978eede10/26953d9/opensovd-models", features = ["jsonschema"] }
schemars = "1"
serde_json = "1"
```

```rust
// $SCRATCH/dump-schemas/src/main.rs
use opensovd_models::{data, discovery, error, version, Items};
use std::{env, fs, path::Path};

fn dump<T: schemars::JsonSchema>(dir: &Path, name: &str) {
    let schema = schemars::schema_for!(T);
    let text = serde_json::to_string_pretty(&schema).unwrap();
    fs::write(dir.join(format!("{name}.json")), text + "\n").unwrap();
    eprintln!("wrote {name}.json");
}

fn main() {
    let out = env::args().nth(1).expect("output dir");
    let dir = Path::new(&out);
    fs::create_dir_all(dir).unwrap();
    dump::<Items<discovery::EntityReference>>(dir, "entities");
    dump::<discovery::EntityCapabilities>(dir, "entity_capabilities");
    dump::<Items<data::Metadata>>(dir, "data_list");
    dump::<data::ReadResponse>(dir, "read_response");
    dump::<Items<data::DataCategoryInformation>>(dir, "data_categories");
    dump::<Items<data::Group>>(dir, "data_groups");
    dump::<version::VersionInfo<version::VendorInfo>>(dir, "version_info");
    dump::<error::GenericError>(dir, "generic_error");
}
```

- [ ] **Step 2: Build and run it on stable, writing into the repo**

```bash
cd $SCRATCH/dump-schemas && cargo run --quiet -- /home/fabian/PycharmProjects/zenode/tests/sovd/schemas
```

Expected: eight `wrote ….json` lines. If cargo picks up the checkout's nightly pin, the path dependency is outside that workspace so it should not; if it does, `rustup run stable cargo run …`.

- [ ] **Step 3: Record the pin and add the dev dependency**

```bash
printf 'opensovd-core 26953d97da4335179083aff41ed52a82882886499e7\nschemas generated with schemars 1 from opensovd-models (feature jsonschema)\n' > tests/sovd/PIN
uv add --group dev "jsonschema>=4.26"
```

Correct the hash in `PIN` to exactly `26953d97da4335179083aff41ed52a82886499e7` (copy it from `docs/design/opensovd-adapter.md`, do not retype it).

- [ ] **Step 4: Sanity-check one fixture**

Run: `uv run python -c "import json; s=json.load(open('tests/sovd/schemas/entities.json')); print(s['\$schema'], s['title'], list(s['properties']))"`
Expected: `https://json-schema.org/draft/2020-12/schema Items_for_EntityReference ['items']`

- [ ] **Step 5: Commit**

```bash
git add tests/sovd pyproject.toml uv.lock
git commit -m "test(sovd): pin opensovd-models schemas as fixtures"
```

---

### Task 2: Wire shapes

**Files:**
- Create: `src/zenode/sovd/__init__.py`, `src/zenode/sovd/model.py`
- Test: `tests/test_sovd_model.py`

**Interfaces:**
- Produces: `Wire` base with `to_json() -> bytes` and `schema_of(cls) -> dict`; `Items[T]`, `EntityReference`, `EntityCapabilities`, `Metadata`, `ReadResponse`, `DataCategoryInformation`, `Group`, `VendorInfo`, `SovdInfo`, `VersionInfo`, `GenericError`, `LogItems`; `SOVD_VERSION = "1.1"`; `error_body(code, message, vendor_code=None) -> bytes`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sovd_model.py
"""The SOVD wire shapes, pinned to opensovd-models at 26953d97.

What matters is what leaves the socket: kebab-case link keys, omitted
optionals rather than nulls or empty lists, `schema` as a sibling key with no
envelope, and `item` (not `id`) in data-categories.
"""

from __future__ import annotations

import json

from zenode.sovd.model import (
    SOVD_VERSION,
    DataCategoryInformation,
    EntityCapabilities,
    EntityReference,
    GenericError,
    Group,
    Items,
    Metadata,
    ReadResponse,
    SovdInfo,
    VendorInfo,
    VersionInfo,
    error_body,
)


def _load(model) -> dict:
    return json.loads(model.to_json())


def test_link_keys_are_kebab_case_and_absent_when_unset():
    body = _load(EntityCapabilities(id="nav", name="nav", belongs_to="http://x/belongs-to"))
    assert body == {"id": "nav", "name": "nav", "belongs-to": "http://x/belongs-to"}


def test_custom_log_link_uses_the_vendor_prefix():
    body = _load(EntityCapabilities(id="nav", name="nav", x_zenode_logs="http://x/logs"))
    assert body["x-zenode-logs"] == "http://x/logs"


def test_schema_is_a_sibling_key_not_an_envelope():
    body = _load(Items[EntityReference](items=[], schema_={"type": "object"}))
    assert body == {"items": [], "schema": {"type": "object"}}


def test_empty_optional_lists_are_omitted():
    body = _load(EntityReference(id="nav", name="nav", href="http://x/components/nav"))
    assert "tags" not in body
    body = _load(Metadata(id="sent", name="sent", category="sysInfo"))
    assert "groups" not in body


def test_data_categories_use_the_item_key():
    assert _load(DataCategoryInformation(item="sysInfo")) == {"item": "sysInfo"}
    assert _load(Group(id="runtime", category="sysInfo")) == {"id": "runtime", "category": "sysInfo"}


def test_read_response_carries_schema_only_when_asked():
    assert _load(ReadResponse(id="sent", data={"value": 3})) == {"id": "sent", "data": {"value": 3}}
    assert "schema" in _load(ReadResponse(id="sent", data={"value": 3}, schema_={"type": "object"}))


def test_version_info_shape():
    body = _load(
        VersionInfo(
            sovd_info=[
                SovdInfo(
                    version=SOVD_VERSION,
                    base_uri="http://robot:7690/sovd/v1",
                    vendor_info=VendorInfo(name="zenode", version="0.1.0"),
                )
            ]
        )
    )
    assert body["sovd_info"][0]["version"] == "1.1"
    assert body["sovd_info"][0]["vendor_info"] == {"version": "0.1.0", "name": "zenode"}


def test_error_bodies_match_upstreams_two_spellings():
    """Entity misses ride in vendor_code; everything else in error_code."""
    assert json.loads(error_body("vendor-specific", "Entity not found: x", "entity-not-found")) == {
        "error_code": "vendor-specific",
        "vendor_code": "entity-not-found",
        "message": "Entity not found: x",
    }
    assert json.loads(error_body("not-responding", "no reply")) == {
        "error_code": "not-responding",
        "message": "no reply",
    }
    assert GenericError.model_fields["error_code"].annotation is str
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_sovd_model.py -q`
Expected: `ModuleNotFoundError: No module named 'zenode.sovd'`

- [ ] **Step 3: Write the package init and the models**

```python
# src/zenode/sovd/__init__.py
"""The SOVD sidecar: ``zenode sovd``.

A read-only ISO 17978 (SOVD) server over what nodes declare — see
``docs/design/opensovd-adapter.md``. Out of process, stdlib HTTP, discovers
everything from the bus. Nothing here is imported by a node.
"""

from .server import make_server
from .sidecar import Sidecar
from .topology import Topology

__all__ = ["Sidecar", "Topology", "make_server"]
```

The import of `server` and `sidecar` will fail until Tasks 5 and 6; for now write `__init__.py` with only the docstring and add the imports in Task 6.

```python
# src/zenode/sovd/model.py
"""SOVD wire shapes, as ``opensovd-models`` spells them at commit 26953d97.

Pydantic rather than hand-built dicts so ``?include-schema=true`` can answer
with ``model_json_schema()`` and so a typo in a key is a test failure here
rather than a client that silently ignores a field. Three rules, all of them
upstream's: link keys are kebab-case, unset optionals are *omitted* (never
``null`` or ``[]``), and ``Response<T>`` is flattened — ``schema`` sits next
to ``items`` or ``id``, there is no ``data`` envelope.
"""

from __future__ import annotations

import json
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

SOVD_VERSION = "1.1"
"""What ``opensovd-core`` reports at the pinned commit. The design note's
conformance stance covers why this is a pin, not a claim."""

T = TypeVar("T")


class Wire(BaseModel):
    """Base for everything that leaves the socket."""

    model_config = ConfigDict(populate_by_name=True)

    def to_json(self) -> bytes:
        return self.model_dump_json(by_alias=True, exclude_none=True).encode()

    @classmethod
    def schema_of(cls) -> dict[str, Any]:
        return cls.model_json_schema(by_alias=True)


class Items(Wire, Generic[T]):
    """``{"items": [...]}``, with the optional sibling ``schema``."""

    items: list[T]
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")


class EntityReference(Wire):
    """One row of ``/components``."""

    id: str
    name: str
    href: str
    translation_id: str | None = None
    tags: list[str] | None = None


class EntityCapabilities(Wire):
    """``/components/{id}`` and the root: links to what the entity supports.

    Only the links the sidecar serves are declared. ``opensovd-models`` has
    many more (``faults``, ``operations``, ``modes`` …); a key absent from the
    response is a capability the entity does not have, which is the whole
    discovery contract.
    """

    id: str
    name: str
    translation_id: str | None = None
    variant: dict[str, str] | None = None
    data: str | None = None
    hosts: str | None = None
    belongs_to: str | None = Field(default=None, alias="belongs-to")
    components: str | None = None
    apps: str | None = None
    areas: str | None = None
    x_zenode_logs: str | None = Field(default=None, alias="x-zenode-logs")
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")


class Metadata(Wire):
    """One row of ``/data``."""

    id: str
    name: str
    category: str
    translation_id: str | None = None
    groups: list[str] | None = None
    tags: list[str] | None = None


class ReadResponse(Wire):
    """``/data/{id}``. Not wrapped — ``schema`` is a direct sibling of ``data``."""

    id: str
    data: Any
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")


class DataCategoryInformation(Wire):
    """One row of ``/data-categories``. The key really is ``item``."""

    item: str


class Group(Wire):
    """One row of ``/data-groups``."""

    id: str
    category: str


class VendorInfo(Wire):
    version: str
    name: str


class SovdInfo(Wire):
    version: str
    base_uri: str
    vendor_info: VendorInfo | None = None


class VersionInfo(Wire):
    sovd_info: list[SovdInfo]
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")


class GenericError(Wire):
    """Every error body. ``error_code`` is upstream's closed vocabulary;
    entity misses travel as ``vendor-specific`` with a ``vendor_code``."""

    error_code: str
    vendor_code: str | None = None
    message: str


def error_body(code: str, message: str, vendor_code: str | None = None) -> bytes:
    return GenericError(error_code=code, vendor_code=vendor_code, message=message).to_json()


def dumps(value: Any) -> bytes:
    """For the one body that is not a model: a schema dict."""
    return json.dumps(value).encode()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_sovd_model.py -q`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/zenode/sovd tests/test_sovd_model.py
git commit -m "feat(sovd): wire shapes pinned to opensovd-models"
```

---

### Task 3: Topology — bounded per-node state and presence

**Files:**
- Create: `src/zenode/sovd/topology.py`
- Test: `tests/test_sovd_topology.py`

**Interfaces:**
- Produces: `Presence = Literal["live", "silent", "gone"]`; `NodeView` (frozen: `name`, `presence`, `info: NodeInfo | None`, `health: NodeHealth | None`, `last_seen_s: float | None`, `logs: list[LogRecordMsg]`); `Topology(namespace, *, retain_gone=600.0, max_gone=64, log_buffer=1000, clock=time.monotonic)` with `presence(name, alive)`, `offer_info(bytes)`, `offer_health(bytes)`, `offer_log(bytes)`, `names() -> list[str]`, `get(name) -> NodeView | None`. `SILENT_AFTER_BEATS = 3`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sovd_topology.py
"""The sidecar's one piece of state, and its three presence answers.

Liveliness admits a node; heartbeat age says whether it is answering;
departure is retained for a bounded window so a crashed node is still
diagnosable. Every container has a cap, because a sidecar runs as long as the
robot does.
"""

from __future__ import annotations

import pytest

from zenode.msgs import LogRecordMsg, NodeHealth, NodeInfo
from zenode.sovd.topology import SILENT_AFTER_BEATS, Topology


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _health(node: str = "nav", **kw) -> bytes:
    return NodeHealth.model_validate(
        {"node": node, "state": "running", "uptime_s": 1.0, "ts_ns": 1, **kw}
    ).model_dump_json().encode()


def _info(node: str = "nav", **kw) -> bytes:
    return NodeInfo(node=node, **kw).model_dump_json().encode()


def _log(node: str = "nav", message: str = "m") -> bytes:
    return LogRecordMsg(
        node=node, level="WARNING", logger="x", message=message, ts_ns=1
    ).model_dump_json().encode()


def test_liveliness_admits_a_node(clock):
    t = Topology("", clock=clock)
    assert t.names() == []
    t.presence("nav", True)
    assert t.names() == ["nav"]
    view = t.get("nav")
    assert view is not None and view.presence == "live" and view.health is None


def test_a_descriptor_that_arrives_before_liveliness_is_kept_but_not_listed(clock):
    """Latched delivery races the liveliness replay; dropping it would lose it for good."""
    t = Topology("", clock=clock)
    t.offer_info(_info(host="jetson"))
    assert t.names() == []
    assert t.get("nav") is None
    t.presence("nav", True)
    assert t.get("nav").info.host == "jetson"


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
    assert t.get("nav").presence == "live"
    clock.now += SILENT_AFTER_BEATS * 2.0 - 0.1
    assert t.get("nav").presence == "live"
    clock.now += 0.2
    assert t.get("nav").presence == "silent"
    assert t.get("nav").last_seen_s == pytest.approx(6.1)


def test_a_node_without_a_heartbeat_is_never_silent(clock):
    t = Topology("", clock=clock)
    t.presence("nav", True)
    t.offer_info(_info(health_interval=None))
    clock.now += 1000
    assert t.get("nav").presence == "live"


def test_departure_is_retained_then_dropped(clock):
    t = Topology("", clock=clock, retain_gone=10.0)
    t.presence("nav", True)
    t.offer_health(_health(sent=5))
    t.offer_log(_log())
    t.presence("nav", False)
    view = t.get("nav")
    assert view.presence == "gone" and view.health.sent == 5 and len(view.logs) == 1
    assert t.names() == ["nav"]
    clock.now += 10.1
    assert t.names() == [] and t.get("nav") is None


def test_a_returning_node_is_live_again_with_its_history(clock):
    t = Topology("", clock=clock)
    t.presence("nav", True)
    t.offer_log(_log())
    t.presence("nav", False)
    t.presence("nav", True)
    assert t.get("nav").presence == "live" and len(t.get("nav").logs) == 1


def test_the_departed_set_is_capped_oldest_first(clock):
    t = Topology("", clock=clock, max_gone=2)
    for i, name in enumerate(["a", "b", "c"]):
        t.presence(name, True)
        clock.now += 1
        t.presence(name, False)
    assert t.names() == ["b", "c"]


def test_log_ring_is_bounded(clock):
    t = Topology("", clock=clock, log_buffer=3)
    t.presence("nav", True)
    for i in range(5):
        t.offer_log(_log(message=str(i)))
    assert [r.message for r in t.get("nav").logs] == ["2", "3", "4"]


def test_views_are_snapshots(clock):
    t = Topology("", clock=clock)
    t.presence("nav", True)
    view = t.get("nav")
    t.offer_log(_log())
    assert view.logs == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_sovd_topology.py -q`
Expected: `ImportError` on `zenode.sovd.topology`

- [ ] **Step 3: Write the topology**

```python
# src/zenode/sovd/topology.py
"""Who is on the bus, and whether they are answering.

Three sources, three answers. The liveliness token says a node *exists*; the
heartbeat's age against its own ``health_interval`` says whether its loop is
running; a dropped token says it is *gone*, and gone nodes stay listed for a
bounded window because the last heartbeat and the last thousand log lines of
a process that crashed a minute ago are the one thing a technician came for.

Everything here is written from zenoh threads and read from HTTP threads,
under one lock, and every container has a cap: a heartbeat and a descriptor
per node, a ``deque(maxlen=…)`` of logs per node, and ``max_gone`` records
that are not currently live. A sidecar runs as long as the robot does.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from ..msgs import LogRecordMsg, NodeHealth, NodeInfo

Presence = Literal["live", "silent", "gone"]

SILENT_AFTER_BEATS = 3
"""Missed heartbeats before a live node is reported silent — the same rule a
subscription ``deadline`` uses for a producer."""


@dataclass
class _Record:
    name: str
    alive: bool = False
    """Admitted by a liveliness token. A record created by a descriptor or
    heartbeat that arrived first is kept but not listed until the token comes:
    latched delivery races the liveliness replay at startup, and dropping the
    descriptor would lose it for good — it is published once."""
    left_at: float | None = None
    info: NodeInfo | None = None
    health: NodeHealth | None = None
    health_at: float | None = None
    logs: deque[LogRecordMsg] = field(default_factory=deque)


@dataclass(frozen=True)
class NodeView:
    """A snapshot of one node for a single HTTP request."""

    name: str
    presence: Presence
    info: NodeInfo | None
    health: NodeHealth | None
    last_seen_s: float | None
    """Seconds since the last heartbeat, or ``None`` before the first."""
    logs: list[LogRecordMsg]


class Topology:
    def __init__(
        self,
        namespace: str,
        *,
        retain_gone: float = 600.0,
        max_gone: int = 64,
        log_buffer: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.namespace = namespace
        self.retain_gone = retain_gone
        self.max_gone = max_gone
        self.log_buffer = log_buffer
        self._clock = clock
        self._records: dict[str, _Record] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ writers

    def presence(self, name: str, alive: bool) -> None:
        """A liveliness token appeared or vanished. Called on the sidecar loop."""
        with self._lock:
            record = self._record(name)
            record.alive = alive
            record.left_at = None if alive else self._clock()
            self._prune()

    def offer_info(self, payload: bytes) -> None:
        try:
            info = NodeInfo.model_validate_json(payload)
        except ValidationError:
            return  # a key expression is not a promise about what is on it
        with self._lock:
            self._record(info.node).info = info
            self._prune()

    def offer_health(self, payload: bytes) -> None:
        try:
            health = NodeHealth.model_validate_json(payload)
        except ValidationError:
            return
        with self._lock:
            record = self._record(health.node)
            record.health = health
            record.health_at = self._clock()
            self._prune()

    def offer_log(self, payload: bytes) -> None:
        try:
            log = LogRecordMsg.model_validate_json(payload)
        except ValidationError:
            return
        with self._lock:
            self._record(log.node).logs.append(log)
            self._prune()

    # ------------------------------------------------------------ readers

    def names(self) -> list[str]:
        """Listed nodes: live, or gone within the retention window."""
        with self._lock:
            self._prune()
            return sorted(n for n, r in self._records.items() if self._listed(r))

    def get(self, name: str) -> NodeView | None:
        with self._lock:
            self._prune()
            record = self._records.get(name)
            if record is None or not self._listed(record):
                return None
            now = self._clock()
            last_seen = None if record.health_at is None else now - record.health_at
            return NodeView(
                name=name,
                presence=self._classify(record, last_seen),
                info=record.info,
                health=record.health,
                last_seen_s=last_seen,
                logs=list(record.logs),
            )

    # ------------------------------------------------------------ internals

    def _record(self, name: str) -> _Record:
        record = self._records.get(name)
        if record is None:
            record = _Record(name, logs=deque(maxlen=self.log_buffer))
            self._records[name] = record
        return record

    def _listed(self, record: _Record) -> bool:
        return record.alive or record.left_at is not None

    def _classify(self, record: _Record, last_seen: float | None) -> Presence:
        if not record.alive:
            return "gone"
        interval = record.info.health_interval if record.info is not None else None
        if interval is None or last_seen is None:
            return "live"
        return "silent" if last_seen > SILENT_AFTER_BEATS * interval else "live"

    def _prune(self) -> None:
        """Drop gone records past retention, then cap everything not live.

        Oldest departure first for the cap; never-admitted records count as
        older than any departure, since they were never worth listing.
        """
        now = self._clock()
        for name, record in list(self._records.items()):
            if record.left_at is not None and now - record.left_at > self.retain_gone:
                del self._records[name]
        not_live = [r for r in self._records.values() if not r.alive]
        excess = len(not_live) - self.max_gone
        if excess > 0:
            not_live.sort(key=lambda r: (r.left_at is not None, r.left_at or 0.0))
            for record in not_live[:excess]:
                del self._records[record.name]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_sovd_topology.py -q`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/zenode/sovd/topology.py tests/test_sovd_topology.py
git commit -m "feat(sovd): bounded topology with live, silent and gone presence"
```

---

### Task 4: Resources — one node as data items

**Files:**
- Create: `src/zenode/sovd/resources.py`
- Test: `tests/test_sovd_resources.py`

**Interfaces:**
- Consumes: `NodeView` (Task 3), `Metadata`, `Group`, `DataCategoryInformation` (Task 2), `RUNTIME_BY_ID` from `zenode.msgs.health`.
- Produces: `service_id(key, namespace) -> str`; `readable_services(view, namespace) -> dict[str, ServiceInfo]` (data id → descriptor, JSON-only, `diagnostic == "read"`); `data_items(view, namespace) -> list[Metadata]`; `read_value(view, data_id, namespace) -> tuple[Any, dict] | None` (value and schema for every non-service item); `categories(view, namespace) -> list[DataCategoryInformation]`; `groups(view, namespace) -> list[Group]`; group names `GROUP_IDENTITY = "identity"`, `GROUP_RUNTIME = "runtime"`, `GROUP_PRESENCE = "presence"`, `GROUP_APP = "app"`, `GROUP_SVC = "svc"`; categories `IDENT_DATA = "identData"`, `SYS_INFO = "sysInfo"`, `CURRENT_DATA = "currentData"`.

Mapping, from the spec's §8 with one refinement: identity (`node`, `host`, `zenode`) goes in `identData`, which is what SOVD calls it, rather than `sysInfo`.

| category | group | ids | value |
|---|---|---|---|
| `identData` | `identity` | `node`, `host`, `zenode` | `{"value": "<str>"}` from `NodeInfo`, falling back to the heartbeat for `host` |
| `sysInfo` | `runtime` | every `RUNTIME_MEASURES` id, plus `state` | `{"value": <number or str>}` from the heartbeat; absent when the heartbeat is absent or the field is `None` |
| `sysInfo` | `presence` | `x-zenode-presence`, `x-zenode-last-seen` | `{"value": "live"}`, `{"value": 6.1}` |
| `currentData` | `app` | each `NodeInfo.measures` id | `{"value": <float>}` from `health.measures`; absent this beat means absent |
| `currentData` | `svc` | `service_id(key)` for each `diagnostic="read"` JSON service | the reply, served by the router through the call bridge |

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sovd_resources.py
"""One node, rendered as SOVD data items.

Ids are collision-free by construction: runtime ids are NodeHealth field
names, measure ids match [a-z][a-z0-9_]*, service ids contain a dot, and the
two presence items carry the vendor prefix.
"""

from __future__ import annotations

from zenode.msgs import Empty, LogRecordMsg, MeasureDescriptor, NodeHealth, NodeInfo, ServiceInfo
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


def _view(**kw) -> NodeView:
    base = dict(name="nav", presence="live", info=None, health=None, last_seen_s=None, logs=[])
    return NodeView(**{**base, **kw})


def _info(**kw) -> NodeInfo:
    return NodeInfo(node="nav", host="jetson", zenode="0.1.0", **kw)


def _health(**kw) -> NodeHealth:
    return NodeHealth.model_validate(
        {"node": "nav", "host": "jetson", "state": "running", "uptime_s": 6.0, "ts_ns": 1, **kw}
    )


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
    assert read_value(view, "sent", "")[0] == {"value": 3}
    assert read_value(view, "battery_soc", "")[0] == {"value": 0.5}
    assert read_value(view, "x-zenode-presence", "")[0] == {"value": "silent"}
    assert read_value(view, "x-zenode-last-seen", "")[0] == {"value": 0.4}
    assert read_value(view, "node", "")[0] == {"value": "nav"}
    value, schema = read_value(view, "battery_soc", "")
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_sovd_resources.py -q`
Expected: `ImportError`

- [ ] **Step 3: Write the resources module**

```python
# src/zenode/sovd/resources.py
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

from typing import Any

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
_STATE_SCHEMA = {"type": "string", "enum": list(NodeState.__args__)}  # type: ignore[attr-defined]


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
    wrapped = {
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
                value = view.health.measures.get(data_id)
                if value is None:
                    return None
                kind = "integer" if d.integral else "number"
                return _wrap(value, {"type": kind}, d.description)
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
```

If pyright rejects `NodeState.__args__`, replace `_STATE_SCHEMA` with `{"type": "string", "enum": list(get_args(NodeState))}` using `typing.get_args`, which is the spelling `topic.py` already uses for `PRIORITIES`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_sovd_resources.py -q`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/zenode/sovd/resources.py tests/test_sovd_resources.py
git commit -m "feat(sovd): render a node's record as data items"
```

---

### Task 5: The HTTP server and router

**Files:**
- Create: `src/zenode/sovd/server.py`
- Test: `tests/test_sovd_server.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces: `class CallBridge(Protocol)` with `topology: Topology`, `namespace: str`, `vendor_version: str`, `call(key: str) -> bytes` raising `ServiceTimeout`, `ServiceError` or `CallsSaturated`; `class CallsSaturated(Exception)`; `make_server(bridge: CallBridge, host: str, port: int, *, base_uri: str = "/sovd", call_timeout: float = 2.0) -> ThreadingHTTPServer`; `normalize_base(path) -> str`.

The call bridge is a protocol so the router is tested with a fake and no zenoh. `call()` is synchronous from the handler thread's point of view; Task 6 implements it over the loop thread.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sovd_server.py
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
        ).model_dump_json().encode()
    )
    topology.offer_health(
        NodeHealth.model_validate(
            {"node": "nav", "state": "running", "uptime_s": 6.0, "ts_ns": 1, "sent": 3,
             "measures": {"battery_soc": 0.5}}
        ).model_dump_json().encode()
    )
    topology.offer_log(
        LogRecordMsg(node="nav", level="WARNING", logger="l", message="hot", ts_ns=1)
        .model_dump_json().encode()
    )
    bridge = FakeBridge(topology)
    server = make_server(bridge, "127.0.0.1", 0, base_uri="/sovd")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", bridge
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(url: str) -> tuple[int, dict | None, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
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
    assert body == {"items": [{"id": "nav", "name": "nav", "href": f"{base}/sovd/v1/components/nav"}]}
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
    assert grps == {"items": [{"id": "presence", "category": "sysInfo"}, {"id": "runtime", "category": "sysInfo"}]}


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
    assert body["data"] == {"value": 0.5} and body["schema"]["properties"]["value"]["type"] == "number"


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
    assert _get(f"{base}/sovd/v1/components/nav/data/x-zenode-presence")[1]["data"] == {"value": "gone"}


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
    req = urllib.request.Request(f"{base}/sovd/v1/components", headers={"Host": "robot:7690"})
    with urllib.request.urlopen(req, timeout=5) as r:
        body = json.loads(r.read())
    assert body["items"][0]["href"] == "http://robot:7690/sovd/v1/components/nav"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_sovd_server.py -q`
Expected: `ImportError`

- [ ] **Step 3: Write the server**

```python
# src/zenode/sovd/server.py
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


_V1 = re.compile(
    r"^/v1(?:/(?P<rest>.*))?$"
)
_COMPONENT = re.compile(
    r"^components/(?P<id>[^/]+)"
    r"(?:/(?P<sub>hosts|belongs-to|data-categories|data-groups|data|x-zenode-logs)"
    r"(?:/(?P<data_id>[^/]+))?)?$"
)


def _segment(value: str) -> str:
    """Percent-encode one path segment the way upstream does."""
    return quote(value, safe="")


class _Handler(BaseHTTPRequestHandler):
    bridge: CallBridge
    base: str
    call_timeout: float

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

    def _base_url(self) -> str:
        host = self.headers.get("Host", "localhost")
        return f"http://{host}{self.base}"

    def _v1(self) -> str:
        return f"{self._base_url()}/v1"

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

    def _route_exists(self) -> bool:
        path = urlsplit(self.path).path
        if not path.startswith(self.base):
            return False
        rest = path[len(self.base) :]
        return rest == "/version-info" or _V1.match(rest) is not None

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        path = split.path
        if not path.startswith(self.base + "/") and path != self.base:
            self._send(404, None)
            return
        rest = path[len(self.base) :]
        query = parse_qs(split.query, keep_blank_values=True)
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
            self._component(cm.group("id"), cm.group("sub"), cm.group("data_id"), query, with_schema)
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
            body.schema_ = Items[item_type].schema_of()  # type: ignore[valid-type]
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
            wanted = query.get("category", [None])[-1]
            rows = [g for g in groups(view, ns) if wanted is None or g.category == wanted]
            self._items(rows, False, Group)
        elif sub == "data" and data_id is None:
            self._data_list(view, query, with_schema)
        elif sub == "data":
            assert data_id is not None
            self._data_read(view, data_id, with_schema)
        elif sub == "x-zenode-logs":
            self._send(200, json.dumps({"items": [r.model_dump() for r in view.logs]}).encode())

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
            self._service_read(view, data_id, services[data_id].key, services[data_id].reply_schema, with_schema)
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
    bridge: CallBridge,
    host: str,
    port: int,
    *,
    base_uri: str = "/sovd",
    call_timeout: float = 2.0,
) -> ThreadingHTTPServer:
    """An HTTP server serving ``bridge`` under ``base_uri``.

    The handler class is built per call so the bridge binds as a class
    attribute, exactly as :func:`zenode.exporter.make_server` does.
    """
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {"bridge": bridge, "base": normalize_base(base_uri), "call_timeout": call_timeout},
    )
    return ThreadingHTTPServer((host, port), handler)
```

Two implementation notes. `Items[item_type].schema_of()` parametrizes the generic at runtime; if pyright or ty reject the dynamic subscript, precompute the four schemas at module import (`_ENTITY_ITEMS_SCHEMA = Items[EntityReference].schema_of()` etc.) and pick by `item_type`. The `x-zenode-logs` body is built with `json.dumps` of `model_dump()` rather than a `Wire` model because `LogRecordMsg` is already a pydantic model with the right keys; keep it that way.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_sovd_server.py -q`
Expected: all pass (19 tests)

- [ ] **Step 5: Commit**

```bash
git add src/zenode/sovd/server.py tests/test_sovd_server.py
git commit -m "feat(sovd): HTTP router with upstream's route set and the status mapping"
```

---

### Task 6: The sidecar — loop thread, subscriptions, capped call bridge

**Files:**
- Create: `src/zenode/sovd/sidecar.py`
- Modify: `src/zenode/sovd/__init__.py`
- Test: `tests/test_sovd_integration.py`

**Interfaces:**
- Consumes: `Topology`, `make_server`, `CallsSaturated`; `zenode.service.call_service`; `zenode.presence.PresenceWatcher`; `zenode.cli._declare_latched_subscriber`; `zenode.msgs.{info_pattern, health_pattern, log_pattern}`.
- Produces: `Sidecar(session, namespace, *, topology=None, call_timeout=2.0, max_calls=32)` with `start()`, `stop()`, `call(key) -> bytes`, attributes `topology`, `namespace`, `vendor_version`. Implements `CallBridge`.

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_sovd_integration.py
"""The sidecar against real nodes in the harness.

One shared in-process session: the harness's. The sidecar runs its own loop
thread on that session, exactly as `zenode sovd` does on its own session.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.request
from collections.abc import Iterator

import pytest
from pydantic import BaseModel

from zenode import Node, Service, TopicSet, metric, serve
from zenode.msgs import Empty
from zenode.sovd import Sidecar, make_server


class Pose(BaseModel):
    x: float = 0.0


class Svc(TopicSet):
    get_pose = Service("state/get_pose", request=Empty, reply=Pose, diagnostic="read")
    boom = Service("state/boom", request=Empty, reply=Pose, diagnostic="read")
    home = Service("motion/home", request=Empty, reply=Pose, diagnostic="operation")


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


def _get(url: str) -> tuple[int, dict | None]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        raw = e.read()
        return e.code, json.loads(raw) if raw else None


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


@pytest.mark.integration
async def test_a_node_is_served_end_to_end(zen):
    nav = await zen.start_node(Nav)
    sidecar = Sidecar(zen.session, zen.namespace, call_timeout=1.0)
    sidecar.start()
    server = make_server(sidecar, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/sovd/v1"
    try:
        await _wait_for(lambda: "nav" in sidecar.topology.names())
        await _wait_for(lambda: (v := sidecar.topology.get("nav")) is not None and v.health is not None and v.info is not None)

        status, body = await asyncio.to_thread(_get, f"{base}/components")
        assert status == 200 and body["items"][0]["id"] == "nav"

        _, body = await asyncio.to_thread(_get, f"{base}/components/nav/data/battery_soc")
        assert body["data"] == {"value": 0.5}

        _, body = await asyncio.to_thread(_get, f"{base}/components/nav/data/state.get_pose")
        assert body == {"id": "state.get_pose", "data": {"x": 1.5}}

        status, body = await asyncio.to_thread(_get, f"{base}/components/nav/data/state.boom")
        assert status == 502 and "no fix" in body["message"]

        status, _ = await asyncio.to_thread(_get, f"{base}/components/nav/data/motion.home")
        assert status == 404  # an operation is not a data resource

        await zen.stop_node(nav)
        await _wait_for(lambda: (v := sidecar.topology.get("nav")) is not None and v.presence == "gone")
        _, body = await asyncio.to_thread(_get, f"{base}/components/nav/data/x-zenode-presence")
        assert body["data"] == {"value": "gone"}
        status, _ = await asyncio.to_thread(_get, f"{base}/components/nav/data/state.get_pose")
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
        from zenode.sovd.server import CallsSaturated

        with pytest.raises(CallsSaturated):
            await asyncio.to_thread(sidecar.call, "state/get_pose")
    finally:
        sidecar.stop()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_sovd_integration.py -q`
Expected: `ImportError: cannot import name 'Sidecar'`

- [ ] **Step 3: Write the sidecar and wire the package init**

```python
# src/zenode/sovd/sidecar.py
"""The glue: one zenoh session, one loop thread, one capped call bridge.

HTTP handler threads must not touch asyncio, and ``call_service`` is a
coroutine, so the sidecar owns a private event loop on a daemon thread. The
presence watcher and every subscription callback hop onto that loop (the same
thread-boundary rule ``pubsub.py`` keeps); a handler thread submits a call with
``run_coroutine_threadsafe`` and waits. A semaphore caps what is in flight: a
client retrying in a loop must not become a queryable storm on the robot.

The session is *given*, not opened. The CLI opens one per process; the test
harness lends its own, which is how the sidecar runs in-process.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading

import zenoh

from .. import __version__
from ..cli import _declare_latched_subscriber
from ..codec import RawCodec
from ..msgs import health_pattern, info_pattern, log_pattern
from ..presence import PresenceWatcher
from ..service import call_service
from ..topic import Service
from .server import CallsSaturated
from .topology import Topology

_PASSTHROUGH: Service[bytes, bytes] = Service(
    "sovd/passthrough",
    request=bytes,
    reply=bytes,
    request_codec=RawCodec(zenoh.Encoding.APPLICATION_JSON),
    reply_codec=RawCodec(zenoh.Encoding.APPLICATION_JSON),
)
"""Bytes in, bytes out, labelled JSON on the wire. ``call_service`` takes the
resolved key separately, so this key is never used; the codecs are what make
the node's ``PydanticJsonCodec`` accept the request."""

_CALLER = "zenode-sovd"
"""The ``node`` label on the query attachment, for the node's trace ring."""


class Sidecar:
    def __init__(
        self,
        session: zenoh.Session,
        namespace: str,
        *,
        topology: Topology | None = None,
        call_timeout: float = 2.0,
        max_calls: int = 32,
    ) -> None:
        self.session = session
        self.namespace = namespace
        self.topology = topology if topology is not None else Topology(namespace)
        self.vendor_version = __version__
        self.call_timeout = call_timeout
        self._calls = threading.BoundedSemaphore(max_calls) if max_calls > 0 else None
        self._max_calls = max_calls
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="zenode-sovd-loop", daemon=True)
        self._watcher: PresenceWatcher | None = None
        self._subscriptions: list[object] = []

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._thread.start()
        topology = self.topology
        self._watcher = PresenceWatcher(self.session, self.namespace, topology.presence, self._loop)
        self._watcher.start()
        self._subscriptions = [
            _declare_latched_subscriber(
                self.session,
                info_pattern(self.namespace),
                lambda s: topology.offer_info(s.payload.to_bytes()),
            ),
            self.session.declare_subscriber(
                health_pattern(self.namespace),
                lambda s: topology.offer_health(s.payload.to_bytes()),
            ),
            self.session.declare_subscriber(
                log_pattern(self.namespace),
                lambda s: topology.offer_log(s.payload.to_bytes()),
            ),
        ]

    def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
        for sub in self._subscriptions:
            try:
                sub.undeclare()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._subscriptions = []
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    # ------------------------------------------------------------ the bridge

    def call(self, key: str) -> bytes:
        """From an HTTP thread: call ``key`` with ``{}`` and return the raw reply."""
        if self._max_calls <= 0:
            raise CallsSaturated
        assert self._calls is not None
        if not self._calls.acquire(blocking=False):
            raise CallsSaturated
        try:
            future = asyncio.run_coroutine_threadsafe(
                call_service(
                    self.session,
                    _PASSTHROUGH,
                    key,
                    b"{}",
                    timeout=self.call_timeout,
                    node=_CALLER,
                ),
                self._loop,
            )
            try:
                return future.result(timeout=self.call_timeout + 1.0)
            except concurrent.futures.TimeoutError:
                future.cancel()
                from ..errors import ServiceTimeout

                raise ServiceTimeout(f"no reply from {key} within {self.call_timeout}s") from None
        finally:
            self._calls.release()
```

Move the `ServiceTimeout` import to the top of the module with the others. The `topology.offer_*` callbacks run on zenoh threads directly; they only take the topology lock, which is what `exporter.Registry.offer` does too. The presence watcher is the one source that hops onto the loop, because `PresenceWatcher` is written that way.

Then make `src/zenode/sovd/__init__.py` the version shown in Task 2 Step 3 with the three imports.

- [ ] **Step 4: Run the integration tests and the whole SOVD set**

Run: `uv run pytest tests/test_sovd_integration.py tests/test_sovd_server.py -q`
Expected: all pass. If `test_a_node_is_served_end_to_end` times out waiting for `nav`, the harness namespace is `""` and `PresenceWatcher` with `history=True` replays the token; check that `zen.session` is passed and not a new session.

- [ ] **Step 5: Commit**

```bash
git add src/zenode/sovd/sidecar.py src/zenode/sovd/__init__.py tests/test_sovd_integration.py
git commit -m "feat(sovd): sidecar loop thread, subscriptions and capped call bridge"
```

---

### Task 7: `zenode sovd` on the CLI

**Files:**
- Modify: `src/zenode/cli.py` (new `cmd_sovd`, new subparser after `export`)
- Modify: `tests/test_cli_commands.py`

**Interfaces:**
- Consumes: `Sidecar`, `Topology`, `make_server`, `_parse_listen` (loopback default needs its own parse: `_parse_listen` defaults to all interfaces).
- Produces: `cmd_sovd(args) -> int`; flags `--listen`, `--base-uri`, `--retain-gone`, `--max-gone`, `--log-buffer`, `--max-calls`, `--call-timeout`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_commands.py` (it already imports `main`, `MagicMock`, `pytest`; add `cmd_sovd` to the `zenode.cli` import list):

```python
# ------------------------------------------------------------------------ sovd


def test_sovd_defaults_to_loopback_and_the_sovd_mount(monkeypatch, cli_args):
    """A SOVD client is anyone with a browser; the exporter's all-interfaces default does not transfer."""
    made: dict[str, object] = {}

    class _Server:
        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            made["closed"] = True

    class _Sidecar:
        def __init__(self, session, namespace, **kw) -> None:
            made["sidecar"] = (namespace, kw)

        def start(self) -> None:
            made["started"] = True

        def stop(self) -> None:
            made["stopped"] = True

    def _make_server(bridge, host, port, **kw):
        made["listen"] = (host, port, kw)
        return _Server()

    session = MagicMock()
    monkeypatch.setattr("zenode.cli._open_session", MagicMock(return_value=session))
    monkeypatch.setattr("zenode.sovd.Sidecar", _Sidecar)
    monkeypatch.setattr("zenode.sovd.make_server", _make_server)
    args = cli_args(
        listen="127.0.0.1:7690", base_uri="/sovd", retain_gone=600.0, max_gone=64,
        log_buffer=1000, max_calls=32, call_timeout=2.0, namespace="robodog",
    )
    assert cmd_sovd(args) == 0
    assert made["listen"] == ("127.0.0.1", 7690, {"base_uri": "/sovd", "call_timeout": 2.0})
    assert made["sidecar"][0] == "robodog"
    assert made["started"] and made["stopped"] and made["closed"]
    session.close.assert_called_once()


def test_sovd_argv_wiring(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr("zenode.cli.cmd_sovd", lambda args: captured.update(vars(args)) or 0)
    with pytest.raises(SystemExit) as e:
        main(["sovd", "--listen", ":7700", "--max-calls", "4"])
    assert e.value.code == 0
    assert captured["listen"] == ":7700" and captured["max_calls"] == 4
    assert captured["base_uri"] == "/sovd" and captured["retain_gone"] == 600.0
```

Check how existing argv tests patch command functions (`main` resolves `fn` via `set_defaults`, so patching the module attribute may not reach it). If `captured` stays empty, patch via the parser: look at how `test_main_*` tests around line 700 do it and follow that pattern.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli_commands.py -q -k sovd`
Expected: `ImportError: cannot import name 'cmd_sovd'`

- [ ] **Step 3: Add the command and the subparser**

In `src/zenode/cli.py`, after `cmd_export`:

```python
# ----------------------------------------------------------------------- sovd


def _parse_loopback_listen(value: str) -> tuple[str, int]:
    """Like :func:`_parse_listen`, but an empty host means loopback.

    The exporter exists to be scraped from elsewhere; the SOVD sidecar serves
    node identity, measurements and service results to anyone with a browser,
    so widening past the box is an explicit act.
    """
    host, _, port = value.rpartition(":")
    try:
        return host or "127.0.0.1", int(port)
    except ValueError:
        raise ConfigError(f"--listen: expected [host:]port, got {value!r}") from None


def cmd_sovd(args: argparse.Namespace) -> int:
    """Serve what nodes declare diagnosable over SOVD (ISO 17978)."""
    from .sovd import Sidecar, Topology, make_server

    transport = _transport_from_args(args)
    host, port = _parse_loopback_listen(args.listen)
    topology = Topology(
        transport.namespace,
        retain_gone=args.retain_gone,
        max_gone=args.max_gone,
        log_buffer=args.log_buffer,
    )
    session = _open_session(transport)
    sidecar = Sidecar(
        session,
        transport.namespace,
        topology=topology,
        call_timeout=args.call_timeout,
        max_calls=args.max_calls,
    )
    sidecar.start()
    server = make_server(sidecar, host, port, base_uri=args.base_uri, call_timeout=args.call_timeout)
    print(f"serving namespace {transport.namespace!r} → http://{host}:{port}{args.base_uri}/v1")
    print("Ctrl-C to stop…")
    try:
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        sidecar.stop()
        session.close()
```

The import is local because `zenode.sovd.sidecar` imports `_declare_latched_subscriber` from this module; a top-level import would be circular. In `main()`, after the `export` subparser:

```python
    p = sub.add_parser("sovd", help="serve declared diagnostics over SOVD (ISO 17978), read-only")
    p.add_argument("--listen", default="127.0.0.1:7690", metavar="[HOST:]PORT",
                   help="address to serve on (default 127.0.0.1:7690; loopback unless widened)")
    p.add_argument("--base-uri", default="/sovd", metavar="PATH",
                   help="mount path; the API lives under PATH/v1 (default /sovd)")
    p.add_argument("--retain-gone", type=float, default=600.0, metavar="SECONDS",
                   help="keep a departed node listed this long (default 600)")
    p.add_argument("--max-gone", type=int, default=64, metavar="N",
                   help="at most this many departed nodes retained (default 64)")
    p.add_argument("--log-buffer", type=int, default=1000, metavar="N",
                   help="log records kept per node (default 1000)")
    p.add_argument("--max-calls", type=int, default=32, metavar="N",
                   help="in-flight service calls before answering 503 (default 32)")
    p.add_argument("--call-timeout", type=float, default=2.0, metavar="SECONDS",
                   help="per service call; a miss is a 504 (default 2)")
    _add_common(p, contract=False)
    p.set_defaults(fn=cmd_sovd)
```

- [ ] **Step 4: Run the CLI tests and the full suite**

Run: `uv run pytest tests/test_cli_commands.py -q && uv run pytest -q 2>&1 | tail -1`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/zenode/cli.py tests/test_cli_commands.py
git commit -m "feat(cli): zenode sovd"
```

---

### Task 8: Conformance traversal against the pinned schemas

**Files:**
- Create: `tests/test_sovd_conformance.py`

**Interfaces:**
- Consumes: the fixtures from Task 1, `Sidecar` and `make_server` from Task 6.

- [ ] **Step 1: Write the test**

```python
# tests/test_sovd_conformance.py
"""Every response the sidecar emits, validated against opensovd-models.

The schemas under tests/sovd/schemas were generated once from the Rust models
at the commit named in tests/sovd/PIN. The traversal mirrors upstream's
test_api.py: version-info, root, components, each component's capabilities,
categories, groups, data list, each data item; then apps and areas. It never
follows hrefs, it rebuilds paths from ids, as upstream does.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.request
from pathlib import Path

import jsonschema
import pytest
from pydantic import BaseModel

from zenode import Node, Service, TopicSet, metric, serve
from zenode.msgs import Empty
from zenode.sovd import Sidecar, make_server

SCHEMAS = Path(__file__).parent / "sovd" / "schemas"


def _schema(name: str) -> dict:
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


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as r:
        assert r.status == 200, url
        return json.loads(r.read())


def _validate(body: dict, name: str) -> None:
    jsonschema.validate(body, _schema(name))


@pytest.mark.integration
async def test_every_response_validates_against_the_pinned_models(zen):
    await zen.start_node(Probe)
    sidecar = Sidecar(zen.session, zen.namespace)
    sidecar.start()
    server = make_server(sidecar, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
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
                assert {m["id"] for m in data["items"]} >= {"frames", "conf.state.get_pose", "sent"}
                for item in data["items"]:
                    body = _get(f"{here}/data/{item['id']}?include-schema=true")
                    _validate(body, "read_response")
                    jsonschema.validate(body["data"], body["schema"])  # as upstream's is_data_item leg
                _validate(_get(f"{here}/hosts"), "entities")
                _validate(_get(f"{here}/belongs-to"), "entities")
            _validate(_get(f"{root}/v1/apps"), "entities")
            _validate(_get(f"{root}/v1/areas"), "entities")

        await asyncio.to_thread(traverse)

        def error_shape() -> None:
            try:
                urllib.request.urlopen(f"{root}/v1/components/ghost", timeout=5)
            except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
                _validate(json.loads(e.read()), "generic_error")
                return
            raise AssertionError("expected 404")

        await asyncio.to_thread(error_shape)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        sidecar.stop()
```

Note the service key `conf/state/get_pose` is namespaced under the test's own prefix so the registry stays clean across modules; the harness namespace is `""`, so the service id is `conf.state.get_pose`.

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_sovd_conformance.py -q`
Expected: 1 passed. A failure names the schema and the offending key; fix the model in `model.py`, not the fixture.

- [ ] **Step 3: Commit**

```bash
git add tests/test_sovd_conformance.py
git commit -m "test(sovd): conformance traversal against the pinned models"
```

---

### Task 9: Docs, status line, full verification

**Files:**
- Create: `docs/sovd.md`
- Modify: `docs/cli.md` (new `### sovd` under "Forwarding telemetry", after `export`), `docs/index.md` (toctree entry after `open-telemetry`, status table row), `docs/design/opensovd-adapter.md` (status line)

- [ ] **Step 1: Write `docs/sovd.md`**

```markdown
# SOVD

`zenode sovd` serves what nodes declare diagnosable over
[ISO 17978](https://www.iso.org/standard/86587.html), Service-Oriented Vehicle
Diagnostics, so a standard SOVD client — a tester, a test bench,
[OpenSOVD](https://projects.eclipse.org/projects/automotive.opensovd)'s own
client and MCP server — can ask one robot one question. Read-only, out of
process, standard library only. The design and its trade-offs are in
[the design note](design/opensovd-adapter.md).

```bash
zenode sovd                              # 127.0.0.1:7690, mounted at /sovd
zenode sovd --listen 0.0.0.0:7690        # reachable off the box: put a proxy in front
```

```
GET http://robot:7690/sovd/version-info
GET http://robot:7690/sovd/v1/components
GET http://robot:7690/sovd/v1/components/nav/data?groups=app
GET http://robot:7690/sovd/v1/components/nav/data/state.get_pose?include-schema=true
```

## What is served

Every live node is a **component**. Its `data` collection holds, by category
and group:

| Category | Group | Items | Source |
|---|---|---|---|
| `identData` | `identity` | `node`, `host`, `zenode` | the descriptor |
| `sysInfo` | `runtime` | every `NodeHealth` field, and `state` | the heartbeat |
| `sysInfo` | `presence` | `x-zenode-presence`, `x-zenode-last-seen` | the sidecar |
| `currentData` | `app` | each `@metric` id | the heartbeat |
| `currentData` | `svc` | each service declared `diagnostic="read"`, as `<key with / as .>` | a service call, on request |

Values are `{"value": …}`; a service reply is served as the object it is.
Nothing is read from topics: a node that wants a value diagnosable keeps it in
a field and declares a read service for it (see
[Contracts](contracts.md#service)).

Log records ride under a custom link, `x-zenode-logs`, as the standard's
`/logs` shape is not public. Faults, operations, modes and configurations are
not advertised; a SOVD client discovers that from the component itself.

## Presence

| `x-zenode-presence` | Meaning |
|---|---|
| `live` | liveliness token held, heartbeats arriving |
| `silent` | token held, three `health_interval`s without a beat — the loop is probably wedged; service reads will time out |
| `gone` | token dropped; listed for `--retain-gone` seconds with its last heartbeat and logs, then forgotten |

## Statuses

| Situation | Status |
|---|---|
| unknown component | 404, `vendor_code: entity-not-found` |
| unknown data id, or a value that is unknown right now | 404 `error-response` |
| service call timed out, or the node is gone | 504 `not-responding` |
| the handler raised, or replied with something that is not JSON | 502 `error-response` |
| more than `--max-calls` calls in flight | 503 `sovd-server-failure` |

## Security

There is no authentication in the sidecar. It binds loopback by default; to
reach it from elsewhere, terminate TLS and authentication at a reverse proxy.
Phase 1 is read-only; operations arrive with mTLS, not before.

## Conformance

A SOVD-compatible subset, validated against the OpenSOVD JSON shapes at
`opensovd-core` commit `26953d97`, never "SOVD compliant". The route set,
`version-info` and the error vocabulary are that commit's.
```

- [ ] **Step 2: Add the CLI section to `docs/cli.md`**

After the `export` section's closing paragraph ("Full detail in [Observability]…"), insert:

```markdown
### `sovd`

A read-only SOVD (ISO 17978) server over what nodes declare diagnosable.

```bash
zenode sovd --listen 127.0.0.1:7690 --base-uri /sovd
```

| Option | Default | Effect |
|---|---|---|
| `--listen [HOST:]PORT` | `127.0.0.1:7690` | Loopback unless widened. |
| `--base-uri PATH` | `/sovd` | The API lives under `PATH/v1`; `version-info` under `PATH`. |
| `--retain-gone SECONDS` | `600` | Keep a departed node listed this long. |
| `--max-gone N` | `64` | Departed nodes retained, oldest dropped first. |
| `--log-buffer N` | `1000` | Log records kept per node. |
| `--max-calls N` | `32` | In-flight service calls before answering 503. |
| `--call-timeout SECONDS` | `2` | Per service call; a miss is a 504. |

See [SOVD](sovd.md) for what is served.
```

- [ ] **Step 3: Index and status line**

In `docs/index.md`, add `sovd` to the main toctree after `open-telemetry`, and change the adapter row's status from `decided` to `phase 1 built`. In `docs/design/opensovd-adapter.md`, change the status sentence to:

```markdown
**Status: Phase 1 implemented.** Drafted 2026-08-26 as a proposal, revised
2026-09-21 after the [storage research note](zenoh-storage-for-sovd.md),
decided 2026-10-02 after a design review that also absorbed the
[reuse research note](opensovd-core-reuse.md), and Phase 1 landed the same
day. User documentation is in [SOVD](../sovd.md); this page keeps the design
and its trade-offs.
```

- [ ] **Step 4: Run every check**

```bash
uv run ruff format src tests examples
uv run ruff check .
uv run pyright
uv run ty check src tests
uv run pytest -q
uv run sphinx-build -E -W -b html docs docs/_build/html 2>&1 | tail -1
```

Expected: all clean, `build succeeded.`

- [ ] **Step 5: Commit**

```bash
git add docs/sovd.md docs/cli.md docs/index.md docs/design/opensovd-adapter.md
git add -u src tests
git commit -m "docs: zenode sovd"
```

---

## Self-review

**Spec coverage.** §8 mapping: components flat (Task 5 root/components), services under their node from `serves` (Task 4), categories/groups/ids (Task 4), `x-zenode-logs` (Task 5), `/version-info` pin (Task 2/5), `/apps`, `/areas` empty (Task 5), nothing else advertised (Task 5 capabilities). §9: bus-only discovery with no `--contract` (Task 6), JSON pass-through and non-JSON skipped (Task 4 `readable_services`), stdlib only (all), upstream's exact route set including `hosts` and `belongs-to` (Task 5), one loop thread with `run_coroutine_threadsafe` and a cap (Task 6), status mapping (Task 5), no cache (Task 5 reads consult only the topology), loopback default (Task 7), public API only (Task 6 imports). §10 presence: live/silent/gone, retention, caps, log ring (Task 3). §11: fixtures from `opensovd-models` with the pin recorded and a zenode-owned traversal in CI marked `integration` (Tasks 1, 8); upstream's `test_api.py` by hand is a release step, not a task. §12 Phase 1 CLI (Task 7). Docs (Task 9).

**Placeholder scan.** None. Two steps say "if the checker rejects X, do Y" with Y spelled out.

**Type consistency.** `Topology.get -> NodeView | None` (Task 3) is what `resources.py` (Task 4) and `server.py` (Task 5) consume. `CallBridge.call(key) -> bytes` (Task 5) is what `Sidecar.call` (Task 6) implements, raising `ServiceTimeout`, `ServiceError` or `CallsSaturated`, all three handled in `_service_read`. `make_server(bridge, host, port, *, base_uri, call_timeout)` is called identically in Tasks 5, 6, 7, 8. `readable_services` returns `dict[str, ServiceInfo]` keyed by data id in Task 4 and is indexed that way in Task 5.
