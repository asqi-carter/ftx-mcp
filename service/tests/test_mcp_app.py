"""FastMCP smoke tests — tool registration, contract, basic invocation.

These run the MCP layer in-process against the same `core.Config` as the
HTTP tests; they do NOT exercise the streamable-http transport (uvicorn
binding is covered by main.py's port-conflict check). The goal is to pin
two things:

1. Every tool the SPEC promises is registered under its documented name.
2. Each tool's docstring carries the "Use this when:" / "Do NOT use this
   when:" guidance — that text is a shipped UX surface per
   SPEC §MCP tool surface.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from service import core
from service.mcp_app import make_mcp
from service.tests.conftest import make_project

EXPECTED_TOOLS = {
    "optix_active_target",
    # cold-start pair (v1.0.7): arm the bridge and open/create a project with
    # no human at the keyboard.
    "optix_bridge_arm",
    "optix_project",
    # (1.0.8): optix_bridge_arm generalised to any design-time [ExportMethod].
    "optix_execute_method",
    "optix_status",  # consolidated optix_health/_doctor/_services_status/_studio_version
    "optix_build_check",
    "optix_list_projects",
    "optix_list_skills",
    "optix_get_skill",
    "optix_find",
    "optix_read_file",
    "optix_deploy",
    "optix_list_screens",
    "optix_get_project_map",
    "optix_bridge_status",
    "optix_describe_node",
    "optix_list_ui_types",
    "optix_describe_type",
    "optix_schema",  # U17: consolidated optix_schema_dump/_list/_diff
    "optix_bridge_edit",
    "optix_bridge_add_label",
    "optix_bridge_add_bound_widget",
    "optix_bridge_add_navigation_panel_item",
    "optix_bridge_ensure_web_engine",

    "optix_bridge_convert_to_type",
    "optix_bridge_validate_expression",
    # generic ExecuteMethod invoke, no optix_bridge_edit op verb
    # equivalent -- never gated (see the FTXMCP_BRIDGE_PRIMITIVES comment).
    "optix_bridge_invoke_method",
    # The 14 per-noun bridge primitives (set_property/bind_property/
    # attach_expression/wire_event/delete_node/move_node/reorder/
    # create_variable/create_folder/create_object/create_type/create_alias/
    # create_widget/add_translation) are gated OFF by default behind
    # FTXMCP_BRIDGE_PRIMITIVES=1 -- each is 1:1 with an optix_bridge_edit op
    # verb, so the DEFAULT surface no longer registers them. See
    # test_bridge_primitives_present_when_gate_on /
    # test_bridge_primitives_absent_by_default below.
    "optix_save",
    "optix_emulator",  # consolidated run/restart/stop/status/log
    "optix_bridge_log_tail",
    "optix_deploy_updatesvc",
    "optix_add_widget",
    "optix_add_model_variable",
    "optix_set_property",
    "optix_deploy_preflight",
    "optix_runtime_start",
    "optix_runtime_stop",
    "optix_runtime_status",
    "optix_routes",  # U17: consolidated optix_routes_save/_get/_list
    "optix_cdp_sweep",
    "optix_cdp_restart",
    # U14 consolidation: the DEFAULT surface is consolidated-only. The 10
    # optix_cdp_* aliases (screenshot/ocr/read_text/find_text/diff/click/
    # fill/type/key/navigate) are OFF by default and only registered when
    # FTXMCP_LEGACY_TOOLS=1. optix_cdp_sweep / optix_cdp_restart are NOT
    # aliases and stay always-registered.
    "optix_observe",
    "optix_interact",
    "optix_read_netlogic",
}

# The 14 per-noun bridge primitives, gated behind FTXMCP_BRIDGE_PRIMITIVES=1
# (see the mcp_app.py gate block near the FTXMCP_SKILLS pop). Kept as a
# separate set so EXPECTED_TOOLS reflects the actual default (gate-off)
# surface while these two sets stay easy to reconcile against each other.
_BRIDGE_PRIMITIVE_TOOLS = {
    "optix_bridge_set_property",
    "optix_bridge_bind_property",
    "optix_bridge_attach_expression",
    "optix_bridge_attach_formatter",
    "optix_bridge_wire_event",
    "optix_bridge_delete_node",
    "optix_bridge_move_node",
    "optix_bridge_reorder",
    "optix_bridge_create_variable",
    "optix_bridge_create_folder",
    "optix_bridge_create_object",
    "optix_bridge_create_netlogic",
    "optix_bridge_create_type",
    "optix_bridge_create_alias",
    "optix_bridge_create_widget",
    "optix_bridge_add_translation",
}


def _list_tools(mcp) -> list:
    return mcp._tool_manager.list_tools()


def _tool_fn(tool):
    """Directly-callable fn for a tool: offloaded (async-wrapped) tools keep
    their original sync fn at _ftx_sync_fn; fast tools are tool.fn as-is."""
    return getattr(tool, "_ftx_sync_fn", tool.fn)


def test_mcp_registers_every_spec_tool(cfg: core.Config) -> None:
    mcp = make_mcp(cfg)
    names = {t.name for t in _list_tools(mcp)}
    missing = EXPECTED_TOOLS - names
    extra = names - EXPECTED_TOOLS
    assert not missing, f"missing MCP tools: {missing}"
    assert not extra, f"unexpected MCP tools (update EXPECTED_TOOLS or SPEC): {extra}"


def test_skill_tools_present_by_default(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.delenv("FTXMCP_SKILLS", raising=False)
    names = {t.name for t in _list_tools(make_mcp(cfg))}
    assert "optix_list_skills" in names and "optix_get_skill" in names


def test_ftxmcp_skills_0_drops_skill_tools(cfg: core.Config, monkeypatch) -> None:
    """FTXMCP_SKILLS=0 removes the skill catalog tools entirely (the self-
    evident-tools-only A/B surface) while leaving authoring tools intact."""
    monkeypatch.setenv("FTXMCP_SKILLS", "0")
    names = {t.name for t in _list_tools(make_mcp(cfg))}
    assert "optix_list_skills" not in names
    assert "optix_get_skill" not in names
    assert "optix_bridge_edit" in names  # authoring surface unaffected


def test_bridge_primitives_absent_by_default(cfg: core.Config, monkeypatch) -> None:
    """The 14 per-noun bridge primitives are gated OFF by default (opposite
    polarity from FTXMCP_SKILLS): each is 1:1 with an optix_bridge_edit op
    verb, so the default surface omits them. optix_bridge_edit and the
    always-on composite/gated-exempt tools stay registered."""
    monkeypatch.delenv("FTXMCP_BRIDGE_PRIMITIVES", raising=False)
    names = {t.name for t in _list_tools(make_mcp(cfg))}
    for n in _BRIDGE_PRIMITIVE_TOOLS:
        assert n not in names, f"{n} should be gated off by default"
    assert "optix_bridge_edit" in names
    assert "optix_bridge_add_bound_widget" in names
    assert "optix_bridge_convert_to_type" in names


def test_bridge_primitives_present_when_gate_on(cfg: core.Config, monkeypatch) -> None:
    """FTXMCP_BRIDGE_PRIMITIVES=1 is the opt-in escape hatch that restores all
    14 per-noun bridge primitives, alongside the always-on optix_bridge_edit."""
    monkeypatch.setenv("FTXMCP_BRIDGE_PRIMITIVES", "1")
    names = {t.name for t in _list_tools(make_mcp(cfg))}
    for n in _BRIDGE_PRIMITIVE_TOOLS:
        assert n in names, f"{n} should be present when the gate is on"
    assert "optix_bridge_edit" in names


def test_mcp_tool_descriptions_carry_use_when_guidance(cfg: core.Config) -> None:
    """Each tool docstring must include the 'Use this when' / 'Do NOT use'
    framing — it is a shipped UX surface for LLM-side MCP clients."""
    mcp = make_mcp(cfg)
    failures: list[str] = []
    for tool in _list_tools(mcp):
        desc = tool.description or ""
        if "Use this when" not in desc:
            failures.append(f"{tool.name}: missing 'Use this when'")
        if "Do NOT use this when" not in desc:
            failures.append(f"{tool.name}: missing 'Do NOT use this when'")
    assert not failures, "tool docstring contract violations:\n  " + "\n  ".join(failures)


def test_mcp_tools_carry_readonly_destructive_annotations(cfg: core.Config) -> None:
    """Every tool declares MCP annotations so clients can auto-run reads and gate
    writes/destructive ops. Reads -> readOnlyHint True; writes -> readOnlyHint
    False, destructiveHint False; destructive -> readOnlyHint False,
    destructiveHint True."""
    READ = {"optix_find","optix_list_projects",
            "optix_list_screens","optix_read_file","optix_describe_node",
            "optix_describe_type","optix_list_ui_types","optix_bridge_status",
            "optix_runtime_status",
            "optix_deploy_preflight","optix_cdp_screenshot","optix_cdp_ocr",
            "optix_cdp_read_text","optix_cdp_find_text","optix_cdp_diff",
            "optix_bridge_validate_expression",
            "optix_get_project_map", "optix_list_skills", "optix_get_skill",
            "optix_schema",  # U17: consolidated, all 3 actions were read-only
            "optix_status",  # consolidated health/doctor/services/version — all read-only
            "optix_active_target",
            # U14 consolidated read-side capture
            "optix_observe",
            # compiles to a throwaway copy / tails a diagnostic log — neither mutates
            "optix_build_check", "optix_bridge_log_tail",
            "optix_bridge_log_tail",
            "optix_read_netlogic"}
    DESTRUCTIVE = {"optix_deploy","optix_deploy_updatesvc","optix_bridge_delete_node",
                   "optix_runtime_stop","optix_cdp_click","optix_cdp_type",
                   "optix_cdp_key","optix_cdp_fill","optix_cdp_navigate",
                   "optix_cdp_sweep",
                   # U14 consolidated action-side driver
                   "optix_interact",
                   # replace=true deletes the original instance after the move
                   "optix_bridge_convert_to_type",
                   # re-author move deletes the original after the copy
                   "optix_bridge_move_node",
                   # U16 batch: a batch may carry a delete op, so the batch tool
                   # inherits the most destructive thing it can dispatch
                   "optix_bridge_edit",
                   # runs arbitrary NetLogic method code; impact
                   # is whatever the method does, not knowable generically, so
                   # it's flagged destructive rather than a plain write.
                   "optix_bridge_invoke_method"}
    mcp = make_mcp(cfg)
    for tool in _list_tools(mcp):
        ann = tool.annotations
        assert ann is not None, f"{tool.name}: no annotations"
        if tool.name in READ:
            assert ann.readOnlyHint is True, f"{tool.name} should be readOnly"
        elif tool.name in DESTRUCTIVE:
            assert ann.readOnlyHint is False and ann.destructiveHint is True, \
                f"{tool.name} should be destructive"
        else:  # write
            assert ann.readOnlyHint is False and ann.destructiveHint is False, \
                f"{tool.name} should be a non-destructive write"


def test_mcp_bridge_tool_returns_structured_nudge_on_failure(
    cfg: core.Config, monkeypatch
) -> None:
    """A bridge write that raises must reach the model as a structured, nudging
    dict (via classify_bridge_failure), never a raw exception."""
    monkeypatch.setenv("FTXMCP_BRIDGE_PRIMITIVES", "1")
    def _raise(*a, **k):
        raise core.BridgeUnavailable("bridge unreachable")
    monkeypatch.setattr(core, "bridge_set_property", _raise)
    monkeypatch.setattr(core, "classify_bridge_failure", lambda cfg, project, exc: {
        "state": "failed", "reason_code": "bridge_unreachable_studio_closed",
        "nudge": "Open the project in Studio and run StartBridge."})
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_bridge_set_property")
    out = _tool_fn(tool)(project="Alpha", node_path="UI/MainWindow/L1", name="Text", value="hi")
    assert out["state"] == "failed"
    assert out["reason_code"] == "bridge_unreachable_studio_closed"
    assert "StartBridge" in out["nudge"]


def test_mcp_bridge_attach_formatter_roundtrip(
    cfg: core.Config, projects_root, monkeypatch
) -> None:
    """MCP-surface roundtrip: optix_bridge_attach_formatter (gated by
    FTXMCP_BRIDGE_PRIMITIVES=1) calls core.bridge_attach_formatter with the
    right arguments and returns ok:true to the caller.

    Confirms the full path from MCP tool invocation → core function →
    structured response — the same roundtrip the bridge HTTP layer validates
    end-to-end in test_bridge_writes.py but exercised here through the
    registered MCP tool surface.
    """
    monkeypatch.setenv("FTXMCP_BRIDGE_PRIMITIVES", "1")
    make_project(projects_root, "Alpha")
    seen: dict = {}

    def fake_attach(cfg_, project, node_path, prop_name, format,
                    sources=None, mode=None):
        seen.update(project=project, node_path=node_path,
                    prop_name=prop_name, format=format,
                    sources=sources, mode=mode)
        return {"ok": True, "via": "attach-formatter",
                "node": node_path, "prop": prop_name}

    monkeypatch.setattr(core, "bridge_attach_formatter", fake_attach)
    monkeypatch.setattr(core, "default_project", lambda c: "Alpha")
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_bridge_attach_formatter")
    out = _tool_fn(tool)(
        node_path="UI/MainWindow/L1",
        prop_name="Text",
        format="../NavPanel{#navIdx}@NodeId",
        sources="navIdx=Model/GlobalVariables/AlarmTab",
        project="Alpha",
    )
    assert out["ok"] is True
    assert seen["node_path"] == "UI/MainWindow/L1"
    assert seen["prop_name"] == "Text"
    assert seen["format"] == "../NavPanel{#navIdx}@NodeId"
    assert seen["sources"] == "navIdx=Model/GlobalVariables/AlarmTab"


def test_mcp_bridge_attach_formatter_node_attribute_refused(
    cfg: core.Config, projects_root, monkeypatch
) -> None:
    """Rejection path: passing a node attribute name as prop_name is refused
    before any HTTP dispatch, and the MCP surface returns a structured error
    (never a raw exception) with a clear reason_code so the caller knows what
    to fix.
    """
    monkeypatch.setenv("FTXMCP_BRIDGE_PRIMITIVES", "1")
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "default_project", lambda c: "Alpha")
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_bridge_attach_formatter")
    # DisplayName is a node attribute — the pre-dispatch guard must fire.
    out = _tool_fn(tool)(
        node_path="UI/MainWindow/L1",
        prop_name="DisplayName",
        format="../NavPanel{#navIdx}@NodeId",
        project="Alpha",
    )
    # _bridge_guarded catches BridgeWriteFailed and returns a structured dict.
    assert out.get("reason_code") == "write_failed"
    assert "node_attribute_not_settable" in out.get("detail", "")


def test_mcp_health_tool_returns_expected_keys(cfg: core.Config) -> None:
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_status")
    out = _tool_fn(tool)(action="health")
    for key in (
        "projects_root",
        "studio_exe",
        "runtime_dir",
        "interactive_session",
        "bind",
    ):
        assert key in out, f"health() missing {key!r}: {out}"


def test_mcp_list_projects_tool_returns_known_project(
    cfg: core.Config, projects_root: Path
) -> None:
    make_project(projects_root, "Alpha")
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_list_projects")
    out = _tool_fn(tool)()
    assert "projects" in out
    names = [p["name"] for p in out["projects"]]
    assert "Alpha" in names


def test_mcp_deploy_preflight_tool_returns_envelope(
    cfg: core.Config, projects_root: Path
) -> None:
    make_project(projects_root, "Alpha")
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_deploy_preflight")
    out = _tool_fn(tool)(project="Alpha")
    for key in ("ready", "blockers", "warnings", "checks"):
        assert key in out, f"preflight envelope missing {key!r}: {out}"


def test_shellout_tools_are_offloaded_async(cfg: core.Config) -> None:
    """Tools that do BLOCKING I/O are async-wrapped so they run OFF the shared
    event loop. A sync tool fn runs directly on the loop (FastMCP Tool.run), so a
    blocking call would stall the loop and drop the MCP streamable-http transport
    (the observed 120s emulator_status hang / bridge-drop under bursts).

    This covers BOTH shell-outs (subprocess/CDP) AND the bridge read tools: a
    bridge read does blocking HTTP, so "read-only" does NOT mean "stays sync" --
    keeping the bridge reads on the loop was the original drop bug. Only a small
    allowlist of provably fast, pure-local tools stays sync (no needless thread
    hop, and unit tests can call their .fn directly)."""
    mcp = make_mcp(cfg)
    by_name = {t.name: t for t in _list_tools(mcp)}
    # Under the _STAY_SYNC denylist everything offloads except provably fast,
    # pure-local tools. "read-only" is NOT "non-blocking": the bridge READ tools
    # do blocking HTTP and must offload too (that was the bridge-drop bug).
    # Only tools present in the DEFAULT (consolidated-only) surface are asserted.
    for n in ("optix_emulator", "optix_status",
              "optix_save", "optix_cdp_sweep", "optix_cdp_restart",
              "optix_observe", "optix_interact", "optix_build_check",
              "optix_describe_node", "optix_get_project_map",
              "optix_bridge_add_bound_widget", "optix_routes"):
        assert by_name[n].is_async is True, f"{n} must be offloaded (async)"
    # only provably fast, pure-local tools stay on the loop
    for n in ("optix_list_projects",):
        assert by_name[n].is_async is False, f"{n} should stay sync (pure-local)"


def test_mcp_call_tool_path_invokes_health(cfg: core.Config) -> None:
    """Exercise the FastMCP `call_tool` async path so we know the
    registered tool surface is wired through the manager, not just
    available via direct `.fn` access."""
    mcp = make_mcp(cfg)

    async def _invoke():
        return await mcp.call_tool("optix_status", {"action": "health"})

    result = asyncio.run(_invoke())
    # `call_tool` returns either a list of ContentBlock (no output_schema)
    # or a tuple (unstructured, structured) when output_schema is set.
    if isinstance(result, tuple):
        _, structured = result
        assert isinstance(structured, dict)
        assert "runtime_dir" in structured
    else:
        # Unstructured content list — at least one block, and serialized
        # JSON should mention a known field.
        assert result, "call_tool returned empty content"
        text = "".join(getattr(b, "text", "") for b in result)
        assert "runtime_dir" in text


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS))
def test_mcp_each_tool_has_nonempty_description(
    cfg: core.Config, tool_name: str
) -> None:
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == tool_name)
    assert tool.description and len(tool.description.strip()) > 50, (
        f"{tool_name} description is too short to be useful: "
        f"{(tool.description or '')[:80]!r}"
    )


# ---- default-project fallback (v1.1 backlog 1.5) -----------------------------

def test_project_scoped_tool_defaults_to_bridge_project(cfg: core.Config, monkeypatch) -> None:
    """Omitting `project` resolves to the bridge's served project."""
    seen = {}
    monkeypatch.setattr(core, "default_project", lambda c: "BridgeProj")
    monkeypatch.setattr(core, "list_screens", lambda c, p: seen.setdefault("project", p) or {"screens": [], "count": 0})
    mcp = make_mcp(cfg)

    async def _invoke():
        return await mcp.call_tool("optix_list_screens", {})

    asyncio.run(_invoke())
    assert seen["project"] == "BridgeProj"


def test_project_scoped_tool_explicit_project_wins(cfg: core.Config, monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(core, "default_project", lambda c: "BridgeProj")
    monkeypatch.setattr(core, "list_screens", lambda c, p: seen.setdefault("project", p) or {"screens": [], "count": 0})
    mcp = make_mcp(cfg)

    async def _invoke():
        return await mcp.call_tool("optix_list_screens", {"project": "Other"})

    asyncio.run(_invoke())
    assert seen["project"] == "Other"


def test_project_scoped_tool_no_project_no_bridge_errors(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "default_project", lambda c: None)
    mcp = make_mcp(cfg)

    async def _invoke():
        return await mcp.call_tool("optix_list_screens", {})

    result = asyncio.run(_invoke())
    if isinstance(result, tuple):
        _, structured = result
        assert structured.get("error") == "no_project"
    else:
        text = "".join(getattr(b, "text", "") for b in result)
        assert "no_project" in text


DEPLOY_FAMILY = {"optix_deploy", "optix_deploy_updatesvc", "optix_deploy_preflight",
                 "optix_runtime_start", "optix_runtime_stop", "optix_runtime_status",
                 "optix_add_widget", "optix_add_model_variable", "optix_set_property"}


def test_deploy_family_hidden_by_default(cfg: core.Config) -> None:
    """FTX_ENABLE_DEPLOY defaults off: the deploy/runtime family (and the
    file-edit authoring that feeds it) stays out of the catalog."""
    import dataclasses
    lean = dataclasses.replace(cfg, enable_deploy=False)
    names = {t.name for t in _list_tools(make_mcp(lean))}
    assert not (names & DEPLOY_FAMILY), names & DEPLOY_FAMILY
    # the emulator-first surface is intact
    for keep in ("optix_emulator", "optix_bridge_add_bound_widget",
                 "optix_observe", "optix_get_project_map"):
        assert keep in names


def test_deploy_family_present_when_enabled(cfg: core.Config) -> None:
    names = {t.name for t in _list_tools(make_mcp(cfg))}  # cfg fixture: enabled
    assert DEPLOY_FAMILY <= names


def test_server_ships_instructions(cfg: core.Config) -> None:
    """The MCP instructions field is the always-visible orientation — it must
    exist, stay short, and point at the skill tools."""
    mcp = make_mcp(cfg)
    ins = mcp._mcp_server.instructions or ""
    assert "optix_list_skills" in ins and "optix_emulator" in ins
    assert len(ins) < 1200, "instructions must stay lean — they cost every session"
    # The always-visible orientation must never name a tool absent from the
    # DEFAULT surface: retired-with-no-alias tools, or aliases gated OFF by
    # default (FTXMCP_LEGACY_TOOLS). Regression guard for the v1.0.4 fix where
    # the block told every session to verify with optix_cdp_screenshot (gated
    # off) — a self-evident-surface break. Every tool named here resolves on a
    # stock install.
    default_tools = set(mcp._tool_manager._tools)
    for absent in ("optix_cdp_screenshot", "optix_cdp_click", "optix_run_emulator",
                   "optix_restart_emulator", "optix_health", "optix_doctor"):
        assert absent not in ins, f"instructions name a non-default tool: {absent}"
    for present in ("optix_get_project_map", "optix_observe", "optix_emulator",
                    "optix_status", "optix_routes"):
        assert present in default_tools, f"instructions cite {present} but it is not registered"


def test_cdp_screenshot_default_returns_dict_with_hint(
    cfg: core.Config, monkeypatch, tmp_path
) -> None:
    """Default (return_image=False) keeps the verified-safe path-only shape,
    now with a hint field telling the model what to do with the path."""
    shot = tmp_path / "shot.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xdbfakejpeg")

    def fake_capture(cfg_, save_path=None, **kw):
        return {"state": "succeeded", "path": str(shot), "b64": None,
                "size_bytes": 8, "navigated": False, "captured_at": "t"}

    monkeypatch.setattr(core, "cdp_screenshot_runtime", fake_capture)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_observe")
    out = _tool_fn(tool)(mode="screenshot", save_path=str(shot))
    assert isinstance(out, dict)
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert "hint" in out and "file tool" in out["hint"]


def test_cdp_screenshot_return_image_yields_typed_image_content(
    cfg: core.Config, monkeypatch, tmp_path
) -> None:
    """return_image=true returns [json-metadata, Image] — TYPED MCP image
    content, never b64 stuffed into the JSON text (the shape that stalled
    Cowork's visualize; see tool docstring)."""
    import json as _json

    from mcp.server.fastmcp import Image as McpImage

    shot = tmp_path / "shot.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xdbfakejpeg")

    def fake_capture(cfg_, save_path=None, **kw):
        return {"state": "succeeded", "path": str(shot), "b64": None,
                "size_bytes": 8, "navigated": False, "captured_at": "t"}

    monkeypatch.setattr(core, "cdp_screenshot_runtime", fake_capture)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_observe")
    out = _tool_fn(tool)(mode="screenshot", save_path=str(shot), return_image=True)
    assert isinstance(out, list) and len(out) == 2
    meta = _json.loads(out[0])
    assert meta["state"] == "succeeded" and meta["path"] == str(shot)
    assert isinstance(out[1], McpImage)
    # b64 must not ride in the JSON text block
    assert "b64" not in out[0] or _json.loads(out[0]).get("b64") in (None,)


def test_cdp_screenshot_return_image_failure_stays_dict(
    cfg: core.Config, monkeypatch
) -> None:
    """A failed capture with return_image=true returns the plain error dict —
    no image block, no crash on a missing file."""
    def fake_capture(cfg_, save_path=None, **kw):
        return {"state": "failed", "path": None, "b64": None, "size_bytes": 0,
                "navigated": False, "captured_at": "t", "error": "boom"}

    monkeypatch.setattr(core, "cdp_screenshot_runtime", fake_capture)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_observe")
    out = _tool_fn(tool)(mode="screenshot", return_image=True)
    assert isinstance(out, dict)
    assert out["state"] == "failed"


# ---- region param (S4 feature 1) + read_text / find_text tools (S4 2, 3) ----

def test_cdp_screenshot_region_forwarded_to_core(
    cfg: core.Config, monkeypatch, tmp_path
) -> None:
    shot = tmp_path / "shot.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xdbfakejpeg")
    seen = {}

    def fake_capture(cfg_, save_path=None, region=None, **kw):
        seen["region"] = region
        return {"state": "succeeded", "path": str(shot), "b64": None,
                "size_bytes": 8, "navigated": False, "captured_at": "t",
                "region": [10.0, 10.0, 20.0, 20.0]}

    monkeypatch.setattr(core, "cdp_screenshot_runtime", fake_capture)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_observe")
    out = _tool_fn(tool)(mode="screenshot", save_path=str(shot), region=[0.1, 0.1, 0.2, 0.2])
    assert seen["region"] == [0.1, 0.1, 0.2, 0.2]
    assert out["region"] == [10.0, 10.0, 20.0, 20.0]


def test_cdp_screenshot_region_composes_with_return_image(
    cfg: core.Config, monkeypatch, tmp_path
) -> None:
    """region and return_image are independent params — the typed-image
    response still carries the resolved `region` in its JSON metadata block."""
    import json as _json

    from mcp.server.fastmcp import Image as McpImage

    shot = tmp_path / "shot.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xdbfakejpeg")

    def fake_capture(cfg_, save_path=None, region=None, **kw):
        return {"state": "succeeded", "path": str(shot), "b64": None,
                "size_bytes": 8, "navigated": False, "captured_at": "t",
                "region": [5.0, 5.0, 15.0, 15.0]}

    monkeypatch.setattr(core, "cdp_screenshot_runtime", fake_capture)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_observe")
    out = _tool_fn(tool)(mode="screenshot", save_path=str(shot), region=[0.0, 0.0, 0.1, 0.1], return_image=True)
    assert isinstance(out, list) and len(out) == 2
    meta = _json.loads(out[0])
    assert meta["state"] == "succeeded" and meta["region"] == [5.0, 5.0, 15.0, 15.0]
    assert isinstance(out[1], McpImage)


def test_cdp_screenshot_bad_region_returns_dict_not_raise(
    cfg: core.Config, monkeypatch
) -> None:
    def fake_capture(cfg_, save_path=None, region=None, **kw):
        return {"state": "failed", "path": None, "b64": None, "size_bytes": 0,
                "navigated": False, "captured_at": "t", "error": "bad_region",
                "detail": "region must be [x, y, w, h]", "region": region}

    monkeypatch.setattr(core, "cdp_screenshot_runtime", fake_capture)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_observe")
    out = _tool_fn(tool)(mode="screenshot", region=[1, 2, 3])
    assert isinstance(out, dict)
    assert out["state"] == "failed" and out["error"] == "bad_region"


def test_cdp_read_text_tool_registered_and_forwards_to_core(
    cfg: core.Config, monkeypatch
) -> None:
    seen = {}

    def fake_read_text(cfg_, region=None, navigate_url=None, settle_seconds=None,
                       psm=6, project=None):
        seen.update(region=region, psm=psm)
        return {"state": "succeeded", "text": "SP-101", "region": region,
                "size_bytes": 10, "navigated": False, "captured_at": "t"}

    monkeypatch.setattr(core, "cdp_read_text_runtime", fake_read_text)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_observe")
    out = _tool_fn(tool)(mode="read_text", region=[0.0, 0.0, 0.5, 0.5], psm=7)
    assert out["text"] == "SP-101"
    assert seen == {"region": [0.0, 0.0, 0.5, 0.5], "psm": 7}


def test_cdp_read_text_tool_degrades_on_missing_tesseract(
    cfg: core.Config, monkeypatch
) -> None:
    monkeypatch.setattr(core, "cdp_read_text_runtime", lambda *a, **k: {
        "state": "failed", "text": None, "error": "tesseract_not_installed",
        "hint": "install tesseract"})
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_observe")
    out = _tool_fn(tool)(mode="read_text")
    assert out["state"] == "failed" and out["error"] == "tesseract_not_installed"


def test_cdp_find_text_tool_registered_and_forwards_to_core(
    cfg: core.Config, monkeypatch
) -> None:
    seen = {}

    def fake_find_text(cfg_, text, navigate_url=None, settle_seconds=None,
                       project=None):
        seen["text"] = text
        return {"state": "succeeded", "found": True, "matches": [
            {"text": "Start", "confidence": 0.95, "bbox_px": [1, 2, 3, 4],
             "bbox_norm": [0.1, 0.2, 0.3, 0.4], "center_px": [2.5, 4.0]}],
            "viewport": {"w": 1000, "h": 800}}

    monkeypatch.setattr(core, "cdp_find_text_runtime", fake_find_text)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_observe")
    out = _tool_fn(tool)(mode="find_text", text="Start")
    assert out["found"] is True and seen["text"] == "Start"
    assert out["matches"][0]["center_px"] == [2.5, 4.0]


def test_cdp_find_text_tool_no_match_is_not_an_error(
    cfg: core.Config, monkeypatch
) -> None:
    monkeypatch.setattr(core, "cdp_find_text_runtime", lambda cfg_, text, **k: {
        "state": "succeeded", "found": False, "matches": [],
        "viewport": {"w": 1000, "h": 800}})
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_observe")
    out = _tool_fn(tool)(mode="find_text", text="Nonexistent")
    assert out["state"] == "succeeded" and out["found"] is False


# ---- optix_routes(action="save"|"get"|"list") (S7, consolidated U17) ----
#
# Motivation: a field test needed to CREATE a routes file server-side and
# had no tool for it, so the model reached for host folder access. These
# tests pin the tool-layer forwarding contract; core.py's test_cdp.py tests
# cover the save->navigate round-trip and validation behavior. U17 folded
# the formerly-separate optix_routes_save/_get/_list tools into one
# action-discriminated optix_routes tool (clean replace, no aliases).

def test_routes_save_tool_registered_and_forwards_to_core(
    cfg: core.Config, monkeypatch
) -> None:
    seen = {}

    def fake_save(cfg_, project, routes, name="ftx_ui_map"):
        seen.update(project=project, routes=routes, name=name)
        return {"state": "succeeded", "path": "/p/dev/ftx_ui_map.json",
                "routes": ["home"], "bytes": 42}

    monkeypatch.setattr(core, "routes_save", fake_save)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_routes")
    out = _tool_fn(tool)(action="save", project="Alpha",
                         routes={"home": {"steps": [{"click": [0, 0]}]}})
    assert out["state"] == "succeeded" and out["path"] == "/p/dev/ftx_ui_map.json"
    assert seen == {"project": "Alpha",
                    "routes": {"home": {"steps": [{"click": [0, 0]}]}},
                    "name": "ftx_ui_map"}


def test_routes_save_tool_custom_name_forwarded(cfg: core.Config, monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(core, "routes_save", lambda cfg_, project, routes, name="ftx_ui_map": (
        seen.update(name=name) or {"state": "succeeded", "path": "p", "routes": [], "bytes": 2}))
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_routes")
    _tool_fn(tool)(action="save", project="Alpha", routes={}, name="custom")
    assert seen["name"] == "custom"


def test_routes_save_tool_surfaces_bad_name_as_dict(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "routes_save", lambda cfg_, project, routes, name="ftx_ui_map": {
        "state": "failed", "error": "bad_name", "name": name})
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_routes")
    out = _tool_fn(tool)(action="save", project="Alpha", routes={}, name="../escape")
    assert out["state"] == "failed" and out["error"] == "bad_name"


def test_routes_save_tool_missing_routes_is_structured_error(
    cfg: core.Config,
) -> None:
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_routes")
    out = _tool_fn(tool)(action="save", project="Alpha")
    assert out["error"] == "missing_param"


def test_routes_get_tool_registered_and_forwards_to_core(
    cfg: core.Config, monkeypatch
) -> None:
    seen = {}

    def fake_get(cfg_, project, name="ftx_ui_map"):
        seen.update(project=project, name=name)
        return {"state": "succeeded", "path": "/p/dev/ftx_ui_map.json",
                "routes": {"version": 1, "routes": {"home": {"steps": []}}}}

    monkeypatch.setattr(core, "routes_get", fake_get)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_routes")
    out = _tool_fn(tool)(action="get", project="Alpha")
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["routes"]["routes"]["home"] == {"steps": []}
    assert seen == {"project": "Alpha", "name": "ftx_ui_map"}


def test_routes_get_tool_not_found_surfaces_as_dict(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "routes_get", lambda cfg_, project, name="ftx_ui_map": {
        "state": "failed", "error": "routes_file_not_found", "path": "/p/dev/missing.json"})
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_routes")
    out = _tool_fn(tool)(action="get", project="Alpha", name="missing")
    assert out["state"] == "failed" and out["error"] == "routes_file_not_found"


def test_routes_list_tool_registered_and_forwards_to_core(
    cfg: core.Config, monkeypatch
) -> None:
    seen = {}

    def fake_list(cfg_, project):
        seen["project"] = project
        return {"state": "succeeded", "files": [
            {"name": "one", "path": "/p/dev/one.json", "routes": ["home"], "mtime": "t"}],
            "count": 1, "skipped": 1}

    monkeypatch.setattr(core, "routes_list", fake_list)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_routes")
    out = _tool_fn(tool)(action="list", project="Alpha")
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["count"] == 1 and out["skipped"] == 1
    assert seen == {"project": "Alpha"}


def test_routes_tool_bad_action_is_structured_error(cfg: core.Config) -> None:
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_routes")
    out = _tool_fn(tool)(action="delete", project="Alpha")
    assert out["error"] == "bad_action"
    assert "valid_actions" in out


# ---- optix_cdp_sweep / optix_cdp_diff (S6) -------------------------------

def test_cdp_sweep_tool_registered_and_forwards_to_core(
    cfg: core.Config, monkeypatch
) -> None:
    seen = {}

    def fake_sweep(cfg_, routes_path=None, out_dir=None, routes=None, warmup=True, **k):
        seen.update(routes_path=routes_path, out_dir=out_dir, routes=routes, warmup=warmup)
        return {"state": "succeeded", "version": 1, "created_at": "t",
                "viewport": {"w": 100, "h": 100}, "ocr": False, "screens": {}}

    monkeypatch.setattr(core, "cdp_sweep_runtime", fake_sweep)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_cdp_sweep")
    out = _tool_fn(tool)(routes_path="dev/routes.json", out_dir="dev/shots",
                  routes=["home"], warmup=False)
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert seen == {"routes_path": "dev/routes.json", "out_dir": "dev/shots",
                    "routes": ["home"], "warmup": False}


def test_cdp_sweep_tool_reports_partial_errors(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "cdp_sweep_runtime", lambda cfg_, **k: {
        "state": "succeeded", "version": 1, "created_at": "t",
        "viewport": {"w": 100, "h": 100}, "ocr": False,
        "screens": {"a": {"error": "boom"}}, "errors": 1})
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_cdp_sweep")
    out = _tool_fn(tool)(routes_path="r.json", out_dir="out")
    assert out["state"] == "succeeded" and out["errors"] == 1


def test_cdp_diff_tool_registered_and_forwards_to_core(
    cfg: core.Config, monkeypatch
) -> None:
    seen = {}

    def fake_diff(dir_a, dir_b, threshold=2.0):
        seen.update(dir_a=dir_a, dir_b=dir_b, threshold=threshold)
        return {"state": "succeeded", "threshold": threshold, "screens": {},
                "added": [], "removed": [],
                "summary": {"same": 0, "changed": 0, "size_mismatch": 0, "errors": 0}}

    monkeypatch.setattr(core, "cdp_diff_runtime", fake_diff)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_observe")
    out = _tool_fn(tool)(mode="diff", dir_a="dev/before", dir_b="dev/after", threshold=5.0)
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert seen == {"dir_a": "dev/before", "dir_b": "dev/after", "threshold": 5.0}


def test_cdp_diff_tool_manifest_not_found_surfaces_as_dict(
    cfg: core.Config, monkeypatch
) -> None:
    monkeypatch.setattr(core, "cdp_diff_runtime", lambda dir_a, dir_b, threshold=2.0: {
        "state": "failed", "error": "manifest_not_found", "dir": dir_a})
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_observe")
    out = _tool_fn(tool)(mode="diff", dir_a="missing", dir_b="also_missing")
    assert out["state"] == "failed" and out["error"] == "manifest_not_found"


# ---- @_with_project decorator: schema/name/doc preservation (U7) -------------
#
# The 35 project-scoped tools share a single `@_with_project` decorator that
# resolves `project` (explicit arg else bridge default), short-circuiting with
# the `no_project` envelope. The decorator uses functools.wraps so FastMCP's
# introspection (name via __name__, description via __doc__, input schema via
# inspect.signature unwrapping __wrapped__) still sees the ORIGINAL tool fn.
# These tests pin that the schema — including the `project` field and every
# other param — survives the wrap; a signature-injection variant that fails to
# unwrap __wrapped__ would drop params here.


def test_with_project_preserves_name_doc_and_schema(cfg: core.Config) -> None:
    """A decorated tool keeps its name, docstring, and `project` schema field.

    Closes the gap left by the registration/description tests: nothing else
    pins `tool.parameters` — a wrapper that lost __wrapped__ would strip
    `project` from the JSON schema and FastMCP would reject `{"project": ...}`
    at call_tool time."""
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_list_screens")
    assert tool.name == "optix_list_screens"
    assert "Use this when" in (tool.description or "")
    props = tool.parameters["properties"]
    assert "project" in props, "decorator dropped the `project` schema field"
    # optional str | None = None -> nullable with a null default
    assert props["project"].get("default", "MISSING") is None


def test_with_project_preserves_full_schema_for_multiarg_tool(cfg: core.Config) -> None:
    """Every non-project param of a many-arg decorated tool survives the wrap.

    optix_find takes query/glob/max_results/context_lines/case_sensitive plus
    project; all must remain in the introspected schema (guards against a wrap
    that fails to follow __wrapped__ and exposes only (*args, **kwargs))."""
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_find")
    props = tool.parameters["properties"]
    for name in ("query", "glob", "max_results", "context_lines",
                 "case_sensitive", "project"):
        assert name in props, f"optix_find lost `{name}` from its schema"


def test_with_project_tool_count_and_annotations_unchanged(cfg: core.Config) -> None:
    """The mechanical pass is behavior-preserving: the shared
    _RO/_RW/_RW_DESTRUCTIVE constants carry the same hint values the per-site
    ToolAnnotations did (spot-check one of each class). Tool count grows as
    new tools land (69: 68 after the U15 schema tools + optix_active_target;
    71 after the U14 consolidated optix_observe + optix_interact land alongside
    the 12 optix_cdp_* aliases; 72 after U16's optix_bridge_edit). The default
    surface is now consolidated-only: the 10 deprecated optix_cdp_* aliases are
    off unless FTXMCP_LEGACY_TOOLS=1, so 72 - 10 = 62 by default. U17 folds the
    3-tool optix_schema_dump/_list/_diff family into optix_schema and the
    3-tool optix_routes_save/_get/_list family into optix_routes (clean
    replace, no aliases): 62 - 4 = 58. Second consolidation pass: the 4-tool
    optix_health/_doctor/_services_status/_studio_version family folds into
    optix_status (58 - 3 = 55); the 5-tool optix_run_emulator/_restart_emulator/
    _stop_emulator/_emulator_status/_runtime_log_tail family folds into
    optix_emulator (55 - 4 = 51); the 14 per-noun bridge primitives are gated
    OFF by default behind FTXMCP_BRIDGE_PRIMITIVES (51 - 14 = 37).
    (v1.0.6): optix_bridge_invoke_method adds one, never gated (37 + 1 = 38).
    (v1.0.7): the cold-start pair optix_bridge_arm (consolidated arm/stop)
    and optix_project (consolidated open/new) add two more (40 + 2 = 42).
    (v1.0.8): optix_execute_method (right-click -> Execute any design-time
    [ExportMethod]) adds one, never gated (42 + 1 = 43).
    (1.0.8): optix_read_netlogic adds one, never gated (43 + 1 = 44)."""
    mcp = make_mcp(cfg)
    by_name = {t.name: t for t in _list_tools(mcp)}
    assert len(by_name) == 44
    assert by_name["optix_list_screens"].annotations.readOnlyHint is True
    write = by_name["optix_bridge_add_bound_widget"].annotations
    assert write.readOnlyHint is False and write.destructiveHint is False
    destr = by_name["optix_bridge_edit"].annotations
    assert destr.readOnlyHint is False and destr.destructiveHint is True


# (tool name, backing core fn, extra required kwargs) — each body calls
# core.<fn>(cfg, project, ...), so the resolved project is the 2nd positional.
_UNIFORM_CASES = [
    ("optix_describe_node", "describe_node", {"path": "UI/Screen1"}),
    ("optix_list_screens", "list_screens", {}),
    ("optix_bridge_set_property", "bridge_set_property",
     {"node_path": "UI/MainWindow/L1", "name": "Text", "value": "hi"}),
]


@pytest.mark.parametrize("tool_name,core_fn,extra", _UNIFORM_CASES,
                         ids=[c[0] for c in _UNIFORM_CASES])
def test_with_project_resolution_is_uniform(cfg: core.Config, monkeypatch,
                                            tool_name, core_fn, extra) -> None:
    """The decorator behaves identically across decorated tools: an omitted
    `project` resolves to the bridge default and reaches core; a None default
    short-circuits with the `no_project` envelope (core never called).

    optix_bridge_set_property is gated OFF by default (FTXMCP_BRIDGE_PRIMITIVES)
    but @_with_project's behavior is identical regardless of gate state, so the
    gate is enabled here to keep this case registered and exercised."""
    monkeypatch.setenv("FTXMCP_BRIDGE_PRIMITIVES", "1")
    seen = {}
    monkeypatch.setattr(core, "default_project", lambda c: "BridgeProj")
    monkeypatch.setattr(core, core_fn,
                        lambda c, p, *a, **k: seen.setdefault("project", p) or {"ok": True})
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == tool_name)
    # default resolution reaches core with the bridge project
    _tool_fn(tool)(**extra)
    assert seen["project"] == "BridgeProj"
    # no bridge project -> no_project envelope, core NOT called
    seen.clear()
    monkeypatch.setattr(core, "default_project", lambda c: None)
    out = _tool_fn(tool)(**extra)
    assert out.get("error") == "no_project"
    assert "project" not in seen


# ---- optix_execute_method MCP-surface acceptance tests ------
#
# optix_execute_method is the MCP surface for right-click -> Execute ANY
# design-time [ExportMethod].  Unlike optix_bridge_arm (which hard-wires
# StartBridge/StopBridge and verifies via the bridge port), this tool always
# passes verify="none" and returns state="executed" once the click lands.
# These tests pin the two observable MCP-level behaviours that do NOT require
# a live Studio: the bad_args guard on empty node/method, and the hand-off to
# core.execute_design_method with the right arguments.


def test_execute_method_tool_rejects_empty_node_or_method(
        cfg: core.Config) -> None:
    """The MCP layer rejects calls where `node` or `method` is empty before
    reaching core — a missing node name or method name has no sensible default
    and must fail fast with a `bad_args` error rather than a confusing UIA
    'row not found' deep in studio_arm."""
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_execute_method")
    fn = _tool_fn(tool)
    for bad in (
        {"project": "P", "node": "", "method": "Apply"},
        {"project": "P", "node": "FormattedLinkScript", "method": ""},
    ):
        out = fn(**bad)
        assert out.get("ok") is False and out.get("error") == "bad_args", (
            f"expected bad_args for {bad}, got {out!r}"
        )


def test_execute_method_tool_delegates_to_core(
        cfg: core.Config, monkeypatch, tmp_path) -> None:
    """optix_execute_method calls core.execute_design_method with the
    project, node, and method supplied by the caller — it does NOT call
    bridge_arm or any other core function."""
    make_project(cfg.projects_root, "MyProj")
    seen: dict = {}

    def _fake_execute_design_method(c, project, node, method, **kw):
        seen.update(project=project, node=node, method=method)
        return {"ok": True, "state": "executed", "nudge": "check Studio Output"}

    monkeypatch.setattr(core, "execute_design_method", _fake_execute_design_method)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_execute_method")
    out = _tool_fn(tool)(
        project="MyProj", node="FormattedLinkScript", method="Apply")
    assert out["ok"] and out["state"] == "executed", out
    assert seen == {"project": "MyProj", "node": "FormattedLinkScript",
                    "method": "Apply"}, (
        f"core.execute_design_method was not called with the right args: {seen!r}"
    )


# ---- optix_bridge_edit MCP-surface tests -------------------
#
# Six regression pins that lock the observable MCP surface of optix_bridge_edit
# so architectural refactors (e.g. changing def->async def, leaking ctx into
# the JSON schema, widening the 'apply'-only bad_ops guard to status) are
# caught as test failures before they reach the service.

def test_bridge_edit_schema_excludes_ctx(cfg: core.Config) -> None:
    """ctx is a FastMCP injection — it must NOT appear in the JSON schema
    that MCP clients see.  Regression guard: if the signature ever loses its
    Context type annotation FastMCP will expose ctx as a callable parameter,
    confusing every LLM-side client.

    The eight documented user-facing parameters must ALL be present."""
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_bridge_edit")
    props = tool.parameters.get("properties", {})
    for expected in ("action", "batch_id", "chunk_seconds", "chunk_ops",
                     "ops", "dry_run", "strict", "project"):
        assert expected in props, (
            f"optix_bridge_edit schema is missing {expected!r}; "
            f"present keys: {list(props)}"
        )
    assert "ctx" not in props, (
        "optix_bridge_edit schema must not expose `ctx` — it is a FastMCP "
        "runtime injection, not a callable parameter"
    )


def test_bridge_edit_is_sync_then_offloaded(cfg: core.Config) -> None:
    """optix_bridge_edit must be a sync def that the post-registration offload
    pass wrapped async.  Regression guard against anyone changing it to
    `async def`: FastMCP runs async tool fns directly on the event loop, so a
    blocking bridge HTTP call inside an async def would stall the loop and
    reproduce the bridge-drop bug the offload pass was introduced to fix.

    Two invariants:
      * is_async is True  — the tool IS async after wrapping
      * _ftx_sync_fn set — the ORIGINAL sync fn is reachable (tests use it to
        call the tool without an event loop)
    """
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_bridge_edit")
    assert tool.is_async is True, (
        "optix_bridge_edit must be offloaded (is_async=True after make_mcp)"
    )
    assert hasattr(tool, "_ftx_sync_fn") and tool._ftx_sync_fn is not None, (
        "optix_bridge_edit must expose _ftx_sync_fn — the sync-wrap offload "
        "path was NOT applied, which means the tool was registered as async def "
        "and the blocking bridge HTTP will stall the event loop"
    )


def test_bridge_edit_direct_call_ctx_none_handles_missing_event_loop(
    cfg: core.Config, monkeypatch, projects_root: Path
) -> None:
    """_tool_fn(tool)(..., ctx=None) applies a 2-op batch to completion.

    The on_progress callback wraps ctx.report_progress inside
    anyio.from_thread.run; with ctx=None that call raises AttributeError.
    core.bridge_edit's defensive try/except swallows the callback failure and
    the apply loop continues — the batch still returns state='succeeded'.

    This proves two things:
      1. _tool_fn() correctly returns the original sync fn (_ftx_sync_fn), not
         the async wrapper, so the call works without an event loop.
      2. The callback defensive wrap actually handles the missing event loop /
         None ctx rather than crashing the tool call.
    """
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(
        core, "bridge_validate_ops",
        lambda cfg_, project, ops, strict=False: {
            "ok": True, "errors": [], "warnings": [],
            "op_count": len(ops), "strict": False,
        },
    )
    monkeypatch.setattr(core, "_apply_one_edit", lambda cfg_, project, op: {})
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_bridge_edit")
    ops = [
        {"op": "set_property", "path": "UI/Foo/Bar", "name": "Width",  "value": "100"},
        {"op": "set_property", "path": "UI/Foo/Bar", "name": "Height", "value": "50"},
    ]
    out = _tool_fn(tool)(ctx=None, project="Alpha", ops=ops)
    assert out.get("state") == "succeeded", (
        f"expected state='succeeded'; got: {out}"
    )
    assert out.get("applied") == 2, (
        f"expected applied=2 (both ops landed); got applied={out.get('applied')}"
    )


def test_bridge_edit_status_action_not_refused_as_bad_ops(
    cfg: core.Config,
) -> None:
    """action='status' with ops=[] must NOT trigger the bad_ops guard.

    The bad_ops pre-check (ops must be a non-empty list) is gated to
    action='apply' only.  action='status' does not consume ops at all — it
    reads the batch journal from disk.  Regression guard against accidentally
    widening the guard to all actions."""
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_bridge_edit")
    # No existing batches -> returns {"batches": [], "project": "Alpha"} or
    # similar status document.  The important invariant is no error='bad_ops'.
    out = _tool_fn(tool)(ctx=None, project="Alpha", action="status", ops=[])
    assert out.get("error") != "bad_ops", (
        f"action='status' must not be refused by the bad_ops guard: {out}"
    )
    # Confirm the result looks like a status document, not an unrelated failure.
    assert "batches" in out or "state" in out or "batch_id" in out, (
        f"unexpected shape from action='status': {out}"
    )


# ---- static integrity gate --------------------------------


def test_excluded_outliers_keep_bespoke_no_project_envelope(cfg: core.Config, monkeypatch) -> None:
    """optix_save / optix_emulator(action="run"|"restart") are intentionally NOT
    decorated with @_with_project: they own a bespoke no-project envelope keyed
    to their success shape ({saved: False} / {launched: False}), NOT the
    generic {error: "no_project"}. Decorating them would silently swap that
    contract. Lock the exclusion in."""
    monkeypatch.setattr(core, "default_project", lambda c: None)
    mcp = make_mcp(cfg)
    by_name = {t.name: t for t in _list_tools(mcp)}
    save_out = _tool_fn(by_name["optix_save"])()
    assert save_out.get("saved") is False
    assert save_out.get("error") != "no_project"
    emu_out = _tool_fn(by_name["optix_emulator"])(action="run")
    assert emu_out.get("launched") is False
    assert emu_out.get("error") != "no_project"


# ---- thread-pool limiter hardening (root-cause A, 2026-09-05) ---------------


def test_thread_limiter_bounds_concurrent_offloaded_calls(
    cfg: core.Config, monkeypatch
) -> None:
    """OPTIX_THREAD_LIMITER caps concurrent offloaded sync tool calls.

    Root-cause A hardening for the 2026-09-05 service-crash incident.
    Evidence: service.jsonl on a test box showed four consecutive "start" entries
    with no following "stop" or "crash" — the external_kill signature
    (Task Scheduler ExecutionTimeLimit, result 0x00041306).  A 3450-op burst
    without a limiter saturates the anyio thread pool and stalls the process.

    This test verifies two invariants:
      1. All N calls complete successfully even when N exceeds the cap (no
         deadlock — queued calls are eventually served).
      2. Peak concurrency inside the sync function is bounded by the limiter,
         not by N (no O(N) thread growth).
    """
    import threading
    import time

    LIMIT = 4
    CALLS = 12  # deliberately > LIMIT so some calls must queue
    monkeypatch.setenv("OPTIX_THREAD_LIMITER", str(LIMIT))

    # Shared concurrency counter — accessed from worker threads, guarded by a lock.
    _lock = threading.Lock()
    _active = 0
    _peak = 0

    def _counted_list_screens(cfg_, project):
        """Sync fn that records peak concurrent invocations."""
        nonlocal _active, _peak
        with _lock:
            _active += 1
            if _active > _peak:
                _peak = _active
        time.sleep(0.02)          # hold briefly so concurrent calls overlap
        with _lock:
            _active -= 1
        return {"screens": [], "count": 0}

    monkeypatch.setattr(core, "list_screens", _counted_list_screens)
    monkeypatch.setattr(core, "default_project", lambda _cfg: "Alpha")

    mcp = make_mcp(cfg)

    async def _run_all():
        coros = [mcp.call_tool("optix_list_screens", {}) for _ in range(CALLS)]
        return await asyncio.gather(*coros)

    results = asyncio.run(_run_all())

    # Invariant 1: all CALLS completed (no deadlock under the limiter).
    assert len(results) == CALLS

    # Invariant 2: peak concurrent sync-fn invocations is bounded by LIMIT.
    assert _peak <= LIMIT, (
        f"thread-pool limiter not effective: peak concurrent invocations={_peak}, "
        f"OPTIX_THREAD_LIMITER={LIMIT}; expected peak <= {LIMIT}"
    )
