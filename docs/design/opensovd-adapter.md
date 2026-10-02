# OpenSOVD adapter

**Status: decided, not implemented.** Drafted 2026-08-26 as a proposal, revised
2026-09-21 after the [storage research note](zenoh-storage-for-sovd.md), and
decided 2026-10-02 after a design review that also absorbed the
[reuse research note](opensovd-core-reuse.md). This page is the design; the two
research notes keep the evidence. Every reference to upstream below is pinned to
`eclipse-opensovd/opensovd-core` at commit
`26953d97da4335179083aff41ed52a82886499e7` (2026-09-21, `main`). "The route
set", "the JSON shapes" and "the SOVD version" all mean that commit.

The short version: zenode gets a **pure-Python, out-of-process sidecar** that
serves what nodes declare diagnosable — their heartbeat, their declared
measurements, and the services the contract marks as reads — over the exact
HTTP surface `opensovd-core` serves today. It discovers everything from the
bus, imports no contract, takes no new dependency, is read-only, and lives in a
subpackage that can become its own distribution. It needs five small changes
to the runtime, all at the contract and message layer. Operations, standard
logs and faults are deliberately later or never.

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

At the pinned commit `opensovd-core` implements **discovery, data, bulk-data
and version-info**: `GET /`, `/components`, `/apps`, `/areas`,
`/components/{id}`, `/components/{id}/data-categories`, `/data-groups`, `/data`,
`/data/{id}` (GET and PUT), and an unversioned `/version-info` reporting SOVD
version `1.1`. Routes sit under a configurable base URI and an API version
segment, e.g. `http://host:7690/sovd/v1/components`. Faults, operations, modes,
logs and functions are not in core.

## 3. Four findings that constrain the design

**The extension point is a Rust trait, and the library is reusable.**
`opensovd_core::DataProvider` (`list` / `read` / `write`, with default
`categories` / `groups` / `tags`) is how data reaches their server.
`Server::builder()` is public, so an out-of-tree Rust crate can implement the
trait, mutate the topology at runtime, and inherit JWT, Rego authorization and
TLS. The [reuse note](opensovd-core-reuse.md) verified this with a 165-line
crate against a live zenode node. Python cannot implement the trait, and there
is no IPC or network provider to put a Python process behind.

**The server cannot be extended beyond what it ships.** The builder exposes no
router. The only mount point is `service(path, svc)`, a nested tower service
inside authentication, and `EntityCapabilities` is built with
`..Default::default()` in each entity handler, driven only by whether a data
provider is registered. A collection the server does not implement cannot be
*advertised*, so a link-following client never sees it, even if a route exists.

**The gateway does not federate.** Its options are `--url`, `--unix-socket`,
`--mock`, `--serve-dir` and CORS settings; it builds its topology in-process
and has no provider that proxies a remote SOVD server. So an adapter cannot sit
*behind* their gateway — it has to **be** a SOVD server.

**The specification is paywalled; the wire format is not.** `opensovd-models`
is Apache-2.0, mirrors the JSON shapes, and derives `schemars::JsonSchema` for
each, so the shapes can be exported as JSON Schema from a checkout.
`tests/opensovd-gateway/test_api.py` is a generic Python traversal that walks
every endpoint and validates responses against the schemas they return.
Neither covers `logs`, `operations` or `faults`, because the models do not
define them: the `logs` link exists in the capabilities struct, the log record
does not.

There is, as of this writing, no SOVD server implementation on PyPI.

## 4. Why it fits zenode

**The contract replaces ODX.** Describing data is the expensive part of SOVD in
its native world — hence `odx-converter`, MDD files, a whole viewer repository.
zenode services and topics are pydantic models and `model_json_schema()` is
exactly what SOVD's `?include-schema=true` and its `schema` response field
want. The data catalogue already exists and is already machine-readable.

**The bus already describes every node.** Each node publishes a latched
[`NodeInfo`](node-measures.md) descriptor — what it publishes, subscribes to and
serves, and its declared measurements — plus a heartbeat and a liveliness token.
A consumer that speaks zenoh and JSON can build the whole SOVD topology from
that, in any language, without importing anything. This is what makes the
sidecar deployable without the contract package, and what kept the Rust route
in §6 feasible.

**The request/reply primitive already exists.** A `Service` is a queryable:
request in, reply out, both typed. That is the same shape as an HTTP resource,
so the adapter translates rather than simulates. `call_service` takes a plain
zenoh session, so the sidecar needs no node and no private back door.

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
else**, and the declaration is the only switch. There is no allowlist in the
sidecar's configuration, because two places that decide exposure is one too
many to review. Three sources, in descending order of how much the node has to
be working for them to answer:

1. **Presence, health, the measure catalog and logs** — already published by
   every node, passively. These keep working when the node does not, which is
   exactly when they matter.
2. **Read services** — computed on request by the node, declared
   `diagnostic="read"` in the contract.
3. **Operations** — services declared `diagnostic="operation"`. Accepted by the
   contract today, served by nothing until §12 Phase 2.

Topics are not a source. A node that wants its pose diagnosable subscribes with
`mode="latest"`, keeps the value in a field, and declares a read service that
returns it — three lines, and a deliberate act.

There is one topic read that would not break the principle, and it is recorded
here so it is not rediscovered. A `latched=True` topic is published through a
zenoh-ext `AdvancedPublisher` whose cache is an ordinary queryable, so a
one-shot query returns its last sample — `Envelope` included — with no
subscription and no state in the sidecar. If topic reads are ever wanted, that
is the shape: **latched topics the contract explicitly marks diagnosable**, as
a fourth source between the heartbeat and services. It needs its own flag on
`Topic`; `latched` means "late joiners get the last value", and giving it a
second meaning would make every state topic externally visible by accident.
§15 has the reasoning, the [research note](zenoh-storage-for-sovd.md) the
evidence.

## 6. Why a Python server and not a Rust bridge

Two routes were live until the review. **Route A** writes a SOVD server in
Python. **Route B** writes a Rust `DataProvider` on `opensovd-core` and reuses
their HTTP, auth and TLS. Both work; the reuse note proved B end to end.

| | Route A — Python sidecar | Route B — Rust bridge |
|---|---|---|
| Code zenode owns | ~500–700 lines: router, JSON shapes, mapping | ~300–600 lines: provider, zenoh client, mapping |
| What it tracks | the JSON shapes, from `opensovd-models` | `opensovd-core` at a git commit — unreleased, no crates.io, one maintainer |
| Free | nothing | JWT, Rego, TLS, discovery routes, conformance |
| Can add a collection upstream lacks | yes | no — not advertised (§3) |
| Runs in `harness()` | yes | no, needs a built binary |
| ARM target | pure Python | native-runner builds upstream, cross-compile unverified |
| Second toolchain in the repo | no | yes, or a second repo |

Route A was chosen, with the explicit framing that **A makes zenode maintain a
SOVD subset, B makes zenode maintain a dependency on a pre-release project**.
For a one-maintainer Python project the first is the smaller liability, and it
is the only route that can ever serve logs or operations without an upstream
hook that does not exist. Route B is **not rejected**: it is the right move if
upstream ships a remote or IPC provider, publishes crates, or gains the
collections zenode wants, and the reuse note records the trigger conditions.
If it is ever taken it lives in its own repository, so zenode stays pure-Python
`uv`.

## 7. The runtime changes

The proposal promised one. The design needs five, all at the contract and
message layer, none in `node.py`'s start-up order or in `pubsub.py`. Each is
useful to zenode without SOVD, which is the test applied to every one.

**`Service.diagnostic`.** SOVD has to know whether a call is a safe read or an
action, and whether it may be exposed at all. One field carries both:

```python
class RobotServices(TopicSet):
    get_pose  = Service("state/get_pose", request=Empty, reply=Pose, diagnostic="read")
    home_axis = Service("motion/home",    request=AxisId, reply=Ack, diagnostic="operation")
    reset_odom = Service("state/reset",   request=Empty, reply=Ack)   # not exposed
```

`diagnostic: Literal["read", "operation"] | None = None`. `None`, the default,
means the sidecar never lists it. `"read"` maps to a SOVD **data** resource,
reachable by GET; the dataclass rejects it unless the request model declares
no fields, because a GET has no body and a read with arguments is a second
serialization path nobody asked for. `"operation"` is validated, published and
unserved until Phase 2. A single tri-state rather than `read_only: bool`
because a boolean conflates "safe to GET" with "externally visible", and the
whole point of §5 is that visibility is the deliberate act. `zenode.msgs` gains
an `Empty` model with no fields for the common case.

**`ServiceInfo` carries what a client needs.** Today it is `key`, `request`
and `reply` as class names. It gains `diagnostic`, `description` (declared on
`Service` since the beginning, consumed by nothing) and `request_schema` /
`reply_schema` as `model_json_schema()` output. With those on the latched
descriptor the sidecar learns every service from the bus, and `zenode topics
--live` gets richer for free.

**`NodeInfo.runtime` describes the heartbeat's own fields.** The exporter owns a
table mapping each `NodeHealth` counter to a name, unit, kind and help text,
and the sidecar would need the same table. Instead the runtime publishes it:
`runtime: list[MeasureDescriptor]`, ids equal to the `NodeHealth` field names,
built from one table that moves to `msgs/health.py` and that `exporter.py`
imports for its names and help text. The exporter keeps its `_total` suffix as
a rendering rule. A consumer in any language then reads a value by field name
and formats it by descriptor, exactly as it already does for `measures`. This
also surfaces `deadline_misses`, which the exporter's table was missing.

**`NodeInfo.health_interval`.** A node whose loop is wedged holds its liveliness
token and stops beating. Without the interval a consumer cannot tell silence
from a slow node; with it, three missed beats is the same rule a subscription
`deadline` uses. `None` means the node runs without a heartbeat and is never
flagged.

**Node names and namespace segments are validated.** `_validate_key` rejects
empty segments and whitespace and nothing else, so `name = "arm/left"` passes
every check and `node_name_from_key` already mis-parses it, and `*`, `%`, `?`
and `#` are all legal in a key that becomes a URL path. Names and each
namespace segment must match `^[A-Za-z0-9_.-]+$`; namespaces may contain `/`,
names may not. Every existing example and test name is valid under it. It is a
breaking change and ships as the next minor's.

## 8. The mapping

| SOVD | zenode source | Mechanism |
|---|---|---|
| root `/` capabilities | the namespace — one sidecar per namespace | — |
| `/version-info` | `version: "1.1"`, the base URI, `vendor_info: {name: "zenode", version}` | — |
| `/components` | live and recently departed nodes (§10) | liveliness |
| `/components/{node}` | `NodeInfo` | latched descriptor |
| `/components/{node}/data`, category `sysInfo` | every `NodeHealth` field, described by `NodeInfo.runtime` | health subscription |
| `/components/{node}/data`, category `currentData`, group `app` | the node's declared measurements | health subscription |
| `/components/{node}/data`, category `currentData`, group `svc` | services declared `diagnostic="read"`, one resource each | service call |
| `/components/{node}/x-zenode-logs` | `<ns>/node/<name>/log` | log subscription |
| `/apps`, `/areas` | empty collections | — |
| `/faults`, `/operations`, `/logs`, `modes`, `updates`, `bulk-data`, `locks`, `configurations` | omitted; capability discovery is designed for exactly this | — |

**Nodes are components, flat.** SOVD separates *Component* (the box) from
*App* (software on one), and `NodeHealth.host` now says which box a node runs
on, so the two-level tree is buildable. It is not built: nothing publishes a
heartbeat for a host, so every host entity would be synthesized, and no
consumer has asked. Flat is a choice, not a wait.

**Services sit under the node that serves them.** `NodeInfo.serves` has linked
each service to its node since `93822c8`, which removed the proposal's reason
for filing services as location-independent `/functions`. Components are what
`opensovd-core` and every client traverse today, and a service id derived from
`serves` is a fact on the bus, not an inference from a key prefix. The cost is
that two nodes serving the same key show it twice, which is also the truth.

**Ids and groups.** A service's resource id is its key with `/` replaced by
`.`, so `state/get_pose` is `state.get_pose`. Measure ids match
`^[a-z][a-z0-9_]*$` and runtime ids are `NodeHealth` field names, neither of
which can contain `.`, so the three sources cannot collide. Categories are what
clients filter on: `sysInfo` for the framework's view of the process,
`currentData` for anything the application produced. Groups carry the
zenode-specific distinction and are what `?groups=app` selects.

```
GET /sovd/v1/components/nav/data?categories=sysInfo
GET /sovd/v1/components/nav/data?groups=app
GET /sovd/v1/components/nav/data/state.get_pose?include-schema=true
```

```json
{
  "id": "state.get_pose",
  "data": {"x": 1.2, "y": -0.4, "theta": 0.78, "frame_id": "odom"},
  "schema": {"type": "object", "properties": {"x": {"type": "number"}}}
}
```

**Logs are a custom link.** `opensovd-models` defines the `logs` link and not
the record, so any body zenode served under `/logs` would be invented, and the
conformance claim in §11 would be false for that one collection. SOVD reserves
the `x-<ext>-` prefix for exactly this; `x-zenode-logs` returns `{"items":
[...]}` of `LogRecordMsg` as JSON, newest last, with no filters or paging. Those
are what the standard's own `/logs` will prescribe when its shape is known, and
inventing them now makes the custom link harder to retire. A technician on the
box has `zenode logs` already.

## 9. Architecture

Mirror `zenode.exporter`. That keeps every project constraint intact.

```
src/zenode/sovd/
    __init__.py
    server.py      # ThreadingHTTPServer and the router; stdlib only
    model.py       # the JSON shapes: Response, Items, EntityReference, EntityCapabilities, Metadata, ReadResponse, Error
    topology.py    # liveliness + NodeInfo + latest health -> entities and resources
```

```bash
zenode sovd --listen 127.0.0.1:7690 --base-uri /sovd
```

- **Discovery is from the bus, and only the bus.** A liveliness subscriber on
  the presence pattern, a latched subscriber on `info_pattern`, a plain
  subscriber on `health_pattern` and one on `log_pattern`. No `--contract`:
  the latched descriptor carries schemas, descriptions and the `diagnostic`
  flag, so the sidecar deploys with nothing but zenode installed. Payloads are
  JSON on the wire and JSON over HTTP, so a service call is a byte pass-through
  with the schema from the descriptor. A service whose codec is not the JSON
  default is listed in the descriptor with its encoding and skipped with a
  warning.
- **No new runtime dependency.** `http.server`, `json`, `urllib.parse` from the
  standard library; pydantic, already required, for the shapes. Nothing
  compiled, which is the whole point on an ARM target.
- **The route set is upstream's, exactly.** `/`, `/version-info`,
  `/components`, `/components/{id}`, `/data-categories`, `/data-groups`,
  `/data`, `/data/{id}`, with `?include-schema`, `?groups` and `?categories`,
  at the pinned commit. No `PUT`. `/apps` and `/areas` answer with empty
  collections so a link-following client never dead-ends.
- **One loop thread, bounded handler threads.** A single asyncio loop thread
  owns the zenoh session and every subscription. `ThreadingHTTPServer` handler
  threads submit `call_service` with `run_coroutine_threadsafe` and wait with a
  timeout. In-flight service calls are capped by a semaphore, default 32,
  `--max-calls`; a request over the cap gets 503. A client retrying in a loop
  must not become a queryable storm on the robot. The thread-boundary invariant
  in `pubsub.py` and `service.py` is untouched because a sidecar runs no user
  handlers.
- **Failures map to status codes, with upstream's error body.** Node not in
  the topology → 404. `ServiceTimeout` → 504. `ServiceError` from the handler
  → 502. A reply that does not decode → 502. Over the call cap → 503. 502 is
  "the upstream answered with something wrong", which is exactly what a
  raising handler is.
- **No cache, no staleness policy for reads.** A read either reaches the node
  or fails — there is no third state where the adapter serves something old
  without saying so. The only retained values are the heartbeat, the descriptor
  and log records, which carry their own timestamps and are explicitly a record
  of the past.
- **Loopback by default.** `--listen 127.0.0.1:7690`; widen explicitly. The
  exporter binds all interfaces because a Prometheus scrape is one aggregator
  that exists to reach it; a SOVD client is anyone with a browser, and the
  sidecar serves node identity, measurements and service results. No TLS in
  Phase 1; a reverse proxy is the documented way off the box, and the
  operations phase brings mTLS.
- **Public API only.** The subpackage imports `zenode.msgs`, `call_service`,
  the latched-subscriber helper and the transport config. Nothing private, so
  the split into a separate distribution that §13 reserves is real.

## 10. Presence: live, silent, gone

Liveliness tokens decide who is a component. Heartbeat age decides whether a
live node is answering. Departure is retained, bounded, and visible.

- **Live**: token held, beats arriving. Served as-is.
- **Silent**: token held, no beat for three `health_interval`s. Still listed,
  with `x-zenode-presence: silent` and `x-zenode-last-seen` in `sysInfo`. A
  read service call will likely 504, which is the honest answer.
- **Gone**: token dropped. Still listed for `--retain-gone` seconds, default
  600, with `x-zenode-presence: gone`, the last heartbeat and the log buffer
  intact, then dropped. The departed set is capped, default 64, oldest first.

The proposal argued that a departed node should return nothing because a stale
answer is worse than none. That holds for *reads*, which still fail. It does
not hold for the last heartbeat and the last thousand log lines of a process
that crashed a minute ago, which are the one thing a technician came for. SOVD
has no "gone" state, so the custom fields say what the standard cannot.

Everything retained is bounded: one heartbeat and one descriptor per node, a
`deque(maxlen=1000)` of log records per node (`--log-buffer`), and the departed
cap. The exporter's per-node dicts, which grow with every name ever seen, are
not the precedent here.

## 11. Conformance

The JSON shapes are generated once from `opensovd-models` via `schemars` at the
pinned commit, committed under `tests/sovd/schemas/`, and a zenode-owned
traversal over a server backed by `harness()` validates every response against
them, marked `integration`. CI needs no Rust toolchain and the pin is a file in
the repository.

Upstream's own `test_api.py` is Apache-2.0 like zenode and is the one thing that
proves their client's assumptions hold, but it launches their gateway binary
and imports their fixtures. It is run by hand against the sidecar before a
release, from the cargo checkout, and is not vendored: a vendored test rots the
day upstream edits a route.

The defensible phrasing is "a SOVD-compatible subset, validated against the
OpenSOVD shapes at commit `26953d9`" — never "SOVD compliant".

## 12. Phases

**Phase 1.** The five runtime changes in §7. The sidecar with discovery, data
and `x-zenode-logs`, the presence model in §10, schema conformance in CI, and
`zenode sovd` on the CLI. Around 500–700 lines with tests, the size of
`exporter.py` plus `otlp_metrics.py`. The runtime changes stand on their own if
the sidecar is deleted.

**Phase 2 — operations.** `diagnostic="operation"` becomes a POSTed execution,
synchronous: the call runs, the response is the completed execution with its
result, no cancellation. This is remote actuation, so it arrives together with
mTLS through the standard library's `ssl` (`--tls-cert`, `--tls-key`,
`--client-ca`) and never ships without it. Gated on a consumer asking for it.

**Phase 3 — the standard's collections.** `/logs` once ISO 17978-3 or an
upstream model gives it a shape, at which point `x-zenode-logs` retires.
`/functions` only if a consumer needs location-independent addressing.

**Never, as far as this note can see.** Faults. Synthesizing them from log
levels and counter transitions is scraping by another name, and first-class
faults are a new reserved key and message type for a project that has no fault
concept. What should *not* happen is importing DTCs, operation cycles and
debounce policy — that lineage is ISO 14229 and does not describe a robot. If
something real ever consumes faults, that is a new note.

## 13. Risks

**Operations are remote actuation.** A POST that reaches `motion/home` is a
robot moving because an HTTP request arrived. The `diagnostic` tri-state keeps
them off the GET path and off the bus entirely in Phase 1; Phase 2 adds mTLS as
a precondition, not an option.

**Authentication.** SOVD assumes OAuth 2.0 and OpenID Connect. Verifying a JWT
needs `cryptography`, a compiled dependency the project's constraints forbid.
Terminate authentication at a reverse proxy, or use mTLS through `ssl`. The
read-only Phase 1 binds loopback by default for the same reason.

**A wedged node answers nothing.** Every service-backed resource depends on the
node's event loop. That is a correct trade — a timeout is more honest than a
stale cached value — but it means the passive path carries the diagnostic load
in exactly the failure it is most needed for. That is the argument for
[declared measurements](node-measures.md) riding on the heartbeat, and for §10
saying "silent" out loud.

**Maturity on both sides.** OpenSOVD's core has discovery and data only. Being
early enough for the mapping to interest them is the upside; tracking a moving
target is the cost, and the pin is what makes the cost visible.

**Maintenance.** A second standard to follow, in a project with one maintainer.
That argues for a self-contained `sovd/` subpackage on the public API only, so
it can be split into a separate distribution without disturbing anything else.

## 14. Scope-fence check

The README's fence: no launch system, no parameter server, no TF tree, no IDL
or codegen, no standard topic keys.

An out-of-process adapter that serves declared services crosses none of them.
Deriving JSON Schema from pydantic is derivation, not code generation. The one
place the fence bites is `/configurations`: SOVD allows writing them, and a
writable configuration resource *is* a parameter server. `NodeConfig` is never
mapped — a node that wants a setting adjustable can declare a service for it,
which puts the decision in the contract where it belongs.

## 15. Rejected alternatives

**A Rust bridge on `opensovd-core`.** Feasible and verified; not taken, for the
reasons in §6. Recorded as the alternative to return to, not as a dead end.

**`--contract` on the sidecar.** The proposal's Phase 1 imported the contract
module to learn services, and deferred bus discovery to a Phase 2. The latched
`NodeInfo` descriptor landed first, through `zenode topics --live`, so the
import was never needed: it coupled the sidecar's deployment to the contract
package for nothing. `cli.py` still imports contracts for typed `echo`, which
is a different job.

**Services as `/functions`.** The proposal filed services under functions
because "nothing on the bus links a service to a node". `NodeInfo.serves` has
since `93822c8`, and `opensovd-core` does not implement functions, so the
mapping would have been both an inference and invisible to the first consumer.

**A sidecar allowlist for operations.** Rejected with §5: exposure is one
decision in one place, the contract. A deployment that must not expose a
service removes the flag, which is reviewable, rather than configuring the
sidecar, which is not.

**A `read_only` boolean.** Would have exposed every read-only service with no
opt-in, conflating "safe to GET" with "externally visible".

**A zenode-shaped body under the standard `/logs` link.** Rejected with §8:
a collection whose shape zenode invented cannot be called validated against
anything, and the custom prefix exists for exactly this.

**Serving topic last-values from a cache in the sidecar.** The first shape this
design took: a subscription per exposed topic, a last-value cache, and a
staleness policy to go with it. All of that exists to paper over the fact that
a topic is a push and a REST read is a pull. Services are a pull already. And
nothing forces such a cache to be contract-driven, so it drifts toward scraping
— see §5. An earlier draft also charged this alternative with needing a new
one-shot history query; that was wrong, the zenoh-ext publisher cache is
already a queryable, which is why §5 admits latched topics behind an explicit
flag some day. Two caveats if that day comes: zenoh-ext's advanced pub/sub is
marked unstable, and the adapter must report the envelope's send time — and
honour `Topic.max_age` where set — or §9's "no third state" stops being true.

**Reading topics back from a zenoh router storage.** `zenohd`'s storage-manager
plugin keeps the latest sample per key and answers `session.get()`. Rejected on
four counts, the first of which is sufficient: a storage **drops the
attachment**, which is where zenode carries the `Envelope`, so a sample read
back has no sender, no sequence number, no send time, and `max_age` is silently
skipped. Only `zenohd` can host the plugin, so every robot would need a router
plus a version-locked `.so`, and none of it could run in `harness()`. A storage
has no expiry, so it answers identically for a live node, a wedged one and one
uninstalled last week. And what is exposed would be declared in a router config
the contract cannot see. Evidence and experiments are in the
[research note](zenoh-storage-for-sovd.md).

**Faults synthesized from logs and counters.** Cheap, and the codes would mean
nothing to a tester. See §12.

**Sitting behind the OpenSOVD gateway as a provider.** Not possible: the
extension point is a Rust trait and the gateway does not federate (§3).

**Putting SOVD concepts in the runtime.** Faults, DTCs, operation cycles and
modes in `node.py` would trade a transport-shaped framework for a
diagnostics-shaped one. The adapter is out-of-process precisely so this stays
reversible.

## 16. References

- [Reusing `opensovd-core`](opensovd-core-reuse.md) — the research note behind
  §3 and §6, with the verified Rust probes and the trigger conditions for
  route B
- [Zenoh storages as the read path](zenoh-storage-for-sovd.md) — the research
  note behind §15's storage rejection
- [Node-defined measurements](node-measures.md) and
  [Live topic discovery](live-topics.md) — the `NodeInfo` descriptor this
  design discovers from
- [Eclipse OpenSOVD](https://projects.eclipse.org/projects/automotive.opensovd)
  — project page
- [`eclipse-opensovd/opensovd`](https://github.com/eclipse-opensovd/opensovd)
  — umbrella repository and the high-level design under `docs/design/`
- [`eclipse-opensovd/opensovd-core`](https://github.com/eclipse-opensovd/opensovd-core)
  — server, gateway, models, providers; pinned at
  `26953d97da4335179083aff41ed52a82886499e7`
- [ISO 17978-1](https://www.iso.org/standard/85133.html),
  [17978-2](https://www.iso.org/standard/86586.html),
  [17978-3](https://www.iso.org/standard/86587.html)
- [ASAM SOVD](https://www.asam.net/standards/detail/sovd/) — the predecessor
  standard and its public overview material
