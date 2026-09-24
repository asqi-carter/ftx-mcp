"""Pure functions for ftx-mcp.

Each function is deterministic given (Config, args) and a Runner for
subprocess execution. The HTTP and MCP surfaces thin-wrap these.

Domain errors raised here (CoreError subclasses) carry an http_status
hint so the FastAPI layer can translate cleanly. The MCP layer surfaces
them as tool errors.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

import psutil

from . import studio_guard, studio_uia
from .deploy_lock import DeployLock

_log = logging.getLogger(__name__)

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


class BridgeLoading(CoreError):
    http_status = 503
    code = "bridge_loading"
    hint = ("The design-time bridge is mid-rebuild or still loading its model — "
            "the response was truncated (IncompleteRead) or the connection was "
            "dropped (RemoteDisconnected). This is transient: retry in a few seconds.")


class BridgeWriteFailed(CoreError):
    http_status = 502
    code = "bridge_write_failed"
    hint = "The bridge reached the live model but the authoring call failed (see message). Common causes: bad node path, unknown UI type, or a value that can't coerce to the property type."


class InvalidBatchId(CoreError):
    http_status = 400
    code = "invalid_batch_id"
    hint = "batch_id is the opaque id a prior optix_bridge_edit call returned (letters, digits, '_' or '-')."


class DeployConfigError(CoreError):
    http_status = 400
    code = "deploy_not_configured"
    hint = "UpdateSvc deploy needs OPTIX_DEPLOY_USERNAME (and usually OPTIX_DEPLOY_IP / OPTIX_DEPLOY_THUMBPRINT) set, plus OPTIX_STUDIO_DEPLOYMENT_PASSWORD in the environment. Run optix_status(action='doctor') for the full checklist."


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


class InvalidNetLogicClass(CoreError):
    http_status = 400
    code = "netlogic_class_invalid"
    hint = (
        "cls must be a bare C# class name (letters, digits, underscores; "
        "no extension, no path separators, no wildcards, no leading digit)"
    )


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
            # creationflags stops this taskkill from flashing a
            # console window, whatever the parent's console state (see
            # _run_subprocess_with_tree_kill below for the full explanation —
            # same root cause, same fix).
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, check=False, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
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
    if os.name == "nt":
        # every child spawned through here (taskkill, tasklist,
        # netstat, powershell, ...) is a console-subsystem tool. Under a
        # console parent (python.exe) it just attaches to the already-open
        # console, invisibly — and that is the normal case, since the task
        # runs python.exe (--hide-console merely HIDES that console, so
        # children still inherit it and draw nothing). Under a windowless
        # parent with no console at all, Windows would instead have nowhere
        # to attach a child and pop a brand-new console that flashes
        # on screen for the call's duration — visible as a repeating
        # cmd/PowerShell-window flash from anything that polls status on an
        # interval (e.g. the /ui dashboard hitting /health or optix_doctor
        # repeatedly). Suppress unconditionally; a caller that genuinely
        # needs a visible console can still override by passing its own
        # creationflags.
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)

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

    def run_powershell(
        self, ps: str, timeout: float, **kwargs: Any
    ) -> subprocess.CompletedProcess:
        """Run a PowerShell script string via `powershell -NoProfile -Command`.

        Shared shape for the many `-Command` call sites: `ps` stays the LAST
        argv element (tests read `runner.calls[...][0][-1]`) and `"powershell"`
        stays element 0 — do not reorder or add argv elements. Only expresses
        the `-Command` form; `-File` launches build their own argv."""
        return self.run(
            ["powershell", "-NoProfile", "-Command", ps], timeout=timeout, **kwargs)


_DEFAULT_RUNNER = Runner()


# ---- config -----------------------------------------------------------

_STUDIO_INSTALL_ROOT = Path(
    r"C:\Program Files\Rockwell Automation\FactoryTalk Optix"
)


def _default_studio_exe() -> Path:
    """Highest-version FTOptixStudio.exe under the default install root.

    Mirrors setup.ps1 step 2's probe so the service default can never go
    stale across Studio updates: a pinned default (formerly 1.7.1.46)
    reported studio_exe_exists=false as soon as a newer Studio was the only
    one installed, because setup.ps1's discovered path only lived in the
    install shell's process env — the scheduled task never saw it.
    FTOPTIX_STUDIO_EXE (any scope) still wins; from_env checks it before
    calling this. Falls back to the historical pinned path when nothing is
    found so /health has a concrete path to report red.
    """
    try:
        candidates = list(_STUDIO_INSTALL_ROOT.glob("Studio */FTOptixStudio.exe"))
    except OSError:
        candidates = []

    def _version_key(exe: Path) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in exe.parent.name.split(" ", 1)[1].split("."))
        except (IndexError, ValueError):
            return (0,)

    if candidates:
        return max(candidates, key=_version_key)
    return _STUDIO_INSTALL_ROOT / "Studio 1.7.1.46" / "FTOptixStudio.exe"


# Recognized values for Config.studio_guard_mode / OPTIX_STUDIO_GUARD_MODE.
# Anything else read from the environment collapses to "blanket".
_STUDIO_GUARD_MODES = frozenset({"blanket", "attributed"})

# Single authoritative Python-side defaults for the bridge port window.
# PortRangeSize in StudioMCPBridge.cs is the single authoritative cap for the
# number of concurrent bridge instances; _BRIDGE_PORT_BASE/_BRIDGE_PORT_RANGE
# must stay in sync with C# BasePort/PortRangeSize in StudioMCPBridge.cs.
# Read by Config.from_env (env vars OPTIX_BRIDGE_PORT_BASE/_RANGE override)
# and used as fallback defaults for the port-range scan in studio_arm.py.
_BRIDGE_PORT_BASE: int = 8768
_BRIDGE_PORT_RANGE: int = 4  # ports 8768..8771; matches PortRangeSize in StudioMCPBridge.cs

# Default CDP emulated-device viewport (Config.cdp_viewport_width/height).
# chrome-cdp launches with --window-size=800,600 (bootstrap/install-chrome-cdp.ps1)
# and the Optix WebPresentationEngine renders to FILL whatever size it's given —
# measured live: cssContentSize == the 800x600 window, but cssVisualViewport /
# cssLayoutViewport == 769x434 (scrollbar/chrome overhead), so a screenshot at
# the raw window size clips the bottom/right of the HMI. 1280x720 is larger
# than the content the HMI needs at 1:1, so the override both un-clips AND
# renders sharper (the canvas fills the given size, it doesn't just reveal more
# of a fixed-size render).
_CDP_VIEWPORT_DEFAULT: tuple[int, int] = (1280, 720)
_CDP_SCALE_DEFAULT: float = 1.0


def _parse_cdp_viewport(raw: str | None) -> tuple[int, int]:
    """Parse OPTIX_CDP_VIEWPORT ("WIDTHxHEIGHT", e.g. "1280x720") into a
    (width, height) int pair. Defensive by design: a missing var, wrong
    shape ("1280"), non-numeric ("ax b"), or non-positive value falls back
    to _CDP_VIEWPORT_DEFAULT rather than raising — a typo'd env var must
    degrade to "screenshots look like today", never crash startup."""
    if raw:
        parts = raw.strip().lower().split("x")
        if len(parts) == 2:
            try:
                w, h = int(parts[0]), int(parts[1])
            except ValueError:
                w = h = 0
            if w > 0 and h > 0:
                return w, h
    return _CDP_VIEWPORT_DEFAULT


def _parse_cdp_scale(raw: str | None) -> float:
    """Parse OPTIX_CDP_SCALE into a positive deviceScaleFactor float.
    Defensive: missing/non-numeric/non-positive falls back to
    _CDP_SCALE_DEFAULT (1.0) rather than raising."""
    if raw:
        try:
            scale = float(raw)
        except ValueError:
            scale = 0.0
        if scale > 0:
            return scale
    return _CDP_SCALE_DEFAULT


@dataclass(frozen=True)
class Config:
    projects_root: Path
    studio_exe: Path
    state_dir: Path
    runtime_dir: Path | None = None
    runtime_launcher: str | None = None
    runtime_test_port: int = 8081
    # External-runtime attach (U19). Default "" = OFF/legacy: the service owns
    # the runtime (F5 emulator / export-deploy) and every liveness probe targets
    # loopback on runtime_test_port. Set OPTIX_RUNTIME_URL to a full
    # scheme://host:port (e.g. "https://10.0.0.5:8443/") to ATTACH to an
    # already-running external WebPresentationEngine instead: CDP navigation +
    # all probes retarget that host/port (chrome-cdp tolerates the self-signed
    # runtime cert via --ignore-certificate-errors), and the two runtime-
    # management actions (run_emulator F5, runtime_start) plus
    # bridge_ensure_web_engine flip to "external — not managed here". Read
    # via runtime_base_url / runtime_probe_host / runtime_probe_port / attach_mode.
    runtime_url: str = ""
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
    # multi-instance bridge support (v1.0.7). StudioMCPBridge.cs no
    # longer exclusively owns :8768 - each Studio instance self-binds the first
    # free port in [bridge_port_base, bridge_port_base + bridge_port_range), so
    # up to `bridge_port_range` projects can have an armed bridge AT THE SAME
    # TIME. `bridge_url` stays as the LEGACY single-bridge override: when
    # OPTIX_BRIDGE_URL is explicitly set (see from_env), it pins the service to
    # that one URL and disables range-scanning entirely - the documented escape
    # hatch for a rebound bridge port stays intact. Otherwise `bridge_url` is
    # unused at rest; every bridge call resolves a project-specific URL from the
    # scanned range (see _bridge_cfg_for / list_bridges).
    bridge_url_pinned: bool = False
    bridge_port_base: int = _BRIDGE_PORT_BASE
    bridge_port_range: int = _BRIDGE_PORT_RANGE  # ports 8768..8771 by default
    # Studio-open corruption-guard mode. "blanket" (default) = the original
    # 2026-03 behavior: any running FTOptixStudio.exe blocks every project's
    # reads/writes with no per-project attribution (see docs/studio-open-detection.md
    # "Why there is no override" — no in-band escape hatch is offered because it'd
    # be reachable by the calling model). "attributed" = when Studio is running BUT
    # the design-time bridge is up and reports it is serving a DIFFERENT project
    # (Studio-open-on-A, file-op-on-B), treat that as attribution and let the
    # operation proceed instead of blanket-refusing — Studio is not holding THIS
    # project's model, so the on-disk bytes are safe to touch. Every ambiguous
    # state (bridge down, multiple Studio PIDs, unresolvable/matching name) still
    # falls back to blanket. This is an operator-set env knob, not a tool
    # parameter — out-of-band, so it does not reopen the "escape hatch reachable
    # by the LLM" hole the no-override design explicitly closed.
    studio_guard_mode: str = "blanket"
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
    # Whole-frame OCR trust gate (cdp_ocr_runtime / cdp_read_text_runtime).
    # Tesseract reports a per-word confidence; these tools aggregate it to a
    # {mean, min} fraction in [0, 1]. When the mean falls below this threshold
    # the read-back is flagged `low_confidence` with a `next_step` nudge toward
    # ground-truth reads (optix_describe_node for the model, an
    # optix_cdp_screenshot return_image=true for the render). Distinct from
    # find_text's per-word 40-conf match filter — that gates word matching, this
    # gates "is this whole OCR pass trustworthy." OPTIX_OCR_CONF_THRESHOLD tunes it.
    ocr_conf_threshold: float = 0.60
    # Emulated device-metrics override applied to every CDP session before any
    # screenshot/click/OCR/route-replay (see core._cdp_session / _cdp.py
    # CDPClient.set_viewport). Fixes runtime screenshots clipping the HMI:
    # chrome-cdp's launch window (800x600) renders a 769x434 visible slice
    # (scrollbar/chrome overhead) and the Optix canvas fills whatever size
    # it's given, so the un-overridden capture is both clipped AND small.
    # OPTIX_CDP_VIEWPORT ("WIDTHxHEIGHT") / OPTIX_CDP_SCALE tune it; malformed
    # values fall back to 1280x720 / 1.0 (see _parse_cdp_viewport/_parse_cdp_scale).
    # Applying it centrally in _cdp_session (rather than only in the
    # screenshot path) is what keeps a LATER, separate click/route-replay call
    # (its own CDP session) seeing the SAME viewport a screenshot was taken
    # at — the hard invariant this feature depends on.
    cdp_viewport_width: int = 1280
    cdp_viewport_height: int = 720
    cdp_viewport_scale: float = 1.0

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
        cdp_vp_w, cdp_vp_h = _parse_cdp_viewport(os.environ.get("OPTIX_CDP_VIEWPORT"))
        return cls(
            projects_root=Path(os.environ.get(
                "OPTIX_PROJECTS_ROOT",
                str(Path.home() / "Documents" / "Rockwell Automation"
                    / "FactoryTalk Optix" / "Projects"),
            )),
            studio_exe=(
                Path(os.environ["FTOPTIX_STUDIO_EXE"])
                if os.environ.get("FTOPTIX_STUDIO_EXE")
                else _default_studio_exe()
            ),
            state_dir=state_dir,
            runtime_dir=runtime_dir,
            runtime_launcher=os.environ.get("OPTIX_RUNTIME_LAUNCHER"),
            runtime_test_port=int(os.environ.get("OPTIX_RUNTIME_TEST_PORT", "8081")),
            runtime_url=os.environ.get("OPTIX_RUNTIME_URL", "").strip(),
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
            # OPTIX_BRIDGE_URL explicitly set = the legacy
            # single-bridge pin (bridge_url_pinned=True), which skips
            # range-scanning entirely and always talks to that one URL —
            # unset = the new default, multi-instance range-scan mode.
            bridge_url=os.environ.get("OPTIX_BRIDGE_URL", "http://127.0.0.1:8768"),
            bridge_url_pinned="OPTIX_BRIDGE_URL" in os.environ,
            bridge_port_base=int(os.environ.get("OPTIX_BRIDGE_PORT_BASE", str(_BRIDGE_PORT_BASE))),
            bridge_port_range=int(os.environ.get("OPTIX_BRIDGE_PORT_RANGE", str(_BRIDGE_PORT_RANGE))),
            bridge_token=os.environ.get("OPTIX_BRIDGE_TOKEN"),
            bridge_enabled=os.environ.get("OPTIX_BRIDGE_ENABLED", "true").strip().lower()
                in ("1", "true", "yes", "on"),
            # Validated string enum — anything but a recognized mode (typos like
            # "atributed", empty, junk) falls back to the safe "blanket" default
            # rather than silently disabling the guard's attribution logic.
            studio_guard_mode=(
                _gm if (_gm := os.environ.get(
                    "OPTIX_STUDIO_GUARD_MODE", "blanket").strip().lower())
                in _STUDIO_GUARD_MODES else "blanket"
            ),
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
            ocr_conf_threshold=float(
                os.environ.get("OPTIX_OCR_CONF_THRESHOLD", "0.60")),
            cdp_viewport_width=cdp_vp_w,
            cdp_viewport_height=cdp_vp_h,
            cdp_viewport_scale=_parse_cdp_scale(os.environ.get("OPTIX_CDP_SCALE")),
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


# dropped the old outright rejection of "/" and "\\" in
# `project` (only ".." is still rejected up front). That extra check blocked
# every legitimately nested project name (e.g. "Site/Cell 4/Line_HMI_...")
# even though the real security boundary — the is_relative_to(root) check
# below, after .resolve() — already prevents escaping projects_root. Needed
# so users can reorganize project folders into subdirectories without
# breaking the MCP.
_OPTIX_SUFFIX = ".optix"


def _is_bare_project_name(project: str) -> bool:
    """True when `project` is a bare NAME and cannot itself carry a path.

    The gate on resolve_project's out-of-root fallback. A bare name has no
    separator (`/`, `\\`), no drive/stream colon, no traversal, and is not a
    relative-directory token. Everything the fallback then matches against
    comes from the OS process table, never from this string — so the caller
    can select among directories Studio already has open, but can never name
    one.
    """
    stripped = (project or "").strip()
    if not stripped or stripped in (".", ".."):
        return False
    return not any(c in stripped for c in ("/", "\\", ":", "\x00")) and ".." not in stripped


def _studio_open_candidates(force: bool = False) -> list[dict]:
    """Project directories a RUNNING FTOptixStudio.exe was launched on.

    Evidence source (a) for resolve_project's out-of-root fallback. Reuses
    studio_guard.studio_state() — the SAME cached process snapshot the
    corruption guard already takes (one psutil pass, name-filtered BEFORE any
    cmdline is read, 2s TTL) — rather than standing up a second scanner: see
    _emulator_pids on why asking process_iter for "cmdline" across every
    process on the box costs tens of seconds unelevated. Nothing new is
    cached here; the only cache is studio_guard's.

    A token is a candidate only when it is an ABSOLUTE path (Studio's CLI is
    always handed one — see _studio_open — and a relative token would resolve
    against this service's cwd, which is not evidence of anything) that names
    either a directory or a .optix file, and the resulting directory holds
    EXACTLY ONE .optix file. Zero .optix means it is not a project directory;
    two or more means the directory cannot be attributed to one project, so
    both are rejected rather than guessed at.

    Returns [{"dir": Path, "optix": Path, "names": {<dir name>, <.optix stem>}
    (lower-cased)}, ...]. Studio opened from the GUI carries no project on its
    command line (studio_guard's module docstring records the measurement), so
    such a process simply contributes no candidate — that is normal, not an
    error.
    """
    import ntpath

    out: list[dict] = []
    seen: set[str] = set()
    for cmd in studio_guard.studio_cmdlines(studio_guard.studio_state(force=force)):
        for tok in cmd:
            if not tok or tok.startswith("-"):
                continue
            low = tok.lower()
            if low.endswith(".exe"):
                continue
            # ntpath.isabs also answers True for a POSIX-absolute token, so
            # this one test covers the Windows box this runs on AND the
            # POSIX box the suite runs on.
            if not (ntpath.isabs(tok) or os.path.isabs(tok)):
                continue
            try:
                cand = Path(tok)
                cand = (cand.parent if low.endswith(_OPTIX_SUFFIX) else cand).resolve()
                if not cand.is_dir():
                    continue
                optix = sorted(cand.glob("*" + _OPTIX_SUFFIX))
            except (OSError, ValueError):
                continue
            if len(optix) != 1:
                continue
            key = str(cand).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "dir": cand,
                "optix": optix[0],
                # The dir basename is NOT enough: Line4_HMI lives at
                # ...\Desktop\Line4_HMI\HMI and is served as "Line4_HMI",
                # the .optix stem. (project_served_names() computes a richer
                # set for bridge routing, including the root node's Name from
                # Nodes/<stem>.yaml; this stays at the two names the security
                # rule is written against, and reads no project file.)
                "names": {cand.name.strip().lower(), optix[0].stem.strip().lower()},
            })
    return out


def _registry_open_candidates() -> list[dict]:
    """Project directories advertised by armed bridge registry entries.

    Evidence source (b) for resolve_project's out-of-root fallback.  Reads
    the bridge registry directory (``%LOCALAPPDATA%\\ftx-mcp\\bridges\\``) and
    returns one candidate per entry whose ``project_path`` resolves to an
    existing directory that holds exactly one ``.optix`` file.

    The matching set for each candidate includes three lower-cased strings:
    the ``project`` field as the bridge reported it, the resolved directory's
    basename, and the ``.optix`` stem — mirroring what ``_studio_open_candidates``
    builds from the Studio command line.

    ``project_path`` may be either the ``.optix`` file itself (from SDK
    reflection) or the directory that contains it (from ``Environment.
    CurrentDirectory``); both shapes are handled identically to the command-
    line token in ``_studio_open_candidates``.

    Returns [{"dir": Path, "optix": Path, "project": str,
    "names": {<lower-cased>}}, ...].
    """
    d = _bridge_registry_dir()
    if d is None:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        project_name = data.get("project")
        project_path = data.get("project_path")
        if not project_name or not project_path:
            continue
        try:
            pp = Path(str(project_path))
            low = str(project_path).lower()
            cand = (pp.parent if low.endswith(_OPTIX_SUFFIX) else pp).resolve()
            if not cand.is_dir():
                continue
            optix = sorted(cand.glob("*" + _OPTIX_SUFFIX))
        except (OSError, ValueError):
            continue
        if len(optix) != 1:
            continue
        key = str(cand).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "dir": cand,
            "optix": optix[0],
            "project": project_name,
            "names": {
                project_name.strip().lower(),
                cand.name.strip().lower(),
                optix[0].stem.strip().lower(),
            },
        })
    return out


def _studio_pid_for_project(project_dir: Path, force: bool = False) -> int:
    """PID of the running FTOptixStudio.exe whose command line names
    `project_dir` (the directory, or its .optix file). 0 when none does.

    Same evidence as _studio_open_candidates (a Studio launched on a project
    path — by this service's optix_project, or a shell double-click — carries
    it as an absolute argv token; a GUI-opened Studio carries nothing and is
    simply not attributable). Used by save() to target Ctrl+S at the right
    instance when no bridge serves the project.
    """
    import ntpath

    try:
        want = str(Path(project_dir).resolve()).lower()
    except (OSError, ValueError):
        return 0
    state = studio_guard.studio_state(force=force)
    for p in state.get("studio", {}).get("procs", []) or []:
        for tok in (p.get("cmdline") or []):
            if not tok or tok.startswith("-") or tok.lower().endswith(".exe"):
                continue
            if not (ntpath.isabs(tok) or os.path.isabs(tok)):
                continue
            try:
                cand = Path(tok)
                cand = cand.parent if tok.lower().endswith(_OPTIX_SUFFIX) else cand
                if str(cand.resolve()).lower() == want:
                    return int(p.get("pid") or 0)
            except (OSError, ValueError):
                continue
    return 0


def resolve_project_info(cfg: Config, project: str) -> dict:
    """resolve_project's answer WITH its provenance:
    {"dir": Path, "source": "projects_root" | "studio_process" | "registry"}.

    THE SECURITY RULE, in order:

      1. ".." anywhere in `project` -> reject (cheap, early, before any scan).
      2. Join onto projects_root and canonicalize. If the result is not under
         projects_root, reject — this is what catches an ABSOLUTE name (a
         drive letter, UNC, or leading slash), because joining an absolute
         path onto projects_root replaces it entirely (pathlib behavior)
         rather than escaping character by character. An absolute name is
         rejected HERE and is never eligible for steps 4-5.
      3. If the joined directory exists, that is the answer (source
         "projects_root"). This is the normal case and costs nothing extra —
         no process scan runs.
      4. ONLY when step 3 missed: the project may ALSO resolve to a directory
         that is demonstrably OPEN IN STUDIO on this box. `project` must be a
         bare name (see _is_bare_project_name) — a subpath never takes this
         path — and the directory must come from a running FTOptixStudio.exe
         command line (see _studio_open_candidates), exist, and hold exactly
         one .optix file whose stem, or whose directory basename, equals
         `project` case-insensitively. The caller's string is only ever
         COMPARED against that evidence; no path is ever taken from it.
      5. ONLY when step 4 also missed: a bridge registry entry (see
         _registry_open_candidates) whose ``project`` field matches the target
         and whose ``project_path`` exists on disk and contains exactly one
         .optix file qualifies as project evidence. The same bare-name and
         comparison-only security rules from step 4 apply here.

    Why step 4 exists: Studio can open a project from ANYWHERE (the field case
    is Line4_HMI at C:\\Users\\<u>\\Desktop\\Line4_HMI\\HMI, outside projects_root
    and armed). Every bridge-routed tool already worked with it — they find the
    bridge by the project it reports — while every resolve_project caller
    answered "project not found". _bridge_want_names already carried the same
    observation in its fallback comment.

    Why step 5 exists: the bridge registry (1.0.8+) writes
    ``%LOCALAPPDATA%\\ftx-mcp\\bridges\\<port>.json`` files that include the
    ``project_path`` field alongside ``project``. This gives a path-yielding
    evidence source even when no Studio process is visible in the scan (e.g.
    the process scanner is denied access or the bridge outlives Studio via
    an armed-but-headless scenario). The caller's string is still only ever
    COMPARED against that evidence; no path is ever taken from it.
    """
    if ".." in project:
        raise ProjectNotFound(f"invalid project name: {project!r}")
    root = cfg.projects_root.resolve()
    project_dir = (cfg.projects_root / project).resolve()
    if not project_dir.is_relative_to(root):
        # Checked BEFORE existence so an absolute name is rejected as what it
        # is, and — load-bearing — can never reach the Studio fallback below.
        raise ProjectNotFound(f"project not under projects_root: {project}")
    shadow = None
    if project_dir.is_dir():
        if not _is_bare_project_name(project) or any(project_dir.glob("*.optix")):
            return {"dir": project_dir, "source": "projects_root"}
        # A same-named in-root folder that is NOT a project (no .optix) - the
        # field shape Desktop\Line4_HMI\HMI with projects_root=Desktop. It used
        # to win here and every tool silently got the container folder. Let an
        # open-in-Studio / registry match answer first; keep it as the fallback.
        shadow = project_dir

    # --- fallback: demonstrably open in Studio --------------------------
    if not _is_bare_project_name(project):
        raise ProjectNotFound(
            f"project not found: {project} (resolved_from: "
            f"projects_root={cfg.projects_root}; open-in-Studio fallback not "
            f"attempted: {project!r} is a subpath, not a bare project name)"
        )
    want = project.strip().lower()
    candidates = _studio_open_candidates()
    for c in candidates:
        if want in c["names"]:
            return {"dir": c["dir"], "source": "studio_process"}

    # --- fallback: bridge registry entry --------------------------------
    reg_candidates = _registry_open_candidates()
    for c in reg_candidates:
        if want in c["names"]:
            return {"dir": c["dir"], "source": "registry"}

    if shadow is not None:
        return {"dir": shadow, "source": "projects_root"}
    if candidates:
        # Project NAMES only — the point is to explain the miss, not to
        # enumerate other people's directories back to the caller.
        seen = ", ".join(sorted({c["optix"].stem for c in candidates}))
        detail = f"Studio has {seen} open, not {project}"
    else:
        detail = ("no running FTOptixStudio.exe names a project directory on its "
                  "command line (a GUI-opened Studio carries none)")
    raise ProjectNotFound(
        f"project not found: {project} (resolved_from: "
        f"projects_root={cfg.projects_root}; open-in-Studio fallback: {detail})"
    )


def resolve_project(cfg: Config, project: str) -> Path:
    """Resolve a project name — which may be a subpath, e.g. a project nested
    several folders under projects_root ("Site/Cell 4/Line_HMI_...") — to its
    directory.

    Thin wrapper over resolve_project_info(), which documents the full
    security rule (including the out-of-root "open in Studio" fallback) and
    reports WHICH of the two sources answered. Use that one when the
    provenance matters (doctor(), bridge_arm()); use this one everywhere the
    directory is all the caller needs.
    """
    return resolve_project_info(cfg, project)["dir"]


def resolve_subpath(cfg: Config, project: str, subpath: str) -> Path:
    project_dir = resolve_project(cfg, project)
    full = (project_dir / subpath).resolve()
    if not full.is_relative_to(project_dir):
        raise PathTraversal(f"path traversal rejected: {subpath}")
    return full


# ---- build check (C# compile pre-flight) ------------------------------

# MSBuild diagnostic line: "<path>(line,col): error CS0117: message [proj]".
# The trailing "[<csproj>]" is stripped. Matches both error and warning; the
# code group covers CSxxxx and longer SDK codes (e.g. NETSDK1045).
_MSBUILD_DIAG = re.compile(
    r"^(?P<file>[^(\n]+?)\((?P<line>\d+),(?P<col>\d+)\):\s+"
    r"(?P<sev>error|warning)\s+(?P<code>[A-Za-z]{1,6}\d+):\s+"
    r"(?P<msg>.*?)(?:\s+\[[^\]\n]+\])?\s*$",
    re.MULTILINE,
)


# Base FTOptix/UAManagedCore/OPC-UA types that only fail to resolve when the
# assembly REFERENCES are missing - the signature of stale .references HintPaths
# (pinned to a Studio version not installed here, or a project moved between
# machines), NOT a real code defect.
_FTOPTIX_REF_TOKENS = ("FTOptix", "UAManagedCore", "IUAVariable", "IUAObject",
                       "BaseNetLogic", "NodeId", "OpcUa", "ExportMethod")


def _parse_msbuild_diagnostics(
    text: str, strip_prefix: Path | None = None, rebase_to: Path | None = None,
) -> tuple[list, list]:
    """Extract deduped {file,line,col,code,message} diagnostics from MSBuild
    output. A path under the throwaway build copy (strip_prefix) is mapped back
    to the real tree (rebase_to) and made relative to it, so file:line stays
    meaningful and no absolute host path leaks."""
    errors: list = []
    warnings: list = []
    seen: set = set()
    for m in _MSBUILD_DIAG.finditer(text):
        raw = m.group("file").strip()
        path = raw
        if strip_prefix is not None and rebase_to is not None:
            # MSBuild diagnostics are ALWAYS Windows paths (the compile runs
            # on the Studio box), so compare them as such regardless of the
            # host: on POSIX a plain Path() keeps the backslashes as a single
            # component and no prefix ever matches. PureWindowsPath is also
            # case-insensitive, which is the right rule for these paths.
            try:
                p = PureWindowsPath(raw)
                sp, rt = PureWindowsPath(strip_prefix), PureWindowsPath(rebase_to)
                if p.is_relative_to(sp):
                    p = rt / p.relative_to(sp)
                if p.is_relative_to(rt):
                    path = p.relative_to(rt).as_posix()
                else:
                    path = str(p)
            except (ValueError, OSError):
                path = raw
        key = (path, m.group("line"), m.group("code"), m.group("msg"))
        if key in seen:
            continue
        seen.add(key)
        item = {
            "file": path,
            "line": int(m.group("line")),
            "col": int(m.group("col")),
            "code": m.group("code"),
            "message": m.group("msg").strip(),
        }
        (errors if m.group("sev") == "error" else warnings).append(item)
    return errors, warnings


def _looks_like_stale_references(errors: list) -> bool:
    """True when errors are dominated by CS0246 'type/namespace not found' on
    FTOptix/UAManagedCore/OPC-UA types - the signature of stale .references
    HintPaths, not a code defect (the project builds fine in Studio, which
    regenerates its references against the installed version)."""
    if not errors:
        return False
    cs0246 = [e for e in errors if e.get("code") == "CS0246"]
    if len(cs0246) < max(5, 0.8 * len(errors)):
        return False
    return any(any(tok in e.get("message", "") for tok in _FTOPTIX_REF_TOKENS)
               for e in cs0246)


def build_check(cfg: Config, project: str, timeout_seconds: int = 240) -> dict:
    """Compile the project's NetSolution and report any C# errors/warnings,
    WITHOUT touching Studio's own build state or running the emulator.

    A broken .cs otherwise fails the build silently and takes the in-Studio
    bridge AND the emulator down with it; this turns that into an instant, safe
    file:line report, and is the correct pre-flight before a deploy. The check
    copies the NetSolution to a throwaway temp dir (bin/obj excluded) and builds
    the COPY there, so it NEVER touches the project's own bin/obj - a concurrent
    Studio build cannot race it and Studio's incremental state is never
    disturbed. Shared compilation is disabled so no background compiler process
    is left behind.

    Returns {ok, returncode, error_count, warning_count, errors[], warnings[],
    csproj, dotnet}, plus `hint` when the failure looks like stale .references,
    and `note_multiple_csproj` when more than one .csproj is present.

    LIMITATIONS a caller should know: (1) references must resolve from within the
    NetSolution copy - true for a standard Optix project (its .references file
    with absolute module HintPaths lives in NetSolution); a csproj that reaches
    OUTSIDE NetSolution won't resolve here. (2) If every error is CS0246 on
    FTOptix/UAManagedCore/OPC-UA types the project's .references HintPaths are
    almost certainly stale (pinned to a Studio version not installed, or carried
    from another machine) - it builds fine in Studio; the `hint` field flags
    this so it is not mistaken for a real code error."""
    project_dir = resolve_project(cfg, project)
    netsol = project_dir / "ProjectFiles" / "NetSolution"
    if not netsol.is_dir():
        return {"ok": False, "error": "no_netsolution",
                "message": f"no ProjectFiles/NetSolution under {project}"}
    csprojs = sorted(netsol.glob("*.csproj"))
    if not csprojs:
        return {"ok": False, "error": "no_csproj",
                "message": f"no .csproj under {project}/ProjectFiles/NetSolution"}
    csproj = csprojs[0]
    dotnet = shutil.which("dotnet") or "dotnet"

    work = Path(tempfile.mkdtemp(prefix="ftxbuild_"))
    try:
        src = work / "NetSolution"
        shutil.copytree(netsol, src,
                        ignore=shutil.ignore_patterns("bin", "obj", ".vs", "*.user"))
        cmd = [
            dotnet, "build", str(src / csproj.name), "-c", "Debug",
            "-o", str(work / "out"), "--nologo", "-v", "minimal",
            "-p:GenerateFullPaths=true", "-p:UseSharedCompilation=false",
        ]
        # Disable node reuse so no background compiler server lingers after the run.
        env = dict(os.environ, MSBUILDDISABLENODEREUSE="1")
        try:
            proc = _run_subprocess_with_tree_kill(
                cmd, capture_output=True, text=True, timeout=timeout_seconds, env=env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "build_timeout",
                    "message": f"dotnet build exceeded {timeout_seconds}s",
                    "csproj": _rel_to(csproj, project_dir)}
        except FileNotFoundError:
            return {"ok": False, "error": "no_dotnet",
                    "message": "the .NET SDK (dotnet) was not found on PATH; install it",
                    "csproj": _rel_to(csproj, project_dir)}
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        errors, warnings = _parse_msbuild_diagnostics(out, src, netsol)
        result = {
            "ok": proc.returncode == 0 and not errors,
            "returncode": proc.returncode,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "errors": errors[:100],
            "warnings": warnings[:40],
            "csproj": _rel_to(csproj, project_dir),
            "dotnet": dotnet,
        }
        if len(csprojs) > 1:
            result["note_multiple_csproj"] = [c.name for c in csprojs[1:]]
        if not result["ok"] and _looks_like_stale_references(errors):
            result["hint"] = (
                "every error is CS0246 on FTOptix/UAManagedCore/OPC-UA types: this is "
                "almost always stale .references HintPaths (pinned to a Studio version "
                "not installed here, or the project moved between machines), NOT a code "
                "defect. Open/rebuild the project in Studio to regenerate its "
                "references, then re-run. Compare ProjectFiles/NetSolution/*.references "
                "HintPaths to the installed Studio version.")
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _rel_to(path: Path, base: Path) -> str:
    try:
        return str(Path(path).relative_to(base)).replace("\\", "/")
    except ValueError:
        return str(path)


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


_LIST_PROJECTS_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", "$RECYCLE.BIN",
    "System Volume Information",
}
_LIST_PROJECTS_MAX_DEPTH = 4  # folders under projects_root a project may be nested


# rewrote from a flat, single-level iterdir() to a recursive
# walk (capped at _LIST_PROJECTS_MAX_DEPTH, skipping _LIST_PROJECTS_SKIP_DIRS)
# so projects organized into subfolders (e.g. Site/Cell 4/<project>) actually
# show up. Companion fix to the resolve_project relaxation above — both were
# needed to support arbitrarily nested project directories without an
# unhandled exception.
def list_projects(cfg: Config) -> list[dict]:
    """Every project under projects_root, however deep it's nested (up to
    _LIST_PROJECTS_MAX_DEPTH folders). `name` is the project's path relative
    to projects_root with forward slashes (e.g. "Site/Cell 4/Line_HMI_..."),
    which resolve_project accepts directly — a top-level project's name is
    unchanged from before (just its own folder name), so this is backward
    compatible with every existing caller.

    Once a directory is identified as a project (it directly contains an
    .optix file) its subtree is NOT walked further — a project's own Nodes/
    ProjectFiles/ folders are never mistaken for nested projects.
    """
    if not cfg.projects_root.is_dir():
        return []
    root = cfg.projects_root.resolve()
    out: list[dict] = []

    def _walk(dir_path: Path, depth: int) -> None:
        try:
            entries = sorted(dir_path.iterdir())
        except OSError:
            return
        optix_files = sorted(e for e in entries if e.is_file() and e.suffix == ".optix")
        if optix_files:
            rel = dir_path.relative_to(root)
            name = rel.as_posix() if str(rel) != "." else ""
            out.append({"name": name, "optix_file": optix_files[0].name})
            return  # a project's own internals are never nested projects
        if depth >= _LIST_PROJECTS_MAX_DEPTH:
            return
        for entry in entries:
            if (entry.is_dir() and entry.name not in _LIST_PROJECTS_SKIP_DIRS
                    and not entry.name.startswith(".")):
                _walk(entry, depth + 1)

    for entry in sorted(root.iterdir()):
        if (entry.is_dir() and entry.name not in _LIST_PROJECTS_SKIP_DIRS
                and not entry.name.startswith(".")):
            _walk(entry, 1)
    return out


def _attributed_studio_pass(
    cfg: Config, state: dict, project_dir: Path
) -> dict | None:
    """Attributed-mode arbiter for a RUNNING Studio.

    Returns a small audit dict {"studio_guard": "attributed",
    "studio_serving": <name>} when attributed mode PERMITS treating the
    running Studio as non-blocking for `project_dir`; returns None (block
    with the blanket rule) otherwise.

    Permits ONLY when every guard-narrowing condition holds:
      * cfg.studio_guard_mode == "attributed" (operator opt-in);
      * exactly one Studio PID — a second Studio instance could be serving
        THIS project without the single-project bridge ever knowing;
      * the bridge is available and names a served project;
      * that served project differs from `project_dir` (Studio-open-on-A,
        file-op-on-B) — Studio is NOT holding this project's model, so the
        on-disk bytes are safe. A bridge that IS serving this project is the
        real hazard the guard exists for and must keep blocking.
    Any ambiguity (blanket mode, multi-PID, bridge down/error, name match)
    returns None so the caller falls back to the blanket block. The name
    comparison reuses `_bridge_name_match`, the same one `_use_bridge_for`
    uses to decide "is the bridge serving THIS project".
    """
    if cfg.studio_guard_mode != "attributed":
        return None
    # NOT relaxed for multi-instance bridging (v1.0.7). With
    # several Studio PIDs open, attribution would need to map EACH running PID
    # to the specific bridge (if any) it owns — bridge_state()/list_bridges()
    # alone don't carry PID, only served-project — and getting that wrong here
    # risks a false "safe to write" downgrade of a CORRUPTION guard. Left
    # conservative on purpose: attributed mode still requires exactly one
    # Studio PID, even though several bridges can now be armed at once. Every
    # multi-instance project still gets protection from the default "blanket"
    # guard_mode regardless (unaffected either way — it never consults the
    # bridge). Revisit only with real PID-per-bridge attribution wired in.
    if len(state["studio"]["pids"]) != 1:
        return None
    try:
        bstate = bridge_state(cfg)
    except Exception:  # noqa: BLE001 — any bridge fault → ambiguous → blanket
        return None
    served = bstate.get("project")
    if not bstate.get("available") or not served:
        return None
    if _bridge_name_match(served, project_served_names(project_dir)):
        return None  # Studio holds THIS project — keep blanket-blocking
    return {"studio_guard": "attributed", "studio_serving": served}


def require_editors_closed(
    cfg: Config, project_dir: Path, force: bool = False
) -> dict | None:
    """Corruption guard: refuse project reads/writes while FTOptixStudio.exe
    is running (blanket rule — Studio's open project is not attributable from
    the outside; see service/studio_guard.py),
    or while VS / VS Code attributably has this project open.

    In "attributed" mode (cfg.studio_guard_mode == "attributed") a running
    Studio no longer blanket-blocks WHEN the design-time bridge proves Studio
    is serving a DIFFERENT project (see `_attributed_studio_pass`). The
    separate VS / VS Code attributed-editor check is unchanged and still
    fires. Returns None when the guard passed with nothing to report;
    returns {"studio_guard": "attributed", "studio_serving": <name>} when it
    passed ONLY because of Studio attribution — callers that surface guard
    metadata (read_file) merge it into their result; every attributed
    downgrade is also written to the audit trail here regardless of caller.

    Detection errors do NOT block: an enumeration fault is not evidence of
    Studio. deploy_preflight surfaces that condition as a warning instead.
    """
    state = studio_guard.studio_state(force=force)
    if state.get("error"):
        return None
    if state["studio"]["running"]:
        downgrade = _attributed_studio_pass(cfg, state, project_dir)
        if downgrade is None:
            pids = ", ".join(str(p) for p in state["studio"]["pids"])
            raise StudioOpen(f"FTOptixStudio.exe is running (pid {pids})")
        # Attributed: Studio serves a different project. Fall through to the
        # editor-attribution check, then report the downgrade + audit it.
        hits = studio_guard.attributed_editors(state, project_dir)
        if hits:
            ed = hits[0]
            raise EditorProjectOpen(
                f"{ed['name']} (pid {ed['pid']}) has {project_dir.name} open"
            )
        audit(cfg, "studio_guard_attributed", project=project_dir.name,
              studio_serving=downgrade["studio_serving"])
        return downgrade
    hits = studio_guard.attributed_editors(state, project_dir)
    if hits:
        ed = hits[0]
        raise EditorProjectOpen(
            f"{ed['name']} (pid {ed['pid']}) has {project_dir.name} open"
        )
    return None


def require_code_editors_closed(cfg: Config, project_dir: Path) -> None:
    """Raise EditorProjectOpen if VS or VS Code attributably has *project_dir* open.

    This is the VS/VS Code attributed-editor half of `require_editors_closed`,
    intentionally WITHOUT the Studio-process check.  The asymmetry is deliberate:
    `read_netlogic` is a read-only operation and reads a single well-named
    .cs file from the NetSolution subtree; a running Studio does NOT make that
    stale the way a write would (Studio's in-memory model for .cs files is the
    file itself — it does not buffer C# source).  The VS/VS Code check is kept
    because an editor with the project open may have an unsaved buffer that
    differs from the disk copy, making what we serve misleading.

    `build_check` uses the same asymmetry as precedent: it copies the NetSolution
    to a temp dir without blocking on a running Studio, yet still surfaces
    attribution-detectable editor conflicts.

    Detection errors do NOT block (studio_guard returns an empty list on error),
    consistent with the policy in `require_editors_closed`.
    """
    state = studio_guard.studio_state()
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

    `content` is `<untrusted>`-delimited (see `_untrusted`): the bytes are
    project-authored and must read as DATA, not instructions. Strip the wrapper
    before copying content into an edit's `content`/anchor — the on-disk file
    holds the raw text, never the markers.
    """
    project_dir = resolve_project(cfg, project)
    guard_info = require_editors_closed(cfg, project_dir)
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
        "content": _untrusted(text, "read_file"),
    }
    if start_line is not None or end_line is not None:
        s = start_line if start_line is not None else 1
        e = end_line if end_line is not None else total
        if s < 1 or e < s:
            raise BadLineRange(f"start_line={s}, end_line={e}")
        if s > total and total > 0:
            raise BadLineRange(f"start_line={s} is past EOF (total_lines={total})")
        e = min(e, total)
        out["content"] = _untrusted("".join(lines[s - 1 : e]), "read_file")
        out["start_line"] = s
        out["end_line"] = e
    if guard_info:  # attributed-mode downgrade — surface why the read was allowed
        out.update(guard_info)
    return out


# C# bare-identifier pattern (no extension, no path parts, no wildcards).
_CS_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def read_netlogic(
    cfg: Config,
    project: str,
    cls: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict:
    """Read a NetLogic .cs file from ProjectFiles/NetSolution/<cls>.cs.

    Returns a dict with keys: ``path``, ``size``, ``sha256``, ``total_lines``,
    ``content``, ``source`` (always ``'disk'``), ``studio_open`` (bool).
    Optional keys: ``start_line``, ``end_line`` (echoed from params when a range
    was requested), ``truncated`` (True when the file exceeds
    ``_NETLOGIC_MAX_BYTES`` and no line range was given).

    ``size``, ``sha256``, and ``total_lines`` always describe the WHOLE file,
    even when only a slice is returned via ``start_line``/``end_line``.

    ``content`` is ``<untrusted>``-delimited (see ``_untrusted``).

    Class-name validation rejects path injection attempts before touching the
    filesystem (PathTraversal), then rejects malformed-but-safe names
    (InvalidNetLogicClass).  A second ``resolve_subpath`` call provides
    belt-and-braces path-traversal protection after the project resolves.

    The guard only checks VS / VS Code attribution (``require_code_editors_closed``),
    NOT a running Studio — .cs reads are not stale while Studio is open because
    Studio does not buffer C# source in memory.  See ``require_code_editors_closed``
    and ``build_check`` for the documented precedent.
    """
    # ------------------------------------------------------------------
    # 1. Class-name validation BEFORE any filesystem access.
    # ------------------------------------------------------------------
    # PathTraversal: anything that looks like a path component (security gate).
    if '/' in cls or '\\' in cls:
        raise PathTraversal(
            f"netlogic class name must not contain path separators: {cls!r}"
        )
    if '..' in cls:
        raise PathTraversal(
            f"netlogic class name must not contain '..': {cls!r}"
        )
    # Drive-letter absolute path (e.g. "C:SomeClass")
    if len(cls) >= 2 and cls[1] == ':':
        raise PathTraversal(
            f"netlogic class name must not be an absolute path: {cls!r}"
        )

    # InvalidNetLogicClass: valid-looking but semantically wrong input.
    if not cls:
        raise InvalidNetLogicClass("cls must be non-empty")
    if cls.endswith('.cs'):
        raise InvalidNetLogicClass(
            f"cls must be a bare class name without the .cs extension: {cls!r}"
        )
    if '*' in cls or '?' in cls:
        raise InvalidNetLogicClass(
            f"cls must not contain wildcards: {cls!r}"
        )
    if not _CS_IDENTIFIER_RE.match(cls):
        # Catches leading digit, spaces, dots, hyphens, unicode non-identifiers, etc.
        raise InvalidNetLogicClass(
            f"cls must be a bare C# identifier (letters/digits/underscores, "
            f"no leading digit): {cls!r}"
        )

    # ------------------------------------------------------------------
    # 2. resolve_subpath — belt-and-braces path-traversal gate.
    # ------------------------------------------------------------------
    rel = f'ProjectFiles/NetSolution/{cls}.cs'
    project_dir = resolve_project(cfg, project)
    full = resolve_subpath(cfg, project, rel)

    # ------------------------------------------------------------------
    # 3. Editor check (VS / VS Code only — Studio is NOT blocked here).
    # ------------------------------------------------------------------
    require_code_editors_closed(cfg, project_dir)

    # ------------------------------------------------------------------
    # 4. Studio-open status for the result dict (informational).
    # ------------------------------------------------------------------
    _gstate = studio_guard.studio_state()
    studio_open = bool(_gstate.get("studio", {}).get("running"))

    # ------------------------------------------------------------------
    # 5. File presence — FileNotFound lists up to 20 sibling .cs files.
    # ------------------------------------------------------------------
    if not full.is_file():
        net_dir = full.parent
        siblings: list[str] = []
        if net_dir.is_dir():
            siblings = sorted(
                p.name for p in net_dir.iterdir()
                if p.suffix == '.cs' and p.is_file()
            )[:20]
        msg = f"NetLogic class not found: {rel}"
        if siblings:
            msg += f"; available in NetSolution: {', '.join(siblings)}"
        raise FileNotFound(msg)

    # ------------------------------------------------------------------
    # 6. Read and decode.
    # ------------------------------------------------------------------
    data = full.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BinaryFile(f"file is not valid UTF-8: {rel}") from exc

    lines = text.splitlines(keepends=True)
    total = len(lines)

    out: dict = {
        "path": rel,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "total_lines": total,
        "source": "disk",
        "studio_open": studio_open,
    }

    # ------------------------------------------------------------------
    # 7. Line-range slice or whole-file (with cap).
    # ------------------------------------------------------------------
    if start_line is not None or end_line is not None:
        s = start_line if start_line is not None else 1
        e = end_line if end_line is not None else total
        if s < 1 or e < s:
            raise BadLineRange(f"start_line={s}, end_line={e}")
        if s > total and total > 0:
            raise BadLineRange(
                f"start_line={s} is past EOF (total_lines={total})"
            )
        e = min(e, total)
        out["content"] = _untrusted("".join(lines[s - 1 : e]), "read_netlogic")
        out["start_line"] = s
        out["end_line"] = e
    elif len(data) > _NETLOGIC_MAX_BYTES:
        # No range requested but file exceeds the byte cap: return the first
        # _NETLOGIC_MAX_BYTES bytes and flag truncation.  Nudge the caller to
        # use start_line / end_line for targeted access.
        truncated_text = data[:_NETLOGIC_MAX_BYTES].decode("utf-8", errors="ignore")
        out["content"] = _untrusted(truncated_text, "read_netlogic")
        out["truncated"] = True
        out["truncation_hint"] = (
            f"File is {len(data):,} bytes (>{_NETLOGIC_MAX_BYTES:,} byte cap); "
            f"only the first {_NETLOGIC_MAX_BYTES:,} bytes are shown. "
            f"Use start_line / end_line to read specific sections."
        )
    else:
        out["content"] = _untrusted(text, "read_netlogic")

    return out


# Directory parts that never hold user-meaningful Optix source. bin/obj are
# the NetSolution build outputs Studio regenerates on every compile.
_FIND_SKIP_PARTS = frozenset({".git", "bin", "obj", ".venv", "__pycache__", ".vs"})
_FIND_MAX_FILE_BYTES = 2_000_000
_NETLOGIC_MAX_BYTES = 1_000_000


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
    # rebind to the bridge SERVING this project (one of possibly
    # several simultaneously armed) rather than the old implicit single cfg.bridge_url.
    _bridge_cfg = _bridge_cfg_for(cfg, project)
    if _bridge_cfg is not None:
        return _bridge_find(_bridge_cfg, project, query, max_results, case_sensitive)
    project_dir = resolve_project(cfg, project)
    require_editors_closed(cfg, project_dir)
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
                # matched line + surrounding context are project-authored file
                # text — delimit as untrusted DATA (path/line are service-derived
                # locators, left raw).
                "text": _untrusted(lines[i][:400], "find_in_project"),
                "context_before": [
                    _untrusted(x[:400], "find_in_project")
                    for x in lines[max(0, i - context_lines) : i]],
                "context_after": [
                    _untrusted(x[:400], "find_in_project")
                    for x in lines[i + 1 : i + 1 + context_lines]],
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

# multi-instance bridge support (v1.0.7). This USED TO be a single
# {available, project, ...} snapshot for the one well-known bridge at
# cfg.bridge_url. Now that up to cfg.bridge_port_range Studio instances can each
# have an armed bridge on their own self-assigned port, the cache is keyed by
# bridge URL instead — see _bridge_health_at, the per-URL building block
# bridge_state/list_bridges/_find_bridge_for are all built on.
_bridge_cache: dict[str, dict] = {}
_bridge_cache_at: dict[str, float] = {}
_BRIDGE_CACHE_TTL = 2.0

# --- Bridge transport diagnostics (state_dir/logs/bridge.jsonl) ---------------
# The design-time bridge lives inside Studio; its socket dies whenever a rebuild
# unloads the NetLogic assembly, so drops/timeouts are EXPECTED and must stay
# diagnosable after the fact. Every _bridge_http call is timed and, per the
# verbosity policy in _bridge_log_call, appended to a size-rotated bridge.jsonl:
# every transport failure, every non-2xx, every real (non-health) op, and every
# health up<->down transition — the steady-state 2s health poll is deduped out.
_BRIDGE_LOG_MAX_BYTES = 2_000_000
_BRIDGE_LOG_BACKUPS = 2
_bridge_last_ok_at: str | None = None        # iso ts of the last OK transport call
_bridge_last_health_ok: bool | None = None   # health-probe transition dedupe


def bridge_event(cfg: Config, **fields) -> None:
    """Append one JSONL line to the bridge transport log
    (state_dir/logs/bridge.jsonl), size-rotated (.jsonl -> .1 -> .2). Best-effort;
    diagnostics must never break a bridge call."""
    try:
        d = cfg.state_dir / "logs"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "bridge.jsonl"
        try:
            if p.exists() and p.stat().st_size >= _BRIDGE_LOG_MAX_BYTES:
                for i in range(_BRIDGE_LOG_BACKUPS, 0, -1):
                    src = p if i == 1 else d / f"bridge.jsonl.{i - 1}"
                    if src.exists():
                        os.replace(src, d / f"bridge.jsonl.{i}")
        except Exception:
            pass
        rec = {"ts": _dt.datetime.now(_dt.UTC).isoformat(timespec="milliseconds"),
               **fields}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _bridge_log_call(cfg: Config, path: str, method: str, latency_ms: int,
                     status: int | None, error: str | None) -> None:
    """Record one transport call + maintain last-seen state, applying the
    verbosity policy: log every failure / non-2xx / non-health op, and only
    health up<->down TRANSITIONS (never the steady 2s health poll)."""
    global _bridge_last_ok_at, _bridge_last_health_ok
    ok = error is None and status is not None and status < 400
    if ok:
        _bridge_last_ok_at = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
    if path.startswith("/bridge/health"):
        should_log = ok != _bridge_last_health_ok   # transition (or first probe) only
        _bridge_last_health_ok = ok
    else:
        should_log = True
    if should_log:
        bridge_event(cfg, path=path, method=method, latency_ms=latency_ms,
                     status=status, ok=ok, error=error)


def _bridge_http(
    cfg: Config, path: str, method: str = "GET", timeout: float = 5.0,
    retries: int = 0,
) -> tuple[int, bytes]:
    """Request cfg.bridge_url + path. Raises BridgeUnavailable on transport error.

    Short timeout: a hung Studio must never stall a call — a timeout means
    "bridge unavailable, fall back", not a hard error. The bridge's write
    endpoints take their params in the query string (no body), so POST sends an
    empty body purely to select the verb. Every attempt is timed and logged to
    bridge.jsonl (see _bridge_log_call). `retries` re-attempts ONLY the transport-
    failure path (safe for idempotent GETs), backoff 0.3s; writes pass retries=0.

    IncompleteRead / RemoteDisconnected are retried within the same budget and,
    if exhausted, classified as BridgeLoading (the bridge is mid-rebuild) rather
    than BridgeUnavailable (which implies it was never up).
    """
    import http.client
    import urllib.error
    import urllib.request
    url = cfg.bridge_url.rstrip("/") + path
    data = b"" if method == "POST" else None
    attempt = 0
    while True:
        req = urllib.request.Request(url, method=method, data=data)
        if cfg.bridge_token:
            req.add_header("Authorization", f"Bearer {cfg.bridge_token}")
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status, body = resp.status, resp.read()
            _bridge_log_call(cfg, path, method, int((time.monotonic() - t0) * 1000), status, None)
            return status, body
        except urllib.error.HTTPError as e:
            body = e.read() or b""
            _bridge_log_call(cfg, path, method, int((time.monotonic() - t0) * 1000), e.code, None)
            return e.code, body
        except (http.client.IncompleteRead, http.client.RemoteDisconnected) as e:
            # The bridge accepted the connection but the response was truncated or
            # the connection was dropped — a signature of a bridge that is currently
            # being rebuilt / reloading its model.  Must be checked BEFORE the
            # URLError/OSError handler because RemoteDisconnected is also a
            # ConnectionResetError (OSError subclass) and would otherwise be
            # swallowed by the more general clause.  Retry within the caller's
            # budget so a brief mid-rebuild window self-heals; if retries are
            # exhausted, surface BridgeLoading (retryable 503) rather than a raw
            # http.client traceback.
            _bridge_log_call(cfg, path, method, int((time.monotonic() - t0) * 1000), None, str(e))
            if attempt < retries:
                attempt += 1
                time.sleep(0.25 * attempt + random.uniform(0.0, 0.4))
                continue
            raise BridgeLoading(
                f"bridge at {url} returned a partial response (mid-rebuild?): {e}"
            ) from e
        except (urllib.error.URLError, OSError) as e:
            _bridge_log_call(cfg, path, method, int((time.monotonic() - t0) * 1000), None, str(e))
            if attempt < retries:
                attempt += 1
                # Backoff grows per attempt AND carries random jitter so that a burst
                # of parallel writes that all failed to connect at once do not all wake
                # and re-collide on the single-threaded accept loop at the same instant.
                time.sleep(0.25 * attempt + random.uniform(0.0, 0.4))
                continue
            raise BridgeUnavailable(f"bridge unreachable at {url}: {e}") from e


def _bridge_get_json(cfg: Config, path: str, timeout: float = 5.0, retries: int = 1) -> tuple[int, dict]:
    """_bridge_http + JSON decode. Non-JSON / empty body -> {}. GETs are
    idempotent, so one transport retry by default smooths a transient blip (heavy
    Studio work briefly stops the single-threaded listener from accepting)."""
    status, raw = _bridge_http(cfg, path, timeout=timeout, retries=retries)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        data = {}
    return status, data if isinstance(data, dict) else {}


def _bridge_post_json(cfg: Config, path: str, timeout: float = 8.0) -> tuple[int, dict]:
    """POST to a bridge query-param endpoint; JSON-decode the response.

    Writes retry transient transport failures (retries=2). The bridge accept loop is
    single-threaded, so a burst of parallel authoring calls can momentarily fail to
    connect while it is mid-handling another; the retry re-attempts and succeeds in
    milliseconds instead of surfacing a hard "cannot connect". Bridge writes are
    idempotent (setting the same property twice is harmless), so retrying is safe.
    """
    status, raw = _bridge_http(cfg, path, method="POST", timeout=timeout, retries=3)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        data = {}
    return status, data if isinstance(data, dict) else {}


def _bridge_post_body(
    cfg: Config, path: str, payload: dict, timeout: float = 20.0
) -> tuple[int, dict]:
    """POST a JSON BODY to the bridge and decode the reply.

    Every other bridge write passes params on the query string (see
    _bridge_http), which is why that helper sends an empty POST body. An op
    BATCH does not fit in a query string, so /bridge/validate_ops is the one
    endpoint that reads a body — hence this separate helper rather than a
    `data=` parameter on _bridge_http, whose "no body" contract other callers
    rely on. Timeout is longer than a single write: the bridge reflects over
    every op in the batch.
    """
    import http.client
    import urllib.error
    import urllib.request
    url = cfg.bridge_url.rstrip("/") + path
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, method="POST", data=body)
    req.add_header("Content-Type", "application/json")
    if cfg.bridge_token:
        req.add_header("Authorization", f"Bearer {cfg.bridge_token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        status, raw = e.code, (e.read() or b"")
    except (http.client.IncompleteRead, http.client.RemoteDisconnected) as e:
        # Must precede OSError: RemoteDisconnected is also a ConnectionResetError.
        raise BridgeLoading(
            f"bridge at {url} returned a partial response (mid-rebuild?): {e}"
        ) from e
    except (urllib.error.URLError, OSError) as e:
        raise BridgeUnavailable(f"bridge unreachable at {url}: {e}") from e
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
    # per-op nudge attached to the exception so that
    # classify_bridge_failure can surface it as a structured, handler-specific
    # nudge rather than the generic write_failed connection message.
    exc_nudge: str | None = None
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or "unknown bridge error"
        code = err.get("code")
        # Keep the machine-readable code (e.g. unsupported_array_write) visible
        # to the caller — the message alone may not name it.
        if code and code not in msg:
            msg = f"{code}: {msg}"
        # when the bridge refuses because in-place rewrite is
        # unsafe, append a nudge naming the existing handler and the replace=True
        # escape hatch so the caller always knows the recovery path.
        if code == "handler_exists":
            existing = err.get("existing") or {}
            browse = existing.get("browse_name", "")
            if browse:
                inline = (f" — call wire_event again with replace=True to delete"
                          f" '{browse}' and create a fresh handler")
                exc_nudge = (f"handler_exists: '{browse}' already handles this event"
                             f" — call wire_event again with replace=True to delete"
                             f" it and create a fresh handler")
            else:
                inline = (" — call wire_event again with replace=True to delete"
                          " the existing handler and create a fresh one")
                exc_nudge = ("handler_exists: an event handler already exists for"
                             " this event type — call wire_event again with"
                             " replace=True to delete it and create a fresh handler")
            msg += inline
    else:
        msg = err or f"status={status}"
    exc = BridgeWriteFailed(f"bridge {op} failed: {msg}")
    if exc_nudge is not None:
        exc.nudge = exc_nudge  # type: ignore[attr-defined]
    raise exc


def _bridge_write_guard(cfg: Config, project: str) -> Config:
    """Raise BridgeUnavailable unless the bridge is serving `project` (writes
    mutate the live model — there is no file fallback); else return `cfg`
    rebound to that SPECIFIC bridge's URL.

    now just _require_bridge_for under its original write-path
    name — with multi-instance support the caller MUST use the returned,
    rebound cfg for its bridge calls (`cfg = _bridge_write_guard(cfg, project)`),
    not the cfg it passed in, or it'll target whichever bridge happens to be
    first in port order instead of the one actually serving `project`."""
    return _require_bridge_for(cfg, project)


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
    # per-op refusals (e.g. handler_exists) may carry a
    # specific nudge attribute; use it when present so the caller gets
    # actionable guidance instead of the generic "check the property" message.
    if isinstance(exc, BridgeWriteFailed):
        exc_nudge = getattr(exc, "nudge", None)
        return {
            "state": "failed", "reason_code": "write_failed",
            "nudge": exc_nudge if exc_nudge else (
                "The design-time bridge is up and serving this project — this is a "
                "per-operation error, not a connection problem, so do NOT restart "
                "Studio. Check the property name/value; the bridge's own message is "
                "in `detail`."
            ),
            "detail": detail,
            "bridge": {"reachable": True, "serving": project, "model_loaded": True},
        }

    # BridgeUnavailable → probe health directly for a precise classification.
    # scan every port in range, not just cfg.bridge_url's base
    # port — with several bridges potentially armed, the base port alone can
    # be unbound while a DIFFERENT port is serving the WRONG project (or is
    # the RIGHT one, mid-transient-failure); probing only the base port would
    # misclassify either case as "Studio closed." Uses _bridge_health_at
    # directly (not list_bridges, which filters to available=True only — a
    # port that answers but hasn't loaded its model yet is "reachable" for
    # classification purposes even though it isn't "available").
    probed = [_bridge_health_at(cfg, port, force=True) for port in _bridge_ports(cfg)]
    responded = [p for p in probed if p.get("responded")]
    reachable = bool(responded)
    serving = None
    model_loaded = None
    wanted = _bridge_want_names(cfg, project) or {project.strip().lower()}
    matched = next((p for p in responded if _bridge_name_match(p.get("project"), wanted)), None)
    if matched is not None:
        serving = matched.get("project")
        model_loaded = bool(matched.get("available"))
    elif responded:
        serving = responded[0].get("project")
        model_loaded = bool(responded[0].get("available"))

    if reachable:
        norm = (serving or "").strip().lower()
        if norm and norm not in ("unknown", "") and norm not in wanted:
            code = "bridge_wrong_project"
            others = ", ".join(repr(p.get("project")) for p in responded)
            nudge = (f"{len(responded)} bridge(s) are reachable, serving {others} — none is "
                     f"{project!r}. Ask the user to open {project!r} in Studio (and run "
                     f"StartBridge if its bridge doesn't come up); other projects' bridges "
                     f"can stay armed at the same time, no need to StopBridge them first.")
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


# UA node ATTRIBUTES (DisplayName, BrowseName, ...) exist as CLR properties on
# every node proxy but are NOT UA child variables — materializing one via the
# bridge fabricated an orphan variable and Studio access-violated on the next
# render (crash confirmed live 2026-08-16, agent set DisplayName). Bridge 1.0.6+
# rejects them too (node_attribute_not_settable); this pre-dispatch refusal is
# the stale-bridge defense — the .cs is hand-pasted into Studio, so an older
# bridge build can never see the request. DisplayName is the one exception:
# set_property routes it to the bridge's dedicated ATTRIBUTE endpoint
# (/bridge/node/displayname, bridge 1.0.7) which assigns the real attribute and
# never touches variable materialization; bind/attach still refuse it (an
# attribute can't carry a DynamicLink or converter).
_NODE_ATTRIBUTE_NAMES = frozenset(
    {"DisplayName", "BrowseName", "Description", "NodeId", "NodeClass"})


def _reject_node_attribute(verb: str, name: str) -> None:
    if name in _NODE_ATTRIBUTE_NAMES:
        raise BridgeWriteFailed(
            f"bridge {verb} rejected: node_attribute_not_settable — {name!r} is "
            f"a node attribute, not a settable property; writing it can crash "
            f"Studio. DisplayName is settable via set_property only; to rename "
            f"a node, use the rename op "
            f"({{op: 'rename', path: ..., new_name: ...}}) or move with new_name."
        )


def _value_prop_nudge_suffix(node_path: str) -> str:
    """Build the pre-1.0.8 bridge nudge for unknown_property / 'Value' errors.

    Older bridges (< 1.0.8) cannot set the OPC-UA ``Value`` attribute via the
    ``name='Value'`` spelling on the variable node itself.  The working
    alternative is to address the *parent* node and use the variable's browse
    name as the property name — i.e. ``path=<parent>, name=<VariableName>``.
    This suffix is appended to the raised ``BridgeWriteFailed`` message (write
    path) and to the matching report error ``message`` (batch-edit path) so the
    LLM caller always sees the correction.
    """
    if "/" in node_path:
        parent, var_name = node_path.rsplit("/", 1)
    else:
        parent, var_name = "", node_path
    return (
        f" — pre-1.0.8 bridge: 'Value' is the OPC-UA variable's own value "
        f"attribute, not a settable child property; use "
        f"path={parent!r}, name={var_name!r} to write the variable's value "
        f"(address the parent node, use the variable name as the property name)"
    )


def bridge_set_property(
    cfg: Config, project: str, node_path: str, name: str, value: str,
    locale: str = "en-US",
) -> dict:
    """Set a property on a live-model node via the design-time bridge.

    On a fresh instance the bridge materializes the inherited property via
    GetOrCreateVariable so it persists AND renders (the fix for the GetVariable-
    returns-null trap). Requires Studio
    open with this project + the bridge running.

    **Setting a variable's value — two equivalent spellings (neither deprecated):**

    1. *On the variable node itself* — ``name="Value"`` targets the node's own
       built-in Value attribute::

           bridge_set_property(cfg, project, "Model/MyVar", "Value", "42")

    2. *From the parent node* — ``name=<VariableName>`` reaches the same UA
       variable through its parent, which is often the more natural address when
       the caller already holds the parent path::

           bridge_set_property(cfg, project, "Model", "MyVar", "42")

       Both forms write to identical storage; choose whichever path you already
       have in hand.  A bridge older than 1.0.8 does not support spelling (1) and
       will return ``unknown_property`` — the exception message will suggest
       spelling (2) as a fallback in that case.

    Array-typed properties (String[] like GridLayout.Columns/Rows, NodeId[] like
    NavigationPanelItem.AliasNodeArray) are NOT writable: the bridge rejects them
    by declared type (unsupported_array_write) because a scalar write to an array
    UA variable crashed Studio outright (2026-07-16). A JSON-array value signals
    that intent, so reject it here too — before dispatch — so a bridge running an
    older build can never see it.
    """
    if name == "DisplayName":
        # Attribute route (bridge 1.0.7+). A 1.0.5/1.0.6 bridge answers the
        # unknown route with not_found — a clean per-op failure, never a crash.
        return _bridge_write(
            cfg, project, "set_displayname", "/bridge/node/displayname",
            {"path": node_path, "value": value, "locale": locale},
        )
    _reject_node_attribute("set_property", name)
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
    try:
        return _bridge_write(
            cfg, project, "set_property", "/bridge/node/property",
            {"path": node_path, "name": name, "value": value, "locale": locale},
        )
    except BridgeWriteFailed as exc:
        msg = str(exc)
        if name == "Value" and "has no settable property 'Value'" in msg:
            raise BridgeWriteFailed(msg + _value_prop_nudge_suffix(node_path)) from exc
        raise


# Child node names that must reside in the FTOptix.UI namespace (not the project
# namespace) for the engine to honour their properties. A bridge older than 1.0.8
# creates these in the project namespace, where writes like RowStart/ColumnStart
# are silently accepted and equally silently ignored at render time.
_UI_NAMESPACE_CHILDREN: frozenset[str] = frozenset({"GridLayoutProperties"})

_UI_NAMESPACE_NUDGE = (
    "note: a bridge older than 1.0.8 creates this node in the project namespace "
    "where RowStart/ColumnStart are silently ignored; bridge ≥ 1.0.8 creates "
    "it in FTOptix.UI as the engine requires"
)


def _bridge_write_ui_ns_child(
    cfg: Config, project: str, op: str, endpoint: str, params: dict,
) -> dict:
    """Like _bridge_write but with UI-namespace-child post-processing.

    For names in _UI_NAMESPACE_CHILDREN the bridge may return wrong_namespace
    (the child was created in the wrong namespace and the path is in the response)
    or ui_namespace_child_unsupported (both creation paths failed). Both are
    surfaced as BridgeWriteFailed with the created_path in the message so the
    caller can clean up. On success, ui_namespace_child:True and a nudge are
    injected into the returned dict.
    """
    from urllib.parse import quote, urlencode
    cfg = _bridge_write_guard(cfg, project)
    qs = urlencode(params, quote_via=quote)
    status, data = _bridge_post_json(cfg, f"{endpoint}?{qs}")
    # Pre-check: surface namespace errors with the created path before the
    # standard error handler discards the extra fields.
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        err_code = data["error"]
        if err_code in ("wrong_namespace", "ui_namespace_child_unsupported"):
            created = data.get("created_path", "")
            exc = BridgeWriteFailed(
                f"bridge {op} failed: {err_code}"
                + (f"; created_path={created}" if created else "")
            )
            audit(cfg, "bridge_write", project=project, op=op, params=params,
                  ok=False, error=str(exc))
            raise exc
    try:
        out = _bridge_write_result(op, status, data)
    except Exception as exc:
        audit(cfg, "bridge_write", project=project, op=op, params=params,
              ok=False, error=str(exc))
        raise
    audit(cfg, "bridge_write", project=project, op=op, params=params, ok=True)
    out["ui_namespace_child"] = True
    out["note"] = _UI_NAMESPACE_NUDGE
    return out


def bridge_create_widget(
    cfg: Config, project: str, screen: str, name: str, widget_type: str = "Label",
) -> dict:
    """Create a builtin UI widget on a screen in the live model via the bridge."""
    params = {"name": name, "screen": screen, "type": widget_type}
    if name in _UI_NAMESPACE_CHILDREN:
        return _bridge_write_ui_ns_child(
            cfg, project, "create_widget", "/bridge/ui/widget", params)
    return _bridge_write(
        cfg, project, "create_widget", "/bridge/ui/widget", params,
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
    if name in _UI_NAMESPACE_CHILDREN:
        return _bridge_write_ui_ns_child(
            cfg, project, "create_object", "/bridge/model/object", params)
    return _bridge_write(
        cfg, project, "create_object", "/bridge/model/object", params)


def bridge_create_netlogic(
    cfg: Config, project: str, parent: str, name: str,
) -> dict:
    """Create a NetLogic node bound to a C# class BY NAME (no Studio "New ->
    NetLogic" step). A NetLogic node carries no proxy and no code reference: the
    runtime binds it to a class whose name equals the node BrowseName, and the
    SDK-style NetSolution .csproj auto-globs every .cs, so a matching class
    compiles in on the next build. `name` MUST equal the C# class name exactly.
    Author the .cs (class : BaseNetLogic) separately, then rebuild/run so the
    runtime instantiates it. For a runtime NetLogic that reads its Owner's
    siblings, place it under that object (parent)."""
    return _bridge_write(
        cfg, project, "create_netlogic", "/bridge/model/netlogic",
        {"parent": parent, "name": name})


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
    # ATTACH MODE (U19): the external runtime owns its own WebPresentationEngine
    # — the service is not the one hosting the canvas, so provisioning one here
    # would be pointless (and would require Studio + the bridge that the attach
    # deployment does not run). Refuse before the bridge write.
    if attach_mode(cfg):
        return {
            "ok": False,
            "error": "external_runtime",
            "hint": (
                "OPTIX_RUNTIME_URL is set — the external runtime owns its "
                "WebPresentationEngine; not provisioning one."
            ),
        }
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
        rec = {"ts": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
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
        rec = {"ts": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
               "tool": tool, "chars_in": chars_in, "chars_out": chars_out,
               "ms": ms, "ok": ok}
        with open(d / "traffic.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _untrusted(value: object, source: str) -> str:
    r"""Delimit project/runtime-derived text as DATA before it re-enters an
    LLM's context, so it reads as content to inspect — never as instructions
    from the operator. See docs/errors.md, "<untrusted> delimiting".

    `source` records provenance the way bridge responses already set
    `source: "bridge"` (describe_node / get_project_map / list_ui_types /
    describe_type) — it is a service-authored constant, never caller input, so
    it needs no escaping.

    Any literal ``</untrusted`` inside the content is escaped to
    ``<\/untrusted`` so authored project text cannot forge the closing boundary
    and smuggle instructions back out of the wrapper. This MARKS a boundary; it
    is not a proof the content is inert. Pair it with the client-side
    permission gating shipped in examples/claude-code-settings.json
    (docs/security.md, "Untrusted tool-response content").
    """
    text = "" if value is None else str(value)
    safe = text.replace("</untrusted", r"<\/untrusted")
    return f'<untrusted source="{source}">{safe}</untrusted>'


# added the `timeout` kwarg (default None -> unchanged 8s
# behavior). Discovered live: invoke_method calling a slow built-in method
# (FindBrokenDynamicLink scanning a whole project) blew past the 8s default
# and came back as a false "bridge unreachable" — the bridge was fine, the
# call was just still running when the client gave up. Generic invoke needs
# to let slow methods run longer.

# --- Service-lifecycle log (state_dir/logs/service.jsonl) ---------------------
# Records process start / clean stop / crash-with-traceback. This is what makes a
# mysterious death diagnosable after the fact: a "start" with no following "stop" or
# "crash" means the process was killed EXTERNALLY (SIGKILL, a manual stop, or a Task
# Scheduler ExecutionTimeLimit termination = result 0x00041306) — whereas a "crash"
# carries the Python traceback. Size-rotated; best-effort (never breaks startup).
_SERVICE_LOG_MAX_BYTES = 1_000_000
_SERVICE_LOG_BACKUPS = 3


def service_event(cfg: Config, event: str, **fields) -> None:
    """Append one JSONL line to the service-lifecycle log
    (state_dir/logs/service.jsonl), size-rotated (.jsonl -> .1 -> .2 -> .3).
    Best-effort: lifecycle logging must never break the service."""
    try:
        d = cfg.state_dir / "logs"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "service.jsonl"
        try:
            if p.exists() and p.stat().st_size >= _SERVICE_LOG_MAX_BYTES:
                for i in range(_SERVICE_LOG_BACKUPS, 0, -1):
                    src = p if i == 1 else d / f"service.jsonl.{i - 1}"
                    if src.exists():
                        os.replace(src, d / f"service.jsonl.{i}")
        except Exception:
            pass
        rec = {"ts": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
               "event": event, **fields}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _read_service_lifecycle(cfg: Config, n: int = 5) -> dict:
    """Read the service-lifecycle log and return a structured digest.

    Reads ``state_dir/logs/service.jsonl`` and its size-rotated predecessors
    (``.1`` through ``.3``), normalises each entry to the four canonical
    fields (event, ts, version, pid), enriches ``crash`` entries with
    ``exc_type`` (type name parsed from the ``error`` field) and
    ``first_frame`` (the first ``File …`` line in the traceback), then
    derives a synthetic ``external_kill`` row for every ``start`` that has no
    following ``stop`` or ``crash`` anywhere in the full log.

    Returns ``{"entries": [...], "n": n}`` where ``entries`` is the *last*
    ``n`` records (including any derived rows appended at the end).  When the
    log directory has never been written a ``"note": "no_log"`` key is
    included and ``entries`` is empty.
    """
    d = cfg.state_dir / "logs"
    # Assemble file list oldest → newest so entries end up chronological.
    paths: list[Path] = []
    for i in range(_SERVICE_LOG_BACKUPS, 0, -1):
        p = d / f"service.jsonl.{i}"
        if p.exists():
            paths.append(p)
    primary = d / "service.jsonl"
    if primary.exists():
        paths.append(primary)

    if not paths:
        return {"entries": [], "n": n, "note": "no_log"}

    # --- parse all JSONL lines in chronological order ---
    all_recs: list[dict] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                all_recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # --- normalise each record to the canonical field set ---
    def _norm(rec: dict) -> dict:
        evt = rec.get("event", "")
        row: dict = {
            "event": evt,
            "ts": rec.get("ts"),
            "version": rec.get("version"),
            "pid": rec.get("pid"),
        }
        if evt == "crash":
            error = rec.get("error") or ""
            row["exc_type"] = (
                error.split(": ", 1)[0] if ": " in error else (error or None)
            )
            tb = rec.get("traceback") or ""
            first_frame: str | None = None
            for tb_line in tb.splitlines():
                stripped = tb_line.strip()
                if stripped.startswith('File "'):
                    first_frame = stripped
                    break
            row["first_frame"] = first_frame
        return row

    normalized = [_norm(r) for r in all_recs]

    # --- derive external_kill rows ---
    # A "start" with no following "stop" or "crash" in the full log means
    # the process was killed externally (SIGKILL, Task Scheduler
    # ExecutionTimeLimit, or a manual stop that did not go through our
    # shutdown path).
    for i, rec in enumerate(all_recs):
        if rec.get("event") != "start":
            continue
        has_closer = any(
            e.get("event") in ("stop", "crash")
            for e in all_recs[i + 1:]
        )
        if not has_closer:
            normalized.append({
                "event": "external_kill",
                "ts": rec.get("ts"),
                "version": rec.get("version"),
                "pid": rec.get("pid"),
                "note": (
                    "start with no following stop or crash — "
                    "process was killed externally "
                    "(SIGKILL, Task Scheduler ExecutionTimeLimit, or manual stop)"
                ),
            })

    return {"entries": normalized[-n:], "n": n}


def _bridge_write(
    cfg: Config, project: str, op: str, endpoint: str, params: dict,
    *, method: str = "POST", timeout: float | None = None,
) -> dict:
    """Guard + (POST|GET) a bridge authoring endpoint + interpret the result.

    Shared shape for the semantic-authoring wrappers below. A write the
    bridge cannot perform safely on its HTTP thread answers not_implemented /
    property_not_materialized, and this raises
    BridgeWriteFailed with the message (no crash).
    """
    from urllib.parse import quote, urlencode
    cfg = _bridge_write_guard(cfg, project)
    # quote_via=quote (percent-encoding, space -> %20) NOT the default quote_plus
    # (space -> +): the bridge's C# query parser percent-decodes but treats '+' as a
    # literal, so a plain "hello from cowork" arrived as "hello+from+cowork". %20
    # round-trips to a real space.
    qs = urlencode(params, quote_via=quote)
    kwargs = {} if timeout is None else {"timeout": timeout}
    if method == "GET":
        status, data = _bridge_get_json(cfg, f"{endpoint}?{qs}", **kwargs)
    else:
        status, data = _bridge_post_json(cfg, f"{endpoint}?{qs}", **kwargs)
    try:
        out = _bridge_write_result(op, status, data)
    except Exception as exc:
        audit(cfg, "bridge_write", project=project, op=op, params=params,
              ok=False, error=str(exc))
        raise
    audit(cfg, "bridge_write", project=project, op=op, params=params, ok=True)
    return out


def _make_bridge_rollback_fail(
    cfg: Config, project: str, node_path: str,
    *, steps: list[str] | None = None,
):
    """Factory for the two bridge composites' transactional `_fail` closure:
    delete the just-created node (rollback) and shape the failure envelope.

    The two composites report DIFFERENT envelopes and both are load-bearing:
    bridge_add_bound_widget carries `steps` (the running step-name list) AND a
    remediation `hint` on rollback failure; bridge_add_navigation_panel_item
    carries NEITHER. Passing `steps` (a list) reproduces the widget shape;
    `steps=None` omits both keys, reproducing the nav-panel-item shape."""
    def _fail(step: str, exc: Exception) -> dict:
        rolled_back = False
        try:
            bridge_delete_node(cfg, project, node_path)
            rolled_back = True
        except Exception:
            pass
        out = {"ok": False, "failed_step": step,
               "rolled_back": rolled_back, "error": str(exc)}
        if steps is not None:
            out["steps"] = steps
        if not rolled_back:
            out["orphaned_path"] = node_path
            if steps is not None:
                out["hint"] = ("rollback failed — delete the half-configured node "
                               f"at {node_path} before retrying")
        return out
    return _fail


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

    _fail = _make_bridge_rollback_fail(cfg, project, node_path, steps=steps)

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

    # steps=None: the nav-panel-item envelope intentionally carries no `steps`
    # and no rollback `hint` (asymmetric with bridge_add_bound_widget).
    _fail = _make_bridge_rollback_fail(cfg, project, node_path)

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
    _reject_node_attribute("bind_property", name)
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


# Native FTOptix.UI commands `command` accepts by NAME. They live on the builtin
# UICommands object OUTSIDE the project root, so none of them can be reached as a
# `method_path` (that answers node_not_found) — which is why they are a command.
# Their InputArguments ride in `args`:
#   OpenDialog    Dialog=<Dialog type path>;AliasNode=<node or empty>;ParentItem=
#   CloseDialog   —
#   OpenKeyboard  KeyboardType=<Numeric|AlphaNumeric|…>;TargetVariable=<variable
#                 path>;ParentItem=      (three arguments, per
#                 Modules/FTOptix.UI/<ver>/Module.xml — a wrong COUNT fails at
#                 runtime, silently, on the click; the bridge checks the three
#                 before it builds anything)
# KEEP IN SYNC with StudioMCPBridge.cs::_UiCommands.
_UI_COMMANDS: tuple[str, ...] = ("OpenDialog", "CloseDialog", "OpenKeyboard")
# The builtin VariableCommands the bridge wires without `args`.
_VARIABLE_COMMANDS: tuple[str, ...] = ("SetVariable", "ToggleVariable")


def bridge_wire_event(
    cfg: Config, project: str, node_path: str, event_type: str,
    method_path: str | None = None, *,
    command: str | None = None, variable: str | None = None,
    value: str | None = None, args: str | None = None,
    object_raw: str | None = None,
    replace: bool = False,
) -> dict:
    """Wire a UI event on `node_path` — to a native command OR a NetLogic ExportMethod.

    `event_type` is a builtin event type name (e.g. MouseClickEvent). Provide EITHER:
      - a native `command` (no custom NetLogic needed): "SetVariable" (needs
        `variable` + `value`) or "ToggleVariable" (needs `variable`). These wire to
        the builtin FTOptix VariableCommands object — the preferred path for common
        actions (set/toggle a variable from a button).
      - a native UI `command` — "OpenDialog", "CloseDialog" or "OpenKeyboard"
        (`_UI_COMMANDS`) — whose arguments ride in `args` as "Name=Value;…".
        OpenKeyboard needs all three of KeyboardType/TargetVariable/ParentItem.
      - a `method_path` ("ObjectPath/MethodName") pointing at a NetLogic [ExportMethod],
        for custom logic.

    When `replace=True`, any existing handler for the same event type is deleted
    before the new one is created (delete-then-create). This is the explicit
    caller escape hatch; the default (False) leaves in-place update / conflict
    error behaviour intact.

    A client-side guard catches the common wrong-event-name trap (e.g. "Click" ->
    "MouseClickEvent") and returns a structured suggestion before hitting the bridge;
    genuinely-unknown names pass through to the bridge, which is authoritative.
    """
    nudge = _canonicalize_event(event_type)
    if nudge is not None:
        return nudge
    # Commands are matched by exact name in the bridge; canonicalize casing
    # here the way event names are, so "opendialog" is not silently routed
    # down the VariableCommands branch and rejected for carrying `args`.
    if command:
        _known = {c.lower(): c for c in _UI_COMMANDS + _VARIABLE_COMMANDS}
        command = _known.get(command.strip().lower(), command)
    ui_command = command in _UI_COMMANDS
    if command:
        params: dict[str, str] = {"path": node_path, "event": event_type, "command": command}
        if variable is not None:
            params["variable"] = variable
        if value is not None:
            params["value"] = value
        # UI commands (_UI_COMMANDS) live on the builtin UICommands object
        # OUTSIDE the project root, so they are a `command`, not a method_path --
        # and unlike the variable commands they take their arguments from `args`:
        # "Dialog=<Dialog type path>;AliasNode=<node or empty>;ParentItem=" for
        # OpenDialog, "KeyboardType=Numeric;TargetVariable=<path>;ParentItem="
        # for OpenKeyboard. An empty ParentItem is late-bound to the event node
        # by the bridge, as a Studio-authored handler does.
        if ui_command and args:
            params["args"] = args
    elif method_path:
        params = {"path": node_path, "event": event_type, "method": method_path}
        # B19: method ARGUMENTS, "Name=Value" pairs separated by ';'. Without
        # them only argument-LESS methods are reachable, which excludes every
        # method the converter needs -- ChangePanel(NewPanel),
        # ChangePanelByTabName(TabName), OpenDialog(Dialog, AliasNode).
        # A value that resolves to a node becomes a NodePointer argument;
        # anything else becomes a String. Only valid with method_path: the
        # command path builds its own InputArguments (VariableToModify/Value/
        # ArrayIndex) and a second source would collide with it.
        if args:
            params["args"] = args
        # B19: late-bind the call TARGET. An absolute NodeId names the
        # design-time node; a UI object is instantiated per session, so the
        # dispatcher finds nothing there. `object_raw` is a literal NodePath
        # RELATIVE TO THE EVENT NODE (e.g. "../Loader"); the bridge prepends the
        # four levels up from ObjectPointer and defaults the @NodeId attribute.
        # Required for every UI-hosted method -- ChangePanel does nothing without
        # it, silently.
        if object_raw:
            params["object_raw"] = object_raw
    else:
        raise BridgeWriteFailed("wire_event needs either command (+variable[/value]) or method_path")
    if args and command and not ui_command:
        raise BridgeWriteFailed(
            "wire_event: `args` applies to method_path only -- the command path "
            "builds its own InputArguments (VariableToModify/Value/ArrayIndex). "
            "Passing both would silently drop one of them.")
    # pass replace=true only when requested so the bridge can
    # delete the existing handler and create a fresh one.
    if replace:
        params["replace"] = "true"
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


# new — generic wrapper around the bridge's new
# /bridge/node/invoke endpoint (IUAObject.ExecuteMethod), added so any
# exported NetLogic method — including Optix's own built-in
# SearchBrokenDynamicLinks/FixAliasDynamicLinkMode tools — can be triggered
# from here instead of requiring a manual right-click -> Execute in Studio.
def bridge_invoke_method(
    cfg: Config, project: str, node_path: str, method_name: str,
    args: str | None = None, timeout: float = 60.0, unsafe: bool = False,
) -> dict:
    """Execute an exported UAMethod on an IUAObject node in the live model.

    `node_path` is the object hosting the method (e.g. a NetLogic node);
    `method_name` is the exported method name. `args`, if given, is a
    comma-separated string of input argument values (positional). Uses
    IUAObject.ExecuteMethod under the hood. Live-model op; requires Studio
    open + the bridge.

    The bridge runs the call under a root Optix session
    (Sessions.ImpersonateRootTemporary). Issue #4 root cause: ExecuteMethod on
    the bridge's HTTP thread had NO session, and native code dereferencing the
    missing session killed FTOptixStudio.exe on any method (a throwing one, a
    no-op, Optix's own SearchBrokenDynamicLinks). Studio's right-click Execute
    survived only because its GUI thread has a session.

    Bridge builds from before that fix still run the call session-less and
    will take Studio down, so this refuses (`invoke_unsupported_bridge`,
    before the invoke request) unless the serving bridge's /bridge/health
    advertises `invoke_session`. `unsafe` is accepted for compatibility
    and has no effect.

    `timeout` (seconds) defaults to 60, well above the 8s default every
    other bridge write uses: an arbitrary method's runtime is unknowable.
    """
    del unsafe  # legacy flag: invoke is session-safe on bridges that allow it
    routed = _bridge_write_guard(cfg, project)
    status, health = _bridge_get_json(routed, "/bridge/health")
    if not (status == 200 and isinstance(health, dict) and health.get("invoke_session")):
        version = health.get("bridge_version") if isinstance(health, dict) else None
        raise BridgeWriteFailed(
            "bridge invoke_method rejected: invoke_unsupported_bridge — the bridge "
            f"serving {project!r} (version {version or 'unknown'}) runs ExecuteMethod "
            "without an Optix session, which kills Studio (issue #4). Rebuild the "
            "NetSolution with the current studio-bridge/StudioMCPBridge.cs and "
            "re-arm, or use optix_execute_method (Studio's right-click Execute)."
        )
    params: dict[str, str] = {"path": node_path, "method": method_name}
    if args is not None:
        params["args"] = args
    return _bridge_write(
        cfg, project, "invoke_method", "/bridge/node/invoke", params,
        timeout=timeout,
    )


def bridge_reorder_node(
    cfg: Config, project: str, node_path: str,
    position: str | None = None, index: int | None = None,
) -> dict:
    """Reorder a node among its siblings in the **graphic-children index space**
    (z-order = render order; last child renders in front).

    ``position`` in {front, back} places the node at one extreme of the
    graphic-children list.  ``index`` is a zero-based integer offset into that
    same list — IUAVariable children (node properties) are excluded from the
    count and do not shift the index, so the graphic index differs from the raw
    ``parent.Children`` index whenever properties are interspersed.

    Exactly one of ``position`` or ``index`` must be supplied; supplying both is
    a local error.  A negative or non-integer ``index`` is rejected before the
    bridge is contacted.

    Uses node.MoveUp()/MoveDown() with a re-reading loop that re-checks
    GraphicIndexOf after every move and stops on no-progress.  Only effective on
    graphic objects inside a TYPE (ScreenType/PanelType). Live-model write.

    Returns on success: {ok, path, from, requested, achieved, space:"graphic_children",
    moves, mode, thread}.  ``achieved`` is the graphic index re-read after all
    moves; if it differs from ``requested`` the bridge instead returns ok:false
    with error:"reorder_index_unreached".
    """
    if position is not None and index is not None:
        raise BridgeWriteFailed(
            "bridge reorder failed: supply position OR index, not both"
        )
    if index is not None:
        if not isinstance(index, int) or isinstance(index, bool):
            raise BridgeWriteFailed(
                "bridge reorder failed: index must be a non-negative integer"
            )
        if index < 0:
            raise BridgeWriteFailed(
                f"bridge reorder failed: index must be >= 0, got {index}"
            )
    params: dict[str, str] = {"path": node_path}
    if position is not None:
        params["position"] = position
    if index is not None:
        params["index"] = str(index)
    return _bridge_write(cfg, project, "reorder", "/bridge/node/reorder", params)


# .NET composite-format positional placeholder: {0}, {0:F2}, {1,8:N1}. `{{`/`}}`
# are literal braces and are stripped before matching.
_POSITIONAL_PLACEHOLDER = re.compile(r"\{(\d+)\s*(?:,[^{}:]*)?(?::[^{}]*)?\}")

# Verbs whose `sources` field is a delimited string on the wire. The positional
# pair splits on ',' (Source0..N); attach_formatter's named `name=path` pairs
# split on ';'.
_ATTACH_SOURCE_SEP: dict[str, str] = {
    "attach_expression": ",",
    "attach_string_formatter": ",",
    "attach_formatter": ";",
}


def _coerce_sources(verb: str, sources):
    """Return `sources` as the delimited string the bridge route reads.

    A list is the natural JSON shape for "several sources", and it used to be
    passed through `str()` — the bridge then looked up the literal
    "['Model/X']" and answered source_not_variable. Join it instead. A dict
    is accepted for attach_formatter's named form ({"navIdx": "Model/..."}).
    """
    if sources is None or isinstance(sources, str):
        return sources
    sep = _ATTACH_SOURCE_SEP.get(verb, ",")
    if isinstance(sources, dict):
        if verb != "attach_formatter":
            raise BridgeWriteFailed(
                f"op {verb!r}: `sources` is positional ({{0}}, {{1}}, ..), so pass "
                f"a list or a comma-separated string, not a name->path mapping")
        return sep.join(f"{k}={v}" for k, v in sources.items())
    if isinstance(sources, (list, tuple)):
        return sep.join(str(x).strip() for x in sources if str(x).strip())
    raise BridgeWriteFailed(
        f"op {verb!r}: `sources` must be a string or a list of paths, "
        f"got {type(sources).__name__}")


def unbound_placeholders(format: str, sources) -> list[int]:
    """Positional placeholders in `format` that no source is bound to.

    "{0:F2}" with no sources attaches a StringFormatter whose Source0 is
    missing: the bridge reports ok and the label renders the bare format
    text. That is how a `source` (singular) typo once reached a live project
    reported as `succeeded`.
    """
    if not format:
        return []
    stripped = format.replace("{{", "").replace("}}", "")
    wanted = {int(m) for m in _POSITIONAL_PLACEHOLDER.findall(stripped)}
    src = _coerce_sources("attach_string_formatter", sources) or ""
    bound = len([s for s in src.split(",") if s.strip()])
    return sorted(i for i in wanted if i >= bound)


def bridge_attach_expression(
    cfg: Config, project: str, node_path: str, prop_name: str,
    expression: str, sources: str | None = None,
) -> dict:
    """Attach an ExpressionEvaluator converter to a property (roadmap tool A).
    `expression` is the FT Optix formula ("dumb Excel"): {0},{1},.. placeholders
    bound to the `sources` (comma-separated model/node paths) in order. e.g.
    expression='if({0} > 40, 0xFFFF0000, 0xFF00FF00)', sources='Model/Speed' on a
    FillColor. Subsumes ConditionalConverter/Linear/etc. Live-model write."""
    _reject_node_attribute("attach_expression", prop_name)
    sources = _coerce_sources("attach_expression", sources)
    params: dict[str, str] = {"path": node_path, "name": prop_name, "expression": expression}
    if sources:
        params["sources"] = sources
    return _bridge_write(cfg, project, "attach_expression", "/bridge/node/attach-expression", params)


def bridge_attach_formatter(
    cfg: Config, project: str, node_path: str, prop_name: str,
    format: str, sources: str | None = None, mode: str | None = None,
) -> dict:
    """Attach a FORMATTED DYNAMIC LINK to a property — a DynamicLink whose path
    is built by a StringFormatter from NAMED sources.

    The "indirect address" shape: a PanelLoader's `Panel` picked by an integer,
    with the candidate panels as readable named variables on the loader instead
    of opaque NodeId GUIDs inside a ValueMapConverter. Equally the
    "{#value} {#eu}" unit-label shape on a Label's Text.

        format  = "../NavPanel{#navIdx}@Pointer"
        sources = "navIdx=Model/GlobalVariables/AlarmVariables/AlarmTab"

    `{#name}` binds to a HasSource child called `Source<name>`, so the source
    NAME is load-bearing — unlike attach_expression, whose sources are
    POSITIONAL (Source0..N) and which is hard-wired to ExpressionEvaluator.
    `sources` is name=path pairs separated by ';'. `mode` is Read (default),
    ReadWrite or Write.

    Replaces whatever was on the property, so migrating off a ValueMapConverter
    needs no separate teardown. Live-model write — and like every converter, a
    mis-wired one renders NOTHING with no error, so render-verify rather than
    trusting ok:true."""
    _reject_node_attribute("attach_formatter", prop_name)
    sources = _coerce_sources("attach_formatter", sources)
    params: dict[str, str] = {"path": node_path, "name": prop_name, "format": format}
    if sources:
        params["sources"] = sources
    if mode:
        params["mode"] = mode
    return _bridge_write(cfg, project, "attach_formatter", "/bridge/node/attach-formatter", params)


def bridge_attach_string_formatter(
    cfg: Config, project: str, node_path: str, prop_name: str,
    format: str, sources: str | list[str] | None = None,
    raw: str | bool | None = None, mode: str | None = None,
) -> dict:
    """Attach a StringFormatter as the converter ON A PROPERTY — the shape that
    FORMATS A DISPLAY VALUE (a Float rendered "67.7", not "67.701805").

    This is NOT attach_formatter. attach_formatter builds a formatted dynamic
    LINK (the StringFormatter hangs off the DynamicLink and produces a NodePath,
    the "indirect address" pattern); used as a value formatter on Label.Text it
    renders EMPTY. This op puts the StringFormatter where attach_expression puts
    its ExpressionEvaluator — HasConverter on the property itself — so its output
    is the rendered text.

        format  = "{0:F1}"   (.NET composite string; {0},{1},.. are the sources)
        sources = "Model/Water/Temps/RawWaterTemp/value"   (positional -> Source0..N)

    `sources` are POSITIONAL: a comma-separated string or a list. Every {N}
    in `format` needs a source (else unbound_placeholder, before any write). Pass `raw=True` to attach each
    source as a LITERAL NodePath (so an alias/template path like "{data}/value"
    attaches on a PanelType and resolves per instance) — otherwise each source
    must resolve to a variable now. Live-model write; render-verify (a mis-wired
    converter renders nothing, ok:true only means attached)."""
    _reject_node_attribute("attach_string_formatter", prop_name)
    sources = _coerce_sources("attach_string_formatter", sources)
    missing = unbound_placeholders(format, sources)
    if missing:
        raise BridgeWriteFailed(
            f"unbound_placeholder: format {format!r} uses "
            f"{', '.join('{%d}' % i for i in missing)} but only "
            f"{len([x for x in (sources or '').split(',') if x.strip()])} source(s) "
            f"are bound — the formatter would attach and render the bare format "
            f"text. Pass `sources` (plural), positional, e.g. sources='Model/Temp'.")
    params: dict[str, str] = {"path": node_path, "name": prop_name, "format": format}
    if sources:
        params["sources"] = sources
    if raw is True or (isinstance(raw, str) and raw.lower() in ("1", "true")):
        params["raw"] = "1"
    if mode:
        params["mode"] = mode
    return _bridge_write(cfg, project, "attach_string_formatter",
                         "/bridge/node/attach-string-formatter", params)


def bridge_retype(
    cfg: Config, project: str, node_path: str, datatype: str,
    dims: str | None = None,
) -> dict:
    """Change a VARIABLE's DataType in place — optionally its array shape —
    keeping the node: NodeId, children (DynamicLink, converters, Mode) and
    every inbound reference survive, which delete+create cannot offer (and
    create_variable cannot make an array at all).

    The current value is converted element-wise; float→text goes through
    ToString("R") so 22.3f becomes "22.3", never 22.299999237060547.

        {"op": "retype", "path": "Recipes/RecipeSchema/EditModel/Setpoints",
         "datatype": "String"}                     # keeps the [10] shape
        {"op": "retype", "path": "Model/Setpoint", "datatype": "Double",
         "dims": "scalar"}

    `dims` omitted keeps the current shape; "scalar" collapses to a scalar;
    "N" sets a one-dimensional array of N. Typical use: storing values as String
    and letting a driver cast String<->REAL at the link, which means retyping
    the model arrays and every StoreColumn behind them."""
    params: dict[str, str] = {"path": node_path, "datatype": datatype}
    if dims is not None and str(dims) != "":
        params["dims"] = str(dims)
    return _bridge_write(cfg, project, "retype", "/bridge/node/retype", params)


def bridge_validate_expression(
    cfg: Config, project: str, expression: str, sources: str | None = None,
) -> dict:
    """Syntax-check an ExpressionEvaluator formula WITHOUT attaching it.

    Optix only validates a formula at RUNTIME (a bad one silently no-ops), so this
    catches the common author-time mistakes up front:
      * unbalanced ()/{}, out-of-range {N} placeholders, unknown functions,
        unterminated strings, number+string concat (pre-existing checks)
      * invalid_literal — colour-hex: a ``#RRGGBB`` / ``#RGB`` token outside a
        string is not an ExpressionEvaluator literal; the response carries the
        converted ``0xAARRGGBB`` form as the actionable fix
      * invalid_literal — boolean-spelling: capitalised ``True`` / ``False``
        (Python f-string shape) are rejected at runtime; the response names the
        lowercase fix (``true`` / ``false``)
      * invalid_literal — NodeId-valued-result: a top-level result that is a
        bare NodeId-typed source reference with no comparison or ``isempty``
        around it is flagged; if the text alone is not conclusive the validator
        drops the check rather than emit a false positive

    Returns {valid, sources, error?}. When ``valid`` is false the ``error``
    string starts with the class prefix (e.g. ``invalid_literal: ...``) so
    callers and ``bridge_edit`` report entries can distinguish literal mistakes
    from structural ones without parsing the prose.  The SAME check gates
    optix_bridge_attach_expression and the bridge's ValidateExpression
    right-click method. Read-only (no model change)."""
    params: dict[str, str] = {"expression": expression}
    if sources:
        params["sources"] = sources
    return _bridge_write(cfg, project, "validate_expression", "/bridge/expr/validate", params)


# ---- U16: batched authoring (validate-then-apply) ---------------------------

# op verb -> (core function, required op keys, optional op keys mapped to kwargs).
# Every entry dispatches to the SAME per-noun bridge_* call the individual tool
# uses, so a batched op and a single op cannot diverge in behaviour. Adding an op
# here is the only change needed to batch it — but the bridge's own
# _MutationRoutes-style validation list must learn the verb too, or
# validate_ops reports it as `unknown_op` (a warning, applied unchecked).
_BRIDGE_EDIT_OPS: dict[str, tuple[str, tuple[str, ...], dict[str, str]]] = {
    "set_property":      ("bridge_set_property", ("path", "name", "value"),
                          {"locale": "locale"}),
    "bind":              ("bridge_bind_property", ("path", "name"),
                          {"source_path": "source_path", "mode": "mode",
                           "raw_path": "raw_path"}),
    "create_widget":     ("bridge_create_widget", ("screen", "name"),
                          {"widget_type": "widget_type"}),
    "create_variable":   ("bridge_create_variable", ("name",),
                          {"parent": "parent", "datatype": "datatype"}),
    "create_folder":     ("bridge_create_folder", ("parent", "name"), {}),
    "create_object":     ("bridge_create_object", ("parent", "name"),
                          {"object_type": "object_type"}),
    "create_type":       ("bridge_create_type", ("name", "parent"),
                          {"base_type": "base_type"}),
    "create_alias":      ("bridge_create_alias", ("parent_path", "name"),
                          {"target_path": "target_path", "kind": "kind"}),
    "delete":            ("bridge_delete_node", ("path",), {}),
    "move":              ("bridge_move_node", ("path", "new_parent"),
                          {"new_name": "new_name"}),
    "reorder":           ("bridge_reorder_node", ("path",),
                          {"position": "position", "index": "index"}),
    "wire_event":        ("bridge_wire_event", ("path", "event_type"),
                          {"method_path": "method_path", "command": "command",
                           "variable": "variable", "value": "value",
                           "args": "args", "object_raw": "object_raw",
                           "replace": "replace"}),
    "attach_expression": ("bridge_attach_expression", ("path", "prop_name", "expression"),
                          {"sources": "sources"}),
    "attach_formatter":  ("bridge_attach_formatter", ("path", "prop_name", "format"),
                          {"sources": "sources", "mode": "mode"}),
    "attach_string_formatter": ("bridge_attach_string_formatter", ("path", "prop_name", "format"),
                          {"sources": "sources", "raw": "raw", "mode": "mode"}),
    "retype":            ("bridge_retype", ("path", "datatype"), {"dims": "dims"}),
    "add_translation":   ("bridge_add_translation", ("key", "value"), {"locale": "locale"}),
}

# Verbs accepted at the batch surface. `rename` is sugar — _normalize_edit_op
# lowers it to `move` (same parent + new_name) before validation, so it never
# reaches _BRIDGE_EDIT_OPS dispatch or the C# validator under its own name.
BRIDGE_EDIT_VERBS: frozenset[str] = frozenset(_BRIDGE_EDIT_OPS) | {"rename"}

# The op key that carries the node path differs per noun (path / screen /
# parent_path); the first positional of each core call is whatever that noun
# names it, so map the op dict onto positionals by name.
_BRIDGE_EDIT_POSITIONAL = {
    "set_property": ("path", "name", "value"),
    "bind": ("path", "name"),
    "create_widget": ("screen", "name"),
    "create_variable": ("name",),
    "create_folder": ("parent", "name"),
    "create_object": ("parent", "name"),
    "create_type": ("name", "parent"),
    "create_alias": ("parent_path", "name"),
    "delete": ("path",),
    "move": ("path", "new_parent"),
    "reorder": ("path",),
    "wire_event": ("path", "event_type"),
    "attach_expression": ("path", "prop_name", "expression"),
    "attach_formatter": ("path", "prop_name", "format"),
    "attach_string_formatter": ("path", "prop_name", "format"),
    "retype": ("path", "datatype"),
    "add_translation": ("key", "value"),
}


# Fields every op may legally carry beyond its verb's own, because
# _normalize_edit_op accepts them as aliases or the batch surface adds them.
_OP_COMMON_FIELDS: frozenset[str] = frozenset({"op", "node_path", "prop_name"})

# Verbs whose blast radius is the whole node at `path` (no partial/scoped form
# exists for any of them — there is no "delete just this property" op). An
# unknown field on one of these is not "an extra the applier ignores"; it is
# very likely a caller trying to SCOPE the op down (e.g. delete's old `name`
# guess for "just this property") and getting the full-node op instead.
# REGRESSION-CITE (2026-09-02): {"op": "delete", "path": "<NavigationPanel>",
# "name": "AttachedPanelLoader"} was meant to clear one property. `name` isn't
# a delete field, so under strict=false (the default) it was a WARNING only —
# report stayed ok:true — and delete ran against the whole node at `path`,
# taking the entire NavigationPanel with it. Recovered by the caller's own
# Ctrl+Z in Studio; the bridge had no undo. These verbs must hard-fail on an
# unknown field even when the caller didn't ask for strict.
_DESTRUCTIVE_OP_VERBS: frozenset[str] = frozenset({"delete", "move", "reorder"})

# Verbs whose node-type field the OTHER surfaces spell `type`. The raw HTTP
# routes take `type` (/bridge/ui/widget?...&type=PanelLoader) and the
# HTTP-first callers (make_op("create_widget", ..., type=...)) follow
# them because it fires over HTTP; the batch op-spec spells it `widget_type` /
# `object_type`. _normalize_edit_op reconciles the two — see I32.
_OP_TYPE_ALIAS: dict[str, str] = {
    "create_widget": "widget_type",
    "create_object": "object_type",
}


def unknown_op_fields(op: dict) -> list[str]:
    """Field names on `op` that its verb does not define.

    The legal set is not new schema — `_BRIDGE_EDIT_OPS` already declares, per
    verb, the required positionals and the optional field map. This is a set
    difference against data that was already there.

    Why this exists: an op carrying a misspelled or invented field used to be
    applied SILENTLY. `optix_bridge_edit` reported `applied: 1`, `errors: []`,
    `warnings: []`, `succeeded` — with `strict: true` — and the node landed in
    the wrong place. Field notes call it "the dangerous one"; it produced a
    phantom `Model/RecipeEditVars`. Reproduced on 1.0.7 (2026-08-23) with a
    `create_folder` op carrying `bogus_field_that_does_not_exist`.

    The gap was exactly one layer deep: MCP TOOL arguments are validated (a
    wrong parameter name is a clean pydantic rejection), but the op dicts
    inside a batch were free-form and unchecked.

    Returns [] for an unknown verb — that is `bridge_validate_ops`' job to
    report, and duplicating it here would produce two errors for one mistake.
    """
    if not isinstance(op, dict):
        return []
    verb = op.get("op")
    spec = _BRIDGE_EDIT_OPS.get(verb)
    if spec is None:
        return []
    _fn, required, optional = spec
    legal = _OP_COMMON_FIELDS | set(required) | set(optional)
    # `name` is not one of these verbs' declared fields, but _normalize_edit_op
    # INJECTS it on purpose (the C# validator reads `name`, the Python applier
    # reads `prop_name`, so both spellings are set). This check runs AFTER that
    # normalisation, so without this the caller gets an "unknown_op_field:
    # name" warning on every single call, for a field the service itself added.
    if verb in ("attach_expression", "attach_formatter", "attach_string_formatter"):
        legal = legal | {"name"}
    return sorted(k for k in op if k not in legal)


def bridge_validate_ops(
    cfg: Config, project: str, ops: list[dict], strict: bool = False
) -> dict:
    """Dry-run an op batch through the bridge's POST /bridge/validate_ops (U16).

    Returns the bridge's report — {ok, op_count, strict, errors, warnings} —
    without touching the model. Errors carry `op_index` so a caller can point at
    the offending op; an `unknown_property` error also carries the guard's
    `valid_properties` + `did_you_mean`.

    Raises BridgeUnavailable when the bridge isn't serving `project` OR when the
    endpoint is missing (an older bridge build answers the unknown route with
    `{"error":{"code":"not_found"}}`), so a caller can degrade rather than
    mistake "cannot validate" for "validated clean".
    """
    cfg = _bridge_write_guard(cfg, project)
    status, data = _bridge_post_body(
        cfg, "/bridge/validate_ops", {"ops": ops, "strict": bool(strict)}
    )
    err = data.get("error")
    if status != 200 or (isinstance(err, dict) and err.get("code") == "not_found"):
        raise BridgeUnavailable(
            "bridge /bridge/validate_ops is unavailable "
            f"(status={status}) — needs a bridge build carrying U16"
        )
    if isinstance(err, dict):
        # bad_body / bad_json / internal — a real answer, but not a report.
        raise BridgeWriteFailed(
            f"bridge validate_ops failed: {err.get('code')}: {err.get('message')}"
        )
    if "ok" not in data:
        raise BridgeWriteFailed(f"bridge validate_ops returned no report: {data}")
    return data


def _normalize_edit_op(op: dict) -> dict:
    """Reconcile the field-name seams between the per-noun bridge tools, the
    batch op-spec, and the C# validator, so an op composed with EITHER surface's
    naming applies cleanly. Three aliases; the first two from live-2026-07-25
    detours, the third from I32 (2026-09-03):

    * `node_path` -> `path`: the standalone tools name the target `node_path`
      (optix_bridge_set_property(node_path=...), optix_bridge_attach_expression,
      ...) but the batch ops AND the C# validator read `path`. An agent carrying
      the per-noun spelling into a batch had every such op rejected.
    * attach_expression `name` <-> `prop_name`: the C# validator (ValidateOnNode)
      keys the property on `name` (one shape with set_property/bind) while the
      Python applier reads `prop_name`. An op with only one spelling
      validates-but-can't-apply or vice versa (state="partial" mid-apply).
    * `type` -> `widget_type` (create_widget) / `object_type` (create_object):
      the raw HTTP routes and the converter that drives them spell the node
      type `type`; the batch op-spec spells it per-noun. Through bridge_edit
      the unknown `type` was only an `unknown_op_field` WARNING under the
      default non-strict mode, so it was DROPPED and the op's own default
      applied — a `Label` / bare `UAObject` under the right name in the right
      parent, reported `succeeded`. A wrong-typed node passes every subsequent
      model read; only a render reveals it. See I32 (and gap-register B4,
      which tabulated the field names but recorded the failure as a rejection
      — that holds only for REQUIRED fields). The alias is applied here and
      the `type` key is REMOVED from the returned op, so the unknown-field
      check downstream (which runs after this normalisation, see bridge_edit)
      stays quiet. Both spellings present and DIFFERING raises rather than
      picking one — there is no safe guess about which type the caller meant.

    Coalesce so both spellings are present; `name` wins for attach_expression.
    Returns the SAME object when nothing needs fixing (identity preserved), else
    a shallow copy — caller op dicts are never mutated. REGRESSION-CITE: keep
    these seams reconciled here (or in a bridge rebuild) — do not re-split the
    validator/applier/tool field names.

    Also lowers the `rename` sugar op into `move` (same parent + new_name) —
    the only safe rename mechanism; node attributes (DisplayName/BrowseName)
    are not writable (they crashed Studio — see _reject_node_attribute). The
    lowering happens BEFORE validation so the C# validator sees a verb it
    knows instead of warning unknown_op."""
    if not isinstance(op, dict):
        return op
    if op.get("op") == "rename":
        path = op.get("path") or op.get("node_path") or ""
        new_name = op.get("new_name") or op.get("name") or ""
        if not path or not new_name:
            raise BridgeWriteFailed(
                "op 'rename' requires path and new_name")
        if "/" not in path:
            raise BridgeWriteFailed(
                f"op 'rename' cannot rename top-level node {path!r} — no parent "
                f"to re-author under")
        parent, old_name = path.rsplit("/", 1)
        if new_name == old_name:
            raise BridgeWriteFailed(
                f"op 'rename': {path!r} is already named {new_name!r}")
        return {"op": "move", "path": path, "new_parent": parent,
                "new_name": new_name}
    needs_path = bool(op.get("node_path")) and not op.get("path")
    prop = None
    if op.get("op") in ("attach_expression", "attach_formatter", "attach_string_formatter"):
        prop = op.get("name") or op.get("prop_name")
        if prop and op.get("name") == prop and op.get("prop_name") == prop:
            prop = None  # already coalesced
    # I32: `type` is the raw-HTTP spelling of create_widget's
    # `widget_type` / create_object's `object_type`. Raise on a real conflict;
    # otherwise fold it into the canonical field and drop the key entirely.
    canon = _OP_TYPE_ALIAS.get(op.get("op"))
    drop_type = canon is not None and "type" in op
    if drop_type:
        alias_val, canon_val = op.get("type"), op.get(canon)
        if (alias_val not in (None, "") and canon_val not in (None, "")
                and alias_val != canon_val):
            raise BridgeWriteFailed(
                f"op {op.get('op')!r} carries both {canon}={canon_val!r} and "
                f"type={alias_val!r} — they name different node types and this "
                f"service will not guess which one you meant. Pass only "
                f"{canon} (the batch op field; `type` is the raw HTTP route's "
                f"spelling of the same thing)."
            )
    # `source` -> `sources` on the attach verbs, and a list `sources` joined to
    # the delimited string the bridge reads. Before this, `source` was only an
    # unknown_op_field WARNING and was dropped, so the formatter attached with
    # no Source0 and the batch reported `succeeded` (2026-09-24 live check).
    fix_sources = False
    if op.get("op") in _ATTACH_SOURCE_SEP:
        one, many = op.get("source"), op.get("sources")
        if "source" in op:
            if (one not in (None, "", []) and many not in (None, "", [])
                    and _coerce_sources(op["op"], one) != _coerce_sources(op["op"], many)):
                raise BridgeWriteFailed(
                    f"op {op.get('op')!r} carries both source={one!r} and "
                    f"sources={many!r}; the field is `sources` (plural) — pass only that")
            fix_sources = True
        elif many is not None and not isinstance(many, str):
            fix_sources = True
    if not needs_path and not prop and not drop_type and not fix_sources:
        return op
    out = dict(op)
    if fix_sources:
        one = out.pop("source", None)
        if out.get("sources") in (None, "", []):
            out["sources"] = one
        out["sources"] = _coerce_sources(out["op"], out["sources"])
    if needs_path:
        out["path"] = op["node_path"]
    if prop:
        out["name"] = out["prop_name"] = prop
    if drop_type:
        alias_val = out.pop("type")
        if alias_val not in (None, "") and out.get(canon) in (None, ""):
            out[canon] = alias_val
    return out


# batch_id becomes a filename under <state_dir>/batches, so it must never carry
# a separator, '..' or a drive prefix.
_BATCH_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _valid_batch_id(batch_id: object) -> bool:
    return isinstance(batch_id, str) and _BATCH_ID_RE.fullmatch(batch_id) is not None


def _write_batch_journal(cfg: Config, data: dict) -> None:
    """Write (or update) the per-batch journal to <state_dir>/batches/<batch_id>.json.

    Best-effort — all filesystem I/O is wrapped in a top-level try/except so a
    write failure never causes a correctly-applying batch to fail.  On each
    successful write the directory is scanned and journals whose file mtime is
    older than OPTIX_BATCH_JOURNAL_DAYS (default 7) days are pruned.
    """
    try:
        batch_id = data["batch_id"]
        if not _valid_batch_id(batch_id):
            return
        batches_dir = cfg.state_dir / "batches"
        batches_dir.mkdir(parents=True, exist_ok=True)
        journal_path = batches_dir / f"{batch_id}.json"
        journal_path.write_text(
            json.dumps(data, default=str), encoding="utf-8"
        )
        # Prune stale journals (best-effort — ignore individual errors).
        days = int(os.environ.get("OPTIX_BATCH_JOURNAL_DAYS", "7"))
        cutoff = time.time() - days * 86400
        for p in batches_dir.iterdir():
            try:
                if p.suffix == ".json" and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        pass


# Per-op outcome fields a caller cannot recover any other way (how a
# wire_event landed, which links were made type-relative, where a reorder
# ended up, what a bind cleared). Everything else in the per-noun result is
# already implied by the op itself.
_OP_DETAIL_KEYS = ("via", "relative_sources", "achieved", "prior binding cleared",
                   "placeholders", "bound", "namespace", "ui_namespace_child")


def _op_timing(i: int, op: dict, ms: int, res) -> dict:
    t = {"index": i, "op": op.get("op"), "ms": ms, "ok": True}
    if isinstance(res, dict):
        detail = {k: res[k] for k in _OP_DETAIL_KEYS if k in res}
        if detail:
            t["detail"] = detail
    return t


def _apply_one_edit(cfg: Config, project: str, op: dict) -> dict:
    """Dispatch ONE validated op to its per-noun bridge_* call."""
    verb = op.get("op")
    spec = _BRIDGE_EDIT_OPS.get(verb)
    if spec is None:
        raise BridgeWriteFailed(
            f"unknown op {verb!r}; valid ops: {', '.join(sorted(_BRIDGE_EDIT_OPS))}"
        )
    fname, required, optional = spec
    missing = [k for k in required if op.get(k) in (None, "")]
    if missing:
        raise BridgeWriteFailed(
            f"op {verb!r} is missing required field(s): {', '.join(missing)}"
        )
    fn = globals()[fname]
    args = [op[k] for k in _BRIDGE_EDIT_POSITIONAL[verb]]
    kwargs = {kw: op[k] for k, kw in optional.items() if op.get(k) is not None}
    return fn(cfg, project, *args, **kwargs)


def bridge_edit(
    cfg: Config, project: str, ops: list[dict],
    dry_run: bool = False, strict: bool = False,
    on_progress: Callable[[dict], None] | None = None,
    batch_id: str | None = None,
    chunk_index: int = 0,
) -> dict:
    """Validate an op batch, then apply it (U16) — the batched authoring path.

    Two phases. The bridge validates the WHOLE batch first, against a
    hypothetical model that accumulates the batch's own creates and deletes, so
    "create X then set X.Prop" passes and the reverse order is caught BEFORE
    anything is written. Only if the report is clean (and dry_run is False) are
    the ops applied, sequentially, each through the same per-noun bridge_* call
    its individual tool uses.

    Returns {state, applied, op_count, batch_id, report, ...}:
      * validation errors, or dry_run  -> state="validated", applied=0
      * all ops applied                -> state="succeeded", applied=len(ops)
      * a mid-batch apply failure       -> state="partial", applied=N,
                                           failed_op={index, op, error}
      * chunk boundary reached          -> state="chunked", applied=N,
                                           chunk_reason in {'time','ops'},
                                           remaining_ops=<count of unstarted ops>

    Per-op timings (A.1): ``validate_ms`` records the bridge_validate_ops
    round-trip; ``op_timings`` is a list of {index, op, ms, ok} dicts, one per
    applied op (omitted on dry_run or validation failure); ``apply_ms`` is the
    total wall-clock time for the apply loop.

    Progress callback (A.2): ``on_progress`` is called (best-effort, wrapped in
    try/except) once with {batch_id, index:0, total, op:None, phase:'validated'}
    before the apply loop, then once after each op with {batch_id, index, total,
    op, ms, applied}. At most one WARNING is logged per batch on the first
    callback exception.

    Batch journal (B.1): a JSON journal is written to
    <state_dir>/batches/<batch_id>.json before the first op and rewritten after
    each op (best-effort — write failures never abort a running batch). Schema:
    {batch_id, project, state, op_count, applied, started_at, updated_at, pid,
    chunk_index, op_timings, report, remaining_ops, failed_op?}.

    Chunking (B.2): the apply loop stops early and returns state='chunked' when
    OPTIX_BATCH_CHUNK_SECONDS (default 90) have elapsed since the loop started,
    or OPTIX_BATCH_CHUNK_OPS (default 40) ops have been applied and ops remain.
    Time is the primary boundary (checked first). Pass the returned batch_id to
    optix_bridge_edit(action='continue', batch_id=...) to process the next chunk.

    ATOMICITY IS NOT PROMISED, and the shape says so. The bridge mutates the
    live Studio model op by op; there is no transaction to roll back to, so a
    failure at op N leaves ops 0..N-1 applied. That is why validation is a
    separate pass — catching the batch up front is the real protection, and
    `state="partial"` reports honestly when it was not enough. Callers must not
    read a missing `failed_op` as "nothing was applied"; read `applied`.
    """
    # A.3: stable identifier for every terminal state, including dry_run /
    # validation-refused paths that never enter the apply loop.
    # Accept a caller-supplied batch_id to support chunk continuation.
    if not batch_id:
        batch_id = uuid.uuid4().hex[:12]
    elif not _valid_batch_id(batch_id):
        raise InvalidBatchId(f"invalid batch_id {batch_id!r}")

    if not isinstance(ops, list) or not ops:
        raise BridgeWriteFailed("bridge_edit requires a non-empty list of ops")

    # Reconcile attach_expression's name/prop_name across the validator/applier
    # seam BEFORE both phases (see _normalize_edit_op). Never mutates caller ops.
    ops = [_normalize_edit_op(op) for op in ops]
    # Unknown-field check BEFORE the bridge round-trip. The C# validator checks
    # op semantics against the model; it does not police the op's own field
    # names, so a typo used to sail through as `applied: 1 succeeded`. Under
    # strict this is an error (refuse the batch); otherwise a warning, so
    # existing callers that pass harmless extras are not broken outright.
    #
    # EXCEPTION: destructive ops (_DESTRUCTIVE_OP_VERBS — delete/move/reorder)
    # hard-fail on an unknown field UNCONDITIONALLY, strict or not. There is no
    # scoped/partial form of these ops, so an unrecognized field is likely a
    # caller trying to narrow the blast radius (e.g. delete's `name`) and
    # instead silently getting the whole node at `path`. Warn-and-proceed is
    # not an acceptable default for an op with no undo. See
    # _DESTRUCTIVE_OP_VERBS's docstring for the incident that motivated this.
    #
    # Separately (I32): `create_widget` with no type at all defaults to a
    # `Label`. That default is what turned a dropped `type` field into an
    # invisible wrong-typed node — it succeeded, was named right, sat in the
    # right parent, and only a render showed it. The alias above closes the
    # dropped-field route; this warning makes the remaining silent default
    # visible in the report rather than only in the rendered screen.
    field_errs: list[dict] = []
    destructive_field_errs: list[dict] = []
    default_warns: list[dict] = []
    for i, op in enumerate(ops):
        if op.get("op") == "create_widget" and not op.get("widget_type"):
            default_warns.append({
                "op_index": i, "code": "default_widget_type",
                "message": ("op 'create_widget' carries no widget_type — the "
                            "bridge will create a 'Label'. The batch field is "
                            "`widget_type`; the raw HTTP routes spell it "
                            "`type` (that "
                            "spelling is accepted here and normalised)."),
                "default": "Label",
            })
        if (op.get("op") == "create_widget"
                and str(op.get("widget_type") or "").strip().lower() == "gridlayout"):
            # Rows/Columns are String[] and not settable over the bridge, and a
            # GridLayout without them in a rendered window blanks the WHOLE web
            # page (troubleshooting.md, 2026-09-05; re-hit in the 1.0.8 battle
            # test). The runtime log only says "Skipping layout".
            default_warns.append({
                "op_index": i, "code": "gridlayout_without_columns",
                "message": ("GridLayout Rows/Columns cannot be set over the bridge; "
                            "until they are defined in Studio, a GridLayout in a "
                            "rendered window blanks the entire web page. Prefer "
                            "RowLayout/ColumnLayout, or create it under a non-"
                            "rendered folder such as UI/Screens."),
            })
        if op.get("op") == "attach_string_formatter":
            missing = unbound_placeholders(op.get("format") or "", op.get("sources"))
            if missing:
                # Always an error, strict or not: the op would "succeed" and
                # render the bare format text — a silent wrong result.
                destructive_field_errs.append({
                    "op_index": i, "code": "unbound_placeholder",
                    "message": (f"format {op.get('format')!r} uses "
                                f"{', '.join('{%d}' % n for n in missing)} with no "
                                "source bound; pass `sources` (plural, positional)"),
                    "unbound": missing,
                })
        unknown = unknown_op_fields(op)
        if not unknown:
            continue
        verb = op.get("op")
        if verb in _DESTRUCTIVE_OP_VERBS:
            destructive_field_errs.append({
                "op_index": i, "code": "unknown_op_field",
                "message": (f"op {verb!r} (destructive, no scoped form) has no "
                            f"field(s) {', '.join(repr(f) for f in unknown)} — "
                            "refusing rather than silently widening this to a "
                            "whole-node op at `path`"),
                "unknown_fields": unknown,
            })
        else:
            field_errs.append({
                "op_index": i, "code": "unknown_op_field",
                "message": (f"op {verb!r} has no field(s) "
                            f"{', '.join(repr(f) for f in unknown)} — they would be "
                            f"silently ignored and the op applied anyway"),
                "unknown_fields": unknown,
            })
    # A.1: time the bridge_validate_ops round-trip.
    _validate_t0 = time.monotonic()
    # Shallow-copy: this dict may be a caller/test fixture or otherwise shared;
    # mutating it in place (below) would corrupt state the caller still holds
    # a reference to. Cheap and correct regardless of where it came from.
    report = dict(bridge_validate_ops(cfg, project, ops, strict=strict))
    validate_ms: int = int((time.monotonic() - _validate_t0) * 1000)
    if destructive_field_errs:
        report["errors"] = list(report.get("errors") or []) + destructive_field_errs
        report["ok"] = False
    if field_errs:
        if strict:
            report["errors"] = list(report.get("errors") or []) + field_errs
            report["ok"] = False
        else:
            report["warnings"] = list(report.get("warnings") or []) + field_errs
    if default_warns:
        # Always a warning, strict or not: a default `Label` is legitimate for
        # a caller who wants one, so this must not refuse an otherwise-clean
        # batch. It exists so the default is *visible*.
        report["warnings"] = list(report.get("warnings") or []) + default_warns
    # Augment unknown_property/'Value' errors with the pre-1.0.8 spelling nudge.
    # The C# validator rejects ``name='Value'`` on IUAVariable nodes (the applier
    # handles it correctly since 1.0.5); append the parent/name workaround so the
    # LLM caller sees the fix without a separate describe_type round-trip.
    _raw_errs = report.get("errors") or []
    _augmented = []
    _value_nudge_needed = False
    for _err in _raw_errs:
        if (
            _err.get("code") == "unknown_property"
            and "has no settable property 'Value'" in (_err.get("message") or "")
        ):
            _idx = _err.get("op_index")
            if _idx is not None and 0 <= _idx < len(ops):
                _op = ops[_idx]
                _op_name = _op.get("name") or _op.get("prop_name") or ""
                _node_path = _op.get("path") or ""
                if _op_name == "Value" and _node_path:
                    _err = dict(_err)
                    _err["message"] = (
                        _err["message"] + _value_prop_nudge_suffix(_node_path)
                    )
                    _value_nudge_needed = True
        _augmented.append(_err)
    if _value_nudge_needed:
        report["errors"] = _augmented
    out: dict = {
        "batch_id": batch_id,
        "op_count": len(ops),
        "applied": 0,
        "report": report,
        "dry_run": bool(dry_run),
        "validate_ms": validate_ms,
    }
    if not report.get("ok") or dry_run:
        out["state"] = "validated"
        if not report.get("ok"):
            out["nudge"] = (
                "validation refused the batch; nothing was applied. Each error "
                "carries op_index — fix those ops and retry."
            )
        # op_timings omitted: nothing was applied (dry_run or validation failure)
        return out

    audit(cfg, "bridge_edit", project=project, ops=len(ops))
    # A.2: emit the pre-loop progress event (phase='validated').
    _progress_warn_logged = False
    n = len(ops)
    if on_progress is not None:
        try:
            on_progress({
                "batch_id": batch_id,
                "index": 0,
                "total": n,
                "op": None,
                "phase": "validated",
            })
        except Exception as _cb_exc:
            _log.warning(
                "bridge_edit on_progress callback raised (batch_id=%s): %s",
                batch_id, _cb_exc,
            )
            _progress_warn_logged = True

    # A.1: per-op timings collected here; apply_ms covers the whole loop.
    # B.1: record apply start time for the journal; B.2: chunk deadline uses it.
    op_timings: list[dict] = []
    _apply_t0 = time.monotonic()
    _started_at = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")

    # B.1: write the initial journal entry (state='applying') before the first op.
    # All calls to _write_batch_journal are wrapped in try/except: journal I/O
    # is best-effort and must never abort a running batch.
    try:
        _write_batch_journal(cfg, {
            "batch_id": batch_id,
            "project": project,
            "state": "applying",
            "op_count": n,
            "applied": 0,
            "started_at": _started_at,
            "updated_at": _started_at,
            "pid": os.getpid(),
            "chunk_index": chunk_index,
            "op_timings": [],
            "report": report,
            "remaining_ops": ops,
        })
    except Exception:
        pass

    # B.2: read chunk thresholds once per call (env vars; monkeypatchable).
    _chunk_seconds = float(os.environ.get("OPTIX_BATCH_CHUNK_SECONDS", "90"))
    _chunk_ops = int(os.environ.get("OPTIX_BATCH_CHUNK_OPS", "40"))

    for i, op in enumerate(ops):
        _op_t0 = time.monotonic()
        try:
            _res = _apply_one_edit(cfg, project, op)
            op_ms = int((time.monotonic() - _op_t0) * 1000)
            op_timings.append(_op_timing(i, op, op_ms, _res))
            out["applied"] = i + 1
            # A.2: emit post-op progress event.
            if on_progress is not None:
                try:
                    on_progress({
                        "batch_id": batch_id,
                        "index": i + 1,
                        "total": n,
                        "op": op.get("op"),
                        "ms": op_ms,
                        "applied": i + 1,
                    })
                except Exception as _cb_exc:
                    if not _progress_warn_logged:
                        _log.warning(
                            "bridge_edit on_progress callback raised (batch_id=%s): %s",
                            batch_id, _cb_exc,
                        )
                        _progress_warn_logged = True
            # B.1: rewrite the journal after each successful op.
            _remaining_ops = ops[i + 1:]
            _now_iso = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
            try:
                _write_batch_journal(cfg, {
                    "batch_id": batch_id,
                    "project": project,
                    "state": "applying",
                    "op_count": n,
                    "applied": i + 1,
                    "started_at": _started_at,
                    "updated_at": _now_iso,
                    "pid": os.getpid(),
                    "chunk_index": chunk_index,
                    "op_timings": op_timings,
                    "report": report,
                    "remaining_ops": _remaining_ops,
                })
            except Exception:
                pass
            # B.2: check chunk boundary — only when ops remain.
            if _remaining_ops:
                _elapsed = time.monotonic() - _apply_t0
                _chunk_reason: str | None = None
                if _elapsed >= _chunk_seconds:
                    _chunk_reason = "time"
                elif (i + 1) >= _chunk_ops:
                    _chunk_reason = "ops"
                if _chunk_reason:
                    out["apply_ms"] = int((time.monotonic() - _apply_t0) * 1000)
                    out["op_timings"] = op_timings
                    out["state"] = "chunked"
                    out["chunk_reason"] = _chunk_reason
                    out["chunk_index"] = chunk_index
                    out["remaining_ops"] = len(_remaining_ops)
                    out["nudge"] = (
                        f"Batch paused after {i + 1} op(s) "
                        f"(chunk_reason={_chunk_reason!r}); "
                        f"{len(_remaining_ops)} op(s) remain. "
                        f"Continue with: "
                        f"optix_bridge_edit(action='continue', "
                        f"batch_id={batch_id!r})"
                    )
                    try:
                        _write_batch_journal(cfg, {
                            "batch_id": batch_id,
                            "project": project,
                            "state": "chunked",
                            "op_count": n,
                            "applied": i + 1,
                            "started_at": _started_at,
                            "updated_at": _now_iso,
                            "pid": os.getpid(),
                            "chunk_index": chunk_index,
                            "op_timings": op_timings,
                            "report": report,
                            "remaining_ops": _remaining_ops,
                        })
                    except Exception:
                        pass
                    return out
        except Exception as exc:
            op_ms = int((time.monotonic() - _op_t0) * 1000)
            op_timings.append({"index": i, "op": op.get("op"), "ms": op_ms, "ok": False})
            out["apply_ms"] = int((time.monotonic() - _apply_t0) * 1000)
            out["op_timings"] = op_timings
            out["state"] = "partial"
            out["failed_op"] = {
                "index": i, "op": op.get("op"), "error": str(exc),
            }
            out["nudge"] = (
                f"op {i} failed AFTER {out['applied']} op(s) had already been "
                "applied. This path is not atomic — the live model now holds the "
                "earlier ops. Inspect with optix_describe_node before retrying, "
                "and retry only the remaining ops."
            )
            # B.1: write failed state to the journal.
            try:
                _write_batch_journal(cfg, {
                    "batch_id": batch_id,
                    "project": project,
                    "state": "partial",
                    "op_count": n,
                    "applied": out["applied"],
                    "started_at": _started_at,
                    "updated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
                    "pid": os.getpid(),
                    "chunk_index": chunk_index,
                    "op_timings": op_timings,
                    "failed_op": out["failed_op"],
                    "report": report,
                    "remaining_ops": ops[i:],
                })
            except Exception:
                pass
            return out
    out["apply_ms"] = int((time.monotonic() - _apply_t0) * 1000)
    out["op_timings"] = op_timings
    out["state"] = "succeeded"
    # B.1: write final succeeded state to the journal.
    try:
        _write_batch_journal(cfg, {
            "batch_id": batch_id,
            "project": project,
            "state": "succeeded",
            "op_count": n,
            "applied": n,
            "started_at": _started_at,
            "updated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "chunk_index": chunk_index,
            "op_timings": op_timings,
            "report": report,
            "remaining_ops": [],
        })
    except Exception:
        pass
    return out


def _load_batch_journal(cfg: Config, batch_id: str) -> dict | None:
    """Load a batch journal from <state_dir>/batches/<batch_id>.json.

    Returns the parsed dict, or None if the file does not exist or cannot be
    parsed. Never raises.
    """
    if not _valid_batch_id(batch_id):
        return None
    try:
        journal_path = cfg.state_dir / "batches" / f"{batch_id}.json"
        return json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def bridge_edit_continue(cfg: Config, project: str, batch_id: str) -> dict:
    """Continue applying the next chunk of a paused (chunked) batch (B.3).

    Loads the journal for batch_id, applies the next chunk of remaining_ops
    using the same time/op-count thresholds as bridge_edit, and rewrites the
    journal. Validation is never repeated — the original report from the
    initial bridge_edit call is echoed unchanged in the returned dict.

    Returns the same output shape as bridge_edit: {state, applied, op_count,
    batch_id, report, op_timings, chunk_index, and when chunked:
    chunk_reason, remaining_ops, nudge}.

    Guard rules (all refuse without applying any ops):
      - batch_project_mismatch: journal project != project argument.
      - state='abandoned': journal state is 'applying' and its pid is dead —
        model may have diverged; use optix_describe_node.
      - batch_not_resumable: state in {succeeded, partial, validated} or any
        non-chunked terminal state; a partial batch is never auto-continued.

    Raises BridgeWriteFailed if the journal cannot be found.
    """
    if not _valid_batch_id(batch_id):
        raise InvalidBatchId(f"invalid batch_id {batch_id!r}")
    journal = _load_batch_journal(cfg, batch_id)
    if journal is None:
        raise BridgeWriteFailed(
            f"no journal found for batch_id={batch_id!r}; "
            "batch may have expired or was never started"
        )

    batch_state = journal.get("state")
    journal_project = journal.get("project")

    # Rule 3: project mismatch — refuse before touching anything.
    if journal_project != project:
        return {
            "state": "failed",
            "error": "batch_project_mismatch",
            "batch_id": batch_id,
            "batch_state": batch_state,
            "message": (
                f"batch {batch_id!r} was journalled for project "
                f"{journal_project!r}, not {project!r}"
            ),
        }

    # Rule 4: applying with dead pid → abandoned (diverged model state).
    if batch_state == "applying":
        pid = journal.get("pid")
        pid_alive = False
        if pid is not None:
            try:
                pid_alive = psutil.pid_exists(int(pid))
            except (ValueError, OSError):
                pid_alive = False
        if not pid_alive:
            return {
                "state": "abandoned",
                "error": "batch_abandoned",
                "batch_id": batch_id,
                "batch_state": "applying",
                "nudge": (
                    "The batch was mid-apply when its process died — the live "
                    "model may have diverged. Inspect with optix_describe_node "
                    "before re-authoring, and retry only the remaining ops."
                ),
            }

    # Rule 2: only 'chunked' is resumable; all other states refuse.
    if batch_state != "chunked":
        out: dict = {
            "state": "failed",
            "error": "batch_not_resumable",
            "batch_id": batch_id,
            "batch_state": batch_state,
        }
        if batch_state == "partial":
            out["nudge"] = (
                "A partial batch is never auto-continued — the live model may "
                "have diverged. Inspect with optix_describe_node before "
                "re-authoring, and retry only the remaining ops."
            )
        return out

    # State is 'chunked': load remaining ops and apply the next chunk.
    ops: list[dict] = list(journal.get("remaining_ops") or [])
    report: dict = dict(journal.get("report") or {})
    chunk_index: int = int(journal.get("chunk_index", 0)) + 1
    op_count: int = int(journal.get("op_count", 0))
    prior_applied: int = int(journal.get("applied", 0))
    started_at: str = str(journal.get("started_at", ""))

    out = {
        "batch_id": batch_id,
        "op_count": op_count,
        "applied": prior_applied,
        "report": report,
    }

    if not ops:
        # No remaining ops despite 'chunked' state — treat as succeeded.
        out["state"] = "succeeded"
        out["apply_ms"] = 0
        out["op_timings"] = []
        out["chunk_index"] = chunk_index - 1
        return out

    # Apply loop — same logic as bridge_edit's apply phase, without re-validation.
    op_timings: list[dict] = []
    _apply_t0 = time.monotonic()
    _now_iso = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")

    # B.1: write initial 'applying' state for this chunk before the first op.
    try:
        _write_batch_journal(cfg, {
            "batch_id": batch_id,
            "project": project,
            "state": "applying",
            "op_count": op_count,
            "applied": prior_applied,
            "started_at": started_at,
            "updated_at": _now_iso,
            "pid": os.getpid(),
            "chunk_index": chunk_index,
            "op_timings": op_timings,
            "report": report,
            "remaining_ops": ops,
        })
    except Exception:
        pass

    # B.2: chunk thresholds — same env vars as bridge_edit.
    _chunk_seconds = float(os.environ.get("OPTIX_BATCH_CHUNK_SECONDS", "90"))
    _chunk_ops = int(os.environ.get("OPTIX_BATCH_CHUNK_OPS", "40"))

    for i, op in enumerate(ops):
        _op_t0 = time.monotonic()
        try:
            _res = _apply_one_edit(cfg, project, op)
            op_ms = int((time.monotonic() - _op_t0) * 1000)
            op_timings.append(_op_timing(i, op, op_ms, _res))
            out["applied"] = prior_applied + (i + 1)
            _remaining_ops = ops[i + 1:]
            _now_iso = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
            # B.1: rewrite journal after each successful op.
            try:
                _write_batch_journal(cfg, {
                    "batch_id": batch_id,
                    "project": project,
                    "state": "applying",
                    "op_count": op_count,
                    "applied": out["applied"],
                    "started_at": started_at,
                    "updated_at": _now_iso,
                    "pid": os.getpid(),
                    "chunk_index": chunk_index,
                    "op_timings": op_timings,
                    "report": report,
                    "remaining_ops": _remaining_ops,
                })
            except Exception:
                pass
            # B.2: check chunk boundary — only when ops remain.
            if _remaining_ops:
                _elapsed = time.monotonic() - _apply_t0
                _chunk_reason: str | None = None
                if _elapsed >= _chunk_seconds:
                    _chunk_reason = "time"
                elif (i + 1) >= _chunk_ops:
                    _chunk_reason = "ops"
                if _chunk_reason:
                    out["apply_ms"] = int((time.monotonic() - _apply_t0) * 1000)
                    out["op_timings"] = op_timings
                    out["state"] = "chunked"
                    out["chunk_reason"] = _chunk_reason
                    out["chunk_index"] = chunk_index
                    out["remaining_ops"] = len(_remaining_ops)
                    out["nudge"] = (
                        f"Batch paused after {out['applied']} op(s) "
                        f"(chunk_reason={_chunk_reason!r}); "
                        f"{len(_remaining_ops)} op(s) remain. "
                        f"Continue with: "
                        f"optix_bridge_edit(action='continue', "
                        f"batch_id={batch_id!r})"
                    )
                    try:
                        _write_batch_journal(cfg, {
                            "batch_id": batch_id,
                            "project": project,
                            "state": "chunked",
                            "op_count": op_count,
                            "applied": out["applied"],
                            "started_at": started_at,
                            "updated_at": _now_iso,
                            "pid": os.getpid(),
                            "chunk_index": chunk_index,
                            "op_timings": op_timings,
                            "report": report,
                            "remaining_ops": _remaining_ops,
                        })
                    except Exception:
                        pass
                    return out
        except Exception as exc:
            op_ms = int((time.monotonic() - _op_t0) * 1000)
            op_timings.append({"index": i, "op": op.get("op"), "ms": op_ms, "ok": False})
            out["apply_ms"] = int((time.monotonic() - _apply_t0) * 1000)
            out["op_timings"] = op_timings
            out["state"] = "partial"
            out["failed_op"] = {
                "index": i, "op": op.get("op"), "error": str(exc),
            }
            out["nudge"] = (
                f"op {i} failed AFTER {out['applied']} op(s) had already been "
                "applied. This path is not atomic — the live model now holds the "
                "earlier ops. Inspect with optix_describe_node before retrying, "
                "and retry only the remaining ops."
            )
            try:
                _write_batch_journal(cfg, {
                    "batch_id": batch_id,
                    "project": project,
                    "state": "partial",
                    "op_count": op_count,
                    "applied": out["applied"],
                    "started_at": started_at,
                    "updated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
                    "pid": os.getpid(),
                    "chunk_index": chunk_index,
                    "op_timings": op_timings,
                    "failed_op": out["failed_op"],
                    "report": report,
                    "remaining_ops": ops[i:],
                })
            except Exception:
                pass
            return out

    out["apply_ms"] = int((time.monotonic() - _apply_t0) * 1000)
    out["op_timings"] = op_timings
    out["state"] = "succeeded"
    out["chunk_index"] = chunk_index
    # B.1: write final succeeded state to the journal.
    try:
        _write_batch_journal(cfg, {
            "batch_id": batch_id,
            "project": project,
            "state": "succeeded",
            "op_count": op_count,
            "applied": out["applied"],
            "started_at": started_at,
            "updated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "chunk_index": chunk_index,
            "op_timings": op_timings,
            "report": report,
            "remaining_ops": [],
        })
    except Exception:
        pass
    return out


def bridge_edit_status(
    cfg: Config,
    project: str,
    batch_id: str | None = None,
) -> dict:
    """Return status for a batch edit or a recent-batch listing (B.4).

    With ``batch_id``:
        Return that journal document from ``<state_dir>/batches/<batch_id>.json``.
        If the id is not found, return a structured dict::

            {"state": "failed", "error": "unknown_batch",
             "batch_id": <id>, "known": [<batch_id>, ...]}

        where ``known`` lists the most-recent batch IDs for *project*
        (newest-first).  This is a RETURN VALUE, never a raised exception —
        ``_bridge_guarded`` (``service/mcp_app.py``) only converts
        ``BridgeUnavailable`` / ``BridgeWriteFailed``, so a raised exception
        would escape as a raw traceback.

    Without ``batch_id``:
        Return the most recent ``OPTIX_BATCH_JOURNAL_N`` (default 5) journals
        for *project*, newest-first, as ``{"batches": [...], "project": ...}``.
        A different project's batches are never included.

    The no-id form is the load-bearing recovery path for a client that timed
    out before seeing the batch_id.
    """
    batches_dir = cfg.state_dir / "batches"

    def _journals_for_project(*, limit: int | None = None) -> list[dict]:
        """Return journal dicts for *project*, sorted newest-first by mtime."""
        if not batches_dir.is_dir():
            return []
        entries: list[tuple[float, dict]] = []
        for p in batches_dir.iterdir():
            if p.suffix != ".json":
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("project") == project:
                    entries.append((p.stat().st_mtime, data))
            except Exception:
                pass
        entries.sort(key=lambda t: t[0], reverse=True)
        if limit is not None:
            entries = entries[:limit]
        return [data for _, data in entries]

    if batch_id is not None:
        if not _valid_batch_id(batch_id):
            return {
                "state": "failed",
                "error": "invalid_batch_id",
                "batch_id": batch_id,
                "message": "batch_id must be letters, digits, '_' or '-' (max 64)",
            }
        journal_path = batches_dir / f"{batch_id}.json"
        try:
            if journal_path.exists():
                return json.loads(journal_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        # Unknown or unreadable — return structured error, never raise.
        known = [d.get("batch_id", "") for d in _journals_for_project()]
        return {
            "state": "failed",
            "error": "unknown_batch",
            "batch_id": batch_id,
            "known": known,
        }

    # No batch_id: return the most-recent N journals for this project.
    max_n = int(os.environ.get("OPTIX_BATCH_JOURNAL_N", "5"))
    return {
        "batches": _journals_for_project(limit=max_n),
        "project": project,
    }


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
        # multi-instance (v1.0.7) — `bridges` is the full list of
        # everything currently armed (one entry per port answering), so the
        # dashboard can show every open project instead of assuming there's
        # only one. `out["bridge"]` stays as the PRIMARY (first-in-port-order)
        # single-bridge summary for back-compat with the pre-1.0.7 dashboard
        # fields — with more than one armed, prefer `bridges` for the full
        # picture; `bridge` alone doesn't say which project it's describing.
        # scan ONCE via _scan_bridge_ports() and derive
        # both `bridges` (armed only) and `sockets` (every configured port,
        # armed or not — so the dashboard can show "4 configured, 1 armed"
        # instead of just the 1) from the same results, instead of two
        # separate range scans (list_bridges() + a second one for the
        # primary-bridge fallback).
        sockets = _scan_bridge_ports(cfg)
        bridges = [s for s in sockets if s.get("available")]
        for b in bridges:
            proj = b.get("project")
            if proj:
                try:
                    b["last_saved_epoch"] = _project_max_mtime(resolve_project(cfg, proj))
                except Exception:
                    pass
        out["bridges"] = bridges
        out["sockets"] = sockets
        if bridges:
            st = bridges[0]
        elif sockets:
            st = sockets[0]
        else:
            st = {"available": False, "project": None, "bridge_version": None,
                  "port": None, "reason": "disabled" if not cfg.bridge_enabled else "no ports configured"}
        out["bridge"] = {"reachable": bool(st.get("available")),
                         "version": st.get("bridge_version"),
                         "project": st.get("project"),
                         "port": st.get("port"),
                         "model_loaded": st.get("model_loaded", st.get("available")),
                         "last_saved_epoch": st.get("last_saved_epoch")}
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
        # The widget-type catalog is identical across every armed bridge (it's
        # Studio's own type system, not per-project) — the primary bridge's
        # port is a fine source for it, no need to ask each one.
        primary_port = out.get("bridge", {}).get("port")
        # skip this call entirely when NOTHING is
        # armed anywhere -- we already know from the `bridges` scan above
        # that there's no listener to ask, so a call here (previously it
        # fell back to cfg's default single bridge_url and asked anyway)
        # was a guaranteed-to-fail, guaranteed-to-cost-a-real-refused-
        # connection round trip on every single /ui/stats poll.
        if primary_port:
            bcfg = dataclasses.replace(cfg, bridge_url=_bridge_url_at(primary_port))
            status, data = _bridge_get_json(bcfg, "/bridge/types/ui")
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


def _bridge_ports(cfg: Config) -> list[int]:
    """Candidate bridge ports to probe, in order.

    a single port when cfg.bridge_url_pinned (OPTIX_BRIDGE_URL was
    set explicitly — the documented legacy escape hatch for a rebound bridge
    port), else the whole configured range [bridge_port_base,
    bridge_port_base + bridge_port_range).
    """
    if cfg.bridge_url_pinned:
        from urllib.parse import urlparse
        return [urlparse(cfg.bridge_url).port or cfg.bridge_port_base]
    return list(range(cfg.bridge_port_base, cfg.bridge_port_base + cfg.bridge_port_range))


def _bridge_url_at(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _bridge_registry_dir() -> "Path | None":
    """The directory where the C# bridge writes per-port JSON registry files.

    ``%LOCALAPPDATA%\\ftx-mcp\\bridges\\`` — the bridge writes ``<port>.json``
    on successful bind and deletes it on stop or crash. Returns the Path when
    it exists (the caller checks for *.json files), else None.
    """
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(local) / "ftx-mcp" / "bridges"
    return d if d.is_dir() else None


def _bridge_registry_entries() -> "list[tuple[int, Path]] | None":
    """Ports listed in the bridge registry, paired with their JSON file paths.

    Returns None when the registry directory is absent or contains no ``*.json``
    files — the caller should fall through to the legacy port-range scan.
    Each file is named ``<port>.json``; the port is parsed from the filename
    (the file content is not read — the /bridge/health call is the authority).
    """
    d = _bridge_registry_dir()
    if d is None:
        return None
    entries: list[tuple[int, Path]] = []
    for f in sorted(d.glob("*.json")):
        try:
            port = int(f.stem)
        except ValueError:
            continue
        entries.append((port, f))
    return entries if entries else None


def _is_connection_refused(exc: BaseException | None) -> bool:
    """True when `exc` (typically a caught BridgeUnavailable's __cause__) is,
    or wraps, a ConnectionRefusedError -- i.e. the OS gave a definitive "no
    -one is listening on that port" answer, as opposed to a timeout or some
    other transport failure.

    (v1.0.8) `_bridge_http` raises `BridgeUnavailable(...) from
    e` where `e` is the `urllib.error.URLError`/`OSError` it caught -- NOT
    the raw socket exception directly. `urllib.request.urlopen` wraps a bare
    `ConnectionRefusedError` in a `URLError`, stashing the original exception
    in `.reason` rather than re-raising it, so `isinstance(cause,
    ConnectionRefusedError)` is always False for a refused connection; only
    `isinstance(cause.reason, ConnectionRefusedError)` catches it. Walk both
    shapes so the caller's fast-path (skip the retry-with-sleep loop for a
    definitively dead port) actually fires.
    """
    if isinstance(exc, ConnectionRefusedError):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, ConnectionRefusedError)


def _bridge_health_at(cfg: Config, port: int, force: bool = False) -> dict:
    """Cached /bridge/health snapshot for ONE specific port.

    Returns {available, project, bridge_version, port, reason}. `available` is
    True only when the bridge is enabled, answers /bridge/health 200, AND
    reports model_loaded. Cached ~2s per port (reads arrive in bursts). Never
    raises — an unreachable bridge is a normal "unavailable", not an error.

    this is the per-port building block bridge_state /
    list_bridges / _find_bridge_for are all built on — see the module-level
    _bridge_cache comment for why the cache moved from one global snapshot to
    one per port.
    """
    global _bridge_cache, _bridge_cache_at
    url = _bridge_url_at(port)
    now = time.time()
    cached_at = _bridge_cache_at.get(url, 0.0)
    if not force and url in _bridge_cache and (now - cached_at) < _BRIDGE_CACHE_TTL:
        return _bridge_cache[url]
    if not cfg.bridge_enabled:
        state = {"available": False, "responded": False, "project": None,
                  "bridge_version": None, "port": port, "project_path": None,
                  "pid": None, "reason": "disabled"}
    else:
        # a bare-connect/immediate-close TCP pre-check
        # used to run here before the HTTP health probe, to skip the slower
        # retry-loop below for ports that obviously have nothing listening.
        # Removed: against a REAL listener (the C# bridge's single-threaded
        # blocking TcpListener), a data-less probe that connects then closes
        # can race the server's own accept/read/write cycle, so
        # AcceptTcpClient()/NetworkStream reads or writes on that connection
        # throw "An established connection was aborted by the software in
        # your host machine" — logged by Loop()'s catch as a StudioBridge
        # "request error" Warning. With a 2s cache TTL matched to the
        # dashboard's 2s poll interval, this fired continuously against the
        # live, armed bridge on every cache-miss scan. The HTTP retry-loop
        # below is the only check now, for every port regardless of pinning;
        # it costs a bit more on cold scans of genuinely-dead ports but never
        # touches a live listener's socket without sending a real request.
        bcfg = dataclasses.replace(cfg, bridge_url=url)
        # The single-threaded listener can briefly stop accepting connections while
        # Studio does heavy designer work (e.g. materializing a ScreenType), so a
        # lone health probe can TIME OUT even though the bridge is fine and every
        # operational endpoint still works. Retry only the
        # TRANSPORT-failure path a few times with a short timeout so a transient block
        # isn't cached as "down". A well-formed HTTP response (even model_loaded=False)
        # means the listener is up -> decide immediately, no retry.
        state = {"available": False, "responded": False, "project": None,
                  "bridge_version": None, "port": port, "project_path": None,
                  "pid": None, "reason": "unreachable"}
        for i in range(3):
            try:
                # retries=0: this loop already retries the transport-failure path.
                status, data = _bridge_get_json(bcfg, "/bridge/health", timeout=2.5, retries=0)
                if status == 200 and data.get("model_loaded"):
                    state = {
                        "available": True,
                        "responded": True,
                        "project": data.get("project"),
                        "bridge_version": data.get("bridge_version"),
                        "port": data.get("port", port),
                        "project_path": data.get("project_path"),
                        "pid": data.get("pid"),
                        "reason": "ok",
                    }
                else:
                    state = {
                        "available": False,
                        "responded": True,
                        "project": data.get("project"),
                        "bridge_version": data.get("bridge_version"),
                        "port": data.get("port", port),
                        "project_path": data.get("project_path"),
                        "pid": data.get("pid"),
                        "reason": f"health status={status} model_loaded={data.get('model_loaded')}",
                    }
                break  # got a response -> listener is up, don't retry
            except BridgeUnavailable as e:
                refused = _is_connection_refused(e.__cause__)
                state = {"available": False, "responded": False, "refused": refused,
                          "project": None, "bridge_version": None, "port": port,
                          "project_path": None, "pid": None, "reason": str(e)}
                # connection *refused* means nothing is
                # listening on this port at all -- a definitive answer, not a
                # transient block, so retrying just adds latency scanning a
                # range where most ports are typically unarmed. Only sleep
                # and retry for other transport failures (e.g. a timeout),
                # which is what the retry loop exists for.
                if refused:
                    break
                if i < 2:
                    time.sleep(0.4)
    state["last_ok"] = _bridge_last_ok_at
    _bridge_cache[url] = state
    _bridge_cache_at[url] = now
    return state


def _scan_bridge_ports(cfg: Config, force: bool = False) -> list[dict]:
    """Every configured port's health snapshot, in port order — armed AND
    unarmed alike (unlike list_bridges(), which filters to available=True).

    Factored out of list_bridges() so a caller that
    wants the FULL picture (e.g. ui_stats()'s `sockets` field, so the /ui
    dashboard can show "4 configured, 1 armed" instead of just the 1) can get
    it from the SAME scan list_bridges() already does, instead of a second
    full range scan.

    Registry + range: the C# bridge writes
    ``%LOCALAPPDATA%\\ftx-mcp\\bridges\\<port>.json`` on successful bind. Every
    registry port AND every configured range port is probed in ONE concurrent
    pass (so the cost is one probe, not one per port), in port order. The
    registry is NOT authoritative on its own: a bridge older than 1.0.8 writes
    no registry file, and right after an upgrade most projects still run one,
    so trusting the registry alone would hide them. What the registry adds is
    ports outside the configured range and stale-entry cleanup: a registry
    file whose port answers connection-refused is deleted. Each result dict
    gains a ``source`` field: ``"registry"`` or ``"scan"``.
    """
    import concurrent.futures

    registry = _bridge_registry_entries() or []
    port_to_file: dict[int, Path] = {p: f for p, f in registry}
    ports = sorted(set(_bridge_ports(cfg)) | set(port_to_file))
    if not ports:
        return []

    def _probe(port: int) -> dict:
        st = dict(_bridge_health_at(cfg, port, force=force))
        f = port_to_file.get(port)
        st["source"] = "registry" if f is not None else "scan"
        # Only remove a registry file on a definitive connection-refused
        # answer. A timeout (responded=False, refused=False) means Studio is
        # alive but busy (e.g. loading a model); keep the entry.
        if f is not None and st.get("refused"):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        return st

    if len(ports) == 1:
        return [_probe(ports[0])]
    # Concurrent: a refused connection is not always near-instant (observed
    # ~2s per refusal where endpoint security intercepts loopback TCP), so a
    # sequential scan of an otherwise-empty range took 30+ s. map() keeps
    # input (port) order, which bridge_state() relies on (results[0]).
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ports)) as ex:
        return list(ex.map(_probe, ports))


def list_bridges(cfg: Config, force: bool = False) -> list[dict]:
    """Every bridge currently answering across the configured port range.

    the multi-instance replacement for what used to be a single
    bridge_state() snapshot. Returns one entry per port that answers
    available=True: [{available, project, bridge_version, port, reason}, ...],
    in port order. Empty when nothing's armed. Drives optix_bridge_status and
    the /ui dashboard, and is what _find_bridge_for scans to route a project to
    its specific bridge.
    """
    return [st for st in _scan_bridge_ports(cfg, force=force) if st["available"]]


def bridge_state(cfg: Config, force: bool = False) -> dict:
    """Back-compat single-bridge view: the FIRST available bridge in port
    order, or a summary {available:False, reason} when none answer.

    kept for callers that only care "is anything up at all"
    (classify_bridge_failure's unreachable-studio branch, the legacy single-
    bridge dashboard fields) — prefer list_bridges() for anything that needs
    to reason about MULTIPLE simultaneously-armed bridges.
    """
    bridges = list_bridges(cfg, force=force)
    if bridges:
        return bridges[0]
    if not cfg.bridge_enabled:
        return {"available": False, "project": None, "bridge_version": None,
                "port": None, "project_path": None, "pid": None,
                "reason": "disabled"}
    # Nothing in the range answered — report against the first candidate port
    # (or the pinned one) so the `reason` reflects a real probe, matching the
    # pre-multi-instance single-bridge behavior when bridge_url_pinned.
    ports = _bridge_ports(cfg)
    return _bridge_health_at(cfg, ports[0], force=force) if ports else {
        "available": False, "project": None, "bridge_version": None,
        "port": None, "project_path": None, "pid": None,
        "reason": "no ports configured",
    }


def reset_bridge_cache() -> None:
    """Test hook: drop the bridge-state TTL cache between cases."""
    global _bridge_cache, _bridge_cache_at
    _bridge_cache, _bridge_cache_at = {}, {}


def default_project(cfg: Config) -> str | None:
    """The project the design-time bridge is currently serving, if exactly
    ONE bridge is armed.

    Lets a caller OMIT `project` and act on the open project — the common
    single-seat flow — instead of naming it every time (and without a
    list_projects round-trip). None when no bridge is serving one.

    with multi-instance support, more than one bridge can be
    armed at once — in that case there is no longer a single "the" project to
    default to, so this now deliberately returns None and requires an
    explicit `project=` rather than guessing which of several open projects
    the caller meant. optix_bridge_status / list_bridges lists what's armed.
    """
    try:
        bridges = list_bridges(cfg)
        if len(bridges) == 1:
            return bridges[0].get("project")
        return None
    except Exception:
        return None


def project_served_names(project_dir: Path) -> set[str]:
    """Every name a bridge may report for the project at `project_dir`,
    lower-cased: the directory name, each .optix stem, and the root node's
    `Name:` from Nodes/<stem>.yaml.

    The bridge answers /bridge/health with Project.Current.BrowseName — the
    project NODE's name, not the folder's. They usually coincide (Studio names
    the folder after the project) but not always: `Line4_HMI/Line4.optix` serves
    as "Line4", and a dir-name-only compare routed NONE of its bridge calls and
    could not find its Studio window either (measured 2026-08-23).
    """
    names = {project_dir.name}
    try:
        optix = sorted(project_dir.glob("*.optix"))
    except OSError:
        optix = []
    for f in optix:
        names.add(f.stem)
        root_yaml = project_dir / "Nodes" / f"{f.stem}.yaml"
        try:
            if root_yaml.is_file():
                for ln in root_yaml.read_text(
                        encoding="utf-8", errors="replace").splitlines()[:10]:
                    m = re.match(r"^Name:\s*(.+?)\s*$", ln)
                    if m:
                        names.add(m.group(1))
                        break
        except OSError:
            pass
    return {n.strip().lower() for n in names if n and n.strip()}


def _bridge_name_match(served: object, want: str | set[str]) -> bool:
    """Case-insensitive, whitespace-trimmed BrowseName ↔ project-name compare.

    The single comparison both `_find_bridge_for` and the attributed
    studio-guard (`_attributed_studio_pass`) use to decide whether a bridge's
    Project.Current.BrowseName names a given on-disk project dir. `want` is
    either one name or the set from `project_served_names` (dir name plus the
    .optix stem / root node name it may be served under).
    """
    s = str(served or "").strip().lower()
    if not s:
        return False
    if isinstance(want, str):
        return s == want.strip().lower()
    return s in want


def _bridge_want_names(cfg: Config, project: str) -> set[str] | None:
    """The names to match a bridge's served-project against for `project`, or
    None for an invalid name (no path separators / no traversal — junk never
    matches). Shared by _find_bridge_for and the old single-bridge callers."""
    try:
        return project_served_names(resolve_project(cfg, project))
    except CoreError:
        # Studio can open a project from ANYWHERE (e.g. the Desktop), not only
        # under projects_root. The bridge serves Project.Current regardless of
        # on-disk location, so live-model access must not require the project to
        # resolve under projects_root — fall back to the requested name itself.
        if not project or "/" in project or "\\" in project or ".." in project:
            return None
        return {project.strip().lower()}


def _find_bridge_for(cfg: Config, project: str) -> dict | None:
    """The bridge (with its resolved `port`) currently serving `project`,
    searching every port in the configured range. None if no armed bridge
    matches.

    this is the routing step that makes multi-instance work —
    with several bridges armed simultaneously, each on its own port, THIS is
    what picks the right one for a given project instead of assuming there's
    only one to check."""
    want = _bridge_want_names(cfg, project)
    if want is None:
        return None
    for st in list_bridges(cfg):
        if _bridge_name_match(st.get("project"), want):
            return st
    return None


def _bridge_cfg_for(cfg: Config, project: str) -> Config | None:
    """`cfg` rebound to the SPECIFIC bridge URL serving `project`, or None if
    no armed bridge serves it.

    the choke point every bridge call site uses. Every
    _bridge_get_json/_bridge_post_json/_bridge_post_body/_bridge_write call
    downstream reads cfg.bridge_url unchanged — rebinding it HERE, once, is
    what makes the rest of the bridge plumbing (unchanged since before
    multi-instance) automatically target the right one of several
    simultaneously-armed bridges. Reassign the local `cfg` in the caller
    (`cfg = _bridge_cfg_for(cfg, project)` or via _require_bridge_for below) so
    every subsequent bridge call in that function body picks it up for free.
    """
    b = _find_bridge_for(cfg, project)
    if b is None:
        return None
    return dataclasses.replace(cfg, bridge_url=_bridge_url_at(b["port"]))


def _require_bridge_for(cfg: Config, project: str) -> Config:
    """`_bridge_cfg_for`, but raises BridgeUnavailable instead of returning
    None — the single choke point every bridge READ call site uses in place of
    the old `if not _use_bridge_for(cfg, project): raise ...` guard."""
    bcfg = _bridge_cfg_for(cfg, project)
    if bcfg is None:
        # Report exactly which ports were probed and what each one is serving,
        # rather than the misleading bridge_state() view (which returns the
        # first bridge in port order regardless of the requested project).
        all_states = _scan_bridge_ports(cfg)
        ports_info = ", ".join(
            f"port={st['port']} serving={st.get('project')!r}"
            f" source={st.get('source')!r} available={st.get('available')}"
            for st in all_states
        )
        raise BridgeUnavailable(
            f"bridge not serving {project!r}; "
            f"ports_probed=[{ports_info or 'none'}]"
        )
    return bcfg


def _use_bridge_for(cfg: Config, project: str) -> bool:
    """True iff SOME armed bridge is serving THE requested project (searching
    every port in the configured range — see _find_bridge_for)."""
    return _find_bridge_for(cfg, project) is not None


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
    cfg = _require_bridge_for(cfg, project)
    status, data = _bridge_get_json(cfg, f"/bridge/nodes?path={quote(path, safe='/')}")
    if status == 404:
        raise NodeNotFound(f"no node at path {path!r} in the live model")
    if status != 200 or not data:
        raise BridgeUnavailable(f"bridge /bridge/nodes returned status={status}")
    data["source"] = "bridge"
    # Property VALUES are model content an author can set to arbitrary text —
    # delimit them as untrusted. Names/paths/types are the node's structural
    # identity (service-derived), left raw.
    for _p in data.get("properties", []):
        if isinstance(_p, dict) and _p.get("value") is not None:
            _p["value"] = _untrusted(_p["value"], "bridge")
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

    Returns {types:[{name} | {name, browse_name}], count, truncated,
    source:"bridge"}. `browse_name` is dropped per-entry when it equals
    `name` — true for nearly all of the ~102 builtin types, so this is a
    pure token-size cut with no information loss: a missing `browse_name`
    means "same as `name`". It is kept only on the rare entry where the
    two genuinely differ. `count` still reflects the full catalog size.

    When any entry has a field name that differs from its browse name (a
    naming misalignment resolved by the bridge's browse-name walk), the
    response also carries `misaligned:[{name, browse_name}]` listing those
    entries, plus a `_hint` nudging callers to use the browse_name as the
    type identifier for describe_type calls.

    Bridge-only (the catalog lives in Studio's type system, not on disk).
    """
    cfg = _require_bridge_for(cfg, project)
    status, data = _bridge_get_json(cfg, "/bridge/types/ui")
    if status != 200 or "types" not in data:
        raise BridgeUnavailable(f"bridge /bridge/types/ui returned status={status}")
    data["source"] = "bridge"
    leaned = []
    misaligned = []
    for t in data.get("types") or []:
        entry = dict(t)
        bn = entry.get("browse_name")
        nm = entry.get("name")
        if bn == nm:
            entry.pop("browse_name", None)
        elif bn:
            # browse_name differs from name — collect for the misaligned summary.
            misaligned.append({"name": nm, "browse_name": bn})
        leaned.append(entry)
    data["types"] = leaned
    if misaligned:
        data["misaligned"] = misaligned
        data["_hint"] = (
            f"{len(misaligned)} type(s) have a catalog field name that differs from "
            "their browse name (resolved_by:\"browse_name\"). Use the browse_name as "
            "the identifier when calling describe_type for those types."
        )
    return data


def describe_type(cfg: Config, project: str, type_name: str) -> dict:
    """Property schema of a builtin UI type via the bridge /bridge/types/schema.

    Returns {type, browse_name, properties:[{name, datatype}], truncated,
    source:"bridge"}. Bridge-only typed introspection — raises BridgeUnavailable
    when Studio/the bridge is down, NodeNotFound for an unknown type.

    When the bridge resolved the type via a browse-name walk (the type's
    catalog field name differs from its browse name), the response carries
    `resolved_by:"browse_name"` (passed through from the bridge). When the
    response browse_name differs from the requested type_name, the response
    also carries `catalog_misaligned:True` as a signal that the caller used
    a field-name alias rather than the canonical browse-name identifier.
    """
    from urllib.parse import quote
    cfg = _require_bridge_for(cfg, project)
    status, data = _bridge_get_json(cfg, f"/bridge/types/schema?type={quote(type_name, safe='')}")
    if status == 404:
        raise NodeNotFound(f"no builtin UI type {type_name!r}")
    if status != 200 or not data:
        raise BridgeUnavailable(f"bridge /bridge/types/schema returned status={status}")
    data["source"] = "bridge"
    # Detect naming misalignment: response browse_name differs from what was requested.
    # resolved_by is passed through as-is from the bridge (already in `data` if set).
    if data.get("browse_name") and data["browse_name"] != type_name:
        data["catalog_misaligned"] = True
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
    cfg = _require_bridge_for(cfg, project)
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
            out_s["map"] = _untrusted(
                "\n".join(f"{m.get('path')} ({m.get('type')})" for m in matches)
                or "(no matches)", "get_project_map")
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
        # The whole outline is model/project-derived node names — wrap ONCE as
        # untrusted (never per-line: that would break the token-lean outline and
        # its exact-line rendering). fmt="json" returns the raw tree unwrapped
        # (structural data, machine-consumed).
        out["map"] = _untrusted("\n".join(_render_map_outline(tree, ids=ids)),
                                "get_project_map")
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
    # Exactly one authoring mode per edit (InvalidEdit contract). Reject a dict
    # that declares more than one instead of letting a silent precedence order
    # pick a winner: a stray "content" alongside find/replace would otherwise
    # overwrite the whole file while the caller expected a surgical replace.
    modes = [k for k in ("content", "find", "insert_after_anchor") if k in edit]
    if len(modes) > 1:
        raise InvalidEdit(
            f"edit on {rel} declares multiple modes {modes}; each edit is exactly "
            "one of: content / find+replace / insert_after_anchor+block"
        )

    if "content" in edit:
        new_text = edit["content"]
        # U11 round-trip guard: read_file hands back `<untrusted source="...">`
        # -delimited content, and an agent that forgets to strip it before
        # reusing the text writes the markers straight into the project file.
        # The ANCHORED modes defend themselves (a wrapped anchor matches
        # nothing and raises edit_anchor_mismatch), but full-replace has no
        # such check — verified 2026-07-24: the wrapper landed on disk with no
        # error at all, and would then ship in the next deploy. Refuse loudly
        # instead. Only the exact leading marker read_file emits is rejected,
        # so project text that merely mentions the word stays writable.
        if new_text.lstrip().startswith('<untrusted source="'):
            raise InvalidEdit(
                f"edit content for {rel} still carries the <untrusted> wrapper "
                "that read_file adds. Strip it before reusing the text — the "
                "on-disk file holds raw content, never the markers."
            )
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
    _bridge_cfg = _bridge_cfg_for(cfg, project)
    if _bridge_cfg is not None:
        return _bridge_list_screens(_bridge_cfg, project)

    from . import optix_model

    project_dir = resolve_project(cfg, project)
    require_editors_closed(cfg, project_dir)
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
    require_editors_closed(cfg, resolve_project(cfg, project))
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
    require_editors_closed(cfg, resolve_project(cfg, project))
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

    require_editors_closed(cfg, resolve_project(cfg, project))
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
    In-process psutil scan (was a Get-NetTCPConnection PowerShell spawn — one of
    the per-restart process-spawn costs); `runner` is kept for signature
    stability. Returns None if not resolvable."""
    port = _bridge_port(cfg)
    try:
        for c in psutil.net_connections(kind="tcp"):
            if (c.status == psutil.CONN_LISTEN and c.laddr
                    and c.laddr.port == port and c.pid):
                return c.pid
    except (psutil.Error, OSError):
        return None
    return None


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
    # When A bridge serves THIS project (one of possibly several simultaneously
    # armed, each on its own port), target Ctrl+S at the exact Studio instance
    # hosting THAT bridge (the PID owning its listener) instead of the first
    # Studio window — with several Studio instances open, "first window" can
    # save the WRONG project silently. When no bridge serves this project
    # (target_pid stays 0), behaviour is unchanged: the first focus-able Studio
    # window — this is also the multi-instance fix for "the
    # wrong project's emulator/save got targeted" — arming a bridge for every
    # open project (see StudioMCPBridge.cs's port range) means target_pid is
    # resolvable for each of them instead of falling back to a guess.
    target_pid = 0
    _bridge_cfg = _bridge_cfg_for(cfg, project)
    if _bridge_cfg is not None:
        bp = _bridge_owner_pid(_bridge_cfg, runner)
        if bp:
            target_pid = bp
    if not target_pid:
        # (1.0.8) No bridge serves this project: identify its Studio by the
        # project path on the process command line instead of guessing. On
        # a six-Studio desktop the "first focus-able window" fallback sent
        # Ctrl+S to the SAME unrelated instance for every project and
        # reported saved:false each time (2026-09-05). When several
        # Studios are open and none can be attributed, refuse — a guessed
        # Ctrl+S silently saves someone else's project.
        cp = _studio_pid_for_project(project_dir)
        if cp:
            target_pid = cp
        else:
            state = studio_guard.studio_state()
            n_studio = len(state.get("studio", {}).get("pids", []) or [])
            if n_studio > 1:
                return {
                    "saved": False, "reason": "ambiguous_studio",
                    "mtime_before": before, "mtime_after": before,
                    "focused": False, "elapsed_seconds": 0.0, "stdout": "",
                    "studio_count": n_studio,
                    "hint": (
                        f"{n_studio} Studio instances are open and none names "
                        f"{project!r} on its command line (Studio opened from the "
                        "GUI carries no project there) and no bridge is armed "
                        "for it. Arm the bridge (optix_bridge_arm) so the save "
                        "can be targeted at the right instance, or save in Studio."
                    ),
                }
    proc = runner.run_powershell(
        _build_save_ps(target_pid, gentle=_gentle_focus()),
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


def _deployment_targets_by_name(cfg: Config) -> dict[str, dict]:
    """Every deployment target DEFINED in Studio's Configuration.xml, keyed by
    display name — {name: {"type": <str>, "ip": <str>}}.

    Reads the SAME file studio_active_deployment_target parses (via
    _studio_configuration_xml, so OPTIX_STUDIO_CONFIG_XML is honored), but
    returns the whole targets collection rather than only the active one — the
    live UIA read yields a display name and needs every definition to look up its
    type/ip. Fail-open: {} on any read/parse error, so an unreadable file just
    disables the UIA-override branch and leaves the fallback untouched.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(_studio_configuration_xml()).getroot()
    except (OSError, ET.ParseError):
        return {}
    by_name: dict[str, dict] = {}
    try:
        for item in root.iter("Item"):
            vals = {v.get("name"): (v.text or "") for v in item.findall("Value")}
            if vals.get("name") != "deployment" or "activeTargetId" not in vals:
                continue
            for coll in item.findall("Collection"):
                if coll.get("name") != "targets":
                    continue
                for t in coll.findall("Item"):
                    tv = {v.get("name"): (v.text or "")
                          for v in t.findall("Value")}
                    nm = tv.get("name")
                    if nm:
                        by_name[nm] = {"type": tv.get("type", ""),
                                       "ip": tv.get("ipAddress", "")}
    except Exception:
        return {}
    return by_name


def resolve_active_target(cfg: Config, bridge_pid: int | None = None) -> dict:
    """Which deploy target Studio has selected — preferring a LIVE per-window UIA
    read, falling back to the Configuration.xml advisory.

    Studio flushes activeTargetId lazily, so the file can say Emulator while the
    live toolbar is on a hardware panel. When a bridge PID is known and the file
    yields target definitions, read the selection straight off that window's
    toolbar (studio_uia, background-safe, Windows-only). A confirmed live name
    that matches a defined target is DEFINITIVE (source "uia_live"). If the read
    is unavailable (off-Windows, uiautomation absent, window/selector not found)
    it returns None and we fall through to studio_active_deployment_target
    unchanged (source stays the config-file path).
    """
    defs = _deployment_targets_by_name(cfg)
    if bridge_pid and defs:
        name = studio_uia.read_selected_target_name(bridge_pid, set(defs))
        if name and name in defs:
            d = defs[name]
            ip = d.get("ip", "")
            is_emu = d.get("type") == "2" and ip.lower() in (
                "localhost", "127.0.0.1", "")
            return {"known": True, "is_emulator": is_emu, "name": name,
                    "ip": ip, "type": d.get("type", ""), "source": "uia_live"}
    return studio_active_deployment_target(cfg)


def active_target(
    cfg: Config, project: str | None = None, runner: Runner = _DEFAULT_RUNNER,
) -> dict:
    """Read the selected deploy target: the live per-window UIA read off the
    bridge's Studio window, with the Configuration.xml advisory as fallback.

    Convenience wrapper over resolve_active_target that resolves the bridge-owner
    PID first. `source` is "uia_live" when the live toolbar was read (definitive,
    Windows + bridge + session-1), else the config-file path (lazy, may be stale).

    multi-instance-aware. Pass `project` to target a SPECIFIC
    armed bridge's Studio window. Without it: zero or exactly one bridge armed
    behaves as before (no bridge -> config-file fallback; one bridge -> that
    one). With SEVERAL bridges armed and no `project` given, this refuses to
    guess and returns {known:False, reason:"ambiguous_bridge",
    armed_projects:[...]} instead — silently picking "whichever bridge happens
    to be on the base port" would be exactly the wrong-window bug this release
    is fixing, just moved into this tool.
    """
    if project is not None:
        bcfg = _bridge_cfg_for(cfg, project)
        bp = _bridge_owner_pid(bcfg, runner) if bcfg is not None else None
        return resolve_active_target(cfg, bridge_pid=bp)
    bridges = list_bridges(cfg)
    if len(bridges) > 1:
        return {
            "known": False, "reason": "ambiguous_bridge",
            "armed_projects": [b.get("project") for b in bridges],
            "hint": (
                "Several bridges are armed at once — pass project= to read "
                "a specific one's toolbar target."
            ),
        }
    # 0 or 1 armed: rebind to that one bridge's OWN port (not cfg.bridge_url's
    # default/base port, which may not be the port the sole armed bridge
    # actually landed on if a lower port in the range was stale/taken).
    bcfg = dataclasses.replace(cfg, bridge_url=_bridge_url_at(bridges[0]["port"])) if bridges else cfg
    bp = _bridge_owner_pid(bcfg, runner)
    return resolve_active_target(cfg, bridge_pid=bp)


# ---- bridge arming + Studio CLI ------------------------------------------

def bridge_arm(cfg: Config, project: str, action: str = "arm") -> dict:
    """Arm or stop the design-time bridge for `project` via the Studio GUI.

    Studio executes nothing from the NetSolution until an explicit Execute, and
    its CLI has no execute verb, so the right-click gesture is the only lever.
    The service runs in session 1, which is the only thing that gesture needs —
    see service/studio_arm.py for the measured facts it encodes.
    """
    from . import studio_arm
    info = resolve_project_info(cfg, project)
    project_dir = info["dir"]
    method = "StartBridge" if action == "arm" else "StopBridge"
    audit(cfg, "bridge_arm", project=project, action=action)
    out = studio_arm.execute_method(
        project, str(project_dir), method=method,
        base_port=cfg.bridge_port_base, port_range=cfg.bridge_port_range,
        aliases=project_served_names(project_dir),
    )
    reset_bridge_cache()
    if isinstance(out, dict):
        # Which source resolved the name — "studio_process" means the project
        # is OUTSIDE projects_root and was matched to a running Studio's
        # command line, so the same call will stop resolving once that Studio
        # closes. setdefault, not [], so execute_method stays free to report
        # its own value.
        out.setdefault("resolved_from", info["source"])
    return out


def execute_design_method(cfg: Config, project: str, node: str, method: str,
                          timeout: float = 20.0) -> dict:
    """Right-click -> Execute an ARBITRARY design-time [ExportMethod].

    Same UI Automation gesture as bridge_arm, which is the only SAFE way to run
    one: invoking a design-time method in-process on the bridge's HTTP thread
    runs it off Studio's UI thread and CRASHES Studio (measured). The GUI
    gesture does not.

    `ok` here means the Execute item was clicked — NOT that the method did what
    you wanted. Arbitrary methods expose no port signal to verify against
    (unlike StartBridge/StopBridge), so confirm the effect with
    optix_describe_node or a render, and read Studio's Output pane for the
    method's own logging.
    """
    from . import studio_arm
    project_dir = resolve_project(cfg, project)
    audit(cfg, "execute_method", project=project, node=node, method=method)
    out = studio_arm.execute_method(
        project, str(project_dir), method=method, node_name=node,
        base_port=cfg.bridge_port_base, port_range=cfg.bridge_port_range,
        timeout=timeout, aliases=project_served_names(project_dir),
        verify="none",
    )
    reset_bridge_cache()
    return out


def _studio_launch(cfg: Config, args: list[str]) -> dict:
    """Launch FTOptixStudio.exe with CLI args and DO NOT WAIT.

    Studio's CLI verbs are GUI launches, not batch commands: `new` creates the
    project and then stays running with it open, and `open` obviously does. So
    a waiting call is wrong twice over — it blocks for the life of the editor,
    and Runner.run tree-kills on TimeoutExpired, which means waiting actually
    KILLS the Studio the command just started (measured: `new` created all 25
    files, then the timeout reaped the editor).

    Detached + own process group so a signal to the service never propagates,
    and stdio to DEVNULL so the service can exit without holding the child's
    pipes. Same shape as _launch_runtime below. Returns {pid}.

    NEVER pass an unrecognised flag: Studio launches and then CRASH-DUMPS on
    one (measured with `/?`), which reads like a broken install.
    """
    if not cfg.studio_exe.is_file():
        return {"ok": False, "error": "studio_exe_missing", "path": str(cfg.studio_exe)}
    cmd = [str(cfg.studio_exe), *args]
    try:
        kwargs: dict[str, Any] = dict(
            cwd=str(cfg.studio_exe.parent), close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
                | subprocess.CREATE_NEW_PROCESS_GROUP)
        proc = subprocess.Popen(cmd, **kwargs)
        return {"ok": True, "pid": proc.pid}
    except Exception as e:
        return {"ok": False, "error": "studio_launch_failed",
                "detail": f"{type(e).__name__}: {e}"}


def project_open(cfg: Config, project: str, wait_seconds: float = 90.0,
                 runner: Runner = _DEFAULT_RUNNER) -> dict:
    """`FTOptixStudio open <project>` and wait until the window is really there.

    The CLI returns immediately while Studio loads, so a naive caller's next
    bridge call fails confusingly. Readiness is the window's UIA project
    identity appearing — the same bridge-free identity read arming uses — not a
    sleep, and not process existence (the process exists long before the
    project is loaded).
    """
    from . import studio_arm
    project_dir = resolve_project(cfg, project)
    candidates = sorted(project_dir.glob("*.optix"))
    if not candidates:
        return {"ok": False, "error": "no_optix_file", "project": project,
                "searched": str(project_dir)}
    already = None
    try:
        import uiautomation as auto
        already = studio_arm._studio_window_for(auto, project)
    except Exception:
        pass
    if already is not None:
        return {"ok": True, "state": "already_open", "project": project}

    audit(cfg, "project_open", project=project)
    launched = _studio_launch(cfg, ["open", str(candidates[0]), "--silent"])
    if not launched.get("ok"):
        return {"ok": False, "error": launched.get("error", "launch_failed"),
                "project": project, "cli": launched}
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            import uiautomation as auto
            if studio_arm._studio_window_for(auto, project) is not None:
                return {"ok": True, "state": "opened", "project": project,
                        "waited_seconds": round(wait_seconds - (deadline - time.time()), 1)}
        except Exception:
            pass
        time.sleep(1.5)
    return {"ok": False, "error": "open_timeout", "project": project,
            "detail": f"no Studio window identified {project!r} within {wait_seconds:g}s",
            "cli": launched}


def project_new(cfg: Config, name: str, template: str | None = None,
                wait_seconds: float = 180.0,
                runner: Runner = _DEFAULT_RUNNER) -> dict:
    """`FTOptixStudio new <name> <projects_root>` — create a project.

    Refuses when the target directory already exists: the CLI would otherwise
    decide what to do with a populated directory, and this tool must never be
    the reason someone's project is touched.
    """
    if ".." in name or "/" in name or "\\" in name:
        return {"ok": False, "error": "invalid_project_name", "name": name}
    dest = (cfg.projects_root / name)
    if dest.exists():
        return {"ok": False, "error": "already_exists", "path": str(dest),
                "nudge": "pick another name; this tool never writes into an existing directory"}
    args = ["new", name, str(cfg.projects_root)]
    if template:
        # -u is REQUIRED alongside --template: without it Studio runs NewProject
        # during IDEContext::Initialize with the USER template library not yet
        # indexed, and the lookup fails `Cannot create a new project with invalid
        # template: <name>` out of TemplateLibrary::GetModelNodeFromPath — for a
        # template name the GUI resolves fine. Verified 2026-09-01: identical
        # argv minus -u fails, with -u creates the project.
        args.append(f"--template={template}")
        args.append("-u")
    args.append("--silent")
    audit(cfg, "project_new", project=name, template=template)
    launched = _studio_launch(cfg, args)
    if not launched.get("ok"):
        return {**launched, "project": name}
    # Readiness is the project TREE on disk, not process exit — Studio stays
    # open on the project it just made (which is convenient: optix_bridge_arm
    # can then arm it).
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if (dest / f"{name}.optix").is_file():
            return {"ok": True, "state": "created", "project": name,
                    "path": str(dest), "pid": launched.get("pid"),
                    "studio_left_open": True}
        time.sleep(1.0)
    out = {"ok": False, "error": "create_timeout", "project": name,
           "path": str(dest), "created": dest.is_dir(),
           "detail": f"no {name}.optix under the project dir within {wait_seconds:g}s"}
    out["path"] = str(dest)
    return out


def run_emulator(
    cfg: Config,
    project: str,
    save_first: bool = False,
    wait_ready: bool = True,
    ready_timeout: float = 60.0,
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
    # ATTACH MODE (U19): OPTIX_RUNTIME_URL is set, so an EXTERNAL runtime owns
    # its own lifecycle — the service must not send F5 (that would start a
    # SECOND, service-owned emulator alongside the attached runtime). Refuse
    # BEFORE the F5 guard / keystroke; the caller screenshots/verifies against
    # the external runtime directly.
    if attach_mode(cfg):
        return {
            "launched": False, "focused": False, "saved": None, "serving": False,
            "state": "external", "reason_code": "external_runtime",
            "runtime_url": cfg.runtime_url,
            "nudge": (
                "OPTIX_RUNTIME_URL is set — the runtime is externally managed; "
                "the service won't send F5. Screenshot/verify against it directly."
            ),
        }
    # MISMATCH GUARD: Before sending F5, confirm no OTHER project's emulator is
    # already running on this host.  The cache-path component
    # (\\Emulator\\Projects\\<Project>\\) uniquely identifies the owning project;
    # acting on the wrong runtime — stopping Beta when the caller asked for Alpha —
    # is silent data loss on a multi-Studio box.
    _running_entries = _emulator_entries()          # all instances, no project filter
    for _re in _running_entries:
        _resolved = _re.get("project_resolved")
        if _resolved and _resolved.casefold() != project.casefold():
            audit(cfg, "emulator_run_refused_mismatch",
                  project=project, resolved=_resolved)
            return {
                "ok": False,
                "error": "runtime_project_mismatch",
                "requested": project,
                "resolved": _resolved,
                "pid": _re["pid"],
                "nudge": (
                    f"The running emulator belongs to project '{_resolved}', not "
                    f"'{project}'. Stop it first "
                    f"(optix_emulator action='stop' project='{_resolved}'), then "
                    "retry. The service will not send F5 while another project's "
                    "runtime is running."
                ),
            }
    # Resolve the bridge-owner PID FIRST — the live UIA target read needs it, and
    # the F5 keystroke below aims at the SAME instance (with several Studio
    # windows open, "first window" can F5 the wrong project). Computed once,
    # reused. This is the crux of the wrong-project-emulator fix —
    # arming a bridge for EVERY open project (StudioMCPBridge.cs now supports
    # several at once, each on its own port) means target_pid resolves for
    # whichever project was actually asked for, instead of only ever being
    # resolvable for the one project that happened to hold the sole global port.
    target_pid = 0
    _bridge_cfg = _bridge_cfg_for(cfg, project)
    if _bridge_cfg is not None:
        bp = _bridge_owner_pid(_bridge_cfg, runner)
        if bp:
            target_pid = bp
    if not target_pid:
        # (1.0.8) No bridge serves this project: identify its Studio by the
        # project path on the process command line instead of guessing. On
        # a six-Studio desktop the "first focus-able window" fallback sent
        # F5 to the SAME unrelated instance for every project. When several
        # Studios are open and none can be attributed, refuse — a guessed
        # F5 silently launches someone else's project's emulator.
        try:
            _project_dir = resolve_project(cfg, project)
        except ProjectNotFound:
            _project_dir = None
        if _project_dir is not None:
            cp = _studio_pid_for_project(_project_dir)
            if cp:
                target_pid = cp
            else:
                _state = studio_guard.studio_state()
                _n_studio = len(_state.get("studio", {}).get("pids", []) or [])
                if _n_studio > 1:
                    return {
                        "ok": False, "error": "ambiguous_studio",
                        "studio_count": _n_studio,
                        "hint": (
                            f"{_n_studio} Studio instances are open and none names "
                            f"{project!r} on its command line (Studio opened from the "
                            "GUI carries no project there) and no bridge is armed "
                            "for it. Arm the bridge (optix_bridge_arm) so the F5 "
                            "can be targeted at the right instance, or launch from Studio."
                        ),
                    }
    # F5 GUARD: F5 runs Studio's SELECTED deployment target, which is only the
    # emulator if the operator's dropdown says so. If the active target is a
    # non-emulator, sending F5 could ship to hardware — refuse instead. The
    # dropdown is operator-owned; the service never switches it. Prefer the LIVE
    # per-window UIA read (definitive); fall back to the lazily-flushed config
    # file when UIA is unavailable (the post-launch process-identity check below
    # is the second layer in either case).
    tgt = resolve_active_target(cfg, bridge_pid=target_pid or None)
    # Kept because `tgt` is REBOUND to the config-file read further down (the
    # F5-sent-but-nothing-spawned diagnosis). Reaching that code means this
    # guard passed, and whether it passed on a definitive live read or on the
    # lazily-flushed file changes what the failure most likely IS — see the
    # blocking-dialog hint below.
    guard_source = tgt.get("source")
    if tgt.get("known") and not tgt.get("is_emulator"):
        audit(cfg, "emulator_run_refused", project=project, target=tgt.get("name"))
        live = tgt.get("source") == "uia_live"
        if live:
            nudge = (
                f"Studio's toolbar target is {tgt.get('name')!r} "
                f"({tgt.get('ip')}) — read LIVE from the window, so this is "
                "definitive, not a stale config guess. F5 runs the SELECTED "
                "target — pressing it now would deploy to that device, not start "
                "the emulator. Ask the user to switch the target dropdown to "
                "Emulator, then retry. The service never changes the selection "
                "itself.")
        else:
            nudge = (
                f"Studio's deployment dropdown is set to {tgt.get('name')!r} "
                f"({tgt.get('ip')}). F5 runs the SELECTED target — pressing it "
                "now could deploy to that device, not start the emulator. Ask "
                "the user to switch the target dropdown to Emulator, then retry. "
                "The service never changes the selection itself.")
        return {
            "launched": False, "focused": False, "saved": None, "serving": False,
            "state": "refused", "reason_code": "active_target_not_emulator",
            "target": {"name": tgt.get("name"), "ip": tgt.get("ip")},
            "source": tgt.get("source"),
            "nudge": nudge,
        }
    saved = None
    if save_first:
        s = save(cfg, project, runner=runner)
        saved = s.get("saved")
    proc = runner.run_powershell(
        _build_save_ps(target_pid, gentle=_gentle_focus(), send_key="{F5}"),
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
    if focused:
        # Invalidate the web-port cache for this project: a successful F5 may
        # start a runtime on a port that differs from what was cached before the
        # launch (e.g. project port was updated between calls).
        _invalidate_web_port_cache(project)
    if focused and wait_ready:
        # F5 spins up FTOptixRuntime + its web engine asynchronously; a CDP screenshot
        # fired immediately hits nothing. Poll the runtime port until it's serving so
        # a caller can screenshot right after.
        import socket
        port = _project_web_port(cfg, project)
        probe_host = runtime_probe_host(cfg)
        started = time.time()
        serving = False
        # The NetLogic security warning is modal and EATS the F5 until it is
        # answered, so a first run would otherwise burn the whole ready_timeout
        # and report target_or_modal with nothing spawned. Clear it IN FLIGHT.
        # Gate the focus-stealing click behind pending_dialog, which is a
        # read-only walk that never takes the foreground — so a normal build,
        # where no dialog is up, is never interrupted. Answering the dialog
        # lets the ALREADY-SENT F5 continue: do NOT resend it (F5 toggles, and
        # a resend would stop the emulator it just started).
        consent_cleared = None
        next_dialog_check = started + 2.0
        while time.time() - started < ready_timeout:
            sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sk.settimeout(0.5)
            try:
                serving = sk.connect_ex((probe_host, int(port))) == 0
            except OSError:
                serving = False
            finally:
                sk.close()
            if serving:
                break
            now = time.time()
            if (consent_cleared is None and target_pid
                    and now >= next_dialog_check and now - started < 25.0):
                next_dialog_check = now + 3.0
                try:
                    dialogs = studio_uia.pending_dialog(target_pid)
                except Exception:
                    dialogs = []
                if any("security warning" in (d.get("title") or "").casefold()
                       for d in dialogs):
                    from . import studio_arm
                    cleared = studio_arm.clear_consent(project)
                    consent_cleared = bool(cleared.get("consent_clicked"))
                    result["consent_cleared"] = consent_cleared
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
                if ident.get("state") == "stopped":
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
                    "still building/loading. Poll optix_emulator(action='status') until "
                    "`running`; do NOT resend F5 (it TOGGLES and would stop it).")
            elif st.get("state") == "stopped" and _bare_runtime_running(cfg, runner):
                # A FTOptixRuntime process DOES exist, but the strict
                # --application-name=Emulator identity match / CIM timing can't
                # confirm it yet and the port isn't serving: that's a slow START,
                # not a failed spawn. Report "starting" instead of crying wolf.
                result["runtime_identity"] = "starting"
                result["hint"] = (
                    f"An FTOptixRuntime process exists but :{port} isn't serving yet "
                    "and its emulator identity isn't confirmable yet — still starting. "
                    "Poll optix_emulator(action='status') until `running`; do NOT resend F5 "
                    "(it TOGGLES and would stop it).")
            elif st.get("state") in ("stopped", "demo_expired"):
                tgt = studio_active_deployment_target(cfg)
                file_claims_emu = tgt.get("known") and tgt.get("is_emulator")
                result["probable_cause"] = "target_or_modal"
                # UIA can SEE a blocking dialog the keystroke path is blind to.
                # Name it when present, turning this from "the service cannot
                # see dialogs" into a concrete cause.
                dialogs = studio_uia.pending_dialog(target_pid) if target_pid else []
                if dialogs:
                    d = dialogs[0]
                    result["blocking_dialog"] = d
                    # The advice MUST branch on how the F5 guard resolved the
                    # target. This hint used to say the dialog was "most likely
                    # a deploy/credentials prompt from a non-emulator target"
                    # and to go set the dropdown to Emulator — but a live UIA
                    # read that says non-emulator REFUSES before F5, so that is
                    # the one case which cannot reach here. Telling a user with
                    # a confirmed-correct dropdown to go fix the dropdown sends
                    # them looking in the wrong place.
                    if guard_source == "uia_live":
                        cause = (
                            "The toolbar target was read LIVE and is the "
                            "Emulator, so this is NOT a target-selection "
                            "problem — the dialog is unrelated (unsaved "
                            "changes, a license/sign-in prompt, a build "
                            "error). Ask the user to dismiss it and retry.")
                    else:
                        cause = (
                            "The target could NOT be read live (no bridge PID, "
                            "uiautomation unavailable, or no session-1 "
                            "desktop); the guard fell back to Studio's "
                            "Configuration.xml, which Studio flushes lazily and "
                            "which may be stale. The toolbar may really be on a "
                            "hardware target despite the file saying Emulator. "
                            "Ask the user to check the dropdown and dismiss the "
                            "dialog, then retry.")
                    result["hint"] = (
                        "F5 was sent and Studio took focus, but NO emulator "
                        f"process spawned — a dialog titled {d.get('title')!r} "
                        f"is open on Studio and is eating the keystroke. {cause} "
                        "Do NOT retry-loop F5 — each press fires at whatever "
                        "target is selected.")
                else:
                    result["hint"] = (
                        "F5 was sent and Studio took focus, but NO emulator process "
                        "spawned. F5 runs Studio's SELECTED deployment target — the "
                        "most likely causes are (1) the toolbar target dropdown is set "
                        "to another target (a deploy/credentials dialog may have opened) "
                        "or (2) a modal dialog (e.g. the NetLogic security warning) ate "
                        "the keystroke. No blocking dialog was visible via UI Automation"
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


def _bare_runtime_running(cfg: Config, runner: Runner = _DEFAULT_RUNNER) -> bool:
    """Any FTOptixRuntime.exe process at all — the identity-agnostic fallback for
    run_emulator's spawn check. The strict --application-name=Emulator match in
    emulator_status can miss a just-F5'd runtime whose command line isn't
    readable yet; a bare presence check tells 'still starting' apart
    from 'nothing spawned'. Best-effort; returns False on any error.
    """
    try:
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() == "ftoptixruntime.exe":
                return True
    except psutil.Error:
        pass
    return False


# Matches the emulator's per-project cache directory embedded in the runtime
# command line, e.g. "…\Emulator\Projects\MyProject\…" (forward or back
# slash; case-insensitive).  Group 1 = the project name component.
_EMULATOR_PROJ_RE = re.compile(
    r'[/\\]Emulator[/\\]Projects[/\\]([^/\\]+)[/\\]',
    re.IGNORECASE,
)


def _emulator_entries(project: str | None = None) -> list[dict]:
    """Rich scan of FTOptixRuntime.exe emulator instances.

    Returns a list of ``{"pid": int, "project_resolved": str | None,
    "matched_path": str | None}`` dicts for every FTOptixRuntime.exe that
    carries ``--application-name=Emulator`` in its command line.

    When *project* is given, only entries whose emulator cache-path component
    (``\\Emulator\\Projects\\<Project>\\``) matches *project* exactly and
    case-insensitively are returned.  A runtime whose path names a different
    project is silently excluded — never attributed to the wrong caller.

    Same process-scan discipline as ``_emulator_pids``: name-only toolhelp
    snapshot first, then cmdline only for FTOptixRuntime.exe hits.  Best-effort:
    unreadable processes are skipped, any scan failure returns [].
    """
    entries: list[dict] = []
    needle = project.casefold() if project is not None else None
    try:
        # Name-only iteration first (cheap toolhelp snapshot); read cmdline
        # ONLY for actual FTOptixRuntime processes. Asking process_iter for
        # "cmdline" up front queries EVERY process on the box and the
        # access-denied fallbacks make that take tens of seconds unelevated.
        for p in psutil.process_iter(["pid", "name"]):
            if (p.info.get("name") or "").lower() != "ftoptixruntime.exe":
                continue
            try:
                cmd = " ".join(p.cmdline())
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            if "--application-name=Emulator" not in cmd:
                continue
            m = _EMULATOR_PROJ_RE.search(cmd)
            project_resolved: str | None = m.group(1) if m else None
            matched_path: str | None = m.group(0) if m else None
            if needle is not None:
                # Exact, case-insensitive match against the path component.
                # A runtime for Line4_HMI must never be returned for Line4.
                if project_resolved is None or project_resolved.casefold() != needle:
                    continue
            entries.append({
                "pid": p.info["pid"],
                "project_resolved": project_resolved,
                "matched_path": matched_path,
            })
    except psutil.Error:
        pass
    return entries


def _emulator_pids(project: str | None = None) -> list[int]:
    """PIDs of FTOptixRuntime.exe instances launched with
    `--application-name=Emulator` — the command-line discriminator that
    separates the emulator from an UpdateSvc-deployed runtime (same exe,
    typically same port). In-process psutil scan; the previous
    Get-CimInstance Win32_Process PowerShell spawn cost seconds per call and
    ran up to four times per restart. Best-effort: unreadable processes are
    skipped, any scan failure returns [].

    When *project* is given, only PIDs whose emulator cache path names that
    project (``\\Emulator\\Projects\\<Project>\\``, exact case-insensitive)
    are returned — a runtime for a different project is never included.
    """
    return [e["pid"] for e in _emulator_entries(project)]


_DEMO_EXPIRED_RE = re.compile(r"Demo mode expired", re.IGNORECASE)


def _tail_last_lifecycle_line(project: str) -> str | None:
    """Return the last non-empty line from the newest FTOptixRuntime.*.log for
    *project*, or ``None`` when the log directory / file does not exist.

    Reads only the last 64 KiB (brief, non-held open) so it never blocks the
    runtime's own log writes.  Used by ``emulator_status`` to surface the
    ``last_lifecycle_line`` field and derive the ``demo_expired`` state.
    """
    log_dir = _emulator_log_dir(project)
    if not log_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in log_dir.glob("FTOptixRuntime.*.log") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not candidates:
        return None
    log = candidates[0]
    try:
        with open(log, "rb") as fh:
            sz = log.stat().st_size
            tail_bytes = min(sz, 65536)
            if tail_bytes < sz:
                fh.seek(sz - tail_bytes)
            data = fh.read(tail_bytes)
        lines = data.decode("utf-8", errors="replace").splitlines()
        for ln in reversed(lines):
            stripped = ln.strip()
            if stripped:
                return stripped
    except OSError:
        pass
    return None


def _any_pid_has_window(pids: list[int]) -> bool:
    """Return True if any of *pids* owns at least one visible top-level window.

    Uses ``EnumWindows`` on Windows; always returns False on other platforms.
    Best-effort: any ctypes / attribute error is swallowed and returns False.
    """
    if os.name != "nt" or not pids:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        pid_set = set(pids)
        found: list[bool] = [False]

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def _collect(hwnd, _lparam) -> bool:
            if found[0]:
                return False  # short-circuit once found
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if window_pid.value in pid_set and user32.IsWindowVisible(hwnd):
                found[0] = True
                return False
            return True

        user32.EnumWindows(EnumWindowsProc(_collect), 0)
        return found[0]
    except Exception:
        return False


def emulator_status(
    cfg: Config,
    runner: Runner = _DEFAULT_RUNNER,
    *,
    project: str | None = None,
) -> dict:
    """Emulator state: stopped / starting / running / zombie / demo_expired.

    F5 in Studio TOGGLES the emulator, so a caller needs the current state to
    avoid a blind start-that-actually-stops.

    Discriminates the EMULATOR from other FTOptixRuntime.exe instances via
    _emulator_pids (see there); `runner` is kept for signature stability.

    When *project* is given the scan is narrowed to that project's emulator
    cache path (``\\Emulator\\Projects\\<Project>\\``, exact case-insensitive).
    The result then includes ``project_resolved`` (the name extracted from the
    path) and ``matched_path`` (the ``\\Emulator\\Projects\\<Project>\\``
    segment) as attribution evidence.  A runtime for a different project is
    never counted.

    States:
      stopped      — no emulator process
      starting     — emulator process up, runtime port not serving yet
                     (still building, or hung mid-init)
      running      — emulator process up AND port serving (safe to CDP-screenshot)
      zombie       — process up with a visible UI window but no port listener
                     (Windows only; reported as 'starting' on non-Windows)
      demo_expired — the last lifecycle log line contains 'Demo mode expired';
                     the FTOptix demo licence has lapsed

    Returns {state, running, pids, port, port_reachable, last_lifecycle_line,
    checked_at}; `running` is kept as a bool for back-compat and is True only
    in the `running` state.  Adds a `hint` when the port is served by something
    that is NOT the emulator.
    """
    if project is not None:
        entries = _emulator_entries(project)
        pids = [e["pid"] for e in entries]
        # Pick the attribution evidence from the first matching entry (all
        # entries share the same project by construction of _emulator_entries).
        first = entries[0] if entries else {}
        project_resolved: str | None = first.get("project_resolved")
        matched_path: str | None = first.get("matched_path")
        # project_resolved is surfaced in the output when project= is given.
        surface_project_resolved = True
    else:
        entries = _emulator_entries(None)
        pids = [e["pid"] for e in entries]
        # When project= is not given, try to infer project_resolved from the
        # first entry so the log tail can still inform the state — but do NOT
        # surface it in the output (callers that didn't filter by project see
        # all emulators and a single project_resolved would be misleading).
        first = entries[0] if entries else {}
        project_resolved = first.get("project_resolved")
        matched_path = None
        surface_project_resolved = False

    # Tail the last lifecycle line from the emulator log — used to detect
    # demo_expired and to surface diagnostic context in the result.
    log_project = project if project is not None else project_resolved
    last_lifecycle_line: str | None = (
        _tail_last_lifecycle_line(log_project) if log_project else None
    )

    port = runtime_probe_port(cfg)
    reachable = _tcp_probe(runtime_probe_host(cfg), port)

    # State derivation — demo_expired wins over all other conditions so the
    # caller understands WHY the emulator isn't serving, even after it exits.
    if last_lifecycle_line and _DEMO_EXPIRED_RE.search(last_lifecycle_line):
        state = "demo_expired"
    elif pids and reachable:
        state = "running"
    elif pids:
        # Process is up but port not serving — distinguish a hung-with-window
        # (zombie) from a still-initialising (starting) emulator.
        state = "zombie" if _any_pid_has_window(pids) else "starting"
    else:
        state = "stopped"

    out = {"state": state, "running": state == "running", "pids": pids,
           "port": port, "port_reachable": reachable,
           "last_lifecycle_line": last_lifecycle_line,
           "checked_at": _now_iso()}
    if surface_project_resolved and project_resolved is not None:
        out["project_resolved"] = project_resolved
    if matched_path is not None:
        out["matched_path"] = matched_path
    if not pids and reachable:
        out["hint"] = (
            f"Port :{port} is serving, but NOT by the emulator — likely the "
            "UpdateSvc-deployed runtime (same exe). Check optix_runtime_status; "
            "starting the emulator now may hit a port conflict."
        )
    elif state in ("starting", "zombie"):
        out["hint"] = (
            f"Emulator process is up but :{port} isn't serving yet — still "
            "building, or hung. Wait/re-check before an optix_cdp_screenshot."
        )
    elif state == "demo_expired":
        out["hint"] = (
            "The FTOptix demo licence has expired — the emulator cannot run "
            "until the licence is renewed or a full licence is applied."
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


_BIND_FAIL_RE = re.compile(
    r"(EADDRINUSE|[Aa]ddress already in use|[Ff]ailed to bind|"
    r"[Cc]ould not bind|[Bb]ind.*[Ff]ailed|[Pp]ort.*(?:in use|already used)|"
    r"[Ww]eb[Ss]erver.*(?:fail|error)|BindException|"
    r"[Ss]ocket.*(?:bind|listen).*(?:fail|error))",
    re.IGNORECASE,
)
_PORT_IN_LOG_RE = re.compile(r":(\d{2,5})\b")


def _scan_bind_fail(project: str) -> dict | None:
    """Scan the runtime log for a bind-fail signature.

    Returns ``{"port": int|None, "holder": str}`` when a bind-failure line is
    found in the most-recent FTOptixRuntime.*.log for ``project``, else
    ``None``.  Only reads the last 64 KiB of the log (brief, non-held open).
    """
    log_dir = _emulator_log_dir(project)
    if not log_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in log_dir.glob("FTOptixRuntime.*.log") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not candidates:
        return None
    log = candidates[0]
    try:
        with open(log, "rb") as fh:
            sz = log.stat().st_size
            tail_bytes = min(sz, 65536)
            if tail_bytes < sz:
                fh.seek(sz - tail_bytes)
            data = fh.read(tail_bytes)
        lines_text = data.decode("utf-8", errors="replace").splitlines()
        for ln in reversed(lines_text):
            if _BIND_FAIL_RE.search(ln):
                m = _PORT_IN_LOG_RE.search(ln)
                fail_port = int(m.group(1)) if m else None
                return {"port": fail_port, "holder": ln.strip()}
    except OSError:
        pass
    return None


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

    `lines` is the tail joined into a SINGLE `<untrusted>`-delimited string
    (runtime log output is project/NetLogic-emitted, so it reads as DATA, never
    instructions — see `_untrusted`), NOT a list[str]. `returned_lines` still
    reports the count. Split on newlines after stripping the wrapper if you need
    per-line iteration.
    """
    log_dir = _emulator_log_dir(project)
    if not log_dir.is_dir():
        return {"error": "no_log_dir", "project": project,
                "hint": (f"no emulator log dir at {log_dir} — the emulator has "
                         "never run for this project (optix_emulator(action='run') first)")}
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
            "mtime": _now_iso(st.st_mtime),
            "lines": _untrusted("\n".join(tail), "runtime_log"),
            "returned_lines": len(tail), "truncated": truncated,
            "filtered": bool(contains)}


def bridge_log_tail(
    cfg: Config, lines: int = 100, contains: str | None = None,
    max_bytes: int = 262144,
) -> dict:
    """Tail the bridge transport diagnostics log (state_dir/logs/bridge.jsonl) —
    the forensic trail for why the design-time bridge dropped/timed out.

    Each line is a JSON event {ts, path, method, latency_ms, status, ok, error}.
    `contains` filters case-insensitively AFTER the tail window is read. Returns
    parsed events (oldest→newest) plus the current `last_ok`, or
    {error:"no_bridge_log"} when nothing has been logged yet (fresh install / no
    bridge call since this build)."""
    d = cfg.state_dir / "logs"
    p = d / "bridge.jsonl"
    if not p.is_file():
        return {"error": "no_bridge_log", "file": str(p), "last_ok": _bridge_last_ok_at,
                "hint": ("no bridge calls logged yet — bridge.jsonl is written on the "
                         "first bridge call; make one (e.g. optix_bridge_status) then retry")}
    st = p.stat()
    with open(p, "rb") as fh:
        if st.st_size > max_bytes:
            fh.seek(st.st_size - max_bytes)
            data = fh.read(max_bytes).split(b"\n", 1)[-1]
            truncated = True
        else:
            data = fh.read()
            truncated = False
    raw_lines = data.decode("utf-8", errors="replace").splitlines()
    if contains:
        needle = contains.lower()
        raw_lines = [ln for ln in raw_lines if needle in ln.lower()]
    tail = raw_lines[-max(1, int(lines)):]
    events: list = []
    for ln in tail:
        try:
            events.append(json.loads(ln))
        except ValueError:
            events.append({"raw": ln})
    return {"file": str(p), "size": st.st_size, "mtime": _now_iso(st.st_mtime),
            "events": events, "returned": len(events), "truncated": truncated,
            "filtered": bool(contains), "last_ok": _bridge_last_ok_at}


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


def _bridge_drop_note(cfg: Config) -> str | None:
    """If the design-time bridge was working earlier this session but is
    unreachable NOW, return a one-step recovery nudge; else None.

    Detection-based, never preemptive: a NetLogic (C#) recompile unloads the
    in-Studio bridge listener's assembly and kills it, but a pure model/YAML save
    doesn't recompile — so the bridge survives some ops and not others. We nudge
    only when it actually dropped (last_ok set, but unreachable now)."""
    if not cfg.bridge_enabled or _bridge_last_ok_at is None:
        return None  # disabled, or never used this session — nothing to recover
    if bridge_state(cfg, force=True).get("available"):
        return None  # survived
    return (
        "design-time bridge is unreachable after this operation. A NetLogic (C#) "
        "recompile unloads the in-Studio bridge listener (a model/YAML-only save "
        f"leaves it alive; last OK {_bridge_last_ok_at}). Restore authoring: right-"
        "click the StudioBridge NetLogic node in Studio -> Run -> StartBridge. "
        "Reads/writes fall back or fail until then; see optix_bridge_log_tail."
    )


def restart_emulator(
    cfg: Config, project: str, runner: Runner = _DEFAULT_RUNNER,
) -> dict:
    """Stop-if-running -> start -> wait serving. THE way to make a structural
    edit visible: one call replaces the status/stop/run dance and removes the
    F5-toggle footgun entirely (F5 on a running emulator stops it).

    A restart rebuilds; if that recompiled NetLogic it will have dropped the
    design-time bridge, so we attach a `bridge_note` recovery nudge when the
    bridge was working before but is unreachable after (see _bridge_drop_note)."""
    # Invalidate the per-project web-port cache before the restart so the
    # port is re-resolved from the live model / YAML after the bounce.
    _invalidate_web_port_cache(project)
    # MISMATCH GUARD: check BEFORE the stop action — stopping the wrong
    # project's emulator is silent data loss on a multi-Studio box.
    _running_entries = _emulator_entries()          # all instances, no project filter
    for _re in _running_entries:
        _resolved = _re.get("project_resolved")
        if _resolved and _resolved.casefold() != project.casefold():
            audit(cfg, "emulator_restart_refused_mismatch",
                  project=project, resolved=_resolved)
            return {
                "ok": False,
                "error": "runtime_project_mismatch",
                "requested": project,
                "resolved": _resolved,
                "pid": _re["pid"],
                "nudge": (
                    f"The running emulator belongs to project '{_resolved}', not "
                    f"'{project}'. Stop it first "
                    f"(optix_emulator action='stop' project='{_resolved}'), then "
                    "retry. The service will not stop or restart while another "
                    "project's runtime is running."
                ),
            }
    st = emulator_status(cfg, runner)
    stopped = None
    if st.get("pids"):
        stopped = stop_emulator(cfg, runner, status=st)
    out = run_emulator(cfg, project, save_first=False, wait_ready=True, runner=runner)
    out["restarted"] = bool(st.get("pids"))
    if stopped is not None:
        out["stopped_pids"] = stopped.get("killed_pids", [])
    note = _bridge_drop_note(cfg)
    if note:
        out["bridge_note"] = note
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


def stop_emulator(
    cfg: Config, runner: Runner = _DEFAULT_RUNNER, status: dict | None = None,
) -> dict:
    """Stop the local FTOptixRuntime emulator by terminating its process(es).

    An explicit, unambiguous stop — vs F5, which toggles and is easy to double-fire.
    Terminates ONLY emulator instances (command-line-matched via emulator_status);
    an UpdateSvc-deployed runtime is the same exe and is deliberately left alone.
    `status` lets a caller that JUST ran emulator_status (restart_emulator) hand
    the result in instead of paying a second scan. Kill is TerminateProcess
    (same semantics as the old Stop-Process -Force), then a short wait so the
    post-kill re-check doesn't race the process teardown.
    Returns {stopped, killed_pids, still_running}.
    """
    audit(cfg, "emulator_stop")
    st = status if status is not None else emulator_status(cfg, runner)
    if not st["pids"]:  # pids, not `running` — a "starting" emulator must be stoppable
        return {"stopped": False, "reason": "not_running", "killed_pids": []}
    procs = []
    try:
        for pid in st["pids"]:
            try:
                p = psutil.Process(pid)
                p.kill()
                procs.append(p)
            except psutil.NoSuchProcess:
                pass  # already gone — that's a successful stop
        psutil.wait_procs(procs, timeout=5)
    except psutil.Error as e:
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


def _port_listener(port: int) -> dict | None:
    """Return ``{"pid": int, "name": str}`` for the process listening on TCP
    ``port``, or ``None`` when nothing is listening or on any access error.

    Uses an in-process psutil scan (same approach as the bridge-PID helper).
    """
    try:
        for c in psutil.net_connections(kind="tcp"):
            if (c.status == psutil.CONN_LISTEN and c.laddr
                    and c.laddr.port == port and c.pid):
                try:
                    name = psutil.Process(c.pid).name()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    name = "?"
                return {"pid": c.pid, "name": name}
    except (psutil.Error, OSError):
        pass
    return None


def doctor(cfg: Config, project: str | None = None) -> dict:
    """One-call dependency check for a layman: every prerequisite + a plain fix.

    Returns {ready, checks:[{name, ok, required, detail, fix}]}. `ready` is True
    when all REQUIRED checks pass (Studio + projects root); feature checks
    (bridge / cdp / deploy / session) are reported but gate only their own
    feature, with a plain-English fix for each red item.

    (1.0.8) `project`, when given, adds one NON-required "project"
    row reporting where that name resolved and WHICH source answered
    (resolve_project_info's provenance: projects_root, or an out-of-root
    directory proven open in Studio). This is the observable surface for the
    fallback — "it resolved, but not from projects_root" is otherwise
    invisible, and a failure here carries the resolved_from breadcrumb.

    Also adds a NON-required "web_port" row (I28 rule 3)
    with three structured fields: ``project_says`` (port the project's own
    WebPresentationEngine is configured to use), ``service_expects`` (port
    resolve_web_port() will actually use, including any OPTIX_RUNTIME_TEST_PORT
    override), and ``listening`` (``{pid, name}`` of whatever process currently
    holds that port, or ``None``).  ``ok`` is True when nothing unexpected
    holds the port.  The ``fix`` text OFFERS (never acts) to unset a
    conflicting OPTIX_RUNTIME_TEST_PORT.

    A "web_engine_bind_failed" row is appended when the runtime log contains
    a bind-failure signature; it carries ``port`` and ``holder`` fields.
    """
    checks: list[dict] = []

    def add(name, ok, detail, fix, required=False):
        checks.append({"name": name, "ok": bool(ok), "required": required,
                       "detail": str(detail), "fix": fix})

    add("studio_exe", cfg.studio_exe.is_file(), cfg.studio_exe,
        "Install FactoryTalk Optix Studio, or set FTOPTIX_STUDIO_EXE to FTOptixStudio.exe.",
        required=True)
    add("projects_root", cfg.projects_root.is_dir(), cfg.projects_root,
        "Create the projects folder, or set OPTIX_PROJECTS_ROOT.", required=True)

    if project:
        try:
            _info = resolve_project_info(cfg, project)
            add("project", True,
                f"{_info['dir']} (resolved_from={_info['source']})",
                "Nothing to fix. resolved_from=studio_process means this project "
                "lives OUTSIDE the projects root and was matched to a directory a "
                "running Studio has open — it resolves only while Studio holds it.")
        except CoreError as _exc:
            add("project", False, str(_exc),
                "Put the project under the projects root, or open it in Studio "
                "(optix_project action=\"open\") so it resolves from the running "
                "Studio's command line.")

        # ---- web_port three-way diagnosis (I28 rule 3) ----
        # project_says: what the project's own WebPresentationEngine is configured
        #               to use (bridge or file path only — no env/default fallback)
        # service_expects: what resolve_web_port() will actually use (including
        #                  OPTIX_RUNTIME_TEST_PORT override and default)
        # listening: psutil scan of who currently holds the service_expects port
        _wp_proj = _resolve_project_engine_port(cfg, project)
        _wp_full = resolve_web_port(cfg, project)
        _wp_expect_port = _wp_full["port"] if _wp_full["port"] is not None else 8081
        _wp_listener = _port_listener(_wp_expect_port)

        # ok = nothing holds the port, or the holder is the Optix runtime itself
        _is_optix_proc = (
            _wp_listener is not None
            and "ftoptixruntime" in (_wp_listener.get("name") or "").lower()
        )
        _wp_ok = _wp_listener is None or _is_optix_proc

        # Build fix — always OFFER (never act) to unset conflicting env override
        _env_override = os.environ.get("OPTIX_RUNTIME_TEST_PORT")
        _wp_fix_parts: list[str] = []
        if _env_override and _wp_proj.get("source") == "project":
            _wp_fix_parts.append(
                f"OPTIX_RUNTIME_TEST_PORT={_env_override!r} is overriding the "
                f"project's configured port {_wp_proj['port']}. "
                "To let the project's port win, unset OPTIX_RUNTIME_TEST_PORT "
                "(e.g. `Remove-Item Env:OPTIX_RUNTIME_TEST_PORT` in PowerShell "
                "or `unset OPTIX_RUNTIME_TEST_PORT` in bash)."
            )
        if _wp_listener and not _is_optix_proc:
            _wp_fix_parts.append(
                f"Port :{_wp_expect_port} is held by "
                f"{_wp_listener.get('name')} (pid {_wp_listener.get('pid')}) — "
                "not the Optix runtime. Stop that process or change the project's "
                "WebPresentationEngine Port property "
                "(optix_bridge_ensure_web_engine) to a free port."
            )
        if not _wp_fix_parts:
            _wp_fix_parts.append(
                "Port is free or held by the Optix runtime. "
                "If a squatter is suspected, check with optix_runtime_status."
            )

        checks.append({
            "name": "web_port",
            "ok": _wp_ok,
            "required": False,
            "detail": (
                f"project_says={_wp_proj.get('port')!r} "
                f"service_expects={_wp_expect_port!r} "
                f"listening={_wp_listener!r}"
            ),
            "fix": " ".join(_wp_fix_parts),
            "project_says": _wp_proj.get("port"),
            "service_expects": _wp_expect_port,
            "listening": _wp_listener,
        })

        # ---- web_engine_bind_failed: scan runtime log for bind errors -------
        _bind_fail = _scan_bind_fail(project)
        if _bind_fail is not None:
            _bf_env = os.environ.get("OPTIX_RUNTIME_TEST_PORT")
            _bf_fix = (
                f"The runtime failed to bind port :{_bind_fail.get('port')}. "
                "Stop the squatter process (see web_port.listening above) or "
                "change the project's WebPresentationEngine port via "
                "optix_bridge_ensure_web_engine."
            )
            if _bf_env:
                _bf_fix += (
                    f" OPTIX_RUNTIME_TEST_PORT={_bf_env!r} may be the source of "
                    "the conflict — unset it to let the project's configured port "
                    "take effect."
                )
            checks.append({
                "name": "web_engine_bind_failed",
                "ok": False,
                "required": False,
                "detail": (
                    f"port={_bind_fail.get('port')!r} "
                    f"holder={_bind_fail.get('holder')!r}"
                ),
                "fix": _bf_fix,
                "port": _bind_fail.get("port"),
                "holder": _bind_fail.get("holder"),
            })

    # a single "bridge" row was ambiguous once
    # multi-instance support (v1.0.7) meant up to bridge_port_range ports
    # could each be independently armed or not — one ok/fail couldn't say
    # WHICH of them. Kept as exactly ONE row named "bridge" (unchanged name,
    # for back-compat with anything reading doctor()'s checks by name — see
    # test_doctor.py) when there's only one port to report (the legacy
    # bridge_url_pinned path, or a range collapsed to size 1); split into one
    # row per port, named "bridge :<port>", only when there's actually more
    # than one to distinguish.
    _bridge_fix = ("For LIVE authoring: open the project in Studio and right-click the "
                   "StudioBridge NetLogic -> StartBridge. Not needed for file-path edits.")
    _sockets = _scan_bridge_ports(cfg)
    if len(_sockets) <= 1:
        st = _sockets[0] if _sockets else {"available": False, "reason": "no ports configured",
                                            "bridge_version": None, "project": None}
        add("bridge", st["available"],
            f"version={st.get('bridge_version')} serving={st.get('project')} ({st.get('reason')})",
            _bridge_fix)
    else:
        for st in _sockets:
            detail = (f"version={st.get('bridge_version')} serving={st.get('project')}"
                       if st.get("available") else st.get("reason", "unreachable"))
            add(f"bridge :{st.get('port')}", st.get("available", False), detail, _bridge_fix)

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

    _tess = _find_tesseract()
    add("tesseract", bool(_tess), _tess or "(not found)",
        "For the zero-vision-token text tools (read_text/find_text, navigate "
        "expect_text, sweep OCR manifests): install Tesseract OCR (winget "
        "install UB-Mannheim.TesseractOCR). Everything else works without it.")
    add("pillow", _load_pil() is not None,
        "installed" if _load_pil() is not None else "(not installed)",
        "For pixel diff in optix_cdp_diff: pip install ftx-mcp[visual]. "
        "Without it, diff degrades to text-only mode (needs OCR manifests).")

    # Deploy prerequisite checks only exist when the deploy integration is
    # wired (it is not in the public distribution) — a red deploy row on a
    # server that cannot deploy is pure confusion.
    if cfg.enable_deploy:
        # U1: doctor sits at the `read` scope (auth.py DEFAULT_SCOPE_RULES /
        # TOOL_SCOPES) — deliberately, because "run this first on a new box" is
        # exactly what a low-privilege caller needs. But `read` is the whole
        # 27-tool introspection tier, so anything printed here is readable by
        # any agent that can list a project. deploy_password was already
        # reduced to a boolean for that reason; username and thumbprint are now
        # treated the same way. Doctor's question is "is this configured?",
        # which a boolean answers completely — the literal value only matters
        # when it is WRONG, and the `fix` string already names the env var.
        # The thumbprint keeps a last-4 tail so an operator can still tell two
        # certs apart without the field being the whole identifier.
        _tp = cfg.deploy_thumbprint
        add("deploy_username", bool(cfg.deploy_username),
            "set" if cfg.deploy_username else "MISSING",
            "For UpdateSvc deploy: set OPTIX_DEPLOY_USERNAME to a Windows account on the target.")
        add("deploy_password", bool(os.environ.get("OPTIX_STUDIO_DEPLOYMENT_PASSWORD")),
            "set" if os.environ.get("OPTIX_STUDIO_DEPLOYMENT_PASSWORD") else "MISSING",
            "For UpdateSvc deploy: set OPTIX_STUDIO_DEPLOYMENT_PASSWORD in the environment.")
        add("deploy_thumbprint", bool(_tp),
            f"set (...{_tp[-4:]})" if _tp else "MISSING",
            "For UpdateSvc deploy: set OPTIX_DEPLOY_THUMBPRINT (the UpdateSvc certificate thumbprint).")

    try:
        interactive = _is_interactive_session()
    except Exception:
        interactive = None
    add("interactive_session", interactive is not False, f"interactive={interactive}",
        "Run the service in an interactive logon session (session 1) so Studio/runtime "
        "launches and SendKeys save work.")

    lifecycle = _read_service_lifecycle(cfg)
    return {
        "ready": all(c["ok"] for c in checks if c["required"]),
        "checks": checks,
        "service_lifecycle": lifecycle,
    }


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
            "port": runtime_probe_port(cfg),
            "tcp_reachable": _tcp_probe(runtime_probe_host(cfg), runtime_probe_port(cfg)),
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
    # The 'test' slot is the runtime canvas — retargeted to the external
    # host/port in attach mode. 'mgmt' is the separate management HMI port and
    # stays loopback (it is not the attached runtime).
    if slot == "test":
        host = runtime_probe_host(cfg)
        port = runtime_probe_port(cfg)
    else:
        host = "127.0.0.1"
        port = int(os.environ.get("OPTIX_HMI_PORT", "8086"))
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        connected = s.connect_ex((host, port)) == 0
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
    # ATTACH MODE (U19): OPTIX_RUNTIME_URL is set, so an external runtime owns
    # its lifecycle — do NOT spawn a service-owned FTOptixRuntime.exe. Refuse at
    # entry (before any tree resolution / spawn) with the external shape.
    if attach_mode(cfg):
        return {
            "state": "external",
            "reason_code": "external_runtime",
            "project": project,
            "runtime_url": cfg.runtime_url,
            "pid": None,
            "nudge": (
                "OPTIX_RUNTIME_URL is set — the runtime is externally managed; "
                "the service won't spawn a runtime. Verify against it directly."
            ),
        }
    runtime_project_dir = _runtime_project_dir(cfg, project)
    bundled_exe = runtime_project_dir / "FTOptixApplication" / "FTOptixRuntime.exe"
    optix_path = runtime_project_dir / f"{project}.optix"

    probe_port = int(port) if port is not None else _project_web_port(cfg, project)
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
    if _tcp_probe(runtime_probe_host(cfg), probe_port, 0.5):
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
        if _tcp_probe(runtime_probe_host(cfg), probe_port, 0.5):
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


def _chrome_cdp_profile_dir() -> Path:
    """The CDP Chrome's user-data-dir (see bootstrap/install-chrome-cdp.ps1)."""
    root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(root) / "ftx-mcp" / "chrome-cdp-profile"


def _cleanup_stale_cdp_chrome(cfg: Config, runner: Runner = _DEFAULT_RUNNER) -> None:
    """Kill wedged chrome.exe from OUR CDP profile and clear its Singleton locks.

    A crashed/hung CDP Chrome leaves a chrome.exe holding the profile's
    SingletonLock, so a fresh task-launched Chrome can't bind the debug port.
    Scoped strictly by --remote-debugging-port + the chrome-cdp-profile
    user-data-dir on the command line, so a normal user Chrome is untouched.
    Best-effort — never raises.
    """
    from urllib.parse import urlparse
    port = urlparse(cfg.cdp_url).port or 9222
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
          "-ErrorAction SilentlyContinue | Where-Object { "
          f"$_.CommandLine -match '--remote-debugging-port={port}' -and "
          "$_.CommandLine -match 'chrome-cdp-profile' } | ForEach-Object { "
          "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        runner.run_powershell(ps, timeout=15)
    except Exception:
        pass
    profile = _chrome_cdp_profile_dir()
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile / lock).unlink()
        except OSError:
            pass


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

    # Tier 2 — clear any wedged CDP Chrome holding the profile SingletonLock,
    # then relaunch the task and wait for the port.
    _cleanup_stale_cdp_chrome(cfg, runner)
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
                          f"after {wait_seconds:.0f}s (see optix_status(action='doctor'))"}
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

    Every CDP tool (click/type/fill/key/screenshot/ocr/find_text/sweep/
    navigate) opens its OWN session through this one function, and each
    session is its own CDP client connection. Applying the viewport
    override HERE — once, right after the session is established — is what
    keeps a screenshot's coordinate space and a LATER, separate click's
    coordinate space identical: without it, a click call issued after a
    screenshot call would see whatever raw window size Chrome falls back to
    once the screenshot's session detaches. See _cdp.CDPClient.set_viewport
    (safe no-op on failure — a rejected override never blocks the session).

    Seam: tests monkeypatch service._cdp.CDPClient (or _connect_ws) so this
    runs without a live Chrome.
    """
    from . import _cdp
    heal = cfg.cdp_autoheal if _heal is None else _heal
    try:
        sess = _cdp.CDPClient(cfg.cdp_url)
    except (_cdp.CDPError, OSError) as e:
        if heal and ensure_chrome_cdp(cfg)["state"] in (
            "ok", "opened_page", "restarted"
        ):
            return _cdp_session(cfg, _heal=False)
        if isinstance(e, _cdp.CDPError):
            raise CDPUnavailable(str(e)) from e
        raise CDPUnavailable(f"CDP endpoint {cfg.cdp_url} unreachable: {e}") from e
    sess.set_viewport(cfg.cdp_viewport_width, cfg.cdp_viewport_height,
                       cfg.cdp_viewport_scale)
    return sess


def attach_mode(cfg: Config) -> bool:
    """True when OPTIX_RUNTIME_URL is set: the service ATTACHES to an external,
    already-running WebPresentationEngine rather than owning the runtime. In
    attach mode the runtime-management actions (F5 emulator, export-deploy
    runtime_start, web-engine provisioning) refuse — the external runtime owns
    its own lifecycle."""
    return bool(cfg.runtime_url)


def runtime_base_url(cfg: Config, project: str | None = None) -> str:
    """The base URL of the Optix web runtime canvas CDP navigation points at.

    Attach mode (runtime_url set): that URL, trailing-slash normalized — may be
    https:// and/or non-loopback (chrome-cdp tolerates the self-signed runtime
    cert via --ignore-certificate-errors; see bootstrap/install-chrome-cdp.ps1).
    Legacy: loopback on the project's WebPresentationEngine port when `project`
    is given (resolved via ``_project_web_port``), else the global runtime test
    port — byte-identical to the pre-I28 default when project is omitted."""
    if cfg.runtime_url:
        base = cfg.runtime_url
        return base if base.endswith("/") else base + "/"
    port = _project_web_port(cfg, project) if project else cfg.runtime_test_port
    return f"http://127.0.0.1:{port}/"


def runtime_probe_host(cfg: Config) -> str:
    """Host the TCP liveness probes connect to. Attach mode: the runtime_url
    hostname. Legacy: loopback (unchanged)."""
    if cfg.runtime_url:
        from urllib.parse import urlparse
        return urlparse(cfg.runtime_url).hostname or "127.0.0.1"
    return "127.0.0.1"


def runtime_probe_port(cfg: Config) -> int:
    """Port the TCP liveness probes connect to. Attach mode: the runtime_url
    port (or the scheme default 443/80 when the URL omits one). Legacy: the
    runtime test port (unchanged)."""
    if cfg.runtime_url:
        from urllib.parse import urlparse
        u = urlparse(cfg.runtime_url)
        if u.port is not None:
            return u.port
        return 443 if (u.scheme or "").lower() == "https" else 80
    return cfg.runtime_test_port


def _runtime_verify_url(cfg: Config, project: str | None = None) -> str:
    """The URL the CDP runtime-verify tools point at by default: the Optix
    runtime's web canvas. Loopback on the project's WebPresentationEngine port
    when ``project`` is given; otherwise the global runtime test port (legacy).
    The external runtime's URL in attach mode (OPTIX_RUNTIME_URL set) —
    see runtime_base_url."""
    return runtime_base_url(cfg, project)


# ---------------------------------------------------------------------------
# Per-project WebPresentationEngine port resolver (I28)
# ---------------------------------------------------------------------------
# Resolution order for resolve_web_port(cfg, project, explicit):
#   1. explicit parameter                   → source="explicit"
#   2. Project's WebPresentationEngine Port → source="project" | "no_engine"
#      a. via armed bridge (describe_node "UI/WebPresentationEngine")
#      b. offline parse of Nodes/UI/UI.yaml (no new YAML dependency)
#   3. OPTIX_RUNTIME_TEST_PORT env var      → source="env_override"
#   4. 8081 default                         → source="default"
#
# cfg.runtime_test_port is the env-override CARRIER (set by from_env) and is
# NEVER mutated here (I28 rule 4). A project's Port property is NEVER edited
# by this resolver (I28 rule 5).

_WEB_PORT_CACHE: dict[str, tuple[float, dict]] = {}   # project → (cached_at, result)
_WEB_PORT_CACHE_TTL: float = 30.0   # seconds

_UNTRUSTED_VALUE_RE = re.compile(r'<untrusted[^>]*>(.*?)</untrusted>', re.DOTALL)
_YAML_PORT_RE = re.compile(r"^\s*Port:\s*(\d+)\s*$")


def _invalidate_web_port_cache(project: str) -> None:
    """Drop the resolve_web_port TTL cache entry for `project`.

    Called on run_emulator and restart_emulator so a port change made
    between launches is re-read from the live model / YAML on the next
    project-scoped call rather than being served from stale cache."""
    _WEB_PORT_CACHE.pop(project, None)


def reset_web_port_cache() -> None:
    """Test hook: drop the entire resolve_web_port cache between cases."""
    global _WEB_PORT_CACHE
    _WEB_PORT_CACHE = {}


def _engine_port_from_bridge_node(node: dict) -> int | None:
    """Extract the integer Port from a describe_node response's properties list.

    The bridge wraps property values in <untrusted source="bridge">…</untrusted>;
    this helper unwraps that envelope before parsing the integer."""
    for prop in node.get("properties", []):
        if not isinstance(prop, dict) or prop.get("name") != "Port":
            continue
        raw = prop.get("value")
        if raw is None:
            continue
        text = str(raw).strip()
        m = _UNTRUSTED_VALUE_RE.search(text)
        if m:
            text = m.group(1).strip()
        try:
            return int(text)
        except ValueError:
            pass
    return None


def _engine_port_from_yaml_span(lines: list[str], span: Any) -> int | None:
    """Scan lines within `span` (a NodeSpan from optix_model) for a Port: <int>
    property.  Returns the port integer, or None if no readable Port is found."""
    for i in range(span.start, span.end):
        m = _YAML_PORT_RE.match(lines[i])
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None


def _resolve_web_port_uncached(cfg: Config, project: str) -> dict:
    """Uncached inner resolver — called by resolve_web_port after a cache miss.

    Returns a {"port", "source", "detail"} dict.  Never raises; on any
    unexpected error the function falls through to the env-override / default
    levels rather than bubbling up an exception from port resolution."""
    # ---- (2a) Bridge path ------------------------------------------------
    _bridge_cfg = _bridge_cfg_for(cfg, project)
    if _bridge_cfg is not None:
        try:
            node = describe_node(_bridge_cfg, project, "UI/WebPresentationEngine")
            port = _engine_port_from_bridge_node(node)
            if port is not None:
                return {
                    "port": port,
                    "source": "project",
                    "detail": f"bridge: UI/WebPresentationEngine Port={port}",
                }
            return {
                "port": None,
                "source": "no_engine",
                "detail": (
                    "WebPresentationEngine found in live model but its Port "
                    "property could not be read; use optix_bridge_ensure_web_engine"
                ),
            }
        except NodeNotFound:
            return {
                "port": None,
                "source": "no_engine",
                "detail": (
                    "no WebPresentationEngine node under UI/ in live model; "
                    "use optix_bridge_ensure_web_engine to add one"
                ),
            }
        except Exception:
            pass  # bridge error — fall through to file path

    # ---- (2b) File path: offline parse of Nodes/UI/UI.yaml ---------------
    try:
        project_dir = resolve_project(cfg, project)
        ui_yaml = project_dir / "Nodes" / "UI" / "UI.yaml"
        if ui_yaml.is_file():
            from . import optix_model as _om
            lines = ui_yaml.read_bytes().decode("utf-8", errors="replace").splitlines()
            for i, ln in enumerate(lines):
                nm = _om._NAME_RE.match(ln)
                if not nm:
                    continue
                span = _om._scan_node(lines, i, nm)
                if span.node_type and "WebPresentation" in span.node_type:
                    port = _engine_port_from_yaml_span(lines, span)
                    if port is not None:
                        return {
                            "port": port,
                            "source": "project",
                            "detail": f"file: Nodes/UI/UI.yaml Port={port}",
                        }
                    return {
                        "port": None,
                        "source": "no_engine",
                        "detail": (
                            "WebPresentationEngine in Nodes/UI/UI.yaml has no "
                            "readable Port property; use optix_bridge_ensure_web_engine"
                        ),
                    }
            # UI.yaml exists but no engine node found
            return {
                "port": None,
                "source": "no_engine",
                "detail": (
                    "Nodes/UI/UI.yaml has no WebPresentationEngine node; "
                    "use optix_bridge_ensure_web_engine to add one"
                ),
            }
    except Exception:
        pass  # can't read project — fall through to env/default

    # ---- (3) OPTIX_RUNTIME_TEST_PORT env var (operator override) ----------
    env_port = os.environ.get("OPTIX_RUNTIME_TEST_PORT")
    if env_port:
        try:
            p = int(env_port)
            return {
                "port": p,
                "source": "env_override",
                "detail": f"OPTIX_RUNTIME_TEST_PORT={env_port}",
            }
        except ValueError:
            pass

    # ---- (4) Hard default -------------------------------------------------
    return {
        "port": 8081,
        "source": "default",
        "detail": "no project port configured and no env override; using default 8081",
    }


def _resolve_project_engine_port(cfg: Config, project: str) -> dict:
    """Resolve the project's own WebPresentationEngine port (bridge/file path only).

    Mirrors steps 2a and 2b of ``_resolve_web_port_uncached`` but does NOT
    fall through to the env-override or default levels — the intent is to
    report exactly what the PROJECT has configured, independent of any
    operator overrides.

    Returns ``{"port": int|None, "source": str, "detail": str}`` where
    ``source`` is one of:

    * ``"project"``   — a readable Port was found in the project tree
    * ``"no_engine"`` — the project's UI tree has no WebPresentationEngine
    * ``"unknown"``   — bridge is down AND the project file is unreadable;
                        port resolution fell back to env/default silently
    """
    # (2a) Bridge path
    _bridge_cfg = _bridge_cfg_for(cfg, project)
    if _bridge_cfg is not None:
        try:
            node = describe_node(_bridge_cfg, project, "UI/WebPresentationEngine")
            port = _engine_port_from_bridge_node(node)
            if port is not None:
                return {
                    "port": port,
                    "source": "project",
                    "detail": f"bridge: UI/WebPresentationEngine Port={port}",
                }
            return {
                "port": None,
                "source": "no_engine",
                "detail": (
                    "WebPresentationEngine found in live model but its Port "
                    "property could not be read"
                ),
            }
        except NodeNotFound:
            return {
                "port": None,
                "source": "no_engine",
                "detail": "no WebPresentationEngine node under UI/ in live model",
            }
        except Exception:
            pass  # bridge error — fall through to file path

    # (2b) File path: offline parse of Nodes/UI/UI.yaml
    try:
        project_dir = resolve_project(cfg, project)
        ui_yaml = project_dir / "Nodes" / "UI" / "UI.yaml"
        if ui_yaml.is_file():
            from . import optix_model as _om
            lines = ui_yaml.read_bytes().decode("utf-8", errors="replace").splitlines()
            for i, ln in enumerate(lines):
                nm = _om._NAME_RE.match(ln)
                if not nm:
                    continue
                span = _om._scan_node(lines, i, nm)
                if span.node_type and "WebPresentation" in span.node_type:
                    port = _engine_port_from_yaml_span(lines, span)
                    if port is not None:
                        return {
                            "port": port,
                            "source": "project",
                            "detail": f"file: Nodes/UI/UI.yaml Port={port}",
                        }
                    return {
                        "port": None,
                        "source": "no_engine",
                        "detail": (
                            "WebPresentationEngine in Nodes/UI/UI.yaml has no "
                            "readable Port property"
                        ),
                    }
            return {
                "port": None,
                "source": "no_engine",
                "detail": "Nodes/UI/UI.yaml has no WebPresentationEngine node",
            }
    except Exception:
        pass

    return {
        "port": None,
        "source": "unknown",
        "detail": "bridge down and project file unreadable; port source is unknown",
    }


def resolve_web_port(
    cfg: Config,
    project: str,
    explicit: int | None = None,
) -> dict:
    """Resolve the WebPresentationEngine TCP port for `project`.

    Returns ``{"port": int | None, "source": str, "detail": str}``
    where ``source`` is one of:

    * ``"explicit"``     — caller supplied an explicit port (highest priority)
    * ``"project"``      — read from the project's WebPresentationEngine node
    * ``"no_engine"``    — project confirmed to have no engine; port is None
    * ``"env_override"`` — OPTIX_RUNTIME_TEST_PORT env var is set
    * ``"default"``      — fallback default 8081

    Resolution order (first match wins):

    1. ``explicit`` parameter.
    2. Project's WebPresentationEngine Port — via the armed design-time bridge
       (``describe_node("UI/WebPresentationEngine")``) when Studio is open,
       or offline parse of ``Nodes/UI/UI.yaml`` using the existing
       ``optix_model`` regex reader (no new YAML dependency).  A project
       whose UI tree has no engine node returns ``port=None,
       source="no_engine"`` with a ``detail`` nudge naming
       ``optix_bridge_ensure_web_engine``.
    3. ``OPTIX_RUNTIME_TEST_PORT`` env var — logged as an explicit operator
       override (``source="env_override"``).
    4. Hard default 8081 (``source="default"``).

    Results for sources 2–4 are cached per project for ``_WEB_PORT_CACHE_TTL``
    seconds.  The cache is invalidated by ``_invalidate_web_port_cache(project)``
    on ``run_emulator`` and ``restart_emulator`` so a port change made between
    launches is picked up.

    ``cfg.runtime_test_port`` is the env-override carrier (populated by
    ``Config.from_env``) and is NEVER mutated here (I28 rule 4).  A project's
    Port property is NEVER edited by this resolver (I28 rule 5).
    """
    # (1) Explicit parameter — ephemeral; not cached
    if explicit is not None:
        return {
            "port": int(explicit),
            "source": "explicit",
            "detail": f"caller-supplied port {int(explicit)}",
        }

    # (2)–(4) Check TTL cache
    now = time.time()
    cached = _WEB_PORT_CACHE.get(project)
    if cached is not None and (now - cached[0]) < _WEB_PORT_CACHE_TTL:
        return cached[1]

    result = _resolve_web_port_uncached(cfg, project)
    _WEB_PORT_CACHE[project] = (now, result)
    return result


def _project_web_port(cfg: Config, project: str) -> int:
    """Convenience wrapper: resolve_web_port and return the integer port,
    falling back to ``runtime_probe_port(cfg)`` when resolution yields
    ``None`` (no_engine or unresolvable).  Use this instead of
    ``cfg.runtime_test_port`` in project-scoped functions."""
    resolved = resolve_web_port(cfg, project)
    port = resolved.get("port")
    return port if port is not None else runtime_probe_port(cfg)


def _maybe_follow_port(cfg: Config, project: str | None) -> dict | None:
    """Self-heal port-follow: if the expected project web-port is dead but the
    emulator process is alive, re-resolve (bypassing the stale cache) and
    redirect to the port the emulator is actually serving.

    Returns ``{"expected": int, "actual": int, "project": str}`` when a
    follow occurred, ``None`` otherwise.  Never raises; exceptions are
    swallowed so the caller proceeds unchanged.

    Algorithm (I28 rule 2):
      1. Resolve the expected port (may come from TTL cache).
      2. TCP-probe the expected port — if reachable, nothing to do.
      3. Check ``_emulator_pids()`` — if the emulator is dead, don't follow.
      4. Invalidate the TTL cache and re-resolve fresh.
      5. TCP-probe the freshly resolved port.
      6. If alive and different: emit a loud stderr log line and return info.
    """
    if project is None:
        return None
    if attach_mode(cfg):
        # Attach mode points at an external runtime URL; emulator tracking
        # doesn't apply.
        return None
    try:
        host = runtime_probe_host(cfg)
        expected_port = _project_web_port(cfg, project)
        if _tcp_probe(host, expected_port):
            return None  # expected port is live — nothing to do
        if not _emulator_pids():
            return None  # emulator is not running; can't follow
        # Cache may be stale — bust it and re-resolve
        _invalidate_web_port_cache(project)
        fresh = resolve_web_port(cfg, project)
        actual_port = fresh.get("port")
        if actual_port is None or actual_port == expected_port:
            return None  # re-resolver found the same (or no) port
        if not _tcp_probe(host, actual_port):
            return None  # newly resolved port is also dead
        import sys
        print(
            f"[ftx-mcp] {project} serves :{actual_port}, "
            f"expected :{expected_port} — following the project",
            file=sys.stderr,
            flush=True,
        )
        return {"expected": expected_port, "actual": actual_port,
                "project": project}
    except Exception:
        return None  # never crash the caller


def _point_screenshot_at_runtime(
    cfg: Config, sess: Any, navigate_url: str | None, settle: float,
    project: str | None = None,
) -> bool:
    """Point the CDP page for a screenshot and return whether we navigated.

    So the model never has to know (or pass) the runtime URL, yet a
    click→screenshot-result flow doesn't get its state wiped:
      - navigate_url given (non-empty): go there.
      - navigate_url is None: go to the runtime URL, but ONLY if the tab isn't
        already on it — re-navigating would reload the Optix SPA and lose any
        prior click/nav state.
      - navigate_url == "": never navigate (screenshot the current tab as-is).

    When ``project`` is supplied the URL is resolved via the project's
    ``WebPresentationEngine`` port (``_runtime_verify_url`` → ``_project_web_port``).

    Note: this helper does NOT sleep after navigation. The settle sleep is
    applied unconditionally by the caller (cdp_screenshot_runtime) before
    capture, so the total settle fires exactly once regardless of whether a
    navigation occurred.
    """
    if navigate_url == "":
        return False
    if navigate_url:
        sess.navigate(navigate_url)
        return True
    # Auto-target the runtime; skip the reload if we're already there.
    target = _runtime_verify_url(cfg, project)
    origin = target.rstrip("/")
    try:
        current = sess.current_url()
    except Exception:
        current = ""
    if current.startswith(origin):
        return False
    sess.navigate(target)
    return True


def _navigate_if_given(sess: Any, navigate_url: str | None, settle: float) -> bool:
    """Navigate ONLY when an explicit truthy URL is given; return whether we
    navigated. Shared by cdp_click/type/key_runtime.

    Deliberately NOT `_point_screenshot_at_runtime`: that helper auto-navigates
    to the runtime URL when navigate_url is None, whereas click/type/key must do
    NOTHING without an explicit URL (they act on whatever the tab currently
    shows — re-navigating would wipe prior click/focus state). The regression
    guard is test_cdp.py::test_cdp_click_sends_trusted_mouse_sequence
    (asserts navigated is False with no URL).

    Note: this helper does NOT sleep after navigation. The settle sleep is
    applied unconditionally by the caller after the dispatch, so the total
    settle fires exactly once regardless of whether a navigation occurred.
    """
    if not navigate_url:
        return False
    sess.navigate(navigate_url)
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

    (x, y) may be normalized viewport fractions (both <= 1.0, resolved via
    sess.viewport_size()) or absolute CSS pixels (either > 1.0, passed through
    unchanged).  Out-of-frame points return state:"failed", error:"bad_point".

    Returns {state, x, y, css_x, css_y, viewport:{w,h}, coords, navigated,
    clicked_at} on success, or {state:"failed", error, ...} on failure.
    """
    audit(cfg, "cdp_click", x=x, y=y)
    from . import _cdp
    settle = cfg.cdp_settle_seconds if settle_seconds is None else settle_seconds
    sess = _cdp_session(cfg)
    try:
        navigated = _navigate_if_given(sess, navigate_url, settle)
        sess.set_viewport(cfg.cdp_viewport_width, cfg.cdp_viewport_height,
                          cfg.cdp_viewport_scale)
        px_point, perr = _resolve_point(sess, [x, y])
        if perr is not None:
            return {
                "state": "failed", "error": "bad_point", "detail": perr,
                "x": float(x), "y": float(y),
                "navigated": navigated, "clicked_at": _now_iso(),
            }
        css_x, css_y = px_point
        vp_w, vp_h = sess.viewport_size()
        coords = "normalized" if (float(x) <= 1.0 and float(y) <= 1.0) else "absolute"
        sess.click(css_x, css_y)
        time.sleep(max(0.0, settle))
        return {
            "state": "succeeded", "x": float(x), "y": float(y),
            "css_x": css_x, "css_y": css_y,
            "viewport": {"w": vp_w, "h": vp_h},
            "coords": coords,
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
        navigated = _navigate_if_given(sess, navigate_url, settle)
        tag = sess.active_element_tag()
        if tag in ("", "BODY", "HTML"):
            return {
                "state": "failed", "error": "no_focused_input",
                "active_element": tag or None, "navigated": navigated,
                "hint": ("nothing editable has keyboard focus — "
                         "optix_interact(action='click') the field first "
                         "(its cursor/selection confirms focus), then type"),
            }
        sess.insert_text(text)
        time.sleep(max(0.0, settle))
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
    project: str | None = None,
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
    "" to act on the current tab as-is).

    (x, y) may be normalized viewport fractions (both <= 1.0, resolved via
    sess.viewport_size()) or absolute CSS pixels (either > 1.0, passed through
    unchanged).  Out-of-frame points return state:"failed", error:"bad_point".

    Returns {state, steps: {clicked, focused_element, typed_chars, committed},
    x, y, css_x, css_y, viewport:{w,h}, coords, filled_at} on success.
    """
    audit(cfg, "cdp_fill", x=x, y=y, text=text)
    from . import _cdp
    if submit and submit not in _cdp.KEY_MAP:
        return {"state": "failed", "error": "invalid_key", "submit": submit,
                "valid_keys": sorted(_cdp.KEY_MAP)}
    settle = cfg.cdp_settle_seconds if settle_seconds is None else settle_seconds
    port_follow = _maybe_follow_port(cfg, project)
    steps: dict = {"clicked": False, "focused_element": None,
                   "typed_chars": 0, "committed": None}
    sess = _cdp_session(cfg)
    try:
        # Auto-target the runtime like optix_cdp_screenshot does — fill is
        # designed to be callable cold, and a fresh chrome-cdp tab sits on
        # about:blank where a click can never focus a field.
        navigated = _point_screenshot_at_runtime(cfg, sess, navigate_url, settle, project)
        sess.set_viewport(cfg.cdp_viewport_width, cfg.cdp_viewport_height,
                          cfg.cdp_viewport_scale)
        px_point, perr = _resolve_point(sess, [x, y])
        if perr is not None:
            return {
                "state": "failed", "error": "bad_point", "detail": perr,
                "steps": steps, "x": float(x), "y": float(y),
                "navigated": navigated,
            }
        css_x, css_y = px_point
        vp_w, vp_h = sess.viewport_size()
        coords = "normalized" if (float(x) <= 1.0 and float(y) <= 1.0) else "absolute"
        sess.click(css_x, css_y)
        steps["clicked"] = True
        time.sleep(0.3)  # let the canvas move focus into its input overlay
        tag = sess.active_element_tag()
        steps["focused_element"] = tag or None
        if tag in ("", "BODY", "HTML"):
            return {"state": "failed", "error": "no_focused_input",
                    "steps": steps, "x": float(x), "y": float(y),
                    "navigated": navigated,
                    "hint": (f"the click at ({x}, {y}) did not focus an editable "
                             "field — check coordinates against a fresh "
                             "screenshot")}
        if select_all:
            sess.select_all()
        sess.insert_text(text)
        steps["typed_chars"] = len(text)
        if submit:
            sess.key(submit)
            steps["committed"] = submit
        time.sleep(max(0.0, settle))
        out: dict[str, Any] = {
            "state": "succeeded", "steps": steps, "x": float(x),
            "y": float(y), "css_x": css_x, "css_y": css_y,
            "viewport": {"w": vp_w, "h": vp_h}, "coords": coords,
            "navigated": navigated,
            "filled_at": _now_iso(), "error": None,
        }
        if port_follow is not None:
            out["port_followed"] = (
                f":{port_follow['expected']}→:{port_follow['actual']}"
            )
        return out
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
        navigated = _navigate_if_given(sess, navigate_url, settle)
        sess.key(key)
        time.sleep(max(0.0, settle))
        return {"state": "succeeded", "key": key, "navigated": navigated,
                "pressed_at": _now_iso(), "error": None}
    except _cdp.CDPError as e:
        return {"state": "failed", "error": str(e), "key": key,
                "navigated": False, "pressed_at": _now_iso()}
    finally:
        sess.close()


def _resolve_region(sess: Any, region: list[float] | None) -> tuple[list[float] | None, str | None]:
    """Resolve a screenshot `region` [x, y, w, h] to absolute CSS pixels.

    Convention: if EVERY value is <= 1.0 the whole list is normalized
    fractions of the viewport (resolved via sess.viewport_size() /
    Page.getLayoutMetrics); if any value is > 1 the list is already absolute
    pixels and is passed through as-is.

    Returns (pixel_region, None) on success, or (None, detail_str) for a
    malformed region. Callers turn detail_str into the standard
    {"state": "failed", "error": "bad_region", ...} shape — this helper never
    raises for bad input (only a genuine CDP transport error propagates, via
    sess.viewport_size()).
    """
    if region is None:
        return None, None
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        return None, "region must be [x, y, w, h]"
    try:
        x, y, w, h = (float(v) for v in region)
    except (TypeError, ValueError):
        return None, "region values must be numeric"
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        return None, "x/y must be >= 0 and w/h must be > 0"
    vp_w, vp_h = sess.viewport_size()
    if vp_w <= 0 or vp_h <= 0:
        return None, "could not resolve viewport size"
    if all(v <= 1.0 for v in (x, y, w, h)):
        px = [x * vp_w, y * vp_h, w * vp_w, h * vp_h]
    else:
        px = [x, y, w, h]
    if px[0] >= vp_w or px[1] >= vp_h:
        return None, f"region {px} outside viewport {vp_w:g}x{vp_h:g}"
    return px, None


# Last fresh(=True) capture digest per CDP endpoint, keyed by cfg.cdp_url:
# (sha256_hex, size_bytes). Used ONLY to detect the "stale tab after
# optix_emulator(action='restart')" failure mode (see cdp_screenshot_runtime) — a
# fresh capture that is byte-IDENTICAL to the previous fresh capture from
# the same endpoint is the empirical signature of a screenshot that never
# actually re-pointed at the restarted runtime. Never consulted for
# fresh=False calls (those intentionally may return the same frame, e.g.
# re-verifying nothing changed).
_LAST_FRESH_CAPTURE: dict[str, tuple[str, int]] = {}


def _capture_digest(data: bytes) -> tuple[str, int]:
    return hashlib.sha256(data).hexdigest(), len(data)


def _cdp_capture_once(
    sess: Any, cfg: Config, quality: int, region: list[float] | None,
) -> tuple[bytes | None, list[float] | None, str | None]:
    """One screenshot attempt: reapply the viewport override, resolve
    `region`, capture the JPEG. Returns (jpeg_bytes, resolved_region,
    bad_region_detail). `bad_region_detail` is set (jpeg/region both None)
    for a malformed region — the caller turns that into the standard
    state='failed', error='bad_region' shape. Factored out of
    cdp_screenshot_runtime so the stale-capture recovery path can call it
    a second time without duplicating the viewport/region/capture dance."""
    sess.set_viewport(cfg.cdp_viewport_width, cfg.cdp_viewport_height,
                       cfg.cdp_viewport_scale)
    px_region, err = _resolve_region(sess, region)
    if err is not None:
        return None, None, err
    clip = None
    if px_region is not None:
        clip = {"x": px_region[0], "y": px_region[1],
                "width": px_region[2], "height": px_region[3], "scale": 1}
    jpeg = sess.screenshot_jpeg(quality=quality, clip=clip)
    return jpeg, px_region, None


def cdp_screenshot_runtime(
    cfg: Config, save_path: str | None = None, quality: int = 65,
    navigate_url: str | None = None, settle_seconds: float | None = None,
    fresh: bool = False, region: list[float] | None = None,
    project: str | None = None,
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
    (capturing mid-navigation fails).

    fresh=True additionally guards against the "stale tab after
    optix_emulator(action='restart')" trap: the runtime process gets a new PID but
    keeps the SAME URL, so the auto-target check above sees the tab is
    "already there" and would otherwise skip navigating entirely, and even
    a forced reload can still be served from Chrome's cache instead of
    actually re-fetching from (and reconnecting to) the restarted runtime.
    So when fresh=True:
      1. if the auto-target above didn't navigate, force one with
         ignore_cache=True (bypasses cache — a plain reload can't).
      2. after capturing, compare this capture's bytes to the last fresh
         capture taken from this cfg.cdp_url. Byte-IDENTICAL is the
         observed signature of a screenshot that's still looking at a
         pre-restart frame (three restarts, three identical screenshots
         was the reported failure). On a match: close this CDP session,
         open a new one (drops any wedged debugger/session state), hard
         reload the runtime URL bypassing cache, wait settle again, and
         recapture ONCE — never loops, so a capture that's genuinely
         unchanged (nothing to see) isn't retried forever. The result
         carries `stale_recovery: {attempted, resolved}` so this is never
         silent — the caller (and the agent reading the JSON) can see a
         recovery fired instead of quietly re-serving a stale frame.
    fresh=False never touches this cache — repeated identical screenshots
    are an expected, legitimate outcome of "confirm nothing changed".

    Before capture, the emulated device viewport is (re)set to
    cfg.cdp_viewport_width x cfg.cdp_viewport_height @ cfg.cdp_viewport_scale
    (default 1280x720 @ 1, tune via OPTIX_CDP_VIEWPORT / OPTIX_CDP_SCALE) so
    the capture isn't clipped to chrome-cdp's launch window size. A rejected
    override (older Chrome/CDP) is a safe no-op — the capture still proceeds
    at whatever size is in effect. This is the SAME override every other CDP
    tool applies at session-open (see _cdp_session), so click/route-replay
    coordinates computed from this screenshot stay valid in a later call.

    region: optional [x, y, w, h] clip, resolved via CDP
    Page.captureScreenshot's native `clip`. Coordinate convention: if ALL
    four values are <= 1.0 they are normalized fractions of the viewport
    (resolved against Page.getLayoutMetrics); if any value is > 1 the whole
    list is absolute pixels. A malformed region (wrong length, negative,
    zero w/h, x/y outside the frame) returns state='failed',
    error='bad_region' — never raises.

    Returns {state, path|b64, size_bytes, navigated, captured_at, region}
    (+ `stale_recovery` when fresh=True triggered the recovery path above).
    `region` in the result is the resolved absolute-pixel [x, y, w, h] (or
    None when no region was requested).
    """
    import base64

    from . import _cdp
    settle = cfg.cdp_settle_seconds if settle_seconds is None else settle_seconds
    port_follow = _maybe_follow_port(cfg, project)
    sess = _cdp_session(cfg)
    try:
        navigated = _point_screenshot_at_runtime(cfg, sess, navigate_url, settle, project)
        if fresh and not navigated:
            # force a reload so a stale frame can never masquerade as current
            # (the auto-target skips re-navigation when already on the runtime).
            # ignore_cache=True: after optix_emulator(action='restart') the runtime
            # process is new but the tab/URL is unchanged, so a plain reload
            # can still be served from Chrome's cache — see _cdp.CDPClient.reload.
            sess.reload(ignore_cache=True)
            time.sleep(max(0.0, settle))
            navigated = True
        # Unconditional settle before capture — fires regardless of whether a
        # navigation occurred (including the already-on-origin no-nav case).
        # _point_screenshot_at_runtime no longer sleeps, so the total settle
        # is applied exactly once here.
        time.sleep(max(0.0, settle))
        # Deliberately AFTER navigate, BEFORE region resolution/capture: a
        # region resolved against the pre-override viewport would be wrong,
        # and a capture taken before the override is applied is the exact
        # clipped-HMI bug this exists to fix.
        jpeg, px_region, err = _cdp_capture_once(sess, cfg, quality, region)
        if err is not None:
            return {
                "state": "failed", "path": None, "b64": None, "size_bytes": 0,
                "navigated": navigated, "captured_at": _now_iso(),
                "error": "bad_region", "detail": err, "region": region,
            }

        stale_recovery: dict[str, bool] | None = None
        if fresh:
            digest = _capture_digest(jpeg)
            prev = _LAST_FRESH_CAPTURE.get(cfg.cdp_url)
            if prev is not None and prev == digest:
                sess.close()
                sess = _cdp_session(cfg)
                recover_target = navigate_url or _runtime_verify_url(cfg, project)
                sess.navigate(recover_target)
                sess.reload(ignore_cache=True)
                time.sleep(max(0.0, settle))
                navigated = True
                jpeg2, px_region2, err2 = _cdp_capture_once(sess, cfg, quality, region)
                if err2 is None:
                    digest2 = _capture_digest(jpeg2)
                    stale_recovery = {"attempted": True, "resolved": digest2 != digest}
                    jpeg, px_region, digest = jpeg2, px_region2, digest2
                else:
                    stale_recovery = {"attempted": True, "resolved": False}
            _LAST_FRESH_CAPTURE[cfg.cdp_url] = digest

        result: dict[str, Any] = {
            "state": "succeeded", "path": None, "b64": None,
            "size_bytes": len(jpeg), "navigated": navigated,
            "captured_at": _now_iso(), "region": px_region,
        }
        if stale_recovery is not None:
            result["stale_recovery"] = stale_recovery
            if not stale_recovery["resolved"]:
                result["next_step"] = (
                    "fresh capture stayed byte-identical to the prior fresh "
                    "capture even after a forced session reconnect + hard "
                    "reload — the runtime canvas genuinely hasn't changed "
                    "(or is not repainting at all). Check optix_emulator(action='status') "
                    "for a wedged process before assuming the edit failed to apply."
                )
        if port_follow is not None:
            result["port_followed"] = (
                f":{port_follow['expected']}→:{port_follow['actual']}"
            )
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


def _find_tesseract() -> str | None:
    """Resolve the tesseract binary: PATH first, then the standard Windows
    install dirs (winget/UB-Mannheim installs don't touch PATH — found live
    2026-07-17). Module-level (not nested in cdp_ocr_runtime) so
    cdp_read_text_runtime and cdp_find_text_runtime share the same
    resolution logic instead of duplicating it."""
    import shutil
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


def _tesseract_missing_hint() -> str:
    """Shared install hint for the tesseract_not_installed degradation
    contract (cdp_ocr_runtime, cdp_read_text_runtime, cdp_find_text_runtime)."""
    return (
        "Install Tesseract-OCR (Windows: `winget install "
        "UB-Mannheim.TesseractOCR`) — PATH is optional; the service also "
        "probes the standard install dirs. This is an OPT-IN fallback — "
        "the default verify path is a vision model on "
        "optix_cdp_screenshot, which needs no OCR."
    )


def _reconstruct_ocr_text(words: list[dict]) -> str:
    """Rebuild plain text from parsed tesseract TSV word rows: one line per
    (block, par, line) group in reading order, words space-joined, groups joined
    with newlines.

    Grouping mirrors _match_tsv_words' line key so both paths agree on what a
    "line" is. NOT byte-identical to tesseract's native plain-text renderer —
    that emits blank lines between paragraphs and its own inter-word spacing;
    this collapses each line to single spaces. Sufficient for the "does the
    screen say X" checks these tools serve; the confidence signal is the new
    value-add, not exact whitespace fidelity (see test_ocr.py)."""
    lines: list[str] = []
    current_key: tuple[int, int, int] | None = None
    current: list[str] = []
    for w in words:
        key = (w["block_num"], w["par_num"], w["line_num"])
        if key != current_key:
            if current:
                lines.append(" ".join(current))
            current = []
            current_key = key
        current.append(w["text"])
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)


def _ocr_confidence_fields(cfg: Config, words: list[dict]) -> dict:
    """Aggregate word-level tesseract confidence into the OCR success envelope.

    Returns {"confidence": {"mean", "min"}} as fractions in [0, 1] (tesseract
    reports 0..100 per word). Below cfg.ocr_conf_threshold (on the mean) adds
    `low_confidence: True` and a structured `next_step` nudge toward
    ground-truth reads. No words (empty frame) => {} (no confidence signal to
    report). The -1 aggregate rows are already dropped by _parse_tesseract_tsv's
    level==5 filter; the >= 0 guard is defensive belt-and-suspenders."""
    confs = [w["conf"] for w in words if w["conf"] >= 0]
    if not confs:
        return {}
    mean = sum(confs) / len(confs) / 100.0
    lowest = min(confs) / 100.0
    fields: dict = {"confidence": {"mean": round(mean, 4), "min": round(lowest, 4)}}
    if mean < cfg.ocr_conf_threshold:
        fields["low_confidence"] = True
        fields["next_step"] = (
            "OCR mean confidence is below the trust threshold — this text "
            "read-back may be wrong. For MODEL truth (what the project actually "
            "declares) use optix_describe_node; for RENDER truth (what the canvas "
            "actually shows) use optix_cdp_screenshot with return_image=true and "
            "read it with vision."
        )
    return fields


def _ocr_capture(
    cfg: Config, *, navigate_url: str | None, settle_seconds: float | None,
    psm: int, runner: Runner, region: list[float] | None, include_region: bool,
    project: str | None = None,
) -> dict:
    """Shared capture+OCR for cdp_ocr_runtime / cdp_read_text_runtime.

    Captures via the tested cdp_screenshot_runtime path, then runs tesseract in
    TSV mode (a single call) so word-level confidence is available; `text` is
    reconstructed from the parsed words. `include_region` gates the `region`
    key that only cdp_read_text_runtime carries. The tesseract_not_installed /
    screenshot-failure / nonzero-return degradation contracts are preserved
    byte-for-byte (never raises).
    """
    import tempfile
    tesseract = _find_tesseract()
    if tesseract is None:
        return {
            "state": "failed", "text": None, "error": "tesseract_not_installed",
            "hint": _tesseract_missing_hint(),
        }
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "runtime.jpg"
        shot = cdp_screenshot_runtime(
            cfg, save_path=str(img), navigate_url=navigate_url,
            settle_seconds=settle_seconds, region=region, project=project,
        )
        if shot.get("state") != "succeeded":
            out = {
                "state": "failed", "text": None,
                "error": shot.get("error", "screenshot_failed"),
                "navigated": shot.get("navigated", False),
            }
            if include_region:
                out["region"] = shot.get("region")
            return out
        # TSV mode: options precede the `tsv` configfile keyword, per tesseract's
        # `tesseract imagename outputbase [options] [configfile]` grammar. This
        # combined `--psm N tsv` form is NOT exercised elsewhere in-repo
        # (find_text's tsv call omits --psm) — validate on the Windows binary.
        proc = runner.run(
            [tesseract, str(img), "stdout", "--psm", str(int(psm)), "tsv"],
            timeout=30, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            out = {
                "state": "failed", "text": None,
                "error": (proc.stderr or "tesseract failed").strip()[:400],
                "navigated": shot.get("navigated", False),
            }
            if include_region:
                out["region"] = shot.get("region")
            return out
        words = _parse_tesseract_tsv(proc.stdout or "")
        # OCR'd canvas text is free-form runtime content — the primary injection
        # vector in this tool family — so delimit it as untrusted. Only the
        # reconstructed TEXT is wrapped; confidence fields stay numeric. The
        # failure paths above return text=None (nothing to delimit).
        out = {
            "state": "succeeded",
            "text": _untrusted(_reconstruct_ocr_text(words),
                               "cdp_read_text" if include_region else "cdp_ocr"),
            "size_bytes": shot.get("size_bytes", 0),
            "navigated": shot.get("navigated", False), "captured_at": _now_iso(),
        }
        if include_region:
            out["region"] = shot.get("region")
        out.update(_ocr_confidence_fields(cfg, words))
        return out


def cdp_ocr_runtime(
    cfg: Config, navigate_url: str | None = None,
    settle_seconds: float | None = None, *, psm: int = 6,
    runner: Runner = _DEFAULT_RUNNER, project: str | None = None,
) -> dict:
    """OCR the runtime canvas via tesseract — an OPT-IN, headless read-back fallback.

    Tesseract is resolved from PATH first, then the standard Windows install
    dirs (winget/UB-Mannheim installs don't touch PATH — found live 2026-07-17).

    The default verify path is a vision model reading optix_cdp_screenshot; this is
    for the case that path can't run (a cron/headless caller with no vision, or the
    blank-render edge we hit on the VM where a human still needs *some* text signal).
    It captures the runtime JPEG through the same tested screenshot path, then runs
    the `tesseract` binary on it in TSV mode to derive word-level confidence.

    Returns {state, text, size_bytes, navigated, captured_at, confidence:{mean,min}}.
    When the mean confidence falls below cfg.ocr_conf_threshold the result also
    carries low_confidence=True and a next_step nudge. If tesseract is not on
    PATH, returns state='failed', error='tesseract_not_installed' with an install
    hint rather than raising — it is optional infrastructure. Text-only: NOT a
    substitute for vision on color/layout checks.
    """
    return _ocr_capture(
        cfg, navigate_url=navigate_url, settle_seconds=settle_seconds,
        psm=psm, runner=runner, region=None, include_region=False, project=project,
    )


def cdp_read_text_runtime(
    cfg: Config, region: list[float] | None = None,
    navigate_url: str | None = None, settle_seconds: float | None = None,
    *, psm: int = 6, runner: Runner = _DEFAULT_RUNNER, project: str | None = None,
) -> dict:
    """OCR a region (or the full frame) of the runtime canvas via tesseract —
    THE cheap check for "does the screen/widget say X", zero vision tokens.

    Captures through the same region-clip path as cdp_screenshot_runtime (see
    its docstring for the region coordinate convention: values all <= 1.0 are
    normalized viewport fractions, any value > 1 means absolute pixels), then
    runs tesseract on the JPEG exactly like cdp_ocr_runtime. NOT a substitute
    for vision on color/layout checks — use cdp_screenshot_runtime for those.

    Returns {state, text, region, size_bytes, navigated, captured_at,
    confidence:{mean,min}} (+ low_confidence/next_step below threshold). If
    tesseract is not installed, returns state='failed',
    error='tesseract_not_installed' with an install hint — same degradation
    contract as cdp_ocr_runtime, never raises. A malformed region degrades the
    same way (state='failed', error='bad_region') via cdp_screenshot_runtime.
    """
    return _ocr_capture(
        cfg, navigate_url=navigate_url, settle_seconds=settle_seconds,
        psm=psm, runner=runner, region=region, include_region=True, project=project,
    )


def _parse_tesseract_tsv(tsv: str) -> list[dict]:
    """Parse `tesseract <img> stdout tsv` output into word-level rows.

    Columns per tesseract's TSV contract: level page_num block_num par_num
    line_num word_num left top width height conf text. Only level==5 (word)
    rows with non-empty text are kept — levels 1-4 are page/block/par/line
    aggregate pseudo-rows with no text of their own."""
    lines = tsv.strip("\n").split("\n")
    if not lines or not lines[0].strip():
        return []
    header = lines[0].split("\t")
    words: list[dict] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < len(header):
            continue
        row = dict(zip(header, cols, strict=False))
        if row.get("level") != "5":
            continue
        text = row.get("text", "")
        if not text.strip():
            continue
        try:
            words.append({
                "block_num": int(row["block_num"]), "par_num": int(row["par_num"]),
                "line_num": int(row["line_num"]), "word_num": int(row["word_num"]),
                "left": float(row["left"]), "top": float(row["top"]),
                "width": float(row["width"]), "height": float(row["height"]),
                "conf": float(row["conf"]), "text": text,
            })
        except (KeyError, ValueError):
            continue
    return words


def _match_tsv_words(words: list[dict], query: str) -> list[dict]:
    """Find `query` in tesseract word boxes: case-insensitive; a multi-word
    query is matched only against ADJACENT words (same block/par/line,
    consecutive word_num) joined with a single space. Words with conf < 40
    are dropped BEFORE matching — a filtered-out word breaks adjacency for
    its neighbors, so a low-confidence word inside a multi-word query
    prevents that query from matching at all (fail-loud over guessing).

    SCALE INVARIANT: the raw tesseract 0..100 value lives ONLY under the key
    `conf` (word rows out of _parse_tesseract_tsv). Anything named
    `confidence` — here and in _ocr_confidence_fields — is a fraction in
    [0, 1]. Before U8 this returned the raw 0..100 under `confidence`, so
    find_text reported 96.76 while its sibling OCR tools reported 0.7754 for
    a comparable read; an agent applying one threshold to both silently got
    it wrong. The `>= 40` filter below stays on the raw scale on purpose."""
    q = (query or "").strip()
    if not q:
        return []
    n = len(q.split())
    filtered = [w for w in words if w["conf"] >= 40]
    matches: list[dict] = []
    for i in range(len(filtered) - n + 1):
        window = filtered[i:i + n]
        if n > 1:
            adjacent = all(
                (a["block_num"], a["par_num"], a["line_num"]) ==
                (b["block_num"], b["par_num"], b["line_num"])
                and b["word_num"] == a["word_num"] + 1
                for a, b in zip(window, window[1:], strict=False)
            )
            if not adjacent:
                continue
        joined = " ".join(w["text"] for w in window)
        if joined.lower() != q.lower():
            continue
        left = min(w["left"] for w in window)
        top = min(w["top"] for w in window)
        right = max(w["left"] + w["width"] for w in window)
        bottom = max(w["top"] + w["height"] for w in window)
        matches.append({
            "text": joined,
            "confidence": round(min(w["conf"] for w in window) / 100.0, 4),
            "bbox_px": [left, top, right - left, bottom - top],
        })
    return matches


def _ocr_css_scale(
    jpeg: bytes, vp_w: float, vp_h: float, cfg: Config,
) -> tuple[float, float]:
    """Factor to convert OCR IMAGE pixels -> viewport CSS pixels.

    The screenshot is rendered at deviceScaleFactor (OPTIX_CDP_SCALE), so its
    pixel dimensions are the CSS viewport times that factor. Return
    (vp_w/img_w, vp_h/img_h) measured from the actual JPEG — self-correcting
    whether or not the viewport override took effect. Falls back to
    1/cfg.cdp_viewport_scale only if Pillow can't measure the image, and to
    (1.0, 1.0) if even that is unusable. At scale=1 the image equals the
    viewport, so the factor is 1.0 and callers are unchanged."""
    if vp_w <= 0 or vp_h <= 0:
        return 1.0, 1.0
    pil = _load_pil()
    if pil is not None:
        try:
            import io
            with pil.Image.open(io.BytesIO(jpeg)) as im:
                img_w, img_h = im.size
            if img_w > 0 and img_h > 0:
                return vp_w / img_w, vp_h / img_h
        except Exception:
            pass
    scale = cfg.cdp_viewport_scale
    if scale and scale > 0:
        return 1.0 / scale, 1.0 / scale
    return 1.0, 1.0


def cdp_find_text_runtime(
    cfg: Config, text: str, navigate_url: str | None = None,
    settle_seconds: float | None = None, runner: Runner = _DEFAULT_RUNNER,
    project: str | None = None,
) -> dict:
    """Locate `text` on the runtime canvas via tesseract TSV word boxes — to
    click a labeled control (center_px feeds cdp_click_runtime directly) or
    to build a navigation route. Requires tesseract (same degradation
    contract as cdp_read_text_runtime / cdp_ocr_runtime).

    Always a full-frame capture (no region — the point is to find where
    something is, before you know its coordinates). Matching is
    case-insensitive; a multi-word `text` is matched only against ADJACENT
    words on the same tesseract line (see _match_tsv_words). Words scoring
    below 40/100 raw are dropped before matching.

    Returns {state, found, matches: [{text, confidence, bbox_px: [x,y,w,h],
    bbox_norm: [x,y,w,h], center_px: [x,y]}], viewport: {w, h}, navigated,
    captured_at}. `matches[].confidence` is a fraction in [0, 1] — the same
    scale as cdp_ocr_runtime / cdp_read_text_runtime's confidence{mean,min},
    so one threshold reads correctly across all three.
    bbox_px / center_px are viewport CSS pixels (the space cdp_click_runtime
    dispatches in), NOT raw image pixels — so center_px stays correct even
    under OPTIX_CDP_SCALE>1, where the capture is rendered larger than the
    viewport (see _ocr_css_scale).
    No match is NOT an error: found=false, matches=[]. Tesseract
    missing => state='failed', error='tesseract_not_installed' (standard
    degradation contract, never raises).
    """
    from . import _cdp
    tesseract = _find_tesseract()
    if tesseract is None:
        return {
            "state": "failed", "found": False, "matches": [],
            "error": "tesseract_not_installed", "hint": _tesseract_missing_hint(),
        }
    audit(cfg, "cdp_find_text", text=text)
    settle = cfg.cdp_settle_seconds if settle_seconds is None else settle_seconds
    port_follow = _maybe_follow_port(cfg, project)
    sess = _cdp_session(cfg)
    try:
        navigated = _point_screenshot_at_runtime(cfg, sess, navigate_url, settle, project)
        vp_w, vp_h = sess.viewport_size()
        jpeg = sess.screenshot_jpeg()
    except _cdp.CDPError as e:
        return {"state": "failed", "found": False, "matches": [],
                "error": str(e), "navigated": False}
    finally:
        sess.close()

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "runtime.jpg"
        img.write_bytes(jpeg)
        proc = runner.run([tesseract, str(img), "stdout", "tsv"],
                          timeout=30, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            return {
                "state": "failed", "found": False, "matches": [],
                "error": (proc.stderr or "tesseract failed").strip()[:400],
                "navigated": navigated,
            }
        words = _parse_tesseract_tsv(proc.stdout or "")

    raw_matches = _match_tsv_words(words, text)
    # Tesseract boxes are in IMAGE pixels. The capture is rendered at the
    # emulated deviceScaleFactor (OPTIX_CDP_SCALE), so at scale>1 the image is
    # LARGER than the CSS viewport that clicks use — center_px would then feed
    # cdp_click_runtime a device-pixel coordinate and the click would land at
    # scale× the intended spot. Rescale image px -> CSS px by the MEASURED
    # image/viewport ratio (not 1/cfg.scale) so it self-corrects even if the
    # viewport override silently no-op'd on an older Chrome. At scale=1 the
    # image equals the viewport and this is an exact no-op. Result: bbox_px /
    # center_px are always in viewport CSS px, the same space as clicks.
    sx, sy = _ocr_css_scale(jpeg, vp_w, vp_h, cfg)
    result_matches = []
    for m in raw_matches:
        x, y, w, h = m["bbox_px"]
        x, y, w, h = x * sx, y * sy, w * sx, h * sy
        if vp_w > 0 and vp_h > 0:
            bbox_norm = [x / vp_w, y / vp_h, w / vp_w, h / vp_h]
        else:
            bbox_norm = [0.0, 0.0, 0.0, 0.0]
        result_matches.append({
            "text": m["text"], "confidence": m["confidence"],
            "bbox_px": [x, y, w, h], "bbox_norm": bbox_norm,
            "center_px": [x + w / 2, y + h / 2],
        })
    out = {
        "state": "succeeded", "found": bool(result_matches),
        "matches": result_matches, "viewport": {"w": vp_w, "h": vp_h},
        "navigated": navigated, "captured_at": _now_iso(),
    }
    if port_follow is not None:
        out["port_followed"] = (
            f":{port_follow['expected']}→:{port_follow['actual']}"
        )
    return out


# ---- cdp_navigate: blind navigation to a banked route (S5) ------------

def _load_routes_file(routes_path: str) -> tuple[dict | None, dict | None]:
    """Load + parse a navigation routes JSON file (see optix_cdp_navigate /
    the optix-blind-authoring skill for the format). Returns (data, None) on
    success, or (None, error_response) with the standard failed-envelope on
    a missing/unparseable/malformed file. Never raises."""
    path = Path(routes_path)
    if not path.is_file():
        return None, {"state": "failed", "error": "routes_file_not_found",
                       "routes_path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        return None, {"state": "failed", "error": "routes_file_invalid",
                       "routes_path": str(path), "detail": str(e)}
    if not isinstance(data, dict) or not isinstance(data.get("routes"), dict):
        return None, {"state": "failed", "error": "routes_file_invalid",
                       "routes_path": str(path),
                       "detail": "routes file must be a JSON object with a 'routes' map"}
    return data, None


def _validate_route_steps(steps: Any) -> tuple[list, str | None, int | None]:
    """Shape-validate a route's `steps` list before any CDP work starts.

    Checks structure only (click present, [x, y] shape, numeric values,
    optional settle_seconds/expect_text types) — NOT viewport bounds, which
    can only be resolved once a CDP session is open (see _resolve_point).
    Returns (steps, None, None) on success, or (steps, detail_str, bad_index)
    naming the first offending step. Never raises."""
    if not isinstance(steps, list) or not steps:
        return steps, "route has no steps", 0
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            return steps, f"step {i} is not an object", i
        click = step.get("click")
        if not isinstance(click, (list, tuple)) or len(click) != 2:
            return steps, f"step {i} missing/invalid 'click' [x, y]", i
        try:
            float(click[0]), float(click[1])
        except (TypeError, ValueError):
            return steps, f"step {i} 'click' values must be numeric", i
        settle = step.get("settle_seconds")
        if settle is not None:
            try:
                float(settle)
            except (TypeError, ValueError):
                return steps, f"step {i} 'settle_seconds' must be numeric", i
        expect_text = step.get("expect_text")
        if expect_text is not None and not isinstance(expect_text, str):
            return steps, f"step {i} 'expect_text' must be a string", i
    return steps, None, None


# ---- routes file management (S7): service owns routes CRUD end-to-end ----
#
# MOTIVATION: a Cowork field test needed to CREATE a routes file for
# optix_cdp_navigate/optix_cdp_sweep and, having no MCP tool to do it, the
# model reached for host folder-access permission instead — its own
# sandboxed file tools cannot see the ftx-mcp service's filesystem. That is
# exactly the failure mode this service exists to prevent: no client should
# ever need local file access to drive Optix. These three functions (+ the
# optix_routes_save/get/list tools that wrap them) make the service the sole
# owner of routes files end-to-end, so the intended loop is entirely
# server-side: optix_cdp_find_text (discover) -> optix_routes_save (bank) ->
# optix_cdp_navigate/optix_cdp_sweep (replay by name).

_DEV_NAME_RE = re.compile(r"[a-zA-Z0-9._-]+")


def _valid_dev_name(name: str) -> bool:
    """True if `name` is safe to use as a `dev/<name>.json` filename stem.

    No path separators and no leading dot. Combined with the fixed `dev/`
    prefix and `.json` suffix this is traversal-safe on its own — the
    allowed character class cannot encode '..', an absolute path, or a
    hidden/dotfile name, so there is nothing for resolve_subpath's
    is_relative_to check to catch that this doesn't already rule out.
    """
    return (
        bool(name)
        and "/" not in name
        and "\\" not in name
        and not name.startswith(".")
        and _DEV_NAME_RE.fullmatch(name) is not None
    )


def _normalize_routes_payload(routes: Any) -> tuple[Any, dict]:
    """Accept either the wrapped shape (`{"version": 1, "routes": {...},
    ...extras}`) or a bare `{route_name: {...}}` mapping. Returns
    (inner_routes_mapping, extra_top_level_keys). Any dict with a dict
    "routes" key is the wrapped shape — EXTRA top-level keys (e.g. the
    screen "structure" maps the blind-authoring cache banks alongside its
    routes) are preserved verbatim through save, not rejected: the loader
    (_load_routes_file) has always tolerated them, and a combined
    routes+structure cache in one dev/ file is the intended workflow. A
    bare mapping containing a route literally named "routes" misdetects as
    wrapped — accepted edge case (documented tradeoff).
    """
    if isinstance(routes, dict) and isinstance(routes.get("routes"), dict):
        extras = {k: v for k, v in routes.items() if k not in ("version", "routes")}
        return routes["routes"], extras
    return routes, {}


def routes_save(cfg: Config, project: str, routes: dict, name: str = "ftx_ui_map") -> dict:
    """Write a routes file under `<project_dir>/dev/<name>.json` — the
    service-owned save half of the routes-banking loop (see the S7
    MOTIVATION comment above _valid_dev_name).

    `routes` accepts either the full versioned shape (`{"version": 1,
    "routes": {...}}`, as read back by routes_get/cdp_navigate) or a bare
    `{route_name: {"steps": [...]}, ...}` mapping — both normalize to the
    versioned shape on disk (see _normalize_routes_payload). Every route's
    `steps` is validated with the SAME _validate_route_steps check
    cdp_navigate_runtime uses, BEFORE anything is written: a malformed step
    anywhere in the payload fails the whole save with error='routes_invalid'
    naming the offending route and step index — never a partial write.

    `name` is sanitized to `[a-zA-Z0-9._-]`, no path separators, no leading
    dot (see _valid_dev_name); anything else fails with error='bad_name'
    rather than touching the filesystem. `dev/` is created if missing.
    Saving over an existing `name` REPLACES its content wholesale (atomic
    tmp-file + os.replace, UTF-8) — it is not a merge.

    Returns {state: 'succeeded', path, routes: [route names], bytes} on
    success. `path` is the absolute path to the written file and is directly
    usable as `routes_path` for cdp_navigate_runtime / cdp_sweep_runtime —
    no separate lookup needed.
    """
    project_dir = resolve_project(cfg, project)
    if not _valid_dev_name(name):
        return {"state": "failed", "error": "bad_name", "name": name}
    inner, extras = _normalize_routes_payload(routes)
    if not isinstance(inner, dict):
        return {
            "state": "failed", "error": "routes_invalid",
            "detail": "routes must be an object mapping route name -> {steps: [...]}",
        }
    for route_name, route_def in inner.items():
        raw_steps = route_def.get("steps") if isinstance(route_def, dict) else None
        _, verr, bad_idx = _validate_route_steps(raw_steps)
        if verr is not None:
            return {
                "state": "failed", "error": "routes_invalid",
                "route": route_name, "step": bad_idx, "detail": verr,
            }
    dev_dir = project_dir / "dev"
    dev_dir.mkdir(parents=True, exist_ok=True)
    target = dev_dir / f"{name}.json"
    payload = {"version": 1, "routes": inner, **extras}
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    tmp = dev_dir / f".{name}.json.tmp-{os.getpid()}"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    audit(cfg, "routes_save", project=project, name=name, path=str(target),
          routes=sorted(inner.keys()))
    return {
        "state": "succeeded", "path": str(target),
        "routes": sorted(inner.keys()), "bytes": len(text.encode("utf-8")),
    }


def routes_get(cfg: Config, project: str, name: str = "ftx_ui_map") -> dict:
    """Read back a routes file saved with routes_save (or hand-banked at the
    same `dev/<name>.json` convention) — {state, path, routes: <full parsed
    versioned dict>} on success.

    Uses the SAME loader as cdp_navigate_runtime/cdp_sweep_runtime
    (_load_routes_file), so "routes_get says it's fine" and "cdp_navigate
    can read it" are the same guarantee. A bad `name` fails with
    error='bad_name' before touching disk (see _valid_dev_name). A missing
    file fails with error='routes_file_not_found' and `path` naming the
    exact path looked for; unparseable/malformed JSON fails with
    error='routes_file_invalid'. Never raises for a missing/bad file.
    """
    project_dir = resolve_project(cfg, project)
    if not _valid_dev_name(name):
        return {"state": "failed", "error": "bad_name", "name": name}
    path = project_dir / "dev" / f"{name}.json"
    data, err = _load_routes_file(str(path))
    if err is not None:
        err = dict(err)
        err["path"] = err.pop("routes_path")
        return err
    return {"state": "succeeded", "path": str(path), "routes": data}


def routes_list(cfg: Config, project: str) -> dict:
    """List every routes file saved under `<project_dir>/dev/*.json` — the
    discovery half of the routes-banking loop, for "what routes have I
    already banked for this project".

    Only files that parse as a valid routes file (JSON object with a dict
    "routes" key — same shape check as _load_routes_file) are listed; a
    `dev/*.json` file that fails to parse or doesn't match that shape (a
    stray unrelated JSON file someone dropped in dev/) is skipped SILENTLY
    per-file rather than failing the whole listing, and only counted in
    `skipped` — one bad file should never hide the rest.

    Returns {state: 'succeeded', files: [{name, path, routes: [route
    names], mtime}, ...], count, skipped}. No `dev/` directory yet is not an
    error — files=[], count=0, skipped=0.
    """
    project_dir = resolve_project(cfg, project)
    dev_dir = project_dir / "dev"
    files: list[dict] = []
    skipped = 0
    if dev_dir.is_dir():
        for p in sorted(dev_dir.glob("*.json")):
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                skipped += 1
                continue
            if not isinstance(data, dict) or not isinstance(data.get("routes"), dict):
                skipped += 1
                continue
            files.append({
                "name": p.stem,
                "path": str(p),
                "routes": sorted(data["routes"].keys()),
                "mtime": _now_iso(p.stat().st_mtime),
            })
    return {"state": "succeeded", "files": files, "count": len(files), "skipped": skipped}


def _resolve_point(sess: Any, point: Any) -> tuple[list[float] | None, str | None]:
    """Resolve a route step's `click` [x, y] to absolute CSS pixels.

    Same convention as _resolve_region: if BOTH values are <= 1.0 the point
    is normalized viewport fractions (resolved via sess.viewport_size()); if
    either value is > 1 the point is already absolute pixels. Returns
    (pixel_point, None) on success, or (None, detail_str) for malformed/
    out-of-frame input — never raises for bad input (only a genuine CDP
    transport error propagates, via sess.viewport_size())."""
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        return None, "click must be [x, y]"
    try:
        x, y = (float(v) for v in point)
    except (TypeError, ValueError):
        return None, "click values must be numeric"
    if x < 0 or y < 0:
        return None, "click x/y must be >= 0"
    vp_w, vp_h = sess.viewport_size()
    if vp_w <= 0 or vp_h <= 0:
        return None, "could not resolve viewport size"
    if x <= 1.0 and y <= 1.0:
        px = [x * vp_w, y * vp_h]
    else:
        px = [x, y]
    if px[0] >= vp_w or px[1] >= vp_h:
        return None, f"click {px} outside viewport {vp_w:g}x{vp_h:g}"
    return px, None


def _run_route_steps(
    sess: Any, steps: list, *, expect: bool, settle_default: float,
    tesseract: str | None, ocr_unavailable: bool, runner: Runner,
    progress: dict[str, int] | None = None,
) -> tuple[int, int, dict | None]:
    """Replay a route's steps on an already-open CDP session: resolve each
    step's `click` to pixels, dispatch a trusted click, wait its
    settle_seconds, then — if `expect` and the step carries `expect_text`
    and OCR is available — OCR the frame and check expect_text is a
    case-insensitive substring of the recognized text.

    Shared by cdp_navigate_runtime (expect follows the caller's `expect`
    flag; the FIRST failure stops the whole route) and cdp_sweep_runtime
    (expect=False always — sweep is a capture pass, not a verification
    pass, so expect_text steps replay their click but never OCR-check).

    `progress`, if given, is mutated in place with the running
    steps_run/verified_steps counts as the loop proceeds — so a caller
    whose surrounding try/except catches a _cdp.CDPError raised mid-loop
    (a genuine transport failure, not returned here) can still read how far
    the route got. Returns (steps_run, verified_steps, error) on normal
    completion, where `error` is None on full completion or a dict
    {"error": "route_invalid"|"expectation_failed", "step": i, ...} at the
    first step-level failure — the caller adds its own state/route/
    steps_run framing (cdp_navigate_runtime) or records it per-screen and
    continues (cdp_sweep_runtime). Never raises for bad step data; a
    genuine CDP transport error still propagates as _cdp.CDPError."""
    p = progress if progress is not None else {}
    p["steps_run"] = p.get("steps_run", 0)
    p["verified_steps"] = p.get("verified_steps", 0)
    for i, step in enumerate(steps):
        px_point, perr = _resolve_point(sess, step["click"])
        if perr is not None:
            return p["steps_run"], p["verified_steps"], {
                "error": "route_invalid", "step": i, "detail": perr,
            }
        sess.click(px_point[0], px_point[1])
        p["steps_run"] += 1
        step_settle = step.get("settle_seconds")
        step_settle = settle_default if step_settle is None else float(step_settle)
        time.sleep(max(0.0, step_settle))

        expect_text = step.get("expect_text")
        if expect and expect_text and not ocr_unavailable:
            import tempfile
            jpeg = sess.screenshot_jpeg()
            with tempfile.TemporaryDirectory() as td:
                img = Path(td) / "nav.jpg"
                img.write_bytes(jpeg)
                proc = runner.run(
                    [tesseract, str(img), "stdout", "--psm", "6"],
                    timeout=30, encoding="utf-8", errors="replace")
                read_back = (proc.stdout or "") if proc.returncode == 0 else ""
            if expect_text.lower() not in read_back.lower():
                return p["steps_run"], p["verified_steps"], {
                    "error": "expectation_failed", "step": i,
                    "expected": expect_text, "read_back": read_back.strip()[:200],
                }
            p["verified_steps"] += 1
    return p["steps_run"], p["verified_steps"], None


def cdp_navigate_runtime(
    cfg: Config, route: str, routes_path: str, expect: bool = True,
    navigate_url: str | None = None, runner: Runner = _DEFAULT_RUNNER,
    project: str | None = None,
) -> dict:
    """Blind-navigate the runtime canvas through a banked sequence of clicks
    from a routes JSON file — zero screenshots to get to a known screen.

    Routes file format (version 1): `{"version": 1, "routes": {"<name>":
    {"steps": [{"click": [x, y], "settle_seconds": 0.5, "expect_text":
    "..."}]}}}`. `click` uses the SAME coordinate convention as
    optix_cdp_screenshot's `region`: both values <= 1.0 are normalized
    viewport fractions, any value > 1 is absolute pixels. `settle_seconds`
    per step defaults to cfg.cdp_settle_seconds. `expect_text` is optional
    per-step OCR verification (see below). Convention: bank routes at
    `dev/ftx_ui_map.json` in the project workspace (the optix-blind-authoring
    skill's cache format).

    ONE CDP session drives the whole route (mirrors optix_cdp_click's
    lifecycle). When navigate_url is omitted, auto-targets the local runtime
    first exactly like optix_cdp_screenshot (skipping the reload if the tab
    is already there, preserving prior state); pass navigate_url="" to act
    on the current tab as-is.

    Per step: resolve `click` to pixels, dispatch a trusted CDP click (same
    as optix_cdp_click), wait settle_seconds, then — if expect=True and the
    step has expect_text — OCR the frame (tesseract) and check expect_text
    is a case-insensitive substring of the recognized text. On the FIRST
    expectation failure, navigation STOPS immediately: state='failed',
    error='expectation_failed', step=<index>, expected=<text>,
    read_back=<first ~200 chars of OCR text> — later steps do not run. Fail
    loud, never drift blind past a screen that didn't load as expected.

    If tesseract is not installed and any step in the route carries
    expect_text (with expect=True), the navigation is NOT failed — expect_text
    checks are skipped for the whole run and the response carries
    ocr_unavailable=true (the clicks themselves are still valuable even
    without text verification).

    File/route problems never raise: missing routes_path -> state='failed',
    error='routes_file_not_found'; unparseable JSON -> 'routes_file_invalid';
    unknown route name -> 'route_not_found' with `available` listing the
    known route names; a malformed step (no `click`, wrong shape/types) ->
    'route_invalid' naming the offending `step` index.

    Returns on success: {state: 'succeeded', route, steps_run, verified_steps,
    ocr_unavailable?, navigated, finished_at}.
    """
    from . import _cdp
    audit(cfg, "cdp_navigate", route=route, routes_path=str(routes_path))

    data, err = _load_routes_file(routes_path)
    if err is not None:
        return err
    routes = data["routes"]
    if route not in routes:
        return {"state": "failed", "error": "route_not_found", "route": route,
                "available": sorted(routes.keys())}
    route_def = routes[route]
    raw_steps = route_def.get("steps") if isinstance(route_def, dict) else None
    steps, verr, bad_idx = _validate_route_steps(raw_steps)
    if verr is not None:
        return {"state": "failed", "error": "route_invalid", "route": route,
                "step": bad_idx, "detail": verr}

    settle_default = cfg.cdp_settle_seconds
    ocr_unavailable = False
    tesseract: str | None = None
    if expect and any(s.get("expect_text") for s in steps):
        tesseract = _find_tesseract()
        if tesseract is None:
            ocr_unavailable = True

    port_follow = _maybe_follow_port(cfg, project)
    sess = _cdp_session(cfg)
    progress = {"steps_run": 0, "verified_steps": 0}
    try:
        navigated = _point_screenshot_at_runtime(cfg, sess, navigate_url, settle_default, project)
        steps_run, verified_steps, run_err = _run_route_steps(
            sess, steps, expect=expect, settle_default=settle_default,
            tesseract=tesseract, ocr_unavailable=ocr_unavailable, runner=runner,
            progress=progress)
        if run_err is not None:
            return {"state": "failed", "route": route, "steps_run": steps_run,
                    **run_err}

        result: dict[str, Any] = {
            "state": "succeeded", "route": route, "steps_run": steps_run,
            "verified_steps": verified_steps, "navigated": navigated,
            "finished_at": _now_iso(),
        }
        if ocr_unavailable:
            result["ocr_unavailable"] = True
            result["hint"] = _tesseract_missing_hint()
        if port_follow is not None:
            result["port_followed"] = (
                f":{port_follow['expected']}→:{port_follow['actual']}"
            )
        return result
    except _cdp.CDPError as e:
        return {"state": "failed", "error": str(e), "route": route,
                "steps_run": progress["steps_run"]}
    finally:
        sess.close()


# ---- cdp_sweep + cdp_diff: visual baseline capture & compare (S6) -----

_ROUTE_FILENAME_SAFE = re.compile(r"[^a-zA-Z0-9._-]")


def _sanitize_route_filename(route: str) -> str:
    """Route name -> safe filename stem for cdp_sweep_runtime's per-route
    JPEG: keep [a-zA-Z0-9._-], replace everything else (spaces, slashes,
    unicode) with '-'."""
    return _ROUTE_FILENAME_SAFE.sub("-", route)


def cdp_sweep_runtime(
    cfg: Config, routes_path: str, out_dir: str, routes: list[str] | None = None,
    warmup: bool = True, navigate_url: str | None = None,
    runner: Runner = _DEFAULT_RUNNER, project: str | None = None,
) -> dict:
    """Capture a full-frame screenshot (+ OCR text, if tesseract is
    installed) of every route in a banked routes file, in ONE CDP session —
    the visual baseline cdp_diff_runtime later compares against.

    Loads routes_path exactly like cdp_navigate_runtime (same routes file
    format and error contract: missing file -> 'routes_file_not_found',
    bad JSON -> 'routes_file_invalid'). Sweeps every route in the file, in
    file order, unless `routes` names a subset — then that subset, in the
    given order; a name in `routes` not present in the file fails the whole
    call with error='route_not_found' (with `available` listing known
    routes), same contract as cdp_navigate_runtime.

    Each route's steps replay through the SAME step-execution as
    cdp_navigate_runtime (_run_route_steps: click -> settle_seconds), but
    expect_text checks are ALWAYS disabled here (expect=False) — sweep is a
    capture pass, not a verification pass. ASSUMPTION: routes start from
    the runtime's initial/home screen (the same assumption banking them for
    cdp_navigate_runtime makes). Between routes the tab is re-navigated
    back to the runtime URL (the same auto-target navigate/screenshot use)
    so each route starts clean; the first route relies on the same
    auto-target the other CDP tools use (skips the navigate if already
    there).

    warmup=True (default): before the real capture, takes and discards one
    full-frame screenshot and waits one settle period — lets
    animations/renders finish so the saved frame isn't a mid-transition
    capture. Saves the full-frame JPEG to <out_dir>/<route>.jpg (route
    names sanitized via _sanitize_route_filename). If tesseract is
    available, OCRs the saved JPEG (stdout, psm 6) and stores its non-empty
    stripped lines as `text`.

    A capture failure on one route — a malformed banked step (an
    out-of-viewport coordinate), or a genuine CDP transport error mid-route
    — does NOT abort the sweep: that route is recorded as {"error": ...} in
    `screens` and the sweep continues (a partial sweep beats none). The
    response then carries "errors": N.

    Writes <out_dir>/manifest.json: {"version": 1, "created_at": ...,
    "viewport": {"w", "h"}, "ocr": <bool>, "screens": {route: {"file",
    "size_bytes", "text"?}}}. Returns that same manifest dict inline plus
    state='succeeded' (and "errors": N when any route failed).
    """
    from . import _cdp
    audit(cfg, "cdp_sweep", routes_path=str(routes_path), out_dir=str(out_dir))

    data, err = _load_routes_file(routes_path)
    if err is not None:
        return err
    all_routes = data["routes"]
    if routes is None:
        selected = list(all_routes.keys())
    else:
        unknown = [r for r in routes if r not in all_routes]
        if unknown:
            return {"state": "failed", "error": "route_not_found",
                    "route": unknown[0], "available": sorted(all_routes.keys())}
        selected = list(routes)

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    settle_default = cfg.cdp_settle_seconds
    target = navigate_url if navigate_url else _runtime_verify_url(cfg, project)
    reload_between = navigate_url != ""
    tesseract = _find_tesseract()

    screens: dict[str, dict] = {}
    errors = 0
    vp_w = vp_h = 0.0

    sess = _cdp_session(cfg)
    try:
        _point_screenshot_at_runtime(cfg, sess, navigate_url, settle_default, project)
        vp_w, vp_h = sess.viewport_size()
        for idx, route in enumerate(selected):
            route_def = all_routes[route]
            raw_steps = route_def.get("steps") if isinstance(route_def, dict) else None
            steps, verr, bad_idx = _validate_route_steps(raw_steps)
            if verr is not None:
                screens[route] = {"error": f"route_invalid: step {bad_idx}: {verr}"}
                errors += 1
                continue
            try:
                if idx > 0 and reload_between:
                    sess.navigate(target)
                    time.sleep(max(0.0, settle_default))
                _, _, run_err = _run_route_steps(
                    sess, steps, expect=False, settle_default=settle_default,
                    tesseract=None, ocr_unavailable=True, runner=runner)
                if run_err is not None:
                    screens[route] = {
                        "error": run_err.get("detail") or run_err.get("error")}
                    errors += 1
                    continue
                if warmup:
                    sess.screenshot_jpeg()  # discard: let the frame settle
                    time.sleep(max(0.0, settle_default))
                jpeg = sess.screenshot_jpeg()
            except _cdp.CDPError as e:
                screens[route] = {"error": str(e)}
                errors += 1
                continue

            fname = _sanitize_route_filename(route)
            out_file = out_path / f"{fname}.jpg"
            out_file.write_bytes(jpeg)
            entry: dict[str, Any] = {"file": out_file.name, "size_bytes": len(jpeg)}
            if tesseract is not None:
                proc = runner.run(
                    [tesseract, str(out_file), "stdout", "--psm", "6"],
                    timeout=30, encoding="utf-8", errors="replace")
                text = (proc.stdout or "") if proc.returncode == 0 else ""
                entry["text"] = [ln.strip() for ln in text.splitlines() if ln.strip()]
            screens[route] = entry
    except _cdp.CDPError as e:
        return {"state": "failed", "error": str(e)}
    finally:
        sess.close()

    manifest = {
        "version": 1, "created_at": _now_iso(),
        "viewport": {"w": vp_w, "h": vp_h}, "ocr": tesseract is not None,
        "screens": screens,
    }
    (out_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    result: dict[str, Any] = {"state": "succeeded", **manifest}
    if errors:
        result["errors"] = errors
    return result


def _load_pil():
    """Lazy import of Pillow, isolated to one seam (this function) so tests
    can cleanly force the no-Pillow degraded path by monkeypatching
    service.core._load_pil instead of touching sys.modules globally.
    Returns a small namespace with Image/ImageChops/ImageStat, or None if
    Pillow (the `visual` optional dependency group) is not installed."""
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError:
        return None
    from types import SimpleNamespace
    return SimpleNamespace(Image=Image, ImageChops=ImageChops, ImageStat=ImageStat)


def _cap_lines(lines: list[str], limit: int = 40) -> list[str]:
    """Cap a text-diff line list at `limit` entries, appending a '+N more'
    sentinel string so a badly-drifted screen can't blow up the response."""
    if len(lines) <= limit:
        return lines
    return lines[:limit] + [f"+{len(lines) - limit} more"]


def _text_line_diff(text_a: list[str], text_b: list[str]) -> tuple[list[str], list[str]]:
    """Line-level explainer for cdp_diff_runtime's 'changed' screens: lines
    in B not in A ("added"), lines in A not in B ("removed"). A simple
    order-preserving set difference (not a positional/sequence diff — OCR
    line order isn't stable enough to make that meaningful), each side
    capped via _cap_lines."""
    set_a = set(text_a)
    set_b = set(text_b)
    added = [ln for ln in text_b if ln not in set_a]
    removed = [ln for ln in text_a if ln not in set_b]
    return _cap_lines(added), _cap_lines(removed)


def _add_text_diff(entry: dict, text_a: list[str], text_b: list[str]) -> None:
    added, removed = _text_line_diff(text_a, text_b)
    entry["text_added"] = added
    entry["text_removed"] = removed


def cdp_diff_runtime(dir_a: str, dir_b: str, threshold: float = 2.0) -> dict:
    """Compare two cdp_sweep_runtime capture directories screen-by-screen —
    a visual regression check, pure file comparison (no CDP session).

    Reads <dir_a>/manifest.json and <dir_b>/manifest.json and matches
    screens by route key. A missing manifest in either dir fails outright:
    state='failed', error='manifest_not_found', naming the offending `dir`.
    Screens present in only one manifest are reported under `added`
    (only in B) / `removed` (only in A) — not treated as errors. A screen
    whose sweep entry in either manifest carries its own {"error": ...}
    (a route that failed to capture) degrades to {"status": "error",
    "detail": ...} and counts toward summary.errors, rather than crashing
    the whole diff.

    Pixel gate (Pillow present — the `visual` optional dependency group,
    lazy-imported via _load_pil so it stays fully optional): opens both
    JPEGs; a size mismatch short-circuits to {"status": "size_mismatch"}
    (no pixel compare, no text explainer); otherwise both are converted to
    grayscale and compared via mean absolute pixel difference scaled to a
    0-100 percentage (`pixel_pct` = mean_abs_diff * 100 / 255); `changed`
    when pixel_pct > threshold (default 2.0), else `same`. An unreadable/
    missing JPEG for a common screen degrades that ONE screen to
    {"status": "error", ...} rather than failing the whole diff.

    Pillow ABSENT: DEGRADED text-only mode. Requires BOTH manifests to
    carry OCR text (their top-level `ocr` flag true, written by
    cdp_sweep_runtime when tesseract ran) — if neither/either does, this
    fails outright: error='no_pillow_no_ocr', with an install hint (Pillow
    for pixels, tesseract for text). Otherwise each common screen's status
    comes from exact equality of the two manifests' OCR `text` lists,
    `pixel_pct` is null, and the top-level response carries
    degraded='no_pillow'.

    Every 'changed' screen additionally gets a text explainer —
    `text_added` / `text_removed` — diffing the two manifests' `text`
    lists (empty list if a manifest wasn't OCR'd), via _text_line_diff
    (order-preserving, capped at 40 lines each with a '+N more' sentinel).

    Returns {state, threshold, degraded?, screens: {route: {status,
    pixel_pct, text_added?, text_removed?}}, added: [...], removed: [...],
    summary: {"same": N, "changed": N, "size_mismatch": N, "errors": N}}.
    """
    path_a = Path(dir_a)
    path_b = Path(dir_b)
    manifest_a = path_a / "manifest.json"
    manifest_b = path_b / "manifest.json"
    if not manifest_a.is_file():
        return {"state": "failed", "error": "manifest_not_found", "dir": str(path_a)}
    if not manifest_b.is_file():
        return {"state": "failed", "error": "manifest_not_found", "dir": str(path_b)}
    try:
        data_a = json.loads(manifest_a.read_text(encoding="utf-8"))
        data_b = json.loads(manifest_b.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        return {"state": "failed", "error": "manifest_invalid", "detail": str(e)}

    screens_a = data_a.get("screens") or {}
    screens_b = data_b.get("screens") or {}
    common = sorted(set(screens_a) & set(screens_b))
    added_routes = sorted(set(screens_b) - set(screens_a))
    removed_routes = sorted(set(screens_a) - set(screens_b))

    pil = _load_pil()
    degraded: str | None = None
    if pil is None:
        both_ocr = bool(data_a.get("ocr")) and bool(data_b.get("ocr"))
        if not both_ocr:
            return {
                "state": "failed", "error": "no_pillow_no_ocr",
                "hint": ("pip install ftx-mcp[visual] for pixel diffing, or "
                         "install tesseract (see optix_cdp_ocr) so both "
                         "sweeps capture OCR text for a degraded text-only "
                         "diff"),
            }
        degraded = "no_pillow"

    screens: dict[str, dict] = {}
    summary = {"same": 0, "changed": 0, "size_mismatch": 0, "errors": 0,
               "text_changed": 0}
    for route in common:
        entry_a = screens_a.get(route) or {}
        entry_b = screens_b.get(route) or {}
        if entry_a.get("error") or entry_b.get("error"):
            screens[route] = {"status": "error",
                               "detail": entry_a.get("error") or entry_b.get("error")}
            summary["errors"] += 1
            continue
        if pil is not None:
            file_a = path_a / entry_a.get("file", f"{route}.jpg")
            file_b = path_b / entry_b.get("file", f"{route}.jpg")
            try:
                with pil.Image.open(file_a) as img_a, pil.Image.open(file_b) as img_b:
                    if img_a.size != img_b.size:
                        screens[route] = {"status": "size_mismatch"}
                        summary["size_mismatch"] += 1
                        continue
                    gray_a = img_a.convert("L")
                    gray_b = img_b.convert("L")
                    diff = pil.ImageChops.difference(gray_a, gray_b)
                    pct = pil.ImageStat.Stat(diff).mean[0] * 100 / 255
            except OSError as e:
                screens[route] = {"status": "error", "detail": str(e)}
                summary["errors"] += 1
                continue
            status = "changed" if pct > threshold else "same"
            screen_entry: dict[str, Any] = {"status": status, "pixel_pct": round(pct, 2)}
            # Text deltas are computed UNCONDITIONALLY (when OCR text exists),
            # not gated on the pixel threshold: a single-label edit moves
            # ~0.6% of pixels on a busy screen - under the 2% default - and
            # was silently swallowed in the field (2026-07-23 Cowork run).
            # Text diffing is nearly free, and live-value churn in the deltas
            # is informative rather than harmful. `text_changed` gives the
            # cheap-text signal its own channel independent of pixel status.
            _add_text_diff(screen_entry, entry_a.get("text") or [],
                           entry_b.get("text") or [])
            screen_entry["text_changed"] = bool(
                screen_entry.get("text_added") or screen_entry.get("text_removed"))
            if screen_entry["text_changed"]:
                summary["text_changed"] = summary.get("text_changed", 0) + 1
            screens[route] = screen_entry
            summary[status] += 1
        else:
            text_a = entry_a.get("text") or []
            text_b = entry_b.get("text") or []
            status = "same" if text_a == text_b else "changed"
            screen_entry = {"status": status, "pixel_pct": None}
            _add_text_diff(screen_entry, text_a, text_b)
            screen_entry["text_changed"] = status == "changed"
            if screen_entry["text_changed"]:
                summary["text_changed"] = summary.get("text_changed", 0) + 1
            screens[route] = screen_entry
            summary[status] += 1

    result: dict[str, Any] = {
        "state": "succeeded", "threshold": threshold, "screens": screens,
        "added": added_routes, "removed": removed_routes, "summary": summary,
    }
    if degraded:
        result["degraded"] = degraded
    return result


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


def _poll_until(
    cfg: Config, deploy_started_at: float, method: str,
    probe: Callable[[], tuple[bool, str | None]],
) -> dict:
    """Shared deploy-verify poll skeleton for verify_export_mtime /
    verify_runtime_probe.

    Polls `probe()` — which returns (ok, confirmed_at) — until the deadline.
    The envelope (`method`/`confirmed_at`/`timeout_seconds`, with confirmed_at
    None on timeout) is asserted verbatim by test_core.py / test_deploy_contract
    and must stay byte-identical."""
    deadline = deploy_started_at + cfg.verify_timeout_seconds
    # ALWAYS probe at least once. The deadline is measured from DEPLOY start,
    # not verify start, so a deploy whose export ate the whole budget would
    # otherwise probe ZERO times and report "not confirmed" without ever
    # having looked — reporting failure for something it never checked.
    while True:
        ok, confirmed_at = probe()
        if ok:
            return {
                "method": method,
                "confirmed_at": confirmed_at,
                "timeout_seconds": cfg.verify_timeout_seconds,
            }
        if time.time() >= deadline:
            break
        time.sleep(cfg.verify_poll_seconds)
    return {
        "method": method,
        "confirmed_at": None,
        "timeout_seconds": cfg.verify_timeout_seconds,
    }


# time.time() and st_mtime are different clocks at the same coarse resolution,
# so a file written immediately AFTER a time.time() sample routinely reports an
# mtime EQUAL to (or a hair below) it. Measured on the Windows VM, 2000 samples:
# a file written right after the sample failed a strict `mtime > started` check
# **75.35%** of the time, median delta +0.000 ms, min -0.000 ms.
#
# A strict `>` therefore makes verification a coin-flip for any deploy that
# completes inside one clock tick — and it is UNRECOVERABLE, because mtimes do
# not change after the export: the poll loop simply burns its whole budget and
# reports a successful deploy as failed. That surfaced as a rare "flaky test"
# that roamed across every deploy test on the VM and never reproduced on Linux.
#
# The tolerance only has to absorb clock granularity. A genuinely stale tree —
# the thing this check exists to catch — is seconds or minutes old, orders of
# magnitude outside this window, so the check keeps its teeth.
_MTIME_CLOCK_TOLERANCE_SECONDS = 0.05


def verify_export_mtime(cfg: Config, runtime_project_dir: Path, deploy_started_at: float) -> dict:
    """Verify the swapped runtime tree's mtime advanced past deploy-start.

    Used when run_after_deploy=False, or when the runtime probe is disabled.
    Polls the runtime tree (NOT the source project tree) — the new tree
    just landed there via os.replace and its mtimes reflect the swap.
    """
    def _probe() -> tuple[bool, str | None]:
        try:
            latest = _project_tree_max_mtime(runtime_project_dir)
        except OSError:
            latest = 0.0
        if latest >= deploy_started_at - _MTIME_CLOCK_TOLERANCE_SECONDS:
            return True, _now_iso(latest)
        return False, None
    return _poll_until(cfg, deploy_started_at, "export_mtime", _probe)


def verify_runtime_probe(
    cfg: Config,
    _runtime_project_dir: Path,
    deploy_started_at: float,
    *,
    project: str | None = None,
) -> dict:
    """Verify the runtime port comes back up after a bounce.

    Polls the project's WebPresentationEngine port (via ``_project_web_port``
    when ``project`` is supplied) for tcp_reachable. The runtime was stopped
    before the swap and (re)started after, so a successful connect is the
    end-to-end signal the deploy actually landed and the runtime is happy.

    ``project`` is bound by ``deploy()`` via ``functools.partial`` so the
    project-specific port is used rather than the global ``cfg.runtime_test_port``
    default.

    ``_runtime_project_dir`` is REQUIRED for signature parity with
    ``verify_export_mtime`` — ``deploy()`` calls positionally
    ``(cfg, runtime_project_dir, started_at)`` after the partial bind.
    """
    def _probe() -> tuple[bool, str | None]:
        port = (
            _project_web_port(cfg, project)
            if project is not None
            else runtime_probe_port(cfg)
        )
        if _tcp_probe(runtime_probe_host(cfg), port, timeout=0.5):
            return True, _now_iso()
        return False, None
    return _poll_until(cfg, deploy_started_at, "runtime_probe", _probe)


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
    # Use the project's own WebPresentationEngine port (via resolver) rather
    # than the global runtime_probe_port so a per-project port shows up here.
    runtime_port = _project_web_port(cfg, project)
    reachable = _tcp_probe(runtime_probe_host(cfg), runtime_port, timeout=1.0)
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
        #
        # Match the dir WITH a trailing separator so a sibling project whose
        # name shares a prefix ('Proj' vs 'Proj2') is not also killed — a bare
        # substring match on the dir would catch 'Proj2'. The runtime exe lives
        # at <dir>\FTOptixApplication\FTOptixRuntime.exe, so the trailing
        # separator is always present in a genuinely matching command line.
        # Double any single quote so a dir name containing ' can't break out of
        # the PowerShell single-quoted literal.
        match_literal = (str(runtime_project_dir) + os.sep).replace("'", "''")
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='FTOptixRuntime.exe'\" | "
            f"Where-Object {{ $_.CommandLine -match [regex]::Escape('{match_literal}') }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        self.runner.run_powershell(ps, timeout=30)
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
                # -File launch: the helper only expresses -Command, so this
                # argv is built directly (see Runner.run_powershell docstring).
                self.runner.run(
                    ["powershell", "-NoProfile", "-File", launcher,
                     "-RuntimeProjectDir", str(runtime_project_dir)],
                    timeout=30)
            else:
                self.runner.run_powershell(
                    f"Start-ScheduledTask -TaskName '{launcher}'", timeout=30)
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
    require_editors_closed(cfg, project_dir)

    if lock is None:
        lock = DeployLock(
            cfg.state_dir / "deploy.lock",
            caller=f"optix_deploy({project})",
        )
    if runtime is None:
        runtime = RuntimeController(runner=runner)
    if verify is None:
        if req.run_after_deploy:
            import functools
            verify = functools.partial(verify_runtime_probe, project=project)
        else:
            verify = verify_export_mtime

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
            require_editors_closed(cfg, project_dir, force=True)

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
                # Fencing checkpoint #1: stopping the runtime is operator-
                # visible. If our lock was age-broken + re-taken since acquire,
                # fail closed (DeployLockEvicted) rather than racing a second
                # holder on runtime.stop/start against the same tree.
                lock.check_still_held()
                runtime.stop(cfg, runtime_project_dir)

            # Fencing checkpoint #2: the tree swap is the genuinely
            # irreversible step (a concurrent swap racing on the same .bak
            # name is the corruption this guards against). Re-check ownership.
            lock.check_still_held()
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
