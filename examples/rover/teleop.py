"""teleop: the source of velocity commands.

Shows the smallest useful node: a config model, a ``publish()`` descriptor and
a timer. It commands a constant arc, which sooner or later drives the rover
towards a wall and gives ``safety`` something to do.

Stop this node and ``drive`` notices the silence within half a second.
"""

from __future__ import annotations

from zenode import Node, NodeConfig, every, publish
from zenode.msgs import Twist, Vector3

from .contract import RoverTopics


class TeleopConfig(NodeConfig):
    linear: float = 0.6
    """m/s, forward."""
    angular: float = 0.3
    """rad/s, counter-clockwise."""


class Teleop(Node):
    name = "teleop"
    config: TeleopConfig  # loaded from [node.teleop] and ZENODE_TELEOP__*

    cmd = publish(RoverTopics.cmd_vel)  # a typed Publisher once the node starts

    @every(0.1)
    def send(self) -> None:
        self.cmd.put(
            Twist(
                linear=Vector3(x=self.config.linear),
                angular=Vector3(z=self.config.angular),
            )
        )
