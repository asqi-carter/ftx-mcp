"""Execute a design-time NetLogic ExportMethod inside a specific FT Optix
Studio window, via UI Automation — the mechanism behind arming/stopping the
design-time bridge.

Windows-ONLY. UNLIKE `service/studio_uia.py`, which is deliberately read-only
and background-safe, this module INTERACTS: it takes the foreground,
right-clicks a project-tree row and clicks a menu item. Prior foreground window
and cursor position are restored in a finally block. Keep the two modules
separate so studio_uia's "never grabs focus" contract stays true.

WHY THIS HAS TO EXIST AT ALL
    Studio BUILDS the NetSolution during project load but EXECUTES nothing from
    it — [ModuleInitializer], static ctor and instance ctor all stay cold until
    an explicit right-click -> Execute (the same probes DO fire inside
    FTOptixRuntime, so the negative is real). Studio's CLI has no execute verb
    either (1.7.4.32: open/new/connect/deploy/export only). The GUI gesture is
    the only lever — and the service already runs in session 1, which is the
    only thing that gesture needs.

THREE MEASURED FACTS THIS ENCODES
    1. Right-clicking a FOLDER lists the Execute entries of its NetLogic
       CHILDREN. So the outermost folder is a valid target and the leaf never
       has to be expanded or scrolled to — this removes the whole
       clipped-row/scroll problem (a row scrolled out of the pane keeps its
       scene rect, so clicking it hits whatever is painted there).
    2. Studio windows are NOT always maximised (measured: a half-screen window
       at x=961 w=958), so absolute pane X-bands misfire. Row names also recur
       in other panes. Rather than guess geometry, every candidate row is TRIED
       and the CONTEXT MENU validates it — a Properties-pane label simply does
       not offer "Execute <method>".
    3. Qt/QML draws context menus and modals INTO the window's scene graph, so
       they are children of the main window, never top-level.
    4. A fresh Studio opens with every top-level folder COLLAPSED, and only
       the NetLogic CATEGORY folder lists Execute entries — a plain folder
       above it shows the generic menu. The Project-view Search box (the
       window's only EditControl) is the geometry-free way in: typing the
       leaf's name filters the tree to the leaf WITH its ancestors, all real
       rows with real context menus. {Right} and a double-click do nothing;
       the expander chevron is not a UIA control, but a click two indent
       steps left of the row text toggles it — kept as the fallback.
    5. The Type-view tiles under the tree share the rows' height and x-band,
       so an indent measured from every same-height control reads 6 px
       (1040 -> 1046) instead of 16 and the chevron click lands on the icon.
       Measure only from rows at or above the one being opened.

Off Windows / without `uiautomation` / with no interactive desktop, arm()
returns a structured {"ok": False, "error": ...} rather than raising.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

from . import core
from .core import _BRIDGE_PORT_BASE as _DEFAULT_BASE, _BRIDGE_PORT_RANGE as _DEFAULT_RANGE
# Imported from core so there is one authoritative declaration; callers pass
# cfg.bridge_port_base / cfg.bridge_port_range (env-overridable) explicitly,
# so these defaults only apply when studio_arm functions are called without
# those arguments (e.g. tests or standalone use).


# ---- navigation, derived from the project YAML ------------------------------

def find_bridge_yaml(project_dir: str | Path,
                     node_name: str = "StudioMCPBridge") -> str | None:
    """The Nodes/*.yaml declaring `node_name`, or None.

    Searched rather than hardcoded to Nodes/NetLogic/NetLogic.yaml: the bridge
    NetLogic can sit under any category folder, and customer forks nest it
    deeper than the stock NewHMIProject layout.
    """
    root = Path(project_dir) / "Nodes"
    if not root.is_dir():
        return None
    for p in sorted(root.rglob("*.yaml")):
        try:
            if node_name in p.read_text(encoding="utf-8", errors="replace"):
                return str(p)
        except OSError:
            continue
    return None


def derive_chain(yaml_path: str, method: str = "StartBridge") -> list[str] | None:
    """Project-tree ancestor rows (outermost first) for the NetLogic exposing
    `method` — replaces a hardcoded parent chain.

    Effective indent = leading spaces + 2 for a '- ' list item, because a list
    item sits one level deeper than its bare-key parent. WITHOUT that
    correction the folder root ties with its own children and drops out of the
    chain, while siblings (BehaviourStartPriority) wrongly enter it.
    """
    try:
        lines = Path(yaml_path).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    entries: list[tuple[int, str, bool]] = []
    pending_method = False
    for ln in lines:
        if re.match(r"^\s*-?\s*Class:\s*Method\s*$", ln):
            pending_method = True
            continue
        m = re.match(r"^(\s*)(-\s*)?Name:\s*(.+?)\s*$", ln)
        if m:
            entries.append((len(m.group(1)) + (2 if m.group(2) else 0),
                            m.group(3), pending_method))
            pending_method = False
    for i, (ind, name, is_method) in enumerate(entries):
        if is_method and name == method:
            chain, want = [], ind
            for j in range(i - 1, -1, -1):
                jind, jname, _ = entries[j]
                if jind < want:
                    chain.append(jname)
                    want = jind
            return list(reversed(chain))
    return None


def folder_ancestors(yaml_path: str) -> list[str]:
    """Project-tree folder rows ABOVE the file that declares the bridge,
    outermost first — the part of the chain `derive_chain` cannot see.

    Studio splits the tree across files: a folder's own yaml lists its
    children as `- File: Sub/Sub.yaml` references, and the child file starts
    over at indent 0. So for a customer fork's layout the bridge file
    (`Misc_/DesignTimeNetLogic/DesignTimeNetLogic.yaml`) yields the chain
    [DesignTimeNetLogic, StudioMCPBridge] while the row actually on screen
    after a fresh open is the PARENT folder `Misc.`, declared one directory up
    in `Misc_/Misc_.yaml`. Measured 2026-08-23: the arm reported
    menu_item_not_found with tried=[] because nothing in the chain was
    visible. Each ancestor directory `D` under Nodes/ carries `D/D.yaml` whose
    first `Name:` is the row text (`Misc_` on disk is `Misc.` in the tree).
    """
    out: list[str] = []
    try:
        yp = Path(yaml_path).resolve()
    except OSError:
        return out
    d = yp.parent
    while d.name and d.name.lower() != "nodes":
        own = d / f"{d.name}.yaml"
        if own != yp and own.is_file():
            try:
                for ln in own.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
                    m = re.match(r"^Name:\s*(.+?)\s*$", ln)
                    if m:
                        out.append(m.group(1))
                        break
            except OSError:
                pass
        if d.parent == d:
            break
        d = d.parent
    return list(reversed(out))


_TAGS = re.compile(r"</?b>")


def _strip_tags(name: object) -> str:
    """Row text without the `<b>..</b>` the Project-view search wraps around
    a match — with a filter typed, the leaf reads `<b>StudioMCPBridge</b>`
    and an exact compare misses it (measured 2026-08-23)."""
    return _TAGS.sub("", str(name or "")).strip()


def _norm(name: object) -> str:
    return _strip_tags(name).lower()


# ---- bridge probing (identity, never "is the base port open") ---------------

# Loopback connect/read timeout for bridge probes. 0.5 s is plenty: a serving
# bridge answers /bridge/health in ~20 ms, and a refused connect on 127.0.0.1
# normally returns at once (on some Windows boxes it takes ~2 s to report
# refusal; that port is treated as "not serving", which is the right answer).
# The old 3 s default made every dead port in the range cost seconds per probe,
# and re-arm probes the whole range several times per poll.
_LOOPBACK_TIMEOUT_S = 0.5


def _bridge_health(port: int, timeout: float = _LOOPBACK_TIMEOUT_S) -> dict | None:
    """GET /bridge/health, or None when nothing answers.

    `timeout` defaults to _LOOPBACK_TIMEOUT_S (0.5 s; rationale above).

    Liveness is decided by a REAL HTTP request, never by a bare TCP connect.
    A connect-then-close makes the bridge log
    "request error: ... An established connection was aborted by the software
    in your host machine" for every probe — 26 such WARNINGs were traced to an
    earlier version of this module inside one afternoon. core.list_bridges has
    always probed over HTTP for the same reason; match it.
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/bridge/health", timeout=timeout) as r:
            return json.loads(r.read().decode()) or {}
    except Exception:
        return None


def _log_mark() -> int | None:
    """Studio log offset now (seam; None when the log is absent)."""
    try:
        return core._studio_log_mark()
    except Exception:
        return None


def _log_since(mark: int | None) -> "list[str] | None":
    """Studio log lines appended after `mark` (seam; None when unreadable)."""
    try:
        return core._studio_log_since(mark)
    except Exception:
        return None


def bridge_project(port: int) -> str | None:
    h = _bridge_health(port)
    return None if h is None else h.get("project")


def bound_ports(base: int = _DEFAULT_BASE, span: int = _DEFAULT_RANGE) -> set[int]:
    """Ports in the range currently served by a bridge (health answered).

    Uses the same short loopback timeout as _bridge_health. Kept for
    list_bridges-style callers; serving_port_for does NOT call it.
    """
    return {p for p in range(base, base + span) if _bridge_health(p) is not None}


def _norm_dir(path: object) -> str:
    """Directory for comparison: forward slashes folded, no trailing
    separator, case-folded; a `.optix` file path reduces to its folder."""
    d = str(path or "").replace("/", "\\").rstrip("\\").strip().lower()
    if d.endswith(".optix"):
        d = d.rsplit("\\", 1)[0] if "\\" in d else ""
    return d


def cmdline_project_paths(cmd: "list[str] | tuple[str, ...]") -> list[str]:
    """Project locations a Studio command line names, as raw path strings.

    Two shapes carry a directory:
      * an ABSOLUTE path token (a directory or a ``.optix`` file) - what
        ``FTOptixStudio open <path>`` and a shell double-click pass;
      * ``new <name> <root>`` - what ``FTOptixStudio new`` is launched with
        (see core.project_new): the project lives at ``<root>\\<name>``.
    The returned strings are NOT checked against the disk and a ``.optix``
    path is returned as given (callers reduce it to its folder). A relative
    token is never evidence (it would resolve against this service's cwd).
    """
    import ntpath
    import os

    toks = [str(t) for t in (cmd or []) if t]
    out: list[str] = []
    new_idx = None
    for i, tok in enumerate(toks):
        if tok.lower() == "new" and i > 0:
            new_idx = i
            break
    if new_idx is not None and len(toks) > new_idx + 2:
        name, root = toks[new_idx + 1], toks[new_idx + 2]
        if (not name.startswith("-") and not root.startswith("-")
                and (ntpath.isabs(root) or os.path.isabs(root))
                and not any(c in name for c in ("/", "\\", ":"))):
            joiner = ntpath.join if ("\\" in root or ntpath.splitdrive(root)[0]) else os.path.join
            out.append(joiner(root, name))
    for tok in toks:
        if tok.startswith("-") or tok.lower().endswith(".exe"):
            continue
        if ntpath.isabs(tok) or os.path.isabs(tok):
            out.append(tok)
    return out


def health_studio_pid(health: dict | None) -> int | None:
    """Studio process id a /bridge/health payload names, or None.

    `studio_pid` (1.0.9 bridges); older bridges report the same value as `pid`.
    """
    if not health:
        return None
    for key in ("studio_pid", "pid"):
        try:
            v = int(health.get(key))
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return None


def _bridge_matches(health: dict | None, project: str, want: set[str],
                    studio_pid: int | None = None,
                    project_dir: str | Path | None = None,
                    name_collision: bool = False) -> bool:
    """Does this bridge belong to THE Studio/project asked about?

    With `studio_pid` known the Studio process id is the only identity that
    counts (two Studios can hold projects with the same name). Otherwise the
    project name must match AND, when both sides know a directory, so must the
    directory - so a same-named project in another Studio is never claimed.

    ``name_collision=True`` means another directory holds a project of the
    same name: a health WITHOUT ``project_dir`` (an older bridge) then proves
    nothing - the name fits both copies - and is not claimed. The caller proves
    that Studio by pid / command line / launch record instead.
    """
    if health is None:
        return False
    if studio_pid:
        return health_studio_pid(health) == studio_pid
    if (health.get("project") or health.get("project_name") or "").strip().lower() not in want:
        return False
    theirs = _norm_dir(health.get("project_dir"))
    ours = _norm_dir(project_dir)
    if theirs and ours and theirs != ours:
        return False
    if name_collision and not theirs:
        return False
    return True


def serving_port_for(project: str, base: int = _DEFAULT_BASE,
                     span: int = _DEFAULT_RANGE,
                     aliases: set[str] | None = None,
                     studio_pid: int | None = None,
                     project_dir: str | Path | None = None,
                     preferred: int | None = None,
                     name_collision: bool = False,
                     health_cache: "dict[int, dict | None] | None" = None
                     ) -> int | None:
    """Port whose bridge serves `project`, or None.

    `health_cache`, when given, receives {port: health payload or None} for
    every port actually probed, so a caller that goes on to need the whole
    range (port-range-full check, "before" snapshot) does not probe it again.

    Ports are probed in order and the FIRST match returns: `preferred` (the
    bridge registry's port for the project) first, then the rest of
    base..base+span-1 ascending. Exactly one health call per port, and
    bound_ports() is never called (it would probe the whole range up front and
    then each serving port a second time).

    This is the ONLY correct fast-exit/verify question once more than one
    bridge can be armed. "Is 8768 open?" answers yes for somebody ELSE's
    Studio, which would report success having armed nothing.

    `aliases` are the OTHER names the bridge may report for this project: it
    answers with Project.Current.BrowseName, which is the project NODE's name,
    not the directory's. They usually agree (Studio names the folder after the
    project) but not always - `Line4_HMI/Line4.optix` serves as "Line4", and a
    dir-name-only compare never matches it (measured 2026-08-23).

    `studio_pid` (the Studio process that owns the project's window) makes the
    match exact: only the bridge whose health names that pid answers.
    `project_dir` guards the name-only path: a bridge reporting a different
    project_dir is another Studio's, even under the same project name.
    `name_collision` (another directory holds a same-named project) makes a
    bridge whose health carries no project_dir unproven: it is not claimed.
    """
    want = {project.strip().lower()} | {a.strip().lower() for a in (aliases or ())}
    order = list(range(base, base + span))
    if preferred is not None:
        order = [preferred] + [p for p in order if p != preferred]
    for p in order:
        h = _bridge_health(p)
        if health_cache is not None:
            health_cache[p] = h
        if h is None:
            continue
        if _bridge_matches(h, project, want, studio_pid, project_dir,
                           name_collision=name_collision):
            return p
    return None


# ---- bridge port range full (PRD 0053 B, #85) -------------------------------

#: Stable marker the bridge writes into its bind-failure log line
#: (studio-bridge/StudioMCPBridge.cs, BindFailureMarker). The verify path scans
#: the Studio log for it; the legacy wording is accepted for older bridges.
BIND_FAILURE_MARKER = "bridge_port_range_full"
_BIND_FAILURE_LEGACY = "cannot bind any port in"

PORT_RANGE_FULL_HINT = ("stop a bridge (optix_bridge_arm action=stop on one of "
                        "them). The bridge's own port range is fixed at 4 "
                        "(PortRangeSize in StudioMCPBridge.cs); "
                        "OPTIX_BRIDGE_PORT_RANGE only widens the range the "
                        "service scans, so more than 4 simultaneous bridges "
                        "needs a regenerated bridge (raise PortRangeSize and "
                        "re-arm)")


def bind_failure_line(lines: "list[str] | None") -> str | None:
    """The bridge's bind-failure line among `lines` (the Studio log since the
    arm click), else None. The last match wins."""
    hits = [ln.strip() for ln in (lines or ())
            if BIND_FAILURE_MARKER in ln or _BIND_FAILURE_LEGACY in ln]
    return hits[-1] if hits else None


def probe_port_range(base: int = _DEFAULT_BASE,
                     span: int = _DEFAULT_RANGE) -> "dict[int, dict | None]":
    """{port: /bridge/health payload or None} for every port in the range."""
    return {p: _bridge_health(p) for p in range(base, base + max(1, span))}


def _range_full_payload(health_by_port: "dict[int, dict | None]", project: str,
                        detail: str | None = None) -> dict:
    """The `bridge_port_range_full` error: `ports`, `owners` [{port, project,
    pid}] for every port whose bridge answered, and the fixed `hint`."""
    owners = []
    for port, h in sorted(health_by_port.items()):
        if h is None:
            continue
        owners.append({"port": port,
                       "project": h.get("project") or h.get("project_name"),
                       "pid": health_studio_pid(h)})
    out = {"ok": False, "error": "bridge_port_range_full",
           "ports": sorted(health_by_port), "owners": owners,
           "hint": PORT_RANGE_FULL_HINT, "project": project}
    if detail:
        out["detail"] = detail
    return out


def port_range_full_result(health_by_port: "dict[int, dict | None]",
                           project: str, want: "set[str] | None" = None,
                           studio_pid: int | None = None,
                           project_dir: str | Path | None = None,
                           name_collision: bool = False) -> dict | None:
    """The `bridge_port_range_full` result when EVERY port in
    `health_by_port` answers a health probe for some OTHER project, else None
    (a free port, or one of the bridges is this project's own)."""
    if not health_by_port or any(h is None for h in health_by_port.values()):
        return None
    names = want or {project.strip().lower()}
    if any(_bridge_matches(h, project, names, studio_pid, project_dir,
                           name_collision=name_collision)
           for h in health_by_port.values()):
        return None
    return _range_full_payload(health_by_port, project)


# ---- the gesture ------------------------------------------------------------

# Home-screen markers. Studio's Home page lists RECENT projects as text rows
# whose labels (project name, full path) look exactly like the project-tree
# root row / in-scene path label that identify a LOADED project. A window
# showing one of these headers is the Home page, never a loaded project.
_HOME_HEADERS = frozenset({"recent", "recents", "recent projects", "recent files",
                           "recently opened", "recently opened projects"})
_TITLE_SUFFIX = "optixstudio"
_TITLE_SPLIT = re.compile(r"\s+[-\u2013\u2014|]\s+")


def is_home_screen(texts: list) -> bool:
    """True when the window's text rows (from `_row_texts`) are Studio's Home
    page: a "Recent projects" header is present. Its Recent entries must never
    be read as a loaded project."""
    for c in texts:
        try:
            if _norm(c.Name) in _HOME_HEADERS:
                return True
        except Exception:
            continue
    return False


def window_title_names_project(title: object, project: str,
                               aliases: set[str] | None = None,
                               project_dir: str | Path | None = None,
                               name_only_ok: bool = True) -> bool:
    """Does a Studio main-window title NAME the project?

    A loaded project shows up as a title segment (`<project> - FactoryTalk
    Optix Studio`, either order, `*` marks unsaved changes) or as the project's
    path. The bare product caption (what Home shows) names nothing.

    ``name_only_ok=False`` accepts only a title segment that carries the
    project DIRECTORY: a bare project name is no proof when another directory
    holds a project of the same name.
    """
    want = {project.strip().lower()} | {a.strip().lower() for a in (aliases or ())}
    pdir = str(project_dir).replace("/", "\\").rstrip("\\").lower() if project_dir else None
    for seg in _TITLE_SPLIT.split(str(title or "")):
        n = _norm(seg).strip("*").strip()
        if not n or _TITLE_SUFFIX in n.replace(" ", ""):
            continue
        if name_only_ok and n in want:
            return True
        n2 = n.replace("/", "\\")
        if pdir and (n2 == pdir or n2.startswith(pdir + "\\")):
            return True
    return False


def home_screen_pids(auto) -> set[int]:
    """Process ids of Studio windows currently showing the Home page."""
    out: set[int] = set()
    try:
        wins = auto.GetRootControl().GetChildren()
    except Exception:
        return out
    for w in wins:
        try:
            if w.ControlTypeName != "WindowControl":
                continue
            if _TITLE_SUFFIX not in (w.Name or "").replace(" ", "").casefold():
                continue
            if is_home_screen(_row_texts(w)):
                out.add(int(getattr(w, "ProcessId", 0) or 0))
        except Exception:
            continue
    return out


def _studio_window_for(auto, project: str, aliases: set[str] | None = None,
                       project_dir: str | Path | None = None,
                       skip_pids: "set[int] | frozenset[int] | tuple" = (),
                       ambiguous: bool = False,
                       proven_pids: "set[int] | frozenset[int] | tuple" = ()):
    """Studio top-level window that has `project` LOADED, or None.

    ``ambiguous=True`` means another directory holds a project of the same
    name: a NAME-only identity (title segment, root row) is then accepted only
    for a window whose pid is in ``proven_pids`` (bridge health dir, command
    line, or the service's own launch record); a directory-bearing identity
    (title/label carrying the path) is always accepted.

    Identity sources, per window:
      * the window title naming the project (see window_title_names_project);
      * the project tree's ROOT row, compared against the project dir name AND
        its aliases (the .optix stem / root node name) - `Line4_HMI` opens with
        a root row reading `Line4`;
      * the in-scene PATH label (`C:\\...\\Line4_HMI` or `...\\Line4.optix`),
        matched by directory - the root row disappears while the Project-view
        search filter is active ("No results found" replaces the tree), and
        the label is what is left.

    A Home page is NEVER a loaded project: its Recent-projects entries carry
    the same name / path labels, so a window showing the Recent header is
    skipped (#25). The tree identity needs no bridge, which is what makes this
    usable at arm time, when by definition no bridge exists yet. `skip_pids`
    drops windows of those Studio processes.
    """
    want = {project.strip().lower()} | {a.strip().lower() for a in (aliases or ())}
    pdir = str(project_dir).replace("/", "\\").rstrip("\\").lower() if project_dir else None
    for w in auto.GetRootControl().GetChildren():
        try:
            if w.ControlTypeName != "WindowControl":
                continue
            if "optixstudio" not in (w.Name or "").replace(" ", "").casefold():
                continue
            if skip_pids and int(getattr(w, "ProcessId", 0) or 0) in skip_pids:
                continue
            wpid = int(getattr(w, "ProcessId", 0) or 0)
            name_ok = not (ambiguous and wpid not in proven_pids)
            if window_title_names_project(w.Name, project, aliases, project_dir,
                                          name_only_ok=name_ok):
                return w
            rows = _row_texts(w)
            if is_home_screen(rows):
                continue
            for c in rows:
                # I34 (2026-09-04): Studio appends `*` to this text while the
                # project has unsaved changes -- the label measured that day read
                # `C:\Users\<u>\Desktop\NavArgDemo*`. An intolerant compare
                # answered studio_window_not_found for every bridge-edited
                # project (unsaved by definition until Ctrl+S), which reads
                # exactly like "Studio is not open". Strip the marker off BOTH
                # ends -- leading in case Studio ever moves it -- for the root-row
                # name match AND the path-prefix match below.
                n = _norm(c.Name).strip("*").strip()
                if name_ok and n in want:
                    return w
                if pdir and (n.replace("/", "\\") == pdir
                             or n.replace("/", "\\").startswith(pdir + "\\")):
                    return w
        except Exception:
            continue
    return None


# How many levels below the window a row's label may sit. 1.7 exposes the label
# as a direct child of the window (depth 1); 1.8 nests it inside a grouping
# container (depth 2-3). 4 leaves headroom without walking the whole scene.
_ROW_TEXT_DEPTH = 4
_ROW_TEXT_MAX_NODES = 3000


def _row_texts(root, max_depth: int = _ROW_TEXT_DEPTH) -> list:
    """TextControl descendants of `root` down to `max_depth` levels, in
    document order (depth 1 = direct children).

    This is the one place that knows the row shape. Both the 1.7 shape (label
    is a direct child) and the 1.8 shape (label inside a grouping child) come
    out as the same flat list, so every caller above is shape-agnostic.
    Open menus are not entered (their inner labels are not tree rows), text
    elements are leaves, and the walk is node-capped so a huge scene can't
    stall it.
    """
    out: list = []
    budget = [_ROW_TEXT_MAX_NODES]

    def walk(el, depth: int) -> None:
        try:
            kids = el.GetChildren()
        except Exception:
            return
        for c in kids:
            if budget[0] <= 0:
                return
            budget[0] -= 1
            try:
                kind = c.ControlTypeName
            except Exception:
                continue
            if kind == "TextControl":
                out.append(c)
            elif depth < max_depth and kind != "MenuControl":
                walk(c, depth + 1)

    walk(root, 1)
    return out


def _candidate_rows(win, names: list[str]) -> list:
    """Visible tree rows matching any of `names`, leftmost first. The label
    may be a direct child of `win` (1.7) or nested in a grouping child (1.8);
    see `_row_texts`.

    Leftmost-first means the OUTERMOST folder is tried before the leaf: it is
    the row most likely already on screen, and (fact 1) its menu carries the
    child NetLogic's Execute entries anyway.
    """
    wanted = {n.lower() for n in names}
    out = []
    for c in _row_texts(win):
        try:
            if _norm(c.Name) not in wanted:
                continue
            r = c.BoundingRectangle
            if r.width() <= 0 or r.height() <= 0:
                continue
            out.append(c)
        except Exception:
            continue
    out.sort(key=lambda c: c.BoundingRectangle.left)
    return out


def _indent_step(win, row) -> int:
    """Pixels per tree level, measured from the rows around `row`: the
    smallest positive gap between distinct row-text left edges in the pane.
    16 at 100 % scaling; falls back to that when there is nothing to measure.
    """
    lefts: set[int] = set()
    try:
        rr = row.BoundingRectangle
        for c in _row_texts(win):
            try:
                r = c.BoundingRectangle
                if r.height() != rr.height() or abs(r.left - rr.left) > 160:
                    continue
                # Same pane only: rows at or above the one being opened. The
                # Type-view tiles BELOW the tree share its row height and
                # x-band and read as a 6 px "indent" (fact 5).
                if r.top > rr.top or r.top <= 0:
                    continue
                lefts.add(r.left)
            except Exception:
                continue
    except Exception:
        pass
    ls = sorted(lefts)
    gaps = [b - a for a, b in zip(ls, ls[1:], strict=False) if 0 < b - a <= 40]
    return min(gaps) if gaps else 16


def _search_box(win):
    """The Project-view Search box: the window's only EditControl, top-left."""
    try:
        boxes = [c for c in win.GetChildren() if c.ControlTypeName == "EditControl"]
    except Exception:
        return None
    boxes.sort(key=lambda c: (c.BoundingRectangle.top, c.BoundingRectangle.left))
    return boxes[0] if boxes else None


class FocusError(RuntimeError):
    """Keyboard focus on a Studio edit box could not be confirmed.

    Raised instead of sending Ctrl+A / Delete / typed text blind: with focus
    elsewhere those keys edit (or delete) whatever Studio element has it.
    """


_FOCUS_ATTEMPTS = 3


def _has_keyboard_focus(auto, box) -> bool:
    """True only if ``box`` itself holds keyboard focus (UIA HasKeyboardFocus,
    or the focused element is the box). Any UIA error counts as unconfirmed."""
    try:
        if bool(box.HasKeyboardFocus):
            return True
    except Exception:
        pass
    try:
        focused = auto.GetFocusedControl()
        return focused is not None and focused == box
    except Exception:
        return False


def _confirm_focus(auto, box, attempts: int = _FOCUS_ATTEMPTS,
                   settle: float = 0.3) -> bool:
    """Click/focus ``box`` up to ``attempts`` times; True once focus is confirmed."""
    for _ in range(max(1, attempts)):
        try:
            r = box.BoundingRectangle
            auto.Click(r.left + r.width() // 2, r.top + r.height() // 2)
        except Exception:
            pass
        try:
            box.SetFocus()
        except Exception:
            pass
        if settle:
            time.sleep(settle)
        if _has_keyboard_focus(auto, box):
            return True
    return False


def _set_filter(auto, box, text: str, attempts: int = _FOCUS_ATTEMPTS,
                settle: float = 0.3) -> None:
    """Replace the Search box content, never sending keys without focus.

    uiautomation key syntax is {Ctrl}a — a ^a is typed LITERALLY (measured: it
    landed '^aStudioMCPBridge^a'). Keys are sent only after the box itself is
    confirmed to hold keyboard focus (bounded retries). If focus is never
    confirmed the value is set through ValuePattern when the box has one;
    otherwise FocusError is raised and no keystroke is sent.
    """
    if _confirm_focus(auto, box, attempts, settle):
        auto.SendKeys("{Ctrl}a{Delete}")
        if text:
            if settle:
                time.sleep(settle * 2 / 3)
            # Focus could have moved since the clear; re-check before typing.
            if not _has_keyboard_focus(auto, box):
                raise FocusError("Search box lost keyboard focus before typing; "
                                 "refusing to send keys blind")
            auto.SendKeys(text)
        return
    try:
        pattern = box.GetValuePattern()
    except Exception:
        pattern = None
    if pattern is not None:
        pattern.SetValue(text)
        return
    raise FocusError(f"could not confirm keyboard focus on the Search box after "
                     f"{attempts} attempts and it exposes no ValuePattern; "
                     f"refusing to send keys blind")


def _click_chevron(auto, win, row) -> int:
    """Toggle a collapsed tree row open by clicking its expander.

    Measured 2026-08-23 on 1.7.4.32: `{Right}` does nothing, a double-click on
    the row does nothing, and the chevron is NOT a UIA ButtonControl. What
    works is a click on the glyph itself, which sits two indent steps (plus
    the glyph's own padding) LEFT of the row text: 34 px at 100 % scaling,
    derived from the 16 px step so it follows DPI. The click is a TOGGLE —
    the caller must confirm the child row appeared and click again if it
    collapsed an already-open row instead. Returns the x clicked.
    """
    r = row.BoundingRectangle
    x = r.left - (2 * _indent_step(win, row) + 2)
    auto.Click(x, r.top + r.height() // 2)
    return x


def _child_visible(win, parent_row, name: str) -> bool:
    """True when a row called `name` sits BELOW and one level or so RIGHT of
    `parent_row` — i.e. in the tree, not the Properties pane that repeats the
    selected node's name far to the right."""
    try:
        pr = parent_row.BoundingRectangle
        reach = 4 * _indent_step(win, parent_row)
        for c in _candidate_rows(win, [name]):
            cr = c.BoundingRectangle
            if cr.top > pr.top and cr.left > pr.left and cr.left - pr.left <= reach:
                return True
    except Exception:
        pass
    return False


def _expand_patterns(row):
    """`row` then up to three ancestors: Qt exposes the expander on the list
    item wrapping the label as often as on the label itself."""
    el = row
    for _ in range(4):
        if el is None:
            return
        yield el
        try:
            el = el.GetParentControl()
        except Exception:
            return


def _expand_row(auto, win, row, child: str, settle: float = 0.8,
                child_shown: bool = False) -> str | None:
    """Open one collapsed tree row so that `child` appears below it.

    ExpandCollapsePattern first (state-aware, so it can never collapse an open
    row); the chevron click is the fallback, and is skipped when the pattern
    says the row is ALREADY open (a toggle would close it). Returns "pattern",
    "chevron", "open" (nothing to do), or None when the child still is not
    visible. `child_shown`: the child is ALREADY on screen, so only a row whose
    pattern reports Collapsed is acted on and the chevron is never clicked.
    """
    already_open = False
    for el in _expand_patterns(row):
        try:
            pat = el.GetExpandCollapsePattern()
        except Exception:
            continue
        if pat is None:
            continue
        try:
            state = int(pat.ExpandCollapseState)
        except Exception:
            state = None
        if state == 1:                     # Expanded
            if child_shown:
                return "open"
            already_open = True
            break
        if state == 3 or (child_shown and state != 0):
            continue                       # LeafNode / unknown: nothing to open
        try:
            pat.Expand()
        except Exception:
            continue
        time.sleep(settle)
        if child_shown or _child_visible(win, row, child):
            return "pattern"
        break
    if child_shown:
        return "open"
    if already_open:
        return None
    _click_chevron(auto, win, row)
    time.sleep(settle)
    if _child_visible(win, row, child):
        return "chevron"
    # A toggle on an already-open row collapses it; undo.
    _click_chevron(auto, win, row)
    time.sleep(settle)
    return None


def _expand_chain(auto, win, names: list[str], expanded: list[str],
                  tried: list[dict], verify_open: bool = False) -> bool:
    """Expand each collapsed ANCESTOR of the leaf (`names[:-1]`), outermost
    first, until the leaf row is on screen. False when an ancestor is not
    visible or cannot be opened — the caller then falls back to the leaf row
    alone. `verify_open`: a child on screen is not taken as proof its parent
    is open (the leaf offered an empty menu) — ask the pattern state instead.
    """
    for i, name in enumerate(names[:-1]):
        nxt = names[i + 1]
        rows = _candidate_rows(win, [name])
        if not rows:
            tried.append({"row": name, "visible": False, "phase": "expand"})
            return False
        row = rows[0]
        shown = _child_visible(win, row, nxt)
        if shown and not verify_open:
            continue                       # already open
        how = _expand_row(auto, win, row, nxt, child_shown=shown)
        if how == "open":
            continue
        if how is None:
            tried.append({"row": name, "expandable": False, "phase": "expand"})
            return False
        expanded.append(f"{how}:{name}")
    return True


def _right_click_leaf(auto, win, leaf: str, want: str, tried: list[dict],
                      **extra) -> object | None:
    """Right-click each filtered row for `leaf`; the Execute menu item, or None."""
    for row in _candidate_rows(win, [leaf]):
        r = row.BoundingRectangle
        rec = {"row": _strip_tags(row.Name), "x": r.left, "y": r.top,
               "leaf": True, **extra}
        auto.RightClick(r.left + r.width() // 2, r.top + r.height() // 2)
        time.sleep(0.8)
        items = _open_menu_items(win)
        rec["menu_items"] = len(items)
        hit = [el for nm, el in items if nm.strip() == want]
        tried.append(rec)
        if hit:
            return hit[0]
        auto.SendKeys("{Esc}")
        time.sleep(0.3)
    return None


def _leaf_pass(auto, win, names: list[str], want: str, tried: list[dict],
               expanded: list[str]) -> object | None:
    """The filtered-tree pass: find `Execute <method>` on the leaf's own menu.

    When an ancestor is not visible in the filtered tree, or the leaf offers no
    menu (0 items) / no row, the collapsed ancestors are expanded in order and
    the leaf is retried. When expansion is impossible the filtered leaf row is
    right-clicked directly (recorded with direct=True).
    """
    leaf, ancestors = names[-1], names[:-1]
    if ancestors and not all(_candidate_rows(win, [a]) for a in ancestors):
        if _expand_chain(auto, win, names, expanded, tried):
            return _right_click_leaf(auto, win, leaf, want, tried, expanded=True)
        return _right_click_leaf(auto, win, leaf, want, tried, direct=True)
    mark = len(tried)
    target = _right_click_leaf(auto, win, leaf, want, tried)
    if target is not None or not ancestors:
        return target
    leaf_recs = tried[mark:]
    if leaf_recs and all(t.get("menu_items") for t in leaf_recs):
        return None                        # a real menu without the entry
    if _expand_chain(auto, win, names, expanded, tried, verify_open=True):
        return _right_click_leaf(auto, win, leaf, want, tried, expanded=True)
    return _right_click_leaf(auto, win, leaf, want, tried, direct=True)


def _open_menu_items(win) -> list[tuple[str, object]]:
    """(name, element) for items of any in-scene menu open under `win`.

    Filtered to MenuItemControl because a descendants walk returns each entry
    twice (the item plus its inner label).
    """
    items: list[tuple[str, object]] = []
    try:
        for m in win.GetChildren():
            if m.ControlTypeName != "MenuControl":
                continue
            stack, seen = list(m.GetChildren()), 0
            while stack and seen < 500:
                el = stack.pop()
                seen += 1
                try:
                    if el.ControlTypeName == "MenuItemControl" and (el.Name or ""):
                        items.append((el.Name, el))
                    stack.extend(el.GetChildren())
                except Exception:
                    continue
    except Exception:
        pass
    return items


def _find_menubar_entry(win, label: str, max_nodes: int = 1500):
    """The menu-bar header named `label` (e.g. "File") under `win`, or None.

    Bounded breadth-first walk over MenuItem / Button / Text / MenuBar-item
    controls; open popups (MenuControl) are not entered - their entries are
    read with `_open_menu_items`.
    """
    queue, seen = list(win.GetChildren()), 0
    while queue and seen < max_nodes:
        el = queue.pop(0)
        seen += 1
        try:
            kind = el.ControlTypeName
            if kind == "MenuControl":
                continue
            if (kind in ("MenuItemControl", "ButtonControl", "TextControl")
                    and (el.Name or "").strip() == label):
                r = el.BoundingRectangle
                if r.width() > 0 and r.height() > 0:
                    return el
            queue.extend(el.GetChildren())
        except Exception:
            continue
    return None


def _close_menu(auto) -> None:
    """Dismiss an open popup menu (Esc; carries no text, so not an edit)."""
    auto.SendKeys("{Esc}")


def _window_for_pid(auto, project: str, aliases: set[str] | None,
                    project_dir: str | Path | None, studio_pid: int | None):
    """(window, None) for the project's Studio window, restricted to
    `studio_pid` when given; (None, {"ok": False, "error": ...}) otherwise."""
    skip: set[int] = set()
    win = None
    try:
        for _ in range(8):
            win = _studio_window_for(auto, project, aliases, project_dir,
                                     skip_pids=skip)
            if win is None or not studio_pid:
                break
            wpid = int(getattr(win, "ProcessId", 0) or 0)
            if wpid == studio_pid:
                break
            skip.add(wpid)
            win = None
    except Exception as e:
        return None, {"ok": False, "error": "uia_failed",
                      "detail": f"{type(e).__name__}: {e}"}
    if win is None:
        return None, {"ok": False, "error": "studio_window_not_found"}
    return win, None


def focus_project_tree(project: str, project_dir: str | Path | None = None,
                       aliases: set[str] | None = None,
                       studio_pid: int | None = None) -> dict:
    """Left-click the project's root row in Studio's project tree.

    Used by core.save before re-sending Ctrl+S: the keystroke is swallowed by
    whatever has focus after a NetLogic create (code editor / web view), and
    Studio 1.8 has no File menu to fall back to. `studio_pid` restricts the
    window to that Studio process (the tree click must land in THAT Studio).
    The row is found with `_candidate_rows(win, [project, *aliases])`, the same
    finder arm uses. Prior cursor and foreground window are restored. Returns
    {"ok": True, "clicked": <row name>} or {"ok": False, "error": ...}
    (`tree_row_not_found`, `studio_window_not_found`, `uia_failed`,
    `uiautomation_unavailable`); never raises.
    """
    try:
        import uiautomation as auto  # lazy: Windows-only, may be absent
    except Exception as e:
        return {"ok": False, "error": "uiautomation_unavailable", "detail": str(e)}
    if aliases is None and project_dir is not None:
        try:
            aliases = core.project_served_names(Path(project_dir))
        except Exception:
            aliases = None
    win, err = _window_for_pid(auto, project, aliases, project_dir, studio_pid)
    if err is not None:
        return err
    prev_cursor = prev_fg = None
    try:
        prev_cursor = auto.GetCursorPos()
        prev_fg = auto.GetForegroundControl()
        win.SetActive()
        time.sleep(0.4)
        names = [project, *sorted(aliases or ())]
        rows = _candidate_rows(win, names)
        if not rows:
            return {"ok": False, "error": "tree_row_not_found"}
        row = rows[0]
        r = row.BoundingRectangle
        auto.Click(r.left + r.width() // 2, r.top + r.height() // 2)
        time.sleep(0.3)
        return {"ok": True, "clicked": _strip_tags(row.Name)}
    except Exception as e:  # pragma: no cover - UIA runtime failure
        return {"ok": False, "error": "uia_failed",
                "detail": f"{type(e).__name__}: {e}"}
    finally:
        try:
            if prev_cursor:
                auto.SetCursorPos(*prev_cursor)
            if prev_fg:
                prev_fg.SetActive()
        except Exception:
            pass


def studio_window_title(pid: int) -> str | None:
    """Title of Studio process `pid`'s main window via Win32 (EnumWindows), or
    None when it cannot be read (non-Windows, no visible window). Cheap, so
    core.save can sample it without a UIA walk."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    except Exception:
        return None
    best: list = [-1, None]
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(h, _l):
        try:
            p = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(p))
            if p.value != pid or not user32.IsWindowVisible(h) or user32.GetWindow(h, 4):
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(rect))
            area = (rect.right - rect.left) * (rect.bottom - rect.top)
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(h, buf, 512)
            if "optix" in buf.value.lower():
                area += 1 << 40
            if area > best[0]:
                best[0], best[1] = area, buf.value
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(proto(_cb), 0)
    except Exception:
        return None
    return best[1]


_SAVE_ITEM = re.compile(r"^save(\s*\t.*|\s+ctrl\+s)?$", re.I)


def save_via_menu(project: str, project_dir: str | Path | None = None,
                  aliases: set[str] | None = None,
                  studio_pid: int | None = None) -> dict:
    """Invoke Studio's File -> Save for `project` via UI Automation.

    The fallback behind core.save when Ctrl+S is swallowed by the active
    document. Same menu path as arm: the project's Studio window, an entry
    click, then `_open_menu_items` to read the opened popup and click the item.
    `studio_pid` restricts the window to that Studio process. Prior foreground
    window and cursor are restored. Returns {"ok": True, "clicked": "Save"} or
    {"ok": False, "error": ...} (`save_disabled` = the item is greyed out, i.e.
    nothing unsaved); never raises.
    """
    try:
        import uiautomation as auto  # lazy: Windows-only, may be absent
    except Exception as e:
        return {"ok": False, "error": "uiautomation_unavailable", "detail": str(e)}
    win, err = _window_for_pid(auto, project, aliases, project_dir, studio_pid)
    if err is not None:
        return err
    prev_cursor = prev_fg = None
    try:
        prev_cursor = auto.GetCursorPos()
        prev_fg = auto.GetForegroundControl()
        win.SetActive()
        time.sleep(0.4)
        entry = _find_menubar_entry(win, "File")
        if entry is None:
            return {"ok": False, "error": "file_menu_not_found"}
        r = entry.BoundingRectangle
        auto.Click(r.left + r.width() // 2, r.top + r.height() // 2)
        time.sleep(0.6)
        items = _open_menu_items(win)
        hit = [el for nm, el in items if _SAVE_ITEM.match(nm.strip())]
        if not hit:
            _close_menu(auto)
            return {"ok": False, "error": "save_item_not_found",
                    "menu_items": [nm for nm, _ in items][:20]}
        item = hit[0]
        if not getattr(item, "IsEnabled", True):
            _close_menu(auto)
            return {"ok": False, "error": "save_disabled"}
        r = item.BoundingRectangle
        auto.Click(r.left + r.width() // 2, r.top + r.height() // 2)
        time.sleep(0.5)
        return {"ok": True, "clicked": "Save"}
    except Exception as e:  # pragma: no cover - UIA runtime failure
        return {"ok": False, "error": "uia_failed",
                "detail": f"{type(e).__name__}: {e}"}
    finally:
        try:
            if prev_cursor:
                auto.SetCursorPos(*prev_cursor)
            if prev_fg:
                prev_fg.SetActive()
        except Exception:
            pass


_CONSENT = re.compile(r"^(Proceed|Yes|Run|Execute|OK)$")
# "Do not show this warning again". Studio's wording varies in casing and
# spacing, so match loosely on the words that carry the meaning.
_SUPPRESS = re.compile(r"do\s*not\s*show.*again", re.I)


def _tick_suppress(auto, box) -> bool:
    """Tick a 'do not show again' checkbox, preferring the toggle pattern.

    Best-effort BY DESIGN: the consent click is what actually unblocks Studio,
    so failing to suppress must never stop us from clicking Proceed.
    """
    try:
        tp = box.GetTogglePattern()
        if tp.ToggleState == 1:          # ToggleState_On — already ticked
            return True
        tp.Toggle()
        return True
    except Exception:
        pass
    try:
        r = box.BoundingRectangle
        auto.Click(r.left + r.width() // 2, r.top + r.height() // 2)
        return True
    except Exception:
        return False


def _press(auto, button) -> str:
    """Press a dialog button, preferring UIA InvokePattern over a screen click.

    InvokePattern acts on the element itself, so it works when the button's
    centre is off-screen or under the taskbar (a tall security warning on a
    small display), where a coordinate click lands on whatever is painted
    there. The coordinate click is only the fallback for an element that has
    no InvokePattern (or whose Invoke raises). Returns "invoke" or "click".
    """
    try:
        ip = button.GetInvokePattern()
        if ip is not None:
            ip.Invoke()
            return "invoke"
    except Exception:
        pass
    r = button.BoundingRectangle
    auto.Click(r.left + r.width() // 2, r.top + r.height() // 2)
    return "click"


def _scan_popup(d) -> tuple[object, object] | None:
    """(suppress_checkbox_or_None, consent_button) inside one popup `d`, or
    None when it holds no consent button. The whole popup is walked: the
    button is often reached before the checkbox."""
    stack, seen = list(d.GetChildren()), 0
    suppress, button = None, None
    while stack and seen < 300:
        el = stack.pop()
        seen += 1
        try:
            nm = (el.Name or "").strip()
            if (suppress is None
                    and el.ControlTypeName == "CheckBoxControl"
                    and _SUPPRESS.search(nm)):
                suppress = el
            elif (button is None
                    and el.ControlTypeName == "ButtonControl"
                    and _CONSENT.match(nm)):
                button = el
            stack.extend(el.GetChildren())
        except Exception:
            continue
    return (suppress, button) if button is not None else None


def _find_consent(win) -> tuple[object, object, object] | None:
    """Locate Studio's consent prompt inside `win`'s in-scene popups.

    Returns (dialog, suppress_checkbox_or_None, button) for the first popup
    holding a consent button, else None. Scans ONLY popups, never the whole
    window, and skips the Studio chrome itself.
    """
    for d in win.GetChildren():
        if d.ControlTypeName not in ("WindowControl", "PaneControl"):
            continue
        if "optixstudio" in (d.Name or "").replace(" ", "").casefold():
            continue
        found = _scan_popup(d)
        if found is not None:
            return d, found[0], found[1]
    return None


# A NetLogic security warning raised as its OWN top-level window rather than
# an in-scene popup.
_SECURITY_WINDOW = re.compile(r"security|warning|netlogic", re.I)


def _find_security_window(auto, win) -> tuple[object, object, object] | None:
    """(dialog, suppress_or_None, button) for a NetLogic security-warning
    TOP-LEVEL window owned by the same process as Studio window `win`, else
    None. The pid check keeps another application's 'Security Warning' dialog
    from ever being pressed."""
    try:
        pid = win.ProcessId
    except Exception:
        return None
    try:
        tops = auto.GetRootControl().GetChildren()
    except Exception:
        return None
    for w in tops:
        try:
            if w.ControlTypeName != "WindowControl" or w.ProcessId != pid:
                continue
            nm = (w.Name or "")
            if "optixstudio" in nm.replace(" ", "").casefold():
                continue
            if not _SECURITY_WINDOW.search(nm):
                continue
            found = _scan_popup(w)
            if found is not None:
                return w, found[0], found[1]
        except Exception:
            continue
    return None


def _clear_security_window(auto, win) -> dict | None:
    """Press Proceed on a Studio-owned NetLogic security-warning window, via
    the same helpers as the in-scene consent path (`_tick_suppress`, then
    `_press`, which prefers UIA InvokePattern). None when no such window is up,
    else {"title", "method", "dismissed"}."""
    found = _find_security_window(auto, win)
    if found is None:
        return None
    dlg, suppress, button = found
    if suppress is not None:
        _tick_suppress(auto, suppress)
        time.sleep(0.15)
    how = _press(auto, button)
    gone = False
    for i in range(6):
        if _find_security_window(auto, win) is None:
            gone = True
            break
        if i < 5:
            time.sleep(0.25)
    return {"title": (getattr(dlg, "Name", "") or "").strip() or None,
            "method": how, "dismissed": gone}


def _click_consent(auto, win, attempts: int = 12, report: dict | None = None,
                   settle_attempts: int = 6) -> bool:
    """Tick 'do not show again', then press Studio's security prompt.

    Returns True when a consent button was pressed. That alone does NOT mean
    the dialog went away: after pressing, the popup is re-queried and, when
    `report` is given, filled with `consent_dismissed` (True when no consent
    prompt remains) and `dialog_title` (the surviving dialog's title, or None).

    Scans ONLY inside in-scene popups. Never the whole window: a stray 'Yes' or
    'OK' elsewhere in the UI must never be clicked.

    The checkbox is ticked BEFORE consenting because pressing Proceed destroys
    the dialog and the box with it. Studio raises this warning once per
    TRIGGER, not once per project, so without the tick the same modal blocks
    the arm, then the first F5, then the next one (verified 2026-09-01: arm
    reported consent_clicked, and F5 immediately hit the identical dialog).
    Ticking it makes the dismissal stick for the life of the project.

    NOTE the whole popup is walked before anything is pressed — the button is
    frequently reached before the checkbox in traversal order, and consenting
    early would dismiss the dialog with the box still unticked.
    """
    if report is not None:
        report.update(consent_dismissed=True, dialog_title=None)
    for _ in range(attempts):
        try:
            found = _find_consent(win)
            if found is not None:
                _dlg, suppress, button = found
                if suppress is not None:
                    _tick_suppress(auto, suppress)
                    time.sleep(0.15)
                _press(auto, button)
                remaining = _await_dismissal(win, settle_attempts)
                if report is not None:
                    report["consent_dismissed"] = remaining is None
                    report["dialog_title"] = (
                        (getattr(remaining, "Name", "") or "").strip() or None
                        if remaining is not None else None)
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _await_dismissal(win, attempts: int = 6):
    """Re-query the popups after a press; None once the prompt is gone, else
    the dialog that survived."""
    dlg = None
    for i in range(max(1, attempts)):
        try:
            found = _find_consent(win)
        except Exception:
            found = None
        if found is None:
            return None
        dlg = found[0]
        if i + 1 < attempts:
            time.sleep(0.25)
    return dlg


def clear_consent(project: str, project_dir: str | Path | None = None,
                  aliases: set[str] | None = None,
                  attempts: int = 8, cfg: "core.Config | None" = None) -> dict:
    """Answer Studio's NetLogic security warning for `project`, if it is up.

    `project_dir`: an explicit directory wins. When omitted, it is resolved
    through core.resolve_project (the same resolution every other tool uses;
    `cfg` defaults to Config.from_env()). A name that cannot be resolved
    returns {"ok": False, "error": "project_not_found", "project": ...} --
    never a silent no-op.

    Exists because the emulator's F5 raises the SAME modal the arm path already
    handles, and the modal EATS the keystroke until answered. Clicking Proceed
    lets the ALREADY-SENT F5 continue, so the caller must NOT resend it — F5
    toggles, and a resend would stop the emulator it just started.

    Foreground window and cursor are restored the way execute_method restores
    them, so clearing a dialog mid-wait doesn't permanently steal focus.
    """
    if project_dir is None:
        try:
            resolved = core.resolve_project(cfg or core.Config.from_env(), project)
        except Exception as e:
            return {"ok": False, "error": "project_not_found",
                    "project": project, "detail": f"{type(e).__name__}: {e}"}
        project_dir = resolved
        if aliases is None:
            try:
                aliases = core.project_served_names(resolved)
            except Exception:
                aliases = None

    try:
        import uiautomation as auto  # lazy: Windows-only, may be absent
    except Exception as e:  # pragma: no cover - import guard
        return {"ok": False, "error": "uiautomation_unavailable", "detail": str(e)}

    win = _studio_window_for(auto, project, aliases, project_dir)
    if win is None:
        return {"ok": False, "error": "studio_window_not_found", "project": project}

    prev_cursor = auto.GetCursorPos()
    prev_fg = auto.GetForegroundControl()
    try:
        win.SetActive()
        time.sleep(0.3)
        rep: dict = {}
        clicked = _click_consent(auto, win, attempts, report=rep)
        out = {"ok": True, "consent_clicked": clicked,
               "consent_dismissed": rep.get("consent_dismissed", True)}
        if clicked and not out["consent_dismissed"]:
            out.update(ok=False, error="consent_not_dismissed",
                       dialog=rep.get("dialog_title"))
        return out
    except Exception as e:  # pragma: no cover - UIA runtime failure
        return {"ok": False, "error": "uia_failed",
                "detail": f"{type(e).__name__}: {e}"}
    finally:
        try:
            auto.SetCursorPos(*prev_cursor)
            if prev_fg:
                prev_fg.SetActive()
        except Exception:
            pass


def studio_pid_for(project: str, aliases: set[str] | None = None,
                   project_dir: str | Path | None = None,
                   name_collision: bool = False,
                   proven_pids: "set[int] | frozenset[int] | tuple" = ()) -> int | None:
    """Process id of the Studio whose window shows `project`, or None.

    Best-effort and read-only (no focus, no clicks): None off Windows, without
    uiautomation, or when no window identifies the project. Callers then fall
    back to name + project_dir matching (see serving_port_for).
    """
    try:
        import uiautomation as auto  # lazy: Windows-only, may be absent
        win = _studio_window_for(auto, project, aliases, project_dir,
                                 ambiguous=name_collision, proven_pids=proven_pids)
        pid = int(getattr(win, "ProcessId", 0) or 0) if win is not None else 0
        return pid or None
    except Exception:
        return None


ELEVATED_HINT = ("Studio is running as administrator; start it without "
                 "elevation, or run the service elevated")

# PowerShell helper: reports token elevation of THIS process (the service's
# token, inherited by the child) and of every FTOptixStudio process. A
# non-elevated caller cannot open an elevated process's token (access denied),
# which is reported as elevated=$true with denied=$true: that denial is itself
# the signature of an integrity gap.
_ELEVATION_PS = (
    "Add-Type -MemberDefinition '"
    "[DllImport(\"kernel32.dll\")] public static extern System.IntPtr OpenProcess(uint a, bool i, uint pid); "
    "[DllImport(\"kernel32.dll\")] public static extern bool CloseHandle(System.IntPtr h); "
    "[DllImport(\"advapi32.dll\")] public static extern bool OpenProcessToken(System.IntPtr p, uint a, out System.IntPtr t); "
    "[DllImport(\"advapi32.dll\")] public static extern bool GetTokenInformation(System.IntPtr t, int c, out int v, int n, out int r); "
    "public static int Elevation(uint pid) { "
    "System.IntPtr p = OpenProcess(0x1000, false, pid); if (p == System.IntPtr.Zero) return -1; "
    "System.IntPtr t; int e = -1; "
    "if (OpenProcessToken(p, 8, out t)) { int v; int r; "
    "if (GetTokenInformation(t, 20, out v, 4, out r)) e = v; CloseHandle(t); } "
    "CloseHandle(p); return e; }' -Name FtxElev -Namespace Ftx; "
    "$svc = [Ftx.FtxElev]::Elevation([uint32]$PID) -eq 1; "
    "$studio = @(Get-Process FTOptixStudio -ErrorAction SilentlyContinue | ForEach-Object { "
    "$e = [Ftx.FtxElev]::Elevation([uint32]$_.Id); "
    "@{ pid = $_.Id; elevated = ($e -ne 0); denied = ($e -eq -1) } }); "
    "@{ service_elevated = $svc; studio = $studio } | ConvertTo-Json -Compress -Depth 4"
)


def probe_elevation(runner: "core.Runner | None" = None) -> dict | None:
    """Run the PowerShell elevation helper; parse its JSON, or None on failure.

    Shape: {"service_elevated": bool, "studio": [{"pid", "elevated", "denied"}]}.
    Never raises (off Windows there is no powershell -> None).
    """
    runner = runner or core._DEFAULT_RUNNER
    try:
        proc = runner.run_powershell(_ELEVATION_PS, timeout=15)
        if getattr(proc, "returncode", 1) != 0:
            return None
        data = json.loads((proc.stdout or "").strip())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    studio = data.get("studio")
    if isinstance(studio, dict):  # ConvertTo-Json unwraps single-item arrays
        studio = [studio]
    return {"service_elevated": bool(data.get("service_elevated")),
            "studio": [s for s in (studio or []) if isinstance(s, dict)]}


def window_missing_result(project: str, probe: dict | None) -> dict:
    """Decide the error for "no Studio window is visible to the service".

    Returns studio_elevated_service_not when a Studio process is running that
    is elevated while the service is not (Windows hides an elevated window
    from a non-elevated process); otherwise studio_window_not_found.
    """
    open_hint = (f"open {project!r} in Studio (optix_project "
                 f"action='open'), then retry")
    if probe and not probe.get("service_elevated"):
        elevated = [s.get("pid") for s in (probe.get("studio") or [])
                    if s.get("elevated")]
        if elevated:
            return {"ok": False, "error": "studio_elevated_service_not",
                    "project": project, "studio_pids": elevated,
                    "hint": ELEVATED_HINT, "nudge": ELEVATED_HINT}
    return {"ok": False, "error": "studio_window_not_found", "project": project,
            "nudge": open_hint}


OS_DIALOG_HINT = "close or answer the dialog, then retry arm"
_STUDIO_PROCESS_PREFIX = "ftoptixstudio"


def os_dialog_hint(blocker: dict) -> str:
    """Hint for a blocking foreign window: names it and says to dismiss it.
    A firewall / consent prompt (kind 'firewall_prompt') gets the desktop /
    New-NetFirewallRule text instead."""
    if blocker.get("kind") == FIREWALL_PROMPT_KIND:
        return ("a Windows Firewall / consent prompt is open; answer it on the "
                "desktop (or pre-create the firewall rule: New-NetFirewallRule "
                "-Program <coreservicehost.exe> -Action Allow), then re-arm")
    title = blocker.get("title") or "(untitled)"
    proc = blocker.get("process") or blocker.get("pid")
    return (f"a window titled {title!r} (process {proc}) is in front of / over "
            f"Studio - dismiss it (close or answer the dialog), then retry")


def _process_name(pid: int) -> str | None:
    """Executable name of process `pid`, or None when it cannot be read."""
    try:
        import psutil
        return psutil.Process(int(pid)).name()
    except Exception:
        return None


def _foreground_window(auto) -> dict | None:
    """The foreground top-level window as {title, pid}, or None.

    The UIA window-enumeration seam: tests replace it. Never raises.
    """
    if auto is None:
        return None
    try:
        fg = auto.GetForegroundControl()
        if fg is None:
            return None
        top = fg
        try:
            top = fg.GetTopLevelControl() or fg
        except Exception:
            pass
        pid = int(getattr(top, "ProcessId", 0) or 0)
        if not pid:
            return None
        out = {"title": str(getattr(top, "Name", "") or ""), "pid": pid}
        try:
            out["hwnd"] = int(getattr(top, "NativeWindowHandle", 0) or 0)
        except Exception:
            out["hwnd"] = 0
        out["class"] = str(getattr(top, "ClassName", "") or "")
        return out
    except Exception:
        return None


def _win32_top_level_windows() -> list[dict]:
    """Visible, titled, non-minimised top-level windows via Win32 ctypes
    (EnumWindows + IsWindowVisible + GetWindowText), topmost first.

    Each item is {title, pid, hwnd, class, rect: (left, top, right, bottom),
    foreground}.
    The Win32 enumeration seam: tests replace it. [] off-Windows or on any
    failure; never raises.
    """
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]
    except Exception:
        return []
    out: list[dict] = []
    try:
        fg_hwnd = user32.GetForegroundWindow()
    except Exception:
        fg_hwnd = None
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(h, _l):
        try:
            if not user32.IsWindowVisible(h) or user32.IsIconic(h):
                return True
            cloaked = wintypes.DWORD()
            if dwm.DwmGetWindowAttribute(h, 14, ctypes.byref(cloaked), 4) == 0 \
                    and cloaked.value:
                return True  # DWMWA_CLOAKED: on another desktop / suspended UWP
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(h, buf, 512)
            if not buf.value:
                return True
            p = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(p))
            rect = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(rect))
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, cls, 256)
            out.append({"title": buf.value, "pid": int(p.value),
                        "hwnd": int(h or 0), "class": cls.value,
                        "rect": (rect.left, rect.top, rect.right, rect.bottom),
                        "foreground": bool(fg_hwnd) and h == fg_hwnd})
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(proto(_cb), 0)
    except Exception:
        return []
    return out


def _rects_overlap(a, b) -> bool:
    """True when two (left, top, right, bottom) rects share positive area."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _is_studio_pid(pid: int | None, studio_pid: int | None,
                   cache: dict[int, bool]) -> bool:
    """True for `studio_pid` or any FTOptixStudio process."""
    if not pid:
        return False
    if studio_pid and pid == studio_pid:
        return True
    if pid not in cache:
        name = _process_name(pid)
        cache[pid] = bool(name and name.casefold().startswith(_STUDIO_PROCESS_PREFIX))
    return cache[pid]


_CONSOLE_HOST_NAMES = ("conhost", "openconsole", "windowsterminal")


def _own_process_tree_pids() -> set[int]:
    """Pids of this service's own process tree: the service itself, every
    ancestor (the shell, the terminal that hosts it) and the console hosts
    (conhost / OpenConsole / WindowsTerminal) among the children of those. A
    window owned by one of these is the operator's console, never an OS dialog
    blocking Studio. Never raises; empty-ish when psutil cannot answer."""
    import os
    pids: set[int] = {os.getpid()}
    try:
        import psutil
        me = psutil.Process(os.getpid())
        chain = [me]
        try:
            chain += me.parents()
        except Exception:
            pass
        for proc in chain:
            pids.add(int(proc.pid))
        for proc in chain:
            try:
                kids = proc.children(recursive=False)
            except Exception:
                continue
            for kid in kids:
                try:
                    name = (kid.name() or "").casefold()
                except Exception:
                    continue
                if any(name.startswith(h) for h in _CONSOLE_HOST_NAMES):
                    pids.add(int(kid.pid))
    except Exception:
        pass
    return pids


_GA_ROOTOWNER = 3
_DIALOG_CLASS = "#32770"
_DIALOG_PROCESS_MARKERS = ("pickerhost", "consent", "windowssecurity")
#: Processes that host the Windows Firewall / consent prompt (PRD 0055, #94).
#: The prompt can be a zero-rect window that overlaps nothing and is not in
#: front, so these count as the blocker whatever their size or position.
_CONSENT_PROCESS_MARKERS = _DIALOG_PROCESS_MARKERS
#: ShellExperienceHost also draws ordinary shell UI: it only counts when its
#: window title is a security alert.
_CONSENT_SHELL_PROCESS = "shellexperiencehost"
_CONSENT_SHELL_TITLE_MARKERS = ("security alert", "windows security", "firewall")
FIREWALL_PROMPT_KIND = "firewall_prompt"


def _own_window_filter() -> dict:
    """Windows that belong to this service's console, whatever process owns
    them: ``{"hwnds": {...}, "needles": [...]}``. ``hwnds`` is the console
    window (``GetConsoleWindow``) and its root owner (``GetAncestor(hwnd,
    GA_ROOTOWNER)``): the console host (conhost / OpenConsole / Windows
    Terminal) owns it, and it need not be in the service's process tree.
    ``needles`` are lower-cased, backslash-normalised paths of the service
    executable and python.exe: a console window is titled with the path of the
    program it runs. Computed once per call; never raises."""
    import os
    import sys
    hwnds: set[int] = set()
    try:
        import ctypes
        h = ctypes.windll.kernel32.GetConsoleWindow()  # type: ignore[attr-defined]
        if h:
            hwnds.add(int(h))
            root = ctypes.windll.user32.GetAncestor(h, _GA_ROOTOWNER)  # type: ignore[attr-defined]
            if root:
                hwnds.add(int(root))
    except Exception:
        pass
    paths: list[str] = []
    for cand in (sys.executable, getattr(sys, "_base_executable", None)):
        if cand:
            paths.append(str(cand))
    try:
        import psutil
        paths.append(psutil.Process(os.getpid()).exe())
    except Exception:
        pass
    needles: list[str] = []
    for pth in paths:
        for v in (pth, os.path.realpath(pth)):
            n = v.replace("/", "\\").casefold()
            if n and n not in needles:
                needles.append(n)
    return {"hwnds": hwnds, "needles": needles}


#: Desktop / shell windows: never a dialog blocking Studio (PRD 0053 B, #85).
_SHELL_WINDOW_CLASSES = frozenset({"progman", "workerw", "shell_traywnd",
                                   "shell_secondarytraywnd"})
_SHELL_WINDOW_TITLES = frozenset({"program manager", "taskbar"})


def _is_shell_window(w: dict) -> bool:
    """True for the desktop (`Progman`, `WorkerW`), the taskbar
    (`Shell_TrayWnd`, `Shell_SecondaryTrayWnd`, titled "Taskbar") and the
    "Program Manager" window, whatever the source reported."""
    cls = str(w.get("class") or "").casefold()
    if cls in _SHELL_WINDOW_CLASSES:
        return True
    return str(w.get("title") or "").strip().casefold() in _SHELL_WINDOW_TITLES \
        and cls in ("", "progman", "shell_traywnd")


def _is_own_window(w: dict, exclude: set[int], own: dict) -> bool:
    """True for a shell window, a window of the service's own process tree,
    its console window / root owner, or one titled with the service /
    python.exe path."""
    if _is_shell_window(w):
        return True
    pid = w.get("pid")
    if pid and pid in exclude:
        return True
    hwnd = w.get("hwnd")
    if hwnd and hwnd in own.get("hwnds", ()):
        return True
    title = str(w.get("title") or "").replace("/", "\\").casefold()
    return bool(title) and any(n in title for n in own.get("needles", ()))


def _blocker_rank(w: dict, process: str | None) -> tuple:
    """Sort key (smaller = more likely the blocker): the #32770 dialog class,
    then PickerHost / consent / WindowsSecurity processes, then the foreground
    window; ties keep enumeration (topmost first) order."""
    proc = (process or "").casefold()
    return (0 if w.get("class") == _DIALOG_CLASS else 1,
            0 if any(m in proc for m in _DIALOG_PROCESS_MARKERS) else 1,
            0 if w.get("foreground") else 1)


def _is_consent_window(w: dict, process: str | None) -> bool:
    """True when window `w` (owned by `process`, an executable name) is a known
    Windows Firewall / consent prompt: a PickerHost / consent / WindowsSecurity
    process, or ShellExperienceHost with a security-alert title. Size, position
    and foreground state are irrelevant."""
    proc = (process or "").casefold()
    if any(m in proc for m in _CONSENT_PROCESS_MARKERS):
        return True
    if _CONSENT_SHELL_PROCESS in proc:
        title = str(w.get("title") or "").casefold()
        return any(m in title for m in _CONSENT_SHELL_TITLE_MARKERS)
    return False


def _blocker_dict(w: dict, process: str | None, source: str) -> dict:
    """{title, process, pid, class, source}; plus kind 'firewall_prompt' when
    the window is a known consent prompt."""
    pid = int(w["pid"])
    out = {"title": w.get("title") or "", "process": process or str(pid),
           "pid": pid, "class": w.get("class") or "", "source": source}
    if _is_consent_window(w, process):
        out["kind"] = FIREWALL_PROMPT_KIND
    return out


def _win32_blocking_window(studio_pid: int | None,
                           exclude: set[int] | None = None,
                           own: dict | None = None,
                           consent_only: bool = False) -> dict | None:
    """Second source: a visible foreign top-level window that is the foreground
    window, or whose rect overlaps Studio's window. None when nothing matches.
    Windows owned by a pid in `exclude` (the service's own process tree), the
    console window and its root owner, and windows titled with the service /
    python.exe path (`own`, see `_own_window_filter`) never count; both are
    computed once when omitted. Candidates are ranked: #32770 dialog class,
    then PickerHost / consent / WindowsSecurity processes, then foreground,
    then topmost. A known consent prompt (`_is_consent_window`) is a candidate
    whatever its size or overlap (a zero-rect PickerHost window counts) and
    outranks every other candidate; `consent_only` returns only such a window."""
    try:
        wins = _win32_top_level_windows()
    except Exception:
        return None
    if not wins:
        return None
    if exclude is None:
        exclude = _own_process_tree_pids()
    if own is None:
        own = _own_window_filter()
    cache: dict[int, bool] = {}
    studio_rects = [w["rect"] for w in wins
                    if w.get("rect") and _is_studio_pid(w.get("pid"), studio_pid, cache)]
    candidates = []
    consent = []
    for w in wins:
        pid = w.get("pid")
        if not pid or _is_own_window(w, exclude, own) \
                or _is_studio_pid(pid, studio_pid, cache):
            continue
        if _is_consent_window(w, _process_name(int(pid))):
            consent.append(w)
            continue
        rect = w.get("rect")
        overlaps = bool(rect) and any(_rects_overlap(rect, r) for r in studio_rects)
        if w.get("foreground") or overlaps:
            candidates.append(w)
    if consent:
        candidates = consent             # a consent prompt outranks the rest
    if not candidates or (consent_only and not consent):
        return None
    ranked = sorted(((_blocker_rank(c, _process_name(int(c["pid"]))), i, c)
                     for i, c in enumerate(candidates)), key=lambda t: t[:2])
    w = ranked[0][2]
    return _blocker_dict(w, _process_name(int(w["pid"])), "win32")


def blocking_window_for(auto, studio_pid: int | None) -> dict | None:
    """A foreign window blocking Studio, else None.

    Two sources, UIA first: the UIA foreground top-level window (skipped when
    `auto` is None), then visible Win32 top-level windows (foreground, or
    overlapping Studio's window), ranked #32770 dialog / PickerHost / consent /
    WindowsSecurity first. Returns {"title", "process", "pid", "class",
    "source": "uia"|"win32"}; process is the executable name, or the pid as
    text when the name cannot be read. A window owned by `studio_pid` or by
    any FTOptixStudio process is Studio's own and never counts; neither does
    one owned by the service's own process tree (service pid, its parents, the
    hosting conhost / WindowsTerminal), the service's console window and its
    root owner, or one titled with the service / python.exe path - those sets
    are computed once per call.
    """
    exclude = _own_process_tree_pids()
    own = _own_window_filter()
    # A known firewall / consent prompt wins over any other candidate, even a
    # UIA foreground window, and even when it is a zero-rect window.
    prompt = _win32_blocking_window(studio_pid, exclude, own, consent_only=True)
    if prompt:
        return prompt
    fg = _foreground_window(auto)
    if fg:
        pid = fg.get("pid")
        cache: dict[int, bool] = {}
        if not _is_own_window(fg, exclude, own) \
                and not _is_studio_pid(pid, studio_pid, cache):
            name = _process_name(pid) if pid else None
            return _blocker_dict(fg, name, "uia")
    return _win32_blocking_window(studio_pid, exclude, own)


def annotate_os_dialog(out: dict, auto, studio_pid: int | None) -> dict:
    """On an arm menu/click failure, explain a foreign blocking window.

    Adds probable_cause "os_dialog", blocking_window {title, process, pid,
    class, source[, kind]} and a hint naming the window and saying to dismiss it to the
    failure dict `out`. Leaves `out` untouched when no foreign window blocks
    Studio. Never raises.
    """
    try:
        blocker = blocking_window_for(auto, studio_pid)
    except Exception:
        blocker = None
    if blocker:
        out["probable_cause"] = "os_dialog"
        out["blocking_window"] = blocker
        out["hint"] = os_dialog_hint(blocker)
    return out


def execute_method(project: str, project_dir: str, method: str = "StartBridge",
                   node_name: str = "StudioMCPBridge",
                   base_port: int = _DEFAULT_BASE, port_range: int = _DEFAULT_RANGE,
                   timeout: float = 15.0, aliases: set[str] | None = None,
                   verify: str | None = None,
                   studio_pid: int | None = None,
                   live_chain: list[str] | None = None,
                   preferred_port: int | None = None,
                   name_collision: bool = False,
                   proven_pids: "set[int] | frozenset[int] | tuple" = ()) -> dict:
    """Right-click the bridge NetLogic (or an ancestor folder) in `project`'s
    Studio window and click `Execute <method>`.

    Walks the tree chain OUTERMOST first — folder ancestors from the directory
    layout, then the rows inside the bridge's own yaml — right-clicking each
    visible row for the menu item and clicking the chevron of any level whose
    child is not yet on screen. A fresh Studio opens with every top-level
    folder collapsed, and only the NetLogic category folder (not a plain
    folder above it) lists `Execute <method>` entries, so expansion down to
    that folder is required. Rows are left expanded afterwards.

    `aliases`: the other names this project's bridge may answer with (see
    serving_port_for / _studio_window_for).

    `studio_pid`: the Studio process that owns the project's window. Resolved
    from the window when omitted. Arm is idempotent PER STUDIO PID: a bridge
    counts as "already armed" only when its /bridge/health names this pid (or,
    when the pid cannot be resolved, this project name AND project_dir) - never
    because some other Studio's bridge happens to be up.

    `preferred_port`: the bridge registry's port for this project, probed
    first by serving_port_for.

    `name_collision`: another directory holds a project of the same name. A
    bridge health without project_dir, or a window title carrying only the
    name, is then unproven (`proven_pids` are the Studios proven by command
    line / launch record). `project_dir` is always the RESOLVED directory.

    `live_chain`: the node's project-tree rows (outermost first, leaf last)
    read from the LIVE model via an armed bridge. When given it IS the chain
    and the saved YAML is not consulted (a node created over the bridge need
    not be in any YAML yet, and the YAML can be stale). When None, the chain
    is derived from the saved YAML (`chain_source: "yaml"`).

    Returns a structured dict; never raises. `state` is one of already_armed /
    armed / stopped, or `error` names the failure.
    """
    # Everything cheap and environment-independent runs BEFORE the
    # uiautomation import: identity fast-exit, then the YAML lookup.
    # Fast exit BEFORE the uiautomation import: "is a bridge already serving
    # this project" is a pure loopback question. Answering it first means a box
    # without uiautomation still gets a useful already_armed/not_running
    # instead of a spurious capability error.
    # HOW success is confirmed. StartBridge/StopBridge are verified by the
    # bridge port appearing or disappearing. Any OTHER design-time method
    # (a generator's Build*, FormattedLinkScript's Apply, ...) produces no
    # such signal, so judging it by the port would report verify_timeout on a
    # perfectly successful run — pass verify="none" for those and confirm the
    # effect by render or describe_node instead.
    mode = verify or ("arm" if method == "StartBridge" else "stop")
    arming = mode == "arm"
    if mode != "none":
        if studio_pid is None:
            studio_pid = studio_pid_for(project, aliases, project_dir,
                                        name_collision, proven_pids)
        # The target project's registry port is probed FIRST (preferred=), so an
        # armed project answers after ONE health call whatever the number of
        # other armed Studios; only a miss walks the range, and that walk's
        # answers are kept (health_seen) for the checks below.
        health_seen: "dict[int, dict | None]" = {}
        already = serving_port_for(project, base_port, port_range, aliases,
                                   studio_pid=studio_pid, project_dir=project_dir,
                                   preferred=preferred_port,
                                   name_collision=name_collision,
                                   health_cache=health_seen)
        if arming and already:
            return {"ok": True, "state": "already_armed", "port": already,
                    "project": project, "studio_pid": studio_pid}
        if arming:
            # Every port held by ANOTHER project's bridge: the arm can only
            # fail to bind. Say so before any keystroke is sent.
            for _p in range(base_port, base_port + max(1, port_range)):
                if _p not in health_seen:
                    health_seen[_p] = _bridge_health(_p)
            full = port_range_full_result(
                dict(health_seen), project,
                {project.strip().lower()} | {a.strip().lower() for a in (aliases or ())},
                studio_pid, project_dir, name_collision)
            if full is not None:
                return full
        if not arming and already is None:
            return {"ok": True, "state": "not_running", "project": project}

    yaml_path: str | None = None
    names: list[str] = []
    if live_chain:
        chain_source = "bridge"
        for n in live_chain:
            if not names or names[-1] != n:
                names.append(n)
    else:
        chain_source = "yaml"
        yaml_path = find_bridge_yaml(project_dir, node_name)
        if yaml_path is None:
            # No YAML declares the node, so the project simply has no bridge
            # NetLogic -- a fresh `optix_project(action="new")` project is
            # exactly this. Distinguish it from "the row was not found in the
            # tree": the fix is to ADD the NetLogic, not to hunt the UI.
            return {"ok": False, "error": "bridge_netlogic_absent",
                    "project": project, "node": node_name,
                    "nudge": (f"{project!r} has no {node_name} NetLogic. Add it "
                              f"(studio-bridge/StudioMCPBridge.cs as a DesignTime "
                              f"NetLogic named {node_name}), then arm.")}
        chain = derive_chain(yaml_path, method)
        for n in folder_ancestors(yaml_path) + list(chain or [node_name]):
            if not names or names[-1] != n:
                names.append(n)

    try:
        import uiautomation as auto  # lazy: Windows-only, may be absent
    except Exception as e:  # pragma: no cover - import guard
        return {"ok": False, "error": "uiautomation_unavailable", "detail": str(e)}

    win = _studio_window_for(auto, project, aliases, project_dir,
                             ambiguous=name_collision, proven_pids=proven_pids)
    if win is not None and studio_pid:
        # The Studio whose bridge/pid was proven is the one to click in.
        wpid = int(getattr(win, "ProcessId", 0) or 0)
        if wpid != studio_pid:
            win, _err = _window_for_pid(auto, project, aliases, project_dir, studio_pid)
    if win is None:
        return window_missing_result(project, probe_elevation())

    if studio_pid is None:
        try:
            studio_pid = int(getattr(win, "ProcessId", 0) or 0) or None
        except Exception:
            studio_pid = None
    if mode != "none" and all(
            p in health_seen for p in range(base_port, base_port + port_range)):
        # Reuse the pre-check's probes of the whole range (no third scan).
        before = {p for p, h in health_seen.items() if h is not None}
    else:
        before = bound_ports(base_port, port_range)
    prev_cursor = auto.GetCursorPos()
    prev_fg = auto.GetForegroundControl()
    tried: list[dict] = []
    expanded: list[str] = []
    filtered = False
    want = f"Execute {method}"
    try:
        win.SetActive()
        time.sleep(0.4)
        target = None
        # Default: filter the Project-view tree to the leaf node and right-click
        # THAT row. Its own context menu is short and never clips; an ancestor
        # folder's aggregated menu can overrun the screen and render the Execute
        # entry off-screen, so a blind click on its rectangle misses (measured
        # 2026-09-07/14: 39-item folder menu ~1032px vs a 768px screen). The
        # chain walk below runs only as a fallback when this pass finds nothing
        # (e.g. no Search box present).
        leaf = names[-1]
        box = _search_box(win)
        if box is not None:
            _set_filter(auto, box, leaf)
            filtered = True
            expanded.append(f"filter:{leaf}")
            time.sleep(1.5)
            target = _leaf_pass(auto, win, names, want, tried, expanded)
        for i, name in enumerate(names):
            if target is not None:
                break
            rows = _candidate_rows(win, [name])
            if not rows:
                tried.append({"row": name, "visible": False})
                break
            for row in rows:
                r = row.BoundingRectangle
                rec = {"row": _strip_tags(row.Name), "x": r.left, "y": r.top}
                auto.RightClick(r.left + r.width() // 2, r.top + r.height() // 2)
                time.sleep(0.8)
                items = _open_menu_items(win)
                rec["menu_items"] = len(items)
                hit = [el for nm, el in items if nm.strip() == want]
                tried.append(rec)
                if hit:
                    target = hit[0]
                    break
                auto.SendKeys("{Esc}")
                time.sleep(0.3)
            if target is not None or i + 1 >= len(names):
                break
            # The next level is not on screen. First choice: filter the tree
            # on the leaf's name, which reveals the whole ancestor chain with
            # no geometry involved (fact 4). Fallback: the chevron of this
            # row — leftmost candidate is the tree's (the same text recurs in
            # the Properties pane).
            nxt = names[i + 1]
            if not _candidate_rows(win, [nxt]) and not filtered:
                box = _search_box(win)
                if box is not None:
                    _set_filter(auto, box, names[-1])
                    filtered = True
                    time.sleep(1.5)
                    expanded.append(f"filter:{names[-1]}")
            if not _candidate_rows(win, [nxt]):
                _click_chevron(auto, win, rows[0])
                time.sleep(0.8)
                expanded.append(name)
                if not _candidate_rows(win, [nxt]):
                    # A toggle on an already-open row collapses it; undo.
                    _click_chevron(auto, win, rows[0])
                    time.sleep(0.8)
        if target is None:
            return annotate_os_dialog(
                {"ok": False, "error": "menu_item_not_found", "wanted": want,
                 "tried": tried, "chain": names, "expanded": expanded,
                 "chain_source": chain_source, "yaml": yaml_path},
                auto, studio_pid)

        log_mark = _log_mark() if arming else None
        r = target.BoundingRectangle
        auto.Click(r.left + r.width() // 2, r.top + r.height() // 2)
        time.sleep(0.9)
        crep: dict = {}
        consented = _click_consent(auto, win, report=crep)
        dismissed = bool(crep.get("consent_dismissed", True))
        if consented and not dismissed:
            # The prompt survived the press: the Execute is still blocked.
            # Say so now rather than as a later verify_timeout.
            title = crep.get("dialog_title")
            return {"ok": False, "error": "consent_not_dismissed",
                    "dialog": title,
                    "detail": (f"pressed the consent button but the dialog "
                               f"{title!r} is still open"),
                    "consent_clicked": True, "consent_dismissed": False,
                    "tried": tried}

        if mode == "none":
            # The click is all there is to report. Deliberately NOT ok:false —
            # the gesture landed; whether the method DID the right thing is a
            # separate question the caller must answer by render/describe_node
            # (a mis-wired converter no-ops silently).
            return {"ok": True, "state": "executed", "project": project,
                    "node": node_name, "method": method,
                    "consent_clicked": consented, "consent_dismissed": dismissed,
                    "tried": tried, "chain": names, "expanded": expanded,
                    "chain_source": chain_source,
                    "nudge": (f"Execute {method!r} was clicked. This method has no "
                              "port signal to verify against — confirm the effect "
                              "with optix_describe_node or a render, not from this "
                              "result. Check the Studio Output pane for its log.")}

        def _poll(seconds: float) -> dict | None:
            deadline = time.time() + seconds
            while time.time() < deadline:
                if arming:
                    port = serving_port_for(project, base_port, port_range, aliases,
                                     studio_pid=studio_pid, project_dir=project_dir,
                                     preferred=preferred_port,
                                     name_collision=name_collision)
                    if port is None:
                        # The bridge logs its bind failure at once; do not wait
                        # out the timeout and blame whatever window is in front.
                        line = bind_failure_line(_log_since(log_mark))
                        if line is not None:
                            res = _range_full_payload(
                                probe_port_range(base_port, port_range),
                                project, detail=line)
                            res.update(consent_clicked=consented,
                                       consent_dismissed=dismissed, tried=tried)
                            return res
                    if port is not None:
                        return {"ok": True, "state": "armed", "port": port,
                                "project": project, "consent_clicked": consented,
                                "consent_dismissed": dismissed,
                                "new_ports": sorted(
                                    bound_ports(base_port, port_range) - before),
                                "tried": tried, "chain": names, "expanded": expanded}
                elif serving_port_for(project, base_port, port_range, aliases,
                                     studio_pid=studio_pid, project_dir=project_dir,
                                     preferred=preferred_port,
                                     name_collision=name_collision) is None:
                    return {"ok": True, "state": "stopped", "project": project,
                            "consent_clicked": consented,
                            "consent_dismissed": dismissed,
                            "tried": tried, "expanded": expanded}
                time.sleep(0.4)
            return None

        done = _poll(timeout)
        if done is not None:
            return done
        # Verification timed out. Studio may have raised the NetLogic security
        # warning as its own top-level window (not an in-scene popup, so
        # _click_consent never saw it): press Proceed before giving up.
        late = _clear_security_window(auto, win)
        if late is not None:
            done = _poll(min(timeout, 15.0))
            if done is not None:
                done.update(security_warning_cleared=True,
                            security_warning=late)
                return done
        out = {"ok": False, "error": "verify_timeout",
               "detail": (f"clicked {want!r} but no bridge is serving "
                          f"{project!r} after {timeout:g}s"),
               "before": sorted(before),
               "after": sorted(bound_ports(base_port, port_range)),
               "consent_clicked": consented, "consent_dismissed": dismissed,
               "tried": tried}
        if late is not None:
            out["security_warning"] = late
            out["detail"] += (f"; a security warning window ({late['title']!r}) "
                              f"was found and Proceed pressed, still not serving")
        return annotate_os_dialog(out, auto, studio_pid)
    except FocusError as e:
        return {"ok": False, "error": "focus_unconfirmed", "detail": str(e),
                "tried": tried}
    except Exception as e:  # pragma: no cover - UIA runtime failure
        return annotate_os_dialog(
            {"ok": False, "error": "uia_failed",
             "detail": f"{type(e).__name__}: {e}", "tried": tried},
            auto, studio_pid)
    finally:
        try:
            if filtered:
                try:
                    box = _search_box(win)
                    if box is not None:
                        _set_filter(auto, box, "")   # give the operator their tree back
                        time.sleep(0.3)
                except FocusError:
                    pass   # no blind keys; the filter text stays, cursor/fg still restore
            auto.SetCursorPos(*prev_cursor)
            if prev_fg:
                prev_fg.SetActive()
        except Exception:
            pass
