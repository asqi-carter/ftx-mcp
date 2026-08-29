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
    # run_emulator resolves its OWN bridge routing via
    # _bridge_cfg_for (multi-instance, v1.0.7) instead of the old
    # _use_bridge_for-then-cfg-unchanged gate — patch it too so "no bridge" by
    # default still holds (target_pid stays 0, matching pre-1.0.7 behavior).
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: None)


@pytest.fixture(autouse=True)
def _no_host_runtime(monkeypatch) -> None:
    """The bare-presence fallback scans REAL host processes (psutil) — a live
    FTOptixRuntime on the test host would flip the no-spawn diagnosis tests
    to 'starting'. Tests exercising the True path override this stub."""
    monkeypatch.setattr(core, "_bare_runtime_running",
                        lambda cfg, runner=None: False)


@pytest.fixture(autouse=True)
def _emulator_target_by_default(monkeypatch, tmp_path) -> None:
    """The F5 guard (v1.0.7) reads the REAL machine's Studio
    Configuration.xml (via OPTIX_STUDIO_CONFIG_XML / the %LOCALAPPDATA%
    default) unless overridden — so on a dev box that actually has
    FactoryTalk Optix Studio installed with a non-emulator target
    selected, these pre-guard tests would spuriously trip the guard and
    never reach the F5 keystroke they're asserting on. Point the guard at
    a path that doesn't exist by default: studio_active_deployment_target()
    then returns known=False and the guard fails open, matching the
    pre-1.0.7 behavior these tests were written against. Tests that
    exercise the guard itself call _config(tmp_path, monkeypatch, ...)
    (below) with a real target, which overrides this default.
    """
    monkeypatch.setenv("OPTIX_STUDIO_CONFIG_XML", str(tmp_path / "no-studio-config.xml"))


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


class _PsProc:
    """Minimal stand-in for a psutil.process_iter(attrs=...) element.
    cmdline=None models an unreadable process (cmdline() raises AccessDenied,
    like the real psutil object)."""

    def __init__(self, pid: int, name: str, cmdline: list[str] | None) -> None:
        self.info = {"pid": pid, "name": name}
        self._cmdline = cmdline

    def cmdline(self) -> list[str]:
        if self._cmdline is None:
            raise core.psutil.AccessDenied(self.info["pid"])
        return self._cmdline


def _mock_procs(monkeypatch, procs: list[_PsProc]) -> None:
    monkeypatch.setattr(core.psutil, "process_iter",
                        lambda attrs=None, ad_value=None: iter(procs))


_EMU_CMD = ["FTOptixRuntime.exe", "--application-name=Emulator"]
_DEPLOYED_CMD = ["FTOptixRuntime.exe", "--application-name=Deployed"]


def test_emulator_status_running_needs_pid_and_port(cfg: core.Config, monkeypatch) -> None:
    """running requires BOTH an emulator PID and the port serving; the PID scan
    must be command-line-discriminated (a deployed runtime is the same exe)."""
    _mock_port(monkeypatch, reachable=True)
    _mock_procs(monkeypatch, [
        _PsProc(1234, "FTOptixRuntime.exe", _EMU_CMD),
        _PsProc(5678, "FTOptixRuntime.exe", _EMU_CMD),
        _PsProc(9999, "FTOptixRuntime.exe", _DEPLOYED_CMD),  # must NOT count
        _PsProc(42, "notepad.exe", ["notepad.exe"]),
    ])
    st = core.emulator_status(cfg)
    assert st["state"] == "running" and st["running"] is True
    assert st["pids"] == [1234, 5678] and st["port_reachable"] is True


def test_emulator_status_starting_when_port_not_serving(cfg: core.Config, monkeypatch) -> None:
    """PID up but port down = starting, NOT running (the pre-1.1 false positive)."""
    _mock_port(monkeypatch, reachable=False)
    _mock_procs(monkeypatch, [_PsProc(1234, "FTOptixRuntime.exe", _EMU_CMD)])
    st = core.emulator_status(cfg)
    assert st["state"] == "starting" and st["running"] is False
    assert "hint" in st


def test_emulator_status_deployed_runtime_is_not_emulator(cfg: core.Config, monkeypatch) -> None:
    """Port serving but no emulator PID (an UpdateSvc-deployed runtime holds the
    port) = not_running with a hint — the 2026-07-16 false-positive trap."""
    _mock_port(monkeypatch, reachable=True)
    _mock_procs(monkeypatch, [_PsProc(9999, "FTOptixRuntime.exe", _DEPLOYED_CMD)])
    st = core.emulator_status(cfg)
    assert st["state"] == "not_running" and st["running"] is False
    assert st["port_reachable"] is True
    assert "hint" in st and "deployed" in st["hint"].lower()


def test_emulator_status_not_running(cfg: core.Config, monkeypatch) -> None:
    _mock_port(monkeypatch, reachable=False)
    _mock_procs(monkeypatch, [])
    st = core.emulator_status(cfg)
    assert st["state"] == "not_running" and st["running"] is False and st["pids"] == []


def test_emulator_status_survives_unreadable_cmdline(cfg: core.Config, monkeypatch) -> None:
    """A process whose cmdline read raises AccessDenied must be skipped,
    not crash the scan."""
    _mock_port(monkeypatch, reachable=False)
    _mock_procs(monkeypatch, [
        _PsProc(7, "FTOptixRuntime.exe", None),
        _PsProc(1234, "FTOptixRuntime.exe", _EMU_CMD),
    ])
    st = core.emulator_status(cfg)
    assert st["pids"] == [1234]


def _mock_kill(monkeypatch, state: dict, killed: list[int]) -> None:
    """Fake psutil.Process/wait_procs: record kills, flip state['stopped']."""
    class FakePs:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def kill(self) -> None:
            killed.append(self.pid)
            state["stopped"] = True

    monkeypatch.setattr(core.psutil, "Process", FakePs)
    monkeypatch.setattr(core.psutil, "wait_procs",
                        lambda procs, timeout=None: (list(procs), []))


def test_stop_emulator_kills_running(cfg: core.Config, monkeypatch) -> None:
    _mock_port(monkeypatch, reachable=True)
    state = {"stopped": False}
    killed: list[int] = []
    monkeypatch.setattr(
        core, "_emulator_pids",
        lambda: [] if state["stopped"] else [1234])
    _mock_kill(monkeypatch, state, killed)
    out = core.stop_emulator(cfg)
    # kills the discriminated PIDs, not every FTOptixRuntime
    assert killed == [1234]
    assert out["stopped"] is True and out["killed_pids"] == [1234]


def test_stop_emulator_stops_a_starting_emulator(cfg: core.Config, monkeypatch) -> None:
    """A PID with the port not yet serving (state=starting) must still be stoppable."""
    _mock_port(monkeypatch, reachable=False)
    state = {"stopped": False}
    killed: list[int] = []
    monkeypatch.setattr(
        core, "_emulator_pids",
        lambda: [] if state["stopped"] else [4321])
    _mock_kill(monkeypatch, state, killed)
    out = core.stop_emulator(cfg)
    assert out["stopped"] is True and out["killed_pids"] == [4321]


def test_stop_emulator_when_not_running(cfg: core.Config, monkeypatch) -> None:
    _mock_port(monkeypatch, reachable=False)
    monkeypatch.setattr(core, "_emulator_pids", lambda: [])
    out = core.stop_emulator(cfg)
    assert out["stopped"] is False and out["reason"] == "not_running"


def test_stop_emulator_uses_prefetched_status(cfg: core.Config, monkeypatch) -> None:
    """status= skips the redundant scan restart_emulator already paid for."""
    _mock_port(monkeypatch, reachable=False)
    state = {"stopped": True}  # live scan says gone (post-kill re-check)
    killed: list[int] = []
    monkeypatch.setattr(core, "_emulator_pids", lambda: [])
    _mock_kill(monkeypatch, state, killed)
    out = core.stop_emulator(cfg, status={"pids": [1234]})
    assert killed == [1234]
    assert out["stopped"] is True and out["killed_pids"] == [1234]


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


def test_run_emulator_no_spawn_names_blocking_dialog(
    cfg, projects_root, monkeypatch
) -> None:
    """When F5 doesn't spawn the emulator AND UIA sees a blocking dialog owned
    by the bridge Studio, the diagnosis must NAME it (U22) instead of saying the
    service can't see dialogs."""
    from service import studio_uia
    _proj(projects_root)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_bridge_owner_pid", lambda cfg, runner=None: 4242)
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, runner=None: {"state": "not_running"})
    monkeypatch.setattr(studio_uia, "pending_dialog",
                        lambda pid: [{"title": "Deploy credentials", "text": "Enter password"}])
    import dataclasses
    cfg2 = dataclasses.replace(cfg, runtime_test_port=65431)
    out = core.run_emulator(cfg2, "Alpha", wait_ready=True, ready_timeout=0.1,
                            runner=runner)
    assert out["probable_cause"] == "target_or_modal"
    assert out["blocking_dialog"]["title"] == "Deploy credentials"
    assert "Deploy credentials" in out["hint"] and "retry-loop" in out["hint"]


def _no_spawn_hint(cfg, projects_root, monkeypatch, *, live: bool) -> str:
    """Drive the F5-sent-but-nothing-spawned path with a dialog visible, with
    the target guard resolving either live (uia) or from the config file."""
    from service import studio_uia
    import dataclasses
    _proj(projects_root)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_bridge_owner_pid", lambda cfg, runner=None: 4242)
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, runner=None: {"state": "not_running"})
    monkeypatch.setattr(studio_uia, "pending_dialog",
                        lambda pid: [{"title": "Device access", "text": "x"}])
    monkeypatch.setattr(core, "resolve_active_target", lambda cfg, bridge_pid=None: {
        "known": True, "is_emulator": True, "name": "Emulator",
        "source": "uia_live" if live else "C:/.../Configuration.xml"})
    cfg2 = dataclasses.replace(cfg, runtime_test_port=65431)
    out = core.run_emulator(cfg2, "Alpha", wait_ready=True, ready_timeout=0.1,
                            runner=runner)
    return out["hint"]


def test_no_spawn_hint_does_not_blame_the_dropdown_when_read_live(
    cfg, projects_root, monkeypatch
) -> None:
    """A live read that says non-emulator REFUSES before F5, so reaching this
    path with source=uia_live means the dropdown is CONFIRMED correct. The hint
    used to tell the user to go set it to Emulator — sending someone with an
    already-correct dropdown to look in the wrong place."""
    hint = _no_spawn_hint(cfg, projects_root, monkeypatch, live=True)
    assert "NOT a target-selection problem" in hint
    assert "unrelated" in hint


def test_no_spawn_hint_flags_possible_staleness_on_the_file_fallback(
    cfg, projects_root, monkeypatch
) -> None:
    """Without a live read the guard trusted Configuration.xml, which Studio
    flushes lazily — so here the dropdown genuinely IS worth checking."""
    hint = _no_spawn_hint(cfg, projects_root, monkeypatch, live=False)
    assert "could NOT be read live" in hint
    assert "stale" in hint and "dropdown" in hint


def test_run_emulator_no_spawn_no_dialog_visible(
    cfg, projects_root, monkeypatch
) -> None:
    """Bridge present but UIA sees no blocking dialog: fall back to the generic
    hypothesis, and say the UIA check found nothing (not 'cannot see')."""
    from service import studio_uia
    _proj(projects_root)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_bridge_owner_pid", lambda cfg, runner=None: 4242)
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, runner=None: {"state": "not_running"})
    monkeypatch.setattr(studio_uia, "pending_dialog", lambda pid: [])
    import dataclasses
    cfg2 = dataclasses.replace(cfg, runtime_test_port=65431)
    out = core.run_emulator(cfg2, "Alpha", wait_ready=True, ready_timeout=0.1,
                            runner=runner)
    assert out["probable_cause"] == "target_or_modal"
    assert "blocking_dialog" not in out
    assert "No blocking dialog was visible" in out["hint"]


def test_active_target_prefers_uia_then_falls_back(
    cfg, monkeypatch, tmp_path
) -> None:
    """core.active_target resolves the bridge PID then prefers the live UIA read;
    off-Windows the read is None so it falls back to the config file."""
    from service import studio_uia
    _config(tmp_path, monkeypatch, "emu-id")
    monkeypatch.setattr(core, "_bridge_owner_pid", lambda cfg, runner=None: 4242)
    # UIA live read wins when present
    monkeypatch.setattr(studio_uia, "read_selected_target_name",
                        lambda pid, names: "Line3 Panel")
    live = core.active_target(cfg)
    assert live["name"] == "Line3 Panel" and live["source"] == "uia_live"
    assert live["is_emulator"] is False
    # None from UIA -> config-file fallback (the file still says Emulator)
    monkeypatch.setattr(studio_uia, "read_selected_target_name",
                        lambda pid, names: None)
    fb = core.active_target(cfg)
    assert fb["name"] == "Emulator" and fb["source"] != "uia_live"
    assert fb["is_emulator"] is True


def test_pending_dialog_returns_empty_off_windows() -> None:
    """Off Windows / uiautomation absent, pending_dialog degrades to [] cleanly
    (no exception) — the signal for 'no visible dialog'."""
    from service import studio_uia
    assert studio_uia.pending_dialog(4242) == []


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


# --- U20: LIVE per-window UIA target read (Windows-only, mocked on Linux) -----

def _with_bridge(monkeypatch, pid: int = 4242) -> None:
    """Force the bridge-owner PID resolution so run_emulator passes a real
    bridge_pid into resolve_active_target (the autouse fixture disables bridges).
    Later monkeypatch wins over the autouse _no_bridge_by_default."""
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_bridge_owner_pid", lambda cfg, runner=None: pid)


def test_uia_live_read_overrides_stale_file_and_refuses(
    cfg, projects_root, monkeypatch, tmp_path
) -> None:
    """The core value: the config file says Emulator (safe) but the LIVE toolbar
    is on a hardware panel. The UIA read must WIN and refuse — source uia_live,
    no keystroke — even though the file would have green-lit the run."""
    _proj(projects_root)
    _config(tmp_path, monkeypatch, "emu-id")  # file claims the emulator is active
    _with_bridge(monkeypatch)
    monkeypatch.setattr(core.studio_uia, "read_selected_target_name",
                        lambda pid, names: "Line3 Panel")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    out = core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    assert out["reason_code"] == "active_target_not_emulator"
    assert out["launched"] is False
    assert out["source"] == "uia_live"
    assert out["target"]["name"] == "Line3 Panel"
    assert "live" in out["nudge"].lower()
    assert runner.calls == []  # NO keystroke was sent


def test_uia_live_read_emulator_proceeds(
    cfg, projects_root, monkeypatch, tmp_path
) -> None:
    """Live toolbar reads Emulator → proceeds via the uia_live path (even if the
    file's active target were something else)."""
    _proj(projects_root)
    _config(tmp_path, monkeypatch, "panel-id")  # file would refuse
    _with_bridge(monkeypatch)
    monkeypatch.setattr(core.studio_uia, "read_selected_target_name",
                        lambda pid, names: "Emulator")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    out = core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    assert out["launched"] is True


def test_uia_none_falls_back_to_config_file(
    cfg, projects_root, monkeypatch, tmp_path
) -> None:
    """UIA returns None (unavailable) → fall back to the config file: refuse when
    the file's active target is a panel, proceed when it's the emulator."""
    _proj(projects_root)
    _with_bridge(monkeypatch)
    monkeypatch.setattr(core.studio_uia, "read_selected_target_name",
                        lambda pid, names: None)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))

    _config(tmp_path, monkeypatch, "panel-id")
    out = core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    assert out["reason_code"] == "active_target_not_emulator"
    assert out["source"] != "uia_live"
    assert "may" in out["nudge"] or "dropdown" in out["nudge"]

    _config(tmp_path, monkeypatch, "emu-id")
    out = core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    assert out["launched"] is True


def test_studio_uia_read_returns_none_on_linux() -> None:
    """The UIA read is Windows-only; on Linux (no uiautomation) it must degrade
    to None cleanly — no exception — so resolve_active_target falls back."""
    from service import studio_uia
    assert studio_uia.read_selected_target_name(1234, {"Emulator"}) is None
