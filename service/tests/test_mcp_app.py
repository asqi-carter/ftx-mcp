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
    "optix_health",
    "optix_doctor",
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
    "optix_bridge_create_widget",
    "optix_bridge_add_label",
    "optix_bridge_add_bound_widget",
    "optix_bridge_add_navigation_panel_item",
    "optix_bridge_ensure_web_engine",
    "optix_bridge_set_property",
    "optix_bridge_create_variable",
    "optix_bridge_create_folder",
    "optix_bridge_create_object",
    "optix_bridge_create_type",
    "optix_bridge_convert_to_type",
    "optix_bridge_move_node",
    "optix_bridge_bind_property",
    "optix_bridge_create_alias",
    "optix_bridge_add_translation",
    "optix_bridge_delete_node",
    "optix_bridge_wire_event",
    "optix_bridge_reorder",
    "optix_bridge_attach_expression",
    "optix_bridge_validate_expression",
    "optix_save",
    "optix_run_emulator",
    "optix_restart_emulator",
    "optix_emulator_status",
    "optix_stop_emulator",
    "optix_runtime_log_tail",
    "optix_deploy_updatesvc",
    "optix_add_widget",
    "optix_add_model_variable",
    "optix_set_property",
    "optix_deploy_preflight",
    "optix_studio_version",
    "optix_runtime_start",
    "optix_runtime_stop",
    "optix_runtime_status",
    "optix_services_status",
    "optix_cdp_click",
    "optix_cdp_fill",
    "optix_cdp_type",
    "optix_cdp_key",
    "optix_cdp_screenshot",
    "optix_cdp_ocr",
    "optix_cdp_read_text",
    "optix_cdp_find_text",
    "optix_cdp_navigate",
    "optix_cdp_restart",
}


def _list_tools(mcp) -> list:
    return mcp._tool_manager.list_tools()


def test_mcp_registers_every_spec_tool(cfg: core.Config) -> None:
    mcp = make_mcp(cfg)
    names = {t.name for t in _list_tools(mcp)}
    missing = EXPECTED_TOOLS - names
    extra = names - EXPECTED_TOOLS
    assert not missing, f"missing MCP tools: {missing}"
    assert not extra, f"unexpected MCP tools (update EXPECTED_TOOLS or SPEC): {extra}"


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
    READ = {"optix_health","optix_doctor","optix_find","optix_list_projects",
            "optix_list_screens","optix_read_file","optix_describe_node",
            "optix_describe_type","optix_list_ui_types","optix_bridge_status",
            "optix_studio_version","optix_runtime_status","optix_services_status",
            "optix_deploy_preflight","optix_cdp_screenshot","optix_cdp_ocr",
            "optix_cdp_read_text","optix_cdp_find_text",
            "optix_bridge_validate_expression",
            "optix_emulator_status", "optix_runtime_log_tail",
            "optix_get_project_map", "optix_list_skills", "optix_get_skill"}
    DESTRUCTIVE = {"optix_deploy","optix_deploy_updatesvc","optix_bridge_delete_node",
                   "optix_runtime_stop","optix_cdp_click","optix_cdp_type",
                   "optix_cdp_key","optix_cdp_fill","optix_cdp_navigate",
                   # replace=true deletes the original instance after the move
                   "optix_bridge_convert_to_type",
                   # re-author move deletes the original after the copy
                   "optix_bridge_move_node"}
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
    def _raise(*a, **k):
        raise core.BridgeUnavailable("bridge unreachable")
    monkeypatch.setattr(core, "bridge_set_property", _raise)
    monkeypatch.setattr(core, "classify_bridge_failure", lambda cfg, project, exc: {
        "state": "failed", "reason_code": "bridge_unreachable_studio_closed",
        "nudge": "Open the project in Studio and run StartBridge."})
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_bridge_set_property")
    out = tool.fn(project="Alpha", node_path="UI/MainWindow/L1", name="Text", value="hi")
    assert out["state"] == "failed"
    assert out["reason_code"] == "bridge_unreachable_studio_closed"
    assert "StartBridge" in out["nudge"]


def test_mcp_health_tool_returns_expected_keys(cfg: core.Config) -> None:
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_health")
    out = tool.fn()
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
    out = tool.fn()
    assert "projects" in out
    names = [p["name"] for p in out["projects"]]
    assert "Alpha" in names


def test_mcp_deploy_preflight_tool_returns_envelope(
    cfg: core.Config, projects_root: Path
) -> None:
    make_project(projects_root, "Alpha")
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_deploy_preflight")
    out = tool.fn(project="Alpha")
    for key in ("ready", "blockers", "warnings", "checks"):
        assert key in out, f"preflight envelope missing {key!r}: {out}"


def test_mcp_call_tool_path_invokes_health(cfg: core.Config) -> None:
    """Exercise the FastMCP `call_tool` async path so we know the
    registered tool surface is wired through the manager, not just
    available via direct `.fn` access."""
    mcp = make_mcp(cfg)

    async def _invoke():
        return await mcp.call_tool("optix_health", {})

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
    for keep in ("optix_run_emulator", "optix_restart_emulator",
                 "optix_bridge_create_widget", "optix_cdp_screenshot",
                 "optix_get_project_map"):
        assert keep in names


def test_deploy_family_present_when_enabled(cfg: core.Config) -> None:
    names = {t.name for t in _list_tools(make_mcp(cfg))}  # cfg fixture: enabled
    assert DEPLOY_FAMILY <= names


def test_server_ships_instructions(cfg: core.Config) -> None:
    """The MCP instructions field is the always-visible orientation — it must
    exist, stay short, and point at the skill tools."""
    mcp = make_mcp(cfg)
    ins = mcp._mcp_server.instructions or ""
    assert "optix_list_skills" in ins and "optix_restart_emulator" in ins
    assert len(ins) < 1200, "instructions must stay lean — they cost every session"


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
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_cdp_screenshot")
    out = tool.fn(save_path=str(shot))
    assert isinstance(out, dict)
    assert out["state"] == "succeeded"
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
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_cdp_screenshot")
    out = tool.fn(save_path=str(shot), return_image=True)
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
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_cdp_screenshot")
    out = tool.fn(return_image=True)
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
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_cdp_screenshot")
    out = tool.fn(save_path=str(shot), region=[0.1, 0.1, 0.2, 0.2])
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
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_cdp_screenshot")
    out = tool.fn(save_path=str(shot), region=[0.0, 0.0, 0.1, 0.1], return_image=True)
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
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_cdp_screenshot")
    out = tool.fn(region=[1, 2, 3])
    assert isinstance(out, dict)
    assert out["state"] == "failed" and out["error"] == "bad_region"


def test_cdp_read_text_tool_registered_and_forwards_to_core(
    cfg: core.Config, monkeypatch
) -> None:
    seen = {}

    def fake_read_text(cfg_, region=None, navigate_url=None, settle_seconds=None,
                       psm=6):
        seen.update(region=region, psm=psm)
        return {"state": "succeeded", "text": "SP-101", "region": region,
                "size_bytes": 10, "navigated": False, "captured_at": "t"}

    monkeypatch.setattr(core, "cdp_read_text_runtime", fake_read_text)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_cdp_read_text")
    out = tool.fn(region=[0.0, 0.0, 0.5, 0.5], psm=7)
    assert out["text"] == "SP-101"
    assert seen == {"region": [0.0, 0.0, 0.5, 0.5], "psm": 7}


def test_cdp_read_text_tool_degrades_on_missing_tesseract(
    cfg: core.Config, monkeypatch
) -> None:
    monkeypatch.setattr(core, "cdp_read_text_runtime", lambda *a, **k: {
        "state": "failed", "text": None, "error": "tesseract_not_installed",
        "hint": "install tesseract"})
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_cdp_read_text")
    out = tool.fn()
    assert out["state"] == "failed" and out["error"] == "tesseract_not_installed"


def test_cdp_find_text_tool_registered_and_forwards_to_core(
    cfg: core.Config, monkeypatch
) -> None:
    seen = {}

    def fake_find_text(cfg_, text, navigate_url=None, settle_seconds=None):
        seen["text"] = text
        return {"state": "succeeded", "found": True, "matches": [
            {"text": "Start", "confidence": 95.0, "bbox_px": [1, 2, 3, 4],
             "bbox_norm": [0.1, 0.2, 0.3, 0.4], "center_px": [2.5, 4.0]}],
            "viewport": {"w": 1000, "h": 800}}

    monkeypatch.setattr(core, "cdp_find_text_runtime", fake_find_text)
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_cdp_find_text")
    out = tool.fn(text="Start")
    assert out["found"] is True and seen["text"] == "Start"
    assert out["matches"][0]["center_px"] == [2.5, 4.0]


def test_cdp_find_text_tool_no_match_is_not_an_error(
    cfg: core.Config, monkeypatch
) -> None:
    monkeypatch.setattr(core, "cdp_find_text_runtime", lambda cfg_, text, **k: {
        "state": "succeeded", "found": False, "matches": [],
        "viewport": {"w": 1000, "h": 800}})
    mcp = make_mcp(cfg)
    tool = next(t for t in _list_tools(mcp) if t.name == "optix_cdp_find_text")
    out = tool.fn(text="Nonexistent")
    assert out["state"] == "succeeded" and out["found"] is False
