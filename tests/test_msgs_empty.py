"""`Empty`: the request of a service that takes no arguments.

It exists so `Service(..., diagnostic="read")` has a canonical request type,
and it forbids extras so a caller cannot smuggle arguments into a read.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zenode.msgs import Empty


def test_empty_has_no_fields():
    assert Empty.model_fields == {}
    assert Empty().model_dump() == {}


def test_empty_rejects_arguments():
    """A read takes no arguments; one that arrives anyway is a caller bug, not a no-op."""
    with pytest.raises(ValidationError):
        Empty.model_validate({"axis": 1})


def test_empty_round_trips_as_an_empty_object():
    assert Empty.model_validate_json(b"{}") == Empty()
    assert Empty().model_dump_json() == "{}"
