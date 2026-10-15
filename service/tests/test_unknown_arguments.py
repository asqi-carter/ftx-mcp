"""Unknown tool arguments are refused (MCP + HTTP) and import_tags has a dry_run.

PRD 0052 A (#81). FastMCP / pydantic used to drop an unknown argument silently,
so a misspelled `dry_run` ran the real import. Black-box: a fake bridge records
every request, and a refused call must record none.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from service import core
from service.http_app import make_app
from service.mcp_app import make_mcp
from service.tests.conftest import make_project

FIXTURE = Path(__file__).parent / "fixtures" / "import_tags_sample.L5X"
STATION = "CommDrivers/RAEtherNet_IPDriver1/Station1"


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


def _structured(result):
    """The dict a FastMCP call_tool answer carries (tuple or content list)."""
    if isinstance(result, tuple):
        return result[1]
    return json.loads("".join(getattr(b, "text", "") for b in result))


def _call(mcp, tool, args):
    return _structured(asyncio.run(mcp.call_tool(tool, args)))


def _recording_bridge(monkeypatch, station_tags=()):
    """Fake core._bridge_http that records (method, path). The station's Tags
    subtree holds one tag node per symbol in ``station_tags``."""
    calls: list = []
    base = f"{STATION}/Tags"

    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        calls.append((method, path))
        if path.startswith("/bridge/health"):
            return 200, json.dumps({"bridge_version": "0.5.0", "project": "Alpha",
                                    "model_loaded": True}).encode()
        if path.startswith("/bridge/nodes"):
            node = unquote(path.split("path=", 1)[1])
            if node == base:
                kids = [{"browse_name": f"T{i}", "node_class": "Object"}
                        for i, _ in enumerate(station_tags)]
                return 200, json.dumps({"path": node, "children": kids}).encode()
            for i, sym in enumerate(station_tags):
                if node == f"{base}/T{i}":
                    return 200, json.dumps({"path": node, "children": [
                        {"browse_name": "SymbolName", "node_class": "Variable"}]}).encode()
                if node == f"{base}/T{i}/SymbolName":
                    return 200, json.dumps({"path": node, "node_class": "Variable",
                                            "value": sym}).encode()
            return 404, b'{"error":{"code":"node_not_found"}}'
        return 200, b'{"ok":true}'

    monkeypatch.setattr(core, "_bridge_http", fake)
    return calls


@pytest.fixture
def alpha(cfg, projects_root):
    proj = make_project(projects_root, "Alpha")
    (proj / "a.L5X").write_bytes(FIXTURE.read_bytes())
    return cfg


def _writes(calls):
    return [c for c in calls if c[0] == "POST"]


# -- MCP: unknown_argument ---------------------------------------------------

@pytest.mark.parametrize("tool,args", [
    ("optix_bridge_edit", {"ops": [], "dry_runn": True}),
    ("optix_save", {"projct": "Alpha"}),
    ("optix_bridge_import_tags", {"station": STATION, "mode": "offline", "file": "a.L5X",
                                  "tags": ["Motor1"], "dry_runn": True}),
])
def test_unknown_kwarg_returns_unknown_argument_on_three_tools(alpha, monkeypatch, tool, args):
    monkeypatch.setenv("FTXMCP_BRIDGE_PRIMITIVES", "1")
    calls = _recording_bridge(monkeypatch)
    out = _call(make_mcp(alpha), tool, {"project": "Alpha", **args})
    bad = [k for k in args if k.endswith(("dry_runn", "projct"))]
    assert out["ok"] is False and out["error"] == "unknown_argument"
    assert out["unknown"] == bad
    assert "project" in out["accepted"] and bad[0] not in out["accepted"]
    assert out["hint"] and "did you mean" in out["hint"]
    assert calls == [], "a refused call must not reach the bridge"


def test_known_arguments_are_not_refused(alpha, monkeypatch):
    _recording_bridge(monkeypatch)
    out = _call(make_mcp(alpha), "optix_status", {"action": "health"})
    assert out.get("error") != "unknown_argument"


# -- HTTP: unknown_argument --------------------------------------------------

def test_http_route_unknown_body_key_is_refused(alpha, monkeypatch):
    seen: list = []
    monkeypatch.setattr(core, "bridge_create_widget",
                        lambda *a, **k: seen.append(a) or {"ok": True})
    client = TestClient(make_app(alpha))
    r = client.post("/projects/Alpha/bridge/widget",
                    json={"screen": "UI/Main", "name": "L1", "widget_typo": "Label"})
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False and body["error"] == "unknown_argument"
    assert body["unknown"] == ["widget_typo"]
    assert body["accepted"] == ["name", "screen", "widget_type"]
    assert body["hint"]
    assert seen == [], "the route function must not run"


def test_http_route_nested_and_valid_bodies(alpha, monkeypatch):
    client = TestClient(make_app(alpha))
    r = client.post("/projects/Alpha/deploy",
                    json={"edits": [{"path": "a.txt", "content": "x", "bogus": 1}]})
    assert r.status_code == 422 and r.json()["error"] == "unknown_argument"
    assert r.json()["unknown"] == ["bogus"]
    monkeypatch.setattr(core, "bridge_create_widget", lambda *a, **k: {"ok": True})
    r = client.post("/projects/Alpha/bridge/widget",
                    json={"screen": "UI/Main", "name": "L1"})
    assert r.status_code == 200


# -- import_tags dry_run -----------------------------------------------------

def test_import_tags_dry_run_reports_plan_without_bridge_import(alpha, monkeypatch):
    calls = _recording_bridge(monkeypatch, station_tags=["Motor1"])
    out = core.bridge_import_tags(alpha, "Alpha", STATION, "offline", ["Motor1", "Ghost"],
                                  file="a.L5X", dry_run=True)
    assert out["ok"] is True and out["dry_run"] is True
    assert out["would_add"] == []   # Motor1 is on the station; Motor10 not requested
    assert out["would_remove"] == []
    assert [s["symbol"] for s in out["would_skip"]] == ["Ghost"]
    assert not any("import-tags" in p for _, p in calls)
    assert _writes(calls) == []


def test_import_tags_dry_run_reports_would_remove(alpha, monkeypatch):
    calls = _recording_bridge(monkeypatch, station_tags=["Motor1", "Pump9"])
    out = core.bridge_import_tags(alpha, "Alpha", STATION, "offline", ["Motor1"],
                                  file="a.L5X", dry_run=True)
    assert out["would_remove"] == ["Pump9"]
    assert out["would_refuse"] == "would_remove_tags" and out["hint"]
    out = core.bridge_import_tags(alpha, "Alpha", STATION, "offline", ["Motor1"],
                                  file="a.L5X", replace=True, dry_run=True)
    assert out["would_remove"] == ["Pump9"] and "would_refuse" not in out
    assert not any("import-tags" in p for _, p in calls)


def test_import_tags_tool_accepts_dry_run(alpha, monkeypatch):
    monkeypatch.setenv("FTXMCP_BRIDGE_PRIMITIVES", "1")
    calls = _recording_bridge(monkeypatch)
    out = _call(make_mcp(alpha), "optix_bridge_import_tags", {
        "project": "Alpha", "station": STATION, "mode": "offline", "file": "a.L5X",
        "tags": ["Motor1"], "dry_run": True})
    assert out["dry_run"] is True and "Motor1" in out["would_add"]
    assert set(out) >= {"would_add", "would_remove", "would_skip"}
    assert not any("import-tags" in p for _, p in calls)


def test_import_tags_batch_op_accepts_dry_run(alpha, monkeypatch):
    calls = _recording_bridge(monkeypatch)
    res = core._apply_one_edit(alpha, "Alpha", {
        "op": "import_tags", "station": STATION, "mode": "offline", "file": "a.L5X",
        "tags": ["Motor1"], "dry_run": True})
    assert res["dry_run"] is True and "Motor1" in res["would_add"]
    assert not any("import-tags" in p for _, p in calls)
    assert "dry_run" not in core.unknown_op_fields(
        {"op": "import_tags", "station": STATION, "mode": "offline", "tags": ["x"],
         "dry_run": True})


def test_import_tags_dry_run_must_be_bool(alpha, monkeypatch):
    calls = _recording_bridge(monkeypatch)
    out = core.bridge_import_tags(alpha, "Alpha", STATION, "offline", ["Motor1"],
                                  file="a.L5X", dry_run="yes")
    assert out["ok"] is False and out["code"] == "invalid_dry_run"
    assert calls == []
