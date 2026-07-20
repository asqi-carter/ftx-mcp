"""Tests for core.save — UI-automation Ctrl+S + mtime verification.

Offline: the PowerShell runner is faked. The fake handler simulates Studio's
on-save disk write (or not), and we assert save() detects the mtime advance.
The real SendKeys/AppActivate is validated against live Studio.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from service import core
from service.tests.conftest import FakeProc, make_fake_runner, make_project


@pytest.fixture(autouse=True)
def _no_bridge_by_default(monkeypatch) -> None:
    """Existing save tests exercise the no-bridge (first-window) path. Force it
    hermetically so they never depend on a live listener; §7 tests override."""
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: False)


def _project_with_yaml(projects_root: Path, name: str = "Alpha") -> Path:
    pdir = make_project(projects_root, name)
    ui = pdir / "Nodes" / "UI"
    ui.mkdir(parents=True, exist_ok=True)
    f = ui / "UI.yaml"
    f.write_text("SmokeLabel:\n  Text: old\n")
    old = time.time() - 3600
    os.utime(f, (old, old))
    return pdir


def test_save_detects_mtime_advance(cfg: core.Config, projects_root: Path) -> None:
    pdir = _project_with_yaml(projects_root, "Alpha")
    yaml = pdir / "Nodes" / "UI" / "UI.yaml"

    def handler(cmd, kwargs):
        # simulate Studio persisting the live model to disk on Ctrl+S
        yaml.write_text("SmokeLabel:\n  Text: new\n")
        return FakeProc(returncode=0, stdout="FOCUSED=True PID=1234")

    out = core.save(cfg, "Alpha", runner=make_fake_runner(handler))
    assert out["saved"] is True
    assert out["focused"] is True
    assert out["mtime_after"] > out["mtime_before"]


def test_save_no_studio_window(cfg: core.Config, projects_root: Path) -> None:
    _project_with_yaml(projects_root, "Alpha")

    def handler(cmd, kwargs):
        return FakeProc(returncode=3, stdout="NO_STUDIO")

    out = core.save(cfg, "Alpha", runner=make_fake_runner(handler))
    assert out["saved"] is False
    assert out["reason"] == "no_studio_window"


def test_save_timeout_when_no_change(cfg: core.Config, projects_root: Path) -> None:
    _project_with_yaml(projects_root, "Alpha")

    def handler(cmd, kwargs):
        # keystroke sent, Studio took focus, but nothing changed on disk
        return FakeProc(returncode=0, stdout="FOCUSED=True PID=1234")

    out = core.save(cfg, "Alpha", timeout=0.2, runner=make_fake_runner(handler))
    assert out["saved"] is False
    assert out["focused"] is True
    # saved=False + focused=True surfaces the UIPI integrity-mismatch hint
    assert "hint" in out and "integrity" in out["hint"].lower()


def test_save_sends_ctrl_s_via_powershell(cfg: core.Config, projects_root: Path) -> None:
    _project_with_yaml(projects_root, "Alpha")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    core.save(cfg, "Alpha", timeout=0.1, runner=runner)
    cmd = runner.calls[0][0]
    assert cmd[0] == "powershell"
    assert "SendKeys" in cmd[-1] and "^s" in cmd[-1]


def test_save_focuses_via_setforegroundwindow(cfg: core.Config, projects_root: Path) -> None:
    # Reliability fix: focus the real HWND with SetForegroundWindow (AppActivate
    # is only the fallback). AppActivate-only intermittently returned FOCUSED=False
    # and the keystroke landed nowhere -> silent saved=False. See core.py comment.
    _project_with_yaml(projects_root, "Alpha")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    core.save(cfg, "Alpha", timeout=0.1, runner=runner)
    ps = runner.calls[0][0][-1]
    assert "SetForegroundWindow" in ps
    assert "MainWindowHandle" in ps
    # force-foreground recipe: AttachThreadInput lifts the fg lock for a background
    # service child (a bare SetForegroundWindow is silently blocked). ALT is
    # deliberately NOT used (it would activate the menu and eat the Ctrl+S).
    assert "AttachThreadInput" in ps
    assert "keybd_event" not in ps
    assert "AppActivate" in ps  # kept as fallback


# ---- §7: save targets the bridge's Studio instance -----------------------

def test_save_no_bridge_uses_first_window(cfg: core.Config, projects_root: Path) -> None:
    # No bridge (the autouse fixture forces it): selection is the first focus-able
    # Studio window, NOT a pid-targeted pick.
    _project_with_yaml(projects_root, "Alpha")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    out = core.save(cfg, "Alpha", timeout=0.1, runner=runner)
    ps = runner.calls[0][0][-1]
    assert "Id -eq" not in ps            # not pid-targeted
    assert "MainWindowTitle -ne ''" in ps  # the first-window selection
    assert "bridge_pid" not in out


def test_save_targets_bridge_instance(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    pdir = _project_with_yaml(projects_root, "Alpha")
    yaml = pdir / "Nodes" / "UI" / "UI.yaml"
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_owner_pid", lambda cfg, runner=None: 4242)

    def handler(cmd, kwargs):
        # only the save PS (targeted) writes; assert it targets pid 4242
        yaml.write_text("SmokeLabel:\n  Text: new\n")
        return FakeProc(returncode=0, stdout="FOCUSED=True PID=4242")

    out = core.save(cfg, "Alpha", runner=make_fake_runner(handler))
    assert out["saved"] is True
    assert out["bridge_pid"] == 4242
    assert out["save_target_pid"] == 4242
    assert out["targeted_bridge_instance"] is True


def test_save_ps_targets_pid_when_bridge_serves(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    _project_with_yaml(projects_root, "Alpha")
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_owner_pid", lambda cfg, runner=None: 4242)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=4242"))
    core.save(cfg, "Alpha", timeout=0.1, runner=runner)
    ps = runner.calls[0][0][-1]
    assert "Id -eq 4242" in ps
    assert "NO_TARGET_WINDOW PID=4242" in ps


def test_save_bridge_studio_no_window(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    _project_with_yaml(projects_root, "Alpha")
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_owner_pid", lambda cfg, runner=None: 4242)
    runner = make_fake_runner(
        lambda cmd, kw: FakeProc(returncode=4, stdout="NO_TARGET_WINDOW PID=4242")
    )
    out = core.save(cfg, "Alpha", runner=runner)
    assert out["saved"] is False
    assert out["reason"] == "bridge_studio_no_window"
    assert out["bridge_pid"] == 4242
    assert "4242" in out["hint"]


def test_save_bridge_owner_unresolved_falls_back(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    # bridge serves the project but its listener pid can't be resolved -> first-window
    _project_with_yaml(projects_root, "Alpha")
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_owner_pid", lambda cfg, runner=None: None)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    out = core.save(cfg, "Alpha", timeout=0.1, runner=runner)
    ps = runner.calls[0][0][-1]
    assert "Id -eq" not in ps
    assert "bridge_pid" not in out


# ---- gentle focus (v1.1 backlog 1.3: no window resize on save / F5) ----------

def test_build_save_ps_gentle_by_default() -> None:
    """Default PS only SW_RESTOREs when minimized (IsIconic guard) and returns
    the foreground afterwards — a maximized Studio must never be resized."""
    ps = core._build_save_ps(0)
    assert "IsIconic" in ps and "if ([Ftx.FtxFg]::IsIconic($h))" in ps
    # the unconditional restore form must be gone
    assert "; [Ftx.FtxFg]::ShowWindow($h,9) | Out-Null; [Ftx.FtxFg]::BringWindowToTop" not in ps
    # foreground hand-back present
    assert "$fg -ne $h" in ps


def test_build_save_ps_legacy_restore_when_opted_out() -> None:
    ps = core._build_save_ps(0, gentle=False)
    assert "if ([Ftx.FtxFg]::IsIconic($h))" not in ps
    assert "ShowWindow($h,9)" in ps


def test_gentle_focus_env_escape_hatch(monkeypatch) -> None:
    monkeypatch.delenv("FTX_SAVE_GENTLE_FOCUS", raising=False)
    assert core._gentle_focus() is True  # default ON
    monkeypatch.setenv("FTX_SAVE_GENTLE_FOCUS", "0")
    assert core._gentle_focus() is False
    monkeypatch.setenv("FTX_SAVE_GENTLE_FOCUS", "false")
    assert core._gentle_focus() is False
    monkeypatch.setenv("FTX_SAVE_GENTLE_FOCUS", "1")
    assert core._gentle_focus() is True


def test_save_uses_gentle_ps_by_default(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    monkeypatch.delenv("FTX_SAVE_GENTLE_FOCUS", raising=False)
    make_project(projects_root, "Alpha")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    core.save(cfg, "Alpha", timeout=0.0, runner=runner)
    ps = runner.calls[0][0][-1]
    assert "IsIconic($h)" in ps


def test_run_emulator_f5_is_gentle_by_default(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    """The F5 path must not resize Studio either (pre-1.3 it hardcoded gentle=False)."""
    monkeypatch.delenv("FTX_SAVE_GENTLE_FOCUS", raising=False)
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: False)
    make_project(projects_root, "Alpha")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    ps = runner.calls[0][0][-1]
    assert "{F5}" in ps and "IsIconic($h)" in ps
