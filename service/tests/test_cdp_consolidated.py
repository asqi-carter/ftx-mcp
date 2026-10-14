"""U14 — consolidated CDP surface (optix_observe / optix_interact).

optix_observe(mode=...) folds the five read CDP tools (screenshot / ocr /
read_text / find_text / diff) and optix_interact(action=...) folds the five
interact CDP tools (click / fill / type / key / navigate) into two
discriminator tools. These tests pin:

  1. each mode/action delegates to the SAME core.* the deprecated alias does,
     with the SAME kwargs (parity);
  2. the screenshot return_image List path (json + typed image) is preserved
     through optix_observe;
  3. an invalid mode/action returns a structured valid-vocab error (no raise);
  4. the FTXMCP_LEGACY_TOOLS gate matrix: unset -> consolidated-only (the 10
     deprecated aliases suppressed), "0" -> same as unset, "1" -> aliases
     restored with the deprecation marker; the kept-as-is sweep/restart and
     the consolidated tools survive every setting.
"""
from __future__ import annotations

import json as _json

import pytest

from service import auth, core
from service.mcp_app import make_mcp

# The 10 optix_cdp_* primitives folded into the consolidated tools; sweep and
# restart are kept as-is (not aliases) and must survive the gate.
_CDP_ALIASES = (
    "optix_cdp_screenshot", "optix_cdp_ocr", "optix_cdp_read_text",
    "optix_cdp_find_text", "optix_cdp_diff", "optix_cdp_click",
    "optix_cdp_fill", "optix_cdp_type", "optix_cdp_key", "optix_cdp_navigate",
)


def _tool_fn(mcp, name):
    """Directly-callable sync fn for a tool; offloaded tools keep the original
    sync fn at _ftx_sync_fn."""
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == name)
    return getattr(tool, "_ftx_sync_fn", tool.fn)


# ---- optix_observe mode -> core.* parity --------------------------------

def test_observe_screenshot_forwards_to_core(cfg, monkeypatch, tmp_path):
    shot = tmp_path / "shot.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xdbfakejpeg")
    seen = {}

    def fake(cfg_, save_path=None, quality=65, navigate_url=None,
             settle_seconds=None, fresh=False, region=None, project=None):
        seen.update(save_path=save_path, quality=quality,
                    navigate_url=navigate_url, settle_seconds=settle_seconds,
                    fresh=fresh, region=region)
        return {"state": "succeeded", "path": str(shot), "size_bytes": 8}

    monkeypatch.setattr(core, "cdp_screenshot_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(
        mode="screenshot", save_path=str(shot), quality=70,
        navigate_url="http://x", settle_seconds=1.0, fresh=True,
        region=[0.1, 0.1, 0.2, 0.2])
    assert seen == {"save_path": str(shot), "quality": 70,
                    "navigate_url": "http://x", "settle_seconds": 1.0,
                    "fresh": True, "region": [0.1, 0.1, 0.2, 0.2]}
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert "hint" in out and "file tool" in out["hint"]


def test_observe_screenshot_default_path_when_omitted(cfg, monkeypatch):
    seen = {}

    def fake(cfg_, save_path=None, **kw):
        seen["save_path"] = save_path
        return {"state": "succeeded", "path": save_path, "size_bytes": 1}

    monkeypatch.setattr(core, "cdp_screenshot_runtime", fake)
    mcp = make_mcp(cfg)
    _tool_fn(mcp, "optix_observe")(mode="screenshot")
    # a temp path is synthesized when save_path is omitted (parity with alias)
    assert seen["save_path"] and seen["save_path"].endswith(".jpg")


def test_observe_screenshot_return_image_preserves_list(cfg, monkeypatch, tmp_path):
    from mcp.server.fastmcp import Image as McpImage

    shot = tmp_path / "shot.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xdbfakejpeg")

    def fake(cfg_, save_path=None, **kw):
        return {"state": "succeeded", "path": str(shot), "size_bytes": 8}

    monkeypatch.setattr(core, "cdp_screenshot_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(
        mode="screenshot", save_path=str(shot), return_image=True)
    assert isinstance(out, list) and len(out) == 2
    meta = _json.loads(out[0])
    assert meta["state"] == "succeeded" and meta["path"] == str(shot)
    assert isinstance(out[1], McpImage)


def test_observe_screenshot_return_image_failure_stays_dict(cfg, monkeypatch):
    def fake(cfg_, save_path=None, **kw):
        return {"state": "failed", "path": None, "error": "boom"}

    monkeypatch.setattr(core, "cdp_screenshot_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(mode="screenshot", return_image=True)
    assert isinstance(out, dict) and out["state"] == "failed"


def test_observe_ocr_forwards_to_core(cfg, monkeypatch):
    seen = {}

    def fake(cfg_, navigate_url=None, settle_seconds=None, psm=6, **kw):
        seen.update(navigate_url=navigate_url, settle_seconds=settle_seconds,
                    psm=psm)
        return {"state": "succeeded", "text": "hi"}

    monkeypatch.setattr(core, "cdp_ocr_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(
        mode="ocr", navigate_url="u", settle_seconds=0.5, psm=7)
    assert seen == {"navigate_url": "u", "settle_seconds": 0.5, "psm": 7}
    assert out["text"] == "hi"


def test_observe_read_text_forwards_to_core(cfg, monkeypatch):
    seen = {}

    def fake(cfg_, region=None, navigate_url=None, settle_seconds=None, psm=6, **kw):
        seen.update(region=region, navigate_url=navigate_url,
                    settle_seconds=settle_seconds, psm=psm)
        return {"state": "succeeded", "text": "X"}

    monkeypatch.setattr(core, "cdp_read_text_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(
        mode="read_text", region=[0.0, 0.0, 0.5, 0.5], navigate_url="u",
        settle_seconds=0.2, psm=11)
    assert seen == {"region": [0.0, 0.0, 0.5, 0.5], "navigate_url": "u",
                    "settle_seconds": 0.2, "psm": 11}
    assert out["text"] == "X"


def test_observe_find_text_forwards_to_core(cfg, monkeypatch):
    seen = {}

    def fake(cfg_, text, navigate_url=None, settle_seconds=None, **kw):
        seen.update(text=text, navigate_url=navigate_url,
                    settle_seconds=settle_seconds)
        return {"state": "succeeded", "found": True, "matches": []}

    monkeypatch.setattr(core, "cdp_find_text_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(
        mode="find_text", text="Start", navigate_url="u", settle_seconds=0.3)
    assert seen == {"text": "Start", "navigate_url": "u", "settle_seconds": 0.3}
    assert out["found"] is True


def test_observe_diff_forwards_to_core(cfg, monkeypatch):
    seen = {}

    def fake(dir_a, dir_b, threshold=2.0):
        seen.update(dir_a=dir_a, dir_b=dir_b, threshold=threshold)
        return {"state": "succeeded", "screens": {}}

    monkeypatch.setattr(core, "cdp_diff_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(
        mode="diff", dir_a="/a", dir_b="/b", threshold=5.0)
    assert seen == {"dir_a": "/a", "dir_b": "/b", "threshold": 5.0}
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))


def test_observe_invalid_mode_returns_structured_error(cfg):
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(mode="bogus")
    assert isinstance(out, dict)
    assert out["error"] == "bad_mode"
    assert set(out["valid_modes"]) == {
        "screenshot", "ocr", "read_text", "find_text", "diff"}


# ---- optix_interact action -> core.* parity -----------------------------

def test_interact_click_forwards_to_core(cfg, monkeypatch):
    seen = {}

    def fake(cfg_, x=None, y=None, navigate_url=None, settle_seconds=None):
        seen.update(x=x, y=y, navigate_url=navigate_url,
                    settle_seconds=settle_seconds)
        return {"state": "succeeded", "x": x, "y": y}

    monkeypatch.setattr(core, "cdp_click_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_interact")(
        action="click", x=10.0, y=20.0, navigate_url="u", settle_seconds=0.5)
    assert seen == {"x": 10.0, "y": 20.0, "navigate_url": "u",
                    "settle_seconds": 0.5}
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))


def test_interact_fill_forwards_to_core(cfg, monkeypatch):
    seen = {}

    def fake(cfg_, x=None, y=None, text=None, submit="Enter", select_all=True,
             navigate_url=None, settle_seconds=None, project=None):
        seen.update(x=x, y=y, text=text, submit=submit, select_all=select_all,
                    navigate_url=navigate_url, settle_seconds=settle_seconds)
        return {"state": "succeeded"}

    monkeypatch.setattr(core, "cdp_fill_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_interact")(
        action="fill", x=1.0, y=2.0, text="42", submit="Tab", select_all=False,
        navigate_url="u", settle_seconds=0.1)
    assert seen == {"x": 1.0, "y": 2.0, "text": "42", "submit": "Tab",
                    "select_all": False, "navigate_url": "u",
                    "settle_seconds": 0.1}
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))


def test_interact_type_forwards_to_core(cfg, monkeypatch):
    seen = {}

    def fake(cfg_, text=None, navigate_url=None, settle_seconds=None):
        seen.update(text=text, navigate_url=navigate_url,
                    settle_seconds=settle_seconds)
        return {"state": "succeeded", "typed_chars": len(text or "")}

    monkeypatch.setattr(core, "cdp_type_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_interact")(
        action="type", text="hello", navigate_url="u", settle_seconds=0.2)
    assert seen == {"text": "hello", "navigate_url": "u", "settle_seconds": 0.2}
    assert out["typed_chars"] == 5


def test_interact_key_forwards_to_core(cfg, monkeypatch):
    seen = {}

    def fake(cfg_, key=None, navigate_url=None, settle_seconds=None):
        seen.update(key=key, navigate_url=navigate_url,
                    settle_seconds=settle_seconds)
        return {"state": "succeeded", "key": key}

    monkeypatch.setattr(core, "cdp_key_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_interact")(
        action="key", key="Enter", navigate_url="u", settle_seconds=0.0)
    assert seen == {"key": "Enter", "navigate_url": "u", "settle_seconds": 0.0}
    assert out["key"] == "Enter"


def test_interact_navigate_forwards_to_core(cfg, monkeypatch):
    seen = {}

    def fake(cfg_, route=None, routes_path=None, expect=True, navigate_url=None,
             project=None):
        seen.update(route=route, routes_path=routes_path, expect=expect,
                    navigate_url=navigate_url)
        return {"state": "succeeded", "route": route}

    monkeypatch.setattr(core, "cdp_navigate_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_interact")(
        action="navigate", route="setup", routes_path="/r.json", expect=False,
        navigate_url="u")
    assert seen == {"route": "setup", "routes_path": "/r.json",
                    "expect": False, "navigate_url": "u"}
    assert out["route"] == "setup"


def test_interact_invalid_action_returns_structured_error(cfg):
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_interact")(action="bogus")
    assert isinstance(out, dict)
    assert out["error"] == "bad_action"
    assert set(out["valid_actions"]) == {
        "click", "fill", "type", "key", "navigate"}


@pytest.mark.parametrize("action,required", [
    ("click", "x, y"),
    ("fill", "x, y, text"),
    ("type", "text"),
    ("key", "key"),
    ("navigate", "route, routes_path"),
])
def test_interact_missing_required_param_returns_structured_error(
        cfg, action, required):
    """Calling an action without its required params returns a structured
    error naming them rather than passing None into core.* (which would raise
    deep in the CDP path)."""
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_interact")(action=action)
    assert isinstance(out, dict)
    assert out["error"] == "missing_param"
    assert out["message"].endswith(required)


# ---- FTXMCP_LEGACY_TOOLS gate -------------------------------------------

def test_default_gate_registers_consolidated_not_aliases(cfg):
    """DEFAULT (FTXMCP_LEGACY_TOOLS unset) is consolidated-only: optix_observe
    / optix_interact are registered, the always-on sweep/restart survive, and
    the 10 deprecated optix_cdp_* aliases are ABSENT."""
    mcp = make_mcp(cfg)
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert {"optix_observe", "optix_interact"} <= names
    assert {"optix_cdp_sweep", "optix_cdp_restart"} <= names
    assert names.isdisjoint(_CDP_ALIASES)  # aliases OFF by default


def test_gate_off_suppresses_aliases_keeps_consolidated(cfg, monkeypatch):
    """FTXMCP_LEGACY_TOOLS=0 is not "1" so it matches the default: aliases
    suppressed, consolidated + sweep/restart kept."""
    monkeypatch.setenv("FTXMCP_LEGACY_TOOLS", "0")
    mcp = make_mcp(cfg)
    names = {t.name for t in mcp._tool_manager.list_tools()}
    # consolidated surface present
    assert {"optix_observe", "optix_interact"} <= names
    # kept-as-is batch/lifecycle tools survive the gate
    assert {"optix_cdp_sweep", "optix_cdp_restart"} <= names
    # the 10 deprecated aliases are suppressed
    assert names.isdisjoint(_CDP_ALIASES)


def test_gate_on_restores_deprecated_aliases(cfg, monkeypatch):
    """FTXMCP_LEGACY_TOOLS=1 is the opt-in escape hatch: it RESTORES the 10
    deprecated aliases alongside the consolidated + sweep/restart tools."""
    monkeypatch.setenv("FTXMCP_LEGACY_TOOLS", "1")
    mcp = make_mcp(cfg)
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert {"optix_observe", "optix_interact"} <= names
    assert set(_CDP_ALIASES) <= names  # aliases restored under the escape hatch
    assert {"optix_cdp_sweep", "optix_cdp_restart"} <= names


def test_deprecated_aliases_carry_deprecation_marker(cfg, monkeypatch):
    """When restored via FTXMCP_LEGACY_TOOLS=1 the aliases carry the
    deprecation prefix (markers only exist when the aliases are registered)."""
    monkeypatch.setenv("FTXMCP_LEGACY_TOOLS", "1")
    mcp = make_mcp(cfg)
    by_name = {t.name: t for t in mcp._tool_manager.list_tools()}
    for alias in _CDP_ALIASES:
        desc = by_name[alias].description or ""
        assert desc.startswith("(deprecated: use optix_observe/optix_interact)")
        # the shipped Use-when guidance survives the prefix
        assert "Use this when" in desc


def test_consolidated_tools_have_scope_entries():
    assert auth.TOOL_SCOPES["optix_observe"] == "read"
    assert auth.TOOL_SCOPES["optix_interact"] == "author"
