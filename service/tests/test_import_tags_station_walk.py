"""import_tags station walk sees Variable tags; dry_run reports the real plan.

PRD 0054 A (#88). RA EtherNet/IP tags are ``Variable`` nodes (dotnet_type
TagStructure) nested under ``Tags/Controller Tags/...``. The fake bridge below
serves a describe tree of such nodes (Variables nested two levels deep), and
optionally the ``GET /bridge/station/tags`` route that returns the bridge's
``ImportTagsSnapshot`` as ``[{path, symbol}]``.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from service import core
from service.tests.conftest import make_project

FIXTURE = Path(__file__).parent / "fixtures" / "import_tags_sample.L5X"  # Motor1, Motor10
STATION = "CommDrivers/RAEtherNet_IPDriver1/Station1"
BASE = f"{STATION}/Tags"


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


@pytest.fixture
def alpha(cfg, projects_root):
    proj = make_project(projects_root, "Alpha")
    (proj / "a.L5X").write_bytes(FIXTURE.read_bytes())
    return cfg


def _tree(symbols_by_path: dict[str, str]) -> dict[str, dict]:
    """Describe tree: Tags/Controller Tags (Objects) -> Variable tag nodes, each
    with a SymbolName Variable child. ``symbols_by_path`` maps the tag's path
    relative to Tags to its symbol; intermediate nodes are Variables too, so the
    tags sit two Variable levels below ``Controller Tags``."""
    nodes: dict[str, dict] = {BASE: {"children": [{"browse_name": "Controller Tags",
                                                    "node_class": "Object"}]}}
    for rel, sym in symbols_by_path.items():
        parts = ["Controller Tags"] + rel.split("/")
        for i in range(len(parts)):
            path = f"{BASE}/" + "/".join(parts[:i + 1])
            kids = nodes.setdefault(path, {"children": []})["children"]
            if i + 1 < len(parts):
                nxt = parts[i + 1]
                if not any(k["browse_name"] == nxt for k in kids):
                    kids.append({"browse_name": nxt, "node_class": "Variable"})
            else:
                kids.append({"browse_name": "SymbolName", "node_class": "Variable"})
                nodes[f"{path}/SymbolName"] = {"node_class": "Variable", "value": sym,
                                               "children": []}
    # Controller Tags itself must list its first-level children
    root = nodes[f"{BASE}/Controller Tags"]["children"] if f"{BASE}/Controller Tags" in nodes else []
    nodes[f"{BASE}/Controller Tags"] = {"children": root}
    return nodes


# T0001 is one Variable level down, T0002 two Variable levels down
PRESENT = {"T0001": "T0001", "Grp/T0002": "T0002"}


def _fake(monkeypatch, nodes, route=True, calls=None):
    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        if calls is not None:
            calls.append((method, path))
        if path.startswith("/bridge/health"):
            return 200, json.dumps({"bridge_version": "0.5.0", "project": "Alpha",
                                    "model_loaded": True}).encode()
        if path.startswith("/bridge/station/tags"):
            if not route:
                return 404, b'{"error":{"code":"not_found"}}'
            out = [{"path": p.rsplit("/SymbolName", 1)[0], "symbol": n["value"]}
                   for p, n in nodes.items() if p.endswith("/SymbolName")]
            return 200, json.dumps(out).encode()
        if path.startswith("/bridge/nodes"):
            node = unquote(path.split("path=", 1)[1])
            if node in nodes:
                return 200, json.dumps({"path": node, **nodes[node]}).encode()
            return 404, b'{"error":{"code":"node_not_found"}}'
        return 200, b'{"ok":true}'
    monkeypatch.setattr(core, "_bridge_http", fake)


@pytest.mark.parametrize("route", [True, False])
def test_station_walk_collects_variable_tags_nested_two_levels(alpha, monkeypatch, route):
    _fake(monkeypatch, _tree(PRESENT), route=route)
    assert sorted(core.import_tags_station_symbols(alpha, STATION)) == ["T0001", "T0002"]


def test_station_walk_uses_the_bridge_route_when_present(alpha, monkeypatch):
    calls: list = []
    _fake(monkeypatch, _tree(PRESENT), route=True, calls=calls)
    core.import_tags_station_symbols(alpha, STATION)
    assert any(p.startswith("/bridge/station/tags?station=") for _, p in calls)
    assert not any(p.startswith("/bridge/nodes") for _, p in calls)


def test_station_walk_without_tags_child_is_empty(alpha, monkeypatch):
    _fake(monkeypatch, {}, route=False)
    assert core.import_tags_station_symbols(alpha, STATION) == []


@pytest.mark.parametrize("route", [True, False])
def test_dry_run_merge_reports_would_add_would_remove_already_present(alpha, monkeypatch, route):
    present = {**PRESENT, "Motor1": "Motor1"}
    _fake(monkeypatch, _tree(present), route=route)
    out = core.bridge_import_tags(alpha, "Alpha", STATION, "offline", ["Motor1", "Motor10"],
                                  file="a.L5X", dry_run=True)
    assert out["ok"] is True and out["dry_run"] is True and out["station_read"] is True
    assert out["would_add"] == ["Motor10"]          # Motor1 is already on the station
    assert out["already_present"] == ["Motor1"]
    assert out["would_remove"] == ["T0001", "T0002"]  # the file lacks them
    assert out["would_refuse"] == "would_remove_tags" and out["hint"]


def test_dry_run_replace_lists_present_tags_not_requested(alpha, monkeypatch):
    _fake(monkeypatch, _tree({**PRESENT, "Motor1": "Motor1"}))
    out = core.bridge_import_tags(alpha, "Alpha", STATION, "offline", ["Motor10"],
                                  file="a.L5X", replace=True, dry_run=True)
    assert out["would_add"] == ["Motor10"]
    assert out["already_present"] == []
    assert out["would_remove"] == ["Motor1", "T0001", "T0002"]
    assert "would_refuse" not in out


def test_merge_guard_refuses_when_file_lacks_a_present_variable_tag(alpha, monkeypatch):
    """The station holds T0001/T0002 (Variables); the file has only Motor1/Motor10."""
    calls: list = []
    _fake(monkeypatch, _tree(PRESENT), calls=calls)
    out = core.bridge_import_tags(alpha, "Alpha", STATION, "offline", ["Motor1"],
                                  file="a.L5X", dry_run=True)
    assert out["would_remove"] == ["T0001", "T0002"]
    assert out["would_refuse"] == "would_remove_tags"
    assert out["would_add"] == ["Motor1"]
    assert not any("import-tags" in p for _, p in calls)
    # a file that still contains them is not refused
    out = core.import_tags_dry_run(alpha, "Alpha", STATION, "offline", ["Motor1"],
                                   ["Motor1", "T0001", "T0002"])
    assert out["would_remove"] == [] and "would_refuse" not in out
    assert out["already_present"] == ["T0001", "T0002"] and out["would_add"] == ["Motor1"]


def test_csharp_route_returns_snapshot_pairs():
    from service.tests.test_bridge_writes import _bridge_cs_text, _cs_region
    text = _bridge_cs_text()
    assert 'StartsWith("GET /bridge/station/tags")' in text
    fn = _cs_region(text, "private string StationTagsJson(", "// The station's \"Tags\" child")
    assert "ImportTagsSnapshot(" in fn and '\\"path\\"' in fn and '\\"symbol\\"' in fn
