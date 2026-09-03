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

    def fake(cfg, path, method="GET", timeout=5.0, **_kwargs):
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


# --- node_attribute_not_settable (2026-08-16: agent set DisplayName; bridge
# --- fabricated an orphan variable and the Studio PROCESS access-violated) ---

def test_set_property_display_name_routes_to_attribute_endpoint(alpha, monkeypatch):
    """DisplayName never reaches the variable-materialization route (which
    crashed Studio) — it dispatches to the dedicated attribute endpoint."""
    cap: list = []
    routes = {"/bridge/node/displayname": (200, {
        "ok": True, "attribute": "DisplayName", "value": "Nice Name"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_set_property(
        alpha, "Alpha", "UI/MainWindow/L1", "DisplayName", "Nice Name")
    assert out["ok"] is True and out["attribute"] == "DisplayName"
    assert [c for c in cap if "/bridge/node/displayname" in c[1]]
    assert not [c for c in cap if "/bridge/node/property" in c[1]]


def test_set_property_display_name_on_stale_bridge_fails_clean(alpha, monkeypatch):
    """A 1.0.5/1.0.6 bridge has no attribute route — the unknown-route
    not_found must surface as a per-op failure, never dispatch to the
    crash-capable property route."""
    cap: list = []
    routes = {"/bridge/node/displayname": (200, {"error": {
        "code": "not_found", "message": "no route"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    with pytest.raises(core.BridgeWriteFailed):
        core.bridge_set_property(
            alpha, "Alpha", "UI/MainWindow/L1", "DisplayName", "Nice Name")
    assert not [c for c in cap if "/bridge/node/property" in c[1]]


def test_set_property_browse_name_rejected_before_dispatch(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_set_property(
            alpha, "Alpha", "UI/MainWindow/L1", "BrowseName", "NewName")
    assert "node_attribute_not_settable" in str(e.value)
    assert not [c for c in cap if "/bridge/node/property" in c[1]]


def test_bind_property_node_attribute_rejected_before_dispatch(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_bind_property(
            alpha, "Alpha", "UI/MainWindow/L1", "DisplayName",
            source_path="Model/Name")
    assert "node_attribute_not_settable" in str(e.value)
    assert not [c for c in cap if "/bridge/node/bind" in c[1]]


def test_attach_expression_node_attribute_rejected_before_dispatch(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_attach_expression(
            alpha, "Alpha", "UI/MainWindow/L1", "DisplayName", "{0}",
            sources="Model/Name")
    assert "node_attribute_not_settable" in str(e.value)
    assert not [c for c in cap if "/bridge/node/attach-expression" in c[1]]


def test_classify_node_attribute_failure_does_not_blame_connection(alpha):
    """Per-op rejection: bridge stays up, nudge must not say restart Studio."""
    exc = core.BridgeWriteFailed(
        "bridge set_property rejected: node_attribute_not_settable — "
        "'DisplayName' is a node attribute, not a settable property.")
    out = core.classify_bridge_failure(alpha, "Alpha", exc)
    assert out["reason_code"] == "write_failed"
    assert out["bridge"]["reachable"] is True
    assert "node_attribute_not_settable" in out["detail"]


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


def test_create_netlogic_posts(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/netlogic": (200, {"ok": True,
              "created_path": "UI/Screens/ScreenD/MyLogic", "type": "NetLogic",
              "node_class": "Object", "bound_class": "MyLogic"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_netlogic(alpha, "Alpha", "UI/Screens/ScreenD", "MyLogic")
    assert out["type"] == "NetLogic" and out["bound_class"] == "MyLogic"
    method, path = next(c for c in cap if "/bridge/model/netlogic" in c[1])
    assert method == "POST"
    assert "parent=UI%2FScreens%2FScreenD" in path and "name=MyLogic" in path


def test_create_netlogic_dup_name_raises(alpha, monkeypatch):
    # The real bridge's DupNameGuard emits "name_exists" (not "dup_name").
    routes = {"/bridge/model/netlogic": (200, {"error": {
        "code": "name_exists",
        "message": "a node named 'MyLogic' already exists under 'UI/Screens/ScreenD'"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_create_netlogic(alpha, "Alpha", "UI/Screens/ScreenD", "MyLogic")
    assert "name_exists" in str(e.value)


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


# ---- unknown_property did-you-mean (C# DeclaredPropertyGuard suggestion) ----
#
# The bridge's DeclaredPropertyGuard bakes a best-effort suggestion into the
# error `message` (not just a sibling did_you_mean field) precisely because
# _bridge_write_result flattens the error dict down to message + code and
# discards every other key. These pin that the suggestion text survives that
# flattening to the raised exception (and thus to classify_bridge_failure's
# `detail`, which is all the MCP tool caller ever sees), and that the extra
# structured fields remain backward-compatible (silently ignored).

def test_unknown_property_suggestion_surfaces_in_raised_message(alpha, monkeypatch):
    """Mirrors test_wire_event_nudges_wrong_event_name_before_bridge's assertion
    shape for the property path: the suggestion, baked into `message` by the C#
    guard, must survive _bridge_write_result's message/code flattening."""
    routes = {"/bridge/node/property": (200, {"error": {
        "code": "unknown_property",
        "message": "Panel has no settable property 'BackgroundColor' "
                   "(did you mean Color?) (call describe_type/describe_node "
                   "for the valid set)",
        "did_you_mean": "Color",
        "valid_properties": ["Color", "Width"]}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_set_property(alpha, "Alpha", "UI/MainWindow/P1",
                                 "BackgroundColor", "red")
    assert "did you mean Color?" in str(e.value)
    assert "unknown_property" in str(e.value)


def test_unknown_property_suggestion_reaches_classify_detail(alpha, monkeypatch):
    """The suggestion has to reach the LLM caller, which only sees
    classify_bridge_failure()'s `detail` (= str(exc)). Assert the passthrough."""
    routes = {"/bridge/node/property": (200, {"error": {
        "code": "unknown_property",
        "message": "Panel has no settable property 'BackgroundColor' "
                   "(did you mean Color?) (call describe_type/describe_node "
                   "for the valid set)",
        "did_you_mean": "Color",
        "valid_properties": ["Color", "Width"]}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    try:
        core.bridge_set_property(alpha, "Alpha", "UI/MainWindow/P1",
                                 "BackgroundColor", "red")
        raise AssertionError("expected BridgeWriteFailed")
    except core.BridgeWriteFailed as exc:
        out = core.classify_bridge_failure(alpha, "Alpha", exc)
    assert out["reason_code"] == "write_failed"
    assert "did you mean Color?" in out["detail"]


def test_unknown_property_no_suggestion_raises_cleanly(alpha, monkeypatch):
    """When the guard finds no close match it emits no did_you_mean and no
    '(did you mean ...)' clause; the plain message must still raise cleanly
    (the did_you_mean-absent path is the SuggestPropertyName null contract)."""
    routes = {"/bridge/node/property": (200, {"error": {
        "code": "unknown_property",
        "message": "Panel has no settable property 'Zzz' "
                   "(call describe_type/describe_node for the valid set)",
        "valid_properties": ["Color", "Width"]}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_set_property(alpha, "Alpha", "UI/MainWindow/P1", "Zzz", "v")
    assert "did you mean" not in str(e.value)
    assert "unknown_property" in str(e.value) and "Zzz" in str(e.value)


# ---- U16 batched authoring: the validate-then-apply flow --------------------
#
# The C# validator itself is covered live (test_bridge_live.py + the VM probe);
# these pin the PYTHON half so Linux/CI catches a regression in the flow —
# specifically that a dirty report or dry_run applies NOTHING, and that the
# not-atomic contract reports honestly instead of pretending.

_OK_REPORT = {"ok": True, "op_count": 2, "strict": False,
              "errors": [], "warnings": []}
_BAD_REPORT = {"ok": False, "op_count": 2, "strict": False, "warnings": [],
               "errors": [{"op_index": 1, "code": "unresolved_reference",
                           "message": "no node at 'UI/MainWindow/Later'"}]}

_TWO_OPS = [
    {"op": "create_widget", "screen": "UI/MainWindow", "name": "B1",
     "widget_type": "Rectangle"},
    {"op": "set_property", "path": "UI/MainWindow/B1", "name": "Width",
     "value": "40"},
]


def _fake_validate(report, *, seen=None):
    def fake(cfg, path, payload, timeout=20.0, **_kwargs):
        if seen is not None:
            seen.append((path, payload))
        return 200, report
    return fake


def test_bridge_edit_applies_after_a_clean_report(alpha, monkeypatch):
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    # bridge_edit's write path resolves its OWN routing via
    # _bridge_write_guard/_require_bridge_for (multi-instance, v1.0.7) instead
    # of the old guard-then-call-cfg-unchanged _use_bridge_for gated — patch it
    # too, passing cfg straight through (same bypass semantics as the True
    # return above, using the SAME cfg this test's fakes expect).
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["applied"] == 2 and out["op_count"] == 2
    assert applied == ["create_widget", "set_property"]


def test_bridge_edit_applies_nothing_when_validation_fails(alpha, monkeypatch):
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_BAD_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    # bridge_edit's write path resolves its OWN routing via
    # _bridge_write_guard/_require_bridge_for (multi-instance, v1.0.7) instead
    # of the old guard-then-call-cfg-unchanged _use_bridge_for gated — patch it
    # too, passing cfg straight through (same bypass semantics as the True
    # return above, using the SAME cfg this test's fakes expect).
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "validated"
    assert out["applied"] == 0 and applied == []
    assert out["report"]["errors"][0]["op_index"] == 1
    assert "op_index" in out["nudge"]


def test_bridge_edit_dry_run_short_circuits_a_clean_batch(alpha, monkeypatch):
    """dry_run must not apply even when the report is clean — that is the whole
    point of pre-flighting a batch an agent just composed."""
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    # bridge_edit's write path resolves its OWN routing via
    # _bridge_write_guard/_require_bridge_for (multi-instance, v1.0.7) instead
    # of the old guard-then-call-cfg-unchanged _use_bridge_for gated — patch it
    # too, passing cfg straight through (same bypass semantics as the True
    # return above, using the SAME cfg this test's fakes expect).
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS, dry_run=True)

    assert out["state"] == "validated"
    assert out["applied"] == 0 and applied == []
    assert out["dry_run"] is True and out["report"]["ok"] is True


def test_bridge_edit_reports_partial_application_honestly(alpha, monkeypatch):
    """NOT atomic: op 1 fails after op 0 landed, so the result must say
    applied=1 + failed_op rather than implying the batch was a no-op."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    # bridge_edit's write path resolves its OWN routing via
    # _bridge_write_guard/_require_bridge_for (multi-instance, v1.0.7) instead
    # of the old guard-then-call-cfg-unchanged _use_bridge_for gated — patch it
    # too, passing cfg straight through (same bypass semantics as the True
    # return above, using the SAME cfg this test's fakes expect).
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    def flaky(cfg, project, op):
        if op["op"] == "set_property":
            raise core.BridgeWriteFailed("bridge set_property failed: boom")
        return {"ok": True}

    monkeypatch.setattr(core, "_apply_one_edit", flaky)
    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "partial"
    assert out["applied"] == 1
    assert out["failed_op"] == {"index": 1, "op": "set_property",
                                "error": "bridge set_property failed: boom"}
    assert "not atomic" in out["nudge"]


# --- I9 follow-up (2026-09-02): destructive ops must hard-fail on an
# unknown field even when strict is left at its default (False). Reproduces
# the CELL4 incident: a delete op carrying `name`, meant to scope the delete
# to one property, instead silently ran against the whole node at `path`.

def test_destructive_op_unknown_field_hard_fails_even_when_not_strict(
    alpha, monkeypatch,
):
    """delete has no scoped/property-level form. Passing `name` (the natural
    but wrong guess for "just this property") must refuse the batch — not
    warn and delete the whole node at `path`, which is what actually happened
    on CELL4's TAB_LIST_SETTING before this fix."""
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "delete", "path": "UI/Screens/3-SETTINGS/TAB_LIST_SETTING",
         "name": "AttachedPanelLoader"}])

    assert out["state"] == "validated"
    assert out["applied"] == 0 and applied == [], (
        "the whole node must NOT be deleted just because `name` was unknown"
    )
    assert out["report"]["ok"] is False
    err = out["report"]["errors"][0]
    assert err["code"] == "unknown_op_field"
    assert err["unknown_fields"] == ["name"]


def test_destructive_op_hard_fail_applies_to_move_and_reorder_too(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: (_ for _ in ()).throw(
                            AssertionError("must not apply")))

    for op in (
        {"op": "move", "path": "UI/Screens/Foo", "new_parent": "UI/Screens",
         "bogus": 1},
        {"op": "reorder", "path": "UI/Screens/Foo", "position": 0, "bogus": 1},
    ):
        out = core.bridge_edit(alpha, "Alpha", [op])
        assert out["state"] == "validated"
        assert out["report"]["ok"] is False
        assert out["report"]["errors"][0]["unknown_fields"] == ["bogus"]


def test_non_destructive_op_unknown_field_still_only_warns_by_default(
    alpha, monkeypatch,
):
    """Regression guard: the destructive hard-fail must not spill over onto
    non-destructive verbs — existing lenient-by-default behavior for those
    (e.g. create_folder/set_property) is unchanged."""
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_folder", "parent": "Model", "name": "X",
         "bogus_field_that_does_not_exist": "xyz"}])

    assert out["state"] == "succeeded"
    assert out["applied"] == 1
    assert out["report"]["ok"] is True
    assert out["report"]["warnings"][0]["code"] == "unknown_op_field"


# --- rename sugar op (lowered to move: same parent + new_name) ---

def test_rename_op_lowers_to_move_before_validation(alpha, monkeypatch):
    """The C# validator never sees 'rename' — it validates the lowered move op,
    and apply dispatches the same lowered op."""
    seen: list = []
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body",
                        _fake_validate(_OK_REPORT, seen=seen))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    # multi-instance (v1.0.7): the write paths gate on _require_bridge_for, which
    # resolves WHICH armed port serves the project; _use_bridge_for is no longer
    # the guard, so patch the resolver too or it probes a real socket.
    monkeypatch.setattr(core, "_require_bridge_for", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op))

    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "rename", "path": "UI/Screens/Foo", "new_name": "Bar"}])

    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    sent = seen[0][1]["ops"][0]
    assert sent == {"op": "move", "path": "UI/Screens/Foo",
                    "new_parent": "UI/Screens", "new_name": "Bar"}
    assert applied == [sent]


def test_rename_op_requires_path_and_new_name(alpha, monkeypatch):
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_edit(alpha, "Alpha", [{"op": "rename", "path": "UI/X"}])
    assert "requires path and new_name" in str(e.value)


def test_rename_op_refuses_top_level_node(alpha, monkeypatch):
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_edit(alpha, "Alpha",
                         [{"op": "rename", "path": "UI", "new_name": "GUI"}])
    assert "top-level" in str(e.value)


def test_rename_op_refuses_noop_same_name(alpha, monkeypatch):
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_edit(alpha, "Alpha",
                         [{"op": "rename", "path": "UI/Screens/Foo",
                           "new_name": "Foo"}])
    assert "already named" in str(e.value)


def test_bridge_edit_sends_ops_and_strict_in_the_body(alpha, monkeypatch):
    seen: list = []
    monkeypatch.setattr(core, "_bridge_post_body",
                        _fake_validate(_OK_REPORT, seen=seen))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    # bridge_edit's write path resolves its OWN routing via
    # _bridge_write_guard/_require_bridge_for (multi-instance, v1.0.7) instead
    # of the old guard-then-call-cfg-unchanged _use_bridge_for gated — patch it
    # too, passing cfg straight through (same bypass semantics as the True
    # return above, using the SAME cfg this test's fakes expect).
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit", lambda cfg, project, op: None)

    core.bridge_edit(alpha, "Alpha", _TWO_OPS, strict=True)

    path, payload = seen[0]
    assert path == "/bridge/validate_ops"
    assert payload["strict"] is True
    assert [o["op"] for o in payload["ops"]] == ["create_widget", "set_property"]


def test_bridge_edit_reconciles_attach_expression_name_and_prop_name(alpha, monkeypatch):
    """The C# validator reads `name` for attach_expression (one shape with
    set_property/bind) while the Python applier reads `prop_name`. A batch
    carrying only ONE spelling used to validate-but-not-apply or vice versa and
    die `partial` mid-apply (found live 2026-07-25 building a segmented tank).
    bridge_edit coalesces so BOTH phases see BOTH fields, in either direction,
    WITHOUT mutating the caller's ops."""
    seen: list = []
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body",
                        _fake_validate(_OK_REPORT, seen=seen))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    # bridge_edit's write path resolves its OWN routing via
    # _bridge_write_guard/_require_bridge_for (multi-instance, v1.0.7) instead
    # of the old guard-then-call-cfg-unchanged _use_bridge_for gated — patch it
    # too, passing cfg straight through (same bypass semantics as the True
    # return above, using the SAME cfg this test's fakes expect).
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op))
    ops = [
        {"op": "attach_expression", "path": "UI/MainWindow/Seg1",
         "prop_name": "FillColor", "expression": "if({0}>5,1,0)",
         "sources": "Model/L"},
        {"op": "attach_expression", "path": "UI/MainWindow/Seg2",
         "name": "FillColor", "expression": "if({0}>6,1,0)",
         "sources": "Model/L"},
    ]
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    # validator side (reads `name`) now sees both, whichever spelling was given
    _, payload = seen[0]
    assert payload["ops"][0]["name"] == "FillColor"
    assert payload["ops"][1]["name"] == "FillColor"
    # applier side (reads `prop_name`) sees both too
    assert applied[0]["prop_name"] == "FillColor"
    assert applied[1]["prop_name"] == "FillColor"
    # caller's original dicts are untouched (normalization returns copies)
    assert "name" not in ops[0]
    assert "prop_name" not in ops[1]


def test_normalize_edit_op_coalesces_attach_expression_fields():
    only_prop = core._normalize_edit_op(
        {"op": "attach_expression", "prop_name": "FillColor"})
    assert only_prop == {"op": "attach_expression",
                         "prop_name": "FillColor", "name": "FillColor"}
    only_name = core._normalize_edit_op(
        {"op": "attach_expression", "name": "FillColor"})
    assert only_name == {"op": "attach_expression",
                         "name": "FillColor", "prop_name": "FillColor"}
    # both present but differing: `name` wins for both, so the two sides can't
    # silently disagree.
    both = core._normalize_edit_op(
        {"op": "attach_expression", "name": "A", "prop_name": "B"})
    assert both["name"] == "A" and both["prop_name"] == "A"


def test_normalize_edit_op_passes_through_other_ops_and_never_mutates():
    sp = {"op": "set_property", "path": "X", "name": "Width", "value": "1"}
    assert core._normalize_edit_op(sp) is sp  # non-attach: identity, untouched
    orig = {"op": "attach_expression", "prop_name": "FillColor"}
    core._normalize_edit_op(orig)
    assert "name" not in orig  # returned a copy; caller's dict unmutated
    empty = {"op": "attach_expression", "path": "X"}  # neither field present
    assert core._normalize_edit_op(empty) is empty


def test_normalize_edit_op_aliases_node_path_to_path():
    """The per-noun tools name the target `node_path`; batch ops + the C#
    validator read `path`. An op carrying only `node_path` must get `path` too."""
    out = core._normalize_edit_op(
        {"op": "set_property", "node_path": "UI/M/R", "name": "Width", "value": "10"})
    assert out["path"] == "UI/M/R" and out["node_path"] == "UI/M/R"
    # attach_expression composed with node_path + prop_name gets path AND name
    ax = core._normalize_edit_op(
        {"op": "attach_expression", "node_path": "UI/M/R", "prop_name": "FillColor",
         "expression": "{0}", "sources": "Model/x"})
    assert ax["path"] == "UI/M/R"
    assert ax["name"] == "FillColor" and ax["prop_name"] == "FillColor"
    # explicit path present -> node_path ignored, op passes through untouched
    both = {"op": "set_property", "path": "UI/M/R", "node_path": "OTHER",
            "name": "W", "value": "1"}
    assert core._normalize_edit_op(both) is both


def test_bridge_edit_treats_a_missing_endpoint_as_unavailable(alpha, monkeypatch):
    """An older bridge answers the unknown route with not_found. That must raise
    BridgeUnavailable — never be mistaken for 'validated clean' and applied."""
    applied: list = []
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    # bridge_edit's write path resolves its OWN routing via
    # _bridge_write_guard/_require_bridge_for (multi-instance, v1.0.7) instead
    # of the old guard-then-call-cfg-unchanged _use_bridge_for gated — patch it
    # too, passing cfg straight through (same bypass semantics as the True
    # return above, using the SAME cfg this test's fakes expect).
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))
    monkeypatch.setattr(
        core, "_bridge_post_body",
        lambda cfg, path, payload, timeout=20.0: (404, {"error": {"code": "not_found"}}))

    with pytest.raises(core.BridgeUnavailable) as e:
        core.bridge_edit(alpha, "Alpha", _TWO_OPS)
    assert "U16" in str(e.value)
    assert applied == []


def test_bridge_edit_rejects_an_empty_batch(alpha, monkeypatch):
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    # bridge_edit's write path resolves its OWN routing via
    # _bridge_write_guard/_require_bridge_for (multi-instance, v1.0.7) instead
    # of the old guard-then-call-cfg-unchanged _use_bridge_for gated — patch it
    # too, passing cfg straight through (same bypass semantics as the True
    # return above, using the SAME cfg this test's fakes expect).
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    with pytest.raises(core.BridgeWriteFailed):
        core.bridge_edit(alpha, "Alpha", [])


def test_apply_one_edit_maps_ops_onto_the_per_noun_calls(alpha, monkeypatch):
    """Each op dispatches to the SAME core.bridge_* call the single-op tool uses,
    with the op dict's fields mapped onto that function's positionals."""
    calls: list = []
    monkeypatch.setattr(core, "bridge_create_widget",
                        lambda cfg, p, screen, name, widget_type="Label":
                        calls.append(("widget", screen, name, widget_type)))
    monkeypatch.setattr(core, "bridge_set_property",
                        lambda cfg, p, node_path, name, value, locale="en-US":
                        calls.append(("prop", node_path, name, value, locale)))

    core._apply_one_edit(alpha, "Alpha", _TWO_OPS[0])
    core._apply_one_edit(alpha, "Alpha", _TWO_OPS[1])

    assert calls == [
        ("widget", "UI/MainWindow", "B1", "Rectangle"),
        ("prop", "UI/MainWindow/B1", "Width", "40", "en-US"),
    ]


def test_apply_one_edit_rejects_a_missing_required_field(alpha):
    with pytest.raises(core.BridgeWriteFailed) as e:
        core._apply_one_edit(alpha, "Alpha", {"op": "set_property",
                                              "path": "UI/X", "name": "Width"})
    assert "missing required field" in str(e.value) and "value" in str(e.value)
