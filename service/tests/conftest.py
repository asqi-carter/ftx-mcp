"""Shared fixtures for service tests."""
from __future__ import annotations

import os
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
def _emulator_project_resolution_lenient(request: pytest.FixtureRequest,
                                         monkeypatch: pytest.MonkeyPatch):
    """run / restart / status resolve the project FIRST and refuse a nonexistent
    one with ``project_not_found`` (#92). Most emulator tests drive those
    entry points with a bare project name and no directory on disk, so by
    default an unresolvable name still resolves (to ``projects_root/<name>``).
    Tests marked ``real_project_resolution`` get the real refusal."""
    if request.node.get_closest_marker("real_project_resolution"):
        return
    real = core._resolve_emulator_project

    def _lenient(cfg, project):
        found, missing = real(cfg, project)
        if missing is not None:
            return cfg.projects_root / project, None
        return found, None

    monkeypatch.setattr(core, "_resolve_emulator_project", _lenient)


@pytest.fixture
def f5_target_studio(monkeypatch: pytest.MonkeyPatch) -> int:
    """Attribute a Studio (pid 4242) to every project so run_emulator reaches
    its F5 keystroke; the last guard refuses ``no_target_studio`` without one."""
    monkeypatch.setattr(core, "_studio_pid_for_project",
                        lambda project_dir, force=False: 4242)
    return 4242


class HostListenerTableAccess(AssertionError):
    """A unit test read the HOST's real listener table (#91)."""


@pytest.fixture(autouse=True)
def _no_host_listener_table(request: pytest.FixtureRequest,
                            monkeypatch: pytest.MonkeyPatch):
    """Guard: ``psutil.net_connections`` raises outside live runs.

    A live run is a test marked ``live`` / ``slow_live`` or any run configured
    with an ``FTX_LIVE_*`` env var (``_is_live_run``): live tests talk to a real
    Studio/emulator, so the real listener table is legitimate there.

    The result depends on whatever happens to listen on the host (a dev box
    often serves 8081), so a unit test that reaches it passes or fails with
    machine state. Tests must patch ``core._port_owner`` /
    ``core._port_listener_pids`` / ``core._pids_listening_on`` instead; a test
    that patches ``psutil.net_connections`` itself wins over this default.
    """
    if _is_live_run(request):
        return

    def _guard(*args: Any, **kwargs: Any):
        raise HostListenerTableAccess(
            "psutil.net_connections called from a non-live test: it reads the "
            "host's listener table. Patch core._port_owner / "
            "core._port_listener_pids, or mark the test `live`.")

    monkeypatch.setattr(core.psutil, "net_connections", _guard)


@pytest.fixture(autouse=True)
def _no_cdp_activate_http(monkeypatch: pytest.MonkeyPatch):
    """``_cdp.activate`` (GET /json/activate/<id>) must never touch a real
    Chrome from unit tests; tests that assert on it patch this seam again."""
    from service import _cdp
    monkeypatch.setattr(_cdp, "_http_get_status", lambda url, timeout: 200)


@pytest.fixture(autouse=True)
def _no_edit_type_probe(request, monkeypatch: pytest.MonkeyPatch):
    """``bridge_edit`` checks widget/object type names against the bridge's type
    catalog at validate time. Tests that fake only the validate/apply seams must
    not reach for a real bridge (slow, and it consumes fake clocks), so the
    check is inert by default; ``test_edit_op_validation`` exercises it."""
    if request.module.__name__.endswith("test_edit_op_validation"):
        return
    monkeypatch.setattr(core, "validate_edit_op_types", lambda cfg, project, ops: [])


def _is_live_run(request: pytest.FixtureRequest) -> bool:
    """True when the test is marked ``live`` / ``slow_live`` or the run is configured
    for the live suite (any non-empty ``FTX_LIVE_*`` env var). Live tests talk to a
    real Studio whose NetLogic template write takes real time, so they need the
    production wait constants."""
    if request.node.get_closest_marker("live") or request.node.get_closest_marker("slow_live"):
        return True
    return any(k.startswith("FTX_LIVE_") and v for k, v in os.environ.items())


@pytest.fixture(autouse=True)
def _fast_netlogic_template_wait(request: pytest.FixtureRequest,
                                 monkeypatch: pytest.MonkeyPatch):
    """Keep create_netlogic's bounded template-write wait short in unit tests
    (most fake projects have no template .cs, so the wait would run to its cap).
    Live tests (marked ``live`` / ``slow_live`` or run under ``FTX_LIVE_*``) keep the
    real constants: a real Studio needs the full wait."""
    if _is_live_run(request):
        return
    monkeypatch.setattr(core, "_NETLOGIC_TEMPLATE_MAX_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(core, "_NETLOGIC_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(core, "_NETLOGIC_SETTLE_POLL", 0.01)


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
