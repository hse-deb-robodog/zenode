"""camera: large raw frames through shared memory, only while someone watches.

``RoverTopics.frame`` declares ``shm=True`` and ``zenode.toml`` enables
``shared_memory``, so a frame reaches a subscriber on this machine without
being copied through a socket. If the pool is unavailable or exhausted the
frame takes the normal path and is counted as ``shm_fallbacks`` on the
heartbeat: slower, never lost. See docs/shared-memory.md.

``@on_matching`` is for producers that are expensive to *run*. With no
subscriber the camera does not grab at all. Start ``detector`` and it begins;
stop ``detector`` and it idles again.
"""

from __future__ import annotations

import time
from typing import Any

from zenode import Node, every, metric, on_matching, publish

from .contract import RoverTopics

WIDTH, HEIGHT = 640, 480
FRAME_BYTES = WIDTH * HEIGHT * 3


class Camera(Node):
    name = "camera"
    # Room for eight frames in flight. Kept under the common 8 MB `ulimit -l`
    # default, which `zenode doctor` checks.
    shm_pool_bytes = 8 * FRAME_BYTES

    frames = publish(RoverTopics.frame)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.buffer = bytearray(FRAME_BYTES)
        self.streaming = False
        self.grabbed = 0
        self.put_seconds = 0.0  # time spent in put() since the last heartbeat
        self.puts = 0

    @on_matching(RoverTopics.frame)
    def on_viewers(self, matching: bool) -> None:
        # Fires once with the current state at start, then on each change.
        self.streaming = matching
        self.log.info("streaming" if matching else "idle: nobody is subscribed")

    @every(1 / 30)
    def grab(self) -> None:
        if not self.streaming:
            return
        self.grabbed += 1
        self.buffer[:4] = self.grabbed.to_bytes(4, "little")  # the frame counter
        frame = bytes(self.buffer)
        started = time.perf_counter()
        self.frames.put(frame)
        self.put_seconds += time.perf_counter() - started
        self.puts += 1

    @metric("frames_grabbed", unit="{frame}", kind="counter", integral=True)
    def _grabbed(self) -> int:
        return self.grabbed

    # The cost shared memory is there to cut. Compare it with the same pipeline
    # run with ZENODE_TRANSPORT__SHARED_MEMORY=false at both ends.
    @metric("publish_ms", unit="ms", description="Mean put() time since the last heartbeat.")
    def _publish_ms(self) -> float | None:
        if not self.puts:
            return None  # unknown, which is not the same as zero
        mean_ms = 1000 * self.put_seconds / self.puts
        self.put_seconds, self.puts = 0.0, 0
        return mean_ms
