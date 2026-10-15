"""Offline tests: web-port parsing/discovery, project-name normalisation and
port clash detection (ftx-mcp-21b.3, PRD 0022 section 9b)."""
from __future__ import annotations

import pytest

from service import core


@pytest.fixture(autouse=True)
def _f5_target_autouse(f5_target_studio) -> None:
    """A Studio is attributed to every project so F5 is reachable (#92 guard)."""


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    core.reset_web_port_cache()
    monkeypatch.setattr(core, "list_bridges", lambda cfg, force=False: [])
    monkeypatch.setattr(core, "_emulator_entries", lambda project=None: [])
    yield
    core.reset_web_port_cache()


def _ports(mapping):
    """Fake discover_web_port: project key -> port."""
    def fake(cfg, project):
        port = mapping.get(core.normalise_project_name(project))
        return {"port": port, "source": "project" if port else "no_engine", "detail": ""}
    return fake


def test_parse_web_port_uint16():
    assert core.parse_web_port("8081 (UInt16)") == 8081
    assert core.parse_web_port('<untrusted source="bridge">8088 (UInt16)</untrusted>') == 8088
    assert core.parse_web_port(" 8081 ") == 8081
    assert core.parse_web_port(8081) == 8081
    assert core.parse_web_port("'8081'") == 8081


@pytest.mark.parametrize("bad", [None, "", "abc", "0", "70000", "-1", True, "80 81"])
def test_parse_web_port_rejects_junk(bad):
    assert core.parse_web_port(bad) is None


def test_yaml_and_bridge_readers_accept_typed_port():
    node = {"properties": [{"name": "Port", "value": "8081 (UInt16)"}]}
    assert core._engine_port_from_bridge_node(node) == 8081


def test_normalise_project_name_x_and_x_hmi_same():
    n = core.normalise_project_name
    assert n("X") == n("X/HMI") == n("x") == n("X/") == n("  X\\ ") == n("x\\hmi\\") == "x"
    assert n("X") != n("Y/HMI")
    assert n(None) == ""


def test_web_port_clash_reported(monkeypatch):
    monkeypatch.setattr(core, "discover_web_port", _ports({"a": 8081, "b": 8081}))
    monkeypatch.setattr(core, "_emulator_entries",
                        lambda project=None: [{"pid": 1, "project_resolved": "B"}])
    clash = core.detect_web_port_clash(None, "A", others=["B"])
    assert clash["error"] == "web_port_taken"
    assert clash["port"] == 8081 and clash["other_project"] == "B"
    assert "8081" in clash["nudge"]


def test_web_port_no_clash_cases(monkeypatch):
    monkeypatch.setattr(core, "discover_web_port",
                        _ports({"a": 8081, "b": 8088, "c": None}))
    assert core.detect_web_port_clash(None, "A", others=["B", "C"]) is None
    # same project named two ways is not a clash with itself
    assert core.detect_web_port_clash(None, "A", others=["a/HMI", "A/"]) is None
    # unknown own port: nothing to compare, no hard-coded default assumed
    assert core.detect_web_port_clash(None, "C", others=["A"]) is None


def test_run_emulator_refuses_clash_before_f5(monkeypatch):
    cfg = core.Config.from_env() if hasattr(core.Config, "from_env") else None
    monkeypatch.setattr(core, "audit", lambda *a, **k: None)
    monkeypatch.setattr(core, "attach_mode", lambda cfg: False)
    monkeypatch.setattr(core, "discover_web_port", _ports({"a": 8081, "b": 8081}))
    monkeypatch.setattr(core, "_clash_candidate_projects", lambda cfg: ["B", "A/HMI"])
    monkeypatch.setattr(core, "_emulator_entries",
                        lambda project=None: [{"pid": 1, "project_resolved": "B"}])

    class Boom:
        def run_powershell(self, *a, **k):
            raise AssertionError("F5 must not be sent on a port clash")

    res = core.run_emulator(cfg, "A", runner=Boom())
    assert res["error"] == "web_port_taken" and res["launched"] is False
    assert res["other_project"] == "B"


def _stop_after_guards(monkeypatch):
    """Make run_emulator raise right after the pre-F5 guards (first step after
    the port-clash check is the bridge lookup)."""
    class Stop(Exception):
        pass

    def stop(*a, **k):
        raise Stop

    monkeypatch.setattr(core, "_bridge_cfg_for", stop)
    return Stop


def test_run_emulator_other_project_running_with_different_port_proceeds(monkeypatch):
    """Project B may run while project A's runtime is up when the web ports
    differ: no runtime_project_mismatch, run proceeds past every guard."""
    cfg = core.Config.from_env()
    monkeypatch.setattr(core, "audit", lambda *a, **k: None)
    monkeypatch.setattr(core, "attach_mode", lambda cfg: False)
    monkeypatch.setattr(core, "_emulator_entries",
                        lambda project=None: [{"pid": 1, "project_resolved": "A"}])
    monkeypatch.setattr(core, "discover_web_port", _ports({"a": 8081, "b": 8082}))
    monkeypatch.setattr(core, "_clash_candidate_projects", lambda cfg: ["A"])
    stop = _stop_after_guards(monkeypatch)
    with pytest.raises(stop):
        core.run_emulator(cfg, "B")


def test_run_emulator_other_project_running_same_port_still_refused(monkeypatch):
    """Equal web ports stay refused (detect_web_port_clash), not as a mismatch."""
    cfg = core.Config.from_env()
    monkeypatch.setattr(core, "audit", lambda *a, **k: None)
    monkeypatch.setattr(core, "attach_mode", lambda cfg: False)
    monkeypatch.setattr(core, "_emulator_entries",
                        lambda project=None: [{"pid": 1, "project_resolved": "A"}])
    monkeypatch.setattr(core, "discover_web_port", _ports({"a": 8081, "b": 8081}))
    monkeypatch.setattr(core, "_clash_candidate_projects", lambda cfg: ["A"])
    _stop_after_guards(monkeypatch)
    res = core.run_emulator(cfg, "B")
    assert res["error"] == "web_port_taken" and res["launched"] is False
    assert res["other_project"] == "A" and res["port"] == 8081


def test_run_emulator_same_project_x_and_x_hmi_is_not_a_clash(monkeypatch):
    cfg = core.Config.from_env()
    monkeypatch.setattr(core, "audit", lambda *a, **k: None)
    monkeypatch.setattr(core, "attach_mode", lambda cfg: False)
    monkeypatch.setattr(core, "_emulator_entries",
                        lambda project=None: [{"pid": 1, "project_resolved": "X"}])
    monkeypatch.setattr(core, "discover_web_port", _ports({"x": 8081}))
    monkeypatch.setattr(core, "_clash_candidate_projects", lambda cfg: ["X"])
    stop = _stop_after_guards(monkeypatch)
    with pytest.raises(stop):
        core.run_emulator(cfg, "x/HMI")


def test_configured_same_port_but_not_running_is_not_a_clash(monkeypatch):
    """#35: two open projects configured for 8081, nothing running/listening."""
    monkeypatch.setattr(core, "discover_web_port", _ports({"a": 8081, "b": 8081}))
    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **k: False)
    assert core.detect_web_port_clash(None, "A", others=["B"]) is None


def test_live_listener_on_port_is_a_clash_without_naming_idle_project(monkeypatch):
    monkeypatch.setattr(core, "discover_web_port", _ports({"a": 8081, "b": 8081}))
    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **k: True)
    clash = core.detect_web_port_clash(None, "A", others=["B"])
    assert clash["error"] == "web_port_taken" and clash["port"] == 8081
    assert clash["other_project"] is None
    assert "'B'" not in clash["nudge"]


def test_clash_names_only_the_running_project(monkeypatch):
    monkeypatch.setattr(core, "discover_web_port",
                        _ports({"a": 8081, "b": 8081, "c": 8081}))
    monkeypatch.setattr(core, "_emulator_entries",
                        lambda project=None: [{"pid": 2, "project_resolved": "C"}])
    clash = core.detect_web_port_clash(None, "A", others=["B", "C"])
    assert clash["other_project"] == "C"
    assert "'B'" not in clash["nudge"]
