# Live topic discovery

**Status: partly implemented.** Drafted 2026-08-26. The runtime half of §3 —
`zenode/msgs/info.py`, the latched descriptor, the change counter on
`publisher()`/`subscribe()`/`serve()`, the republish from the health tick — is
built and shipped, because [node measurements](node-measures.md) needed the
catalog it carries. `zenode topics --live` and the contract/bus diff (§4) are
**not** built, and remain a proposal.

Two details differ from §3: the field is `EntityInfo.schema_name`, because
pydantic v2 warns that `schema` shadows a `BaseModel` attribute, and `NodeInfo`
carries `host` as well. The reserved-key filtering in §3 belongs to the CLI side
and is therefore not built either — every node's descriptor currently lists its
own health, log, info and trace entities.

The rest of the page is kept for the trade-offs and the rejected alternatives.

`zenode topics` lists the *contract*: it imports a module, reads
`topic._REGISTRY`, and prints what was declared. That is a static view of a
static thing, and it is correct as far as it goes — but it answers a question
nobody asks twice. What an operator in front of a misbehaving robot wants is
the other one: **what is on the bus right now, and who is on each side of it.**
`ros2 topic list` answers that; zenode has no answer at all.

The failure that makes it worth building is narrow and extremely common: the
contract is perfect, both nodes are up, both report `state="running"`, and
nothing flows — because one side resolved `robodog/state/odom` and the other
`robodog/state/odometry`, or because one process was started without
`ZENODE_TRANSPORT__NAMESPACE` and is publishing into the empty namespace.
Today the only route to that answer is to guess a key and run `zenode hz` on
it. `deadline_misses` says a subscription went silent; nothing says *whether
anyone was ever going to send*.

---

## 1. What zenoh already tells you

Zenoh has an admin space, and it carries more than expected. Measured against
`eclipse-zenoh` 1.9.0, with a real `zenohd` in the loop:

```python
cfg = zenoh.Config()
cfg.insert_json5("adminspace", '{"enabled": true, "permissions": {"read": true, "write": false}}')
session = zenoh.open(cfg)

for reply in session.get("@/*/*/subscriber/**", timeout=3.0):
    print(reply.ok.key_expr, bytes(reply.ok.payload))
```

```
@/b39d…/peer/subscriber/robodog/state/odometry     {"routers":[],"peers":["ae0c…"],"clients":[]}
@/b39d…/peer/queryable/robodog/node/planner/trace  {"routers":[],"peers":["ae0c…"],"clients":[]}
@/b39d…/peer/token/robodog/node/planner            {"routers":[],"peers":["ae0c…"],"clients":[]}
```

Three things follow, and the first two are better than expected:

- **The nodes need no configuration.** `adminspace.enabled` is set on the
  *querying* session only; what comes back is the querier's own routing table,
  which zenoh populates from the network regardless of what the far end
  configured. A CLI-side flag, nothing in `node.py`.
- **The payload names the holder.** Each entry lists the zids holding it, and
  zenode's presence tokens appear in the same space as
  `token/<ns>/node/<name>`. Joining the two gives **key ↔ node name** with no
  protocol of zenode's own.
- **`@/*/*/…` spans peers and routers alike**, so one query covers the local
  routing table and every reachable router's.

### Why it is not enough

| Limit | Measured behaviour |
|---|---|
| **Publishers need a router** | Over a direct peer link, a remote `declare_publisher` never appears in the peer's admin space. Through `zenohd` it does, as `@/<router>/router/publisher/robodog/cmd/vel`. So a publish-only node is invisible in a routerless peer mesh — the topology `examples/` and `harness()` use. |
| **Client mode has no table** | A `mode="client"` session's own admin space returns no entities at all. The query has to become "my own zid, plus every router, dedup", and in a routerless deployment a client-mode CLI sees nothing. |
| **Bare keys only** | No schema, no `latched`, no `max_age`, no QoS. Nothing that distinguishes `zenode topics` from `ros2 topic list`. |
| **zid → node is 1:N** | A zid identifies a *session*. One process running two nodes — `harness()`, or anything using `adopt_session` — collapses them into one row. |

The first two are the disqualifying ones: the mechanism is weakest exactly
where a small robot deployment lives, and it degrades silently rather than
loudly.

---

## 2. The three questions, and which are already answered

| Question | Answered by | Today |
|---|---|---|
| Which nodes are up? | liveliness | `zenode nodes` |
| Is anything flowing on this key? | subscribing to it | `zenode hz`, `zenode echo` |
| **Who publishes and subscribes what?** | nothing | — |

Only the third is missing, and the deliverable is not a list. It is the
**diff between the contract and the bus**, which is a thing `ros2 topic list`
structurally cannot produce because ROS has no contract to diff against:

| Row | Meaning |
|---|---|
| declared **and** live | fine, and most rows are this |
| declared, nobody publishes it | the bug, most of the time |
| live, not in the contract | a typo'd key, a stale binary, or a node in the wrong namespace |

That third row is what catches `state/odom` against `state/odometry`, and it
costs nothing extra once the data is on the bus.

---

## 3. The descriptor

Every node publishes what it declared, once, on a reserved key.

```python
# zenode/msgs/info.py
def info_key(name: str) -> str: ...          # node/<name>/info
def info_pattern(namespace: str) -> str: ...


class EntityInfo(BaseModel):
    key: str
    """Resolved and absolute — what actually went on the wire, not the
    contract-relative form. A namespace mismatch is one of the things this
    exists to make visible, so the namespace must be *in* the value."""
    schema: str = ""
    """The payload model's class name. A label for a human reading a table,
    not a type identity — see below."""
    flags: list[str] = []
    """`latched(1)`, `max_age=0.2`, `shm`, `trace@0.1`, `prio=real_time` —
    the same strings `zenode topics` already renders from a `Topic`."""


class ServiceInfo(BaseModel):
    key: str
    request: str = ""
    reply: str = ""


class NodeInfo(BaseModel):
    node: str
    zenode: str = ""
    """Version. A fleet mid-rollout is a thing that happens."""
    publishes: list[EntityInfo] = []
    subscribes: list[EntityInfo] = []
    serves: list[ServiceInfo] = []
```

### `schema` is a label, not a type

The tempting next step is for `zenode echo` to decode a payload from the
`schema` string and drop `--contract`. It must not: a class name is not a type,
two contracts may both call something `Pose`, and a tool that decodes by name
will eventually decode wrongly and confidently. `--contract` stays the only
route to a typed decode. What the descriptor buys `echo` is the *question* —
"who publishes this, and what do they call it" — not the answer.

### Latched, not a queryable

Both mechanisms exist in the runtime already: the trace service is a queryable
at `<ns>/node/<name>/trace`, and `Topic(latched=True)` is a
`zenoh_ext.AdvancedPublisher` (`node.py:692`). The measurements note
[rejected a queryable for the measure catalog](node-measures.md#3-rejected-alternatives)
because it puts the answer behind the event loop, and the same reasoning
applies here with more force:

- **A wedged node is when you need it most.** `Node.serve` dispatches to the
  node's event loop — that is the thread-boundary invariant, not an accident —
  so a node stuck in a synchronous driver call answers no queries. Its
  descriptor, published at startup and cached by zenoh's own threads, is still
  there. "Who was *supposed* to publish this key" is precisely a
  wedged-node question.
- **No timeout, retry or discovery loop in the consumer.** A sidecar started an
  hour later gets the history query for free.
- **One mechanism for two payloads.** The measure catalog in the measurements
  note is the same shape on the same schedule; it should be a field on
  `NodeInfo`, not a second latched topic.

The cost of latching is that the value is a snapshot, and `publisher()`,
`subscribe()` and `serve()` are public API that a handler may call after start.
The fix is small and bounded: those three call sites bump a counter, and the
health timer — which already runs — republishes when it differs from the last
published value. No new timer, no polling, and a node whose declarations never
change publishes exactly once.

### Where it goes in `start()`

`Node.start()` has a load-bearing order (`node.py:325`). The descriptor publish
belongs **last**, after `_wire_bindings()` and beside the trace service, so the
first snapshot is complete. It is the one thing in that block that may fail
without taking the node down: a descriptor is diagnostics, and the "degrade,
never crash" rule applies — log it and carry on.

### Reserved keys, and hiding them

Every node publishes health and logs and serves trace, so every node's
descriptor lists three entities nobody wants to see. They are recognizable —
they are exactly what `health_key`, `log_key`, `trace_key` and `info_key`
build — so filter them by default and show them under `--all`. Without that,
a ten-node robot prints thirty rows of runtime plumbing before the first
application topic.

---

## 4. The CLI

`zenode topics --live` opens a session, collects descriptors, and renders. With
no `--contract` it is `ros2 topic list -t` with more detail:

```
KEY                          SCHEMA        FLAGS              PUBLISHERS   SUBSCRIBERS
robodog/cmd/vel              Twist         prio=real_time     teleop       nav
robodog/state/odometry       Odometry      max_age=0.2        odometry     nav, logger
robodog/state/odom           Odometry      -                  -            planner      ← nobody publishes
```

With both, the diff is the point, and the contract supplies the rows the bus
cannot:

```
zenode topics --live --contract robodog.contract

KEY                          SCHEMA        DECLARED   PUBLISHERS   SUBSCRIBERS
robodog/cmd/vel              Twist         yes        teleop       nav
robodog/state/odometry       Odometry      yes        odometry     nav
robodog/vision/detections    Detections    yes        -            -            ← declared, nothing running
robodog/state/odom           ?             no         -            planner      ← not in the contract
```

`zenode nodes <name>` becomes the `ros2 node info` equivalent from the same
descriptor, which is the natural place for the per-node view and needs no new
transport.

Exit status stays 0 in every case. A topic with no publisher is a normal state
thirty seconds into a boot, and a listing command that fails intermittently
during startup is a listing command people stop trusting.

---

## 5. Rejected alternatives

**The admin space as the primary mechanism.** Free, and it sees *everything* on
the bus, zenode or not — a Rust node, a `zenoh-plugin-ros2dds` bridge. But
publishers need a router, client mode needs a router, and it carries no schema
or flags. It stays worth having later as a raw `zenode doctor --bus` view,
where "what is actually out there, including things zenode did not write" is
the whole point and the missing publishers matter less.

**The key list on `NodeHealth`.** One fewer topic. But it is static data on a
0.5 Hz heartbeat forever, which is the same objection the measurements note
raises against shipping units on every beat, and the heartbeat's priority band
(`data_low`) was chosen on the assumption that it stays tiny.

**A liveliness token per publisher**, at `<ns>/node/<name>/pub/<key>`. Genuinely
elegant: retracted automatically when the process dies, no payload, no
republish logic, discoverable with the `liveliness().get()` call
`presence.list_nodes` already makes. It loses on two counts — N tokens per node
instead of one message, and a token has nowhere to put the schema or the flags,
which is most of the value. It would be the right answer if the goal were
parity with `ros2 topic list`, and the goal is the diff.

**A queryable, symmetrical with the trace service.** Cheaper (no publisher, no
cache) and always current, so the change-counter above disappears. Rejected for
the wedged-loop case in §3; the counter is five lines and answering when the
loop is stuck is not something that can be added later.

**Importing the node classes in the CLI to read their declarations.** This is
how `zenode echo --contract` learns types and it does not transfer: a contract
module imports pydantic and zenode, a node class imports hardware drivers. The
[SOVD note](opensovd-adapter.md#15-rejected-alternatives) makes the same point
about the same temptation.

---

## 6. Relationship to the other notes

This is the "per-node descriptor on the bus, latched" that the first draft of
[the SOVD adapter note](opensovd-adapter.md#9-architecture) filed under a later
phase, and the "full node descriptor" left open at the end of
[the measurements note](node-measures.md#4-open-questions). Both deferred it
for the same reason — nothing needed it yet.

`zenode topics --live` is what needs it, and it changed the sequencing: the
descriptor is justified by a CLI command that is worth building on its own, so
it landed first and the two notes inherited it. Concretely, the measure
catalog became a `measures: list[MeasureDescriptor]` field on `NodeInfo`
rather than a second latched topic, and the SOVD sidecar discovers services
from the bus and never takes a `--contract` flag. The adapter note's own
changes to the descriptor — `diagnostic`, `description` and the two schemas on
`ServiceInfo`, plus `runtime` and `health_interval` on `NodeInfo` — are listed
in [its §7](opensovd-adapter.md#7-the-runtime-changes), so the descriptor is
revised once.

---

## 7. Open questions

- **`--strict` for CI.** "Every topic in the contract has a live publisher"
  is a real smoke test for a deployment, and it is one flag away. It is also
  a race against startup order, and a check that flaps is worse than no check.
  Deferred until something actually runs it.
- **Whether `--live` is a flag or a command.** `zenode topics --live` keeps one
  noun; `zenode live` keeps the two outputs from growing conditionals. The flag
  is proposed because the diff needs both halves in one table anyway.
- **How long to wait.** Latched history arrives on subscribe, so the CLI needs
  a collection window — the same `--timeout` shape `zenode nodes` already has,
  with the same "you may see fewer nodes than exist" caveat.
- **Descriptors for non-`Node` participants.** A raw zenoh publisher written in
  Rust will never publish one. That is the admin space's job, and the reason
  the fallback above is worth keeping in the drawer.

## 8. Cost

Roughly 60 lines in `msgs/info.py`, 25 in `node.py` (the publish, the change
counter on three call sites, the republish in the health tick), and 90 in
`cli.py` for `--live` and the diff rendering. One new reserved key, through the
checklist at the end of `docs/conventions.md`, and a section in `docs/cli.md`.

Nothing in `pubsub.py` or `service.py` moves, and the runtime side works
without the CLI side — the descriptor is useful to anything on the bus, which
is the test that it belongs in the runtime at all.
