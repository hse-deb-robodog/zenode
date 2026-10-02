# Reusing the Rust `opensovd-core` server instead of writing one

**Status: research note. Not a decision, and nothing here is implemented.**
The decision was taken on 2026-10-02 in the adapter note along the lines of §7:
route A, with route B recorded as the alternative to return to. This page keeps
the evidence; section numbers it cites from the adapter note refer to the
proposal as drafted on 2026-09-21, which the decided version renumbered. It
answers one question raised against the [OpenSOVD adapter](opensovd-adapter.md)
proposal, against primary sources and two throwaway experiments, so the
adapter's §3, §9, §11 and §13 can be revisited on evidence. Written 2026-09-21
against `eclipse-opensovd/opensovd-core` at commit
`26953d97da4335179083aff41ed52a82886499e7` (2026-09-21, `main`), the umbrella
repository `opensovd` at `e769a39` (2026-06-29), `fault-lib` at `4a53a62`
(2025-11-12), and `eclipse-zenoh` 1.9.0, the version `uv.lock` pins.

The question: the adapter note proposes a SOVD HTTP server written in Python.
Is there a way to use the Rust `opensovd-core` server or gateway **as it is**,
so that zenode does not implement, test and maintain a SOVD server at all?

The short version: **as it is, no; with about 300 to 600 lines of Rust, partly.**
The stock `opensovd-gateway` binary cannot be pointed at anything. Without
`--mock` it serves an empty entity tree, `--serve-dir` is a static file mount
for a web UI, `--unix-socket` is a listen address, and the gateway still has no
provider that reaches another process — no remote SOVD proxy, no IPC, no plugin
loading. Both §3 claims of the adapter note hold.

What §3 did not say, and what changes the picture, is that the *library* is
reusable. `opensovd-server` exposes a public `Server::builder()`; an
out-of-tree crate can take it as a git dependency, implement `DataProvider`,
add and remove components at runtime, and get JWT authentication, Rego
authorization, TLS and mTLS without writing any of it. A 165-line probe did
exactly that against a live zenode node: liveliness became `/components`, the
heartbeat became `data`, the latched descriptor supplied the service list, and
a `GET` on a data resource called a zenode `Service` through the zenoh Rust
crate. No Python, no contract import.

The catch is scope. `opensovd-core` implements discovery, `data`, `bulk-data`
and `version-info`. **Operations, logs, functions, faults, modes and locks do
not exist upstream**, entity capabilities are hardcoded, and the route module
is private — so a bridge built on it cannot serve half of what the adapter
note's Phase 1 promises until upstream grows those collections or zenode forks.
And the mapping from a heartbeat to `sysInfo` never goes away; it only moves
from Python into Rust, into a second toolchain, in a project with one
maintainer.

Each claim below is tagged **[run]** (executed on 2026-09-21), **[source]**
(read from the code, issue or document that owns it) or **[inferred]**.

---

## 1. What `opensovd-core` is today

Apache-2.0, workspace version `0.1.1`, Rust edition 2024 [C1]. The
public history starts on 2026-04-28 with "Initial OpenSOVD Core implementation
(#34)" and is 86 commits long [G1]. In the 90 days before this note there were
41 commits: 22 from dependabot, 14 from one developer (`lh-sag`), and 5 from
three others; GitHub lists 8 contributors in total [G1]. **[source]**

| Crate | What it is | Source |
|---|---|---|
| `opensovd-core` | `Topology`, entities, the `DataProvider`, `BulkDataProvider` and `DiscoveryProvider` traits. No HTTP. | [C2] |
| `opensovd-models` | serde types for the JSON shapes | [C1] |
| `opensovd-server` | axum router, `ServerBuilder`, authentication and authorization layers, TLS | [C3] |
| `opensovd-providers` | `DataProviderBuilder`, `Constant`, `ReadableDataResource` — in-process helpers only | [C4] |
| `opensovd-extra` | `JwtAuthenticator` (HS512, RS512), `RegorusAuthorizer` (Rego policies) | [C5] |
| `opensovd-client` | Rust SOVD client, TCP and unix socket | [C1] |
| `opensovd-mocks` | the `--mock` topology, `publish = false` | [C1] |
| `opensovd-cli/gateway`, `opensovd-cli/mcp` | the two shipped binaries | [C6] |

**Collections implemented:** discovery (`/`, `/components`, `/apps`, `/areas`,
relations), `data` (`data-categories`, `data-groups`, `data`, `data/{id}` GET
and PUT), `bulk-data` (added 2026-09-17, #164) and `version-info` [C7]. The
model crate *reserves* `faults`, `operations`, `modes`, `functions`, `locks`
and `logs` as optional links on `EntityCapabilities` [C8]; no route, trait or
provider implements any of them. Issue #156 (2026-08-28) proposes `faults` and
was welcomed by the maintainer, with no pull request yet [I1]. Nothing open
proposes operations, logs or functions. **[source]**

**Releases.** Nothing is on crates.io — `opensovd-core`, `-server`, `-models`,
`-client` and `-providers` all return "does not exist" [R1]. **[run]** There
are no version tags. Two rolling pre-releases, `latest` and `nightly`, carry
prebuilt `opensovd-gateway` and `opensovd-mcp` for
`x86_64-unknown-linux-gnu`, **`aarch64-unknown-linux-gnu`**,
`aarch64-apple-darwin` and `x86_64-pc-windows-msvc`, plus a container image on
GHCR [R2]. The Linux binary is 5.9 MB and links only libc and libgcc. **[run]**

**Toolchain.** `rust-toolchain.toml` pins `nightly-2026-05-07`, and no
`rust-version` is declared [C1]. The library crates nevertheless build on
stable: the probe in §3 compiled `opensovd-core` and `opensovd-server` with
stable cargo 1.97.1. **[run]**

**Stated stability.** None. Version 0.1.1, pre-release tags only, and the
umbrella roadmap targets an MVP "by end of 2026" with "SOVD server delivers
read/paginate DTCs" scheduled for 26Q2 [D2] — which, per #156, has not
happened in this repository. **[source]**

## 2. Every extension point

### 2.1 `DataProvider`

The exact trait at the examined commit [C2]:

```rust
#[async_trait]
pub trait DataProvider: Send + Sync + 'static {
    async fn list(&self, filter: DataFilter) -> Result<Vec<Metadata>>;
    async fn read(&self, data_id: &str, include_schema: bool) -> Result<Data>;
    async fn write(&self, data_id: &str, value: serde_json::Value) -> Result<()>;
    // defaulted, derived from list():
    async fn categories(&self) -> Result<Vec<CategoryInfo>>;
    async fn groups(&self, category_filter: Option<&str>) -> Result<Vec<GroupInfo>>;
    async fn tags(&self) -> Result<Vec<TagInfo>>;
}
```

Async, via `async-trait`. Values and schemas are both plain
`serde_json::Value`: `Data { data, schema: Option<Value> }`, and `Metadata`
carries `id`, `name`, `category`, `groups`, `tags`, an optional `schema`, and
`is_readable` / `is_writable`. Errors are `NotFound`, `ReadOnly` and
`Internal(String)`, which the server maps to 404, 400 and 500
(`opensovd-server/src/routes/error.rs`). There is no variant for "the backend
did not answer": a zenode `ServiceTimeout` can only become `Internal`, whose
message the server logs and replaces with "An internal error occurred" — a 500
where the adapter note's §9 wants a 504 that says what happened. **[source]**

That shape suits zenode well. A provider does not need Rust types for the
payloads it forwards — a heartbeat or a service reply passes through as a
`Value`, and a JSON Schema produced by pydantic's `model_json_schema()` would
pass through as the `schema` without translation. `list()` is called per
request, so a node that gains a measurement needs no re-registration.
**[inferred]** from the signature, and consistent with §3's run.

One provider is attached per entity with
`Component::new(id, name).with_data_provider(p)`; `App` takes one the same way,
`Area` does not, whatever `docs/architecture.md` draws [C9]. The provider must apply `DataFilter` itself — the probe ignored
it and `?categories=sysInfo` returned everything. **[run]**

### 2.2 The topology is dynamic

This is the point most relevant to zenode, where nodes appear and disappear
with their liveliness tokens. `Topology` is an `Arc<RwLock<…>>` handle that is
cloned into the server; `topology.write().await` yields a guard with
`add_component`, `add_app`, `add_area` and the matching `remove_*`, and
`subscribe()` broadcasts `TopologyEvent`s [C10]. The server reads it per
request, so entities can be added and removed while it is serving. **[source]**
Confirmed: a component added one second after `serve()` appeared in
`/components`, answered `data` reads, and vanished when removed. **[run]**

### 2.3 `DiscoveryProvider`

A second trait yields a long-lived stream of `(remove, add)` topology diffs,
registered with `ServerBuilder::discovery(Box<dyn DiscoveryProvider>)`; several
streams are merged [C11]. Its doc comment describes finding *remote SOVD
servers*, e.g. by mDNS-SD. No implementation is in the tree — only a mock in the
server's tests. The open pull request #119 adds an mDNS implementation, and its
own design note is explicit about what that does not do [I2]:

> The mDNS provider does not currently implement full SOVD federation or
> proxying. In particular, it does not yet connect to a discovered private HPC
> […] attach remote data providers to gateway topology entities; forward
> read/write/operation requests […] That proxy layer should be designed
> separately.

So discovered servers become name-only components. A zenoh liveliness
subscriber fits this trait naturally, but writing to `Topology` directly (§2.2)
does the same with less ceremony. **[source]**

### 2.4 `BulkDataProvider`, `Authenticator`, `Authorizer`, layers, services

- `BulkDataProvider` — streamed upload, download and delete per category [C2].
  Irrelevant to the adapter's scope.
- `Authenticator` / `Authorizer` — traits in `opensovd-server`, with `NoAuth`
  and `AllowAll` as defaults and `JwtAuthenticator` / `RegorusAuthorizer` in
  `opensovd-extra` [C3] [C5]. The JWT verifier takes a static key (HMAC secret
  or an RSA public key in PKCS#1 DER); there is no JWKS fetch or OpenID Connect
  discovery. Issue #33 asks for X.509 trust anchors [I3].
- `ServerBuilder::layer(…)` — any tower layer, applied outside authentication.
- `ServerBuilder::service(path, svc)` — mounts any tower service at a path,
  *inside* the authentication and authorization layers [C3]. This is the only
  way to add a route without forking, and it is what `--serve-dir` uses.

### 2.5 What does not exist

Checked by reading the tree and by searching issues and pull requests across
the organisation [G2]. **[source]**

| Mechanism | Present? |
|---|---|
| Dynamic loading — `cdylib`, plugin directory | No. `unsafe_code = "deny"` workspace-wide [C1]. |
| Config-file-driven generic provider | No. The gateway has no config file at all [C6]. |
| Static JSON provider | No. See `--serve-dir` below. |
| Remote SOVD / HTTP proxy provider, gateway federation | No. Planned in the umbrella design ("SOVD Gateway — forwards SOVD requests to appropriate backend targets") [D1], named as future work in #119 [I2], asked about in #9 without a maintainer answer [I4]. |
| IPC provider (the design's "Diagnostic Library … relays diagnostic resources via IPC to the SOVD Server") | No. Issue #58 proposes a self-registering HTTP variant with a `GenericHttpProxy` on the server side; it is labelled `needs-triage`, has no comments and no pull request [I5]. |
| gRPC, SOME/IP, uProtocol, iceoryx, zenoh | No mention anywhere in the organisation's issues or in the three cloned repositories [G2]. |
| Provider traits for faults, operations, logs | No (§1). |

### 2.6 What the gateway flags actually do

From `opensovd-cli/gateway/src/main.rs` and `cli.rs` [C6], then executed against
the prebuilt `latest` x86_64 binary (`0.1.1-dev (26953d9 2026-09-21)`):

- **`--unix-socket PATH`** makes the *server listen* on a unix socket (or an
  abstract one with `@name`) instead of TCP. It is a front-side transport for
  SOVD clients, not a back-side channel to providers. **[source]**
- **`--mock`** swaps the empty topology for `opensovd_mocks::create_mock_topology()`.
  Without it the topology is `Topology::default()` and nothing can fill it:
  `GET /sovd/v1/components` returned `{"items":[]}`. **[run]**
- **`--serve-dir /ui:./dist`** mounts `tower_http::services::ServeDir`. Could a
  Python sidecar simply write JSON files for it to serve? No, on four counts,
  all observed. **[run]**
  - A file without an extension is served as `application/octet-stream`.
  - Anything but GET and HEAD returns 405, so no PUT and no operations.
  - Mounting over a real route panics at startup: `--serve-dir
    /sovd/v1/components:./static` died with "Insertion failed due to conflict
    with previously registered route".
  - Files mounted elsewhere are not entities: `/components` stays empty, so no
    SOVD client would ever traverse to them.

## 3. An out-of-tree Rust bridge is feasible — verified

The public entry point a third party calls [C3]:

```rust
Server::builder()
    .base_uri("http://0.0.0.0:7690/sovd")?
    .listener(tokio::net::TcpListener::bind(addr).await?)   // or a UnixListener
    .topology(topology)                                      // the shared handle from §2.2
    .authenticator(JwtAuthenticator::new(algo, &key, issuer))
    .authorizer(RegorusAuthorizer::from_paths(&policies, &data)?)
    .tls(rustls_server_config)                               // feature "tls"; client CAs give mTLS
    .layer(cors_or_trace_layer)
    .build()?
    .serve().await?;
```

`examples/server/simple/simple.rs` is the upstream template: 206 lines, one
component, seven data items [C12]. No third-party provider was found anywhere.

**Probe 1 — the API is usable from outside. [run]** A 73-line crate with
`opensovd-core` and `opensovd-server` as git dependencies pinned to `26953d9`
implemented `DataProvider` over a `serde_json::Value`, added a component one
second after startup and removed it three seconds later. Cold build: 14 s on
stable cargo 1.97.1. `/components`, the entity, `data?groups=`, `data/{id}`,
`data-categories`, a 404 for an unknown id and a 400 for a PUT on a read-only
item all behaved correctly with no HTTP code written.

**Probe 2 — it works against a real zenode node. [run]** 165 lines, adding
`zenoh = "=1.9.0"` and `zenoh-ext` (both with `unstable`). Cold build 67 s;
debug binary 347 MB, not representative of a release build. It connected to
`examples/talker.py` listening on `tcp/127.0.0.1:17447` and did four things:

| Bus side | SOVD side | Observed |
|---|---|---|
| liveliness subscriber on `node/*`, `history(true)` | `add_component` / `remove_component` | `talker` listed; gone within 4 s of `kill -9` |
| subscriber on `node/*/health`, payload kept as `Value` | `list()` and `read()`; `host`, `node` as `identData`, five fields as `sysInfo`, `measures` as `currentData` group `app` | `{"id":"uptime_s","data":4.0034…}`, `{"id":"host","data":"cachyos-x8664"}`, the `@metric`s `amplitude` and `ticks` listed |
| zenoh-ext advanced subscriber on `node/*/info`, `HistoryConfig::default().detect_late_publishers().max_samples(1)` | service ids taken from `NodeInfo.serves` | `demo/sum` and `node/talker/trace` listed, although the bridge started 3 s after the node |
| `session.get("demo/sum")` with a JSON payload, `Encoding::APPLICATION_JSON` and an envelope attachment | `GET …/data/svc:demo.sum` | `{"id":"svc:demo.sum","data":{"total":6.5}}` |

What a real bridge needs beyond the probe: `DataFilter`, schemas on `read`,
request bodies for services, a staleness rule for the heartbeat, namespaces,
configuration, logging, tests. A realistic size for *discovery plus data* is
**300 to 600 lines of Rust** plus its tests. **[inferred]**

**What it cannot do without upstream changes. [source]**

- **Advertise anything but `data` and `bulk-data`.** The component handler
  builds `EntityCapabilities { hosts, belongs_to, data, bulk_data,
  ..Default::default() }` [C13]. There is no hook to set `operations`, `logs`
  or `faults` links, and the root handler emits only `components`, `apps` and
  `areas` — no `functions`.
- **Reuse their router for new collections.** `mod routes` is private in
  `opensovd-server` [C3]; only `ServerBuilder` is exported. New collections can
  be mounted with `.service()`, but they are hand-written axum routes that the
  capability responses do not link to — a SOVD client cannot discover them. At
  that point zenode is writing a SOVD implementation again, in Rust.
- **POST.** No route in the server accepts an execution. Operations are blocked
  on upstream entirely.

## 4. What zenode already puts on the bus for a non-Python consumer

All **[source]** unless marked.

**Payloads are JSON by default.** `PydanticJsonCodec` encodes with
`model_dump_json()` and declares `zenoh.Encoding.APPLICATION_JSON`
(`src/zenode/codec.py`). A Rust process forwards them as `serde_json::Value`
without knowing the pydantic type — confirmed for `NodeHealth`, `NodeInfo` and
a service reply. **[run]** `RawCodec` topics (camera frames) are opaque bytes
and are not a diagnostic source anyway.

**The envelope is decodable without Python.** It rides in the zenoh attachment
as compact JSON — `{"n": node, "s": seq, "t": ts_ns, "tp": traceparent?}`
(`src/zenode/envelope.py`) — and the decoder is tolerant by design, so a bridge
that sends `{"n":"sovd-bridge","s":0,"t":…}` on a query is a well-formed caller.
The probe did. **[run]**

**Presence** is a liveliness token at `<ns>/node/<name>`
(`src/zenode/presence.py`); the Rust liveliness API reads it directly. **[run]**

**A service call on the wire** (`src/zenode/service.py`):

| Element | Value |
|---|---|
| key | the resolved service key, e.g. `demo/sum` or `<ns>/demo/sum` |
| selector parameters | none |
| query payload | the request model as JSON, encoding `application/json`. **Required** — a query with no payload gets `{"error":"missing request payload"}`; an `Empty` request is `{}` |
| query attachment | the envelope; optional, used for tracing |
| reply | one sample on the same key, JSON, encoding `application/json` |
| error reply | `reply_err` with `{"error":"<message>"}`; `bad request: …` for a payload that fails validation |
| no server | no reply before the timeout |

**The latched descriptor is most of Phase 2 already, and lacks exactly the
parts a bridge needs.** Commit `93822c8` added `NodeInfo` on
`<ns>/node/<name>/info`, published through a zenoh-ext advanced publisher and
served from zenoh's own threads (`src/zenode/msgs/info.py`,
`src/zenode/reporting.py`). It carries `node`, `host`, `zenode`, `publishes`
and `subscribes` (key, `schema_name`, flags), `measures` (id, unit, kind,
`integral`, description) and:

```python
class ServiceInfo(BaseModel):
    key: str
    request: str = ""    # the class *name*
    reply: str = ""
```

Set against the adapter note's Phase 2 ("a per-node descriptor … listing what
that node serves with schemas attached"):

| Needed by a contract-free bridge | In `NodeInfo` today |
|---|---|
| which node serves which key | yes — and this answers §7's "nothing on the bus links a service to a node" |
| measure catalog with units | yes |
| `host`, for the component/app split of §8 | yes |
| request and reply **JSON Schema** | **no** — names only, and `info.py` says why a name must never be used as a type identity |
| **`read_only`** | **no** — the field does not exist on `Service` yet either |
| `Service.description` | **no** — declared on `Service`, not published |

The gap is three fields on `ServiceInfo`, plus the `read_only` flag the adapter
note already asks for in its §6. A schema is a kilobyte or three per service,
published once and latched, so the "bytes worth counting" argument of
`info.py` is not violated. **[inferred]** Without them a non-Python bridge can
list services but cannot tell a safe read from a motor command, and must not
expose them — the probe's `svc:` resources hard-coded a request body and are
not a design.

## 5. Other reuse routes

**(i) PyO3 / maturin bindings around `opensovd-server`.** Nothing of the kind
exists upstream [G2]. It would be a compiled wheel per architecture, built and
published by zenode, wrapping an async Rust server and calling back into Python
for every `read` — tokio and the GIL in one process. It would be an optional
sidecar extra, not a node dependency, so it does not break the letter of
"runtime dependencies are `eclipse-zenoh` and `pydantic`"; it breaks its
reason, which is not shipping compiled artefacts for ARM targets. It costs more
than route B and buys only that the mapping stays in Python. **Rejected.**
**[inferred]**

**(ii) Python sidecar stays, speaking an IPC the Rust server consumes.** There
is no such IPC (§2.5). If #58 landed as written, a registered app would expose
`/api/data`, `/api/data/{id}` and an SSE stream over HTTP [I5] — still an HTTP
server in Python, for data only, behind an untriaged proposal. **Not available.**
**[source]**

**(iii) `fault-lib` as the faults route.** `main` is a 2025-11-12 skeleton: a
`FaultSink` trait with no transport behind it, one dependency (`thiserror`),
and the Diagnostic Fault Manager lives only in open pull requests (#4, #7)
[F1]. It is also the ISO 14229 lineage — DTCs, debouncing, operation cycles —
that the adapter note's §10 declines to import. **Not a route today, and not
the model zenode wants.** **[source]**

**(iv) `opensovd-client`, `opensovd-mcp`.** Consumers, not servers. They work
against any conformant server, so they are equally a benefit of routes A and B
and decide nothing. The `classic-diagnostic-adapter` contains a second, much
fuller SOVD server, but it is driven by MDD files and UDS transport and is not
a general library [G3]. **Irrelevant to the choice.** **[source]**

**(v) Upstreaming a zenoh provider or a remote provider.** Process-wise the
project is open: Apache-2.0, Eclipse Contributor Agreement, "create a new issue
describing the work you plan to do", conventional commits, `good first issue`
and `help wanted` labels in use [C14], and the one outside proposal that is
comparable (#156, faults) got "Looking forward to the PR" within eleven days
[I1]. But review capacity is one person (§1), #58 and #9 — the two issues
closest to this one — have had no maintainer reply in four and ten months
[I4] [I5], and zenoh appears nowhere in the project [G2]. A *zenoh* provider
upstream is unlikely to be accepted as a core dependency and would not encode
zenode's conventions in any case. What could plausibly be upstreamed is
generic: an `OperationProvider` trait and routes, a `LogProvider`, or a
capability hook. Each is a Rust contribution to a standard zenode does not own
a copy of, reviewed on someone else's schedule. **Possible, slow, and not a
substitute for a plan.** **[inferred]**

**(vi) Keep the Python server, shrink its burden.** Already in the adapter
note's §9: vendor `tests/opensovd-gateway/test_api.py`, a 294-line traversal
that validates every response against the schema the server returns, using
`httpx` and `jsonschema` as test-only dependencies [C15]. Two facts sharpen
it. The traversal asserts only on collections upstream implements, so it
covers discovery and data and says nothing about logs or operations. And
`opensovd-models` derives its schemas with `schemars`; there is no standalone
OpenAPI document in the repository to vendor — the schemas exist only as
`?include-schema=true` responses from a running server. **[source]**

## 6. Cost comparison

| | **A** own Python server | **B** out-of-tree Rust bridge | **C** upstream provider | **D** PyO3 |
|---|---|---|---|---|
| SOVD HTTP, routing, models, error shapes | zenode writes and maintains | upstream's | upstream's | upstream's |
| Discovery + `data` | zenode | works today **[run]** | works today | works today |
| Logs, operations, functions | zenode writes them; nothing blocks | **blocked on upstream**, or hand-written axum routes no client can discover | blocked on own upstream PRs | blocked, as B |
| Faults | adapter §10 options 1 to 3 | blocked; #156 open, UDS-shaped | blocked | blocked |
| The mapping (heartbeat → `sysInfo`, measures → `currentData`, …) | Python, as further columns on `exporter.Metric` — one table | **Rust, a second table** that cannot share `exporter.py`'s | Rust, in someone else's repository | Python |
| Learns services from | `--contract` import (Phase 1), descriptor later | descriptor only — **needs §4's three fields first** | as B | `--contract` |
| JWT / OAuth bearer | cannot: needs `cryptography`. Reverse proxy | built in: HS512, RS512, Rego policies | as B | as B |
| TLS, mTLS | stdlib `ssl` | built in, rustls | as B | as B |
| Toolchain and CI | none new | cargo, clippy, a Rust test job, release binaries per target, a second lockfile | the ECA and upstream's nightly toolchain, `prek`, Bazel PRs in flight | maturin, wheels per Python × architecture |
| ARM | nothing to build | cross-compile or an arm64 runner; upstream does the latter for its own binaries [R2] | upstream's binaries, if it lands | wheel per target |
| Release coupling | none; vendored test file | git `rev` pin on a 0.1.x library with no tags and no crates.io release | their release cadence | as B, plus PyO3 |
| Tested with `harness()` | yes, in-process, `integration` marker | **no** — `harness()` is peer mode with no endpoints, so an external binary cannot join. Needs a listening node and a subprocess, as upstream tests its own gateway | not zenode's tests | partly |
| "Degrade, never crash" | zenode's code, zenode's rules | upstream's server is `deny(unsafe_code)` and lint-strict, but the gateway panicked on a route conflict **[run]** | — | — |
| Project constraints | intact | intact: a sidecar binary is not a node dependency. Best kept in its own repository | intact | bends the ARM reason |
| New code, order of magnitude | 500 to 700 lines of Python with tests, full Phase 1 | 300 to 600 lines of Rust plus tests, **data only**, plus the descriptor change in the runtime | unbounded | more than B |

Two things about this table are easy to misread.

**B does not remove the part that is hard.** The HTTP surface for discovery and
data is perhaps a third of the adapter's Phase 1; the rest is topology,
staleness, the mapping table, service calls, the operation allowlist and
tests. All of that survives in B, translated into a language the rest of the
project is not written in. What B really removes is `server.py`, `model.py`,
and the authentication problem.

**B's authentication win is real but narrower than it looks.** HS512 and RS512
against a static key is not OAuth 2.0 with OpenID Connect discovery; a
deployment with a real identity provider still needs key distribution or a
proxy. It is nevertheless strictly more than the Python server can do.

## 7. Recommendation

**Do not pivot now. Correct the adapter note, and make the one runtime change
that keeps the pivot cheap later.**

1. **The answer to "can we use it as-is" is no**, and that is unlikely to
   change soon: remote providers are design-document intent with no issue
   owner, and the collections zenode needs most after `data` — logs and
   operations — have no upstream proposal at all.
2. **Route B is a legitimate option, not a rejected one**, and the adapter
   note's §13 should say so. It is the better choice if, and only if, the
   first deliverable is accepted as *discovery plus data with real
   authentication*, the maintainer is willing to own a Rust crate, and
   operations — which §11 of the adapter note already treats as the dangerous
   half — can wait for upstream. It should then live in its own repository, so
   that zenode stays a pure-Python `uv` project.
3. **Otherwise route A stands**, because it is the only one where logs,
   operations and functions are not blocked on anyone, where the mapping stays
   in one table, and where the whole thing runs under `harness()`.
4. **Independently of A or B, pull the descriptor half of Phase 2 forward**:
   `read_only` on `Service`, and `read_only`, `description`, `request_schema`
   and `reply_schema` on `ServiceInfo`. Route A loses its `--contract`
   deployment coupling; route B becomes possible at all; and `zenode topics
   --live` gains schemas. This is the only work item that is not wasted under
   either outcome.
5. **Revisit when any of these happens upstream:** an `operations` collection
   lands; a remote or IPC provider lands (#58, or the proxy layer #119 defers);
   or the crates are tagged and published.

Corrections owed to the adapter note, if this is accepted:

- §2: add `bulk-data`, and that the server ships JWT, Rego, TLS and mTLS.
- §3, first finding: still true, but incomplete — the trait is implementable
  from an out-of-tree crate, and the topology is mutable at runtime.
- §3, second finding: still true. `--serve-dir` and `--unix-socket` are not
  back-side channels.
- §7, "nothing on the bus links a service to a node today": outdated since
  `93822c8`; `NodeInfo.serves` does.
- §11, authentication: the Rust server is a third option beside a reverse
  proxy and stdlib mTLS.
- §13, "Sitting behind the OpenSOVD gateway as a provider. Not possible": true
  for the stock binary; a custom binary built on their library is possible and
  is rejected, if it is, on cost and scope rather than feasibility.

## 8. Open questions and what was not verified

- **Cross-compilation was not attempted.** Upstream builds aarch64 on a native
  arm64 runner rather than cross-compiling [R2]. With authentication or TLS
  enabled the dependency tree includes `aws-lc-sys`, which needs a C toolchain
  and CMake for the target; zenoh's default features pull in TLS and QUIC
  transports as well. Whether a `cross` build of the bridge is painless is
  unknown. **[inferred]**
- **Release-profile size and memory were not measured.** Only the debug build
  was produced. Upstream's release gateway is 5.9 MB without zenoh.
- **Authentication, authorization and TLS were read, not run.** The `auth` and
  `mtls` examples exist [C12]; neither was executed here, and the probe used
  `NoAuth`.
- **The conformance traversal was not run against the probe.** `test_api.py`
  expects `--mock` fixtures in places; how much of it is generic was judged by
  reading the first 60 lines, not by running it.
- **Namespaces.** The probe used the empty namespace. A bridge for
  `<ns>/node/*` is a string prefix, but one SOVD server per namespace versus
  `/areas` (adapter §7) was not explored.
- **Heartbeat staleness.** The probe serves the last heartbeat for as long as
  the liveliness token lives. A wedged node keeps its token; the bridge would
  need the adapter note's "carry its own timestamp" rule, and `DataProvider`
  has no field for a value's age other than putting it in the data.
- **zenoh-ext is unstable in Rust too.** The advanced subscriber needs the
  `unstable` feature on both crates; the same caveat the
  [storage note](zenoh-storage-for-sovd.md) records for Python applies.
- **Wire compatibility across zenoh versions** was tested only for Rust 1.9.0
  against Python 1.9.0. The current crate is 1.10.1 [R1].
- **Upstream intent was read from artefacts, not asked.** Nobody was contacted.
  A question on #9 or #58 — "is a remote or IPC `DataProvider` wanted, and
  would an `OperationProvider` be accepted?" — would cost one comment and
  replace §5 (v)'s inference with an answer.
- **GitHub code search** returned nothing for "zenoh" across the organisation,
  but code search is incomplete for small repositories; the three cloned
  repositories were grepped in full, the other twelve were not.
- The ISO 17978-3 text was not consulted; every statement about what SOVD
  permits is second-hand, from `opensovd-models` and upstream's design
  documents.

## 9. References

`eclipse-opensovd/opensovd-core` at
[`26953d9`](https://github.com/eclipse-opensovd/opensovd-core/tree/26953d97da4335179083aff41ed52a82886499e7):

- **[C1]** `Cargo.toml` (workspace members, version `0.1.1`, edition 2024,
  `unsafe_code = "deny"`, no `rust-version`), `rust-toolchain.toml`
  (`nightly-2026-05-07`), `opensovd-mocks/Cargo.toml` (`publish = false`).
- **[C2]** `opensovd-core/src/lib.rs` (the public exports),
  `opensovd-core/src/data.rs` (`DataProvider`, `Data`, `Metadata`,
  `DataError`), `opensovd-core/src/bulkdata.rs`.
- **[C3]** `opensovd-server/src/server.rs` — `ServerBuilder` L153-L420
  (`listener`, `base_uri`, `authenticator`, `authorizer`, `layer`, `topology`,
  `service`, `discovery`, `tls`, `build`), `run_discovery` L422-L470;
  `opensovd-server/src/lib.rs` — `mod routes` is private.
- **[C4]** `opensovd-providers/src/data/builder.rs`, `resource.rs`,
  `constant.rs`.
- **[C5]** `opensovd-extra/src/auth/jwt.rs` (`JwtAlgorithm::{HS512, RS512}`,
  `Claims`), `opensovd-extra/src/auth/rego.rs`,
  `examples/server/auth/sovd_authz.rego`.
- **[C6]** `opensovd-cli/gateway/src/main.rs` (`configure_topology`,
  `configure_listener`, the `--serve-dir` wiring), `cli.rs`, `serve_dir.rs`.
- **[C7]** `opensovd-server/src/routes/mod.rs` — the route inventory in the
  module docstring and `router()`.
- **[C8]** `opensovd-models/src/discovery.rs` L55-L156 — `EntityCapabilities`.
- **[C9]** `opensovd-core/src/entity/component.rs`, `app.rs`, `area.rs`.
- **[C10]** `opensovd-core/src/topology.rs` — `TopologyWriteGuard` L282-L342,
  `Topology::{subscribe, read, write}` L357 onward; `docs/architecture.md`.
- **[C11]** `opensovd-core/src/discovery.rs`;
  `opensovd-server/tests/builder.rs` L194-L213 (the only implementation, a mock).
- **[C12]** `examples/server/simple/simple.rs`, `examples/server/auth/auth.rs`,
  `examples/server/mtls/mtls.rs`.
- **[C13]** `opensovd-server/src/routes/entities/component.rs` L123-L133;
  `opensovd-server/src/routes/entities/mod.rs` L65-L72 (the root entity).
- **[C14]** `CONTRIBUTING.md`.
- **[C15]** `tests/opensovd-gateway/test_api.py`; `pyproject.toml` (test
  dependencies).

Issues and pull requests in `eclipse-opensovd/opensovd-core`:

- **[I1]** [#156](https://github.com/eclipse-opensovd/opensovd-core/issues/156)
  — implement the `faults` resource; maintainer reply 2026-09-08.
- **[I2]** [#119](https://github.com/eclipse-opensovd/opensovd-core/pull/119)
  — mDNS discovery, open; `docs/mdns/README.md` at head `2dd412b`, section
  "What mDNS Does Not Do Yet". Issue [#31](https://github.com/eclipse-opensovd/opensovd-core/issues/31).
- **[I3]** [#33](https://github.com/eclipse-opensovd/opensovd-core/issues/33)
  — JWT authentication with X.509 trust anchors.
- **[I4]** [#9](https://github.com/eclipse-opensovd/opensovd-core/issues/9)
  — gateway versus server architectural model; opened 2025-11-06, one
  community comment asking which code connects the gateway to a server.
- **[I5]** [#58](https://github.com/eclipse-opensovd/opensovd-core/issues/58)
  — `opensovd-diagnostic-lib`, self-registering apps and a `GenericHttpProxy`;
  opened 2026-05-06, `needs-triage`, no comments.

Design documents, `eclipse-opensovd/opensovd` at
[`e769a39`](https://github.com/eclipse-opensovd/opensovd/tree/e769a390156984afd5b540a6001570e96fd765a7):

- **[D1]** `docs/design/design.md` — Diagnostic Library ("relays diagnostic
  resources via IPC to the SOVD Server"), SOVD Gateway ("forwards SOVD requests
  to appropriate backend targets"), the example entity hierarchy.
- **[D2]** `docs/design/mvp.md` — MVP use cases and the 25Q4 to 26Q4 timeline.

Other repositories and registries:

- **[F1]** `eclipse-opensovd/fault-lib` at
  [`4a53a62`](https://github.com/eclipse-opensovd/fault-lib/tree/4a53a6284490ba779fa6a8e8620f0a4c07fbc2e6)
  — `Cargo.toml`, `src/sink.rs`, `src/api.rs`; open pull requests #4 and #7.
- **[G1]** GitHub API, 2026-09-21: `repos/eclipse-opensovd/opensovd-core`
  (created 2025-07-24, 21 stars, 26 forks), `/commits?since=2026-06-23`,
  `/commits?path=…` for first-commit dates, `/contributors`.
- **[G2]** `gh search issues --owner eclipse-opensovd` for `zenoh`,
  `uprotocol`, `iceoryx`, `federation`, `remote provider`: no results.
  `grep -rli zenoh` over the three clones: no results. `gh repo list
  eclipse-opensovd`: fifteen repositories, none with Python bindings.
- **[G3]** `eclipse-opensovd/classic-diagnostic-adapter` at `c1a5d8b` — README.
- **[R1]** crates.io API, 2026-09-21: `opensovd-core`, `opensovd-server`,
  `opensovd-models`, `opensovd-client`, `opensovd-providers` — "does not
  exist"; `zenoh` max stable 1.10.1.
- **[R2]** [opensovd-core releases](https://github.com/eclipse-opensovd/opensovd-core/releases)
  — `latest` (2026-09-21) and `nightly` pre-releases and their assets;
  `.github/workflows/ci.yaml` L105-L108 for the build matrix
  (`ubuntu-24.04-arm` for aarch64).

zenode, this repository at `3602083`: `src/zenode/codec.py`,
`src/zenode/envelope.py`, `src/zenode/service.py`, `src/zenode/presence.py`,
`src/zenode/reporting.py`, `src/zenode/msgs/info.py`,
`src/zenode/msgs/health.py`, `src/zenode/msgs/log.py`,
`src/zenode/testing.py` (`local_transport`), and commit `93822c8`.

Experiments: two throwaway Rust crates and one prebuilt binary, run on
2026-09-21 on x86_64 Linux with stable cargo 1.97.1. They were kept out of the
repository on purpose; §2.6 and §3 state what each did.
