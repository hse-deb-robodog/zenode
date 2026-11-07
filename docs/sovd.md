# SOVD

zenode integrates with [ISO 17978](https://www.iso.org/standard/86587.html)
(Service-Oriented Vehicle Diagnostics) through **`zenode-sovd`**, an
out-of-process telemetry provider built on
[Eclipse OpenSOVD](https://github.com/eclipse-opensovd/opensovd-core)
(`opensovd-core` and `opensovd-server`) and packaged as a standalone Docker
image in its own repository.

Instead of maintaining a custom HTTP server inside `zenode`, `zenode` nodes
publish self-describing telemetry on the Zenoh bus, and `zenode-sovd` bridges
that bus into OpenSOVD's `Topology` and `DataProvider` traits. The design and
trade-offs are documented in [the adapter design note](design/opensovd-adapter.md)
and [the `opensovd-core` reuse note](design/opensovd-core-reuse.md).

## Running with Docker

Run `zenode-sovd` alongside any system running zenode:

```bash
# Connect to a local zenode router/node on port 7447 and serve SOVD on 7690
docker run --rm --network=host ghcr.io/zenode/zenode-sovd:latest \
    --connect tcp/127.0.0.1:7447 \
    --listen 127.0.0.1:7690 \
    --base-uri /sovd
```

Or in a `docker-compose.yml` alongside your robot stack:

```yaml
services:
  zenode-sovd:
    image: ghcr.io/zenode/zenode-sovd:latest
    network_mode: host
    command:
      - --connect=tcp/127.0.0.1:7447
      - --listen=0.0.0.0:7690
      - --base-uri=/sovd
      - --namespace=
```

Query it with any SOVD client:

```
GET http://robot:7690/sovd/version-info
GET http://robot:7690/sovd/v1/components
GET http://robot:7690/sovd/v1/components/nav/data?groups=app
GET http://robot:7690/sovd/v1/components/nav/data/state.get_pose?include-schema=true
```

`zenode-sovd` imports no Python contract. It learns every node, service, schema
and measurement from the latched `<ns>/node/<name>/info` descriptor on the
Zenoh bus.

## What is served

Every live node holding a liveliness token at `<ns>/node/<name>` is registered
as a SOVD **component** in the OpenSOVD topology. Its `data` collection exposes:

| Category | Group | Items | Source |
|---|---|---|---|
| `identData` | `identity` | `node`, `host`, `zenode` | the latched `NodeInfo` descriptor |
| `sysInfo` | `runtime` | every `NodeHealth` field (described by `NodeInfo.runtime`), and `state` | the `NodeHealth` heartbeat |
| `sysInfo` | `presence` | `x-zenode-presence`, `x-zenode-last-seen` | liveliness + heartbeat age |
| `currentData` | `app` | each `@metric` id declared in `NodeInfo.measures` | the `NodeHealth` heartbeat |
| `currentData` | `svc` | each service declared `diagnostic="read"`, with `/` replaced by `.` | a Zenoh queryable call on request |

Scalar values are wrapped as `{"value": …}` with a matching JSON Schema so
standard OpenSOVD clients can validate them; a service reply is already a JSON
object and is forwarded with the `reply_schema` published in `NodeInfo.serves`.
Nothing is scraped from topics: a node that wants a value diagnosable either
exposes it as a `@metric` on its heartbeat or declares a `diagnostic="read"`
service (see [Contracts](contracts.md#service)).

## Settings and writes

Two descriptor fields are there for `zenode-sovd` to serve settings as
`storedData` and accept `PUT` on a data item:

- `NodeInfo.config` / `NodeInfo.config_schema`: the node's `NodeConfig` and its
  JSON Schema. A node opts in with `describe_config = True`; without that,
  both are empty. These fields are read-only.
- `ServiceInfo.diagnostic == "write"`: a service whose typed request is the
  `PUT` body, validated against `request_schema`. The node's handler applies
  the write. A successful write republishes the descriptor, so the new value
  appears in `NodeInfo.config`.

Setting a field of `NodeInfo.config` does not write it. A setting is writable
only if the node declares a `"write"` service for it.

`examples/tunable.py` runs both halves, with a small client in place of
`zenode-sovd`:

```bash
uv run python examples/tunable.py drive
uv run python examples/tunable.py tune 0.8
```

## Presence

| `x-zenode-presence` | Meaning |
|---|---|
| `live` | liveliness token held, heartbeats arriving within `3 × health_interval` |
| `silent` | liveliness token held, more than three `health_interval`s without a heartbeat — the node's loop is likely wedged |

When a node's liveliness token is dropped, its component is removed from the
active OpenSOVD topology (or retained for the configured grace window).

## Trying it with the OpenSOVD MCP server

OpenSOVD ships `opensovd-mcp`, a Model Context Protocol server over any SOVD
endpoint, so an AI assistant can inspect a running zenode system:

```bash
# terminal 1: a node and the zenode-sovd container
uv run python examples/talker.py
docker run --rm --network=host zenode-sovd:latest \
    --connect tcp/127.0.0.1:17447

# terminal 2: register the OpenSOVD MCP server with Claude Code
claude mcp add --transport stdio --scope project sovd -- \
    docker run -i --rm --network=host ghcr.io/eclipse-opensovd/opensovd-mcp \
    --url http://127.0.0.1:7690/sovd/v1
```
