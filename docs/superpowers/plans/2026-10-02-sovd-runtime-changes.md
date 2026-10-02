# SOVD Phase 1a: runtime changes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the five contract- and message-layer changes that the OpenSOVD sidecar needs, each useful to zenode on its own, with no sidecar code yet.

**Architecture:** Every change is at the contract (`topic.py`), message (`msgs/`) or reporter (`reporting.py`) layer. `Service` gains a `diagnostic` tri-state validated in the dataclass; the latched `NodeInfo` descriptor gains the service schemas, the heartbeat's own field catalog and the health interval; node names and namespace segments get a regex. The exporter's metric table stops owning kind/help/integral and reads them from the one catalog the runtime now publishes.

**Tech Stack:** Python 3.11+, pydantic v2, eclipse-zenoh, pytest (`asyncio_mode=auto`), ruff, pyright, ty, sphinx + myst.

**Spec:** `docs/design/opensovd-adapter.md`, §7 "The runtime changes" and §12 "Phases" (Phase 1). The sidecar itself is a separate plan.

## Global Constraints

- Runtime dependencies stay `eclipse-zenoh` and `pydantic`, nothing added.
- `topic.py` and `msgs/*` import no zenoh; `reporting.py` imports no zenoh.
- Node-name and namespace-segment regex, verbatim from the spec: `^[A-Za-z0-9_.-]+$`. Namespaces may contain `/`, names may not.
- `Service.diagnostic` values, verbatim: `Literal["read", "operation"] | None`, default `None`. `"read"` requires a request model with no fields.
- Runtime descriptor ids equal `NodeHealth` field names. The exporter keeps its `_total` suffix as a rendering rule.
- Docstrings carry the *why*, matching the surrounding files. Every behavior change edits the matching `docs/` page.
- Checks that must pass before the final commit: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format src tests examples`, `uv run pyright`, `uv run ty check src tests`, `uv run sphinx-build -W -b html docs docs/_build/html`.
- Commit messages are one short subject line in the repo's `type(scope): summary` style, no body, no attribution.

---

## File map

| File | Responsibility after this plan |
|---|---|
| `src/zenode/msgs/empty.py` (new) | `Empty`, the field-less request model |
| `src/zenode/msgs/__init__.py` | export `Empty`, `RUNTIME_MEASURES` |
| `src/zenode/topic.py` | `Diagnostic` literal, `Service.diagnostic` + validation, `validate_node_name`, `validate_namespace` |
| `src/zenode/msgs/health.py` | `RUNTIME_MEASURES`: one `MeasureDescriptor` per numeric `NodeHealth` field |
| `src/zenode/msgs/info.py` | `ServiceInfo` schema/diagnostic/encoding fields; `NodeInfo.runtime`, `NodeInfo.health_interval` |
| `src/zenode/exporter.py` | `Metric` reads kind/help/integral from a runtime descriptor; `deadline_misses_total` row |
| `src/zenode/reporting.py` | builds the richer `ServiceInfo`, fills `runtime` and `health_interval` |
| `src/zenode/node.py` | validates name and namespace; passes `health_interval` to the reporter |
| `src/zenode/config.py` | `TransportConfig.namespace` validator |
| `tests/test_topic.py`, `tests/test_reporting.py`, `tests/test_exporter.py`, `tests/test_node_lifecycle.py`, `tests/test_config.py`, `tests/test_msgs_empty.py` (new), `tests/test_runtime_measures.py` (new) | tests |
| `docs/contracts.md`, `docs/nodes.md`, `docs/configuration.md`, `docs/open-telemetry.md` | user docs |

---

### Task 0: Branch and commit the decided design

**Files:**
- Modify (already edited, uncommitted): `docs/design/opensovd-adapter.md`, `docs/design/live-topics.md`, `docs/design/zenoh-storage-for-sovd.md`, `docs/index.md`
- Add (untracked): `docs/design/opensovd-core-reuse.md`

The toctree in `docs/index.md` now lists `design/opensovd-core-reuse`, so the reuse note must be committed with it or the `-W` docs build fails. The untracked `.html`/`.png` diagrams stay untracked: the sidecar diagram still shows `read_only` and `--contract`, which the decided design dropped.

- [x] **Step 1: Create the branch off `dev`**

```bash
git switch -c feat/sovd-runtime
```

- [x] **Step 2: Verify the docs build passes with the reuse note present**

Run: `uv run sphinx-build -W -b html docs docs/_build/html 2>&1 | tail -1`
Expected: `build succeeded.`

- [x] **Step 3: Commit the design**

```bash
git add docs/design/opensovd-adapter.md docs/design/live-topics.md docs/design/zenoh-storage-for-sovd.md docs/design/opensovd-core-reuse.md docs/index.md
git commit -m "docs: decide the OpenSOVD adapter design"
```

---

### Task 1: `Empty`, the field-less request model

**Files:**
- Create: `src/zenode/msgs/empty.py`
- Modify: `src/zenode/msgs/__init__.py`
- Test: `tests/test_msgs_empty.py`

**Interfaces:**
- Produces: `zenode.msgs.Empty` — a pydantic `BaseModel` with no fields and `extra="forbid"`. Task 2's validation names it in its error message.

- [x] **Step 1: Write the failing test**

```python
# tests/test_msgs_empty.py
"""`Empty`: the request of a service that takes no arguments.

It exists so `Service(..., diagnostic="read")` has a canonical request type,
and it forbids extras so a caller cannot smuggle arguments into a read.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zenode.msgs import Empty


def test_empty_has_no_fields():
    assert Empty.model_fields == {}
    assert Empty().model_dump() == {}


def test_empty_rejects_arguments():
    """A read takes no arguments; one that arrives anyway is a caller bug, not a no-op."""
    with pytest.raises(ValidationError):
        Empty.model_validate({"axis": 1})


def test_empty_round_trips_as_an_empty_object():
    assert Empty.model_validate_json(b"{}") == Empty()
    assert Empty().model_dump_json() == "{}"
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_msgs_empty.py -q`
Expected: FAIL, `ImportError: cannot import name 'Empty' from 'zenode.msgs'`

- [x] **Step 3: Write the model**

```python
# src/zenode/msgs/empty.py
"""The request of a service that takes no arguments.

A ``Service`` declared ``diagnostic="read"`` is served as a SOVD *data*
resource, which is a GET with no body — so its request has to be a model with
no fields, and this is the canonical one. ``extra="forbid"`` rather than the
pydantic default of ignoring unknown keys: a read that receives arguments is a
caller that thinks it is calling something else, and silently dropping them
would hide exactly that.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Empty(BaseModel):
    """A request with no fields. Serializes as ``{}``."""

    model_config = ConfigDict(extra="forbid")
```

Then in `src/zenode/msgs/__init__.py` add the import and the `__all__` entry, keeping `__all__` sorted:

```python
from .empty import Empty
```

and `"Empty",` as the first `__all__` entry (it sorts before `"EntityInfo"`).

- [x] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_msgs_empty.py -q`
Expected: 3 passed

- [x] **Step 5: Commit**

```bash
git add src/zenode/msgs/empty.py src/zenode/msgs/__init__.py tests/test_msgs_empty.py
git commit -m "feat(msgs): add Empty, the field-less request model"
```

---

### Task 2: `Service.diagnostic`

**Files:**
- Modify: `src/zenode/topic.py:185-206` (the `Service` dataclass)
- Test: `tests/test_topic.py`

**Interfaces:**
- Consumes: `zenode.msgs.Empty` from Task 1 (tests only — `topic.py` must not import `msgs`, which imports `topic`).
- Produces: `zenode.topic.Diagnostic = Literal["read", "operation"]`, `DIAGNOSTICS: tuple[Diagnostic, ...]`, and `Service.diagnostic: Diagnostic | None = None`. Task 5 copies `service.diagnostic` onto `ServiceInfo`.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_topic.py`:

```python
# --------------------------------------------------------------------- diagnostic


class _NoFields(BaseModel):
    pass


class _WithField(BaseModel):
    axis: int = 0


def test_services_are_not_diagnosable_by_default():
    """Exposure to a diagnostic client is a deliberate act, so the default is off."""
    svc = Service("state/get_pose", request=_NoFields, reply=Msg)
    assert svc.diagnostic is None


def test_a_read_needs_a_request_without_fields():
    """A SOVD data resource is a GET with no body; a read with arguments has nowhere to put them."""
    Service("state/get_pose", request=_NoFields, reply=Msg, diagnostic="read")
    with pytest.raises(ContractError, match="no fields"):
        Service("state/get_pose", request=_WithField, reply=Msg, diagnostic="read")


def test_a_read_rejects_a_raw_request():
    """``bytes`` has no field list to be empty."""
    with pytest.raises(ContractError, match="no fields"):
        Service("state/blob", request=bytes, reply=bytes, diagnostic="read")


def test_an_operation_may_take_arguments():
    svc = Service("motion/home", request=_WithField, reply=Msg, diagnostic="operation")
    assert svc.diagnostic == "operation"


def test_an_unknown_diagnostic_is_a_contract_error():
    with pytest.raises(ContractError, match="diagnostic"):
        Service("x/y", request=_NoFields, reply=Msg, diagnostic="write")  # type: ignore[arg-type]
```

Check the file's existing imports: it must have `pytest`, `BaseModel`, `Service`, `ContractError` and the `Msg` model. If `ContractError` is not imported, add `from zenode import ContractError` (it is re-exported from `zenode`).

- [x] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_topic.py -q -k "diagnos or read or operation"`
Expected: FAIL with `TypeError: Service.__init__() got an unexpected keyword argument 'diagnostic'`

- [x] **Step 3: Add the field and its validation**

In `src/zenode/topic.py`, after `CONGESTION_CONTROLS` add:

```python
Diagnostic = Literal["read", "operation"]
"""How a diagnostic client may reach a service. ``"read"`` is served as a
side-effect-free data resource (an HTTP GET); ``"operation"`` is an action and
is only ever reachable by an explicit POST. ``None``, the default on
:class:`Service`, means the service is not exposed at all — what a robot shows
a diagnostic client is a decision for the contract, not a sidecar's config."""

DIAGNOSTICS: tuple[Diagnostic, ...] = get_args(Diagnostic)
```

Replace the `Service` class with:

```python
@dataclass(frozen=True)
class Service(Generic[Req, Rep]):
    """A request/reply endpoint, served over a zenoh queryable.

    Args:
        key: Hierarchical key, relative to the deployment namespace.
        request: Request payload type — a Pydantic model, or ``bytes``.
        reply: Reply payload type.
        request_codec: Wire format for the request. Defaults per ``request``.
        reply_codec: Wire format for the reply. Defaults per ``reply``.
        description: One line for humans; published on the node's descriptor
            and shown by diagnostic tooling.
        diagnostic: Whether, and how, a diagnostic client (the SOVD sidecar)
            may call this service. ``"read"`` becomes a data resource reachable
            by GET, so its ``request`` must be a model with no fields — a GET
            has no body, and a read that takes arguments would need a second
            serialization path nobody asked for. ``"operation"`` may act and
            is never reachable by GET. ``None`` is not exposed. A single
            tri-state rather than a ``read_only`` flag because a boolean
            conflates "safe to GET" with "externally visible", and visibility
            is the deliberate act.
    """

    key: str
    request: type[Req]
    reply: type[Rep]
    request_codec: Codec[Req] = _CODEC_UNSET
    reply_codec: Codec[Rep] = _CODEC_UNSET
    description: str = ""
    diagnostic: Diagnostic | None = None
    is_absolute: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        what = f"Service({self.key!r})"
        _validate_key(self.key, what=what)
        if self.diagnostic is not None and self.diagnostic not in DIAGNOSTICS:
            raise ContractError(
                f"{what}: diagnostic must be one of {DIAGNOSTICS} or None, got {self.diagnostic!r}"
            )
        if self.diagnostic == "read":
            # `model_fields` is pydantic's; `bytes` has none, and a model with
            # any is a read with arguments. `zenode.msgs.Empty` is the canonical
            # no-field request — named here rather than imported, because
            # `msgs` imports this module.
            fields = getattr(self.request, "model_fields", None)
            if fields is None or fields:
                raise ContractError(
                    f"{what}: diagnostic='read' needs a request model with no fields "
                    f"(zenode.msgs.Empty), got {self.request.__name__}"
                )
        if self.request_codec is None:
            object.__setattr__(self, "request_codec", default_codec(self.request))
        if self.reply_codec is None:
            object.__setattr__(self, "reply_codec", default_codec(self.reply))

    def resolve(self, namespace: str) -> str:
        return resolve_key(self.key, namespace, absolute=self.is_absolute)
```

Export the new names: in `src/zenode/__init__.py`, the `from .topic import (...)` block and `__all__` both list topic names (e.g. `Priority`, `PRIORITIES`); add `Diagnostic` and `DIAGNOSTICS` beside them, keeping each list sorted the way the file already sorts it.

- [x] **Step 4: Run the tests**

Run: `uv run pytest tests/test_topic.py -q`
Expected: all pass, including the five new ones

- [x] **Step 5: Commit**

```bash
git add src/zenode/topic.py src/zenode/__init__.py tests/test_topic.py
git commit -m "feat(contract): Service.diagnostic marks a service read, operation or hidden"
```

---

### Task 3: Validate node names and namespace segments

**Files:**
- Modify: `src/zenode/topic.py` (next to `_validate_key`)
- Modify: `src/zenode/node.py:225-239` (`Node.__init__`)
- Modify: `src/zenode/config.py` (`TransportConfig`)
- Test: `tests/test_node_lifecycle.py`, `tests/test_config.py`, `tests/test_topic.py`

**Interfaces:**
- Produces: `zenode.topic.validate_node_name(name: str, *, what: str) -> None` and `zenode.topic.validate_namespace(namespace: str, *, what: str) -> None`, both raising `ContractError`. `NAME_SEGMENT: re.Pattern[str]`.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_topic.py`:

```python
# --------------------------------------------------------------- names and namespaces

from zenode.topic import validate_namespace, validate_node_name


@pytest.mark.parametrize("name", ["nav", "arm-left", "cam.front", "Nav_2", "zenode-test-probe"])
def test_good_node_names(name):
    validate_node_name(name, what="Node")


@pytest.mark.parametrize("name", ["", "arm/left", "nav*", "a b", "nav?", "nav#1", "nav%", "näv"])
def test_bad_node_names(name):
    """A name becomes a key segment and, for diagnostics, a URL path segment."""
    with pytest.raises(ContractError, match="name"):
        validate_node_name(name, what="Node")


@pytest.mark.parametrize("namespace", ["", "robodog", "fleet/robot1", "a.b/c-d"])
def test_good_namespaces(namespace):
    validate_namespace(namespace, what="transport")


@pytest.mark.parametrize("namespace", ["/robodog", "robodog/", "a//b", "a b", "fleet/*", "ns?"])
def test_bad_namespaces(namespace):
    with pytest.raises(ContractError, match="namespace"):
        validate_namespace(namespace, what="transport")
```

Append to `tests/test_node_lifecycle.py` (the file already defines a `Quiet` node and imports `ContractError`, `local_transport`):

```python
def test_a_node_name_must_be_a_single_safe_segment():
    """`arm/left` passes key validation and then mis-parses in `node_name_from_key`."""

    class Slashed(Quiet):
        name = "arm/left"

    with pytest.raises(ContractError, match="name"):
        Slashed(transport=local_transport(""))


def test_an_explicit_namespace_is_validated_too():
    with pytest.raises(ContractError, match="namespace"):
        Quiet(transport=local_transport(""), namespace="bad namespace")
```

Append to `tests/test_config.py` (it imports `TransportConfig` or `load_transport_config`; use `TransportConfig` directly and import it from `zenode` if missing):

```python
def test_the_transport_namespace_is_validated():
    """A namespace prefixes every key; one zenoh cannot route is caught at load."""
    from pydantic import ValidationError

    from zenode import TransportConfig

    TransportConfig(namespace="fleet/robot1")
    with pytest.raises(ValidationError, match="namespace"):
        TransportConfig(namespace="fleet/*")
```

- [x] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_topic.py tests/test_node_lifecycle.py tests/test_config.py -q -k "name or namespace"`
Expected: `ImportError` for the validators in `test_topic.py`; the node and config tests fail because no error is raised.

- [x] **Step 3: Add the validators**

In `src/zenode/topic.py`, add `import re` to the imports and, directly after `_validate_key`:

```python
NAME_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
"""What a node name and each namespace segment may be. Stricter than
:func:`_validate_key` on purpose: a name becomes a segment of every reserved
key (``node/<name>/health``) and, for diagnostic tooling, a URL path segment,
and neither can carry ``/``, zenoh's ``*`` and ``$*`` wildcards, or the URL
delimiters ``?``, ``#`` and ``%``. Measure ids already take the same stance
for the same reason."""


def validate_node_name(name: str, *, what: str) -> None:
    """Reject a node name that cannot be a key segment or a path segment."""
    if not name:
        raise ContractError(f"{what}: name must not be empty")
    if not NAME_SEGMENT.match(name):
        raise ContractError(
            f"{what}: name {name!r} must match {NAME_SEGMENT.pattern} — "
            "one segment, no '/', wildcards or URL delimiters"
        )


def validate_namespace(namespace: str, *, what: str) -> None:
    """Reject a namespace whose segments cannot be key or path segments.

    Empty is allowed and means "no prefix". Segments are separated by ``/``,
    so ``fleet/robot1`` is fine and ``fleet/*`` is not.
    """
    if not namespace:
        return
    _validate_key(namespace, what=f"{what} namespace")
    for segment in namespace.split("/"):
        if not NAME_SEGMENT.match(segment):
            raise ContractError(
                f"{what}: namespace {namespace!r} has segment {segment!r}, "
                f"which must match {NAME_SEGMENT.pattern}"
            )
```

In `src/zenode/node.py`, import the two validators from `.topic` (the file already imports from `.topic`; extend that import) and in `__init__` replace

```python
        if not self.name:
            raise ContractError(f"{type(self).__name__} must set a class-level `name`")
```

with

```python
        if not self.name:
            raise ContractError(f"{type(self).__name__} must set a class-level `name`")
        validate_node_name(self.name, what=type(self).__name__)
```

and after `self.namespace = ...` add:

```python
        validate_namespace(self.namespace, what=type(self).__name__)
```

Keep the "must set a class-level `name`" check first: `tests/test_node_lifecycle.py::test_a_node_must_be_named` matches that message.

In `src/zenode/config.py`, extend the pydantic import to include `field_validator`, import `ContractError` alongside `ConfigError` is **not** needed — import the validator instead: `from .topic import validate_namespace`. Then on `TransportConfig` add, after the field declarations:

```python
    @field_validator("namespace")
    @classmethod
    def _namespace_is_routable(cls, value: str) -> str:
        # The contract layer owns the rule; pydantic only reports it. ValueError
        # is what pydantic turns into a ValidationError naming the field.
        try:
            validate_namespace(value, what="[transport]")
        except ContractError as e:
            raise ValueError(str(e)) from None
        return value
```

with `from .errors import ConfigError, ContractError`.

Check for an import cycle: `topic.py` imports `codec` and `errors`; `config.py` imports `errors` and now `topic`. `codec.py` must not import `config` — confirm with `grep -n "^from\|^import" src/zenode/codec.py`.

- [x] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass. If an existing test uses a name or namespace the regex rejects, the failure names it; the regex is the spec, so change the test's fixture string, not the regex.

- [x] **Step 5: Commit**

```bash
git add src/zenode/topic.py src/zenode/node.py src/zenode/config.py tests/test_topic.py tests/test_node_lifecycle.py tests/test_config.py
git commit -m "feat!: validate node names and namespace segments"
```

---

### Task 4: The heartbeat describes its own fields

**Files:**
- Modify: `src/zenode/msgs/health.py`
- Modify: `src/zenode/msgs/__init__.py`
- Modify: `src/zenode/exporter.py:75-250` (`Metric`, `COUNTERS`, `GAUGES`)
- Test: `tests/test_runtime_measures.py` (new), `tests/test_exporter.py`

**Interfaces:**
- Consumes: `MeasureDescriptor` from `zenode.msgs.info`.
- Produces: `zenode.msgs.health.RUNTIME_MEASURES: tuple[MeasureDescriptor, ...]` and `RUNTIME_BY_ID: dict[str, MeasureDescriptor]`. `exporter.Metric` keeps the attributes `name`, `kind`, `help`, `value`, `otlp`, `unit`, `integral` (the first three and `integral` now derived from `descriptor`). Task 5 puts `RUNTIME_MEASURES` on `NodeInfo.runtime`.

- [x] **Step 1: Write the failing tests**

```python
# tests/test_runtime_measures.py
"""The heartbeat's own fields, described once and shared by every consumer.

`RUNTIME_MEASURES` is the catalog `NodeInfo.runtime` publishes and the table
the exporter renders from. One source, so the Prometheus name, the OTLP name,
and what a diagnostic client shows cannot disagree about what a field means.
"""

from __future__ import annotations

from zenode.exporter import COUNTERS, GAUGES
from zenode.msgs import NodeHealth
from zenode.msgs.health import RUNTIME_BY_ID, RUNTIME_MEASURES

_NOT_MEASURES = {"node", "host", "state", "measures", "ts_ns"}


def test_every_numeric_health_field_is_described():
    """A field nobody described is a field nobody exports — deadline_misses was one."""
    numeric = set(NodeHealth.model_fields) - _NOT_MEASURES
    assert {d.id for d in RUNTIME_MEASURES} == numeric


def test_ids_are_unique_and_indexable():
    assert len(RUNTIME_BY_ID) == len(RUNTIME_MEASURES)


def test_counters_are_counters_and_integral():
    for id_ in ("sent", "received", "dropped", "stale", "handler_errors",
                "timer_overruns", "deadline_misses", "logs_dropped", "shm_fallbacks"):
        assert RUNTIME_BY_ID[id_].kind == "counter", id_
        assert RUNTIME_BY_ID[id_].integral, id_


def test_every_descriptor_has_a_description_and_a_unit():
    for d in RUNTIME_MEASURES:
        assert d.description, d.id
        assert d.unit, d.id


def test_the_exporter_table_covers_the_catalog_exactly_once():
    """The exporter renders *from* the catalog; a row without a descriptor is a second table."""
    exported = [m.descriptor.id for m in (*COUNTERS, *GAUGES)]
    assert sorted(exported) == sorted(d.id for d in RUNTIME_MEASURES)


def test_exporter_rows_take_kind_and_help_from_the_descriptor():
    by_name = {m.name: m for m in (*COUNTERS, *GAUGES)}
    assert by_name["sent_total"].kind == "counter"
    assert by_name["sent_total"].help == "Messages published."
    assert by_name["sent_total"].integral is True
    assert by_name["uptime_seconds"].kind == "gauge"
```

Append to `tests/test_exporter.py` (it has a `registry_with(...)`-style helper and `_lines`; follow the pattern of `test_counters_and_gauges_are_typed` at line 55 — read it and copy its setup):

```python
def test_deadline_misses_are_exported():
    """On the heartbeat since the start, in the exporter's table since now."""
    text = _render_one(deadline_misses=4)  # use the module's existing helper for one node
    assert "# TYPE zenode_node_deadline_misses_total counter" in text
    assert 'zenode_node_deadline_misses_total{namespace="robodog",node="camera"} 4' in text
```

Adapt `_render_one` to whatever helper the module actually uses to build a registry with one `NodeHealth` and render it — the test at line 62 (`test_labels_carry_node_and_namespace`) shows it.

- [x] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_runtime_measures.py tests/test_exporter.py -q`
Expected: `ImportError: cannot import name 'RUNTIME_BY_ID'`; the exporter test fails on the missing series.

- [x] **Step 3: Add the catalog to `msgs/health.py`**

Append to `src/zenode/msgs/health.py` (import `MeasureDescriptor` from `.info` at the top; `info.py` does not import `health.py`, so there is no cycle):

```python
RUNTIME_MEASURES: tuple[MeasureDescriptor, ...] = (
    MeasureDescriptor(id="uptime_s", unit="s", description="Time since this node started."),
    MeasureDescriptor(
        id="sent", unit="{message}", kind="counter", integral=True,
        description="Messages published.",
    ),
    MeasureDescriptor(
        id="received", unit="{message}", kind="counter", integral=True,
        description="Messages received.",
    ),
    MeasureDescriptor(
        id="dropped", unit="{message}", kind="counter", integral=True,
        description="Messages dropped by a full queue.",
    ),
    MeasureDescriptor(
        id="stale", unit="{message}", kind="counter", integral=True,
        description="Messages dropped past max_age.",
    ),
    MeasureDescriptor(
        id="handler_errors", unit="{error}", kind="counter", integral=True,
        description="Exceptions raised inside subscription, service and timer handlers.",
    ),
    MeasureDescriptor(
        id="timer_overruns", unit="{overrun}", kind="counter", integral=True,
        description="Timer deadlines missed because a body outran its interval.",
    ),
    MeasureDescriptor(
        id="deadline_misses", unit="{miss}", kind="counter", integral=True,
        description="Subscriptions that went silent past their deadline.",
    ),
    MeasureDescriptor(
        id="logs_dropped", unit="{record}", kind="counter", integral=True,
        description="Log records dropped before publishing, leaving `zenode logs` incomplete.",
    ),
    MeasureDescriptor(
        id="shm_fallbacks", unit="{message}", kind="counter", integral=True,
        description="Messages on a shm=True topic that published through the normal path.",
    ),
    MeasureDescriptor(
        id="cpu_percent", unit="%",
        description="Process CPU since the last heartbeat, as a percentage of one core.",
    ),
    MeasureDescriptor(
        id="rss_bytes", unit="By", integral=True, description="Process resident set size."
    ),
    MeasureDescriptor(
        id="queue_max_depth", unit="{message}", integral=True,
        description="Deepest any subscription queue got since the last heartbeat.",
    ),
    MeasureDescriptor(
        id="age_mean_ms", unit="ms",
        description="Publish-to-dequeue delay, mean over the last heartbeat interval.",
    ),
    MeasureDescriptor(
        id="age_max_ms", unit="ms",
        description="Publish-to-dequeue delay, worst case over the last heartbeat interval.",
    ),
    MeasureDescriptor(
        id="handler_mean_ms", unit="ms",
        description="Time spent inside handlers, mean over the last heartbeat interval.",
    ),
    MeasureDescriptor(
        id="handler_max_ms", unit="ms",
        description="Time spent inside handlers, worst case over the last heartbeat interval.",
    ),
)
"""The heartbeat's own numeric fields, described the way ``@metric`` describes
an application's. Ids are the :class:`NodeHealth` field names, so a consumer
reads a value by ``getattr(health, id)`` and formats it by descriptor, in any
language. Published on :attr:`zenode.msgs.NodeInfo.runtime` and rendered by
``zenode export``, which keeps its own series names (``sent_total``, seconds
instead of ``ms``) as rendering rules on top of this one table. Units are the
*wire* units: ``ms`` here, because that is what the field holds."""

RUNTIME_BY_ID: dict[str, MeasureDescriptor] = {d.id: d for d in RUNTIME_MEASURES}
```

Run `uv run ruff format src` afterwards; the layout above is illustrative.

Export from `src/zenode/msgs/__init__.py`: add `RUNTIME_MEASURES` to the `.health` import and to `__all__`.

- [x] **Step 4: Make `exporter.Metric` read from the catalog**

In `src/zenode/exporter.py`, import `RUNTIME_BY_ID` from `.msgs.health` and replace the `Metric` dataclass with:

```python
@dataclass(frozen=True, slots=True)
class Metric:
    """One exported field of ``NodeHealth``, in both wire formats.

    Shared by the Prometheus and OTLP paths on purpose: two tables would drift,
    and a field exported by one and not the other is the kind of gap nobody
    notices until a dashboard is silently missing a series. What the field
    *means* — kind, help, integral — comes from the runtime's own catalog,
    :data:`zenode.msgs.health.RUNTIME_MEASURES`, so this row only adds the two
    series names and the unit conversion; a third consumer reads the catalog
    off the bus and needs no table at all.
    """

    name: str
    """Prometheus name, without the ``zenode_node_`` prefix."""
    descriptor: MeasureDescriptor
    value: Callable[[NodeHealth], float | None]
    """``None`` omits the series rather than reporting zero: a node with no
    ``/proc`` has unknown CPU, and unknown is not idle."""
    otlp: str
    """OTLP name, dotted per OpenTelemetry convention. Collectors normalise it
    back to the Prometheus form, so both paths land on one series."""
    unit: str
    """UCUM, as *exported*: ``s`` where the wire field is ``ms``, because
    Prometheus wants base units and ``value`` does the conversion."""

    @property
    def kind(self) -> str:
        return self.descriptor.kind

    @property
    def help(self) -> str:
        return self.descriptor.description

    @property
    def integral(self) -> bool:
        return self.descriptor.integral
```

Then rewrite every row. Counter example:

```python
COUNTERS: tuple[Metric, ...] = (
    Metric("sent_total", RUNTIME_BY_ID["sent"], lambda h: h.sent, "zenode.node.sent", "{message}"),
    Metric("received_total", RUNTIME_BY_ID["received"], lambda h: h.received, "zenode.node.received", "{message}"),
    Metric("dropped_total", RUNTIME_BY_ID["dropped"], lambda h: h.dropped, "zenode.node.dropped", "{message}"),
    Metric("stale_total", RUNTIME_BY_ID["stale"], lambda h: h.stale, "zenode.node.stale", "{message}"),
    Metric("handler_errors_total", RUNTIME_BY_ID["handler_errors"], lambda h: h.handler_errors, "zenode.node.handler_errors", "{error}"),
    Metric("timer_overruns_total", RUNTIME_BY_ID["timer_overruns"], lambda h: h.timer_overruns, "zenode.node.timer_overruns", "{overrun}"),
    Metric("deadline_misses_total", RUNTIME_BY_ID["deadline_misses"], lambda h: h.deadline_misses, "zenode.node.deadline_misses", "{miss}"),
    Metric("shm_fallbacks_total", RUNTIME_BY_ID["shm_fallbacks"], lambda h: h.shm_fallbacks, "zenode.node.shm_fallbacks", "{message}"),
    Metric("logs_dropped_total", RUNTIME_BY_ID["logs_dropped"], lambda h: h.logs_dropped, "zenode.node.logs_dropped", "{record}"),
)
```

Gauges, keeping the existing names, OTLP names, units and the `/ 1000.0` conversions:

```python
GAUGES: tuple[Metric, ...] = (
    Metric("uptime_seconds", RUNTIME_BY_ID["uptime_s"], lambda h: h.uptime_s, "zenode.node.uptime", "s"),
    Metric("cpu_percent", RUNTIME_BY_ID["cpu_percent"], lambda h: h.cpu_percent, "zenode.node.cpu", "%"),
    Metric("rss_bytes", RUNTIME_BY_ID["rss_bytes"], lambda h: h.rss_bytes, "zenode.node.rss", "By"),
    Metric("queue_max_depth", RUNTIME_BY_ID["queue_max_depth"], lambda h: h.queue_max_depth, "zenode.node.queue_max_depth", "{message}"),
    Metric("message_age_mean_seconds", RUNTIME_BY_ID["age_mean_ms"], lambda h: h.age_mean_ms / 1000.0, "zenode.node.message_age_mean", "s"),
    Metric("message_age_max_seconds", RUNTIME_BY_ID["age_max_ms"], lambda h: h.age_max_ms / 1000.0, "zenode.node.message_age_max", "s"),
    Metric("handler_duration_mean_seconds", RUNTIME_BY_ID["handler_mean_ms"], lambda h: h.handler_mean_ms / 1000.0, "zenode.node.handler_duration_mean", "s"),
    Metric("handler_duration_max_seconds", RUNTIME_BY_ID["handler_max_ms"], lambda h: h.handler_max_ms / 1000.0, "zenode.node.handler_duration_max", "s"),
)
```

The three descriptions that the old table worded differently from the health docstrings (`handler_errors`, `timer_overruns`, `deadline_misses`) are now the catalog's; `tests/test_exporter.py::test_counters_and_gauges_are_typed` asserts on `sent_total`'s help, which is unchanged.

- [x] **Step 5: Run the tests**

Run: `uv run pytest tests/test_runtime_measures.py tests/test_exporter.py tests/test_otlp_metrics.py tests/test_cli_health.py -q`
Expected: all pass. If `test_otlp_metrics.py` counts metrics per resource, the count grows by one for `deadline_misses`; update that expected number.

- [x] **Step 6: Commit**

```bash
git add src/zenode/msgs/health.py src/zenode/msgs/__init__.py src/zenode/exporter.py tests/test_runtime_measures.py tests/test_exporter.py tests/test_otlp_metrics.py
git commit -m "feat(msgs): describe the heartbeat's own fields in one catalog"
```

---

### Task 5: The descriptor carries schemas, the runtime catalog and the health interval

**Files:**
- Modify: `src/zenode/msgs/info.py` (`ServiceInfo`, `NodeInfo`)
- Modify: `src/zenode/reporting.py` (`ReporterSources`, `build_info`)
- Modify: `src/zenode/node.py:279-295` (`ReporterSources(...)` construction)
- Test: `tests/test_reporting.py`

**Interfaces:**
- Consumes: `Service.diagnostic` (Task 2), `RUNTIME_MEASURES` (Task 4).
- Produces: `ServiceInfo.diagnostic: Diagnostic | None`, `ServiceInfo.description: str`, `ServiceInfo.request_schema: dict[str, Any]`, `ServiceInfo.reply_schema: dict[str, Any]`, `ServiceInfo.request_encoding: str`, `ServiceInfo.reply_encoding: str`; `NodeInfo.runtime: list[MeasureDescriptor]`, `NodeInfo.health_interval: float | None`; `ReporterSources.health_interval: float | None`.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_reporting.py` (it defines `SUM`, `_FakeServer`, `sources()` and `reporter()`; add `from zenode.msgs import Empty` and `from zenode.msgs.health import RUNTIME_MEASURES` to its imports):

```python
# ------------------------------------------------------------- descriptor: services


GET_POSE = Service(
    "state/get_pose", request=Empty, reply=Ping, diagnostic="read", description="Where am I."
)


def test_the_descriptor_says_how_a_service_may_be_called():
    r = reporter(servers=[_FakeServer(GET_POSE, "robodog/state/get_pose"), _FakeServer(SUM, "robodog/svc/sum")])
    by_key = {s.key: s for s in r.build_info().serves}
    assert by_key["robodog/state/get_pose"].diagnostic == "read"
    assert by_key["robodog/state/get_pose"].description == "Where am I."
    assert by_key["robodog/svc/sum"].diagnostic is None


def test_the_descriptor_carries_both_schemas():
    """A consumer that never imports the contract still knows the shapes."""
    r = reporter(servers=[_FakeServer(SUM, "robodog/svc/sum")])
    (info,) = r.build_info().serves
    assert info.request_schema == Ping.model_json_schema()
    assert info.reply_schema == Ping.model_json_schema()
    assert info.request_encoding == "application/json"
    assert info.reply_encoding == "application/json"


def test_a_raw_service_has_no_schema_but_names_its_encoding():
    raw = Service("svc/blob", request=bytes, reply=bytes)
    r = reporter(servers=[_FakeServer(raw, "robodog/svc/blob")])
    (info,) = r.build_info().serves
    assert info.request_schema == {}
    assert info.request_encoding == "application/octet-stream"


# -------------------------------------------------------- descriptor: runtime catalog


def test_the_descriptor_carries_the_runtime_catalog():
    """Every heartbeat field is described on the bus, like `measures` already is."""
    info = reporter().build_info()
    assert info.runtime == list(RUNTIME_MEASURES)


def test_the_descriptor_carries_the_health_interval():
    assert reporter(health_interval=2.0).build_info().health_interval == 2.0
    assert reporter(health_interval=None).build_info().health_interval is None
```

Also add `"health_interval": 2.0,` to the `defaults` dict in `sources()`.

- [x] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_reporting.py -q`
Expected: the new tests fail — `ReporterSources.__init__() got an unexpected keyword argument 'health_interval'` makes every test in the module fail until Step 3 lands, which is expected.

- [x] **Step 3: Extend the messages**

In `src/zenode/msgs/info.py`, add `from typing import Any, Literal` and `from ..topic import Diagnostic, resolve_key`, then replace `ServiceInfo` and extend `NodeInfo`:

```python
class ServiceInfo(BaseModel):
    """One service a node serves, with everything a caller that never imports
    the contract needs: the shapes, the wire encodings, and whether a
    diagnostic client may call it."""

    key: str
    request: str = ""
    reply: str = ""
    """Class names, for a human reading a table — see :attr:`EntityInfo.schema_name`."""
    diagnostic: Diagnostic | None = None
    """``"read"``, ``"operation"`` or not exposed — :attr:`zenode.Service.diagnostic`."""
    description: str = ""
    request_schema: dict[str, Any] = {}
    reply_schema: dict[str, Any] = {}
    """``model_json_schema()`` of the two models; ``{}`` for a ``bytes``
    payload. A few kilobytes per service, once per start on a latched key:
    cheap, and it is what lets a sidecar validate and document a call without
    a contract import."""
    request_encoding: str = ""
    reply_encoding: str = ""
    """The zenoh encoding string the codec declares, e.g. ``application/json``.
    A consumer that passes JSON through needs to know when it cannot."""
```

In `NodeInfo`, after `measures` add:

```python
    runtime: list[MeasureDescriptor] = []
    """The heartbeat's own fields, described the way ``measures`` describes the
    application's — :data:`zenode.msgs.health.RUNTIME_MEASURES`. On the bus so
    a consumer in any language reads a heartbeat without a table of its own."""
    health_interval: float | None = None
    """Seconds between this node's heartbeats, or ``None`` for a node that
    publishes none. A consumer cannot tell silence from a slow node without it;
    with it, three missed beats is the same rule a subscription ``deadline``
    uses."""
```

- [x] **Step 4: Build them in the reporter and feed the interval from the node**

In `src/zenode/reporting.py`, import `RUNTIME_MEASURES` from `.msgs.health` and add `health_interval: float | None` to `ReporterSources` after `zenode_version`, documented:

```python
    health_interval: float | None
    """Published on the descriptor so a consumer can judge silence."""
```

Add a helper after `_entity_info`:

```python
def _json_schema(model: type[Any]) -> dict[str, Any]:
    """``model_json_schema()`` where there is one; ``{}`` for ``bytes``."""
    schema = getattr(model, "model_json_schema", None)
    return schema() if callable(schema) else {}


def _service_info(service: Service[Any, Any], key: str) -> ServiceInfo:
    """One served service, as it appears on the descriptor."""
    return ServiceInfo(
        key=key,
        request=service.request.__name__,
        reply=service.reply.__name__,
        diagnostic=service.diagnostic,
        description=service.description,
        request_schema=_json_schema(service.request),
        reply_schema=_json_schema(service.reply),
        request_encoding=str(service.request_codec.encoding),
        reply_encoding=str(service.reply_codec.encoding),
    )
```

and in `build_info` replace the inline `ServiceInfo(...)` comprehension with `serves=[_service_info(server.service, server.key) for server in s.servers]`, and add `runtime=list(RUNTIME_MEASURES)` and `health_interval=s.health_interval`.

In `src/zenode/node.py`, in the `ReporterSources(...)` call add `health_interval=self.health_interval,` after `zenode_version=__version__,`.

- [x] **Step 5: Run the tests**

Run: `uv run pytest tests/test_reporting.py tests/test_info.py tests/test_integration.py -q`
Expected: all pass.

- [x] **Step 6: Commit**

```bash
git add src/zenode/msgs/info.py src/zenode/reporting.py src/zenode/node.py tests/test_reporting.py
git commit -m "feat(info): descriptor carries service schemas, the runtime catalog and the health interval"
```

---

### Task 6: Docs, lint, type checks, full verification

**Files:**
- Modify: `docs/contracts.md:121-137` (Service section)
- Modify: `docs/nodes.md:36` (class attributes table) and `docs/nodes.md:296-304` (descriptor paragraph)
- Modify: `docs/configuration.md:87` (namespace row)
- Modify: `docs/open-telemetry.md` (wherever the `zenode_node_*` series are listed — find with `grep -n logs_dropped docs/open-telemetry.md`)

- [x] **Step 1: Document `diagnostic` and `Empty` in `docs/contracts.md`**

In the Service parameter table add two rows after `request_codec / reply_codec`:

```markdown
| `description` | `""` | One line for humans, published on the node's descriptor. |
| `diagnostic` | `None` | `"read"`: callable by a diagnostic client as a side-effect-free data resource; needs a request with no fields (`zenode.msgs.Empty`). `"operation"`: an action, never reachable by GET. `None`: not exposed. |
```

After the paragraph ending "No server produces `ServiceTimeout`." add:

```markdown
`diagnostic` is how a service opts into the [OpenSOVD sidecar](design/opensovd-adapter.md).
The default is off: what a robot shows a diagnostic client is decided here,
in the contract, not in the sidecar's configuration.

```python
GET_POSE = Service("state/get_pose", request=Empty, reply=Pose, diagnostic="read")
HOME     = Service("motion/home", request=AxisId, reply=Ack, diagnostic="operation")
```
```

- [x] **Step 2: Document the name rule and the descriptor fields in `docs/nodes.md`**

Change the `name` row of the class attributes table to:

```markdown
| `name` | — | Required. Identifies the node on the network and in logs. One segment matching `[A-Za-z0-9_.-]+`: it becomes part of every reserved key and, for diagnostics, a URL path. |
```

In the Measurements section, replace the paragraph starting "Unit, kind and description travel separately" with:

```markdown
Unit, kind and description travel separately, on the latched
`<ns>/node/<name>/info` descriptor, so they are not repeated at the heartbeat
rate. The same descriptor describes the heartbeat's own fields (`runtime`),
carries the node's `health_interval` so a consumer can tell silence from a
slow node, and lists each served service with its JSON schemas and
`diagnostic` flag. `zenode health` prints the values under its table and
`zenode export` exports them as `zenode_app_<id>`; see
[Observability](open-telemetry.md#application-metrics) for the export detail and
for where the line between a measurement and a read-only service falls.
```

- [x] **Step 3: Document the namespace rule in `docs/configuration.md`**

Change the `namespace` row to:

```markdown
| `namespace` | `""` | Prefixed to every relative key. Segments separated by `/`, each matching `[A-Za-z0-9_.-]+`; `fleet/robot1` is fine, `fleet/*` is rejected at load. |
```

- [x] **Step 4: Add `deadline_misses_total` to the exporter's series list in `docs/open-telemetry.md`**

Run `grep -n "logs_dropped" docs/open-telemetry.md`. Where the node series are listed, add `zenode_node_deadline_misses_total` with the help text "Subscriptions that went silent past their deadline." in the same format as its neighbours. If the list is a table with an OTLP column, the OTLP name is `zenode.node.deadline_misses`.

- [x] **Step 5: Run every check**

```bash
uv run ruff format src tests examples
uv run ruff check .
uv run pyright
uv run ty check src tests
uv run pytest -q
uv run sphinx-build -W -b html docs docs/_build/html 2>&1 | tail -1
```

Expected: ruff clean, pyright and ty report 0 errors, pytest all passed, `build succeeded.`

If pyright objects to `Diagnostic | None` on a frozen dataclass default or to the `getattr(..., "model_fields")` narrowing, annotate the local (`fields: dict[str, Any] | None = getattr(...)`) rather than adding an ignore.

- [x] **Step 6: Commit**

```bash
git add docs/contracts.md docs/nodes.md docs/configuration.md docs/open-telemetry.md
git add -u src tests
git commit -m "docs: diagnostic services, name rules, and the richer descriptor"
```

---

## Self-review

**Spec coverage.** §7 lists five changes: `Service.diagnostic` (Task 2, with `Empty` from Task 1), `ServiceInfo` fields (Task 5), `NodeInfo.runtime` with the table moved to `msgs/health.py` and consumed by the exporter, surfacing `deadline_misses` (Task 4 + 5), `NodeInfo.health_interval` (Task 5), name and namespace validation as a breaking change (Task 3). §9's "a service whose codec is not the JSON default is listed with its encoding" is the `request_encoding`/`reply_encoding` pair in Task 5. Docs for each are in Task 6. The sidecar, `zenode sovd`, the presence model and conformance fixtures are out of scope for this plan by design.

**Placeholders.** None; every step has its code or exact command. Two steps say "adapt to the module's existing helper" for `tests/test_exporter.py` and point at the line that shows it.

**Type consistency.** `Diagnostic` is defined in Task 2 and imported by Task 5's `info.py`. `RUNTIME_MEASURES` / `RUNTIME_BY_ID` are defined in Task 4 and consumed in Tasks 4 and 5. `ReporterSources.health_interval` is added in Task 5 and the test helper updated in the same task. `Metric.kind/help/integral` remain attributes (as properties), so `otlp_metrics.py` and `exporter.render` need no change.
