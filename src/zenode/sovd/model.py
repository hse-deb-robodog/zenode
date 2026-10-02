"""SOVD wire shapes, as ``opensovd-models`` spells them at commit 26953d97.

Pydantic rather than hand-built dicts so ``?include-schema=true`` can answer
with ``model_json_schema()`` and so a typo in a key is a test failure here
rather than a client that silently ignores a field. Three rules, all of them
upstream's: link keys are kebab-case, unset optionals are *omitted* (never
``null`` or ``[]``), and ``Response<T>`` is flattened — ``schema`` sits next
to ``items`` or ``id``, there is no ``data`` envelope.
"""

from __future__ import annotations

import json
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

SOVD_VERSION = "1.1"
"""What ``opensovd-core`` reports at the pinned commit. The design note's
conformance stance covers why this is a pin, not a claim."""

T = TypeVar("T")


class Wire(BaseModel):
    """Base for everything that leaves the socket.

    Aliases are *serialization* aliases: the sidecar builds these models and
    emits them, never parses them, so the constructor keeps the Python names
    and only the wire gets the kebab-case.
    """

    def to_json(self) -> bytes:
        return self.model_dump_json(by_alias=True, exclude_none=True).encode()

    @classmethod
    def schema_of(cls) -> dict[str, Any]:
        return cls.model_json_schema(by_alias=True)


class Items(Wire, Generic[T]):
    """``{"items": [...]}``, with the optional sibling ``schema``."""

    items: list[T]
    schema_: dict[str, Any] | None = Field(default=None, serialization_alias="schema")


class EntityReference(Wire):
    """One row of ``/components``."""

    id: str
    name: str
    href: str
    translation_id: str | None = None
    tags: list[str] | None = None


class EntityCapabilities(Wire):
    """``/components/{id}`` and the root: links to what the entity supports.

    Only the links the sidecar serves are declared. ``opensovd-models`` has
    many more (``faults``, ``operations``, ``modes`` …); a key absent from the
    response is a capability the entity does not have, which is the whole
    discovery contract.
    """

    id: str
    name: str
    translation_id: str | None = None
    variant: dict[str, str] | None = None
    data: str | None = None
    hosts: str | None = None
    belongs_to: str | None = Field(default=None, serialization_alias="belongs-to")
    components: str | None = None
    apps: str | None = None
    areas: str | None = None
    x_zenode_logs: str | None = Field(default=None, serialization_alias="x-zenode-logs")
    schema_: dict[str, Any] | None = Field(default=None, serialization_alias="schema")


class Metadata(Wire):
    """One row of ``/data``."""

    id: str
    name: str
    category: str
    translation_id: str | None = None
    groups: list[str] | None = None
    tags: list[str] | None = None


class ReadResponse(Wire):
    """``/data/{id}``. Not wrapped — ``schema`` is a direct sibling of ``data``."""

    id: str
    data: Any
    schema_: dict[str, Any] | None = Field(default=None, serialization_alias="schema")


class DataCategoryInformation(Wire):
    """One row of ``/data-categories``. The key really is ``item``."""

    item: str


class Group(Wire):
    """One row of ``/data-groups``."""

    id: str
    category: str


class VendorInfo(Wire):
    version: str
    name: str


class SovdInfo(Wire):
    version: str
    base_uri: str
    vendor_info: VendorInfo | None = None


class VersionInfo(Wire):
    sovd_info: list[SovdInfo]
    schema_: dict[str, Any] | None = Field(default=None, serialization_alias="schema")


class GenericError(Wire):
    """Every error body. ``error_code`` is upstream's closed vocabulary;
    entity misses travel as ``vendor-specific`` with a ``vendor_code``."""

    error_code: str
    vendor_code: str | None = None
    message: str


def error_body(code: str, message: str, vendor_code: str | None = None) -> bytes:
    return GenericError(error_code=code, vendor_code=vendor_code, message=message).to_json()


def dumps(value: Any) -> bytes:
    """For the one body that is not a model: a plain dict."""
    return json.dumps(value).encode()
