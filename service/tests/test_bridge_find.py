"""Tests for the v0.3 backlog §6 bridge-aware optix_find.

find_in_project is a disk file-scan gated by require_editors_closed, so it
hard-fails while Studio is open — exactly when the sibling bridge reads succeed.
When the bridge serves the project, find now searches the LIVE model instead.

All offline: core._bridge_http is monkeypatched with a small canned node tree.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from service import core
from service.tests.conftest import make_project


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


# A tiny live model: UI/Screens/{Screen1,Screen2} each with a Header label.
_TREE = {
    "UI": {"browse_name": "UI", "node_class": "Object", "dotnet_type": "Folder",
           "children": [{"browse_name": "Screens"}], "properties": []},
    "UI/Screens": {"browse_name": "Screens", "node_class": "Object",
                   "dotnet_type": "Folder",
                   "children": [{"browse_name": "Screen1"}, {"browse_name": "Screen2"}],
                   "properties": []},
    "UI/Screens/Screen1": {"browse_name": "Screen1", "node_class": "Object",
                           "dotnet_type": "Screen",
                           "children": [{"browse_name": "Header"}], "properties": []},
    "UI/Screens/Screen1/Header": {"browse_name": "Header", "node_class": "Variable",
                                  "dotnet_type": "Label", "children": [],
                                  "properties": [{"name": "Text", "datatype": "LocalizedText",
                                                  "value": "Welcome banner"}]},
    "UI/Screens/Screen2": {"browse_name": "Screen2", "node_class": "Object",
                           "dotnet_type": "Screen",
                           "children": [{"browse_name": "Header"}], "properties": []},
    "UI/Screens/Screen2/Header": {"browse_name": "Header", "node_class": "Variable",
                                  "dotnet_type": "Label", "children": [],
                                  "properties": [{"name": "Text", "datatype": "LocalizedText",
                                                  "value": "Settings page"}]},
}


def _bridge_serving(project: str = "Alpha", tree: dict | None = None,
                    *, unreachable: bool = False):
    """Fake core._bridge_http: /bridge/health healthy + /bridge/nodes from `tree`."""
    tree = _TREE if tree is None else tree

    def fake(cfg, path, timeout=5.0):
        if unreachable:
            raise core.BridgeUnavailable("bridge unreachable at test")
        if path.startswith("/bridge/health"):
            return 200, json.dumps(
                {"bridge_version": "0.9.1", "project": project, "model_loaded": True}
            ).encode()
        if path.startswith("/bridge/nodes"):
            q = parse_qs(urlparse(path).query)
            node = q.get("path", [""])[0]
            if node in tree:
                return 200, json.dumps(tree[node]).encode()
            return 404, b'{"error":{"code":"not_found"}}'
        return 404, b"{}"
    return fake


@pytest.fixture
def alpha(projects_root):
    return make_project(projects_root, "Alpha")


def test_find_routes_to_bridge_when_studio_open(cfg, alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _bridge_serving())
    out = core.find_in_project(cfg, "Alpha", "Header")
    assert out["source"] == "bridge"
    paths = {m["path"] for m in out["matches"]}
    assert "UI/Screens/Screen1/Header" in paths
    assert "UI/Screens/Screen2/Header" in paths
    assert all(m["matched_on"] == "name" for m in out["matches"])


def test_find_matches_property_value(cfg, alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _bridge_serving())
    out = core.find_in_project(cfg, "Alpha", "Welcome")
    assert out["source"] == "bridge"
    assert out["match_count"] == 1
    m = out["matches"][0]
    assert m["path"] == "UI/Screens/Screen1/Header"
    assert m["matched_on"] == "property_value"


def test_find_case_insensitive_by_default(cfg, alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _bridge_serving())
    out = core.find_in_project(cfg, "Alpha", "header")
    assert out["match_count"] == 2


def test_find_case_sensitive_no_match(cfg, alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _bridge_serving())
    out = core.find_in_project(cfg, "Alpha", "header", case_sensitive=True)
    assert out["match_count"] == 0


def test_find_max_results_truncates(cfg, alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _bridge_serving())
    out = core.find_in_project(cfg, "Alpha", "Header", max_results=1)
    assert out["match_count"] == 1
    assert out["truncated"] is True


def test_find_falls_back_to_disk_when_bridge_down(cfg, alpha, monkeypatch):
    # bridge unreachable -> _use_bridge_for False -> disk scan (Studio assumed closed)
    monkeypatch.setattr(core, "_bridge_http", _bridge_serving(unreachable=True))
    (alpha / "Nodes").mkdir(exist_ok=True)
    (alpha / "Nodes" / "UI.yaml").write_text("Header:\n  Text: on disk\n")
    out = core.find_in_project(cfg, "Alpha", "Header")
    assert out.get("source") != "bridge"
    assert "files_scanned" in out  # the disk-scan result shape
    assert out["match_count"] >= 1


def test_find_bridge_only_serves_matching_project(cfg, alpha, monkeypatch):
    # bridge serving a DIFFERENT project must not answer for this one -> disk path
    monkeypatch.setattr(core, "_bridge_http", _bridge_serving(project="OtherProj"))
    (alpha / "Nodes").mkdir(exist_ok=True)
    (alpha / "Nodes" / "UI.yaml").write_text("Header:\n  Text: on disk\n")
    out = core.find_in_project(cfg, "Alpha", "Header")
    assert out.get("source") != "bridge"
