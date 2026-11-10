# ADR-0001: Persisting runtime config writes as per-node overrides

- **Status:** Proposed
- **Date:** 2026-11-10

## Context

A node can expose a setting for runtime change by declaring a
`diagnostic="write"` service. The handler validates the value, applies it, and
replaces `self.config`; the descriptor then shows the new value
(`examples/tunable.py`). The change lives in memory only. After a restart the
node is back to what `zenode.toml` and the environment say.

For session tuning that is correct. For calibration-like settings (a wheel
radius measured on this robot, a speed cap set during commissioning) it means
someone has to copy the value into `zenode.toml` by hand, on the robot.

Constraints this decision has to respect:

- **Scope fence: no parameter server.** zenode never writes a request body
  into `NodeConfig`; the handler owns the write (`docs/contracts.md`).
- **Runtime dependencies are `eclipse-zenoh` and `pydantic`.** The stdlib reads
  TOML (`tomllib`) but cannot write it.
- **Degrade, never crash.** A bad file must not keep a node from starting.
- **Config layering is defaults < TOML < environment**, each node validating
  only its own section, so one node's broken config never affects another.

## Decision

Persistence is **opt-in, explicit, and per node**. zenode provides the storage
and the loading; the node's handler decides what is stored and when.

1. **A per-node overrides file.** `<state_dir>/<namespace>/<node>.json`, holding
   only the fields that were persisted, never a full config dump. One node is
   the only writer of its file, so there is no locking.

2. **A new config layer.** defaults < TOML < **persisted overrides** <
   environment. The environment stays on top so an operator can still pin a
   value.

3. **One call the handler makes.**

   ```python
   @serve(SET_SPEED_LIMIT)
   async def on_speed_limit(self, req: SpeedLimit) -> SpeedLimit:
       ...
       self.config = self.config.model_copy(update={"max_speed": req.max_speed})
       await self.persist({"max_speed": req.max_speed})
       return SpeedLimit(max_speed=self.config.max_speed)
   ```

   `persist()` merges the given fields into the file. A node that never calls
   it behaves exactly as today, and no file is created.

4. **zenode never persists on its own.** No automatic write-through after a
   `"write"` call, and no `persist` flag that zenode interprets in a request
   body. A node that wants try-then-commit declares a second service
   (`drive/save`) whose handler calls `persist()`.

### Rules for `persist()`

- **Validated before it is written.** The fields must exist on the node's
  `NodeConfig`, and the merged result must validate. Otherwise it raises, and
  inside a handler that becomes an error reply with the file untouched.
- **Atomic.** Write a temp file in the same directory, `fsync`, `os.replace`.
  A power cut leaves the old file or the new one, never half of either.
- **Off the event loop.** `fsync` on flash can take tens of milliseconds, so
  the write goes through the same thread path as `Node.blocking`.
- **No write when nothing changed**, to spare flash on a robot that receives
  the same value repeatedly.
- **Secrets are refused.** A `SecretStr` field cannot be persisted: the JSON
  dump masks it, and writing the clear value to a state file is not something
  zenode should do implicitly.
- **Warns when the environment pins the field.** The write succeeds, but the
  log says the value will not take effect after a restart.

### Rules for loading

- **A missing file is not an error**, as with `zenode.toml`.
- **Unknown keys are dropped with a warning.** A field removed by a software
  update must not block start.
- **An overrides file that makes the config invalid is ignored as a whole.**
  The node logs an error and starts from TOML and environment. The file is left
  in place for inspection, and the next successful `persist()` replaces it.
  Dropping only the offending field was considered, but cross-field validators
  make "the offending field" ambiguous.

## Alternatives considered

- **Write a `.env` file.** Rejected. zenode reads the process environment, not
  `.env` files, so this needs a loader first. The environment is also the top
  layer: a runtime write stored there would mask `zenode.toml` permanently and
  take away the operator's way to pin a value.
- **All nodes rewrite `zenode.toml`.** Rejected. Several processes rewriting
  one file race each other. A rewrite loses hand-written comments and layout.
  Writing TOML needs a new dependency or a hand-rolled writer. On a robot the
  file is often deploy-managed or read-only.
- **Automatic persistence after every `"write"` call.** Rejected. zenode does
  not know which config fields a write touched, because the handler replaces
  `self.config` itself. Learning that would mean mapping request bodies onto
  config fields, which is the parameter server the scope fence excludes. It
  would also make every diagnostic write permanent, including a speed cap
  raised for one test.
- **A `persist: bool` field in the request body.** Rejected as a zenode
  convention. It lets the client decide something the node should own. A node
  may still define such a field in its own request type.
- **A generic `node/<name>/config/save` service on every node.** Rejected.
  It would be a zenode-owned write endpoint into config, on by default, which
  is again a parameter server.

## Consequences

- Nodes that opt in keep tuned values across restarts without anyone editing
  `zenode.toml` on the robot.
- There is now a third place a value can come from. Debugging "why is
  `max_speed` 0.8?" needs tooling support: `zenode doctor` should list active
  overrides per node.
- A persisted value is silently outranked by an environment variable for the
  same field. The warning at `persist()` time is the only signal.
- zenode gains a writable state directory, which it has not needed so far.
  Deployments with a read-only root must point it somewhere writable, or
  `persist()` fails (as an error reply, not a crash).
- Removing an override needs its own path (see open questions). Deleting the
  file by hand always works.
- `docs/configuration.md`, `docs/contracts.md` and `examples/tunable.py` need
  updating with the implementation.

## Open questions

1. **Where is `state_dir`?** Proposal: `$ZENODE_STATE_DIR`, else
   `$XDG_STATE_HOME/zenode`, else `~/.local/state/zenode`. The alternative is
   no default at all, so that `persist()` fails until a deployment sets the
   directory explicitly.
2. **Clearing overrides.** Proposal: `persist({"max_speed": None})` is
   ambiguous for fields where `None` is valid, so add a separate
   `self.unpersist("max_speed")` and `self.unpersist()` for all.
3. **Should the descriptor say which fields are overridden?** A
   `config_overrides: list[str]` on `NodeInfo` would let a diagnostic client
   show "persisted" next to a value. It is a wire change to `zenode.msgs` and
   needs the `docs/conventions.md` checklist.
4. **Nested models.** Proposal: `persist()` takes the same nested dict shape
   as the TOML section and deep-merges, matching how environment overrides
   already descend with `__`.
