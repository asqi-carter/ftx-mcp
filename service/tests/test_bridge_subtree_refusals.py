"""Python-side surfacing of the bridge subtree refusals (PRD 0019, bridge 1.0.9).

Offline: the bridge is faked. Covers move_node, convert_to_type and the batch
`rename` sugar (lowered to move) returning contains_netlogic /
contains_event_handler unchanged with a hint, and a batch continuing past a
refused item.
"""
from __future__ import annotations

import json

import pytest

from service import core
from service.tests.conftest import make_project

_CODES = ["contains_netlogic", "contains_event_handler"]


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


@pytest.fixture
def alpha(cfg, projects_root):
    make_project(projects_root, "Alpha")
    return cfg


def _refusing_bridge(code):
    health = (200, {"bridge_version": "1.0.9", "project": "Alpha", "model_loaded": True})
    refusal = (200, {"ok": False, "error": {"code": code, "message": f"refused at UI/P/Child ({code})"}})

    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        status, body = health if path.startswith("/bridge/health") else refusal
        return status, json.dumps(body).encode()

    return fake


def _healthy_bridge(cfg, path, method="GET", timeout=5.0, **_kw):
    body = {"bridge_version": "1.0.9", "project": "Alpha", "model_loaded": True}
    return 200, json.dumps(body).encode()


def _check(exc, code):
    assert code in str(exc)
    assert exc.bridge_code == code
    assert exc.hint == core.BRIDGE_SUBTREE_REFUSALS[code]
    assert "Do not retry as-is" in exc.hint
    assert exc.nudge.startswith(f"{code}:")


@pytest.mark.parametrize("code", _CODES)
def test_move_node_surfaces_code_and_hint(alpha, monkeypatch, code):
    monkeypatch.setattr(core, "_bridge_http", _refusing_bridge(code))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_move_node(alpha, "Alpha", "UI/P", "UI/Q")
    _check(e.value, code)


@pytest.mark.parametrize("code", _CODES)
def test_convert_to_type_surfaces_code_and_hint(alpha, monkeypatch, code):
    monkeypatch.setattr(core, "_bridge_http", _refusing_bridge(code))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_convert_to_type(alpha, "Alpha", "UI/P", "T", "UI/Templates")
    _check(e.value, code)


@pytest.mark.parametrize("code", _CODES)
def test_rename_lowered_surfaces_code_and_hint(alpha, monkeypatch, code):
    monkeypatch.setattr(core, "_bridge_http", _refusing_bridge(code))
    op = core._normalize_edit_op({"op": "rename", "path": "UI/P", "new_name": "P2"})
    assert op["op"] == "move"
    with pytest.raises(core.BridgeWriteFailed) as e:
        core._apply_one_edit(alpha, "Alpha", op)
    _check(e.value, code)


@pytest.mark.parametrize("code", _CODES)
def test_classify_failure_carries_code_and_hint(alpha, monkeypatch, code):
    monkeypatch.setattr(core, "_bridge_http", _refusing_bridge(code))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_move_node(alpha, "Alpha", "UI/P", "UI/Q")
    out = core.classify_bridge_failure(alpha, "Alpha", e.value)
    assert out["bridge_code"] == code
    assert out["hint"] == core.BRIDGE_SUBTREE_REFUSALS[code]
    assert out["nudge"].startswith(f"{code}:")


def test_codes_listed_as_known_bridge_errors():
    for code in _CODES:
        assert code in core.KNOWN_BRIDGE_ERRORS
        assert code in core.bridge_move_node.__doc__
        assert code in core.bridge_convert_to_type.__doc__


@pytest.mark.parametrize("code", _CODES)
def test_batch_continues_past_refused_item(alpha, monkeypatch, code):
    ok_report = {"ok": True, "op_count": 3, "strict": False, "errors": [], "warnings": []}
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0, **_: (200, ok_report))
    applied: list = []
    real = core._apply_one_edit

    def fake_apply(cfg, project, op):
        if op["op"] == "move" and op["path"] == "UI/Bad":
            monkeypatch.setattr(core, "_bridge_http", _refusing_bridge(code))
            return real(cfg, project, op)
        applied.append(op["path"])
        return {}

    monkeypatch.setattr(core, "_apply_one_edit", fake_apply)
    monkeypatch.setattr(core, "_bridge_http", _refusing_bridge(code))
    ops = [
        {"op": "delete", "path": "UI/A"},
        {"op": "rename", "path": "UI/Bad", "new_name": "Bad2"},
        {"op": "delete", "path": "UI/C"},
    ]
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert out["state"] == "succeeded"
    assert applied == ["UI/A", "UI/C"]
    assert out["applied"] == 2
    assert len(out["refused_ops"]) == 1
    r = out["refused_ops"][0]
    assert r["index"] == 1 and r["code"] == code and r["hint"]
    assert "refused_ops" in out["nudge"]
    assert [t["ok"] for t in out["op_timings"]] == [True, False, True]


# ---- mixed batches: subtree refusals are per-op, not a batch failure ---------

def _validate_report(errors):
    return {"ok": not errors, "op_count": 3, "strict": False,
            "errors": errors, "warnings": []}


def _mixed_batch(monkeypatch, refused_op, errors, code=None):
    """Run [delete A, <refused_op>, delete C]; the bridge's validate stage
    reports `errors`. Returns (result, paths that landed)."""
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0, **_:
                        (200, _validate_report(errors)))
    landed: list = []
    real = core._apply_one_edit

    def fake_apply(cfg, project, op):
        if op["path"] == "UI/Bad":
            return real(cfg, project, op)  # refusing bridge (apply-time case)
        landed.append(op["path"])
        return {}

    monkeypatch.setattr(core, "_apply_one_edit", fake_apply)
    if code:
        monkeypatch.setattr(core, "_bridge_http", _refusing_bridge(code))
    ops = [{"op": "delete", "path": "UI/A"}, refused_op,
           {"op": "delete", "path": "UI/C"}]
    return core.bridge_edit(_CFG["cfg"], "Alpha", ops), landed


@pytest.fixture(autouse=True)
def _stash_cfg(alpha, monkeypatch):
    _CFG["cfg"] = alpha
    # Default: a healthy bridge for Alpha (tests override _bridge_http to refuse).
    monkeypatch.setattr(core, "_bridge_http", _healthy_bridge)
    yield
    _CFG.clear()


_CFG: dict = {}


def _assert_mixed(out, landed, code):
    assert out["state"] == "succeeded", out
    assert landed == ["UI/A", "UI/C"]
    assert out["applied"] == 2
    assert len(out["refused_ops"]) == 1
    r = out["refused_ops"][0]
    assert set(("index", "op", "path", "code", "hint")) <= set(r)
    assert r["index"] == 1 and r["path"] == "UI/Bad" and r["code"] == code
    assert r["hint"] == core.BRIDGE_SUBTREE_REFUSALS[code]
    assert "index 1" in out["nudge"] and "refused_ops" in out["nudge"]
    assert out["report"]["ok"] is True and not out["report"]["errors"]


@pytest.mark.parametrize("code", _CODES)
def test_mixed_batch_rename_lowered_to_move_refused_at_validation(monkeypatch, code):
    err = {"op_index": 1, "code": code, "message": "refused at UI/Bad/Child"}
    out, landed = _mixed_batch(
        monkeypatch, {"op": "rename", "path": "UI/Bad", "new_name": "Bad2"}, [err])
    _assert_mixed(out, landed, code)
    assert out["refused_ops"][0]["op"] == "move"  # rename is lowered to move


@pytest.mark.parametrize("code", _CODES + ["refused_netlogic_move"])
def test_mixed_batch_move_refused_at_validation(monkeypatch, code):
    err = {"op_index": 1, "code": code, "message": "refused at UI/Bad"}
    out, landed = _mixed_batch(
        monkeypatch, {"op": "move", "path": "UI/Bad", "new_parent": "UI/Q"}, [err])
    _assert_mixed(out, landed, code)
    assert out["refused_ops"][0]["op"] == "move"


@pytest.mark.parametrize("code", _CODES)
def test_mixed_batch_convert_to_type_refused(monkeypatch, code):
    # The bridge's validator does not pre-check convert_to_type, so the
    # refusal comes back at apply time; the batch still carries on.
    out, landed = _mixed_batch(
        monkeypatch,
        {"op": "convert_to_type", "path": "UI/Bad", "type_name": "T",
         "types_folder": "UI/Templates"}, [], code=code)
    _assert_mixed(out, landed, code)
    assert out["refused_ops"][0]["op"] == "convert_to_type"


def test_create_rename_set_property_applies_two_refuses_one(monkeypatch, alpha):
    err = {"op_index": 1, "code": "contains_event_handler", "message": "m"}
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0, **_:
                        (200, _validate_report([err])))
    landed: list = []
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: landed.append(op["op"]) or {})
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_widget", "screen": "UI", "name": "W"},
        {"op": "rename", "path": "UI/Panel", "new_name": "P2"},
        {"op": "set_property", "path": "UI/W", "name": "Text", "value": "x"},
    ])
    assert landed == ["create_widget", "set_property"]
    assert out["applied"] == 2 and out["state"] == "succeeded"
    assert len(out["refused_ops"]) == 1
    assert out["refused_ops"][0]["code"] == "contains_event_handler"
    assert [t["ok"] for t in out["op_timings"]] == [True, False, True]


def test_dry_run_reports_refusal_without_batch_failure(monkeypatch, alpha):
    err = {"op_index": 1, "code": "contains_netlogic", "message": "m"}
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0, **_:
                        (200, _validate_report([err])))
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "delete", "path": "UI/A"},
        {"op": "move", "path": "UI/Bad", "new_parent": "UI/Q"},
    ], dry_run=True)
    assert out["applied"] == 0 and out["report"]["ok"] is True
    assert [r["index"] for r in out["refused_ops"]] == [1]


def test_other_validation_errors_still_refuse_whole_batch(monkeypatch, alpha):
    errs = [
        {"op_index": 1, "code": "contains_event_handler", "message": "m"},
        {"op_index": 2, "code": "unresolved_reference", "message": "no node"},
    ]
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0, **_:
                        (200, _validate_report(errs)))
    applied: list = []
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op) or {})
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "delete", "path": "UI/A"},
        {"op": "move", "path": "UI/Bad", "new_parent": "UI/Q"},
        {"op": "delete", "path": "UI/Nope"},
    ])
    assert out["applied"] == 0 and out["state"] == "validated"
    assert applied == []
    assert "refused_ops" not in out
    assert out["report"]["ok"] is False
    assert "validation refused the batch" in out["nudge"]
