# Node-defined measurements + `host`

## Context

`NodeHealth` is a closed set: every field is computed by the runtime in
`Node._publish_health` (`src/zenode/node.py:871`), and an application has no way to put a
number of its own on the heartbeat. `docs/open-telemetry.md:186` currently states this as a
fact and redirects users to the OpenTelemetry metrics SDK — which contradicts the project's
own dependency stance for the values people actually want (pack SoC, frames processed).
Separately, nothing on the bus says which *machine* a node runs on: the presence key is a
name, `NodeHealth.node` is the same name, and a zenoh ZID identifies a session, not a host.

`docs/design/node-measures.md` proposes both as additive fields on one message. The user wants
this landed before any work starts on `docs/design/opensovd-adapter.md`, which depends on it:
the SOVD note's §11 names heartbeat-borne measurements as the answer to "a wedged node answers
no service calls", and its §8 blocks the Component/App entity split on `host` existing.

**Decisions taken during planning** (all four are open questions in the design notes):

1. **Catalog transport — fold into `NodeInfo`.** `docs/design/live-topics.md` §6 argues the
   measure catalog should be a `measures:` field on a broader per-node descriptor rather than a
   standalone `node/<name>/measures` topic, so the wire is not revised twice. We build
   `zenode/msgs/info.py` now. The `zenode topics --live` CLI from that note is **not** in
   scope — live-topics §8 notes the runtime side stands alone.
2. **Decorator — `@metric(id, unit=, kind=, integral=, description=)`**, one decorator, aligned
   with Prometheus/OTel vocabulary.
3. **`host` override — `[transport] host`**, beside `namespace`, the other deployment label a
   node already reads off `TransportConfig`. Zero new plumbing.
4. **`host` on Prometheus — a label on every `zenode_node_*` series**, as node-measures.md §1
   describes.

## Approach

Three layers, in dependency order.

**Contract.** A `@metric` decorator in `declarative.py` that only stamps metadata and returns
the function unchanged — the existing idiom (`declarative.py:65`), so a measurement stays
directly callable in a test. Validation is at decoration time (id shape, reserved names) and at
class-definition time (duplicate ids), because `NodeConfig(extra="forbid")` and
`Node.__init_subclass__` already set the precedent that typos fail at import, not on the first
message.

**Runtime.** `NodeHealth` gains `host: str` and `measures: dict[str, float]`. A new latched
`<ns>/node/<name>/info` topic carries the cold half — the `MeasureDescriptor` list plus the
entity lists `NodeInfo` is specified with. Latched rather than queryable is the whole point:
`Node.serve` dispatches to the event loop, so a wedged node answers no query, while its cached
descriptor is still served by zenoh's own threads.

**Consumers.** `exporter.Metric`'s "one table, because two would drift" invariant is preserved
by making the catalog *become* the table for application measurements, read by the Prometheus
and OTLP paths alike. `zenode health` grows a `HOST` column and per-node measurement rows.

Bounding is by *declaration*, not by a runtime cap: the set of ids is fixed once the class body
executes, which is what keeps `exporter.py`'s cardinality guarantee checkable.

## File changes

### Contract layer

**Modify `src/zenode/topic.py`** — extract `topic_flags(topic: Topic[Any]) -> list[str]`,
lifting the flag-string construction currently inlined in `cli.py:116-135`
(`latched(1)`, `max_age=…`, `trace@…`, `shm`, `prio=…`, `express`). `cmd_topics` and the new
descriptor builder both call it, so the two renderings cannot drift. No zenoh import, so the
contract stays tooling-importable.

**Modify `src/zenode/config.py`** — `TransportConfig.host: str = ""`, documented as a
deployment label that nothing is addressed by. Env override `ZENODE_TRANSPORT__HOST` and TOML
`[transport] host` come free from the existing loader.

**Modify `src/zenode/declarative.py`** — add:

```python
METRICS_ATTR = "__zenode_metric__"

@dataclass(frozen=True)
class Measurement:
    id: str
    unit: str = ""
    kind: Literal["gauge", "counter"] = "gauge"
    integral: bool = False
    description: str = ""
    attr: str = ""          # filled by collect_metrics, resolved via getattr at call time

def metric(id: str, *, unit="", kind="gauge", integral=False, description="") -> Callable[[F], F]
def collect_metrics(cls: type) -> dict[str, Measurement]
```

- `metric()` validates at decoration: `^[a-z][a-z0-9_]*$`, not a `NodeHealth.model_fields`
  name, `kind` in `("gauge", "counter")`, and refuses stacking two `@metric`s on one method —
  each raising `ContractError`.
- `collect_metrics` walks `reversed(cls.__mro__)` and keys by **id**, mirroring
  `collect_bindings` (`declarative.py:258`) so a subclass replaces a parent's measurement by
  redeclaring its id, and an undecorated override inherits it (resolution is `getattr(self, attr)`,
  as `_wire_bindings` does). Two attributes in *one* class body sharing an id is a
  `ContractError`.

**Create `src/zenode/msgs/info.py`** — the descriptor, with builders beside the existing
`health_key`/`log_key`/`trace_key`:

```python
def info_key(name: str) -> str:      # node/<name>/info
def info_pattern(namespace: str) -> str

class EntityInfo(BaseModel):
    key: str                 # resolved and absolute — a namespace mismatch is what this shows
    schema_name: str = ""
    flags: list[str] = []

class ServiceInfo(BaseModel):
    key: str
    request: str = ""
    reply: str = ""

class MeasureDescriptor(BaseModel):
    id: str
    unit: str = ""
    kind: Literal["gauge", "counter"] = "gauge"
    integral: bool = False
    description: str = ""

class NodeInfo(BaseModel):
    node: str
    host: str = ""
    zenode: str = ""
    publishes: list[EntityInfo] = []
    subscribes: list[EntityInfo] = []
    serves: list[ServiceInfo] = []
    measures: list[MeasureDescriptor] = []
```

Two deviations from the design notes, both deliberate:

- `EntityInfo.schema_name`, not `schema` — verified: pydantic v2 warns
  `Field name "schema" shadows an attribute in parent "BaseModel"`.
- `MeasureDescriptor` drops the note's `name` field. `id` *is* the name; two identifiers for
  one thing is the drift the single-table rule exists to prevent.

`ServiceInfo.read_only` is deliberately **not** added — that is the SOVD note's own contract
change (`opensovd-adapter.md` §6), and adding a defaulted optional field later is additive.

**Modify `src/zenode/msgs/health.py`** — `host: str = ""` and `measures: dict[str, float] = {}`,
each with a docstring carrying the *why* (`host` is a deployment label, not an identity; a
measurement absent from the dict is unknown, not zero). Both are defaulted on a plain
`BaseModel`, so old↔new publishers and sidecars interoperate in both directions — no
coordinated rollout.

**Modify `src/zenode/msgs/__init__.py`** — export the `info` symbols alongside the existing
`health_key`/`log_key` re-exports.

### Runtime

**Modify `src/zenode/node.py`:**

- `__init__`: `self.__host = self.__transport.host or socket.gethostname()`,
  `self.__metrics = collect_metrics(type(self))`, `self.__measure_errors = 0`,
  `self.__decl_version = 0`, `self.__info_version = -1`, `self.__info_pub = None`.
- `__init_subclass__` (`node.py:181`): call `collect_metrics(cls)` so a duplicate id raises at
  import, next to the existing reserved-name guard. No new entry in `_OVERRIDE_POINTS` — `host`
  comes from config, not a ClassVar, so the subclass guard is untouched.
- `publisher()`, `subscribe()`, `serve()`: `self.__decl_version += 1`. These are public API a
  handler may call after start, and this is what makes the latched snapshot self-correcting.
- `_publish_health()` (`node.py:871`): add `host=self.__host`, `measures=self._sample_metrics()`,
  and fold `self.__measure_errors` into the `handler_errors` sum. Then republish the descriptor
  if `__decl_version != __info_version`.
- New `_sample_metrics() -> dict[str, float]`: for each `Measurement`, call
  `getattr(self, m.attr)()` at the point `cpu_percent` is computed. `None` **or non-finite**
  omits the value (a NaN would serialize as invalid OTLP JSON downstream); a raising callable
  is logged, counted in `__measure_errors`, and omits that one value. **The heartbeat itself
  never fails** — a broken measurement must not take the liveness signal with it.
- New `_publish_info()`: builds `NodeInfo` from `self.__publishers` / `self.__subscriptions` /
  `self.__servers` (using `topic_flags`) plus `self.__metrics`, and puts it. Wrapped in
  `try/except` and logged — a descriptor is diagnostics, and "degrade, never crash" applies.
  The zenode version comes from a function-local `from . import __version__` (module-level
  would be a cycle: `zenode/__init__.py` imports `node`).
- `start()` (`node.py:325`): the info publisher is declared in the `health_interval is not None`
  block beside the health publisher, and `_publish_info()` is called **last**, after
  `_wire_bindings()` and after the trace `serve()` — so the first snapshot is complete. The
  order in `start()` is load-bearing and this is the only safe insertion point.

Both new publishers use the existing `Topic(..., latched=True)` / `priority="data_low"` path;
no change to `pubsub.py` or `service.py`, so the thread-boundary invariant is untouched.

### Consumers

**Modify `src/zenode/exporter.py`:**

- Module docstring: *bounded by construction* → *bounded by declaration*, with the reason.
- `Registry` gains `offer_info(payload)` and a `dict[str, NodeInfo]` under the same lock;
  `snapshot()` returns catalogs alongside samples so a scrape and a push report the same thing.
- `_labels()` gains `host` (empty `host` is omitted rather than emitted as `host=""`).
- `render()` grows an application-measurement block after the static table: series
  `zenode_app_<id>` (`zenode_app_<id>_total` for `kind="counter"`), `TYPE` from the descriptor,
  `HELP` from `description` with the unit appended when set.
- Conflict rule, stated in a comment: one Prometheus name cannot carry two types. Where two
  nodes declare the same id with a different `kind`, the descriptor from the first node in
  sorted order wins and disagreeing nodes' points are omitted, logged once.
- Values with no descriptor yet are omitted rather than guessed — latched history makes the gap
  transient, and a series whose `TYPE` flips mid-history is worse than a short hole.

**Modify `src/zenode/otlp_metrics.py`:**

- `encode()` takes the catalogs and emits app measurements as `zenode.app.<id>`, `sum` for
  counters and `gauge` otherwise, `asInt`/`asDouble` from `integral` — the flag exists for
  exactly this (`otlp_metrics.py:58-70`).
- `host.name` resource attribute when `host` is non-empty, beside the existing `service.name` /
  `service.namespace` — the OpenTelemetry semantic convention.
- Counters need no new machinery: application counters share the node's start instant, so
  `_start_ns` (`otlp_metrics.py:73`) already derives the right `startTimeUnixNano` and a restart
  reads as a reset.

**Modify `src/zenode/cli.py`:**

- `_HEALTH_HEADER` / `_health_row`: a `HOST` column.
- `cmd_health`: a second subscription to `info_pattern` for the catalog, and per-node
  measurement rows printed under the table (`  battery_soc = 0.87 [1]`), units from the catalog
  where known. Rows only for nodes reporting measurements, so a fleet without any is unchanged.
- `cmd_export`: a subscription feeding `registry.offer_info`.
- `cmd_topics`: use `topic_flags()` instead of the inline construction.
- **Both info subscriptions must be `zext.declare_advanced_subscriber` with
  `HistoryConfig(detect_late_publishers=True, max_samples=1)`** — a plain subscriber receives no
  zenoh-ext cache, so a sidecar started after the nodes would never see a descriptor. `cmd_echo`
  (`cli.py:162`) is the existing precedent.

**Modify `src/zenode/__init__.py`** — export `metric` (import + `__all__`).

**Modify `examples/talker.py`** — one `@metric` so the end-to-end check below has something to
show. Keeps the examples the documentation they already are.

### Docs

- `docs/conventions.md` — §1 gains a line that the unit rules cover measurements (SoC as
  0.0–1.0, radians never `*_deg`); §5's reserved-key table gains `node/<name>/info` and, while
  there, the `log` and `trace` rows it is already missing.
- `docs/open-telemetry.md` — rewrite §"Application metrics" (`:184-223`), which currently states
  `NodeHealth` "is not extensible" and hands the user to the OTel SDK. Replace with `@metric`,
  the catalog, the `zenode_app_` prefix, and the fence: **scalars that describe a node's health
  go on the heartbeat; application state goes behind a read-only service.** Add `host` to the
  health-metrics table.
- `docs/nodes.md` — a Measurements subsection in the health material: declaring one, `None` is
  unknown, must not block, evaluated from the health timer.
- `docs/cli.md` — the new `health` column and rows.
- `docs/api/msgs.rst` — a `zenode.msgs.info` automodule stanza.
- `docs/design/node-measures.md`, `docs/design/live-topics.md` — status headers updated to say
  what shipped and what did not (the descriptor did; `zenode topics --live` did not). `docs/index.md`'s
  design-note table follows.

### Tests

New `tests/test_measures.py` (decoration validation, MRO collection and override-by-id,
duplicate detection at class definition, `None`/raising/non-finite handling, `handler_errors`
accounting) and `tests/test_info.py` (key/pattern symmetry in the style of
`test_cli_health.py:26`, descriptor contents, republish on a post-start `subscribe()`).
Extensions to `test_exporter.py` (app series, TYPE/HELP from the catalog, kind conflict, `host`
label), `test_otlp_metrics.py` (`asInt`/`asDouble`, `host.name`), `test_cli_health.py` (row
width against `_HEALTH_HEADER`, measurement rows), `test_config.py` (`[transport] host` + env),
and one `@pytest.mark.integration` round trip in `test_integration.py` alongside
`test_health_heartbeat` (`:231`) asserting a measurement reaches the heartbeat and the catalog
reaches a late-joining advanced subscriber.

## Acceptance criteria

1. `@metric("battery_soc", unit="1")` on a node method leaves the method directly callable:
   `node._soc()` returns the float in a test with no node started.
2. `@metric("Battery-SOC")`, `@metric("uptime_s")` (a `NodeHealth` field name), and two
   attributes in one class body declaring id `x` each raise `ContractError` at import.
3. A subclass redeclaring id `battery_soc` on a different attribute replaces the parent's;
   an undecorated override of the parent's method is the one called.
4. A started node's `NodeHealth.measures` contains `battery_soc`; a measurement returning `None`
   is absent from the dict rather than `0.0`; one that raises is absent, is counted in
   `handler_errors`, and the heartbeat is still published.
5. `NodeHealth.host` equals `socket.gethostname()` by default and `[transport] host = "jetson"`
   otherwise; `ZENODE_TRANSPORT__HOST` overrides both.
6. A subscriber declared **after** a node started receives that node's `NodeInfo` from the
   latched cache, containing its measure descriptors and its publish/subscribe/serve entities.
7. Calling `node.subscribe(...)` from a handler after start causes exactly one republished
   `NodeInfo` on the next heartbeat, and none on the heartbeats after that.
8. `render()` emits `# TYPE zenode_app_frames_processed_total counter` and
   `zenode_app_battery_soc{host="…",namespace="…",node="…"} 0.87`; a node whose descriptor has
   not arrived contributes no app series.
9. `encode()` carries `battery_soc` as `asDouble` and `frames_processed` as `asInt` under
   `zenode.app.*`, with `host.name` on the resource.
10. `zenode health` prints a `HOST` column, `_health_row(...)` is exactly `len(_HEALTH_HEADER)`
    wide, and measurement rows appear only for nodes reporting them.
11. `uv run pytest -q`, `uv run ruff check .`, `uv run pyright`, `uv run ty check src tests`
    and `uv run sphinx-build -W -b html docs docs/_build/html` all pass.

## Verification

```bash
uv run pytest -q                                  # full suite
uv run pytest -m "not integration" -q             # fast pass
uv run ruff check . && uv run ruff format src tests examples
uv run pyright && uv run ty check src tests
uv run sphinx-build -W -b html docs docs/_build/html
```

End to end, three terminals — this is what proves the latched catalog and the sidecar, which no
unit test covers together:

```bash
uv run python examples/talker.py
uv run zenode health --watch --connect tcp/127.0.0.1:17447    # HOST column + measurement rows
uv run zenode export --prometheus :9100 --connect tcp/127.0.0.1:17447
curl -s localhost:9100/metrics | grep -E 'zenode_app_|host='
```

The load-bearing case is starting `zenode export` **after** the talker: the app series must
appear, which only works if the descriptor came from the zenoh-ext history query.

Edge cases to exercise by hand: a node with `health_interval = None` (no heartbeat, no
descriptor, no crash); a measurement that raises every tick (`handler_errors` climbs, heartbeat
continues); two nodes declaring the same id with different `kind` (one wins, the other is
omitted and logged once).

## Risks

**One Prometheus name, two types.** Two nodes declaring the same id with different `kind` cannot
both be exported. Mitigated by the sorted-first-wins rule plus a log line, and by documenting
that an id means the same thing fleet-wide.

**Adding `host` to every series changes label sets.** Chosen deliberately over the info-metric
join. At 0.1.x with no shipped dashboards the cost is a release note; recording rules matching
exact label sets would need updating.

**The descriptor's blast radius.** `_publish_info` touches `Node.start()`, whose ordering is
load-bearing, and adds a counter to three public wiring methods. Mitigated by placing the
publish last (after the trace service, so nothing it observes is half-built) and by wrapping it
in `try/except` — a failed descriptor must never fail a start.

**Scope creep from `NodeInfo`.** Building the full descriptor rather than a narrow measure
catalog means `publishes`/`subscribes`/`serves` ship with no consumer in this change. That is
the deliberate cost of not revising the wire twice; `zenode topics --live` (live-topics §4, ~90
lines in `cli.py`) is now one command away and is explicitly **not** in this plan.

**A blocking measurement stalls the loop.** Same rule as any timer body, and the same answer:
`Node.blocking`. Documented in `docs/nodes.md`; not enforceable at runtime.