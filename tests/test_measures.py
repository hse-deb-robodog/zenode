"""``@metric``: declaration and collection — the declarative layer.

Sampling (what a ``None``, non-finite or raising measurement costs) is the
reporter's behavior, tested in ``test_reporting.py``; a value reaching a real
heartbeat is in ``test_integration.py``. Everything here is in-process and
needs no session.
"""

from __future__ import annotations

from typing import Any

import pytest

from zenode import ContractError, Node, metric
from zenode.declarative import collect_metrics
from zenode.msgs import NodeHealth

# ------------------------------------------------------------------ decoration


def test_a_measurement_stays_directly_callable():
    """The whole point of stamping metadata: no node, no session, just a call."""

    class Nav(Node):
        name = "nav"

        @metric("battery_soc", unit="1")
        def _soc(self) -> float:
            return 0.87

    assert Nav()._soc() == 0.87


@pytest.mark.parametrize(
    "bad", ["Battery-SOC", "battery-soc", "1st", "_soc", "battery soc", "", "batterySoc"]
)
def test_an_id_that_is_not_a_metric_name_is_refused(bad):
    with pytest.raises(ContractError, match="id must match"):
        metric(bad)


def test_a_nodehealth_field_name_is_refused():
    """Two things called `uptime_s`, one computed by the runtime, is undebuggable."""
    with pytest.raises(ContractError, match="NodeHealth field"):
        metric("uptime_s")


def test_every_nodehealth_field_is_refused():
    for field in NodeHealth.model_fields:
        with pytest.raises(ContractError):
            metric(field)


def test_an_unknown_kind_is_refused():
    """Spelled as a string, so an untyped caller's typo has to fail somewhere."""
    bad_kind: Any = "histogram"
    with pytest.raises(ContractError, match="kind must be one of"):
        metric("x", kind=bad_kind)


def test_stacking_two_metrics_on_one_method_is_refused():
    """One callable produces one number; the second declaration could only be lost."""
    with pytest.raises(ContractError, match="already declares"):

        @metric("a")
        @metric("b")
        def _both(self) -> float:
            return 1.0


def test_two_attributes_of_one_class_cannot_share_an_id():
    with pytest.raises(ContractError, match="both declare"):

        class Nav(Node):
            name = "nav"

            @metric("x")
            def _one(self) -> float:
                return 1.0

            @metric("x")
            def _two(self) -> float:
                return 2.0


def test_a_duplicate_id_fails_at_class_definition_not_at_the_first_heartbeat():
    """`Node.__init_subclass__` is the guard, so the traceback names the import."""
    with pytest.raises(ContractError):
        type(
            "Broken",
            (Node,),
            {
                "name": "broken",
                "a": metric("dup")(lambda self: 1.0),
                "b": metric("dup")(lambda self: 2.0),
            },
        )


# ------------------------------------------------------------------ collection


class Base(Node):
    name = "base"

    @metric("battery_soc", unit="1", description="Pack state of charge.")
    def _soc(self) -> float:
        return 0.5

    @metric("frames_processed", kind="counter", integral=True)
    def _frames(self) -> int:
        return 7


def test_collection_is_keyed_by_id_and_carries_the_attribute():
    metrics = collect_metrics(Base)
    assert set(metrics) == {"battery_soc", "frames_processed"}
    assert metrics["battery_soc"].attr == "_soc"
    assert metrics["battery_soc"].unit == "1"
    assert metrics["frames_processed"].kind == "counter"
    assert metrics["frames_processed"].integral is True


def test_an_undecorated_override_inherits_the_declaration():
    class Child(Base):
        name = "child"

        def _soc(self) -> float:  # no decorator
            return 0.9

    metrics = collect_metrics(Child)
    assert metrics["battery_soc"].attr == "_soc"
    assert Child()._soc() == 0.9


def test_a_subclass_replaces_a_parents_measurement_by_redeclaring_its_id():
    class Child(Base):
        name = "child"

        @metric("battery_soc", unit="1", description="From the BMS instead.")
        def _soc_from_bms(self) -> float:
            return 0.31

    metrics = collect_metrics(Child)
    assert metrics["battery_soc"].attr == "_soc_from_bms"
    assert metrics["battery_soc"].description == "From the BMS instead."
    assert len([m for m in metrics.values() if m.id == "battery_soc"]) == 1


def test_redecorating_an_attribute_with_a_new_id_drops_the_old_one():
    """Otherwise the inherited id survives, still pointing at the same method."""

    class Child(Base):
        name = "child"

        @metric("pack_soc")
        def _soc(self) -> float:
            return 0.4

    metrics = collect_metrics(Child)
    assert "battery_soc" not in metrics
    assert metrics["pack_soc"].attr == "_soc"


def test_a_node_with_no_measurements_collects_nothing():
    class Plain(Node):
        name = "plain"

    assert collect_metrics(Plain) == {}
