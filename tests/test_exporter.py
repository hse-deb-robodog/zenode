"""NodeHealth re-served as Prometheus metrics."""

import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from zenode.exporter import Registry, Sample, make_server, render
from zenode.msgs.health import NodeHealth
from zenode.msgs.info import MeasureDescriptor, NodeInfo


def _catalog(node: str = "camera", **measures: dict[str, Any]) -> NodeInfo:
    """A node's descriptor, from ``{id: {kind, unit, ...}}``."""
    return NodeInfo(
        node=node,
        measures=[MeasureDescriptor(id=name, **spec) for name, spec in measures.items()],
    )


def _health(node: str = "camera", **kwargs: Any) -> NodeHealth:
    # Built through model_validate rather than the constructor: a **kwargs
    # spread widens every field to one union, which a checker cannot reconcile
    # with `state`'s Literal. Validation still runs, so a typo still fails.
    return NodeHealth.model_validate(
        {"node": node, "state": "running", "uptime_s": 6.0, "ts_ns": 1, **kwargs}
    )


def _lines(text: str, prefix: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(prefix)]


@pytest.fixture
def clock(monkeypatch) -> dict[str, float]:
    """A hand-cranked monotonic clock, for aging a registry's samples."""
    fake = {"now": 10.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["now"])
    return fake


def _value(text: str, prefix: str) -> str:
    matches = _lines(text, prefix)
    assert len(matches) == 1, f"expected one {prefix!r}, got {matches}"
    return matches[0].rsplit(" ", 1)[1]


# -------------------------------------------------------------------- format


def test_counters_and_gauges_are_typed():
    text = render({"camera": Sample(_health(sent=177), 100.0)}, "", now=100.0)
    assert "# TYPE zenode_node_sent_total counter" in text
    assert "# TYPE zenode_node_uptime_seconds gauge" in text
    assert "# HELP zenode_node_sent_total Messages published." in text


def test_labels_carry_node_and_namespace():
    text = render({"camera": Sample(_health(sent=3), 100.0)}, "robodog", now=100.0)
    assert 'zenode_node_sent_total{namespace="robodog",node="camera"} 3' in text


def test_deadline_misses_are_exported():
    """On the heartbeat since the start, in the exporter's table since now."""
    text = render({"camera": Sample(_health(deadline_misses=4), 100.0)}, "robodog", now=100.0)
    assert "# TYPE zenode_node_deadline_misses_total counter" in text
    assert 'zenode_node_deadline_misses_total{namespace="robodog",node="camera"} 4' in text


def test_milliseconds_become_seconds():
    """Prometheus convention is base units; NodeHealth reports milliseconds."""
    text = render({"camera": Sample(_health(age_max_ms=121.5), 100.0)}, "", now=100.0)
    assert _value(text, "zenode_node_message_age_max_seconds") == "0.1215"


def test_unknown_resources_are_omitted_not_zeroed():
    """A node with no /proc has unknown CPU, and unknown is not idle."""
    text = render(
        {"camera": Sample(_health(cpu_percent=None, rss_bytes=None), 100.0)}, "", now=100.0
    )
    assert _lines(text, "zenode_node_cpu_percent") == []
    assert _lines(text, "zenode_node_rss_bytes") == []

    text = render(
        {"camera": Sample(_health(cpu_percent=61.8, rss_bytes=4096), 100.0)}, "", now=100.0
    )
    assert _value(text, "zenode_node_cpu_percent") == "61.8"
    assert _value(text, "zenode_node_rss_bytes") == "4096"


def test_state_rides_on_the_info_metric():
    text = render({"camera": Sample(_health(state="stopping"), 100.0)}, "", now=100.0)
    assert 'zenode_node_info{namespace="",node="camera",state="stopping"} 1' in text


def test_last_seen_grows_with_silence():
    text = render({"camera": Sample(_health(), 100.0)}, "", now=104.5)
    assert _value(text, "zenode_node_last_seen_seconds") == "4.5"


def test_stale_nodes_are_dropped_entirely(clock):
    """Frozen counters read as a healthy node doing nothing; absence does not."""
    registry = Registry("", stale_after=60.0)
    registry.offer(_health("gone").model_dump_json().encode())
    clock["now"] = 100.0
    registry.offer(_health("camera").model_dump_json().encode())
    text = registry.render()
    assert 'node="camera"' in text
    assert 'node="gone"' not in text


def test_every_node_appears_under_every_metric():
    samples = {
        "camera": Sample(_health("camera", sent=1), 100.0),
        "motors": Sample(_health("motors", sent=2), 100.0),
    }
    text = render(samples, "", now=100.0)
    assert len(_lines(text, "zenode_node_sent_total{")) == 2


def test_label_values_are_escaped():
    text = render({'we"ird': Sample(_health('we"ird'), 100.0)}, "", now=100.0)
    assert r'node="we\"ird"' in text


def test_empty_registry_still_renders():
    assert render({}, "", now=100.0).endswith("\n")


def test_host_becomes_a_label():
    text = render({"camera": Sample(_health(sent=3, host="jetson"), 100.0)}, "robodog", now=100.0)
    assert 'zenode_node_sent_total{host="jetson",namespace="robodog",node="camera"} 3' in text


def test_an_unknown_host_is_omitted_rather_than_labelled_empty():
    """An older node publishes no host, and `host=""` would read as one."""
    text = render({"camera": Sample(_health(sent=3), 100.0)}, "robodog", now=100.0)
    assert 'zenode_node_sent_total{namespace="robodog",node="camera"} 3' in text
    assert 'host=""' not in text


def test_the_host_label_reaches_the_info_and_last_seen_series_too():
    text = render({"camera": Sample(_health(host="jetson"), 100.0)}, "", now=100.0)
    assert 'zenode_node_info{host="jetson",namespace="",node="camera",state="running"} 1' in text
    assert 'zenode_node_last_seen_seconds{host="jetson",namespace="",node="camera"}' in text


# ----------------------------------------------------- application measurements


def test_app_measurements_take_their_type_and_help_from_the_catalog():
    text = render(
        {"camera": Sample(_health(measures={"frames_processed": 1204.0}), 100.0)},
        "",
        now=100.0,
        catalogs={
            "camera": _catalog(
                frames_processed={
                    "kind": "counter",
                    "unit": "{frame}",
                    "description": "Frames handled.",
                }
            )
        },
    )
    assert "# TYPE zenode_app_frames_processed_total counter" in text
    assert "# HELP zenode_app_frames_processed_total Frames handled. [{frame}]" in text
    assert 'zenode_app_frames_processed_total{namespace="",node="camera"} 1204' in text


def test_a_gauge_measurement_carries_no_total_suffix():
    text = render(
        {"camera": Sample(_health(measures={"battery_soc": 0.87}, host="jetson"), 100.0)},
        "robodog",
        now=100.0,
        catalogs={"camera": _catalog(battery_soc={"unit": "1"})},
    )
    assert "# TYPE zenode_app_battery_soc gauge" in text
    assert 'zenode_app_battery_soc{host="jetson",namespace="robodog",node="camera"} 0.87' in text


def test_a_value_without_a_descriptor_contributes_no_series():
    """A series whose TYPE flips mid-history is worse than a transient hole."""
    text = render({"camera": Sample(_health(measures={"battery_soc": 0.87}), 100.0)}, "", now=100.0)
    assert _lines(text, "zenode_app_") == []


def test_a_node_whose_own_catalog_has_not_arrived_is_left_out_of_a_shared_series():
    samples = {
        "camera": Sample(_health("camera", measures={"battery_soc": 0.5}), 100.0),
        "motors": Sample(_health("motors", measures={"battery_soc": 0.6}), 100.0),
    }
    text = render(samples, "", now=100.0, catalogs={"camera": _catalog(battery_soc={})})
    assert len(_lines(text, "zenode_app_battery_soc{")) == 1
    assert 'node="camera"' in _lines(text, "zenode_app_battery_soc{")[0]


def test_two_kinds_for_one_id_export_the_first_node_in_sorted_order():
    """One Prometheus name cannot carry two types; the rule must be stable."""
    samples = {
        "camera": Sample(_health("camera", measures={"widgets": 1.0}), 100.0),
        "motors": Sample(_health("motors", measures={"widgets": 2.0}), 100.0),
    }
    catalogs = {
        "camera": _catalog("camera", widgets={"kind": "gauge"}),
        "motors": _catalog("motors", widgets={"kind": "counter"}),
    }
    text = render(samples, "", now=100.0, catalogs=catalogs)
    assert "# TYPE zenode_app_widgets gauge" in text
    assert _lines(text, "zenode_app_widgets_total") == []
    assert len(_lines(text, "zenode_app_widgets{")) == 1
    assert 'node="camera"' in _lines(text, "zenode_app_widgets{")[0]


def test_a_kind_conflict_is_logged_once_however_often_the_registry_is_read(caplog):
    """A deployment mistake is not news on every 15-second scrape or push."""
    registry = Registry("")
    registry.offer(_health("camera", measures={"widgets": 1.0}).model_dump_json().encode())
    registry.offer(_health("motors", measures={"widgets": 2.0}).model_dump_json().encode())
    registry.offer_info(_catalog("camera", widgets={"kind": "gauge"}).model_dump_json().encode())
    registry.offer_info(_catalog("motors", widgets={"kind": "counter"}).model_dump_json().encode())
    with caplog.at_level("WARNING", logger="zenode.exporter"):
        text = registry.render()  # a scrape
        registry.snapshot()  # what a push reads

    assert "# TYPE zenode_app_widgets gauge" in text
    assert len([r for r in caplog.records if "widgets" in r.getMessage()]) == 1


def test_a_measurement_absent_from_this_heartbeat_is_simply_not_reported():
    """`None` from a measurement means unknown, and unknown emits no point."""
    text = render(
        {"camera": Sample(_health(), 100.0)},
        "",
        now=100.0,
        catalogs={"camera": _catalog(battery_soc={})},
    )
    assert _lines(text, "zenode_app_") == []


def test_a_stale_node_takes_its_app_series_with_it(clock):
    registry = Registry("", stale_after=60.0)
    registry.offer(_health("gone", measures={"battery_soc": 0.5}).model_dump_json().encode())
    registry.offer_info(_catalog("gone", battery_soc={}).model_dump_json().encode())
    clock["now"] = 100.0
    assert _lines(registry.render(), "zenode_app_") == []


def test_a_dead_nodes_descriptor_cannot_win_a_kind_conflict(clock, caplog):
    """`offer_info` keeps a descriptor after its node dies; conflicts must not.

    Otherwise a node decommissioned an hour ago takes a live node's series off
    `/metrics` while the push path — which reads the same snapshot — carries it
    normally, and one registry gives two answers.
    """
    registry = Registry("", stale_after=60.0)
    registry.offer(_health("alpha", measures={"widgets": 1.0}).model_dump_json().encode())
    registry.offer_info(_catalog("alpha", widgets={"kind": "counter"}).model_dump_json().encode())
    clock["now"] = 100.0  # alpha is now stale …
    registry.offer(_health("zulu", measures={"widgets": 2.0}).model_dump_json().encode())
    registry.offer_info(_catalog("zulu", widgets={"kind": "gauge"}).model_dump_json().encode())

    with caplog.at_level("WARNING", logger="zenode.exporter"):
        text = registry.render()
    assert "# TYPE zenode_app_widgets gauge" in text
    assert 'zenode_app_widgets{namespace="",node="zulu"} 2' in text
    # …and the once-only warning is not burned by a node nobody can see.
    assert [r for r in caplog.records if "widgets" in r.getMessage()] == []


def test_a_counter_past_a_million_is_rendered_exactly():
    """`%g` alone would emit 1.20457e+06, which is not a count any more."""
    text = render(
        {"camera": Sample(_health(measures={"frames": 1204567.0}), 100.0)},
        "",
        now=100.0,
        catalogs={"camera": _catalog(frames={"kind": "counter"})},
    )
    assert 'zenode_app_frames_total{namespace="",node="camera"} 1204567' in text


# ------------------------------------------------------------------ registry


def test_registry_keeps_the_newest_per_node():
    registry = Registry("")
    registry.offer(_health(sent=1).model_dump_json().encode())
    registry.offer(_health(sent=9).model_dump_json().encode())
    assert len(registry) == 1
    assert _value(registry.render(), "zenode_node_sent_total") == "9"


def test_registry_ignores_foreign_payloads():
    """A key expression is not a promise about what is published on it."""
    registry = Registry("")
    registry.offer(b"not json")
    registry.offer(b'{"unrelated": true}')
    assert len(registry) == 0


def test_registry_ignores_foreign_payloads_on_the_info_key_too():
    registry = Registry("")
    registry.offer_info(b"not json")
    registry.offer_info(b'{"unrelated": true}')
    assert registry.snapshot() == ({}, {})


def test_registry_joins_a_catalog_to_its_nodes_heartbeat():
    registry = Registry("")
    registry.offer(_health(measures={"battery_soc": 0.87}).model_dump_json().encode())
    registry.offer_info(_catalog(battery_soc={"unit": "1"}).model_dump_json().encode())

    samples, catalogs = registry.snapshot()
    assert set(samples) == {"camera"} and set(catalogs) == {"camera"}
    assert 'zenode_app_battery_soc{namespace="",node="camera"} 0.87' in registry.render()


def test_a_snapshot_reports_no_catalog_for_a_node_it_dropped_as_stale():
    """A scrape and a push must agree on which nodes exist, and on their types."""
    registry = Registry("", stale_after=0.0)
    registry.offer(_health().model_dump_json().encode())
    registry.offer_info(_catalog().model_dump_json().encode())
    time.sleep(0.01)
    assert registry.snapshot() == ({}, {})


# ---------------------------------------------------------------------- http


@pytest.fixture
def served() -> Iterator[tuple[str, Registry]]:
    registry = Registry("robodog")
    server = make_server(registry, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", registry
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_metrics_endpoint_serves_the_registry(served):
    base, registry = served
    registry.offer(_health(sent=42).model_dump_json().encode())

    with urllib.request.urlopen(f"{base}/metrics", timeout=5) as response:
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/plain")
        body = response.read().decode()

    assert 'zenode_node_sent_total{namespace="robodog",node="camera"} 42' in body


def test_unknown_paths_are_404(served):
    base, _ = served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base}/nope", timeout=5)
    assert excinfo.value.code == 404


def test_floats_are_not_full_precision():
    """Uptime as `6.0023076990000845` is bytes on every scrape for nothing."""
    text = render({"camera": Sample(_health(uptime_s=6.0023076990000845), 100.0)}, "", now=100.0)
    assert _value(text, "zenode_node_uptime_seconds") == "6.00231"


# ------------------------------------------------------------- self counters


def test_self_counters_are_absent_by_default():
    assert "zenode_exporter" not in render({}, "", now=100.0)


def test_self_counters_are_typed_and_labelled_by_outcome():
    text = render(
        {},
        "",
        now=100.0,
        self_stats={"log_records": {"shipped": 12, "queue_full": 3, "push_failed": 1}},
    )
    assert "# TYPE zenode_exporter_log_records_total counter" in text
    assert 'zenode_exporter_log_records_total{outcome="shipped"} 12' in text
    assert 'zenode_exporter_log_records_total{outcome="queue_full"} 3' in text
    assert 'zenode_exporter_log_records_total{outcome="push_failed"} 1' in text


def test_self_counters_accompany_node_series():
    """A broken sidecar must be visible on the same scrape as the nodes."""
    text = render(
        {"camera": Sample(_health(sent=1), 100.0)},
        "",
        now=100.0,
        self_stats={"metric_pushes": {"ok": 4, "failed": 2}},
    )
    assert 'zenode_node_sent_total{namespace="",node="camera"} 1' in text
    assert 'zenode_exporter_metric_pushes_total{outcome="failed"} 2' in text


def test_registry_publishes_the_configured_self_stats():
    registry = Registry("")
    registry.self_stats = lambda: {"metric_pushes": {"ok": 7, "failed": 0}}
    assert 'zenode_exporter_metric_pushes_total{outcome="ok"} 7' in registry.render()
