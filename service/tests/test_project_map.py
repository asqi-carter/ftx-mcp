"""Tests for core.get_project_map — one-call component map (bridge /bridge/map)."""
from __future__ import annotations

import pytest

from service import core


def _inner(wrapped: str) -> str:
    """Strip the U11 `<untrusted source="get_project_map"> ... </untrusted>`
    wrapper so the outline can be asserted line-by-line as before."""
    assert wrapped.startswith('<untrusted source="get_project_map">'), wrapped
    assert wrapped.endswith("</untrusted>"), wrapped
    return wrapped[wrapped.index(">") + 1 : -len("</untrusted>")]


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
    # get_project_map now resolves its OWN bridge routing via
    # _require_bridge_for (multi-instance, v1.0.7) instead of the old
    # guard-then-call-cfg-unchanged pattern _use_bridge_for used to gate —
    # patching _use_bridge_for alone no longer has any effect on it. Patch
    # _require_bridge_for to pass `cfg` straight through unchanged (same
    # bypass semantics as the old True-return: "a bridge is up, serving this
    # project", using the SAME cfg the test's _bridge_get_json fakes expect).
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_require_bridge_for", lambda cfg, project: cfg)


def test_map_outline_rendering(cfg: core.Config, monkeypatch) -> None:
    seen = {}
    def fake_get(cfg_, q):
        seen["q"] = q
        return 200, TREE
    monkeypatch.setattr(core, "_bridge_get_json", fake_get)
    out = core.get_project_map(cfg, "NewProj")
    # the whole outline is wrapped exactly once as untrusted (U11)
    assert out["map"].count('<untrusted source="get_project_map">') == 1
    lines = _inner(out["map"]).splitlines()
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
    lines = _inner(out["map"]).splitlines()
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
    assert _inner(out["map"]).splitlines() == [
        "UI/Screens/ScreenA/HelloLabel (Label)",
        "UI/Screens/ScreenB/T1 (Label)"]
    # json format returns the structured matches
    out2 = core.get_project_map(cfg, "P", match="Label", fmt="json")
    assert out2["matches"][0]["path"] == "UI/Screens/ScreenA/HelloLabel"


# ---- PRD 0023 #2: target_path + dangling-link diagnostic ---------------------

def test_map_prints_target_path_for_dynamic_link_sources(cfg: core.Config, monkeypatch) -> None:
    tree = {"path": "UI/Lbl", "mode": "detail", "budget_left": 5, "map": {
        "name": "Fmt", "type": "StringFormatter", "children": [
            {"name": "Source0", "type": "UAVariable", "id": "ns=2;g=AAAA",
             "target_path": "Model/ZzzLinkA"},
            {"name": "Source1", "type": "UAVariable", "target_path": None,
             "target_missing": True},
        ]}}
    monkeypatch.setattr(core, "_bridge_get_json", lambda c, q: (200, tree))
    out = core.get_project_map(cfg, "P", path="UI/Lbl", ids=True)
    lines = _inner(out["map"]).splitlines()
    assert "  Source0 (UAVariable)  -> Model/ZzzLinkA  [ns=2;g=AAAA]" in lines
    assert "  Source1 (UAVariable)  [DANGLING LINK: target missing]" in lines
    assert out["diagnostics"] == [core._dangling_link_diag("UI/Lbl/Source1")]
    assert out["diagnostics"][0]["code"] == "dangling_link"


def test_map_without_dangling_links_has_no_diagnostics(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_get_json", lambda c, q: (200, TREE))
    assert "diagnostics" not in core.get_project_map(cfg, "P")


def test_describe_prefers_target_path_over_guid_and_flags_dangling(
        cfg: core.Config, monkeypatch) -> None:
    node = {"path": "UI/Lbl/Fmt", "properties": [
        {"name": "Source0", "datatype": "NodeId", "value": "{NodeId:ns=2;g=AAAA}",
         "target_path": "Model/ZzzLinkA"},
        {"name": "Source1", "datatype": "NodeId", "value": "{NodeId:ns=2;g=BBBB}",
         "target_path": None, "target_missing": True},
        {"name": "Format", "datatype": "String", "value": "{0}"},
    ]}
    monkeypatch.setattr(core, "_require_bridge_for", lambda c, p: c)
    monkeypatch.setattr(core, "_bridge_get_json", lambda c, q: (200, node))
    out = core.describe_node(cfg, "P", "UI/Lbl/Fmt")
    vals = {p["name"]: p["value"] for p in out["properties"]}
    assert "Model/ZzzLinkA" in vals["Source0"] and "AAAA" not in vals["Source0"]
    assert "g=BBBB" in vals["Source1"]            # GUID only as fallback
    assert "{0}" in vals["Format"]
    assert [d["path"] for d in out["diagnostics"]] == ["UI/Lbl/Fmt/Source1"]
    assert out["diagnostics"][0]["code"] == "dangling_link"


def test_map_and_describe_via_fake_bridge_http_prefer_target_path(
        cfg: core.Config, monkeypatch) -> None:
    """End-to-end through the JSON decode layer (fake `_bridge_http`): map and
    describe print target_path instead of the GUID, and target_missing is
    surfaced as a dangling_link diagnostic on both."""
    import json

    map_body = {"path": "UI/Lbl", "mode": "detail", "budget_left": 5, "map": {
        "name": "Fmt", "type": "StringFormatter", "children": [
            {"name": "Source0", "type": "UAVariable", "target_path": "Model/A"},
            {"name": "Source1", "type": "UAVariable", "target_path": None,
             "target_missing": True}]}}
    node_body = {"path": "UI/Lbl/Fmt", "properties": [
        {"name": "Source0", "datatype": "NodeId", "value": "{NodeId:ns=2;g=AAAA}",
         "target_path": "Model/A"},
        {"name": "Source1", "datatype": "NodeId", "value": "{NodeId:ns=2;g=BBBB}",
         "target_path": None, "target_missing": True}]}

    def fake_http(c, path, method="GET", timeout=5.0, **_kw):
        body = map_body if "/bridge/map" in path else node_body
        return 200, json.dumps(body).encode()

    monkeypatch.setattr(core, "_bridge_http", fake_http)
    monkeypatch.setattr(core, "_require_bridge_for", lambda c, p: c)

    m = core.get_project_map(cfg, "P", path="UI/Lbl")
    assert "Source0 (UAVariable)  -> Model/A" in _inner(m["map"])
    assert [d["code"] for d in m["diagnostics"]] == ["dangling_link"]

    d = core.describe_node(cfg, "P", "UI/Lbl/Fmt")
    vals = {p["name"]: p["value"] for p in d["properties"]}
    assert "Model/A" in vals["Source0"] and "AAAA" not in vals["Source0"]
    assert [x["code"] for x in d["diagnostics"]] == ["dangling_link"]
