"""Tests for core.run_emulator — F5 emulator-launch (design-time counterpart to
a deploy). Offline: the PowerShell runner is faked, save() is stubbed. The real
SendKeys is validated against live Studio."""
from __future__ import annotations

from pathlib import Path

import pytest

from service import core
from service.tests.conftest import FakeProc, make_fake_runner, make_project


@pytest.fixture(autouse=True)
def _no_bridge_by_default(monkeypatch) -> None:
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: False)


def _proj(projects_root: Path) -> None:
    make_project(projects_root, "Alpha")


def test_run_emulator_sends_f5(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    _proj(projects_root)
    monkeypatch.setattr(core, "save", lambda *a, **k: {"saved": True})
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    out = core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    assert out["launched"] is True and out["focused"] is True
    ps = runner.calls[0][0][-1]
    assert "SendKeys" in ps and "{F5}" in ps and "^s" not in ps


def test_run_emulator_no_save_by_default(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    """F5 saves as part of staging — an explicit ^s beforehand is redundant, so
    the default must NOT save (v1.1 backlog 1.2)."""
    _proj(projects_root)
    monkeypatch.setattr(core, "save", lambda *a, **k: (_ for _ in ()).throw(AssertionError("save should not run by default")))
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    out = core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    assert out["saved"] is None and out["launched"] is True


def test_run_emulator_saves_when_opted_in(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    _proj(projects_root)
    seen = {}

    def fake_save(*a, **k):
        seen["called"] = True
        return {"saved": True}

    monkeypatch.setattr(core, "save", fake_save)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    out = core.run_emulator(cfg, "Alpha", save_first=True, wait_ready=False, runner=runner)
    assert seen.get("called") is True and out["saved"] is True


def test_run_emulator_no_studio(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    _proj(projects_root)
    monkeypatch.setattr(core, "save", lambda *a, **k: {"saved": True})
    runner = make_fake_runner(lambda cmd, kw: FakeProc(returncode=3, stdout="NO_STUDIO"))
    out = core.run_emulator(cfg, "Alpha", runner=runner)
    assert out["launched"] is False and out["reason"] == "no_studio_window"


def test_run_emulator_focused_false_gives_integrity_hint(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    _proj(projects_root)
    monkeypatch.setattr(core, "save", lambda *a, **k: {"saved": True})
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=False PID=1"))
    out = core.run_emulator(cfg, "Alpha", runner=runner)
    assert out["launched"] is False
    assert "hint" in out and "integrity" in out["hint"].lower()


def test_run_emulator_waits_until_serving(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    """wait_ready polls the runtime port until it's serving (refused twice, then up)
    so a CDP screenshot fired right after actually hits something."""
    import socket as _socket
    _proj(projects_root)
    monkeypatch.setattr(core, "save", lambda *a, **k: {"saved": True})
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    seq = [1, 1, 0]  # connect_ex: nonzero=refused, 0=serving

    class FakeSock:
        def settimeout(self, *a): pass
        def connect_ex(self, addr): return seq.pop(0) if seq else 0
        def close(self): pass

    monkeypatch.setattr(_socket, "socket", lambda *a, **k: FakeSock())
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    out = core.run_emulator(cfg, "Alpha", runner=runner)
    assert out["serving"] is True
    assert out["ready_port"] == cfg.runtime_test_port


def _mock_port(monkeypatch, reachable: bool) -> None:
    """Force the emulator_status port probe to a deterministic result."""
    import socket as _socket

    class FakeSock:
        def settimeout(self, *a): pass
        def connect_ex(self, addr): return 0 if reachable else 111
        def close(self): pass

    monkeypatch.setattr(_socket, "socket", lambda *a, **k: FakeSock())


def test_emulator_status_running_needs_pid_and_port(cfg: core.Config, monkeypatch) -> None:
    """running requires BOTH an emulator PID and the port serving."""
    _mock_port(monkeypatch, reachable=True)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "PIDS=1234,5678"))
    st = core.emulator_status(cfg, runner=runner)
    assert st["state"] == "running" and st["running"] is True
    assert st["pids"] == [1234, 5678] and st["port_reachable"] is True
    # the PID probe must be CommandLine-discriminated, not a name-only match
    ps = runner.calls[0][0][-1]
    assert "--application-name=Emulator" in ps and "Get-CimInstance" in ps


def test_emulator_status_starting_when_port_not_serving(cfg: core.Config, monkeypatch) -> None:
    """PID up but port down = starting, NOT running (the pre-1.1 false positive)."""
    _mock_port(monkeypatch, reachable=False)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "PIDS=1234"))
    st = core.emulator_status(cfg, runner=runner)
    assert st["state"] == "starting" and st["running"] is False
    assert "hint" in st


def test_emulator_status_deployed_runtime_is_not_emulator(cfg: core.Config, monkeypatch) -> None:
    """Port serving but no emulator PID (an UpdateSvc-deployed runtime holds the
    port) = not_running with a hint — the 2026-07-16 false-positive trap."""
    _mock_port(monkeypatch, reachable=True)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "PIDS="))
    st = core.emulator_status(cfg, runner=runner)
    assert st["state"] == "not_running" and st["running"] is False
    assert st["port_reachable"] is True
    assert "hint" in st and "deployed" in st["hint"].lower()


def test_emulator_status_not_running(cfg: core.Config, monkeypatch) -> None:
    _mock_port(monkeypatch, reachable=False)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "PIDS="))
    st = core.emulator_status(cfg, runner=runner)
    assert st["state"] == "not_running" and st["running"] is False and st["pids"] == []


def test_stop_emulator_kills_running(cfg: core.Config, monkeypatch) -> None:
    _mock_port(monkeypatch, reachable=True)
    state = {"stopped": False}

    def handler(cmd, kw):
        c = cmd[-1]
        if "Stop-Process -Id" in c:
            assert "1234" in c  # kills the discriminated PIDs, not every FTOptixRuntime
            state["stopped"] = True
            return FakeProc(0, "")
        if "Get-CimInstance" in c:
            return FakeProc(0, "PIDS=" if state["stopped"] else "PIDS=1234")
        return FakeProc(0, "")

    out = core.stop_emulator(cfg, runner=make_fake_runner(handler))
    assert out["stopped"] is True and out["killed_pids"] == [1234]


def test_stop_emulator_stops_a_starting_emulator(cfg: core.Config, monkeypatch) -> None:
    """A PID with the port not yet serving (state=starting) must still be stoppable."""
    _mock_port(monkeypatch, reachable=False)
    state = {"stopped": False}

    def handler(cmd, kw):
        c = cmd[-1]
        if "Stop-Process -Id" in c:
            state["stopped"] = True
            return FakeProc(0, "")
        if "Get-CimInstance" in c:
            return FakeProc(0, "PIDS=" if state["stopped"] else "PIDS=4321")
        return FakeProc(0, "")

    out = core.stop_emulator(cfg, runner=make_fake_runner(handler))
    assert out["stopped"] is True and out["killed_pids"] == [4321]


def test_stop_emulator_when_not_running(cfg: core.Config, monkeypatch) -> None:
    _mock_port(monkeypatch, reachable=False)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "PIDS="))
    out = core.stop_emulator(cfg, runner=runner)
    assert out["stopped"] is False and out["reason"] == "not_running"


# --- F5 target guard (2026-07-17): F5 runs the SELECTED deployment target ---

_CONFIG_XML = """<Configuration>
  <Collection name="windows">
    <Item>
      <Value name="name" dataType="String">deployment</Value>
      <Value name="activeTargetId" dataType="String">{active}</Value>
      <Collection name="targets">
        <Item>
          <Value name="id" dataType="String">emu-id</Value>
          <Value name="name" dataType="String">Emulator</Value>
          <Value name="ipAddress" dataType="String">localhost</Value>
          <Value name="type" dataType="Int32">2</Value>
        </Item>
        <Item>
          <Value name="id" dataType="String">panel-id</Value>
          <Value name="name" dataType="String">Line3 Panel</Value>
          <Value name="ipAddress" dataType="String">192.168.1.11</Value>
          <Value name="type" dataType="Int32">1</Value>
        </Item>
      </Collection>
    </Item>
  </Collection>
</Configuration>"""


def _config(tmp_path, monkeypatch, active):
    p = tmp_path / "Configuration.xml"
    p.write_text(_CONFIG_XML.format(active=active), encoding="utf-8")
    monkeypatch.setenv("OPTIX_STUDIO_CONFIG_XML", str(p))
    return p


def test_run_emulator_refuses_when_hardware_target_selected(
    cfg, projects_root, monkeypatch, tmp_path
) -> None:
    """F5 fires at Studio's SELECTED target — with a panel selected, pressing
    it could deploy to hardware. The guard must refuse BEFORE any keystroke."""
    _proj(projects_root)
    _config(tmp_path, monkeypatch, "panel-id")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    out = core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    assert out["reason_code"] == "active_target_not_emulator"
    assert out["launched"] is False
    assert out["target"]["name"] == "Line3 Panel"
    assert "192.168.1.11" in out["nudge"] or out["target"]["ip"] == "192.168.1.11"
    assert runner.calls == []   # NO keystroke was sent


def test_run_emulator_proceeds_when_emulator_selected(
    cfg, projects_root, monkeypatch, tmp_path
) -> None:
    _proj(projects_root)
    _config(tmp_path, monkeypatch, "emu-id")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    out = core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    assert out["launched"] is True


def test_run_emulator_fails_open_when_config_missing(
    cfg, projects_root, monkeypatch, tmp_path
) -> None:
    """Unknown installs must not brick emulator runs — absent/unreadable config
    means known=False and the run proceeds (second-layer identity check still
    applies live)."""
    _proj(projects_root)
    monkeypatch.setenv("OPTIX_STUDIO_CONFIG_XML", str(tmp_path / "nope.xml"))
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    out = core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    assert out["launched"] is True


def test_active_target_parser_reports_structure(tmp_path, monkeypatch, cfg) -> None:
    _config(tmp_path, monkeypatch, "emu-id")
    t = core.studio_active_deployment_target(cfg)
    assert t["known"] is True and t["is_emulator"] is True and t["name"] == "Emulator"
    _config(tmp_path, monkeypatch, "panel-id")
    t = core.studio_active_deployment_target(cfg)
    assert t["is_emulator"] is False and t["ip"] == "192.168.1.11"


def test_run_emulator_no_spawn_hypothesizes_target_or_modal(
    cfg, projects_root, monkeypatch
) -> None:
    """F5 sent, focused, port never serves, NO emulator process: the response
    must teach the wrong-target/modal hypothesis and forbid retry-looping
    (live-earned 2026-07-17: 'optixServer' selected in the toolbar)."""
    _proj(projects_root)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, runner=None: {"state": "not_running"})
    import dataclasses
    cfg2 = dataclasses.replace(cfg, runtime_test_port=65431)
    out = core.run_emulator(cfg2, "Alpha", wait_ready=True, ready_timeout=0.1,
                            runner=runner)
    assert out["serving"] is False
    assert out["probable_cause"] == "target_or_modal"
    assert "dropdown" in out["hint"] and "retry-loop" in out["hint"]


def test_run_emulator_still_starting_says_poll_not_toggle(
    cfg, projects_root, monkeypatch
) -> None:
    _proj(projects_root)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, runner=None: {"state": "starting"})
    import dataclasses
    cfg2 = dataclasses.replace(cfg, runtime_test_port=65431)
    out = core.run_emulator(cfg2, "Alpha", wait_ready=True, ready_timeout=0.1,
                            runner=runner)
    assert out["runtime_identity"] == "starting"
    assert "TOGGLES" in out["hint"] and "probable_cause" not in out
