"""Tests for runtime_start / runtime_stop (core + MCP/HTTP surface).

These tests inject a fake spawn (so no real FTOptixRuntime.exe is launched)
and a fake socket probe (so no real port binding is required). They cover:

- success: spawn invoked once, port reachable, state=running, pid surfaced
- not_reachable: port never binds, state=not_reachable, pid still surfaced
- missing runtime tree → ProjectNotFound
- missing FTOptixRuntime.exe → RuntimeBinaryNotFound
- invalid project name (path traversal) → ProjectNotFound
- stop: invokes RuntimeController.stop, returns state=stopped
- runtime_dir not configured → RuntimeDirNotConfigured
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from service import core

from .conftest import make_fake_runner


def _make_runtime_tree(runtime_dir: Path, project: str) -> Path:
    """Create a fake runtime tree mimicking Studio's export layout."""
    project_dir = runtime_dir / project
    app_dir = project_dir / "FTOptixApplication"
    app_dir.mkdir(parents=True)
    exe = app_dir / "FTOptixRuntime.exe"
    exe.write_bytes(b"fake-runtime")
    return project_dir


def test_runtime_start_success(cfg: core.Config, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_runtime_tree(runtime_dir, "Proj")
    spawned: list[Path] = []

    def fake_spawn(exe: Path) -> int:
        spawned.append(exe)
        return 4242

    # J: probe is called twice. First is the idempotency check (port not
    # yet bound -> False), then after spawn the post-spawn poll sees the
    # port bound -> True.
    calls = {"n": 0}
    def fake_probe(host: str, port: int, timeout: float = 0.5) -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(core, "_tcp_probe", fake_probe)

    result = core.runtime_start(cfg, "Proj", spawn=fake_spawn, timeout=2.0)

    assert result["state"] == "running"
    assert result["project"] == "Proj"
    assert result["port"] == cfg.runtime_test_port
    assert result["pid"] == 4242
    assert result["tcp_reachable"] is True
    assert result["confirmed_at"] is not None
    assert result["timeout_seconds"] == 2.0
    assert len(spawned) == 1
    assert spawned[0].name == "FTOptixRuntime.exe"


def test_runtime_start_already_running_skips_spawn(
    cfg: core.Config, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """J: a runtime_start call against an already-bound port must NOT
    spawn a second runtime. Returns state=already_running with pid=None
    so repeat calls are safe.
    """
    _make_runtime_tree(runtime_dir, "Proj")
    spawned: list[Path] = []

    def fake_spawn(exe: Path) -> int:
        spawned.append(exe)
        return -1  # should never be reached

    # Idempotency probe sees the port already bound on the first try.
    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **kw: True)

    result = core.runtime_start(cfg, "Proj", spawn=fake_spawn, timeout=2.0)

    assert result["state"] == "already_running"
    assert result["pid"] is None
    assert result["tcp_reachable"] is True
    assert result["confirmed_at"] is not None
    assert spawned == []


def test_runtime_start_explicit_port(cfg: core.Config, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_runtime_tree(runtime_dir, "Proj")
    seen_ports: list[int] = []
    calls = {"n": 0}

    def fake_probe(host: str, port: int, timeout: float = 0.5) -> bool:
        seen_ports.append(port)
        calls["n"] += 1
        return calls["n"] > 1  # idempotency check False, post-spawn True

    monkeypatch.setattr(core, "_tcp_probe", fake_probe)

    result = core.runtime_start(
        cfg, "Proj", port=9999, spawn=lambda _: 1, timeout=2.0
    )
    assert result["port"] == 9999
    assert 9999 in seen_ports


def test_runtime_start_not_reachable(cfg: core.Config, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_runtime_tree(runtime_dir, "Proj")
    monkeypatch.setattr(core, "_tcp_probe", lambda host, port, timeout=0.5: False)

    # Short timeout — the poll loop honors verify_poll_seconds=0.05 from the
    # cfg fixture, so this returns in well under a second.
    result = core.runtime_start(
        cfg, "Proj", spawn=lambda _: 99, timeout=0.2
    )

    assert result["state"] == "not_reachable"
    assert result["pid"] == 99
    assert result["tcp_reachable"] is False
    assert result["confirmed_at"] is None
    assert result["elapsed_seconds"] >= 0.2


def test_runtime_start_missing_tree(cfg: core.Config) -> None:
    with pytest.raises(core.ProjectNotFound):
        core.runtime_start(cfg, "NotDeployed", spawn=lambda _: 0)


def test_runtime_start_missing_exe(cfg: core.Config, runtime_dir: Path) -> None:
    # Tree exists but neither an FTOptixApplication bundle NOR a .optix +
    # shared-exe fallback is present -> still RuntimeBinaryNotFound.
    (runtime_dir / "Proj").mkdir()
    with pytest.raises(core.RuntimeBinaryNotFound):
        core.runtime_start(cfg, "Proj", spawn=lambda _: 0)


def test_runtime_start_bundle_mode_reported(cfg: core.Config, runtime_dir: Path) -> None:
    # Export-bundle present -> mode=bundle, spawns the bundled exe.
    proj_dir = _make_runtime_tree(runtime_dir, "Proj")
    spawned: list[Path] = []
    result = core.runtime_start(
        cfg, "Proj", port=59998, spawn=lambda e: spawned.append(e) or 11, timeout=0.1
    )
    assert result["mode"] == "bundle"
    assert spawned == [proj_dir / "FTOptixApplication" / "FTOptixRuntime.exe"]


def test_runtime_start_shared_exe_mode(cfg: core.Config, runtime_dir: Path) -> None:
    # ApplicationFiles-style tree: a .optix but NO FTOptixApplication bundle,
    # plus the shared FTOptixRuntime.exe in the Studio install -> Path-B shared
    # mode launches the shared exe against the project .optix (no export).
    proj_dir = runtime_dir / "Proj"
    proj_dir.mkdir()
    (proj_dir / "Proj.optix").write_text("optix")
    shared = (
        cfg.studio_exe.parent / "FTOptixRuntime" / "1.7.1.46" / "Win32_x64" / "FTOptixRuntime.exe"
    )
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b"shared-runtime")
    spawned: list[Path] = []
    result = core.runtime_start(
        cfg, "Proj", port=59997, spawn=lambda e: spawned.append(e) or 22, timeout=0.1
    )
    assert result["mode"] == "shared"
    assert result["pid"] == 22
    assert spawned == [shared]
    assert result["runtime_exe"] == str(shared)


def test_runtime_start_path_traversal(cfg: core.Config) -> None:
    with pytest.raises(core.ProjectNotFound):
        core.runtime_start(cfg, "../escape", spawn=lambda _: 0)
    with pytest.raises(core.ProjectNotFound):
        core.runtime_start(cfg, "with/slash", spawn=lambda _: 0)


def test_runtime_start_runtime_dir_not_configured(tmp_path: Path) -> None:
    studio = tmp_path / "studio.exe"
    studio.write_text("fake")
    cfg_no_runtime = core.Config(
        projects_root=tmp_path / "proj",
        studio_exe=studio,
        state_dir=tmp_path / "state",
        runtime_dir=None,
    )
    with pytest.raises(core.RuntimeDirNotConfigured):
        core.runtime_start(cfg_no_runtime, "Proj", spawn=lambda _: 0)


def test_runtime_stop_invokes_controller(
    cfg: core.Config, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RuntimeController.stop early-returns on non-Windows; pretend we're on
    # Windows so the WMI/PowerShell pipeline path exercises.
    monkeypatch.setattr(core.os, "name", "nt")
    project_dir = _make_runtime_tree(runtime_dir, "Proj")
    runner = make_fake_runner()

    result = core.runtime_stop(cfg, "Proj", runner=runner)

    assert result["state"] == "stopped"
    assert result["project"] == "Proj"
    assert result["runtime_project_dir"] == str(project_dir.resolve())
    assert len(runner.calls) == 1
    cmd = runner.calls[0][0]
    assert cmd[0] == "powershell"
    assert any("FTOptixRuntime" in arg for arg in cmd)
    # The CommandLine match must be boundary-anchored (dir + separator) so a
    # sibling project 'Proj2' can't be caught by a bare 'Proj' substring match.
    ps = next(arg for arg in cmd if "CommandLine" in arg)
    assert (str(project_dir.resolve()) + os.sep) in ps


def test_runtime_stop_non_windows_no_op(cfg: core.Config, runtime_dir: Path) -> None:
    # On non-Windows the RuntimeController.stop early-returns without touching
    # the runner. The function shape still returns the success envelope.
    if core.os.name == "nt":
        pytest.skip("test asserts non-Windows behavior")
    _make_runtime_tree(runtime_dir, "Proj")
    runner = make_fake_runner()
    result = core.runtime_stop(cfg, "Proj", runner=runner)
    assert result["state"] == "stopped"
    assert len(runner.calls) == 0


def test_runtime_stop_missing_tree(cfg: core.Config) -> None:
    with pytest.raises(core.ProjectNotFound):
        core.runtime_stop(cfg, "NotDeployed")


def test_runtime_stop_runtime_dir_not_configured(tmp_path: Path) -> None:
    studio = tmp_path / "studio.exe"
    studio.write_text("fake")
    cfg_no_runtime = core.Config(
        projects_root=tmp_path / "proj",
        studio_exe=studio,
        state_dir=tmp_path / "state",
        runtime_dir=None,
    )
    with pytest.raises(core.RuntimeDirNotConfigured):
        core.runtime_stop(cfg_no_runtime, "Proj")


def test_runtime_controller_start_falls_back_to_default_spawn(
    cfg: core.Config, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When cfg.runtime_launcher is None, RuntimeController.start spawns
    FTOptixRuntime.exe directly via _default_runtime_spawn. This is the
    Joe-laptop default — no scheduled-task launcher needed for deploy
    bounce to relaunch the runtime."""
    project_dir = _make_runtime_tree(runtime_dir, "Proj")
    cfg_no_launcher = dataclasses.replace(cfg, runtime_launcher=None)
    spawned: list[Path] = []
    monkeypatch.setattr(
        core, "_default_runtime_spawn",
        lambda exe: spawned.append(exe) or 4242,
    )
    controller = core.RuntimeController()
    controller.start(cfg_no_launcher, project_dir)
    assert len(spawned) == 1
    assert spawned[0].name == "FTOptixRuntime.exe"
    assert spawned[0].parent == project_dir / "FTOptixApplication"


def test_runtime_controller_start_uses_configured_launcher(
    cfg: core.Config, runtime_dir: Path
) -> None:
    """When cfg.runtime_launcher is set, RuntimeController.start invokes
    the launcher (scheduled-task name). The direct-spawn fallback is
    skipped — this is the v0.1 path for installs that provision a
    dedicated runtime-launcher task."""
    project_dir = _make_runtime_tree(runtime_dir, "Proj")
    cfg_launcher = dataclasses.replace(cfg, runtime_launcher="MyLauncherTask")
    runner = make_fake_runner()
    controller = core.RuntimeController(runner=runner)
    controller.start(cfg_launcher, project_dir)
    assert len(runner.calls) == 1
    cmd = runner.calls[0][0]
    assert cmd[0] == "powershell"
    assert any("Start-ScheduledTask" in arg for arg in cmd)
    assert any("MyLauncherTask" in arg for arg in cmd)


def test_runtime_controller_start_no_launcher_missing_exe_is_noop(
    cfg: core.Config, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If no launcher AND no FTOptixRuntime.exe on disk (e.g. swap
    aborted mid-deploy), start is a silent no-op. The deploy's verify
    step reports not-reachable; treating this as 'launcher missing' would
    mask the real swap-output bug."""
    project_dir = runtime_dir / "Proj"
    project_dir.mkdir()
    cfg_no_launcher = dataclasses.replace(cfg, runtime_launcher=None)
    spawned: list[Path] = []
    monkeypatch.setattr(
        core, "_default_runtime_spawn",
        lambda exe: spawned.append(exe) or 0,
    )
    controller = core.RuntimeController()
    controller.start(cfg_no_launcher, project_dir)
    assert spawned == []


def test_minimize_windows_for_pid_non_windows_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """K: helper is a strict no-op on non-Windows. Returns 0 without
    touching ctypes.windll (which doesn't exist on Linux/macOS) so the
    surrounding try/except in _default_runtime_spawn never fires.
    """
    monkeypatch.setattr(core.os, "name", "posix")
    assert core._minimize_windows_for_pid(12345) == 0


def test_minimize_windows_for_pid_windows_invokes_show_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K: on Windows, helper enumerates visible top-level windows owned
    by `pid` and calls ShowWindow(hwnd, SW_MINIMIZE=6) on each. Mocks
    ctypes.windll.user32 so the test runs on any platform.

    Patches needed because the helper imports ctypes lazily and uses
    Windows-only symbols (windll, WINFUNCTYPE) that don't exist on Linux:
      - ctypes.windll: missing on non-Windows; replace with our fake.
      - ctypes.WINFUNCTYPE: missing on non-Windows; replace with a
        passthrough decorator (callback gets invoked as a plain Python
        function by our fake EnumWindows, so no C-callback wrap needed).
      - ctypes.byref: real impl returns an opaque CArgObject we can't
        write through; identity-replace so our fake
        GetWindowThreadProcessId can set window_pid.value directly.
      - wintypes.DWORD: replace with a trivial mutable holder for the
        same reason — the helper reads .value after the API call.
    """
    import ctypes
    from ctypes import wintypes

    monkeypatch.setattr(core.os, "name", "nt")

    class FakeDWORD:
        def __init__(self) -> None:
            self.value = 0

    monkeypatch.setattr(ctypes, "byref", lambda x: x)
    monkeypatch.setattr(wintypes, "DWORD", FakeDWORD)
    monkeypatch.setattr(
        ctypes, "WINFUNCTYPE", lambda *_types: (lambda fn: fn), raising=False
    )

    show_window_calls: list[tuple[int, int]] = []
    target_pid = 4242

    # PascalCase method names below mirror the Win32 user32 API so the
    # helper's attribute lookups (user32.EnumWindows, etc.) resolve to
    # these fakes. N802 is suppressed per-method, not by class rename.
    class FakeUser32:
        def EnumWindows(self, callback, lparam):  # noqa: N802
            # Three hwnds: 101 visible owned by target, 202 visible
            # owned by other pid (must NOT be minimized), 303 visible
            # owned by target. So only 101 and 303 should be in the
            # minimized set.
            for hwnd, owning_pid, visible in (
                (101, target_pid, 1),
                (202, 9999, 1),
                (303, target_pid, 1),
            ):
                self._next_pid = owning_pid
                self._next_visible = visible
                callback(hwnd, lparam)
            return 1

        def GetWindowThreadProcessId(self, hwnd, pid_dword):  # noqa: N802
            pid_dword.value = self._next_pid
            return 1

        def IsWindowVisible(self, hwnd):  # noqa: N802
            return self._next_visible

        def ShowWindow(self, hwnd, cmd):  # noqa: N802
            show_window_calls.append((hwnd, cmd))
            return 1

    class FakeWinDLL:
        user32 = FakeUser32()

    monkeypatch.setattr(ctypes, "windll", FakeWinDLL(), raising=False)

    n = core._minimize_windows_for_pid(target_pid, timeout=1.0)

    # SW_MINIMIZE = 6 per Win32 ShowWindow nCmdShow constants.
    assert n == 2
    assert show_window_calls == [(101, 6), (303, 6)]
