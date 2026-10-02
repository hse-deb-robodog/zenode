"""Who is on the bus, and whether they are answering.

Three sources, three answers. The liveliness token says a node *exists*; the
heartbeat's age against its own ``health_interval`` says whether its loop is
running; a dropped token says it is *gone*, and gone nodes stay listed for a
bounded window because the last heartbeat and the last thousand log lines of
a process that crashed a minute ago are the one thing a technician came for.

Everything here is written from zenoh threads and read from HTTP threads,
under one lock, and every container has a cap: a heartbeat and a descriptor
per node, a ``deque(maxlen=…)`` of logs per node, and ``max_gone`` records
that are not currently live. A sidecar runs as long as the robot does.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from ..msgs import LogRecordMsg, NodeHealth, NodeInfo

Presence = Literal["live", "silent", "gone"]

SILENT_AFTER_BEATS = 3
"""Missed heartbeats before a live node is reported silent — the same rule a
subscription ``deadline`` uses for a producer."""


@dataclass
class _Record:
    name: str
    alive: bool = False
    """Admitted by a liveliness token. A record created by a descriptor or
    heartbeat that arrived first is kept but not listed until the token comes:
    latched delivery races the liveliness replay at startup, and dropping the
    descriptor would lose it for good — it is published once."""
    left_at: float | None = None
    info: NodeInfo | None = None
    health: NodeHealth | None = None
    health_at: float | None = None
    logs: deque[LogRecordMsg] = field(default_factory=deque)


@dataclass(frozen=True)
class NodeView:
    """A snapshot of one node for a single HTTP request."""

    name: str
    presence: Presence
    info: NodeInfo | None
    health: NodeHealth | None
    last_seen_s: float | None
    """Seconds since the last heartbeat, or ``None`` before the first."""
    logs: list[LogRecordMsg]


class Topology:
    def __init__(
        self,
        namespace: str,
        *,
        retain_gone: float = 600.0,
        max_gone: int = 64,
        log_buffer: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.namespace = namespace
        self.retain_gone = retain_gone
        self.max_gone = max_gone
        self.log_buffer = log_buffer
        self._clock = clock
        self._records: dict[str, _Record] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ writers

    def presence(self, name: str, alive: bool) -> None:
        """A liveliness token appeared or vanished."""
        with self._lock:
            record = self._record(name)
            record.alive = alive
            record.left_at = None if alive else self._clock()
            self._prune()

    def offer_info(self, payload: bytes) -> None:
        try:
            info = NodeInfo.model_validate_json(payload)
        except ValidationError:
            return  # a key expression is not a promise about what is on it
        with self._lock:
            self._record(info.node).info = info
            self._prune()

    def offer_health(self, payload: bytes) -> None:
        try:
            health = NodeHealth.model_validate_json(payload)
        except ValidationError:
            return
        with self._lock:
            record = self._record(health.node)
            record.health = health
            record.health_at = self._clock()
            self._prune()

    def offer_log(self, payload: bytes) -> None:
        try:
            log = LogRecordMsg.model_validate_json(payload)
        except ValidationError:
            return
        with self._lock:
            self._record(log.node).logs.append(log)
            self._prune()

    # ------------------------------------------------------------ readers

    def names(self) -> list[str]:
        """Listed nodes: live, or gone within the retention window."""
        with self._lock:
            self._prune()
            return sorted(n for n, r in self._records.items() if self._listed(r))

    def get(self, name: str) -> NodeView | None:
        with self._lock:
            self._prune()
            record = self._records.get(name)
            if record is None or not self._listed(record):
                return None
            now = self._clock()
            last_seen = None if record.health_at is None else now - record.health_at
            return NodeView(
                name=name,
                presence=self._classify(record, last_seen),
                info=record.info,
                health=record.health,
                last_seen_s=last_seen,
                logs=list(record.logs),
            )

    # ------------------------------------------------------------ internals

    def _record(self, name: str) -> _Record:
        record = self._records.get(name)
        if record is None:
            record = _Record(name, logs=deque(maxlen=self.log_buffer))
            self._records[name] = record
        return record

    @staticmethod
    def _listed(record: _Record) -> bool:
        return record.alive or record.left_at is not None

    @staticmethod
    def _classify(record: _Record, last_seen: float | None) -> Presence:
        if not record.alive:
            return "gone"
        interval = record.info.health_interval if record.info is not None else None
        if interval is None or last_seen is None:
            return "live"
        return "silent" if last_seen > SILENT_AFTER_BEATS * interval else "live"

    def _prune(self) -> None:
        """Drop gone records past retention, then cap everything not live.

        Oldest departure first for the cap; never-admitted records count as
        older than any departure, since they were never worth listing.
        """
        now = self._clock()
        for name, record in list(self._records.items()):
            if record.left_at is not None and now - record.left_at > self.retain_gone:
                del self._records[name]
        not_live = [r for r in self._records.values() if not r.alive]
        excess = len(not_live) - self.max_gone
        if excess > 0:
            not_live.sort(key=lambda r: (r.left_at is not None, r.left_at or 0.0))
            for record in not_live[:excess]:
                del self._records[record.name]
