"""Acceptance tests for multi-Studio support (ftx-mcp-21b.5).

Consolidated, fake-driven checks of: identity-keyed bridge routing, per-Studio
ALREADY_ARMED, web-port parsing, project-name normalisation, the per-project
CDP tab registry and external emulator change detection. All offline.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from service import _cdp, core, studio_arm


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    core.reset_bridge_cache()
    core.reset_cdp_tab_registry()
    core.reset_emulator_record()
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    yield
    core.reset_bridge_cache()
    core.reset_cdp_tab_registry()
    core.reset_emulator_record()


class FakeBridge:
    """A fake Studio bridge: serves /bridge/health and records other calls."""

    def __init__(self, port: int, project: str, studio_pid: int) -> None:
        self.port = port
        self.project = project
        self.studio_pid = studio_pid
        self.calls: list[str] = []

    def health(self) -> dict:
        return {"model_loaded": True, "bridge_version": "1.0.9", "port": self.port,
                "project": self.project, "project_name": self.project,
                "studio_pid": self.studio_pid, "pid": self.studio_pid}


def _install_bridges(monkeypatch, *bridges: FakeBridge) -> None:
    by_port = {b.port: b for b in bridges}

    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        port = urlparse(cfg.bridge_url).port
        if port not in by_port:
            raise core.BridgeUnavailable(f"nothing on {port}") from ConnectionRefusedError()
        b = by_port[port]
        if path.startswith("/bridge/health"):
            return 200, json.dumps(b.health()).encode()
        b.calls.append(path)
        return 200, json.dumps({"path": "UI", "children": []}).encode()

    monkeypatch.setattr(core, "_bridge_http", fake)


def test_two_fake_bridges_and_two_projects_route_each_call_to_correct_bridge(
        multi_bridge_cfg, monkeypatch) -> None:
    # Beta sits on the lower port: port order must not decide routing.
    beta = FakeBridge(8768, "Beta", 200)
    alpha = FakeBridge(8769, "Alpha", 100)
    _install_bridges(monkeypatch, beta, alpha)

    assert core.resolve_bridge(multi_bridge_cfg, "Alpha")["port"] == 8769
    assert core.resolve_bridge(multi_bridge_cfg, "Beta")["port"] == 8768

    core.describe_node(multi_bridge_cfg, "Alpha", "UI")
    core.describe_node(multi_bridge_cfg, "Beta", "UI")
    core.describe_node(multi_bridge_cfg, "Alpha", "UI")
    assert len(alpha.calls) == 2 and len(beta.calls) == 1

    # A project nobody serves is an error, never a guess at "some" bridge.
    with pytest.raises(core.NoBridgeForProject):
        core.resolve_bridge(multi_bridge_cfg, "Gamma")
    assert len(alpha.calls) == 2 and len(beta.calls) == 1


def test_already_armed_is_never_reported_for_another_studios_bridge(monkeypatch) -> None:
    bridges = {8768: {"project": "Plant", "studio_pid": 111, "pid": 111,
                      "project_name": "Plant", "project_dir": r"C:\work\A\Plant"}}
    monkeypatch.setattr(studio_arm, "bound_ports", lambda *a, **k: set(bridges))
    monkeypatch.setattr(studio_arm, "_bridge_health",
                        lambda p, timeout=3.0: bridges.get(p))

    # Studio 111 owns the bridge on 8768 ...
    assert studio_arm.serving_port_for("Plant", studio_pid=111) == 8768
    # ... but Studio 222 (same project name) has none of its own.
    assert studio_arm.serving_port_for("Plant", studio_pid=222) is None
    monkeypatch.setattr(studio_arm, "studio_pid_for", lambda *a, **k: 222)
    out = studio_arm.execute_method("Plant", "/nonexistent", method="StartBridge")
    assert out.get("state") != "already_armed"
    assert out.get("port") != 8768


def test_8081_uint16_parses_to_8081() -> None:
    assert core.parse_web_port("8081 (UInt16)") == 8081
    assert core.parse_web_port("not a port") is None


def test_x_and_x_hmi_normalise_to_the_same_project() -> None:
    n = core.normalise_project_name
    assert n("X") == n("X/HMI")
    assert n("X") != n("Y/HMI")


# ---- CDP registry ----------------------------------------------------------

class FakeChrome:
    def __init__(self, pages=()) -> None:
        self.pages = list(pages)
        self.closed: list[str] = []
        self.opened = 0

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(_cdp, "list_pages", lambda url, timeout=5.0: list(self.pages))

        def open_tab(url, page="about:blank", timeout=5.0):
            self.opened += 1
            t = {"id": f"new{self.opened}", "url": "about:blank", "type": "page",
                 "webSocketDebuggerUrl": f"ws://x/new{self.opened}"}
            self.pages.append(t)
            return t

        def close_tab(url, tid, timeout=5.0):
            self.closed.append(tid)
            self.pages = [p for p in self.pages if p["id"] != tid]
            return True

        monkeypatch.setattr(_cdp, "open_tab", open_tab)
        monkeypatch.setattr(_cdp, "close_tab", close_tab)


def _page(i: str, url: str) -> dict:
    return {"id": i, "url": url, "type": "page", "webSocketDebuggerUrl": f"ws://x/{i}"}


def test_cdp_registry_reuses_one_tab_and_closes_leaked_tabs(cfg, monkeypatch) -> None:
    chrome = FakeChrome([
        _page("p1", "http://127.0.0.1:8081/"),
        _page("p2", "http://127.0.0.1:8081/#x"),
        _page("p3", "http://127.0.0.1:8081/"),
        _page("other", "http://127.0.0.1:8088/"),
    ])
    chrome.install(monkeypatch)

    first = core.cdp_tab_for(cfg, "A", 8081)
    assert first["id"] == "p1"                      # adopted, no new tab opened
    assert chrome.opened == 0
    assert sorted(chrome.closed) == ["p2", "p3"]    # leaked tabs closed
    assert {p["id"] for p in chrome.pages} == {"p1", "other"}

    chrome.closed.clear()
    again = core.cdp_tab_for(cfg, "A/HMI", 8081)    # same project, other spelling
    assert again["id"] == "p1" and again["reused"] is True
    assert chrome.opened == 0 and chrome.closed == []


# ---- external change -------------------------------------------------------

def test_external_emulator_stop_is_detected_and_reported_with_external_change_true(
        cfg, monkeypatch) -> None:
    monkeypatch.setattr(core, "attach_mode", lambda c: False)
    live: list[dict] = [{"pid": 7, "project_resolved": "Alpha"}]
    monkeypatch.setattr(core, "_emulator_entries",
                        lambda project=None: [dict(e) for e in live])

    # Baseline: emulator running.
    core.with_external_change_check(cfg, lambda: {"state": "running"})

    live.clear()                                    # stopped outside MCP
    out = core.with_external_change_check(cfg, lambda: {"state": "idle"})
    assert out["external_change"] is True
    assert out["external_changes"] == [{"change": "stopped", "pid": 7, "project": "alpha"}]
    assert out["state"] == "idle"

    # Reported once only.
    assert "external_change" not in core.with_external_change_check(cfg, lambda: {})
