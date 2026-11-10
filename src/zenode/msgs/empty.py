"""The request of a service that takes no arguments.

A ``Service`` declared ``diagnostic="read"`` is served as a SOVD *data*
resource, which is a GET with no body — so its request has to be a model with
no fields, and this is the canonical one. ``extra="forbid"`` rather than the
pydantic default of ignoring unknown keys: a read that receives arguments is a
caller that thinks it is calling something else, and silently dropping them
would hide exactly that.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Empty(BaseModel):
    """A request with no fields. Serializes as ``{}``."""

    model_config = ConfigDict(extra="forbid")
