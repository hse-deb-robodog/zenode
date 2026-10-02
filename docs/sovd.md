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

The sidecar imports no contract. It learns every node, service and
measurement from the latched `<ns>/node/<name>/info` descriptor, so it runs
anywhere zenode is installed and the bus is reachable.

## What is served

Every live node is a **component**. Its `data` collection holds, by category
and group:

| Category | Group | Items | Source |
|---|---|---|---|
| `identData` | `identity` | `node`, `host`, `zenode` | the descriptor |
| `sysInfo` | `runtime` | every `NodeHealth` field, and `state` | the heartbeat |
| `sysInfo` | `presence` | `x-zenode-presence`, `x-zenode-last-seen` | the sidecar |
| `currentData` | `app` | each `@metric` id | the heartbeat |
| `currentData` | `svc` | each service declared `diagnostic="read"`, as the key with `/` as `.` | a service call, on request |

Values are `{"value": …}`; a service reply is served as the object it is.
Nothing is read from topics: a node that wants a value diagnosable keeps it in
a field and declares a read service for it (see
[Contracts](contracts.md#service)). An item is listed only while it can be
read, so a value that is unknown right now — `cpu_percent` before `/proc`
answered, a measurement absent from this heartbeat — is absent from the list
rather than a 404 waiting to happen.

Log records ride under a custom link, `x-zenode-logs`, because the standard's
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
| unknown data id | 404 `error-response` |
| service call timed out, or the node is gone | 504 `not-responding` |
| the handler raised, or replied with something that is not JSON | 502 `error-response` |
| more than `--max-calls` calls in flight | 503 `sovd-server-failure` |

## Trying it with the OpenSOVD MCP server

OpenSOVD ships `opensovd-mcp`, a Model Context Protocol server over any SOVD
endpoint, so an AI assistant can ask a robot about itself. It takes one flag,
`--url`, whose default is `http://localhost:7690/sovd/v1` — where `zenode sovd`
serves by default.

```bash
# terminal 1: a node and the sidecar
uv run python examples/talker.py
uv run zenode sovd --connect tcp/127.0.0.1:17447

# terminal 2: register the MCP server with Claude Code, from the container …
claude mcp add --transport stdio --scope project sovd -- \
    docker run -i --rm --network=host ghcr.io/eclipse-opensovd/opensovd-mcp \
    --url http://127.0.0.1:7690/sovd/v1

# … or from a build of the pinned opensovd-core checkout (it pins a nightly; stable works)
cargo +stable build --release -p opensovd-mcp
claude mcp add --transport stdio --scope project sovd -- \
    ./target/release/opensovd-mcp --url http://127.0.0.1:7690/sovd/v1
```

At the pinned commit the MCP server exposes `list_components`, `list_areas`
and `list_apps`, a `sovd://topology` resource and an `explore-topology`
prompt. It reads discovery only: data items, measurements, service reads and
`x-zenode-logs` are reachable with any HTTP client against the same URL, not
through it yet.

## Security

There is no authentication in the sidecar. It binds loopback by default; to
reach it from elsewhere, terminate TLS and authentication at a reverse proxy.
Phase 1 is read-only; operations arrive together with mTLS, not before.

## Conformance

A SOVD-compatible subset, validated against the OpenSOVD JSON shapes at
`opensovd-core` commit `26953d97`, never "SOVD compliant". The route set,
`version-info` and the error vocabulary are that commit's. The schemas live
under `tests/sovd/schemas` and an integration test walks every endpoint
against them.
