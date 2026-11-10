"""drive: follows velocity commands and reports where the rover is.

The node with the most to lose, so it shows the defensive half of zenode:

- ``max_age`` on the topic drops a command that is too old to act on, and a
  ``deadline`` on the subscription catches commands that stop arriving.
  ``@on_silence`` / ``@on_resume`` turn that into a motor watchdog.
- It does not move until ``safety`` has said it may. ``estop`` is latched, so
  a restarted ``drive`` learns the current state at once.
- The control loop runs at a rate from config, with ``on_error="stop"``: a
  control loop that raises must not keep running blind.
- Opening the motor controller blocks, so it goes through ``self.blocking``.
- ``describe_config`` publishes the settings on the node's descriptor, and a
  ``"write"`` service makes exactly one of them changeable at runtime.
"""

from __future__ import annotations

import math
import time
from typing import Any

from zenode import Node, NodeConfig, every, metric, on_resume, on_silence, publish, serve, subscribe
from zenode.msgs import Empty, Pose2D, Twist

from .contract import EStop, RoverServices, RoverTopics, SpeedLimit
from .geometry import load_geometry


class DriveConfig(NodeConfig):
    max_speed: float = 0.5
    """m/s. Writable at runtime through ``RoverServices.speed_limit``."""
    hard_max_speed: float = 1.5
    """m/s. The chassis limit, which no write may exceed."""
    control_rate_hz: float = 20.0


class Motors:
    """A stand-in for a motor controller on a serial port."""

    def __init__(self) -> None:
        time.sleep(0.5)  # opening the port and homing takes a while
        self.left = self.right = 0.0  # wheel speeds, rad/s

    def set(self, left: float, right: float) -> None:
        self.left, self.right = left, right

    def close(self) -> None:
        self.set(0.0, 0.0)


class Drive(Node):
    name = "drive"
    describe_config = True
    config: DriveConfig

    odom = publish(RoverTopics.odom)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.geometry = load_geometry()
        self.motors: Motors | None = None
        self.pose = Pose2D()
        self.command: Twist | None = None  # None while commands are silent
        self.estop: EStop | None = None  # None until `safety` has spoken
        self.speed = 0.0

    async def on_start(self) -> None:
        # A synchronous call here would stall the event loop, the start
        # timeout and Ctrl-C with it. `zenode health` shows state="starting"
        # meanwhile, because the heartbeat already ticks.
        self.motors = await self.blocking(Motors)

    async def on_stop(self) -> None:
        # Runs even when on_start failed part-way, so tolerate a missing port.
        if self.motors is not None:
            self.motors.close()

    # --- inputs -----------------------------------------------------------

    @subscribe(RoverTopics.cmd_vel, mode="latest", deadline=0.5)
    def on_cmd(self, msg: Twist) -> None:
        self.command = msg

    @on_silence(RoverTopics.cmd_vel)
    def cmd_lost(self, silent_for: float) -> None:
        self.command = None
        self.log.warning("no velocity command for %.1f s; stopping", silent_for)

    @on_resume(RoverTopics.cmd_vel)
    def cmd_back(self, silent_for: float) -> None:
        self.log.info("velocity commands resumed after %.1f s", silent_for)

    @subscribe(RoverTopics.estop)
    def on_estop(self, msg: EStop) -> None:
        if self.estop is None or msg.active != self.estop.active:
            if msg.active:
                self.log.warning("estop raised: %s", msg.reason)
            else:
                self.log.warning("estop released")
        self.estop = msg

    # --- control loop -----------------------------------------------------

    @every("control_rate_hz", unit="hz", on_error="stop")
    def tick(self) -> None:
        assert self.motors is not None  # bindings activate after on_start
        linear, angular = self._allowed_twist()

        # Twist to wheel speeds and back: what the motors are told is what
        # the odometry integrates.
        r, half = self.geometry.wheel_radius, self.geometry.wheel_separation / 2
        self.motors.set((linear - angular * half) / r, (linear + angular * half) / r)
        linear = (self.motors.left + self.motors.right) * r / 2
        angular = (self.motors.right - self.motors.left) * r / (2 * half)

        dt = 1.0 / self.config.control_rate_hz
        self.pose = Pose2D(
            x=self.pose.x + linear * math.cos(self.pose.theta) * dt,
            y=self.pose.y + linear * math.sin(self.pose.theta) * dt,
            theta=self.pose.theta + angular * dt,
        )
        self.speed = linear
        self.odom.put(self.pose)

    def _allowed_twist(self) -> tuple[float, float]:
        """The commanded twist, zeroed when unsafe and scaled to the cap."""
        if self.command is None or self.estop is None or self.estop.active:
            return 0.0, 0.0
        linear, angular = self.command.linear.x, self.command.angular.z
        if abs(linear) > self.config.max_speed:
            # Scale both, so the cap slows the arc down without tightening it.
            scale = self.config.max_speed / abs(linear)
            linear, angular = linear * scale, angular * scale
        return linear, angular

    # Rides on the heartbeat: `zenode health` and `zenode export` report it
    # without importing this module. It must not block, so it returns a value
    # the control loop already computed.
    @metric("speed", unit="m/s", description="Forward speed.")
    def _speed(self) -> float:
        return self.speed

    # --- services ---------------------------------------------------------

    @serve(RoverServices.speed_limit)
    def on_speed_limit(self, req: SpeedLimit) -> SpeedLimit:
        # The schema already rejected a non-positive value. What only this
        # node knows is its chassis. Raising becomes an error reply.
        if req.max_speed > self.config.hard_max_speed:
            raise ValueError(
                f"max_speed {req.max_speed} m/s exceeds the chassis limit "
                f"of {self.config.hard_max_speed} m/s"
            )
        # Replace, never mutate: every reader keeps a whole snapshot. After a
        # successful write the descriptor is republished on the next heartbeat.
        self.config = self.config.model_copy(update={"max_speed": req.max_speed})
        self.log.warning("max_speed set to %.2f m/s", req.max_speed)
        return SpeedLimit(max_speed=self.config.max_speed)

    @serve(RoverServices.reset_odom)
    def on_reset_odom(self, _: Empty) -> Pose2D:
        # In this simulation the odometry is the rover's position, so a reset
        # also carries it back to the middle of the room.
        self.pose = Pose2D()
        self.log.warning("odometry reset")
        return self.pose
