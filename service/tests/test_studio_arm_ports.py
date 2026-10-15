"""serving_port_for probes in order and returns at the first match (PRD 0048 C)."""
from __future__ import annotations

import pytest

from service import studio_arm


def _fake(monkeypatch, serving: dict[int, dict]):
    """Install a recording _bridge_health; ports absent from `serving` refuse."""
    calls: list[int] = []

    def fake(port, timeout=studio_arm._LOOPBACK_TIMEOUT_S):
        calls.append(port)
        return serving.get(port)

    monkeypatch.setattr(studio_arm, "_bridge_health", fake)

    def boom(*a, **k):
        raise AssertionError("serving_port_for must not call bound_ports()")

    monkeypatch.setattr(studio_arm, "bound_ports", boom)
    return calls


def test_rearm_with_registry_port_preferred_makes_one_health_call(monkeypatch):
    calls = _fake(monkeypatch, {8770: {"project": "Plant"}, 8768: {"project": "Other"}})
    port = studio_arm.serving_port_for("Plant", 8768, 4, preferred=8770)
    assert port == 8770
    assert calls == [8770]


def test_no_registry_entry_second_port_serving_makes_two_health_calls(monkeypatch):
    calls = _fake(monkeypatch, {8769: {"project": "Plant"}})
    port = studio_arm.serving_port_for("Plant", 8768, 4)
    assert port == 8769
    assert calls == [8768, 8769]


def test_refused_ports_are_not_retried(monkeypatch):
    calls = _fake(monkeypatch, {})
    assert studio_arm.serving_port_for("Plant", 8768, 4, preferred=8770) is None
    assert sorted(calls) == [8768, 8769, 8770, 8771]
    assert len(calls) == len(set(calls))
    assert calls[0] == 8770  # preferred first, then ascending
    assert calls[1:] == [8768, 8769, 8771]


def test_loopback_timeout_is_half_a_second():
    assert studio_arm._LOOPBACK_TIMEOUT_S == 0.5
    import inspect
    sig = inspect.signature(studio_arm._bridge_health)
    assert sig.parameters["timeout"].default == 0.5


def test_bound_ports_uses_short_timeout(monkeypatch):
    seen: list[float] = []

    def fake(port, timeout=studio_arm._LOOPBACK_TIMEOUT_S):
        seen.append(timeout)
        return {"project": "x"} if port == 8769 else None

    monkeypatch.setattr(studio_arm, "_bridge_health", fake)
    assert studio_arm.bound_ports(8768, 4) == {8769}
    assert set(seen) == {0.5}
