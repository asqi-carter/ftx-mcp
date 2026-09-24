"""Issue #4: /bridge/node/invoke runs under a root Optix session.

Root cause (2026-09-24): ExecuteMethod on the bridge's HTTP thread had no Optix
session, and native code dereferencing it killed FTOptixStudio.exe on ANY
method. The bridge now wraps the call in Sessions.ImpersonateRootTemporary and
advertises `invoke_session` in /bridge/health; the service refuses invoke
against a bridge that doesn't, because an older build still runs session-less.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from service import core
from service.mcp_app import make_mcp
from service.tests.conftest import make_project

REPO = Path(__file__).resolve().parents[2]
BRIDGE_CS = REPO / "studio-bridge" / "StudioMCPBridge.cs"

_HEALTH = {"bridge_version": "1.0.8", "project": "Alpha", "model_loaded": True}


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


@pytest.fixture
def alpha(cfg, projects_root):
    make_project(projects_root, "Alpha")
    return cfg


def _fake_bridge(routes, capture, *, session=True):
    health = {**_HEALTH, **({"invoke_session": "root"} if session else {})}
    merged = {"/bridge/health": (200, health), **routes}

    def fake(cfg, path, method="GET", timeout=5.0, **_kwargs):
        capture.append((method, path, timeout))
        for prefix, (status, body) in merged.items():
            if path.startswith(prefix):
                return status, json.dumps(body).encode()
        return 404, b'{"error":{"code":"not_found"}}'

    return fake


_OK = {"/bridge/node/invoke": (200, {"ok": True, "path": "UI/Logic1", "method": "Go",
                                     "session": "root", "output_args": []})}


def test_invoke_runs_by_default_with_long_timeout(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(_OK, cap))
    out = core.bridge_invoke_method(alpha, "Alpha", "UI/Logic1", "Go", args="1,2")
    assert out["ok"] is True
    method, path, timeout = next(c for c in cap if "/bridge/node/invoke" in c[1])
    assert method == "POST" and timeout == 60.0
    assert "method=Go" in path and "args=1%2C2" in path
    assert "unsafe" not in path


def test_legacy_unsafe_flag_is_accepted_and_ignored(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(_OK, cap))
    core.bridge_invoke_method(alpha, "Alpha", "UI/Logic1", "Go", unsafe=True)
    _m, path, _t = next(c for c in cap if "/bridge/node/invoke" in c[1])
    assert "unsafe" not in path


@pytest.mark.parametrize("version", ["1.0.7", "1.0.8+sfmt", "1.0.8"])
def test_bridge_without_invoke_session_is_refused_before_invoking(alpha, monkeypatch, version):
    """A pre-fix build runs ExecuteMethod session-less and kills Studio — even
    one reporting 1.0.8 (a pre-fix template build reported '1.0.8+sfmt')."""
    cap: list = []
    monkeypatch.setitem(_HEALTH, "bridge_version", version)
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(_OK, cap, session=False))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_invoke_method(alpha, "Alpha", "UI/Logic1", "Go")
    msg = str(e.value)
    assert "invoke_unsupported_bridge" in msg and version in msg
    assert "optix_execute_method" in msg
    assert not any("/bridge/node/invoke" in p for _, p, _ in cap)


def test_bridge_side_error_passes_through(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/invoke": (200, {"ok": False, "error": {
        "code": "execution_failed", "message": "MissingMethodException: Nope"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, cap))
    with pytest.raises(core.BridgeWriteFailed, match="MissingMethodException"):
        core.bridge_invoke_method(alpha, "Alpha", "UI/Logic1", "Nope")


def test_mcp_tool_keeps_legacy_unsafe_param(cfg) -> None:
    tool = next(t for t in make_mcp(cfg)._tool_manager.list_tools()
                if t.name == "optix_bridge_invoke_method")
    assert tool.parameters["properties"]["unsafe"].get("default") is False
    assert "invoke_unsupported_bridge" in (tool.description or "")


def test_bridge_invokes_only_inside_root_impersonation() -> None:
    text = BRIDGE_CS.read_text(encoding="utf-8")
    body = text[text.index("private string InvokeMethodInline("):]
    body = body[:body.index("\n    }\n")]
    scope = body.find("Sessions.ImpersonateRootTemporary()")
    call = body.find(".ExecuteMethod(")
    assert scope != -1 and call != -1 and scope < call
    assert body.count(".ExecuteMethod(") == 1
    assert "execution_unsafe" not in body
    assert "CurrentSessionInfo" not in body


def test_health_advertises_invoke_session() -> None:
    text = BRIDGE_CS.read_text(encoding="utf-8")
    health = text[text.index("private string HealthJson()"):]
    health = health[:health.index("\n    }\n")]
    assert re.search(r'invoke_session\\\\?":\\\\?"root', health)
