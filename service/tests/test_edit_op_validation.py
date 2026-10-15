"""Validate-time refusals for edit ops, and refusals that name the right rule
(PRD 0050 D, #72).

Offline: the bridge seams (`_bridge_get_json`, `describe_type`,
`_bridge_post_body`, `_apply_one_edit`) are faked; no Studio is involved.
"""
from __future__ import annotations

import pytest

from service import core
from service.tests.conftest import make_project

_OK_REPORT = {"ok": True, "op_count": 1, "strict": False, "errors": [], "warnings": []}
_CATALOG = {"types": [{"name": n} for n in (
    "Label", "Button", "Panel", "TextBox", "Rectangle", "Image", "Switch")],
    "count": 7, "truncated": False}


@pytest.fixture
def alpha(cfg, projects_root):
    make_project(projects_root, "Alpha")
    return cfg


@pytest.fixture
def bridge(alpha, monkeypatch):
    """Fake the bridge: catalog fetch, type resolver, node lookup, validate."""
    calls: list = []
    state = {"extra_types": set(), "nodes": set()}

    def get_json(cfg, path, timeout=5.0, retries=1):
        calls.append(path)
        if path.startswith("/bridge/types/ui"):
            return 200, _CATALOG
        if path.startswith("/bridge/nodes"):
            from urllib.parse import unquote
            node = unquote(path.split("path=", 1)[1])
            if node in state["nodes"]:
                return 200, {"path": node}
            return 404, {"error": {"code": "node_not_found"}}
        return 404, {}

    def describe(cfg, project, type_name):
        calls.append(f"describe:{type_name}")
        if type_name in state["extra_types"]:
            return {"type": type_name}
        raise core.NodeNotFound(f"no builtin UI type {type_name!r}")

    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_bridge_get_json", get_json)
    monkeypatch.setattr(core, "describe_type", describe)
    monkeypatch.setattr(core, "validate_edit_op_types",
                        _REAL_VALIDATE_EDIT_OP_TYPES)
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, timeout=20.0, **kw: (200, dict(_OK_REPORT)))
    applied: list = []
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op) or {"ok": True})
    state["calls"], state["applied"] = calls, applied
    return state


_REAL_VALIDATE_EDIT_OP_TYPES = core.validate_edit_op_types


def _errors(out, code=None):
    errs = out["report"]["errors"]
    return [e for e in errs if code is None or e["code"] == code]


# ---- (1) widget / object type names ---------------------------------------

def test_create_widget_bad_type_refused_at_validate_time(alpha, bridge):
    ops = [{"op": "create_widget", "screen": "UI/Screens/S", "name": "B",
            "widget_type": "Buton"}]
    out = core.bridge_edit(alpha, "Alpha", ops)
    errs = _errors(out, "invalid_widget_type")
    assert len(errs) == 1
    assert errs[0]["did_you_mean"] == "Button"
    assert errs[0]["op_index"] == 0
    assert "Buton" in errs[0]["message"]
    assert out["state"] == "validated" and out["applied"] == 0
    assert bridge["applied"] == []


def test_create_widget_valid_type_passes_and_catalog_fetched_once(alpha, bridge):
    ops = [{"op": "create_widget", "screen": "UI/Screens/S", "name": f"W{i}",
            "widget_type": t} for i, t in enumerate(["Button", "Label", "Button"])]
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert _errors(out) == []
    assert out["state"] == "succeeded"
    assert sum(c.startswith("/bridge/types/ui") for c in bridge["calls"]) == 1


def test_create_widget_module_type_known_to_the_resolver_is_accepted(alpha, bridge):
    bridge["extra_types"].add("AlarmGrid")
    ops = [{"op": "create_widget", "screen": "UI/Screens/S", "name": "G",
            "widget_type": "AlarmGrid"}]
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert _errors(out) == []


def test_create_widget_type_is_case_sensitive_with_did_you_mean(alpha, bridge):
    ops = [{"op": "create_widget", "screen": "UI/Screens/S", "name": "B",
            "widget_type": "button"}]
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert _errors(out, "invalid_widget_type")[0]["did_you_mean"] == "Button"


def test_create_widget_type_path_checked_against_model_and_batch(alpha, bridge):
    bridge["nodes"].add("UI/Templates/Card")
    ops = [
        {"op": "create_type", "parent": "UI/Templates", "name": "Made"},
        {"op": "create_widget", "screen": "UI/Screens/S", "name": "A",
         "widget_type": "UI/Templates/Card"},
        {"op": "create_widget", "screen": "UI/Screens/S", "name": "B",
         "widget_type": "UI/Templates/Made"},
        {"op": "create_widget", "screen": "UI/Screens/S", "name": "C",
         "widget_type": "UI/Templates/Nope"},
    ]
    out = core.bridge_edit(alpha, "Alpha", ops)
    errs = _errors(out, "invalid_widget_type")
    assert [e["op_index"] for e in errs] == [3]


def test_create_object_bad_type_refused_at_validate_time(alpha, bridge):
    ops = [{"op": "create_object", "parent": "Model", "name": "O",
            "object_type": "Pnael"}]
    out = core.bridge_edit(alpha, "Alpha", ops)
    errs = _errors(out, "invalid_object_type")
    assert len(errs) == 1
    assert errs[0]["op_index"] == 0
    assert errs[0]["did_you_mean"] == "Panel"
    assert bridge["applied"] == []


def test_create_object_unknown_type_without_a_close_match(alpha, bridge):
    ops = [{"op": "create_object", "parent": "Model", "name": "O",
            "object_type": "Qqqqqqqq"}]
    out = core.bridge_edit(alpha, "Alpha", ops)
    errs = _errors(out, "invalid_object_type")
    assert errs and errs[0]["did_you_mean"] is None


def test_create_object_folder_and_absent_type_are_untouched(alpha, bridge):
    ops = [{"op": "create_object", "parent": "Model", "name": "F", "object_type": "Folder"},
           {"op": "create_object", "parent": "Model", "name": "P"}]
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert _errors(out) == []


def test_empty_object_type_is_missing_field_and_creates_nothing(alpha, bridge):
    ops = [{"op": "create_object", "parent": "Model", "name": "O", "object_type": ""}]
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert _errors(out, "missing_field")
    assert out["state"] == "validated" and bridge["applied"] == []


def test_empty_widget_type_is_missing_field_and_absent_keeps_default_warning(alpha, bridge):
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_widget", "screen": "UI/Screens/S", "name": "W", "widget_type": " "}])
    assert _errors(out, "missing_field")
    assert bridge["applied"] == []
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_widget", "screen": "UI/Screens/S", "name": "W"}])
    assert _errors(out) == []
    assert [w["code"] for w in out["report"]["warnings"]] == ["default_widget_type"]


def test_empty_object_type_per_noun_creates_no_base_object(alpha, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("bridge must not be reached")
    monkeypatch.setattr(core, "_bridge_write", boom)
    res = core.bridge_create_object(alpha, "Alpha", "Model", "O", "")
    assert res["ok"] is False and res["code"] == "missing_field"
    with pytest.raises(core.BridgeWriteFailed, match="missing_field"):
        core.bridge_create_widget(alpha, "Alpha", "UI/Screens/S", "W", "")


def test_type_check_fails_open_when_the_bridge_cannot_answer(alpha, monkeypatch):
    def down(cfg, path, **kw):
        raise core.BridgeUnavailable("down")
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_bridge_get_json", down)
    ops = [{"op": "create_widget", "screen": "S", "name": "B", "widget_type": "Buton"}]
    assert _REAL_VALIDATE_EDIT_OP_TYPES(alpha, "Alpha", ops) == []


# ---- (2) browse names -------------------------------------------------------

@pytest.mark.parametrize("name", ["D/E", "D\\E", "A\tB", "A\nB", "A\x00B", " A", "A ", "\tA"])
def test_create_variable_invalid_browse_name_refused(alpha, bridge, name):
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_variable", "parent": "Model", "name": name, "datatype": "Int32"}])
    errs = _errors(out, "invalid_browse_name")
    assert len(errs) == 1 and errs[0]["op_index"] == 0
    assert out["state"] == "validated" and bridge["applied"] == []


@pytest.mark.parametrize("name", ["A.B", "A[1]", "A@B", "Motor_1", "a b"])
def test_create_variable_round_trip_characters_stay_allowed(alpha, bridge, name):
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_variable", "parent": "Model", "name": name, "datatype": "Int32"}])
    assert _errors(out) == []
    assert out["state"] == "succeeded"


def test_per_noun_create_variable_and_object_refuse_before_any_write(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_write",
                        lambda *a, **k: pytest.fail("bridge must not be reached"))
    res = core.bridge_create_variable(alpha, "Alpha", "D/E", "Model", "Int32")
    assert res["ok"] is False and res["code"] == "invalid_browse_name"
    res = core.bridge_create_object(alpha, "Alpha", "Model", "D\\E")
    assert res["ok"] is False and res["code"] == "invalid_browse_name"


def test_post_create_existence_check_uses_the_escaped_path(alpha, monkeypatch):
    seen: list = []

    def get_json(cfg, path, timeout=5.0, retries=1):
        seen.append(path)
        return 404, {"error": {"code": "node_not_found"}}

    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_bridge_get_json", get_json)
    assert core._created_node_absent(alpha, "Alpha", "Model/Grp", "Tag[1]@x.y") is True
    # one segment per name: escaped, not split on a raw '/' or sent with raw [ ] @
    assert seen == ["/bridge/nodes?path=Model/Grp/Tag%5B1%5D%40x.y"]
    seen.clear()
    core._created_node_absent(alpha, "Alpha", "Model", "D/E")
    assert seen == ["/bridge/nodes?path=Model/D%2FE"]


# ---- (3) formatter strings --------------------------------------------------

@pytest.mark.parametrize("fmt,token", [
    ("{0:F1", "{0:F1"),
    ("{0", "{0"),
    ("{a}", "{a}"),
    ("{0}}", "}"),
    ("{#y}", "{#y}"),
    ("{}", "{}"),
    ("{0}{", "{"),
])
def test_attach_string_formatter_bad_format_refused(alpha, bridge, fmt, token):
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "attach_string_formatter", "path": "UI/S/L", "prop_name": "Text",
         "format": fmt, "sources": "Model/T"}])
    errs = _errors(out, "bad_format_string")
    assert errs, out["report"]
    assert errs[0]["token"] == token and errs[0]["op_index"] == 0
    assert out["state"] == "validated" and bridge["applied"] == []


@pytest.mark.parametrize("fmt", ["{0}", "{0:F1}", "{0,8:N1}", "{{lit}} {0}", "{0}/{1:D2}"])
def test_attach_string_formatter_good_formats_pass(alpha, bridge, fmt):
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "attach_string_formatter", "path": "UI/S/L", "prop_name": "Text",
         "format": fmt, "sources": ["Model/A", "Model/B"]}])
    assert _errors(out) == []


@pytest.mark.parametrize("fmt,token", [("../P{#}@NodeId", "{#}"),
                                       ("../P{#y}@NodeId", "{#y}"),
                                       ("../P{#a b}@NodeId", "{#a b}"),
                                       ("../P{#k@NodeId", "{#k@NodeId")])
def test_attach_formatter_bad_format_refused(alpha, bridge, fmt, token):
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "attach_formatter", "path": "UI/S/L", "prop_name": "Text",
         "format": fmt, "sources": "k=Model/K"}])
    errs = _errors(out, "bad_format_string")
    assert errs and errs[0]["token"] == token and errs[0]["op_index"] == 0
    assert bridge["applied"] == []


def test_attach_formatter_named_placeholder_bound_by_sources_passes(alpha, bridge):
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "attach_formatter", "path": "UI/S/L", "prop_name": "Text",
         "format": "../Nav{#k}@NodeId/{#m}", "sources": {"k": "Model/K", "m": "Model/M"}}])
    assert _errors(out) == []


def test_per_noun_formatters_raise_bad_format_string(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_write",
                        lambda *a, **k: pytest.fail("bridge must not be reached"))
    with pytest.raises(core.BridgeWriteFailed, match="bad_format_string.*'{0:F1'"):
        core.bridge_attach_string_formatter(
            alpha, "Alpha", "UI/S/L", "Text", "{0:F1", "Model/T")
    with pytest.raises(core.BridgeWriteFailed, match="bad_format_string.*'{#}'"):
        core.bridge_attach_formatter(
            alpha, "Alpha", "UI/S/L", "Text", "../P{#}", "k=Model/K")


def test_unbound_placeholder_is_kept(alpha, bridge):
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "attach_string_formatter", "path": "UI/S/L", "prop_name": "Text",
         "format": "{0:F1}"}])
    assert _errors(out, "unbound_placeholder")
    assert not _errors(out, "bad_format_string")


# ---- (4) failed vs partial --------------------------------------------------

def _failing_apply(fail_at):
    def apply(cfg, project, op):
        if op["name"] == fail_at:
            raise core.BridgeWriteFailed("boom")
        return {"ok": True}
    return apply


_VAR_OPS = [{"op": "create_variable", "parent": "Model", "name": n, "datatype": "Int32"}
            for n in ("A", "B", "C")]


def test_batch_failing_with_nothing_applied_is_failed_not_partial(alpha, bridge, monkeypatch):
    monkeypatch.setattr(core, "_apply_one_edit", _failing_apply("A"))
    out = core.bridge_edit(alpha, "Alpha", _VAR_OPS)
    assert out["applied"] == 0
    assert out["state"] == "failed"
    assert out["failed_op"]["index"] == 0
    assert "op 0 failed before anything was applied; the model is unchanged" in out["nudge"]
    assert "not atomic" not in out["nudge"]


def test_batch_failing_after_applied_ops_is_partial_with_not_atomic_wording(
        alpha, bridge, monkeypatch):
    monkeypatch.setattr(core, "_apply_one_edit", _failing_apply("C"))
    out = core.bridge_edit(alpha, "Alpha", _VAR_OPS)
    assert out["applied"] == 2
    assert out["state"] == "partial"
    assert "op 2 failed AFTER 2 op(s)" in out["nudge"]
    assert "not atomic" in out["nudge"]


def test_apply_failure_outcome_helper():
    assert core._apply_failure_outcome(3, 0)[0] == "failed"
    assert core._apply_failure_outcome(3, 1)[0] == "partial"


# ---- (2b) shared browse-name guard on every creating/renaming op (0052 B, #80)

# One valid-otherwise op per creating/renaming verb; the name field is filled in
# by the test. `rename` is lowered to `move` before validation.
_NAMING_OPS = {
    "create_variable": ("name", {"op": "create_variable", "parent": "Model", "datatype": "Int32"}),
    "create_object": ("name", {"op": "create_object", "parent": "Model"}),
    "create_folder": ("name", {"op": "create_folder", "parent": "Model"}),
    "create_widget": ("name", {"op": "create_widget", "screen": "UI/Screens/S",
                               "widget_type": "Label"}),
    "create_type": ("name", {"op": "create_type", "parent": "Types"}),
    "create_alias": ("name", {"op": "create_alias", "parent_path": "Model/T"}),
    "create_netlogic": ("name", {"op": "create_netlogic", "parent": "NetLogic"}),
    "rename": ("new_name", {"op": "rename", "path": "Model/Old"}),
    "move": ("new_name", {"op": "move", "path": "Model/Old", "new_parent": "Model/G"}),
    "convert_to_type": ("type_name", {"op": "convert_to_type", "path": "UI/Screens/S/P",
                                      "types_folder": "UI/Types"}),
}


def test_every_batch_verb_is_classified_for_the_browse_name_guard():
    """A new op verb must be declared name-bearing or explicitly non-naming."""
    verbs = set(core.BRIDGE_EDIT_VERBS)
    classified = set(core._BROWSE_NAME_FIELDS) | set(core._NON_NAMING_EDIT_VERBS)
    assert verbs - classified == set(), "classify new op verbs for invalid_browse_name"
    assert set(core._BROWSE_NAME_FIELDS) <= verbs
    assert set(_NAMING_OPS) == set(core._BROWSE_NAME_FIELDS)


@pytest.mark.parametrize("verb", sorted(core._BROWSE_NAME_FIELDS))
def test_every_creating_or_renaming_op_refuses_a_slash_name(alpha, bridge, verb):
    field, base = _NAMING_OPS[verb]
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "create_folder", "parent": "Model", "name": "Fine"},
        {**base, field: "A/B"}], dry_run=True)
    errs = _errors(out, "invalid_browse_name")
    assert len(errs) == 1 and errs[0]["op_index"] == 1 and errs[0]["field"] == field
    assert out["state"] == "validated" and bridge["applied"] == []


@pytest.mark.parametrize("verb", sorted(core._BROWSE_NAME_FIELDS))
def test_every_creating_or_renaming_op_accepts_a_plain_name(alpha, bridge, verb):
    field, base = _NAMING_OPS[verb]
    out = core.bridge_edit(alpha, "Alpha", [{**base, field: "Fine_1"}], dry_run=True)
    assert _errors(out, "invalid_browse_name") == []


def test_per_noun_tools_refuse_a_slash_name_before_any_write(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_write",
                        lambda *a, **k: pytest.fail("bridge must not be reached"))
    monkeypatch.setattr(core, "_bridge_write_ui_ns_child",
                        lambda *a, **k: pytest.fail("bridge must not be reached"))
    with pytest.raises(core.BridgeWriteFailed, match="invalid_browse_name"):
        core.bridge_create_widget(alpha, "Alpha", "UI/Screens/S", "A/B")
    results = [
        core.bridge_create_folder(alpha, "Alpha", "Model", "A/B"),
        core.bridge_create_type(alpha, "Alpha", "A/B", "Types"),
        core.bridge_create_alias(alpha, "Alpha", "Model/T", "A/B"),
        core.bridge_create_netlogic(alpha, "Alpha", "NetLogic", "A/B"),
        core.bridge_move_node(alpha, "Alpha", "Model/Old", "Model", "X/Y"),
        core.bridge_convert_to_type(alpha, "Alpha", "UI/S/P", "A/B", "UI/Types"),
    ]
    for res in results:
        assert res["ok"] is False and res["code"] == "invalid_browse_name", res
