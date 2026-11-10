"""rangers: several range sensors, one node, one key per sensor.

The set of sensors is data (``[node.rangers.sensors]``), so the wiring is
imperative: one publisher per sensor, created in ``on_start``. Each publishes
on a concrete key such as ``sensors/front/range``. A publisher on the wildcard
``sensors/*/range`` is refused, because a put would land on every key at once.

There is no hardware, so a reading is computed from the pose: the distance
from the hull to the wall of a square room, along the sensor's bearing. The
readings are published from inside the ``odom`` handler, which is why they
belong to the trace that the pose started.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import Field

from zenode import Node, NodeConfig, Publisher, subscribe
from zenode.msgs import Pose2D

from .contract import Range, RoverTopics, range_topic
from .geometry import load_geometry


class RangersConfig(NodeConfig):
    sensors: dict[str, float] = Field(default_factory=lambda: {"front": 0.0})
    """Sensor name to bearing in radians, counter-clockwise from straight ahead."""
    room_half_size: float = 2.0
    """m. The room is a square centred on the origin."""


class Rangers(Node):
    name = "rangers"
    config: RangersConfig

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.geometry = load_geometry()
        self.outputs: dict[str, Publisher[Range]] = {}

    async def on_start(self) -> None:
        for sensor in self.config.sensors:
            self.outputs[sensor] = self.publisher(range_topic(sensor))

    @subscribe(RoverTopics.odom, mode="latest")
    def on_odom(self, pose: Pose2D) -> None:
        for sensor, bearing in self.config.sensors.items():
            meters = self._distance_to_wall(pose, pose.theta + bearing)
            self.outputs[sensor].put(Range(meters=meters))

    def _distance_to_wall(self, pose: Pose2D, heading: float) -> float:
        """From the hull edge to the first wall along ``heading``; never negative."""
        half = self.config.room_half_size
        hits = []
        for position, direction in ((pose.x, math.cos(heading)), (pose.y, math.sin(heading))):
            if abs(direction) > 1e-9:
                hits.append((math.copysign(half, direction) - position) / direction)
        return max(0.0, min(hits) - self.geometry.body_radius)
