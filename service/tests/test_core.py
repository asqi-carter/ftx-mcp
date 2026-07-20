"""Tests for service.core — pure functions, no Studio required."""
from __future__ import annotations

from pathlib import Path

import pytest

from service import core
from service.tests.conftest import FakeProc, make_export_handler, make_fake_runner, make_project


def test_health_reports_paths_and_runtime_state(cfg: core.Config) -> None:
    h = core.health(cfg)
    assert h["ok"] is True
    assert h["projects_root_exists"] is True
    assert h["studio_exe_exists"] is True
    assert h["runtime_dir_exists"] is True
    assert h["runtime_test_port"] == 8081
    assert h["bind"]["http_port"] == 8765
    assert h["bind"]["mcp_port"] == 8766
    # interactive_session: None on non-Windows, bool on Windows
    assert "interactive_session" in h
    assert h["interactive_session"] in (True, False, None)


def test_list_projects_finds_optix_dirs(cfg: core.Config, projects_root: Path) -> None:
    make_project(projects_root, "Alpha")
    make_project(projects_root, "Beta")
    (projects_root / "NotAProject").mkdir()  # no .optix
    listed = core.list_projects(cfg)
    names = [p["name"] for p in listed]
    assert names == ["Alpha", "Beta"]


def test_resolve_project_rejects_traversal(cfg: core.Config, projects_root: Path) -> None:
    make_project(projects_root, "Alpha")
    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "../etc")
    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "Missing")


def test_resolve_subpath_rejects_traversal(cfg: core.Config, projects_root: Path) -> None:
    make_project(projects_root, "Alpha")
    with pytest.raises(core.PathTraversal):
        core.resolve_subpath(cfg, "Alpha", "../../etc/passwd")


def test_read_file_returns_sha256(cfg: core.Config, projects_root: Path) -> None:
    p = make_project(projects_root, "Alpha")
    # write_bytes — write_text("\n") triggers newline translation to CRLF on Windows
    (p / "screen.yaml").write_bytes(b"Hello, World!\n")
    out = core.read_file(cfg, "Alpha", "screen.yaml")
    import hashlib
    assert out["content"] == "Hello, World!\n"
    assert out["size"] == 14
    assert out["sha256"] == hashlib.sha256(b"Hello, World!\n").hexdigest()


def test_read_file_rejects_binary(cfg: core.Config, projects_root: Path) -> None:
    p = make_project(projects_root, "Alpha")
    (p / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(core.BinaryFile):
        core.read_file(cfg, "Alpha", "blob.bin")


def test_studio_version_runs_binary(cfg: core.Config) -> None:
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "1.7.1.46", ""))
    out = core.studio_version(cfg, runner=runner)
    assert out["ok"] is True
    assert out["stdout"] == "1.7.1.46"


class _NoopRuntime:
    """Test double: runtime stop/start are no-ops, so deploy() doesn't try
    to run powershell on a Linux test runner."""

    def stop(self, _cfg, _runtime_project_dir): pass
    def start(self, _cfg, _runtime_project_dir): pass


def test_deploy_exports_swaps_and_verifies_via_runtime_probe(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_after_deploy=True: export -> swap -> bounce -> probe runtime port."""
    make_project(projects_root, "Alpha")
    runner = make_fake_runner(make_export_handler())

    # Make the runtime probe succeed once swap landed.
    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **kw: True)

    req = core.DeployRequest(
        edits=[{"path": "screen.yaml", "content": "<screen/>"}],
        commit_message="test deploy",
        run_after_deploy=True,
    )
    result = core.deploy(cfg, "Alpha", req, runner=runner, runtime=_NoopRuntime())

    assert result["state"] == "succeeded", result
    assert result["studio_exit"] == 0
    assert result["verification"]["method"] == "runtime_probe"
    assert result["verification"]["confirmed_at"] is not None
    assert "screen.yaml" in result["files_written"]
    # Runtime tree was swapped into place
    assert (cfg.runtime_dir / "Alpha" / "runtime-marker").is_file()


def test_deploy_skips_bounce_and_uses_export_mtime_when_run_after_deploy_false(
    cfg: core.Config, projects_root: Path
) -> None:
    make_project(projects_root, "Alpha")
    runner = make_fake_runner(make_export_handler())

    req = core.DeployRequest(edits=[], run_after_deploy=False)
    result = core.deploy(cfg, "Alpha", req, runner=runner, runtime=_NoopRuntime())

    assert result["state"] == "succeeded", result
    assert result["verification"]["method"] == "export_mtime"
    assert result["verification"]["confirmed_at"] is not None


def test_deploy_runtime_probe_offline_succeeds_with_unreachable_marker(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I: run_after_deploy=True but runtime never comes back ->
    state=succeeded with runtime_reachable=False. The swap landed; the
    runtime offline state is reported as a sub-marker, not a deploy
    failure (the operator's next step is to check runtime logs, not
    re-deploy)."""
    make_project(projects_root, "Alpha")
    runner = make_fake_runner(make_export_handler())

    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **kw: False)

    req = core.DeployRequest(edits=[], run_after_deploy=True)
    result = core.deploy(cfg, "Alpha", req, runner=runner, runtime=_NoopRuntime())

    assert result["state"] == "succeeded", result
    assert result["runtime_reachable"] is False
    assert result["verification"]["method"] == "runtime_probe"
    assert result["verification"]["confirmed_at"] is None


def test_deploy_fails_when_studio_export_returns_nonzero(
    cfg: core.Config, projects_root: Path
) -> None:
    make_project(projects_root, "Alpha")

    def studio_handler(cmd: list[str], _kw: dict) -> FakeProc:
        return FakeProc(2, "", "export failed")

    runner = make_fake_runner(studio_handler)
    req = core.DeployRequest(edits=[])
    result = core.deploy(cfg, "Alpha", req, runner=runner, runtime=_NoopRuntime())
    assert result["state"] == "failed"
    assert result["studio_exit"] == 2
    assert "export failed" in result["stderr_tail"]


def test_deploy_raises_when_runtime_dir_missing(
    cfg: core.Config, projects_root: Path
) -> None:
    make_project(projects_root, "Alpha")
    cfg_no_runtime = core.Config(**{**cfg.__dict__, "runtime_dir": None})
    with pytest.raises(core.RuntimeDirNotConfigured):
        core.deploy(cfg_no_runtime, "Alpha", core.DeployRequest())


def test_deploy_raises_when_project_has_no_optix_file(
    cfg: core.Config, projects_root: Path
) -> None:
    p = projects_root / "Empty"
    p.mkdir()
    with pytest.raises(core.ProjectNotFound):
        core.deploy(cfg, "Empty", core.DeployRequest(), runtime=_NoopRuntime())


# ---- subprocess tree-kill on timeout (L) -----------------------------


def _make_fake_popen(
    *, raise_timeout: bool = False, returncode: int = 0, pid: int = 12345,
):
    """Build a Popen replacement that raises TimeoutExpired or completes
    immediately depending on raise_timeout. Used to drive
    _run_subprocess_with_tree_kill without spawning real children.
    """
    import subprocess as _subprocess

    class FakePopen:
        def __init__(self, cmd, **_kwargs):
            self._cmd = cmd
            self.pid = pid
            self.returncode = returncode
            self._communicate_calls = 0

        def communicate(self, timeout=None):
            self._communicate_calls += 1
            if raise_timeout and self._communicate_calls == 1:
                raise _subprocess.TimeoutExpired(self._cmd, timeout)
            return ("out", "err")

        def __enter__(self): return self
        def __exit__(self, *_a): pass

    return FakePopen


def test_run_subprocess_with_tree_kill_no_timeout_uses_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calls without timeout fall through to subprocess.run unchanged."""
    import subprocess

    captured: dict = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    result = core._run_subprocess_with_tree_kill(["foo", "bar"], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout == "ok"
    assert captured["cmd"] == ["foo", "bar"]
    assert "timeout" not in captured["kwargs"]


def test_run_subprocess_with_tree_kill_invokes_tree_kill_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L: TimeoutExpired triggers _tree_kill on the child's pid, not just
    the direct child's Popen.kill (subprocess.run's default).
    """
    killed: list[int] = []
    monkeypatch.setattr(core, "_tree_kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(core.subprocess, "Popen", _make_fake_popen(raise_timeout=True, pid=4242))

    import subprocess as _sp
    with pytest.raises(_sp.TimeoutExpired):
        core._run_subprocess_with_tree_kill(
            ["sleep", "30"], timeout=0.1, capture_output=True, text=True,
        )
    assert killed == [4242], f"expected one tree-kill of pid 4242, got {killed}"


def test_run_subprocess_with_tree_kill_no_kill_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful completion must not invoke _tree_kill."""
    killed: list[int] = []
    monkeypatch.setattr(core, "_tree_kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(core.subprocess, "Popen", _make_fake_popen(raise_timeout=False, returncode=0))

    result = core._run_subprocess_with_tree_kill(
        ["echo", "hi"], timeout=5, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert killed == []


def test_runner_default_fn_is_tree_kill_aware() -> None:
    """Regression: a fresh Runner() must default to the tree-kill-aware
    fn, not subprocess.run. If a refactor swaps it back, deploy-timeout
    behavior silently regresses to direct-child-only kill."""
    assert core.Runner().fn is core._run_subprocess_with_tree_kill


def test_atomic_swap_replaces_existing_runtime_tree(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing runtime tree is replaced atomically; the .bak
    intermediate is dropped after the swap completes."""
    make_project(projects_root, "Alpha")
    # Pre-existing runtime tree with old content
    old_runtime = cfg.runtime_dir / "Alpha"
    old_runtime.mkdir()
    (old_runtime / "old-file").write_text("stale")

    runner = make_fake_runner(make_export_handler(payload=b"new-bundle"))
    monkeypatch.setattr(core, "_tcp_probe", lambda *a, **kw: True)

    req = core.DeployRequest(edits=[], run_after_deploy=True)
    result = core.deploy(cfg, "Alpha", req, runner=runner, runtime=_NoopRuntime())

    assert result["state"] == "succeeded"
    assert (old_runtime / "runtime-marker").read_bytes() == b"new-bundle"
    assert not (old_runtime / "old-file").exists()
    # No leftover .bak after a successful swap
    assert not (cfg.runtime_dir / "Alpha.bak").exists()


class TestConfigFromEnv:
    """Coverage for Config.from_env() env-var resolution.

    Pre-fix gap: OPTIX_RUNTIME_DIR was resolved from LOCALAPPDATA when unset,
    ignoring OPTIX_STATE_DIR overrides. install-smoke runs that redirected
    only OPTIX_STATE_DIR ended up with runtime_dir pointing at the prod
    %LOCALAPPDATA%\\ftx-mcp\\runtime tree.
    """

    def test_state_dir_override_propagates_to_runtime_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom_state = tmp_path / "redirected-state"
        monkeypatch.setenv("OPTIX_STATE_DIR", str(custom_state))
        monkeypatch.delenv("OPTIX_RUNTIME_DIR", raising=False)

        cfg = core.Config.from_env()

        assert cfg.state_dir == custom_state
        assert cfg.runtime_dir == custom_state / "runtime"
        assert cfg.tokens_path == custom_state / "secrets" / "tokens.json.dpapi"

    def test_explicit_runtime_dir_still_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom_state = tmp_path / "state"
        custom_runtime = tmp_path / "elsewhere" / "runtime"
        monkeypatch.setenv("OPTIX_STATE_DIR", str(custom_state))
        monkeypatch.setenv("OPTIX_RUNTIME_DIR", str(custom_runtime))

        cfg = core.Config.from_env()

        assert cfg.state_dir == custom_state
        assert cfg.runtime_dir == custom_runtime

    def test_default_falls_back_to_localappdata_or_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPTIX_STATE_DIR", raising=False)
        monkeypatch.delenv("OPTIX_RUNTIME_DIR", raising=False)
        monkeypatch.delenv("OPTIX_TOKENS_PATH", raising=False)

        cfg = core.Config.from_env()

        # state_dir resolves to either LOCALAPPDATA\ftx-mcp (Windows)
        # or ~/.local/share/ftx-mcp (POSIX). In both cases runtime_dir
        # is state_dir/runtime, and tokens_path is state_dir/secrets/...
        assert cfg.state_dir.name == "ftx-mcp"
        assert cfg.runtime_dir == cfg.state_dir / "runtime"
        assert cfg.tokens_path == cfg.state_dir / "secrets" / "tokens.json.dpapi"

    def test_deploy_source_transfer_and_cdp_settle_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for v in ("OPTIX_DEPLOY_KEEP_SOURCE", "OPTIX_CDP_SETTLE_SECONDS"):
            monkeypatch.delenv(v, raising=False)
        cfg = core.Config.from_env()
        assert cfg.deploy_disable_source_transfer is True   # skip source by default
        assert cfg.cdp_settle_seconds == 1.0

    def test_keep_source_and_custom_settle_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPTIX_DEPLOY_KEEP_SOURCE", "1")
        monkeypatch.setenv("OPTIX_CDP_SETTLE_SECONDS", "0.4")
        cfg = core.Config.from_env()
        assert cfg.deploy_disable_source_transfer is False  # keep source -> don't disable
        assert cfg.cdp_settle_seconds == 0.4

