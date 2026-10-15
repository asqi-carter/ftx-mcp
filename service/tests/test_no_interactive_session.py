"""no_interactive_session guard on tools that must reach the desktop (PRD 0051, #78).

With ``_is_interactive_session`` False (service in session 0) project_open,
bridge_arm, save, run_emulator, restart_emulator and the native capture /
click refuse up front; with True they behave as before.
"""
from __future__ import annotations

import pytest

from service import core
from service.tests.conftest import make_project

CODE = "no_interactive_session"


def _calls(cfg):
    """(name, zero-arg callable) for every guarded tool."""
    return {
        "open": lambda: core.project_open(cfg, "Alpha", wait_seconds=0),
        "new": lambda: core.project_new(cfg, "Fresh", wait_seconds=0),
        "execute_method": lambda: core.execute_design_method(
            cfg, "Alpha", node="Gen", method="Build"),
        "arm": lambda: core.bridge_arm(cfg, "Alpha"),
        "save": lambda: core.save(cfg, "Alpha", timeout=0.1),
        "run": lambda: core.run_emulator(cfg, "Alpha", wait_ready=False),
        "restart": lambda: core.restart_emulator(cfg, "Alpha"),
        "native_capture": lambda: core.native_screenshot_runtime(
            cfg, project="Alpha", pids_fn=lambda p: [], windows_fn=lambda p: []),
        "native_click": lambda: core.native_interact(
            cfg, "click", x=10, y=10, project="Alpha",
            pids_fn=lambda p: [], windows_fn=lambda p: []),
    }


@pytest.fixture
def alpha(projects_root):
    return make_project(projects_root, "Alpha")


@pytest.fixture
def forbid_side_effects(monkeypatch):
    """A refused call must not audit, launch or probe anything."""
    def boom(*a, **k):
        raise AssertionError("side effect before the interactive guard")
    monkeypatch.setattr(core, "audit", boom)
    monkeypatch.setattr(core, "_studio_launch", boom)
    monkeypatch.setattr(core, "emulator_status", boom)


@pytest.mark.parametrize("name", [
    "open", "new", "arm", "save", "run", "restart", "execute_method",
    "native_capture", "native_click"])
def test_tool_refuses_in_session_zero(cfg, alpha, monkeypatch, forbid_side_effects, name):
    monkeypatch.setattr(core, "_is_interactive_session", lambda: False)
    out = _calls(cfg)[name]()
    assert out["ok"] is False
    assert out["error"] == CODE
    assert out["hint"] == (
        "the service is running in session 0 (started before logon or by a "
        "non-interactive task); start it from the logged-on session: "
        "bootstrap\\services.ps1 start, or enable-autostart (runs at logon)")


def test_native_refusals_keep_failed_state(cfg, monkeypatch):
    monkeypatch.setattr(core, "_is_interactive_session", lambda: False)
    calls = _calls(cfg)
    assert calls["native_capture"]()["state"] == "failed"
    assert calls["native_click"]()["state"] == "failed"


@pytest.mark.parametrize("probe", [True, None])
def test_native_unchanged_when_interactive(cfg, monkeypatch, probe):
    monkeypatch.setattr(core, "_is_interactive_session", lambda: probe)
    calls = _calls(cfg)
    for key in ("native_capture", "native_click"):
        out = calls[key]()
        assert out.get("error") != CODE
        assert out["error"] == "native_runtime_not_found"


@pytest.mark.parametrize("name", ["open", "arm", "save", "run", "restart"])
def test_gui_tools_unchanged_when_interactive(cfg, alpha, monkeypatch, name):
    """With True the guard is a no-op: the call proceeds into its normal path
    (whatever that returns or raises offline), never the refusal."""
    monkeypatch.setattr(core, "_is_interactive_session", lambda: True)
    monkeypatch.setattr(core, "_studio_launch",
                        lambda *a, **k: {"ok": False, "error": "launch_failed"})
    try:
        out = _calls(cfg)[name]()
    except Exception:
        return  # proceeded past the guard into real work
    if isinstance(out, dict):
        assert out.get("error") != CODE


def test_unknown_session_is_not_a_refusal(monkeypatch):
    monkeypatch.setattr(core, "_is_interactive_session", lambda: None)
    assert core._require_interactive_session() is None
    monkeypatch.setattr(core, "_is_interactive_session", lambda: True)
    assert core._require_interactive_session() is None


def test_doctor_marks_interactive_row_required(cfg, monkeypatch):
    monkeypatch.setattr(core, "_is_interactive_session", lambda: False)
    out = core.doctor(cfg)
    row = next(c for c in out["checks"] if c["name"] == "interactive_session")
    assert row["required"] is True
    assert row["ok"] is False
    assert {"project_open", "bridge_arm", "save", "run_emulator",
            "restart_emulator"} <= set(row["required_for"])
    assert out["ready"] is False


def test_doctor_interactive_row_ok_when_interactive(cfg, monkeypatch):
    monkeypatch.setattr(core, "_is_interactive_session", lambda: True)
    row = next(c for c in core.doctor(cfg)["checks"] if c["name"] == "interactive_session")
    assert row["required"] is True and row["ok"] is True


def test_setup_ps1_names_status_doctor():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "bootstrap" / "setup.ps1").read_text(
        encoding="utf-8")
    assert "optix_doctor" not in text
    assert text.count("optix_status action=doctor") >= 2


def test_project_new_refuses_in_session_zero(cfg, monkeypatch, forbid_side_effects):
    """`optix_project new` from a session-0 service launches nothing."""
    monkeypatch.setattr(core, "_is_interactive_session", lambda: False)
    out = core.project_new(cfg, "Fresh", wait_seconds=0)
    assert out["ok"] is False and out["error"] == CODE
    assert not (cfg.projects_root / "Fresh").exists()


def test_project_new_via_mcp_tool_refuses_in_session_zero(cfg, monkeypatch, forbid_side_effects):
    from service.mcp_app import make_mcp
    monkeypatch.setattr(core, "_is_interactive_session", lambda: False)
    tool = make_mcp(cfg)._tool_manager._tools["optix_project"]
    out = tool._ftx_sync_fn(action="new", project="Fresh")
    assert out["error"] == CODE


def test_guard_constant_names_exist_and_are_unique():
    names = core.INTERACTIVE_SESSION_TOOLS
    assert len(names) == len(set(names))
    for n in names:
        assert callable(getattr(core, n)), n


# ---- registry walk -------------------------------------------------------

# Primitives that start Studio or drive its window: the launch helper, the
# SendKeys script builder, studio_arm's UI gestures and the native click.
_LAUNCH_PRIMITIVES = {
    "_studio_launch", "_build_save_ps", "execute_method",
    "focus_project_tree", "post_click",
}


def _call_names(node) -> set[str]:
    import ast
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _launching_core_functions() -> set[str]:
    """Core functions that reach a launch primitive, directly or through other
    functions that do not guard themselves. A caller of an already-guarded
    function (e.g. ``deploy_updatesvc`` -> ``save``) is covered by the callee's
    own refusal, so the walk stops at the constant's entries."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(core))
    calls = {n.name: _call_names(n) for n in tree.body
             if isinstance(n, ast.FunctionDef)}
    guarded = set(core.INTERACTIVE_SESSION_TOOLS)
    reach = {name for name, c in calls.items() if c & _LAUNCH_PRIMITIVES}
    reach -= _LAUNCH_PRIMITIVES
    changed = True
    while changed:
        changed = False
        for name, c in calls.items():
            if name not in reach and c & (reach - guarded):
                reach.add(name)
                changed = True
    return reach


def _core_functions_called_by_tool(tool) -> set[str]:
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(getattr(tool, "_ftx_sync_fn", tool.fn)))
    return {n.attr for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "core"}


def test_every_launching_mcp_tool_is_in_guard_constant(cfg):
    """Walk the MCP registry: a tool whose body calls a core function that
    reaches `_studio_launch` or the keystroke helpers must be in
    INTERACTIVE_SESSION_TOOLS (and so refuse in session 0)."""
    from service.mcp_app import make_mcp
    launching = _launching_core_functions()
    # the walker itself must see the known launchers
    assert {"project_open", "project_new", "save", "bridge_arm",
            "execute_design_method", "native_interact"} <= launching
    tools = make_mcp(cfg)._tool_manager._tools
    reached: dict[str, set[str]] = {}
    called: set[str] = set()
    for name, tool in tools.items():
        names = _core_functions_called_by_tool(tool)
        called |= names
        hit = names & launching
        if hit:
            reached[name] = hit
    guarded = set(core.INTERACTIVE_SESSION_TOOLS)
    missing = {t: sorted(h - guarded) for t, h in reached.items() if h - guarded}
    assert not missing, f"MCP tools reach Studio launch/keystrokes unguarded: {missing}"
    # no stale entry: every guarded function is called by some registered tool
    assert guarded <= called, sorted(guarded - called)
