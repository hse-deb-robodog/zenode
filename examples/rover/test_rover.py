"""Testing rover nodes: logic directly, message flow through the harness.

    uv run pytest examples/rover -q

There are two kinds of test here (docs/testing.md):

- The decorators only stamp metadata, so a handler is still an ordinary
  method. A test about *logic* constructs the node and calls it, with no
  session.
- A test about *message flow* uses ``harness()``: one in-process zenoh session
  with multicast off, so it needs no router and cannot reach a real robot.
"""

from __future__ import annotations

import pytest

from zenode.msgs import Twist, Vector3
from zenode.testing import harness

from .contract import EStop, Range, RoverTopics, SpeedLimit, range_topic
from .drive import Drive, DriveConfig
from .safety import Safety

FORWARD = Twist(linear=Vector3(x=1.0), angular=Vector3(z=0.5))


def test_speed_cap_scales_the_whole_arc() -> None:
    drive = Drive(config=DriveConfig(max_speed=0.5))
    drive.estop = EStop()
    drive.on_cmd(FORWARD)

    # Halving the speed halves the turn rate, so the path keeps its radius.
    assert drive._allowed_twist() == (0.5, 0.25)


def test_drive_does_not_move_before_safety_has_spoken() -> None:
    drive = Drive(config=DriveConfig())
    drive.on_cmd(FORWARD)

    assert drive._allowed_twist() == (0.0, 0.0)


def test_speed_limit_above_the_chassis_limit_is_refused() -> None:
    drive = Drive(config=DriveConfig(hard_max_speed=1.5))

    with pytest.raises(ValueError, match="chassis limit"):
        drive.on_speed_limit(SpeedLimit(max_speed=2.0))
    assert drive.config.max_speed == 0.5  # a refused write changes nothing


@pytest.mark.integration
async def test_safety_raises_and_releases_the_estop() -> None:
    async with harness(namespace="test") as h:
        await h.start_node(Safety)
        # Latched: the collector joins after the first publish and still
        # receives it.
        estops = h.collect(RoverTopics.estop)
        assert (await estops.next()).reason == "no range sensor is reporting"

        front = h.publisher(range_topic("front"))
        front.put(Range(meters=2.0))
        assert not (await estops.next()).active

        front.put(Range(meters=0.1))
        raised = await estops.next()
        assert raised.active
        assert "front" in raised.reason


@pytest.mark.integration
async def test_drive_moves_only_after_the_estop_is_released() -> None:
    async with harness(namespace="test") as h:
        drive = await h.start_node(Drive)
        poses = h.collect(RoverTopics.odom)
        commands = h.publisher(RoverTopics.cmd_vel)

        commands.put(FORWARD)
        for _ in range(3):
            assert (await poses.next()).x == 0.0

        h.publisher(RoverTopics.estop).put(EStop())
        # Wait on the pose itself, since a fixed sleep would be flaky. Commands
        # keep flowing so the subscription's deadline stays satisfied.
        for _ in range(40):
            commands.put(FORWARD)
            if (await poses.next()).x > 0.0:
                break
        assert drive.pose.x > 0.0
