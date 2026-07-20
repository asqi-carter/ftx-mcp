"""Pure functions for ftx-mcp.

Each function is deterministic given (Config, args) and a Runner for
subprocess execution. The HTTP and MCP surfaces thin-wrap these.

Domain errors raised here (CoreError subclasses) carry an http_status
hint so the FastAPI layer can translate cleanly. The MCP layer surfaces
them as tool errors.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import studio_guard
from .deploy_lock import DeployLock

# ---- domain errors ----------------------------------------------------

class CoreError(Exception):
    """Base class for domain errors returned to clients via the standardized
    error envelope. Subclasses set `http_status`, `code` (snake_case kind),
    and optionally `hint` (a short remediation pointer) and `docs_anchor`
    (relative anchor under docs/troubleshooting.md)."""
    http_status = 500
    code = "internal_error"
    hint: str | None = None
    docs_anchor: str | None = None


class ProjectNotFound(CoreError):
    http_status = 404
    code = "project_not_found"
    hint = "GET /projects to discover available projects"


class FileNotFound(CoreError):
    http_status = 404
    code = "file_not_found"


class PathTraversal(CoreError):
    http_status = 400
    code = "path_traversal_rejected"
    hint = "Use a path relative to the project root; '..' segments are not allowed"


class BinaryFile(CoreError):
    http_status = 415
    code = "binary_file_unsupported"
    hint = "Only UTF-8 text files are readable via this endpoint"


class StudioMissing(CoreError):
    http_status = 500
    code = "studio_exe_missing"
    hint = "Confirm FT Optix Studio is installed and FTOPTIX_STUDIO_EXE points at FTOptixStudio.exe"


class RuntimeDirNotConfigured(CoreError):
    http_status = 500
    code = "runtime_dir_not_configured"
    hint = "Set OPTIX_RUNTIME_DIR at user-env scope. Default is %LOCALAPPDATA%\\ftx-mcp\\runtime\\"


class TreeSwapFailed(CoreError):
    http_status = 500
    code = "tree_swap_failed"
    hint = "Runtime may still hold a lock on the project tree; verify the runtime stopped, then re-run."


class RuntimeBinaryNotFound(CoreError):
    http_status = 500
    code = "runtime_binary_not_found"
    hint = "Deploy the project first; FTOptixRuntime.exe is staged into the runtime tree by Studio's export."


class CDPUnavailable(CoreError):
    http_status = 503
    code = "cdp_unavailable"
    hint = ("The Chrome DevTools endpoint isn't reachable. Confirm the "
            "ftx-mcp-chrome-cdp task is running (services.ps1 status) "
            "and that Chrome was started with --remote-debugging-port.")


class BridgeUnavailable(CoreError):
    http_status = 503
    code = "bridge_unavailable"
    hint = "The design-time bridge is not serving this project. Open the project in Studio and right-click the StudioBridge NetLogic -> StartBridge, or rely on the file-path fallback."


class BridgeWriteFailed(CoreError):
    http_status = 502
    code = "bridge_write_failed"
    hint = "The bridge reached the live model but the authoring call failed (see message). Common causes: bad node path, unknown UI type, or a value that can't coerce to the property type."


class DeployConfigError(CoreError):
    http_status = 400
    code = "deploy_not_configured"
    hint = "UpdateSvc deploy needs OPTIX_DEPLOY_USERNAME (and usually OPTIX_DEPLOY_IP / OPTIX_DEPLOY_THUMBPRINT) set, plus OPTIX_STUDIO_DEPLOYMENT_PASSWORD in the environment. Run optix_doctor for the full checklist."


class StudioOpen(CoreError):
    http_status = 409
    code = "studio_open"
    hint = (
        "FactoryTalk Optix Studio is running on this box. While a project is "
        "open, Studio's in-memory model is the source of truth: disk reads are "
        "stale and file writes get stomped by Studio's save/close. Close Studio, "
        "then retry. There is no override."
    )
    docs_anchor = "studio-open"


class EditorProjectOpen(CoreError):
    http_status = 409
    code = "editor_project_open"
    hint = (
        "A code editor (VS / VS Code) has this project open; service edits race "
        "unsaved editor buffers. Close the project in the editor, then retry."
    )
    docs_anchor = "editor-project-open"


class InvalidEdit(CoreError):
    http_status = 422
    code = "edit_invalid"
    hint = (
        "Each edit is exactly one of: {path, content} (full replace), "
        "{path, find, replace[, expect_count]} (anchored replace), "
        "{path, insert_after_anchor, block} (anchored insert)."
    )


class EditAnchorMismatch(CoreError):
    http_status = 422
    code = "edit_anchor_mismatch"
    hint = (
        "The batch was refused atomically — no files were written. Re-read the "
        "file (optix_read_file); it may have changed since you last saw it, or "
        "your anchor may not be unique. Widen the anchor or set expect_count."
    )


class InvalidQuery(CoreError):
    http_status = 400
    code = "find_query_invalid"
    hint = "query is a single-line literal (no regex, no newlines); glob must be project-relative"


class BadLineRange(CoreError):
    http_status = 400
    code = "bad_line_range"
    hint = "start_line is 1-based and must not point past EOF; end_line >= start_line"


class ScreenNotFound(CoreError):
    http_status = 404
    code = "screen_not_found"
    hint = "Use optix_list_screens to see the screen/panel names in this project"


class NodeNotFound(CoreError):
    http_status = 404
    code = "node_not_found"
    hint = "Use optix_find to locate the node name and the file it lives in"


class WidgetSpecInvalid(CoreError):
    http_status = 422
    code = "widget_spec_invalid"
    hint = "Each widget is {kind: 'label'|'switch', name, ...}; see optix_add_widget docs for per-kind params"


class StructuralEditUnsupported(CoreError):
    http_status = 422
    code = "structural_edit_unsupported"
    hint = "This shape isn't covered by the granular tool; fall back to an anchored optix_deploy edit"


# ---- runner (subprocess injection point for tests) -------------------

def _tree_kill(pid: int) -> None:
    """Kill the process tree rooted at pid. Best-effort.

    On Windows, subprocess.run(timeout=...) calls Popen.kill() on the
    direct child only (TerminateProcess on that PID), orphaning any
    descendants. FT Optix Studio spawns helper processes during export;
    a hung Studio outlives the timeout-fired kill by minutes. taskkill
    /T traverses the tree, /F forces termination.

    POSIX: requires the child to have been spawned in its own process
    group (preexec_fn=os.setsid in _run_subprocess_with_tree_kill).
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _run_subprocess_with_tree_kill(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """subprocess.run replacement that tree-kills on TimeoutExpired.

    Mirrors the subprocess.run signature used by Runner.run (capture_output,
    text, check, timeout). Without a timeout, falls through to subprocess.run
    so non-timed calls retain identical behavior.

    With a timeout, Popens the child and on TimeoutExpired invokes
    _tree_kill to flatten the process tree before re-raising. This is
    the L fix: Windows subprocess.run only kills the direct child on
    timeout (TerminateProcess), so Studio export children outlive
    deploy_timeout_seconds by minutes (phase2-roadmap Finding 2).
    """
    timeout = kwargs.pop("timeout", None)
    if timeout is None:
        return subprocess.run(cmd, **kwargs)

    capture_output = kwargs.pop("capture_output", False)
    check = kwargs.pop("check", False)
    if capture_output:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    if os.name != "nt":
        # New process group so _tree_kill's killpg can reach grandchildren.
        kwargs.setdefault("start_new_session", True)

    with subprocess.Popen(cmd, **kwargs) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _tree_kill(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout = stderr = "" if kwargs.get("text") else b""
            raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr) from None

    rc = proc.returncode
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, cmd, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, rc, stdout, stderr)


@dataclass
class Runner:
    """Subprocess runner; tests inject a fake to avoid touching Studio.

    Default fn is _run_subprocess_with_tree_kill (L): tree-kills the
    child process tree on TimeoutExpired rather than only Popen.kill'ing
    the direct child.
    """
    fn: Callable[..., subprocess.CompletedProcess] = _run_subprocess_with_tree_kill

    def run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        kwargs.setdefault("check", False)
        return self.fn(cmd, **kwargs)


_DEFAULT_RUNNER = Runner()


# ---- config -----------------------------------------------------------

@dataclass(frozen=True)
class Config:
    projects_root: Path
    studio_exe: Path
    state_dir: Path
    runtime_dir: Path | None = None
    runtime_launcher: str | None = None
    runtime_test_port: int = 8081
    bind_host: str = "127.0.0.1"
    bind_http_port: int = 8765
    bind_mcp_port: int = 8766
    deploy_timeout_seconds: int = 180
    verify_timeout_seconds: int = 30
    verify_poll_seconds: float = 0.5
    runtime_stop_grace_seconds: float = 5.0
    auth_required: bool = True  # conservative default for direct construction;
    # from_env() resolves the real default (FTX_AUTH_REQUIRED, false on loopback)
    # Deploy/runtime integration is DISABLED in this distribution (no env
    # activation path — see Config.from_env). The standard workflow is
    # author -> emulator preview -> verify via MCP; shipping happens from
    # Studio's own Deploy dialog. The implementation remains in source for
    # possible future reintegration; tests may construct Config with this
    # True to exercise the dormant code.
    enable_deploy: bool = False
    tokens_path: Path | None = None
    # CDP debug endpoint (the ftx-mcp-chrome-cdp Chrome). Used for
    # trusted coordinate clicks + screenshots on the Optix canvas. See
    # service/_cdp.py.
    cdp_url: str = "http://127.0.0.1:9222"
    # v0.4 design-time read-bridge (NetLogic HTTP listener inside Studio).
    # When Studio is open with the target project AND the bridge is up, reads
    # route through the live model instead of refusing/file-scanning.
    bridge_url: str = "http://127.0.0.1:8768"  # :8768 since bridge v0.5.0 (was :8767)
    bridge_token: str | None = None
    bridge_enabled: bool = True
    # UpdateSvc CLI-deploy ('deploy' verb) — the production path (vs export+swap).
    # Password is read by the Studio CLI from OPTIX_STUDIO_DEPLOYMENT_PASSWORD in
    # the inherited env, never stored here. ip = the UpdateSvc host (cert-bound
    # hostname, NOT 127.0.0.1 unless the cert is); username = a Windows account on
    # the target (a logged-in one for --run-after-deploy to self-start the runtime).
    deploy_ip_address: str = "127.0.0.1"
    deploy_username: str | None = None
    deploy_thumbprint: str | None = None
    # Pass --disable-source-project-transfer to the deploy verb: the target gets
    # the built runtime but NOT the source .optix tree. Correct + faster for the
    # deploy-to-run workflow (the source lives on the dev box); set False if you
    # need to open/edit the project ON the target. Default on. OPTIX_DEPLOY_KEEP_SOURCE=1
    # restores the old always-transfer-source behavior.
    deploy_disable_source_transfer: bool = True
    # Post-navigate settle before a CDP screenshot/click. The Optix web runtime
    # renders well under 0.3s once :8081 answers (measured: settle
    # 0.3s..3.5s produced byte-identical captures), so the old fixed 3.5s was ~3s
    # of dead wait per verify. 1.0s keeps ~3x headroom. OPTIX_CDP_SETTLE_SECONDS
    # tunes it; callers can still pass an explicit settle_seconds to override.
    cdp_settle_seconds: float = 1.0
    # Silent one-shot self-heal of the chrome-cdp instance. When a CDP tool
    # can't connect (Chrome closed/crashed) or finds no page target (all tabs
    # closed), the session layer transparently opens a page or restarts the
    # ftx-mcp-chrome-cdp task once, then retries. OPTIX_CDP_AUTOHEAL=0
    # disables it (surface the raw CDPUnavailable instead). See
    # core.ensure_chrome_cdp / _cdp_session.
    cdp_autoheal: bool = True

    @classmethod
    def from_env(cls) -> Config:
        local = os.environ.get("LOCALAPPDATA")
        default_state = (
            Path(local) / "ftx-mcp" if local
            else Path.home() / ".local" / "share" / "ftx-mcp"
        )
        state_dir = Path(os.environ.get("OPTIX_STATE_DIR", str(default_state)))
        # runtime_dir defaults to state_dir/runtime so OPTIX_STATE_DIR overrides
        # both state and runtime locations in one shot. OPTIX_RUNTIME_DIR still
        # wins when explicitly set (split-state setups that put the runtime on
        # a separate volume from logs/secrets).
        runtime_dir_env = os.environ.get("OPTIX_RUNTIME_DIR")
        if runtime_dir_env:
            runtime_dir: Path | None = Path(runtime_dir_env)
        else:
            runtime_dir = state_dir / "runtime"
        return cls(
            projects_root=Path(os.environ.get(
                "OPTIX_PROJECTS_ROOT",
                str(Path.home() / "Documents" / "Rockwell Automation"
                    / "FactoryTalk Optix" / "Projects"),
            )),
            studio_exe=Path(os.environ.get(
                "FTOPTIX_STUDIO_EXE",
                r"C:\Program Files\Rockwell Automation\FactoryTalk Optix"
                r"\Studio 1.7.1.46\FTOptixStudio.exe",
            )),
            state_dir=state_dir,
            runtime_dir=runtime_dir,
            runtime_launcher=os.environ.get("OPTIX_RUNTIME_LAUNCHER"),
            runtime_test_port=int(os.environ.get("OPTIX_RUNTIME_TEST_PORT", "8081")),
            bind_host=os.environ.get("OPTIX_BIND_HOST", "127.0.0.1"),
            bind_http_port=int(os.environ.get("OPTIX_HTTP_PORT", "8765")),
            bind_mcp_port=int(os.environ.get("OPTIX_MCP_PORT", "8766")),
            # Default OFF: the common install is loopback-only, where a bearer
            # token adds ~no security (any local process runs as you and can read
            # it) but real friction + a DPAPI failure mode. The LAN guard in
            # main.py still REFUSES to start on a non-loopback bind without auth,
            # so exposing it to the network forces an explicit FTX_AUTH_REQUIRED=true.
            auth_required=os.environ.get("FTX_AUTH_REQUIRED", "false").strip().lower()
                in ("1", "true", "yes", "on"),
            # Deploy/runtime tooling is NOT wired in this distribution: the
            # implementation is retained in source for possible future
            # reintegration, but there is no runtime activation path — this
            # server authors, previews (emulator), and verifies; shipping to
            # hardware happens from Studio's own Deploy dialog.
            # (Was: enable_deploy=os.environ FTX_ENABLE_DEPLOY opt-in.)
            enable_deploy=False,
            tokens_path=Path(os.environ["OPTIX_TOKENS_PATH"])
                if os.environ.get("OPTIX_TOKENS_PATH")
                else state_dir / "secrets" / "tokens.json.dpapi",
            cdp_url=os.environ.get("OPTIX_CDP_URL", "http://127.0.0.1:9222"),
            bridge_url=os.environ.get("OPTIX_BRIDGE_URL", "http://127.0.0.1:8768"),
            bridge_token=os.environ.get("OPTIX_BRIDGE_TOKEN"),
            bridge_enabled=os.environ.get("OPTIX_BRIDGE_ENABLED", "true").strip().lower()
                in ("1", "true", "yes", "on"),
            deploy_ip_address=os.environ.get("OPTIX_DEPLOY_IP", "127.0.0.1"),
            deploy_username=os.environ.get("OPTIX_DEPLOY_USERNAME"),
            deploy_thumbprint=os.environ.get("OPTIX_DEPLOY_THUMBPRINT"),
            deploy_disable_source_transfer=os.environ.get(
                "OPTIX_DEPLOY_KEEP_SOURCE", "").strip().lower()
                not in ("1", "true", "yes", "on"),
            cdp_settle_seconds=float(
                os.environ.get("OPTIX_CDP_SETTLE_SECONDS", "1.0")),
            cdp_autoheal=os.environ.get("OPTIX_CDP_AUTOHEAL", "true").strip().lower()
                in ("1", "true", "yes", "on"),
        )


# ---- helpers ----------------------------------------------------------

def _is_interactive_session() -> bool | None:
    """Returns True if running in a Windows interactive logon session,
    False if running in a service/SSH/network session, None on non-Windows
    (where DPAPI has no equivalent constraint).

    Detection uses GetProcessWindowStation + GetUserObjectInformationW.
    Interactive sessions (RDP, console) bind to WinSta0. Services,
    OpenSSH-spawned processes, and LocalSystem-context processes bind to
    Service-0x*-* window stations. The latter cannot decrypt DPAPI blobs
    written by interactive sessions, which is what makes Studio crash on
    deploy. See docs/troubleshooting.md.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwsta = user32.GetProcessWindowStation()
        if not hwsta:
            return None
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        UOI_NAME = 2
        ok = user32.GetUserObjectInformationW(
            hwsta, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)
        )
        if not ok:
            return None
        return buf.value.lower() == "winsta0"
    except Exception:
        return None


def _now_iso(ts: float | None = None) -> str:
    when = _dt.datetime.fromtimestamp(ts, _dt.UTC) if ts else _dt.datetime.now(_dt.UTC)
    return when.isoformat(timespec="seconds")


def resolve_project(cfg: Config, project: str) -> Path:
    if "/" in project or "\\" in project or ".." in project:
        raise ProjectNotFound(f"invalid project name: {project!r}")
    project_dir = (cfg.projects_root / project).resolve()
    root = cfg.projects_root.resolve()
    if not project_dir.is_dir():
        raise ProjectNotFound(f"project not found: {project}")
    if not project_dir.is_relative_to(root):
        raise ProjectNotFound(f"project not under projects_root: {project}")
    return project_dir


def resolve_subpath(cfg: Config, project: str, subpath: str) -> Path:
    project_dir = resolve_project(cfg, project)
    full = (project_dir / subpath).resolve()
    if not full.is_relative_to(project_dir):
        raise PathTraversal(f"path traversal rejected: {subpath}")
    return full


# ---- read ops ---------------------------------------------------------

def health(cfg: Config) -> dict:
    from . import __version__  # single source of truth — prevents version skew
    return {
        "ok": True,
        "version": __version__,
        "projects_root": str(cfg.projects_root),
        "projects_root_exists": cfg.projects_root.is_dir(),
        "studio_exe": str(cfg.studio_exe),
        "studio_exe_exists": cfg.studio_exe.is_file(),
        "runtime_dir": str(cfg.runtime_dir) if cfg.runtime_dir else None,
        "runtime_dir_exists": cfg.runtime_dir.is_dir() if cfg.runtime_dir else False,
        "runtime_launcher": cfg.runtime_launcher,
        "runtime_test_port": cfg.runtime_test_port,
        "interactive_session": _is_interactive_session(),
        "bind": {
            "host": cfg.bind_host,
            "http_port": cfg.bind_http_port,
            "mcp_port": cfg.bind_mcp_port,
        },
    }


def list_projects(cfg: Config) -> list[dict]:
    if not cfg.projects_root.is_dir():
        return []
    out: list[dict] = []
    for entry in sorted(cfg.projects_root.iterdir()):
        if not entry.is_dir():
            continue
        optix_files = sorted(entry.glob("*.optix"))
        if optix_files:
            out.append({"name": entry.name, "optix_file": optix_files[0].name})
    return out


def require_editors_closed(project_dir: Path, force: bool = False) -> None:
    """Corruption guard: refuse project reads/writes while FTOptixStudio.exe
    is running (blanket rule — Studio's open project is not attributable from
    the outside; see service/studio_guard.py),
    or while VS / VS Code attributably has this project open.

    Detection errors do NOT block: an enumeration fault is not evidence of
    Studio. deploy_preflight surfaces that condition as a warning instead.
    """
    state = studio_guard.studio_state(force=force)
    if state.get("error"):
        return
    if state["studio"]["running"]:
        pids = ", ".join(str(p) for p in state["studio"]["pids"])
        raise StudioOpen(f"FTOptixStudio.exe is running (pid {pids})")
    hits = studio_guard.attributed_editors(state, project_dir)
    if hits:
        ed = hits[0]
        raise EditorProjectOpen(
            f"{ed['name']} (pid {ed['pid']}) has {project_dir.name} open"
        )


def read_file(
    cfg: Config,
    project: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict:
    """Read a UTF-8 project file, optionally a 1-based inclusive line range.

    `size`, `sha256`, and `total_lines` always describe the WHOLE file —
    sha256 doubles as a version fingerprint for anchored edits even when
    only a slice of content is returned.
    """
    project_dir = resolve_project(cfg, project)
    require_editors_closed(project_dir)
    full = resolve_subpath(cfg, project, path)
    if not full.is_file():
        raise FileNotFound(f"file not found: {path}")
    data = full.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BinaryFile(f"file is not valid UTF-8: {path}") from e
    lines = text.splitlines(keepends=True)
    total = len(lines)
    out = {
        "path": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "total_lines": total,
        "content": text,
    }
    if start_line is not None or end_line is not None:
        s = start_line if start_line is not None else 1
        e = end_line if end_line is not None else total
        if s < 1 or e < s:
            raise BadLineRange(f"start_line={s}, end_line={e}")
        if s > total and total > 0:
            raise BadLineRange(f"start_line={s} is past EOF (total_lines={total})")
        e = min(e, total)
        out["content"] = "".join(lines[s - 1 : e])
        out["start_line"] = s
        out["end_line"] = e
    return out


# Directory parts that never hold user-meaningful Optix source. bin/obj are
# the NetSolution build outputs Studio regenerates on every compile.
_FIND_SKIP_PARTS = frozenset({".git", "bin", "obj", ".venv", "__pycache__", ".vs"})
_FIND_MAX_FILE_BYTES = 2_000_000


def find_in_project(
    cfg: Config,
    project: str,
    query: str,
    glob: str = "**/*",
    max_results: int = 200,
    context_lines: int = 2,
    case_sensitive: bool = False,
) -> dict:
    """Literal single-line search across a project's UTF-8 text files.

    Discovery primitive for "which file/line holds this node/screen/
    property" — the precursor to an anchored edit. Skips VCS/build dirs,
    binary files, and files over ~2 MB. Matching is case-insensitive by
    default; no regex.
    """
    if not query:
        raise InvalidQuery("query must be non-empty")
    if "\n" in query or "\r" in query:
        raise InvalidQuery("query must be single-line (anchored edits handle multi-line)")
    max_results = max(1, min(int(max_results), 1000))
    # Bridge path: when Studio is open with THIS project and the
    # bridge is up, the on-disk files are stale (Studio holds the authoritative model)
    # and the disk scan below would hard-refuse via require_editors_closed — exactly
    # when the sibling reads (describe_node / list_screens) succeed. Search the LIVE
    # model instead, for parity. Scoped to node identity (browse-name / path /
    # property name+value), which is the query shape callers reach `find` for while
    # authoring. When the bridge is down or serving a different project, fall through
    # to the file scan unchanged.
    if _use_bridge_for(cfg, project):
        return _bridge_find(cfg, project, query, max_results, case_sensitive)
    project_dir = resolve_project(cfg, project)
    require_editors_closed(project_dir)
    context_lines = max(0, min(int(context_lines), 10))
    needle = query if case_sensitive else query.lower()

    matches: list[dict] = []
    files_scanned = 0
    truncated = False
    try:
        candidates = sorted(p for p in project_dir.glob(glob) if p.is_file())
    except (ValueError, NotImplementedError) as e:
        raise InvalidQuery(f"bad glob {glob!r}: {e}") from e
    resolved_root = project_dir.resolve()
    for f in candidates:
        rel = f.relative_to(project_dir)
        if any(part in _FIND_SKIP_PARTS for part in rel.parts):
            continue
        try:
            f.resolve().relative_to(resolved_root)  # symlink-escape guard
        except ValueError:
            continue
        try:
            if f.stat().st_size > _FIND_MAX_FILE_BYTES:
                continue
            text = f.read_bytes().decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files_scanned += 1
        hay = text if case_sensitive else text.lower()
        if needle not in hay:
            continue
        lines = text.splitlines()
        hay_lines = hay.splitlines()
        for i, hay_line in enumerate(hay_lines):
            if needle not in hay_line:
                continue
            if len(matches) >= max_results:
                truncated = True
                break
            matches.append({
                "path": str(rel).replace("\\", "/"),
                "line": i + 1,
                "text": lines[i][:400],
                "context_before": [x[:400] for x in lines[max(0, i - context_lines) : i]],
                "context_after": [x[:400] for x in lines[i + 1 : i + 1 + context_lines]],
            })
        if truncated:
            break
    return {
        "query": query,
        "glob": glob,
        "case_sensitive": case_sensitive,
        "files_scanned": files_scanned,
        "match_count": len(matches),
        "matches": matches,
        "truncated": truncated,
    }


# ---- v0.4 design-time read-bridge -------------------------------------
#
# A NetLogic HTTP listener inside Studio (studio-bridge/StudioMCPBridge.cs)
# exposes the LIVE project model over loopback. When Studio is open with the
# target project AND the bridge is up, reads route here — turning Studio-open
# from a hard refusal into a MODE. When the bridge is absent or serving a
# different project, every caller falls back to today's file path (incl. the
# deploy guard's refusal). Phase 0 only ADDS a path; it never makes an existing
# path less safe. The bridge solves the attribution the OS guard cannot
# (studio_guard is non-attributable): the bridge serving on its port IS the
# open-project identity.

_bridge_cache: dict | None = None
_bridge_cache_at: float = 0.0
_BRIDGE_CACHE_TTL = 2.0


def _bridge_http(
    cfg: Config, path: str, method: str = "GET", timeout: float = 5.0
) -> tuple[int, bytes]:
    """Request cfg.bridge_url + path. Raises BridgeUnavailable on transport error.

    Short timeout: a hung Studio must never stall a call — a timeout means
    "bridge unavailable, fall back", not a hard error. The bridge's write
    endpoints take their params in the query string (no body), so POST sends an
    empty body purely to select the verb.
    """
    import urllib.error
    import urllib.request
    url = cfg.bridge_url.rstrip("/") + path
    data = b"" if method == "POST" else None
    req = urllib.request.Request(url, method=method, data=data)
    if cfg.bridge_token:
        req.add_header("Authorization", f"Bearer {cfg.bridge_token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (urllib.error.URLError, OSError) as e:
        raise BridgeUnavailable(f"bridge unreachable at {url}: {e}") from e


def _bridge_get_json(cfg: Config, path: str, timeout: float = 5.0) -> tuple[int, dict]:
    """_bridge_http + JSON decode. Non-JSON / empty body -> {}."""
    status, raw = _bridge_http(cfg, path, timeout=timeout)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        data = {}
    return status, data if isinstance(data, dict) else {}


def _bridge_post_json(cfg: Config, path: str, timeout: float = 8.0) -> tuple[int, dict]:
    """POST to a bridge query-param endpoint; JSON-decode the response."""
    status, raw = _bridge_http(cfg, path, method="POST", timeout=timeout)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        data = {}
    return status, data if isinstance(data, dict) else {}


def _bridge_write_result(op: str, status: int, data: dict) -> dict:
    """Interpret a bridge write response: raise on failure, else return data.

    The bridge returns {ok:true,...} on success, {ok:false,error:...} on an
    inline failure, or {error:{code,message}} on a routing/validation error.
    """
    if status == 200 and data.get("ok") is True:
        return data
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or "unknown bridge error"
        code = err.get("code")
        # Keep the machine-readable code (e.g. unsupported_array_write) visible
        # to the caller — the message alone may not name it.
        if code and code not in msg:
            msg = f"{code}: {msg}"
    else:
        msg = err or f"status={status}"
    raise BridgeWriteFailed(f"bridge {op} failed: {msg}")


def _bridge_write_guard(cfg: Config, project: str) -> None:
    """Raise BridgeUnavailable unless the bridge is serving `project` (writes
    mutate the live model — there is no file fallback)."""
    if not _use_bridge_for(cfg, project):
        st = bridge_state(cfg)
        raise BridgeUnavailable(
            f"bridge not serving {project!r} (state: {st.get('reason')}, "
            f"serving={st.get('project')!r})"
        )


def classify_bridge_failure(cfg: Config, project: str, exc: Exception) -> dict:
    """Turn a raw bridge exception into a structured, actionable failure the
    model can relay to the user.

    Unlike the CDP tools, this NEVER auto-restarts — the design-time bridge is a
    NetLogic listener inside the user's FactoryTalk Optix Studio, and StartBridge
    has no programmatic trigger, so the only correct recovery is to nudge the
    operator. Classification uses the bridge's own /bridge/health (which reports
    the serving `project` via Project.Current.BrowseName) and, when the bridge is
    unreachable, the Studio-process signal from studio_guard.

    Returns {state:"failed", reason_code, nudge, detail, bridge:{reachable,
    serving, model_loaded}}. reason_code ∈ {write_failed, bridge_wrong_project,
    bridge_model_loading, bridge_transient, bridge_unreachable_studio_open,
    bridge_unreachable_studio_closed}.
    """
    from . import studio_guard
    detail = str(exc)

    # BridgeWriteFailed = the bridge answered and rejected the op. It is UP —
    # do not let the model misfire an "open Studio" nudge.
    if isinstance(exc, BridgeWriteFailed):
        return {
            "state": "failed", "reason_code": "write_failed",
            "nudge": ("The design-time bridge is up and serving this project — this is a "
                      "per-operation error, not a connection problem, so do NOT restart "
                      "Studio. Check the property name/value; the bridge's own message is "
                      "in `detail`."),
            "detail": detail,
            "bridge": {"reachable": True, "serving": project, "model_loaded": True},
        }

    # BridgeUnavailable → probe health directly for a precise classification.
    reachable, serving, model_loaded = False, None, None
    try:
        status, data = _bridge_get_json(cfg, "/bridge/health")
        reachable = status == 200
        serving = data.get("project")
        model_loaded = bool(data.get("model_loaded"))
    except BridgeUnavailable:
        reachable = False

    if reachable:
        norm = (serving or "").strip().lower()
        if norm and norm not in ("unknown", "") and norm != project.strip().lower():
            code = "bridge_wrong_project"
            nudge = (f"FactoryTalk Optix Studio's bridge is serving {serving!r}, not "
                     f"{project!r}. Ask the user to open {project!r} in Studio (and run "
                     f"StartBridge if the bridge doesn't come up).")
        elif not model_loaded:
            code = "bridge_model_loading"
            nudge = ("The design-time bridge is up but the project isn't loaded yet — "
                     "retry in a few seconds.")
        else:
            code = "bridge_transient"
            nudge = "The design-time bridge reports healthy now — retry the operation."
    else:
        st = studio_guard.studio_state(force=True)
        running = bool(st.get("studio", {}).get("running"))
        if running:
            code = "bridge_unreachable_studio_open"
            nudge = (f"FactoryTalk Optix Studio is running, but its design-time bridge "
                     f"isn't reachable. Ask the user to make sure {project!r} is the open "
                     f"project AND run StartBridge: in the Studio Project tree, right-click "
                     f"the StudioBridge NetLogic node → Run → StartBridge.")
        else:
            code = "bridge_unreachable_studio_closed"
            nudge = (f"FactoryTalk Optix Studio isn't running. Ask the user to open "
                     f"{project!r} in Studio, then run StartBridge (right-click the "
                     f"StudioBridge NetLogic node → Run → StartBridge). This is a live "
                     f"design-time edit — it needs the project open in Studio.")

    return {
        "state": "failed", "reason_code": code, "nudge": nudge, "detail": detail,
        "bridge": {"reachable": reachable, "serving": serving, "model_loaded": model_loaded},
    }


def bridge_set_property(
    cfg: Config, project: str, node_path: str, name: str, value: str,
    locale: str = "en-US",
) -> dict:
    """Set a property on a live-model node via the design-time bridge.

    On a fresh instance the bridge materializes the inherited property via
    GetOrCreateVariable so it persists AND renders (the fix for the GetVariable-
    returns-null trap). Requires Studio
    open with this project + the bridge running.

    Array-typed properties (String[] like GridLayout.Columns/Rows, NodeId[] like
    NavigationPanelItem.AliasNodeArray) are NOT writable: the bridge rejects them
    by declared type (unsupported_array_write) because a scalar write to an array
    UA variable crashed Studio outright (2026-07-16). A JSON-array value signals
    that intent, so reject it here too — before dispatch — so a bridge running an
    older build can never see it.
    """
    probe = value
    if isinstance(probe, str) and probe.lstrip().startswith("["):
        try:
            probe = json.loads(probe)
        except ValueError:
            pass
    if isinstance(probe, (list, tuple)):
        raise BridgeWriteFailed(
            f"bridge set_property rejected: unsupported_array_write — value for "
            f"{name!r} is a JSON array. Array-typed properties (String[] like "
            f"GridLayout.Columns/Rows, NodeId[] like NavigationPanelItem."
            f"AliasNodeArray) can't be written via set_property; author them in "
            f"Studio directly."
        )
    return _bridge_write(
        cfg, project, "set_property", "/bridge/node/property",
        {"path": node_path, "name": name, "value": value, "locale": locale},
    )


def bridge_create_widget(
    cfg: Config, project: str, screen: str, name: str, widget_type: str = "Label",
) -> dict:
    """Create a builtin UI widget on a screen in the live model via the bridge."""
    return _bridge_write(
        cfg, project, "create_widget", "/bridge/ui/widget",
        {"name": name, "screen": screen, "type": widget_type},
    )


def bridge_create_variable(
    cfg: Config, project: str, name: str, parent: str = "Model",
    datatype: str = "Boolean",
) -> dict:
    """Create a model variable in the live model via the bridge."""
    return _bridge_write(
        cfg, project, "create_variable", "/bridge/model/variable",
        {"name": name, "parent": parent, "datatype": datatype},
    )


def bridge_create_folder(cfg: Config, project: str, parent: str, name: str) -> dict:
    """Create a structural Folder (OpcUa FolderType) in the live model."""
    return _bridge_write(
        cfg, project, "create_folder", "/bridge/model/folder",
        {"parent": parent, "name": name},
    )


def bridge_create_object(
    cfg: Config, project: str, parent: str, name: str,
    object_type: str | None = None,
) -> dict:
    """Create a plain Object container (BaseObjectType), or an instance of a
    project-defined ObjectType when `object_type` is a path (the reuse half of
    the create_type/templates workflow)."""
    params = {"parent": parent, "name": name}
    if object_type:
        params["type"] = object_type
    return _bridge_write(
        cfg, project, "create_object", "/bridge/model/object", params)


def bridge_create_type(
    cfg: Config, project: str, name: str, parent: str,
    base_type: str | None = None,
) -> dict:
    """Create an ObjectType (reusable template) in the live model. base_type is
    a builtin catalog name (RowLayout, ...) or a path to another ObjectType;
    empty = bare BaseObjectType-derived."""
    params = {"name": name, "parent": parent}
    if base_type:
        params["base"] = base_type
    return _bridge_write(
        cfg, project, "create_type", "/bridge/model/type", params)


def bridge_move_node(
    cfg: Config, project: str, node_path: str, new_parent: str,
    new_name: str | None = None,
) -> dict:
    """Reparent a live instance by re-authoring: copy the subtree under the new
    parent (link fixups included), then delete the original. The node gets a
    NEW NodeId — inbound references from elsewhere are not rewritten."""
    params = {"path": node_path, "new_parent": new_parent}
    if new_name:
        params["new_name"] = new_name
    return _bridge_write(
        cfg, project, "move_node", "/bridge/node/move", params)


def bridge_convert_to_type(
    cfg: Config, project: str, node_path: str, type_name: str,
    types_folder: str, replace: bool = True,
) -> dict:
    """Convert a live instance into a reusable ObjectType (Studio's right-click
    refactor, which has no public API): new type subtyping the instance's own
    type, children MOVED in, original optionally replaced by an instance of the
    new type. Response reports moved_children, link audit
    (links_verified/relative_links_unverified/broken_links) and steps."""
    return _bridge_write(
        cfg, project, "convert_to_type", "/bridge/node/convert-to-type",
        {"path": node_path, "type_name": type_name, "types_folder": types_folder,
         "replace": "true" if replace else "false"},
    )


def bridge_add_label(
    cfg: Config, project: str, screen: str, name: str, text: str,
    left: float | None = None, top: float | None = None, locale: str = "en-US",
) -> dict:
    """One-shot: create a Label on `screen` and set its Text (+ optional position)
    via the live bridge — collapses create_widget + set_property x1-3 into a single
    call (the common "add a label" case). Each underlying step raises
    BridgeWriteFailed on failure, so a partial failure surfaces the failing step.
    Returns {ok, created_path, text, left, top}.
    """
    bridge_create_widget(cfg, project, screen, name, "Label")
    path = f"{screen}/{name}"
    bridge_set_property(cfg, project, path, "Text", text, locale)
    if left is not None:
        bridge_set_property(cfg, project, path, "LeftMargin", str(left))
    if top is not None:
        bridge_set_property(cfg, project, path, "TopMargin", str(top))
    return {"ok": True, "created_path": path, "text": text, "left": left, "top": top}


def bridge_ensure_web_engine(
    cfg: Config, project: str, port: int = 8081, ip: str = "0.0.0.0",
) -> dict:
    """Ensure a Web presentation engine exists under UI via the design-time bridge.

    Without a WebUIPresentationEngine the deployed runtime serves no canvas — this
    is the manual "add UI → Web presentation engine" setup step from fresh-box
    validation. Idempotent: the bridge returns {existed:true} if one is already
    present, else creates + configures one (Port, Protocol=HTTP, StartWindow →the
    first window) and returns {existed:false, path, port, start_window}. Requires
    Studio open with this project + the bridge running.
    """
    return _bridge_write(
        cfg, project, "ensure_web_engine", "/bridge/setup/web-engine",
        {"port": str(int(port)), "ip": ip},
    )


def audit(cfg: Config, event: str, **fields) -> None:
    """Append one JSONL line to the local audit trail
    (state_dir/logs/audit.jsonl): every model-mutating operation (bridge
    writes, saves, emulator lifecycle, CDP input) records what/when/outcome.
    Local file, plain JSON, no redaction needed (no secrets pass through
    authoring params). Best-effort: auditing must never break the operation."""
    try:
        d = cfg.state_dir / "logs"
        d.mkdir(parents=True, exist_ok=True)
        rec = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
               "event": event, **fields}
        with open(d / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def traffic(cfg: Config, tool: str, chars_in: int, chars_out: int,
            ms: int, ok: bool) -> None:
    """Append one JSONL line of per-tool-call traffic stats
    (state_dir/logs/traffic.jsonl): tool name, request/response sizes in
    characters, wall-clock ms, outcome. Sizes only — argument and result
    CONTENT is never recorded here (the audit trail covers mutations).
    Feeds local usage/cost estimation (chars/4 ~ tokens). Best-effort:
    stats must never break the call."""
    try:
        d = cfg.state_dir / "logs"
        d.mkdir(parents=True, exist_ok=True)
        rec = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
               "tool": tool, "chars_in": chars_in, "chars_out": chars_out,
               "ms": ms, "ok": ok}
        with open(d / "traffic.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _bridge_write(
    cfg: Config, project: str, op: str, endpoint: str, params: dict,
    *, method: str = "POST",
) -> dict:
    """Guard + (POST|GET) a bridge authoring endpoint + interpret the result.

    Shared shape for the semantic-authoring wrappers below. Several target
    endpoints require the bridge's main-thread-marshaled write path;
    until that ships the bridge replies not_implemented / property_not_materialized
    and this raises BridgeWriteFailed with the message (no crash).
    """
    from urllib.parse import urlencode, quote
    _bridge_write_guard(cfg, project)
    # quote_via=quote (percent-encoding, space -> %20) NOT the default quote_plus
    # (space -> +): the bridge's C# query parser percent-decodes but treats '+' as a
    # literal, so a plain "hello from cowork" arrived as "hello+from+cowork". %20
    # round-trips to a real space.
    qs = urlencode(params, quote_via=quote)
    if method == "GET":
        status, data = _bridge_get_json(cfg, f"{endpoint}?{qs}")
    else:
        status, data = _bridge_post_json(cfg, f"{endpoint}?{qs}")
    try:
        out = _bridge_write_result(op, status, data)
    except Exception as exc:
        audit(cfg, "bridge_write", project=project, op=op, params=params,
              ok=False, error=str(exc))
        raise
    audit(cfg, "bridge_write", project=project, op=op, params=params, ok=True)
    return out


def bridge_add_bound_widget(
    cfg: Config,
    project: str,
    screen: str,
    name: str,
    widget_type: str,
    left: float | None = None,
    top: float | None = None,
    width: float | None = None,
    height: float | None = None,
    text: str | None = None,
    bind_property: str | None = None,
    source_path: str | None = None,
    mode: str = "Read",
) -> dict:
    """Composite: create a widget, position it, optionally set its text and
    bind one property — the create/set/set/bind dance in one call.

    TRANSACTIONAL: the underlying bridge writes raise on failure, so any step
    failing after creation triggers an automatic ROLLBACK (the created node
    is deleted) — no orphaned half-configured widgets, and a retry with the
    same name is safe. The failure names its step: {ok: false, failed_step,
    steps, rolled_back, error}.
    """
    steps: list[str] = ["create"]
    created = bridge_create_widget(cfg, project, screen, name, widget_type)
    node_path = created.get("created_path") or f"{screen}/{name}"

    def _fail(step: str, exc: Exception) -> dict:
        rolled_back = False
        try:
            bridge_delete_node(cfg, project, node_path)
            rolled_back = True
        except Exception:
            pass
        out = {"ok": False, "failed_step": step, "steps": steps,
               "rolled_back": rolled_back, "error": str(exc)}
        if not rolled_back:
            out["orphaned_path"] = node_path
            out["hint"] = ("rollback failed — delete the half-configured node "
                           f"at {node_path} before retrying")
        return out

    # LeftMargin/TopMargin are the settable position properties on Optix
    # visual items (Left/Top are not settable — same mapping add_label uses)
    props = [("LeftMargin", left), ("TopMargin", top), ("Width", width),
             ("Height", height), ("Text", text)]
    for pname, pval in props:
        if pval is None:
            continue
        step = f"set {pname}"
        try:
            bridge_set_property(cfg, project, node_path, pname, str(pval))
        except (BridgeUnavailable, BridgeWriteFailed) as e:
            return _fail(step, e)
        steps.append(step)
    if bind_property and source_path:
        step = f"bind {bind_property}"
        try:
            bridge_bind_property(cfg, project, node_path, bind_property,
                                 source_path, mode)
        except (BridgeUnavailable, BridgeWriteFailed) as e:
            return _fail(step, e)
        steps.append(step)
    return {"ok": True, "created_path": node_path, "type": widget_type,
            "steps": steps}


def bridge_add_navigation_panel_item(
    cfg: Config,
    project: str,
    panel_path: str,
    title: str,
    screen_path: str | None = None,
    name: str | None = None,
) -> dict:
    """Composite: add a tab to a NavigationPanel — create the item (the bridge
    auto-routes it into Panels), set its Title (an empty Title renders an
    invisible zero-width tab, so title is required), and point it at a screen."""
    item_name = name or "".join(c for c in title if c.isalnum()) or "Tab"
    created = bridge_create_widget(cfg, project, panel_path, item_name,
                                   "NavigationPanelItem")
    node_path = created.get("created_path") or f"{panel_path}/Panels/{item_name}"

    def _fail(step: str, exc: Exception) -> dict:
        rolled_back = False
        try:
            bridge_delete_node(cfg, project, node_path)
            rolled_back = True
        except Exception:
            pass
        out = {"ok": False, "failed_step": step, "rolled_back": rolled_back,
               "error": str(exc)}
        if not rolled_back:
            out["orphaned_path"] = node_path
        return out

    try:
        bridge_set_property(cfg, project, node_path, "Title", title)
    except (BridgeUnavailable, BridgeWriteFailed) as e:
        return _fail("set Title", e)
    if screen_path:
        try:
            bridge_set_property(cfg, project, node_path, "Panel", screen_path)
        except (BridgeUnavailable, BridgeWriteFailed) as e:
            return _fail("set Panel", e)
    return {"ok": True, "created_path": node_path, "title": title,
            "panel": screen_path}


def bridge_bind_property(
    cfg: Config, project: str, node_path: str, name: str,
    source_path: str | None = None, mode: str = "Read",
    raw_path: str | None = None,
) -> dict:
    """Bind a node property to a model variable (DynamicLink).

    `node_path`.`name` receives a dynamic link to `source_path`; `mode` in
    {Read, Write, ReadWrite} (FTOptix DynamicLinkMode). Live-model write.

    `raw_path` (instead of source_path) writes a LITERAL NodePath —
    "{Alias1}/MyInt" or "../../Alias1/MyInt" — resolved per instance at
    RUNTIME, never at bind time. This is the alias/template late-binding
    mechanism; a resolvable source_path through an alias is a contradiction.
    """
    if bool(source_path) == bool(raw_path):
        raise BridgeWriteFailed(
            "bridge bind_property rejected: pass exactly one of source_path "
            "(resolvable now) or raw_path (literal NodePath for alias/template "
            "late binding)")
    params = {"path": node_path, "name": name, "mode": mode}
    if source_path:
        params["source"] = source_path
    else:
        params["raw"] = raw_path
    return _bridge_write(
        cfg, project, "bind_property", "/bridge/node/bind", params)


def bridge_create_alias(
    cfg: Config, project: str, parent_path: str, name: str,
    target_path: str | None = None, kind: str | None = None,
) -> dict:
    """Create an alias `name` under `parent_path`. `target_path` is optional —
    a template's alias is unassigned by design (instances point it somewhere).
    `kind` (builtin type name or a path to a type node) sets the type
    constraint Studio's "+ Alias" carries."""
    params = {"parent": parent_path, "name": name}
    if target_path:
        params["target"] = target_path
    if kind:
        params["kind"] = kind
    return _bridge_write(
        cfg, project, "create_alias", "/bridge/node/alias", params)


# The builtin FT Optix UI event types wireable via the bridge, canonical casing.
# This is the AUTHORITATIVE set — verified live against the bridge's
# ResolveEventType surface (FTOptix.UI.ObjectTypes public static NodeId *Event
# fields). Do NOT add speculative names (an earlier list guessed KeyDownEvent /
# MouseEnterEvent / ValueChangedEvent, none of which this bridge can resolve —
# suggesting one would send the caller after a non-existent event). The bridge
# (0.9.21+) returns this same set as valid_events on a miss, so the two agree.
_CANONICAL_UI_EVENTS = (
    "MouseClickEvent", "MouseDoubleClickEvent", "MouseDownEvent", "MouseEvent",
    "MouseUpEvent", "URLRedirectionEvent", "UserValueChangedEvent",
)
# Frequent non-canonical names an LLM reaches for -> the real event. This is the
# exact trap the A/B measured: describe-first discipline did NOT save arms that
# guessed "Click" — the canonical name isn't derivable, it must be known. Every
# target here MUST be in _CANONICAL_UI_EVENTS (a wireable event).
_EVENT_ALIASES = {
    "click": "MouseClickEvent", "clicked": "MouseClickEvent",
    "onclick": "MouseClickEvent", "mouseclick": "MouseClickEvent",
    "tap": "MouseClickEvent", "press": "MouseClickEvent", "pressed": "MouseClickEvent",
    "doubleclick": "MouseDoubleClickEvent", "dblclick": "MouseDoubleClickEvent",
    "mousedown": "MouseDownEvent", "mouseup": "MouseUpEvent",
    "mousemove": "MouseEvent", "mouse": "MouseEvent",
    "change": "UserValueChangedEvent", "changed": "UserValueChangedEvent",
    "valuechanged": "UserValueChangedEvent", "redirect": "URLRedirectionEvent",
    "urlredirect": "URLRedirectionEvent",
}


def _canonicalize_event(event_type: str) -> dict | None:
    """Client-side nudge for the documented wrong-event-name trap.

    Returns None when `event_type` is a recognized canonical event (case-insensitive
    match -> caller proceeds with the canonical casing baked in by the caller). Returns
    a structured reject dict (mirroring the property guard's shape) when the name is a
    known alias for a real event — so the model gets the right name immediately instead
    of a bare bridge error. An UNKNOWN name (not canonical, not a known alias) returns
    None and is passed through to the bridge, which is the authority for the full
    catalog and rejects with event_not_found.
    """
    key = event_type.strip().lower().removesuffix("event")
    canon_by_key = {e.lower().removesuffix("event"): e for e in _CANONICAL_UI_EVENTS}
    if key in canon_by_key:
        return None  # recognized (any casing) — let it through
    if key in _EVENT_ALIASES:
        suggestion = _EVENT_ALIASES[key]
        return {
            "ok": False, "code": "noncanonical_event", "given": event_type,
            "suggestion": suggestion,
            "message": (
                f"'{event_type}' is not a builtin FT Optix event name. Use "
                f"'{suggestion}'. (Event names are not derivable from describe_type — "
                "they must be the exact builtin identifier.)"
            ),
            "valid_events": list(_CANONICAL_UI_EVENTS),
        }
    return None  # unknown — bridge is the authority


def bridge_wire_event(
    cfg: Config, project: str, node_path: str, event_type: str,
    method_path: str | None = None, *,
    command: str | None = None, variable: str | None = None,
    value: str | None = None,
) -> dict:
    """Wire a UI event on `node_path` — to a native command OR a NetLogic ExportMethod.

    `event_type` is a builtin event type name (e.g. MouseClickEvent). Provide EITHER:
      - a native `command` (no custom NetLogic needed): "SetVariable" (needs
        `variable` + `value`) or "ToggleVariable" (needs `variable`). These wire to
        the builtin FTOptix VariableCommands object — the preferred path for common
        actions (set/toggle a variable from a button).
      - a `method_path` ("ObjectPath/MethodName") pointing at a NetLogic [ExportMethod],
        for custom logic.

    A client-side guard catches the common wrong-event-name trap (e.g. "Click" ->
    "MouseClickEvent") and returns a structured suggestion before hitting the bridge;
    genuinely-unknown names pass through to the bridge, which is authoritative.
    """
    nudge = _canonicalize_event(event_type)
    if nudge is not None:
        return nudge
    if command:
        params: dict[str, str] = {"path": node_path, "event": event_type, "command": command}
        if variable is not None:
            params["variable"] = variable
        if value is not None:
            params["value"] = value
    elif method_path:
        params = {"path": node_path, "event": event_type, "method": method_path}
    else:
        raise BridgeWriteFailed("wire_event needs either command (+variable[/value]) or method_path")
    return _bridge_write(cfg, project, "wire_event", "/bridge/node/event", params)


def bridge_add_translation(
    cfg: Config, project: str, key: str, value: str, locale: str = "en-US",
) -> dict:
    """Add or update a translation for a LocalizedText `key`."""
    return _bridge_write(
        cfg, project, "add_translation", "/bridge/i18n/translation",
        {"key": key, "value": value, "locale": locale},
    )


def bridge_delete_node(cfg: Config, project: str, node_path: str) -> dict:
    """Delete a node (and its outbound references) from the live model."""
    return _bridge_write(
        cfg, project, "delete_node", "/bridge/node/delete", {"path": node_path},
    )


def bridge_node_references(cfg: Config, project: str, node_path: str) -> dict:
    """Find nodes referencing `node_path` (delete-impact analysis). Read-only."""
    return _bridge_write(
        cfg, project, "node_references", "/bridge/node/references",
        {"path": node_path}, method="GET",
    )


def bridge_reorder_node(
    cfg: Config, project: str, node_path: str,
    position: str | None = None, index: int | None = None,
) -> dict:
    """Reorder a node among its siblings = z-order (render order is child order;
    last child renders in front). `position` in {front, back} OR an explicit
    `index`. Uses node.MoveUp()/MoveDown(); only effective on graphic objects inside
    a TYPE (ScreenType/PanelType). Live-model write."""
    params: dict[str, str] = {"path": node_path}
    if position is not None:
        params["position"] = position
    if index is not None:
        params["index"] = str(int(index))
    return _bridge_write(cfg, project, "reorder", "/bridge/node/reorder", params)


def bridge_attach_expression(
    cfg: Config, project: str, node_path: str, prop_name: str,
    expression: str, sources: str | None = None,
) -> dict:
    """Attach an ExpressionEvaluator converter to a property (roadmap tool A).
    `expression` is the FT Optix formula ("dumb Excel"): {0},{1},.. placeholders
    bound to the `sources` (comma-separated model/node paths) in order. e.g.
    expression='if({0} > 40, 0xFFFF0000, 0xFF00FF00)', sources='Model/Speed' on a
    FillColor. Subsumes ConditionalConverter/Linear/etc. Live-model write."""
    params: dict[str, str] = {"path": node_path, "name": prop_name, "expression": expression}
    if sources:
        params["sources"] = sources
    return _bridge_write(cfg, project, "attach_expression", "/bridge/node/attach-expression", params)


def bridge_validate_expression(
    cfg: Config, project: str, expression: str, sources: str | None = None,
) -> dict:
    """Syntax-check an ExpressionEvaluator formula WITHOUT attaching it.

    Optix only validates a formula at RUNTIME (a bad one silently no-ops), so this
    catches the common author-time mistakes up front (unbalanced ()/{}, out-of-range
    {N} placeholders, unknown functions). Returns {valid, sources, error?}. The SAME
    check gates optix_bridge_attach_expression and the bridge's ValidateExpression
    right-click method. Read-only (no model change)."""
    params: dict[str, str] = {"expression": expression}
    if sources:
        params["sources"] = sources
    return _bridge_write(cfg, project, "validate_expression", "/bridge/expr/validate", params)


def ui_stats(cfg: Config) -> dict:
    """Aggregate live status for the /ui dashboard. Defensive — never raises;
    every source is wrapped so a down bridge/cdp still yields a usable payload."""
    from . import __version__
    out: dict = {"service": {"version": __version__}, "bridge": {"reachable": False},
                 "cdp": {}, "runtime": {}, "doctor": [], "capabilities": {}}
    try:
        doc = doctor(cfg)
        out["doctor"] = doc.get("checks", [])
        for c in out["doctor"]:
            n = c.get("name")
            if n == "interactive_session":
                out["service"]["interactive"] = c.get("ok")
            elif n == "cdp":
                out["cdp"]["alive"] = c.get("ok")
    except Exception:
        pass
    try:
        st = bridge_state(cfg)
        out["bridge"] = {"reachable": bool(st.get("available")),
                         "version": st.get("bridge_version"),
                         "project": st.get("project"),
                         "model_loaded": st.get("model_loaded", st.get("available"))}
        # Last-saved marker for the served project (newest node-YAML mtime) so the
        # dashboard can show "saved Ns ago" next to the Save control.
        proj = st.get("project")
        if proj:
            try:
                out["bridge"]["last_saved_epoch"] = _project_max_mtime(resolve_project(cfg, proj))
            except Exception:
                pass
    except Exception:
        pass

    # Config / flags panel — what's toggled on this install (read-only surface).
    try:
        gentle = _gentle_focus()
        out["flags"] = {
            "bind_host": cfg.bind_host,
            "loopback": cfg.bind_host == "127.0.0.1",
            "auth_required": bool(cfg.auth_required),
            "gentle_save": gentle,
            "cdp_autoheal": bool(getattr(cfg, "cdp_autoheal", False)),
            "deploy_ip": cfg.deploy_ip_address,
            "deploy_enabled": bool(cfg.enable_deploy),
            "deploy_configured": bool(cfg.deploy_username and cfg.deploy_thumbprint),
            "disable_source_transfer": bool(cfg.deploy_disable_source_transfer),
        }
    except Exception:
        pass
    try:
        status, data = _bridge_get_json(cfg, "/bridge/types/ui")
        types = data.get("types", []) if status == 200 else []
        out["capabilities"]["widget_types"] = len(types)
        out["capabilities"]["gallery"] = [t.get("browse_name") for t in types[:60] if t.get("browse_name")]
    except Exception:
        pass
    try:
        port = getattr(cfg, "runtime_test_port", None)
        out["runtime"]["port"] = port
        # 3-state emulator status (cached — the console polls every few
        # seconds and the discriminated check shells out). port_reachable alone
        # is NOT "emulator running": the UpdateSvc-deployed app is the same exe
        # on the same port and auto-relaunches at boot.
        if port:
            st = _emulator_state_cached(cfg)
            out["runtime"]["serving"] = bool(st.get("port_reachable"))
            out["runtime"]["emulator_state"] = st.get("state")
    except Exception:
        pass
    return out


def bridge_state(cfg: Config, force: bool = False) -> dict:
    """Cached snapshot of the design-time bridge.

    Returns {available, project, bridge_version, reason}. `available` is True
    only when the bridge is enabled, answers /bridge/health 200, AND reports
    model_loaded. Cached ~2s (reads arrive in bursts). Never raises — an
    unreachable bridge is a normal "unavailable", not an error.
    """
    global _bridge_cache, _bridge_cache_at
    now = time.time()
    if not force and _bridge_cache is not None and (now - _bridge_cache_at) < _BRIDGE_CACHE_TTL:
        return _bridge_cache
    if not cfg.bridge_enabled:
        state = {"available": False, "project": None, "bridge_version": None, "reason": "disabled"}
    else:
        # The single-threaded listener can briefly stop accepting connections while
        # Studio does heavy designer work (e.g. materializing a ScreenType), so a
        # lone health probe can TIME OUT even though the bridge is fine and every
        # operational endpoint still works. Retry only the
        # TRANSPORT-failure path a few times with a short timeout so a transient block
        # isn't cached as "down". A well-formed HTTP response (even model_loaded=False)
        # means the listener is up -> decide immediately, no retry.
        state = {"available": False, "project": None, "bridge_version": None, "reason": "unreachable"}
        for i in range(3):
            try:
                status, data = _bridge_get_json(cfg, "/bridge/health", timeout=2.5)
                if status == 200 and data.get("model_loaded"):
                    state = {
                        "available": True,
                        "project": data.get("project"),
                        "bridge_version": data.get("bridge_version"),
                        "reason": "ok",
                    }
                else:
                    state = {
                        "available": False,
                        "project": data.get("project"),
                        "bridge_version": data.get("bridge_version"),
                        "reason": f"health status={status} model_loaded={data.get('model_loaded')}",
                    }
                break  # got a response -> listener is up, don't retry
            except BridgeUnavailable as e:
                state = {"available": False, "project": None, "bridge_version": None, "reason": str(e)}
                if i < 2:
                    time.sleep(0.4)
    _bridge_cache, _bridge_cache_at = state, now
    return state


def reset_bridge_cache() -> None:
    """Test hook: drop the bridge-state TTL cache between cases."""
    global _bridge_cache, _bridge_cache_at
    _bridge_cache, _bridge_cache_at = None, 0.0


def default_project(cfg: Config) -> str | None:
    """The project the design-time bridge is currently serving, if any.

    Lets a caller OMIT `project` and act on the open project — the common
    single-seat flow — instead of naming it every time (and without a
    list_projects round-trip). None when no bridge is serving one.
    """
    try:
        st = bridge_state(cfg)
        return st.get("project") if st.get("available") else None
    except Exception:
        return None


def _use_bridge_for(cfg: Config, project: str) -> bool:
    """True iff the bridge is available AND serving THE requested project.

    The bridge reports Project.Current.BrowseName; match it against the resolved
    project dir name (standard Optix projects name the dir after the project). A
    bridge serving a DIFFERENT project must NOT answer for this one.
    """
    st = bridge_state(cfg)
    if not st["available"] or not st.get("project"):
        return False
    try:
        want = resolve_project(cfg, project).name
    except CoreError:
        return False
    return str(st["project"]).strip().lower() == want.strip().lower()


# Standard Optix top-level roots under Project.Current (the bridge's ResolveNode is
# Project.Current.Get(path)). A find seeds its live-model BFS from these; a root that
# doesn't exist in a given project (e.g. no Objects) 404s and is skipped.
_BRIDGE_FIND_ROOTS = ("UI", "Model", "Objects")
_BRIDGE_FIND_MAX_NODES = 800


def _bridge_find(
    cfg: Config, project: str, query: str, max_results: int, case_sensitive: bool,
) -> dict:
    """Node search over the LIVE model via the bridge — the Studio-open counterpart
    to find_in_project's disk file-scan.

    BFS the model tree from the standard roots and match the query against each
    node's browse-name, path, and property names/values (case-insensitive by
    default). Returns {query, source:"bridge", nodes_visited, match_count,
    matches:[{path, browse_name, node_class, dotnet_type, matched_on, value}],
    truncated}. Scoped to node/property identity, NOT free text-in-files — this is
    the 'find Screen1 while Studio is open' case the guarded file-scan refuses.
    """
    from urllib.parse import quote
    needle = query if case_sensitive else query.lower()

    def _has(s: object) -> bool:
        if s is None:
            return False
        s = str(s)
        return needle in (s if case_sensitive else s.lower())

    matches: list[dict] = []
    visited: set[str] = set()
    truncated = False
    queue: list[str] = list(_BRIDGE_FIND_ROOTS)
    while queue:
        if len(visited) >= _BRIDGE_FIND_MAX_NODES:
            truncated = True
            break
        path = queue.pop(0)
        if path in visited:
            continue
        visited.add(path)
        status, data = _bridge_get_json(cfg, f"/bridge/nodes?path={quote(path, safe='/')}")
        if status != 200 or not data:
            continue  # 404 root / transient — skip, keep walking siblings
        matched_on: str | None = None
        value: object = None
        if _has(data.get("browse_name")) or _has(path):
            matched_on = "name"
        else:
            for p in data.get("properties", []):
                if _has(p.get("name")):
                    matched_on, value = "property_name", p.get("name")
                    break
                if _has(p.get("value")):
                    matched_on, value = "property_value", p.get("value")
                    break
        if matched_on is not None:
            if len(matches) >= max_results:
                truncated = True
                break
            matches.append({
                "path": path,
                "browse_name": data.get("browse_name"),
                "node_class": data.get("node_class"),
                "dotnet_type": data.get("dotnet_type"),
                "matched_on": matched_on,
                "value": value,
            })
        for c in data.get("children", []):
            bn = c.get("browse_name")
            if bn:
                queue.append(f"{path}/{bn}")
    return {
        "query": query,
        "source": "bridge",
        "case_sensitive": case_sensitive,
        "nodes_visited": len(visited),
        "match_count": len(matches),
        "matches": matches,
        "truncated": truncated,
    }


def describe_node(cfg: Config, project: str, path: str) -> dict:
    """Browse one node in the LIVE model via the design-time bridge.

    Returns the bridge node shape {path, browse_name, node_class, dotnet_type,
    children[], properties[], truncated} plus source:"bridge". Requires Studio
    open with this project AND the bridge running — this is a live-model-only,
    typed-introspection capability with no file-path equivalent, so it raises
    BridgeUnavailable rather than falling back.
    """
    from urllib.parse import quote
    if not _use_bridge_for(cfg, project):
        st = bridge_state(cfg)
        raise BridgeUnavailable(
            f"bridge not serving {project!r} (state: {st.get('reason')}, "
            f"serving={st.get('project')!r})"
        )
    status, data = _bridge_get_json(cfg, f"/bridge/nodes?path={quote(path, safe='/')}")
    if status == 404:
        raise NodeNotFound(f"no node at path {path!r} in the live model")
    if status != 200 or not data:
        raise BridgeUnavailable(f"bridge /bridge/nodes returned status={status}")
    data["source"] = "bridge"
    return data


def _bridge_list_screens(cfg: Config, project: str) -> dict:
    """Screen list from the LIVE model via the bridge /bridge/screens endpoint."""
    status, data = _bridge_get_json(cfg, "/bridge/screens")
    if status != 200 or "screens" not in data:
        raise BridgeUnavailable(f"bridge /bridge/screens returned status={status}")
    screens = data.get("screens", [])
    return {"screens": screens, "count": len(screens), "source": "bridge"}


def list_ui_types(cfg: Config, project: str) -> dict:
    """The builtin UI type catalog from the LIVE model via the bridge.

    Returns {types:[{name, browse_name}], count, truncated, source:"bridge"}.
    Bridge-only (the catalog lives in Studio's type system, not on disk).
    """
    if not _use_bridge_for(cfg, project):
        st = bridge_state(cfg)
        raise BridgeUnavailable(
            f"bridge not serving {project!r} (state: {st.get('reason')})"
        )
    status, data = _bridge_get_json(cfg, "/bridge/types/ui")
    if status != 200 or "types" not in data:
        raise BridgeUnavailable(f"bridge /bridge/types/ui returned status={status}")
    data["source"] = "bridge"
    return data


def describe_type(cfg: Config, project: str, type_name: str) -> dict:
    """Property schema of a builtin UI type via the bridge /bridge/types/schema.

    Returns {type, browse_name, properties:[{name, datatype}], truncated,
    source:"bridge"}. Bridge-only typed introspection — raises BridgeUnavailable
    when Studio/the bridge is down, NodeNotFound for an unknown type.
    """
    from urllib.parse import quote
    if not _use_bridge_for(cfg, project):
        st = bridge_state(cfg)
        raise BridgeUnavailable(
            f"bridge not serving {project!r} (state: {st.get('reason')})"
        )
    status, data = _bridge_get_json(cfg, f"/bridge/types/schema?type={quote(type_name, safe='')}")
    if status == 404:
        raise NodeNotFound(f"no builtin UI type {type_name!r}")
    if status != 200 or not data:
        raise BridgeUnavailable(f"bridge /bridge/types/schema returned status={status}")
    data["source"] = "bridge"
    return data


def _render_map_outline(node: dict, indent: int = 0, ids: bool = False) -> list[str]:
    """Compact indented outline from the bridge's map tree — one line per node,
    2-3x leaner in tokens than the JSON tree for LLM consumption."""
    pad = "  " * indent
    label = node.get("name", "?")
    if node.get("coll"):
        label += " {" + node["coll"] + "}"      # placeholder collection: element type
    elif node.get("type"):
        label += " (" + node["type"] + ")"
    if node.get("ref"):
        label += "  -> " + node["ref"]          # pointer/link target, dereferenced
    if ids and node.get("id"):
        label += "  [" + node["id"] + "]"
    if node.get("n") is not None:
        label += f"  (+{node['n']} inside)"     # unexpanded: hidden descendants
    if node.get("vars"):
        label += f"  ({node['vars']} vars)"     # overview: folded leaf plumbing
    lines = [pad + label]
    for c in node.get("children", []):
        lines.extend(_render_map_outline(c, indent + 1, ids))
    if node.get("more"):
        lines.append("  " * (indent + 1) + f"... +{node['more']} more (raise max_nodes)")
    return lines


def get_project_map(
    cfg: Config,
    project: str,
    path: str | None = None,
    depth: int | None = None,
    max_nodes: int = 800,
    ids: bool = False,
    match: str | None = None,
    fmt: str = "outline",
) -> dict:
    """Project component map in ONE bridge call — the cheap alternative to
    walking with repeated describe_node.

    Depth is DYNAMIC by node kind (bridge mode=auto): pointed at a FOLDER
    (or unscoped), the walk expands folders recursively and renders each
    COMPONENT as one line with its descendant count — variables/methods fold
    into "(N vars)" — orientation without plumbing. Pointed at a COMPONENT
    (MainWindow, a screen), the walk goes full-detail (depth 6). Passing
    depth= explicitly forces a full walk at that depth. Truncation (max_nodes
    budget) is always explicit, never silent. fmt="outline" (default) is the
    token-lean indented text; fmt="json" the raw tree.
    """
    from urllib.parse import quote
    mode = "detail" if depth is not None else "auto"
    if depth is None:
        depth = 6
    if not _use_bridge_for(cfg, project):
        st = bridge_state(cfg)
        raise BridgeUnavailable(
            f"bridge not serving {project!r} (state: {st.get('reason')})"
        )
    q = (f"/bridge/map?depth={int(depth)}&max={int(max_nodes)}"
         f"&ids={1 if ids else 0}&mode={mode}")
    if match:
        q += f"&match={quote(match, safe='')}"
    if path:
        q += f"&path={quote(path, safe='/')}"
    status, data = _bridge_get_json(cfg, q)
    if status == 404:
        raise NodeNotFound(f"no node at path {path!r}")
    if status != 200 or not data:
        raise BridgeUnavailable(f"bridge /bridge/map returned status={status}")
    if data.get("mode") == "search":
        matches = data.get("matches", [])
        out_s: dict = {
            "project": project, "path": path or "(project root)",
            "mode": "search", "match": match,
            "hit_count": len(matches), "visited": data.get("visited"),
            "hits_capped": bool(data.get("hits_capped")),
            "source": "bridge",
        }
        if fmt == "json":
            out_s["matches"] = matches
        else:
            out_s["map"] = "\n".join(
                f"{m.get('path')} ({m.get('type')})" for m in matches) or "(no matches)"
        return out_s
    tree = data.get("map") or {}
    out: dict = {
        "project": project, "path": path or "(project root)",
        "mode": data.get("mode", mode), "depth": depth, "max_nodes": max_nodes,
        "truncated": (data.get("budget_left", 1) or 0) <= 0,
        "source": "bridge",
    }
    if fmt == "json":
        out["map"] = tree
    else:
        out["map"] = "\n".join(_render_map_outline(tree, ids=ids))
    return out


# ---- edit resolution (docs/architecture.md, Edit modes) ---------------

def _file_eol(text: str) -> str:
    """Dominant EOL of a file: CRLF when any CRLF is present, else LF."""
    return "\r\n" if "\r\n" in text else "\n"


def _to_eol(s: str, eol: str) -> str:
    """Normalize the caller's newlines to the target file's EOL, so a
    skill/agent can always write '\\n' and match CRLF files byte-exactly."""
    return s.replace("\r\n", "\n").replace("\n", eol)


def _resolve_edit_content(target: Path, edit: dict, rel: str) -> tuple[bytes, dict]:
    """Compute the post-edit bytes for one edit WITHOUT writing.

    deploy() resolves the whole batch first and only then writes, so any
    anchor mismatch refuses the batch atomically — zero files touched.
    """
    if "content" in edit:
        new_text = edit["content"]
        new_bytes = new_text.encode("utf-8")
        before = target.stat().st_size if target.is_file() else 0
        return new_bytes, {
            "path": rel,
            "mode": "content",
            "bytes_before": before,
            "bytes_after": len(new_bytes),
        }

    if "find" not in edit and "insert_after_anchor" not in edit:
        raise InvalidEdit(f"unrecognized edit shape for {rel}: keys={sorted(edit)}")

    # Anchored modes operate on the file's current text.
    if not target.is_file():
        raise FileNotFound(f"file not found for anchored edit: {rel}")
    data = target.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BinaryFile(f"file is not valid UTF-8: {rel}") from e
    eol = _file_eol(text)

    if "find" in edit:
        if "replace" not in edit:
            raise InvalidEdit(f"edit on {rel} has 'find' without 'replace'")
        find = _to_eol(edit["find"], eol)
        replace = _to_eol(edit["replace"], eol)
        if not find:
            raise InvalidEdit(f"empty 'find' on {rel}")
        expected = edit.get("expect_count", 1)
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise InvalidEdit(f"expect_count on {rel} must be a positive integer")
        n = text.count(find)
        if n != expected:
            raise EditAnchorMismatch(
                f"{rel}: 'find' matched {n} time(s), expected {expected}"
            )
        new_text = text.replace(find, replace)
        new_bytes = new_text.encode("utf-8")
        return new_bytes, {
            "path": rel,
            "mode": "find_replace",
            "occurrences": n,
            "bytes_before": len(data),
            "bytes_after": len(new_bytes),
        }

    # insert_after_anchor
    if "block" not in edit:
        raise InvalidEdit(f"edit on {rel} has 'insert_after_anchor' without 'block'")
    anchor = _to_eol(edit["insert_after_anchor"], eol)
    block = _to_eol(edit["block"], eol)
    if not anchor:
        raise InvalidEdit(f"empty 'insert_after_anchor' on {rel}")
    if not block:
        raise InvalidEdit(f"empty 'block' on {rel}")
    n = text.count(anchor)
    if n != 1:
        raise EditAnchorMismatch(
            f"{rel}: insert_after_anchor matched {n} time(s), expected exactly 1"
        )
    idx = text.find(anchor)
    line_end = text.find(eol, idx + len(anchor))
    if line_end == -1:
        # anchor sits on the final, unterminated line
        insert_at = len(text)
        lead = eol if text and not text.endswith(eol) else ""
    else:
        insert_at = line_end + len(eol)
        lead = ""
    if not block.endswith(eol):
        block += eol
    new_text = text[:insert_at] + lead + block + text[insert_at:]
    new_bytes = new_text.encode("utf-8")
    return new_bytes, {
        "path": rel,
        "mode": "insert_after_anchor",
        "bytes_before": len(data),
        "bytes_after": len(new_bytes),
    }


# ---- granular edit tools ------------------------------------------------
# These RESOLVE edits and return them; they never write. The caller forwards
# the returned `edits` to optix_deploy, keeping the guarded/locked/verified
# write path singular. Composable: collect edits from several tool calls into
# one optix_deploy (the proven demo flow — switch + label + model var).

def _ui_yaml_files(project_dir: Path) -> list[str]:
    """Project-relative UI node YAMLs, where screens/panels live."""
    root = project_dir / "Nodes" / "UI"
    if not root.is_dir():
        return []
    return sorted(
        str(p.relative_to(project_dir)).replace("\\", "/")
        for p in root.rglob("*.yaml")
        if p.is_file()
    )


def _read_lines(cfg: Config, project: str, rel: str) -> list[str] | None:
    full = resolve_subpath(cfg, project, rel)
    if not full.is_file():
        return None
    try:
        return full.read_bytes().decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None


def list_screens(cfg: Config, project: str, glob: str = "Nodes/UI/**/*.yaml") -> dict:
    """Enumerate Screen/Panel/Dialog nodes across a project's UI YAML.

    Routes through the design-time bridge (live model) when Studio is open with
    this project AND the bridge is up; otherwise the file path runs unchanged —
    including require_editors_closed, which still refuses if Studio is open with
    no bridge. The `source` field ("bridge"|"file") records which path answered.
    """
    if _use_bridge_for(cfg, project):
        return _bridge_list_screens(cfg, project)

    from . import optix_model

    project_dir = resolve_project(cfg, project)
    require_editors_closed(project_dir)
    screens: list[dict] = []
    for p in sorted(project_dir.glob(glob)):
        if not p.is_file():
            continue
        rel = str(p.relative_to(project_dir)).replace("\\", "/")
        try:
            lines = p.read_bytes().decode("utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for s in optix_model.list_screens(lines):
            screens.append({**s, "file": rel})
    return {"screens": screens, "count": len(screens), "source": "file"}


def _locate_screen(cfg: Config, project: str, screen: str, screen_file: str | None):
    """Return (rel, lines, NodeSpan) for `screen`, or raise ScreenNotFound."""
    from . import optix_model

    project_dir = resolve_project(cfg, project)
    candidates = [screen_file] if screen_file else _ui_yaml_files(project_dir)
    for rel in candidates:
        lines = _read_lines(cfg, project, rel)
        if lines is None:
            continue
        node = optix_model.find_node(lines, screen, type_filter=optix_model.SCREEN_TYPES)
        if node is not None:
            return rel, lines, node
    raise ScreenNotFound(f"no Screen/Panel named {screen!r} in {'the given file' if screen_file else 'Nodes/UI'}")


_WIDGET_PARAMS = {
    "label": {"name", "text", "left", "top", "width", "height", "text_color", "font_size", "visible_bind"},
    "switch": {"name", "checked_bind", "left", "top", "width", "height"},
}


def add_widget(
    cfg: Config,
    project: str,
    screen: str,
    widgets: list[dict],
    screen_file: str | None = None,
) -> dict:
    """Resolve an edit that adds one or more widgets to a screen's children.

    widgets: [{kind: 'label'|'switch', name, ...params}]. Returns
    {edits, file, screen, widgets, preview} — forward `edits` to optix_deploy.
    """
    from . import optix_model, optix_templates

    if not widgets:
        raise WidgetSpecInvalid("widgets list is empty")
    require_editors_closed(resolve_project(cfg, project))
    rel, lines, node = _locate_screen(cfg, project, screen, screen_file)

    blocks: list[str] = []
    names: list[str] = []
    for w in widgets:
        if not isinstance(w, dict) or "kind" not in w or "name" not in w:
            raise WidgetSpecInvalid(f"widget needs at least {{kind, name}}: {w!r}")
        kind = w["kind"]
        builder = optix_templates.WIDGET_BUILDERS.get(kind)
        if builder is None:
            raise WidgetSpecInvalid(f"unknown widget kind {kind!r}; supported: {sorted(optix_templates.WIDGET_BUILDERS)}")
        allowed = _WIDGET_PARAMS[kind]
        extra = set(w) - {"kind"} - allowed
        if extra:
            raise WidgetSpecInvalid(f"{kind} got unsupported params {sorted(extra)}; allowed: {sorted(allowed)}")
        try:
            block = builder(**{k: v for k, v in w.items() if k != "kind"})
        except TypeError as e:
            raise WidgetSpecInvalid(f"{kind} {w.get('name')!r}: {e}") from e
        blocks.append(block)  # column-0; plan_first_child reindents
        names.append(w["name"])

    block_col0 = "\n".join(blocks)
    edit = {"path": rel, **optix_model.plan_first_child(lines, node, block_col0)}
    return {
        "edits": [edit],
        "file": rel,
        "screen": screen,
        "widgets": names,
        "preview": block_col0,
    }


def add_model_variable(
    cfg: Config,
    project: str,
    name: str,
    datatype: str = "Boolean",
    value: bool = False,
    model_file: str = "Nodes/Model/Model.yaml",
) -> dict:
    """Resolve an edit adding a Boolean variable to the Model folder — the
    bind target a Switch writes and a Label's Visible reads.

    The emitted variable is the BARE export-safe shape (Name/Type/DataType
    only). `value` is accepted for API stability but NOT emitted: an explicit
    `Value` / `AccessLevel` on a file-added model variable hangs Studio export
    on FactoryTalk-template projects (W4 finding — see optix_templates.boolean_var
    and docs/optix-patterns/model-variable-export-safety.md)."""
    from . import optix_model, optix_templates

    if datatype != "Boolean":
        raise StructuralEditUnsupported(
            f"add_model_variable tier-1 supports Boolean only, got {datatype!r}"
        )
    require_editors_closed(resolve_project(cfg, project))
    lines = _read_lines(cfg, project, model_file)
    if lines is None:
        raise NodeNotFound(f"model file not found: {model_file}")
    # The Model folder node owns the variables; insert as its first child.
    # A fresh project's Model folder is an empty stub (no Children:), so
    # plan_first_child creates the Children block for the first variable.
    model_node = optix_model.find_node(lines, "Model")
    if model_node is None:
        raise StructuralEditUnsupported(
            f"{model_file} has no Model node; add the variable via an anchored edit"
        )
    block_col0 = optix_templates.boolean_var(name)
    edit = {"path": model_file, **optix_model.plan_first_child(lines, model_node, block_col0)}
    return {
        "edits": [edit],
        "file": model_file,
        "variable": name,
        "target_path": f"{{Model}}/{name}",
        "preview": block_col0,
    }


def set_property(
    cfg: Config,
    project: str,
    file: str,
    widget: str,
    property: str,
    value: str,
) -> dict:
    """Resolve a find/replace edit that changes an inline shorthand property
    (Text, Left, Top, Width, Height, TextColor, ...) on a named widget.

    Returns {edits, file, widget, property, old_value, new_value}. Child-node
    properties (bindings, expanded variables) are not inline — those raise
    structural_edit_unsupported; use an anchored optix_deploy edit.
    """
    from . import optix_model

    require_editors_closed(resolve_project(cfg, project))
    lines = _read_lines(cfg, project, file)
    if lines is None:
        raise NodeNotFound(f"file not found: {file}")
    node = optix_model.find_node(lines, widget)
    if node is None:
        raise NodeNotFound(f"no node named {widget!r} in {file}")
    prop_re = re.compile(rf"^(?P<indent> *){re.escape(property)}:\s*(?P<val>.*?)\s*$")
    prop_idx = None
    for j in range(node.start + 1, node.end):
        m = prop_re.match(lines[j])
        # only the widget's OWN inline property (at the node's body indent),
        # never a child node's same-named property deeper in the block.
        if m and len(m.group("indent")) == node.body_indent:
            prop_idx = j
            old_val = m.group("val")
            break
    if prop_idx is None:
        raise StructuralEditUnsupported(
            f"{property!r} is not an inline property on {widget!r} (it may be a child node); use an anchored edit"
        )
    # Unique find = the widget's Name header .. the property line. The Name
    # line makes the slice unique even if the bare property line recurs.
    old_slice = "\n".join(lines[node.start : prop_idx + 1])
    new_prop_line = f"{' ' * node.body_indent}{property}: {value}"
    new_slice = "\n".join(lines[node.start : prop_idx] + [new_prop_line])
    edit = {"path": file, "find": old_slice, "replace": new_slice, "expect_count": 1}
    return {
        "edits": [edit],
        "file": file,
        "widget": widget,
        "property": property,
        "old_value": old_val,
        "new_value": value,
    }


def studio_version(cfg: Config, runner: Runner = _DEFAULT_RUNNER) -> dict:
    if not cfg.studio_exe.is_file():
        return {
            "ok": False,
            "error": "studio_exe missing",
            "studio_exe": str(cfg.studio_exe),
        }
    proc = runner.run([str(cfg.studio_exe), "--version"], timeout=10)
    return {
        "ok": proc.returncode == 0,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "returncode": proc.returncode,
    }


# UI-automation save: Studio is native C++/Qt with NO programmatic save API
# (verified by reflection against the installed Studio assemblies), so persisting the live model to disk is a focused Ctrl+S. The window with a real
# title = the project window (a home-screen Studio has none).
#
# Focus Studio, then Ctrl+S. Two things make this reliable:
#
#   1. Foreground: the save() caller is the long-lived service process, and a
#      plain SetForegroundWindow / AppActivate from a non-foreground background
#      process can be blocked by Windows' foreground-lock. AttachThreadInput
#      (attach the calling thread to the current foreground window's thread across
#      the SetForegroundWindow call) lets a background process legitimately take
#      the foreground. We deliberately do NOT tap ALT to lift the lock — ALT
#      activates Studio's menu bar and the subsequent Ctrl+S then targets the menu
#      and no-ops. AppActivate stays as a last-ditch fallback.
#
#   2. **Integrity level (the load-bearing requirement):** SendKeys is a UIPI
#      operation — a MEDIUM-integrity process cannot inject input into a HIGHER-
#      integrity (elevated) window. So the service and Studio must run at the SAME
#      integrity. The normal case is both non-elevated: a layman double-clicks
#      Studio (medium) and the service task is RunLevel=Limited (medium) — save
#      works. If Studio is launched ELEVATED (e.g. from an admin/RunLevel=Highest
#      context) while the service is Limited, SetForegroundWindow can still read
#      True but the Ctrl+S is silently dropped by UIPI and the save no-ops to
#      saved=False. Diagnosed live (an elevated-launch test
#      artifact): a medium service could not save an elevated Studio; relaunching
#      Studio non-elevated made every service /save succeed (~1.5-2.7s). Fix if you
#      hit this: run Studio non-elevated, or run the service RunLevel=Highest.
#      saved=False with focused=True across repeated calls is the integrity-
#      mismatch tell (surfaced as a hint on the save result).
def _bridge_port(cfg: Config) -> int:
    """TCP port of the bridge listener parsed from cfg.bridge_url (default 8768)."""
    from urllib.parse import urlparse
    return urlparse(cfg.bridge_url).port or 8768


def _bridge_owner_pid(cfg: Config, runner: Runner = _DEFAULT_RUNNER) -> int | None:
    """PID owning the bridge's TCP listener — i.e. the Studio instance HOSTING the
    design-time bridge (the bridge NetLogic runs inside that Studio process). Used by
    save() to target Ctrl+S at the SAME instance the bridge authored into.
    Windows-only (Get-NetTCPConnection); returns None if not resolvable."""
    port = _bridge_port(cfg)
    ps = (
        f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen "
        f"-ErrorAction SilentlyContinue | Select-Object -First 1; "
        f"if ($c) {{ Write-Output $c.OwningProcess }}"
    )
    try:
        proc = runner.run(["powershell", "-NoProfile", "-Command", ps], timeout=10)
    except Exception:
        return None
    s = (proc.stdout or "").strip()
    return int(s) if s.isdigit() else None


def _gentle_focus() -> bool:
    """Gentle window focus is the DEFAULT: only un-minimize,
    never un-maximize/resize Studio, and hand the foreground back afterwards.
    FTX_SAVE_GENTLE_FOCUS=0/false is the escape hatch back to the legacy
    unconditional SW_RESTORE (kept in case a box surfaces where the gentle path
    can't take focus)."""
    return os.environ.get("FTX_SAVE_GENTLE_FOCUS", "1").strip().lower() not in ("0", "false")


def _build_save_ps(target_pid: int = 0, gentle: bool = True, send_key: str = "^s") -> str:
    """Ctrl+S-to-Studio PowerShell. When target_pid > 0 (the bridge's
    Studio instance), select THAT process's window rather than the first Studio
    window — so a two-instance desktop can't Ctrl+S the wrong project. The title
    filter is relaxed for a targeted pick (an authoring window may have an empty
    MainWindowTitle); a non-zero MainWindowHandle is enough. Emits NO_TARGET_WINDOW
    / exit 4 when the targeted instance has no focus-able window.

    `gentle` (default ON; FTX_SAVE_GENTLE_FOCUS=0 opts out)
    exists so that: only SW_RESTORE fires when the window is actually
    MINIMIZED (so a maximized Studio is not un-maximized/resized on every save or
    F5), and after the keystroke completes, return the foreground to whatever
    window the user had (Studio no longer hogs the screen)."""
    if target_pid > 0:
        select = (
            f"$p = Get-Process FTOptixStudio -ErrorAction SilentlyContinue | "
            f"Where-Object {{ $_.Id -eq {target_pid} -and $_.MainWindowHandle -ne 0 }} | "
            "Select-Object -First 1; "
            f"if (-not $p) {{ Write-Output 'NO_TARGET_WINDOW PID={target_pid}'; exit 4 }}; "
        )
    else:
        select = (
            "$p = Get-Process FTOptixStudio -ErrorAction SilentlyContinue | "
            "Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -ne '' } | "
            "Select-Object -First 1; "
            "if (-not $p) { Write-Output 'NO_STUDIO'; exit 3 }; "
        )
    return (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -MemberDefinition '"
        "[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(System.IntPtr h); "
        "[DllImport(\"user32.dll\")] public static extern bool ShowWindow(System.IntPtr h, int c); "
        "[DllImport(\"user32.dll\")] public static extern bool BringWindowToTop(System.IntPtr h); "
        "[DllImport(\"user32.dll\")] public static extern System.IntPtr GetForegroundWindow(); "
        "[DllImport(\"user32.dll\")] public static extern uint GetWindowThreadProcessId(System.IntPtr h, System.IntPtr pid); "
        "[DllImport(\"kernel32.dll\")] public static extern uint GetCurrentThreadId(); "
        "[DllImport(\"user32.dll\")] public static extern bool IsIconic(System.IntPtr h); "
        "[DllImport(\"user32.dll\")] public static extern bool AttachThreadInput(uint a, uint b, bool c);"
        "' -Name FtxFg -Namespace Ftx; "
        + select +
        "$h = $p.MainWindowHandle; "
    "$fg = [Ftx.FtxFg]::GetForegroundWindow(); "
    "$curT = [Ftx.FtxFg]::GetCurrentThreadId(); "
    "$fgT = [Ftx.FtxFg]::GetWindowThreadProcessId($fg, [System.IntPtr]::Zero); "
    "[Ftx.FtxFg]::AttachThreadInput($curT,$fgT,$true) | Out-Null; "
    + (
        # gentle: only un-minimize (never un-maximize a maximized Studio)
        "if ([Ftx.FtxFg]::IsIconic($h)) { [Ftx.FtxFg]::ShowWindow($h,9) | Out-Null }; "
        if gentle else
        "[Ftx.FtxFg]::ShowWindow($h,9) | Out-Null; "                 # 9 = SW_RESTORE
    ) +
    "[Ftx.FtxFg]::BringWindowToTop($h) | Out-Null; "
    "$ok = [Ftx.FtxFg]::SetForegroundWindow($h); "
    "[Ftx.FtxFg]::AttachThreadInput($curT,$fgT,$false) | Out-Null; "
    "if (-not $ok) { $ok = (New-Object -ComObject WScript.Shell).AppActivate($p.Id) }; "
    "Start-Sleep -Milliseconds 400; "
    "[System.Windows.Forms.SendKeys]::SendWait('" + send_key + "'); "
    + (
        # gentle: after the save lands, hand the foreground back so Studio doesn't hog
        "Start-Sleep -Milliseconds 250; "
        "if ($fg -ne [System.IntPtr]::Zero -and $fg -ne $h) { [Ftx.FtxFg]::SetForegroundWindow($fg) | Out-Null }; "
        if gentle else ""
    ) +
    "Write-Output ('FOCUSED=' + $ok + ' PID=' + $p.Id)"
)


def _project_max_mtime(project_dir: Path) -> float:
    """Newest mtime across the project's node YAML (a save bumps these)."""
    latest = 0.0
    nodes = project_dir / "Nodes"
    root = nodes if nodes.is_dir() else project_dir
    for f in root.rglob("*.yaml"):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        latest = max(latest, m)
    return latest


def save(
    cfg: Config,
    project: str,
    timeout: float | None = None,
    runner: Runner = _DEFAULT_RUNNER,
) -> dict:
    """Persist the open project to disk by sending Ctrl+S to Studio (SendKeys).

    The only autonomous save path (Studio has no save API). Requires the service
    to be in an interactive session (session 1) so the keystroke reaches Studio,
    and the project open in Studio. Verifies by polling the project's node-YAML
    mtime until it advances. Returns {saved, mtime_before, mtime_after, focused,
    elapsed_seconds, stdout}. saved=False = keystroke sent but nothing changed
    within timeout (nothing to save, or Studio didn't take focus).
    """
    audit(cfg, "save", project=project)
    project_dir = resolve_project(cfg, project)
    deadline_s = float(timeout) if timeout is not None else 12.0
    before = _project_max_mtime(project_dir)
    # When the bridge serves THIS project, target Ctrl+S at the exact Studio
    # instance hosting the bridge (the PID owning its listener) instead of the first
    # Studio window — with two Studio instances open, "first window" can save the
    # WRONG project silently. When no bridge (target_pid stays 0), behaviour is
    # unchanged: the first focus-able Studio window.
    target_pid = 0
    if _use_bridge_for(cfg, project):
        bp = _bridge_owner_pid(cfg, runner)
        if bp:
            target_pid = bp
    proc = runner.run(
        ["powershell", "-NoProfile", "-Command", _build_save_ps(
            target_pid, gentle=_gentle_focus())],
        timeout=30,
    )
    out = (proc.stdout or "").strip()
    if "NO_STUDIO" in out or proc.returncode == 3:
        return {
            "saved": False, "reason": "no_studio_window",
            "mtime_before": before, "mtime_after": before,
            "focused": False, "elapsed_seconds": 0.0, "stdout": out,
        }
    if "NO_TARGET_WINDOW" in out or proc.returncode == 4:
        # The bridge's Studio instance exists but has no focus-able window to receive
        # the keystroke. Refusing here is safer than falling back to "first window",
        # which could Ctrl+S a different project.
        return {
            "saved": False, "reason": "bridge_studio_no_window",
            "mtime_before": before, "mtime_after": before,
            "focused": False, "elapsed_seconds": 0.0, "stdout": out,
            "bridge_pid": target_pid,
            "hint": (
                "The design-time bridge is serving this project in a Studio instance "
                f"(pid {target_pid}) with no focus-able window, so the save cannot be "
                "targeted at the right instance. Restore/un-minimize that Studio window "
                "and retry."
            ),
        }
    focused = "FOCUSED=True" in out
    started = time.time()
    after = before
    while time.time() - started < deadline_s:
        after = _project_max_mtime(project_dir)
        if after > before:
            break
        time.sleep(cfg.verify_poll_seconds)
    result = {
        "saved": after > before,
        "mtime_before": before, "mtime_after": after,
        "focused": focused,
        "elapsed_seconds": round(time.time() - started, 3),
        "stdout": out,
    }
    # Record that the save was aimed at the bridge's Studio instance, and confirm
    # the window it actually focused belongs to that pid (by construction it should).
    if target_pid:
        m = re.search(r"PID=(\d+)", out)
        result["bridge_pid"] = target_pid
        result["save_target_pid"] = int(m.group(1)) if m else None
        result["targeted_bridge_instance"] = result["save_target_pid"] == target_pid
    # saved=False WITH focused=True is the UIPI integrity-mismatch signature: the
    # keystroke was sent to a window we could focus but not inject into (usually an
    # elevated Studio while the service runs non-elevated). Surface the fix rather
    # than leaving a silent no-op. (Also fires for a genuinely nothing-to-save
    # call; the hint is advisory.)
    if not result["saved"] and focused:
        result["hint"] = (
            "Ctrl+S was sent to a focused Studio but nothing saved. If Studio is "
            "running elevated while this service is not (or vice-versa), Windows "
            "UIPI blocks the keystroke — run both at the same integrity level "
            "(normally: launch Studio non-elevated). Otherwise there may have been "
            "nothing unsaved to save."
        )
    return result


def _studio_configuration_xml() -> Path:
    """Studio's per-user IDE state (window layout, deployment targets)."""
    override = os.environ.get("OPTIX_STUDIO_CONFIG_XML")
    if override:
        return Path(override)
    return (Path(os.path.expandvars("%LOCALAPPDATA%"))
            / "Rockwell Automation" / "FactoryTalk Optix" / "FTOptixStudio"
            / "Configuration.xml")


def studio_active_deployment_target(cfg: Config) -> dict:
    """Which deployment target Studio's dropdown has selected — the thing F5
    actually runs.

    Parses FTOptixStudio/Configuration.xml: the `deployment` item's
    `activeTargetId` resolved against the `targets` collection. The Emulator
    entry is identified structurally (type == 2, ipAddress localhost), not by
    its user-editable display name. Returns {known, is_emulator, name, ip,
    type, source}; known=False (fail-open, with reason) when the file or the
    section can't be read — an absent file must not brick emulator runs on
    installs we haven't seen.
    """
    import xml.etree.ElementTree as ET
    path = _studio_configuration_xml()
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        return {"known": False, "reason": f"config not readable: {exc}",
                "source": str(path)}
    active_id = None
    targets: dict[str, dict] = {}
    for item in root.iter("Item"):
        vals = {v.get("name"): (v.text or "") for v in item.findall("Value")}
        if vals.get("name") == "deployment" and "activeTargetId" in vals:
            active_id = vals.get("activeTargetId")
            for coll in item.findall("Collection"):
                if coll.get("name") != "targets":
                    continue
                for t in coll.findall("Item"):
                    tv = {v.get("name"): (v.text or "") for v in t.findall("Value")}
                    if tv.get("id"):
                        targets[tv["id"]] = tv
    if not active_id:
        return {"known": False, "reason": "no deployment/activeTargetId in config",
                "source": str(path)}
    t = targets.get(active_id)
    if t is None:
        return {"known": False, "reason": f"activeTargetId {active_id} not in targets",
                "source": str(path)}
    ttype = t.get("type", "")
    ip = t.get("ipAddress", "")
    is_emu = ttype == "2" and ip.lower() in ("localhost", "127.0.0.1", "")
    return {"known": True, "is_emulator": is_emu, "name": t.get("name", "?"),
            "ip": ip, "type": ttype, "source": str(path)}


def run_emulator(
    cfg: Config,
    project: str,
    save_first: bool = False,
    wait_ready: bool = True,
    ready_timeout: float = 30.0,
    runner: Runner = _DEFAULT_RUNNER,
) -> dict:
    """Launch the project in Studio's built-in emulator by sending F5.

    The design-time counterpart to a deploy: F5 is Studio's "start" — it stages
    the (in-Studio) project and spins up FTOptixRuntime locally, without touching
    the Application Update Service. F5 itself saves as part of staging, so an
    explicit ^s beforehand is a redundant focus-grab + keystroke round-trip —
    save_first therefore defaults to False (the UpdateSvc
    deploy path is the one that genuinely needs save-first, and keeps it). Pass
    save_first=True only if a caller needs disk-parity for YAML reads BEFORE the
    emulator comes up. Requires session-1 interactivity (the keystroke must
    reach Studio) and the project open.

    F5 brings the runtime up ASYNCHRONOUSLY, so with wait_ready (default) this polls
    the runtime port until it's serving before returning — otherwise a CDP screenshot
    fired immediately hits nothing. Returns {launched, focused, saved, serving,
    waited_seconds, stdout}. launched=False = F5 was sent but Studio wasn't focus-able.
    serving=True means the runtime port answered (safe to screenshot).
    """
    audit(cfg, "emulator_run", project=project)
    # F5 GUARD: F5 runs Studio's SELECTED deployment target, which is only the
    # emulator if the operator's dropdown says so. If Studio's persisted state
    # says a non-emulator target is active, sending F5 could ship to hardware —
    # refuse instead. The dropdown is operator-owned; the service never
    # switches it. (Studio may flush this file lazily, so the post-launch
    # process-identity check below is the second layer.)
    tgt = studio_active_deployment_target(cfg)
    if tgt.get("known") and not tgt.get("is_emulator"):
        audit(cfg, "emulator_run_refused", project=project, target=tgt.get("name"))
        return {
            "launched": False, "focused": False, "saved": None, "serving": False,
            "state": "refused", "reason_code": "active_target_not_emulator",
            "target": {"name": tgt.get("name"), "ip": tgt.get("ip")},
            "nudge": (
                f"Studio's deployment dropdown is set to {tgt.get('name')!r} "
                f"({tgt.get('ip')}). F5 runs the SELECTED target — pressing it "
                "now could deploy to that device, not start the emulator. Ask "
                "the user to switch the target dropdown to Emulator, then retry. "
                "The service never changes the selection itself."),
        }
    saved = None
    if save_first:
        s = save(cfg, project, runner=runner)
        saved = s.get("saved")
    # Aim F5 at the exact Studio instance hosting the bridge, same as
    # save — with two Studio windows open, "first window" can F5 the wrong project.
    target_pid = 0
    if _use_bridge_for(cfg, project):
        bp = _bridge_owner_pid(cfg, runner)
        if bp:
            target_pid = bp
    proc = runner.run(
        ["powershell", "-NoProfile", "-Command",
         _build_save_ps(target_pid, gentle=_gentle_focus(), send_key="{F5}")],
        timeout=30,
    )
    out = (proc.stdout or "").strip()
    if "NO_STUDIO" in out or proc.returncode == 3:
        return {"launched": False, "reason": "no_studio_window",
                "focused": False, "saved": saved, "stdout": out}
    if "NO_TARGET_WINDOW" in out or proc.returncode == 4:
        return {"launched": False, "reason": "bridge_studio_no_window",
                "focused": False, "saved": saved, "stdout": out,
                "bridge_pid": target_pid,
                "hint": ("The bridge's Studio instance has no focus-able window to "
                         "receive F5. Restore/un-minimize that Studio and retry.")}
    focused = "FOCUSED=True" in out
    result = {"launched": focused, "focused": focused, "saved": saved, "stdout": out}
    if focused and wait_ready:
        # F5 spins up FTOptixRuntime + its web engine asynchronously; a CDP screenshot
        # fired immediately hits nothing. Poll the runtime port until it's serving so
        # a caller can screenshot right after.
        import socket
        port = cfg.runtime_test_port
        started = time.time()
        serving = False
        while time.time() - started < ready_timeout:
            sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sk.settimeout(0.5)
            try:
                serving = sk.connect_ex(("127.0.0.1", int(port))) == 0
            except OSError:
                serving = False
            finally:
                sk.close()
            if serving:
                break
            time.sleep(0.5)
        result["serving"] = serving
        result["ready_port"] = port
        result["waited_seconds"] = round(time.time() - started, 1)
        if serving:
            # SECOND LAYER of the F5 target guard: the port answering proves a
            # runtime is up, not WHICH one. Confirm the process identity via
            # the --application-name=Emulator discriminator; if the port
            # answers but no emulator process exists, F5 ran something else
            # (a deployed app, or a non-emulator target) — say so loudly.
            try:
                ident = emulator_status(cfg, runner=runner)
                result["runtime_identity"] = ident.get("state")
                if ident.get("state") == "not_running":
                    result["warning"] = (
                        f"Port :{port} answers but NO emulator process exists — "
                        "F5 ran Studio's selected target and it was not the "
                        "emulator (or a deployed app owns the port). Check "
                        "Studio's deployment dropdown before trusting this run.")
            except Exception:
                pass
        if not serving:
            # Diagnosis ladder (live-earned 2026-07-17: a Studio with
            # "optixServer" selected in the toolbar dropdown ate every F5 and
            # popped a credentials dialog while the emulator never spawned —
            # and Configuration.xml still claimed Emulator, so the file check
            # cannot green-light). Discriminate by process state so the model
            # hypothesizes the RIGHT cause instead of retry-looping F5.
            try:
                st = emulator_status(cfg, runner=runner)
            except Exception:
                st = {}
            result["runtime_identity"] = st.get("state")
            if st.get("state") == "starting":
                result["hint"] = (
                    "The emulator process exists but its port isn't serving yet — "
                    "still building/loading. Poll optix_emulator_status until "
                    "`running`; do NOT resend F5 (it TOGGLES and would stop it).")
            elif st.get("state") == "not_running":
                tgt = studio_active_deployment_target(cfg)
                file_claims_emu = tgt.get("known") and tgt.get("is_emulator")
                result["probable_cause"] = "target_or_modal"
                result["hint"] = (
                    "F5 was sent and Studio took focus, but NO emulator process "
                    "spawned. F5 runs Studio's SELECTED deployment target — the "
                    "most likely causes are (1) the toolbar target dropdown is set "
                    "to another target (a deploy/credentials dialog may have opened) "
                    "or (2) a modal dialog (e.g. the NetLogic security warning) ate "
                    "the keystroke. The service cannot see the dropdown or dialogs"
                    + (" — Studio's saved config claims Emulator, but that file "
                       "lags the live toolbar, so don't trust it" if file_claims_emu else "")
                    + ". Ask the user to: set the target dropdown to Emulator, "
                    "dismiss any open dialog, then retry. Do NOT retry-loop F5 — "
                    "each press fires at whatever target is selected.")
            else:
                result["hint"] = (
                    f"F5 sent + Studio focused, but nothing is serving on :{port} after "
                    f"{int(ready_timeout)}s — the emulator may still be building, or its web "
                    "engine serves a different port. Verify before an optix_cdp_screenshot."
                )
    if not focused:
        result["hint"] = (
            "F5 was sent but no Studio window took focus. If Studio runs elevated "
            "while this service does not (or vice-versa), Windows UIPI blocks the "
            "keystroke — run both at the same integrity level."
        )
    return result


def emulator_status(cfg: Config, runner: Runner = _DEFAULT_RUNNER) -> dict:
    """Emulator state: not_running / starting / running.

    F5 in Studio TOGGLES the emulator, so a caller needs the current state to
    avoid a blind start-that-actually-stops.

    Discriminates the EMULATOR from other FTOptixRuntime.exe instances — the
    UpdateSvc-deployed runtime is the SAME exe, typically on the SAME port, so a
    name-only process match reports "running" for a deployed app when no emulator
    exists. Studio launches the emulator with
    `--application-name=Emulator` on its command line; only those PIDs count.

    States:
      not_running — no emulator process
      starting    — emulator process up, runtime port not serving yet
                    (still building, or hung mid-init)
      running     — emulator process up AND port serving (safe to CDP-screenshot)

    Returns {state, running, pids, port, port_reachable, checked_at}; `running`
    is kept as a bool for back-compat and is True only in the `running` state.
    Adds a `hint` when the port is served by something that is NOT the emulator.
    """
    import socket
    ps = ("$p = Get-CimInstance Win32_Process -Filter \"Name='FTOptixRuntime.exe'\" "
          "-ErrorAction SilentlyContinue | "
          "Where-Object { $_.CommandLine -match '--application-name=Emulator' }; "
          "if ($p) { 'PIDS=' + (($p | ForEach-Object { $_.ProcessId }) -join ',') } else { 'PIDS=' }")
    pids: list[int] = []
    try:
        proc = runner.run(["powershell", "-NoProfile", "-Command", ps], timeout=15)
        m = re.search(r"PIDS=([\d,]*)", (proc.stdout or ""))
        if m and m.group(1):
            pids = [int(x) for x in m.group(1).split(",") if x]
    except Exception:
        pass
    port = cfg.runtime_test_port
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        reachable = s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        reachable = False
    finally:
        s.close()
    if pids and reachable:
        state = "running"
    elif pids:
        state = "starting"
    else:
        state = "not_running"
    out = {"state": state, "running": state == "running", "pids": pids,
           "port": port, "port_reachable": reachable, "checked_at": _now_iso()}
    if not pids and reachable:
        out["hint"] = (
            f"Port :{port} is serving, but NOT by the emulator — likely the "
            "UpdateSvc-deployed runtime (same exe). Check optix_runtime_status; "
            "starting the emulator now may hit a port conflict."
        )
    elif state == "starting":
        out["hint"] = (
            f"Emulator process is up but :{port} isn't serving yet — still "
            "building, or hung. Wait/re-check before an optix_cdp_screenshot."
        )
    return out


def _emulator_log_dir(project: str) -> Path:
    """The emulator's per-project log directory. Studio launches the emulator
    with --logfile-path=%LOCALAPPDATA%\\Rockwell Automation\\FactoryTalk Optix\\
    Emulator\\Log\\<project>; the runtime writes rotating FTOptixRuntime.N.log
    files there (.0 = current). OPTIX_EMULATOR_LOG_ROOT overrides the root
    (tests / non-standard installs)."""
    root = os.environ.get("OPTIX_EMULATOR_LOG_ROOT")
    if root:
        return Path(root) / project
    return (Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
            / "Rockwell Automation" / "FactoryTalk Optix" / "Emulator" / "Log" / project)


def runtime_log_tail(
    cfg: Config,
    project: str,
    lines: int = 100,
    contains: str | None = None,
    max_bytes: int = 262144,
) -> dict:
    """Tail the emulator/NetLogic runtime log for `project` — non-blocking.

    The richer runtime-debug signal (NetLogic output, exceptions) than any
    deploy log; the piece that makes emulator-first debuggable.
    HARD CONSTRAINT (observed): a HELD read
    handle on the live log blocks the runtime's own writes. So this does ONE
    brief shared open, seeks to the last `max_bytes`, reads, and closes
    immediately — it never holds the handle, never uses -Wait semantics.

    Picks the newest FTOptixRuntime.*.log in the project's emulator log dir
    (rotation: .0 is current). `contains` filters lines case-insensitively
    AFTER the tail window is read. Returns {project, file, size, mtime,
    lines, returned_lines, truncated} or {error, hint} when no log exists.
    """
    log_dir = _emulator_log_dir(project)
    if not log_dir.is_dir():
        return {"error": "no_log_dir", "project": project,
                "hint": (f"no emulator log dir at {log_dir} — the emulator has "
                         "never run for this project (optix_run_emulator first)")}
    candidates = sorted(
        (p for p in log_dir.glob("FTOptixRuntime.*.log") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return {"error": "no_log_file", "project": project,
                "hint": f"no FTOptixRuntime.*.log under {log_dir}"}
    log = candidates[0]
    st = log.stat()
    # One brief, non-exclusive open: read only the tail window, close at once.
    with open(log, "rb") as fh:
        if st.st_size > max_bytes:
            fh.seek(st.st_size - max_bytes)
            data = fh.read(max_bytes)
            data = data.split(b"\n", 1)[-1]  # drop the partial first line
            truncated = True
        else:
            data = fh.read()
            truncated = False
    text_lines = data.decode("utf-8", errors="replace").splitlines()
    if contains:
        needle = contains.lower()
        text_lines = [ln for ln in text_lines if needle in ln.lower()]
    tail = text_lines[-max(1, int(lines)):]
    return {"project": project, "file": str(log), "size": st.st_size,
            "mtime": _now_iso(st.st_mtime), "lines": tail,
            "returned_lines": len(tail), "truncated": truncated,
            "filtered": bool(contains)}


def _skills_dir() -> Path:
    """The bundled authoring playbooks (skills/*/SKILL.md). The skill tools
    serve the same content over MCP for Desktop/Cowork/Claude Code clients.
    OPTIX_SKILLS_DIR overrides. Falls back to the pre-rename .claude/skills
    location so an older checkout keeps working."""
    override = os.environ.get("OPTIX_SKILLS_DIR")
    if override:
        return Path(override)
    root = Path(__file__).resolve().parent.parent
    d = root / "skills"
    if d.is_dir():
        return d
    return root / ".claude" / "skills"


def _skill_frontmatter(text: str) -> dict:
    """name/description from the SKILL.md frontmatter (--- fenced)."""
    out: dict = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return out
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        if ":" in ln:
            k, _, v = ln.partition(":")
            out[k.strip()] = v.strip().strip('"')
    return out


def list_skills(cfg: Config) -> dict:
    """One-liner catalog of the bundled playbooks."""
    d = _skills_dir()
    skills = []
    if d.is_dir():
        for p in sorted(d.glob("*/SKILL.md")):
            fm = _skill_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            skills.append({"name": fm.get("name", p.parent.name),
                           "description": fm.get("description", "")})
    return {"skills": skills, "count": len(skills)}


def get_skill(cfg: Config, name: str) -> dict:
    """Full playbook content by name."""
    d = _skills_dir()
    p = d / name / "SKILL.md"
    if not p.is_file():
        available = [x.parent.name for x in d.glob("*/SKILL.md")] if d.is_dir() else []
        raise NodeNotFound(
            f"no skill {name!r} — available: {', '.join(available) or '(none)'}")
    return {"name": name, "content": p.read_text(encoding="utf-8", errors="replace")}


def restart_emulator(
    cfg: Config, project: str, runner: Runner = _DEFAULT_RUNNER,
) -> dict:
    """Stop-if-running -> start -> wait serving. THE way to make a structural
    edit visible: one call replaces the status/stop/run dance and removes the
    F5-toggle footgun entirely (F5 on a running emulator stops it)."""
    st = emulator_status(cfg, runner)
    stopped = None
    if st.get("pids"):
        stopped = stop_emulator(cfg, runner)
    out = run_emulator(cfg, project, save_first=False, wait_ready=True, runner=runner)
    out["restarted"] = bool(st.get("pids"))
    if stopped is not None:
        out["stopped_pids"] = stopped.get("killed_pids", [])
    return out


_EMU_STATE_CACHE: dict = {"t": 0.0, "v": None}


def _emulator_state_cached(cfg: Config, ttl: float = 5.0) -> dict:
    """emulator_status with a short TTL cache, for polling consumers (the
    console dashboard). Tool/HTTP callers use emulator_status directly."""
    now = time.time()
    if _EMU_STATE_CACHE["v"] is None or now - _EMU_STATE_CACHE["t"] > ttl:
        _EMU_STATE_CACHE["v"] = emulator_status(cfg)
        _EMU_STATE_CACHE["t"] = now
    return _EMU_STATE_CACHE["v"]


def stop_emulator(cfg: Config, runner: Runner = _DEFAULT_RUNNER) -> dict:
    """Stop the local FTOptixRuntime emulator by terminating its process(es).

    An explicit, unambiguous stop — vs F5, which toggles and is easy to double-fire.
    Terminates ONLY emulator instances (CommandLine-matched via emulator_status);
    an UpdateSvc-deployed runtime is the same exe and is deliberately left alone.
    Returns {stopped, killed_pids, still_running}.
    """
    audit(cfg, "emulator_stop")
    st = emulator_status(cfg, runner)
    if not st["pids"]:  # pids, not `running` — a "starting" emulator must be stoppable
        return {"stopped": False, "reason": "not_running", "killed_pids": []}
    ids = ",".join(str(p) for p in st["pids"])
    try:
        runner.run(["powershell", "-NoProfile", "-Command",
                    f"Stop-Process -Id {ids} -Force -ErrorAction SilentlyContinue"],
                   timeout=15)
    except Exception as e:
        return {"stopped": False, "reason": f"stop_failed: {e}", "killed_pids": []}
    after = emulator_status(cfg, runner)
    return {"stopped": not after["pids"], "killed_pids": st["pids"],
            "still_running": after["pids"]}


def deploy_updatesvc(
    cfg: Config,
    project: str,
    run_after: bool = False,
    disable_source_transfer: bool | None = None,
    save_first: bool = True,
    runner: Runner = _DEFAULT_RUNNER,
) -> dict:
    """Deploy via the FT Optix Application Update Service (the CLI `deploy` verb).

    The production deploy path (vs export+tree-swap): runs `FTOptixStudio.exe
    deploy <optix> --ip-address --username [--thumbprint] [--run-after-deploy]`,
    which opens the SAVED project FROM DISK, builds, and transfers it to the
    UpdateSvc on `deploy_ip_address`. Because it reads disk, unsaved in-Studio /
    bridge edits would NOT ship — so with save_first (default) we Ctrl+S the project
    first, exactly like run_emulator. The password is read by the CLI from
    OPTIX_STUDIO_DEPLOYMENT_PASSWORD in the inherited env. Deploy as a logged-in
    user with run_after=True and the verb starts the runtime itself (otherwise the
    transfer still completes; only the auto-start hits 22e000b). Requires an
    interactive session. Returns {deployed, saved, ip_address, username,
    run_after_deploy, returncode, stdout_tail}.
    """
    saved = None
    if save_first:
        try:
            saved = save(cfg, project, runner=runner).get("saved")
        except Exception:
            saved = None
    project_dir = resolve_project(cfg, project)
    optix_files = sorted(project_dir.glob("*.optix"))
    if not optix_files:
        raise CoreError(f"no .optix file in project: {project}")
    if not cfg.deploy_username:
        raise DeployConfigError("deploy_username not set (OPTIX_DEPLOY_USERNAME)")
    if not os.environ.get("OPTIX_STUDIO_DEPLOYMENT_PASSWORD"):
        raise DeployConfigError(
            "OPTIX_STUDIO_DEPLOYMENT_PASSWORD not in environment "
            "(the Studio CLI reads the deploy password from it)"
        )
    # Build-race awareness: a local GUI Studio open on the project
    # contends with the deploy verb's own Studio for the NetSolution build (CS2012
    # DLL lock). The verb retries and usually wins, but surface it so a caller can
    # close Studio for a clean run.
    try:
        studio_running = bool(studio_guard.studio_state().get("studio", {}).get("running"))
    except Exception:
        studio_running = None
    cmd = [
        str(cfg.studio_exe), "deploy", str(optix_files[0]),
        f"--ip-address={cfg.deploy_ip_address}",
        f"--username={cfg.deploy_username}",
    ]
    if cfg.deploy_thumbprint:
        cmd.append(f"--thumbprint={cfg.deploy_thumbprint}")
    if run_after:
        cmd.append("--run-after-deploy")
    # Skip transferring the source .optix tree to the target — the target only
    # needs the built runtime for the deploy-to-run + verify loop, and the source
    # lives on the dev box. Per-call override wins over the cfg default.
    skip_source = (cfg.deploy_disable_source_transfer
                   if disable_source_transfer is None else disable_source_transfer)
    if skip_source:
        cmd.append("--disable-source-project-transfer")
    proc = runner.run(cmd, timeout=cfg.deploy_timeout_seconds, env=dict(os.environ))
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    completed = "Deployment successfully completed" in out
    return {
        "deployed": completed,
        "saved": saved,
        "ip_address": cfg.deploy_ip_address,
        "username": cfg.deploy_username,
        "run_after_deploy": run_after,
        "source_transfer_disabled": skip_source,
        "studio_running_locally": studio_running,
        "build_race_warning": (
            "ADVISORY (deploy still succeeded): a local Studio is open on this box. "
            "Its NetSolution build can race the deploy verb's build (CS2012); the verb "
            "retries and wins. Studio staying open is EXPECTED for the live-bridge "
            "loop -- you do NOT need to close it." if studio_running else None
        ),
        "returncode": proc.returncode,
        "stdout_tail": out[-2000:],
    }


# NOTE: serve_deployed_bundle was retired. Deploying as a logged-in
# user with `--run-after-deploy` self-starts the runtime, and the CDP verify path
# is pure loopback (no inbound firewall rule needed), so the separate serve step
# was redundant for the happy path. Recovery (reboot/crash) = re-deploy. The
# original launcher lives in legacy/serve-deployed-bundle.ps1 + git history.


def doctor(cfg: Config) -> dict:
    """One-call dependency check for a layman: every prerequisite + a plain fix.

    Returns {ready, checks:[{name, ok, required, detail, fix}]}. `ready` is True
    when all REQUIRED checks pass (Studio + projects root); feature checks
    (bridge / cdp / deploy / session) are reported but gate only their own
    feature, with a plain-English fix for each red item.
    """
    from urllib.parse import urlparse
    checks: list[dict] = []

    def add(name, ok, detail, fix, required=False):
        checks.append({"name": name, "ok": bool(ok), "required": required,
                       "detail": str(detail), "fix": fix})

    add("studio_exe", cfg.studio_exe.is_file(), cfg.studio_exe,
        "Install FactoryTalk Optix Studio, or set FTOPTIX_STUDIO_EXE to FTOptixStudio.exe.",
        required=True)
    add("projects_root", cfg.projects_root.is_dir(), cfg.projects_root,
        "Create the projects folder, or set OPTIX_PROJECTS_ROOT.", required=True)

    st = bridge_state(cfg)
    add("bridge", st["available"],
        f"version={st.get('bridge_version')} serving={st.get('project')} ({st.get('reason')})",
        "For LIVE authoring: open the project in Studio and right-click the "
        "StudioBridge NetLogic -> StartBridge. Not needed for file-path edits.")

    try:
        from . import _cdp
        cdp_st = _cdp.probe(cfg.cdp_url)
    except Exception:
        cdp_st = {"alive": False, "has_page": False}
    # Healthy = alive AND has a page target. A Chrome that's up but tab-less is
    # not driveable; autoheal opens a page on demand, so gate on `alive` and
    # note the page state in the detail.
    add("cdp", cdp_st["alive"],
        f"{cfg.cdp_url} alive={cdp_st['alive']} has_page={cdp_st['has_page']}",
        "For canvas verify (screenshot/click): start the ftx-mcp-chrome-cdp "
        "task (services.ps1 start), or call optix_cdp_restart, so Chrome exposes "
        "the CDP debug port with a page target.")

    # Deploy prerequisite checks only exist when the deploy integration is
    # wired (it is not in the public distribution) — a red deploy row on a
    # server that cannot deploy is pure confusion.
    if cfg.enable_deploy:
        add("deploy_username", bool(cfg.deploy_username), cfg.deploy_username or "(unset)",
            "For UpdateSvc deploy: set OPTIX_DEPLOY_USERNAME to a Windows account on the target.")
        add("deploy_password", bool(os.environ.get("OPTIX_STUDIO_DEPLOYMENT_PASSWORD")),
            "set" if os.environ.get("OPTIX_STUDIO_DEPLOYMENT_PASSWORD") else "MISSING",
            "For UpdateSvc deploy: set OPTIX_STUDIO_DEPLOYMENT_PASSWORD in the environment.")
        add("deploy_thumbprint", bool(cfg.deploy_thumbprint), cfg.deploy_thumbprint or "(unset)",
            "For UpdateSvc deploy: set OPTIX_DEPLOY_THUMBPRINT (the UpdateSvc certificate thumbprint).")

    try:
        interactive = _is_interactive_session()
    except Exception:
        interactive = None
    add("interactive_session", interactive is not False, f"interactive={interactive}",
        "Run the service in an interactive logon session (session 1) so Studio/runtime "
        "launches and SendKeys save work.")

    return {"ready": all(c["ok"] for c in checks if c["required"]), "checks": checks}


def _tcp_probe(host: str, port: int, timeout: float = 0.5) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def services_status(cfg: Config, runner: Runner = _DEFAULT_RUNNER) -> dict:
    from urllib.parse import urlparse
    return {
        "health": health(cfg),
        "studio_version": studio_version(cfg, runner),
        "runtime_test": {
            "port": cfg.runtime_test_port,
            "tcp_reachable": _tcp_probe("127.0.0.1", cfg.runtime_test_port),
            "checked_at": _now_iso(),
        },
        "cdp": {
            "url": cfg.cdp_url,
            "tcp_reachable": _tcp_probe(
                urlparse(cfg.cdp_url).hostname or "127.0.0.1",
                urlparse(cfg.cdp_url).port or 9222),
            **_cdp_health(cfg),
            "checked_at": _now_iso(),
        },
    }


def _cdp_health(cfg: Config) -> dict:
    """{alive, has_page} for the chrome-cdp endpoint (DevTools HTTP), tolerant
    of a dead endpoint. Richer than the bare TCP probe: a Chrome with all tabs
    closed is TCP-reachable but has no page target to drive."""
    from . import _cdp
    try:
        return _cdp.probe(cfg.cdp_url)
    except Exception:
        return {"alive": False, "has_page": False}


def runtime_status(cfg: Config, slot: str) -> dict:
    """Best-effort runtime probe — returns port-state only.

    'test' probes cfg.runtime_test_port (default 8081); 'mgmt' probes the
    Phase 2 management HMI port (default 8086, OPTIX_HMI_PORT override).
    """
    if slot not in {"test", "mgmt"}:
        raise ProjectNotFound(f"unknown runtime slot: {slot}")
    port = cfg.runtime_test_port if slot == "test" else int(
        os.environ.get("OPTIX_HMI_PORT", "8086")
    )
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        connected = s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        connected = False
    finally:
        s.close()
    return {
        "slot": slot,
        "port": port,
        "tcp_reachable": connected,
        "checked_at": _now_iso(),
    }


# ---- runtime lifecycle ------------------------------------------------

def _runtime_project_dir(cfg: Config, project: str) -> Path:
    """Resolve the swapped-runtime tree for a project under cfg.runtime_dir."""
    if not cfg.runtime_dir:
        raise RuntimeDirNotConfigured("runtime_dir not configured")
    if "/" in project or "\\" in project or ".." in project:
        raise ProjectNotFound(f"invalid project name: {project!r}")
    runtime_project_dir = (cfg.runtime_dir / project).resolve()
    root = cfg.runtime_dir.resolve()
    if not runtime_project_dir.is_dir():
        raise ProjectNotFound(f"runtime tree not found: {project} (deploy first)")
    if not runtime_project_dir.is_relative_to(root):
        raise ProjectNotFound(f"runtime tree not under runtime_dir: {project}")
    return runtime_project_dir


def _minimize_windows_for_pid(pid: int, timeout: float = 2.0) -> int:
    """K: minimize every visible top-level window owned by `pid`.

    FTOptixRuntime is PE subsystem 2 (GUI). DETACHED_PROCESS doesn't
    suppress its main window, and STARTF_USESHOWWINDOW +
    SW_SHOWMINNOACTIVE hints are ignored — Optix opens a small floating
    window anyway. Post-spawn EnumWindows + ShowWindow(SW_MINIMIZE) is
    the working path; window creation is async after CreateProcess
    returns, so we poll for up to `timeout` seconds for a window to
    appear before giving up.

    No-op on non-Windows. Returns the count of windows minimized
    (0 if none appeared within the timeout).
    """
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    SW_MINIMIZE = 6

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    hwnds: list[int] = []

    def collect(hwnd, _lparam):
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid and user32.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    callback = EnumWindowsProc(collect)
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnds.clear()
        user32.EnumWindows(callback, 0)
        if hwnds:
            for hwnd in hwnds:
                user32.ShowWindow(hwnd, SW_MINIMIZE)
            return len(hwnds)
        time.sleep(0.05)
    return 0


def _default_runtime_spawn(exe: Path) -> int:
    """Spawn FTOptixRuntime.exe detached from the calling process.

    On Windows: DETACHED_PROCESS prevents inheriting the parent console (the
    service has none anyway, but the flag also blocks console attach if a
    test harness has one), CREATE_NEW_PROCESS_GROUP makes the child its own
    process group (so SIGINT to the service doesn't propagate). FTOptixRuntime
    is PE Subsystem 2 (GUI), so no console window is shown either way.

    Returns the child PID. The Popen object is intentionally discarded — we
    do not .wait() because the runtime is long-running. stdin/out/err are
    closed so the service can exit without keeping the child's pipes alive.
    """
    if os.name != "nt":
        proc = subprocess.Popen(
            [str(exe)],
            cwd=str(exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.pid
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        [str(exe)],
        cwd=str(exe.parent),
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # K: suppress FTOptixRuntime's floating window. Best-effort; if the
    # window doesn't appear within the poll deadline (e.g. headless
    # WebPresentationEngine-only build) the helper returns 0 and we
    # continue. Failure does not affect deploy success.
    try:
        _minimize_windows_for_pid(proc.pid)
    except Exception:
        pass
    return proc.pid


def _shared_runtime_exe(cfg: Config) -> Path | None:
    """The shared FTOptixRuntime.exe bundled with the Studio install.

    Used by the Path-B shared-exe runtime model: run an ApplicationFiles-style
    tree (no per-project export bundle) emulator-style, the same binary Studio's
    ▶ Run launches. Located under the Studio dir:
    <studio>/FTOptixRuntime/<version>/Win32_x64/FTOptixRuntime.exe.
    """
    studio_dir = cfg.studio_exe.parent
    candidates = sorted(studio_dir.glob("FTOptixRuntime/*/Win32_x64/FTOptixRuntime.exe"))
    return candidates[-1] if candidates else None


def _shared_runtime_spawn(exe: Path, optix_path: Path, app_name: str, log_dir: Path) -> int:
    """Spawn the shared FTOptixRuntime against a project .optix, detached.

    Mirrors Studio's ▶ Run invocation (--application-name / --logfile-path /
    --enable-feature-preview <optix>). Detached so it survives the service
    lifecycle, GUI subsystem so no console. cwd is the runtime tree.
    """
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    args = [
        str(exe),
        f"--application-name={app_name}",
        f"--logfile-path={log_dir}",
        "-l", "INFO",
        "--enable-feature-preview",
        str(optix_path),
    ]
    if os.name != "nt":
        proc = subprocess.Popen(
            args, cwd=str(optix_path.parent),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc.pid
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        args, cwd=str(optix_path.parent), creationflags=flags, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _minimize_windows_for_pid(proc.pid)
    except Exception:
        pass
    return proc.pid


def runtime_start(
    cfg: Config,
    project: str,
    port: int | None = None,
    timeout: float | None = None,
    spawn: Callable[[Path], int] | None = None,
) -> dict:
    """Launch FTOptixRuntime against the swapped runtime tree for `project`.

    Uses the FTOptixRuntime.exe bundled into the runtime tree by Studio's
    `--platform=Win32_x64` export. The spawn is detached so the runtime
    survives the service-process lifecycle. Polls the project's runtime port
    for tcp_reachable until `timeout` seconds elapse.

    The service must be running in a Windows interactive session (session 1)
    for the runtime to launch successfully — same DPAPI/interactive constraint
    as Studio. See docs/troubleshooting.md §Studio crashes.

    Args:
      project: name of a project whose tree is already swapped under runtime_dir
      port: TCP port to probe (default cfg.runtime_test_port, typically 8081).
      timeout: seconds to wait for the port to bind (default 30).
      spawn: test injection point; production uses _default_runtime_spawn.

    Returns:
      {state, project, port, pid, tcp_reachable, started_at, confirmed_at,
       elapsed_seconds, timeout_seconds, runtime_exe}
      state ∈ {running, not_reachable}. not_reachable means we spawned a
      process but its port did not bind within the timeout — typical causes:
      service is in session 0 (not interactive), WebPresentationEngine not
      configured in the project, port collision.
    """
    runtime_project_dir = _runtime_project_dir(cfg, project)
    bundled_exe = runtime_project_dir / "FTOptixApplication" / "FTOptixRuntime.exe"
    optix_path = runtime_project_dir / f"{project}.optix"

    probe_port = int(port) if port is not None else cfg.runtime_test_port
    timeout_seconds = float(timeout) if timeout is not None else 30.0

    if bundled_exe.is_file():
        # Export-bundle model: Studio's --platform export staged a per-project
        # FTOptixRuntime.exe; it self-locates its app, so spawn with no args.
        exe = bundled_exe
        mode = "bundle"
        spawn_fn = spawn or _default_runtime_spawn
    else:
        # Path-B shared-exe model: an ApplicationFiles tree copied in WITHOUT an
        # export bundle (e.g. a Studio-open deploy of the saved tree). Launch the
        # shared FTOptixRuntime.exe from the Studio install against the project
        # .optix, emulator-style — no export needed.
        shared = _shared_runtime_exe(cfg)
        if shared is None or not optix_path.is_file():
            raise RuntimeBinaryNotFound(
                f"no export bundle at {bundled_exe} and no shared-exe fallback "
                f"(shared_runtime={'missing' if shared is None else shared}, "
                f"optix={'present' if optix_path.is_file() else 'missing'})"
            )
        exe = shared
        mode = "shared"
        log_dir = runtime_project_dir / "rt-log"
        spawn_fn = spawn or (lambda e: _shared_runtime_spawn(e, optix_path, project, log_dir))

    started_at = time.time()

    # J: idempotency — if something is already bound to the probe port,
    # a second spawn would orphan the first runtime (Optix doesn't share
    # the port; the second process either fails silently or fights for it).
    # Return without spawning so repeat calls are safe.
    if _tcp_probe("127.0.0.1", probe_port, 0.5):
        confirmed_at = time.time()
        return {
            "state": "already_running",
            "project": project,
            "port": probe_port,
            "pid": None,
            "tcp_reachable": True,
            "started_at": _now_iso(started_at),
            "confirmed_at": _now_iso(confirmed_at),
            "elapsed_seconds": round(confirmed_at - started_at, 3),
            "timeout_seconds": timeout_seconds,
            "runtime_exe": str(exe),
            "mode": mode,
        }

    pid = spawn_fn(exe)

    deadline = started_at + timeout_seconds
    confirmed_at: float | None = None
    while time.time() < deadline:
        if _tcp_probe("127.0.0.1", probe_port, 0.5):
            confirmed_at = time.time()
            break
        time.sleep(cfg.verify_poll_seconds)

    state = "running" if confirmed_at is not None else "not_reachable"
    return {
        "state": state,
        "project": project,
        "port": probe_port,
        "pid": pid,
        "tcp_reachable": confirmed_at is not None,
        "started_at": _now_iso(started_at),
        "confirmed_at": _now_iso(confirmed_at) if confirmed_at else None,
        "elapsed_seconds": round((confirmed_at or time.time()) - started_at, 3),
        "timeout_seconds": timeout_seconds,
        "runtime_exe": str(exe),
        "mode": mode,
    }


def runtime_stop(
    cfg: Config,
    project: str,
    runner: Runner = _DEFAULT_RUNNER,
) -> dict:
    """Stop FTOptixRuntime processes attached to the project's runtime tree.

    Match-and-kill via Get-CimInstance: any FTOptixRuntime.exe whose
    CommandLine references the runtime project dir is sent Stop-Process -Force.
    No-op on non-Windows. Idempotent — stopping when nothing is running is a
    successful no-op.

    Returns: {state, project, stopped_at, runtime_project_dir}.
    state is always "stopped" on success (we cannot reliably enumerate
    pre-/post-kill counts without WMI-on-WMI race).
    """
    runtime_project_dir = _runtime_project_dir(cfg, project)
    controller = RuntimeController(runner=runner)
    controller.stop(cfg, runtime_project_dir)
    return {
        "state": "stopped",
        "project": project,
        "runtime_project_dir": str(runtime_project_dir),
        "stopped_at": _now_iso(),
    }


# ---- CDP coordinate clicks (the Optix-canvas-reliable path) -----------

_CHROME_CDP_TASK = "ftx-mcp-chrome-cdp"


def ensure_chrome_cdp(
    cfg: Config, runner: Runner = _DEFAULT_RUNNER, allow_restart: bool = True,
    wait_seconds: float = 12.0,
) -> dict:
    """Make the CDP Chrome reachable and driveable, healing if needed.

    Two tiers matching the two real failure modes:
      - Tier 1 (cheap): Chrome alive but no page target (all tabs closed) →
        open one via _cdp.ensure_page. No process work.
      - Tier 2 (process): Chrome down (closed/crashed/reboot) and allow_restart
        → (re)start the ftx-mcp-chrome-cdp scheduled task, which is the
        single source of truth for how that Chrome launches (flags/headless/
        port live in install-chrome-cdp.ps1, never duplicated here), then wait
        for the port and open a page.

    Returns {state, alive, has_page, restarted, detail}. state ∈
    {'ok', 'opened_page', 'restarted', 'failed'}. Never raises — a truly broken
    launch (Chrome uninstalled, task deregistered) returns state='failed' with
    a hint rather than looping.
    """
    from urllib.parse import urlparse
    from . import _cdp
    u = urlparse(cfg.cdp_url)
    host = u.hostname or "127.0.0.1"
    port = u.port or 9222

    st = _cdp.probe(cfg.cdp_url)
    if st["alive"] and st["has_page"]:
        return {"state": "ok", "alive": True, "has_page": True,
                "restarted": False, "detail": "already healthy"}
    if st["alive"]:  # up but no page target → Tier 1
        try:
            _cdp.ensure_page(cfg.cdp_url)
            return {"state": "opened_page", "alive": True, "has_page": True,
                    "restarted": False, "detail": "opened a page target"}
        except _cdp.CDPError as e:
            return {"state": "failed", "alive": True, "has_page": False,
                    "restarted": False, "detail": str(e)}

    if not allow_restart:
        return {"state": "failed", "alive": False, "has_page": False,
                "restarted": False,
                "detail": f"CDP {cfg.cdp_url} down and restart disabled"}

    # Tier 2 — relaunch the task, then wait for the port.
    try:
        runner.run(["schtasks", "/run", "/tn", _CHROME_CDP_TASK], timeout=15)
    except Exception as e:
        return {"state": "failed", "alive": False, "has_page": False,
                "restarted": False,
                "detail": f"could not start {_CHROME_CDP_TASK}: {e} "
                          "(is chrome-cdp installed? run bootstrap/install-chrome-cdp.ps1)"}
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if _tcp_probe(host, port, timeout=0.5):
            break
        time.sleep(0.5)
    if not _cdp.probe(cfg.cdp_url)["alive"]:
        return {"state": "failed", "alive": False, "has_page": False,
                "restarted": True,
                "detail": f"started {_CHROME_CDP_TASK} but {cfg.cdp_url} still down "
                          f"after {wait_seconds:.0f}s (see optix_doctor)"}
    try:
        _cdp.ensure_page(cfg.cdp_url)
    except _cdp.CDPError as e:
        return {"state": "failed", "alive": True, "has_page": False,
                "restarted": True, "detail": str(e)}
    return {"state": "restarted", "alive": True, "has_page": True,
            "restarted": True, "detail": f"restarted {_CHROME_CDP_TASK}"}


def _cdp_session(cfg: Config, _heal: bool | None = None):
    """Open a CDP session; raise CDPUnavailable on any transport failure.

    When cfg.cdp_autoheal is on (default), a first connect failure triggers a
    single silent ensure_chrome_cdp() (open a page or restart the task) and one
    retry — so screenshot/click self-recover when Chrome was closed. `_heal` is
    the internal recursion guard (the retry passes False so heal fires once).

    Seam: tests monkeypatch service._cdp.CDPClient (or _connect_ws) so this
    runs without a live Chrome.
    """
    from . import _cdp
    heal = cfg.cdp_autoheal if _heal is None else _heal
    try:
        return _cdp.CDPClient(cfg.cdp_url)
    except (_cdp.CDPError, OSError) as e:
        if heal and ensure_chrome_cdp(cfg)["state"] in (
            "ok", "opened_page", "restarted"
        ):
            return _cdp_session(cfg, _heal=False)
        if isinstance(e, _cdp.CDPError):
            raise CDPUnavailable(str(e)) from e
        raise CDPUnavailable(f"CDP endpoint {cfg.cdp_url} unreachable: {e}") from e


def _runtime_verify_url(cfg: Config) -> str:
    """The URL the CDP runtime-verify tools point at by default: the local
    Optix runtime's web canvas (loopback, on the runtime test port)."""
    return f"http://127.0.0.1:{cfg.runtime_test_port}/"


def _point_screenshot_at_runtime(
    cfg: Config, sess: Any, navigate_url: str | None, settle: float
) -> bool:
    """Point the CDP page for a screenshot and return whether we navigated.

    So the model never has to know (or pass) the runtime URL, yet a
    click→screenshot-result flow doesn't get its state wiped:
      - navigate_url given (non-empty): go there.
      - navigate_url is None: go to the runtime URL, but ONLY if the tab isn't
        already on it — re-navigating would reload the Optix SPA and lose any
        prior click/nav state.
      - navigate_url == "": never navigate (screenshot the current tab as-is).
    """
    if navigate_url == "":
        return False
    if navigate_url:
        sess.navigate(navigate_url)
        time.sleep(max(0.0, settle))
        return True
    # Auto-target the runtime; skip the reload if we're already there.
    target = _runtime_verify_url(cfg)
    origin = target.rstrip("/")
    try:
        current = sess.current_url()
    except Exception:
        current = ""
    if current.startswith(origin):
        return False
    sess.navigate(target)
    time.sleep(max(0.0, settle))
    return True


def cdp_click_runtime(
    cfg: Config, x: float, y: float, navigate_url: str | None = None,
    settle_seconds: float | None = None,
) -> dict:
    """Click viewport (x, y) on the Optix runtime canvas via CDP.

    Uses a trusted CDP Input.dispatchMouseEvent (move→press→release), which —
    unlike a synthetic DOM click — actually reaches Optix's canvas
    hit-tester. When navigate_url is given, the page is pointed there first and
    given settle_seconds to load the Optix canvas before the click (clicking
    mid-navigation fails). Otherwise it clicks whatever Chrome currently shows.
    Returns {state, x, y, navigated, clicked_at}.
    """
    audit(cfg, "cdp_click", x=x, y=y)
    from . import _cdp
    settle = cfg.cdp_settle_seconds if settle_seconds is None else settle_seconds
    sess = _cdp_session(cfg)
    try:
        navigated = False
        if navigate_url:
            sess.navigate(navigate_url)
            time.sleep(max(0.0, settle))
            navigated = True
        sess.click(float(x), float(y))
        return {
            "state": "succeeded", "x": float(x), "y": float(y),
            "navigated": navigated, "clicked_at": _now_iso(), "error": None,
        }
    except _cdp.CDPError as e:
        return {
            "state": "failed", "x": float(x), "y": float(y),
            "navigated": False, "clicked_at": _now_iso(), "error": str(e),
        }
    finally:
        sess.close()


def cdp_type_runtime(
    cfg: Config, text: str, navigate_url: str | None = None,
    settle_seconds: float | None = None,
) -> dict:
    """Type `text` into whatever currently holds keyboard focus on the runtime
    canvas, via CDP Input.insertText (one call, no per-char keycode synthesis).

    Precondition: the caller focused an editable target first (cdp_click on a
    TextBox/SpinBox puts it in a keyboard-ready state — cursor / select-all).
    Guard: if the focused DOM element is BODY/none, nothing editable has focus
    and insertText would silently no-op — returns no_focused_input instead
    (fail-loud contract). The Optix canvas itself (CANVAS,
    or an internal INPUT overlay) counts as focused. Committing the value is a
    SEPARATE step: cdp_key_runtime("Enter"). Returns {state, typed_chars,
    navigated, typed_at}.
    """
    audit(cfg, "cdp_type", text=text)
    from . import _cdp
    settle = cfg.cdp_settle_seconds if settle_seconds is None else settle_seconds
    sess = _cdp_session(cfg)
    try:
        navigated = False
        if navigate_url:
            sess.navigate(navigate_url)
            time.sleep(max(0.0, settle))
            navigated = True
        tag = sess.active_element_tag()
        if tag in ("", "BODY", "HTML"):
            return {
                "state": "failed", "error": "no_focused_input",
                "active_element": tag or None, "navigated": navigated,
                "hint": ("nothing editable has keyboard focus — optix_cdp_click "
                         "the field first (its cursor/selection confirms focus), "
                         "then type"),
            }
        sess.insert_text(text)
        return {
            "state": "succeeded", "typed_chars": len(text),
            "active_element": tag, "navigated": navigated,
            "typed_at": _now_iso(), "error": None,
        }
    except _cdp.CDPError as e:
        return {"state": "failed", "error": str(e), "navigated": False,
                "typed_at": _now_iso()}
    finally:
        sess.close()


def cdp_fill_runtime(
    cfg: Config, x: float, y: float, text: str,
    submit: str | None = "Enter", select_all: bool = True,
    navigate_url: str | None = None, settle_seconds: float | None = None,
) -> dict:
    """One-call field update: click (x, y) -> focus guard -> (select-all) ->
    type -> commit. The composite for the click/type/Enter trio so a single
    tool call updates a TextBox/SpinBox; the primitives remain for stepping,
    Escape-cancel, and screenshot-mid-entry.

    select_all (default True) gives REPLACE semantics on a non-empty TextBox
    (a click places a caret, so a bare type would append). submit=None types
    without committing. The focus guard fails loud (no_focused_input) with the
    per-step report, so a click that landed on a non-editable region names
    itself. Auto-targets the running HMI when navigate_url is omitted (pass
    "" to act on the current tab as-is). Returns {state, steps: {clicked,
    focused_element, typed_chars, committed}, x, y, filled_at}.
    """
    audit(cfg, "cdp_fill", x=x, y=y, text=text)
    from . import _cdp
    if submit and submit not in _cdp.KEY_MAP:
        return {"state": "failed", "error": "invalid_key", "submit": submit,
                "valid_keys": sorted(_cdp.KEY_MAP)}
    settle = cfg.cdp_settle_seconds if settle_seconds is None else settle_seconds
    steps: dict = {"clicked": False, "focused_element": None,
                   "typed_chars": 0, "committed": None}
    sess = _cdp_session(cfg)
    try:
        # Auto-target the runtime like optix_cdp_screenshot does — fill is
        # designed to be callable cold, and a fresh chrome-cdp tab sits on
        # about:blank where a click can never focus a field.
        navigated = _point_screenshot_at_runtime(cfg, sess, navigate_url, settle)
        sess.click(float(x), float(y))
        steps["clicked"] = True
        time.sleep(0.3)  # let the canvas move focus into its input overlay
        tag = sess.active_element_tag()
        steps["focused_element"] = tag or None
        if tag in ("", "BODY", "HTML"):
            return {"state": "failed", "error": "no_focused_input",
                    "steps": steps, "x": float(x), "y": float(y),
                    "navigated": navigated,
                    "hint": ("the click at ({}, {}) did not focus an editable "
                             "field — check coordinates against a fresh "
                             "screenshot".format(x, y))}
        if select_all:
            sess.select_all()
        sess.insert_text(text)
        steps["typed_chars"] = len(text)
        if submit:
            sess.key(submit)
            steps["committed"] = submit
        return {"state": "succeeded", "steps": steps, "x": float(x),
                "y": float(y), "navigated": navigated,
                "filled_at": _now_iso(), "error": None}
    except _cdp.CDPError as e:
        return {"state": "failed", "error": str(e), "steps": steps,
                "x": float(x), "y": float(y), "filled_at": _now_iso()}
    finally:
        sess.close()


def cdp_key_runtime(
    cfg: Config, key: str, navigate_url: str | None = None,
    settle_seconds: float | None = None,
) -> dict:
    """Press one named key on the runtime canvas via CDP Input.dispatchKeyEvent
    (keyDown + keyUp).

    Enter is what COMMITS a TextBox/SpinBox edit (typed values don't stick
    without it); Escape cancels; Tab moves focus. Unknown keys fail loud with
    the valid list (invalid_key). A key press with no pending edit is a safe
    no-op, like a real keyboard. Returns {state, key, navigated, pressed_at}.
    """
    audit(cfg, "cdp_key", key=key)
    from . import _cdp
    if key not in _cdp.KEY_MAP:
        return {"state": "failed", "error": "invalid_key", "key": key,
                "valid_keys": sorted(_cdp.KEY_MAP)}
    settle = cfg.cdp_settle_seconds if settle_seconds is None else settle_seconds
    sess = _cdp_session(cfg)
    try:
        navigated = False
        if navigate_url:
            sess.navigate(navigate_url)
            time.sleep(max(0.0, settle))
            navigated = True
        sess.key(key)
        return {"state": "succeeded", "key": key, "navigated": navigated,
                "pressed_at": _now_iso(), "error": None}
    except _cdp.CDPError as e:
        return {"state": "failed", "error": str(e), "key": key,
                "navigated": False, "pressed_at": _now_iso()}
    finally:
        sess.close()


def cdp_screenshot_runtime(
    cfg: Config, save_path: str | None = None, quality: int = 65,
    navigate_url: str | None = None, settle_seconds: float | None = None,
    fresh: bool = False,
) -> dict:
    """Capture the runtime canvas via CDP Page.captureScreenshot (JPEG).

    Saves server-side when save_path is given (else returns base64).

    Navigation, so the caller never needs to know the runtime URL:
      - navigate_url omitted (None): auto-target the local Optix runtime,
        skipping the reload if the tab is already there (preserving prior
        click/nav state). This is the common "show me the runtime" case.
      - navigate_url given: point the page there first.
      - navigate_url == "": screenshot whatever the tab currently shows.
    After a navigation it waits settle_seconds for the Optix canvas to render
    (capturing mid-navigation fails). Returns {state, path|b64, size_bytes,
    navigated, captured_at}.
    """
    import base64
    from . import _cdp
    settle = cfg.cdp_settle_seconds if settle_seconds is None else settle_seconds
    sess = _cdp_session(cfg)
    try:
        navigated = _point_screenshot_at_runtime(cfg, sess, navigate_url, settle)
        if fresh and not navigated:
            # force a reload so a stale frame can never masquerade as current
            # (the auto-target skips re-navigation when already on the runtime)
            sess.reload()
            time.sleep(max(0.0, settle))
            navigated = True
        jpeg = sess.screenshot_jpeg(quality=quality)
        result: dict[str, Any] = {
            "state": "succeeded", "path": None, "b64": None,
            "size_bytes": len(jpeg), "navigated": navigated,
            "captured_at": _now_iso(),
        }
        if save_path:
            out = Path(save_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(jpeg)
            result["path"] = str(out)
        else:
            result["b64"] = base64.b64encode(jpeg).decode("ascii")
        return result
    except _cdp.CDPError as e:
        return {
            "state": "failed", "path": None, "b64": None, "size_bytes": 0,
            "navigated": False, "captured_at": _now_iso(), "error": str(e),
        }
    finally:
        sess.close()


def cdp_ocr_runtime(
    cfg: Config, navigate_url: str | None = None,
    settle_seconds: float | None = None, *, psm: int = 6,
    runner: Runner = _DEFAULT_RUNNER,
) -> dict:
    """OCR the runtime canvas via tesseract — an OPT-IN, headless read-back fallback.

    Tesseract is resolved from PATH first, then the standard Windows install
    dirs (winget/UB-Mannheim installs don't touch PATH — found live 2026-07-17).

    The default verify path is a vision model reading optix_cdp_screenshot; this is
    for the case that path can't run (a cron/headless caller with no vision, or the
    blank-render edge we hit on the VM where a human still needs *some* text signal).
    It captures the runtime JPEG through the same tested screenshot path, then runs
    the `tesseract` binary on it.

    Returns {state, text, size_bytes, navigated, captured_at}. If tesseract is not on
    PATH, returns state='failed', error='tesseract_not_installed' with an install
    hint rather than raising — it is optional infrastructure. Text-only: NOT a
    substitute for vision on color/layout checks.
    """
    import shutil
    import tempfile
    def _find_tesseract() -> str | None:
        hit = shutil.which("tesseract")
        if hit:
            return hit
        for cand in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        ):
            if os.path.isfile(cand):
                return cand
        return None

    tesseract = _find_tesseract()
    if tesseract is None:
        return {
            "state": "failed", "text": None, "error": "tesseract_not_installed",
            "hint": (
                "Install Tesseract-OCR (Windows: `winget install "
                "UB-Mannheim.TesseractOCR`) — PATH is optional; the service also "
                "probes the standard install dirs. This is an OPT-IN fallback — "
                "the default verify path is a vision model on "
                "optix_cdp_screenshot, which needs no OCR."
            ),
        }
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "runtime.jpg"
        shot = cdp_screenshot_runtime(
            cfg, save_path=str(img), navigate_url=navigate_url,
            settle_seconds=settle_seconds,
        )
        if shot.get("state") != "succeeded":
            return {
                "state": "failed", "text": None,
                "error": shot.get("error", "screenshot_failed"),
                "navigated": shot.get("navigated", False),
            }
        proc = runner.run(
            [tesseract, str(img), "stdout", "--psm", str(int(psm))], timeout=30)
        if proc.returncode != 0:
            return {
                "state": "failed", "text": None,
                "error": (proc.stderr or "tesseract failed").strip()[:400],
                "navigated": shot.get("navigated", False),
            }
        return {
            "state": "succeeded", "text": (proc.stdout or "").strip(),
            "size_bytes": shot.get("size_bytes", 0),
            "navigated": shot.get("navigated", False), "captured_at": _now_iso(),
        }


# ---- deploy + verify --------------------------------------------------

def _git(runner: Runner, project_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return runner.run(["git", "-C", str(project_dir), *args])


# `git log` formatter: \x1f between fields, one record per line. Both
# control bytes are forbidden in commit messages by `git commit-tree` so
# they round-trip safely.
_GIT_LOG_FIELD_SEP = "\x1f"
_GIT_LOG_FORMAT = (
    f"%H{_GIT_LOG_FIELD_SEP}%an{_GIT_LOG_FIELD_SEP}%aI{_GIT_LOG_FIELD_SEP}%s"
)


def git_log(
    cfg: Config, project: str, limit: int = 10, runner: Runner = _DEFAULT_RUNNER
) -> list[dict]:
    """Return the last `limit` commits on the project's HEAD branch.

    Each entry: `{sha, author, date, message}`. Empty list if the project
    is not a git repo (no `.git`) — same shape as a fresh clone with no
    history. Empty list if `git log` fails (e.g. shallow / corrupted).

    The `limit` is clamped to [1, 100] — the HMI only renders a small
    window, and an unbounded read against a deep history would block the
    HTTP path.
    """
    project_dir = resolve_project(cfg, project)
    limit = max(1, min(int(limit), 100))

    proc = _git(
        runner, project_dir,
        "log", f"-n{limit}", f"--pretty=format:{_GIT_LOG_FORMAT}",
    )
    if proc.returncode != 0:
        return []

    out: list[dict] = []
    for raw_line in (proc.stdout or "").splitlines():
        if not raw_line:
            continue
        parts = raw_line.split(_GIT_LOG_FIELD_SEP, 3)
        if len(parts) != 4:
            continue
        out.append({
            "sha": parts[0],
            "author": parts[1],
            "date": parts[2],
            "message": parts[3],
        })
    return out


# Deploy outcome buffer. JSONL, one entry per deploy
# completion, capped at MAX_ENTRIES lines OR MAX_BYTES bytes — whichever
# bound trips first. Writes happen on lock release inside `deploy()`.
DEPLOY_BUFFER_FILENAME = "deploys.jsonl"
DEPLOY_BUFFER_MAX_ENTRIES = 100
DEPLOY_BUFFER_MAX_BYTES = 1024 * 1024  # 1 MB


def _deploy_buffer_path(cfg: Config) -> Path:
    return cfg.state_dir / DEPLOY_BUFFER_FILENAME


def _trim_deploy_buffer(path: Path) -> None:
    """Enforce the size + entry caps. Rewrites the file atomically if
    trimming is needed; no-op otherwise."""
    if not path.exists():
        return
    raw = path.read_bytes()
    line_count = raw.count(b"\n")
    if len(raw) <= DEPLOY_BUFFER_MAX_BYTES and line_count <= DEPLOY_BUFFER_MAX_ENTRIES:
        return

    lines = [line for line in raw.splitlines(keepends=True) if line.strip()]
    lines = lines[-DEPLOY_BUFFER_MAX_ENTRIES:]
    while lines and sum(len(line) for line in lines) > DEPLOY_BUFFER_MAX_BYTES:
        lines.pop(0)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(b"".join(lines))
    tmp.replace(path)


def record_deploy_outcome(cfg: Config, project: str, result: dict) -> None:
    """Append a deploy outcome to the circular buffer.

    Stored shape mirrors the deploy result envelope plus the project
    name and a fast-lookup `state`. Best-effort: any write/trim failure
    is swallowed so a buffer-side glitch never fails the deploy itself.
    """
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    path = _deploy_buffer_path(cfg)
    entry = {
        "project": project,
        "state": result.get("state"),
        "studio_exit": result.get("studio_exit"),
        "started_at": result.get("started_at"),
        "completed_at": result.get("completed_at"),
        "git_sha": result.get("git_sha"),
        "git_state": result.get("git_state"),
        "runtime_reachable": result.get("runtime_reachable"),
        "files_written": result.get("files_written") or [],
        "verification": result.get("verification"),
        "stderr_tail": result.get("stderr_tail") or "",
        "stdout_tail": result.get("stdout_tail") or "",
    }
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        _trim_deploy_buffer(path)
    except OSError:
        # Buffer is best-effort: a full disk should not break the deploy
        # contract. The deploy result the caller already received is the
        # source of truth; the buffer is HMI sugar.
        return


def last_deploy_tail(cfg: Config, project: str | None = None) -> dict | None:
    """Return the most recent deploy outcome, or None if the buffer is
    missing/empty. Reads the whole file (capped at 1 MB) and
    returns the last well-formed JSONL entry.

    When `project` is set, returns the most recent entry whose `project`
    field matches; entries that don't parse or lack the field are
    skipped. Returns None when no matching entry exists."""
    path = _deploy_buffer_path(cfg)
    if not path.exists():
        return None
    raw = path.read_bytes()
    if not raw.strip():
        return None
    for line in reversed(raw.splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            entry = json.loads(text.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if project is not None and entry.get("project") != project:
            continue
        return entry
    return None


def _git_commit_if_changed(
    runner: Runner, project_dir: Path, message: str
) -> tuple[str | None, str]:
    """Commit any staged/working changes.

    Returns (sha, state) where:
      sha   : HEAD sha after the commit (or current HEAD if nothing to
              commit). None when project_dir is not a git repo or HEAD
              cannot be resolved.
      state : "not_a_repo" | "clean" | "committed". Surfaced into the
              deploy result envelope (H) so a null git_sha has a reason
              attached instead of looking like an error.
    """
    check = _git(runner, project_dir, "rev-parse", "--show-toplevel")
    if check.returncode != 0:
        return (None, "not_a_repo")
    _git(runner, project_dir, "add", "-A")
    status = _git(runner, project_dir, "status", "--porcelain")
    if (status.stdout or "").strip():
        _git(runner, project_dir, "commit", "-m", message)
        state = "committed"
    else:
        state = "clean"
    sha = _git(runner, project_dir, "rev-parse", "HEAD")
    return (sha.stdout.strip() if sha.returncode == 0 else None, state)


def _project_tree_max_mtime(project_dir: Path) -> float:
    latest = project_dir.stat().st_mtime
    for p in project_dir.rglob("*"):
        try:
            if p.is_file():
                m = p.stat().st_mtime
                if m > latest:
                    latest = m
        except OSError:
            continue
    return latest


def verify_export_mtime(cfg: Config, runtime_project_dir: Path, deploy_started_at: float) -> dict:
    """Verify the swapped runtime tree's mtime advanced past deploy-start.

    Used when run_after_deploy=False, or when the runtime probe is disabled.
    Polls the runtime tree (NOT the source project tree) — the new tree
    just landed there via os.replace and its mtimes reflect the swap.
    """
    deadline = deploy_started_at + cfg.verify_timeout_seconds
    while time.time() < deadline:
        try:
            latest = _project_tree_max_mtime(runtime_project_dir)
        except OSError:
            latest = 0.0
        if latest > deploy_started_at:
            return {
                "method": "export_mtime",
                "confirmed_at": _now_iso(latest),
                "timeout_seconds": cfg.verify_timeout_seconds,
            }
        time.sleep(cfg.verify_poll_seconds)
    return {
        "method": "export_mtime",
        "confirmed_at": None,
        "timeout_seconds": cfg.verify_timeout_seconds,
    }


def verify_runtime_probe(cfg: Config, _runtime_project_dir: Path, deploy_started_at: float) -> dict:
    """Verify the runtime port comes back up after a bounce.

    Polls cfg.runtime_test_port for tcp_reachable. The runtime was stopped
    before the swap and (re)started after, so a successful connect is the
    end-to-end signal the deploy actually landed and the runtime is happy.
    """
    deadline = deploy_started_at + cfg.verify_timeout_seconds
    while time.time() < deadline:
        if _tcp_probe("127.0.0.1", cfg.runtime_test_port, timeout=0.5):
            return {
                "method": "runtime_probe",
                "confirmed_at": _now_iso(),
                "timeout_seconds": cfg.verify_timeout_seconds,
            }
        time.sleep(cfg.verify_poll_seconds)
    return {
        "method": "runtime_probe",
        "confirmed_at": None,
        "timeout_seconds": cfg.verify_timeout_seconds,
    }


@dataclass
class DeployRequest:
    edits: list[dict] = field(default_factory=list)  # [{"path": str, "content": str}, ...]
    commit_message: str = "Automated edit"
    run_after_deploy: bool = True


def deploy_preflight(
    cfg: Config,
    project: str,
    runner: Runner = _DEFAULT_RUNNER,
) -> dict:
    """Run every deploy precondition without launching Studio.

    Returns:
      {
        ready: bool,
        blockers: [{code, message, hint?}, ...],
        warnings: [{code, message, hint?}, ...],
        checks: { ... per-check details ... },
      }

    Blockers will fail the deploy; warnings won't but indicate degraded
    operation. Call this before optix_deploy when first wiring up a box
    or after a box reboot to catch missing config without consuming a
    full Studio process slot.
    """
    blockers: list[dict] = []
    warnings: list[dict] = []
    checks: dict = {}

    # 1. Project resolves
    project_dir: Path | None = None
    try:
        project_dir = resolve_project(cfg, project)
        optix_files = sorted(project_dir.glob("*.optix"))
        if not optix_files:
            blockers.append({
                "code": "project_no_optix_file",
                "message": f"no .optix file in project: {project}",
                "hint": "the directory exists but lacks a .optix manifest",
            })
            checks["project"] = {"resolved": True, "optix_file": None}
        else:
            checks["project"] = {"resolved": True, "optix_file": optix_files[0].name}
    except CoreError as e:
        blockers.append({"code": e.code, "message": str(e), "hint": e.hint})
        checks["project"] = {"resolved": False}

    # 2. Studio binary present
    checks["studio_exe"] = {
        "path": str(cfg.studio_exe),
        "present": cfg.studio_exe.is_file(),
    }
    if not cfg.studio_exe.is_file():
        blockers.append({
            "code": StudioMissing.code,
            "message": f"studio_exe missing: {cfg.studio_exe}",
            "hint": StudioMissing.hint,
        })

    # 3. Runtime dir present (export-based deploy target)
    checks["runtime_dir"] = {
        "path": str(cfg.runtime_dir),
        "exists": cfg.runtime_dir.is_dir() if cfg.runtime_dir else False,
    }
    if cfg.runtime_dir is None:
        blockers.append({
            "code": RuntimeDirNotConfigured.code,
            "message": "OPTIX_RUNTIME_DIR not configured",
            "hint": RuntimeDirNotConfigured.hint,
        })

    # 4. Interactive session (Windows only)
    interactive = _is_interactive_session()
    checks["interactive_session"] = interactive
    if interactive is False:
        blockers.append({
            "code": "non_interactive_session",
            "message": "service is not in an interactive logon session",
            "hint": (
                "Studio will crash with 0xC0000005 during project open due to DPAPI binding. "
                "See docs/troubleshooting.md."
            ),
        })

    # 5. Lock state — held? stale-recoverable? free?
    lock_path = cfg.state_dir / "deploy.lock"
    if lock_path.exists():
        try:
            import json as _json
            blob = _json.loads(lock_path.read_text(encoding="utf-8"))
            checks["lock"] = {"held": True, "state": blob}
            # Stale (dead PID or > stale_seconds) won't block — DeployLock
            # will recover. A live, fresh PID will block.
            from .deploy_lock import _pid_alive
            holder_alive = _pid_alive(int(blob.get("pid", -1)))
            if holder_alive:
                blockers.append({
                    "code": "deploy_lock_held",
                    "message": f"deploy lock held by live pid {blob.get('pid')}",
                    "hint": "wait for the in-flight deploy to finish",
                })
        except (OSError, ValueError):
            checks["lock"] = {"held": True, "state": "corrupt"}
            warnings.append({
                "code": "deploy_lock_corrupt",
                "message": "lock file present but unreadable; will be cleared on next acquire",
            })
    else:
        checks["lock"] = {"held": False}

    # 6. Git status — informational only
    if project_dir is not None:
        try:
            r = runner.run(
                ["git", "-C", str(project_dir), "rev-parse", "--show-toplevel"],
                timeout=5,
            )
            is_repo = r.returncode == 0
            checks["git"] = {"is_repo": is_repo}
            if is_repo:
                r = runner.run(
                    ["git", "-C", str(project_dir), "status", "--porcelain"],
                    timeout=5,
                )
                dirty = bool(r.stdout.strip()) if r.returncode == 0 else None
                checks["git"]["dirty"] = dirty
                if dirty:
                    warnings.append({
                        "code": "git_dirty",
                        "message": "project has uncommitted changes",
                        "hint": "the deploy will commit them with the supplied commit_message",
                    })
        except (FileNotFoundError, OSError):
            checks["git"] = {"is_repo": None}

    # 7. Runtime port — TCP probe (informational; absence is normal pre-bounce)
    runtime_port = cfg.runtime_test_port
    reachable = _tcp_probe("127.0.0.1", runtime_port, timeout=1.0)
    checks["runtime"] = {
        "port": runtime_port,
        "tcp_reachable": reachable,
    }
    # No warning emitted — a stopped runtime pre-deploy is the normal case
    # for the export-based path (the deploy bounces it).

    # 8. Studio / editor processes — corruption guard (blanket rule for
    # Studio; cmdline attribution for VS / VS Code). Detection rationale:
    gstate = studio_guard.studio_state()
    if gstate.get("error"):
        checks["studio_guard"] = {"error": gstate["error"]}
        warnings.append({
            "code": "studio_guard_unavailable",
            "message": f"process enumeration failed: {gstate['error']}",
            "hint": (
                "the guard cannot rule Studio out; verify by eye that "
                "FactoryTalk Optix Studio is closed before deploying"
            ),
        })
    else:
        checks["studio_guard"] = {
            "studio_running": gstate["studio"]["running"],
            "studio_pids": gstate["studio"]["pids"],
            "editor_procs": [
                {"pid": e["pid"], "name": e["name"]} for e in gstate["editors"]
            ],
        }
        if gstate["studio"]["running"]:
            pids = ", ".join(str(p) for p in gstate["studio"]["pids"])
            blockers.append({
                "code": StudioOpen.code,
                "message": f"FTOptixStudio.exe is running (pid {pids})",
                "hint": StudioOpen.hint,
            })
        else:
            hits = (
                studio_guard.attributed_editors(gstate, project_dir)
                if project_dir is not None
                else []
            )
            if hits:
                ed = hits[0]
                blockers.append({
                    "code": EditorProjectOpen.code,
                    "message": (
                        f"{ed['name']} (pid {ed['pid']}) has this project open"
                    ),
                    "hint": EditorProjectOpen.hint,
                })
            elif gstate["editors"]:
                names = ", ".join(sorted({e["name"] for e in gstate["editors"]}))
                warnings.append({
                    "code": "editor_processes_detected",
                    "message": f"editor process(es) running: {names}",
                    "hint": (
                        "not attributed to this project; if you are editing "
                        "this project's NetSolution, close it before deploying"
                    ),
                })

    return {
        "ready": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
    }


# ---- runtime control (export-based deploy) ---------------------------

class RuntimeController:
    """Stop/start hook for the FTOptixRuntime process attached to a runtime
    tree. Tests inject a fake; the production impl shells out via
    cfg.runtime_launcher (a scheduled-task name or a script path).

    Stopping uses Get-CimInstance to find FTOptixRuntime processes whose
    command line matches the runtime project dir, then taskkill /pid /F.
    Starting invokes the launcher (Start-ScheduledTask <name> or a .ps1).
    """

    def __init__(self, runner: Runner = _DEFAULT_RUNNER) -> None:
        self.runner = runner

    def stop(self, cfg: Config, runtime_project_dir: Path) -> None:
        if os.name != "nt":
            return
        # Best-effort: find FTOptixRuntime processes whose CommandLine
        # references the runtime project dir, then kill them. WMI's
        # CommandLine match is the safest way to scope to *this* project's
        # runtime instance without touching others.
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='FTOptixRuntime.exe'\" | "
            f"Where-Object {{ $_.CommandLine -match [regex]::Escape('{runtime_project_dir}') }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        self.runner.run(["powershell", "-NoProfile", "-Command", ps], timeout=30)
        time.sleep(cfg.runtime_stop_grace_seconds)

    def start(self, cfg: Config, runtime_project_dir: Path) -> None:
        """Spawn FTOptixRuntime for the swapped runtime tree.

        Two paths:
          - configured launcher: cfg.runtime_launcher names a scheduled
            task or .ps1. Original v0.1 path; used when an installer set
            up a dedicated runtime-launcher task at provisioning time.
          - direct spawn (fallback, v0.2.x): no launcher configured.
            Spawn FTOptixRuntime.exe directly via _default_runtime_spawn,
            the same path optix_runtime_start uses. Means a Joe-laptop
            install doesn't need to also create a runtime-launcher task
            for deploys to bounce cleanly.
        """
        if cfg.runtime_launcher:
            launcher = cfg.runtime_launcher
            if launcher.lower().endswith(".ps1"):
                cmd = ["powershell", "-NoProfile", "-File", launcher,
                       "-RuntimeProjectDir", str(runtime_project_dir)]
            else:
                cmd = ["powershell", "-NoProfile", "-Command",
                       f"Start-ScheduledTask -TaskName '{launcher}'"]
            self.runner.run(cmd, timeout=30)
            return
        exe = runtime_project_dir / "FTOptixApplication" / "FTOptixRuntime.exe"
        if not exe.is_file():
            return
        _default_runtime_spawn(exe)


def _atomic_swap(staging_dir: Path, target_dir: Path) -> None:
    """Replace target_dir with staging_dir contents.

    Sequence: rename target -> target.bak (if present), rename staging ->
    target, drop target.bak. The intermediate .bak preserves the prior
    runtime tree across the swap, so an interrupted swap leaves a
    recoverable state. After both renames succeed, .bak is dropped.
    """
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    backup = target_dir.with_name(target_dir.name + ".bak")

    if backup.exists():
        # Stale backup from a prior interrupted swap. Drop it.
        import shutil
        shutil.rmtree(backup, ignore_errors=True)

    if target_dir.exists():
        try:
            target_dir.rename(backup)
        except OSError as e:
            raise TreeSwapFailed(
                f"could not move existing runtime tree aside: {e}"
            ) from e

    try:
        staging_dir.rename(target_dir)
    except OSError as e:
        # Rollback: try to restore the backup.
        if backup.exists() and not target_dir.exists():
            try:
                backup.rename(target_dir)
            except OSError:
                pass
        raise TreeSwapFailed(f"could not move staging tree into place: {e}") from e

    if backup.exists():
        import shutil
        shutil.rmtree(backup, ignore_errors=True)

    # Bump target_dir mtime to the current wall-clock time so callers using
    # verify_export_mtime can rely on `latest > deploy_started_at` even when
    # the kernel's CLOCK_REALTIME_COARSE (the source for filesystem mtimes)
    # lags time.time() by a jiffy.
    try:
        now = time.time()
        os.utime(target_dir, (now, now))
    except OSError:
        pass


def deploy(
    cfg: Config,
    project: str,
    req: DeployRequest,
    runner: Runner = _DEFAULT_RUNNER,
    lock: DeployLock | None = None,
    runtime: RuntimeController | None = None,
    verify: Callable[[Config, Path, float], dict] | None = None,
) -> dict:
    """Edit -> git-commit -> Studio export -> atomic tree swap -> runtime
    bounce -> verify.

    Returns the deploy-contract result schema (state ∈ {succeeded, failed}).
    """
    if not cfg.studio_exe.is_file():
        raise StudioMissing(f"studio_exe missing: {cfg.studio_exe}")
    if cfg.runtime_dir is None:
        raise RuntimeDirNotConfigured("OPTIX_RUNTIME_DIR not configured")

    project_dir = resolve_project(cfg, project)
    optix_files = sorted(project_dir.glob("*.optix"))
    if not optix_files:
        raise ProjectNotFound(f"no .optix file in project: {project}")
    optix_file = optix_files[0]

    # Corruption guard, check #1 of 2 (cheap, cached): refuse before any
    # state change while Studio / an attributed editor holds the project.
    require_editors_closed(project_dir)

    if lock is None:
        lock = DeployLock(
            cfg.state_dir / "deploy.lock",
            caller=f"optix_deploy({project})",
        )
    if runtime is None:
        runtime = RuntimeController(runner=runner)
    if verify is None:
        verify = verify_runtime_probe if req.run_after_deploy else verify_export_mtime

    started_at = time.time()
    started_iso = _now_iso(started_at)

    staging_root = cfg.state_dir / "export-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_dir = staging_root / project
    if staging_dir.exists():
        import shutil
        shutil.rmtree(staging_dir, ignore_errors=True)

    runtime_project_dir = cfg.runtime_dir / project

    # M: post-lock work runs inside try/finally so exception-path deploys
    # also write to the outcome buffer (the buffer is the source of truth
    # for HMI/operator tail; missing entries hide real failures).
    result: dict | None = None
    git_sha: str | None = None
    git_state: str = "not_a_repo"  # H: surfaced when commit step is skipped/fails
    try:
        with lock.acquire():
            # Corruption guard, check #2 of 2 (forced, uncached): Studio can
            # open between the entry check and this point (TOCTOU). This is
            # the last gate before bytes hit the project tree; a refusal here
            # is recorded in the outcome buffer by the finally block.
            require_editors_closed(project_dir, force=True)

            # Two-phase edit application (docs/architecture.md, Edit modes): resolve every
            # edit to its post-edit bytes first — any anchor mismatch or
            # invalid shape refuses the WHOLE batch with zero files touched —
            # then write. Duplicate paths are refused because a later
            # anchored edit would resolve against pre-batch disk state and
            # silently drop the earlier edit on write.
            staged: list[tuple[Path, bytes]] = []
            edit_summary: list[dict] = []
            seen_paths: set[str] = set()
            for edit in req.edits:
                rel = edit.get("path")
                if not rel:
                    raise InvalidEdit("edit missing 'path'")
                if rel in seen_paths:
                    raise InvalidEdit(
                        f"multiple edits target {rel}; combine them into one edit"
                    )
                seen_paths.add(rel)
                target = resolve_subpath(cfg, project, rel)
                new_bytes, summary = _resolve_edit_content(target, edit, rel)
                staged.append((target, new_bytes))
                edit_summary.append(summary)

            written: list[str] = []
            for (target, new_bytes), edit in zip(staged, req.edits, strict=True):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(new_bytes)
                written.append(edit["path"])

            git_sha, git_state = _git_commit_if_changed(
                runner, project_dir, req.commit_message
            )

            cmd = [
                str(cfg.studio_exe),
                "export",
                str(optix_file),
                "--platform=Win32_x64",
                f"--location={staging_dir}",
            ]
            proc = runner.run(cmd, timeout=cfg.deploy_timeout_seconds)
            completed_at = time.time()
            completed_iso = _now_iso(completed_at)

            base_result = {
                "studio_exit": proc.returncode,
                "started_at": started_iso,
                "completed_at": completed_iso,
                "git_sha": git_sha,
                "git_state": git_state,
                "files_written": written,
                "edit_summary": edit_summary,
                "stdout_tail": (proc.stdout or "")[-2000:],
                "stderr_tail": (proc.stderr or "")[-2000:],
                "runtime_reachable": None,  # I: set below when a probe ran
            }

            if proc.returncode != 0:
                result = {
                    **base_result,
                    "state": "failed",
                    "verification": {
                        "method": None,
                        "confirmed_at": None,
                        "timeout_seconds": cfg.verify_timeout_seconds,
                    },
                }
                return result

            # Bounce the runtime so the swap can complete without a file lock.
            if req.run_after_deploy:
                runtime.stop(cfg, runtime_project_dir)

            try:
                _atomic_swap(staging_dir, runtime_project_dir)
            except TreeSwapFailed as e:
                result = {
                    **base_result,
                    "state": "failed",
                    "verification": {
                        "method": None,
                        "confirmed_at": None,
                        "timeout_seconds": cfg.verify_timeout_seconds,
                    },
                    "stderr_tail": (base_result["stderr_tail"] + f"\n{e}")[-2000:],
                }
                return result

            if req.run_after_deploy:
                runtime.start(cfg, runtime_project_dir)

            verification = verify(cfg, runtime_project_dir, started_at)
            confirmed = verification.get("confirmed_at") is not None

            # I: graceful verify gradation. The runtime_probe path is
            # treated as advisory — swap-succeeded + runtime-unreachable
            # is "succeeded with runtime_offline marker", not "failed".
            # The new YAML/CS may crash the runtime on load (operator
            # checks runtime logs, doesn't re-deploy) or the runtime may
            # be restarting; either way the deploy itself landed. The
            # export_mtime path stays binary: confirmed_at=None there
            # means the swap didn't visibly take effect on disk, which
            # IS a deploy failure.
            if verification.get("method") == "runtime_probe":
                state = "succeeded"
                runtime_reachable: bool | None = confirmed
            elif confirmed:
                state = "succeeded"
                runtime_reachable = None  # export_mtime: no probe ran
            else:
                state = "failed"
                runtime_reachable = None

            result = {
                **base_result,
                "state": state,
                "runtime_reachable": runtime_reachable,
                "verification": verification,
            }
            return result
    except Exception as exc:
        if result is None:
            result = {
                "studio_exit": -1,
                "started_at": started_iso,
                "completed_at": _now_iso(time.time()),
                "git_sha": git_sha,
                "git_state": git_state,
                "files_written": [],
                "stdout_tail": "",
                "stderr_tail": f"{type(exc).__name__}: {exc}"[-2000:],
                "state": "failed",
                "runtime_reachable": None,
                "verification": {
                    "method": None,
                    "confirmed_at": None,
                    "timeout_seconds": cfg.verify_timeout_seconds,
                },
            }
        raise
    finally:
        if result is not None:
            record_deploy_outcome(cfg, project, result)
