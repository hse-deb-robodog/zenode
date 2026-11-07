"""A node whose settings a diagnostic client can read, and one it can change.

Run each role in its own terminal::

    uv run python examples/tunable.py drive
    uv run python examples/tunable.py tune 0.8      # new max speed, in m/s

``drive`` sets ``describe_config = True``, so its ``DriveConfig`` and the
config's JSON Schema ride on its latched descriptor (``node/drive/info``).
That is what ``zenode-sovd`` serves as ``storedData``. ``tune`` stands in for
that diagnostic client: it reads the descriptor, calls the ``"write"``
service, and reads the descriptor again to see the change.

Two things this does *not* do, on purpose:

- Writing ``NodeInfo.config`` back changes nothing. Only ``speed_limit`` is
  writable, because ``drive`` declared a service for it and its handler
  decides what a write means. Everything else in the config stays read-only.
- ``fleet_token`` is a ``SecretStr``, so the descriptor carries
  ``"**********"``. Declared as a plain ``str``, it would go out as written.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from pydantic import BaseModel, Field, SecretStr

from zenode import Node, NodeConfig, Service, Topic, TransportConfig, run, serve
from zenode.msgs import NodeInfo, info_key

ENDPOINT = "tcp/127.0.0.1:17448"


class DriveConfig(NodeConfig):
    """What ``[node.drive]`` in zenode.toml configures. SI units on the wire."""

    max_speed: float = 0.5
    """m/s. Writable at runtime through :data:`SET_SPEED_LIMIT`."""
    hard_max_speed: float = 1.5
    """m/s. The chassis limit, which no write may exceed. Read-only."""
    wheel_radius: float = 0.08
    """m. Read-only."""
    fleet_token: SecretStr = SecretStr("change-me")


class SpeedLimit(BaseModel):
    """The PUT body. Its schema reaches the client on ``NodeInfo.serves``."""

    max_speed: float = Field(gt=0, description="Commanded speed cap, m/s.")


# `"write"`: reachable by PUT, needs a typed body, and the handler owns it.
SET_SPEED_LIMIT = Service(
    "drive/speed_limit",
    request=SpeedLimit,
    reply=SpeedLimit,
    description="Change the speed cap; refused above the chassis limit.",
    diagnostic="write",
)


class Drive(Node):
    name = "drive"
    describe_config = True
    config: DriveConfig

    @serve(SET_SPEED_LIMIT)
    async def on_speed_limit(self, req: SpeedLimit) -> SpeedLimit:
        # Validation beyond the schema belongs here: only the node knows its
        # chassis. Raising becomes an error reply, and the config is untouched.
        if req.max_speed > self.config.hard_max_speed:
            raise ValueError(
                f"max_speed {req.max_speed} m/s exceeds the chassis limit "
                f"of {self.config.hard_max_speed} m/s"
            )
        # A real driver would push the new cap to the motor controller here.
        # Replacing (rather than mutating) keeps every reader's snapshot whole.
        self.config = self.config.model_copy(update={"max_speed": req.max_speed})
        self.log.warning("max_speed set to %.2f m/s", req.max_speed)
        # After a successful write, the node republishes its descriptor on
        # the next heartbeat, so the new value appears in NodeInfo.config.
        return SpeedLimit(max_speed=self.config.max_speed)


DRIVE_INFO = Topic(info_key("drive"), NodeInfo, latched=True)


class Tune(Node):
    """A stand-in for a diagnostic client that never imports DriveConfig."""

    name = "tune"
    health_interval = None  # a one-shot tool, not a fleet member

    def __init__(self, max_speed: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.max_speed = max_speed
        self.descriptors: asyncio.Queue[NodeInfo] = asyncio.Queue()

    async def on_start(self) -> None:
        # Latched: the descriptor arrives from the publisher's cache even
        # though `drive` published it long before this node existed.
        self.subscribe(DRIVE_INFO, self.descriptors.put_nowait)
        self.spawn(self.session_script(), name="tune")

    async def session_script(self) -> None:
        try:
            before = await asyncio.wait_for(self.descriptors.get(), timeout=10)
            print("settings:", before.config)
            writes = [s.key for s in before.serves if s.diagnostic == "write"]
            print("writable through:", writes)

            reply = await self.call(SET_SPEED_LIMIT, SpeedLimit(max_speed=self.max_speed))
            print("drive accepted max_speed =", reply.max_speed)

            after = await asyncio.wait_for(self.descriptors.get(), timeout=10)
            print("settings now:", after.config)
        except Exception as e:  # an error reply (over the limit), or no drive
            print("tune failed:", e)
        finally:
            self.stop()


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args[:1] == ["drive"]:
        run(Drive, transport=TransportConfig(listen=[ENDPOINT]))
    elif args[:1] == ["tune"] and len(args) == 2:
        run(Tune(float(args[1]), transport=TransportConfig(connect=[ENDPOINT])))
    else:
        sys.exit(f"usage: {sys.argv[0]} {{drive | tune <max_speed m/s>}}")


if __name__ == "__main__":
    main()
