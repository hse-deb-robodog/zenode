# Node-defined measurements

**Status: implemented.** Drafted 2026-08-26, landed the same month. The page is
kept for the trade-offs and the rejected alternatives, which the code no longer
states; [Nodes](../nodes.md#measurements) and
[Observability](../open-telemetry.md#application-metrics) document what shipped.

Four things differ from the draft below, each decided when it was built:

- **The decorator is `@metric`**, not `@measure` — one decorator with
  `kind="gauge"|"counter"`, aligned with Prometheus and OTel vocabulary. §4
  left the name open.
- **The catalog is a field on a broader descriptor**, not a
  `node/<name>/measures` topic of its own. §4's last open question and
  [live-topics](live-topics.md) §6 both argued for it, so the wire is not
  revised twice: `zenode/msgs/info.py` carries `NodeInfo`, and `measures` is one
  of its fields.
- **`MeasureDescriptor` has no `name`.** The `id` *is* the name; two identifiers
  for one thing is the drift a single table exists to prevent.
- **`host` is overridden from `[transport] host`**, beside `namespace` — the
  other deployment label a node already reads off `TransportConfig`.

`NodeHealth` is a closed set. Every field on it is computed by the runtime in
`Node._publish_health`, and an application has no way to put a number of its
own on the heartbeat — pack state of charge, frames processed, distance to the
nearest obstacle. Those are the numbers a person asks for first when a robot
misbehaves.

Separately, nothing on the bus says which *machine* a node runs on.

Both are additive fields on the same message, so they are proposed together:
one contract change is better than two.

---

## 1. `host`

### What is missing

Nothing published today identifies the machine behind a node:

- the presence key is `<ns>/node/<name>` — a name, not a location
- `NodeHealth.node` is the same name
- the zenoh ZID identifies a *session*, so it distinguishes processes, not
  hosts

A tool watching the bus therefore cannot tell whether `nav` and `perception`
share a box. On a single-computer robot that costs nothing; on a robot with a
Jetson and two microcontroller bridges, it means the observability output never
answers "where is the CPU burning".

### The field

```python
class NodeHealth(BaseModel):
    node: str
    host: str = ""
    """The machine this node runs on. A deployment label, not an identity:
    nothing is addressed by it. Empty where the deployment did not set one."""
```

Default from `socket.gethostname()`, overridable from configuration. The
override is the point: in a container the hostname is a random hex id, and on a
robot the useful name is `jetson-perception`. That is the same "the deployment
supplies it" stance `namespace` already takes.

### What it must not become

- **Not part of a key.** `<ns>/node/**` is reserved and its builders
  (`presence_key`, `health_key`, `log_key`, `trace_key`) are what keep the
  publisher and the CLI from drifting. Putting the host in a key would break
  those and the duplicate-name check with them.
- **Not an identity.** Node names are expected to be unique within a namespace
  and `allow_duplicates=False` enforces it. `host` is informational; no lookup
  may depend on it.

### Compatibility

`NodeHealth` is a plain `BaseModel`, so unknown fields are ignored, and the
field has a default. An old publisher reaching a new sidecar and a new
publisher reaching an old one both work. No coordinated rollout.

### Why it earns its place without anything else changing

- a `host` column in `zenode nodes` and `zenode health`
- a `host` label on the exported Prometheus series
- `host.name` as an OTLP resource attribute, which is the OpenTelemetry
  semantic convention sitting beside the `service.name` / `service.namespace`
  already emitted by `zenode.otlp_metrics`

`pid` is the obvious next candidate, for OpenTelemetry's `service.instance.id`.
It is deliberately not proposed until something asks for it.

---

## 2. Measurements

### Why not just use a service

A read-only `Service` can already return any value a node knows, on demand and
freshly computed. That is the right mechanism for most of what a diagnostic
reader wants, and [the SOVD adapter note](opensovd-adapter.md) builds on it.

It is not the right mechanism for everything, because **a service call reaches
the node's event loop**. When that loop is blocked — a driver call that did not
return, a handler stuck on a lock, precisely the situation being diagnosed —
the call times out and reports nothing at all.

The heartbeat is the path that still works there. It is published by a timer
that has already run, it carries whatever the last successful sample produced,
and its absence is itself the signal. So the division of labour is:

| | Heartbeat measurement | Read-only service |
|---|---|---|
| Reaches | the bus, unprompted | the node's event loop, on request |
| Survives a wedged node | the last value does | no |
| Freshness | up to `health_interval` old | computed now |
| Shape | one scalar | any model |
| Declared by | `@measure` | `Service(..., read_only=True)` |

A value that answers "is this node healthy" belongs on the heartbeat. A value
that answers "what is the robot doing" belongs behind a service. Battery
state-of-charge is the former and it is worth saying why: when a node stops
answering, the last thing it said about its own power is evidence.

### The constraint decides the shape

The obvious API — a `dict[str, float]` handlers write into freely — is
unbounded, and it breaks a guarantee `zenode.exporter` makes in its module
docstring: *cardinality is bounded by construction, one series set per live
node.* A node that keys by detected-object id would defeat that in an
afternoon, and the damage lands in someone else's time-series database.

So the answer is not a dictionary anything can be written into. It is
**declared** measurements, bounded exactly the way `Topic` and `Service`
declarations bound the contract.

### Declaring one

Following the existing idiom — `zenode.declarative` decorators stamp metadata
and return the function unchanged, and `collect_*` walks the MRO base-first:

```python
class Nav(Node):
    name = "nav"

    @measure("battery_soc", unit="1", description="Pack state of charge, 0–1.")
    def _soc(self) -> float | None:
        return self._driver.soc          # None is unknown, and unknown is not zero

    @measure("frames_processed", unit="{frame}", kind="counter", integral=True)
    def _frames(self) -> int:
        return self._count
```

- The decorator only stamps `__zenode_measures__`, so the method stays directly
  callable in a test, like every other binding.
- Collection is base-first with override by id, matching `collect_bindings`, so
  a subclass may replace a parent's measurement by redeclaring its id.
- The set is fixed once the class is defined. **That declaration is the bound**
  — no runtime cap, no drop-after-N, no cardinality surprise downstream.
- Names are validated at declaration time against `[a-z][a-z0-9_]*` and against
  the built-in field names, and a bad one raises at import. Typos fail loudly,
  the stance `NodeConfig(extra="forbid")` already takes.

### Evaluation

Callables run from the existing health timer, at the point where `cpu_percent`
is computed.

- Returning `None` means unknown; the value is omitted for that heartbeat
  rather than sent as zero. `cpu_percent` already makes that promise and
  measurements inherit it.
- Raising is counted in `handler_errors` and omits that one value. **The
  heartbeat itself never fails** — a broken measurement must not take the
  liveness signal with it.
- A callable must not block. Same rule as any timer body; `Node.blocking` is
  the answer for anything that does. A measurement that needs to talk to
  hardware should read a value the node already cached, not go and fetch one.

### The wire: values hot, catalog cold

```python
class NodeHealth(BaseModel):
    ...
    measures: dict[str, float] = {}
```

Unit, kind and description never change, so shipping them on every heartbeat
(`health_interval`, default 2.0 s) is waste. They go once, on a **latched**
topic in the reserved space, with builders beside the existing ones:

```python
# zenode/msgs/measure.py
def measures_key(name: str) -> str: ...      # node/<name>/measures
def measures_pattern(namespace: str) -> str: ...


class MeasureDescriptor(BaseModel):
    id: str
    name: str
    unit: str = ""
    kind: Literal["gauge", "counter"] = "gauge"
    integral: bool = False
    description: str = ""


class MeasureCatalog(BaseModel):
    node: str
    items: list[MeasureDescriptor]
```

`latched=True` is the right tool: a sidecar started an hour later still
receives the catalog through the zenoh-ext history query. That is also what
keeps the sidecar out of the application's import graph, which matters — node
classes import hardware drivers, and a monitoring process must never load them.

`integral` mirrors `exporter.Metric.integral`: the value crosses the wire as a
JSON number and the flag tells the encoder whether OTLP should carry it as
`asInt` or `asDouble`.

### What each consumer does with it

| Consumer | Result |
|---|---|
| `zenode health` | extra rows per node |
| `zenode export` (Prometheus) | `zenode_app_<id>{namespace,node}`, `HELP`/`TYPE` from the catalog |
| OTLP push | the same values, from the same catalog |
| A SOVD adapter | a data resource on that node's component — see [OpenSOVD adapter](opensovd-adapter.md) |

The `zenode_app_` prefix separates application-owned names from runtime-owned
ones, so a future built-in field can never collide with someone's measurement.

Counters need no new machinery: application counters share the node's start
instant, so `otlp_metrics._start_ns` already derives the correct
`startTimeUnixNano` and a restart still reads as a reset rather than a fall.

The exporter's "one table, because two would drift" invariant survives intact —
the catalog simply *becomes* the table for application measurements, read by
the pull and push paths alike. Its docstring guarantee changes from *bounded by
construction* to *bounded by declaration*, which is still bounded, and should
be reworded to say so.

### The fence

**Scalars that describe a node's health go on the heartbeat. Application state
does not.**

A `BatteryState` model, a pose, a costmap — those are not measurements. Expose
them behind a read-only service, where they are computed on request and can be
any shape. Without that line the heartbeat grows without bound and pub/sub has
been reinvented inside a 0.5 Hz message.

### Conventions

`docs/conventions.md` is normative for zenode-owned keys, and measurements
travel on one. So its rules apply to a declared measurement unchanged: SI on
the wire, `battery_soc` as 0.0–1.0 and never percent, radians and never
`*_deg`. The `unit` field is where that becomes visible to a reader, and §1 of
the conventions gains a line saying the rule covers measurements too.

---

## 3. Rejected alternatives

**A free `dict[str, float]` on the node.** Unbounded names, unbounded
cardinality, and the failure lands in a database nobody on the robot owns. The
declaration is what makes the bound checkable.

**Read-only services for everything, no measurements at all.** Simpler by one
mechanism, and it is the right default for application values — but it makes
every diagnostic depend on a responsive event loop, which is the one thing a
misbehaving node does not have. See the table in §2.

**Units on every heartbeat.** Simpler by one topic, but it repeats static
strings at the heartbeat rate forever, and the project's stated position is
that nothing may grow — or repeat pointlessly — on a robot that runs for a
week.

**A queryable instead of a latched catalog.** Symmetrical with the built-in
trace service, and it costs no publisher. But it puts the catalog behind the
same event loop the measurements exist to survive, and the sidecar would have
to notice each node, issue a request, handle the timeout and retry. Latched
delivery has none of those failure modes.

**Host in the presence key.** Breaks the reserved-key builders and the
duplicate check, and makes an informational label load-bearing.

**Letting tooling import the node classes to read declarations.** This is how
`zenode echo --contract` learns types, and it does not transfer: a contract
module is importable anywhere, a node class pulls in drivers.

---

## 4. Open questions

- **The decorator's name.** `@measure`, `@metric`, `@gauge`; a paired
  `@counter` or the `kind=` argument shown above. Undecided.
- **String-valued measurements.** Excluded here on purpose: they are not
  metrics, and the honest home for them is an identification resource.
- **Push as well as pull.** Everything above is pull-at-heartbeat. A
  `self.frames.inc()` style counter object would suit values incremented inside
  handlers, at the cost of a second mechanism. Deferred until pull proves
  awkward.
- **Whether the catalog should generalize into a full node descriptor** —
  measurements *and* the services a node serves, with their schemas. That would
  let a SOVD adapter drop its `--contract` argument entirely. It is deliberately
  not proposed here: the narrow catalog is what measurements need, and the
  general version should be designed when something actually needs it.

## 5. Cost

Roughly 120 lines in the runtime (decorator, MRO collection, the call site in
`_publish_health`, the catalog publish) and 150 across the exporter, the OTLP
encoders and the CLI. One contract change, through the checklist at the end of
`docs/conventions.md`.

Worth noting for sequencing: this is justified with no reference to SOVD at
all. Prometheus and OTLP consumers gain application measurements the day it
lands, which means it can be built and shipped before any decision is taken on
[the adapter](opensovd-adapter.md).
