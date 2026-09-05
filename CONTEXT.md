# zenode

Minimal pub/sub node runtime for robots, built directly on zenoh. Nodes report themselves
onto the bus; out-of-process tooling observes the fleet from there.

## Language

**Reporter**:
A node's self-reporting module — the single owner of the heartbeat and the descriptor.
_Avoid_: monitor, telemetry, health publisher

**Heartbeat**:
The periodic message carrying a node's liveness, health counters, and measurements. Its
absence is the signal a fleet alerts on.
_Avoid_: ping, status, health message

**Descriptor**:
The latched snapshot of everything a node has declared — what it publishes, subscribes to,
serves, and measures — readable even while the node itself is wedged.
_Avoid_: manifest, node info
