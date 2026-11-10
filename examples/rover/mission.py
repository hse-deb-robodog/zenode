"""mission: a one-shot client that talks to the running rover and exits.

    uv run python -m examples.rover mission 0.3     # new speed cap, in m/s

It waits for the nodes it needs, sets the speed cap through the ``"write"``
service, resets the odometry through the ``"operation"`` service, and stops.
Try ``mission 9`` to see the refusal: ``drive`` raises, and the caller gets a
``ServiceError`` carrying the message instead of a silent timeout.

This is a one-shot tool, so it publishes no heartbeat. It takes a constructor
argument, which is why ``__main__`` builds the instance itself.
"""

from __future__ import annotations

from typing import Any

from zenode import Node, ServiceError, ServiceTimeout
from zenode.msgs import Empty

from .contract import RoverServices, SpeedLimit


class Mission(Node):
    name = "mission"
    health_interval = None

    def __init__(self, max_speed: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)  # first: wires config, transport, logging
        self.max_speed = max_speed

    async def on_start(self) -> None:
        # on_start must return for the node to be running, so the script runs
        # as a tracked background task.
        self.spawn(self.script(), name="mission")

    async def script(self) -> None:
        try:
            await self.wait_for_nodes({"drive", "safety"}, timeout=10.0)
            limit = await self.call(RoverServices.speed_limit, SpeedLimit(max_speed=self.max_speed))
            print(f"drive accepted max_speed = {limit.max_speed} m/s")
            pose = await self.call(RoverServices.reset_odom, Empty())
            print(f"odometry reset to x={pose.x} y={pose.y} theta={pose.theta}")
        except TimeoutError as e:  # wait_for_nodes: somebody is not running
            print(f"mission aborted: {e}")
        except ServiceTimeout as e:  # nobody serves the key
            print(f"mission aborted, no answer: {e}")
        except ServiceError as e:  # the handler raised, e.g. over the limit
            print(f"drive refused: {e}")
        finally:
            self.stop()
