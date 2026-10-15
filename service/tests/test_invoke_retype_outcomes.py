"""invoke_method and retype report real outcomes (#73).

Offline: ``core._bridge_http`` is faked, so these are contract tests for the
service wrappers (structured refusals come back as results, nothing but the one
guarded POST reaches the bridge) plus source-order checks that the C# bridge
resolves the method before ``ExecuteMethod`` and checks value convertibility
before it writes a retyped value.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from service import core
from service.tests.conftest import make_project

_BRIDGE_CS = Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs"
_HEALTH = {"bridge_version": "1.0.9", "project": "Alpha", "model_loaded": True,
           "invoke_session": "root"}


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
    merged = {"/bridge/health": (200, _HEALTH), **routes}

    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        if capture is not None:
            capture.append((method, path))
        for prefix, (status, body) in merged.items():
            if path.startswith(prefix):
                return status, json.dumps(body).encode()
        return 404, b'{"error":{"code":"not_found"}}'

    return fake


def _invoke(alpha, **kw):
    return core.bridge_invoke_method(alpha, "Alpha", "UI/Logic1", "Go", **kw)


def _csharp_body(signature: str) -> str:
    text = _BRIDGE_CS.read_text(encoding="utf-8")
    body = text[text.index(signature):]
    return body[:body.index("\n    }\n")]


# ---- invoke_method ---------------------------------------------------------

def test_invoke_method_not_found_returns_available(alpha, monkeypatch):
    body = {"ok": False, "error": "method_not_found", "object": "UI/Logic1",
            "method": "Go", "available": ["Other", "Refresh"]}
    monkeypatch.setattr(core, "_bridge_http", _fake({"/bridge/node/invoke": (200, body)}))
    out = _invoke(alpha)
    assert out["ok"] is False
    assert out["error"] == "method_not_found" == out["code"]
    assert out["available"] == ["Other", "Refresh"]


def test_invoke_method_not_found_never_calls_execute_method() -> None:
    body = _csharp_body("private string InvokeMethodInline(")
    guard = body.find("WireMethodGuard(")
    call = body.find(".ExecuteMethod(")
    assert guard != -1 and call != -1 and guard < call


def test_invoke_argument_count_mismatch_reports_expected_and_given(alpha, monkeypatch):
    body = {"ok": False, "error": "argument_count_mismatch", "path": "UI/Logic1",
            "method": "Go", "expected": 2, "given": 1}
    monkeypatch.setattr(core, "_bridge_http", _fake({"/bridge/node/invoke": (200, body)}))
    out = _invoke(alpha, args="1")
    assert out["ok"] is False and out["error"] == "argument_count_mismatch"
    assert out["expected"] == 2 and out["given"] == 1


def test_invoke_argument_count_checked_before_execute_method() -> None:
    body = _csharp_body("private string InvokeMethodInline(")
    mismatch = body.find("argument_count_mismatch")
    call = body.find(".ExecuteMethod(")
    assert mismatch != -1 and mismatch < call
    assert '\\"expected\\":' in body and '\\"given\\":' in body


def test_invoke_method_failed_carries_detail(alpha, monkeypatch):
    body = {"ok": False, "error": "method_failed", "path": "UI/Logic1",
            "method": "Go", "detail": "ERROR NullReferenceException in Go"}
    monkeypatch.setattr(core, "_bridge_http", _fake({"/bridge/node/invoke": (200, body)}))
    out = _invoke(alpha)
    assert out["ok"] is False and out["error"] == "method_failed"
    assert "NullReferenceException" in out["detail"]


def test_invoke_method_failed_covers_exception_and_studio_log() -> None:
    body = _csharp_body("private string InvokeMethodInline(")
    assert body.count("MethodFailedJson(") == 2          # catch + log error line
    assert "StudioLogMark()" in body and "StudioLogErrorSince(" in body
    assert body.find("StudioLogMark()") < body.find(".ExecuteMethod(")


def test_invoke_success_still_ok(alpha, monkeypatch):
    ok = {"ok": True, "path": "UI/Logic1", "method": "Go", "session": "root",
          "output_args": []}
    monkeypatch.setattr(core, "_bridge_http", _fake({"/bridge/node/invoke": (200, ok)}))
    assert _invoke(alpha)["ok"] is True


# ---- retype ----------------------------------------------------------------

_UNCONVERTIBLE = {"ok": False, "error": "value_unconvertible", "path": "Model/Flag",
                  "from": "String", "to": "Int32", "count": 1, "samples": ["true"],
                  "message": "1 value(s) cannot be converted"}


def test_retype_unconvertible_value_is_refused_with_samples(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake({"/bridge/node/retype": (200, _UNCONVERTIBLE)}, cap))
    out = core.bridge_retype(alpha, "Alpha", "Model/Flag", "Int32")
    assert out["ok"] is False
    assert out["error"] == "value_unconvertible" == out["code"]
    assert out["samples"] == ["true"]
    _m, path = next(c for c in cap if "/bridge/node/retype" in c[1])
    assert "allow_value_loss" not in path


def test_retype_allow_value_loss_forwards_flag_and_reports_reset(alpha, monkeypatch):
    cap: list = []
    ok = {"ok": True, "values_converted": 0, "values_reset": 1,
          "warning": "reset 1 value(s)"}
    monkeypatch.setattr(core, "_bridge_http",
                        _fake({"/bridge/node/retype": (200, ok)}, cap))
    out = core.bridge_retype(alpha, "Alpha", "Model/Flag", "Int32",
                             allow_value_loss=True)
    assert out["ok"] is True and out["values_reset"] == 1 and out["warning"]
    _m, path = next(c for c in cap if "/bridge/node/retype" in c[1])
    assert "allow_value_loss=true" in path


def test_retype_dry_run_reports_would_reset(alpha, monkeypatch):
    cap: list = []
    ok = {"ok": True, "dry_run": True, "would_retype": True, "would_reset": 2,
          "warning": "would reset 2 value(s)"}
    monkeypatch.setattr(core, "_bridge_http",
                        _fake({"/bridge/node/retype": (200, ok)}, cap))
    out = core.bridge_retype(alpha, "Alpha", "Model/Flag", "Int32",
                             dry_run=True, allow_value_loss=True)
    assert out["would_reset"] == 2
    _m, path = next(c for c in cap if "/bridge/node/retype" in c[1])
    assert "dry_run=true" in path and "allow_value_loss=true" in path


def test_apply_one_edit_retype_forwards_allow_value_loss(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake(
        {"/bridge/node/retype": (200, {"ok": True, "values_reset": 1})}, cap))
    core._apply_one_edit(alpha, "Alpha", {
        "op": "retype", "path": "Model/Flag", "datatype": "Int32",
        "allow_value_loss": True})
    _m, path = next(c for c in cap if "/bridge/node/retype" in c[1])
    assert "allow_value_loss=true" in path


def test_apply_one_edit_retype_unconvertible_fails_the_op(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http",
                        _fake({"/bridge/node/retype": (200, _UNCONVERTIBLE)}))
    with pytest.raises(core.BridgeWriteFailed, match="value_unconvertible"):
        core._apply_one_edit(alpha, "Alpha", {
            "op": "retype", "path": "Model/Flag", "datatype": "Int32"})


def test_validate_retype_op_rejects_non_bool_allow_value_loss():
    errs = core.validate_retype_op("Model/X", "Int32", None, "yes")
    assert [e["code"] for e in errs] == ["invalid_field"]
    assert core.validate_retype_op("Model/X", "Int32", None, True) == []


def test_unknown_op_fields_accepts_allow_value_loss():
    assert core.unknown_op_fields({
        "op": "retype", "path": "Model/X", "datatype": "Int32",
        "allow_value_loss": True}) == []


def test_bridge_retype_checks_convertibility_before_writing() -> None:
    body = _csharp_body("private string RetypeVariableInline(")
    refuse = body.find("value_unconvertible")
    write = body.find("v.Value = new UAValue(newVal)")
    assert refuse != -1 and write != -1 and refuse < write
    assert "allow_value_loss" in body
    assert "values_reset" in body and "would_reset" in body and "warning" in body
