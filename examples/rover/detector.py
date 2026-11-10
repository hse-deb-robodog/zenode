"""detector: a consumer that is slower than its producer.

"Inference" takes 50 ms and the camera delivers a frame every 33 ms, so this
node cannot keep up, and the example is about what happens then. The
subscription uses ``mode="queue"`` with a small ``queue_size``: when the queue
is full the *oldest* frame is dropped and counted. The camera is never blocked
and the queue never holds more than four frames.

Watch it on the heartbeat::

    uv run zenode health detector --watch

``dropped`` climbs and ``queue_max_depth`` sits at the queue size. Switch the
subscription to ``mode="latest"`` and the node always works on the newest
frame instead, which is usually what a perception node wants.

The handler never mentions shared memory. A frame is ``bytes`` either way.
"""

from __future__ import annotations

import asyncio
from typing import Any

from zenode import Node, NodeConfig, publish, subscribe

from .contract import Detection, RoverTopics


class DetectorConfig(NodeConfig):
    inference_time: float = 0.05
    """s per frame."""


class Detector(Node):
    name = "detector"
    config: DetectorConfig

    detections = publish(RoverTopics.detections)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.last_frame = 0

    @subscribe(RoverTopics.frame, mode="queue", queue_size=4)
    async def on_frame(self, frame: bytes) -> None:
        number = int.from_bytes(frame[:4], "little")
        await asyncio.sleep(self.config.inference_time)  # stand in for a model
        skipped = number - self.last_frame - 1 if self.last_frame else 0
        self.last_frame = number
        self.detections.put(Detection(frame=number, skipped=max(0, skipped)))
