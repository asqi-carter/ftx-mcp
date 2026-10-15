"""Live per-window read of FT Optix Studio's selected deploy target via UI
Automation.

Windows-ONLY. The read walks the native Qt toolbar of a specific Studio window
(by PID) and returns the Name of the currently-selected deployment target — the
thing an F5 keystroke will actually run. It is a READ-ONLY, background-safe
pre-check (no foreground grab, no cursor, no keystroke); proven on Studio
1.7.1.46 to catch the stale-file divergence where Configuration.xml still claimed
Emulator while the live toolbar was on a hardware panel.

Off Windows (Linux host, CI) and on installs without the `uiautomation` package
or an interactive desktop, every entry point degrades to None. None is the
signal to the caller: "I cannot read the live selection — fall back to the
Configuration.xml advisory." The config-file path in core.py stays the fallback,
so behavior is unchanged wherever this read is unavailable.
"""
from __future__ import annotations

import re as _re


def read_selected_target_name(pid: int, target_names: set[str]) -> str | None:
    """Return the live selected deploy-target Name for Studio window `pid`.

    Proven per-window read (background-safe, no foreground/cursor): find the
    top-level window whose ProcessId == pid, then walk its descendants for the
    first ButtonControl whose ClassName starts with 'WindowToolbarButton' and
    whose Name is one of `target_names` and does not end with 'Output' (the
    status-bar toggles). The QMLTYPE_### suffix on the ClassName is volatile and
    is deliberately NOT keyed on.

    Returns the target name on success, or None on ANY failure — ImportError
    (uiautomation absent / off-Windows), no interactive desktop, no matching
    window, or a walk error. None means "fall back to the config file".
    """
    try:
        import uiautomation as auto  # lazy: Windows-only, may be absent

        win = None
        for w in auto.GetRootControl().GetChildren():
            try:
                if w.ProcessId == pid:
                    win = w
                    break
            except Exception:
                continue
        if win is None:
            return None

        found: list[str] = []

        def walk(c, depth: int, ctr: list[int]) -> None:
            if found or ctr[0] > 20000 or depth > 40:
                return
            ctr[0] += 1
            try:
                cls = getattr(c, "ClassName", "") or ""
                if (c.ControlTypeName == "ButtonControl"
                        and cls.startswith("WindowToolbarButton")):
                    nm = c.Name or ""
                    if nm in target_names and not nm.endswith("Output"):
                        found.append(nm)
                        return
            except Exception:
                pass
            try:
                kids = c.GetChildren()
            except Exception:
                kids = []
            for k in kids:
                try:
                    if k.ProcessId != pid:
                        continue
                except Exception:
                    pass
                walk(k, depth + 1, ctr)

        walk(win, 0, [0])
        return found[0] if found else None
    except Exception:
        return None


def _is_app_window(title: str) -> bool:
    """True for a caption that is Studio's own app identity, not a dialog.

    Matched SPACE-STRIPPED: the main project window is 'FactoryTalk Optix
    Studio', but undocked tool panes (a floating Properties panel, seen live on
    the VM 2026-07-24) are captioned with the bare executable name
    'FTOptixStudio'. Both collapse to 'optixstudio'; a real dialog ('Device
    access') keeps its own caption.
    """
    return not title or "optixstudio" in title.replace(" ", "").casefold()


_USER_LABEL = _re.compile(r"\b(user(?:name)?)\s*:\s*\S+", _re.IGNORECASE)


def _dialog_text(ctrl) -> str:
    """Static text exposed directly under a dialog node, account name redacted.

    Only LABELS and static text are read (`.Name`), never `ValuePattern`, so a
    field's typed contents — a password above all — are never harvested. But a
    credentials dialog renders its account as a static label, so a live capture
    read 'Please insert the access password for Device: Panel Username: admin'
    (VM, 2026-07-24). That string flows into run_emulator's result, and thus
    into model context and transcripts, from a tool sitting BELOW the deploy
    scope — the same disclosure this repo just closed in doctor's
    deploy_username row. The dialog's job here is naming the blocker, which
    'Device access' on 'Panel' does completely; the account name adds no
    diagnostic value, so it is dropped.
    """
    text = ""
    try:
        for t in ctrl.GetChildren():
            if (t.ControlTypeName in ("TextControl", "EditControl")
                    and (t.Name or "")):
                text += t.Name + " "
    except Exception:
        pass
    return _USER_LABEL.sub(r"\1: <redacted>", text).strip()


def pending_dialog(pid: int) -> list[dict]:
    """Blocking dialog(s) owned by Studio window `pid`, if any.

    Read-only, background-safe. Returns a list of {title, text} dicts (usually
    0 or 1) — e.g. the deploy/credentials prompt that eats an F5 keystroke and
    leaves the emulator never spawning. This gives the F5-not-serving diagnosis
    a concrete cause instead of "the service cannot see dialogs".

    Looks in TWO places, because Studio uses both:

      1. top-level windows owned by `pid` (a dialog that really is its own
         desktop window), and
      2. WindowControl children NESTED INSIDE those windows.

    (2) is the case that actually fires in practice. Studio is Qt/QML and draws
    its modals into the main window's scene graph, so they are NOT children of
    the desktop root. Confirmed live on the VM 2026-07-24: clicking Play with a
    hardware panel selected raised a 'Device access' password prompt that sat as
    a child of 'FactoryTalk Optix Studio' and was invisible to a desktop-root
    scan — the top-level-only version of this function returned [] with the
    prompt plainly on screen. Only DIRECT children are scanned; descending
    further starts picking up ordinary in-scene panels.

    Returns [] on ANY failure — off-Windows / uiautomation absent / no
    interactive desktop / no dialog. [] means "no visible blocking dialog (or I
    cannot see one)".
    """
    try:
        import uiautomation as auto  # lazy: Windows-only, may be absent

        hits: list[dict] = []
        for w in auto.GetRootControl().GetChildren():
            try:
                if w.ProcessId != pid or w.ControlTypeName != "WindowControl":
                    continue
                # (2) in-scene modals parented to this window
                try:
                    for d in w.GetChildren():
                        if (d.ControlTypeName == "WindowControl"
                                and not _is_app_window(d.Name or "")):
                            hits.append({"title": d.Name or "",
                                         "text": _dialog_text(d)})
                except Exception:
                    pass
                # (1) the window itself, when it is a distinct dialog
                if _is_app_window(w.Name or ""):
                    continue
                hits.append({"title": w.Name or "", "text": _dialog_text(w)})
            except Exception:
                continue
        return hits
    except Exception:
        return []


def _is_upgrade_modal(title: str, text: str) -> bool:
    """True when a dialog's title/text identifies a Studio project upgrade."""
    blob = f"{title} {text}".casefold()
    return "upgrade" in blob and "project" in blob


def find_upgrade_modal(auto) -> dict | None:
    """Return {title, text} of a Studio 'Project Upgrade' modal, or None.

    READ-ONLY: only names/static text are read; the dialog is never clicked
    (upgrading a project is one-way, so it stays an operator decision).
    Scans the same two places as `pending_dialog` (top-level dialog windows and
    WindowControl children nested in a Studio window), but is not keyed on a
    PID: right after `FTOptixStudio open` the launcher's PID is not necessarily
    the editor's. A match needs both 'upgrade' and 'project' in the dialog's
    title/text, so unrelated desktop windows do not trigger it.
    Returns None on ANY failure.
    """
    try:
        for w in auto.GetRootControl().GetChildren():
            try:
                if w.ControlTypeName != "WindowControl":
                    continue
                cands = []
                try:
                    cands = [d for d in w.GetChildren()
                             if d.ControlTypeName == "WindowControl"
                             and not _is_app_window(d.Name or "")]
                except Exception:
                    pass
                if not _is_app_window(w.Name or ""):
                    cands.append(w)
                for d in cands:
                    title, text = d.Name or "", _dialog_text(d)
                    if _is_upgrade_modal(title, text):
                        return {"title": title, "text": text}
            except Exception:
                continue
    except Exception:
        pass
    return None
