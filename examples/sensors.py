"""Many sources, one subscription: wildcard keys.

Run each role in its own terminal::

    uv run python examples/sensors.py sensors
    uv run python examples/sensors.py monitor

and point the CLI at the same wildcard from a third::

    uv run zenode topics 'sensors/**' --contract examples.sensors
    uv run zenode echo 'sensors/*/temp' --contract examples.sensors \\
        --connect tcp/127.0.0.1:17449
    uv run zenode hz 'sensors/**' --connect tcp/127.0.0.1:17449

(Quote the key: bash, zsh and fish all try to expand an unquoted ``*`` as a
file glob before zenode ever sees it.)

The contract declares one wildcard topic, ``sensors/*/temp``. The ``sensors``
node publishes on concrete keys under it, one per sensor and each at its own
rate; the ``monitor`` node subscribes once to the wildcard and tells the
sensors apart by ``Envelope.key``. ``echo`` decodes every concrete key through
the wildcard topic, and ``hz`` reports one rate per sensor, not a blend.

Three things this shows on purpose:

- The payload does not repeat the sensor's name. The key already says which
  sensor spoke, and a name in both places is two sources of truth.
- Publishing needs a concrete key. ``publish(SensorTopics.temps)`` or
  ``self.publisher(SensorTopics.temps)`` raises ``ContractError``, because a
  put on a wildcard would land on every sensor's key at once.
- ``deadline=`` on a wildcard subscription only trips when *every* sensor is
  silent: one live sensor keeps the subscription fed. Per-sensor staleness is
  the handler's job, which is why ``monitor`` keeps its own last-seen times.
"""

from __future__ import annotations

import math
import sys
import time
from typing import Any

from pydantic import BaseModel

from zenode import Envelope, Node, Topic, TopicSet, TransportConfig, every, run

ENDPOINT = "tcp/127.0.0.1:17449"


class Temperature(BaseModel):
    """One reading. Degrees Celsius, per docs/conventions.md."""

    celsius: float


class SensorTopics(TopicSet):
    # Subscribe-only: the `*` is one key segment, the sensor's name.
    temps = Topic("sensors/*/temp", Temperature, description="Every sensor's temperature.")


def temp_topic(sensor: str) -> Topic[Temperature]:
    """The concrete key one sensor publishes on, inside the wildcard above."""
    return Topic(f"sensors/{sensor}/temp", Temperature)


SENSORS: dict[str, tuple[float, float]] = {
    # name: (publish interval in s, base temperature in °C)
    "motor_left": (0.1, 45.0),
    "motor_right": (0.1, 47.0),
    "battery": (1.0, 31.0),
}


class Sensors(Node):
    name = "sensors"

    async def on_start(self) -> None:
        # Imperative, because the set of sensors is data rather than a fixed
        # list of class attributes. One publisher and one timer per sensor.
        for sensor, (interval, base) in SENSORS.items():
            pub = self.publisher(temp_topic(sensor))

            def read(pub=pub, base=base) -> None:
                # A stand-in for the hardware: a slow drift around the base.
                pub.put(Temperature(celsius=base + 2.0 * math.sin(time.monotonic() / 5.0)))

            self.every(interval, read, name=f"read:{sensor}")


class Monitor(Node):
    name = "monitor"
    stale_after = 3.0
    """Seconds without a reading before a sensor is reported stale."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Bounded by the number of sensors on the bus, not by time.
        self.latest: dict[str, tuple[float, float]] = {}

    async def on_start(self) -> None:
        self.subscribe(SensorTopics.temps, self.on_temp)

    def on_temp(self, msg: Temperature, env: Envelope) -> None:
        # env.key is the concrete key, namespace included — e.g.
        # "sensors/battery/temp". The wildcard segment is the sensor's name.
        if env.key is None:  # only an envelope built by hand lacks one
            return
        sensor = env.key.split("/")[-2]
        # Monotonic, not env.ts_ns: staleness must not depend on clock sync.
        self.latest[sensor] = (msg.celsius, time.monotonic())

    @every(2.0)
    def report(self) -> None:
        now = time.monotonic()
        lines = []
        for sensor in sorted(self.latest):
            celsius, seen = self.latest[sensor]
            age = now - seen
            flag = "  STALE" if age > self.stale_after else ""
            lines.append(f"  {sensor:<12} {celsius:6.2f} °C  ({age:4.1f}s ago){flag}")
        print("\n".join(lines) if lines else "  no readings yet")
        print()


def main() -> None:
    role = sys.argv[1:2]
    if role == ["sensors"]:
        run(Sensors, transport=TransportConfig(listen=[ENDPOINT]))
    elif role == ["monitor"]:
        run(Monitor, transport=TransportConfig(connect=[ENDPOINT]))
    else:
        sys.exit(f"usage: {sys.argv[0]} {{sensors | monitor}}")


if __name__ == "__main__":
    main()
