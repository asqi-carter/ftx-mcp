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
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: None)


@pytest.fixture(autouse=True)
def _short_resend_and_grace(monkeypatch) -> None:
    """Real-time tests use a short re-send cadence / grace so a no-write case
    does not wait out the production 5 s / 15 s. Virtual-clock tests set the
    production values explicitly."""
    monkeypatch.setattr(core, "_SAVE_RESEND_S", 0.2)
    monkeypatch.setattr(core, "_SAVE_WRITE_GRACE_S", 0.3)


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
    # focused + same integrity (probe unparseable here): no UIPI blame (ftx-mcp-h4a.2)
    assert "hint" in out and "NetSolution build" in out["hint"]
    assert "nothing unsaved" not in out["hint"]
    assert "UIPI" not in out["hint"]


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
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: cfg)
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
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: cfg)
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
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: cfg)
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
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: cfg)
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


def test_run_emulator_f5_is_gentle_by_default(f5_target_studio, cfg: core.Config, projects_root: Path, monkeypatch, tmp_path) -> None:
    """The F5 path must not resize Studio either (pre-1.3 it hardcoded gentle=False)."""
    monkeypatch.delenv("FTX_SAVE_GENTLE_FOCUS", raising=False)
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: False)
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: None)
    # F5 guard (v1.0.7) reads the real machine's Studio Configuration.xml
    # unless overridden — this test predates the guard and isn't testing
    # it, so point it at a path that doesn't exist (known=False -> fails
    # open), same fix as test_run_emulator.py's _emulator_target_by_default.
    monkeypatch.setenv("OPTIX_STUDIO_CONFIG_XML", str(tmp_path / "no-studio-config.xml"))
    make_project(projects_root, "Alpha")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1"))
    core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    ps = runner.calls[0][0][-1]
    assert "{F5}" in ps and "IsIconic($h)" in ps


# ---- (1.0.8) multi-Studio targeting without a bridge ---------------------

def _studio_scan(*entries):
    """[(pid, cmdline_tokens), ...] -> studio_guard scan records."""
    return [{"pid": pid, "name": "ftoptixstudio.exe", "cmdline": list(cmd)}
            for pid, cmd in entries]


def test_save_targets_studio_by_cmdline_when_no_bridge(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    """Two Studios open, no bridge: the one whose argv names THIS project's
    .optix gets the Ctrl+S — never the first window."""
    from service import studio_guard
    make_project(projects_root, "Alpha")
    make_project(projects_root, "Beta")
    alpha = str(projects_root / "Alpha" / "Alpha.optix")
    beta = str(projects_root / "Beta" / "Beta.optix")
    monkeypatch.setattr(studio_guard, "_scan", lambda: _studio_scan(
        (111, ["C:\\Studio\\FTOptixStudio.exe", alpha]),
        (222, ["C:\\Studio\\FTOptixStudio.exe", "open", beta])))
    studio_guard.reset_cache()
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda c, p: None)
    captured = {}

    def fake_build(target_pid=0, gentle=True, send_key="^s"):
        captured["pid"] = target_pid
        return "ps"
    monkeypatch.setattr(core, "_build_save_ps", fake_build)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=222"))
    core.save(cfg, "Beta", timeout=0.01, runner=runner)
    assert captured["pid"] == 222


def test_save_refuses_when_several_studios_and_none_attributable(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    from service import studio_guard
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(studio_guard, "_scan", lambda: _studio_scan(
        (111, ["C:\\Studio\\FTOptixStudio.exe"]),
        (222, ["C:\\Studio\\FTOptixStudio.exe"])))
    studio_guard.reset_cache()
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda c, p: None)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=111"))
    out = core.save(cfg, "Alpha", timeout=0.01, runner=runner)
    assert out["saved"] is False and out["reason"] == "ambiguous_studio"
    assert out["studio_count"] == 2
    assert runner.calls == []  # no keystroke was sent


def test_save_single_unattributable_studio_keeps_legacy_first_window(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    from service import studio_guard
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(studio_guard, "_scan", lambda: _studio_scan(
        (111, ["C:\\Studio\\FTOptixStudio.exe"])))
    studio_guard.reset_cache()
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda c, p: None)
    captured = {}

    def fake_build(target_pid=0, gentle=True, send_key="^s"):
        captured["pid"] = target_pid
        return "ps"
    monkeypatch.setattr(core, "_build_save_ps", fake_build)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=111"))
    core.save(cfg, "Alpha", timeout=0.01, runner=runner)
    assert captured["pid"] == 0


# ---- 1.0.9 #18: EnumWindows fallback when MainWindowHandle is 0 -------------

def test_build_save_ps_has_enumwindows_fallback_targeted() -> None:
    ps = core._build_save_ps(4242)
    for needle in ("EnumWindows", "GetWindowThreadProcessId", "IsWindowVisible",
                   "FindWindowForPid", "Optix"):
        assert needle in ps, needle
    # fallback is used only when Get-Process reports handle 0
    assert "if ($h -eq [System.IntPtr]::Zero)" in ps
    # unowned windows only (GW_OWNER == 4), largest area wins
    assert "GetWindow(h, 4)" in ps
    # the targeted pid is always honoured: process pick AND enumeration use it,
    # never another Studio
    assert "Id -eq 4242" in ps
    assert "FindWindowForPid([uint32]4242)" in ps
    assert "if (pid != want" in ps


def test_build_save_ps_no_target_window_only_after_enumeration() -> None:
    ps = core._build_save_ps(4242)
    assert ps.index("FindWindowForPid([uint32]4242)") < ps.index("NO_TARGET_WINDOW PID=4242")
    assert "exit 4" in ps


def test_build_save_ps_f5_variant_has_same_fallback() -> None:
    ps = core._build_save_ps(4242, send_key="{F5}")
    assert "SendWait('{F5}')" in ps
    assert "EnumWindows" in ps and "IsWindowVisible" in ps
    assert "FindWindowForPid([uint32]4242)" in ps
    assert "Id -eq 4242" in ps


def test_build_save_ps_untargeted_enumerates_per_studio_process() -> None:
    ps = core._build_save_ps(0)
    assert "EnumWindows" in ps and "FindWindowForPid([uint32]$c.Id)" in ps
    assert "NO_STUDIO" in ps and "NO_TARGET_WINDOW" not in ps


def test_save_and_f5_share_fallback_in_generated_ps(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    """save() and run_emulator() both go through _build_save_ps with the pid."""
    _project_with_yaml(projects_root, "Alpha")
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_bridge_owner_pid", lambda cfg, runner=None: 4242)
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=4242"))
    core.save(cfg, "Alpha", timeout=0.1, runner=runner)
    ps = runner.calls[0][0][-1]
    assert "EnumWindows" in ps and "FindWindowForPid([uint32]4242)" in ps


# ---- re-send until the write lands (PRD 0047 A, #54) -------------------------

class _Clock:
    """Virtual clock patched over ``core.time`` so 30 s saves run instantly."""

    def __init__(self) -> None:
        self.t = 1000.0

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@pytest.fixture
def vclock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(core, "time", clock)
    monkeypatch.setattr(core, "_SAVE_RESEND_S", 5.0)
    monkeypatch.setattr(core, "_SAVE_WRITE_GRACE_S", 15.0)
    return clock


def _virtual_save(cfg, projects_root, monkeypatch, vclock, write_after,
                  tree=None, **kw):
    """Run save() on the virtual clock. ``write_after(keys)`` -> absolute virtual
    time at which the YAML mtime advances (None = never); ``keys`` is the list of
    keystroke times so far. Returns (result, keystroke_times)."""
    from service import studio_arm
    _project_with_yaml(projects_root, "Alpha")
    keys: list[float] = []
    monkeypatch.setattr(
        studio_arm, "focus_project_tree",
        tree or (lambda *a, **k: {"ok": True, "clicked": "Alpha"}))
    monkeypatch.setattr(studio_arm, "probe_elevation", lambda runner=None: {
        "service_elevated": False, "studio": [{"pid": 1234, "elevated": False}]})

    def fake_mtime(_pdir) -> float:
        w = write_after(keys) if keys else None
        return 2.0 if w is not None and vclock.t >= w else 1.0

    def handler(cmd, kw):
        keys.append(vclock.t)
        return FakeProc(0, "FOCUSED=True PID=1234")

    monkeypatch.setattr(core, "_project_max_mtime", fake_mtime)
    out = core.save(cfg, "Alpha", runner=make_fake_runner(handler), **kw)
    return out, keys


def test_save_write_1s_after_first_key_is_ctrl_s(
    cfg: core.Config, projects_root: Path, monkeypatch, vclock
) -> None:
    out, keys = _virtual_save(
        cfg, projects_root, monkeypatch, vclock,
        lambda k: k[0] + 1.0)
    assert out["saved"] is True
    assert out["method"] == "ctrl_s"
    assert out["keystrokes_sent"] == 1 and len(keys) == 1
    assert 0.9 <= out["write_latency_s"] <= 1.2
    assert [a["method"] for a in out["attempts"]] == ["ctrl_s"]
    assert "hint" not in out
    for gone in ("title_clean", "menu_fallback"):
        assert gone not in out


def test_save_write_after_second_key_is_tree_focus_ctrl_s(
    cfg: core.Config, projects_root: Path, monkeypatch, vclock
) -> None:
    """2nd key at 5 s; the write lands 8 s after the first key (3 s after it),
    before the 3rd key (10 s)."""
    out, keys = _virtual_save(
        cfg, projects_root, monkeypatch, vclock,
        lambda k: k[0] + 8.0)
    assert out["saved"] is True
    assert out["method"] == "tree_focus_ctrl_s"
    assert out["keystrokes_sent"] == 2 and len(keys) == 2
    assert abs((keys[1] - keys[0]) - 5.0) < 0.2
    assert 2.8 <= out["write_latency_s"] <= 3.3
    assert [a["method"] for a in out["attempts"]] == ["ctrl_s", "tree_focus_ctrl_s"]
    assert all(a["ok"] for a in out["attempts"])


def test_save_nothing_for_30s_sends_six_keystrokes_and_hint_order(
    cfg: core.Config, projects_root: Path, monkeypatch, vclock
) -> None:
    """No write: keys at 0,5,..,25 (6), then 15 s grace; hint busy > focus > UIPI."""
    from service import studio_arm
    studio_arm_probe = {"service_elevated": False,
                        "studio": [{"pid": 1234, "elevated": True}]}
    out, keys = _virtual_save(
        cfg, projects_root, monkeypatch, vclock, lambda k: None)
    assert out["saved"] is False
    assert out["keystrokes_sent"] == 6 and len(keys) == 6
    assert out["elapsed_seconds"] <= 30.0 + 15.0 + 1.0
    assert "write_latency_s" not in out
    hint = out["hint"]
    assert "UIPI" not in hint                   # same integrity, focused
    assert "nothing unsaved" not in hint
    assert hint.index("busy") < hint.index("NetSolution build") < hint.index("Focus")
    assert 'optix_studio_log(contains="solution")' in hint
    # UIPI last, only when the integrity levels differ
    monkeypatch.setattr(studio_arm, "probe_elevation", lambda runner=None: studio_arm_probe)
    h2 = core._save_failed_hint(True, 1234, make_fake_runner(lambda c, k: FakeProc(0, "")),
                                out["attempts"])
    assert h2.index("busy") < h2.index("Focus") < h2.index("UIPI")


def test_save_unfocused_lists_uipi_last(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    _project_with_yaml(projects_root, "Alpha")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=False PID=1234"))
    out = core.save(cfg, "Alpha", timeout=0.3, runner=runner)
    assert out["saved"] is False and out["focused"] is False
    h = out["hint"]
    assert h.index("busy") < h.index("Focus") < h.index("UIPI")


def test_save_write_12s_after_sixth_key_inside_grace_is_saved(
    cfg: core.Config, projects_root: Path, monkeypatch, vclock
) -> None:
    out, keys = _virtual_save(
        cfg, projects_root, monkeypatch, vclock,
        lambda k: (k[5] + 12.0) if len(k) >= 6 else None)
    assert out["saved"] is True
    assert out["keystrokes_sent"] == 6
    assert out["method"] == "tree_focus_ctrl_s"
    assert 11.8 <= out["write_latency_s"] <= 12.3
    assert "hint" not in out


def test_save_write_after_grace_expires_is_not_saved(
    cfg: core.Config, projects_root: Path, monkeypatch, vclock
) -> None:
    out, keys = _virtual_save(
        cfg, projects_root, monkeypatch, vclock,
        lambda k: (k[5] + 16.0) if len(k) >= 6 else None)
    assert out["saved"] is False and out["keystrokes_sent"] == 6


def test_save_tree_row_missing_still_resends_plain_ctrl_s(
    cfg: core.Config, projects_root: Path, monkeypatch, vclock
) -> None:
    out, keys = _virtual_save(
        cfg, projects_root, monkeypatch, vclock,
        lambda k: k[0] + 8.0,
        tree=lambda *a, **k: {"ok": False, "error": "tree_row_not_found"})
    assert out["saved"] is True
    assert out["keystrokes_sent"] == 2
    assert out["method"] == "ctrl_s"            # the tree was never focused
    assert out["attempts"][1]["error"] == "tree_row_not_found"
    assert "file_menu_not_found" not in str(out)


def test_save_slow_tree_click_does_not_eat_poll_budget(
    cfg: core.Config, projects_root: Path, monkeypatch, vclock
) -> None:
    """A 4 s UIA fallback, then a write 10 s after that keystroke: saved."""
    def slow_tree(*a, **k):
        vclock.t += 4.0
        return {"ok": True, "clicked": "Alpha"}

    out, keys = _virtual_save(
        cfg, projects_root, monkeypatch, vclock,
        lambda k: (k[1] + 10.0) if len(k) >= 2 else None, tree=slow_tree)
    assert out["saved"] is True
    assert out["method"] == "tree_focus_ctrl_s"


def test_save_resend_failure_stops_sending_but_keeps_polling(
    cfg: core.Config, projects_root: Path, monkeypatch, vclock
) -> None:
    from service import studio_arm
    _project_with_yaml(projects_root, "Alpha")
    monkeypatch.setattr(studio_arm, "focus_project_tree",
                        lambda *a, **k: {"ok": True, "clicked": "Alpha"})
    n = {"calls": 0}

    def handler(cmd, kw):
        n["calls"] += 1
        if n["calls"] >= 2:
            return FakeProc(3, "NO_STUDIO")
        return FakeProc(0, "FOCUSED=True PID=1234")

    out = core.save(cfg, "Alpha", runner=make_fake_runner(handler))
    assert out["saved"] is False
    assert out["keystrokes_sent"] == 1 and n["calls"] >= 2
    assert out["attempts"][-1]["error"] == "ctrl_s_not_sent"


def test_save_no_uia_when_first_key_takes(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    from service import studio_arm
    pdir = _project_with_yaml(projects_root, "Alpha")
    yaml = pdir / "Nodes" / "UI" / "UI.yaml"
    uia: list = []
    monkeypatch.setattr(studio_arm, "focus_project_tree",
                        lambda *a, **k: uia.append("tree") or {"ok": True})

    def handler(cmd, kw):
        yaml.write_text("a:\n  b: 1\n")
        return FakeProc(0, "FOCUSED=True PID=1234")

    out = core.save(cfg, "Alpha", runner=make_fake_runner(handler))
    assert out["saved"] is True and out["method"] == "ctrl_s"
    assert uia == [] and out["keystrokes_sent"] == 1


def _fake_create_netlogic_bridge(monkeypatch, kind: str) -> None:
    """Fake the bridge response WITHOUT a netsolution_build note: the service
    itself must add it."""
    monkeypatch.setattr(core, "_create_netlogic_post",
                        lambda *a, **k: {"ok": True, "kind": kind,
                                         "created_path": "Model/Zz"})
    monkeypatch.setattr(core, "_await_netlogic_template", lambda *a, **k: None)
    monkeypatch.setattr(core, "_netlogic_late_guard", lambda *a, **k: None)
    monkeypatch.setattr(core, "_attach_late_warnings", lambda *a, **k: None)
    monkeypatch.setattr(core, "_snapshot_netlogic_source", lambda *a, **k: None)
    monkeypatch.setattr(core, "_clear_netlogic_restores", lambda *a, **k: None)


_EXPECTED_NETSOLUTION_NOTE = (
    "Studio rebuilds the user .NET solution after a NetLogic create (about 30 s on "
    "the bench; builds while the bridge is armed can report MSB3027 locked-DLL "
    "warnings in the Studio log). Studio drops Ctrl+S during the build; optix_save "
    "re-sends until it lands.")


def test_bridge_create_netlogic_design_time_reports_netsolution_build(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    _fake_create_netlogic_bridge(monkeypatch, "design_time")
    out = core.bridge_create_netlogic(cfg, "Alpha", "Model", "Zz", kind="design_time")
    nb = out["netsolution_build"]
    assert nb["state"] == "building"
    assert nb["note"] == _EXPECTED_NETSOLUTION_NOTE


def test_bridge_create_netlogic_runtime_default_reports_netsolution_build(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    _fake_create_netlogic_bridge(monkeypatch, "runtime")
    rt = core.bridge_create_netlogic(cfg, "Alpha", "Model", "Zz")
    assert rt["netsolution_build"] == {
        "state": "building", "note": _EXPECTED_NETSOLUTION_NOTE}


def test_save_via_menu_without_uiautomation_is_structured_error(monkeypatch) -> None:
    import sys
    from service import studio_arm
    monkeypatch.setitem(sys.modules, "uiautomation", None)
    out = studio_arm.save_via_menu("Alpha")
    assert out["ok"] is False and out["error"] == "uiautomation_unavailable"


def test_save_default_timeout_is_30s(cfg: core.Config) -> None:
    assert core.Config.save_timeout_seconds == 30.0
    import inspect
    from service import mcp_app
    assert "timeout: float = 30.0" in inspect.getsource(mcp_app.make_mcp)


def test_focus_project_tree_clicks_row_and_restores(monkeypatch) -> None:
    """studio_arm.focus_project_tree: row found via _candidate_rows, left-clicked
    at its centre, cursor and foreground restored; missing row -> error."""
    import sys
    import types
    from service import studio_arm

    class Rect:
        left, top = 10, 20
        def width(self): return 100
        def height(self): return 30

    clicks: list = []
    restored: list = []
    fg = types.SimpleNamespace(SetActive=lambda: restored.append("fg"))
    win = types.SimpleNamespace(SetActive=lambda: None, ProcessId=77)
    fake = types.SimpleNamespace(
        Click=lambda x, y: clicks.append((x, y)),
        GetCursorPos=lambda: (5, 6),
        SetCursorPos=lambda x, y: restored.append(("cur", x, y)),
        GetForegroundControl=lambda: fg)
    monkeypatch.setitem(sys.modules, "uiautomation", fake)
    monkeypatch.setattr(studio_arm, "_studio_window_for", lambda *a, **k: win)
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    row = types.SimpleNamespace(Name="Alpha", BoundingRectangle=Rect())
    monkeypatch.setattr(studio_arm, "_candidate_rows", lambda w, names: [row])
    out = studio_arm.focus_project_tree("Alpha", aliases=set(), studio_pid=77)
    assert out == {"ok": True, "clicked": "Alpha"}
    assert clicks == [(60, 35)]
    assert ("cur", 5, 6) in restored and "fg" in restored
    monkeypatch.setattr(studio_arm, "_candidate_rows", lambda w, names: [])
    out = studio_arm.focus_project_tree("Alpha", aliases=set(), studio_pid=77)
    assert out["error"] == "tree_row_not_found"
    # a window of a different Studio pid is never clicked
    monkeypatch.setattr(studio_arm, "_candidate_rows", lambda w, names: [row])
    clicks.clear()
    out = studio_arm.focus_project_tree("Alpha", aliases=set(), studio_pid=99)
    assert out["error"] == "studio_window_not_found" and clicks == []


def test_new_command_line_attributes_studio_to_root_name(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    """`new X <root>` on the command line attributes that Studio to <root>/X."""
    from service import studio_guard
    make_project(projects_root, "Alpha")
    make_project(projects_root, "Beta")
    monkeypatch.setattr(studio_guard, "_scan", lambda: _studio_scan(
        (111, ["C:\\Studio\\FTOptixStudio.exe"]),
        (222, ["C:\\Studio\\FTOptixStudio.exe", "new", "Beta", str(projects_root), "--silent"])))
    studio_guard.reset_cache()
    assert core._studio_pid_for_project(projects_root / "Beta") == 222
    assert core._studio_pid_for_project(projects_root / "Alpha") == 0
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda c, p: None)
    captured = {}
    monkeypatch.setattr(core, "_build_save_ps",
                        lambda target_pid=0, gentle=True, send_key="^s": captured.setdefault("pid", target_pid) and "ps")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=222"))
    out = core.save(cfg, "Beta", timeout=0.01, runner=runner)
    assert out.get("reason") != "ambiguous_studio" and captured["pid"] == 222


def test_save_new_launched_project_with_pid_on_record_is_not_ambiguous(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    """A Studio this service launched (pid on record) is targeted even with
    several Studios open and nothing on any command line."""
    from service import studio_guard
    make_project(projects_root, "Gamma")
    monkeypatch.setattr(studio_guard, "_scan", lambda: _studio_scan(
        (111, ["C:\\Studio\\FTOptixStudio.exe"]),
        (333, ["C:\\Studio\\FTOptixStudio.exe"])))
    studio_guard.reset_cache()
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda c, p: None)
    monkeypatch.setattr(core, "_STUDIO_LAUNCH_DIRS", {})
    core._record_studio_launch(333, projects_root / "Gamma")
    captured = {}
    monkeypatch.setattr(core, "_build_save_ps",
                        lambda target_pid=0, gentle=True, send_key="^s": captured.setdefault("pid", target_pid) and "ps")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=333"))
    out = core.save(cfg, "Gamma", timeout=0.01, runner=runner)
    assert out.get("reason") != "ambiguous_studio" and captured["pid"] == 333
    # a dead pid's record is dropped, restoring the refusal
    monkeypatch.setattr(studio_guard, "_scan", lambda: _studio_scan(
        (111, ["C:\\Studio\\FTOptixStudio.exe"]),
        (444, ["C:\\Studio\\FTOptixStudio.exe"])))
    studio_guard.reset_cache()
    out = core.save(cfg, "Gamma", timeout=0.01, runner=runner)
    assert out["saved"] is False and out["reason"] == "ambiguous_studio"


# ---- PRD 0049 E (#66): failure causes are attributed honestly ----------------

def test_save_dead_target_pid_is_studio_exited_without_busy_or_focus_text(
    cfg: core.Config, projects_root: Path, monkeypatch, vclock
) -> None:
    monkeypatch.setattr(core, "_studio_pid_for_project", lambda *a, **k: 4321)
    monkeypatch.setattr(core.psutil, "pid_exists", lambda pid: False)
    out, _keys = _virtual_save(
        cfg, projects_root, monkeypatch, vclock, lambda k: None)
    assert out["saved"] is False
    assert out["cause"] == "studio_exited"
    hint = out["hint"]
    assert "4321" in hint and "closed or crashed during the save" in hint
    low = hint.lower()
    assert "busy" not in low and "focus" not in low and "uipi" not in low


def test_save_live_target_pid_keeps_busy_focus_hint_and_cause(
    cfg: core.Config, projects_root: Path, monkeypatch, vclock
) -> None:
    monkeypatch.setattr(core, "_studio_pid_for_project", lambda *a, **k: 4321)
    monkeypatch.setattr(core.psutil, "pid_exists", lambda pid: True)
    out, _keys = _virtual_save(
        cfg, projects_root, monkeypatch, vclock, lambda k: None)
    assert out["cause"] == "busy_or_focus"
    assert out["hint"].index("busy") < out["hint"].index("Focus")


def test_save_studio_log_access_denied_is_write_denied_with_path(
    cfg: core.Config, projects_root: Path, monkeypatch, tmp_path
) -> None:
    logdir = tmp_path / "studiolog"
    logdir.mkdir()
    log = logdir / "FTOptixStudio.0.log"
    log.write_text("old line\n", encoding="utf-8")
    monkeypatch.setenv("OPTIX_STUDIO_LOG_DIR", str(logdir))
    _project_with_yaml(projects_root, "Alpha")
    denied = r"C:\Projects\Alpha\Nodes\Alpha.yaml"

    def handler(cmd, kw):
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f'ERROR copy_file: Access is denied: "{denied}"\n')
        return FakeProc(0, "FOCUSED=True PID=1234")

    out = core.save(cfg, "Alpha", timeout=0.3, runner=make_fake_runner(handler))
    assert out["saved"] is False
    assert out["cause"] == "write_denied"
    assert denied in out["hint"] and out["denied_path"] == denied
    assert "busy" not in out["hint"].lower()


def test_save_old_denied_log_lines_before_first_key_are_ignored(
    cfg: core.Config, projects_root: Path, monkeypatch, tmp_path
) -> None:
    log = tmp_path / "FTOptixStudio.0.log"
    log.write_text('copy_file: Access is denied: "C:\\x\\old.yaml"\n', encoding="utf-8")
    monkeypatch.setenv("OPTIX_STUDIO_LOG_DIR", str(tmp_path))
    _project_with_yaml(projects_root, "Alpha")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=1234"))
    out = core.save(cfg, "Alpha", timeout=0.3, runner=runner)
    assert out["cause"] == "busy_or_focus"


def test_save_success_has_no_failure_cause(cfg: core.Config, projects_root: Path) -> None:
    pdir = _project_with_yaml(projects_root, "Alpha")
    yaml = pdir / "Nodes" / "UI" / "UI.yaml"

    def handler(cmd, kw):
        yaml.write_text("SmokeLabel:\n  Text: new\n")
        return FakeProc(0, "FOCUSED=True PID=1234")

    out = core.save(cfg, "Alpha", runner=make_fake_runner(handler))
    assert out["saved"] is True and "cause" not in out


# ---- PRD 0051 (#67): hint attribution ----------------------------------------

def test_denied_path_names_destination_under_project_with_source_secondary() -> None:
    proj = r"C:\Projects\Alpha"
    line = (r'ERROR copy_file: Access is denied: "C:\Users\u\AppData\Local\Temp\a.yaml"'
            r' -> "C:\Projects\Alpha\Nodes\Alpha.yaml"')
    path, got = core._denied_path_from_log([line], proj)
    assert path == r"C:\Projects\Alpha\Nodes\Alpha.yaml" and got == line
    assert core._denied_paths(line, proj) == (
        r"C:\Projects\Alpha\Nodes\Alpha.yaml",
        r"C:\Users\u\AppData\Local\Temp\a.yaml")
    # destination first on the line: still the one under the project
    rev = (r'copy_file C:\Projects\Alpha\Nodes\Alpha.yaml from '
           r'C:\Temp\a.yaml: Access is denied')
    assert core._denied_paths(rev, proj)[0] == r"C:\Projects\Alpha\Nodes\Alpha.yaml"
    # neither under the project -> the last path; a single path has no source
    other = r'copy_file: Access is denied: "C:\a\x.yaml" -> "D:\b\y.yaml"'
    assert core._denied_paths(other, proj) == (r"D:\b\y.yaml", r"C:\a\x.yaml")
    assert core._denied_paths(r'Access is denied: "C:\a\x.yaml"', proj) == (
        r"C:\a\x.yaml", None)


def test_write_denied_classification_carries_destination_and_source(monkeypatch) -> None:
    proj = r"C:\Projects\Alpha"
    dest, src = r"C:\Projects\Alpha\Nodes\Alpha.yaml", r"C:\Temp\stage\Alpha.yaml"
    monkeypatch.setattr(core, "_save_target_gone", lambda pid: False)
    monkeypatch.setattr(core, "_studio_log_since", lambda mark: [
        f'ERROR copy_file: Access is denied: "{src}" -> "{dest}"'])
    cause, hint, extra = core._classify_save_failure(
        True, 1234, None, [], 0, proj)
    assert cause == "write_denied"
    assert extra["denied_path"] == dest and extra["denied_source"] == src
    assert dest in hint and src not in hint


def _two_studios(monkeypatch, titles):
    """Two unattributable Studios (pids 111, 222) whose windows have `titles`."""
    from service import studio_arm, studio_guard
    monkeypatch.setattr(studio_guard, "_scan", lambda: _studio_scan(
        (111, ["C:\\Studio\\FTOptixStudio.exe"]),
        (222, ["C:\\Studio\\FTOptixStudio.exe"])))
    studio_guard.reset_cache()
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda c, p: None)
    monkeypatch.setattr(studio_arm, "_win32_top_level_windows", lambda: [
        {"title": t, "pid": pid, "rect": (0, 0, 9, 9), "foreground": False}
        for pid, t in titles.items()])


def test_save_project_not_open_when_no_studio_names_it(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    make_project(projects_root, "Alpha")
    _two_studios(monkeypatch, {111: "Other - FactoryTalk Optix Studio",
                               222: "FactoryTalk Optix Studio"})
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=111"))
    out = core.save(cfg, "Alpha", timeout=0.01, runner=runner)
    assert out["saved"] is False and out["reason"] == "project_not_open"
    assert out["error"] == "project_not_open"
    assert out["hint"] == "optix_project open Alpha"
    assert runner.calls == []


def test_save_one_studio_naming_it_is_targeted_two_is_ambiguous(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    make_project(projects_root, "Alpha")
    _two_studios(monkeypatch, {111: "Other - FactoryTalk Optix Studio",
                               222: "Alpha - FactoryTalk Optix Studio"})
    captured = {}
    monkeypatch.setattr(core, "_build_save_ps",
                        lambda target_pid=0, gentle=True, send_key="^s":
                        captured.setdefault("pid", target_pid) and "ps")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=222"))
    out = core.save(cfg, "Alpha", timeout=0.01, runner=runner)
    assert out.get("reason") not in ("project_not_open", "ambiguous_studio")
    assert captured["pid"] == 222
    _two_studios(monkeypatch, {111: "Alpha - FactoryTalk Optix Studio",
                               222: "Alpha* - FactoryTalk Optix Studio"})
    out = core.save(cfg, "Alpha", timeout=0.01, runner=runner)
    assert out["reason"] == "ambiguous_studio" and out["studio_count"] == 2


def test_run_emulator_project_not_open_and_ambiguous(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    make_project(projects_root, "Alpha")
    _two_studios(monkeypatch, {111: "Other - FactoryTalk Optix Studio",
                               222: "FactoryTalk Optix Studio"})
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "FOCUSED=True PID=111"))
    out = core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    assert out["ok"] is False and out["error"] == "project_not_open"
    assert out["hint"] == "optix_project open Alpha" and runner.calls == []
    _two_studios(monkeypatch, {111: "Alpha - FactoryTalk Optix Studio",
                               222: "Alpha - FactoryTalk Optix Studio"})
    out = core.run_emulator(cfg, "Alpha", wait_ready=False, runner=runner)
    assert out["error"] == "ambiguous_studio" and out["studio_count"] == 2
