# Configuration

Reference for configuring a deployment: the TOML file, environment overrides,
and how nodes share facts without duplicating them.

## Overview

One TOML file describes a deployment. Each node validates only its own
section, so a broken `[node.joy]` cannot prevent `nav` from starting.

```toml
[transport]
mode = "client"
connect = ["tcp/192.168.4.100:7447"]
namespace = "robodog"

[node.nav]
max-speed = 0.5

[geometry]
wheel-radius = 0.0485
```

With no `./zenode.toml`, defaults apply: peer mode with multicast scouting,
which works on a LAN with no configuration at all. A file that is named
explicitly, through `$ZENODE_CONFIG` or `run(config_path=…)`, must exist; a
missing one raises `ConfigError`.

## Precedence

Lowest to highest:

1. Model defaults
2. TOML file: `./zenode.toml`, or `$ZENODE_CONFIG`, or `run(config_path=…)`
3. Environment variables

Environment names are derived from the section: `ZENODE_TRANSPORT__CONNECT`,
`ZENODE_NAV__MAX_SPEED`, `ZENODE_GEOMETRY__WHEEL_RADIUS`. The double underscore
separates section from field, and a further one descends into a nested model:
`ZENODE_NAV__PID__KP` sets `kp` in `[node.nav.pid]`.

An environment value is parsed as JSON when it parses (`0.5`, `true`,
`["a", "b"]`). Otherwise a value containing a comma becomes a list
(`tcp/a:7447,tcp/b:7447`), and anything else stays a string. A list with one
element has no comma, so write it as JSON: `ZENODE_TRANSPORT__CONNECT='["tcp/a:7447"]'`.
A string field
whose value looks like a number therefore needs JSON quotes:
`ZENODE_CAM__DEVICE='"0"'`.

Keys may be written `max-speed` or `max_speed`; both resolve to the same field.
Unknown keys in `[transport]` and `[node.*]` are rejected at load, whether they
come from the file or the environment, so a typo fails at startup.

## Node configuration

Declare a model and annotate it on the node. The section is `[node.<name>]`:

```python
class NavConfig(NodeConfig):
    max_speed: float = 0.5
    control_rate_hz: float = 15.0


class Nav(Node):
    name = "nav"
    config: NavConfig      # loaded automatically
```

`run()` loads and validates it before the node starts. A model with required
fields and nothing to fill them raises `ConfigError` with the validation
detail at startup, instead of at first use.

The loaded model is available as `self.config`, and `@every` can read a rate
from it by name:

```python
@every("control_rate_hz", unit="hz")
async def tick(self) -> None: ...
```

## Changing settings at runtime

Configuration is loaded once, before the node starts. Nothing watches the file
or the environment afterwards.

A setting can be changed at runtime only if the node declares a
`diagnostic="write"` service for it. The handler validates the value, applies
it, and replaces `self.config` (see [Contracts](contracts.md#service)). A node
with `describe_config = True` carries its current config on its descriptor,
which shows the change after the next heartbeat (see [Nodes](nodes.md) and
[the SOVD page](sovd.md)).

A runtime change lives in memory. It is not written back to the TOML file, and
a restart returns the node to what the file and the environment say.
In `examples/rover`, `drive.py` serves such a write and `mission.py` calls it.

## Transport

```toml
[transport]
mode = "peer"                 # or "client"
connect = ["tcp/host:7447"]
listen = []
namespace = "robodog"
host = "jetson"               # deployment label; defaults to socket.gethostname()
multicast_scouting = true
shared_memory = false
timestamping = true
```

| Field | Default | Effect |
|---|---|---|
| `mode` | `peer` | `peer` discovers others directly; `client` connects to a router. |
| `connect` | `[]` | Endpoints to dial. |
| `listen` | `[]` | Endpoints to accept on. |
| `namespace` | `""` | Prefixed to every relative key. Segments separated by `/`, each matching `[A-Za-z0-9_.-]+`; `fleet/robot1` is fine, `fleet/*` is rejected at load. |
| `host` | `""` | The machine a node reports running on. Empty means `socket.gethostname()`. |
| `multicast_scouting` | `true` | Automatic discovery on a LAN. |
| `shared_memory` | `false` | See [Shared memory](shared-memory.md). |
| `timestamping` | `true` | HLC timestamps; required for latched topics. |
| `overrides` | `{}` | Raw zenoh config entries, inserted by path. |

`overrides` is the escape hatch for anything zenode does not model:

```toml
[transport.overrides]
"transport/link/tx/queue/congestion_control/wait_before_drop" = 1000
```

### Choosing a mode

`peer` with multicast is right on a robot and on a lab LAN: nodes find each
other with no infrastructure. Use `client` with an explicit `connect` when
multicast is blocked, which is common on corporate networks, in Docker with
default bridge networking, and across subnets. `zenode doctor` reports whether
scouting is working.

## Shared sections

Facts that belong to the robot rather than to any one node (chassis geometry,
frame ids, calibration) get their own section instead of being copied into two
`[node.*]` tables where they can silently diverge:

```toml
[geometry]
wheel-radius = 0.0485
wheel-separation = 0.235
```

```python
class Geometry(BaseModel):
    wheel_radius: float
    wheel_separation: float


geometry = load_section(Geometry, "geometry")
```

`load_section` follows the same precedence and supports `ZENODE_GEOMETRY__*`.
Two nodes computing odometry from the same wheel radius should read it from one
place. See [Conventions](conventions.md) for what belongs there.

A plain `BaseModel` such as `Geometry` ignores unknown keys. Set
`model_config = ConfigDict(extra="forbid")` on it to have a typo rejected, as
node sections are.

A dotted path reads a subtable: `load_section(Limits, "geometry.limits")` reads
`[geometry.limits]`. Its environment prefix comes from the last segment
(`ZENODE_LIMITS__*`); pass `env_section=` to choose another.

## Logging

Logging is configured by `run()`, from the environment rather than the TOML
file, so it can be changed per process without editing a shared deployment
file:

| Variable | Default | Effect |
|---|---|---|
| `ZENODE_LOG` | `INFO` | Console level. |
| `ZENODE_LOG_FORMAT` | `auto` | `human`, `json`, or `auto` (human on a tty, JSON when redirected). |
| `RUST_LOG` | `warn` | zenoh's own Rust-side logging. `off` to silence. |

See [Observability](open-telemetry.md) for what a node emits and how to collect
it.

## Checking a deployment

```bash
zenode doctor
```

Reports the config file in use, the resolved transport, whether a session
opens, connectivity, whether multicast scouting works, live nodes, shared
memory availability and the memlock limit. Run it first when a node cannot see
another.
