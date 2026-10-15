"""``create_netlogic`` as an ``optix_bridge_edit`` op (PRD 0051, #79).

Offline: the bridge seams are faked. A default install hides the gated
``optix_bridge_create_netlogic`` primitive, so the batch tool must be able to
create a NetLogic through the SAME implementation.
"""
from __future__ import annotations

import pytest

from service import core
from service.mcp_app import make_mcp
from service.tests.conftest import make_project
from service.tests.test_mcp_app import _list_tools, _tool_fn


@pytest.fixture
def alpha(cfg, projects_root):
    make_project(projects_root, "Alpha")
    return cfg


@pytest.fixture
def seams(alpha, monkeypatch):
    """Healthy validate stage, no op-type catalog fetch; record shared-impl calls."""
    monkeypatch.setattr(
        core, "bridge_validate_ops",
        lambda cfg_, project, ops, strict=False: {
            "ok": True, "errors": [], "warnings": [],
            "op_count": len(ops), "strict": strict})
    monkeypatch.setattr(core, "validate_edit_op_types", lambda *a, **k: [])
    calls: list = []

    def fake_create(cfg_, project, parent, name, kind=None, dry_run=False):
        calls.append((project, parent, name, kind))
        return {"ok": True, "created_path": f"{parent}/{name}", "type": "NetLogic"}

    monkeypatch.setattr(core, "bridge_create_netlogic", fake_create)
    return calls


def _edit_tool(cfg):
    tool = next(t for t in _list_tools(make_mcp(cfg))
                if t.name == "optix_bridge_edit")
    return tool, _tool_fn(tool)


def test_create_netlogic_is_a_valid_edit_op():
    assert "create_netlogic" in core.BRIDGE_EDIT_VERBS
    fn, required, optional = core._BRIDGE_EDIT_OPS["create_netlogic"]
    assert fn == "bridge_create_netlogic"
    assert required == ("parent", "name") and set(optional) == {"kind"}
    assert core._BRIDGE_EDIT_POSITIONAL["create_netlogic"] == required
    assert core.unknown_op_fields(
        {"op": "create_netlogic", "parent": "Model", "name": "L", "bogus": 1}) == ["bogus"]


def test_batch_with_create_netlogic_dispatches_to_shared_implementation(
        alpha, seams, monkeypatch):
    ops = [{"op": "create_folder", "parent": "Model", "name": "F"},
           {"op": "create_netlogic", "parent": "Model", "name": "MyLogic",
            "kind": "design_time"}]
    monkeypatch.setattr(core, "bridge_create_folder", lambda *a, **k: {"ok": True})
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert out["state"] == "succeeded" and out["applied"] == 2, out
    assert seams == [("Alpha", "Model", "MyLogic", "design_time")]


def test_create_netlogic_kind_defaults_to_none(alpha, seams):
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_netlogic", "parent": "Model", "name": "L"}])
    assert out["state"] == "succeeded"
    assert seams == [("Alpha", "Model", "L", None)]


def test_create_netlogic_tool_and_op_share_one_implementation():
    """The op table names the same function the primitive tool calls."""
    import inspect
    from service import mcp_app
    assert "core.bridge_create_netlogic(" in inspect.getsource(mcp_app)
    assert core._BRIDGE_EDIT_OPS["create_netlogic"][0] == "bridge_create_netlogic"


@pytest.mark.parametrize("op,code", [
    ({"op": "create_netlogic", "parent": "Model", "name": "L", "kind": "Design_Time"},
     "invalid_kind"),
    ({"op": "create_netlogic", "parent": "Model", "kind": "runtime"}, "missing_field"),
    ({"op": "create_netlogic", "name": "L"}, "missing_field"),
])
def test_create_netlogic_bad_request_refused_at_validation(alpha, seams, op, code):
    out = core.bridge_edit(alpha, "Alpha", [op], dry_run=True)
    assert out["state"] == "validated" and out["applied"] == 0
    assert code in {e["code"] for e in out["report"]["errors"]}
    assert seams == []


def test_create_netlogic_refusal_counts_as_failed_op(alpha, seams, monkeypatch):
    monkeypatch.setattr(
        core, "bridge_create_netlogic",
        lambda *a, **k: {"ok": False, "code": "name_exists", "message": "dup"})
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_netlogic", "parent": "Model", "name": "L"}])
    assert out["state"] == "failed" and out["applied"] == 0, out


def test_bad_op_verb_no_longer_names_create_netlogic(cfg, seams, alpha):
    _tool, fn = _edit_tool(cfg)
    ok = fn(ctx=None, project="Alpha", ops=[
        {"op": "create_netlogic", "parent": "Model", "name": "L"}])
    assert ok.get("error") != "bad_op_verb", ok
    assert ok.get("state") == "succeeded"
    bad = fn(ctx=None, project="Alpha", ops=[{"op": "frobnicate"}])
    assert bad["error"] == "bad_op_verb"
    assert "create_netlogic" in bad["valid_ops"]


def test_create_netlogic_in_docstring(cfg):
    tool, _fn = _edit_tool(cfg)
    assert "create_netlogic" in (tool.description or "")
