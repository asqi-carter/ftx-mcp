"""wire_event validates its method and target node before touching a handler (#70).

Offline: ``core._bridge_http`` is faked, so these are contract tests for the
service wrapper (structured refusals come back as results, a batch counts them
as failed ops, validate_ops errors carry op_index) plus source-order checks that
the C# bridge runs its guards before it deletes or creates anything.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from service import core
from service.tests.conftest import make_project

_HEALTHY = {"/bridge/health": (200, {"bridge_version": "0.5.0", "project": "Alpha",
                                     "model_loaded": True})}
_BRIDGE_CS = Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs"


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


@pytest.fixture
def alpha(cfg, projects_root):
    make_project(projects_root, "Alpha")
    return cfg


def _fake(routes, capture=None):
    merged = {**_HEALTHY, **routes}

    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        if capture is not None:
            capture.append((method, path))
        for prefix, (status, body) in merged.items():
            if path.startswith(prefix):
                return status, json.dumps(body).encode()
        return 404, b'{"error":{"code":"not_found"}}'

    return fake


def _wire(alpha, **kw):
    kw.setdefault("method_path", "UI/Logic/DoThing")
    return core.bridge_wire_event(alpha, "Alpha", "UI/Screen/Btn", "MouseClickEvent", **kw)


# ---- method_not_found ------------------------------------------------------

def test_wire_event_method_not_found_returns_available_list(alpha, monkeypatch):
    body = {"ok": False, "error": "method_not_found", "object": "UI/Logic",
            "method": "DoThing", "available": ["Other", "Refresh"],
            "message": "'UI/Logic' has no method 'DoThing'."}
    monkeypatch.setattr(core, "_bridge_http", _fake({"/bridge/node/event": (200, body)}))
    out = _wire(alpha, replace=True)
    assert out["ok"] is False
    assert out["error"] == "method_not_found" == out["code"]
    assert out["object"] == "UI/Logic" and out["method"] == "DoThing"
    assert out["available"] == ["Other", "Refresh"]


def test_wire_event_export_method_not_loaded_has_reopen_required(alpha, monkeypatch):
    body = {"ok": False, "error": "export_method_not_loaded", "object": "NetLogic/Nl",
            "method": "Run", "available": [], "reopen_required": True}
    monkeypatch.setattr(core, "_bridge_http", _fake({"/bridge/node/event": (200, body)}))
    out = _wire(alpha, method_path="NetLogic/Nl/Run")
    assert out["ok"] is False and out["error"] == "export_method_not_loaded"
    assert out["reopen_required"] is True


# ---- event_not_on_node -----------------------------------------------------

def test_wire_event_on_variable_returns_event_not_on_node(alpha, monkeypatch):
    body = {"ok": False, "error": "event_not_on_node", "node": "Model/Flag",
            "node_class": "Variable", "dotnet_type": "UAVariable",
            "event": "MouseClickEvent", "hint": "Wire the event on the widget."}
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake({"/bridge/node/event": (200, body)}, cap))
    out = _wire(alpha, replace=True)
    assert out["ok"] is False and out["error"] == "event_not_on_node"
    assert out["node_class"] == "Variable" and out["dotnet_type"] == "UAVariable"
    assert out["hint"]
    # nothing but the one guarded POST reached the bridge: no follow-up create
    assert [c for c in cap if "/bridge/node/event" in c[1]] == [c for c in cap if c[0] == "POST"]
    assert len([c for c in cap if "/bridge/node/event" in c[1]]) == 1


def test_wire_event_multistep_refusal_is_structured(alpha, monkeypatch):
    body = {"ok": False, "error": "method_not_found", "object": "UI/Logic",
            "method": "Nope", "call_index": 1, "available": ["A"]}
    monkeypatch.setattr(core, "_bridge_http", _fake({}))
    monkeypatch.setattr(core, "_bridge_post_body", lambda cfg, path, b, **_kw: (200, body))
    out = core.bridge_wire_event(
        alpha, "Alpha", "UI/Screen/Btn", "MouseClickEvent",
        calls=[{"method_path": "UI/Logic/A"}, {"method_path": "UI/Logic/Nope"}])
    assert out["error"] == "method_not_found" and out["call_index"] == 1


# ---- replace failure -------------------------------------------------------

def test_wire_event_replace_failure_reports_previous_handler_state(alpha, monkeypatch):
    lost = {"ok": False, "error": "replace_failed", "previous_handler_lost": True,
            "new_handler_kept": True, "message": "could not delete"}
    monkeypatch.setattr(core, "_bridge_http", _fake({"/bridge/node/event": (200, lost)}))
    out = _wire(alpha, replace=True)
    assert out["ok"] is False and out["previous_handler_lost"] is True

    kept = {"ok": False, "error": "replace_failed", "previous_handler_kept": True}
    monkeypatch.setattr(core, "_bridge_http", _fake({"/bridge/node/event": (200, kept)}))
    out = _wire(alpha, replace=True)
    assert out["previous_handler_kept"] is True and out["previous_handler_lost"] is False


# ---- unchanged behaviour ---------------------------------------------------

def test_wire_event_valid_call_unchanged(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake(
        {"/bridge/node/event": (200, {"ok": True, "via": "eventhandler"})}, cap))
    out = _wire(alpha)
    assert out["ok"] is True and out["via"] == "eventhandler"
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert m == "POST" and "method=UI%2FLogic%2FDoThing" in p


def test_wire_event_other_bridge_errors_still_raise(alpha, monkeypatch):
    body = {"ok": False, "error": {"code": "node_not_found", "message": "no node"}}
    monkeypatch.setattr(core, "_bridge_http", _fake({"/bridge/node/event": (200, body)}))
    with pytest.raises(core.BridgeWriteFailed):
        _wire(alpha)


# ---- batch -----------------------------------------------------------------

def test_wire_event_refusal_fails_the_batch_op(alpha, monkeypatch):
    body = {"ok": False, "error": "event_not_on_node", "node": "Model/Flag",
            "node_class": "Variable", "dotnet_type": "UAVariable",
            "event": "MouseClickEvent", "hint": "h", "message": "m"}
    monkeypatch.setattr(core, "_bridge_http", _fake({"/bridge/node/event": (200, body)}))
    with pytest.raises(core.BridgeWriteFailed) as ei:
        core._apply_one_edit(alpha, "Alpha", {
            "op": "wire_event", "path": "Model/Flag", "event_type": "MouseClickEvent",
            "method_path": "UI/Logic/DoThing"})
    assert ei.value.op_result["error"] == "event_not_on_node"


def test_batch_validate_rejects_invalid_wire_event_with_op_index(alpha, monkeypatch):
    report = {"ok": False, "op_count": 2, "strict": False, "warnings": [], "errors": [
        {"op_index": 1, "code": "method_not_found", "message": "no method",
         "object": "UI/Logic", "method": "Nope", "available": ["A"]}]}

    def fake_post(cfg, path, body, **_kw):
        assert path == "/bridge/validate_ops"
        return 200, report

    monkeypatch.setattr(core, "_bridge_post_body", fake_post)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    applied: list = []
    monkeypatch.setattr(core, "_apply_one_edit", lambda c, p, op: applied.append(op))
    ops = [{"op": "create_folder", "parent": "UI", "name": "F"},
           {"op": "wire_event", "path": "UI/Screen/Btn", "event_type": "MouseClickEvent",
            "method_path": "UI/Logic/Nope"}]
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert out["state"] == "validated" and applied == []
    err = out["report"]["errors"][0]
    assert err["op_index"] == 1 and err["code"] == "method_not_found"


# ---- C# bridge: guards run before any handler is deleted or created --------

def _slice(src: str, start: str, end: str) -> str:
    a = src.index(start)
    return src[a:src.index(end, a)]


def test_bridge_guards_run_before_handler_is_deleted_or_created():
    src = _BRIDGE_CS.read_text(encoding="utf-8")
    body = _slice(src, "private string WireEventInline(", "// ---- wire_event, multi-step form")
    guard_event = body.index("EventOnNodeGuard(node, path, evt)")
    guard_method = body.index("WireMethodGuard(objNode")
    lookup = body.index("existingHandler = null")
    create = body.index("InformationModel.MakeObject<FTOptix.CoreBase.EventHandler>")
    delete_old = body.index("replaceTarget.Delete()")
    assert guard_event < lookup and guard_method < lookup
    assert guard_event < create and guard_method < create
    assert create < delete_old, "the previous handler is deleted only after the new one is built"
    # the old eager delete is gone
    assert not re.search(r"existingHandler\.Delete\(\)", body)
    assert "previous_handler_lost" in body and "previous_handler_kept" in body
    # a build failure removes the half-built handler
    assert "finally" in body and "eh.Delete()" in body


def test_bridge_steps_and_validate_ops_use_the_same_guards():
    src = _BRIDGE_CS.read_text(encoding="utf-8")
    steps = _slice(src, "private string WireEventStepsInline(", "// ValidateOpsJson helper (#70)")
    assert "EventOnNodeGuard(node, path, evt)" in steps
    target = _slice(src, "private string ResolveWireCallTarget(", "private static string WireStepVia(")
    assert "WireMethodGuard(objNode" in target
    assert "ValidateWireTargetOp(idx, op, path, created, addErr)" in src
    vt = _slice(src, "private void ValidateWireTargetOp(", "private static void addGuardErr(")
    assert "EventOnNodeGuard(" in vt and "WireMethodGuard(" in vt
