"""The glue: one zenoh session, one loop thread, one capped call bridge.

HTTP handler threads must not touch asyncio, and ``call_service`` is a
coroutine, so the sidecar owns a private event loop on a daemon thread. The
presence watcher hops onto that loop (the same thread-boundary rule
``pubsub.py`` keeps); a handler thread submits a call with
``run_coroutine_threadsafe`` and waits. A semaphore caps what is in flight: a
client retrying in a loop must not become a queryable storm on the robot.

The session is *given*, not opened. The CLI opens one per process; the test
harness lends its own, which is how the sidecar runs in-process.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import threading
from typing import Any

import zenoh

from .. import __version__
from ..cli import _declare_latched_subscriber
from ..codec import RawCodec
from ..errors import ServiceTimeout
from ..msgs import health_pattern, info_pattern, log_pattern
from ..presence import PresenceWatcher
from ..service import call_service
from ..topic import Service
from .server import CallsSaturated
from .topology import Topology

_PASSTHROUGH: Service[bytes, bytes] = Service(
    "sovd/passthrough",
    request=bytes,
    reply=bytes,
    request_codec=RawCodec(zenoh.Encoding.APPLICATION_JSON),
    reply_codec=RawCodec(zenoh.Encoding.APPLICATION_JSON),
)
"""Bytes in, bytes out, labelled JSON on the wire. ``call_service`` takes the
resolved key separately, so this key is never used; the codecs are what make
the node's ``PydanticJsonCodec`` accept the request."""

_CALLER = "zenode-sovd"
"""The ``node`` label on the query attachment, for the node's trace ring."""


class Sidecar:
    def __init__(
        self,
        session: zenoh.Session,
        namespace: str,
        *,
        topology: Topology | None = None,
        call_timeout: float = 2.0,
        max_calls: int = 32,
    ) -> None:
        self.session = session
        self.namespace = namespace
        self.topology = topology if topology is not None else Topology(namespace)
        self.vendor_version = __version__
        self.call_timeout = call_timeout
        self._calls = threading.BoundedSemaphore(max_calls) if max_calls > 0 else None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="zenode-sovd-loop", daemon=True)
        self._watcher: PresenceWatcher | None = None
        self._subscriptions: list[Any] = []

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._thread.start()
        topology = self.topology
        self._watcher = PresenceWatcher(self.session, self.namespace, topology.presence, self._loop)
        self._watcher.start()
        self._subscriptions = [
            _declare_latched_subscriber(
                self.session,
                info_pattern(self.namespace),
                lambda s: topology.offer_info(s.payload.to_bytes()),
            ),
            self.session.declare_subscriber(
                health_pattern(self.namespace),
                lambda s: topology.offer_health(s.payload.to_bytes()),
            ),
            self.session.declare_subscriber(
                log_pattern(self.namespace),
                lambda s: topology.offer_log(s.payload.to_bytes()),
            ),
        ]

    def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        for sub in self._subscriptions:
            with contextlib.suppress(Exception):
                sub.undeclare()
        self._subscriptions = []
        if self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    # ------------------------------------------------------------ the bridge

    def call(self, key: str) -> bytes:
        """From an HTTP thread: call ``key`` with ``{}`` and return the raw reply."""
        if self._calls is None or not self._calls.acquire(blocking=False):
            raise CallsSaturated
        try:
            future = asyncio.run_coroutine_threadsafe(
                call_service(
                    self.session, _PASSTHROUGH, key, b"{}", timeout=self.call_timeout, node=_CALLER
                ),
                self._loop,
            )
            try:
                return future.result(timeout=self.call_timeout + 1.0)
            except concurrent.futures.TimeoutError:
                # The coroutine's own timeout should fire first; this is the
                # belt for a loop that is itself stuck.
                future.cancel()
                raise ServiceTimeout(f"no reply from {key} within {self.call_timeout}s") from None
        finally:
            self._calls.release()
