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


# --- 1.0.9 #3: guarded typed string[] path for GridLayout Rows/Columns ---------

def _grid_cfg(alpha, on=True):
    import dataclasses
    return dataclasses.replace(alpha, enable_grid_string_array=on)


def test_grid_string_array_flag_defaults_off(alpha):
    assert core.Config.__dataclass_fields__["enable_grid_string_array"].default is False
    assert alpha.enable_grid_string_array is False


def test_grid_string_array_flag_from_env(monkeypatch):
    monkeypatch.setenv("FTX_GRID_STRING_ARRAY", "1")
    assert core.Config.from_env().enable_grid_string_array is True
    monkeypatch.delenv("FTX_GRID_STRING_ARRAY")
    assert core.Config.from_env().enable_grid_string_array is False


@pytest.mark.parametrize("name", ["Rows", "Columns"])
def test_grid_string_array_flag_on_dispatches_typed(alpha, monkeypatch, name):
    cap: list = []
    routes = {"/bridge/node/property": (200, {"ok": True, "via": "typed-string-array",
                                              "datatype": "String[]"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_set_property(_grid_cfg(alpha), "Alpha", "UI/MainWindow/G1",
                                   name, '["1*","1*"]')
    assert out["ok"] is True
    _m, path = next(c for c in cap if "/bridge/node/property" in c[1])
    assert "typed=string%5B%5D" in path
    assert f"name={name}" in path


def test_grid_string_array_flag_off_still_rejected(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_set_property(alpha, "Alpha", "UI/MainWindow/G1", "Columns",
                                 '["1*","1*"]')
    assert "unsupported_array_write" in str(e.value)
    assert "FTX_GRID_STRING_ARRAY" in str(e.value)
    assert not [c for c in cap if "/bridge/node/property" in c[1]]


@pytest.mark.parametrize("name,value", [
    ("AliasNodeArray", '["UI/Screens/ScreenA"]'),   # other array property
    ("Children", '["a"]'),                           # other name
    ("rows", '["1*"]'),                              # case-sensitive
    ("Columns", '[1, 2]'),                           # non-string elements
    ("Columns", '["1*", 2]'),
    ("Columns", '[]'),                               # empty
    ("Columns", '[""]'),                             # blank element
    ("Columns", '[["1*"]]'),                         # nested
])
def test_grid_string_array_guard_not_loosened_when_flag_on(alpha, monkeypatch, name, value):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_set_property(_grid_cfg(alpha), "Alpha", "UI/MainWindow/G1",
                                 name, value)
    assert "unsupported_array_write" in str(e.value)
    assert not [c for c in cap if "/bridge/node/property" in c[1]]


def test_grid_string_array_non_gridlayout_rejected_by_bridge(alpha, monkeypatch):
    """The service cannot see node types; the bridge refuses a non-GridLayout and
    the code surfaces (never falls back to the scalar path)."""
    routes = {"/bridge/node/property": (200, {"error": {
        "code": "typed_array_not_supported",
        "message": "typed string[] writes are guarded to GridLayout nodes; "
                   "UI/MainWindow/R1 is Rectangle"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_set_property(_grid_cfg(alpha), "Alpha", "UI/MainWindow/R1",
                                 "Columns", ["1*"])
    assert "typed_array_not_supported" in str(e.value)


def test_grid_validate_ops_annotates_only_eligible_ops(alpha, monkeypatch):
    sent: list = []

    def fake_post(cfg, path, body, **kw):
        sent.append(body)
        return 200, {"ok": True, "op_count": 3, "strict": False, "errors": [], "warnings": []}

    monkeypatch.setattr(core, "_bridge_post_body", fake_post)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    ops = [
        {"op": "set_property", "path": "UI/G", "name": "Columns", "value": ["1*", "1*"]},
        {"op": "set_property", "path": "UI/G", "name": "AliasNodeArray", "value": ["a"]},
        {"op": "set_property", "path": "UI/L", "name": "Text", "value": "x"},
    ]
    core.bridge_validate_ops(_grid_cfg(alpha), "Alpha", ops)
    wire = sent[-1]["ops"]
    assert wire[0]["typed"] == "string[]" and wire[0]["value"] == '["1*", "1*"]'
    assert "typed" not in wire[1] and "typed" not in wire[2]
    core.bridge_validate_ops(alpha, "Alpha", ops)       # flag off: untouched
    assert all("typed" not in o for o in sent[-1]["ops"])


def test_gridlayout_warning_retired_only_when_both_set_and_flag_on(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_post_body", lambda *a, **k: (200, {
        "ok": True, "op_count": 1, "strict": False, "errors": [], "warnings": []}))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit", lambda cfg, project, op: {"ok": True})
    create = {"op": "create_widget", "screen": "UI/MainWindow", "name": "G",
              "type": "GridLayout"}
    both = [create,
            {"op": "set_property", "path": "UI/MainWindow/G", "name": "Rows", "value": ["1*"]},
            {"op": "set_property", "path": "UI/MainWindow/G", "name": "Columns", "value": ["1*"]}]
    one = both[:2]

    def codes(cfg, ops):
        out = core.bridge_edit(cfg, "Alpha", [dict(o) for o in ops])
        return [w["code"] for w in out["report"]["warnings"]]

    assert "gridlayout_without_columns" not in codes(_grid_cfg(alpha), both)
    assert "gridlayout_without_columns" in codes(_grid_cfg(alpha), one)
    assert "gridlayout_without_columns" in codes(alpha, both)       # flag off
    assert "gridlayout_without_columns" in codes(_grid_cfg(alpha), [create])


def test_bridge_source_typed_path_is_isolated_from_generic_guard():
    """Static pin on StudioMCPBridge.cs: the typed branch is gated on the explicit
    typed=string[] marker, limited to Rows/Columns on GridLayout, and the generic
    array guards/CoerceAssign backstop are still present and unconditional."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "studio-bridge" /
           "StudioMCPBridge.cs").read_text(encoding="utf-8")
    assert 'QueryParam(firstLine, "typed") == "string[]"' in src
    assert 'name == "Rows" || name == "Columns"' in src
    assert 't.Name == "GridLayout"' in src
    assert "var arrErr = DeclaredArrayGuard(node, name);\n            if (arrErr != null) return arrErr;" in src
    assert "if (IsArrayVariable(prop))\n                    return ArrayWriteError(" in src
    # the scalar coercion backstop still refuses arrays
    assert "unsupported_array_write: property is array-typed" in src


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
        alpha, "Alpha", "UI/MainWindow/L1", "Panel", "../Panel@NodeId")
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


# ---- ftx-mcp-wvk.3: create_object Folder / FolderType -------------------------

@pytest.mark.parametrize("spelling", ["Folder", "FolderType", "folder", "FOLDERTYPE"])
def test_create_object_folder_resolves_to_foldertype(alpha, monkeypatch, spelling):
    """Folder and FolderType both resolve to FolderType (ns=0 i=61) and succeed."""
    from urllib.parse import parse_qs, urlsplit
    assert core.resolve_create_object_type(spelling) == "FolderType"
    assert core.FOLDER_TYPE_NODE_ID == "ns=0;i=61"
    cap: list = []
    routes = {"/bridge/model/object": (200, {"ok": True, "created_path": "Model/F1",
              "type": "FolderType", "node_class": "Object"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_object(alpha, "Alpha", "Model", "F1", spelling)
    assert out["ok"] is True and out["type"] == "FolderType"
    _, path = next(c for c in cap if "/bridge/model/object" in c[1])
    assert parse_qs(urlsplit(path).query)["type"] == ["FolderType"]


def test_create_object_folder_not_refused_by_validator():
    assert core.validate_create_object_op("F", "Folder") == []
    assert core.validate_create_object_op("F", "FolderType") == []
    assert core.resolve_create_object_type("UI/Templates/Card") == "UI/Templates/Card"
    assert core.resolve_create_object_type(None) is None


def test_create_object_folder_type_not_found_names_working_route(alpha, monkeypatch):
    """When the bridge cannot create the Folder on this Studio version the result
    is type_not_found with a hint naming create_folder."""
    routes = {"/bridge/model/object": (200, {"error": {
        "code": "type_not_found", "message": "FolderType (ns=0;i=61) is not resolvable"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_create_object(alpha, "Alpha", "Model", "F1", "Folder")
    assert out["ok"] is False and out["code"] == "type_not_found"
    assert "create_folder" in out["hint"]
    assert out["type_node_id"] == "ns=0;i=61"


def test_create_object_other_type_not_found_still_raises(alpha, monkeypatch):
    routes = {"/bridge/model/object": (200, {"error": {
        "code": "type_not_found", "message": "no node at type path: Nope"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed):
        core.bridge_create_object(alpha, "Alpha", "Model", "X", "Nope")


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


def test_create_netlogic_design_time_posts_kind(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/netlogic": (200, {"ok": True,
              "created_path": "Model/DtLogic", "type": "NetLogic",
              "kind": "design_time", "node_class": "Object",
              "bound_class": "DtLogic"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_netlogic(alpha, "Alpha", "Model", "DtLogic",
                                      kind="design_time")
    assert out["kind"] == "design_time"
    method, path = next(c for c in cap if "/bridge/model/netlogic" in c[1])
    assert method == "POST" and "kind=design_time" in path


def test_create_netlogic_default_kind_omits_param(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/netlogic": (200, {"ok": True,
              "created_path": "Model/Logic", "type": "NetLogic"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_create_netlogic(alpha, "Alpha", "Model", "Logic", kind="runtime")
    _, path = next(c for c in cap if "/bridge/model/netlogic" in c[1])
    assert "kind=" not in path


def test_create_netlogic_invalid_kind_structured_error(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    out = core.bridge_create_netlogic(alpha, "Alpha", "Model", "Logic",
                                      kind="designtime")
    assert out["ok"] is False and out["code"] == "invalid_kind"
    err = out["errors"][0]
    assert err["field"] == "kind" and err["allowed"] == ["runtime", "design_time"]
    assert not any("/bridge/model/netlogic" in c[1] for c in cap)


def test_create_netlogic_missing_name_structured_error(alpha):
    out = core.bridge_create_netlogic(alpha, "Alpha", "Model", "")
    assert out["ok"] is False and out["code"] == "missing_field"
    assert out["errors"][0]["field"] == "name"


def test_create_netlogic_design_time_dry_run_reports_plan(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    out = core.bridge_create_netlogic(alpha, "Alpha", "Model", "DtLogic",
                                      kind="design_time", dry_run=True)
    assert out["ok"] is True and out["dry_run"] is True
    plan = out["plan"]
    assert plan["kind"] == "design_time" and plan["created_path"] == "Model/DtLogic"
    assert plan["children"] == [{
        "name": "BehaviourStartPriority",
        "variable_type": "BehaviourStartPriorityVariableType", "value": 180}]
    assert not any("/bridge/model/netlogic" in c[1] for c in cap)


def test_create_netlogic_dry_run_validates_kind(alpha):
    out = core.bridge_create_netlogic(alpha, "Alpha", "Model", "X",
                                      kind="bogus", dry_run=True)
    assert out["ok"] is False and out["code"] == "invalid_kind"


def test_create_netlogic_design_time_old_bridge_refused(alpha, monkeypatch):
    # A bridge that ignores `kind` answers without kind=design_time: surface it.
    routes = {"/bridge/model/netlogic": (200, {"ok": True,
              "created_path": "Model/DtLogic", "type": "NetLogic"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_create_netlogic(alpha, "Alpha", "Model", "DtLogic",
                                    kind="design_time")
    assert "design_time" in str(e.value)


def test_create_netlogic_design_time_bridge_source():
    """The C# mints BehaviourStartPriority typed BehaviourStartPriorityVariableType
    = 180 (source-level; the bridge is not compiled in CI)."""
    from pathlib import Path
    cs = (Path(__file__).parent.parent.parent / "studio-bridge"
          / "StudioMCPBridge.cs").read_text(encoding="utf-8")
    start = cs.index("private string CreateNetLogicInline")
    body = cs[start:start + 4500]
    assert "BehaviourStartPriorityVariableType" in body
    assert '"BehaviourStartPriority"' in body and "(byte)180" in body
    assert "design_time" in body and "invalid_kind" in body


def _method_body(cs: str, signature: str) -> str:
    start = cs.index(signature)
    return cs[start:cs.index("\n    }\n", start)]


def test_create_netlogic_add_inside_root_impersonation():
    """#27: CreateNetLogicInline's MakeObject and Add (parentNode.Add) both sit
    inside a `using (...Sessions.ImpersonateRootTemporary())` block; without a
    session the design-time instantiation of an already-compiled class killed
    Studio (c0000374). Source-level: the bridge is not compiled in CI."""
    from pathlib import Path
    cs = (Path(__file__).parent.parent.parent / "studio-bridge"
          / "StudioMCPBridge.cs").read_text(encoding="utf-8")
    body = _method_body(cs, "private string CreateNetLogicInline")
    scope = body.index("using (parentNode.Context.Sessions.ImpersonateRootTemporary())")
    open_brace = body.index("{", scope)
    depth, end = 0, None
    for i in range(open_brace, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    assert end is not None
    block = body[open_brace:end]
    assert "parentNode.Add(o)" in block
    assert "InformationModel.MakeObject(" in block
    assert body.count("parentNode.Add(") == 1
    assert body.count("InformationModel.MakeObject(") == 1


def test_session_affine_instantiation_paths_are_impersonated():
    """#27 audit: every bridge path that instantiates an Object into the model
    (create_object, create_netlogic, convert_to_type replace, move copy) wraps
    its MakeObject + Add in ImpersonateRootTemporary."""
    from pathlib import Path
    cs = (Path(__file__).parent.parent.parent / "studio-bridge"
          / "StudioMCPBridge.cs").read_text(encoding="utf-8")
    for sig in ("private string CreateObjectInline", "private string CreateNetLogicInline",
                "private string ConvertToTypeInline", "private string MoveNodeInline"):
        body = _method_body(cs, sig)
        scope = body.index("Sessions.ImpersonateRootTemporary()")
        assert body.index("InformationModel.MakeObject(", scope) > scope, sig
        assert "InformationModel.MakeObject(" not in body[:scope], sig


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
                        lambda cfg, project, verb, route, params, **kw: calls.append((route, params)) or {"ok": True})
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


# ---- wire_event multi-step (`calls`) + append (PRD 0024 / 1.0.9 9e) ----------

def _fake_bodied_bridge(monkeypatch, reply=None, *, capture=None):
    """Healthy fake bridge whose JSON-BODY POST (`_bridge_post_body`) is captured
    as (path, payload); the query-string route must NOT be used for a list."""
    cap = capture if capture is not None else []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))

    def fake_post_body(cfg, path, payload, timeout=20.0):
        cap.append(("BODY", path, payload))
        return 200, (reply if reply is not None else {"ok": True, "via": "eventhandler+steps"})

    monkeypatch.setattr(core, "_bridge_post_body", fake_post_body)
    return cap


_TWO_CALLS = [
    {"command": "SetVariable", "variable": "Model/Flag", "value": "true"},
    {"method_path": "UI/Logic/DoThing", "args": "NewPanel=UI/Screens/Home",
     "object_raw": "../Loader"},
]


def test_validate_wire_calls_clean_list_is_normalized():
    errors, norm = core.validate_wire_calls(
        [{"command": "togglevariable", "variable": "Model/Flag"},
         {"method_path": "UI/Logic/DoThing", "args": "A=1"}])
    assert errors == []
    # bridge spelling: `method`, canonical command casing, order preserved
    assert norm == [{"command": "ToggleVariable", "variable": "Model/Flag"},
                    {"method": "UI/Logic/DoThing", "args": "A=1"}]


@pytest.mark.parametrize("calls,code,idx", [
    ("nope", "calls_not_a_list", None),
    ([], "calls_empty", None),
    (["x"], "call_not_an_object", 0),
    ([{}], "call_missing_action", 0),
    ([{"command": "ToggleVariable", "variable": "M/F", "method_path": "A/B"}], "call_ambiguous", 0),
    ([{"command": "Explode"}], "call_unknown_command", 0),
    ([{"command": "ToggleVariable"}], "call_missing_variable", 0),
    ([{"command": "SetVariable", "variable": "M/F"}], "call_missing_value", 0),
    ([{"command": "ToggleVariable", "variable": "M/F", "args": "A=1"}],
     "call_args_with_variable_command", 0),
    ([{"method_path": "A/B", "variable": "M/F"}], "call_field_not_applicable", 0),
    ([{"method_path": "A/B", "bogus": "x"}], "unknown_call_field", 0),
    ([{"method_path": "A/B", "args": "no-equals"}], "call_bad_args", 0),
    ([{"command": "OpenKeyboard", "args": "KeyboardType=Numeric"}], "call_bad_args", 0),
    ([{"method_path": ["A/B"]}], "call_field_type", 0),
    # second call bad: the index points at it
    ([{"method_path": "A/B"}, {"command": "ToggleVariable"}], "call_missing_variable", 1),
])
def test_validate_wire_calls_structured_errors(calls, code, idx):
    errors, norm = core.validate_wire_calls(calls)
    assert norm == []
    hit = [e for e in errors if e["code"] == code]
    assert hit, f"expected {code}, got {errors}"
    assert hit[0]["call_index"] == idx and hit[0]["message"]


def test_wire_event_calls_posts_ordered_list_as_json_body(alpha, monkeypatch):
    cap = _fake_bodied_bridge(monkeypatch)
    out = core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                                 calls=_TWO_CALLS)
    assert out["ok"] is True
    (_tag, path, payload), = [c for c in cap if c[0] == "BODY"]
    assert path.startswith("/bridge/node/event?") and "event=MouseClickEvent" in path
    assert "append=" not in path and "replace=" not in path
    # order preserved, bridge field names
    assert payload == {"calls": [
        {"command": "SetVariable", "variable": "Model/Flag", "value": "true"},
        {"method": "UI/Logic/DoThing", "args": "NewPanel=UI/Screens/Home",
         "object_raw": "../Loader"}]}


def test_wire_event_append_flag_reaches_bridge(alpha, monkeypatch):
    cap = _fake_bodied_bridge(monkeypatch)
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                           calls=_TWO_CALLS, append=True)
    (_t, path, payload), = [c for c in cap if c[0] == "BODY"]
    assert "append=true" in path and len(payload["calls"]) == 2


def test_wire_event_single_call_with_append_appends_one_step(alpha, monkeypatch):
    cap = _fake_bodied_bridge(monkeypatch)
    core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                           command="ToggleVariable", variable="Model/Flag", append=True)
    (_t, path, payload), = [c for c in cap if c[0] == "BODY"]
    assert "append=true" in path
    assert payload == {"calls": [{"command": "ToggleVariable", "variable": "Model/Flag"}]}


def test_wire_event_calls_invalid_returns_structured_error_and_writes_nothing(alpha, monkeypatch):
    cap = _fake_bodied_bridge(monkeypatch)
    out = core.bridge_wire_event(
        alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
        calls=[{"method_path": "A/B"}, {"command": "SetVariable", "variable": "M/F"}])
    assert out["ok"] is False and out["code"] == "invalid_wire_calls"
    assert out["errors"][0]["code"] == "call_missing_value"
    assert out["errors"][0]["call_index"] == 1
    assert not any(c[0] == "BODY" or "/bridge/node/event" in c[1] for c in cap)


def test_wire_event_calls_conflicts_with_single_call_args(alpha, monkeypatch):
    cap = _fake_bodied_bridge(monkeypatch)
    out = core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                                 method_path="A/B", calls=_TWO_CALLS)
    assert out["code"] == "invalid_wire_calls"
    assert out["errors"][0]["code"] == "calls_conflict"
    assert not any(c[0] == "BODY" for c in cap)


def test_wire_event_append_with_replace_is_refused(alpha, monkeypatch):
    cap = _fake_bodied_bridge(monkeypatch)
    out = core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                                 calls=_TWO_CALLS, append=True, replace=True)
    assert out["code"] == "invalid_wire_calls"
    assert any(e["code"] == "append_with_replace" for e in out["errors"])
    assert not any(c[0] == "BODY" for c in cap)


def test_wire_event_op_spec_accepts_calls_and_append():
    _fn, _required, optional = core._BRIDGE_EDIT_OPS["wire_event"]
    assert optional["calls"] == "calls" and optional["append"] == "append"
    assert core.unknown_op_fields(
        {"op": "wire_event", "path": "UI/B", "event_type": "MouseClickEvent",
         "calls": _TWO_CALLS, "append": True}) == []


def test_apply_one_edit_forwards_calls_and_append(alpha, monkeypatch):
    cap = _fake_bodied_bridge(monkeypatch)
    core._apply_one_edit(alpha, "Alpha", {
        "op": "wire_event", "path": "UI/MainWindow/Btn", "event_type": "MouseClickEvent",
        "calls": _TWO_CALLS, "append": True})
    (_t, path, payload), = [c for c in cap if c[0] == "BODY"]
    assert "append=true" in path and len(payload["calls"]) == 2


def _dry_run(alpha, monkeypatch, op):
    """bridge_edit(dry_run) against a bridge whose validator reports clean, so any
    error in the report comes from the service-side list validation."""
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(
        core, "_bridge_post_body",
        lambda cfg, path, payload, timeout=20.0: (
            200, {"ok": True, "op_count": len(payload["ops"]), "errors": [], "warnings": []}))
    return core.bridge_edit(alpha, "Alpha", [op], dry_run=True)


def test_dry_run_accepts_valid_calls_list(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "wire_event", "path": "UI/MainWindow/Btn", "event_type": "MouseClickEvent",
        "calls": _TWO_CALLS})
    assert out["state"] == "validated" and out["report"]["ok"] is True
    assert out["applied"] == 0


def test_dry_run_returns_structured_errors_for_invalid_calls(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "wire_event", "path": "UI/MainWindow/Btn", "event_type": "MouseClickEvent",
        "calls": [{"method_path": "A/B"}, {"command": "ToggleVariable"}, {}]})
    assert out["state"] == "validated" and out["report"]["ok"] is False
    errs = out["report"]["errors"]
    assert [(e["op_index"], e["call_index"], e["code"]) for e in errs] == [
        (0, 1, "call_missing_variable"), (0, 2, "call_missing_action")]
    assert out["applied"] == 0 and "nudge" in out


def test_dry_run_refuses_calls_alongside_single_call_fields(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "wire_event", "path": "UI/MainWindow/Btn", "event_type": "MouseClickEvent",
        "method_path": "A/B", "calls": _TWO_CALLS})
    assert out["report"]["ok"] is False
    assert out["report"]["errors"][0]["code"] == "calls_conflict"


def test_dry_run_single_call_wire_event_unaffected(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "wire_event", "path": "UI/MainWindow/Btn", "event_type": "MouseClickEvent",
        "command": "ToggleVariable", "variable": "Model/Flag"})
    assert out["report"]["ok"] is True


def test_wire_event_multistep_bridge_error_surfaces(alpha, monkeypatch):
    _fake_bodied_bridge(monkeypatch, reply={
        "error": {"code": "invalid_wire_calls", "message": "calls[1]: no variable at: Model/Nope",
                  "call_index": 1}})
    with pytest.raises(core.BridgeWriteFailed, match="calls\\[1\\]"):
        core.bridge_wire_event(alpha, "Alpha", "UI/MainWindow/Btn", "MouseClickEvent",
                               calls=_TWO_CALLS)


def test_wire_event_multistep_keeps_node_attribute_guard():
    """BrowseName/DisplayName are never written as attributes (PRD 0024): the
    guard still sits on set_property/bind, and the multi-step path adds no
    attribute write of its own."""
    with pytest.raises(core.BridgeWriteFailed):
        core._reject_node_attribute("set_property", "BrowseName")
    from pathlib import Path
    text = (Path(__file__).parent.parent.parent / "studio-bridge" / "StudioMCPBridge.cs"
            ).read_text(encoding="utf-8", errors="replace")
    start = text.find("private string WireEventStepsInline(")
    end = text.find("// ---- setup / project scaffolding", start)
    body = text[start:end]
    assert "BrowseName =" not in body and "DisplayName =" not in body


# -- source-level checks of the C# multi-step logic ----------------------------

def _bridge_cs_text() -> str:
    from pathlib import Path
    return (Path(__file__).parent.parent.parent / "studio-bridge" / "StudioMCPBridge.cs"
            ).read_text(encoding="utf-8", errors="replace")


def _cs_region(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    assert start >= 0, f"{start_marker!r} not found in StudioMCPBridge.cs"
    end = text.find(end_marker, start + 1)
    assert end > start, f"{end_marker!r} not found after {start_marker!r}"
    return text[start:end]


def test_wire_event_route_reads_body_and_dispatches_to_steps():
    text = _bridge_cs_text()
    route = _cs_region(text, 'StartsWith("POST /bridge/node/event")',
                       'StartsWith("POST /bridge/node/invoke")')
    assert "ReadRequestBody(" in route and "WireEventInline(firstLine," in route
    inline = _cs_region(text, "private string WireEventInline(", "string path = QueryParam")
    assert "WireEventStepsInline(firstLine, reqBody)" in inline


def test_wire_event_steps_appends_method_containers_in_order():
    text = _bridge_cs_text()
    body = _cs_region(text, "private string AppendWireCall(", "private string WireEventStepsInline(")
    # a NEW MethodContainerN is added to the SAME handler's MethodsToCall
    assert 'MakeObject("MethodContainer" + k)' in body
    assert "handler.MethodsToCall.Add(mc)" in body
    # same container shape as MethodContainer1: pointer, method, arguments
    for needle in ('"ObjectPointer"', '"Method"', '"InputArguments"', "HasDynamicLink",
                   '"../../../../..@NodeId"', "readback_mismatch"):
        assert needle in body, needle
    # never creates a second EventHandler
    assert "MakeObject<FTOptix.CoreBase.EventHandler>" not in body


def test_wire_event_steps_append_mode_keeps_existing_calls_and_rolls_back():
    text = _bridge_cs_text()
    body = _cs_region(text, "private string WireEventStepsInline(", "private void ValidateWireCallsOp(")
    # append=true skips the single-call (create / replace-in-place) path entirely
    assert 'QueryParam(firstLine, "append")' in body
    assert "appendMode" in body and "if (!appendMode)" in body
    # existing containers are counted, and the post-write count is verified
    assert "priorCount" in body and "readback_mismatch" in body
    # every call is resolved before call 0 is written; a failing append is undone
    assert body.index("ResolveWireCallTarget(") < body.index("string r0 = WireEventInline(")
    assert "mcAdded.Delete()" in body and "handler.Delete()" in body
    # calls are appended in list order
    assert "for (int i = first; i < steps.Count; i++)" in body


def test_validate_ops_checks_wire_event_calls_in_bridge():
    text = _bridge_cs_text()
    case = _cs_region(text, 'case "wire_event":', 'case "move":')
    assert "ValidateWireCallsOp(" in case
    helper = _cs_region(text, "private void ValidateWireCallsOp(", "// ---- setup / project scaffolding")
    for code in ("calls_not_a_list", "call_missing_action", "call_ambiguous",
                 "call_unknown_command", "call_missing_variable", "unresolved_reference",
                 "call_bad_args", "calls_empty"):
        assert f'"{code}"' in helper, code
    assert "HypoResolve(" in helper  # targets created earlier in the batch validate clean


# -- set_text_key: a text property set to a translation key (ftx-mcp-8fy.2) ----

def test_validate_text_key_op_clean():
    assert core.validate_text_key_op("UI/W/L1", "Text", "greeting") == []
    assert core.validate_text_key_op("UI/W/L1", "Text", "Ns.group_1/hello", "de-DE") == []


@pytest.mark.parametrize("path,name,key,locale,code,field", [
    ("", "Text", "k", "en-US", "missing_field", "path"),
    ("UI/L", None, "k", "en-US", "missing_field", "name"),
    ("UI/L", "Text", "", "en-US", "missing_field", "key"),
    ("UI/L", "Text", "   ", "en-US", "missing_field", "key"),
    ("UI/L", "Text", " k", "en-US", "invalid_text_key", "key"),
    ("UI/L", "Text", "a\nb", "en-US", "invalid_text_key", "key"),
    ("UI/L", "Text", "k" * 257, "en-US", "invalid_text_key", "key"),
    ("UI/L", "Text", "k", "", "invalid_locale", "locale"),
    ("UI/L", "Text", "k", "en US", "invalid_locale", "locale"),
    ("UI/L", "BrowseName", "k", "en-US", "node_attribute_not_settable", "name"),
    ("UI/L", "DisplayName", "k", "en-US", "node_attribute_not_settable", "name"),
])
def test_validate_text_key_op_structured_errors(path, name, key, locale, code, field):
    errs = core.validate_text_key_op(path, name, key, locale)
    assert [(e["code"], e["field"]) for e in errs] == [(code, field)]
    assert errs[0]["message"]


def test_set_text_key_posts_key_not_value(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/text-key": (200, {"ok": True, "key": "greeting",
                                              "text_id": "greeting", "translated": False})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_set_text_key(alpha, "Alpha", "UI/MainWindow/L1", "Text", "greeting")
    assert out["ok"] is True and out["text_id"] == "greeting"
    method, path = next(c for c in cap if "/bridge/node/text-key" in c[1])
    assert method == "POST"
    assert "path=UI%2FMainWindow%2FL1" in path and "name=Text" in path
    assert "key=greeting" in path and "locale=en-US" in path
    assert "value=" not in path          # a key, never a literal value
    assert not any("/bridge/node/property" in c[1] for c in cap)


@pytest.mark.parametrize("attr", ["BrowseName", "DisplayName", "Description", "NodeId"])
def test_set_text_key_rejects_node_attributes_before_dispatch(alpha, monkeypatch, attr):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    with pytest.raises(core.BridgeWriteFailed, match="node_attribute_not_settable"):
        core.bridge_set_text_key(alpha, "Alpha", "UI/MainWindow/L1", attr, "greeting")
    assert not any("/bridge/node/" in c[1] for c in cap)


def test_set_text_key_invalid_returns_structured_error_and_writes_nothing(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    out = core.bridge_set_text_key(alpha, "Alpha", "UI/MainWindow/L1", "Text", " bad key ")
    assert out["ok"] is False and out["code"] == "invalid_text_key"
    assert out["errors"][0]["code"] == "invalid_text_key"
    assert not any("/bridge/node/" in c[1] for c in cap)


def test_set_text_key_bridge_error_surfaces(alpha, monkeypatch):
    routes = {"/bridge/node/text-key": (200, {"error": {
        "code": "not_localized_text", "message": "property 'Width' is Double"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed, match="not_localized_text"):
        core.bridge_set_text_key(alpha, "Alpha", "UI/MainWindow/L1", "Width", "k")


def test_set_text_key_op_is_a_batch_verb():
    assert "set_text_key" in core.BRIDGE_EDIT_VERBS
    assert core.unknown_op_fields(
        {"op": "set_text_key", "path": "A", "name": "Text", "key": "k", "locale": "de-DE"}) == []
    assert core.unknown_op_fields(
        {"op": "set_text_key", "path": "A", "name": "Text", "value": "k"}) == ["value"]


def test_apply_one_edit_set_text_key(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/text-key": (200, {"ok": True, "text_id": "k"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core._apply_one_edit(alpha, "Alpha", {
        "op": "set_text_key", "path": "UI/W/L1", "name": "Text", "key": "k", "locale": "de-DE"})
    _m, path = next(c for c in cap if "/bridge/node/text-key" in c[1])
    assert "key=k" in path and "locale=de-DE" in path


def test_dry_run_accepts_valid_set_text_key(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "set_text_key", "path": "UI/W/L1", "name": "Text", "key": "greeting"})
    assert out["state"] == "validated" and out["report"]["ok"] is True
    assert out["applied"] == 0


def test_dry_run_set_text_key_returns_structured_errors(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "set_text_key", "path": "UI/W/L1", "name": "DisplayName", "key": " k"})
    assert out["state"] == "validated" and out["report"]["ok"] is False
    errs = out["report"]["errors"]
    assert {(e["op_index"], e["code"]) for e in errs} == {
        (0, "node_attribute_not_settable"), (0, "invalid_text_key")}
    assert out["applied"] == 0


def test_set_property_still_rejects_browsename_and_displayname_route_unchanged(alpha, monkeypatch):
    """_reject_node_attribute is preserved: BrowseName is refused, and the literal
    DisplayName route is still the attribute endpoint (not text-key)."""
    with pytest.raises(core.BridgeWriteFailed, match="node_attribute_not_settable"):
        core.bridge_set_property(alpha, "Alpha", "UI/W/L1", "BrowseName", "x")
    cap: list = []
    routes = {"/bridge/node/displayname": (200, {"ok": True})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_set_property(alpha, "Alpha", "UI/W/L1", "DisplayName", "x")
    assert any("/bridge/node/displayname" in c[1] for c in cap)
    assert not any("text-key" in c[1] for c in cap)


# -- source-level checks of the C# set_text_key logic ---------------------------

def test_set_text_key_route_is_dispatched_and_logged():
    text = _bridge_cs_text()
    route = _cs_region(text, 'StartsWith("POST /bridge/node/text-key")',
                       'StartsWith("POST /bridge/node/bind")')
    assert "SetTextKeyInline(firstLine)" in route
    assert '"POST /bridge/node/text-key"' in _cs_region(
        text, "_MutationRoutes = {", "};")


def test_set_text_key_bridge_writes_text_id_not_literal_and_no_attributes():
    text = _bridge_cs_text()
    body = _cs_region(text, "private string SetTextKeyInline(", "private static string TextKeyError(")
    # a keyed LocalizedText: (namespace, TextId, text, locale) -- not the literal ctor
    assert "new LocalizedText(nsIdx, key, known, locale)" in body
    assert "new LocalizedText(raw" not in body
    assert "TextId" in body                       # read-back
    # attributes refused FIRST, before the node is even resolved
    assert body.index("IsNodeAttributeName(name)") < body.index("ResolveNode(path)")
    assert "node_attribute_not_settable" in body
    # same crash-safety gates as set_property before materializing a property
    assert body.index("DeclaredPropertyGuard(node, name)") < body.index("GetOrCreateVariable(name)")
    assert "DeclaredArrayGuard(node, name)" in body
    assert "not_localized_text" in body
    # never assigns a node attribute
    assert "BrowseName =" not in body and "DisplayName =" not in body
    assert "node.DisplayName" not in body


def test_validate_ops_checks_set_text_key_in_bridge():
    text = _bridge_cs_text()
    case = _cs_region(text, 'case "set_text_key":', 'case "delete":')
    for needle in ("IsNodeAttributeName(name)", "text_key_missing", "invalid_text_key",
                   "TextKeyError(tkKey)", "moved_earlier_in_batch", "ValidateOnNode("):
        assert needle in case, needle
    vo = _cs_region(text, "private void ValidateOnNode(", "private static string NearestHint(")
    assert 'verb == "set_text_key"' in vo and "not_localized_text" in vo


def test_set_text_key_registered_as_gated_author_tool(cfg):
    from service import auth
    from service.mcp_app import make_mcp
    assert auth.TOOL_SCOPES.get("optix_bridge_set_text_key") == "author"
    names = {t.name for t in make_mcp(cfg)._tool_manager.list_tools()}
    assert "optix_bridge_set_text_key" not in names      # gated with the primitives


# -- add_translations bulk + remove_translation (ftx-mcp-8fy.3) -----------------

def test_validate_translation_rows_clean_applies_default_locale():
    errs, norm = core.validate_translation_rows(
        [{"key": "a", "value": "A"}, {"key": "a", "value": "Ah", "locale": "de-DE"}], "en-US")
    assert errs == []
    assert norm == [{"key": "a", "value": "A", "locale": "en-US"},
                    {"key": "a", "value": "Ah", "locale": "de-DE"}]


@pytest.mark.parametrize("rows,code,idx", [
    ("nope", "rows_not_a_list", None),
    ([], "rows_empty", None),
    (["x"], "row_not_an_object", 0),
    ([{"value": "v"}], "missing_field", 0),
    ([{"key": "k"}], "missing_field", 0),
    ([{"key": "k", "value": 3}], "missing_field", 0),
    ([{"key": " k", "value": "v"}], "invalid_text_key", 0),
    ([{"key": "k", "value": "v", "locale": "en US"}], "invalid_locale", 0),
    ([{"key": "k", "value": "v", "bogus": 1}], "unknown_row_field", 0),
    ([{"key": "k", "value": "v"}, {"key": "k", "value": "w"}], "duplicate_row", 1),
])
def test_validate_translation_rows_structured_errors(rows, code, idx):
    errs, _ = core.validate_translation_rows(rows)
    assert code in [e["code"] for e in errs]
    e = next(e for e in errs if e["code"] == code)
    assert e["message"]
    assert e.get("row_index") == idx


def test_validate_translation_rows_reports_every_bad_row():
    errs, _ = core.validate_translation_rows(
        [{"key": "", "value": "v"}, {"key": "ok", "value": "v"}, {"key": "k", "value": None}])
    assert [(e["row_index"], e["field"]) for e in errs] == [(0, "key"), (2, "value")]


def test_validate_remove_translation_op():
    assert core.validate_remove_translation_op("greeting") == []
    assert core.validate_remove_translation_op("")[0]["code"] == "missing_field"
    assert core.validate_remove_translation_op(None)[0]["code"] == "missing_field"
    assert core.validate_remove_translation_op("a\nb")[0]["code"] == "invalid_text_key"


def test_add_translations_posts_all_rows_in_one_call(alpha, monkeypatch):
    cap: list = []
    bodies: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))

    def fake_body(cfg, path, payload, timeout=20.0):
        bodies.append((path, payload))
        return 200, {"ok": True, "count": len(payload["rows"]), "added": 2, "updated": 0}

    monkeypatch.setattr(core, "_bridge_post_body", fake_body)
    out = core.bridge_add_translations(
        alpha, "Alpha", [{"key": "a", "value": "A"}, {"key": "b", "value": "B", "locale": "de-DE"}])
    assert out["ok"] is True and out["count"] == 2
    assert len(bodies) == 1                                   # ONE bridge call
    path, payload = bodies[0]
    assert path.startswith("/bridge/i18n/translations?") and "locale=en-US" in path
    assert "dry_run" not in path
    assert payload["rows"] == [{"key": "a", "value": "A", "locale": "en-US"},
                               {"key": "b", "value": "B", "locale": "de-DE"}]


def test_add_translations_dry_run_flag_reaches_bridge(alpha, monkeypatch):
    seen: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0: (seen.append(path) or (200, {"ok": True, "dry_run": True})))
    out = core.bridge_add_translations(alpha, "Alpha", [{"key": "a", "value": "A"}], dry_run=True)
    assert out["dry_run"] is True and "dry_run=true" in seen[0]


def test_add_translations_invalid_rows_structured_error_and_no_bridge_write(alpha, monkeypatch):
    seen: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0: (seen.append(path) or (200, {"ok": True})))
    out = core.bridge_add_translations(
        alpha, "Alpha", [{"key": "good", "value": "v"}, {"key": "", "value": "v"}])
    assert out["ok"] is False and out["code"] == "invalid_translation_rows"
    assert out["errors"][0]["row_index"] == 1
    assert seen == []                                          # the good row was NOT sent either


def test_remove_translation_existing_key_is_not_supported(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/i18n/translation/remove": (200, {"ok": False, "error": {
        "code": "not_supported", "message": core.REMOVE_TRANSLATION_NOT_SUPPORTED}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_remove_translation(alpha, "Alpha", "k1")
    assert out["ok"] is False and out["code"] == "not_supported" and out["key"] == "k1"
    assert out["message"] == core.REMOVE_TRANSLATION_NOT_SUPPORTED and out["nudge"]
    m, p = next(c for c in cap if "/bridge/i18n/translation/remove" in c[1])
    assert m == "POST" and "key=k1" in p and "dry_run" not in p


def test_remove_translation_dry_run_is_still_not_supported(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/i18n/translation/remove": (200, {"ok": False, "error": {
        "code": "not_supported", "message": "x"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_remove_translation(alpha, "Alpha", "k1", dry_run=True)
    assert out["ok"] is False and out["code"] == "not_supported"
    assert any("dry_run=true" in c[1] for c in cap)


def test_remove_translation_not_supported_message_is_exact():
    assert core.REMOVE_TRANSLATION_NOT_SUPPORTED == (
        "removing a translation row is not supported by the Optix SDK; edit the key in "
        "Studio's Translations editor or overwrite its text with add/set")


def test_remove_translation_missing_row_is_structured_error(alpha, monkeypatch):
    routes = {"/bridge/i18n/translation/remove": (200, {"error": {
        "code": "translation_not_found", "message": "no translation row for key 'nope'"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_remove_translation(alpha, "Alpha", "nope")
    assert out["ok"] is False and out["code"] == "translation_not_found"
    assert out["key"] == "nope" and out["message"] and out["nudge"]


def test_remove_translation_other_bridge_errors_still_raise(alpha, monkeypatch):
    routes = {"/bridge/i18n/translation/remove": (200, {"error": {
        "code": "boom", "message": "kaput"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed, match="kaput"):
        core.bridge_remove_translation(alpha, "Alpha", "k")


def test_remove_translation_invalid_key_writes_nothing(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    out = core.bridge_remove_translation(alpha, "Alpha", " ")
    assert out["ok"] is False and out["code"] == "invalid_translation_key"
    assert not any("/bridge/i18n/" in c[1] for c in cap)


def test_translation_ops_are_batch_verbs():
    assert {"add_translations", "remove_translation"} <= core.BRIDGE_EDIT_VERBS
    assert core.unknown_op_fields(
        {"op": "add_translations", "rows": [], "locale": "de-DE"}) == []
    assert core.unknown_op_fields({"op": "remove_translation", "key": "k"}) == []
    assert core.unknown_op_fields({"op": "remove_translation", "key": "k", "locale": "x"}) == ["locale"]


def test_apply_one_edit_add_translations(alpha, monkeypatch):
    bodies: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0: (bodies.append(payload) or (200, {"ok": True})))
    core._apply_one_edit(alpha, "Alpha", {
        "op": "add_translations", "rows": [{"key": "a", "value": "A"}], "locale": "fr-FR"})
    assert bodies[0]["rows"] == [{"key": "a", "value": "A", "locale": "fr-FR"}]


def test_apply_one_edit_missing_row_fails_the_op(alpha, monkeypatch):
    routes = {"/bridge/i18n/translation/remove": (200, {"error": {
        "code": "translation_not_found", "message": "no row"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed, match="translation_not_found"):
        core._apply_one_edit(alpha, "Alpha", {"op": "remove_translation", "key": "zzz"})


def test_apply_one_edit_remove_translation_not_supported_fails_the_op(alpha, monkeypatch):
    routes = {"/bridge/i18n/translation/remove": (200, {"ok": False, "error": {
        "code": "not_supported", "message": "x"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed, match="not_supported"):
        core._apply_one_edit(alpha, "Alpha", {"op": "remove_translation", "key": "a"})


def test_dry_run_accepts_valid_translation_ops(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "add_translations", "rows": [{"key": "a", "value": "A"}]})
    assert out["state"] == "validated" and out["report"]["ok"] is True and out["applied"] == 0


def test_dry_run_translation_ops_return_structured_errors(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "add_translations",
        "rows": [{"key": "a", "value": "A"}, {"key": "a", "value": "B"}, {"value": "x"}]})
    assert out["report"]["ok"] is False and out["applied"] == 0
    errs = out["report"]["errors"]
    assert {(e["op_index"], e["code"], e["row_index"]) for e in errs} == {
        (0, "duplicate_row", 1), (0, "missing_field", 2)}
    out = _dry_run(alpha, monkeypatch, {"op": "remove_translation", "key": ""})
    assert [e["code"] for e in out["report"]["errors"]] == ["missing_field"]


def test_batch_remove_translation_reports_not_supported_with_op_index(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {"op": "remove_translation", "key": "a"})
    assert out["report"]["ok"] is False and out["applied"] == 0
    errs = out["report"]["errors"]
    assert [(e["op_index"], e["code"], e["message"]) for e in errs] == [
        (0, "not_supported", core.REMOVE_TRANSLATION_NOT_SUPPORTED)]


def test_batch_remove_translation_applies_nothing(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    monkeypatch.setattr(core, "_bridge_post_body", lambda cfg, path, payload, timeout=20.0: (
        200, {"ok": True, "op_count": len(payload["ops"]), "errors": [], "warnings": []}))
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "add_translations", "rows": [{"key": "a", "value": "A"}]},
        {"op": "remove_translation", "key": "a"}])
    assert out["state"] != "succeeded" and out["applied"] == 0
    assert [(e["op_index"], e["code"]) for e in out["report"]["errors"]] == [(1, "not_supported")]
    assert not any("/bridge/i18n/" in c[1] for c in cap)

def test_dry_run_translation_ops_do_not_mutate(alpha, monkeypatch):
    """dry_run never reaches a translation route, only /bridge/validate_ops."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    posted: list = []
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0: (posted.append(path) or (
                            200, {"ok": True, "op_count": 2, "errors": [], "warnings": []})))
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "add_translations", "rows": [{"key": "a", "value": "A"}]},
        {"op": "remove_translation", "key": "a"}], dry_run=True)
    assert out["applied"] == 0 and out["state"] == "validated"
    assert posted == ["/bridge/validate_ops"] or all("i18n" not in p for p in posted)
    assert not any("/bridge/i18n/" in c[1] for c in cap)


# -- source-level checks of the C# translation logic ----------------------------

def test_translation_routes_are_dispatched_before_the_prefix_route_and_logged():
    text = _bridge_cs_text()
    bulk = text.index('StartsWith("POST /bridge/i18n/translations")')
    rem = text.index('StartsWith("POST /bridge/i18n/translation/remove")')
    single = text.index('StartsWith("POST /bridge/i18n/translation")')
    # "…/translation" is a prefix of both new routes, so they MUST be tested first
    assert bulk < single and rem < single
    assert "AddTranslationsInline(firstLine, ReadRequestBody(" in text[bulk:single]
    assert "RemoveTranslationInline(firstLine)" in text[rem:single]
    assert '"POST /bridge/i18n/translation"' in _cs_region(text, "_MutationRoutes = {", "};")


def test_add_translations_bridge_validates_all_rows_before_writing():
    text = _bridge_cs_text()
    body = _cs_region(text, "private string AddTranslationsInline(", "private string RemoveTranslationInline(")
    assert "ReadRequestBody" not in body                     # body is passed in by the dispatcher
    assert "TranslationRowError(" in body and "duplicate_row" in body
    # the write calls come strictly after the validation loop, and honour dry_run
    assert body.index("rows.Add(") < body.index("UpsertTranslation(")
    assert "InformationModel.SetTranslation(" in text      # existing key -> SetTranslation (in UpsertTranslation)
    assert "if (!dryRun)" in body
    assert "row_failed" in body                              # partial failure says how many applied
    assert "new LocalizedText(nsIdx, k, v, loc)" in body


def test_remove_translation_bridge_is_not_supported_and_never_calls_the_sdk():
    text = _bridge_cs_text()
    assert "RemoveTranslation(" not in text.replace("RemoveTranslationInline(", "")
    body = _cs_region(text, "private string RemoveTranslationInline(", "private string DeleteNodeInline(")
    assert "translation_not_found" in body and "not_supported" in body
    assert body.index("TextKeyError(") < body.index("translation_not_found") < body.index("not_supported")
    assert "removing a translation row is not supported by the Optix SDK; edit the key in " \
           "Studio's Translations editor or overwrite its text with add/set" in text
    assert "dryRun" not in body                      # dry_run gets the same answer


def test_remove_translation_inline_and_batch_share_one_existence_check():
    """ftx-mcp-phv.4 (#40): the inline route and the batch case call the same helper."""
    text = _bridge_cs_text()
    inline = _cs_region(text, "private string RemoveTranslationInline(", "private string DeleteNodeInline(")
    batch = _cs_region(text, 'case "remove_translation":', 'case "delete":')
    assert "TranslationRowExists(key)" in inline
    assert "LookupTranslation" not in inline            # no private lookup of its own
    assert "TranslationRowExists(rmKey)" in batch
    assert "LookupTranslation" not in batch
    helper = _cs_region(text, "private static bool TranslationRowExists(", "private static void NoteTranslationWritten(")
    assert "TranslationLookupHit(key)" in helper and "TranslationRowsWritten" in helper
    # the raw SDK lookup for translation rows lives only in TranslationLookupText
    # (PRD 0052 C: namespaced + per-locale probe; TranslationLookupHit delegates)
    lookup = _cs_region(text, "private static string TranslationLookupText(", "private static bool TranslationLookupHit(")
    assert "InformationModel.LookupTranslation(" in lookup
    assert "LookupTranslation" not in _cs_region(text, "private static bool UpsertTranslation(", "// Structural check")


def test_remove_translation_after_primitive_add_and_batch_add_is_not_supported(monkeypatch):
    """Python side: remove of a key just added (primitive or batch) is not_supported,
    an unknown key is translation_not_found; the bridge answer is surfaced verbatim."""
    rows = {}

    def bridge(cfg, project, op, path, params, *a, **k):
        if path == "/bridge/i18n/translation/remove":
            key = params["key"]
            if key not in rows:
                raise core.BridgeWriteFailed("translation_not_found: no translation row for key '%s'" % key)
            return {"ok": False, "error": {"code": "not_supported", "message": "removing a translation row is not supported"}}
        raise AssertionError(path)

    monkeypatch.setattr(core, "_bridge_write", bridge, raising=False)
    rows["PrimKey"] = "x"   # as if added by primitive add_translations
    rows["BatchKey"] = "y"  # as if added by a batch add_translations op
    cfg = project = None
    for key in ("PrimKey", "BatchKey"):
        res = core.bridge_remove_translation(cfg, project, key)
        assert res["ok"] is False and res["code"] == "not_supported", res
    unknown = core.bridge_remove_translation(cfg, project, "NoSuchKey")
    assert unknown["ok"] is False and unknown["code"] == "translation_not_found", unknown


def test_validate_ops_checks_translation_ops_in_bridge():
    text = _bridge_cs_text()
    add = _cs_region(text, 'case "add_translations":', 'case "remove_translation":')
    for needle in ("rows_empty", "TranslationRowError(", "duplicate_row", "txAdded.Add("):
        assert needle in add, needle
    rem = _cs_region(text, 'case "remove_translation":', 'case "delete":')
    for needle in ("translation_not_found", "TranslationRowExists(rmKey)", "txAdded", "not_supported"):
        assert needle in rem, needle


def test_translation_tools_registered_as_gated_author_tools(cfg):
    from service import auth
    from service.mcp_app import make_mcp
    names = {t.name for t in make_mcp(cfg)._tool_manager.list_tools()}
    for tool in ("optix_bridge_add_translations", "optix_bridge_remove_translation"):
        assert auth.TOOL_SCOPES.get(tool) == "author"
        assert tool not in names                             # gated with the primitives


# -- set_modelling_rule: ModellingRule on type members (ftx-mcp-8fy.4) -----------

@pytest.mark.parametrize("rule", ["Mandatory", "Optional", "None", "mandatory", "OPTIONAL", "none"])
def test_validate_modelling_rule_op_accepts_the_three_rules(rule):
    assert core.validate_modelling_rule_op("Model/Types/T/Speed", rule) == []


def test_normalize_modelling_rule_is_canonical():
    assert core.normalize_modelling_rule("mandatory") == "Mandatory"
    assert core.normalize_modelling_rule("OPTIONAL") == "Optional"
    assert core.normalize_modelling_rule("none") == "None"
    for bad in ("Required", "", " Mandatory", None, 1, "ExposesItsArray"):
        assert core.normalize_modelling_rule(bad) is None


@pytest.mark.parametrize("path,rule,code,field", [
    ("", "Mandatory", "missing_field", "path"),
    (None, "Mandatory", "missing_field", "path"),
    ("T/Speed", None, "missing_field", "rule"),
    ("T/Speed", "  ", "missing_field", "rule"),
    ("T/Speed", "Required", "invalid_modelling_rule", "rule"),
    ("T/Speed", "Mandatory ", "invalid_modelling_rule", "rule"),
    ("T/Speed", 3, "invalid_modelling_rule", "rule"),
])
def test_validate_modelling_rule_op_structured_errors(path, rule, code, field):
    errs = core.validate_modelling_rule_op(path, rule)
    assert [(e["code"], e["field"]) for e in errs] == [(code, field)]
    assert errs[0]["message"]


def test_invalid_modelling_rule_error_lists_allowed_values():
    err = core.validate_modelling_rule_op("T/Speed", "Required")[0]
    assert err["allowed"] == ["Mandatory", "Optional", "None"]


def test_set_modelling_rule_posts_canonical_rule(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/modelling-rule": (200, {
        "ok": True, "rule": "Mandatory", "previous": "None"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_set_modelling_rule(alpha, "Alpha", "Model/Types/T/Speed", "mandatory")
    assert out["ok"] is True and out["rule"] == "Mandatory"
    method, path = next(c for c in cap if "/bridge/node/modelling-rule" in c[1])
    assert method == "POST"
    assert "path=Model%2FTypes%2FT%2FSpeed" in path and "rule=Mandatory" in path
    assert "dry_run" not in path


def test_set_modelling_rule_dry_run_forwards_flag(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/modelling-rule": (200, {"ok": True, "would_set": True})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_set_modelling_rule(alpha, "Alpha", "T/Speed", "Optional", dry_run=True)
    _m, path = next(c for c in cap if "/bridge/node/modelling-rule" in c[1])
    assert "dry_run=true" in path and "rule=Optional" in path


def test_set_modelling_rule_invalid_rule_returns_structured_error_and_writes_nothing(
        alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    out = core.bridge_set_modelling_rule(alpha, "Alpha", "T/Speed", "Required", dry_run=True)
    assert out["ok"] is False and out["code"] == "invalid_modelling_rule"
    assert out["errors"][0]["field"] == "rule"
    assert not any("/bridge/node/" in c[1] for c in cap)


def test_set_modelling_rule_bridge_error_surfaces(alpha, monkeypatch):
    routes = {"/bridge/node/modelling-rule": (200, {"error": {
        "code": "not_a_type_member", "message": "UI/Screen1 is not a member"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed, match="not_a_type_member"):
        core.bridge_set_modelling_rule(alpha, "Alpha", "UI/Screen1", "Mandatory")


def test_set_modelling_rule_op_is_a_batch_verb():
    assert "set_modelling_rule" in core.BRIDGE_EDIT_VERBS
    assert core.unknown_op_fields(
        {"op": "set_modelling_rule", "path": "T/Speed", "rule": "Optional"}) == []
    assert core.unknown_op_fields(
        {"op": "set_modelling_rule", "path": "T/Speed", "rule": "Optional",
         "value": "x"}) == ["value"]


def test_apply_one_edit_set_modelling_rule(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/modelling-rule": (200, {"ok": True, "rule": "None"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core._apply_one_edit(alpha, "Alpha", {
        "op": "set_modelling_rule", "path": "T/Speed", "rule": "none"})
    _m, path = next(c for c in cap if "/bridge/node/modelling-rule" in c[1])
    assert "rule=None" in path


def test_dry_run_accepts_valid_set_modelling_rule(alpha, monkeypatch):
    for rule in ("Mandatory", "Optional", "None"):
        out = _dry_run(alpha, monkeypatch, {
            "op": "set_modelling_rule", "path": "Model/Types/T/Speed", "rule": rule})
        assert out["state"] == "validated" and out["report"]["ok"] is True
        assert out["applied"] == 0


def test_dry_run_rejects_invalid_modelling_rule_with_structured_error(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "set_modelling_rule", "path": "Model/Types/T/Speed", "rule": "Required"})
    assert out["state"] == "validated" and out["report"]["ok"] is False
    errs = out["report"]["errors"]
    assert [(e["op_index"], e["code"], e["field"]) for e in errs] == [
        (0, "invalid_modelling_rule", "rule")]
    assert out["applied"] == 0


def test_dry_run_rejects_missing_rule(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {"op": "set_modelling_rule", "path": "T/Speed"})
    assert out["report"]["ok"] is False
    assert [e["code"] for e in out["report"]["errors"]] == ["missing_field"]


def test_apply_batch_with_invalid_modelling_rule_applies_nothing(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    monkeypatch.setattr(
        core, "_bridge_post_body",
        lambda cfg, path, payload, timeout=20.0: (
            200, {"ok": True, "op_count": len(payload["ops"]), "errors": [], "warnings": []}))
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "set_modelling_rule", "path": "T/Speed", "rule": "Required"}])
    assert out.get("applied", 0) == 0
    assert not any("/bridge/node/modelling-rule" in c[1] for c in cap)


# -- source-level checks of the C# set_modelling_rule logic ----------------------

def test_set_modelling_rule_route_is_dispatched_and_logged():
    text = _bridge_cs_text()
    route = _cs_region(text, 'StartsWith("POST /bridge/node/modelling-rule")',
                       'StartsWith("POST /bridge/node/property")')
    assert "SetModellingRuleInline(firstLine)" in route
    assert '"POST /bridge/node/modelling-rule"' in _cs_region(
        text, "_MutationRoutes = {", "};")


def test_set_modelling_rule_bridge_validates_before_writing():
    text = _bridge_cs_text()
    body = _cs_region(text, "private string SetModellingRuleInline(",
                      "// Structural check for a translation key")
    # rule checked, then node resolved, then type-membership, then the write
    assert (body.index("CanonModellingRule(ruleIn)") < body.index("ResolveNode(path)")
            < body.index("IsTypeMember(node)") < body.index("node.ModellingRule = "))
    for code in ("invalid_modelling_rule", "node_not_found", "not_a_type_member"):
        assert f'"{code}"' in body, code
    assert "if (!dryRun)" in body and "would_set" in body
    assert "(NamingRuleType)Enum.Parse(typeof(NamingRuleType), rule)" in body
    # a node setting only: never touches variables or attributes
    assert "GetOrCreateVariable" not in body
    assert "BrowseName =" not in body and "DisplayName =" not in body


def test_modelling_rule_names_are_exactly_the_three():
    text = _bridge_cs_text()
    assert '{ "Mandatory", "Optional", "None" }' in text


def test_validate_ops_checks_set_modelling_rule_in_bridge():
    text = _bridge_cs_text()
    case = _cs_region(text, 'case "set_modelling_rule":', 'case "add_translations":')
    for needle in ("invalid_modelling_rule", "CanonModellingRule(mrRule)", "not_a_type_member",
                   "IsTypeMember(mrLive)", "moved_earlier_in_batch", "modifies_deleted_node",
                   "unresolved_reference"):
        assert needle in case, needle


def test_set_modelling_rule_registered_as_gated_author_tool(cfg):
    from service import auth
    from service.mcp_app import make_mcp
    tool = "optix_bridge_set_modelling_rule"
    assert auth.TOOL_SCOPES.get(tool) == "author"
    names = {t.name for t in make_mcp(cfg)._tool_manager.list_tools()}
    assert tool not in names                                 # gated with the primitives


# -- retype to LocalizedText (ftx-mcp-8fy.5) -------------------------------------

@pytest.mark.parametrize("dt", ["LocalizedText", "localizedtext", "LOCALIZEDTEXT", "String", "Float"])
def test_validate_retype_op_accepts_supported_datatypes(dt):
    assert core.validate_retype_op("Model/Caption", dt) == []


def test_normalize_retype_datatype_is_canonical():
    assert core.normalize_retype_datatype("localizedtext") == "LocalizedText"
    assert core.normalize_retype_datatype("INT32") == "Int32"
    for bad in ("Color", "NodeId", "", None, 3, " String"):
        assert core.normalize_retype_datatype(bad) is None


@pytest.mark.parametrize("path,dt,dims,code,field", [
    ("", "LocalizedText", None, "missing_field", "path"),
    (None, "LocalizedText", None, "missing_field", "path"),
    ("Model/X", None, None, "missing_field", "datatype"),
    ("Model/X", "  ", None, "missing_field", "datatype"),
    ("Model/X", "Color", None, "invalid_datatype", "datatype"),
    ("Model/X", "Text", None, "invalid_datatype", "datatype"),
    ("Model/X", "LocalizedText", "1,2", "invalid_dims", "dims"),
    ("Model/X", "LocalizedText", "-3", "invalid_dims", "dims"),
    ("Model/X", "LocalizedText", "abc", "invalid_dims", "dims"),
])
def test_validate_retype_op_structured_errors(path, dt, dims, code, field):
    errs = core.validate_retype_op(path, dt, dims)
    assert [(e["code"], e["field"]) for e in errs] == [(code, field)]
    assert errs[0]["message"]


def test_validate_retype_op_accepts_dims_forms():
    for dims in (None, "", "scalar", "0", "10", 10):
        assert core.validate_retype_op("Model/X", "LocalizedText", dims) == []


def test_invalid_retype_datatype_lists_allowed_values():
    err = core.validate_retype_op("Model/X", "Color")[0]
    assert "LocalizedText" in err["allowed"] and "String" in err["allowed"]


def test_retype_to_localized_text_posts_canonical_params(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/retype": (200, {
        "ok": True, "from": {"datatype": "String", "dims": None},
        "to": {"datatype": "LocalizedText", "dims": None}, "values_converted": 1})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_retype(alpha, "Alpha", "Model/Caption", "localizedtext")
    assert out["ok"] is True and out["to"]["datatype"] == "LocalizedText"
    method, path = next(c for c in cap if "/bridge/node/retype" in c[1])
    assert method == "POST"
    assert "path=Model%2FCaption" in path and "datatype=LocalizedText" in path
    assert "dry_run" not in path and "dims" not in path


def test_retype_dry_run_forwards_flag(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/retype": (200, {"ok": True, "would_retype": True})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_retype(alpha, "Alpha", "Model/Caption", "LocalizedText", dry_run=True)
    assert out["would_retype"] is True
    _m, path = next(c for c in cap if "/bridge/node/retype" in c[1])
    assert "dry_run=true" in path


def test_retype_non_convertible_value_is_a_structured_refusal(alpha, monkeypatch):
    routes = {"/bridge/node/retype": (200, {"error": {
        "code": "value_not_convertible",
        "message": "'Model/Ref' (NodeId) cannot be retyped to LocalizedText: "
                   "its DataType NodeId has no text form",
        "field": "datatype"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_retype(alpha, "Alpha", "Model/Ref", "LocalizedText")
    assert out["ok"] is False and out["code"] == "value_not_convertible"
    assert out["errors"][0]["field"] == "datatype"
    assert "no text form" in out["errors"][0]["message"]
    assert "Nothing was changed" in out["nudge"]


def test_retype_other_bridge_errors_still_raise(alpha, monkeypatch):
    routes = {"/bridge/node/retype": (200, {"error": {
        "code": "not_a_variable", "message": "'UI/Screen1' is a Panel, not a variable"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed, match="not_a_variable"):
        core.bridge_retype(alpha, "Alpha", "UI/Screen1", "LocalizedText")


def test_retype_invalid_datatype_writes_nothing(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    out = core.bridge_retype(alpha, "Alpha", "Model/X", "Color")
    assert out["ok"] is False and out["code"] == "invalid_datatype"
    assert not any("/bridge/node/" in c[1] for c in cap)


def test_apply_one_edit_retype_non_convertible_fails_the_op(alpha, monkeypatch):
    routes = {"/bridge/node/retype": (200, {"error": {
        "code": "value_not_convertible", "message": "no text form"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed, match="value_not_convertible"):
        core._apply_one_edit(alpha, "Alpha", {
            "op": "retype", "path": "Model/Ref", "datatype": "LocalizedText"})


def test_apply_one_edit_retype_to_localized_text(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/retype": (200, {"ok": True})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core._apply_one_edit(alpha, "Alpha", {
        "op": "retype", "path": "Model/Caption", "datatype": "LocalizedText"})
    _m, path = next(c for c in cap if "/bridge/node/retype" in c[1])
    assert "datatype=LocalizedText" in path


def test_dry_run_accepts_retype_to_localized_text(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "retype", "path": "Model/Caption", "datatype": "LocalizedText"})
    assert out["state"] == "validated" and out["report"]["ok"] is True
    assert out["applied"] == 0


def test_dry_run_rejects_invalid_retype_with_structured_error(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "retype", "path": "Model/Caption", "datatype": "Color"})
    assert out["state"] == "validated" and out["report"]["ok"] is False
    errs = out["report"]["errors"]
    assert [(e["op_index"], e["code"], e["field"]) for e in errs] == [
        (0, "invalid_datatype", "datatype")]
    assert out["applied"] == 0


def test_dry_run_surfaces_bridge_value_not_convertible(alpha, monkeypatch):
    """The bridge's validate_ops reports value_not_convertible per op; dry_run passes it on."""
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(
        core, "_bridge_post_body",
        lambda cfg, path, payload, timeout=20.0: (200, {
            "ok": False, "op_count": 1, "warnings": [],
            "errors": [{"op_index": 0, "code": "value_not_convertible",
                        "message": "NodeId has no text form"}]}))
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "retype", "path": "Model/Ref", "datatype": "LocalizedText"}], dry_run=True)
    assert out["report"]["ok"] is False and out["applied"] == 0
    assert [e["code"] for e in out["report"]["errors"]] == ["value_not_convertible"]


def test_apply_batch_with_invalid_retype_applies_nothing(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    monkeypatch.setattr(
        core, "_bridge_post_body",
        lambda cfg, path, payload, timeout=20.0: (
            200, {"ok": True, "op_count": len(payload["ops"]), "errors": [], "warnings": []}))
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "retype", "path": "Model/X", "datatype": "Color"}])
    assert out.get("applied", 0) == 0
    assert not any("/bridge/node/retype" in c[1] for c in cap)


# -- source-level checks of the C# retype-to-LocalizedText logic -----------------

def test_retype_localized_text_is_a_supported_clr_target():
    text = _bridge_cs_text()
    body = _cs_region(text, "private static Type RetypeClrType(", "// Sample text for the retype")
    assert 'case "LocalizedText": return typeof(LocalizedText);' in body


def test_retype_bridge_checks_convertibility_before_writing():
    text = _bridge_cs_text()
    body = _cs_region(text, "private string RetypeVariableInline(",
                      "private static Type RetypeClrType(")
    assert (body.index("LocalizedTextRetypeRefusal(oldDt, oldVal, toArray)")
            < body.index("v.DataType = ResolveDataType(dtName)")
            < body.index("v.Value = new UAValue(newVal)"))
    assert "value_not_convertible" in body and '"not_a_variable"' in body
    # dry_run runs the checks and skips every write
    assert "bool dryRun" in body and "if (!dryRun)" in body and "would_retype" in body
    assert body.index("if (!dryRun)") > body.index("LocalizedTextRetypeRefusal(")


def test_retype_localized_text_scalar_keeps_existing_text_and_locale():
    text = _bridge_cs_text()
    body = _cs_region(text, "private static object RetypeScalar(", "private static NodeId ResolveDataType(")
    lt = body[:body.index('if (dtName == "String")')]
    assert 'dtName == "LocalizedText"' in lt
    assert "return existingLt;" in lt                      # TextId preserved
    assert 'new LocalizedText(text, "en-US")' in lt        # never an empty localeId


def test_retype_localized_text_refusal_rules():
    text = _bridge_cs_text()
    body = _cs_region(text, "private static string LocalizedTextRetypeRefusal(",
                      "private static object RetypeScalar(")
    assert "has no text form" in body and "would drop" in body
    for dt in ("NodeId", "ByteString"):
        assert f'"{dt}"' not in _cs_region(text, "_TextConvertibleDataTypes = {", "};"), dt
    for dt in ("String", "LocalizedText", "Int32", "Float", "Boolean", "DateTime"):
        assert f'"{dt}"' in _cs_region(text, "_TextConvertibleDataTypes = {", "};"), dt


def test_validate_ops_checks_retype_to_localized_text_in_bridge():
    text = _bridge_cs_text()
    case = _cs_region(text, 'case "retype":', 'case "reorder":')
    for needle in ("invalid_datatype", "RetypeClrType(rtDt)", "value_not_convertible",
                   "LocalizedTextRetypeRefusal(rtOldDt, rtOldVal, rtToArray)",
                   "not_a_variable", "moved_earlier_in_batch", "modifies_deleted_node",
                   "unresolved_reference"):
        assert needle in case, needle


def test_retype_registered_as_gated_author_tool(cfg):
    from service import auth
    from service.mcp_app import make_mcp
    tool = "optix_bridge_retype"
    assert auth.TOOL_SCOPES.get(tool) == "author"
    names = {t.name for t in make_mcp(cfg)._tool_manager.list_tools()}
    assert tool not in names                                 # gated with the primitives


# -- create_variable NodePointer Kind (ftx-mcp-8fy.6) ------------------------------

@pytest.mark.parametrize("dt", ["NodePointer", "nodepointer", "NODEPOINTER"])
def test_validate_create_variable_nodepointer_with_target_or_kind_is_clean(dt):
    assert core.validate_create_variable_op("Sel", dt, target="UI/Screens/A") == []
    assert core.validate_create_variable_op("Sel", dt, kind="Button") == []
    assert core.validate_create_variable_op("Sel", dt, "UI/Screens/A", "Button") == []


@pytest.mark.parametrize("target,kind", [(None, None), ("", ""), ("  ", None), (None, " ")])
def test_validate_create_variable_nodepointer_without_kind_source_is_refused(target, kind):
    errs = core.validate_create_variable_op("Sel", "NodePointer", target, kind)
    assert [(e["code"], e["field"]) for e in errs] == [("kind_required", "target")]
    assert "BaseObject" in errs[0]["message"]


def test_validate_create_variable_target_on_other_datatype_is_refused():
    errs = core.validate_create_variable_op("X", "Int32", target="UI/Screens/A")
    assert [(e["code"], e["field"]) for e in errs] == [("kind_not_applicable", "target")]
    errs = core.validate_create_variable_op("X", "String", kind="Button")
    assert [(e["code"], e["field"]) for e in errs] == [("kind_not_applicable", "kind")]
    assert core.validate_create_variable_op("X", "Int32") == []


def test_create_variable_nodepointer_posts_target_and_canonical_datatype(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/variable": (200, {
        "ok": True, "created_path": "Model/Sel", "datatype": "NodePointer",
        "kind": "Screen", "target": "UI/Screens/A"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_variable(
        alpha, "Alpha", "Sel", "Model", "nodepointer", target="UI/Screens/A")
    assert out["kind"] == "Screen"
    method, path = next(c for c in cap if "/bridge/model/variable" in c[1])
    assert method == "POST"
    assert "datatype=NodePointer" in path and "target=UI%2FScreens%2FA" in path
    assert "kind=" not in path


def test_create_variable_nodepointer_forwards_explicit_kind(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/variable": (200, {"ok": True, "created_path": "Model/Sel"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_create_variable(alpha, "Alpha", "Sel", "Model", "NodePointer", kind="Button")
    _m, path = next(c for c in cap if "/bridge/model/variable" in c[1])
    assert "kind=Button" in path and "target=" not in path


def test_create_variable_plain_datatype_sends_no_target_or_kind(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/variable": (200, {"ok": True, "created_path": "Model/F"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_create_variable(alpha, "Alpha", "F")
    _m, path = next(c for c in cap if "/bridge/model/variable" in c[1])
    assert "target=" not in path and "kind=" not in path and "datatype=Boolean" in path


def test_create_variable_nodepointer_without_target_writes_nothing(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    out = core.bridge_create_variable(alpha, "Alpha", "Sel", "Model", "NodePointer")
    assert out["ok"] is False and out["code"] == "kind_required"
    assert not any("/bridge/model/variable" in c[1] for c in cap)


def test_create_variable_bridge_kind_refusal_raises(alpha, monkeypatch):
    routes = {"/bridge/model/variable": (200, {"error": {
        "code": "node_not_found", "message": "no target node at: UI/Nope"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed, match="node_not_found"):
        core.bridge_create_variable(
            alpha, "Alpha", "Sel", "Model", "NodePointer", target="UI/Nope")


def test_apply_one_edit_create_variable_nodepointer_forwards_target(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/variable": (200, {"ok": True, "created_path": "Model/Sel"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core._apply_one_edit(alpha, "Alpha", {
        "op": "create_variable", "parent": "Model", "name": "Sel",
        "datatype": "NodePointer", "target": "UI/Screens/A"})
    _m, path = next(c for c in cap if "/bridge/model/variable" in c[1])
    assert "datatype=NodePointer" in path and "target=UI%2FScreens%2FA" in path


def test_apply_one_edit_create_variable_nodepointer_without_target_fails_the_op(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    with pytest.raises(core.BridgeWriteFailed, match="kind_required"):
        core._apply_one_edit(alpha, "Alpha", {
            "op": "create_variable", "parent": "Model", "name": "Sel",
            "datatype": "NodePointer"})


def test_dry_run_accepts_create_variable_nodepointer_with_target(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "create_variable", "parent": "Model", "name": "Sel",
        "datatype": "NodePointer", "target": "UI/Screens/A"})
    assert out["state"] == "validated" and out["report"]["ok"] is True
    assert out["applied"] == 0


def test_dry_run_rejects_create_variable_nodepointer_without_kind_source(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "create_variable", "parent": "Model", "name": "Sel", "datatype": "NodePointer"})
    assert out["state"] == "validated" and out["report"]["ok"] is False
    assert [(e["op_index"], e["code"]) for e in out["report"]["errors"]] == [
        (0, "kind_required")]
    assert out["applied"] == 0


def test_dry_run_rejects_target_on_non_nodepointer_variable(alpha, monkeypatch):
    out = _dry_run(alpha, monkeypatch, {
        "op": "create_variable", "parent": "Model", "name": "X",
        "datatype": "Int32", "target": "UI/Screens/A"})
    assert out["report"]["ok"] is False
    assert [e["code"] for e in out["report"]["errors"]] == ["kind_not_applicable"]


def test_dry_run_surfaces_bridge_resolved_kind(alpha, monkeypatch):
    """The bridge's validate_ops reports the Kind it resolved; dry_run passes it on."""
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    resolved = [{"op_index": 0, "path": "Model/Sel", "datatype": "NodePointer", "kind": "Screen"}]
    monkeypatch.setattr(
        core, "_bridge_post_body",
        lambda cfg, path, payload, timeout=20.0: (200, {
            "ok": True, "op_count": 1, "errors": [], "warnings": [], "resolved": resolved}))
    out = core.bridge_edit(alpha, "Alpha", [{
        "op": "create_variable", "parent": "Model", "name": "Sel",
        "datatype": "NodePointer", "target": "UI/Screens/A"}], dry_run=True)
    assert out["report"]["resolved"] == resolved


def test_dry_run_surfaces_bridge_unresolved_target(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(
        core, "_bridge_post_body",
        lambda cfg, path, payload, timeout=20.0: (200, {
            "ok": False, "op_count": 1, "warnings": [], "resolved": [],
            "errors": [{"op_index": 0, "code": "node_not_found",
                        "message": "no target node at: UI/Nope"}]}))
    out = core.bridge_edit(alpha, "Alpha", [{
        "op": "create_variable", "parent": "Model", "name": "Sel",
        "datatype": "NodePointer", "target": "UI/Nope"}], dry_run=True)
    assert out["report"]["ok"] is False and out["applied"] == 0


def test_create_variable_tool_exposes_target_and_kind():
    import inspect
    from service import mcp_app
    src = inspect.getsource(mcp_app)
    start = src.index("def optix_bridge_create_variable(")
    sig = src[start:src.index('"""', start)]
    assert "target: str | None = None" in sig and "kind: str | None = None" in sig
    assert "target=target, kind=kind" in src[start:start + 3000]


# -- source-level checks of the C# NodePointer Kind logic ------------------------

def test_bridge_create_variable_makes_typed_nodepointer_with_resolved_kind():
    text = _bridge_cs_text()
    body = _cs_region(text, "private string WriteVariableInline(", "// create_variable accepts")
    assert "IsNodePointerDataType(dtName)" in body
    assert "MakeVariable<FTOptix.Core.NodePointer>" in body
    assert "ResolveNodePointerKind(" in body
    # Kind is set from the resolved id, BEFORE the node is attached to its parent
    assert body.index("np.Kind = ptrKindId") < body.index("parentNode.Add(np)")
    # the pointed-to node becomes the value; the response reports the Kind
    assert "np.Value = ptrTargetNode.NodeId" in body and '\\"kind\\":' in body
    # never a hard-coded BaseObjectType (ns=0;i=58) Kind
    assert "new NodeId(0, 58)" not in body
    # the Kind is resolved (and any refusal returned) before anything is created
    assert body.index("ResolveNodePointerKind(") < body.index("MakeVariable<FTOptix.Core.NodePointer>")


def test_bridge_nodepointer_kind_is_the_pointed_to_nodes_type():
    text = _bridge_cs_text()
    kind_of = _cs_region(text, "private static NodeId KindIdOfNode(", "private bool ResolveNodePointerKind(")
    assert "o.ObjectType.NodeId" in kind_of and "uv.VariableType.NodeId" in kind_of
    assert "NodeClass.ObjectType" in kind_of
    resolver = _cs_region(text, "private bool ResolveNodePointerKind(", "// POST /bridge/node/retype")
    assert "KindIdOfNode(targetNode" in resolver
    # neither target nor kind -> refused, never a silent unset/BaseObject Kind
    assert '"kind_required"' in resolver and '"kind_unresolved"' in resolver
    assert "NodeId(0, 58)" not in resolver


def test_bridge_validate_ops_reports_resolved_nodepointer_kind():
    text = _bridge_cs_text()
    case = _cs_region(text, 'if (verb == "create_variable")', 'case "create_widget":')
    # same resolver as the write path, so dry_run cannot disagree with apply
    assert "ResolveNodePointerKind(" in case and "IsNodePointerDataType(cvDt)" in case
    # a target created earlier in the batch resolves through the hypothetical model
    assert "HypoResolve(cvTarget" in case and "HypoKindName(" in case
    assert '"kind_not_applicable"' in case
    assert '\\"kind\\":' in case
    report = _cs_region(text, 'bool ok = errCount == 0;', "catch (System.Text.Json.JsonException")
    assert '\\"resolved\\":[' in report


# -- expression pow, Studio 1.8 (ftx-mcp-8fy.7) ------------------------------------

_POW_EXPR = "pow({0}, 2) + 1"
_POW_V17_MSG = ("function_requires_version: 'pow' requires Optix Studio 1.8 or later "
                "(this Studio is 1.7.4.32)")
_POW_V17_BODY = {
    "ok": True, "valid": False, "sources": 1, "error": _POW_V17_MSG,
    "requires_version": {"function": "pow", "min_version": "1.8",
                         "studio_version": "1.7.4.32"},
}


def test_validate_expression_pow_valid_on_studio_18(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(
        {"/bridge/expr/validate": (200, {"ok": True, "valid": True, "sources": 1})}, capture=cap))
    out = core.bridge_validate_expression(alpha, "Alpha", _POW_EXPR, sources="Model/X")
    assert out["valid"] is True and "error" not in out
    _m, path = next(c for c in cap if "/bridge/expr/validate" in c[1])
    assert "expression=pow" in path


def test_validate_expression_pow_on_studio_17_is_a_structured_error(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(
        {"/bridge/expr/validate": (200, _POW_V17_BODY)}))
    out = core.bridge_validate_expression(alpha, "Alpha", _POW_EXPR, sources="Model/X")
    assert out["valid"] is False
    assert out["error"].startswith("function_requires_version:")
    assert "requires Optix Studio 1.8" in out["error"]
    assert out["requires_version"] == {
        "function": "pow", "min_version": "1.8", "studio_version": "1.7.4.32"}


def test_attach_expression_pow_on_studio_17_refused_with_clear_message(alpha, monkeypatch):
    body = {"error": {"code": "bad_expression", "message": _POW_V17_MSG,
                      "requires_version": _POW_V17_BODY["requires_version"]}}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(
        {"/bridge/node/attach-expression": (200, body)}))
    with pytest.raises(core.BridgeWriteFailed) as exc:
        core.bridge_attach_expression(
            alpha, "Alpha", "UI/MainWindow/L1", "Text", _POW_EXPR, sources="Model/X")
    msg = str(exc.value)
    assert "bad_expression" in msg and "pow" in msg and "1.8" in msg


def test_attach_expression_pow_on_studio_18_posts_the_expression(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(
        {"/bridge/node/attach-expression": (200, {"ok": True, "path": "UI/MainWindow/L1"})},
        capture=cap))
    out = core.bridge_attach_expression(
        alpha, "Alpha", "UI/MainWindow/L1", "Text", _POW_EXPR, sources="Model/X")
    assert out.get("ok") is True
    _m, path = next(c for c in cap if "/bridge/node/attach-expression" in c[1])
    assert "expression=pow" in path


def test_validate_expression_tool_docs_mention_pow_needs_18():
    import inspect
    from service import mcp_app
    src = inspect.getsource(mcp_app)
    for name in ("def optix_bridge_attach_expression(", "def optix_bridge_validate_expression("):
        start = src.index(name)
        doc = src[start:src.index('"""', src.index('"""', start) + 3)]
        assert "pow" in doc and "1.8" in doc, name


# -- source-level checks of the C# pow gating --------------------------------------

def test_bridge_pow_is_a_version_gated_expression_function():
    text = _bridge_cs_text()
    # pow is NOT in the always-present set; it lives in the version table at 1.8
    base = _cs_region(text, "ExprFunctions =", "ExprFunctionMinVersion")
    assert '"pow"' not in base
    table = _cs_region(text, "ExprFunctionMinVersion =", "private static string _studioVersionCache")
    assert '{ "pow", "1.8" }' in table


def test_bridge_validate_expression_syntax_gates_pow_on_studio_version():
    text = _bridge_cs_text()
    body = _cs_region(text, "private static string ValidateExpressionSyntax(",
                      "// Class 1 (LiteralLintTable) helper")
    assert "ExprFunctionMinVersion.ContainsKey(w)" in body
    assert "ExprFunctionVersionErr(w, StudioVersionString())" in body
    # the version check runs before the generic unknown-function error
    assert body.index("ExprFunctionVersionErr(") < body.index('return "unknown function')
    helper = _cs_region(text, "private static string ExprFunctionVersionErr(",
                        "// LiteralLintTable --")
    assert "function_requires_version: '" in helper and "requires Optix Studio" in helper
    assert "VersionAtLeast(studioVersion, min)" in helper


def test_bridge_version_compare_never_refuses_when_version_unknown():
    text = _bridge_cs_text()
    cmp_ = _cs_region(text, "private static bool VersionAtLeast(", "private static string NormalizeVersion(")
    assert cmp_.count("return true;") >= 2          # unparseable -> do not block
    assert "have.Major > need.Major" in cmp_ and "have.Minor >= need.Minor" in cmp_


def test_bridge_requires_version_is_structured_in_validate_and_error_bodies():
    text = _bridge_cs_text()
    extra = _cs_region(text, "private static string RequiresVersionExtra(", "private static string Bool(")
    for field in ("requires_version", "function", "min_version", "studio_version"):
        assert f'\\"{field}\\"' in extra, field
    validate = _cs_region(text, "private string ValidateExprJson(", "// Diagnostic: reflect a CLR type")
    assert "RequiresVersionExtra(err)" in validate
    err_json = _cs_region(text, "private static string ErrorJson(", "// 1.0.8: ValidateExpressionSyntax")
    assert "RequiresVersionExtra(message)" in err_json
    # attach_expression's gate surfaces it as bad_expression through ErrorJson
    assert 'ErrorJson("bad_expression", exprErr)' in text


def test_bridge_studio_version_is_the_process_product_version_not_the_ui_assembly():
    text = _bridge_cs_text()
    fn = _cs_region(text, "private static string StudioVersionString()",
                    "private static string CleanProductVersion(")
    # ftx-mcp-mnk.1: the Studio process version resource, not FTOptix.UI's 21.2.0.98
    assert "Process.GetCurrentProcess().MainModule.FileVersionInfo" in fn
    assert "ProductVersion" in fn
    assert "FTOptix.UI" not in fn and "Assembly" not in fn
    assert 'string version = "unknown"' in fn


def test_bridge_clean_product_version_keeps_leading_numeric_version():
    text = _bridge_cs_text()
    fn = _cs_region(text, "private static string CleanProductVersion(",
                    "// True iff `version`")
    assert "char.IsDigit(raw[cut])" in fn and "raw[cut] == '.'" in fn


def test_bridge_health_reports_studio_version():
    text = _bridge_cs_text()
    health = _cs_region(text, "private string HealthJson()", "private string NodeJson")
    assert '\\"studio_version\\"' in health and "StudioVersionString()" in health


# -- create_variable variable_type / array_dimensions / properties (1.0.9 5b, ftx-mcp-090.2) --

def _var_path(cap):
    return next(c for c in cap if "/bridge/model/variable" in c[1])[1]


def test_create_variable_station_tag_array_posts_fields(alpha, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    cap: list = []
    routes = {"/bridge/model/variable": (200, {
        "ok": True, "created_path": "Model/Tags", "datatype": "Int32",
        "variable_type": "RAEtherNetIPTag", "array_dimensions": [4],
        "properties_set": ["SymbolName"]})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_variable(
        alpha, "Alpha", "Tags", "Model", "Int32",
        variable_type="RAEtherNetIPTag", array_dimensions=4,
        properties={"SymbolName": "MyTags", "Enabled": True})
    assert out["variable_type"] == "RAEtherNetIPTag"
    q = parse_qs(urlsplit(_var_path(cap)).query)
    assert q["variable_type"] == ["RAEtherNetIPTag"]
    assert q["array_dimensions"] == ["4"]
    assert json.loads(q["properties"][0]) == {"SymbolName": "MyTags", "Enabled": "true"}
    assert "dry_run" not in q


def test_create_variable_plain_sends_no_station_tag_fields(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/variable": (200, {"ok": True, "created_path": "Model/F"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_create_variable(alpha, "Alpha", "F")
    p = _var_path(cap)
    assert "variable_type=" not in p and "array_dimensions=" not in p
    assert "properties=" not in p and "dry_run=" not in p


@pytest.mark.parametrize("dims", [4, "4", [4], "[4]", (4,)])
def test_create_variable_array_dimensions_forms_normalise(alpha, monkeypatch, dims):
    cap: list = []
    routes = {"/bridge/model/variable": (200, {"ok": True, "created_path": "Model/A"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_create_variable(alpha, "Alpha", "A", "Model", "Int32", array_dimensions=dims)
    assert "array_dimensions=4" in _var_path(cap)


@pytest.mark.parametrize("dims", [0, -1, "abc", [2, 3], [], True, 1.5, "0", {}])
def test_create_variable_invalid_array_dimensions_structured_error(alpha, monkeypatch, dims):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    out = core.bridge_create_variable(alpha, "Alpha", "A", "Model", "Int32",
                                      array_dimensions=dims)
    assert out["ok"] is False and out["code"] == "invalid_array_dimensions"
    assert out["errors"][0]["field"] == "array_dimensions"
    assert not any("/bridge/model/variable" in c[1] for c in cap)


@pytest.mark.parametrize("vt,code", [
    ("Not A Type!", "unknown_variable_type"),
    ("Bad Path/X", "unknown_variable_type"),
    ("", "invalid_variable_type"),
    (5, "invalid_variable_type"),
])
def test_create_variable_invalid_variable_type_structured_error(alpha, monkeypatch, vt, code):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    out = core.bridge_create_variable(alpha, "Alpha", "T", "Model", "Int32",
                                      variable_type=vt)
    assert out["ok"] is False and out["code"] == code
    assert out["errors"][0]["field"] == "variable_type"
    assert not any("/bridge/model/variable" in c[1] for c in cap)


def test_validate_create_variable_properties_errors():
    v = core.validate_create_variable_op
    assert v("T", "Int32", variable_type="RAEtherNetIPTag",
             properties={"SymbolName": "x", "N": 3, "B": False}) == []
    assert [e["code"] for e in v("T", "Int32", properties=["SymbolName"])] == ["invalid_properties"]
    errs = v("T", "Int32", properties={"Value": 1, "bad name": "x", "L": [1], "N": None})
    assert [e["code"] for e in errs] == [
        "invalid_property_name", "invalid_property_name",
        "invalid_property_value", "invalid_property_value"]
    assert errs[0]["field"] == "properties.Value"


def test_validate_create_variable_station_fields_on_nodepointer_refused():
    errs = core.validate_create_variable_op(
        "P", "NodePointer", target="UI/A", variable_type="RAEtherNetIPTag",
        array_dimensions=3)
    assert [e["code"] for e in errs] == [
        "variable_type_not_applicable", "array_dimensions_not_applicable"]


def test_create_variable_dry_run_validates_then_asks_bridge(alpha, monkeypatch):
    out = core.bridge_create_variable(alpha, "Alpha", "T", "Model", "Int32",
                                      array_dimensions=0, dry_run=True)
    assert out["ok"] is False and out["code"] == "invalid_array_dimensions"
    cap: list = []
    routes = {"/bridge/model/variable": (200, {
        "ok": True, "dry_run": True, "would_create": True,
        "created_path": "Model/T", "variable_type": "RAEtherNetIPTag"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_variable(
        alpha, "Alpha", "T", "Model", "Int32", variable_type="RAEtherNetIPTag",
        array_dimensions=2, properties={"SymbolName": "S"}, dry_run=True)
    assert out["dry_run"] is True and out["would_create"] is True
    assert "dry_run=true" in _var_path(cap)


@pytest.mark.parametrize("code,field", [
    ("unknown_variable_type", "variable_type"),
    ("unknown_property", "properties"),
])
def test_create_variable_bridge_refusal_becomes_structured_error(alpha, monkeypatch, code, field):
    routes = {"/bridge/model/variable": (200, {"error": {
        "code": code, "message": "no such thing in this Studio"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_create_variable(
        alpha, "Alpha", "T", "Model", "Int32", variable_type="NoSuchTag",
        properties={"SymbolName": "S"})
    assert out["ok"] is False and out["code"] == code
    assert out["errors"][0]["field"] == field
    assert "no such thing" in out["errors"][0]["message"]


def test_apply_one_edit_create_variable_forwards_station_fields(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/variable": (200, {"ok": True, "created_path": "Model/T"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core._apply_one_edit(alpha, "Alpha", {
        "op": "create_variable", "parent": "Model", "name": "T", "datatype": "Int32",
        "variable_type": "RAEtherNetIPTag", "array_dimensions": 3,
        "properties": {"SymbolName": "S"}})
    p = _var_path(cap)
    assert "variable_type=RAEtherNetIPTag" in p and "array_dimensions=3" in p
    assert "properties=" in p


def test_apply_one_edit_create_variable_bad_dims_fails_the_op(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    with pytest.raises(core.BridgeWriteFailed, match="invalid_array_dimensions"):
        core._apply_one_edit(alpha, "Alpha", {
            "op": "create_variable", "parent": "Model", "name": "T",
            "datatype": "Int32", "array_dimensions": "x"})


def test_create_variable_station_tag_bridge_source():
    """The C# resolves the VariableType, shapes the array and sets declared
    properties before parent.Add (source-level; the bridge is not compiled in CI)."""
    from pathlib import Path
    cs = (Path(__file__).parent.parent.parent / "studio-bridge"
          / "StudioMCPBridge.cs").read_text(encoding="utf-8")
    start = cs.index("private string WriteVariableInline")
    body = cs[start:cs.index("private static bool IsNodePointerDataType")]
    for needle in ('"variable_type"', '"array_dimensions"', '"properties"',
                   "PrepareVariableExtras", "ArrayDimensions", "dry_run"):
        assert needle in body, needle
    assert body.index("PrepareVariableExtras") < body.index("InformationModel.MakeVariable(name, ResolveDataType(dtName), vtId)")
    assert body.rindex("parentNode.Add(v)") > body.index("v.Add(pchild)")
    assert "unknown_variable_type" in cs and "unknown_property" in cs


# ---- ftx-mcp-vvk.1: typed driver tag resolution ------------------------------

def _bridge_source() -> str:
    from pathlib import Path
    return (Path(__file__).parent.parent.parent / "studio-bridge"
            / "StudioMCPBridge.cs").read_text(encoding="utf-8")


def _cs_body(cs: str, start_marker: str, end_marker: str) -> str:
    start = cs.index(start_marker)
    return cs[start:cs.index(end_marker, start)]


_RESOLVE_START = "private static IUANode ResolveVariableTypeNodeEx"
_RESOLVE_END = "private static IUANode ResolveVariableTypeByPath"


@pytest.mark.parametrize("vt", [
    "RAEtherNetIPTag",                                   # short name
    "FTOptix.RAEtherNetIP.RAEtherNetIPTag",              # Module.Type
    "BaseVariableType/BaseDataVariableType/RAEtherNetIPTag",  # type-node path
])
def test_variable_type_forms_pass_service_validation(vt):
    """name, Module.Type and type-path spellings all reach the bridge."""
    assert core.validate_create_variable_op(
        "T", "Boolean", variable_type=vt,
        properties={"SymbolName": "S"}) == []


def test_variable_type_resolves_by_short_name_source():
    body = _cs_body(_bridge_source(), _RESOLVE_START, _RESOLVE_END)
    assert "FindAllVariableTypesByBrowseName(searchRoot, name, hits)" in body
    assert "hits.Count == 1" in body and "return hits[0]" in body


def test_variable_type_resolves_by_module_type_source():
    cs = _bridge_source()
    body = _cs_body(cs, _RESOLVE_START, _RESOLVE_END)
    assert "name.LastIndexOf('.')" in body
    assert "ResolveModuleVariableTypeId(name.Substring(0, dot), name.Substring(dot + 1))" in body
    cat = _cs_body(cs, "private static List<KeyValuePair<string, FieldInfo>> EnumerateModuleVariableTypes",
                   "private static string VariableTypeLabel")
    assert 't.Name != "VariableTypes"' in cat and 'StartsWith("FTOptix.")' in cat


def test_variable_type_resolves_by_type_node_path_source():
    cs = _bridge_source()
    body = _cs_body(cs, _RESOLVE_START, _RESOLVE_END)
    assert "name.IndexOf('/') >= 0" in body
    assert "ResolveVariableTypeByPath(name, root)" in body
    walk = _cs_body(cs, _RESOLVE_END, "private static NodeId ResolveModuleVariableTypeId")
    assert "NodeClass.VariableType" in walk and "kid.BrowseName == segs[i]" in walk


def test_ambiguous_variable_type_refused_source():
    """A short name hit in >1 module is refused, never silently picked, in both
    the write path and the batch validator (both go through PrepareVariableExtras)."""
    cs = _bridge_source()
    body = _cs_body(cs, "private string PrepareVariableExtras", "// POST /bridge/model/type?name=")
    assert '"ambiguous_variable_type"' in body
    assert "candidates: " in body and "ResolveVariableTypeNodeEx(vtName, out vtAmbiguous)" in body
    assert body.index("ambiguous_variable_type") < body.index('"unknown_variable_type"')
    res = _cs_body(cs, _RESOLVE_START, _RESOLVE_END)
    assert "ambiguous = hits.Select(" in res and "ambiguous.Count > 1 ? null" in res


def test_typed_tag_made_with_makevariable_and_symbolname_set_source():
    cs = _bridge_source()
    body = _cs_body(cs, "private string WriteVariableInline", "private static bool IsNodePointerDataType")
    assert "InformationModel.MakeVariable(name, ResolveDataType(dtName), vtId)" in body
    assert "CoerceAssign(pchild, extraProps[pi].Value" in body
    assert body.index("CoerceAssign(pchild") < body.rindex("parentNode.Add(v)")


def test_describe_node_reports_variable_type_source():
    cs = _bridge_source()
    assert "LinkTargetJson(sv) + VariableTypeJson(sv)" in cs
    assert "private static string VariableTypeJson" in cs


def test_ambiguous_variable_type_becomes_structured_error_with_candidates(alpha, monkeypatch):
    msg = ("variable_type 'RAEtherNetIPTag' is ambiguous: several modules define a "
           "VariableType with that name; use Module.Type or a type path. candidates: "
           "FTOptix.RAEtherNetIP.RAEtherNetIPTag, FTOptix.Other.RAEtherNetIPTag; "
           "nothing was created")
    routes = {"/bridge/model/variable": (200, {"error": {
        "code": "ambiguous_variable_type", "message": msg}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_create_variable(
        alpha, "Alpha", "T", "Model", "Boolean", variable_type="RAEtherNetIPTag",
        properties={"SymbolName": "S"})
    assert out["ok"] is False and out["code"] == "ambiguous_variable_type"
    err = out["errors"][0]
    assert err["field"] == "variable_type"
    assert err["candidates"] == ["FTOptix.RAEtherNetIP.RAEtherNetIPTag",
                                 "FTOptix.Other.RAEtherNetIPTag"]


# ---- ftx-mcp-vvk.2: create_object refuses a VariableType ---------------------

def test_create_object_validator_refuses_variable_type():
    errs = core.validate_create_object_op("T", "RAEtherNetIPTag")
    assert [e["code"] for e in errs] == ["variable_type_not_object"]
    assert errs[0]["field"] == "object_type"
    assert errs[0]["hint"] == 'use create_variable with variable_type: "RAEtherNetIPTag"'


@pytest.mark.parametrize("t", [
    "FTOptix.RAEtherNetIP.RAEtherNetIPTag",
    "BaseVariableType/BaseDataVariableType/RAEtherNetIPTag",
])
def test_create_object_validator_refuses_variable_type_spellings(t):
    errs = core.validate_create_object_op("T", t)
    assert errs and errs[0]["code"] == "variable_type_not_object"
    assert f'variable_type: "{t}"' in errs[0]["hint"]


def test_create_object_validator_live_resolver_and_clean_cases():
    assert core.validate_create_object_op("T", "Vendor.CustomTagType",
                                          is_variable_type=lambda t: True)
    assert core.validate_create_object_op("T", None) == []
    assert core.validate_create_object_op("T", "UI/Templates/PumpCard") == []
    assert core.validate_create_object_op("T", "FTOptix.Alarm.DigitalAlarm") == []


def test_create_object_dry_run_batch_refuses_variable_type(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    ops = [{"op": "create_object", "parent": "Model", "name": "T",
            "object_type": "RAEtherNetIPTag"}]
    out = core.bridge_edit(alpha, "Alpha", ops, dry_run=True)
    assert out["report"]["ok"] is False
    errs = [e for e in out["report"]["errors"] if e["code"] == "variable_type_not_object"]
    assert errs and errs[0]["op_index"] == 0
    assert 'create_variable with variable_type: "RAEtherNetIPTag"' in errs[0]["hint"]


def test_create_object_direct_route_refuses_before_any_write(alpha, monkeypatch):
    capture: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=capture))
    out = core.bridge_create_object(alpha, "Alpha", "Model", "T", "RAEtherNetIPTag")
    assert out["ok"] is False and out["code"] == "variable_type_not_object"
    assert 'create_variable with variable_type: "RAEtherNetIPTag"' in out["hint"]
    assert not any("/bridge/model/object" in path for _m, path in capture)


def test_create_object_bridge_refusal_becomes_structured_error(alpha, monkeypatch):
    routes = {"/bridge/model/object": (200, {"error": {
        "code": "variable_type_not_object", "message": "variable_type_not_object: x"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_create_object(alpha, "Alpha", "Model", "T", "Vendor.OtherTag")
    assert out["ok"] is False and out["code"] == "variable_type_not_object"


def test_create_object_variable_type_refusal_bridge_source():
    cs = _bridge_source()
    body = _cs_body(cs, "private string CreateObjectInline",
                    "o = InformationModel.MakeObject(name, typeId)")
    assert "VariableTypeNotObjectMessage(typePath)" in body
    assert '"variable_type_not_object"' in body
    # ftx-mcp-aaw.6: the dry-run validator must NOT repeat the service's error.
    assert 'addErr(idx, "variable_type_not_object"' not in cs


def test_create_object_variable_type_error_reported_once(alpha, monkeypatch):
    """The service validator is the single source of variable_type_not_object."""
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    ops = [{"op": "create_object", "parent": "Model", "name": "T",
            "object_type": "RAEtherNetIPTag"}]
    out = core.bridge_edit(alpha, "Alpha", ops, dry_run=True)
    errs = [e for e in out["report"]["errors"] if e["code"] == "variable_type_not_object"]
    assert len(errs) == 1, errs


# ---- 1.0.9 5c: bind raw_path with [i] array-element index -------------------

def test_bind_raw_path_array_element_posts_literal_nodepath(alpha, monkeypatch):
    """raw_path with an [i] index reaches the bridge verbatim as `raw=`."""
    cap: list = []
    routes = {"/bridge/node/bind": (200, {"ok": True,
              "path": "UI/MainWindow/L1/Text", "raw": "Model/Tags[1]",
              "mode": "Read", "via": "dynamiclink-raw"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_bind_property(alpha, "Alpha", "UI/MainWindow/L1", "Text",
                                    raw_path="Model/Tags[1]")
    assert out["via"] == "dynamiclink-raw"
    _, path = next(c for c in cap if "/bridge/node/bind" in c[1])
    assert "raw=Model%2FTags%5B1%5D" in path and "source=" not in path


@pytest.mark.parametrize("raw", [
    "Model/Tags[0]", "Model/Tags[12]", "{Alias1}/Tags[1]",
    "../../Alias1/Tags[3]", "Model/Sub/Arr[7]", "{Alias1}/MyInt", "Model/V"])
def test_validate_raw_path_accepts(raw):
    assert core.validate_raw_path(raw) == []


@pytest.mark.parametrize("raw,code", [
    ("Model/Tags[x]", "invalid_raw_path_index"),
    ("Model/Tags[-1]", "invalid_raw_path_index"),
    ("Model/Tags[]", "invalid_raw_path_index"),
    ("Model/Tags[1.5]", "invalid_raw_path_index"),
    ("Model/Tags[1][2]", "invalid_raw_path_index"),
    ("Model/Tags[99999999999]", "invalid_raw_path_index"),
    ("Model/Tags[1", "invalid_raw_path"),
    ("Model/Tags1]", "invalid_raw_path"),
    ("Model/[1]", "invalid_raw_path"),
    ("../[1]", "invalid_raw_path"),
    ("Model//Tags", "invalid_raw_path"),
    ("/Model/Tags", "invalid_raw_path"),
    ("{Alias1/MyInt", "invalid_raw_path"),
    (" Model/Tags[1]", "invalid_raw_path"),
    ("   ", "invalid_raw_path"),
])
def test_validate_raw_path_rejects_malformed(raw, code):
    errs = core.validate_raw_path(raw)
    assert errs and errs[0]["code"] == code and errs[0]["field"] == "raw_path", errs


def test_bind_raw_path_malformed_index_is_structured_error_and_writes_nothing(
        alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    out = core.bridge_bind_property(alpha, "Alpha", "UI/X", "Text",
                                    raw_path="Model/Tags[abc]")
    assert out["ok"] is False and out["code"] == "invalid_raw_path_index"
    assert out["errors"][0]["field"] == "raw_path"
    assert not cap          # the bridge was never contacted
    out = core.bridge_bind_property(alpha, "Alpha", "UI/X", "Text",
                                    raw_path="Model//Tags")
    assert out["ok"] is False and out["code"] == "invalid_raw_path"
    assert not cap


def test_bind_raw_path_dry_run_returns_plan_without_bridge(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    out = core.bridge_bind_property(alpha, "Alpha", "UI/X", "Text",
                                    raw_path="Model/Tags[1]", dry_run=True)
    assert out["ok"] is True and out["dry_run"] is True and out["would_bind"] is True
    assert out["plan"]["raw"] == "Model/Tags[1]"
    assert out["plan"]["via"] == "dynamiclink-raw"
    assert not cap
    bad = core.bridge_bind_property(alpha, "Alpha", "UI/X", "Text",
                                    raw_path="Model/Tags[-1]", dry_run=True)
    assert bad["ok"] is False and bad["code"] == "invalid_raw_path_index"


def test_bridge_edit_dry_run_rejects_bad_bind_raw_path(alpha, monkeypatch):
    """A malformed raw_path in a batch bind op is a per-op validation error
    (dry_run and apply alike); a valid [i] path passes validation."""
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0: (
                            200, {"ok": True, "op_count": len(payload["ops"]),
                                  "errors": [], "warnings": []}))
    bad = core.bridge_edit(alpha, "Alpha", [{
        "op": "bind", "path": "UI/X", "name": "Text",
        "raw_path": "Model/Tags[oops]"}], dry_run=True)
    assert bad["applied"] == 0 and bad["state"] == "validated", bad
    assert [e["code"] for e in bad["report"]["errors"]] == ["invalid_raw_path_index"], bad
    ok = core.bridge_edit(alpha, "Alpha", [{
        "op": "bind", "path": "UI/X", "name": "Text",
        "raw_path": "Model/Tags[1]"}], dry_run=True)
    assert ok["report"]["errors"] == [] and ok["state"] == "validated", ok


def _patch_validate_clean(monkeypatch):
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0: (
                            200, {"ok": True, "op_count": len(payload["ops"]),
                                  "errors": [], "warnings": []}))


_NODE_MISSING = (404, {"error": {"code": "node_not_found", "message": "no node"}})


def test_bridge_edit_dry_run_rejects_unresolvable_bind_source(alpha, monkeypatch):
    """create_widget + bind with a bad source_path: dry_run returns a structured
    source_not_found error on the bind op, and nothing is applied."""
    routes = {
        "/bridge/nodes?path=Model/Missing": _NODE_MISSING,
        "/bridge/nodes?path=Model/Real": (200, {"path": "Model/Real", "node_class": "Variable"}),
    }
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    _patch_validate_clean(monkeypatch)
    ops = [
        {"op": "create_widget", "screen": "UI/Screens/S", "name": "L1", "widget_type": "Label"},
        {"op": "bind", "path": "UI/Screens/S/L1", "name": "Text",
         "source_path": "Model/Missing"},
    ]
    out = core.bridge_edit(alpha, "Alpha", ops, dry_run=True)
    assert out["state"] == "validated" and out["applied"] == 0, out
    assert out["report"]["ok"] is False
    errs = out["report"]["errors"]
    assert [(e["op_index"], e["code"], e["field"]) for e in errs] == [
        (1, "source_not_found", "source_path")], errs
    assert errs[0]["source_path"] == "Model/Missing"
    # A resolvable live source on the same batch shape is clean.
    ops[1]["source_path"] = "Model/Real"
    ok = core.bridge_edit(alpha, "Alpha", ops, dry_run=True)
    assert ok["report"]["ok"] is True and ok["report"]["errors"] == [], ok


def test_bridge_edit_dry_run_resolves_in_batch_bind_source(alpha, monkeypatch):
    """A bind source created earlier in the same batch resolves in dry_run even
    though the live model has no such node; a non-variable or later-created
    source does not."""
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    _patch_validate_clean(monkeypatch)
    bind = {"op": "bind", "path": "UI/Screens/S/L1", "name": "Text",
            "source_path": "Model/NewVar"}
    create_var = {"op": "create_variable", "parent": "Model", "name": "NewVar",
                  "datatype": "String"}
    create_lbl = {"op": "create_widget", "screen": "UI/Screens/S", "name": "L1",
                  "widget_type": "Label"}
    ok = core.bridge_edit(alpha, "Alpha", [create_var, create_lbl, bind], dry_run=True)
    assert ok["report"]["ok"] is True and ok["report"]["errors"] == [], ok
    assert not [c for c in cap if "Model/NewVar" in c[1]]  # resolved in-batch, no live call
    # Reverse order: the source does not exist yet when the bind runs.
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(
        {"/bridge/nodes?path=Model/NewVar": _NODE_MISSING}))
    late = core.bridge_edit(alpha, "Alpha", [create_lbl, bind, create_var], dry_run=True)
    assert [e["code"] for e in late["report"]["errors"]] == ["source_not_found"], late
    # In-batch source that is a widget, not a variable.
    notvar = core.bridge_edit(alpha, "Alpha", [create_lbl, {**bind, "source_path": "UI/Screens/S/L1"}],
                              dry_run=True)
    assert [e["code"] for e in notvar["report"]["errors"]] == ["source_not_variable"], notvar


# -- create_variable datatype validation (ftx-mcp-xk7.3) ---------------------------

def _bridge_resolve_datatype_names() -> list[str]:
    import re
    body = _cs_region(_bridge_cs_text(), "private static NodeId ResolveDataType(",
                      "// Shared duplicate-sibling refusal")
    return re.findall(r'case "(\w+)":', body)


def test_create_variable_datatypes_match_bridge_source():
    """The Python list is the bridge's ResolveDataType case list (drift guard)."""
    bridge = _bridge_resolve_datatype_names()
    assert len(bridge) == 18
    assert list(core.CREATE_VARIABLE_DATATYPES) == bridge


def test_create_variable_datatypes_match_bridge_error_message():
    import re
    body = _cs_region(_bridge_cs_text(), "unsupported datatype '", "// Shared duplicate-sibling")
    tail = body.split("expected one of:", 1)[1]
    names = re.findall(r"[A-Za-z0-9]+", tail.replace('" +', "").replace('"', ""))
    assert sorted(names) == sorted(core.CREATE_VARIABLE_DATATYPES)


@pytest.mark.parametrize("dt", _bridge_resolve_datatype_names() + ["NodePointer"])
def test_validate_create_variable_accepts_every_bridge_datatype(dt):
    kw = {"kind": "Button"} if dt == "NodePointer" else {}
    assert core.validate_create_variable_op("V", dt, **kw) == []


@pytest.mark.parametrize("dt", ["Color2", "int32", "Bool", "", "Variant", "Struct", 5])
def test_validate_create_variable_rejects_unsupported_datatype(dt):
    errs = core.validate_create_variable_op("V", dt)
    assert [(e["code"], e["field"]) for e in errs] == [("invalid_datatype", "datatype")]
    assert "Int32" in errs[0]["allowed"] and "NodePointer" in errs[0]["allowed"]


def test_validate_create_variable_default_datatype_is_clean():
    assert core.validate_create_variable_op("V", None) == []


def test_dry_run_rejects_create_variable_unsupported_datatype(alpha, monkeypatch):
    applied: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(_OK_REPORT))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op["op"]))
    ops = [{"op": "create_variable", "parent": "Model", "name": "V", "datatype": "Bogus"}]
    out = core.bridge_edit(alpha, "Alpha", ops, dry_run=True)
    assert applied == [] and out["report"]["ok"] is False, out
    err = out["report"]["errors"][0]
    assert (err["code"], err["field"], err["op_index"]) == ("invalid_datatype", "datatype", 0)

    ok_ops = [{"op": "create_variable", "parent": "Model", "name": f"V{i}", "datatype": dt}
              for i, dt in enumerate(core.CREATE_VARIABLE_DATATYPES)]
    out = core.bridge_edit(alpha, "Alpha", ok_ops, dry_run=True)
    assert out["report"]["ok"] is True, out


# --- #34: create_netlogic must not blindly re-send the POST after a timeout ---

_NL_PATH = "/bridge/model/netlogic"


def _timeout_then_routes(routes, calls):
    """Fake _bridge_http: the netlogic POST times out like the real helper does
    for retry_ambiguous=False (BridgeWriteTimeout); other paths use `routes`."""
    base = _fake_bridge(routes)

    def fake(cfg, path, method="GET", timeout=5.0, **kw):
        calls.append((method, path, timeout, kw))
        if path.startswith(_NL_PATH):
            raise core.BridgeWriteTimeout("timed out")
        return base(cfg, path, method=method, timeout=timeout, **kw)
    return fake


def test_create_netlogic_timeout_verifies_node_and_succeeds(alpha, monkeypatch):
    calls: list = []
    routes = {"/bridge/nodes": (200, {"path": "Model/Slow", "browse_name": "Slow",
                                      "node_class": "Object", "children": [],
                                      "properties": []})}
    monkeypatch.setattr(core, "_bridge_http", _timeout_then_routes(routes, calls))
    out = core.bridge_create_netlogic(alpha, "Alpha", "Model", "Slow")
    assert out["ok"] is True and out["created_path"] == "Model/Slow"
    assert out["verified_after_timeout"] is True
    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 1  # never re-sent
    assert posts[0][2] >= 30  # long timeout for the compile


def test_create_netlogic_timeout_node_missing_raises(alpha, monkeypatch):
    monkeypatch.setattr(core, "_NETLOGIC_VERIFY_DELAY", 0)
    calls: list = []
    monkeypatch.setattr(core, "_bridge_http", _timeout_then_routes({}, calls))
    with pytest.raises(core.BridgeWriteTimeout):
        core.bridge_create_netlogic(alpha, "Alpha", "Model", "Slow")
    assert len([c for c in calls if c[0] == "POST"]) == 1


def test_create_netlogic_genuine_name_exists_still_error(alpha, monkeypatch):
    routes = {_NL_PATH: (200, {"error": {"code": "name_exists", "message": "dup"}}),
              "/bridge/nodes": (200, {"path": "Model/Dup", "children": [],
                                      "properties": []})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_create_netlogic(alpha, "Alpha", "Model", "Dup")
    assert "name_exists" in str(e.value)


def _urlopen_timeouts(monkeypatch, counter):
    import urllib.request

    def boom(req, timeout=None):
        counter.append(req.full_url)
        raise TimeoutError("timed out")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(core.time, "sleep", lambda *_: None)


def test_bridge_http_non_idempotent_post_timeout_not_retried(alpha, monkeypatch):
    hits: list = []
    _urlopen_timeouts(monkeypatch, hits)
    with pytest.raises(core.BridgeWriteTimeout):
        core._bridge_http(alpha, "/x", method="POST", timeout=1, retries=3,
                          retry_ambiguous=False)
    assert len(hits) == 1


def test_bridge_http_default_post_still_retries_timeouts(alpha, monkeypatch):
    hits: list = []
    _urlopen_timeouts(monkeypatch, hits)
    with pytest.raises(core.BridgeUnavailable):
        core._bridge_http(alpha, "/x", method="POST", timeout=1, retries=3)
    assert len(hits) == 4


# -- add_translations updates existing keys in place (ftx-mcp-aqs.3, #38) --------

def _bridge_method_body(src: str, signature: str) -> str:
    """Return the C# text of the method starting at ``signature`` (brace-matched)."""
    start = src.index(signature)
    open_i = src.index("{", start)
    depth = 0
    for i in range(open_i, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


def _bridge_src() -> str:
    from pathlib import Path
    return (Path(__file__).parent.parent.parent / "studio-bridge" / "StudioMCPBridge.cs"
            ).read_text(encoding="utf-8")


class _FakeTranslationTable:
    """In-memory stand-in for the bridge's translation table (SDK semantics:
    AddTranslation throws on an existing key; SetTranslation updates it).
    ``fail_keys`` make a row error, like the SDK rejecting that one row."""

    def __init__(self, existing=(), fail_keys=()):
        self.rows = {k: "old" for k in existing}
        self.fail_keys = set(fail_keys)
        self.set_calls: list = []
        self.add_calls: list = []

    def post(self, cfg, path, payload, timeout=20.0):
        added = updated = 0
        for n, row in enumerate(payload["rows"]):
            k = row["key"]
            if k in self.fail_keys:
                return 200, {"error": {"code": "row_failed", "message":
                             f"rows[{n}] key '{k}' failed after {n} row(s) were applied"}}
            if k in self.rows:                       # LookupTranslation hit -> SetTranslation
                self.set_calls.append(k)
                updated += 1
            else:
                self.add_calls.append(k)
                added += 1
            self.rows[k] = row["value"]
        return 200, {"ok": True, "count": len(payload["rows"]), "added": added, "updated": updated}


def test_add_translations_existing_key_calls_set_translation_and_counts_updated(alpha, monkeypatch):
    # C#: an existing key goes through SetTranslation and is counted in `updated`.
    src = _bridge_src()
    upsert = _bridge_method_body(src, "private static bool UpsertTranslation(")
    assert "TranslationRowExists(key)" in upsert     # the one shared existence check
    assert upsert.count("NoteTranslationWritten(key, locale, text)") == 3     # every write path records the key
    assert upsert.index("InformationModel.SetTranslation(lt)") < upsert.index("InformationModel.AddTranslation(lt)")
    assert "already contains" in upsert               # Lookup miss on an existing key still updates
    bulk = _bridge_method_body(src, "private string AddTranslationsInline(")
    assert "UpsertTranslation(" in bulk
    assert "InformationModel.AddTranslation(" not in bulk      # no direct Add that can throw
    assert "if (isNew) added++; else updated++;" in bulk

    # Python side: the counts the bridge reports reach the caller untouched.
    table = _FakeTranslationTable(existing=["k1"])
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body", table.post)
    out = core.bridge_add_translations(alpha, "Alpha", [{"key": "k1", "value": "new"}])
    assert out["ok"] is True and out["updated"] == 1 and out["added"] == 0
    assert table.set_calls == ["k1"] and table.rows["k1"] == "new"


def test_add_translations_new_keys_count_as_added(alpha, monkeypatch):
    table = _FakeTranslationTable(existing=["old"])
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body", table.post)
    out = core.bridge_add_translations(
        alpha, "Alpha", [{"key": "n1", "value": "A"}, {"key": "n2", "value": "B"},
                         {"key": "old", "value": "C"}])
    assert out["added"] == 2 and out["updated"] == 1
    assert table.add_calls == ["n1", "n2"] and table.set_calls == ["old"]
    # the single-key route also reports new vs existing via UpsertTranslation
    single = _bridge_method_body(_bridge_src(), "private string AddTranslationInline(")
    assert "bool isNew = UpsertTranslation(lt, locale, value);" in single


def test_add_translations_row_error_does_not_abort_rows_already_applied(alpha, monkeypatch):
    table = _FakeTranslationTable(existing=["a"], fail_keys=["bad"])
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body", table.post)
    with pytest.raises(core.BridgeWriteFailed) as ei:
        core.bridge_add_translations(
            alpha, "Alpha", [{"key": "a", "value": "A2"}, {"key": "bad", "value": "x"},
                             {"key": "z", "value": "Z"}])
    assert "row_failed" in str(ei.value) and "1 row(s) were applied" in str(ei.value)
    assert table.rows["a"] == "A2"                   # the row before the error stays applied
    assert "z" not in table.rows                     # rows after it are not attempted
    # C#: the failing row returns row_failed from inside the loop, never rolling back
    bulk = _bridge_method_body(_bridge_src(), "private string AddTranslationsInline(")
    assert 'row_failed' in bulk and '\\"partial\\":' in bulk     # reports partial/written
    assert "rollback" not in bulk.lower()


def test_add_translations_edit_op_behaves_like_dedicated_tool(alpha, monkeypatch):
    rows = [{"key": "k1", "value": "v1"}, {"key": "k2", "value": "v2"}]
    results = []
    for use_edit_op in (False, True):
        table = _FakeTranslationTable(existing=["k1"])
        monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
        monkeypatch.setattr(core, "_bridge_post_body", table.post)
        if use_edit_op:
            res = core._apply_one_edit(alpha, "Alpha", {"op": "add_translations", "rows": rows})
        else:
            res = core.bridge_add_translations(alpha, "Alpha", rows)
        results.append((res["added"], res["updated"], table.rows, table.set_calls, table.add_calls))
    assert results[0] == results[1] == (1, 1, {"k1": "v1", "k2": "v2"}, ["k1"], ["k2"])
    # both reach the same bridge routine (one C# code path)
    assert core._BRIDGE_EDIT_OPS["add_translations"][0] == "bridge_add_translations"


# ---- #48: create_object / create_variable never report success without a node ----

_NODE_ABSENT_ROUTE = {"/bridge/nodes": (404, {"error": {"code": "node_not_found"}})}


def test_create_object_post_check_finds_no_node_returns_not_created(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/object": (200, {"ok": True, "created_path": "Model/Pens/Pen1"}),
              **_NODE_ABSENT_ROUTE}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_object(alpha, "Alpha", "Model/Pens", "Pen1", "SomeType")
    assert out["ok"] is False
    assert out["code"] == "create_object_not_created"
    assert out["parent"] == "Model/Pens" and out["type"] == "SomeType"
    assert "Nothing was created" in out["message"]
    assert any(p.startswith("/bridge/nodes?path=Model/Pens/Pen1") for _, p in cap)


def test_create_object_post_check_node_present_returns_ok(alpha, monkeypatch):
    routes = {"/bridge/model/object": (200, {"ok": True, "created_path": "Model/Motor1",
                                             "type": "BaseObjectType"}),
              "/bridge/nodes": (200, {"path": "Model/Motor1", "node_class": "Object"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_create_object(alpha, "Alpha", "Model", "Motor1")
    assert out["ok"] is True and out["created_path"] == "Model/Motor1"


def test_create_object_post_check_unverifiable_does_not_fail_create(alpha, monkeypatch):
    # Only an explicit node_not_found counts as "nothing created".
    routes = {"/bridge/model/object": (200, {"ok": True, "created_path": "Model/M"}),
              "/bridge/nodes": (500, {"error": {"code": "boom"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    assert core.bridge_create_object(alpha, "Alpha", "Model", "M")["ok"] is True


def test_create_object_ui_namespace_child_also_post_checked(alpha, monkeypatch):
    routes = {"/bridge/model/object": (200, {"ok": True,
              "created_path": "UI/W/G/GridLayoutProperties", "via": "x"}),
              **_NODE_ABSENT_ROUTE}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_create_object(alpha, "Alpha", "UI/W/G", "GridLayoutProperties")
    assert out["code"] == "create_object_not_created"


def test_batch_create_object_post_check_fails_the_op(alpha, monkeypatch):
    routes = {"/bridge/model/object": (200, {"ok": True, "created_path": "Model/Pen1"}),
              **_NODE_ABSENT_ROUTE}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as e:
        core._apply_one_edit(alpha, "Alpha", {
            "op": "create_object", "parent": "Model", "name": "Pen1",
            "object_type": "SomeType"})
    assert "create_object_not_created" in str(e.value)
    assert e.value.op_result["parent"] == "Model"


@pytest.mark.parametrize("parent,otype", [
    ("Model", None),                                   # container
    ("UI/Screens/S", "UI/Templates/CardType"),         # project type
    ("CommDrivers", "FTOptix.RAEtherNetIP.RAEtherNetIPDriver"),  # driver
    ("CommDrivers/D", "FTOptix.RAEtherNetIP.RAEtherNetIPStation"),  # station
])
def test_create_object_successful_kinds_still_ok(alpha, monkeypatch, parent, otype):
    routes = {"/bridge/model/object": (200, {"ok": True, "created_path": f"{parent}/N1"}),
              "/bridge/nodes": (200, {"node_class": "Object"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    assert core.bridge_create_object(alpha, "Alpha", parent, "N1", otype)["ok"] is True


def test_create_variable_post_check_finds_no_node_returns_not_created(alpha, monkeypatch):
    routes = {"/bridge/model/variable": (200, {"ok": True, "created_path": "Model/Flag"}),
              **_NODE_ABSENT_ROUTE}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_create_variable(alpha, "Alpha", "Flag")
    assert out["ok"] is False and out["code"] == "create_variable_not_created"
    assert out["parent"] == "Model" and "Nothing was created" in out["message"]


def test_create_variable_dry_run_skips_post_check(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/model/variable": (200, {"ok": True, "dry_run": True}),
              **_NODE_ABSENT_ROUTE}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_variable(alpha, "Alpha", "Flag", dry_run=True)
    assert out["ok"] is True
    assert not any(p.startswith("/bridge/nodes") for _, p in cap)


def test_create_object_trendpen_is_variable_type_not_object(alpha):
    out = core.bridge_create_object(alpha, "Alpha", "Model/T/Pens", "Pen1", "TrendPen")
    assert out["code"] == "variable_type_not_object"
    assert "create_variable" in out["hint"]
    assert core.validate_create_object_op("Pen1", "TrendPen")[0]["code"] \
        == "variable_type_not_object"


# ---- set_property: on-demand creation of type-declared optional children ----
# (ftx-mcp-wvk.2). The creation itself runs in the C# bridge against real Studio
# (live test: test_trend_pen_set_property_creates_color_live); these cover the
# Python pass-through and pin the C# decision structure.

def _bridge_cs() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "studio-bridge"
            / "StudioMCPBridge.cs").read_text(encoding="utf-8-sig")


def test_set_property_creates_optional_child_reports_name(alpha, monkeypatch):
    """The bridge's created_optional_child field reaches the caller untouched."""
    cap: list = []
    routes = {"/bridge/node/property": (200, {
        "ok": True, "via": "created_optional_child", "datatype": "Color",
        "value": "#ff0000", "created_optional_child": "Color"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_set_property(alpha, "Alpha", "Model/T/Pens/Pen1", "Color", "#ff0000")
    assert out["ok"] is True
    assert out["created_optional_child"] == "Color"
    posts = [p for m, p in cap if m == "POST" and p.startswith("/bridge/node/property")]
    assert len(posts) == 1 and "name=Color" in posts[0]


def test_set_property_undeclared_property_still_property_not_found(alpha, monkeypatch):
    """A name the type does not declare keeps failing with property_not_found."""
    routes = {"/bridge/node/property": (200, {
        "error": {"code": "property_not_found",
                  "message": "node Model/T/Pens/Pen1 has no variable or "
                             "materializable property Bogus"}})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    with pytest.raises(core.BridgeWriteFailed) as ei:
        core.bridge_set_property(alpha, "Alpha", "Model/T/Pens/Pen1", "Bogus", "1")
    assert "property_not_found" in str(ei.value)


def test_bridge_set_property_creates_only_type_declared_children():
    """C# structure: the on-demand create is gated on a TYPE declaration, runs
    before the variable-node guard, coerces before attaching, and the undeclared
    fall-through still ends in property_not_found."""
    src = _bridge_cs()
    body = src[src.index("private string SetPropertyInline"):]
    body = body[:body.index("// ---- typed string[] write")]
    assert "FindDeclaredOptionalChild(node, name)" in body
    # variable nodes: declared-child creation precedes the unknown_property guard
    assert body.index("FindDeclaredOptionalChild(node, name)") \
        < body.index("DeclaredPropertyGuard(node, name)")
    assert "IsNodeAttributeName(name)" in body[:body.index("DeclaredPropertyGuard(node, name)")]
    assert 'ErrorJson("property_not_found"' in body

    helper = src[src.index("private static IUAVariable FindDeclaredOptionalChild"):]
    helper = helper[:helper.index("private static string WithCreatedOptionalChild")]
    assert "FindDeclaredVariableTypeProperty(uv.VariableType, name)" in helper
    assert "uo.ObjectType" in helper and "SuperType" in helper
    # no per-type special case
    assert "TrendPen" not in helper and "Color" not in helper
    # coerce BEFORE node.Add so a bad value leaves the model untouched
    assert helper.index("CoerceAssign(child") < helper.index("node.Add(child)")
    assert '"created_optional_child"' in helper


# --- routed_into_collection at apply time (#84 E) ---------------------------

def test_create_widget_routed_reply_carries_routed_into_collection_warning(alpha, monkeypatch):
    """A widget the bridge routed into a placeholder collection (e.g. a
    NavigationPanelItem under a NavigationPanel lands in Panels) reports a
    `routed_into_collection` warning with `routed_path`."""
    routes = {"/bridge/ui/widget": (200, {
        "ok": True, "created_path": "UI/Nav/Panels/Tab1",
        "type": "NavigationPanelItem", "routed_into": "Panels"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_create_widget(alpha, "Alpha", "UI/Nav", "Tab1",
                                    "NavigationPanelItem")
    warns = [w for w in out["warnings"] if w["code"] == "routed_into_collection"]
    assert len(warns) == 1
    assert warns[0]["routed_path"] == "UI/Nav/Panels/Tab1"
    assert warns[0]["routed_into"] == "Panels"


def test_create_widget_not_routed_has_no_routed_warning(alpha, monkeypatch):
    routes = {"/bridge/ui/widget": (200, {
        "ok": True, "created_path": "UI/MainWindow/L2", "type": "Label"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes))
    out = core.bridge_create_widget(alpha, "Alpha", "UI/MainWindow", "L2", "Label")
    assert not any(w.get("code") == "routed_into_collection"
                   for w in out.get("warnings") or [])


def test_batch_apply_surfaces_routed_into_collection_warning():
    """_surface_op_warnings copies the apply-time routed warning into the batch
    result, so a forward-referenced NavigationPanel (not predicted at validate
    time) is still reported."""
    out: dict = {}
    res = core._add_routed_warning({
        "ok": True, "created_path": "UI/Nav/Panels/Tab1", "routed_into": "Panels"})
    core._surface_op_warnings(out, 3, {"op": "create_widget"}, res)
    w = out["warnings"][0]
    assert w["op_index"] == 3 and w["code"] == "routed_into_collection"
    assert w["routed_path"] == "UI/Nav/Panels/Tab1"


def test_validate_ops_routed_warning_emits_routed_path_in_csharp():
    """The C# validator's routed_into_collection warning carries `routed_path`
    (the doc promises it); pending live bench check of the JSON itself."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "studio-bridge"
           / "StudioMCPBridge.cs").read_text(encoding="utf-8")
    m = re.search(r'addWarnX\(idx, "routed_into_collection".*?routed_path', src, re.S)
    assert m, "routed_into_collection warning must include routed_path"
