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

def _bridge_health(port: int, timeout: float = 3.0) -> dict | None:
    """GET /bridge/health, or None when nothing answers.

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


def bridge_project(port: int) -> str | None:
    h = _bridge_health(port)
    return None if h is None else h.get("project")


def bound_ports(base: int = _DEFAULT_BASE, span: int = _DEFAULT_RANGE) -> set[int]:
    """Ports in the range currently served by a bridge (health answered)."""
    return {p for p in range(base, base + span) if _bridge_health(p) is not None}


def serving_port_for(project: str, base: int = _DEFAULT_BASE,
                     span: int = _DEFAULT_RANGE,
                     aliases: set[str] | None = None) -> int | None:
    """Port whose bridge serves `project`, or None.

    This is the ONLY correct fast-exit/verify question once more than one
    bridge can be armed. "Is 8768 open?" answers yes for somebody ELSE's
    Studio, which would report success having armed nothing.

    `aliases` are the OTHER names the bridge may report for this project: it
    answers with Project.Current.BrowseName, which is the project NODE's name,
    not the directory's. They usually agree (Studio names the folder after the
    project) but not always — `Line4_HMI/Line4.optix` serves as "Line4", and a
    dir-name-only compare never matches it (measured 2026-08-23).
    """
    want = {project.strip().lower()} | {a.strip().lower() for a in (aliases or ())}
    for p in sorted(bound_ports(base, span)):
        if (bridge_project(p) or "").strip().lower() in want:
            return p
    return None


# ---- the gesture ------------------------------------------------------------

def _studio_window_for(auto, project: str, aliases: set[str] | None = None,
                       project_dir: str | Path | None = None):
    """Studio top-level window whose project tree identifies `project`.

    The Win32 caption is useless — every Studio window reports 'FactoryTalk
    Optix Studio' (the path shown in the title bar is Studio's in-scene QML
    chrome, not the window text). The project name and its full path ARE
    exposed as direct TextControl children, so identity resolves with NO bridge
    involved — which is what makes this usable at arm time, when by definition
    no bridge exists yet.

    Two identities are accepted, because each is absent in a real case:
      * the tree's ROOT row, compared against the project dir name AND its
        aliases (the .optix stem / root node name) — `Line4_HMI` opens with a
        root row reading `Line4`;
      * the in-scene PATH label (`C:\\...\\Line4_HMI` or `...\\Line4.optix`),
        matched by directory — the root row disappears while the Project-view
        search filter is active ("No results found" replaces the tree), and
        the label is what is left.
    """
    want = {project.strip().lower()} | {a.strip().lower() for a in (aliases or ())}
    pdir = str(project_dir).replace("/", "\\").rstrip("\\").lower() if project_dir else None
    for w in auto.GetRootControl().GetChildren():
        try:
            if w.ControlTypeName != "WindowControl":
                continue
            if "optixstudio" not in (w.Name or "").replace(" ", "").casefold():
                continue
            for c in w.GetChildren():
                if c.ControlTypeName != "TextControl":
                    continue
                # I34 (2026-09-04): Studio appends `*` to this text while the
                # project has unsaved changes -- the label measured that day read
                # `C:\Users\<u>\Desktop\NavArgDemo*`. An intolerant compare
                # answered studio_window_not_found for every bridge-edited
                # project (unsaved by definition until Ctrl+S), which reads
                # exactly like "Studio is not open". Strip the marker off BOTH
                # ends -- leading in case Studio ever moves it -- for the root-row
                # name match AND the path-prefix match below.
                n = _norm(c.Name).strip("*").strip()
                if n in want:
                    return w
                if pdir and (n.replace("/", "\\") == pdir
                             or n.replace("/", "\\").startswith(pdir + "\\")):
                    return w
        except Exception:
            continue
    return None


def _candidate_rows(win, names: list[str]) -> list:
    """Visible tree rows matching any of `names`, leftmost first.

    Leftmost-first means the OUTERMOST folder is tried before the leaf: it is
    the row most likely already on screen, and (fact 1) its menu carries the
    child NetLogic's Execute entries anyway.
    """
    wanted = {n.lower() for n in names}
    out = []
    for c in win.GetChildren():
        try:
            if c.ControlTypeName != "TextControl" or _norm(c.Name) not in wanted:
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
        for c in win.GetChildren():
            try:
                if c.ControlTypeName != "TextControl":
                    continue
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


def _set_filter(auto, box, text: str) -> None:
    """Replace the Search box content. uiautomation key syntax is {Ctrl}a —
    a ^a is typed LITERALLY (measured: it landed '^aStudioMCPBridge^a')."""
    r = box.BoundingRectangle
    auto.Click(r.left + r.width() // 2, r.top + r.height() // 2)
    time.sleep(0.3)
    auto.SendKeys("{Ctrl}a{Delete}")
    if text:
        time.sleep(0.2)
        auto.SendKeys(text)


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


def _click_consent(auto, win, attempts: int = 12) -> bool:
    """Tick 'do not show again', then click Studio's security prompt.

    Scans ONLY inside in-scene popups. Never the whole window: a stray 'Yes' or
    'OK' elsewhere in the UI must never be clicked.

    The checkbox is ticked BEFORE consenting because clicking Proceed destroys
    the dialog and the box with it. Studio raises this warning once per
    TRIGGER, not once per project, so without the tick the same modal blocks
    the arm, then the first F5, then the next one (verified 2026-09-01: arm
    reported consent_clicked, and F5 immediately hit the identical dialog).
    Ticking it makes the dismissal stick for the life of the project.

    NOTE the whole popup is walked before anything is clicked — the button is
    frequently reached before the checkbox in traversal order, and consenting
    early would dismiss the dialog with the box still unticked.
    """
    for _ in range(attempts):
        try:
            for d in win.GetChildren():
                if d.ControlTypeName not in ("WindowControl", "PaneControl"):
                    continue
                if "optixstudio" in (d.Name or "").replace(" ", "").casefold():
                    continue
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
                if button is None:
                    continue
                if suppress is not None:
                    _tick_suppress(auto, suppress)
                    time.sleep(0.15)
                r = button.BoundingRectangle
                auto.Click(r.left + r.width() // 2, r.top + r.height() // 2)
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def clear_consent(project: str, project_dir: str | Path | None = None,
                  aliases: set[str] | None = None,
                  attempts: int = 8) -> dict:
    """Answer Studio's NetLogic security warning for `project`, if it is up.

    Exists because the emulator's F5 raises the SAME modal the arm path already
    handles, and the modal EATS the keystroke until answered. Clicking Proceed
    lets the ALREADY-SENT F5 continue, so the caller must NOT resend it — F5
    toggles, and a resend would stop the emulator it just started.

    Foreground window and cursor are restored the way execute_method restores
    them, so clearing a dialog mid-wait doesn't permanently steal focus.
    """
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
        return {"ok": True, "consent_clicked": _click_consent(auto, win, attempts)}
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


def execute_method(project: str, project_dir: str, method: str = "StartBridge",
                   node_name: str = "StudioMCPBridge",
                   base_port: int = _DEFAULT_BASE, port_range: int = _DEFAULT_RANGE,
                   timeout: float = 15.0, aliases: set[str] | None = None,
                   verify: str | None = None) -> dict:
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
        already = serving_port_for(project, base_port, port_range, aliases)
        if arming and already:
            return {"ok": True, "state": "already_armed", "port": already,
                    "project": project}
        if not arming and already is None:
            return {"ok": True, "state": "not_running", "project": project}

    yaml_path = find_bridge_yaml(project_dir, node_name)
    if yaml_path is None:
        # No YAML declares the node, so the project simply has no bridge
        # NetLogic -- a fresh `optix_project(action="new")` project is exactly
        # this. Distinguish it from "the row was not found in the tree": the
        # fix is to ADD the NetLogic, not to hunt the UI.
        return {"ok": False, "error": "bridge_netlogic_absent",
                "project": project, "node": node_name,
                "nudge": (f"{project!r} has no {node_name} NetLogic. Add it "
                          f"(studio-bridge/StudioMCPBridge.cs as a DesignTime "
                          f"NetLogic named {node_name}), then arm.")}
    chain = derive_chain(yaml_path, method)
    names: list[str] = []
    for n in folder_ancestors(yaml_path) + list(chain or [node_name]):
        if not names or names[-1] != n:
            names.append(n)

    try:
        import uiautomation as auto  # lazy: Windows-only, may be absent
    except Exception as e:  # pragma: no cover - import guard
        return {"ok": False, "error": "uiautomation_unavailable", "detail": str(e)}

    win = _studio_window_for(auto, project, aliases, project_dir)
    if win is None:
        return {"ok": False, "error": "studio_window_not_found", "project": project,
                "nudge": (f"open {project!r} in Studio (optix_project "
                          f"action='open'), then retry")}

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
            for row in _candidate_rows(win, [leaf]):
                r = row.BoundingRectangle
                rec = {"row": _strip_tags(row.Name), "x": r.left, "y": r.top,
                       "leaf": True}
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
            return {"ok": False, "error": "menu_item_not_found", "wanted": want,
                    "tried": tried, "chain": names, "expanded": expanded,
                    "yaml": yaml_path}

        r = target.BoundingRectangle
        auto.Click(r.left + r.width() // 2, r.top + r.height() // 2)
        time.sleep(0.9)
        consented = _click_consent(auto, win)

        if mode == "none":
            # The click is all there is to report. Deliberately NOT ok:false —
            # the gesture landed; whether the method DID the right thing is a
            # separate question the caller must answer by render/describe_node
            # (a mis-wired converter no-ops silently).
            return {"ok": True, "state": "executed", "project": project,
                    "node": node_name, "method": method,
                    "consent_clicked": consented, "tried": tried,
                    "chain": names, "expanded": expanded,
                    "nudge": (f"Execute {method!r} was clicked. This method has no "
                              "port signal to verify against — confirm the effect "
                              "with optix_describe_node or a render, not from this "
                              "result. Check the Studio Output pane for its log.")}

        deadline = time.time() + timeout
        while time.time() < deadline:
            if arming:
                port = serving_port_for(project, base_port, port_range, aliases)
                if port is not None:
                    return {"ok": True, "state": "armed", "port": port,
                            "project": project, "consent_clicked": consented,
                            "new_ports": sorted(
                                bound_ports(base_port, port_range) - before),
                            "tried": tried, "chain": names, "expanded": expanded}
            elif serving_port_for(project, base_port, port_range, aliases) is None:
                return {"ok": True, "state": "stopped", "project": project,
                        "consent_clicked": consented, "tried": tried,
                        "expanded": expanded}
            time.sleep(0.4)
        return {"ok": False, "error": "verify_timeout",
                "detail": (f"clicked {want!r} but no bridge is serving "
                           f"{project!r} after {timeout:g}s"),
                "before": sorted(before),
                "after": sorted(bound_ports(base_port, port_range)),
                "consent_clicked": consented, "tried": tried}
    except Exception as e:  # pragma: no cover - UIA runtime failure
        return {"ok": False, "error": "uia_failed", "detail": f"{type(e).__name__}: {e}",
                "tried": tried}
    finally:
        try:
            if filtered:
                box = _search_box(win)
                if box is not None:
                    _set_filter(auto, box, "")   # give the operator their tree back
                    time.sleep(0.3)
            auto.SetCursorPos(*prev_cursor)
            if prev_fg:
                prev_fg.SetActive()
        except Exception:
            pass
