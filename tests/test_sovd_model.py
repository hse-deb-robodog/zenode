"""The SOVD wire shapes, pinned to opensovd-models at 26953d97.

What matters is what leaves the socket: kebab-case link keys, omitted
optionals rather than nulls or empty lists, `schema` as a sibling key with no
envelope, and `item` (not `id`) in data-categories.
"""

from __future__ import annotations

import json
from typing import Any

from zenode.sovd.model import (
    SOVD_VERSION,
    DataCategoryInformation,
    EntityCapabilities,
    EntityReference,
    GenericError,
    Group,
    Items,
    Metadata,
    ReadResponse,
    SovdInfo,
    VendorInfo,
    VersionInfo,
    Wire,
    error_body,
)


def _load(model: Wire) -> dict[str, Any]:
    return json.loads(model.to_json())


def test_link_keys_are_kebab_case_and_absent_when_unset():
    body = _load(EntityCapabilities(id="nav", name="nav", belongs_to="http://x/belongs-to"))
    assert body == {"id": "nav", "name": "nav", "belongs-to": "http://x/belongs-to"}


def test_custom_log_link_uses_the_vendor_prefix():
    body = _load(EntityCapabilities(id="nav", name="nav", x_zenode_logs="http://x/logs"))
    assert body["x-zenode-logs"] == "http://x/logs"


def test_schema_is_a_sibling_key_not_an_envelope():
    body = _load(Items[EntityReference](items=[], schema_={"type": "object"}))
    assert body == {"items": [], "schema": {"type": "object"}}


def test_empty_optional_lists_are_omitted():
    body = _load(EntityReference(id="nav", name="nav", href="http://x/components/nav"))
    assert "tags" not in body
    body = _load(Metadata(id="sent", name="sent", category="sysInfo"))
    assert "groups" not in body


def test_data_categories_use_the_item_key():
    assert _load(DataCategoryInformation(item="sysInfo")) == {"item": "sysInfo"}
    assert _load(Group(id="runtime", category="sysInfo")) == {
        "id": "runtime",
        "category": "sysInfo",
    }


def test_read_response_carries_schema_only_when_asked():
    assert _load(ReadResponse(id="sent", data={"value": 3})) == {"id": "sent", "data": {"value": 3}}
    assert "schema" in _load(ReadResponse(id="sent", data={"value": 3}, schema_={"type": "object"}))


def test_version_info_shape():
    body = _load(
        VersionInfo(
            sovd_info=[
                SovdInfo(
                    version=SOVD_VERSION,
                    base_uri="http://robot:7690/sovd/v1",
                    vendor_info=VendorInfo(name="zenode", version="0.1.0"),
                )
            ]
        )
    )
    assert body["sovd_info"][0]["version"] == "1.1"
    assert body["sovd_info"][0]["vendor_info"] == {"version": "0.1.0", "name": "zenode"}


def test_error_bodies_match_upstreams_two_spellings():
    """Entity misses ride in vendor_code; everything else in error_code."""
    assert json.loads(error_body("vendor-specific", "Entity not found: x", "entity-not-found")) == {
        "error_code": "vendor-specific",
        "vendor_code": "entity-not-found",
        "message": "Entity not found: x",
    }
    assert json.loads(error_body("not-responding", "no reply")) == {
        "error_code": "not-responding",
        "message": "no reply",
    }
    assert GenericError.model_fields["error_code"].annotation is str
