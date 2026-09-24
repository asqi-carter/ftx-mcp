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


def test_wire_event_open_keyboard_forwards_args(alpha, monkeypatch):
    """OpenKeyboard is a native UI command like OpenDialog: it must reach the
    bridge as `command` WITH its `args` (three InputArguments -- KeyboardType,
    TargetVariable, ParentItem). Before it was in _UI_COMMANDS, `args` alongside
    a `command` was rejected outright as a method-path-only field."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True})}, capture=cap))
    core.bridge_wire_event(
        alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
        command="OpenKeyboard",
        args="KeyboardType=Numeric;TargetVariable=Model/Setpoint;ParentItem=")
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert m == "POST" and "command=OpenKeyboard" in p
    assert "KeyboardType%3DNumeric" in p and "TargetVariable%3DModel%2FSetpoint" in p
    assert "ParentItem%3D" in p and "method=" not in p


def test_wire_event_command_casing_is_canonicalized(alpha, monkeypatch):
    """"opendialog" must reach the bridge as OpenDialog (UI-command branch),
    not fall through to the VariableCommands branch and be refused for `args`."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True})}, capture=cap))
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                           command="opendialog", args="Dialog=UI/Dialogs/D;AliasNode=;ParentItem=")
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert "command=OpenDialog" in p and "args=" in p
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                           command="TOGGLEVARIABLE", variable="Model/Flag")
    m, p = cap[-1]
    assert "command=ToggleVariable" in p


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


# ---- B19: wire_event `args` / `object_raw` / the UICommands command mode ----

def test_wire_event_forwards_args_with_method_path(alpha, monkeypatch):
    """B19 part 1: without `args` only argument-LESS methods are reachable,
    which excludes every method a navigation action needs (ChangePanel(NewPanel), …)."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True, "via": "eventhandler+args"})}, capture=cap))
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                           "UI/MainWindow/Loader/ChangePanel",
                           args="NewPanel=UI/Screens/ScreenB")
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert m == "POST" and "method=UI%2FMainWindow%2FLoader%2FChangePanel" in p
    assert "args=NewPanel%3DUI%2FScreens%2FScreenB" in p


def test_wire_event_forwards_object_raw_with_method_path(alpha, monkeypatch):
    """B19 part 2: an absolute NodeId names the DESIGN-TIME node, but a UI object
    is instantiated per session — so the call target must be late-bound."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True, "via": "eventhandler+lateobj"})}, capture=cap))
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                           "UI/MainWindow/Loader/ChangePanel",
                           object_raw="../Loader")
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert m == "POST" and "method=UI%2FMainWindow%2FLoader%2FChangePanel" in p
    assert "object_raw=..%2FLoader" in p


def test_wire_event_replace_true_forwards_param(alpha, monkeypatch):
    """replace=True must send replace=true to the bridge so the
    bridge can delete the existing handler and create a fresh one."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True, "replaced": True})}, capture=cap))
    out = core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                                 command="ToggleVariable", variable="Model/Flag",
                                 replace=True)
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert m == "POST" and "replace=true" in p
    assert out.get("replaced") is True


def test_wire_event_handler_exists_unsafe_raises_with_nudge(alpha, monkeypatch):
    """a structured handler_exists error (unsafe in-place rewrite)
    must raise BridgeWriteFailed whose message names the existing handler and
    mentions replace=True so the caller knows the recovery path."""
    handler_exists_body = {
        "ok": False,
        "error": {
            "code": "handler_exists",
            "message": (
                "a handler for this event type already exists and cannot be safely "
                "rewritten in place: EH_MouseClickEvent_Btn "
                "— use replace=true to delete it first"
            ),
            "existing": {
                "browse_name": "EH_MouseClickEvent_Btn",
                "target": "ns=2;i=1001/ChangePanel",
                "args": ["NewPanel", "AliasNode"],
            },
        },
    }
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, handler_exists_body)}))
    with pytest.raises(core.BridgeWriteFailed) as exc_info:
        core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                               method_path="UI/MainWindow/Loader/ChangePanel")
    msg = str(exc_info.value)
    # The handler name must appear in the raised message.
    assert "EH_MouseClickEvent_Btn" in msg
    # The replace=True recovery path must be mentioned.
    assert "replace=True" in msg or "replace=true" in msg


def test_wire_event_handler_exists_unsafe_no_existing_field(alpha, monkeypatch):
    """a handler_exists error without an 'existing' field (e.g.
    from an older bridge) still raises BridgeWriteFailed with a replace=True nudge."""
    handler_exists_body = {
        "ok": False,
        "error": {
            "code": "handler_exists",
            "message": "a handler for this event type already exists",
        },
    }
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, handler_exists_body)}))
    with pytest.raises(core.BridgeWriteFailed) as exc_info:
        core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                               command="ToggleVariable", variable="Model/Flag")
    msg = str(exc_info.value)
    assert "replace=True" in msg or "replace=true" in msg


def test_wire_event_replace_false_omits_param(alpha, monkeypatch):
    """replace=False (the default) must NOT add a replace param to the bridge
    call so the existing in-place / conflict-error behaviour is preserved."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True})}, capture=cap))
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                           command="ToggleVariable", variable="Model/Flag")
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert m == "POST" and "replace=" not in p


@pytest.mark.parametrize("command,extra", [
    ("SetVariable", {"variable": "Model/Flag", "value": "true"}),
    ("ToggleVariable", {"variable": "Model/Flag"}),
])
def test_wire_event_args_with_variable_command_raises(alpha, monkeypatch, command, extra):
    """The variable commands build their own InputArguments (VariableToModify/
    Value/ArrayIndex); a second source would silently drop one of them."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True})}, capture=cap))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                               command=command, args="NewPanel=UI/Screens/ScreenB", **extra)
    assert "`args` applies to method_path only" in str(e.value)
    # the guard fires before the write — nothing was POSTed
    assert not any("/bridge/node/event" in c[1] for c in cap)


@pytest.mark.parametrize("command", ["OpenDialog", "CloseDialog"])
def test_wire_event_ui_command_forwards_args(alpha, monkeypatch, command):
    """UI commands live on /Objects/Commands/UICommands, OUTSIDE the project
    root, so they are a `command` — and unlike the variable commands they take
    their arguments from `args`."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True})}, capture=cap))
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                           command=command,
                           args="Dialog=UI/Dialogs/Confirm;AliasNode=;ParentItem=")
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert m == "POST" and f"command={command}" in p
    assert "args=Dialog%3DUI%2FDialogs%2FConfirm%3BAliasNode%3D%3BParentItem%3D" in p
    # command mode is not the method path — no `method` param is sent
    assert "method=" not in p


def test_wire_event_object_raw_ignored_in_command_mode(alpha, monkeypatch):
    """Current contract, pinned deliberately: `object_raw` is method-path only —
    the command path resolves its own target (VariableCommands / UICommands), so
    an `object_raw` passed alongside a `command` is silently dropped, not an error."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, {"ok": True})}, capture=cap))
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                           command="OpenDialog",
                           args="Dialog=UI/Dialogs/Confirm;ParentItem=",
                           object_raw="../Loader")
    m, p = next(c for c in cap if "/bridge/node/event" in c[1])
    assert m == "POST" and "command=OpenDialog" in p
    assert "object_raw" not in p


def test_wire_event_op_spec_accepts_args_and_object_raw():
    """The batch surface must carry B19's two new fields: `unknown_op_fields`
    is a set difference against `_BRIDGE_EDIT_OPS`, so a field missing from the
    op spec would be rejected as an unknown field (and, since 1.0.8, warned on)."""
    _fn, _required, optional = core._BRIDGE_EDIT_OPS["wire_event"]
    assert "args" in optional and "object_raw" in optional
    assert core.unknown_op_fields(
        {"op": "wire_event", "path": "UI/MainWindow/Btn",
         "event_type": "MouseClickEvent",
         "method_path": "UI/MainWindow/Loader/ChangePanel",
         "args": "NewPanel=UI/Screens/ScreenB",
         "object_raw": "../Loader"}) == []


def test_wire_event_op_spec_accepts_replace():
    """1.0.8: replace must be in the batch op spec so a batch op
    that passes replace=True is not rejected as an unknown field."""
    _fn, _required, optional = core._BRIDGE_EDIT_OPS["wire_event"]
    assert "replace" in optional
    assert core.unknown_op_fields(
        {"op": "wire_event", "path": "UI/MainWindow/Btn",
         "event_type": "MouseClickEvent",
         "command": "ToggleVariable", "variable": "Model/Flag",
         "replace": True}) == []


def test_wire_event_in_place_update_returns_updated_and_changed(alpha, monkeypatch):
    """when the bridge returns updated:true with a changed list,
    bridge_wire_event passes the result back so callers can inspect it."""
    updated_body = {
        "ok": True,
        "updated": True,
        "node": "UI/MainWindow/Btn",
        "event": "MouseClickEvent",
        "via": "command:Toggle",
        "changed": ["Method", "InputArguments"],
    }
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, updated_body)}))
    out = core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                                 command="ToggleVariable", variable="Model/Flag")
    assert out["ok"] is True
    assert out.get("updated") is True
    assert out.get("changed") == ["Method", "InputArguments"]


def test_wire_event_inline_has_existing_handler_lookup():
    """1.0.8 static inspection: WireEventInline must contain an
    existing-handler lookup (scanning node.Children for EventHandler with
    matching ListenEventType) BEFORE the MakeObject<...EventHandler> call.
    This guards against the silent no-op where a duplicate handler was built
    unconditionally (the bug this task fixes)."""
    from pathlib import Path
    bridge_cs = (
        Path(__file__).parent.parent.parent / "studio-bridge" / "StudioMCPBridge.cs"
    )
    assert bridge_cs.exists(), f"StudioMCPBridge.cs not found at {bridge_cs}"
    text = bridge_cs.read_text(encoding="utf-8", errors="replace")

    # Locate WireEventInline.
    wire_start = text.find("private string WireEventInline(")
    assert wire_start >= 0, "WireEventInline not found in StudioMCPBridge.cs"
    # Locate the unconditional create path (MakeObject<FTOptix.CoreBase.EventHandler>).
    make_start = text.find('MakeObject<FTOptix.CoreBase.EventHandler>', wire_start)
    assert make_start >= 0, (
        "MakeObject<FTOptix.CoreBase.EventHandler> not found after WireEventInline"
    )
    # The existing-handler lookup must appear between WireEventInline and MakeObject.
    region = text[wire_start:make_start]
    assert "ListenEventType" in region, (
        "WireEventInline must scan for an existing handler by ListenEventType "
        "BEFORE the unconditional MakeObject<...EventHandler> call"
    )
    assert "existingHandler" in region, (
        "WireEventInline must record the existing handler before the create path"
    )

    # No ok:true return inside WireEventInline that isn't preceded by a readback.
    # Extract the WireEventInline body (up to the end of its catch block).
    wire_end = text.find("\n    private ", wire_start + 1)
    if wire_end < 0:
        wire_end = len(text)
    wire_body = text[wire_start:wire_end]
    # Every ok:true return must be preceded by some form of readback verification.
    # We check that "readback" or "rbObjPtr" or "rbMethodVar" appears in the body.
    assert ("readback" in wire_body or "rbObjPtr" in wire_body), (
        "WireEventInline must contain readback verification for updated:true responses"
    )


def test_wire_event_inline_no_unconditional_ok_true():
    """1.0.8 static inspection: WireEventInline must not contain any
    unconditional ok:true return.

    Two properties are verified against the C# source text:

    (1) An existing-handler lookup — scanning node.Children for an
        FTOptix.CoreBase.EventHandler whose ListenEventType matches, recorded
        in ``existingHandler`` — must appear BEFORE the
        ``MakeObject<FTOptix.CoreBase.EventHandler>`` call.  This gate ensures
        the create path is only reached when no matching handler exists (or
        when replace=true deleted it first).

    (2) Every ``"ok":true`` return that appears in the pre-create region
        (between the start of WireEventInline and the MakeObject call) must be
        preceded by a readback verification (rbObjPtr / rbMethodVar markers).
        This ensures the in-place-update path cannot silently claim success
        without confirming that the write actually landed.

    Together these block the silent no-op regression class: either a duplicate
    handler is created blindly, or an update is reported as ok without being
    verified.
    """
    import re
    from pathlib import Path

    bridge_cs = (
        Path(__file__).parent.parent.parent / "studio-bridge" / "StudioMCPBridge.cs"
    )
    assert bridge_cs.exists(), f"StudioMCPBridge.cs not found at {bridge_cs}"
    text = bridge_cs.read_text(encoding="utf-8", errors="replace")

    # --- Locate WireEventInline ---
    wire_start = text.find("private string WireEventInline(")
    assert wire_start >= 0, "WireEventInline not found in StudioMCPBridge.cs"

    # --- Locate the create-path marker within the function ---
    make_start = text.find("MakeObject<FTOptix.CoreBase.EventHandler>", wire_start)
    assert make_start >= 0, (
        "MakeObject<FTOptix.CoreBase.EventHandler> not found after WireEventInline; "
        "the create path must still be present"
    )

    # Region of WireEventInline before the unconditional create call.
    pre_make = text[wire_start:make_start]

    # --- Assertion (1): existing-handler lookup must precede MakeObject ---
    assert "existingHandler" in pre_make, (
        "WireEventInline must record the result of an existing-handler scan "
        "(existingHandler variable) BEFORE the MakeObject<...EventHandler> call; "
        "the gate that prevents duplicate/silent handler creation is missing"
    )
    assert "ListenEventType" in pre_make, (
        "WireEventInline must match by ListenEventType before creating a new handler; "
        "the identity-based existing-handler lookup is missing"
    )

    # --- Assertion (2): every ok:true in the pre-create region has readback ---
    # C# string literals escape double-quotes as \"; the literal file text is
    # therefore  \"ok\":true  (backslash + quote + ok + backslash + quote + ...).
    ok_true_re = re.compile(r'\\\"ok\\\":true')
    ok_true_hits = list(ok_true_re.finditer(pre_make))

    # The in-place update path MUST emit an ok:true in this region; if it was
    # accidentally removed the test should also fail (prevents vacuous pass).
    assert ok_true_hits, (
        "No ok:true return found in WireEventInline before MakeObject<...EventHandler>; "
        "the in-place update path may have been accidentally removed"
    )

    for m in ok_true_hits:
        # Scan up to 5000 chars before this ok:true for readback marker terms.
        preceding = pre_make[max(0, m.start() - 5000):m.start()]
        assert (
            "rbObjPtr" in preceding
            or "rbMethodVar" in preceding
            or "Read-back" in preceding
        ), (
            f"ok:true at pre-MakeObject offset {m.start()} in WireEventInline "
            "is not preceded by a readback verification (rbObjPtr / rbMethodVar / "
            "Read-back comment). All ok:true returns in the in-place-update path "
            "must be guarded by an explicit readback to prevent silent no-ops."
        )


def test_validate_expression_posts(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/expr/validate": (200, {"ok": True, "valid": True, "sources": 1})}, capture=cap))
    out = core.bridge_validate_expression(alpha, "Alpha", "if({0},1,2)", sources="Model/X")
    m, p = next(c for c in cap if "/bridge/expr/validate" in c[1])
    assert m == "POST" and "expression=if" in p and "sources=Model%2FX" in p
    assert out["valid"] is True


def test_validate_expression_invalid_literal_surfaces_reason_and_fix(alpha, monkeypatch):
    """bridge_validate_expression: when the bridge returns valid:false + an
    invalid_literal dict, the function must surface that dict unchanged so the
    caller can read the reason string and the fix string.

    Covers the design-time/runtime gap: expressions containing
    tokens the ExpressionEvaluator rejects at runtime (e.g. '#RRGGBB' hex colour
    literals, capitalised True/False booleans) must be caught at author time via
    a structured invalid_literal payload instead of a generic error string.
    No live bridge — _bridge_http is monkeypatched to return the exact shape
    the C# bridge emits."""
    invalid_literal_payload = {
        "reason": "hex color '#FF0000' is not a valid literal; the ExpressionEvaluator rejects it at runtime",
        "fix": "use a Color constructor: Color(255, 0, 0, 255)",
    }
    bridge_response = {
        "ok": True,
        "valid": False,
        "invalid_literal": invalid_literal_payload,
    }
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/expr/validate": (200, bridge_response)}))
    out = core.bridge_validate_expression(alpha, "Alpha", "#FF0000")

    assert out["valid"] is False
    assert "invalid_literal" in out, "invalid_literal field must be present in the returned dict"
    il = out["invalid_literal"]
    assert il["reason"] == invalid_literal_payload["reason"], (
        "reason string must be surfaced unchanged from the bridge response"
    )
    assert il["fix"] == invalid_literal_payload["fix"], (
        "fix string must be surfaced unchanged from the bridge response"
    )


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


def test_classify_handler_exists_surfaces_specific_nudge(alpha, monkeypatch):
    """a BridgeWriteFailed from a handler_exists rejection must
    carry a nudge that names the existing handler and mentions replace=True;
    classify_bridge_failure must surface that nudge (not the generic write_failed
    message) and must still return reason_code='write_failed' with bridge up."""
    handler_exists_body = {
        "ok": False,
        "error": {
            "code": "handler_exists",
            "message": (
                "a handler for this event type already exists and cannot be safely "
                "rewritten in place: EH_MouseClickEvent_Btn"
            ),
            "existing": {
                "browse_name": "EH_MouseClickEvent_Btn",
                "target": "ns=2;i=1001/ChangePanel",
            },
        },
    }
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, handler_exists_body)}))
    try:
        core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                               method_path="UI/MainWindow/Loader/ChangePanel")
    except core.BridgeWriteFailed as exc:
        out = core.classify_bridge_failure(alpha, "Alpha", exc)
        assert out["state"] == "failed"
        assert out["reason_code"] == "write_failed"
        assert out["bridge"]["reachable"] is True
        # nudge must name the existing handler and mention replace=True
        assert "EH_MouseClickEvent_Btn" in out["nudge"]
        assert "replace=True" in out["nudge"] or "replace=true" in out["nudge"]
        # must NOT be the generic connection-problem message
        assert "not a connection problem" not in out["nudge"]
        assert "handler_exists" in out["detail"]
    else:
        pytest.fail("BridgeWriteFailed was not raised for handler_exists")


def test_classify_handler_exists_no_existing_field_generic_nudge(alpha, monkeypatch):
    """handler_exists without an 'existing' field (older bridge)
    still produces a specific nudge mentioning replace=True."""
    handler_exists_body = {
        "ok": False,
        "error": {
            "code": "handler_exists",
            "message": "a handler for this event type already exists",
        },
    }
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/event": (200, handler_exists_body)}))
    try:
        core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                               command="ToggleVariable", variable="Model/Flag")
    except core.BridgeWriteFailed as exc:
        out = core.classify_bridge_failure(alpha, "Alpha", exc)
        assert out["reason_code"] == "write_failed"
        assert out["bridge"]["reachable"] is True
        assert "replace=True" in out["nudge"] or "replace=true" in out["nudge"]
        assert "not a connection problem" not in out["nudge"]
    else:
        pytest.fail("BridgeWriteFailed was not raised for handler_exists")


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


# --- bridge_attach_formatter acceptance tests ---

def test_bridge_attach_formatter_posts_correct_params(alpha, monkeypatch):
    """Roundtrip: a formatted dynamic link with a named {#token} source is
    accepted, POSTed to the attach-formatter endpoint, and ok:true returned.

    Confirms the path/name/format/sources params round-trip through the
    query-string encoder so the bridge sees the right values.  Any existing
    converter or DynamicLink on the property is replaced server-side — the
    test's ok:true return mirrors that contract.
    """
    cap: list = []
    routes = {
        "/bridge/node/attach-formatter": (
            200, {"ok": True, "via": "attach-formatter",
                  "node": "UI/MainWindow/L1", "prop": "Text"}),
    }
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_attach_formatter(
        alpha, "Alpha",
        "UI/MainWindow/L1", "Text",
        "../NavPanel{#navIdx}@NodeId",
        sources="navIdx=Model/GlobalVariables/AlarmTab",
    )
    assert out["ok"] is True
    method, path = next(c for c in cap if "/bridge/node/attach-formatter" in c[1])
    assert method == "POST"
    assert "path=UI%2FMainWindow%2FL1" in path
    assert "name=Text" in path
    assert "format=" in path and "navIdx" in path
    assert "sources=" in path and "Model%2FGlobalVariables%2FAlarmTab" in path


def test_bridge_attach_formatter_mode_forwarded(alpha, monkeypatch):
    """Optional `mode` parameter is included in the POST query-string."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/attach-formatter": (200, {"ok": True})},
                                     capture=cap))
    core.bridge_attach_formatter(
        alpha, "Alpha", "UI/MainWindow/L1", "Text",
        "../NavPanel{#navIdx}@NodeId",
        sources="navIdx=Model/Nav", mode="ReadWrite",
    )
    _, path = next(c for c in cap if "/bridge/node/attach-formatter" in c[1])
    assert "mode=ReadWrite" in path


def test_bridge_attach_formatter_no_sources_no_mode_omits_params(alpha, monkeypatch):
    """When sources and mode are absent the query-string omits those keys."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/attach-formatter": (200, {"ok": True})},
                                     capture=cap))
    core.bridge_attach_formatter(
        alpha, "Alpha", "UI/MainWindow/L1", "Panel", "{#key}@NodeId")
    _, path = next(c for c in cap if "/bridge/node/attach-formatter" in c[1])
    assert "sources=" not in path
    assert "mode=" not in path


def test_attach_formatter_node_attribute_rejected_before_dispatch(alpha, monkeypatch):
    """Missing-required-parameter guard: node attributes (DisplayName, BrowseName,
    Description, NodeId, NodeClass) are not settable as DynamicLink targets —
    the pre-dispatch check raises BridgeWriteFailed with a clear error code and
    no HTTP call is made to the bridge.
    """
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    with pytest.raises(core.BridgeWriteFailed) as exc_info:
        core.bridge_attach_formatter(
            alpha, "Alpha", "UI/MainWindow/L1", "DisplayName",
            "../NavPanel{#navIdx}@NodeId",
            sources="navIdx=Model/Nav")
    assert "node_attribute_not_settable" in str(exc_info.value)
    # The guard must fire BEFORE any HTTP round-trip.
    assert not [c for c in cap if "/bridge/node/attach-formatter" in c[1]]


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


# ---- GridLayoutProperties / UI-namespace-child handling ----

def test_create_widget_grid_layout_props_success_proxy(alpha, monkeypatch):
    """bridge_create_widget("GridLayoutProperties") uses _bridge_write_ui_ns_child;
    on a proxy-property success the wrapper injects ui_namespace_child and note."""
    cap: list = []
    routes = {"/bridge/ui/widget": (200, {
        "ok": True, "created_path": "UI/MainWindow/MyGrid/GridLayoutProperties",
        "namespace": "FTOptix.UI", "via": "proxy-property",
        "mode": "inline", "thread": "http-bg",
    })}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_widget(
        alpha, "Alpha",
        "UI/MainWindow/MyGrid", "GridLayoutProperties", "GridLayoutProperties")
    assert out["ok"] is True
    assert out["ui_namespace_child"] is True
    assert "bridge older than 1.0.8" in out["note"]
    assert out["namespace"] == "FTOptix.UI"
    method, path = next(c for c in cap if "/bridge/ui/widget" in c[1])
    assert method == "POST"
    assert "name=GridLayoutProperties" in path


def test_create_object_grid_layout_props_success_ns_create(alpha, monkeypatch):
    """bridge_create_object("GridLayoutProperties") routes through ui-ns helper;
    namespace-qualified-create success injects ui_namespace_child:True."""
    routes = {"/bridge/model/object": (200, {
        "ok": True, "created_path": "UI/MainWindow/MyGrid/GridLayoutProperties",
        "namespace": "FTOptix.UI", "via": "namespace-qualified-create",
        "mode": "inline", "thread": "http-bg",
    })}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_create_object(
        alpha, "Alpha", "UI/MainWindow/MyGrid", "GridLayoutProperties")
    assert out["ok"] is True
    assert out["ui_namespace_child"] is True
    assert "bridge older than 1.0.8" in out["note"]


def test_create_widget_grid_layout_props_wrong_namespace_raises(alpha, monkeypatch):
    """wrong_namespace bridge response raises BridgeWriteFailed with created_path."""
    routes = {"/bridge/ui/widget": (200, {
        "ok": False, "error": "wrong_namespace",
        "child_ns": 2, "ui_ns": 1,
        "created_path": "UI/MainWindow/MyGrid/GridLayoutProperties",
    })}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as exc_info:
        core.bridge_create_widget(
            alpha, "Alpha",
            "UI/MainWindow/MyGrid", "GridLayoutProperties", "GridLayoutProperties")
    msg = str(exc_info.value)
    assert "wrong_namespace" in msg
    assert "created_path=" in msg


def test_create_object_grid_layout_props_unsupported_raises(alpha, monkeypatch):
    """ui_namespace_child_unsupported bridge response raises BridgeWriteFailed."""
    routes = {"/bridge/model/object": (200, {
        "ok": False, "error": "ui_namespace_child_unsupported",
    })}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as exc_info:
        core.bridge_create_object(
            alpha, "Alpha", "UI/MainWindow/MyGrid", "GridLayoutProperties")
    assert "ui_namespace_child_unsupported" in str(exc_info.value)


def test_create_widget_non_grid_props_name_no_extra_keys(alpha, monkeypatch):
    """A create_widget call for a name NOT in _UI_NAMESPACE_CHILDREN (e.g.
    'Rectangle1') must use the plain _bridge_write path: the result must NOT
    contain ui_namespace_child or note keys injected by the ui-ns helper."""
    routes = {"/bridge/ui/widget": (200, {
        "ok": True, "created_path": "UI/MainWindow/Rectangle1",
        "type": "Rectangle",
    })}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_create_widget(
        alpha, "Alpha", "UI/MainWindow", "Rectangle1", "Rectangle")
    assert out["ok"] is True
    assert "ui_namespace_child" not in out, (
        "non-special name must not carry ui_namespace_child — "
        "_bridge_write_ui_ns_child was invoked when it should not have been"
    )
    assert "note" not in out, (
        "non-special name must not carry a note nudge — "
        "_bridge_write_ui_ns_child was invoked when it should not have been"
    )


def test_bridge_edit_grid_layout_props_two_op_batch(alpha, monkeypatch):
    """A two-op batch [create_widget(GridLayoutProperties), set_property(RowStart)]
    must validate and apply fully — bridge_edit must handle ui_namespace_child
    results from the create_widget half without short-circuiting the batch.

    Pins the interaction between bridge_edit's apply loop and the extra keys
    (_bridge_write_ui_ns_child injects ui_namespace_child:True and note) that
    bridge_create_widget emits for GridLayoutProperties names."""
    applied: list = []
    ok_report = {"ok": True, "op_count": 2, "strict": False,
                 "errors": [], "warnings": []}
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(ok_report))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    # Track which ops are applied and fake the per-op bridge calls.
    def fake_apply(cfg, project, op):
        applied.append(op["op"])
        return {"ok": True}

    monkeypatch.setattr(core, "_apply_one_edit", fake_apply)

    ops = [
        {"op": "create_widget", "screen": "UI/MainWindow/MyGrid",
         "name": "GridLayoutProperties", "widget_type": "GridLayoutProperties"},
        {"op": "set_property", "path": "UI/MainWindow/MyGrid/GridLayoutProperties",
         "name": "RowStart", "value": "0"},
    ]
    out = core.bridge_edit(alpha, "Alpha", ops)

    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["applied"] == 2 and out["op_count"] == 2
    assert applied == ["create_widget", "set_property"]


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


# ---- unknown_property/'Value' pre-1.0.8 bridge nudge -----------------------
#
# Pre-1.0.8 bridges reject name='Value' on IUAVariable nodes as unknown_property
# ("has no settable property 'Value'"). The Python write paths detect this and
# append the working alternative spelling: path=<parent>, name=<VariableName>.
# This lets the LLM caller self-correct without a describe_type round-trip.

def test_value_property_nudge_appended_to_bridge_set_property_error(alpha, monkeypatch):
    """bridge_set_property(name='Value') with a pre-1.0.8 unknown_property response
    must append the parent/name workaround spelling to the raised BridgeWriteFailed
    message so the caller can self-correct immediately."""
    routes = {"/bridge/node/property": (200, {"error": {
        "code": "unknown_property",
        "message": "Model/MyVar has no settable property 'Value' "
                   "(call describe_type/describe_node for the valid set)",
        "valid_properties": []}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_set_property(alpha, "Alpha", "Model/MyVar", "Value", "42")
    msg = str(e.value)
    assert "unknown_property" in msg
    assert "pre-1.0.8" in msg, "nudge must name the bridge-version cause"
    assert "path='Model'" in msg, "nudge must give the parent path"
    assert "name='MyVar'" in msg, "nudge must give the variable name as property name"


def test_value_property_nudge_not_added_for_non_value_name(alpha, monkeypatch):
    """The pre-1.0.8 nudge must NOT be appended when name != 'Value' — only the
    exact 'Value' property on a variable triggers the rewrite suggestion."""
    routes = {"/bridge/node/property": (200, {"error": {
        "code": "unknown_property",
        "message": "Node has no settable property 'Zzz' "
                   "(call describe_type/describe_node for the valid set)",
        "valid_properties": []}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_set_property(alpha, "Alpha", "Model/MyVar", "Zzz", "42")
    msg = str(e.value)
    assert "pre-1.0.8" not in msg, "nudge must only appear for name='Value'"
    assert "unknown_property" in msg


def test_value_property_nudge_in_bridge_edit_report_error(alpha, monkeypatch):
    """bridge_edit: a validation report error with unknown_property/'Value' must
    have the pre-1.0.8 nudge appended to its message field so the LLM caller
    sees the workaround inside the returned report (not just in a top-level nudge)."""
    _bad_value_report = {
        "ok": False, "op_count": 1, "strict": False, "warnings": [],
        "errors": [{"op_index": 0, "code": "unknown_property",
                    "message": "Model/MyVar has no settable property 'Value' "
                               "(call describe_type/describe_node for the valid set)"}],
    }
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_bad_value_report))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    ops = [{"op": "set_property", "path": "Model/MyVar", "name": "Value", "value": "42"}]
    out = core.bridge_edit(alpha, "Alpha", ops)

    assert out["state"] == "validated"
    err_msg = out["report"]["errors"][0]["message"]
    assert "pre-1.0.8" in err_msg, "nudge must name the bridge-version cause in report error"
    assert "path='Model'" in err_msg, "nudge must give the parent path"
    assert "name='MyVar'" in err_msg, "nudge must give the variable name as property name"


def test_value_property_nudge_bridge_edit_state_validated_applied_zero(alpha, monkeypatch):
    """bridge_edit: unknown_property/'Value' validation failure must leave state=='validated'
    and applied==0 — nothing is written to the model when validation rejects the batch."""
    _bad_value_report = {
        "ok": False, "op_count": 1, "strict": False, "warnings": [],
        "errors": [{"op_index": 0, "code": "unknown_property",
                    "message": "Model/Temp has no settable property 'Value' "
                               "(call describe_type/describe_node for the valid set)"}],
    }
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_bad_value_report))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    ops = [{"op": "set_property", "path": "Model/Temp", "name": "Value", "value": "22.3"}]
    out = core.bridge_edit(alpha, "Alpha", ops)

    assert out["state"] == "validated", "dirty report must not proceed to apply"
    assert out["applied"] == 0, "applied must be zero when validation rejects the batch"
    assert applied == [], "no op must be dispatched when validation failed"
    err_msg = out["report"]["errors"][0]["message"]
    assert "pre-1.0.8" in err_msg, "nudge suffix must be present in the report error"
    assert "path='Model'" in err_msg, "nudge must carry the parent path"
    assert "name='Temp'" in err_msg, "nudge must carry the variable name"


def test_value_property_clean_report_applies_exactly_one_op(alpha, monkeypatch):
    """bridge_edit: a clean validation report for set_property name='Value' must
    apply exactly one op — confirming the Python layer never had its own Value
    rejection that would silently refuse a valid batch."""
    _clean_report = {"ok": True, "op_count": 1, "strict": False,
                     "errors": [], "warnings": []}
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_clean_report))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    ops = [{"op": "set_property", "path": "Model/Temp", "name": "Value", "value": "22.3"}]
    out = core.bridge_edit(alpha, "Alpha", ops)

    assert out["state"] == "succeeded", (
        "a clean validation report for name='Value' must succeed — "
        "the Python layer must have no own Value rejection"
    )
    assert out["applied"] == 1, "exactly one op must be applied"
    assert applied == ["set_property"], "the set_property op must reach _apply_one_edit"


def test_value_property_nudge_not_added_in_bridge_edit_for_other_property(alpha, monkeypatch):
    """bridge_edit: an unknown_property error on a property OTHER than 'Value'
    must NOT receive the pre-1.0.8 Value nudge — the nudge is Value-specific."""
    _bad_report = {
        "ok": False, "op_count": 1, "strict": False, "warnings": [],
        "errors": [{"op_index": 0, "code": "unknown_property",
                    "message": "Panel has no settable property 'BorderThickness' "
                               "(call describe_type/describe_node for the valid set)"}],
    }
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_bad_report))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    ops = [{"op": "set_property", "path": "UI/MainWindow/P1",
            "name": "BorderThickness", "value": "2"}]
    out = core.bridge_edit(alpha, "Alpha", ops)

    assert out["state"] == "validated"
    err_msg = out["report"]["errors"][0]["message"]
    assert "pre-1.0.8" not in err_msg, "Value nudge must not appear for non-Value properties"
    assert "unknown_property" in err_msg or "BorderThickness" in err_msg


def test_validate_on_node_cs_contains_iuavariable_self_value_branch():
    """Static assertion: StudioMCPBridge.cs ValidateOnNode must contain the
    IUAVariable self-value branch (line ~2771 in the applier; added symmetrically
    to the validator so pre-1.0.8 bridges stop emitting unknown_property for
    name='Value' on variable nodes).

    Greps for 'IUAVariable selfVar' within the ValidateOnNode region and the
    cite comment ('SetPropertyInline line 2771') that ties the validator branch
    to the applier's existing mirror."""
    import re
    cs_path = (
        __file__                        # …/service/tests/test_bridge_writes.py
        .replace("service/tests/test_bridge_writes.py", "")
        .replace("service\\tests\\test_bridge_writes.py", "")
    )
    # Resolve via pathlib relative to this test file's directory
    from pathlib import Path
    bridge_cs = Path(__file__).parent.parent.parent / "studio-bridge" / "StudioMCPBridge.cs"
    assert bridge_cs.exists(), f"StudioMCPBridge.cs not found at {bridge_cs}"

    text = bridge_cs.read_text(encoding="utf-8", errors="replace")

    # Locate the ValidateOnNode method body.
    validate_start = text.find("private void ValidateOnNode(")
    assert validate_start >= 0, "ValidateOnNode method not found in StudioMCPBridge.cs"

    # Grab a generous window around the method (up to ~250 lines after the
    # opening brace) — enough to contain the IUAVariable self-value branch.
    validate_region = text[validate_start:validate_start + 8000]

    assert "IUAVariable selfVar" in validate_region, (
        "ValidateOnNode must contain the IUAVariable self-value branch "
        "(set_property name='Value' on a variable node must bypass DeclaredPropertyGuard)"
    )
    assert "SetPropertyInline" in validate_region or "2771" in validate_region, (
        "ValidateOnNode must cite the mirror in SetPropertyInline (line 2771) "
        "to document validator/applier parity for the Value branch"
    )


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


def test_bridge_edit_report_entry_carries_invalid_literal_unmodified(alpha, monkeypatch):
    """bridge_edit: when the bridge's validate_ops report contains an error entry
    that has an invalid_literal field, that field must be passed through
    unmodified in out['report']['errors'][0]['invalid_literal'].

    Covers the 1.0.8 epic: the C# bridge emits invalid_literal on
    expressions that contain tokens the ExpressionEvaluator rejects at runtime
    (capitalised True/False booleans, bare NodeId results, #RRGGBB hex colour
    literals). Python must not strip or transform this field — the LLM caller
    reads reason and fix directly from the report entry.

    No live bridge — _bridge_post_body is monkeypatched to return the exact
    report shape the C# bridge emits for an invalid_literal violation."""
    _invalid_literal_entry = {
        "reason": "capitalised 'True' is not a valid Boolean literal in Optix expressions",
        "fix": "use lowercase 'true'",
    }
    _invalid_literal_report = {
        "ok": False,
        "op_count": 1,
        "strict": False,
        "warnings": [],
        "errors": [
            {
                "op_index": 0,
                "code": "invalid_literal",
                "message": (
                    "'True' is not a valid Boolean literal; "
                    "the ExpressionEvaluator rejects it at runtime"
                ),
                "invalid_literal": _invalid_literal_entry,
            }
        ],
    }
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_invalid_literal_report))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    ops = [{"op": "attach_expression", "path": "UI/MainWindow/L1",
            "prop_name": "Visible", "expression": "True"}]
    out = core.bridge_edit(alpha, "Alpha", ops)

    assert out["state"] == "validated", "validation must refuse when report is not ok"
    assert out["applied"] == 0, "nothing must be applied when the report has errors"

    err = out["report"]["errors"][0]
    assert err["code"] == "invalid_literal"
    assert "invalid_literal" in err, (
        "invalid_literal key must be present in the report error entry"
    )
    il = err["invalid_literal"]
    assert il["reason"] == _invalid_literal_entry["reason"], (
        "reason string must be passed through unmodified from the bridge report"
    )
    assert il["fix"] == _invalid_literal_entry["fix"], (
        "fix string must be passed through unmodified from the bridge report"
    )


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


# --- I32 (2026-09-03): `type` is the raw-HTTP spelling of
# create_widget's `widget_type` and create_object's `object_type`. Through
# bridge_edit it was an unknown field — a WARNING only under the default
# non-strict mode — so it was dropped and the op's own default produced a
# `Label` / bare `UAObject` while the batch reported `succeeded`. Prior art:
# gap-register B4 tabulated the field names but recorded the failure as a
# rejection, which holds only for REQUIRED fields.

def test_normalize_edit_op_aliases_type_to_widget_type():
    out = core._normalize_edit_op(
        {"op": "create_widget", "screen": "UI/MainWindow", "name": "PL",
         "type": "PanelLoader"})
    assert out["widget_type"] == "PanelLoader"
    # the alias key is REMOVED, so the unknown-field check downstream stays
    # quiet about a field this service just consumed
    assert "type" not in out
    assert core.unknown_op_fields(out) == []


def test_normalize_edit_op_aliases_type_to_object_type():
    out = core._normalize_edit_op(
        {"op": "create_object", "parent": "Model", "name": "Rec",
         "type": "RecipeType"})
    assert out["object_type"] == "RecipeType"
    assert "type" not in out
    assert core.unknown_op_fields(out) == []


def test_normalize_edit_op_raises_when_type_and_canonical_disagree():
    with pytest.raises(core.BridgeWriteFailed) as ei:
        core._normalize_edit_op(
            {"op": "create_widget", "screen": "UI/MainWindow", "name": "PL",
             "widget_type": "Rectangle", "type": "PanelLoader"})
    msg = str(ei.value)
    # both values named — never silently pick one
    assert "Rectangle" in msg and "PanelLoader" in msg
    with pytest.raises(core.BridgeWriteFailed):
        core._normalize_edit_op(
            {"op": "create_object", "parent": "Model", "name": "R",
             "object_type": "A", "type": "B"})
    # agreeing spellings are not a conflict — the alias key just goes away
    agree = core._normalize_edit_op(
        {"op": "create_widget", "screen": "UI/M", "name": "PL",
         "widget_type": "PanelLoader", "type": "PanelLoader"})
    assert agree["widget_type"] == "PanelLoader" and "type" not in agree


def test_normalize_edit_op_leaves_canonical_type_spelling_untouched():
    """No `type` key -> identity return, as for every other non-aliased op."""
    cw = {"op": "create_widget", "screen": "UI/M", "name": "B1",
          "widget_type": "Rectangle"}
    assert core._normalize_edit_op(cw) is cw
    co = {"op": "create_object", "parent": "Model", "name": "O",
          "object_type": "FolderType"}
    assert core._normalize_edit_op(co) is co
    # caller's dict is never mutated when the alias IS applied
    orig = {"op": "create_widget", "screen": "UI/M", "name": "B1",
            "type": "PanelLoader"}
    core._normalize_edit_op(orig)
    assert orig == {"op": "create_widget", "screen": "UI/M", "name": "B1",
                    "type": "PanelLoader"}


def test_type_on_a_verb_without_the_alias_is_still_an_unknown_field(
    alpha, monkeypatch,
):
    """The alias is scoped to create_widget/create_object. `type` anywhere else
    must keep reporting unknown_op_field — this fix must not turn `type` into a
    globally-swallowed field."""
    assert core.unknown_op_fields(
        {"op": "set_property", "path": "UI/M/R", "name": "Width",
         "value": "10", "type": "Rectangle"}) == ["type"]
    sp = {"op": "set_property", "path": "UI/M/R", "name": "Width",
          "value": "10", "type": "Rectangle"}
    assert core._normalize_edit_op(sp) is sp  # untouched: not an aliased verb

    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op))
    out = core.bridge_edit(alpha, "Alpha", [dict(sp)])
    warn = [w for w in out["report"]["warnings"]
            if w["code"] == "unknown_op_field"]
    assert warn and warn[0]["unknown_fields"] == ["type"]


def test_bridge_edit_applies_the_type_alias_end_to_end(alpha, monkeypatch):
    """An HTTP-first caller's make_op(..., type=...) shape must reach the applier
    as widget_type, with no unknown_op_field warning and no silent Label."""
    applied: list = []
    seen: list = []
    monkeypatch.setattr(core, "_bridge_post_body",
                        _fake_validate(_OK_REPORT, seen=seen))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op))

    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_widget", "screen": "UI/MainWindow", "name": "PL",
         "type": "PanelLoader"},
        {"op": "create_object", "parent": "Model", "name": "Rec",
         "type": "RecipeType"}])

    assert out["state"] == "succeeded" and out["applied"] == 2
    assert applied[0]["widget_type"] == "PanelLoader"
    assert applied[1]["object_type"] == "RecipeType"
    # the bridge's validator sees the normalised ops too
    assert "type" not in seen[0][1]["ops"][0]
    assert out["report"]["warnings"] == []


def test_create_widget_without_any_type_warns_about_the_label_default(
    alpha, monkeypatch,
):
    """The Label default is legitimate, but it must be VISIBLE — a wrong-typed
    node passes every subsequent model read (I32)."""
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op))

    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_widget", "screen": "UI/MainWindow", "name": "B1"}])

    assert out["state"] == "succeeded"  # a warning, never a refusal
    warn = [w for w in out["report"]["warnings"]
            if w["code"] == "default_widget_type"]
    assert len(warn) == 1
    assert warn[0]["default"] == "Label" and warn[0]["op_index"] == 0
    # ...and it does NOT fire when a type was supplied under either spelling
    for op in ({"op": "create_widget", "screen": "UI/M", "name": "B",
                "widget_type": "Rectangle"},
               {"op": "create_widget", "screen": "UI/M", "name": "B",
                "type": "Rectangle"}):
        rep = core.bridge_edit(alpha, "Alpha", [op])["report"]
        assert [w for w in rep["warnings"]
                if w["code"] == "default_widget_type"] == []


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


# --- 1.0.8: new bridge report codes pass through intact --------------------
# The bridge validator gained a hypothetical model that introduces new codes:
#   moved_earlier_in_batch, routed_into_collection, ambiguous_container,
#   not_renderable, read_only_collection.
# bridge_validate_ops and bridge_edit are plain pass-throughs for the report
# dict — any top-level key the bridge adds must survive to the caller.

def test_bridge_edit_passes_through_moved_earlier_in_batch(alpha, monkeypatch):
    """A fake report carrying `moved_earlier_in_batch` must reach the caller
    with op_index, code, and message intact, state='validated', applied=0."""
    report_with_new_code = {
        "ok": False,
        "op_count": 2,
        "strict": False,
        "errors": [{
            "op_index": 1,
            "code": "moved_earlier_in_batch",
            "message": "path 'UI/Screens/Foo' was moved to 'UI/Screens/Bar' by op 0",
            "new_path": "UI/Screens/Bar",
        }],
        "warnings": [],
    }
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body",
                        _fake_validate(report_with_new_code))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "validated"
    assert out["applied"] == 0 and applied == []
    err = out["report"]["errors"][0]
    assert err["code"] == "moved_earlier_in_batch"
    assert err["op_index"] == 1
    assert "UI/Screens/Bar" in err["message"]
    assert err["new_path"] == "UI/Screens/Bar"


def test_bridge_edit_passes_through_routed_into_collection_warning(alpha, monkeypatch):
    """routed_into_collection is a per-op WARNING (not an error) — the batch
    must apply and the warning must reach the caller intact."""
    report_with_routing_warn = {
        "ok": True,
        "op_count": 1,
        "strict": False,
        "errors": [],
        "warnings": [{
            "op_index": 0,
            "code": "routed_into_collection",
            "message": ("create_widget routed into 'NavigationPanel.Panels'; "
                        "actual path: UI/Nav/Panels/Tab1"),
            "routed_path": "UI/Nav/Panels/Tab1",
        }],
    }
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body",
                        _fake_validate(report_with_routing_warn))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_widget", "screen": "UI/Nav", "name": "Tab1",
         "widget_type": "NavigationPanelItem"},
    ])

    assert out["state"] == "succeeded"
    assert out["applied"] == 1 and applied == ["create_widget"]
    warns = out["report"]["warnings"]
    routing_warns = [w for w in warns if w["code"] == "routed_into_collection"]
    assert len(routing_warns) == 1
    assert routing_warns[0]["op_index"] == 0
    assert routing_warns[0]["routed_path"] == "UI/Nav/Panels/Tab1"


def test_bridge_edit_passes_through_ambiguous_container_error(alpha, monkeypatch):
    """ambiguous_container is an error from the bridge validator — must reach
    the caller with all fields intact and refuse the batch (applied=0)."""
    report_with_ambiguous = {
        "ok": False,
        "op_count": 1,
        "strict": False,
        "errors": [{
            "op_index": 0,
            "code": "ambiguous_container",
            "message": ("create_widget matched 2 placeholder collections under "
                        "'UI/Chart' — pass the explicit sub-path"),
            "candidates": ["UI/Chart/Pens", "UI/Chart/Zones"],
        }],
        "warnings": [],
    }
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body",
                        _fake_validate(report_with_ambiguous))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op))

    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_widget", "screen": "UI/Chart", "name": "P1",
         "widget_type": "Pen"},
    ])

    assert out["state"] == "validated"
    assert out["applied"] == 0 and applied == []
    err = out["report"]["errors"][0]
    assert err["code"] == "ambiguous_container"
    assert err["candidates"] == ["UI/Chart/Pens", "UI/Chart/Zones"]


def test_bridge_edit_preserves_extra_top_level_key_alongside_python_warning(
    alpha, monkeypatch,
):
    """An extra top-level key the bridge adds (e.g. 'hypothetical') must
    survive the report merge alongside a Python-side unknown-op-field warning.
    Regression guard: report = dict(bridge_validate_ops(...)) must not strip
    keys it doesn't know about."""
    report_with_extra_key = {
        "ok": True,
        "op_count": 1,
        "strict": False,
        "errors": [],
        "warnings": [],
        # Hypothetical future key from the bridge — Python must not discard it.
        "hypothetical": {"relocated": {}, "created": {}},
    }
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body",
                        _fake_validate(report_with_extra_key))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    # create_folder with an unknown field triggers a Python-side warning.
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_folder", "parent": "Model", "name": "X",
         "extra_field": "should_warn"},
    ])

    assert out["state"] == "succeeded"
    report = out["report"]
    # The extra bridge key must be present.
    assert "hypothetical" in report
    assert report["hypothetical"] == {"relocated": {}, "created": {}}
    # The Python-side unknown-field warning must also be present.
    field_warns = [w for w in report.get("warnings", [])
                   if w["code"] == "unknown_op_field"]
    assert len(field_warns) == 1
    assert "extra_field" in field_warns[0]["unknown_fields"]


# --- Static inspection: the C# bridge carries the hypothetical-model symbols --
# This is the only offline gate confirming the C# half was written.  A live
# bridge build is verified by live Tier-2 CI; this test protects against a
# Python-only cherry-pick that forgets to include the .cs changes.

def test_bridge_cs_contains_hypothetical_model_symbols():
    """studio-bridge/StudioMCPBridge.cs must contain the four symbols that
    implement the hypothetical model introduced by 1.0.8.  If this test
    fails the C# half is missing and the new bridge codes will never fire."""
    from pathlib import Path
    cs = Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs"
    assert cs.is_file(), f"C# bridge file not found at {cs}"
    text = cs.read_text(encoding="utf-8", errors="ignore")
    for symbol in ("HypoResolve", "relocated", "moved_earlier_in_batch",
                   "routed_into_collection"):
        assert symbol in text, (
            f"symbol {symbol!r} missing from StudioMCPBridge.cs — "
            "the hypothetical-model C# half may not have been committed"
        )


# --- Hypothetical-model ordering contract and pre-flight pin tests ------------
# These three fixtures pin the Python contract that the C# hypothetical model
# depends on: op ordering is preserved through the full batch, destructive
# unknown-field errors prevent application entirely, and default_widget_type
# remains a warning (never an error) regardless of strict mode.


def test_bridge_edit_ordering_contract_create_move_set(alpha, monkeypatch):
    """A batch of [create_widget, move(+new_name), set_property] in that order
    must be accepted when the bridge validator reports ok=True.

    This pins the ordering contract the C# hypothetical model depends on: the
    validator accumulates creates and moves in batch order, so a set_property
    that targets the widget's post-move path is accepted rather than rejected
    as 'unresolved reference'.  Python must forward all three ops to the
    applier in the same sequence the caller specified."""
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body",
                        _fake_validate({"ok": True, "op_count": 3, "strict": False,
                                        "errors": [], "warnings": []}))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    ops = [
        {"op": "create_widget", "screen": "UI/Screens/Main",
         "name": "MyBtn", "widget_type": "Button"},
        {"op": "move", "path": "UI/Screens/Main/MyBtn",
         "new_parent": "UI/Screens/Main/Toolbar", "new_name": "OK"},
        {"op": "set_property", "path": "UI/Screens/Main/Toolbar/OK",
         "name": "Width", "value": "80"},
    ]
    out = core.bridge_edit(alpha, "Alpha", ops)

    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["applied"] == 3
    assert applied == ["create_widget", "move", "set_property"]


def test_move_with_unknown_field_refuses_and_never_applies(alpha, monkeypatch):
    """A move op carrying an unknown field must be refused by Python's
    destructive-op hard-fail check (unknown_op_field in errors, ok=False).

    The apply bridge (_apply_one_edit) must never be invoked — verified via an
    explicit 'applied' list rather than an exception, so the zero-apply
    assertion appears directly in the test output rather than as a side-effect
    of a throw.  Regression guard: a move with an unknown field must not
    silently widen to a whole-node move against the real `path`."""
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body",
                        _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "move", "path": "UI/Screens/Foo",
         "new_parent": "UI/Screens/Bar", "bogus": 1},
    ])

    assert out["report"]["ok"] is False
    assert out["applied"] == 0
    assert applied == [], "apply bridge must not be called when a field error is detected"
    err = out["report"]["errors"][0]
    assert err["code"] == "unknown_op_field"
    assert "bogus" in err["unknown_fields"]


def test_default_widget_type_is_always_a_warning_never_an_error_even_under_strict(
    alpha, monkeypatch,
):
    """default_widget_type must never refuse the batch — strict or not.

    A missing widget_type is not a field typo; it engages the documented 'Label'
    default, which is legitimate (a caller wanting a Label should not be broken).
    Under strict=True, unknown FIELD NAMES become errors; but a missing optional
    field that has a documented default is always a WARNING so an otherwise-clean
    batch is never refused.  This drives bridge_edit twice (non-strict, strict)
    and confirms state='succeeded' and the warning fires in both modes."""
    for strict in (False, True):
        applied: list = []
        monkeypatch.setattr(core, "_bridge_post_body",
                            _fake_validate(_OK_REPORT))
        monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
        monkeypatch.setattr(core, "_apply_one_edit",
                            lambda cfg, project, op: applied.append(op["op"]))

        out = core.bridge_edit(alpha, "Alpha", [
            {"op": "create_widget", "screen": "UI/MainWindow", "name": "Lbl"},
        ], strict=strict)

        assert out["state"] == "succeeded", (
            f"strict={strict}: default_widget_type must not refuse the batch"
        )
        assert out["applied"] == 1 and "create_widget" in applied
        warns = [w for w in out["report"]["warnings"]
                 if w["code"] == "default_widget_type"]
        assert len(warns) == 1, (
            f"strict={strict}: expected exactly one default_widget_type warning"
        )
        assert warns[0]["default"] == "Label"
        errs = [e for e in out["report"].get("errors", [])
                if e.get("code") == "default_widget_type"]
        assert errs == [], (
            f"strict={strict}: default_widget_type must be a warning, never an error"
        )


# ---- bridge_reorder_node: local-validation and result passthrough tests -----

def test_reorder_position_and_index_together_raises(alpha, monkeypatch):
    """Supplying both position and index locally raises BridgeWriteFailed."""
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    with pytest.raises(core.BridgeWriteFailed) as exc:
        core.bridge_reorder_node(alpha, "Alpha", "UI/Screens/Foo",
                                 position="front", index=2)
    assert "position OR index" in str(exc.value)


def test_reorder_negative_index_raises(alpha, monkeypatch):
    """A negative index is rejected locally before hitting the bridge."""
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    with pytest.raises(core.BridgeWriteFailed) as exc:
        core.bridge_reorder_node(alpha, "Alpha", "UI/Screens/Foo", index=-1)
    assert ">= 0" in str(exc.value)


def test_reorder_non_integer_index_raises(alpha, monkeypatch):
    """A non-integer (e.g. float) index is rejected locally."""
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    with pytest.raises(core.BridgeWriteFailed) as exc:
        core.bridge_reorder_node(alpha, "Alpha", "UI/Screens/Foo", index=1.5)  # type: ignore[arg-type]
    assert "non-negative integer" in str(exc.value)


def test_reorder_bool_index_raises(alpha, monkeypatch):
    """bool is a subclass of int but must be rejected as a non-integer index."""
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    with pytest.raises(core.BridgeWriteFailed):
        core.bridge_reorder_node(alpha, "Alpha", "UI/Screens/Foo", index=True)  # type: ignore[arg-type]


def test_reorder_success_passes_new_fields_through(alpha, monkeypatch):
    """Success response with graphic-children fields is returned unchanged."""
    cap: list = []
    payload = {
        "ok": True, "path": "UI/Screens/Foo",
        "from": 0, "requested": 2, "achieved": 2,
        "space": "graphic_children", "moves": 2,
        "mode": "inline", "thread": "http-bg",
    }
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/reorder": (200, payload)}, capture=cap))
    out = core.bridge_reorder_node(alpha, "Alpha", "UI/Screens/Foo", index=2)
    assert out["ok"] is True
    assert out["from"] == 0
    assert out["requested"] == 2
    assert out["achieved"] == 2
    assert out["space"] == "graphic_children"
    assert out["moves"] == 2
    _, path = next(c for c in cap if "/bridge/node/reorder" in c[1])
    assert "index=2" in path


def test_reorder_index_unreached_raises_bridge_write_failed(alpha, monkeypatch):
    """Bridge reorder_index_unreached response raises BridgeWriteFailed."""
    payload = {
        "ok": False, "error": "reorder_index_unreached",
        "requested": 3, "achieved": 2, "from": 0,
        "space": "graphic_children", "moves": 6,
    }
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/reorder": (200, payload)}))
    with pytest.raises(core.BridgeWriteFailed) as exc:
        core.bridge_reorder_node(alpha, "Alpha", "UI/Screens/Foo", index=3)
    assert "reorder_index_unreached" in str(exc.value)


def test_reorder_noop_response_passes_through(alpha, monkeypatch):
    """Noop short-circuit response carries new fields."""
    payload = {
        "ok": True, "path": "UI/Screens/Foo",
        "from": 1, "requested": 1, "achieved": 1,
        "space": "graphic_children", "moves": 0, "noop": True,
    }
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/reorder": (200, payload)}))
    out = core.bridge_reorder_node(alpha, "Alpha", "UI/Screens/Foo", index=1)
    assert out["noop"] is True and out["moves"] == 0 and out["space"] == "graphic_children"


def test_reorder_position_front_posts_correctly(alpha, monkeypatch):
    """position=front is forwarded to the bridge without an index param."""
    cap: list = []
    payload = {
        "ok": True, "path": "UI/Screens/Foo",
        "from": 0, "requested": 4, "achieved": 4,
        "space": "graphic_children", "moves": 4, "mode": "inline", "thread": "http-bg",
    }
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/reorder": (200, payload)}, capture=cap))
    out = core.bridge_reorder_node(alpha, "Alpha", "UI/Screens/Foo", position="front")
    assert out["ok"] is True
    _, path = next(c for c in cap if "/bridge/node/reorder" in c[1])
    assert "position=front" in path
    assert "index=" not in path


def test_reorder_string_index_raises_before_dispatch(alpha, monkeypatch):
    """A string index ('two') is rejected locally before _bridge_write is called.

    Regression guard for the off-by-one: a string index reaching the C# bridge
    would be silently ignored (int.TryParse fails, target=0) and the node would
    move to the BACK rather than raising an error. The Python guard must catch it
    before dispatch."""
    called: list = []
    monkeypatch.setattr(core, "_bridge_write",
                        lambda *a, **kw: called.append(a) or {})
    with pytest.raises(core.BridgeWriteFailed) as exc:
        core.bridge_reorder_node(alpha, "Alpha", "UI/Screens/Foo",
                                 index="two")  # type: ignore[arg-type]
    assert "non-negative integer" in str(exc.value)
    assert called == [], "_bridge_write must not be called for a string index"


def test_reorder_position_and_index_together_never_calls_bridge_write(alpha, monkeypatch):
    """position+index guard fires before _bridge_write — not just before _bridge_http.

    Both guards fire in bridge_reorder_node itself so even a caller that bypasses
    _bridge_http (e.g. a test double) can't inadvertently land the write."""
    called: list = []
    monkeypatch.setattr(core, "_bridge_write",
                        lambda *a, **kw: called.append(a) or {})
    with pytest.raises(core.BridgeWriteFailed) as exc:
        core.bridge_reorder_node(alpha, "Alpha", "UI/Screens/Foo",
                                 position="back", index=0)
    assert "position OR index" in str(exc.value)
    assert called == [], "_bridge_write must not be called when both args are supplied"


def test_reorder_25_to_16_result_returned_intact(alpha, monkeypatch):
    """Fake ok:true with from=25, requested=16, achieved=16, moves=9 is returned
    intact — pins the exact regression values from the panelbg incident where
    25->16 landed at 15 due to the pre-fix raw-children counting bug."""
    payload = {
        "ok": True, "path": "UI/Screens/PanelBg/BgRect",
        "from": 25, "requested": 16, "achieved": 16,
        "space": "graphic_children", "moves": 9,
        "mode": "inline", "thread": "http-bg",
    }
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/reorder": (200, payload)}, capture=cap))
    out = core.bridge_reorder_node(alpha, "Alpha",
                                   "UI/Screens/PanelBg/BgRect", index=16)
    assert out["ok"] is True
    assert out["from"] == 25
    assert out["requested"] == 16
    assert out["achieved"] == 16
    assert out["space"] == "graphic_children"
    assert out["moves"] == 9
    # The regression: achieved must equal requested (not 15, as it did before
    # the GraphicChildren/GraphicIndexOf fix in the C# bridge).
    assert out["achieved"] == out["requested"], (
        "achieved must equal requested — the 25->16 landing-at-15 regression "
        "is not present when the C# bridge uses GraphicIndexOf correctly"
    )
    _, path = next(c for c in cap if "/bridge/node/reorder" in c[1])
    assert "index=16" in path
    assert "position=" not in path


def test_reorder_not_a_graphic_child_raises(alpha, monkeypatch):
    """Bridge not_a_graphic_child error (dict-form via ErrorJson) raises
    BridgeWriteFailed with the code visible in the message.

    The not_a_graphic_child guard fires when an IUAVariable (property node)
    is passed as the reorder target — it exists in the parent's Children list
    but is excluded from the graphic-children index space.  The Python caller
    must surface the code so the model knows to use describe_node instead of
    assuming every child is reorderable."""
    payload = {
        "error": {
            "code": "not_a_graphic_child",
            "message": (
                "node is not a graphic child of its parent "
                "(it may be an IUAVariable/property): UI/Screens/Panel1/Width"
            ),
        }
    }
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/reorder": (200, payload)}))
    with pytest.raises(core.BridgeWriteFailed) as exc:
        core.bridge_reorder_node(alpha, "Alpha",
                                 "UI/Screens/Panel1/Width", index=0)
    assert "not_a_graphic_child" in str(exc.value)


def test_reorder_index_unreached_carries_code_from_bridge_write_result(alpha, monkeypatch):
    """reorder_index_unreached (string error) propagates through _bridge_write_result.

    The bridge returns ok:false with error as a STRING (not a dict) and separate
    requested/achieved/from fields.  _bridge_write_result reads the string error
    directly and raises BridgeWriteFailed with the code as the message.  Both
    from and requested are present in the fake response to mirror the real C#
    shape; only the code reaches the exception text (the indices are separate
    top-level fields that _bridge_write_result does not extract)."""
    payload = {
        "ok": False, "error": "reorder_index_unreached",
        "requested": 16, "achieved": 15,
        "from": 25, "space": "graphic_children", "moves": 9,
    }
    monkeypatch.setattr(core, "_bridge_http",
                        _fake_bridge({"/bridge/node/reorder": (200, payload)}))
    with pytest.raises(core.BridgeWriteFailed) as exc:
        core.bridge_reorder_node(alpha, "Alpha",
                                 "UI/Screens/PanelBg/BgRect", index=16)
    assert "reorder_index_unreached" in str(exc.value), (
        "the error code must survive _bridge_write_result's string-error path "
        "and appear in the raised BridgeWriteFailed"
    )


# --- Static inspection: the C# bridge carries the graphic-children symbols ----
# Offline gate confirming the C# reorder fix is present.  A live build is
# verified by Tier-2 CI; this test catches a Python-only cherry-pick that
# omits the .cs changes.

def test_bridge_cs_contains_graphic_children_symbols():
    """studio-bridge/StudioMCPBridge.cs must contain the four symbols that
    implement the graphic-children index space for ReorderInline.  If this test
    fails the C# fix is missing and the 25->16 off-by-one regression is live."""
    from pathlib import Path
    cs = Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs"
    assert cs.is_file(), f"C# bridge file not found at {cs}"
    text = cs.read_text(encoding="utf-8", errors="ignore")
    for symbol in ("GraphicChildren", "GraphicIndexOf",
                   "reorder_index_unreached", "not_a_graphic_child"):
        assert symbol in text, (
            f"symbol {symbol!r} missing from StudioMCPBridge.cs — "
            "the graphic-children reorder fix may not have been committed"
        )


# --- Static inspection: the C# bridge carries the ui-namespace-child symbols --
# Offline gate confirming the GridLayoutProperties namespace fix is present in
# the C# half.  A live build is verified by Tier-2 CI; this test catches a
# Python-only cherry-pick that omits the .cs changes.

def test_bridge_cs_contains_ui_namespace_child_symbols():
    """studio-bridge/StudioMCPBridge.cs must contain the two error codes that
    implement the GridLayoutProperties namespace fix: wrong_namespace (node
    created but ended up in the project namespace instead of FTOptix.UI) and
    ui_namespace_child_unsupported (all creation paths failed, caller must
    clean up).  If this test fails the C# half is missing and the silent
    RowStart/ColumnStart-ignored-at-render bug is still live."""
    from pathlib import Path
    cs = Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs"
    assert cs.is_file(), f"C# bridge file not found at {cs}"
    text = cs.read_text(encoding="utf-8", errors="ignore")
    for symbol in ("wrong_namespace", "ui_namespace_child_unsupported"):
        assert symbol in text, (
            f"symbol {symbol!r} missing from StudioMCPBridge.cs — "
            "the GridLayoutProperties UI-namespace-child fix may not have been committed"
        )


# --- VirtualKeyboard* catalog misalignment and ResolveUiTypeId fix ------------
# Offline tests for the browse-name walk (ResolveUiTypeId step (b)) that
# handles types whose ObjectTypes field constant differs from their live
# BrowseName (the VirtualKeyboard* family).  Python tests cover the
# list_ui_types lean-out, describe_type catalog_misaligned flag, resolved_by
# passthrough, and schema-dump round-trip; a live build is verified by Tier-2
# CI.  The companion static-inspection test (below) catches a Python-only
# cherry-pick that omits the .cs changes.


def test_list_ui_types_virtual_keyboard_misaligned_payload(alpha, monkeypatch):
    """Fake /bridge/types/ui payload with 3 aligned + 2 misaligned entries.

    The two misaligned entries use contrived browse-names (ContentX, Locales)
    that mirror the VirtualKeyboard* catalog pattern where the ObjectTypes field
    constant differs from the live BrowseName.

    Asserts:
    * misaligned carries exactly those two entries in their payload order.
    * The three aligned entries carry no browse_name key after lean-out.
    * count is unchanged (lean-out is token-only, no entries dropped).
    * The _hint nudge is present and references browse_name + describe_type.
    """
    types = [
        {"name": "Button", "browse_name": "Button"},
        {"name": "VirtualKeyboardTextButton", "browse_name": "ContentX",
         "resolved_by": "browse_name"},
        {"name": "Label", "browse_name": "Label"},
        {"name": "VirtualKeyboardSymbolButton", "browse_name": "Locales",
         "resolved_by": "browse_name"},
        {"name": "Rectangle", "browse_name": "Rectangle"},
    ]
    routes = {"/bridge/types/ui": (200, {"types": types, "count": 5})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.list_ui_types(alpha, "Alpha")

    # misaligned summary lists the two mismatched entries only.
    assert "misaligned" in out
    assert out["misaligned"] == [
        {"name": "VirtualKeyboardTextButton", "browse_name": "ContentX"},
        {"name": "VirtualKeyboardSymbolButton", "browse_name": "Locales"},
    ], f"unexpected misaligned: {out['misaligned']!r}"

    # The three aligned entries must carry no browse_name after lean-out.
    by_name = {t["name"]: t for t in out["types"]}
    for aligned in ("Button", "Label", "Rectangle"):
        assert "browse_name" not in by_name[aligned], (
            f"aligned type {aligned!r} should not carry browse_name after lean-out; "
            f"got: {by_name[aligned]!r}"
        )

    # count is unchanged — lean-out is token-only, no entries dropped.
    assert out["count"] == 5, (
        f"count must equal the original payload count (5), got {out['count']!r}"
    )

    # nudge is present and useful.
    assert "_hint" in out
    hint = out["_hint"]
    assert "describe_type" in hint, (
        f"_hint must reference describe_type so callers know how to use the "
        f"browse_name; got: {hint!r}"
    )


def test_list_ui_types_all_aligned_no_misaligned_key(alpha, monkeypatch):
    """When all types are aligned (browse_name == name), misaligned is absent
    and no _hint nudge is emitted — the common case for the full ~102-type
    catalog once the ResolveUiTypeId fix is in place."""
    types = [
        {"name": "Button", "browse_name": "Button"},
        {"name": "Label", "browse_name": "Label"},
        {"name": "Rectangle", "browse_name": "Rectangle"},
    ]
    routes = {"/bridge/types/ui": (200, {"types": types, "count": 3})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.list_ui_types(alpha, "Alpha")

    assert "misaligned" not in out, (
        f"fully-aligned catalog must carry no misaligned key; got {out.get('misaligned')!r}"
    )
    assert "_hint" not in out, (
        "fully-aligned catalog must not emit a _hint nudge"
    )


def test_describe_type_virtual_keyboard_text_button_sets_catalog_misaligned(
    alpha, monkeypatch
):
    """describe_type('VirtualKeyboardTextButton') with response browse_name='ContentX'
    (differs from the requested name) sets catalog_misaligned:True.

    This is the concrete VirtualKeyboard* scenario: the caller used the
    field-name alias (VirtualKeyboardTextButton) rather than the canonical
    browse-name identifier (ContentX), so catalog_misaligned flags the
    mismatch.  resolved_by:'browse_name' is also passed through from the bridge.
    """
    schema = {
        "type": "VirtualKeyboardTextButton",
        "browse_name": "ContentX",
        "resolved_by": "browse_name",
        "properties": [{"name": "Text", "datatype": "String", "settable": True}],
        "truncated": False,
    }
    routes = {"/bridge/types/schema": (200, schema)}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.describe_type(alpha, "Alpha", "VirtualKeyboardTextButton")

    assert out["catalog_misaligned"] is True, (
        "browse_name='ContentX' differs from requested 'VirtualKeyboardTextButton' — "
        "catalog_misaligned must be True"
    )
    assert out["resolved_by"] == "browse_name"


def test_describe_type_matching_browse_name_no_catalog_misaligned(
    alpha, monkeypatch
):
    """When the requested name matches the response browse_name (canonical
    form), catalog_misaligned is NOT set — the caller used the correct
    identifier and there is no mismatch to report.  resolved_by still passes
    through when present in the bridge response."""
    schema = {
        "type": "ContentX",
        "browse_name": "ContentX",
        "resolved_by": "browse_name",
        "properties": [{"name": "Text", "datatype": "String", "settable": True}],
        "truncated": False,
    }
    routes = {"/bridge/types/schema": (200, schema)}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.describe_type(alpha, "Alpha", "ContentX")

    assert "catalog_misaligned" not in out, (
        "browse_name matches requested name — catalog_misaligned must NOT be set"
    )
    assert out["resolved_by"] == "browse_name"


def test_resolved_by_browse_name_survives_list_ui_types_lean_out_and_describe_type(
    alpha, monkeypatch
):
    """resolved_by:'browse_name' survives both the list_ui_types lean-out pass
    and the describe_type passthrough.

    list_ui_types lean-out: only the redundant browse_name is stripped when
    browse_name == name; resolved_by is left untouched in both aligned and
    misaligned entries.

    describe_type: resolved_by from the bridge response is passed through into
    the returned dict unchanged.
    """
    # list_ui_types: resolved_by must survive for both aligned and misaligned.
    types = [
        # misaligned — browse_name kept, resolved_by kept.
        {"name": "VirtualKeyboardTextButton", "browse_name": "ContentX",
         "resolved_by": "browse_name"},
        # aligned — browse_name dropped, resolved_by survives.
        {"name": "Label", "browse_name": "Label", "resolved_by": "browse_name"},
    ]
    routes = {"/bridge/types/ui": (200, {"types": types, "count": 2})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.list_ui_types(alpha, "Alpha")

    by_name = {t["name"]: t for t in out["types"]}
    # Misaligned: browse_name AND resolved_by both survive lean-out.
    assert by_name["VirtualKeyboardTextButton"]["browse_name"] == "ContentX"
    assert by_name["VirtualKeyboardTextButton"]["resolved_by"] == "browse_name", (
        "resolved_by must survive lean-out for misaligned entry"
    )
    # Aligned: browse_name is dropped but resolved_by is NOT stripped.
    assert "browse_name" not in by_name["Label"], (
        "aligned entry should not carry browse_name after lean-out"
    )
    assert by_name["Label"]["resolved_by"] == "browse_name", (
        "resolved_by must survive lean-out even for an aligned entry"
    )

    # describe_type: resolved_by passes through unchanged.
    schema = {
        "type": "ContentX",
        "browse_name": "ContentX",
        "resolved_by": "browse_name",
        "properties": [],
        "truncated": False,
    }
    routes2 = {"/bridge/types/schema": (200, schema)}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes2))
    out2 = core.describe_type(alpha, "Alpha", "ContentX")
    assert out2["resolved_by"] == "browse_name", (
        "describe_type must pass resolved_by through from the bridge response"
    )
    assert "catalog_misaligned" not in out2


def test_schema_dump_with_resolved_by_round_trips_no_key_error_no_phantom_diff(
    cfg,
):
    """A schema dump carrying resolved_by per type round-trips through the
    caching/diff path with no KeyError and no phantom property change in a diff.

    schema_diff only compares name/datatype/settable per property; extra fields
    (resolved_by at the type or property level) must be silently ignored — they
    must not trigger KeyError during cache write/read or appear as spurious
    property changes in a cross-version diff.
    """
    from service import optix_schema

    # Synthetic dump with resolved_by at both the type level and per-property.
    dump_v1 = {
        "studio_version": "1.7.1.46",
        "generated_at": "2026-09-05T00:00:00Z",
        "types": {
            "VirtualKeyboardTextButton": {
                "browse_name": "ContentX",
                "resolved_by": "browse_name",
                "properties": [
                    {"name": "Text", "datatype": "String", "settable": True,
                     "resolved_by": "browse_name"},
                    {"name": "Width", "datatype": "Int32", "settable": True},
                ],
            },
            "Label": {
                "browse_name": "Label",
                "properties": [
                    {"name": "Text", "datatype": "String", "settable": True},
                ],
            },
        },
    }
    # v2 identical to v1 — diff must be empty (no phantom changes from resolved_by).
    dump_v2 = {
        "studio_version": "1.7.2.0",
        "generated_at": "2026-09-05T01:00:00Z",
        "types": {
            "VirtualKeyboardTextButton": {
                "browse_name": "ContentX",
                "resolved_by": "browse_name",
                "properties": [
                    {"name": "Text", "datatype": "String", "settable": True,
                     "resolved_by": "browse_name"},
                    {"name": "Width", "datatype": "Int32", "settable": True},
                ],
            },
            "Label": {
                "browse_name": "Label",
                "properties": [
                    {"name": "Text", "datatype": "String", "settable": True},
                ],
            },
        },
    }

    # Cache both dumps (no KeyError on write).
    path1 = optix_schema.cache_dump(cfg, dump_v1)
    path2 = optix_schema.cache_dump(cfg, dump_v2)
    assert path1.is_file(), f"cache_dump did not write v1 at {path1}"
    assert path2.is_file(), f"cache_dump did not write v2 at {path2}"

    # Round-trip via load_dump (no KeyError on read).
    loaded1 = optix_schema.load_dump(cfg, "1.7.1.46")
    loaded2 = optix_schema.load_dump(cfg, "1.7.2.0")
    assert loaded1 is not None, "load_dump returned None for v1"
    assert loaded2 is not None, "load_dump returned None for v2"

    # Diff must be empty — no phantom changes from the extra resolved_by fields.
    diff = optix_schema.schema_diff(loaded1, loaded2)
    assert diff["added_types"] == [], (
        f"phantom added_types from resolved_by fields: {diff['added_types']!r}"
    )
    assert diff["removed_types"] == [], (
        f"phantom removed_types from resolved_by fields: {diff['removed_types']!r}"
    )
    assert diff["changed_types"] == {}, (
        f"phantom property changes from resolved_by fields: {diff['changed_types']!r}"
    )


# --- Static inspection: the C# bridge carries the ResolveUiTypeId symbols -----
# Offline gate confirming the VirtualKeyboard* catalog fix is present in the
# C# half.  A live build is verified by Tier-2 CI; this test catches a
# Python-only cherry-pick that omits the .cs changes.

def test_bridge_cs_contains_resolve_ui_type_id_and_resolved_by():
    """studio-bridge/StudioMCPBridge.cs must contain:
    * ResolveUiTypeId — the browse-name-walk resolver that handles the
      VirtualKeyboard* catalog (field name != BrowseName).
    * resolved_by — the JSON field that surfaces the resolver path to callers.
    * WriteWidgetInline must call ResolveUiTypeId rather than a bare GetField
      so widget creation benefits from the browse-name walk for VirtualKeyboard*
      types; a bare GetField returns null for those types and causes type_not_found.

    If this test fails the C# fix is missing and VirtualKeyboard* create_widget
    calls will return type_not_found rather than succeeding."""
    from pathlib import Path
    cs = Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs"
    assert cs.is_file(), f"C# bridge file not found at {cs}"
    text = cs.read_text(encoding="utf-8", errors="ignore")

    for symbol in ("ResolveUiTypeId", "resolved_by"):
        assert symbol in text, (
            f"symbol {symbol!r} missing from StudioMCPBridge.cs — "
            "the VirtualKeyboard* ResolveUiTypeId fix may not have been committed"
        )

    # WriteWidgetInline must delegate to ResolveUiTypeId, not use a bare
    # GetField on the type name.  Locate the METHOD DEFINITION (not a call site)
    # and verify ResolveUiTypeId is called within its body.
    method_def = "private string WriteWidgetInline("
    write_widget_start = text.find(method_def)
    assert write_widget_start != -1, (
        f"{method_def!r} method definition not found in StudioMCPBridge.cs"
    )
    method_window = text[write_widget_start:write_widget_start + 2000]
    assert "ResolveUiTypeId(" in method_window, (
        "WriteWidgetInline must call ResolveUiTypeId (not a bare GetField) so "
        "VirtualKeyboard* widget creation uses the browse-name walk; "
        "the method body window did not contain the call"
    )


# ---- A.3: batch_id in all terminal states -----------------------------------

def test_bridge_edit_batch_id_present_on_success(alpha, monkeypatch):
    """batch_id must appear in the returned dict when the batch succeeds."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: {"ok": True})

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert "batch_id" in out
    assert isinstance(out["batch_id"], str) and len(out["batch_id"]) == 12


def test_bridge_edit_batch_id_present_on_dry_run(alpha, monkeypatch):
    """batch_id must appear even when dry_run=True (nothing applied)."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS, dry_run=True)

    assert out["state"] == "validated"
    assert "batch_id" in out
    assert len(out["batch_id"]) == 12


def test_bridge_edit_batch_id_present_on_validation_failure(alpha, monkeypatch):
    """batch_id must appear even when the batch is refused by the validator."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_BAD_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "validated"
    assert "batch_id" in out
    assert len(out["batch_id"]) == 12


def test_bridge_edit_batch_ids_are_unique(alpha, monkeypatch):
    """Each call to bridge_edit must produce a distinct batch_id."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: {"ok": True})

    ids = {core.bridge_edit(alpha, "Alpha", _TWO_OPS)["batch_id"]
           for _ in range(5)}

    assert len(ids) == 5, "batch_ids must be unique across calls"


# ---- A.1: per-op timings and validate_ms ------------------------------------

def test_bridge_edit_validate_ms_present_on_success(alpha, monkeypatch):
    """validate_ms must be a non-negative int in the result for a successful batch."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: {"ok": True})

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert "validate_ms" in out
    assert isinstance(out["validate_ms"], int) and out["validate_ms"] >= 0


def test_bridge_edit_validate_ms_present_on_dry_run(alpha, monkeypatch):
    """validate_ms must appear even when dry_run=True."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS, dry_run=True)

    assert "validate_ms" in out and out["validate_ms"] >= 0


def test_bridge_edit_op_timings_on_success(alpha, monkeypatch):
    """op_timings must be a list of {index, op, ms, ok} for each applied op."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: {"ok": True})

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "succeeded"
    assert "op_timings" in out
    timings = out["op_timings"]
    assert len(timings) == 2
    for i, entry in enumerate(timings):
        assert entry["index"] == i
        assert entry["op"] == _TWO_OPS[i]["op"]
        assert isinstance(entry["ms"], int) and entry["ms"] >= 0
        assert entry["ok"] is True
    assert "apply_ms" in out and out["apply_ms"] >= 0


def test_bridge_edit_op_timings_omitted_on_dry_run(alpha, monkeypatch):
    """op_timings must NOT appear when dry_run=True (nothing applied)."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS, dry_run=True)

    assert "op_timings" not in out, "op_timings must be absent for dry_run"
    assert "apply_ms" not in out


def test_bridge_edit_op_timings_omitted_on_validation_failure(alpha, monkeypatch):
    """op_timings must NOT appear when validation refuses the batch."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_BAD_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert "op_timings" not in out, "op_timings must be absent when validation fails"
    assert "apply_ms" not in out


def test_bridge_edit_op_timings_failed_op_has_ok_false(alpha, monkeypatch):
    """The timing entry for a failing op must carry ok=False."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    def flaky(cfg, project, op):
        if op["op"] == "set_property":
            raise core.BridgeWriteFailed("boom")
        return {"ok": True}

    monkeypatch.setattr(core, "_apply_one_edit", flaky)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "partial"
    assert "op_timings" in out
    timings = out["op_timings"]
    # First op (create_widget) succeeded, second (set_property) failed.
    assert timings[0]["ok"] is True and timings[0]["op"] == "create_widget"
    assert timings[1]["ok"] is False and timings[1]["op"] == "set_property"
    assert timings[1]["ms"] >= 0
    assert "apply_ms" in out


# ---- A.2: on_progress callback ----------------------------------------------

def test_bridge_edit_on_progress_called_validated_then_per_op(alpha, monkeypatch):
    """on_progress must be called once with phase='validated' before the loop,
    then once per applied op with {batch_id, index, total, op, ms, applied}."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: {"ok": True})

    events: list[dict] = []
    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS, on_progress=events.append)

    assert out["state"] == "succeeded"
    # Three events: one pre-loop + one per op (2 ops).
    assert len(events) == 3

    # Pre-loop event.
    pre = events[0]
    assert pre["phase"] == "validated"
    assert pre["index"] == 0 and pre["total"] == 2 and pre["op"] is None
    assert pre["batch_id"] == out["batch_id"]

    # Post-op events.
    for i, ev in enumerate(events[1:], start=1):
        assert ev["batch_id"] == out["batch_id"]
        assert ev["index"] == i and ev["total"] == 2
        assert ev["op"] == _TWO_OPS[i - 1]["op"]
        assert isinstance(ev["ms"], int) and ev["ms"] >= 0
        assert ev["applied"] == i


def test_bridge_edit_on_progress_not_called_on_dry_run(alpha, monkeypatch):
    """on_progress must NOT be called when dry_run=True (no apply loop)."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    events: list[dict] = []
    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS, dry_run=True,
                           on_progress=events.append)

    assert out["state"] == "validated"
    assert events == [], "on_progress must not be called for dry_run"


def test_bridge_edit_on_progress_not_called_when_validation_fails(alpha, monkeypatch):
    """on_progress must NOT be called when the validator refuses the batch."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_BAD_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    events: list[dict] = []
    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS, on_progress=events.append)

    assert out["state"] == "validated"
    assert events == []


def test_bridge_edit_on_progress_callback_exception_does_not_abort(alpha, monkeypatch):
    """A raising on_progress callback must not prevent the batch from applying;
    the ops must still complete and out['state'] == 'succeeded'."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    applied: list = []
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))

    def bad_callback(event: dict) -> None:
        raise RuntimeError("callback broke")

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS, on_progress=bad_callback)

    assert out["state"] == "succeeded"
    assert applied == ["create_widget", "set_property"]


def test_bridge_edit_on_progress_callback_exception_logs_at_most_one_warning(
    alpha, monkeypatch, caplog,
):
    """A raising on_progress callback must log at most one WARNING per batch,
    regardless of how many callback invocations fail."""
    import logging as _logging
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: {"ok": True})

    def bad_callback(event: dict) -> None:
        raise RuntimeError("always fails")

    with caplog.at_level(_logging.WARNING, logger="service.core"):
        core.bridge_edit(alpha, "Alpha", _TWO_OPS, on_progress=bad_callback)

    warns = [r for r in caplog.records
             if r.levelno == _logging.WARNING and "on_progress" in r.getMessage()]
    assert len(warns) == 1, (
        f"Expected exactly 1 on_progress WARNING, got {len(warns)}: "
        f"{[r.getMessage() for r in warns]}"
    )


# --- B.1: batch journal tests ------------------------------------------------

def _patch_apply_ok(monkeypatch):
    """Patch _apply_one_edit to succeed without hitting the real bridge."""
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: {"ok": True})


def test_batch_journal_created_for_succeeded_batch(alpha, monkeypatch):
    """B.1: A succeeded batch must leave a journal file in
    <state_dir>/batches/<batch_id>.json with state='succeeded'."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "succeeded"
    batches_dir = alpha.state_dir / "batches"
    journal_path = batches_dir / f"{out['batch_id']}.json"
    assert journal_path.exists(), "journal file must be created"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "succeeded"
    assert journal["batch_id"] == out["batch_id"]
    assert journal["project"] == "Alpha"
    assert journal["op_count"] == 2
    assert journal["applied"] == 2
    assert journal["remaining_ops"] == []
    assert "started_at" in journal
    assert "updated_at" in journal
    assert "pid" in journal
    assert journal["chunk_index"] == 0


def test_batch_journal_created_for_partial_batch(alpha, monkeypatch):
    """B.1: A partial (mid-batch failure) batch must leave a journal with
    state='partial' and failed_op populated."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    def flaky(cfg, project, op):
        if op["op"] == "set_property":
            raise core.BridgeWriteFailed("boom")
        return {"ok": True}

    monkeypatch.setattr(core, "_apply_one_edit", flaky)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert out["state"] == "partial"
    batches_dir = alpha.state_dir / "batches"
    journal_path = batches_dir / f"{out['batch_id']}.json"
    assert journal_path.exists()
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "partial"
    assert "failed_op" in journal
    assert journal["failed_op"]["op"] == "set_property"
    # remaining_ops includes the failed op and anything after it
    assert len(journal["remaining_ops"]) >= 1


def test_batch_journal_write_failure_does_not_abort_batch(alpha, monkeypatch, tmp_path):
    """B.1: A journal write failure must never abort a correctly-applying batch."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)

    # Make the batches dir unwritable by patching _write_batch_journal to raise.
    write_calls: list = []

    def _bad_journal(cfg, data):
        write_calls.append(data)
        raise OSError("disk full")

    monkeypatch.setattr(core, "_write_batch_journal", _bad_journal)

    # Despite the journal failure, the batch must still succeed.
    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)
    assert out["state"] == "succeeded"
    assert out["applied"] == 2
    # The helper was called (attempted), even though it raised.
    assert len(write_calls) > 0


def test_batch_journal_prunes_old_files(alpha, monkeypatch):
    """B.1: Journals older than OPTIX_BATCH_JOURNAL_DAYS must be pruned on write."""
    import os as _os
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)
    monkeypatch.setenv("OPTIX_BATCH_JOURNAL_DAYS", "7")

    # Pre-create a stale journal (mtime = 8 days ago).
    batches_dir = alpha.state_dir / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)
    stale_path = batches_dir / "staledeadbeef.json"
    stale_path.write_text("{}", encoding="utf-8")
    old_mtime = _os.path.getmtime(str(stale_path)) - (8 * 86400)
    _os.utime(str(stale_path), (old_mtime, old_mtime))

    core.bridge_edit(alpha, "Alpha", _TWO_OPS)

    assert not stale_path.exists(), "stale journal must have been pruned"


def test_batch_journal_accepts_caller_supplied_batch_id(alpha, monkeypatch):
    """B.1: When bridge_edit is called with batch_id=..., the journal must use
    the supplied ID (not generate a new one)."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)

    supplied_id = "abc123def456"
    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS, batch_id=supplied_id)

    assert out["batch_id"] == supplied_id
    journal_path = alpha.state_dir / "batches" / f"{supplied_id}.json"
    assert journal_path.exists()
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["batch_id"] == supplied_id


# --- B.2: chunking tests -------------------------------------------------------

def test_bridge_edit_chunks_by_time(alpha, monkeypatch):
    """B.2: When OPTIX_BATCH_CHUNK_SECONDS is exceeded, the loop must stop and
    return state='chunked' with chunk_reason='time'."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "0")  # expire immediately
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "100")    # ops limit far away
    _patch_apply_ok(monkeypatch)

    # Use three ops so there is always a "remaining" op after the first one.
    three_ops = _TWO_OPS + [
        {"op": "set_property", "path": "UI/MainWindow/B1", "name": "Height",
         "value": "20"},
    ]
    out = core.bridge_edit(alpha, "Alpha", three_ops)

    assert out["state"] == "chunked", out
    assert out["chunk_reason"] == "time"
    assert out["applied"] >= 1
    assert out["remaining_ops"] >= 1
    assert "batch_id" in out
    assert "optix_bridge_edit" in out["nudge"]
    assert "action='continue'" in out["nudge"]
    assert out["batch_id"] in out["nudge"]
    assert "chunk_index" in out


def test_bridge_edit_chunks_by_ops_count(alpha, monkeypatch):
    """B.2: When OPTIX_BATCH_CHUNK_OPS is reached, the loop must stop and
    return state='chunked' with chunk_reason='ops'."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")  # time limit far away
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "1")          # after 1 op, chunk
    _patch_apply_ok(monkeypatch)

    three_ops = _TWO_OPS + [
        {"op": "set_property", "path": "UI/MainWindow/B1", "name": "Height",
         "value": "20"},
    ]
    out = core.bridge_edit(alpha, "Alpha", three_ops)

    assert out["state"] == "chunked", out
    assert out["chunk_reason"] == "ops"
    assert out["applied"] == 1
    assert out["remaining_ops"] == 2
    assert "batch_id" in out


def test_bridge_edit_no_chunk_when_last_op(alpha, monkeypatch):
    """B.2: Chunking must NOT trigger on the last op even when the threshold is
    already reached — a batch that finishes in one chunk must return 'succeeded'."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "0")   # expire immediately
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "0")       # also tripped
    _patch_apply_ok(monkeypatch)

    # Single-op batch: no remaining ops → must not chunk.
    single_op = [{"op": "create_widget", "screen": "UI/MainWindow",
                  "name": "B1", "widget_type": "Rectangle"}]
    single_report = {"ok": True, "op_count": 1, "strict": False,
                     "errors": [], "warnings": []}
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(single_report))

    out = core.bridge_edit(alpha, "Alpha", single_op)
    assert out["state"] == "succeeded", out


def test_bridge_edit_chunk_journal_has_remaining_ops(alpha, monkeypatch):
    """B.2+B.1: When chunking fires, the journal must record state='chunked'
    and contain the remaining (not-yet-applied) ops verbatim."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "1")
    _patch_apply_ok(monkeypatch)

    three_ops = _TWO_OPS + [
        {"op": "set_property", "path": "UI/MainWindow/B1", "name": "Height",
         "value": "20"},
    ]
    out = core.bridge_edit(alpha, "Alpha", three_ops)

    assert out["state"] == "chunked"
    journal_path = alpha.state_dir / "batches" / f"{out['batch_id']}.json"
    assert journal_path.exists()
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "chunked"
    assert len(journal["remaining_ops"]) == 2  # ops[1] and ops[2] remain
    # The remaining ops are stored verbatim.
    assert journal["remaining_ops"][0]["op"] == "set_property"


def test_bridge_edit_time_boundary_takes_priority_over_ops(alpha, monkeypatch):
    """B.2: When both time and ops thresholds are exceeded simultaneously,
    chunk_reason must be 'time' (time is the primary boundary)."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "0")  # both fire
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "1")
    _patch_apply_ok(monkeypatch)

    three_ops = _TWO_OPS + [
        {"op": "set_property", "path": "UI/MainWindow/B1", "name": "Height",
         "value": "20"},
    ]
    out = core.bridge_edit(alpha, "Alpha", three_ops)

    assert out["state"] == "chunked"
    assert out["chunk_reason"] == "time"


def test_bridge_edit_chunk_index_propagated(alpha, monkeypatch):
    """B.1+B.2: chunk_index passed to bridge_edit must appear in the journal."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "1")
    _patch_apply_ok(monkeypatch)

    three_ops = _TWO_OPS + [
        {"op": "set_property", "path": "UI/MainWindow/B1", "name": "Height",
         "value": "20"},
    ]
    out = core.bridge_edit(alpha, "Alpha", three_ops, chunk_index=2)

    assert out["chunk_index"] == 2
    journal_path = alpha.state_dir / "batches" / f"{out['batch_id']}.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["chunk_index"] == 2


# --- B.3: bridge_edit_continue tests ------------------------------------------

def _setup_chunked_journal(alpha, monkeypatch, *, chunk_ops=1):
    """Create a chunked batch via bridge_edit and return (out, three_ops)."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", str(chunk_ops))
    _patch_apply_ok(monkeypatch)
    three_ops = _TWO_OPS + [
        {"op": "set_property", "path": "UI/MainWindow/B1", "name": "Height",
         "value": "20"},
    ]
    out = core.bridge_edit(alpha, "Alpha", three_ops)
    assert out["state"] == "chunked"
    return out, three_ops


def test_bridge_edit_continue_resumes_chunked_batch(alpha, monkeypatch):
    """B.3: bridge_edit_continue must apply remaining ops and return succeeded."""
    first, _ = _setup_chunked_journal(alpha, monkeypatch)
    batch_id = first["batch_id"]

    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "99999")

    out = core.bridge_edit_continue(alpha, "Alpha", batch_id)

    assert out["state"] == "succeeded", out
    assert out["batch_id"] == batch_id
    assert "op_timings" in out
    assert "report" in out


def test_bridge_edit_continue_echoes_original_report(alpha, monkeypatch):
    """B.3: continue must echo the original validation report, not re-validate."""
    seen: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT, seen=seen))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "1")
    _patch_apply_ok(monkeypatch)
    three_ops = _TWO_OPS + [
        {"op": "set_property", "path": "UI/MainWindow/B1", "name": "Height",
         "value": "20"},
    ]
    out = core.bridge_edit(alpha, "Alpha", three_ops)
    assert out["state"] == "chunked"
    initial_seen_count = len(seen)

    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "99999")
    core.bridge_edit_continue(alpha, "Alpha", out["batch_id"])

    # No additional validate round-trips after the initial enqueue.
    assert len(seen) == initial_seen_count, (
        f"bridge_edit_continue must not call _bridge_post_body "
        f"(called {len(seen) - initial_seen_count} extra times)"
    )


def test_bridge_edit_continue_increments_chunk_index(alpha, monkeypatch):
    """B.3: chunk_index in the journal must increment on each continue call."""
    first, _ = _setup_chunked_journal(alpha, monkeypatch)
    batch_id = first["batch_id"]

    journal_path = alpha.state_dir / "batches" / f"{batch_id}.json"
    j0 = json.loads(journal_path.read_text())
    assert j0["chunk_index"] == 0

    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "99999")
    out = core.bridge_edit_continue(alpha, "Alpha", batch_id)
    assert out["state"] == "succeeded"
    assert out["chunk_index"] == 1

    j1 = json.loads(journal_path.read_text())
    assert j1["chunk_index"] == 1


def test_bridge_edit_continue_refuses_succeeded_batch(alpha, monkeypatch):
    """B.3 rule 2: continue must refuse a succeeded batch with batch_not_resumable."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)
    assert out["state"] == "succeeded"
    batch_id = out["batch_id"]

    cont = core.bridge_edit_continue(alpha, "Alpha", batch_id)

    assert cont["state"] == "failed"
    assert cont["error"] == "batch_not_resumable"
    assert cont["batch_id"] == batch_id
    assert cont["batch_state"] == "succeeded"


def test_bridge_edit_continue_refuses_partial_batch_with_nudge(alpha, monkeypatch):
    """B.3 rule 2: continue must refuse a partial batch and nudge describe_node."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)

    call_count = [0]

    def _fail_second(cfg, project, op):
        call_count[0] += 1
        if call_count[0] > 1:
            raise RuntimeError("apply failure")

    monkeypatch.setattr(core, "_apply_one_edit", _fail_second)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)
    assert out["state"] == "partial"
    batch_id = out["batch_id"]

    cont = core.bridge_edit_continue(alpha, "Alpha", batch_id)

    assert cont["state"] == "failed"
    assert cont["error"] == "batch_not_resumable"
    assert cont["batch_state"] == "partial"
    assert "optix_describe_node" in cont.get("nudge", "")


def test_bridge_edit_continue_refuses_validated_batch(alpha, monkeypatch):
    """B.3 rule 2: continue must refuse a journal with state='validated'."""
    import json as _json

    # bridge_edit with dry_run does not write a journal, so synthesise one.
    batches_dir = alpha.state_dir / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)
    batch_id = "validatedabcd"
    journal_data = {
        "batch_id": batch_id,
        "project": "Alpha",
        "state": "validated",
        "op_count": 2,
        "applied": 0,
        "started_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "pid": 1,
        "chunk_index": 0,
        "op_timings": [],
        "report": _OK_REPORT,
        "remaining_ops": list(_TWO_OPS),
    }
    (batches_dir / f"{batch_id}.json").write_text(
        _json.dumps(journal_data), encoding="utf-8"
    )

    cont = core.bridge_edit_continue(alpha, "Alpha", batch_id)
    assert cont["state"] == "failed"
    assert cont["error"] == "batch_not_resumable"
    assert cont["batch_state"] == "validated"


def test_bridge_edit_continue_refuses_project_mismatch(alpha, monkeypatch):
    """B.3 rule 3: continue must refuse when the project doesn't match the journal."""
    first, _ = _setup_chunked_journal(alpha, monkeypatch)
    batch_id = first["batch_id"]

    cont = core.bridge_edit_continue(alpha, "OtherProject", batch_id)

    assert cont["state"] == "failed"
    assert cont["error"] == "batch_project_mismatch"
    assert cont["batch_id"] == batch_id


def test_bridge_edit_continue_returns_abandoned_for_dead_pid(alpha, monkeypatch, tmp_path):
    """B.3 rule 4: if the journal's pid is dead and state='applying', return abandoned."""
    import json as _json

    # Write a synthetic journal with state='applying' and a certainly-dead pid.
    batches_dir = alpha.state_dir / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)
    dead_pid = 99999999  # guaranteed non-existent
    batch_id = "deadpid123456"
    journal_data = {
        "batch_id": batch_id,
        "project": "Alpha",
        "state": "applying",
        "op_count": 2,
        "applied": 1,
        "started_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:01+00:00",
        "pid": dead_pid,
        "chunk_index": 0,
        "op_timings": [],
        "report": {"ok": True, "op_count": 2, "strict": False,
                   "errors": [], "warnings": []},
        "remaining_ops": [
            {"op": "set_property", "path": "UI/MainWindow/B1",
             "name": "Width", "value": "40"},
        ],
    }
    (batches_dir / f"{batch_id}.json").write_text(
        _json.dumps(journal_data), encoding="utf-8"
    )

    cont = core.bridge_edit_continue(alpha, "Alpha", batch_id)

    assert cont["state"] == "abandoned"
    assert cont["error"] == "batch_abandoned"
    assert cont["batch_id"] == batch_id
    assert "optix_describe_node" in cont.get("nudge", "")


def test_bridge_edit_continue_raises_on_missing_journal(alpha):
    """B.3: BridgeWriteFailed must be raised when the batch_id is unknown."""
    import pytest as _pytest
    with _pytest.raises(core.BridgeWriteFailed, match="no journal found"):
        core.bridge_edit_continue(alpha, "Alpha", "nonexistent000")


def test_bridge_edit_continue_chunked_again_when_slow(alpha, monkeypatch):
    """B.3: if the continue call itself hits the time limit, it returns chunked."""
    first, _ = _setup_chunked_journal(alpha, monkeypatch, chunk_ops=1)
    batch_id = first["batch_id"]

    # Remaining ops are 2; set chunk limit to 1 so the continue also chunks.
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "1")

    out = core.bridge_edit_continue(alpha, "Alpha", batch_id)

    assert out["state"] == "chunked", out
    assert out["chunk_index"] == 1
    assert out["remaining_ops"] >= 1
    assert "optix_bridge_edit" in out["nudge"]
    assert "action='continue'" in out["nudge"]


def test_bridge_edit_continue_applied_total_is_cumulative(alpha, monkeypatch):
    """B.3: 'applied' in the output must count all ops applied across chunks."""
    first, _ = _setup_chunked_journal(alpha, monkeypatch, chunk_ops=1)
    batch_id = first["batch_id"]
    prior = first["applied"]  # ops applied in first chunk

    monkeypatch.setenv("OPTIX_BATCH_CHUNK_SECONDS", "99999")
    monkeypatch.setenv("OPTIX_BATCH_CHUNK_OPS", "99999")

    out = core.bridge_edit_continue(alpha, "Alpha", batch_id)
    assert out["state"] == "succeeded"
    assert out["applied"] > prior  # more ops applied in total


# --- B.4: bridge_edit_status tests --------------------------------------------

def test_bridge_edit_status_with_known_batch_id(alpha, monkeypatch):
    """B.4: bridge_edit_status(cfg, project, batch_id) returns the journal doc."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)
    bid = out["batch_id"]

    status = core.bridge_edit_status(alpha, "Alpha", batch_id=bid)

    assert status["batch_id"] == bid
    assert status["state"] == "succeeded"
    assert status["project"] == "Alpha"
    assert status["op_count"] == 2


def test_bridge_edit_status_unknown_batch_id_returns_structured_error(alpha):
    """B.4: An unknown batch_id must return {state:'failed', error:'unknown_batch'}
    as a RETURN VALUE, never as a raised exception."""
    result = core.bridge_edit_status(alpha, "Alpha", batch_id="nonexistent000")

    # Must be a return value, not an exception.
    assert result["state"] == "failed"
    assert result["error"] == "unknown_batch"
    assert result["batch_id"] == "nonexistent000"
    assert "known" in result


def test_bridge_edit_status_without_batch_id_returns_list(alpha, monkeypatch):
    """B.4: Without a batch_id, return the most-recent journals for the project."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)
    bid = out["batch_id"]

    status = core.bridge_edit_status(alpha, "Alpha")

    assert "batches" in status
    assert status["project"] == "Alpha"
    batch_ids = [b["batch_id"] for b in status["batches"]]
    assert bid in batch_ids


def test_bridge_edit_status_filters_by_project(alpha, monkeypatch, cfg, projects_root):
    """B.4: The no-id form must never return a batch journalled for a different project."""
    from service.tests.conftest import make_project

    make_project(projects_root, "Beta")
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)

    # Write a journal for Alpha directly (simulate a completed batch).
    alpha_id = "alpha_batch_01"
    batches_dir = alpha.state_dir / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)
    (batches_dir / f"{alpha_id}.json").write_text(
        json.dumps({"batch_id": alpha_id, "project": "Alpha", "state": "succeeded",
                    "op_count": 1, "applied": 1}),
        encoding="utf-8",
    )
    # Write a journal for Beta into the same batches dir.
    beta_id = "beta_batch_002"
    (batches_dir / f"{beta_id}.json").write_text(
        json.dumps({"batch_id": beta_id, "project": "Beta", "state": "succeeded",
                    "op_count": 1, "applied": 1}),
        encoding="utf-8",
    )

    status = core.bridge_edit_status(alpha, "Alpha")

    batch_ids = [b["batch_id"] for b in status["batches"]]
    assert alpha_id in batch_ids
    assert beta_id not in batch_ids, "Beta's batch must never appear in Alpha's status"


def test_bridge_edit_status_without_batch_id_no_batches_dir(alpha):
    """B.4: When the batches dir does not exist, the no-id form returns an empty list."""
    status = core.bridge_edit_status(alpha, "Alpha")

    assert status["batches"] == []
    assert status["project"] == "Alpha"


def test_bridge_edit_status_unknown_batch_id_includes_known_list(alpha, monkeypatch):
    """B.4: The unknown-id response includes a list of known batch IDs for the project."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    _patch_apply_ok(monkeypatch)

    out = core.bridge_edit(alpha, "Alpha", _TWO_OPS)
    existing_bid = out["batch_id"]

    result = core.bridge_edit_status(alpha, "Alpha", batch_id="no_such_id_xyz")

    assert result["error"] == "unknown_batch"
    assert existing_bid in result["known"]


def test_normalize_edit_op_coalesces_attach_string_formatter_fields():
    """attach_string_formatter (2026-09-16) has the same name/prop_name seam as
    attach_expression — the C# validator reads `name`, the applier `prop_name`."""
    only_prop = core._normalize_edit_op(
        {"op": "attach_string_formatter", "prop_name": "Text"})
    assert only_prop == {"op": "attach_string_formatter",
                         "prop_name": "Text", "name": "Text"}
    only_name = core._normalize_edit_op(
        {"op": "attach_string_formatter", "name": "Text"})
    assert only_name == {"op": "attach_string_formatter",
                         "name": "Text", "prop_name": "Text"}


def test_attach_string_formatter_registered_in_edit_tables():
    """The verb must be dispatchable at the batch surface, routable to the
    bridge, and its positional/optional fields declared, or optix_bridge_edit
    rejects it as unknown / drops raw+mode as unknown fields."""
    assert "attach_string_formatter" in core.BRIDGE_EDIT_VERBS
    assert "attach_string_formatter" in core._BRIDGE_EDIT_OPS
    fn, required, optional = core._BRIDGE_EDIT_OPS["attach_string_formatter"]
    assert fn == "bridge_attach_string_formatter"
    assert required == ("path", "prop_name", "format")
    assert set(optional) == {"sources", "raw", "mode"}
    assert core._BRIDGE_EDIT_POSITIONAL["attach_string_formatter"] == (
        "path", "prop_name", "format")
    # raw + mode must be legal fields (not flagged as unknown) once name is injected
    op = {"op": "attach_string_formatter", "path": "UI/M/L", "name": "Text",
          "prop_name": "Text", "format": "{0:F1}", "sources": "Model/x",
          "raw": "1", "mode": "Read"}
    assert core.unknown_op_fields(op) == []


def test_bridge_attach_string_formatter_builds_raw_param(alpha, monkeypatch):
    """raw=True must reach the bridge as raw=1 (literal alias source); a falsey
    raw must not send the param at all."""
    calls: list = []
    monkeypatch.setattr(core, "_bridge_write",
                        lambda cfg, project, verb, route, params: calls.append((route, params)) or {"ok": True})
    core.bridge_attach_string_formatter(
        alpha, "Alpha", "UI/Templates/ValueOnlyHP/value", "Text",
        "{0:F1}", sources="{data}/value", raw=True)
    route, params = calls[-1]
    assert route == "/bridge/node/attach-string-formatter"
    assert params["raw"] == "1"
    assert params["format"] == "{0:F1}"
    calls.clear()
    core.bridge_attach_string_formatter(
        alpha, "Alpha", "UI/M/L", "Text", "{0:F1}", sources="Model/x")
    _, params2 = calls[-1]
    assert "raw" not in params2
