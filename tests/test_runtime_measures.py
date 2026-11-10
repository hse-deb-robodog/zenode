"""The heartbeat's own fields, described once and shared by every consumer.

`RUNTIME_MEASURES` is the catalog `NodeInfo.runtime` publishes and the table
the exporter renders from. One source, so the Prometheus name, the OTLP name,
and what a diagnostic client shows cannot disagree about what a field means.
"""

from __future__ import annotations

from zenode.exporter import COUNTERS, GAUGES
from zenode.msgs import NodeHealth
from zenode.msgs.health import RUNTIME_BY_ID, RUNTIME_MEASURES

_NOT_MEASURES = {"node", "host", "state", "measures", "ts_ns"}


def test_every_numeric_health_field_is_described():
    """A field nobody described is a field nobody exports — deadline_misses was one."""
    numeric = set(NodeHealth.model_fields) - _NOT_MEASURES
    assert {d.id for d in RUNTIME_MEASURES} == numeric


def test_ids_are_unique_and_indexable():
    assert len(RUNTIME_BY_ID) == len(RUNTIME_MEASURES)


def test_counters_are_counters_and_integral():
    for id_ in (
        "sent",
        "received",
        "dropped",
        "stale",
        "handler_errors",
        "timer_overruns",
        "deadline_misses",
        "logs_dropped",
        "shm_fallbacks",
    ):
        assert RUNTIME_BY_ID[id_].kind == "counter", id_
        assert RUNTIME_BY_ID[id_].integral, id_


def test_every_descriptor_has_a_description_and_a_unit():
    for d in RUNTIME_MEASURES:
        assert d.description, d.id
        assert d.unit, d.id


def test_the_exporter_table_covers_the_catalog_exactly_once():
    """The exporter renders *from* the catalog; a row without a descriptor is a second table."""
    exported = [m.descriptor.id for m in (*COUNTERS, *GAUGES)]
    assert sorted(exported) == sorted(d.id for d in RUNTIME_MEASURES)


def test_exporter_rows_take_kind_and_help_from_the_descriptor():
    by_name = {m.name: m for m in (*COUNTERS, *GAUGES)}
    assert by_name["sent_total"].kind == "counter"
    assert by_name["sent_total"].help == "Messages published."
    assert by_name["sent_total"].integral is True
    assert by_name["uptime_seconds"].kind == "gauge"
