from typing import Any

import pytest
from pydantic import BaseModel

from zenode import (
    Service,
    Topic,
    TopicSet,
    find_topic,
    registered_entries,
    registered_services,
    registered_topics,
)
from zenode.errors import ContractError
from zenode.topic import PRIORITIES, validate_namespace, validate_node_name


class Msg(BaseModel):
    value: int = 0


def test_resolve_with_namespace():
    t = Topic("state/odometry", Msg)
    assert t.resolve("") == "state/odometry"
    assert t.resolve("robodog") == "robodog/state/odometry"


def test_absolute_ignores_namespace():
    t = Topic.absolute("livox/lidar", bytes)
    assert t.resolve("robodog") == "livox/lidar"


@pytest.mark.parametrize("bad", ["", "/lead", "trail/", "a//b", "a b"])
def test_key_validation(bad):
    with pytest.raises(ContractError):
        Topic(bad, Msg)


def test_semantics_validation():
    with pytest.raises(ContractError):
        Topic("k", Msg, max_age=0)
    with pytest.raises(ContractError):
        Topic("k", Msg, latched=True, history=0)


def test_topicset_registers():
    class DemoSet(TopicSet):
        ping = Topic("demo/registry/ping", Msg)
        svc = Service("demo/registry/svc", request=Msg, reply=Msg)

    assert find_topic("demo/registry/ping") is DemoSet.ping
    assert find_topic("ns/demo/registry/ping", "ns") is DemoSet.ping
    assert find_topic("demo/registry/missing") is None


def test_the_registry_can_be_filtered_by_owner(isolated_registry):
    """The registry is process-global; a contract test wants only its own."""

    class MineTopics(TopicSet):
        ping = Topic("mine/ping", Msg)
        svc = Service("mine/svc", request=Msg, reply=Msg)

    class TheirsTopics(TopicSet):
        ping = Topic("theirs/ping", Msg)

    owner = f"{__name__}.{MineTopics.__qualname__}"

    assert [e.attr for e in registered_entries(owner)] == ["ping", "svc"]
    assert [t.key for _, t in registered_topics(owner)] == ["mine/ping"]
    assert [s.key for _, s in registered_services(owner)] == ["mine/svc"]
    assert {t.key for _, t in registered_topics()} == {"mine/ping", TheirsTopics.ping.key}


# ---------------------------------------------------------------- trace_ratio


def test_trace_ratio_defaults_to_everything():
    assert Topic("t/a", Msg, trace=True).trace_ratio == 1.0


def test_trace_ratio_rejects_out_of_range():
    with pytest.raises(ContractError, match=r"between 0\.0 and 1\.0"):
        Topic("t/b", Msg, trace=True, trace_ratio=1.5)
    with pytest.raises(ContractError, match=r"between 0\.0 and 1\.0"):
        Topic("t/c", Msg, trace=True, trace_ratio=-0.1)


def test_trace_ratio_without_a_root_is_an_error():
    """It would silently do nothing: a non-root never starts a trace to sample."""
    with pytest.raises(ContractError, match="no effect without trace=True"):
        Topic("t/d", Msg, trace_ratio=0.1)


# ------------------------------------------------------------------------ QoS


def test_qos_defaults_match_zenoh():
    t = Topic("q/a", Msg)
    assert (t.priority, t.congestion_control, t.express) == ("data", "drop", False)


def test_qos_accepts_every_priority_band():
    for band in PRIORITIES:
        assert Topic("q/b", Msg, priority=band).priority == band


def test_qos_rejects_an_unknown_priority():
    """A type checker catches this statically; the guard is for whoever has none."""
    typo: Any = "realtime"
    with pytest.raises(ContractError, match="priority must be one of"):
        Topic("q/c", Msg, priority=typo)


def test_qos_rejects_an_unknown_congestion_control():
    """`block_first` is real in zenoh but flagged unstable, so zenode does not expose it."""
    unsupported: Any = "block_first"
    with pytest.raises(ContractError, match="congestion_control must be one of"):
        Topic("q/d", Msg, congestion_control=unsupported)


# --------------------------------------------------------------------- diagnostic


class _NoFields(BaseModel):
    pass


class _WithField(BaseModel):
    axis: int = 0


def test_services_are_not_diagnosable_by_default():
    """Exposure to a diagnostic client is a deliberate act, so the default is off."""
    svc = Service("state/get_pose", request=_NoFields, reply=Msg)
    assert svc.diagnostic is None


def test_a_read_needs_a_request_without_fields():
    """A SOVD data resource is a GET with no body; a read with arguments has nowhere to put them."""
    Service("state/get_pose", request=_NoFields, reply=Msg, diagnostic="read")
    with pytest.raises(ContractError, match="no fields"):
        Service("state/get_pose", request=_WithField, reply=Msg, diagnostic="read")


def test_a_read_rejects_a_raw_request():
    """``bytes`` has no field list to be empty."""
    with pytest.raises(ContractError, match="no fields"):
        Service("state/blob", request=bytes, reply=bytes, diagnostic="read")


def test_an_operation_may_take_arguments():
    svc = Service("motion/home", request=_WithField, reply=Msg, diagnostic="operation")
    assert svc.diagnostic == "operation"


def test_an_unknown_diagnostic_is_a_contract_error():
    bad: Any = "write"  # the Literal is for checkers; the dataclass still guards at runtime
    with pytest.raises(ContractError, match="diagnostic"):
        Service("x/y", request=_NoFields, reply=Msg, diagnostic=bad)


# --------------------------------------------------------------- names and namespaces


@pytest.mark.parametrize("name", ["nav", "arm-left", "cam.front", "Nav_2", "zenode-test-probe"])
def test_good_node_names(name):
    validate_node_name(name, what="Node")


@pytest.mark.parametrize("name", ["", "arm/left", "nav*", "a b", "nav?", "nav#1", "nav%", "näv"])
def test_bad_node_names(name):
    """A name becomes a key segment and, for diagnostics, a URL path segment."""
    with pytest.raises(ContractError, match="name"):
        validate_node_name(name, what="Node")


@pytest.mark.parametrize("namespace", ["", "robodog", "fleet/robot1", "a.b/c-d"])
def test_good_namespaces(namespace):
    validate_namespace(namespace, what="transport")


@pytest.mark.parametrize("namespace", ["/robodog", "robodog/", "a//b", "a b", "fleet/*", "ns?"])
def test_bad_namespaces(namespace):
    with pytest.raises(ContractError, match="namespace"):
        validate_namespace(namespace, what="transport")
