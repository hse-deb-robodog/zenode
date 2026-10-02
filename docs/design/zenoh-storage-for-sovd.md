---
orphan: true
---

# Zenoh storages as the read path for SOVD

**Status: research note. Not a decision, and nothing here is implemented.** It
answers one question raised against the [OpenSOVD adapter](opensovd-adapter.md)
proposal, against primary sources and a handful of throwaway experiments, so
the adapter's §5, §9 and §13 can be revisited on evidence. Written 2026-09-21
against `eclipse-zenoh` 1.9.0, the version `uv.lock` pins.

The question: the zenoh router can store the latest value — or a history — per
key. Could the SOVD sidecar *query* that with `session.get()` instead of keeping
its own last-value cache and subscriptions?

The short version: **a router storage can do it, and it is the wrong tool
here.** It needs a `zenohd` process plus a plugin `.so` that a Python session
cannot load, it is a cache with no expiry that keeps answering after the node
is dead, and — the finding that settles it — **it drops the zenoh attachment**,
which is where zenode keeps the `Envelope`. A stored sample comes back with its
payload, encoding and HLC timestamp, and without its sender, sequence number,
send time and `traceparent`.

The surprise is on the other side. zenode's `latched=True` topics already sit
behind a queryable — the zenoh-ext publisher cache — and **a plain
`session.get()` from a third party retrieves the last sample from it,
attachment intact, in about a millisecond, with no router, no subscription and
no cache in the sidecar.** That removes most of the cost §13 of the adapter
note charged against topic reads. It does not remove the *declared, not
scraped* argument, and it leans on an API zenoh still marks unstable.

Each claim below is tagged **[run]** (executed in this repository's venv),
**[source]** (read from the code or docs that own it) or **[inferred]**.

---

## 1. What the storage-manager plugin is

`zenoh-plugin-storage-manager` gives `zenohd` "the ability to store values
associated with a set of keys, allowing other nodes to query the most recent
values" [Z1]. A *backend* is a library wrapping some storage technology,
a *volume* is a configured instance of a backend, and a *storage* binds a key
expression to a volume [Z1]. Mechanically a storage is a subscriber and
a queryable on the same key expression: it stores what it hears and replies to
`get` with what it holds [S1].

The configuration shape, from `DEFAULT_CONFIG.json5` [S2]:

```text
plugins: {
  storage_manager: {
    volumes: { /* "memory" always exists; others name a backend library */ },
    storages: {
      pose: {
        key_expr: "robot1/state/**",     // what to subscribe to and answer for
        strip_prefix: "robot1/state",    // removed from keys before storing
        volume: "memory",
        complete: true,                  // see below; defaults to false
        // replication: { interval: 10.0, sub_intervals: 5, hot: 6, warm: 30, propagation_delay: 250 },
      },
    },
  },
}
```

`StorageConfig` has exactly these knobs — `key_expr`, `complete`,
`strip_prefix`, the volume, `garbage_collection` and `replication`
[S3]. **There is no TTL or max-age field.** `garbage_collection`
(`period`, `lifespan`) prunes replication metadata and tombstones, not stored
values [S2].

**Latest or history is a property of the backend, not of the storage.** Each
backend reports a `Capability { persistence, history }`, where
`History::Latest` "saves only the latest value per key" and `History::All`
"saves all the values including historical values" [S4]:

| Volume | Persistence | History | Source |
|---|---|---|---|
| `memory` (built in) | volatile | latest | [S5] |
| rocksdb | durable | latest | [S6] |
| filesystem | durable | latest | [S6] |
| s3 | durable | latest | [S6] |
| influxdb v1 / v2 | durable | **all** | [S6] |

So "a history of messages" means InfluxDB, and only InfluxDB. The `memory`
volume is "not persistent through restarts of `zenohd`" [Z1]. With a
`Latest` backend the plugin compares HLC timestamps and skips a sample older
than the one it holds [S1] — with two publishers on one key and skewed
clocks, *latest* means highest timestamp, not last to arrive **[inferred]**.

**Replication** aligns storages declared as replicas by exchanging fingerprints
of time intervals; it "only works for storage that have the `History::Latest`
capability" and requires timestamped samples [S7], [S2]. Not
relevant on a single robot; not tested here.

**How a `get` is answered.** The storage's queryable replies with one sample
per stored entry, built from payload, encoding and timestamp [S1]. A
wildcard selector is expanded against the stored keys. `complete: true` makes
the queryable advertise that it holds every key matching its expression
[S2]; that matters to the query's `target`: `BEST_MATCHING` (the
default) lets zenoh choose, `ALL` reaches every matching queryable,
`ALL_COMPLETE` only the complete ones [P1]. Consolidation (`AUTO`,
`NONE`, `MONOTONIC`, `LATEST`) decides what happens when several queryables
reply for one key — `LATEST` keeps the highest timestamp [P1].

**[run]** A `memory` storage on `exp2/**` in `zenohd` 1.9.0 answered
`get("exp2/ts/pose")` in about 1.5 ms over loopback, and `get("exp2/**")` with
one reply per key.

## 2. Where it can run

**Only in `zenohd`.** The docs say so — "The Zenoh router (`zenohd` executable)
supports the loading of plugins" [Z2] — and the build confirms it:

- In the `zenoh` crate, `plugins` and `runtime_plugins` are features outside
  `default`, and the runtime's plugin manager is compiled only under
  `#[cfg(feature = "plugins")]` [S8]. `zenohd` turns both on
  [S9].
- `zenoh-python` 1.9.0 builds `zenoh` with `default-features = false` plus
  `internal`, `unstable`, `shared-memory` and `zenoh/default`. No `plugins`
  [S10].
- **[run]** The installed `zenoh.abi3.so` contains zero `zenoh_plugin_trait` or
  `PluginsManager` symbols. The config *schema* still has `plugins_loading` and
  `plugins`, so the keys are accepted — and silently ignored. A peer session
  opened with `plugins_loading/enabled: true` and a `storage_manager` entry
  pointing at the real `.so`, marked `__required__: true`, opened without
  error, loaded nothing, stored nothing, and listed no plugin in its
  adminspace.
- **[run]** The wheel ships no plugin library: the package directory is
  `zenoh.abi3.so` plus stubs.

So a deployment that wants a storage needs `zenohd` and
`libzenoh_plugin_storage_manager.so` on the robot. The zenohd README calls the
two bundled plugins "statically linked" [S11], but **[run]** the 1.9.0
release binary loaded the storage manager *from the `.so` next to it*, and
plugins "should be built with the exact same Rust version as `zenohd`" and the
same zenoh commit and features or they are rejected [S11] — the pair
has to be upgraded together.

ARM is not the obstacle. **[run]** The 1.9.0 release carries standalone and
Debian archives for `aarch64-unknown-linux-gnu`, `aarch64-…-musl`,
`armv7-…-gnueabihf`, `arm-…-gnueabi` and `arm-…-gnueabihf`; the aarch64 and
armv7 standalone zips each contain `zenohd` (about 16 MB),
`libzenoh_plugin_rest.so` and `libzenoh_plugin_storage_manager.so` (about
3.5 MB) [R1]. Nothing was executed on ARM.

## 3. What a storage preserves — and what it does not

This is the finding that decides the question.

The backend interface cannot carry an attachment. What goes in is
`put(key, payload, encoding, timestamp)`; what comes out is

```rust
pub struct StoredData {
    pub payload: ZBytes,
    pub encoding: Encoding,
    pub timestamp: Timestamp,
}
```

[S4]. The plugin calls `storage.put(...)` with exactly
`payload()`, `encoding()` and the timestamp, and replies with
`q.reply(key, entry.payload).encoding(entry.encoding).timestamp(entry.timestamp)`
[S1]. The word *attachment* does not occur in the storage service, at
1.9.0 or on `main` as of today, and `StoredData` is unchanged on `main`. The
only upstream mention found is an open feature request from 2026-09-12 that
lists "the original sample attachment (if any)" among metadata a future
`_meta_only` query could return [I1].

**[run]** Put with `attachment=b"envelope-A"` and
`encoding=application/json`, then `get` from a third session:

```
key=exp2/ts/pose payload=b'{"x":1}' enc=application/json att=None ts=7688011671174942096/8fdd87f0…
```

Encoding and timestamp survive; the attachment is `None`.

For zenode that means the whole `Envelope` — `node`, `seq`, `ts_ns`,
`traceparent` (`src/zenode/envelope.py:17-23`) — is gone. `decode_envelope(None)`
yields an empty `Envelope`, `age_s()` returns `None`, and the staleness check
returns early on `age is None` (`src/zenode/pubsub.py:461`). **A sample read
from a storage is one `Topic.max_age` cannot judge.**

What does survive is the **zenoh HLC timestamp**, which is a second, coarser
answer to "how old":

- Storing requires a timestamp. The router stamps un-timestamped data by
  default — `timestamping.enabled: { router: true, peer: false, client: false }`
  [S2] — and the storage itself falls back to
  `sample.timestamp().unwrap_or(session.new_timestamp())` [S1].
- **[run]** A peer with timestamping *off* still had its sample stored, and the
  stored timestamp's HLC id was the **router's** zenoh id, where the sample from
  a timestamping peer carried the peer's own: stamped downstream, at the
  router's receive time, by the router's clock.
- zenode turns timestamping **on** for every session
  (`src/zenode/config.py:77,93`) because advanced publishers refuse to start
  without it [S12], so for zenode publishers the stored timestamp is the
  publisher's own HLC reading — wall-clock based, the same clock domain as
  `Envelope.ts_ns`, and the same NTP requirement as the "two clocks" rule in
  `CLAUDE.md`. `Timestamp.get_time()` returns a `datetime` in Python
  [P1].

So age is recoverable; provenance, ordering and trace context are not.

## 4. zenoh-ext advanced pub/sub

`AdvancedPublisher` / `AdvancedSubscriber` landed in Rust in zenoh 1.1.0
[R2] and in zenoh-python in **1.5.0** [R3] — it is absent from
`zenoh/ext.pyi` at the 1.4.0 tag and present at 1.5.0 **[run]**. In the
installed 1.9.0, `zenoh.ext` exposes `declare_advanced_publisher`,
`declare_advanced_subscriber`, `CacheConfig`, `HistoryConfig`,
`RecoveryConfig`, `MissDetectionConfig`, `RepliesConfig`, `Miss` and
`SampleMissListener` **[run]**. **Every one of them is decorated `@_unstable`**
[P2], mirroring `#[zenoh_macros::unstable]` on the Rust side
[S13]. The older `PublicationCache` and `QueryingSubscriber` are
`#[deprecated = "Use AdvancedPublisher and AdvancedSubscriber instead."]`
[S14] and are not exposed in zenoh-python at all **[run]**.

**How the cache answers.** `AdvancedCache` is a `VecDeque<Sample>` bounded by
`max_samples`, plus a queryable [S13]. The queryable is not on the
topic key but under a suffix the publisher builds:

```
<topic key>/@adv/pub/<zenoh id>/<entity id | "uhlc">/<meta | "_">
```

[S12]. It understands `_max`, `_sn` and `_time` selector parameters and
replies with `reply_sample(SampleBuilder::from(sample.clone())…)` — the whole
cached `Sample`, attachment included [S13]. An `AdvancedSubscriber`
with history does nothing more exotic than

```rust
session.get(key_expr / "@adv" / "**", params: _max=N)
    .consolidation(ConsolidationMode::None)
    .accept_replies(ReplyKeyExpr::Any)
    .target(QueryTarget::All)
```

[S15]. `accept_replies(Any)` is needed because the reply carries the
*topic* key, which does not match the `@adv` selector; zenoh-python exposes it
as `accept_replies=zenoh.ReplyKeyExpr.ANY` or the `_anyke` selector parameter
[P1].

**So a plain `session.get()` works.** **[run]**, two peer sessions over
loopback TCP, publisher with `CacheConfig(1)`, two puts, then a late third
party:

| Query | Result |
|---|---|
| `get(KEY)` | 0 replies — nothing is declared on the topic key itself |
| `get(KEY/@adv/**)` | 0 replies — reply key does not match, dropped |
| `get(KEY/@adv/**?_anyke;_max=1)` | 1 reply: last payload, encoding, **attachment intact**, HLC timestamp; 0.4–0.9 ms |
| `get("exp/**?_anyke")` | 0 replies — `**` does not match the verbatim `@adv` chunk, so a namespace-wide scrape of caches is not possible by accident |
| `AdvancedSubscriber(history=HistoryConfig(detect_late_publishers=True, max_samples=1))` | same sample, attachment intact; about 8 ms to first callback |
| same `get` after `publisher.undeclare()` | 0 replies in 0.2 ms — no queryable, no wait for a timeout |

The same held through a router with a client-mode querier (about 1 ms), and
against a real zenode latched topic published through `harness()`: the reply
decoded to `Envelope(node='zenode-test-probe', seq=2, ts_ns=…)` with a correct
`age_s()`.

## 5. How zenode does `latched` today

Exactly this mechanism, and nothing else.

- `Topic.latched` / `Topic.history` are the contract fields
  (`src/zenode/topic.py:78-79,87,125-127`).
- `Node.publisher()` declares
  `zext.declare_advanced_publisher(session, key, cache=zext.CacheConfig(topic.history), publisher_detection=True, **qos)`
  for a latched topic and a plain publisher otherwise
  (`src/zenode/node.py:733-742`).
- `Node.subscribe()` declares
  `zext.declare_advanced_subscriber(..., history=zext.HistoryConfig(detect_late_publishers=True, max_samples=topic.history))`
  (`src/zenode/node.py:799-805`).
- The CLI has the same thing as `_declare_latched_subscriber`
  (`src/zenode/cli.py:85-101`), used by `zenode echo` when the contract says the
  topic is latched (`cli.py:171-172`) and for the node descriptor
  (`cli.py:365-367`, `cli.py:476-478`). Its docstring already names the
  consumer: "a CLI or sidecar started after the nodes".
- The runtime's own latched topic is the `NodeInfo` descriptor
  (`src/zenode/node.py:390-394`).
- `testing.py` has no latched-specific code; the probe node goes through
  `Node.subscribe()`, in peer mode with multicast off and no router
  (`src/zenode/testing.py:40-42`).

There is no zenode-declared queryable for topics and no storage anywhere.
Consequence: **"read the latest value of a latched topic" is possible today
with no router storage** — either the supported way, by declaring an
`AdvancedSubscriber` briefly, or as a one-shot `get` on `<key>/@adv/**`.

## 6. One-shot latest reads inside zenoh, compared

| Mechanism | Works for | Cost | Notes |
|---|---|---|---|
| `get` on the publisher cache (`<key>/@adv/**?_anyke;_max=1`) | latched topics | one query, ~1 ms **[run]**; synchronous, fits a `ThreadingHTTPServer` handler | unstable API; the `@adv` layout is an implementation detail with no RFC in `eclipse-zenoh/roadmap` **[run: searched]**. Use `target=ALL` — two publishers on one key each answer |
| `AdvancedSubscriber(history=…)`, declared then undeclared | latched topics | ~8 ms to first sample **[run]**: declaration, liveliness query, then the same `get` | the supported spelling of the row above; callback-shaped, so the handler needs a wait-with-timeout |
| `get` on a queryable the node declares | anything the node chooses | one query; runs a Python handler on the node's loop | this *is* a zenode `Service` — the adapter note's current answer |
| `get` on a router storage | any key the storage covers | ~1.5 ms **[run]**; needs `zenohd` + plugin | no attachment (§3) |
| liveliness `get` | presence only | one query | tokens carry no payload; an advanced publisher with `publisher_detection=True` does declare one at `<key>/@adv/pub/…` **[run]**, which tells a sidecar *that* a latched publisher exists without reading from it |

All latencies are single runs on loopback on a development machine: good for
orders of magnitude, nothing more.

One behavior worth knowing, because it bears on the adapter's §11. **[run]** A
publisher process that published once and then spun in a Python busy loop for
six seconds kept answering cache queries in about a millisecond — the
queryable callback is a Rust closure on zenoh's threads [S13] and never
enters Python. After `SIGKILL` the same query returned zero replies
immediately. So the publisher cache answers for a node whose event loop is
wedged and goes silent when the process is gone.

## 7. Against the adapter's own arguments

Four options, kept apart:

- **(a)** router storage, queried by the sidecar;
- **(b)** the publisher-side advanced cache, on topics the contract marks;
- **(c)** the adapter note's current answer — `read_only` services;
- **(d)** a subscription cache inside the sidecar, rejected in §13.

**[§5, declared, not scraped](opensovd-adapter.md#5-the-principle-declared-not-scraped).**
A storage on `<ns>/**` is scraping moved into a JSON5 file: the 4K camera frame
is now held in the router's RAM as well. A storage on an explicit key list is a
declaration — but a *deployment-side* one, in a router config that lives
outside the contract package, is not imported by the sidecar, and is not
reviewed with the node. It can also drift from the contract silently. (b) is
the only topic-read option whose declaration can live on the `Topic`, though
`latched` today means "late joiners get the last value", not "diagnosable"; §5
would want its own flag rather than a second meaning for an existing one. (d)
can be made contract-driven too, but has nothing forcing it to be.

**[§9, no cache, no staleness policy](opensovd-adapter.md#9-architecture).** A
storage *is* a cache, with no expiry (§1) and volatile or durable depending on
the volume — a rocksdb storage will serve last week's pose after a reboot. The
staleness information that survives is the HLC timestamp only (§3). (b) is also
a cache, but it sits in the publisher, is bounded by `Topic.history`, carries
the `Envelope`, and is destroyed with the process. §9's actual sentence —
"there is no third state where the adapter serves something old without saying
so" — can be kept under (b) if the adapter always reports the envelope time and
applies `Topic.max_age` where the contract sets one. Under (a) it can be kept
only by switching to the HLC timestamp, which is a second age mechanism next to
the one the runtime uses everywhere else.

**[§11, a wedged node answers nothing](opensovd-adapter.md#11-risks).** A
storage answers when the node is wedged, when it has crashed, and after it has
been uninstalled, identically, until `zenohd` restarts. For a diagnostic client
a last-known value is legitimate *if labelled*; what is not is an answer that
cannot distinguish "the node said this 50 ms ago" from "a node said this on
Tuesday" without the caller doing timestamp arithmetic. (b) behaves better by
accident of where it lives: alive-but-wedged still answers with an honest
timestamp, dead answers nothing (§6). That fills the gap §11 describes between
the passive heartbeat and services, without pretending.

**Project constraints.** (a) adds no Python dependency, so the letter of
"eclipse-zenoh and pydantic, full stop" holds — but it adds a required native
binary and a version-locked plugin to every robot that wants SOVD topic reads,
which is the same cost in a different column. It also cannot be tested in
`harness()`, which is peer mode with no router by design; tests would need a
`zenohd` on the CI machine. (b) and (c) run in the harness as it is. "Zenoh is
the transport, not an abstraction to hedge against" cuts in favour of using
zenoh's own cache rather than rebuilding one in the sidecar, and says nothing
for or against the router.

**Two clocks.** `max_age` is a wall-clock comparison against `Envelope.ts_ns`.
(a) removes `ts_ns`. The HLC timestamp is wall-clock derived as well, so
nothing new is required of NTP, but it would be a third time source in a
codebase that is careful to have two.

| | (a) router storage | (b) publisher cache | (c) `read_only` service | (d) sidecar subscription |
|---|---|---|---|---|
| Extra deployment | `zenohd` + plugin `.so`, version-locked | none | none | none |
| Works in `harness()` | no | yes **[run]** | yes | yes |
| Where exposure is declared | router config | the `Topic` | the `Service` | sidecar config or contract |
| Envelope (node, seq, ts, trace) | **lost** **[run]** | kept **[run]** | reply is fresh; n/a | kept |
| Age information | HLC timestamp only | `Envelope.ts_ns` + HLC | none needed | `Envelope.ts_ns` + HLC |
| Node loop wedged | answers, last value | answers, last value **[run]** | times out | answers, last value |
| Node process dead | **answers, forever** **[run]** | no reply, immediately **[run]** | times out | answers until the sidecar's policy says stop |
| Non-latched, high-rate topics | yes | no | via a field + service | yes |
| Standing traffic | every sample to the router | none | none | every sample to the sidecar |
| Sidecar state | none | none | none | cache + staleness policy |
| API stability | stable plugin config | zenoh-ext **unstable** | stable | stable |

## 8. Recommendation

Do not build the SOVD read path on router storages. The attachment loss alone
disqualifies it for a framework whose delivery metadata lives in the
attachment, and the rest — a mandatory router, a cache with no expiry, exposure
declared in a file the contract cannot see — runs against §5, §9 and §11
together. Nothing stops a deployment from running a storage for its own reasons
(persisting a map, feeding InfluxDB); zenode does not need to know, and should
document only that samples read back from one arrive with an empty `Envelope`.

Keep (c), `read_only` services, as the adapter's primary read mechanism. It is
still the only option where a GET proves the node is answering *now*.

Revise §13's rejection rather than its conclusion. Its cost list — "a
last-value cache in the sidecar, a staleness policy, a subscription per exposed
topic, and either `latched=True` everywhere or a new `Node.latest()` one-shot
history query" — is mostly obsolete: for latched topics the one-shot query
exists, needs no sidecar cache and no subscription, and brings its own
timestamp. What remains of the objection is the part that was always the
strongest: exposure must be declared in the contract. If topic reads are ever
wanted, the shape that survives this note is **(b): latched topics that the
contract explicitly marks diagnosable, read with a one-shot query, reported
with their envelope age** — as a fourth source in §5's list, between the
heartbeat and services. Prefer the `AdvancedSubscriber` spelling that
`cli.py` already has unless the 8 ms matters; the raw `@adv` selector is
faster and synchronous but couples zenode to an undocumented key layout.

Neither (b) nor anything else here is needed for the adapter's Phase 1.

## 9. Open questions and things not verified

- **Newer zenoh.** Everything was run on 1.9.0. 1.10.0 and 1.10.1 exist
  (2026-08-14, 2026-09-07) [R1]; only `StoredData` and the absence of
  "attachment" in the storage service were re-checked on `main`. The `@adv` key
  layout and `_anyke` behavior were not re-checked there.
- **Stability of the `@adv` layout.** No RFC was found in
  `eclipse-zenoh/roadmap`. It is an implementation detail of an unstable API
  and could change in a minor release. The `AdvancedSubscriber` path is
  insulated from that; the raw `get` is not.
- **Topology.** Verified: peer-to-peer over loopback TCP, client-to-client
  through one `zenohd`, and single-session in `harness()`. Not verified:
  multiple hosts, multicast-scouted peers, zenoh `namespace` configuration,
  SHM-backed samples in the cache, access-control rules on `@adv` keys.
- **Storage versus services on one prefix.** A storage on `<ns>/**` also
  matches every service key. With `complete` both `false` and `true`, 20 of 20
  default-target (`BEST_MATCHING`) calls still reached the service queryable in
  peer and in client mode **[run]**. That is two topologies, not a proof;
  `BEST_MATCHING`'s selection rule was not read in source.
- **Does SOVD carry a timestamp on a data read?** Whether `ReadResponse` in
  `opensovd-models` has a field for the value's age was not checked. §7's
  argument that (b) can stay honest depends on having somewhere to put it.
- **InfluxDB history queries** (`_time` selectors against a `History::All`
  backend) and **replication** were read about, not run.
- **ARM.** Release assets exist and contain the plugin; nothing was executed
  on an ARM target.
- **readthedocs.** The zenoh-python API statements cite the `.pyi` stubs
  shipped in the 1.9.0 wheel and the same files at the 1.9.0 tag, not the
  rendered readthedocs pages.
- **Latencies** are single runs on loopback.

## 10. References

Zenoh documentation:

- **[Z1]** [Storage manager plugin](https://zenoh.io/docs/manual/plugin-storage-manager/)
  — backends, volumes, storages, the `memory` volume.
- **[Z2]** [Zenoh plugins](https://zenoh.io/docs/manual/plugins/)
  — "The Zenoh router (`zenohd` executable) supports the loading of plugins".

`eclipse-zenoh/zenoh` at tag `1.9.0`:

- **[S1]** [`plugins/zenoh-plugin-storage-manager/src/storages_mgt/service.rs`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/plugins/zenoh-plugin-storage-manager/src/storages_mgt/service.rs)
  — subscriber + queryable with `.complete(...)` L142-L154; timestamp fallback
  L182; `History::Latest` guard L318-L327; `storage.put(key, payload, encoding, timestamp)`
  L332-L341; replies L570-L577 and L603-L610.
- **[S2]** [`DEFAULT_CONFIG.json5`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/DEFAULT_CONFIG.json5)
  — `timestamping` L205-L213; `plugins_loading` L791-L801; `storage_manager`
  example incl. `strip_prefix`, `garbage_collection`, `replication`, `complete`
  L832-L937.
- **[S3]** [`plugins/zenoh-backend-traits/src/config.rs`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/plugins/zenoh-backend-traits/src/config.rs#L63-L82)
  — `StorageConfig`, `ReplicaConfig`.
- **[S4]** [`plugins/zenoh-backend-traits/src/lib.rs`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/plugins/zenoh-backend-traits/src/lib.rs#L159-L196)
  — `Capability`, `Persistence`, `History` L159-L182; `StoredData` L192-L196;
  `Storage::put` / `get` L233-L259. `StoredData` is identical on `main`
  (checked 2026-09-21).
- **[S5]** [`plugins/zenoh-plugin-storage-manager/src/memory_backend/mod.rs`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/plugins/zenoh-plugin-storage-manager/src/memory_backend/mod.rs#L58-L63)
- **[S6]** `get_capability()` on `main` as of 2026-09-21 in
  [zenoh-backend-rocksdb `src/lib.rs`](https://github.com/eclipse-zenoh/zenoh-backend-rocksdb/blob/main/src/lib.rs),
  [zenoh-backend-filesystem `src/lib.rs`](https://github.com/eclipse-zenoh/zenoh-backend-filesystem/blob/main/src/lib.rs),
  [zenoh-backend-s3 `src/lib.rs`](https://github.com/eclipse-zenoh/zenoh-backend-s3/blob/main/src/lib.rs),
  [zenoh-backend-influxdb `v1/src/lib.rs` and `v2/src/lib.rs`](https://github.com/eclipse-zenoh/zenoh-backend-influxdb/tree/main).
- **[S7]** [`plugins/zenoh-plugin-storage-manager/src/replication/mod.rs`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/plugins/zenoh-plugin-storage-manager/src/replication/mod.rs#L15-L25)
- **[S8]** [`zenoh/Cargo.toml`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/zenoh/Cargo.toml#L34-L54)
  features, and [`zenoh/src/net/runtime/mod.rs`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/zenoh/src/net/runtime/mod.rs#L672-L718)
  `#[cfg(feature = "plugins")]` around `load_plugins` / `start_plugins`.
- **[S9]** [`zenohd/Cargo.toml`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/zenohd/Cargo.toml#L39-L42)
- **[S11]** [`zenohd/README.md`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/zenohd/README.md#plugins)
  — the Rust-ABI compatibility note and the bundled plugins, L90-L97.
- **[S12]** [`zenoh-ext/src/advanced_publisher.rs`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/zenoh-ext/src/advanced_publisher.rs#L351-L368)
  — the `@adv/pub/<zid>/…` suffix L351-L361; the timestamping requirement L368;
  `cache_sample` of the full sample L749, L761.
- **[S13]** [`zenoh-ext/src/advanced_cache.rs`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/zenoh-ext/src/advanced_cache.rs#L219-L383)
  — `VecDeque<Sample>` L219-L224; queryable, `_sn`, `_max` L247-L261;
  `reply_sample` L286, L324; `cache_sample` L374-L383.
- **[S14]** [`zenoh-ext/src/publication_cache.rs` L34](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/zenoh-ext/src/publication_cache.rs#L34),
  [`querying_subscriber.rs` L36](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/zenoh-ext/src/querying_subscriber.rs#L36)
- **[S15]** [`zenoh-ext/src/advanced_subscriber.rs`](https://github.com/eclipse-zenoh/zenoh/blob/1.9.0/zenoh-ext/src/advanced_subscriber.rs#L860-L899)
  — the initial history query.

`eclipse-zenoh/zenoh-python` at tag `1.9.0`:

- **[S10]** [`Cargo.toml`](https://github.com/eclipse-zenoh/zenoh-python/blob/1.9.0/Cargo.toml#L36-L53)
  — features and the `zenoh` dependency.
- **[P1]** [`zenoh/__init__.pyi`](https://github.com/eclipse-zenoh/zenoh-python/blob/1.9.0/zenoh/__init__.pyi)
  — `ConsolidationMode`, `QueryTarget`, `ReplyKeyExpr` and `_anyke`,
  `Session.get(accept_replies=…)`, `Timestamp.get_time()`. Read from the
  installed wheel under `.venv/lib/python3.12/site-packages/zenoh/`.
- **[P2]** [`zenoh/ext.pyi`](https://github.com/eclipse-zenoh/zenoh-python/blob/1.9.0/zenoh/ext.pyi)
  — `_unstable` marker L43-L44, applied from L117 on.

Releases and issues:

- **[R1]** [zenoh 1.9.0 release assets](https://github.com/eclipse-zenoh/zenoh/releases/tag/1.9.0);
  [all releases](https://github.com/eclipse-zenoh/zenoh/releases).
- **[R2]** [zenoh 1.1.0](https://github.com/eclipse-zenoh/zenoh/releases/tag/1.1.0)
  — "Add Advanced Pub/Sub feature", [zenoh#1582](https://github.com/eclipse-zenoh/zenoh/pull/1582).
- **[R3]** [zenoh-python 1.5.0](https://github.com/eclipse-zenoh/zenoh-python/releases/tag/1.5.0)
  — "implement advanced pub/sub", [zenoh-python#537](https://github.com/eclipse-zenoh/zenoh-python/pull/537).
- **[I1]** [zenoh#2783](https://github.com/eclipse-zenoh/zenoh/issues/2783)
  — open feature request for metadata-only storage queries.

Experiments: seven throwaway scripts run with `uv run python` against the
pinned `eclipse-zenoh` 1.9.0 wheel and the `zenohd` 1.9.0
`x86_64-unknown-linux-gnu` standalone release. They were kept out of the
repository on purpose; each is a dozen lines and §3, §4 and §6 state what it
did.
