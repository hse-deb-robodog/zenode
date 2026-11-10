"""safety: decides whether the rover may move, and tells everyone.

One wildcard subscription covers every range sensor; ``Envelope.key`` says
which one spoke. The decision goes out on the latched ``estop`` topic, so a
node that starts later still receives it.

Two reasons raise the stop:

- A sensor reads closer than ``stop_distance``. It is released only beyond
  ``release_distance``, so a reading that hovers at the threshold does not
  make the rover stutter.
- No sensor reports at all. A ``deadline`` on a wildcard subscription trips
  only when *every* matching key is silent, which is the case that matters
  here. Per-sensor staleness would be the handler's job.

The warnings logged here are published on the bus (``publish_logs_at``
defaults to WARNING), so ``zenode logs`` shows them from any machine.
"""

from __future__ import annotations

from typing import Any

from zenode import Envelope, Node, NodeConfig, on_silence, publish, subscribe

from .contract import EStop, Range, RoverTopics


class SafetyConfig(NodeConfig):
    stop_distance: float = 0.30
    """m. Closer than this raises the stop."""
    release_distance: float = 0.50
    """m. Farther than this, on every sensor, releases it."""


class Safety(Node):
    name = "safety"
    config: SafetyConfig
    # A second safety node would publish a second, possibly contradicting
    # estop. Refuse to start instead of warning.
    allow_duplicates = False

    estop = publish(RoverTopics.estop)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.too_close: set[str] = set()
        self.sensors_silent = True  # nothing heard yet is not "safe"
        self.published: EStop | None = None

    async def on_start(self) -> None:
        # publish() descriptors exist before on_start, so the initial state is
        # on the bus before any handler of this node can run.
        self._decide()

    @subscribe(RoverTopics.ranges, deadline=1.0)
    def on_range(self, msg: Range, env: Envelope) -> None:
        if env.key is None:  # only an envelope built by hand lacks one
            return
        sensor = env.key.split("/")[-2]  # rover/sensors/<name>/range
        self.sensors_silent = False
        if msg.meters < self.config.stop_distance:
            self.too_close.add(sensor)
        elif msg.meters > self.config.release_distance:
            self.too_close.discard(sensor)
        self._decide()

    @on_silence(RoverTopics.ranges)
    def ranges_lost(self, silent_for: float) -> None:
        self.sensors_silent = True
        self._decide()

    def _decide(self) -> None:
        if self.too_close:
            state = EStop(active=True, reason=f"obstacle at {', '.join(sorted(self.too_close))}")
        elif self.sensors_silent:
            state = EStop(active=True, reason="no range sensor is reporting")
        else:
            state = EStop()
        if state == self.published:
            return  # latched: publish changes, not a stream
        self.published = state
        self.estop.put(state)
        if state.active:
            self.log.warning("estop raised: %s", state.reason)
        else:
            self.log.warning("estop released")
