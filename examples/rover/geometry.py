"""The ``[geometry]`` section: facts about the robot that no single node owns.

``drive`` needs the wheels and ``rangers`` needs the hull. Copied into both
``[node.*]`` tables the values could drift apart; read from one section they
cannot. ``load_section`` applies the same precedence as node config, so
``ZENODE_GEOMETRY__WHEEL_RADIUS=0.06`` overrides the file.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from zenode import load_section


class Geometry(BaseModel):
    # A plain BaseModel ignores unknown keys; forbid them so a typo in
    # zenode.toml fails at startup, as it does in a [node.*] section.
    model_config = ConfigDict(extra="forbid")

    wheel_radius: float = 0.05
    wheel_separation: float = 0.30
    body_radius: float = 0.20


def load_geometry() -> Geometry:
    return load_section(Geometry, "geometry")
