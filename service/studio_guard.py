"""Studio/editor process detection for the corruption guard.

FactoryTalk Optix Studio holds the project's in-memory model as the source
of truth while a project is open: file-level writes from this service race
Studio's autosave/save-on-close path (the stale in-memory model stomps the
edits back), and reads return stale disk state that misleads the caller's
next edit. The guard therefore refuses project READS as well as writes
while Studio is running.

Detection options were probed live on Studio 1.7.1.46 / Windows 11 25H2
(project open in Studio):

- process cmdline:    bare exe path when opened via GUI -> no attribution
- open file handles:  Studio holds ZERO handles on the project tree (loads
                      to memory and closes; 54 handles, all DLLs + logs)
- window title:       "FactoryTalk Optix Studio" — no project name, and
                      windows are only visible from inside the interactive
                      logon session anyway
- lock/sentinel files: none created in the project dir
- Configuration.xml:  recent-projects list only (written on open, but a
                      recents entry is not "currently open")

Hence the BLANKET rule: any running FTOptixStudio.exe blocks all project
reads/finds/deploys, with no per-project attribution attempted. There is
deliberately NO override: an in-band escape hatch (tool parameter) is
reachable by the calling model, which defeats the guard. The remediation
is closing Studio.

VS / VS Code are different: their command lines usually carry the opened
folder / solution path, so they ARE attributable. An attributed match
blocks; a bare editor process is only a preflight warning (a dev box runs
VS Code approximately always).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import psutil

# Processes that hold an in-memory model of the whole project (blanket-block).
STUDIO_PROCS = ("ftoptixstudio.exe",)
# Editors that race unsaved buffers, but only for a project they have open
# (cmdline-attributable, unlike Studio).
EDITOR_PROCS = ("devenv.exe", "code.exe")

# Snapshots are cheap (~50ms) but the guard fires on every read; cache so
# a burst of optix_read_file calls costs one scan.
CACHE_TTL_SECONDS = 2.0

_cache: dict | None = None
_cache_at: float = 0.0

ScanFn = Callable[[], list[dict]]


def _scan() -> list[dict]:
    """Enumerate guard-relevant processes: [{pid, name, cmdline}, ...]."""
    out: list[dict] = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        name = (p.info.get("name") or "").lower()
        if name in STUDIO_PROCS or name in EDITOR_PROCS:
            out.append({
                "pid": p.info["pid"],
                "name": name,
                "cmdline": p.info.get("cmdline") or [],
            })
    return out


def studio_state(force: bool = False, scan: ScanFn | None = None) -> dict:
    """Cached snapshot of guard-relevant process state.

    Returns either
      {studio: {running, pids}, editors: [{pid, name, cmdline}], checked_at}
    or, when process enumeration itself fails (an infra fault, NOT evidence
    of Studio), {error: "<repr>", checked_at}.

    force=True bypasses the TTL cache — used for the post-lock re-check in
    deploy() (TOCTOU: Studio could open between the entry check and the
    first file write). `scan` is a test injection point; injected scans
    never touch the module cache.
    """
    global _cache, _cache_at
    now = time.time()
    if (
        not force
        and scan is None
        and _cache is not None
        and (now - _cache_at) < CACHE_TTL_SECONDS
    ):
        return _cache
    try:
        procs = scan() if scan is not None else _scan()
        state: dict = {
            "studio": {
                "running": any(p["name"] in STUDIO_PROCS for p in procs),
                "pids": [p["pid"] for p in procs if p["name"] in STUDIO_PROCS],
            },
            "editors": [p for p in procs if p["name"] in EDITOR_PROCS],
            "checked_at": now,
        }
    except Exception as exc:  # noqa: BLE001 - any psutil failure means "unknown", never "open"
        state = {"error": f"{type(exc).__name__}: {exc}", "checked_at": now}
    if scan is None:
        _cache, _cache_at = state, now
    return state


def reset_cache() -> None:
    """Test hook: drop the TTL cache between cases."""
    global _cache, _cache_at
    _cache, _cache_at = None, 0.0


def attributed_editors(state: dict, project_dir: Path) -> list[dict]:
    """Editor processes whose cmdline names this project's directory.

    Matching is case-insensitive and slash-agnostic (VS Code passes
    forward-slash URIs for some launch paths).
    """
    if state.get("error"):
        return []
    needle = str(project_dir).lower().replace("\\", "/")
    if not needle:
        return []
    hits: list[dict] = []
    for p in state.get("editors", []):
        joined = " ".join(p.get("cmdline") or []).lower().replace("\\", "/")
        if needle in joined:
            hits.append(p)
    return hits
