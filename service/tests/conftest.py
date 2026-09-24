"""Shared fixtures for service tests."""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from service import core, studio_guard


@pytest.fixture(autouse=True)
def _no_host_processes(monkeypatch: pytest.MonkeyPatch):
    """Isolate the suite from the host's real process table.

    The corruption guard (studio_guard) refuses project reads/writes while
    FTOptixStudio.exe is running. On a Windows dev box that is the NORMAL
    state, so without this the guard fires against real machine state and
    ~25 unrelated tests fail. Tests that exercise the guard itself patch
    `_scan` again in the test body, which wins over this default.
    """
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])
    studio_guard.reset_cache()
    yield
    studio_guard.reset_cache()


@pytest.fixture(autouse=True)
def _no_host_localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path_factory):
    """Isolate the suite from the host's %LOCALAPPDATA%.

    On a Windows box with armed bridges, %LOCALAPPDATA%\\ftx-mcp\\bridges holds
    the REAL bridge registry: tests would discover the operator's bridges and,
    on a faked connection-refused, delete their registry files. Studio's
    Configuration.xml lives there too. Tests that exercise either set
    LOCALAPPDATA again in the test body, which wins over this default.
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path_factory.mktemp("localappdata")))


@dataclass
class FakeProc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def make_fake_runner(
    handler: Callable[[list[str], dict], FakeProc] | None = None,
) -> core.Runner:
    """Build a Runner whose subprocess calls are intercepted by `handler`."""
    calls: list[tuple[list[str], dict]] = []

    def fn(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append((cmd, kwargs))
        if handler is None:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        result = handler(cmd, kwargs)
        return subprocess.CompletedProcess(cmd, result.returncode, result.stdout, result.stderr)

    runner = core.Runner(fn=fn)
    runner.calls = calls  # type: ignore[attr-defined]
    return runner


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    s = tmp_path / "state"
    s.mkdir()
    return s


@pytest.fixture
def runtime_dir(tmp_path: Path) -> Path:
    r = tmp_path / "runtime"
    r.mkdir()
    return r


@pytest.fixture
def cfg(projects_root: Path, state_dir: Path, runtime_dir: Path, tmp_path: Path) -> core.Config:
    studio_exe = tmp_path / "FTOptixStudio.exe"
    studio_exe.write_text("fake")
    return core.Config(
        projects_root=projects_root,
        studio_exe=studio_exe,
        state_dir=state_dir,
        runtime_dir=runtime_dir,
        verify_timeout_seconds=2,
        verify_poll_seconds=0.05,
        runtime_stop_grace_seconds=0.0,
        cdp_autoheal=False,  # heal path is exercised by dedicated tests, not implicitly
        enable_deploy=True,  # tests exercise the full surface; the default-off
                             # gate has its own dedicated tests
        # v1.0.7 multi-instance bridge support made single-bridge
        # tests (nearly the whole suite — they monkeypatch core._bridge_http
        # for ONE URL) potentially probe a whole port RANGE instead. Pinning
        # here keeps this shared fixture on the legacy single-URL bridge path
        # (bridge_url's one port, no range-scan) so every existing test's
        # behavior is unchanged. Multi-instance scanning/routing itself is
        # exercised by dedicated tests using the `multi_bridge_cfg` fixture
        # below (bridge_url_pinned=False) instead of this one.
        bridge_url_pinned=True,
    )


@pytest.fixture
def multi_bridge_cfg(cfg: core.Config) -> core.Config:
    """`cfg`, but with range-scanning ON (bridge_url_pinned=False) — for tests
    that specifically exercise multi-instance bridge discovery/routing across
    several simultaneously-armed ports, rather than the single pinned bridge
    the base `cfg` fixture models."""
    import dataclasses
    return dataclasses.replace(cfg, bridge_url_pinned=False,
                                bridge_port_base=8768, bridge_port_range=4)


def make_project(projects_root: Path, name: str = "TestProj") -> Path:
    """Create a source project under projects_root with a .optix manifest.

    Backdates dir + file mtimes so verifiers using mtime comparison have
    a clean baseline (Windows mtime resolution is 15.6 ms; without the
    backdating, a fast test can land both the project mtime and the
    deploy-start timestamp inside the same tick).
    """
    import os
    import time
    p = projects_root / name
    p.mkdir()
    (p / f"{name}.optix").write_text("fake-optix-marker")
    past = time.time() - 1.0
    os.utime(p, (past, past))
    for child in p.iterdir():
        os.utime(child, (past, past))
    return p


def make_export_handler(payload: bytes = b"export-bundle") -> Callable[[list[str], dict], FakeProc]:
    """Return a fake-Studio handler that writes a staged export bundle.

    The deploy() call passes `--location=<staging_dir>`; this handler
    writes a marker file under that staging dir so the atomic swap has a
    tree to move into the runtime location.
    """
    def handler(cmd: list[str], _kwargs: dict) -> FakeProc:
        loc: str | None = None
        for arg in cmd:
            if isinstance(arg, str) and arg.startswith("--location="):
                loc = arg.split("=", 1)[1]
                break
        if loc:
            staging = Path(loc)
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "runtime-marker").write_bytes(payload)
        return FakeProc(returncode=0, stdout="export ok", stderr="")
    return handler
