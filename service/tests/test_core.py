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
    # content is <untrusted>-delimited (U11); sha256/size describe the raw file
    assert out["content"] == core._untrusted("Hello, World!\n", "read_file")
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


def test_run_subprocess_with_tree_kill_suppresses_console_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lock-in: every child spawned through here
    (taskkill/tasklist/netstat/powershell/...) is a console-subsystem tool.
    Under any parent with no console of its own,
    Windows allocates a brand-new console for each one, which flashes on
    screen for the call's duration — the "PowerShell keeps popping up and
    closing" bug. creationflags=CREATE_NO_WINDOW must be set by default on
    Windows so no future call site (or refactor of this function) can drop
    it silently. A caller that explicitly passes its own creationflags is
    respected (setdefault, not overwrite).
    """
    monkeypatch.setattr(core.os, "name", "nt")
    monkeypatch.setattr(core.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return core.subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    core._run_subprocess_with_tree_kill(["powershell", "-NoProfile", "-Command", "1"])
    assert captured["kwargs"]["creationflags"] == 0x08000000


def test_run_subprocess_with_tree_kill_respects_explicit_creationflags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied creationflags must win over the CREATE_NO_WINDOW
    default (setdefault, not an unconditional overwrite) — a caller that
    genuinely needs a visible console (or a different flag combination)
    can still opt out."""
    monkeypatch.setattr(core.os, "name", "nt")
    monkeypatch.setattr(core.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return core.subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    core._run_subprocess_with_tree_kill(["cmd"], creationflags=0x00000010)
    assert captured["kwargs"]["creationflags"] == 0x00000010


def test_tree_kill_taskkill_suppresses_console_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion lock-in for _tree_kill's own taskkill call, which bypasses
    Runner/_run_subprocess_with_tree_kill entirely (it's the timeout-kill
    path, not a normal shell-out) and so needs the same flag applied
    directly rather than inherited from the shared wrapper."""
    monkeypatch.setattr(core.os, "name", "nt")
    monkeypatch.setattr(core.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return core.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    core._tree_kill(4242)
    assert captured["cmd"][0] == "taskkill"
    assert captured["kwargs"]["creationflags"] == 0x08000000


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

    def test_studio_exe_env_override_wins_over_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override = tmp_path / "Custom" / "FTOptixStudio.exe"
        monkeypatch.setenv("FTOPTIX_STUDIO_EXE", str(override))

        cfg = core.Config.from_env()

        assert cfg.studio_exe == override

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

    def test_ocr_conf_threshold_default_and_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPTIX_OCR_CONF_THRESHOLD", raising=False)
        assert core.Config.from_env().ocr_conf_threshold == 0.60
        monkeypatch.setenv("OPTIX_OCR_CONF_THRESHOLD", "0.8")
        assert core.Config.from_env().ocr_conf_threshold == 0.8

    def test_cdp_viewport_default_and_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPTIX_CDP_VIEWPORT ('WIDTHxHEIGHT') / OPTIX_CDP_SCALE tune the
        emulated device viewport applied before every CDP screenshot/click
        (see core._cdp_session, core.cdp_screenshot_runtime). Default is
        1280x720 @ scale 1 — larger than chrome-cdp's 800x600 launch window,
        which fixes the clipped-HMI bug (see _CDP_VIEWPORT_DEFAULT)."""
        monkeypatch.delenv("OPTIX_CDP_VIEWPORT", raising=False)
        monkeypatch.delenv("OPTIX_CDP_SCALE", raising=False)
        cfg = core.Config.from_env()
        assert cfg.cdp_viewport_width == 1280
        assert cfg.cdp_viewport_height == 720
        assert cfg.cdp_viewport_scale == 1.0

        monkeypatch.setenv("OPTIX_CDP_VIEWPORT", "1920x1080")
        monkeypatch.setenv("OPTIX_CDP_SCALE", "2.0")
        cfg = core.Config.from_env()
        assert cfg.cdp_viewport_width == 1920
        assert cfg.cdp_viewport_height == 1080
        assert cfg.cdp_viewport_scale == 2.0

    @pytest.mark.parametrize("bad_viewport", [
        "1280",           # missing the 'x' separator
        "axb",            # non-numeric
        "0x720",          # zero width
        "1280x-720",      # negative height
        "1280x720x1",     # too many parts
        "",                # empty
    ])
    def test_cdp_viewport_malformed_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, bad_viewport: str
    ) -> None:
        monkeypatch.setenv("OPTIX_CDP_VIEWPORT", bad_viewport)
        cfg = core.Config.from_env()
        assert cfg.cdp_viewport_width == 1280
        assert cfg.cdp_viewport_height == 720

    @pytest.mark.parametrize("bad_scale", ["abc", "0", "-1", ""])
    def test_cdp_scale_malformed_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, bad_scale: str
    ) -> None:
        monkeypatch.setenv("OPTIX_CDP_SCALE", bad_scale)
        cfg = core.Config.from_env()
        assert cfg.cdp_viewport_scale == 1.0

    def test_studio_guard_mode_default_and_validation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPTIX_STUDIO_GUARD_MODE is a validated string enum: unset or any
        unrecognized value (typo, junk, empty) collapses to the safe
        'blanket' default; only 'attributed' opts in, case/space-insensitive."""
        monkeypatch.delenv("OPTIX_STUDIO_GUARD_MODE", raising=False)
        assert core.Config.from_env().studio_guard_mode == "blanket"
        monkeypatch.setenv("OPTIX_STUDIO_GUARD_MODE", "attributed")
        assert core.Config.from_env().studio_guard_mode == "attributed"
        monkeypatch.setenv("OPTIX_STUDIO_GUARD_MODE", "  ATTRIBUTED  ")
        assert core.Config.from_env().studio_guard_mode == "attributed"
        monkeypatch.setenv("OPTIX_STUDIO_GUARD_MODE", "blanket")
        assert core.Config.from_env().studio_guard_mode == "blanket"
        for junk in ("atributed", "", "on", "yes", "off", "garbage"):
            monkeypatch.setenv("OPTIX_STUDIO_GUARD_MODE", junk)
            assert core.Config.from_env().studio_guard_mode == "blanket"


class TestDefaultStudioExe:
    """v1.0.1 (field report finding 2): the studio_exe default is a live
    highest-version probe, not a pinned path. The pinned 1.7.1.46 default
    reported studio_exe_exists=false on any box whose only Studio was newer,
    because setup.ps1's discovered path lived in the install shell's process
    env — invisible to the scheduled task that launches the service.
    """

    def _make_install(self, root: Path, version: str) -> Path:
        exe = root / f"Studio {version}" / "FTOptixStudio.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        return exe

    def test_picks_highest_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(core, "_STUDIO_INSTALL_ROOT", tmp_path)
        self._make_install(tmp_path, "1.7.1.46")
        newest = self._make_install(tmp_path, "1.7.3.39")

        assert core._default_studio_exe() == newest

    def test_version_sort_is_numeric_not_lexicographic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(core, "_STUDIO_INSTALL_ROOT", tmp_path)
        self._make_install(tmp_path, "1.7.9.1")
        newest = self._make_install(tmp_path, "1.7.10.2")

        assert core._default_studio_exe() == newest

    def test_missing_root_falls_back_to_pinned_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(core, "_STUDIO_INSTALL_ROOT", tmp_path / "absent")

        exe = core._default_studio_exe()

        # A concrete (red-in-/health) path, still under the install root.
        assert exe.name == "FTOptixStudio.exe"
        assert exe.parent.name == "Studio 1.7.1.46"

    def test_unparseable_version_dirs_do_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(core, "_STUDIO_INSTALL_ROOT", tmp_path)
        self._make_install(tmp_path, "beta")
        newest = self._make_install(tmp_path, "1.7.3.39")

        assert core._default_studio_exe() == newest



def test_hide_console_is_opt_in_via_flag_or_env(monkeypatch) -> None:
    """The console is hidden ONLY on explicit request. Locks in the
    single-launcher decision: the scheduled task always runs console-subsystem
    python.exe, so the service itself must hide the window -- and must not do
    it uninvited, because the default start is meant to keep the console."""
    from service.main import _should_hide_console

    monkeypatch.delenv("OPTIX_HIDE_CONSOLE", raising=False)
    assert _should_hide_console([]) is False              # default: visible
    assert _should_hide_console(["--hide-console"]) is True

    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("OPTIX_HIDE_CONSOLE", truthy)
        assert _should_hide_console([]) is True, truthy
    for falsy in ("0", "false", "", "no"):
        monkeypatch.setenv("OPTIX_HIDE_CONSOLE", falsy)
        assert _should_hide_console([]) is False, falsy
        # the flag still wins over an explicit off
        assert _should_hide_console(["--hide-console"]) is True, truthy


def test_module_entrypoint_forwards_argv() -> None:
    """`python -m service` runs service/__main__.py, NOT main.py -- so main.py's
    own `if __name__ == "__main__": main(sys.argv[1:])` never fires. If
    __main__.py calls main() bare, every command-line flag is silently dropped:
    the scheduled task's `-m service --hide-console` parsed fine and did
    nothing, because main() could not see an argv it was never given.

    Asserted structurally (the call has >=1 positional arg) rather than by
    string match, so reformatting cannot defeat it."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "__main__.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "main"
    ]
    assert calls, "__main__.py no longer calls main() at all"
    for c in calls:
        assert c.args or c.keywords, (
            "__main__.py calls main() with no arguments -- command-line flags "
            "will be silently ignored under `python -m service`"
        )


# ---------------------------------------------------------------------------
# read_netlogic tests
# ---------------------------------------------------------------------------

def _make_netlogic(projects_root: Path, project: str, cls: str, content: bytes) -> Path:
    """Create a NetSolution .cs file in a minimal Optix project tree."""
    p = make_project(projects_root, project)
    net_dir = p / "ProjectFiles" / "NetSolution"
    net_dir.mkdir(parents=True, exist_ok=True)
    cs_file = net_dir / f"{cls}.cs"
    cs_file.write_bytes(content)
    return p


class TestReadNetlogicValidation:
    """Class-name validation fires BEFORE any filesystem access."""

    def test_empty_cls_raises_invalid(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.InvalidNetLogicClass):
            core.read_netlogic(cfg, "Alpha", "")

    def test_cs_extension_raises_invalid(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.InvalidNetLogicClass):
            core.read_netlogic(cfg, "Alpha", "MyLogic.cs")

    def test_wildcard_star_raises_invalid(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.InvalidNetLogicClass):
            core.read_netlogic(cfg, "Alpha", "My*Logic")

    def test_wildcard_question_raises_invalid(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.InvalidNetLogicClass):
            core.read_netlogic(cfg, "Alpha", "My?Logic")

    def test_leading_digit_raises_invalid(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.InvalidNetLogicClass):
            core.read_netlogic(cfg, "Alpha", "1BadName")

    def test_non_identifier_chars_raises_invalid(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.InvalidNetLogicClass):
            core.read_netlogic(cfg, "Alpha", "My-Logic")

    def test_space_in_name_raises_invalid(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.InvalidNetLogicClass):
            core.read_netlogic(cfg, "Alpha", "My Logic")

    def test_forward_slash_raises_traversal(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.PathTraversal):
            core.read_netlogic(cfg, "Alpha", "sub/MyLogic")

    def test_backslash_raises_traversal(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.PathTraversal):
            core.read_netlogic(cfg, "Alpha", "sub\\MyLogic")

    def test_dotdot_raises_traversal(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.PathTraversal):
            core.read_netlogic(cfg, "Alpha", "..MyLogic")

    def test_drive_letter_raises_traversal(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.PathTraversal):
            core.read_netlogic(cfg, "Alpha", "C:MyLogic")

    def test_valid_identifier_with_underscore(self, cfg: core.Config, projects_root: Path) -> None:
        """Underscores are valid in C# identifiers."""
        _make_netlogic(projects_root, "Alpha", "_MyLogic", b"// cs\n")
        out = core.read_netlogic(cfg, "Alpha", "_MyLogic")
        assert out["path"].endswith("_MyLogic.cs")

    def test_valid_identifier_with_digits_inside(self, cfg: core.Config, projects_root: Path) -> None:
        """Digits inside the identifier are fine."""
        _make_netlogic(projects_root, "Alpha", "Logic2", b"// cs\n")
        out = core.read_netlogic(cfg, "Alpha", "Logic2")
        assert out["path"].endswith("Logic2.cs")


class TestReadNetlogicHappyPath:
    """Successful reads: result shape, sha256, total_lines, etc."""

    def test_returns_expected_keys(self, cfg: core.Config, projects_root: Path) -> None:
        content = b"using System;\nnamespace X { }\n"
        _make_netlogic(projects_root, "Alpha", "MyLogic", content)
        out = core.read_netlogic(cfg, "Alpha", "MyLogic")
        assert out["path"] == "ProjectFiles/NetSolution/MyLogic.cs"
        assert out["size"] == len(content)
        assert out["source"] == "disk"
        assert "studio_open" in out
        assert isinstance(out["studio_open"], bool)
        assert out["total_lines"] == 2
        assert "sha256" in out
        assert "content" in out

    def test_content_is_untrusted_wrapped(self, cfg: core.Config, projects_root: Path) -> None:
        _make_netlogic(projects_root, "Alpha", "MyLogic", b"hello\n")
        out = core.read_netlogic(cfg, "Alpha", "MyLogic")
        assert out["content"] == core._untrusted("hello\n", "read_netlogic")

    def test_sha256_matches_whole_file(self, cfg: core.Config, projects_root: Path) -> None:
        import hashlib
        content = b"int x = 1;\n"
        _make_netlogic(projects_root, "Alpha", "MyLogic", content)
        out = core.read_netlogic(cfg, "Alpha", "MyLogic")
        assert out["sha256"] == hashlib.sha256(content).hexdigest()

    def test_no_truncated_key_for_small_file(self, cfg: core.Config, projects_root: Path) -> None:
        _make_netlogic(projects_root, "Alpha", "MyLogic", b"small\n")
        out = core.read_netlogic(cfg, "Alpha", "MyLogic")
        assert "truncated" not in out


class TestReadNetlogicFileNotFound:
    """FileNotFound includes the project-relative path and sibling .cs files."""

    def test_missing_class_raises_file_not_found(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.FileNotFound):
            core.read_netlogic(cfg, "Alpha", "DoesNotExist")

    def test_missing_class_message_includes_rel_path(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.FileNotFound) as exc_info:
            core.read_netlogic(cfg, "Alpha", "DoesNotExist")
        assert "ProjectFiles/NetSolution/DoesNotExist.cs" in str(exc_info.value)

    def test_missing_class_lists_siblings(self, cfg: core.Config, projects_root: Path) -> None:
        p = make_project(projects_root, "Alpha")
        net_dir = p / "ProjectFiles" / "NetSolution"
        net_dir.mkdir(parents=True, exist_ok=True)
        (net_dir / "SiblingLogic.cs").write_bytes(b"// sibling\n")
        with pytest.raises(core.FileNotFound) as exc_info:
            core.read_netlogic(cfg, "Alpha", "DoesNotExist")
        assert "SiblingLogic.cs" in str(exc_info.value)


class TestReadNetlogicBinaryFile:
    """BinaryFile raised for non-UTF-8 content."""

    def test_binary_content_raises(self, cfg: core.Config, projects_root: Path) -> None:
        _make_netlogic(projects_root, "Alpha", "MyLogic", b"\xff\xfe\x00\x01")
        with pytest.raises(core.BinaryFile):
            core.read_netlogic(cfg, "Alpha", "MyLogic")


class TestReadNetlogicLineRange:
    """start_line / end_line slicing and BadLineRange errors."""

    def _setup(self, projects_root: Path) -> None:
        content = b"L1\nL2\nL3\nL4\nL5\n"
        _make_netlogic(projects_root, "Alpha", "MyLogic", content)

    def test_line_range_returns_slice(self, cfg: core.Config, projects_root: Path) -> None:
        self._setup(projects_root)
        out = core.read_netlogic(cfg, "Alpha", "MyLogic", start_line=2, end_line=3)
        assert out["content"] == core._untrusted("L2\nL3\n", "read_netlogic")
        assert out["start_line"] == 2
        assert out["end_line"] == 3

    def test_end_line_clamped_to_eof(self, cfg: core.Config, projects_root: Path) -> None:
        self._setup(projects_root)
        out = core.read_netlogic(cfg, "Alpha", "MyLogic", start_line=4, end_line=999)
        assert out["end_line"] == 5  # clamped to total_lines
        assert out["content"] == core._untrusted("L4\nL5\n", "read_netlogic")

    def test_start_line_zero_raises_bad_range(self, cfg: core.Config, projects_root: Path) -> None:
        self._setup(projects_root)
        with pytest.raises(core.BadLineRange):
            core.read_netlogic(cfg, "Alpha", "MyLogic", start_line=0)

    def test_end_before_start_raises_bad_range(self, cfg: core.Config, projects_root: Path) -> None:
        self._setup(projects_root)
        with pytest.raises(core.BadLineRange):
            core.read_netlogic(cfg, "Alpha", "MyLogic", start_line=3, end_line=2)

    def test_start_past_eof_raises_bad_range(self, cfg: core.Config, projects_root: Path) -> None:
        self._setup(projects_root)
        with pytest.raises(core.BadLineRange):
            core.read_netlogic(cfg, "Alpha", "MyLogic", start_line=100)

    def test_size_and_sha256_describe_whole_file_on_slice(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        import hashlib
        content = b"L1\nL2\nL3\nL4\nL5\n"
        self._setup(projects_root)
        out = core.read_netlogic(cfg, "Alpha", "MyLogic", start_line=2, end_line=3)
        assert out["size"] == len(content)
        assert out["sha256"] == hashlib.sha256(content).hexdigest()
        assert out["total_lines"] == 5


class TestReadNetlogicTruncation:
    """Files over _NETLOGIC_MAX_BYTES are truncated with a nudge."""

    def test_large_file_truncated_flag(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        big = b"x" * (core._NETLOGIC_MAX_BYTES + 1)
        _make_netlogic(projects_root, "Alpha", "BigLogic", big)
        out = core.read_netlogic(cfg, "Alpha", "BigLogic")
        assert out["truncated"] is True
        assert "truncation_hint" in out
        assert "start_line" in out["truncation_hint"] or "end_line" in out["truncation_hint"]

    def test_large_file_size_is_whole_file(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        big = b"y" * (core._NETLOGIC_MAX_BYTES + 100)
        _make_netlogic(projects_root, "Alpha", "BigLogic", big)
        out = core.read_netlogic(cfg, "Alpha", "BigLogic")
        assert out["size"] == len(big)

    def test_small_file_not_truncated(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        small = b"z" * (core._NETLOGIC_MAX_BYTES - 1)
        _make_netlogic(projects_root, "Alpha", "SmallLogic", small)
        out = core.read_netlogic(cfg, "Alpha", "SmallLogic")
        assert "truncated" not in out


class TestRequireCodeEditorsClosed:
    """require_code_editors_closed raises EditorProjectOpen for VS/VS Code,
    but does NOT block on a running Studio."""

    def test_raises_when_editor_has_project_open(
        self, cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from service import studio_guard
        p = make_project(projects_root, "Alpha")
        # Simulate VS Code with the project directory in its cmdline.
        # Name must be lowercase to match EDITOR_PROCS = ("devenv.exe", "code.exe").
        monkeypatch.setattr(
            studio_guard, "_scan",
            lambda: [{"name": "code.exe", "pid": 999, "cmdline": ["code.exe", str(p)]}],
        )
        studio_guard.reset_cache()
        with pytest.raises(core.EditorProjectOpen):
            core.require_code_editors_closed(cfg, p)

    def test_does_not_raise_when_no_editors(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        # _no_host_processes fixture already patches _scan to return []
        p = make_project(projects_root, "Alpha")
        core.require_code_editors_closed(cfg, p)  # must not raise

    def test_read_netlogic_does_not_block_on_studio_running(
        self, cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Studio running alone does NOT block read_netlogic (unlike read_file)."""
        from service import studio_guard
        _make_netlogic(projects_root, "Alpha", "MyLogic", b"// ok\n")
        # Simulate Studio running (but no editors).
        # Name must be lowercase to match STUDIO_PROCS = ("ftoptixstudio.exe",).
        monkeypatch.setattr(
            studio_guard, "_scan",
            lambda: [{"name": "ftoptixstudio.exe", "pid": 42, "cmdline": []}],
        )
        studio_guard.reset_cache()
        # read_netlogic should succeed (Studio alone doesn't block it)
        out = core.read_netlogic(cfg, "Alpha", "MyLogic")
        assert out["studio_open"] is True
        assert out["source"] == "disk"
