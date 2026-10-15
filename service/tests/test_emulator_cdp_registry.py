"""Offline tests: Studio-run-path emulator start, per-project CDP tab registry
and external emulator change detection (ftx-mcp-21b.4)."""
from __future__ import annotations

import pytest

from service import _cdp, core
from service.tests.conftest import FakeProc, make_fake_runner, make_project


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    core.reset_cdp_tab_registry()
    core.reset_emulator_record()
    yield
    core.reset_cdp_tab_registry()
    core.reset_emulator_record()


# ---- emulator start goes through Studio's run path -------------------------

def test_run_emulator_uses_studio_run_path_not_runtime_exe(cfg, projects_root, monkeypatch):
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "audit", lambda *a, **k: None)
    monkeypatch.setattr(core, "attach_mode", lambda c: False)
    monkeypatch.setattr(core, "_emulator_entries", lambda project=None: [])
    monkeypatch.setattr(core, "detect_web_port_clash", lambda *a, **k: None)
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda c, p: object())
    monkeypatch.setattr(core, "_bridge_owner_pid", lambda b, r: 4242)
    monkeypatch.setenv("OPTIX_STUDIO_CONFIG_XML", "/nonexistent/config.xml")

    def boom(*a, **k):
        raise AssertionError("runtime exe must not be launched directly")

    monkeypatch.setattr(core.subprocess, "Popen", boom)
    monkeypatch.setattr(core, "_spawn_runtime", boom, raising=False)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=4242"))
    out = core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    assert out["launched"] is True
    ps = runner.calls[0][0][-1]
    assert "{F5}" in ps and "4242" in ps          # targeted Studio window, run key


# ---- CDP tab registry ------------------------------------------------------

class FakeChrome:
    def __init__(self, pages):
        self.pages = list(pages)
        self.closed: list[str] = []
        self.opened = 0

    def install(self, monkeypatch):
        monkeypatch.setattr(_cdp, "list_pages", lambda url, timeout=5.0: list(self.pages))

        def open_tab(url, page="about:blank", timeout=5.0):
            self.opened += 1
            t = {"id": f"new{self.opened}", "url": "about:blank",
                 "type": "page", "webSocketDebuggerUrl": f"ws://x/new{self.opened}"}
            self.pages.append(t)
            return t

        def close_tab(url, tid, timeout=5.0):
            self.closed.append(tid)
            self.pages = [p for p in self.pages if p["id"] != tid]
            return True

        monkeypatch.setattr(_cdp, "open_tab", open_tab)
        monkeypatch.setattr(_cdp, "close_tab", close_tab)


def _page(i, url):
    return {"id": i, "url": url, "type": "page", "webSocketDebuggerUrl": f"ws://x/{i}"}


def test_cdp_registry_reuses_one_tab_per_project_port(cfg, monkeypatch):
    chrome = FakeChrome([])
    chrome.install(monkeypatch)
    a = core.cdp_tab_for(cfg, "A", 8081)
    b = core.cdp_tab_for(cfg, "a/HMI", 8081)       # same project, same port
    assert a["id"] == b["id"] and b["reused"] is True
    assert chrome.opened == 1
    c = core.cdp_tab_for(cfg, "B", 8088)           # other project -> its own tab
    assert c["id"] != a["id"] and chrome.opened == 2
    assert core.cdp_tab_registry() == {"a:8081": a["id"], "b:8088": c["id"]}


def test_cdp_registry_closes_leaked_tabs_for_port(cfg, monkeypatch):
    chrome = FakeChrome([
        _page("p1", "http://127.0.0.1:8081/"),
        _page("p2", "http://127.0.0.1:8081/#x"),
        _page("p3", "http://127.0.0.1:8081/"),
        _page("other", "http://127.0.0.1:8088/"),
        _page("blank", "about:blank"),
    ])
    chrome.install(monkeypatch)
    tab = core.cdp_tab_for(cfg, "A", 8081)
    assert tab["id"] == "p1"                         # adopted, not duplicated
    assert sorted(chrome.closed) == ["p2", "p3"]     # leaks closed
    assert {p["id"] for p in chrome.pages} == {"p1", "other", "blank"}
    # a later call finds exactly one tab and closes nothing more
    chrome.closed.clear()
    assert core.cdp_tab_for(cfg, "A", 8081)["id"] == "p1"
    assert chrome.closed == []


def test_cdp_registry_forgets_vanished_tab(cfg, monkeypatch):
    chrome = FakeChrome([])
    chrome.install(monkeypatch)
    first = core.cdp_tab_for(cfg, "A", 8081)
    chrome.pages.clear()                              # user closed the tab
    second = core.cdp_tab_for(cfg, "A", 8081)
    assert second["id"] != first["id"] and second["reused"] is False
    assert list(core.cdp_tab_registry().values()) == [second["id"]]


def test_cdp_session_pins_project_tab(cfg, monkeypatch):
    chrome = FakeChrome([])
    chrome.install(monkeypatch)
    monkeypatch.setattr(core, "attach_mode", lambda c: False)
    monkeypatch.setattr(core, "_project_web_port", lambda c, p: 8081)
    seen = {}

    class FakeClient:
        def __init__(self, url, ws_url=None, **kw):
            seen["ws"] = ws_url

        def set_viewport(self, *a, **k):
            return True

    monkeypatch.setattr(_cdp, "CDPClient", FakeClient)
    core._cdp_session(cfg, project="A")
    core._cdp_session(cfg, project="A")
    assert seen["ws"] == "ws://x/new1" and chrome.opened == 1


# ---- external change detection --------------------------------------------

def _entries(monkeypatch, entries):
    monkeypatch.setattr(core, "_emulator_entries",
                        lambda project=None: [dict(e) for e in entries])


def test_external_change_first_call_is_baseline(cfg, monkeypatch):
    _entries(monkeypatch, [{"pid": 1, "project_resolved": "A"}])
    assert core.detect_external_change(cfg) is None


def test_external_start_and_stop_detected_and_record_updated(cfg, monkeypatch):
    monkeypatch.setattr(core, "attach_mode", lambda c: False)
    _entries(monkeypatch, [])
    assert core.detect_external_change(cfg) is None
    _entries(monkeypatch, [{"pid": 7, "project_resolved": "Alpha"}])
    ch = core.detect_external_change(cfg)
    assert ch["external_change"] is True
    assert ch["changes"] == [{"change": "started", "pid": 7, "project": "alpha"}]
    assert core.detect_external_change(cfg) is None      # record was updated
    _entries(monkeypatch, [])
    ch = core.detect_external_change(cfg)
    assert ch["changes"] == [{"change": "stopped", "pid": 7, "project": "alpha"}]


def test_external_project_change_detected(cfg, monkeypatch):
    monkeypatch.setattr(core, "attach_mode", lambda c: False)
    _entries(monkeypatch, [{"pid": 7, "project_resolved": "A"}])
    core.detect_external_change(cfg)
    _entries(monkeypatch, [{"pid": 7, "project_resolved": "B"}])
    ch = core.detect_external_change(cfg)
    assert ch["changes"][0]["change"] == "project_changed"
    assert ch["changes"][0]["from_project"] == "a" and ch["changes"][0]["project"] == "b"


def test_with_external_change_check_flags_result_not_own_effects(cfg, monkeypatch):
    monkeypatch.setattr(core, "attach_mode", lambda c: False)
    live: list[dict] = []
    monkeypatch.setattr(core, "_emulator_entries",
                        lambda project=None: [dict(e) for e in live])
    core.with_external_change_check(cfg, lambda: {"state": "stopped"})   # baseline
    live.append({"pid": 9, "project_resolved": "A"})                      # outside MCP
    out = core.with_external_change_check(cfg, lambda: {"state": "running"})
    assert out["external_change"] is True
    assert out["external_changes"][0]["change"] == "started"
    assert out["state"] == "running"
    # our own stop (inside the call) is not reported on the next call
    def own_stop():
        live.clear()
        return {"stopped": True}
    out = core.with_external_change_check(cfg, own_stop)
    assert "external_change" not in out
    assert "external_change" not in core.with_external_change_check(cfg, lambda: {})


def test_attach_external_change_patches_image_list_shape():
    import json
    change = {"external_change": True, "previous_recorded_at": "t",
              "changes": [{"change": "stopped", "pid": 1, "project": "a"}]}
    out = core.attach_external_change([json.dumps({"state": "succeeded"}), object()], change)
    assert json.loads(out[0])["external_change"] is True


def test_no_background_thread_introduced():
    import inspect
    src = inspect.getsource(core.detect_external_change) + inspect.getsource(core.cdp_tab_for)
    assert "Thread(" not in src and "Timer(" not in src
