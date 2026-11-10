"""Entry point: one rover node per process.

    uv run python -m examples.rover drive
    uv run python -m examples.rover mission 0.3

Run it from the repository root, one terminal per node. This is deliberately
not a launcher: zenode has none, and on a robot each node is its own systemd
unit or container.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from zenode import Node, load_transport_config, run

from .camera import Camera
from .detector import Detector
from .drive import Drive
from .mission import Mission
from .rangers import Rangers
from .safety import Safety
from .telemetry import setup_telemetry
from .teleop import Teleop

NODES: dict[str, type[Node]] = {
    node.name: node for node in (Teleop, Drive, Rangers, Safety, Camera, Detector)
}


def main() -> None:
    # The rover's config sits next to its code, not in the working directory.
    # Through the environment, so load_section() and the CLI find it too.
    os.environ.setdefault("ZENODE_CONFIG", str(Path(__file__).with_name("zenode.toml")))

    args = sys.argv[1:]
    if len(args) == 1 and args[0] in NODES:
        setup_telemetry(args[0])
        # Given a class, run() loads [transport] and [node.<name>] for it.
        run(NODES[args[0]])
    elif len(args) == 2 and args[0] == "mission":
        # Given an instance, run() takes it as built, so the transport is
        # loaded here.
        run(Mission(max_speed=float(args[1]), transport=load_transport_config()))
    else:
        sys.exit(f"usage: python -m examples.rover {{{' | '.join(NODES)} | mission <max_speed>}}")


if __name__ == "__main__":
    main()
