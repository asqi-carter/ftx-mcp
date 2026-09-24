"""Tests for U19 external-runtime CDP attach (OPTIX_RUNTIME_URL).

Covers the derived read-helpers (runtime_base_url / runtime_probe_host /
runtime_probe_port / attach_mode), the CDP reach chokepoint retarget
(_runtime_verify_url), and the attach-mode management refusals (run_emulator F5,
runtime_start spawn, bridge_ensure_web_engine). The regression guarantee — legacy
behavior is byte-identical when runtime_url is unset — is asserted alongside each
retarget, and the existing 127.0.0.1 probe tests (test_run_emulator etc.) cover
the unset path end-to-end.

Also covers resolve_web_port (I28), the per-project
WebPresentationEngine port resolver, and _maybe_follow_port (I28 rule 2)
self-heal port-following.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from service import core
from service.tests.conftest import FakeProc, make_fake_runner, make_project


# ---- derived read-helpers --------------------------------------------------

def test_runtime_base_url_unset_is_loopback(cfg: core.Config) -> None:
    assert cfg.runtime_url == ""
    assert core.runtime_base_url(cfg) == f"http://127.0.0.1:{cfg.runtime_test_port}/"


def test_runtime_probe_host_port_unset(cfg: core.Config) -> None:
    assert core.runtime_probe_host(cfg) == "127.0.0.1"
    assert core.runtime_probe_port(cfg) == cfg.runtime_test_port


def test_runtime_helpers_https_external(cfg: core.Config) -> None:
    c = dataclasses.replace(cfg, runtime_url="https://10.0.0.5:8443/")
    assert core.runtime_base_url(c) == "https://10.0.0.5:8443/"
    assert core.runtime_probe_host(c) == "10.0.0.5"
    assert core.runtime_probe_port(c) == 8443


def test_runtime_base_url_normalizes_trailing_slash(cfg: core.Config) -> None:
    c = dataclasses.replace(cfg, runtime_url="http://127.0.0.1:9000")
    assert core.runtime_base_url(c) == "http://127.0.0.1:9000/"
    assert core.runtime_probe_port(c) == 9000
    assert core.runtime_probe_host(c) == "127.0.0.1"


def test_runtime_probe_port_scheme_default(cfg: core.Config) -> None:
    """A URL with no explicit port falls back to the scheme default."""
    https = dataclasses.replace(cfg, runtime_url="https://runtime.example/")
    http = dataclasses.replace(cfg, runtime_url="http://runtime.example/")
    assert core.runtime_probe_port(https) == 443
    assert core.runtime_probe_port(http) == 80


def test_attach_mode_true_false(cfg: core.Config) -> None:
    assert core.attach_mode(cfg) is False
    assert core.attach_mode(dataclasses.replace(cfg, runtime_url="https://10.0.0.5:8443/")) is True


# ---- CDP reach chokepoint --------------------------------------------------

def test_runtime_verify_url_reflects_runtime_url(cfg: core.Config) -> None:
    assert core._runtime_verify_url(cfg) == f"http://127.0.0.1:{cfg.runtime_test_port}/"
    c = dataclasses.replace(cfg, runtime_url="https://10.0.0.5:8443/")
    assert core._runtime_verify_url(c) == "https://10.0.0.5:8443/"


# ---- attach-mode management refusals ---------------------------------------

def test_run_emulator_external_no_keystroke(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    """With OPTIX_RUNTIME_URL set, run_emulator refuses BEFORE sending F5 — the
    external runtime owns its lifecycle. No PowerShell/keystroke is dispatched."""
    make_project(projects_root, "Alpha")
    # save must never run; the refusal is before staging.
    monkeypatch.setattr(core, "save", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("save should not run in attach mode")))
    c = dataclasses.replace(cfg, runtime_url="https://10.0.0.5:8443/")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    out = core.run_emulator(c, "Alpha", runner=runner)
    assert out["launched"] is False
    assert out["state"] == "external"
    assert out["reason_code"] == "external_runtime"
    assert "OPTIX_RUNTIME_URL" in out["nudge"]
    assert runner.calls == []  # no F5, no bridge PID shell-out


def test_runtime_start_external_no_spawn(cfg: core.Config, monkeypatch) -> None:
    """runtime_start refuses at entry in attach mode — no runtime is spawned and
    the runtime-tree resolution is never reached."""
    c = dataclasses.replace(cfg, runtime_url="https://10.0.0.5:8443/")

    def _boom(_e):
        raise AssertionError("spawn should not run in attach mode")

    out = core.runtime_start(c, "Alpha", spawn=_boom)
    assert out["state"] == "external"
    assert out["reason_code"] == "external_runtime"
    assert out["pid"] is None
    assert "OPTIX_RUNTIME_URL" in out["nudge"]


def test_bridge_ensure_web_engine_external(cfg: core.Config, monkeypatch) -> None:
    """In attach mode the external runtime owns its WebPresentationEngine — the
    early return fires before any bridge write."""
    monkeypatch.setattr(core, "_bridge_write", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("bridge write should not run in attach mode")))
    c = dataclasses.replace(cfg, runtime_url="https://10.0.0.5:8443/")
    out = core.bridge_ensure_web_engine(c, "Alpha")
    assert out["ok"] is False
    assert out["error"] == "external_runtime"
    assert "OPTIX_RUNTIME_URL" in out["hint"]


# ---- probe-host regression (unset stays loopback) --------------------------

def test_emulator_status_unset_probes_loopback(cfg: core.Config, monkeypatch) -> None:
    """With runtime_url unset, the emulator_status probe still hits 127.0.0.1 on
    the runtime test port — the regression guarantee for the legacy path."""
    seen = {}

    def fake_probe(host, port, timeout=0.5):
        seen["host"] = host
        seen["port"] = port
        return True

    monkeypatch.setattr(core, "_tcp_probe", fake_probe)
    monkeypatch.setattr(core, "_emulator_pids", lambda: [1234])
    st = core.emulator_status(cfg)
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == cfg.runtime_test_port
    assert st["port"] == cfg.runtime_test_port


# ---- resolve_web_port (I28) -----------------------------------

_WPE_YAML = """\
- Name: WebPresentationEngine
  Type: FTOptix.WebUI.WebPresentationEngine
  Port: 9090
"""

_WPE_YAML_NO_PORT = """\
- Name: WebPresentationEngine
  Type: FTOptix.WebUI.WebPresentationEngine
"""

_UI_YAML_NO_ENGINE = """\
- Name: UI
  Type: FTOptix.UI.UIElement
"""


def _make_ui_yaml(projects_root: Path, project: str, content: str) -> None:
    """Write Nodes/UI/UI.yaml inside a project directory."""
    ui_dir = projects_root / project / "Nodes" / "UI"
    ui_dir.mkdir(parents=True, exist_ok=True)
    (ui_dir / "UI.yaml").write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_web_port_cache() -> None:
    """Ensure resolve_web_port cache is empty between tests."""
    core.reset_web_port_cache()


def test_resolve_web_port_default(cfg: core.Config, projects_root: Path) -> None:
    """No bridge, no UI.yaml, no env override → default 8081."""
    make_project(projects_root, "Alpha")
    result = core.resolve_web_port(cfg, "Alpha")
    assert result["source"] == "default"
    assert result["port"] == 8081
    assert "8081" in result["detail"]


def test_resolve_web_port_env_override(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OPTIX_RUNTIME_TEST_PORT set → source=env_override with that port."""
    make_project(projects_root, "Alpha")
    monkeypatch.setenv("OPTIX_RUNTIME_TEST_PORT", "9999")
    result = core.resolve_web_port(cfg, "Alpha")
    assert result["source"] == "env_override"
    assert result["port"] == 9999
    assert "9999" in result["detail"]


def test_resolve_web_port_explicit_beats_env(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """explicit parameter takes priority over env override."""
    make_project(projects_root, "Alpha")
    monkeypatch.setenv("OPTIX_RUNTIME_TEST_PORT", "9999")
    result = core.resolve_web_port(cfg, "Alpha", explicit=7777)
    assert result["source"] == "explicit"
    assert result["port"] == 7777


def test_resolve_web_port_yaml_engine_found(
    cfg: core.Config, projects_root: Path
) -> None:
    """Nodes/UI/UI.yaml has a WebPresentationEngine with Port → source=project."""
    make_project(projects_root, "Alpha")
    _make_ui_yaml(projects_root, "Alpha", _WPE_YAML)
    result = core.resolve_web_port(cfg, "Alpha")
    assert result["source"] == "project"
    assert result["port"] == 9090
    assert "9090" in result["detail"]


def test_resolve_web_port_yaml_engine_no_port(
    cfg: core.Config, projects_root: Path
) -> None:
    """WebPresentationEngine found but no Port property → source=no_engine, port=None."""
    make_project(projects_root, "Alpha")
    _make_ui_yaml(projects_root, "Alpha", _WPE_YAML_NO_PORT)
    result = core.resolve_web_port(cfg, "Alpha")
    assert result["source"] == "no_engine"
    assert result["port"] is None
    assert "optix_bridge_ensure_web_engine" in result["detail"]


def test_resolve_web_port_yaml_no_engine_node(
    cfg: core.Config, projects_root: Path
) -> None:
    """UI.yaml exists but has no WebPresentationEngine node → source=no_engine."""
    make_project(projects_root, "Alpha")
    _make_ui_yaml(projects_root, "Alpha", _UI_YAML_NO_ENGINE)
    result = core.resolve_web_port(cfg, "Alpha")
    assert result["source"] == "no_engine"
    assert result["port"] is None


def test_resolve_web_port_cached(cfg: core.Config, projects_root: Path) -> None:
    """Second call returns the same dict from cache (no file re-read)."""
    make_project(projects_root, "Alpha")
    _make_ui_yaml(projects_root, "Alpha", _WPE_YAML)
    r1 = core.resolve_web_port(cfg, "Alpha")
    # Overwrite the YAML — result should still come from cache
    _make_ui_yaml(projects_root, "Alpha", _UI_YAML_NO_ENGINE)
    r2 = core.resolve_web_port(cfg, "Alpha")
    assert r1 == r2
    assert r2["port"] == 9090


def test_invalidate_web_port_cache(cfg: core.Config, projects_root: Path) -> None:
    """_invalidate_web_port_cache forces re-resolution on next call."""
    make_project(projects_root, "Alpha")
    _make_ui_yaml(projects_root, "Alpha", _WPE_YAML)
    r1 = core.resolve_web_port(cfg, "Alpha")
    assert r1["port"] == 9090
    # Overwrite the YAML and invalidate cache
    _make_ui_yaml(projects_root, "Alpha", _UI_YAML_NO_ENGINE)
    core._invalidate_web_port_cache("Alpha")
    r2 = core.resolve_web_port(cfg, "Alpha")
    assert r2["source"] == "no_engine"
    assert r2["port"] is None


def test_project_web_port_fallback_when_no_engine(
    cfg: core.Config, projects_root: Path
) -> None:
    """_project_web_port falls back to runtime_probe_port(cfg) when port is None."""
    make_project(projects_root, "Alpha")
    _make_ui_yaml(projects_root, "Alpha", _UI_YAML_NO_ENGINE)
    port = core._project_web_port(cfg, "Alpha")
    assert port == core.runtime_probe_port(cfg)


def test_verify_runtime_probe_uses_project_port(
    cfg: core.Config, projects_root: Path, tmp_path: Path
) -> None:
    """verify_runtime_probe probes the project-specific port when project kwarg is bound."""
    import functools
    import time

    make_project(projects_root, "Alpha")
    _make_ui_yaml(projects_root, "Alpha", _WPE_YAML)  # port 9090

    probed_ports: list[int] = []

    def fake_probe(host: str, port: int, timeout: float = 0.5) -> bool:
        probed_ports.append(port)
        return True

    import unittest.mock as _mock
    with _mock.patch.object(core, "_tcp_probe", fake_probe):
        verify = functools.partial(core.verify_runtime_probe, project="Alpha")
        verify(cfg, tmp_path, time.time() - 1.0)

    assert probed_ports, "probe was never called"
    assert probed_ports[0] == 9090, (
        f"expected project port 9090, got {probed_ports[0]}"
    )


# ---- CDP helper project-port binding (I28) -----------------

def test_runtime_base_url_project_uses_wpe_port(
    cfg: core.Config, projects_root: Path
) -> None:
    """runtime_base_url(cfg, project) uses the project's WebPresentationEngine port."""
    make_project(projects_root, "Beta")
    _make_ui_yaml(projects_root, "Beta", _WPE_YAML)  # port 9090
    url = core.runtime_base_url(cfg, "Beta")
    assert "9090" in url, f"expected port 9090 in url, got: {url}"
    assert url.startswith("http://127.0.0.1:9090/")


def test_runtime_verify_url_project_uses_wpe_port(
    cfg: core.Config, projects_root: Path
) -> None:
    """_runtime_verify_url(cfg, project) delegates to runtime_base_url with project."""
    make_project(projects_root, "Beta")
    _make_ui_yaml(projects_root, "Beta", _WPE_YAML)  # port 9090
    url = core._runtime_verify_url(cfg, "Beta")
    assert "9090" in url, f"expected port 9090 in _runtime_verify_url, got: {url}"


def test_cdp_screenshot_project_navigates_to_project_port(
    cfg: core.Config, projects_root: Path
) -> None:
    """cdp_screenshot_runtime with project= navigates to the project's WPE port, not the global default."""
    import unittest.mock as _mock

    make_project(projects_root, "Beta")
    _make_ui_yaml(projects_root, "Beta", _WPE_YAML)  # port 9090

    navigate_calls: list[str] = []

    class FakeVP:
        def set_viewport(self, *a, **kw): pass
        def current_url(self): return "about:blank"
        def navigate(self, url: str): navigate_calls.append(url)
        def screenshot_jpeg(self, **kw): return b"\xff\xd8\xff"
        def close(self): pass
        def viewport_size(self): return (1024.0, 768.0)

    with _mock.patch.object(core, "_cdp_session", return_value=FakeVP()):
        with _mock.patch("builtins.open", _mock.mock_open()):
            try:
                core.cdp_screenshot_runtime(cfg, project="Beta")
            except Exception:
                pass  # session errors are fine — we only care about the navigate call

    # At least one navigate call should target port 9090
    assert any("9090" in c for c in navigate_calls), (
        f"expected navigation to port 9090 but got: {navigate_calls!r}. "
        "cdp_screenshot_runtime is not propagating the project's WPE port."
    )


def _mcp_tool_sync_fn(mcp, name: str):
    """Return the directly-callable sync fn for a named MCP tool.

    Offloaded (async-wrapped) tools keep their original sync callable at
    ``_ftx_sync_fn``; fast sync tools expose it as ``tool.fn`` directly.
    Mirrors the helper in test_mcp_app.py."""
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == name)
    return getattr(tool, "_ftx_sync_fn", tool.fn)


def test_interact_fill_action_propagates_project_port(
    cfg: core.Config, projects_root: Path
) -> None:
    """optix_interact fill action passes project to cdp_fill_runtime → project port used."""
    import unittest.mock as _mock
    from service import mcp_app

    make_project(projects_root, "Gamma")
    _make_ui_yaml(projects_root, "Gamma", _WPE_YAML)  # port 9090

    captured: dict = {}

    def fake_fill_runtime(cfg_, x, y, text, **kwargs):
        captured["project"] = kwargs.get("project")
        return {"state": "succeeded", "steps": {}, "x": x, "y": y,
                "navigated": False, "filled_at": "t", "error": None}

    with _mock.patch.object(core, "cdp_fill_runtime", fake_fill_runtime):
        mcp = mcp_app.make_mcp(cfg)
        fn = _mcp_tool_sync_fn(mcp, "optix_interact")
        fn(action="fill", x=10.0, y=20.0, text="hello", project="Gamma")

    assert captured.get("project") == "Gamma", (
        f"expected project='Gamma' propagated to cdp_fill_runtime, got: {captured!r}"
    )


def test_interact_navigate_action_propagates_project_port(
    cfg: core.Config, projects_root: Path, tmp_path: Path
) -> None:
    """optix_interact navigate action passes project to cdp_navigate_runtime."""
    import json
    import unittest.mock as _mock
    from service import mcp_app

    make_project(projects_root, "Gamma")
    _make_ui_yaml(projects_root, "Gamma", _WPE_YAML)  # port 9090

    # Write a minimal valid routes file
    routes_file = tmp_path / "routes.json"
    routes_file.write_text(
        json.dumps({"version": 1, "routes": {"home": {"steps": []}}}),
        encoding="utf-8",
    )

    captured: dict = {}

    def fake_navigate_runtime(cfg_, route, routes_path, **kwargs):
        captured["project"] = kwargs.get("project")
        return {"state": "succeeded", "route": route, "steps_run": 0,
                "verified_steps": 0, "navigated": False, "finished_at": "t"}

    with _mock.patch.object(core, "cdp_navigate_runtime", fake_navigate_runtime):
        mcp = mcp_app.make_mcp(cfg)
        fn = _mcp_tool_sync_fn(mcp, "optix_interact")
        fn(action="navigate", route="home",
           routes_path=str(routes_file), project="Gamma")

    assert captured.get("project") == "Gamma", (
        f"expected project='Gamma' propagated to cdp_navigate_runtime, got: {captured!r}"
    )


# ---- _maybe_follow_port (I28 rule 2) ----------------------


def test_maybe_follow_port_no_project(cfg: core.Config) -> None:
    """_maybe_follow_port returns None immediately when project is None."""
    result = core._maybe_follow_port(cfg, None)
    assert result is None


def test_maybe_follow_port_attach_mode(cfg: core.Config) -> None:
    """In attach mode _maybe_follow_port returns None (external runtime)."""
    import dataclasses
    c = dataclasses.replace(cfg, runtime_url="https://10.0.0.5:8443/")
    result = core._maybe_follow_port(c, "Alpha")
    assert result is None


def test_maybe_follow_port_expected_port_alive(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the expected port is reachable, _maybe_follow_port is a no-op."""
    make_project(projects_root, "Alpha")
    _make_ui_yaml(projects_root, "Alpha", _WPE_YAML)  # port 9090

    monkeypatch.setattr(core, "_tcp_probe", lambda host, port, timeout=0.5: True)
    result = core._maybe_follow_port(cfg, "Alpha")
    assert result is None


def test_maybe_follow_port_emulator_dead(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the expected port is dead AND no emulator PIDs, no follow happens."""
    make_project(projects_root, "Alpha")
    _make_ui_yaml(projects_root, "Alpha", _WPE_YAML)  # port 9090

    monkeypatch.setattr(core, "_tcp_probe", lambda host, port, timeout=0.5: False)
    monkeypatch.setattr(core, "_emulator_pids", lambda: [])
    result = core._maybe_follow_port(cfg, "Alpha")
    assert result is None


def test_maybe_follow_port_new_port_also_dead(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Port is dead, emulator alive, but re-resolve yields same dead port → no follow."""
    make_project(projects_root, "Alpha")
    _make_ui_yaml(projects_root, "Alpha", _WPE_YAML)  # port 9090

    monkeypatch.setattr(core, "_tcp_probe", lambda host, port, timeout=0.5: False)
    monkeypatch.setattr(core, "_emulator_pids", lambda: [1234])
    # resolve_web_port will re-read UI.yaml (same port 9090) after cache bust
    result = core._maybe_follow_port(cfg, "Alpha")
    assert result is None


def test_maybe_follow_port_follows_alive_port(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Port dead, emulator alive, re-resolve finds a different live port → follow."""
    _WPE_YAML_9090 = """\
- Name: WebPresentationEngine
  Type: FTOptix.WebUI.WebPresentationEngine
  Port: 9090
"""
    _WPE_YAML_9091 = """\
- Name: WebPresentationEngine
  Type: FTOptix.WebUI.WebPresentationEngine
  Port: 9091
"""
    make_project(projects_root, "Beta")
    # Prime the cache with port 9090 (the "stale" value)
    _make_ui_yaml(projects_root, "Beta", _WPE_YAML_9090)
    r = core.resolve_web_port(cfg, "Beta")
    assert r["port"] == 9090

    # Now switch the YAML to port 9091 (the "actual" running port)
    _make_ui_yaml(projects_root, "Beta", _WPE_YAML_9091)

    probe_calls: list[int] = []

    def fake_probe(host: str, port: int, timeout: float = 0.5) -> bool:
        probe_calls.append(port)
        # 9090 is dead, 9091 is alive
        return port == 9091

    monkeypatch.setattr(core, "_tcp_probe", fake_probe)
    monkeypatch.setattr(core, "_emulator_pids", lambda: [1234])

    result = core._maybe_follow_port(cfg, "Beta")
    assert result is not None, "expected a follow, got None"
    assert result["expected"] == 9090
    assert result["actual"] == 9091
    assert result["project"] == "Beta"
    # Cache should now be updated to 9091
    assert core.resolve_web_port(cfg, "Beta")["port"] == 9091


def test_maybe_follow_port_stderr_log(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A successful follow emits a loud log line to stderr."""
    _WPE_YAML_OLD = """\
- Name: WebPresentationEngine
  Type: FTOptix.WebUI.WebPresentationEngine
  Port: 8081
"""
    _WPE_YAML_NEW = """\
- Name: WebPresentationEngine
  Type: FTOptix.WebUI.WebPresentationEngine
  Port: 8088
"""
    make_project(projects_root, "Log")
    _make_ui_yaml(projects_root, "Log", _WPE_YAML_OLD)
    core.resolve_web_port(cfg, "Log")  # prime cache with 8081
    _make_ui_yaml(projects_root, "Log", _WPE_YAML_NEW)

    def fake_probe(host: str, port: int, timeout: float = 0.5) -> bool:
        return port == 8088  # only new port alive

    monkeypatch.setattr(core, "_tcp_probe", fake_probe)
    monkeypatch.setattr(core, "_emulator_pids", lambda: [999])

    core._maybe_follow_port(cfg, "Log")
    captured = capsys.readouterr()
    assert "Log" in captured.err
    assert ":8088" in captured.err
    assert ":8081" in captured.err
    assert "following the project" in captured.err


def test_cdp_screenshot_includes_port_followed(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cdp_screenshot_runtime includes port_followed in the result when a follow fires."""
    import unittest.mock as _mock

    _WPE_YAML_9000 = """\
- Name: WebPresentationEngine
  Type: FTOptix.WebUI.WebPresentationEngine
  Port: 9000
"""
    _WPE_YAML_9001 = """\
- Name: WebPresentationEngine
  Type: FTOptix.WebUI.WebPresentationEngine
  Port: 9001
"""
    make_project(projects_root, "Shot")
    _make_ui_yaml(projects_root, "Shot", _WPE_YAML_9000)
    core.resolve_web_port(cfg, "Shot")  # prime cache with 9000
    _make_ui_yaml(projects_root, "Shot", _WPE_YAML_9001)

    def fake_probe(host: str, port: int, timeout: float = 0.5) -> bool:
        return port == 9001

    monkeypatch.setattr(core, "_tcp_probe", fake_probe)
    monkeypatch.setattr(core, "_emulator_pids", lambda: [42])

    navigate_calls: list[str] = []

    class FakeVP:
        def set_viewport(self, *a, **kw): pass
        def current_url(self): return "about:blank"
        def navigate(self, url: str): navigate_calls.append(url)
        def screenshot_jpeg(self, **kw): return b"\xff\xd8\xff"
        def close(self): pass
        def viewport_size(self): return (1024.0, 768.0)

    with _mock.patch.object(core, "_cdp_session", return_value=FakeVP()):
        result = core.cdp_screenshot_runtime(cfg, project="Shot")

    assert "port_followed" in result, (
        f"expected 'port_followed' in result, got: {result!r}"
    )
    assert result["port_followed"] == ":9000→:9001", (
        f"unexpected port_followed value: {result['port_followed']!r}"
    )
    # Navigation should have gone to the new port
    assert any("9001" in c for c in navigate_calls), (
        f"expected navigation to port 9001 after follow, got: {navigate_calls!r}"
    )
