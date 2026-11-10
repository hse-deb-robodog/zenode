"""The rover's contract: every key, payload and delivery rule the nodes share.

This module is the only thing two rover nodes have in common. It imports no
node and no zenoh, so the CLI can load it on a laptop that runs none of them::

    uv run zenode topics --contract examples.rover.contract

Keys are relative. ``zenode.toml`` sets ``namespace = "rover"``, so ``odom``
is ``rover/odom`` on the bus. SI units throughout, per docs/conventions.md.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from zenode import Service, Topic, TopicSet
from zenode.msgs import Empty, Pose2D, Twist


class Range(BaseModel):
    """One range reading. The key names the sensor, so the payload does not."""

    meters: float


class EStop(BaseModel):
    """Whether the rover may move, and why not."""

    active: bool = False
    reason: str = ""


class Detection(BaseModel):
    """What the detector made of one camera frame."""

    frame: int
    skipped: int = 0
    """Frames the detector never saw between the previous one and this."""


class SpeedLimit(BaseModel):
    """The speed cap, as a service request and as its reply."""

    max_speed: float = Field(gt=0, description="Commanded speed cap, m/s.")


class RoverTopics(TopicSet):
    # A stale command is worse than none, so subscribers drop one older than
    # max_age. At 10 Hz it is small and rare enough for express=True, and it
    # outranks everything else on a congested link.
    cmd_vel = Topic(
        "cmd_vel",
        Twist,
        max_age=0.5,
        priority="real_time",
        express=True,
        description="Velocity command, body frame.",
    )
    # The trace root: one pose in five starts a trace that follows the data
    # through `rangers` into `safety`. Publishing from a timer is outside any
    # trace, so without trace=True nothing downstream would be traced at all.
    odom = Topic(
        "odom",
        Pose2D,
        trace=True,
        trace_ratio=0.2,
        description="Pose in the world frame, integrated from wheel speeds.",
    )
    # Subscribe-only: `*` is one key segment, the sensor's name.
    ranges = Topic("sensors/*/range", Range, description="Every range sensor.")
    # Latched, so a node that starts after the stop was raised still learns of
    # it. "block", because a stop must not be dropped on a congested link. It
    # changes a few times a minute, so blocking the publisher is affordable.
    estop = Topic(
        "estop",
        EStop,
        latched=True,
        priority="interactive_high",
        congestion_control="block",
        description="Emergency stop state. Only `safety` publishes it.",
    )
    # Raw bytes through shared memory, below everything else in priority. The
    # subscriber reads it like any other topic.
    frame = Topic(
        "camera/frame",
        bytes,
        shm=True,
        priority="data_low",
        description="Raw RGB frame; the first four bytes are a frame counter.",
    )
    detections = Topic("camera/detections", Detection, description="One per processed frame.")


def range_topic(sensor: str) -> Topic[Range]:
    """The concrete key one sensor publishes on, inside ``RoverTopics.ranges``."""
    return Topic(f"sensors/{sensor}/range", Range)


class RoverServices(TopicSet):
    # "write": reachable by a diagnostic client as a PUT; the handler decides
    # what the write means.
    speed_limit = Service(
        "drive/speed_limit",
        request=SpeedLimit,
        reply=SpeedLimit,
        description="Change the speed cap; refused above the chassis limit.",
        diagnostic="write",
    )
    # "operation": an action with a side effect, never reachable by GET.
    reset_odom = Service(
        "drive/reset_odom",
        request=Empty,
        reply=Pose2D,
        description="Put the rover back at the origin.",
        diagnostic="operation",
    )
