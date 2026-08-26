# OpenSOVD adapter

**Status: proposal. Not implemented, and no decision taken.** This page records
what SOVD is, what an adapter would look like, and what it would cost, so the
question can be answered on evidence. Drafted 2026-08-26.

The short version: it is possible, and it makes sense as an **out-of-process
sidecar that serves what nodes declare** — services for reads and actions, the
heartbeat and log topics for everything that has to keep working when a node
does not. It does not make sense inside the runtime, and it should not start
with faults.

---

## 1. What SOVD is

Service-Oriented Vehicle Diagnostics, standardized as **ISO 17978** — Part 1
(general principles, 2026-05), Part 2 (use cases, 2026-04), Part 3 (the API,
2026-03), previously ASAM SOVD 1.0.

It is HTTP/REST, JSON and OAuth 2.0 over a tree of *entities* — components,
apps, areas, functions — each exposing the resource collections it supports:
`data`, `faults`, `operations`, `modes`, `configurations`, `logs`, `updates`,
`bulk-data`, `locks`. A client discovers what an entity can do by reading the
entity itself; a collection absent from the response is a capability the entity
does not have. That self-description is what makes a partial implementation
legitimate rather than broken.

Two distinctions from the standard drive everything below:

- **`data` is a side-effect-free read** (GET, and PUT where writable).
  **`operations` may act** — they are POSTed as executions, with status and
  cancellation.
- **Components and apps are located somewhere; functions are not.** A function
  entity is a vehicle capability whose implementation is deliberately not tied
  to one box.

## 2. What Eclipse OpenSOVD ships today

Eclipse SDV project, Apache-2.0, around fifteen repositories, almost all Rust.

| Repository | What it is |
|---|---|
| `opensovd-core` | server, gateway, client, models, providers |
| `fault-lib` | applications report faults over IPC to a Diagnostic Fault Manager, which debounces them into ISO 14229 DTCs |
| `classic-diagnostic-adapter`, `uds2sovd-proxy` | UDS bridges in both directions |
| `odx-converter`, `mdd-ui` | ODX to MDD data descriptions, and a viewer |
| `opensovd-mcp` | an MCP server over SOVD |

`opensovd-core` currently implements **discovery, data and version-info** —
`GET /`, `/components`, `/apps`, `/areas`, `/components/{id}/data-categories`,
`/data-groups`, `/data`, `/data/{id}` (GET and PUT), and an unversioned
`/version-info`. Routes sit under a configurable base URI and an API version
segment, e.g. `http://host:7690/sovd/v1/components`. Faults, operations, modes
and functions are not in core yet.

## 3. Three findings that constrain the design

**The extension point is a Rust trait.** `opensovd_core::DataProvider`
(`list` / `read` / `write`, with default `categories` / `groups` / `tags`) is
how data reaches their server. Python cannot implement it.

**The gateway does not federate.** Its options are `--url`, `--unix-socket`,
`--mock`, `--serve-dir` and CORS settings; it builds its topology in-process
and has no provider that proxies a remote SOVD server. So an adapter cannot sit
*behind* their gateway — it has to **be** a SOVD server, which any SOVD client
can then talk to, including their own client crate and their MCP server.

**The specification is paywalled; the wire format is not.** `opensovd-models`
is Apache-2.0 and mirrors the JSON shapes, and
`tests/opensovd-gateway/test_api.py` in `opensovd-core` is a **Python** generic
traversal that walks every endpoint and validates responses against the schemas
they return. That is a usable conformance harness without buying ISO 17978-3.

There is, as of this writing, no SOVD server implementation on PyPI.

## 4. Why it fits zenode

**The contract replaces ODX.** Describing data is the expensive part of SOVD in
its native world — hence `odx-converter`, MDD files, a whole viewer repository.
zenode services and topics are pydantic models and `model_json_schema()` is
exactly what SOVD's `?include-schema=true` and its `schema` response field
want. The data catalogue already exists and is already machine-readable.

**The request/reply primitive already exists.** A `Service` is a queryable:
request in, reply out, both typed. That is the same shape as an HTTP resource,
so the adapter translates rather than simulates.

**The sidecar precedent is in the repository.** `zenode.exporter` runs a stdlib
`ThreadingHTTPServer` off a zenoh subscription with no dependencies, and its
module docstring's argument — nodes stay dependency-free, where the data goes
is a deployment decision — applies here word for word.

**It is not a duplicate of `zenode export`.** That serves dashboards:
aggregate, over time, pushed or scraped. SOVD serves a technician or a test
bench asking one robot one question now, with a standard client. Different
consumer, different interaction model.

## 5. The principle: declared, not scraped

An adapter could help itself to whatever is on the bus — subscribe to every
topic, keep the last sample, serve it as a data resource. That is rejected.

What a robot exposes to a diagnostic client is a decision with safety and
privacy consequences, and it belongs to the person who wrote the node, in the
contract, where it can be reviewed. Scraping means a 4K camera frame becomes a
"data resource" by accident, a value nobody meant to publish externally leaves
the robot, and the exposed surface changes whenever someone adds a topic.

So: **the adapter serves what the contract declares diagnosable, and nothing
else.** Concretely, three sources, in descending order of how much the node has
to be working for them to answer:

1. **Presence, health and logs** — already published by every node, passively.
   These keep working when the node does not, which is exactly when they
   matter.
2. **Read-only services** — computed on request by the node.
3. **Operations** — the remaining services, which may act.

Topics are not a source. A node that wants its pose diagnosable subscribes with
`mode="latest"`, keeps the value in a field, and declares a read-only service
that returns it — three lines, and a deliberate act.

## 6. The one contract change it needs

SOVD has to know whether a call is a safe read or an action, and `Service` does
not currently say:

```python
class RobotServices(TopicSet):
    get_pose = Service("state/get_pose", request=Empty, reply=Pose, read_only=True)
    home_axis = Service("motion/home", request=AxisId, reply=Ack)   # an operation
```

`read_only=True` maps to a SOVD **data** resource, reachable by GET. Anything
else maps to an **operation**, reachable only by POST. Without the flag the
adapter either guesses or makes everything an operation, and a GET that can
fire a motor is not something to ship.

It is one boolean on a frozen dataclass that already validates its own
arguments, at the layer where `latched` and `max_age` live. `Service` also
already carries a `description` field with no consumer today; this gives it
one, as the SOVD resource description.

## 7. The mapping

| SOVD | zenode source | Mechanism |
|---|---|---|
| root `/` capabilities | the namespace — one SOVD server per robot | — |
| `/components` | live nodes | liveliness |
| `/components/{node}/data` `sysInfo` | `cpu_percent`, `rss_bytes`, `uptime_s`, `state`, `queue_max_depth` | health subscription |
| `/components/{node}/data` `currentData` | counters, latencies, and the node's [declared measurements](node-measures.md) | health subscription |
| `/components/{node}/data` `identData` | node name, namespace, zenode version, `host` | health subscription |
| `/components/{node}/logs` | `<ns>/node/<name>/log` | log subscription |
| `/functions/{fn}/data/{id}` | services declared `read_only=True` | service call |
| `/functions/{fn}/operations/{id}` | all other services | service call |
| `/areas` | namespaces, where more than one is deployed | — |
| `/version-info` | zenode version, SOVD version | — |
| `/faults` | nothing exists yet | see §10 |
| `modes`, `updates`, `bulk-data`, `locks` | unmapped | omit the link; capability discovery is designed for exactly this |

### Why services are functions, not component resources

A zenode service key is `nav/get_map`. Which process answers it is deliberately
not part of the contract — that is the point of a key-addressed queryable, and
it is why nothing on the bus links a service to a node today. Filing services
under `/components/{node}` would invent that relationship.

SOVD already has the entity type for a capability that is not tied to a box:
**functions**. The service key splits along the grouping the conventions
already prescribe — `nav/get_map` becomes function `nav`, operation `get_map`.

Two caveats, both real. `opensovd-core` does not implement `/functions` yet, so
a client written against it will not traverse them. And deriving the function
id from a key prefix is inference, mild but real. The fallbacks, in order, are:
expose services on the root entity (the model permits it; their server does not
emit it), or synthesize a single `/components/robot` that carries them. This
is an open question that a test against a real SOVD client should settle.

### Runtime and application data separate cleanly

SOVD's grouping mechanism — `/data-groups` and `?groups=`, both already in
`opensovd-core` — carries the distinction without straining anything:

```
GET /v1/components/nav/data?groups=runtime      # the framework's counters
GET /v1/components/nav/data?groups=app          # the node's own measurements
GET /v1/functions/state/data/get_pose?include-schema=true
```

SOVD also reserves an `x-<ext>-` prefix for custom data categories, so a
genuinely zenode-specific category is available if `currentData` proves a poor
fit.

## 8. The entity model is flat, for now

SOVD separates **Component** (the physical box) from **App** (software running
on one), linked by `/apps/{id}/is-located-on` and `/components/{id}/hosts`. A
zenode node is a process, so *app* is the semantically correct entity.

Building that two-level tree needs to know which machine each node runs on, and
nothing published today says so. Until `NodeHealth` carries a
[`host`](node-measures.md#1-host), the adapter should expose **nodes as
components**: flat, and inventing nothing. Revisit once the field exists.

## 9. Architecture

Mirror `zenode.exporter`. That keeps every project constraint intact.

```
src/zenode/sovd/
    __init__.py
    server.py      # ThreadingHTTPServer and the router; stdlib only
    model.py       # Response, Items, EntityReference, EntityCapabilities, Metadata, ReadResponse
    topology.py    # live nodes + latest health + the contract -> entities and resources
    resources.py   # the table: health field -> (sovd id, category, group)
```

```bash
zenode sovd --listen 0.0.0.0:7690 --base-uri /sovd --contract myrobot.contract
```

```
GET http://robot:7690/sovd/version-info
GET http://robot:7690/sovd/v1/components
GET http://robot:7690/sovd/v1/components/nav/data?groups=runtime
GET http://robot:7690/sovd/v1/functions/state/data/get_pose?include-schema=true
```

```json
{
  "id": "get_pose",
  "data": {"x": 1.2, "y": -0.4, "theta": 0.78, "frame_id": "odom"},
  "schema": {"type": "object", "properties": {"x": {"type": "number"}}}
}
```

- **`--contract` is how the sidecar learns the services.** `cli.py` already
  does this for `zenode topics` and `zenode echo`: import the module, read
  `registered_services()`. A contract module imports only pydantic and zenode,
  so it is safe to load in a monitoring process — unlike a node class, which
  pulls in drivers. The cost is that the sidecar's deployment is coupled to the
  contract package; §11 covers the way out.
- **No new runtime dependency.** `http.server`, `json`, `urllib.parse` and
  `ssl` from the standard library; pydantic, already required, for schemas.
  Nothing compiled, which is the whole point on an ARM target.
- **Synchronous.** Handler threads call the service and block; a
  `ThreadingHTTPServer` makes that fine, and a `ServiceTimeout` becomes a 504.
  The thread-boundary invariant in `pubsub.py` and `service.py` is untouched
  because a sidecar runs no user handlers.
- **No cache, no staleness policy.** A read either reaches the node or fails —
  there is no third state where the adapter serves something old without
  saying so. The only cached values are the heartbeat and log records, which
  carry their own timestamps and are explicitly a record of the past.
- **One table.** `exporter.Metric` already carries a Prometheus name and an
  OTLP name in one row precisely because two tables would drift. The SOVD id
  and category belong as further columns on that row, not in a parallel table.
- **Conformance.** Vendor OpenSOVD's `test_api.py` traversal and point it at a
  server backed by `harness()`, marked `integration`.

## 10. Phases

**Phase 1 — no runtime change beyond `read_only`.** Discovery from liveliness,
`sysInfo`/`currentData`/`identData` from the heartbeat, `logs` from the log
topic, data and operations from services listed by `--contract`. Around 500–700
lines with tests, the size of `exporter.py` plus `otlp_metrics.py`. Nothing in
`node.py` or `pubsub.py` moves, so if the idea is wrong, a directory is
deleted.

**Phase 2 — drop `--contract`.** A per-node descriptor on the bus, latched,
listing what that node serves with schemas attached, would let the sidecar
discover everything without importing anything. It also subsumes the measure
catalog in [the measurements note](node-measures.md). Worth doing only once the
deployment coupling actually hurts.

**Phase 3 — faults.** SOVD's centre of gravity is `/faults`, and zenode has no
fault concept at all. Three options, in increasing commitment:

1. **Synthesize in the adapter** from ERROR-level log records and counter
   transitions (`deadline_misses`, `handler_errors`, `shm_fallbacks`,
   `logs_dropped`). Cheap, no contract change — but the codes are invented by
   the adapter and mean nothing to a tester.
2. **First-class faults**: a `NodeFault` message, a `self.fault(...)` API and a
   reserved key. Honest severities, stable identifiers, environment data. It is
   a new reserved key and a new message type, so it goes through the
   conventions checklist — a real commitment for a 0.1.x project.
3. **Omit faults.** Do not advertise the collection. Legal under capability
   discovery, and the adapter is still a data, logs and operations server.

Recommendation: start at (1) or (3), and do (2) only when something real
consumes it. What should *not* happen is importing DTCs, operation cycles and
debounce policy — that lineage is ISO 14229 and does not describe a robot.

## 11. Risks

**Operations are remote actuation.** A POST that reaches `motion/home` is a
robot moving because an HTTP request arrived. `read_only` keeps them off the
GET path, which is necessary but not sufficient: operations still need an
explicit per-service allowlist, off by default, and it should be visible
wherever the sidecar is documented.

**Authentication.** SOVD assumes OAuth 2.0 and OpenID Connect. Verifying a JWT
needs `cryptography`, a compiled dependency the project's constraints forbid.
Terminate authentication at a reverse proxy, or use mTLS through the standard
library's `ssl`. The operations path must never ship unauthenticated.

**A wedged node answers nothing.** Every service-backed resource depends on the
node's event loop. That is a correct trade — a timeout is more honest than a
stale cached value — but it means the passive path carries the diagnostic load
in exactly the failure it is most needed for. That is the argument for
[declared measurements](node-measures.md) riding on the heartbeat.

**Conformance claims.** Implementing against `opensovd-models` rather than the
standard means the defensible phrasing is "a SOVD-compatible subset, validated
against the OpenSOVD conformance traversal" — never "SOVD compliant".

**Maturity on both sides.** OpenSOVD's core has discovery and data only, and no
functions. Being early enough for the mapping to interest them is the upside;
tracking a moving target is the cost.

**Maintenance.** A second standard to follow, in a project with one maintainer.
That argues for a self-contained `sovd/` subpackage that can be split into a
separate distribution later without disturbing anything else.

## 12. Scope-fence check

The README's fence: no launch system, no parameter server, no TF tree, no IDL
or codegen, no standard topic keys.

An out-of-process adapter that serves declared services crosses none of them.
Deriving JSON Schema from pydantic is derivation, not code generation. The one
place the fence bites is `/configurations`: SOVD allows writing them, and a
writable configuration resource *is* a parameter server. Do not map
`NodeConfig` at all — a node that wants a setting adjustable can declare a
service for it, which puts the decision in the contract where it belongs.

## 13. Rejected alternatives

**Serving topic last-values as data resources.** The first shape this design
took. It needs a last-value cache in the sidecar, a staleness policy, a
subscription per exposed topic, and either `latched=True` everywhere or a new
`Node.latest()` one-shot history query. All of that exists to paper over the
fact that a topic is a push and a REST read is a pull. Services are a pull
already. It also scrapes rather than declares — see §5.

**Sitting behind the OpenSOVD gateway as a provider.** Not possible: the
extension point is a Rust trait and the gateway does not federate (§3).

**Putting SOVD concepts in the runtime.** Faults, DTCs, operation cycles and
modes in `node.py` would trade a transport-shaped framework for a
diagnostics-shaped one. The adapter is out-of-process precisely so this stays
reversible.

## 14. References

- [Eclipse OpenSOVD](https://projects.eclipse.org/projects/automotive.opensovd)
  — project page
- [`eclipse-opensovd/opensovd`](https://github.com/eclipse-opensovd/opensovd)
  — umbrella repository and the high-level design under `docs/design/`
- [`eclipse-opensovd/opensovd-core`](https://github.com/eclipse-opensovd/opensovd-core)
  — server, gateway, models, providers; the source of the endpoint and JSON
  shapes above
- [ISO 17978-1](https://www.iso.org/standard/85133.html),
  [17978-2](https://www.iso.org/standard/86586.html),
  [17978-3](https://www.iso.org/standard/86587.html)
- [ASAM SOVD](https://www.asam.net/standards/detail/sovd/) — the predecessor
  standard and its public overview material
