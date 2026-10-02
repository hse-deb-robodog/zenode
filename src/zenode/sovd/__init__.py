"""The SOVD sidecar: ``zenode sovd``.

A read-only ISO 17978 (SOVD) server over what nodes declare — see
``docs/design/opensovd-adapter.md``. Out of process, stdlib HTTP, discovers
everything from the bus. Nothing here is imported by a node.
"""

from .server import make_server
from .sidecar import Sidecar
from .topology import Topology

__all__ = ["Sidecar", "Topology", "make_server"]
