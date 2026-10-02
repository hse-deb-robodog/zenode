# zenode documentation

```{toctree}
:hidden:

contracts
nodes
configuration
testing
cli
conventions
open-telemetry
shared-memory
api/index
```

```{toctree}
:hidden:
:caption: Design notes

design/live-topics
design/node-measures
design/opensovd-adapter
```

zenode is a typed node framework for distributed robot systems on
[Eclipse Zenoh](https://zenoh.io). Independent processes ("nodes") are coupled
only through a typed topic contract. The runtime handles session bootstrap,
thread-to-asyncio dispatch, configuration, presence, health and shutdown.

There is no launch system, no IDL and no coordinator. Processes are started
however you like and find each other over the network.

## Reference

| Document | Covers |
|---|---|
| [Contracts](contracts.md) | `Topic`, `Service`, `TopicSet`, codecs, delivery semantics |
| [Nodes](nodes.md) | Lifecycle, wiring, handlers, timers, silence detection |
| [Configuration](configuration.md) | TOML files, environment overrides, shared sections |
| [Testing](testing.md) | The in-process harness |
| [CLI](cli.md) | `zenode topics`, `echo`, `health`, `logs`, `trace`, … |
| [Conventions](conventions.md) | Units, frames, time, key naming |
| [Observability](open-telemetry.md) | Logging, tracing, metrics, exporting |
| [Shared memory](shared-memory.md) | Publishing large payloads |
| [API reference](api/index.rst) | Generated from the package docstrings |

## Installation

```bash
pip install zenode
```

Requires Python 3.11 or newer. The only runtime dependencies are
`eclipse-zenoh` and `pydantic`. Optional extras:

```bash
pip install 'zenode[otel]'      # OpenTelemetry spans
```

## A first node

Two processes exchanging one typed message. Put the contract somewhere both can
import. It is the only thing they share.

```python
# contract.py
from pydantic import BaseModel
from zenode import Topic, TopicSet


class Reading(BaseModel):
    celsius: float


class Sensors(TopicSet):
    temperature = Topic("sensors/temperature", Reading)
```

```python
# sensor.py
import random
from zenode import Node, every, publish, run
from contract import Reading, Sensors


class Sensor(Node):
    name = "sensor"
    readings = publish(Sensors.temperature)

    @every(1.0)
    async def sample(self) -> None:
        self.readings.put(Reading(celsius=round(random.uniform(18, 24), 2)))


run(Sensor)
```

```python
# monitor.py
from zenode import Node, run, subscribe
from contract import Reading, Sensors


class Monitor(Node):
    name = "monitor"

    @subscribe(Sensors.temperature)
    async def on_reading(self, msg: Reading) -> None:
        self.log.info("%.2f °C", msg.celsius)


run(Monitor)
```

```bash
python sensor.py     # in one terminal
python monitor.py    # in another
```

They discover each other over multicast, so no configuration is needed on a
LAN.

## Runnable examples

[`examples/`](https://github.com/hse-deb-robodog/zenode/tree/main/examples) in the
repository contains complete programs, each runnable as-is:

| Example | Shows |
|---|---|
| `contract.py`, `talker.py`, `listener.py` | Typed pub/sub and a service call between two processes; the recommended starting point for a new contract |
| `otel_pipeline.py` | A traced three-node pipeline exporting spans to an OpenTelemetry backend |
| `shm_camera.py` | Shared-memory publishing, with `--plain` to compare against the normal path |

## What the runtime guarantees

- Handlers never run on zenoh threads. Callbacks copy bytes out and hand them
  to the event loop, so there is one concurrency model.
- A malformed payload or a raising handler never kills a node. Errors are
  logged and counted, and the subscription continues.
- Backpressure is explicit: `mode="queue"` drops the oldest sample when full,
  `mode="latest"` keeps only the newest. Drops are counted.
- `on_stop` runs whenever `on_start` was entered, including when it raised
  part-way, so a node that fails while acquiring hardware still releases what
  it took.

## Where to go next

Read [Contracts](contracts.md) first, since it is the part two processes must
agree on. [Nodes](nodes.md) covers everything a single process does.

## Design notes

Where a design was taken, the note keeps its trade-offs and rejected
alternatives, the reasoning the code no longer states. Where it was not, the
note records a proposal, so the decision can still be taken or dropped on
purpose. The status line at the top of each says which it is.

| Note | Status | Question it answers |
|---|---|---|
| [Live topic discovery](design/live-topics.md) | partly built | How to see what is actually on the bus right now, and how it differs from the contract |
| [Node-defined measurements](design/node-measures.md) | built | How an application puts its own numbers on the heartbeat, and what identifies the machine a node runs on |
| [OpenSOVD adapter](design/opensovd-adapter.md) | proposal | Whether zenode should speak [ISO 17978](https://www.iso.org/standard/86587.html) vehicle diagnostics, and what that would cost |
