"""SPEC §Deploy contract — exhaustive schema and behavior tests.

These tests pin the result-schema shape across both terminal states
(`succeeded`, `failed`) and the export-based deploy mechanism's
behavior. v0.2.x does NOT have a `partial_unverified` state — that
returns in v0.3 once the UpdateSvc/OPC UA path re-enters the surface.

If you change the deploy result schema, you change the contract — these
tests should be the gate.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from service import core
from service.deploy_lock import DeployLock, LockHeld
from service.tests.conftest import FakeProc, make_export_handler, make_fake_runner, make_project

# ---- schema lock-in --------------------------------------------------

REQUIRED_TOP_LEVEL_FIELDS = {
    "studio_exit",
    "started_at",
    "completed_at",
    "git_sha",
    "git_state",
    "runtime_reachable",
    "files_written",
    "stdout_tail",
    "stderr_tail",
    "state",
    "verification",
}

REQUIRED_VERIFICATION_FIELDS = {"method", "confirmed_at", "timeout_seconds"}

VALID_STATES = {"succeeded", "failed"}
VALID_VERIFY_METHODS: set[str | None] = {None, "runtime_probe", "export_mtime"}
VALID_GIT_STATES = {"not_a_repo", "clean", "committed"}


class _NoopRuntime:
    def stop(self, _cfg, _runtime_project_dir): pass
    def start(self, _cfg, _runtime_project_dir): pass


def _assert_contract_shape(result: dict) -> None:
    """Every deploy result, regardless of state, must satisfy this shape."""
    assert REQUIRED_TOP_LEVEL_FIELDS.issubset(set(result.keys())), (
        f"missing fields: {REQUIRED_TOP_LEVEL_FIELDS - set(result.keys())}"
    )
    assert result["state"] in VALID_STATES, f"invalid state: {result['state']}"
    assert isinstance(result["studio_exit"], int)
    assert isinstance(result["started_at"], str) and result["started_at"].endswith("+00:00")
    assert isinstance(result["completed_at"], str) and result["completed_at"].endswith("+00:00")
    assert result["git_sha"] is None or isinstance(result["git_sha"], str)
    assert result["git_state"] in VALID_GIT_STATES, f"invalid git_state: {result['git_state']}"
    assert result["runtime_reachable"] in (None, True, False), (
        f"invalid runtime_reachable: {result['runtime_reachable']}"
    )
    assert isinstance(result["files_written"], list)
    assert isinstance(result["stdout_tail"], str)
    assert isinstance(result["stderr_tail"], str)
    assert len(result["stdout_tail"]) <= 2000
    assert len(result["stderr_tail"]) <= 2000

    v = result["verification"]
    assert REQUIRED_VERIFICATION_FIELDS.issubset(set(v.keys()))
    assert v["method"] in VALID_VERIFY_METHODS, f"invalid method: {v['method']}"
    assert v["confirmed_at"] is None or isinstance(v["confirmed_at"], str)
    assert isinstance(v["timeout_seconds"], int | float)


def _find_studio_call(runner) -> tuple[list[str], dict]:
    """runner.calls includes git invocations. Pick out the Studio export call."""
    for cmd, kwargs in runner.calls:
        if "export" in cmd and any(c.endswith("FTOptixStudio.exe") for c in cmd):
            return cmd, kwargs
    raise AssertionError(f"no Studio export call recorded; calls={runner.calls}")


def test_contract_shape_on_succeeded(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **kw: True)

    runner = make_fake_runner(make_export_handler())
    result = core.deploy(cfg, "Alpha", core.DeployRequest(), runner=runner, runtime=_NoopRuntime())
    _assert_contract_shape(result)
    assert result["state"] == "succeeded"
    assert result["studio_exit"] == 0
    assert result["verification"]["method"] == "runtime_probe"
    assert result["verification"]["confirmed_at"] is not None
    assert result["runtime_reachable"] is True


def test_contract_shape_on_failed_export(cfg: core.Config, projects_root: Path) -> None:
    make_project(projects_root, "Alpha")
    runner = make_fake_runner(
        lambda _cmd, _k: FakeProc(returncode=3221225477, stdout="", stderr="boom")
    )
    result = core.deploy(cfg, "Alpha", core.DeployRequest(), runner=runner, runtime=_NoopRuntime())
    _assert_contract_shape(result)
    assert result["state"] == "failed"
    assert result["studio_exit"] == 3221225477
    assert result["stderr_tail"] == "boom"
    assert result["verification"]["method"] is None


def test_runtime_probe_offline_marks_succeeded_runtime_unreachable(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I: Studio export exits 0, swap completes, but runtime probe times
    out -> state=succeeded with runtime_reachable=False. The deploy
    itself landed; the runtime may be crashed-on-load by the new
    payload, restarting, or otherwise unreachable for reasons orthogonal
    to deploy success. Operator should check runtime logs, not retry.
    """
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **kw: False)

    runner = make_fake_runner(make_export_handler())
    result = core.deploy(cfg, "Alpha", core.DeployRequest(), runner=runner, runtime=_NoopRuntime())
    _assert_contract_shape(result)
    assert result["state"] == "succeeded"
    assert result["runtime_reachable"] is False
    assert result["studio_exit"] == 0
    assert result["verification"]["method"] == "runtime_probe"
    assert result["verification"]["confirmed_at"] is None


def test_contract_shape_on_failed_swap(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Export succeeds but the atomic swap raises -> state=failed,
    method=None (we never reached the verify step)."""
    make_project(projects_root, "Alpha")

    def boom(_staging, _target):
        raise core.TreeSwapFailed("simulated lock")
    monkeypatch.setattr(core, "_atomic_swap", boom)

    runner = make_fake_runner(make_export_handler())
    result = core.deploy(cfg, "Alpha", core.DeployRequest(), runner=runner, runtime=_NoopRuntime())
    _assert_contract_shape(result)
    assert result["state"] == "failed"
    assert result["verification"]["method"] is None
    assert "simulated lock" in result["stderr_tail"]


# ---- subprocess cmd shape -------------------------------------------


def test_export_cmd_includes_required_flags(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **kw: True)

    runner = make_fake_runner(make_export_handler())
    core.deploy(
        cfg,
        "Alpha",
        core.DeployRequest(run_after_deploy=True),
        runner=runner,
        runtime=_NoopRuntime(),
    )
    cmd, _ = _find_studio_call(runner)
    assert "export" in cmd
    assert "--platform=Win32_x64" in cmd
    assert any(c.startswith("--location=") for c in cmd)


def test_export_cmd_does_not_pass_user_or_password(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The export-based path must not surface deploy-only flags. UpdateSvc
    arguments (--ip-address, --username, --run-after-deploy, encryption
    flags) belong to the v0.3 path and are absent here."""
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **kw: True)

    runner = make_fake_runner(make_export_handler())
    core.deploy(cfg, "Alpha", core.DeployRequest(), runner=runner, runtime=_NoopRuntime())
    cmd, kwargs = _find_studio_call(runner)
    assert not any(c.startswith("--username=") for c in cmd)
    assert not any(c.startswith("--ip-address=") for c in cmd)
    assert "--run-after-deploy" not in cmd
    assert "--disable-project-encryption" not in cmd
    # No env-mediated password leak either
    env = kwargs.get("env") or {}
    assert "OPTIX_STUDIO_DEPLOYMENT_PASSWORD" not in env


# ---- edits round-trip -----------------------------------------------


def test_edits_land_on_disk_and_appear_in_files_written(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **kw: True)
    runner = make_fake_runner(make_export_handler())
    req = core.DeployRequest(
        edits=[
            {"path": "Nodes/UI/screen.yaml", "content": "Type: Panel\n"},
            {"path": "ProjectFiles/Logic.cs", "content": "// stub\n"},
        ]
    )
    result = core.deploy(cfg, "Alpha", req, runner=runner, runtime=_NoopRuntime())
    assert sorted(result["files_written"]) == sorted(
        ["Nodes/UI/screen.yaml", "ProjectFiles/Logic.cs"]
    )
    assert (project_dir / "Nodes" / "UI" / "screen.yaml").read_text() == "Type: Panel\n"
    assert (project_dir / "ProjectFiles" / "Logic.cs").read_text() == "// stub\n"


def test_edits_reject_path_traversal(cfg: core.Config, projects_root: Path) -> None:
    make_project(projects_root, "Alpha")
    runner = make_fake_runner(make_export_handler())
    req = core.DeployRequest(
        edits=[{"path": "../../escape.txt", "content": "x"}],
    )
    with pytest.raises(core.PathTraversal):
        core.deploy(cfg, "Alpha", req, runner=runner, runtime=_NoopRuntime())


# ---- lock contention -----------------------------------------------


def test_concurrent_deploy_raises_lock_held(cfg: core.Config, projects_root: Path) -> None:
    """A second deploy attempt while the first is in flight must raise
    LockHeld. This exercises the SPEC §Concurrency single-writer guarantee."""
    make_project(projects_root, "Alpha")

    started = threading.Event()
    release = threading.Event()
    second_error: list[BaseException] = []

    def slow_studio(cmd: list[str], _kwargs: dict) -> FakeProc:
        # Only block on the Studio export — git calls must return promptly.
        if "export" in cmd and any(c.endswith("FTOptixStudio.exe") for c in cmd):
            started.set()
            release.wait(timeout=5)
        return FakeProc(returncode=0)

    runner = make_fake_runner(slow_studio)

    def first() -> None:
        core.deploy(cfg, "Alpha", core.DeployRequest(), runner=runner, runtime=_NoopRuntime())

    t1 = threading.Thread(target=first)
    t1.start()
    started.wait(timeout=5)

    # Now try to acquire the same lock — should fail with LockHeld
    second_lock = DeployLock(cfg.state_dir / "deploy.lock", caller="probe")
    try:
        with second_lock.acquire():
            pytest.fail("second deploy unexpectedly acquired the lock")
    except LockHeld as e:
        second_error.append(e)

    release.set()
    t1.join(timeout=5)

    assert len(second_error) == 1
    assert isinstance(second_error[0], LockHeld)


# ---- git_state surfacing (H) ----------------------------------------


def test_git_state_not_a_repo_surfaces(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H: a non-git project_dir surfaces git_state='not_a_repo' next to
    git_sha=None, so callers can distinguish 'no commit attempted' from
    'commit attempted and failed silently'.
    """
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **kw: True)

    export_handler = make_export_handler()

    def handler(cmd: list[str], kwargs: dict) -> FakeProc:
        # rev-parse --show-toplevel returns non-zero on non-git dirs.
        if "rev-parse" in cmd and "--show-toplevel" in cmd:
            return FakeProc(returncode=128, stdout="", stderr="fatal: not a git repository")
        return export_handler(cmd, kwargs)

    runner = make_fake_runner(handler)
    result = core.deploy(cfg, "Alpha", core.DeployRequest(), runner=runner, runtime=_NoopRuntime())
    _assert_contract_shape(result)
    assert result["state"] == "succeeded"
    assert result["git_sha"] is None
    assert result["git_state"] == "not_a_repo"


# ---- exception-path outcome recording (M) ---------------------------


def test_record_deploy_outcome_on_unhandled_exception(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exceptions raised mid-deploy must still land in the outcome buffer
    via the finally block. Without this, an OSError mid-swap silently
    drops the deploy from the HMI tail.
    """
    make_project(projects_root, "Alpha")

    def boom(_staging, _target):
        raise OSError("simulated mid-swap crash")
    monkeypatch.setattr(core, "_atomic_swap", boom)

    runner = make_fake_runner(make_export_handler())
    with pytest.raises(OSError):
        core.deploy(cfg, "Alpha", core.DeployRequest(), runner=runner, runtime=_NoopRuntime())

    entry = core.last_deploy_tail(cfg, project="Alpha")
    assert entry is not None
    assert entry["state"] == "failed"
    assert "simulated mid-swap crash" in entry["stderr_tail"]
