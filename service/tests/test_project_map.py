"""Tests for core.get_project_map — one-call component map (bridge /bridge/map)."""
from __future__ import annotations

import pytest

from service import core

TREE = {
    "path": "", "budget_left": 12,
    "map": {"name": "NewProj", "type": "Project", "children": [
        {"name": "UI", "type": "Folder", "children": [
            {"name": "MainWindow", "type": "WindowType", "children": [
                {"name": "NavPanel", "type": "NavigationPanel", "children": [
                    {"name": "Panels", "coll": "NavigationPanelItem", "n": 3},
                ]},
            ]},
        ]},
        {"name": "Model", "type": "Folder", "n": 58},
        {"name": "Alarms", "type": "Folder", "children": [], "more": 4},
    ]},
}


@pytest.fixture(autouse=True)
def _bridge_up(monkeypatch):
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)


def test_map_outline_rendering(cfg: core.Config, monkeypatch) -> None:
    seen = {}
    def fake_get(cfg_, q):
        seen["q"] = q
        return 200, TREE
    monkeypatch.setattr(core, "_bridge_get_json", fake_get)
    out = core.get_project_map(cfg, "NewProj")
    lines = out["map"].splitlines()
    assert lines[0] == "NewProj (Project)"
    assert "  UI (Folder)" in lines
    assert "      NavPanel (NavigationPanel)" in lines
    # collection marker replaces the noise type; depth-limited count shown
    assert "        Panels {NavigationPanelItem}  (+3 inside)" in lines
    assert "  Model (Folder)  (+58 inside)" in lines
    # explicit truncation marker
    assert any("+4 more" in ln for ln in lines)
    assert out["truncated"] is False and out["source"] == "bridge"


def test_map_defaults_auto_mode_vs_explicit_depth(cfg: core.Config, monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(core, "_bridge_get_json",
                        lambda c, q: seen.setdefault("q", q) and (200, TREE) or (200, TREE))
    core.get_project_map(cfg, "P")                      # no depth -> bridge decides (auto)
    assert "mode=auto" in seen["q"] and "path=" not in seen["q"]
    seen.clear()
    core.get_project_map(cfg, "P", path="UI/MainWindow")  # scoped, no depth -> still auto
    assert "mode=auto" in seen["q"] and "path=UI/MainWindow" in seen["q"]
    seen.clear()
    core.get_project_map(cfg, "P", depth=2)             # explicit depth -> full walk
    assert "mode=detail" in seen["q"] and "depth=2" in seen["q"]


def test_map_overview_skip_rendering(cfg: core.Config, monkeypatch) -> None:
    tree = {"path": "", "mode": "overview", "budget_left": 5, "map": {
        "name": "Root", "type": "ProjectFolder", "children": [
            {"name": "Model", "type": "Folder", "children": [], "vars": 3},
            {"name": "MainWindow", "type": "WindowType", "n": 24},
        ]}}
    monkeypatch.setattr(core, "_bridge_get_json", lambda c, q: (200, tree))
    out = core.get_project_map(cfg, "P")
    assert out["mode"] == "overview"
    lines = out["map"].splitlines()
    assert "  Model (Folder)  (3 vars)" in lines
    assert "  MainWindow (WindowType)  (+24 inside)" in lines


def test_map_json_format_and_ids_passthrough(cfg: core.Config, monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(core, "_bridge_get_json",
                        lambda c, q: (seen.setdefault("q", q), (200, TREE))[1])
    out = core.get_project_map(cfg, "P", ids=True, fmt="json")
    assert "ids=1" in seen["q"]
    assert isinstance(out["map"], dict) and out["map"]["name"] == "NewProj"


def test_map_truncated_flag(cfg: core.Config, monkeypatch) -> None:
    exhausted = dict(TREE, budget_left=0)
    monkeypatch.setattr(core, "_bridge_get_json", lambda c, q: (200, exhausted))
    out = core.get_project_map(cfg, "P")
    assert out["truncated"] is True


def test_map_404_raises_node_not_found(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_get_json", lambda c, q: (404, {}))
    with pytest.raises(core.NodeNotFound):
        core.get_project_map(cfg, "P", path="UI/Ghost")


def test_map_deref_rendering(cfg: core.Config, monkeypatch) -> None:
    tree = {"path": "UI/MainWindow", "mode": "detail", "budget_left": 5, "map": {
        "name": "NavPanel", "type": "NavigationPanel", "children": [
            {"name": "Panel", "type": "NodePointer", "ref": "UI/Screens/ScreenA"},
            {"name": "Caption", "type": "UAVariable", "children": [
                {"name": "DynamicLink", "type": "DynamicLink", "ref": "../../Model/TextValue"}]},
        ]}}
    monkeypatch.setattr(core, "_bridge_get_json", lambda c, q: (200, tree))
    out = core.get_project_map(cfg, "P", path="UI/MainWindow")
    assert "  Panel (NodePointer)  -> UI/Screens/ScreenA" in out["map"]
    assert "    DynamicLink (DynamicLink)  -> ../../Model/TextValue" in out["map"]


def test_map_search_mode(cfg: core.Config, monkeypatch) -> None:
    seen = {}
    res = {"path": "", "mode": "search", "match": "Label",
           "matches": [{"path": "UI/Screens/ScreenA/HelloLabel", "type": "Label"},
                        {"path": "UI/Screens/ScreenB/T1", "type": "Label"}],
           "visited": 900, "hits_capped": False}
    monkeypatch.setattr(core, "_bridge_get_json", lambda c, q: (seen.setdefault("q", q), (200, res))[1])
    out = core.get_project_map(cfg, "P", match="Label")
    assert "match=Label" in seen["q"]
    assert out["mode"] == "search" and out["hit_count"] == 2
    assert out["map"].splitlines() == [
        "UI/Screens/ScreenA/HelloLabel (Label)",
        "UI/Screens/ScreenB/T1 (Label)"]
    # json format returns the structured matches
    out2 = core.get_project_map(cfg, "P", match="Label", fmt="json")
    assert out2["matches"][0]["path"] == "UI/Screens/ScreenA/HelloLabel"
