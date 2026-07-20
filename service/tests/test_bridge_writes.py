"""Tests for the design-time bridge WRITE wrappers (service.core).

Offline: core._bridge_http is monkeypatched to validate POST routing, payload
construction, success/failure interpretation, and the serving-project guard —
no live Studio. (The C# materialization fix itself is validated against real
Studio; these cover the Python wrapper layer that kills the raw-curl gap.)
"""
from __future__ import annotations

import json

import pytest

from service import core
from service.tests.conftest import make_project


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


_HEALTHY = {"/bridge/health": (200, {"bridge_version": "0.5.0-phase1-materialize",
                                     "project": "Alpha", "model_loaded": True})}


def _fake_bridge(routes, *, capture=None, unreachable=False):
    """Fake core._bridge_http accepting the new `method` kwarg (GET + POST)."""
    merged = {**_HEALTHY, **routes}

    def fake(cfg, path, method="GET", timeout=5.0):
        if capture is not None:
            capture.append((method, path))
        if unreachable:
            raise core.BridgeUnavailable("bridge unreachable at test")
        for prefix, (status, body) in merged.items():
            if path.startswith(prefix):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                return status, raw
        return 404, b'{"error":{"code":"not_found"}}'

    return fake


@pytest.fixture
def alpha(cfg, projects_root):
    """cfg with a resolvable project 'Alpha' matching the bridge's reported project."""
    make_project(projects_root, "Alpha")
    return cfg


def test_set_property_success_posts_correct_params(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/property": (200, {"ok": True, "via": "clr-property",
                                              "datatype": "LocalizedText", "value": "Hi"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_set_property(alpha, "Alpha", "UI/MainWindow/L1", "Text", "Hi")
    assert out["ok"] is True and out["via"] == "clr-property"
    method, path = next(c for c in cap if "/bridge/node/property" in c[1])
    assert method == "POST"
    assert "path=UI%2FMainWindow%2FL1" in path
    assert "name=Text" in path and "value=Hi" in path and "locale=en-US" in path


def test_set_property_inline_failure_raises(alpha, monkeypatch):
    routes = {"/bridge/node/property": (200, {"ok": False,
              "error": {"code": "property_not_found", "message": "no prop X"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_set_property(alpha, "Alpha", "UI/MainWindow/L1", "X", "v")
    assert "no prop X" in str(e.value)


def test_create_widget_success(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/ui/widget": (200, {"ok": True,
              "created_path": "UI/MainWindow/L2", "type": "Label"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_widget(alpha, "Alpha", "UI/MainWindow", "L2", "Label")
    assert out["created_path"] == "UI/MainWindow/L2"
    method, path = next(c for c in cap if "/bridge/ui/widget" in c[1])
    assert method == "POST" and "type=Label" in path and "name=L2" in path


def test_create_variable_success(alpha, monkeypatch):
    routes = {"/bridge/model/variable": (200, {"ok": True,
              "created_path": "Model/Flag", "datatype": "Boolean"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_create_variable(alpha, "Alpha", "Flag")
    assert out["created_path"] == "Model/Flag"


def test_ensure_web_engine_creates(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/setup/web-engine": (200, {"ok": True, "existed": False,
              "path": "UI/WebPresentationEngine", "port": 9000,
              "start_window": "MainWindow"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_ensure_web_engine(alpha, "Alpha", port=9000)
    assert out["existed"] is False and out["path"] == "UI/WebPresentationEngine"
    method, path = next(c for c in cap if "/bridge/setup/web-engine" in c[1])
    assert method == "POST" and "port=9000" in path and "ip=0.0.0.0" in path


def test_ensure_web_engine_idempotent(alpha, monkeypatch):
    routes = {"/bridge/setup/web-engine": (200, {"ok": True, "existed": True,
              "path": "UI/WebPresentationEngine"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_ensure_web_engine(alpha, "Alpha")
    assert out["existed"] is True


def test_write_guard_wrong_project_raises(alpha, monkeypatch):
    # bridge serves "Alpha"; asking for "Beta" must refuse (no cross-project write).
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    with pytest.raises(core.BridgeUnavailable):
        core.bridge_set_property(alpha, "Beta", "UI/MainWindow/L1", "Text", "Hi")


def test_write_guard_unreachable_raises(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, unreachable=True))
    with pytest.raises(core.BridgeUnavailable):
        core.bridge_set_property(alpha, "Alpha", "UI/MainWindow/L1", "Text", "Hi")


def test_routing_error_surfaces_message(alpha, monkeypatch):
    # bridge routes to an unknown endpoint -> 404 {error:{code}} -> BridgeWriteFailed
    routes = {"/bridge/ui/widget": (404, {"error": {"code": "type_not_found",
              "message": "no builtin UI type: Bogus"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_create_widget(alpha, "Alpha", "UI/MainWindow", "X", "Bogus")
    assert "no builtin UI type" in str(e.value)


# ---- semantic-authoring wrappers (bind / alias / event / i18n / delete / refs) ----

def test_bind_property_posts(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/bind": (200, {"ok": True})}, capture=cap))
    core.bridge_bind_property(alpha, "Alpha", "UI/MainWindow/L1", "Text", "Model/V1", "ReadWrite")
    m, p = next(c for c in cap if "/bridge/node/bind" in c[1])
    assert m == "POST" and "source=Model%2FV1" in p and "mode=ReadWrite" in p


def test_create_alias_posts(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/alias": (200, {"ok": True})}, capture=cap))
    core.bridge_create_alias(alpha, "Alpha", "Model", "CurrentMotor", "Model/Motor1")
    m, p = next(c for c in cap if "/bridge/node/alias" in c[1])
    assert m == "POST" and "name=CurrentMotor" in p and "target=Model%2FMotor1" in p


def test_wire_event_posts(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True})}, capture=cap))
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent", "UI/Logic/DoThing")
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert m == "POST" and "event=MouseClickEvent" in p and "method=UI%2FLogic%2FDoThing" in p


def test_wire_event_native_set_command(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True})}, capture=cap))
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                           command="SetVariable", variable="Model/Flag", value="true")
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert m == "POST" and "command=SetVariable" in p
    assert "variable=Model%2FFlag" in p and "value=true" in p and "method=" not in p


def test_wire_event_native_toggle_command(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True})}, capture=cap))
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                           command="ToggleVariable", variable="Model/Flag")
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert m == "POST" and "command=ToggleVariable" in p and "variable=Model%2FFlag" in p


def test_wire_event_requires_command_or_method(alpha):
    with pytest.raises(core.BridgeWriteFailed):
        core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent")


def test_wire_event_nudges_wrong_event_name_before_bridge(alpha, monkeypatch):
    """The documented A/B trap: 'Click' must be caught client-side with a
    canonical suggestion, WITHOUT hitting the bridge (no POST captured)."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True})}, capture=cap))
    out = core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "Click",
                                 command="ToggleVariable", variable="Model/Flag")
    assert out["ok"] is False and out["code"] == "noncanonical_event"
    assert out["suggestion"] == "MouseClickEvent"
    assert "MouseClickEvent" in out["valid_events"]
    # guard fired before any write — the event route was never POSTed
    assert not any("/bridge/node/event" in c[1] for c in cap)


def test_event_aliases_only_target_wireable_events():
    """Every alias must resolve to an event in the authoritative canonical set —
    else the nudge would suggest a non-existent event (the bug the live 0.9.21
    validation surfaced: KeyDownEvent/MouseEnterEvent aren't wireable)."""
    for alias, target in core._EVENT_ALIASES.items():
        assert target in core._CANONICAL_UI_EVENTS, \
            f"alias {alias!r} -> {target!r} not in _CANONICAL_UI_EVENTS"


def test_wire_event_accepts_canonical_event_any_casing(alpha, monkeypatch):
    """A recognized event (any casing) passes straight through to the bridge."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True})}, capture=cap))
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "mouseclickevent",
                           command="ToggleVariable", variable="Model/Flag")
    assert any("/bridge/node/event" in c[1] for c in cap)


def test_wire_event_passes_unknown_event_to_bridge(alpha, monkeypatch):
    """A name that is neither canonical nor a known alias is the bridge's call —
    it passes through (bridge is the authority for the full event catalog)."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True})}, capture=cap))
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "SomeExoticEvent",
                           command="ToggleVariable", variable="Model/Flag")
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert m == "POST" and "event=SomeExoticEvent" in p


def test_validate_expression_posts(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/expr/validate": (200, {"ok": True, "valid": True, "sources": 1})}, capture=cap))
    out = core.bridge_validate_expression(alpha, "Alpha", "if({0},1,2)", sources="Model/X")
    m, p = next(c for c in cap if "/bridge/expr/validate" in c[1])
    assert m == "POST" and "expression=if" in p and "sources=Model%2FX" in p
    assert out["valid"] is True


def test_add_translation_posts(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/i18n/translation": (200, {"ok": True})}, capture=cap))
    core.bridge_add_translation(alpha, "Alpha", "Key1", "Hello", "en-US")
    m, p = next(c for c in cap if "/bridge/i18n/translation" in c[1])
    assert m == "POST" and "key=Key1" in p and "value=Hello" in p


def test_delete_node_posts(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/delete": (200, {"ok": True})}, capture=cap))
    core.bridge_delete_node(alpha, "Alpha", "UI/MainWindow/Old")
    m, p = next(c for c in cap if "/bridge/node/delete" in c[1])
    assert m == "POST" and "path=UI%2FMainWindow%2FOld" in p


def test_node_references_is_get(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/references": (200, {"ok": True, "references": []})}, capture=cap))
    out = core.bridge_node_references(alpha, "Alpha", "Model/Motor1")
    m, p = next(c for c in cap if "/bridge/node/references" in c[1])
    assert m == "GET" and out["ok"] is True


def test_semantic_not_implemented_raises(alpha, monkeypatch):
    # endpoint not built in the .cs yet -> graceful failure, not a crash
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/bind": (200, {"ok": False,
                         "error": {"code": "not_implemented", "message": "bind pending marshaling"}})}))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_bind_property(alpha, "Alpha", "UI/MainWindow/L1", "Text", "Model/V1")
    assert "pending marshaling" in str(e.value)


# ---- classify_bridge_failure: structured, nudging errors (no auto-restart) ----

def test_classify_write_failed_says_bridge_is_up(alpha):
    exc = core.BridgeWriteFailed("bridge set_property failed: CoreException: bad enum")
    out = core.classify_bridge_failure(alpha, "Alpha", exc)
    assert out["state"] == "failed" and out["reason_code"] == "write_failed"
    assert out["bridge"]["reachable"] is True
    assert "not a connection problem" in out["nudge"]
    assert "CoreException" in out["detail"]


def test_classify_wrong_project(alpha, monkeypatch):
    # /bridge/health reports serving 'Alpha'; the write targeted 'Beta'.
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    out = core.classify_bridge_failure(alpha, "Beta", core.BridgeUnavailable("not serving Beta"))
    assert out["reason_code"] == "bridge_wrong_project"
    assert out["bridge"]["serving"] == "Alpha"
    assert "Alpha" in out["nudge"] and "Beta" in out["nudge"]


def test_classify_wrong_project_is_case_insensitive(alpha, monkeypatch):
    routes = {"/bridge/health": (200, {"bridge_version": "x", "project": "ALPHA",
                                       "model_loaded": True})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.classify_bridge_failure(alpha, "alpha", core.BridgeUnavailable("x"))
    assert out["reason_code"] == "bridge_transient"  # same project, different case


def test_classify_model_loading(alpha, monkeypatch):
    routes = {"/bridge/health": (200, {"bridge_version": "x", "project": "unknown",
                                       "model_loaded": False})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.classify_bridge_failure(alpha, "Alpha", core.BridgeUnavailable("x"))
    assert out["reason_code"] == "bridge_model_loading"


def test_classify_transient_when_healthy_but_write_said_unavailable(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))  # serves Alpha, loaded
    out = core.classify_bridge_failure(alpha, "Alpha", core.BridgeUnavailable("race"))
    assert out["reason_code"] == "bridge_transient"


def test_classify_unreachable_studio_open(alpha, monkeypatch):
    from service import studio_guard
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, unreachable=True))
    monkeypatch.setattr(studio_guard, "studio_state",
                        lambda force=False: {"studio": {"running": True, "pids": [7]}, "editors": []})
    out = core.classify_bridge_failure(alpha, "Alpha", core.BridgeUnavailable("unreachable"))
    assert out["reason_code"] == "bridge_unreachable_studio_open"
    assert "StartBridge" in out["nudge"] and out["bridge"]["reachable"] is False


def test_classify_unreachable_studio_closed(alpha, monkeypatch):
    from service import studio_guard
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, unreachable=True))
    monkeypatch.setattr(studio_guard, "studio_state",
                        lambda force=False: {"studio": {"running": False, "pids": []}, "editors": []})
    out = core.classify_bridge_failure(alpha, "Alpha", core.BridgeUnavailable("unreachable"))
    assert out["reason_code"] == "bridge_unreachable_studio_closed"
    assert "isn't running" in out["nudge"]


# --- unsupported_array_write (Cowork 2026-07-16: NodeId[] AliasNodeArray write
# --- crashed the Studio PROCESS; String[] Columns/Rows raised CoreException) ---

def test_set_property_json_array_value_rejected_before_dispatch(alpha, monkeypatch):
    """A JSON-array value never reaches the bridge — even a healthy one."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_set_property(
            alpha, "Alpha", "UI/MainWindow/NavPanel/Panels/ArrayTestItem",
            "AliasNodeArray", '["UI/Screens/ScreenA"]')
    assert "unsupported_array_write" in str(e.value)
    assert "AliasNodeArray" in str(e.value)
    assert not [c for c in cap if "/bridge/node/property" in c[1]]


def test_set_property_python_list_value_rejected_before_dispatch(alpha, monkeypatch):
    """Defensive: a caller handing a real list (HTTP surface) is rejected too."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_set_property(alpha, "Alpha", "UI/MainWindow/G1", "Columns",
                                 ["1*", "1*"])
    assert "unsupported_array_write" in str(e.value)
    assert not [c for c in cap if "/bridge/node/property" in c[1]]


def test_set_property_bracket_literal_text_still_dispatches(alpha, monkeypatch):
    """'[TODO]' isn't JSON — a bracketed literal on a String prop must pass."""
    cap: list = []
    routes = {"/bridge/node/property": (200, {"ok": True, "via": "variable",
                                              "datatype": "String", "value": "[TODO]"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_set_property(alpha, "Alpha", "UI/MainWindow/L1", "Text", "[TODO]")
    assert out["ok"] is True
    assert [c for c in cap if "/bridge/node/property" in c[1]]


def test_set_property_bridge_array_error_surfaces_code(alpha, monkeypatch):
    """The bridge's own declared-type gate (String[]/NodeId[]/Int32[]...) surfaces
    its code, not just the message — the service must not swallow it."""
    routes = {"/bridge/node/property": (200, {"error": {
        "code": "unsupported_array_write",
        "message": "property 'Columns' on GridLayout is array-typed (String[]). "
                   "Array writes aren't supported via set_property."}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_set_property(alpha, "Alpha", "UI/MainWindow/G1", "Columns", "1*")
    assert "unsupported_array_write" in str(e.value)
    assert "String[]" in str(e.value)


def test_classify_array_write_failure_does_not_blame_connection(alpha):
    """unsupported_array_write is a per-op rejection: bridge stays up, nudge
    must not tell the user to restart Studio (the crash it prevents did)."""
    exc = core.BridgeWriteFailed(
        "bridge set_property failed: unsupported_array_write: property "
        "'AliasNodeArray' on NavigationPanelItem is array-typed (NodeId[]).")
    out = core.classify_bridge_failure(alpha, "Alpha", exc)
    assert out["reason_code"] == "write_failed"
    assert out["bridge"]["reachable"] is True
    assert "unsupported_array_write" in out["detail"]


# --- structural authoring family (folder/object/type/convert — 2026-07-17) ---

def test_create_folder_posts(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/folder": (200, {"ok": True,
              "created_path": "UI/Templates", "kind": "folder"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_folder(alpha, "Alpha", "UI", "Templates")
    assert out["ok"] is True and out["kind"] == "folder"
    method, path = next(c for c in cap if "/bridge/model/folder" in c[1])
    assert method == "POST"
    assert "parent=UI" in path and "name=Templates" in path


def test_create_object_plain_posts_without_type_param(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/object": (200, {"ok": True,
              "created_path": "Model/Motor1", "type": "BaseObjectType",
              "node_class": "Object"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_object(alpha, "Alpha", "Model", "Motor1")
    assert out["type"] == "BaseObjectType"
    _, path = next(c for c in cap if "/bridge/model/object" in c[1])
    assert "type=" not in path


def test_create_object_instance_of_custom_type(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/object": (200, {"ok": True,
              "created_path": "UI/Screens/ScreenD/Card1",
              "type": "UI/Templates/CardType", "node_class": "Object"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_object(alpha, "Alpha", "UI/Screens/ScreenD", "Card1",
                                    object_type="UI/Templates/CardType")
    assert out["ok"] is True
    _, path = next(c for c in cap if "/bridge/model/object" in c[1])
    assert "type=UI%2FTemplates%2FCardType" in path


def test_create_object_not_a_type_raises(alpha, monkeypatch):
    routes = {"/bridge/model/object": (200, {"error": {
        "code": "not_a_type",
        "message": "UI/MainWindow/L1 is Object, not an ObjectType"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_create_object(alpha, "Alpha", "Model", "X",
                                  object_type="UI/MainWindow/L1")
    assert "not_a_type" in str(e.value)


def test_create_type_posts_base(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/type": (200, {"ok": True,
              "created_path": "UI/Templates/CardType", "base": "RowLayout",
              "node_class": "ObjectType"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_type(alpha, "Alpha", "CardType", "UI/Templates",
                                  base_type="RowLayout")
    assert out["node_class"] == "ObjectType"
    _, path = next(c for c in cap if "/bridge/model/type" in c[1])
    assert "base=RowLayout" in path and "name=CardType" in path


def test_create_type_bare_omits_base(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/type": (200, {"ok": True,
              "created_path": "Model/Types/MotorType", "base": "BaseObjectType",
              "node_class": "ObjectType"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_create_type(alpha, "Alpha", "MotorType", "Model/Types")
    _, path = next(c for c in cap if "/bridge/model/type" in c[1])
    assert "base=" not in path


def test_convert_to_type_posts_and_returns_audit(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/convert-to-type": (200, {"ok": True,
              "type_path": "UI/Templates/CardType", "copied_nodes": 3,
              "skipped": ["Text/Converter (ExpressionEvaluator): not copied"],
              "replaced": True, "instance_path": "UI/Screens/ScreenD/Card",
              "links_verified": 2, "relative_links_unverified": 0,
              "broken_links": [], "steps": ["create_type", "copy_subtree",
                                            "delete_original", "instantiate"]})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_convert_to_type(
        alpha, "Alpha", "UI/Screens/ScreenD/Card", "CardType", "UI/Templates")
    assert out["copied_nodes"] == 3 and out["replaced"] is True
    assert out["skipped"] and "not copied" in out["skipped"][0]
    _, path = next(c for c in cap if "/bridge/node/convert-to-type" in c[1])
    assert "replace=true" in path and "type_name=CardType" in path


def test_convert_to_type_replace_false(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/convert-to-type": (200, {"ok": True,
              "type_path": "UI/Templates/T", "copied_nodes": 0, "skipped": [],
              "replaced": False, "links_verified": 0,
              "relative_links_unverified": 0, "broken_links": [],
              "steps": ["create_type", "copy_subtree"]})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_convert_to_type(alpha, "Alpha", "UI/X", "T", "UI/Templates",
                                replace=False)
    _, path = next(c for c in cap if "/bridge/node/convert-to-type" in c[1])
    assert "replace=false" in path


def test_convert_to_type_folder_missing_surfaces_nudge(alpha, monkeypatch):
    routes = {"/bridge/node/convert-to-type": (200, {"error": {
        "code": "folder_not_found",
        "message": "no types folder at: UI/Templates — create it first "
                   "(/bridge/model/folder)"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_convert_to_type(alpha, "Alpha", "UI/X", "T", "UI/Templates")
    assert "folder_not_found" in str(e.value)
    assert "create it first" in str(e.value)


# --- alias parameters + raw-path (late) binding (2026-07-17) ---

def test_create_alias_template_slot_no_target(alpha, monkeypatch):
    """Template alias: kind constraint, NO target — params must reflect that."""
    cap: list = []
    routes = {"/bridge/node/alias": (200, {"ok": True,
              "alias": "UI/Templates/Row/Alias1", "target": None,
              "kind": "BaseObject", "via": "alias-create"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_alias(alpha, "Alpha", "UI/Templates/Row", "Alias1",
                                   kind="BaseObject")
    assert out["ok"] is True and out["target"] is None
    _, path = next(c for c in cap if "/bridge/node/alias" in c[1])
    assert "kind=BaseObject" in path and "target=" not in path


def test_create_alias_with_target_still_posts(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/alias": (200, {"ok": True,
              "alias": "UI/X/A", "target": "Model/BaseObject", "kind": None})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_create_alias(alpha, "Alpha", "UI/X", "A",
                             target_path="Model/BaseObject")
    _, path = next(c for c in cap if "/bridge/node/alias" in c[1])
    assert "target=Model%2FBaseObject" in path


def test_bind_property_raw_path_posts_raw_not_source(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/bind": (200, {"ok": True,
              "path": "UI/Templates/Row/Label1/Text",
              "raw": "{Alias1}/MyInt", "mode": "Read",
              "via": "dynamiclink-raw"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_bind_property(alpha, "Alpha", "UI/Templates/Row/Label1",
                                    "Text", raw_path="{Alias1}/MyInt")
    assert out["via"] == "dynamiclink-raw"
    _, path = next(c for c in cap if "/bridge/node/bind" in c[1])
    assert "raw=%7BAlias1%7D%2FMyInt" in path and "source=" not in path


def test_bind_property_requires_exactly_one_of_source_or_raw(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    with pytest.raises(core.BridgeWriteFailed):
        core.bridge_bind_property(alpha, "Alpha", "UI/X", "Text")
    with pytest.raises(core.BridgeWriteFailed):
        core.bridge_bind_property(alpha, "Alpha", "UI/X", "Text",
                                  source_path="Model/V", raw_path="{A}/V")


def test_bind_property_source_through_alias_error_nudges_raw(alpha, monkeypatch):
    """The bridge's source_not_variable now nudges toward raw_path — the exact
    Cowork dead-end (binding through Alias1 with a resolvable source)."""
    routes = {"/bridge/node/bind": (200, {"error": {
        "code": "source_not_variable",
        "message": "source is not a variable: UI/Templates/Row/Alias1/MyString "
                   "— binding THROUGH an alias ({Alias1}/Child or "
                   "../../Alias1/Child) is deliberately unresolvable at bind "
                   "time; pass it as raw= instead"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_bind_property(alpha, "Alpha", "UI/Templates/Row/Label1",
                                  "Text", source_path="UI/Templates/Row/Alias1/MyString")
    assert "raw=" in str(e.value)


# --- move_node (re-author reparent, 2026-07-17) ---

def test_move_node_posts_and_reports_new_identity(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/move": (200, {"ok": True,
              "from": "UI/Screens/ScreenB/CenterColumn",
              "to": "UI/Screens/ScreenB/Scroll/VLayout/CenterColumn",
              "copied_nodes": 12, "skipped": [], "links_verified": 10,
              "relative_links_unverified": 0, "broken_links": [],
              "steps": ["create_copy", "copy_subtree", "delete_original"],
              "note": "the moved node has a NEW NodeId — inbound references "
                      "from elsewhere to the old subtree are not rewritten"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_move_node(alpha, "Alpha", "UI/Screens/ScreenB/CenterColumn",
                                "UI/Screens/ScreenB/Scroll/VLayout")
    assert out["copied_nodes"] == 12 and "NEW NodeId" in out["note"]
    _, path = next(c for c in cap if "/bridge/node/move" in c[1])
    assert "new_parent=UI%2FScreens%2FScreenB%2FScroll%2FVLayout" in path
    assert "new_name=" not in path


def test_move_node_new_name_posts(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/move": (200, {"ok": True, "from": "UI/X/A",
              "to": "UI/Y/B", "copied_nodes": 1, "skipped": [],
              "links_verified": 0, "relative_links_unverified": 0,
              "broken_links": [], "steps": []})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_move_node(alpha, "Alpha", "UI/X/A", "UI/Y", new_name="B")
    _, path = next(c for c in cap if "/bridge/node/move" in c[1])
    assert "new_name=B" in path


def test_move_node_into_self_error_surfaces(alpha, monkeypatch):
    routes = {"/bridge/node/move": (200, {"error": {
        "code": "move_into_self",
        "message": "new_parent UI/X/A/Inner is inside the subtree being moved"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_move_node(alpha, "Alpha", "UI/X/A", "UI/X/A/Inner")
    assert "move_into_self" in str(e.value)


def test_bridge_write_appends_audit_line(alpha, monkeypatch, tmp_path):
    """Every live-model mutation leaves a JSONL audit line (SECURITY.md
    'traces of tool calls' posture — added 2026-07-17)."""
    routes = {"/bridge/node/property": (200, {"ok": True, "via": "variable",
                                              "datatype": "String", "value": "x"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    core.bridge_set_property(alpha, "Alpha", "UI/MainWindow/L1", "Text", "x")
    audit_file = alpha.state_dir / "logs" / "audit.jsonl"
    assert audit_file.is_file()
    rec = json.loads(audit_file.read_text().strip().splitlines()[-1])
    assert rec["event"] == "bridge_write" and rec["op"] == "set_property"
    assert rec["ok"] is True and rec["project"] == "Alpha" and rec["ts"]


def test_failed_bridge_write_audited_with_error(alpha, monkeypatch):
    routes = {"/bridge/node/property": (200, {"error": {
        "code": "unknown_property", "message": "no prop X"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed):
        core.bridge_set_property(alpha, "Alpha", "UI/MainWindow/L1", "X", "v")
    rec = json.loads((alpha.state_dir / "logs" / "audit.jsonl")
                     .read_text().strip().splitlines()[-1])
    assert rec["ok"] is False and "no prop X" in rec["error"]
