# The rover

A simulated differential-drive robot in a square room: seven nodes, one
contract, one `zenode.toml`. There is no hardware, and every node does a job
the others depend on, so you can break one and watch the rest react.

```
teleop ──cmd_vel──▶ drive ──odom──▶ rangers ──sensors/*/range──▶ safety
                      ▲                                             │
                      └──────────────────── estop (latched) ────────┘

camera ──camera/frame (shared memory)──▶ detector ──camera/detections──▶

mission ── calls drive/speed_limit and drive/reset_odom, then exits
```

| File | Node | What it shows |
|---|---|---|
| `contract.py` | | Topics, services, delivery rules and QoS in one importable module |
| `zenode.toml`, `geometry.py` | | Per-node sections and a shared `[geometry]` section |
| `teleop.py` | `teleop` | The smallest node: config, `publish()`, `@every` |
| `drive.py` | `drive` | `max_age`, `deadline`, `@on_silence`/`@on_resume`, a config-rate control loop, `self.blocking`, `@metric`, `describe_config`, `"write"` and `"operation"` services |
| `rangers.py` | `rangers` | Imperative wiring from config: one publisher per sensor key |
| `safety.py` | `safety` | A wildcard subscription, `Envelope.key`, a latched topic, `allow_duplicates = False` |
| `camera.py` | `camera` | Shared memory, raw `bytes`, `@on_matching` |
| `detector.py` | `detector` | Queue backpressure and counted drops |
| `mission.py` | `mission` | Presence, service calls and their errors, a node with constructor arguments |
| `telemetry.py` | | Optional OpenTelemetry export |
| `test_rover.py` | | Testing handlers directly and message flow through `harness()` |

## Running it

From the repository root, one terminal per node:

```bash
uv run python -m examples.rover safety
uv run python -m examples.rover drive
uv run python -m examples.rover rangers
uv run python -m examples.rover teleop
uv run python -m examples.rover camera
uv run python -m examples.rover detector
```

Any order works. The nodes find each other by multicast scouting, and each one
tolerates the others being absent.

In one more terminal, point the CLI at the rover's config. It supplies the
`rover` namespace, so the commands below use the same relative keys the
contract does:

```bash
export ZENODE_CONFIG=examples/rover/zenode.toml     # fish: set -x ZENODE_CONFIG …
uv run zenode doctor --contract examples.rover.contract
```

If `doctor` reports that multicast scouting is not working (firewalls often
drop UDP 7446), give the nodes explicit endpoints instead. One node listens
and the others connect to it:

```bash
ZENODE_TRANSPORT__LISTEN='["tcp/127.0.0.1:17450"]'  uv run python -m examples.rover safety
ZENODE_TRANSPORT__CONNECT='["tcp/127.0.0.1:17450"]' uv run python -m examples.rover drive
```

## What happens

`drive` does not move until `safety` allows it, and `safety` raises the stop
until it hears a range sensor. Once all four core nodes are up the rover
follows a left-hand arc at its speed cap, the right-hand sensor closes in on a
wall after a few seconds, and `safety` stops it:

```
safety  WARNING  estop raised: no range sensor is reporting
safety  WARNING  estop released
safety  WARNING  estop raised: obstacle at right
```

It stays stopped until something moves it. That is `mission`:

```bash
uv run python -m examples.rover mission 0.3
```

```
drive accepted max_speed = 0.3 m/s
odometry reset to x=0.0 y=0.0 theta=0.0
```

The rover is back in the middle of the room, `safety` releases the stop, and
it sets off again, slower.

## Things to try

**Look at the contract and the data.**

```bash
uv run zenode topics --contract examples.rover.contract
uv run zenode echo odom --contract examples.rover.contract --meta
uv run zenode echo 'sensors/*/range' --contract examples.rover.contract
uv run zenode hz 'sensors/**'
```

Quote a wildcard key, or the shell expands it as a file glob. `hz` reports one
rate per sensor, not a blend.

**Stop `teleop`.** Within half a second `drive` logs
`no velocity command for 0.5 s; stopping`, and `zenode health` counts it under
`MISS`. Start `teleop` again and `drive` logs the resume.

**Stop `rangers`.** `safety` raises the stop with
`no range sensor is reporting`. No data is not the same as no obstacle.

**Restart `drive` while the stop is raised.** It logs the stop as soon as it
is up, because `estop` is latched. It also restarts at the origin, so `safety`
then releases it.

**Start a second `safety`.** It refuses to start (`DuplicateNodeError`), since
two nodes publishing `estop` could contradict each other.

**Watch the detector fall behind.**

```bash
uv run zenode health --watch
```

`detector` shows `DROP` climbing and `QMAX` at its queue size of 4, because it
needs 50 ms per frame and gets one every 33 ms. Stop it, and `camera` logs
`idle: nobody is subscribed` and stops grabbing. Any subscriber counts, so a
`zenode echo camera/frame` in another terminal starts the camera too.

**Ask for too much.**

```bash
uv run python -m examples.rover mission 9
```

```
drive refused: rover/drive/speed_limit: max_speed 9.0 m/s exceeds the chassis limit of 1.5 m/s
```

The handler raised, the caller got the message, and `drive` counts one under
`ERR`. Its config is unchanged.

**Read the logs from the bus.** Warnings and above are published, so this
works from any machine on the network:

```bash
uv run zenode logs
uv run zenode logs --node safety
```

**Follow one pose through three nodes.** `odom` is a trace root, so one pose
in five starts a trace. The log lines of every node that handled it carry the
same `trace` id. Take one from a `safety` log line:

```bash
uv run zenode trace 869031049706dd5e36b72fa5c824a929
```

```
TRACE 869031049706dd5e36b72fa5c824a929
  rangers  rover/odom                 seq 1  ← drive    age 0.4ms  handler 0.2ms
  safety   rover/sensors/front/range  seq 1  ← rangers  age 0.5ms  handler 0.2ms
  safety   rover/sensors/left/range   seq 1  ← rangers  age 0.5ms  handler 0.0ms
  safety   rover/sensors/right/range  seq 1  ← rangers  age 0.5ms  handler 0.0ms
  drive    rover/estop                seq 2  ← safety   age 0.2ms  handler 0.1ms
```

No collector is involved; the CLI asks each live node for its ring buffer.

**Export metrics.** `drive`'s `speed` and `camera`'s `frames_grabbed` ride on
their heartbeats, so a sidecar can serve them without importing either node:

```bash
uv run zenode export --prometheus :9100
curl -s localhost:9100/metrics | grep zenode_app
```

**Change a setting without a restart.** `drive` sets `describe_config`, so its
descriptor (`rover/node/drive/info`) carries its config and the schema of the
`"write"` service. That is what `zenode-sovd` serves to a diagnostic client;
see `docs/sovd.md`. The change is lost on restart.

**Override config from the environment.**

```bash
ZENODE_DRIVE__MAX_SPEED=1.0 uv run python -m examples.rover drive
ZENODE_GEOMETRY__BODY_RADIUS=0.5 uv run python -m examples.rover rangers
```

## Tests

```bash
uv run pytest examples/rover -q
```

## OpenTelemetry

Set `OTLP_ENDPOINT` and the nodes export their spans; see `telemetry.py`.
