"""Tests for the v0.4 design-time read-bridge routing (service.core).

All offline: the bridge HTTP client (core._bridge_http) is monkeypatched, so
these validate mode-detection / attribution / fallback without a live Studio.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from service import core
from service.tests.conftest import make_project


@pytest.fixture(autouse=True)
def _clear_bridge_cache() -> None:
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


def _bridge(routes: dict, *, unreachable: bool = False):
    """Fake core._bridge_http: route path-prefix -> (status, dict|bytes)."""
    def fake(cfg: core.Config, path: str, timeout: float = 5.0, **_kwargs):
        if unreachable:
            raise core.BridgeUnavailable("bridge unreachable at test")
        for prefix, (status, body) in routes.items():
            if path.startswith(prefix):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                return status, raw
        return 404, b'{"error":{"code":"not_found"}}'
    return fake


_HEALTHY = {"/bridge/health": (200, {"bridge_version": "0.4.0-phase0",
                                     "project": "Alpha", "model_loaded": True})}


# ---- _use_bridge_for routing ------------------------------------------

def test_use_bridge_for_project_under_projects_root(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "_bridge_http", _bridge(_HEALTHY))  # serves "Alpha"
    assert core._use_bridge_for(cfg, "Alpha") is True


def test_use_bridge_for_project_opened_outside_projects_root(
    cfg: core.Config, monkeypatch
) -> None:
    """Studio can open a project from ANYWHERE (e.g. the Desktop); the bridge
    serves Project.Current regardless of on-disk location. Route to the bridge
    by NAME even when the project isn't under projects_root (resolve_project
    would raise) — otherwise every live-model tool is wrongly refused."""
    routes = {"/bridge/health": (200, {"bridge_version": "1.0.1",
                                        "project": "DesktopProj", "model_loaded": True})}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    # Not on disk under projects_root, but the name matches what the bridge serves.
    assert core._use_bridge_for(cfg, "DesktopProj") is True
    # A different served project must still be refused.
    assert core._use_bridge_for(cfg, "OtherProj") is False
    # Invalid names never match, even via the name fallback.
    assert core._use_bridge_for(cfg, "a/b") is False
    assert core._use_bridge_for(cfg, "../x") is False


# ---- bridge_state -----------------------------------------------------

def test_bridge_state_available(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_http", _bridge(_HEALTHY))
    st = core.bridge_state(cfg)
    assert st["available"] is True
    assert st["project"] == "Alpha"
    assert st["bridge_version"] == "0.4.0-phase0"


def test_bridge_state_disabled(cfg: core.Config, monkeypatch) -> None:
    cfg = dataclasses.replace(cfg, bridge_enabled=False)
    # even if the listener would answer, disabled short-circuits
    monkeypatch.setattr(core, "_bridge_http", _bridge(_HEALTHY))
    st = core.bridge_state(cfg)
    assert st["available"] is False
    assert st["reason"] == "disabled"


def test_bridge_state_model_not_loaded(cfg: core.Config, monkeypatch) -> None:
    routes = {"/bridge/health": (200, {"project": "Alpha", "model_loaded": False})}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    assert core.bridge_state(cfg)["available"] is False


def test_bridge_state_unreachable(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_http", _bridge({}, unreachable=True))
    st = core.bridge_state(cfg)
    assert st["available"] is False
    assert "unreachable" in st["reason"]


def test_bridge_state_retries_transient_block(cfg: core.Config, monkeypatch) -> None:
    """A transient transport failure (listener briefly blocked by heavy designer
    work) is retried, not cached as down (battle-test 2026-07-16)."""
    calls = {"n": 0}

    def fake(cfg_, path, timeout=5.0, **_kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise core.BridgeUnavailable("transient listener block")
        return 200, json.dumps({"project": "Alpha", "bridge_version": "x", "model_loaded": True}).encode()

    monkeypatch.setattr(core, "_bridge_http", fake)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    st = core.bridge_state(cfg)
    assert st["available"] is True
    assert calls["n"] == 3  # two failures retried, third succeeded


def test_bridge_state_not_ready_does_not_retry(cfg: core.Config, monkeypatch) -> None:
    """A well-formed HTTP response (even model_loaded=False) means the listener is
    up -> decide immediately, do NOT burn retries."""
    calls = {"n": 0}

    def fake(cfg_, path, timeout=5.0, **_kwargs):
        calls["n"] += 1
        return 200, json.dumps({"project": "Alpha", "model_loaded": False}).encode()

    monkeypatch.setattr(core, "_bridge_http", fake)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    assert core.bridge_state(cfg)["available"] is False
    assert calls["n"] == 1


def test_bridge_state_cached(cfg: core.Config, monkeypatch) -> None:
    calls = {"n": 0}

    def fake(cfg_, path, timeout=5.0, **_kwargs):
        calls["n"] += 1
        return 200, json.dumps({"project": "Alpha", "model_loaded": True}).encode()

    monkeypatch.setattr(core, "_bridge_http", fake)
    core.bridge_state(cfg)
    core.bridge_state(cfg)
    assert calls["n"] == 1  # second read served from the ~2s cache


def test_default_project_from_bridge(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_http", _bridge(_HEALTHY))
    assert core.default_project(cfg) == "Alpha"


def test_default_project_none_when_bridge_down(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_http", _bridge({}, unreachable=True))
    assert core.default_project(cfg) is None


# ---- _use_bridge_for (attribution) ------------------------------------

def test_use_bridge_for_matching_project(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "_bridge_http", _bridge(_HEALTHY))
    assert core._use_bridge_for(cfg, "Alpha") is True


def test_use_bridge_for_different_project_refuses(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    make_project(projects_root, "Beta")
    # bridge is serving Alpha, caller asks about Beta -> must NOT answer
    monkeypatch.setattr(core, "_bridge_http", _bridge(_HEALTHY))
    assert core._use_bridge_for(cfg, "Beta") is False


def test_use_bridge_for_unavailable(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "_bridge_http", _bridge({}, unreachable=True))
    assert core._use_bridge_for(cfg, "Alpha") is False


# ---- describe_node ----------------------------------------------------

_NODE = {
    "path": "UI/MainWindow", "browse_name": "MainWindow",
    "node_class": "ObjectType", "dotnet_type": "WindowType",
    "children": [], "properties": [{"name": "Width", "datatype": "Size", "value": "800 (Float)"}],
    "truncated": False,
}


def test_describe_node_returns_live_shape(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    make_project(projects_root, "Alpha")
    routes = {**_HEALTHY, "/bridge/nodes": (200, _NODE)}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    out = core.describe_node(cfg, "Alpha", "UI/MainWindow")
    assert out["dotnet_type"] == "WindowType"
    assert out["properties"][0]["name"] == "Width"
    assert out["source"] == "bridge"


def test_describe_node_wraps_property_values_as_untrusted(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    """Property VALUES are author-settable model content -> <untrusted> (U11);
    the property NAME (structural identity) stays raw."""
    make_project(projects_root, "Alpha")
    routes = {**_HEALTHY, "/bridge/nodes": (200, _NODE)}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    out = core.describe_node(cfg, "Alpha", "UI/MainWindow")
    assert out["properties"][0]["name"] == "Width"
    assert out["properties"][0]["value"] == core._untrusted("800 (Float)", "bridge")


def test_describe_node_raises_when_bridge_down(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "_bridge_http", _bridge({}, unreachable=True))
    with pytest.raises(core.BridgeUnavailable):
        core.describe_node(cfg, "Alpha", "UI/MainWindow")


def test_describe_node_404_is_node_not_found(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    make_project(projects_root, "Alpha")
    routes = {**_HEALTHY, "/bridge/nodes": (404, {"error": {"code": "node_not_found"}})}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    with pytest.raises(core.NodeNotFound):
        core.describe_node(cfg, "Alpha", "UI/Nope")


# ---- list_screens routing ---------------------------------------------

def test_list_screens_routes_to_bridge(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    make_project(projects_root, "Alpha")
    screens = [{"name": "Overview", "type": "Screen", "path": "UI/Screens/Overview", "child_count": 2}]
    routes = {**_HEALTHY, "/bridge/screens": (200, {"screens": screens})}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    out = core.list_screens(cfg, "Alpha")
    assert out["source"] == "bridge"
    assert out["count"] == 1
    assert out["screens"][0]["name"] == "Overview"


# ---- type discovery ---------------------------------------------------

def test_list_ui_types(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    make_project(projects_root, "Alpha")
    types = [{"name": "Label", "browse_name": "Label"}, {"name": "Button", "browse_name": "Button"}]
    routes = {**_HEALTHY, "/bridge/types/ui": (200, {"types": types, "count": 2})}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    out = core.list_ui_types(cfg, "Alpha")
    assert out["source"] == "bridge"
    assert {t["name"] for t in out["types"]} == {"Label", "Button"}


def test_list_ui_types_drops_redundant_browse_name(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    """browse_name is stripped per-entry when it equals name (the ~102-type
    catalog's common case) but kept when the two genuinely differ. `count`
    still reflects the full catalog, unaffected by the leaner per-entry shape."""
    make_project(projects_root, "Alpha")
    types = [
        {"name": "Label", "browse_name": "Label"},
        {"name": "Rectangle", "browse_name": "Rect"},
    ]
    routes = {**_HEALTHY, "/bridge/types/ui": (200, {"types": types, "count": 2})}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    out = core.list_ui_types(cfg, "Alpha")
    assert out["count"] == 2
    by_name = {t["name"]: t for t in out["types"]}
    assert "browse_name" not in by_name["Label"]
    assert by_name["Rectangle"]["browse_name"] == "Rect"


def test_describe_type_schema(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    make_project(projects_root, "Alpha")
    schema = {"type": "Label", "browse_name": "Label",
              "properties": [{"name": "Text", "datatype": "LocalizedText"}]}
    routes = {**_HEALTHY, "/bridge/types/schema": (200, schema)}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    out = core.describe_type(cfg, "Alpha", "Label")
    assert out["source"] == "bridge"
    assert out["properties"][0]["name"] == "Text"


def test_describe_type_unknown_is_node_not_found(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    make_project(projects_root, "Alpha")
    routes = {**_HEALTHY, "/bridge/types/schema": (404, {"error": {"code": "type_not_found"}})}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    with pytest.raises(core.NodeNotFound):
        core.describe_type(cfg, "Alpha", "Nope")


def test_list_ui_types_misaligned_builds_summary(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    """list_ui_types emits misaligned:[{name,browse_name}] for entries where
    browse_name != name, plus a _hint nudge. Aligned entries produce no misaligned
    key and no hint."""
    make_project(projects_root, "Alpha")
    types = [
        {"name": "Label", "browse_name": "Label"},
        {"name": "VirtualKeyboardType", "browse_name": "VirtualKeyboard", "resolved_by": "browse_name"},
        {"name": "Button", "browse_name": "Button"},
    ]
    routes = {**_HEALTHY, "/bridge/types/ui": (200, {"types": types, "count": 3})}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    out = core.list_ui_types(cfg, "Alpha")
    assert "misaligned" in out
    assert out["misaligned"] == [{"name": "VirtualKeyboardType", "browse_name": "VirtualKeyboard"}]
    assert "_hint" in out
    # Aligned entries produce no misaligned key
    by_name = {t["name"]: t for t in out["types"]}
    assert "browse_name" not in by_name["Label"]
    assert "browse_name" not in by_name["Button"]
    assert by_name["VirtualKeyboardType"]["browse_name"] == "VirtualKeyboard"


def test_list_ui_types_no_misaligned_when_all_aligned(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    """When all types are aligned (browse_name == name), misaligned is absent."""
    make_project(projects_root, "Alpha")
    types = [{"name": "Label", "browse_name": "Label"}, {"name": "Button", "browse_name": "Button"}]
    routes = {**_HEALTHY, "/bridge/types/ui": (200, {"types": types, "count": 2})}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    out = core.list_ui_types(cfg, "Alpha")
    assert "misaligned" not in out
    assert "_hint" not in out


def test_describe_type_passes_resolved_by_through(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    """describe_type passes resolved_by from the bridge response through unchanged."""
    make_project(projects_root, "Alpha")
    schema = {
        "type": "VirtualKeyboard",
        "browse_name": "VirtualKeyboard",
        "resolved_by": "browse_name",
        "properties": [],
        "truncated": False,
    }
    routes = {**_HEALTHY, "/bridge/types/schema": (200, schema)}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    out = core.describe_type(cfg, "Alpha", "VirtualKeyboard")
    assert out["resolved_by"] == "browse_name"
    # aligned name: no catalog_misaligned
    assert "catalog_misaligned" not in out


def test_describe_type_catalog_misaligned_when_browse_name_differs(
    cfg: core.Config, projects_root: Path, monkeypatch
) -> None:
    """catalog_misaligned:True is set when the response browse_name differs from
    the requested type name — signals the caller used a field-name alias rather than
    the canonical browse-name identifier."""
    make_project(projects_root, "Alpha")
    schema = {
        "type": "VirtualKeyboardType",
        "browse_name": "VirtualKeyboard",
        "properties": [],
        "truncated": False,
    }
    routes = {**_HEALTHY, "/bridge/types/schema": (200, schema)}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    out = core.describe_type(cfg, "Alpha", "VirtualKeyboardType")
    assert out["catalog_misaligned"] is True


def test_type_tools_require_bridge(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "_bridge_http", _bridge({}, unreachable=True))
    with pytest.raises(core.BridgeUnavailable):
        core.list_ui_types(cfg, "Alpha")
    with pytest.raises(core.BridgeUnavailable):
        core.describe_type(cfg, "Alpha", "Label")


def test_bridge_state_includes_project_path_and_pid(cfg: core.Config, monkeypatch) -> None:
    """bridge_state() must surface project_path and pid from /bridge/health.

    Shape contract: project_path is a str at its expected key; pid is an int.
    Both keys must be present — a KeyError here would mean the field was dropped.
    """
    routes = {"/bridge/health": (200, {
        "bridge_version": "1.1.0",
        "project": "Alpha",
        "model_loaded": True,
        "port": 8768,
        "project_path": r"C:\Users\dev\Projects\Alpha\Alpha.optix",
        "pid": 12345,
    })}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    st = core.bridge_state(cfg)
    assert st["available"] is True
    # value assertions
    assert st["project_path"] == r"C:\Users\dev\Projects\Alpha\Alpha.optix"
    assert st["pid"] == 12345
    # type assertions — the shape contract
    assert isinstance(st["project_path"], str)
    assert isinstance(st["pid"], int)


def test_bridge_state_project_path_null(cfg: core.Config, monkeypatch) -> None:
    """project_path may be null when Studio cannot determine the on-disk path.

    Shape contract: the key must still be present; project_path is None (not
    absent), pid is still an int at its key.
    """
    routes = {"/bridge/health": (200, {
        "bridge_version": "1.1.0",
        "project": "Alpha",
        "model_loaded": True,
        "port": 8768,
        "project_path": None,
        "pid": 99,
    })}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    st = core.bridge_state(cfg)
    # project_path key must exist and be explicitly None
    assert "project_path" in st
    assert st["project_path"] is None
    # pid key must exist and carry the integer value
    assert "pid" in st
    assert st["pid"] == 99
    assert isinstance(st["pid"], int)


def test_bridge_state_project_path_absent_from_old_bridge(cfg: core.Config, monkeypatch) -> None:
    """Older bridge versions that don't send project_path/pid yield None — no KeyError."""
    routes = {"/bridge/health": (200, {
        "bridge_version": "0.4.0",
        "project": "Alpha",
        "model_loaded": True,
    })}
    monkeypatch.setattr(core, "_bridge_http", _bridge(routes))
    st = core.bridge_state(cfg)
    assert st["available"] is True
    # both keys must be present in the returned dict (None, not missing)
    assert "project_path" in st
    assert "pid" in st
    assert st["project_path"] is None
    assert st["pid"] is None


def test_list_screens_falls_back_to_file(cfg: core.Config, projects_root: Path, monkeypatch) -> None:
    p = make_project(projects_root, "Alpha")
    ui = p / "Nodes" / "UI"
    ui.mkdir(parents=True)
    (ui / "Screens.yaml").write_bytes(
        b"- Name: Overview\n  Type: Screen\n  Children:\n  - Name: Title\n    Type: Label\n"
    )
    # bridge disabled -> file path; Studio not running on the test host, so
    # require_editors_closed passes and the YAML is scanned.
    cfg = dataclasses.replace(cfg, bridge_enabled=False)
    monkeypatch.setattr(core, "_bridge_http", _bridge({}, unreachable=True))
    out = core.list_screens(cfg, "Alpha")
    assert out["source"] == "file"
    assert any(s["name"] == "Overview" for s in out["screens"])
