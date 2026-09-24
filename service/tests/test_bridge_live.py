"""Live-bridge CONTRACT tests — VM-only, gated, INERT on Linux/CI.

Unlike test_bridge*.py (which mock ``core._bridge_http`` to test the Python
client's request-shaping + response-interpretation), this module drives the
ACTUAL C# design-time bridge running inside FactoryTalk Optix Studio. It
validates the contracts the bridge itself is responsible for — the guard
messages, suggestion baking, and schema introspection that the mocks can only
assume. A green run here is the ground truth that the mocked tests are faithful.

HOW TO RUN (on the Windows VM, NOT CI):
    1. Open FactoryTalk Optix Studio on the project named by FTX_LIVE_PROJECT.
    2. Run StartBridge: Project tree -> right-click the StudioBridge NetLogic
       node -> Run -> StartBridge.
    3. Set the gate + target and run pytest:
           set FTX_LIVE_BRIDGE=1
           set FTX_LIVE_PROJECT=<open project name>
           python -m pytest service/tests/test_bridge_live.py -q

Without FTX_LIVE_BRIDGE=1 the whole module is skipped, so it adds only skips to
the Linux/CI count and never touches the network.

WHAT THE LIVE PROJECT MUST CONTAIN (all overridable via env — see each const):
    * A node at FTX_LIVE_NODE (default UI/MainWindow/Rectangle1) whose type is a
      Rectangle (or any builtin carrying a Color-family property).
    * The builtin type named by FTX_LIVE_TYPE (default Rectangle) resolvable in
      the type catalog (it is, in every stock project — it's a Studio builtin).

HOME for the live-contract suite:
    * U5  (did_you_mean)  — a misspelled property surfaces the bridge's
      DeclaredPropertyGuard "(did you mean ...)" suggestion.
    * bridge reachability — core.bridge_state reports available + serving.
    * type catalog        — core.list_ui_types returns a non-empty builtin set.
    * U15 (describe_type)  — REAL contract: describe_type returns per-property
      {name, datatype, settable} for a builtin, incl. a Color-family property.
    * U15 (schema dump)   — /bridge/schema/dump contract as a REAL gate; the C#
      endpoint (service.optix_schema.SCHEMA_DUMP_ROUTE) shipped with U15.
    * U16 (validate_ops)  — POST /bridge/validate_ops as a REAL gate across all
      three tiers (per-op validity, batch coherence, strict lint) plus proof that
      validation and bridge_edit's dry_run mutate nothing. Endpoint shipped
      with U16.
    * (rewire round-trip) — wire_event twice on the same button:
      second call updates args in-place (updated:True), describe_node on the
      handler's InputArguments reads back the new arg value with no leftover
      from the first call. No special CI seeding required beyond Rectangle1.
"""
from __future__ import annotations

import json
import re
import os
import pathlib

import pytest

from service import core, optix_schema

pytestmark = pytest.mark.skipif(
    os.environ.get("FTX_LIVE_BRIDGE") != "1",
    reason="live-bridge contract tests: set FTX_LIVE_BRIDGE=1 on the VM with "
           "Studio open + bridge armed on FTX_LIVE_PROJECT",
)


# ---- Project-specific targets (env-overridable; defaults for a stock box) ---
# FTX_LIVE_PROJECT MUST contain a node at FTX_LIVE_NODE that is a Rectangle-like
# control exposing a Color-FAMILY property. NOTE: a Rectangle exposes FillColor
# and BorderColor, NOT BackgroundColor — so the default misspelling below is
# "BorderColo" (a near-miss for the real "BorderColor") which the C#
# SuggestPropertyName guard can actually resolve on a Rectangle. Every FTX_LIVE_*
# is env-overridable when the live project uses a different node/type/property.
_LIVE_NODE = os.environ.get("FTX_LIVE_NODE", "UI/MainWindow/Rectangle1")
_MISSPELLED_PROP = os.environ.get("FTX_LIVE_MISSPELLED_PROP", "BorderColo")
_EXPECTED_SUGGESTION = os.environ.get("FTX_LIVE_EXPECTED_SUGGESTION", "BorderColor")
# The builtin UI type whose property schema test_describe_type_shape introspects.
_LIVE_TYPE = os.environ.get("FTX_LIVE_TYPE", "Rectangle")
# A never-declared property name used to exercise the crash-safety REJECTION
# path (test_set_property_validity_gate). Must NOT be a real property — the
# point is the guard's rejection, not a mutation.
_GARBAGE_PROP = os.environ.get("FTX_LIVE_GARBAGE_PROP", "ZzzNotARealProperty")
# Common builtin type names — at least one must appear in the catalog.
_COMMON_TYPES = {"Rectangle", "Label", "Button", "Panel", "Image"}
# A NavigationPanel node used by test_validate_ops_create_widget_routed_path.
# The CI setup (the live-CI setup script) creates NavPanel1 at UI/MainWindow before the
# live suite runs and sets FTX_LIVE_NAV_PANEL accordingly; override when the
# live project already contains a NavigationPanel at a different path.
_LIVE_NAV_PANEL = os.environ.get("FTX_LIVE_NAV_PANEL", "UI/MainWindow/NavPanel1")
# A PanelType (or ScreenType) with at least THREE graphic children and at least
# one interspersed IUAVariable child (property) so the graphic-children index
# space differs from the raw Children index.  The CI setup (the live-CI setup script)
# creates a suitable PanelBg test panel and sets these vars before this suite
# runs; override when your live project uses a different layout.
_LIVE_REORDER_PANEL = os.environ.get("FTX_LIVE_REORDER_PANEL", "UI/MainWindow")
# The graphic child INSIDE _LIVE_REORDER_PANEL to reorder.  Must have at least
# two sibling graphic children so there is room to move it both UP and DOWN.
_LIVE_REORDER_CHILD = os.environ.get("FTX_LIVE_REORDER_CHILD",
                                     "UI/MainWindow/Rectangle1")
# A GridLayout widget whose GridLayoutProperties child will be created/deleted
# by test_grid_layout_props_create_set_delete.  The CI setup (the live-CI setup script)
# creates GridLayout1 at UI/MainWindow and sets FTX_LIVE_GRID_PANEL before
# this suite runs; override when the live project has a GridLayout at a
# different path.  The test skips gracefully when this env var is unset and no
# GridLayout1 exists at the default path.
_LIVE_GRID_PANEL = os.environ.get("FTX_LIVE_GRID_PANEL", "UI/MainWindow/GridLayout1")
# Parent path for model variables used by Value-property set_property tests.
# Every stock FTOptix project has a top-level "Model" namespace; override with
# FTX_LIVE_MODEL_PARENT when the live project's model variables live elsewhere.
_LIVE_MODEL_PARENT = os.environ.get("FTX_LIVE_MODEL_PARENT", "Model")


@pytest.fixture
def live():
    """Config from the VM environment + the target project name.

    Skips (rather than fails) when FTX_LIVE_PROJECT is unset so a partially
    configured box degrades to a skip, consistent with the module gate.
    """
    project = os.environ.get("FTX_LIVE_PROJECT")
    if project is None:
        pytest.skip("FTX_LIVE_PROJECT unset — name the project open in Studio")
    core.reset_bridge_cache()
    cfg = core.Config.from_env()
    # Multi-instance (1.0.7+): several bridges may be armed at once, one per
    # open project. Bind cfg to the bridge serving THIS project — the same
    # choke point every tool goes through — instead of whichever bridge
    # bridge_state() lists first (CI run 11: it reported the operator's
    # Smoke108 on 8768 while the CI project sat armed on 8769).
    bcfg = core._bridge_cfg_for(cfg, project)
    if bcfg is None:
        st = core.bridge_state(cfg, force=True)
        pytest.skip(
            f"no armed bridge serves {project!r} (bridge_state: "
            f"{st.get('reason')!r}, serving {st.get('project')!r}) — open "
            f"{project!r} in Studio and run StartBridge"
        )
    return bcfg, project


# ---- reachability ----------------------------------------------------------

def test_bridge_reachable(live):
    """The bridge serving FTX_LIVE_PROJECT is available. Reachability is the
    precondition every other live test depends on; asserting it explicitly
    makes a mis-armed bridge a clear single failure instead of a cascade.

    Multi-instance: bridge_state() echoes whichever bridge answers FIRST at
    its top level (CI run 12: Smoke108 on 8768 while this project sat armed
    on 8769), so the per-project answer is the matching list_bridges() entry."""
    cfg, project = live
    bridges = core.list_bridges(cfg, force=True)
    mine = [b for b in bridges
            if (b.get("project") or "").strip().lower() == project.strip().lower()]
    assert mine, (
        f"no bridge serves {project!r}; armed: {[b.get('project') for b in bridges]} "
        f"- open {project!r} in Studio and run StartBridge"
    )
    assert mine[0].get("available") is True, f"bridge not available: {mine[0].get('reason')!r}"
    assert mine[0].get("reason") == "ok"

def test_list_ui_types_nonempty(live):
    """core.list_ui_types returns the builtin UI type catalog from the live
    model. Shape: {types:[{name} | {name, browse_name}], count, truncated,
    source} — browse_name is omitted per-entry when it equals name (the
    common case), so a missing browse_name means "same as name".
    Assert the catalog is non-empty and includes a common builtin (Rectangle/
    Label/Button/...) so we know the type system is actually reflected, not an
    empty stub."""
    cfg, project = live
    out = core.list_ui_types(cfg, project)
    assert out["source"] == "bridge"
    assert out["count"] > 0, "empty type catalog — bridge type reflection broken"
    types = out["types"]
    assert isinstance(types, list) and types
    seen = {str(t.get("browse_name") or t.get("name") or "") for t in types}
    assert seen & _COMMON_TYPES, (
        f"catalog has {len(seen)} types but none of the common builtins "
        f"{sorted(_COMMON_TYPES)}; sample: {sorted(seen)[:10]}"
    )


# ---- U15 describe_type contract (made real) --------------------------------

def test_describe_type_shape(live):
    """U15: describe_type on a builtin (FTX_LIVE_TYPE, default Rectangle) returns
    a property schema sourced from the bridge. Each property must carry
    {name, datatype, settable} — the exact per-property shape the schema-dump
    contract (optix_schema DUMP CONTRACT) is built on — and a Color-family
    property must be present. Shape: {type, browse_name, properties:[...],
    truncated, source}."""
    cfg, project = live
    schema = core.describe_type(cfg, project, _LIVE_TYPE)
    assert schema["source"] == "bridge"
    props = schema["properties"]
    assert isinstance(props, list) and props, (
        f"{_LIVE_TYPE!r} returned no properties"
    )
    for p in props:
        assert "name" in p and p["name"], f"property missing name: {p!r}"
        assert "datatype" in p, f"property {p.get('name')!r} missing datatype"
        # `settable` is the U15 write-gate signal the schema-dump contract
        # requires; pin it so the C# reflection can't silently drop it.
        assert "settable" in p, (
            f"property {p['name']!r} missing 'settable' — U15 describe_type "
            f"contract requires the write-gate flag on every property"
        )
    names_lower = {str(p["name"]).lower() for p in props}
    assert any("color" in n for n in names_lower), (
        f"{_LIVE_TYPE!r} has no Color-family property; got {sorted(names_lower)}"
    )


# ---- crash-safety validity gate (REJECTION path only) ----------------------

def test_set_property_validity_gate(live):
    """Setting an UNDECLARED property (FTX_LIVE_GARBAGE_PROP) on a real node
    (FTX_LIVE_NODE) must be REJECTED by the bridge's DeclaredPropertyGuard —
    surfacing as BridgeWriteFailed, never a crash and never a silent success.
    This is the crash-safety gate: a scalar write to an unmaterialized/undeclared
    UA variable used to crash Studio outright (2026-07-16 array trap), so the
    guard rejecting BEFORE touching the model is load-bearing.

    We assert the REJECTION only (a garbage name is never written), so the live
    project is never mutated. classify_bridge_failure must classify it as a
    per-op write_failed with the bridge still reachable (NOT an "open Studio"
    nudge). When FTX_LIVE_NODE is a correctly-staged real node the guard's
    message also carries the unknown_property code + valid-set hint; we assert
    that softly since a mis-set FTX_LIVE_NODE yields a node-not-found rejection
    instead — either way the crash-safety contract (raise, don't crash) holds.

    Note: valid_properties is a STRUCTURED sibling field on the C# error dict
    that _bridge_write_result flattens away (message+code only reach the caller),
    so it never appears in str(exc) — the message's "(...valid set)" hint is the
    surface the LLM actually sees."""
    cfg, project = live
    with pytest.raises(core.BridgeWriteFailed) as excinfo:
        core.bridge_set_property(cfg, project, _LIVE_NODE, _GARBAGE_PROP, "red")

    detail = str(excinfo.value)
    assert detail, "empty BridgeWriteFailed message"

    out = core.classify_bridge_failure(cfg, project, excinfo.value)
    assert out["reason_code"] == "write_failed", (
        f"a rejected write must classify as write_failed, got {out['reason_code']!r}"
    )
    assert out["bridge"]["reachable"] is True
    assert out["detail"] == detail

    # Softer contract: on a correctly-staged node the guard names the code.
    if "unknown_property" in detail:
        assert "valid" in detail.lower(), (
            "unknown_property rejection should point at the valid-property set"
        )


# ---- U5 did_you_mean (retained) --------------------------------------------

def test_live_misspelled_property_surfaces_did_you_mean(live):
    """U5: setting a MISSPELLED property on a real node makes the live bridge's
    DeclaredPropertyGuard reject the write AND bake a "(did you mean <Prop>?)"
    suggestion into the error message. That suggestion must survive
    _bridge_write_result's message/code flattening to the raised
    BridgeWriteFailed AND reach classify_bridge_failure()'s ``detail`` — the
    only field the MCP tool caller ever sees. Mirrors the mocked contract in
    test_bridge_writes.py::test_unknown_property_suggestion_reaches_classify_detail
    against the real C# guard."""
    cfg, project = live
    with pytest.raises(core.BridgeWriteFailed) as excinfo:
        core.bridge_set_property(cfg, project, _LIVE_NODE, _MISSPELLED_PROP, "red")

    raised = str(excinfo.value)
    assert "did you mean" in raised.lower(), (
        f"live bridge did not suggest a correction for {_MISSPELLED_PROP!r} on "
        f"{_LIVE_NODE!r}; raised: {raised!r}. Confirm FTX_LIVE_NODE is a "
        f"Rectangle-like node with a {_EXPECTED_SUGGESTION} property."
    )
    assert _EXPECTED_SUGGESTION in raised

    # The suggestion must also reach the LLM-facing classify detail.
    out = core.classify_bridge_failure(cfg, project, excinfo.value)
    assert out["reason_code"] == "write_failed"
    assert "did you mean" in out["detail"].lower()
    assert _EXPECTED_SUGGESTION in out["detail"]


# ---- U15 schema-dump contract (REAL gate; the C# endpoint shipped) ---------

def test_schema_dump_contract(live):
    """U15: exercise the schema-dump path (optix_schema.fetch_schema_dump, which
    GETs SCHEMA_DUMP_ROUTE = /bridge/schema/dump) and assert the DUMP CONTRACT
    the C# must satisfy (optix_schema module docstring):
        {studio_version, generated_at,
         types: {<T>: {browse_name, properties:[{name, datatype, settable}]}}}

    Was xfail(raises=BridgeUnavailable) while only the Python half existed. The
    C# endpoint shipped with U15, so this is now a real gate — it caught nothing
    on the way in (first live run xpassed), and its job from here is to fail loud
    if a future bridge build changes the dump shape.

    Note the dump is NOT capped at the bridge's MaxItems, unlike describe_type:
    a truncated dump would cache as a schema that looks complete and would then
    surface as phantom add/removes in the next cross-version diff."""
    cfg, project = live
    dump = optix_schema.fetch_schema_dump(cfg, project)

    # Only reached once the endpoint exists — assert the full dump contract.
    assert isinstance(dump.get("studio_version"), str) and dump["studio_version"]
    assert isinstance(dump.get("generated_at"), str) and dump["generated_at"]
    types = dump.get("types")
    assert isinstance(types, dict) and types, "dump has no types map"
    for tname, tbody in types.items():
        assert isinstance(tbody, dict), f"type {tname!r} body not an object"
        assert isinstance(tbody.get("browse_name"), str), (
            f"type {tname!r} missing browse_name"
        )
        props = tbody.get("properties")
        assert isinstance(props, list), f"type {tname!r} properties not a list"
        for p in props:
            assert {"name", "datatype", "settable"} <= set(p), (
                f"type {tname!r} property {p!r} missing name/datatype/settable"
            )


# ---- U16 validate_ops contract (REAL gate; the C# endpoint shipped) --------

# A node the batch pretends to create. Never actually created: every call below
# is validation-only, and the last test proves the model is untouched.
_HYPO = _LIVE_NODE.rsplit("/", 1)[0] + "/ZzzU16Hypothetical"


def _report(cfg, project, ops, strict=False):
    rep = core.bridge_validate_ops(cfg, project, ops, strict=strict)
    assert isinstance(rep.get("ok"), bool), rep
    assert isinstance(rep.get("errors"), list) and isinstance(rep.get("warnings"), list)
    for e in rep["errors"] + rep["warnings"]:
        assert isinstance(e.get("op_index"), int), e
        assert isinstance(e.get("code"), str) and e["code"], e
    return rep


def test_validate_ops_contract(live):
    """U16: POST /bridge/validate_ops reports on an op batch without touching the
    model. Pins the REPORT SHAPE the C# must keep:
        {ok: bool,
         errors:   [{op_index: int, code: str, ...}],
         warnings: [...]}

    Was xfail while only the shape was agreed; the endpoint shipped with U16, so
    this is now a real gate. `_report` asserts the shape on every call below."""
    cfg, project = live
    rep = _report(cfg, project, [
        {"op": "set_property", "path": _LIVE_NODE, "name": "Width", "value": "125"},
    ])
    assert rep["ok"] is True, rep
    assert rep["errors"] == []
    assert rep.get("op_count") == 1


def test_validate_ops_accepts_create_then_reference(live):
    """Tier 2: a batch is validated against a HYPOTHETICAL model carrying its own
    creates, so referring to a node the batch creates EARLIER is legal."""
    cfg, project = live
    rep = _report(cfg, project, [
        {"op": "create_widget", "screen": _LIVE_NODE.rsplit("/", 1)[0],
         "name": _HYPO.rsplit("/", 1)[1], "widget_type": "Rectangle"},
        {"op": "set_property", "path": _HYPO, "name": "Width", "value": "40"},
    ])
    assert rep["ok"] is True, rep


def test_validate_ops_rejects_reversed_order_with_a_hint(live):
    """The same two ops in the wrong order must fail — and the message must name
    the later create, because "no such node" alone does not tell an agent that
    its ORDERING is the bug."""
    cfg, project = live
    rep = _report(cfg, project, [
        {"op": "set_property", "path": _HYPO, "name": "Width", "value": "40"},
        {"op": "create_widget", "screen": _LIVE_NODE.rsplit("/", 1)[0],
         "name": _HYPO.rsplit("/", 1)[1], "widget_type": "Rectangle"},
    ])
    assert rep["ok"] is False
    codes = [e["code"] for e in rep["errors"]]
    assert "unresolved_reference" in codes, rep
    msg = " ".join(e["message"] for e in rep["errors"])
    assert "LATER op" in msg or "op order" in msg, msg


def test_validate_ops_flags_a_misspelled_property_with_did_you_mean(live):
    """Tier 1 reuses the SAME DeclaredPropertyGuard the write path runs, so the
    report carries its valid_properties + did_you_mean."""
    cfg, project = live
    rep = _report(cfg, project, [
        {"op": "set_property", "path": _LIVE_NODE,
         "name": _MISSPELLED_PROP, "value": "1"},
    ])
    assert rep["ok"] is False
    err = next(e for e in rep["errors"] if e["code"] == "unknown_property")
    guard = (err.get("guard") or {}).get("error") or {}
    assert guard.get("did_you_mean") == _EXPECTED_SUGGESTION, err
    assert _EXPECTED_SUGGESTION in (guard.get("valid_properties") or []), err


def test_validate_ops_catches_delete_then_modify(live):
    """Tier 2 coherence: deleting a node a later op still touches is refused up
    front — the batch would otherwise half-apply and strand the model."""
    cfg, project = live
    rep = _report(cfg, project, [
        {"op": "delete", "path": _LIVE_NODE},
        {"op": "set_property", "path": _LIVE_NODE, "name": "Width", "value": "1"},
    ])
    assert rep["ok"] is False
    assert "modifies_deleted_node" in [e["code"] for e in rep["errors"]], rep


def test_validate_ops_strict_promotes_warnings(live):
    """Tier 3 lint is warnings-only by default; strict makes them fatal."""
    cfg, project = live
    ops = [{"op": "create_widget", "screen": _LIVE_NODE.rsplit("/", 1)[0],
            "name": _LIVE_NODE.rsplit("/", 1)[1], "widget_type": "Rectangle"}]

    lax = _report(cfg, project, ops)
    assert lax["ok"] is True
    assert "already_exists" in [w["code"] for w in lax["warnings"]], lax

    strict = _report(cfg, project, ops, strict=True)
    assert strict["ok"] is False
    assert "already_exists" in [e["code"] for e in strict["errors"]], strict


def test_validate_ops_create_move_set_on_new_path(live):
    """U16 hypothetical model: create → move → set_property on the NEW path
    → ok:True. The validator must track where the node lands after the move so
    a later op on the post-move path is not rejected as unresolved_reference."""
    cfg, project = live
    parent = _LIVE_NODE.rsplit("/", 1)[0]
    orig_name = "ZzzU16Original"
    orig_path = parent + "/" + orig_name
    new_name = "ZzzU16Moved"
    new_path = parent + "/" + new_name
    rep = _report(cfg, project, [
        {"op": "create_widget", "screen": parent,
         "name": orig_name, "widget_type": "Rectangle"},
        {"op": "move", "path": orig_path, "new_parent": parent, "new_name": new_name},
        {"op": "set_property", "path": new_path, "name": "Width", "value": "50"},
    ])
    assert rep["ok"] is True, rep


def test_validate_ops_create_move_set_on_old_path(live):
    """U16 hypothetical model: create → move → set_property on the OLD path
    → ok:False, code moved_earlier_in_batch. The rejection message must name
    the new path so the caller knows where the node actually landed."""
    cfg, project = live
    parent = _LIVE_NODE.rsplit("/", 1)[0]
    orig_name = "ZzzU16Original"
    orig_path = parent + "/" + orig_name
    new_name = "ZzzU16Moved"
    new_path = parent + "/" + new_name
    rep = _report(cfg, project, [
        {"op": "create_widget", "screen": parent,
         "name": orig_name, "widget_type": "Rectangle"},
        {"op": "move", "path": orig_path, "new_parent": parent, "new_name": new_name},
        {"op": "set_property", "path": orig_path, "name": "Width", "value": "50"},
    ])
    assert rep["ok"] is False, rep
    codes = [e["code"] for e in rep["errors"]]
    assert "moved_earlier_in_batch" in codes, rep
    msg = " ".join(
        e["message"] for e in rep["errors"] if e["code"] == "moved_earlier_in_batch"
    )
    assert new_path in msg or new_name in msg, (
        f"moved_earlier_in_batch message should name the new path {new_path!r}; "
        f"got: {msg!r}"
    )


def test_validate_ops_create_widget_routed_path(live):
    """U16: create_widget of a type that auto-routes into a placeholder
    collection (NavigationPanelItem → NavigationPanel.Panels), then
    set_property on the ROUTED path → ok:True.

    This is the exact batch that failed 2026-09-04: the old validator predicted
    the child at <parent>/<name> while WriteWidgetInline placed it at
    <parent>/Panels/<name>, so a subsequent set_property on the real (routed)
    path was rejected as unresolved_reference. Requires a NavigationPanel at
    FTX_LIVE_NAV_PANEL (default UI/MainWindow/NavPanel1); the CI setup
    (the live-CI setup script) creates that node before this suite runs."""
    cfg, project = live
    try:
        core.describe_node(cfg, project, _LIVE_NAV_PANEL)
    except core.NodeNotFound:
        pytest.skip(
            f"no NavigationPanel at {_LIVE_NAV_PANEL!r} — set FTX_LIVE_NAV_PANEL "
            f"to an existing NavigationPanel path, or ensure the CI project creates one"
        )
    item_name = "ZzzU16NavItem"
    routed_path = _LIVE_NAV_PANEL + "/Panels/" + item_name
    rep = _report(cfg, project, [
        {"op": "create_widget", "screen": _LIVE_NAV_PANEL,
         "name": item_name, "widget_type": "NavigationPanelItem"},
        {"op": "set_property", "path": routed_path, "name": "Title", "value": "Tab"},
    ])
    assert rep["ok"] is True, rep
    warn_codes = [w["code"] for w in rep["warnings"]]
    assert "routed_into_collection" in warn_codes, (
        f"expected routed_into_collection warning; warnings: {rep['warnings']!r}"
    )


def test_validate_ops_create_container_not_renderable(live):
    """U16: create_widget with widget_type='Container' is rejected at VALIDATE
    time as not_renderable. Previously this refusal fired only at apply time,
    leaving the batch in a partial state with no rollback."""
    cfg, project = live
    parent = _LIVE_NODE.rsplit("/", 1)[0]
    rep = _report(cfg, project, [
        {"op": "create_widget", "screen": parent,
         "name": "ZzzU16Container", "widget_type": "Container"},
    ])
    assert rep["ok"] is False, rep
    codes = [e["code"] for e in rep["errors"]]
    assert "not_renderable" in codes, rep


def test_validate_ops_create_variable_then_set_value_is_clean(live):
    """Hypothetical-branch fix: create_variable followed by set_property
    name='Value' on the same (not-yet-live) node must validate CLEAN.

    Before the fix, the hypothetical branch hit unverifiable_property (or
    unknown_property if the CLR type resolved but didn't expose 'Value') because
    the validator treated every create_variable node as a widget and tried to
    reflect its properties via CLR type lookup.  create_variable always produces
    an IUAVariable, so 'Value' is the variable self-assign path — valid by
    definition regardless of the declared data type."""
    cfg, project = live
    parent = _LIVE_NODE.rsplit("/", 1)[0]
    var_name = "ZzzU16HypoVar"
    var_path = parent + "/" + var_name
    rep = _report(cfg, project, [
        {"op": "create_variable", "parent": parent,
         "name": var_name, "type": "Int32"},
        {"op": "set_property", "path": var_path, "name": "Value", "value": "42"},
    ])
    assert rep["ok"] is True, rep
    # Must produce no errors AND no unverifiable_property warning for op 1.
    codes_op1 = [
        e["code"] for e in rep.get("errors", []) + rep.get("warnings", [])
        if e.get("op_index") == 1
    ]
    assert "unverifiable_property" not in codes_op1, (
        "set_property name=Value on an in-batch create_variable should be CLEAN, "
        f"not warned; op_index=1 codes: {codes_op1!r}"
    )


# ---- Value property set_property — Tier-2 live contract --------------------
#
# Covers the four scenarios from the "Fix set_property name=Value on live
# variables" epic: (1) validate against an existing model variable → ok:True;
# (2) bridge_edit applies and reads back via describe_node; (3) in-batch
# create_variable + set_property name=Value is CLEAN (no unverifiable_property)
# — already covered by test_validate_ops_create_variable_then_set_value_is_clean
# above; (4) the IUAVariable self-assign shortcut does NOT accidentally reach
# widgets that have a real child "Value" property (e.g. SpinBox) — those
# IUAObjects follow the normal DeclaredPropertyGuard path and must still
# validate ok:True.


def test_validate_ops_value_on_existing_model_variable(live):
    """Tier-2: validate_ops on set_property name='Value' against an EXISTING
    (live, not hypothetical) model variable → ok: True, errors == [].

    Before the fix the validator hit DeclaredPropertyGuard on the live IUAVariable
    node and got unknown_property because 'Value' is not a CLR-declared property
    on the widget proxy — it is the IUAVariable's own value attribute, handled by
    a special early-return path.  This test pins the happy path: a real variable
    lives in the model, and validate_ops must confirm the op as clean without
    going anywhere near DeclaredPropertyGuard.

    The variable is created at test-start and deleted on exit so the project is
    always left clean (creates and deletes are live model mutations; they don't
    require a full Studio restart).
    """
    cfg, project = live
    parent = _LIVE_MODEL_PARENT
    var_name = "ZzzU16ValueValidateVar"
    var_path = parent + "/" + var_name

    # Create the variable so it is a live (existing) node when validate_ops runs.
    try:
        core.bridge_create_variable(cfg, project, var_name, parent, "Int32")
    except core.BridgeWriteFailed as exc:
        pytest.skip(
            f"could not create test variable at {var_path!r}: {exc} — "
            f"check that FTX_LIVE_MODEL_PARENT ({parent!r}) exists in the project"
        )

    try:
        rep = _report(cfg, project, [
            {"op": "set_property", "path": var_path, "name": "Value", "value": "99"},
        ])
        assert rep["ok"] is True, (
            f"validate_ops rejected set_property name=Value on existing model "
            f"variable {var_path!r}; errors: {rep.get('errors')!r}, "
            f"warnings: {rep.get('warnings')!r}. "
            "The IUAVariable self-assign path should be accepted unconditionally."
        )
        assert rep["errors"] == [], (
            f"validate_ops produced errors for set_property name=Value on existing "
            f"model variable; errors: {rep['errors']!r}"
        )
    finally:
        try:
            core.bridge_delete_node(cfg, project, var_path)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass  # best-effort cleanup; a stuck node does not fail this test


def test_bridge_edit_value_property_applies_and_reads_back(live):
    """Tier-2: bridge_edit of set_property name='Value' on a model variable
    applies correctly and the written value reads back through describe_node.

    This is the APPLY-path complement to test_validate_ops_value_on_existing_model_variable:
    where that test only validates (no mutation), this one actually writes through
    bridge_edit and verifies the round-trip.  describe_node on the PARENT exposes
    each IUAVariable child with its current value, so the newly written integer
    must appear there.

    Variable: Int32, written value 77, expected describe_node entry
    "77 (Int32)" (UAValue.ToString() format used by the bridge's ValueString).
    """
    cfg, project = live
    parent = _LIVE_MODEL_PARENT
    var_name = "ZzzU16ValueWriteVar"
    var_path = parent + "/" + var_name

    try:
        core.bridge_create_variable(cfg, project, var_name, parent, "Int32")
    except core.BridgeWriteFailed as exc:
        pytest.skip(
            f"could not create test variable at {var_path!r}: {exc} — "
            f"check that FTX_LIVE_MODEL_PARENT ({parent!r}) exists in the project"
        )

    try:
        # Apply the write through bridge_edit (not dry_run).
        out = core.bridge_edit(cfg, project, [
            {"op": "set_property", "path": var_path, "name": "Value", "value": "77"},
        ])
        assert out.get("state") == "succeeded", (
            f"bridge_edit did not apply; state={out.get('state')!r}: {out!r}"
        )
        assert out.get("applied") == 1, (
            f"bridge_edit reported applied={out.get('applied')!r}, expected 1: {out!r}"
        )

        # Read back via describe_node on the PARENT.  Each IUAVariable child of
        # the parent appears in the `properties` list with its current value in
        # "VALUE (DataType)" format (UAValue.ToString()), wrapped by _untrusted.
        parent_node = core.describe_node(cfg, project, parent)
        props = {p["name"]: p.get("value") for p in (parent_node.get("properties") or [])}
        assert "77" in (props.get(var_name) or ""), (
            f"set_property name=Value did not round-trip; describe_node({parent!r}) "
            f"returned {var_name}={props.get(var_name)!r}, expected it to contain '77'. "
            "Confirm the bridge's SetPropertyInline self-assign path wrote to the "
            "variable's own Value attribute."
        )
    finally:
        try:
            core.bridge_delete_node(cfg, project, var_path)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass


@pytest.mark.parametrize("dtype,value,want", [
    ("Int64", "123456789012345", "123456789012345"),
    ("UInt64", "4294967295", "4294967295"),
    ("Int64", "-9007199254740993", "-9007199254740993"),
])
def test_set_value_keeps_64bit_width(live, dtype, value, want):
    """1.0.8 battle test: the Value write cast every integer to (int), so
    Int64/UInt64 wrapped mod 2^32 with ok:true (123456789012345 read back as
    -2045911175; UInt64 4294967295 as 18446744073709551615)."""
    cfg, project = live
    parent = _LIVE_MODEL_PARENT
    var_name = "Zzz64Bit" + dtype
    var_path = parent + "/" + var_name
    core.bridge_create_variable(cfg, project, var_name, parent, dtype)
    try:
        out = core.bridge_edit(cfg, project, [
            {"op": "set_property", "path": var_path, "name": "Value", "value": value},
        ])
        assert out.get("state") == "succeeded", out
        props = {p["name"]: p.get("value")
                 for p in (core.describe_node(cfg, project, parent).get("properties") or [])}
        raw = re.sub(r"</?untrusted[^>]*>", "", props.get(var_name) or "")
        got = raw.split(" (")[0].replace(",", "")
        assert got == want, f"{dtype} {value!r} read back as {props.get(var_name)!r}"
    finally:
        try:
            core.bridge_delete_node(cfg, project, var_path)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass


@pytest.mark.parametrize("dtype,value", [
    ("Int16", "70000"), ("Byte", "-1"), ("UInt64", "-1"), ("Int64", "99999999999999999999"),
])
def test_set_value_out_of_range_is_refused(live, dtype, value):
    cfg, project = live
    parent = _LIVE_MODEL_PARENT
    var_name = "ZzzRange" + dtype
    var_path = parent + "/" + var_name
    core.bridge_create_variable(cfg, project, var_name, parent, dtype)
    try:
        out = core.bridge_edit(cfg, project, [
            {"op": "set_property", "path": var_path, "name": "Value", "value": value},
        ])
        assert out.get("applied") in (0, None), out
        assert "out of range" in json.dumps(out), out
    finally:
        try:
            core.bridge_delete_node(cfg, project, var_path)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass


def test_validate_ops_value_on_widget_with_value_child_unchanged(live):
    """Tier-2: validate_ops on set_property name='Value' for a SpinBox widget
    (an IUAObject with a real child 'Value' property) is accepted — ok: True,
    errors == [].  Regression guard for the IUAVariable scope restriction.

    The fix that enables set_property name=Value on IUAVariables adds an early
    return in the validator ONLY when the resolved node IS an IUAVariable
    (model variable).  Widgets like SpinBox are IUAObjects, not IUAVariables —
    the early return must NOT fire for them.  Their 'Value' goes through
    DeclaredPropertyGuard instead, where it passes because SpinBox's CLR proxy
    (FTOptix.UI.SpinBox) declares a 'Value' property.  The guard's existing
    acceptance must be unchanged after the fix.

    A temporary SpinBox is created at test-start and deleted on exit so the
    project is left clean.  The test skips gracefully when 'SpinBox' is not a
    known type in this Studio build.
    """
    cfg, project = live
    scratch = _LIVE_SCRATCH_PANEL
    child_name = "ZzzU16SpinBoxValueGuard"
    child_path = scratch + "/" + child_name

    # Confirm the scratch panel exists; skip if absent (misconfigured env).
    try:
        core.describe_node(cfg, project, scratch)
    except core.NodeNotFound:
        pytest.skip(
            f"no node at {scratch!r} — set FTX_LIVE_SCRATCH_PANEL to an existing "
            "PanelType/Screen path, or ensure FTX_LIVE_NODE resolves to a node "
            "whose parent is a PanelType"
        )

    # Best-effort pre-clean from any prior failed run.
    try:
        core.bridge_delete_node(cfg, project, child_path)
    except (core.BridgeWriteFailed, core.NodeNotFound):
        pass

    # Create the SpinBox so it is a live node when validate_ops runs.
    try:
        out = core.bridge_create_widget(cfg, project, scratch, child_name, "SpinBox")
    except core.BridgeWriteFailed as exc:
        pytest.skip(
            f"bridge_create_widget('SpinBox') failed: {exc} — "
            "SpinBox may not be available in this Studio build; "
            "set FTX_LIVE_SCRATCH_PANEL if the default panel is wrong"
        )

    try:
        assert out.get("ok") is True, (
            f"create_widget('SpinBox') returned ok:False: {out!r}"
        )

        # Validate set_property name=Value on the live SpinBox.
        # SpinBox is IUAObject (not IUAVariable), so the validator must NOT
        # take the IUAVariable self-assign early-return shortcut.  Instead it
        # goes through DeclaredPropertyGuard, which passes because SpinBox
        # declares 'Value' in FTOptix.UI.
        rep = _report(cfg, project, [
            {"op": "set_property", "path": child_path,
             "name": "Value", "value": "50"},
        ])
        assert rep["ok"] is True, (
            f"validate_ops rejected set_property name=Value on SpinBox "
            f"({child_path!r}), which is an IUAObject with a real 'Value' child. "
            "Regression guard: the IUAVariable self-assign shortcut must not "
            "affect IUAObjects.  DeclaredPropertyGuard should have passed since "
            "FTOptix.UI.SpinBox declares 'Value'. "
            f"errors: {rep.get('errors')!r}"
        )
        assert rep["errors"] == [], (
            f"unexpected errors for SpinBox set_property name=Value: {rep['errors']!r}"
        )
    finally:
        try:
            core.bridge_delete_node(cfg, project, child_path)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass


def test_reorder_graphic_child_live(live):
    """Tier-2: reorder a graphic child UP, DOWN, to front, and to back in a
    real PanelType, verify the achieved graphic index via describe_node after
    each move.

    Pins the 25->16 landing-at-15 regression: the pre-fix C# code used raw
    Children indices (including IUAVariable/property children) instead of the
    GraphicChildren / GraphicIndexOf index space, so a reorder toward a lower
    index landed one position earlier than requested whenever properties were
    interspersed before the target position. The fix changes the C# bridge to
    use GraphicChildren() + GraphicIndexOf(); this test verifies the contract
    on real model data.

    Environment (all env-overridable):
        FTX_LIVE_REORDER_PANEL  – parent PanelType path (default: UI/MainWindow)
        FTX_LIVE_REORDER_CHILD  – child node to reorder (default:
                                   UI/MainWindow/Rectangle1)
    The panel must contain at least 3 graphic children so that UP and DOWN
    both have room.  If fewer than 3 graphic children are found the test skips
    rather than silently passing a degenerate scenario."""
    cfg, project = live
    panel_path = _LIVE_REORDER_PANEL
    child_path = _LIVE_REORDER_CHILD

    # --- confirm the panel is reachable ----------------------------------
    try:
        core.describe_node(cfg, project, panel_path)
    except core.NodeNotFound:
        pytest.skip(
            f"no node at {panel_path!r} — set FTX_LIVE_REORDER_PANEL to an "
            "existing PanelType path with graphic children"
        )

    def _graphic_index(child_browse_name: str) -> int:
        """Current graphic index of child_browse_name under panel_path.

        describe_node separates IUAVariable children (properties) into
        ``properties`` and graphic children into ``children``, so the
        ``children`` list IS the graphic-children list — the index within
        it is the graphic index used by bridge_reorder_node."""
        parent = core.describe_node(cfg, project, panel_path)
        gkids = [c["browse_name"] for c in (parent.get("children") or [])]
        try:
            return gkids.index(child_browse_name)
        except ValueError:
            return -1

    child_browse = child_path.rsplit("/", 1)[-1]

    # --- preflight: ensure the child exists and is a graphic child -------
    start_idx = _graphic_index(child_browse)
    if start_idx < 0:
        pytest.skip(
            f"{child_path!r} is not a graphic child of {panel_path!r} — "
            "set FTX_LIVE_REORDER_CHILD to a graphic (non-IUAVariable) child "
            "of FTX_LIVE_REORDER_PANEL"
        )

    parent_desc = core.describe_node(cfg, project, panel_path)
    n_gkids = len(parent_desc.get("children") or [])
    if n_gkids < 3:
        pytest.skip(
            f"{panel_path!r} has only {n_gkids} graphic child(ren) — need ≥ 3 "
            "for UP/DOWN/front/back reorder tests to have room"
        )

    # === reorder to front ================================================
    out_front = core.bridge_reorder_node(cfg, project, child_path,
                                         position="front")
    assert out_front.get("ok") is True, f"reorder front failed: {out_front}"
    assert out_front.get("space") == "graphic_children", out_front
    assert out_front.get("achieved") is not None, out_front
    idx_after_front = _graphic_index(child_browse)
    assert idx_after_front == n_gkids - 1, (
        f"after position=front expected graphic index {n_gkids - 1}, "
        f"got {idx_after_front} (describe_node) — "
        f"bridge reported achieved={out_front.get('achieved')!r}"
    )
    assert out_front["achieved"] == idx_after_front, (
        "bridge-reported achieved must match the index read back via describe_node"
    )

    # === reorder to back =================================================
    out_back = core.bridge_reorder_node(cfg, project, child_path,
                                        position="back")
    assert out_back.get("ok") is True, f"reorder back failed: {out_back}"
    idx_after_back = _graphic_index(child_browse)
    assert idx_after_back == 0, (
        f"after position=back expected graphic index 0, "
        f"got {idx_after_back} (describe_node) — "
        f"bridge reported achieved={out_back.get('achieved')!r}"
    )
    assert out_back["achieved"] == idx_after_back

    # === reorder DOWN (index 1 — one step toward the front) ==============
    target_down = 1
    out_down = core.bridge_reorder_node(cfg, project, child_path,
                                        index=target_down)
    assert out_down.get("ok") is True, f"reorder index={target_down} failed: {out_down}"
    assert out_down.get("space") == "graphic_children", out_down
    assert out_down["requested"] == target_down, out_down
    idx_after_down = _graphic_index(child_browse)
    # Regression: before the GraphicChildren/GraphicIndexOf fix, reordering
    # toward a lower index from back (0) overshot/undershot when IUAVariable
    # children were interspersed. The achieved index must match requested.
    assert idx_after_down == target_down, (
        f"after index={target_down} expected graphic index {target_down}, "
        f"got {idx_after_down} (describe_node) — "
        f"bridge reported achieved={out_down.get('achieved')!r}. "
        "This may be the 25->16 landing-at-15 off-by-one regression."
    )
    assert out_down["achieved"] == idx_after_down, (
        "bridge-reported achieved must match the index read back via describe_node"
    )

    # === reorder UP (index n-2 — one step before the very front) =========
    target_up = n_gkids - 2
    out_up = core.bridge_reorder_node(cfg, project, child_path,
                                      index=target_up)
    assert out_up.get("ok") is True, f"reorder index={target_up} failed: {out_up}"
    assert out_up["requested"] == target_up, out_up
    idx_after_up = _graphic_index(child_browse)
    assert idx_after_up == target_up, (
        f"after index={target_up} expected graphic index {target_up}, "
        f"got {idx_after_up} (describe_node) — "
        f"bridge reported achieved={out_up.get('achieved')!r}. "
        "This may be the 25->16 landing-at-15 off-by-one regression."
    )
    assert out_up["achieved"] == idx_after_up


def test_grid_layout_props_create_set_delete(live):
    """Tier-2: create a GridLayoutProperties child under a GridLayout widget,
    assert the result namespace is FTOptix.UI and ui_namespace_child is True,
    set RowStart and ColumnStart on the child, then delete it.

    Pins the GridLayoutProperties namespace fix introduced in bridge ≥ 1.0.8:
    a plain create_widget / create_object call for a child named
    "GridLayoutProperties" used to land the node in the PROJECT namespace,
    where RowStart / ColumnStart writes are silently accepted but equally
    silently ignored at render time. TryCreateUiNamespaceChild now routes
    the creation so the node resides in FTOptix.UI.

    Environment:
        FTX_LIVE_GRID_PANEL – path to a GridLayout widget
                               (default: UI/MainWindow/GridLayout1).
    The CI setup (the live-CI setup script) creates GridLayout1 before this suite
    runs and sets FTX_LIVE_GRID_PANEL accordingly; override when the live
    project already contains a GridLayout at a different path.
    """
    cfg, project = live
    grid_path = _LIVE_GRID_PANEL
    child_path = grid_path + "/GridLayoutProperties"

    # --- confirm the parent GridLayout exists --------------------------------
    try:
        core.describe_node(cfg, project, grid_path)
    except core.NodeNotFound:
        pytest.skip(
            f"no GridLayout at {grid_path!r} — set FTX_LIVE_GRID_PANEL to an "
            "existing GridLayout widget path, or ensure the CI project creates one"
        )

    # --- ensure no leftover child from a previous failed run ----------------
    try:
        core.bridge_delete_node(cfg, project, child_path)
    except (core.BridgeWriteFailed, core.NodeNotFound):
        pass  # absent is fine; this is a best-effort pre-clean

    # === create GridLayoutProperties =========================================
    out = core.bridge_create_widget(
        cfg, project, grid_path, "GridLayoutProperties", "GridLayoutProperties")

    assert out.get("ok") is True, f"create GridLayoutProperties failed: {out}"
    assert out.get("ui_namespace_child") is True, (
        "bridge_create_widget for 'GridLayoutProperties' must set "
        "ui_namespace_child:True — the FTOptix.UI namespace fix is not active; "
        f"got: {out!r}"
    )
    assert out.get("namespace") == "FTOptix.UI", (
        f"GridLayoutProperties must land in FTOptix.UI namespace, "
        f"got namespace={out.get('namespace')!r}; full response: {out!r}"
    )
    assert out.get("created_path") == child_path, (
        f"unexpected created_path: {out.get('created_path')!r}, expected {child_path!r}"
    )

    # === set RowStart and ColumnStart =========================================
    # These writes prove the child ACCEPTS the grid-position properties. They do
    # NOT prove the namespace: a project-qualified GridLayoutProperties accepts
    # and reads them back just as cleanly (measured 2026-09-05 on CI run 20's
    # leftover child) and only differs at RENDER time, which no model read can
    # see. The namespace verdict is the bridge's own QualifiedBrowseName
    # read-back, asserted above via namespace == "FTOptix.UI".
    #
    # describe_node values are `<value> (<DataType>)` wrapped in core's
    # `<untrusted source="bridge">` envelope, so the round-trip check is a
    # containment test on the formatted value, not equality with the raw string.
    try:
        r_row = core.bridge_set_property(
            cfg, project, child_path, "RowStart", "1")
        assert r_row.get("ok") is True, (
            f"set RowStart failed: {r_row!r}"
        )

        r_col = core.bridge_set_property(
            cfg, project, child_path, "ColumnStart", "2")
        assert r_col.get("ok") is True, (
            f"set ColumnStart failed: {r_col!r}"
        )

        # Read the child back and verify the properties round-trip.
        node = core.describe_node(cfg, project, child_path)
        props = {p["name"]: p.get("value") for p in (node.get("properties") or [])}
        assert "1 (Int32)" in (props.get("RowStart") or ""), (
            f"RowStart write did not round-trip; describe_node returned "
            f"RowStart={props.get('RowStart')!r}, expected it to contain "
            "'1 (Int32)'"
        )
        assert "2 (Int32)" in (props.get("ColumnStart") or ""), (
            f"ColumnStart write did not round-trip; got "
            f"{props.get('ColumnStart')!r}, expected it to contain '2 (Int32)'"
        )
    finally:
        # === delete the child (cleanup) ======================================
        try:
            del_out = core.bridge_delete_node(cfg, project, child_path)
            assert del_out.get("ok") is True, (
                f"delete GridLayoutProperties failed: {del_out!r}"
            )
        except core.BridgeWriteFailed as exc:
            pytest.fail(
                f"cleanup: could not delete {child_path!r}: {exc}. "
                "Delete it manually before re-running."
            )


def test_validate_ops_and_dry_run_mutate_nothing(live):
    """The whole point: validation is side-effect free, and bridge_edit's
    dry_run applies nothing even when the report is clean."""
    cfg, project = live

    def width():
        node = core.describe_node(cfg, project, _LIVE_NODE)
        return next((p.get("value") for p in node.get("properties") or []
                     if p.get("name") == "Width"), None)

    before = width()
    parent = _LIVE_NODE.rsplit("/", 1)[0]
    ops = [
        {"op": "create_widget", "screen": parent,
         "name": _HYPO.rsplit("/", 1)[1], "widget_type": "Rectangle"},
        {"op": "set_property", "path": _LIVE_NODE, "name": "Width", "value": "999"},
    ]

    out = core.bridge_edit(cfg, project, ops, dry_run=True)
    assert out["state"] == "validated" and out["applied"] == 0, out
    assert out["dry_run"] is True

    assert width() == before, "dry_run changed a live property value"
    kids = [c.get("browse_name")
            for c in core.describe_node(cfg, project, parent).get("children") or []]
    assert _HYPO.rsplit("/", 1)[1] not in kids, "dry_run created a node"


# ---- VirtualKeyboard* catalog (live, Tier-2) --------------------------------
# Verifies the ResolveUiTypeId browse-name-walk fix against a real Studio bridge.
# * list_ui_types returns an empty (or absent) misaligned list — all builtins
#   resolve cleanly once the browse-name walk is in place.
# * describe_type("VirtualKeyboardTextButton") returns a non-empty property list
#   with the correct browse name (no type_not_found from the old bare-GetField path).
# * create_widget("VirtualKeyboardTextButton") into a scratch panel succeeds and
#   the widget is deleted again (regression from the bare-GetField pre-fix).
#
# Env overrides (all optional):
#   FTX_LIVE_VK_TYPE         – VirtualKeyboard widget type (default: VirtualKeyboardTextButton)
#   FTX_LIVE_SCRATCH_PANEL   – PanelType/Screen for the temp create_widget child
#                              (default: parent of FTX_LIVE_NODE, e.g. UI/MainWindow)

_LIVE_VK_TYPE = os.environ.get("FTX_LIVE_VK_TYPE", "VirtualKeyboardTextButton")
_LIVE_SCRATCH_PANEL = os.environ.get(
    "FTX_LIVE_SCRATCH_PANEL",
    "/".join(_LIVE_NODE.split("/")[:-1]),  # parent of _LIVE_NODE
)
# ---- Expression literal rejection / runtime-log acceptance ----
# Three Tier-2 live tests covering the design-time/runtime gap for invalid
# ExpressionEvaluator literals:
#
#   1. validate_expression on the bad expression → valid:False + invalid_literal
#      + fix value 0xFFFFF809  (literal detection is read-only)
#   2. attach_expression with the bad expression → BridgeWriteFailed + no new
#      ExpressionEvaluator child visible via describe_node  (gate is load-bearing)
#   3. corrected 0xAARRGGBB expression attaches, emulator restarts, runtime log
#      contains no "Invalid Expression parameter" for the test widget
#      (gated on FTX_LIVE_HMI=1; the live-CI setup script arms the emulator)
#
# The expression under test:  if(true, #FFF809, 0xFFF0F0F0)
#   • #FFF809  — CSS-style RGB shorthand, NOT a valid ExpressionEvaluator
#     literal; the validator converts it to 0xFFFFF809 (AABBGGRR, AA=FF)
#   • 0xFFF0F0F0 — already correct AARRGGBB literal
#
# Corrected expression:  if(true, 0xFFFFF809, 0xFFF0F0F0)
#
# No {N} placeholder: measured live 2026-09-05 (CI run 43, aee56eb) that the
# bridge validates placeholder/source-count BEFORE it checks literals, so a
# placeholder with 0 sources fails as bad_expression before the literal gate
# is ever reached -- the opposite order this file originally assumed. Using a
# bare `true` condition exercises the literal check (and the boolean-
# spelling rule) without needing any bound source.
#
# Override any of these with the corresponding FTX_LIVE_EXPR_* env var.
_LIVE_EXPR_BAD = os.environ.get(
    "FTX_LIVE_EXPR_BAD", "if(true, #FFF809, 0xFFF0F0F0)"
)
_LIVE_EXPR_GOOD = os.environ.get(
    "FTX_LIVE_EXPR_GOOD", "if(true, 0xFFFFF809, 0xFFF0F0F0)"
)
# Source list for a {N} placeholder, only needed if FTX_LIVE_EXPR_BAD/GOOD are
# overridden with an expression that has one.
_LIVE_EXPR_SOURCE = os.environ.get("FTX_LIVE_EXPR_SOURCE", "")
# Color-family property on FTX_LIVE_NODE to use for attach tests. FillColor is
# declared on every Rectangle; override when FTX_LIVE_NODE is a different type.
_LIVE_EXPR_PROP = os.environ.get("FTX_LIVE_EXPR_PROP", "FillColor")
# ---- wire_event re-wire round-trip constants ----------------
# Any existing node is used as the method object for the ChangePanel wire; the
# bridge only requires that the PARENT node resolves (method name is stored as-is),
# so a Rectangle widget works even though it has no real ChangePanel method.
# Rectangle1 is seeded by the standard CI setup (the live-CI setup script), so the
# default works out of the box. Override when the live project has it elsewhere.
_LIVE_REWIRE_METHOD_OBJ = os.environ.get(
    "FTX_LIVE_REWIRE_METHOD_OBJ",
    _LIVE_SCRATCH_PANEL + "/Rectangle1",
)

# ---- Normalized-coordinate click round-trip (Tier-2, HMI gate) ---------------
# 1.0.8: optix_interact was passing normalized coords raw to CDP instead of
# converting them via _resolve_point.  This test pins the fixed contract on the
# live test box: click at normalized fractions → CSS pixels → button fires →
# the model variable toggles → a variable-BOUND widget on the canvas repaints.
#
# The ground truth is a screenshot-region diff, NOT describe_node.  A
# ToggleVariable command executes in the EMULATOR process (FTOptixRuntime),
# which holds its own copy of the model; the design-time bridge that
# describe_node talks to lives inside Studio and only ever reports the
# CONFIGURED value.  Measured 2026-09-05 on CI_204acdb: a click that
# demonstrably flipped the runtime variable (the bound Switch repainted) left
# describe_node reporting `False (Bool)` before and after.  Any live test that
# asserts a runtime write via describe_node can therefore never pass.
#
# Gate: FTX_LIVE_HMI=1 (emulator running).  The CI setup (the live-CI setup script)
# resizes UI/MainWindow to 1280x720 (so the rendered window fills the CDP CSS
# viewport 1:1), creates ClickToggleBtn (Button at 40,40, 1200x600) wired
# MouseClickEvent → ToggleVariable → Model/ClickToggle, creates ToggleView
# (Switch at 40,670, 120x28, Checked bound Read to Model/ClickToggle), starts
# the emulator with `optix_emulator action=run`, and sets FTX_LIVE_HMI=1.
#
# Env overrides (all optional):
#   FTX_LIVE_HMI         – "1" when the emulator is running (gating condition)
#   FTX_LIVE_TOGGLE_VAR  – model variable the button toggles
#                           (default: Model/ClickToggle) — existence pre-check only
#   FTX_LIVE_TOGGLE_X    – normalized x of the toggle button (default: 0.5)
#   FTX_LIVE_TOGGLE_Y    – normalized y of the toggle button (default: 0.5)
#   FTX_LIVE_TOGGLE_IND  – path of the bound indicator widget
#                           (default: UI/MainWindow/ToggleView)
#   FTX_LIVE_TOGGLE_REGION – "x,y,w,h" CSS-pixel capture region over that
#                           indicator (default: "36,664,128,40").  Must NOT
#                           overlap the button: a click paints a focus ring on
#                           the button border, which would diff even on a miss.
#   FTX_LIVE_HMI_URL     – Chrome navigation target before the click
#                           (default: http://localhost:8081/)

_LIVE_TOGGLE_VAR = os.environ.get("FTX_LIVE_TOGGLE_VAR", "Model/ClickToggle")
_LIVE_TOGGLE_X = float(os.environ.get("FTX_LIVE_TOGGLE_X", "0.5"))
_LIVE_TOGGLE_Y = float(os.environ.get("FTX_LIVE_TOGGLE_Y", "0.5"))
_LIVE_TOGGLE_IND = os.environ.get("FTX_LIVE_TOGGLE_IND", "UI/MainWindow/ToggleView")
_LIVE_TOGGLE_REGION = [
    float(v) for v in
    os.environ.get("FTX_LIVE_TOGGLE_REGION", "36,664,128,40").split(",")
]
_LIVE_HMI_URL = os.environ.get("FTX_LIVE_HMI_URL", "http://localhost:8081/")


def test_list_ui_types_no_unreconciled_misaligned_live(live):
    """Every catalog entry the bridge CAN reconcile is reconciled; the only
    misaligned entries left are the ones the SDK genuinely cannot align.

    Two distinct causes hide behind one symptom (`name != browse_name`), and
    only one of them is a bug:

    * a STALE field constant — the compiled `FTOptix.UI.ObjectTypes` NodeId
      points at an unrelated node in the loaded module, so the catalog answered
      with a stranger's browse name.  Measured 2026-09-05 on FTOptix.UI
      21.2.0.89: `VirtualKeyboardTextButton` -> 8/1398 -> the DataType
      `VirtualKeyboardTypeEnum`, `VirtualKeyboardPanel` -> 8/1392 -> the
      Variable `ContentX`, `VirtualKeyboardSymbolButton` -> the Variable
      `Locales`.  The ResolveUiTypeId browse-name walk finds the real type node
      and the entry aligns; the bug is fixed.
    * a field constant NAMED differently from the node it correctly points at.
      `UserValueChangedEvent` resolves to 8/690, an ObjectType under
      BaseEventType whose BrowseName is `UserValueChanged` — the right node,
      spelled without the `Event` suffix the constant carries.  NO node named
      `UserValueChangedEvent` exists, so no walk can ever align it, and
      reporting it in `misaligned` is exactly what that field is for.

    So the contract is not "misaligned is empty" (unachievable on this SDK) but
    "nothing that could be reconciled was left unreconciled": every remaining
    entry must be one whose `name` resolves to NO type at all."""
    cfg, project = live
    out = core.list_ui_types(cfg, project)
    assert out["source"] == "bridge"
    misaligned = out.get("misaligned", [])

    # The VirtualKeyboard* block is the stale-constant class the fix targets:
    # it must be gone from the misaligned list entirely.
    vk_left = [e for e in misaligned if str(e.get("name", "")).startswith("VirtualKeyboard")]
    assert vk_left == [], (
        "VirtualKeyboard* types are still misaligned — the ResolveUiTypeId "
        f"browse-name walk did not reconcile them: {vk_left!r}"
    )

    # Anything still listed must be genuinely unresolvable BY THAT NAME: if
    # describe_type(name) answers, the walk found a node and the catalog had no
    # business reporting the entry as misaligned.
    for entry in misaligned:
        name = entry["name"]
        try:
            schema = core.describe_type(cfg, project, name)
        except core.NodeNotFound:
            continue  # genuinely no node carries this name — legitimate
        pytest.fail(
            f"catalog reports {name!r} as misaligned (browse_name="
            f"{entry.get('browse_name')!r}) yet describe_type({name!r}) resolves "
            f"to browse_name={schema.get('browse_name')!r} — the bridge could "
            "have reconciled this entry and did not"
        )


def test_virtual_keyboard_types_resolve_by_browse_name_live(live):
    """The VirtualKeyboard* block resolves, and says HOW it resolved.

    `resolved_by:"browse_name"` is the visible half of the fix: a stale field
    constant was corrected by the type-tree walk rather than silently.  An entry
    that resolves without that marker would mean the constant aligned after all
    and this test is watching the wrong thing."""
    cfg, project = live
    out = core.list_ui_types(cfg, project)
    by_name = {t["name"]: t for t in out["types"]}
    for name in ("VirtualKeyboardTextButton", "VirtualKeyboardSymbolButton",
                 "VirtualKeyboardPanel"):
        assert name in by_name, f"{name!r} missing from the catalog: {sorted(by_name)!r}"
        entry = by_name[name]
        # Lean-out drops browse_name when it equals name, so its ABSENCE is the
        # aligned signal.
        assert "browse_name" not in entry, (
            f"{name!r} still reports a foreign browse_name "
            f"{entry.get('browse_name')!r} — the catalog was not corrected"
        )
        assert entry.get("resolved_by") == "browse_name", (
            f"{name!r} aligned without resolved_by:'browse_name'; the bridge "
            f"must mark a walk-corrected entry: {entry!r}"
        )


def test_describe_type_virtual_keyboard_text_button_live(live):
    """describe_type('VirtualKeyboardTextButton') returns a non-empty property
    list with the correct browse name (live contract).

    Before the ResolveUiTypeId fix the C# bridge returned type_not_found (404)
    for VirtualKeyboardTextButton because its ObjectTypes field constant does
    not match its live BrowseName and the old code used only GetField, not the
    browse-name walk.  With the fix this call must succeed.

    Env: FTX_LIVE_VK_TYPE (default: VirtualKeyboardTextButton)."""
    cfg, project = live
    vk_type = _LIVE_VK_TYPE
    try:
        schema = core.describe_type(cfg, project, vk_type)
    except core.NodeNotFound:
        pytest.fail(
            f"describe_type({vk_type!r}) raised NodeNotFound on the live bridge — "
            "the ResolveUiTypeId browse-name walk fix is missing from the build "
            f"(set FTX_LIVE_VK_TYPE if a different VirtualKeyboard* type should be used)"
        )
    assert schema["source"] == "bridge"
    props = schema.get("properties") or []
    assert props, (
        f"describe_type({vk_type!r}) returned no properties; the type was resolved "
        "but reflection is empty — check the bridge's per-type schema reflection"
    )
    assert schema.get("browse_name"), (
        f"describe_type({vk_type!r}) returned no browse_name; "
        f"got schema keys: {sorted(schema)!r}"
    )


def test_create_delete_virtual_keyboard_text_button_live(live):
    """create_widget(widget_type='VirtualKeyboardTextButton') into a scratch panel
    succeeds, and the created widget is deleted again.

    Before the ResolveUiTypeId fix WriteWidgetInline used a bare GetField which
    returns null for VirtualKeyboard* types (field name != BrowseName) and the
    bridge responded type_not_found.  With the fix it uses ResolveUiTypeId
    (browse-name walk) and succeeds.

    The scratch panel defaults to the parent of FTX_LIVE_NODE (e.g.
    UI/MainWindow); override with FTX_LIVE_SCRATCH_PANEL.  The temp widget name
    (ZzzVKTestWidget) is deleted in a finally block so the project is always left
    clean.

    Env: FTX_LIVE_VK_TYPE, FTX_LIVE_SCRATCH_PANEL."""
    cfg, project = live
    vk_type = _LIVE_VK_TYPE
    scratch_panel = _LIVE_SCRATCH_PANEL
    child_name = "ZzzVKTestWidget"
    child_path = scratch_panel + "/" + child_name

    # Confirm the scratch panel exists; skip gracefully if absent.
    try:
        core.describe_node(cfg, project, scratch_panel)
    except core.NodeNotFound:
        pytest.skip(
            f"no node at {scratch_panel!r} — set FTX_LIVE_SCRATCH_PANEL to an "
            "existing PanelType/Screen path, or ensure FTX_LIVE_NODE resolves "
            "to a node whose parent is a PanelType"
        )

    # Best-effort pre-clean: remove any leftover child from a prior failed run.
    try:
        core.bridge_delete_node(cfg, project, child_path)
    except (core.BridgeWriteFailed, core.NodeNotFound):
        pass

    # Create the VirtualKeyboard widget — must not raise type_not_found.
    try:
        out = core.bridge_create_widget(
            cfg, project, scratch_panel, child_name, vk_type)
    except core.BridgeWriteFailed as exc:
        pytest.fail(
            f"bridge_create_widget({vk_type!r}) failed: {exc} — the "
            "ResolveUiTypeId browse-name walk fix is missing from the live bridge"
        )
    finally:
        # Attempt cleanup regardless of how the create went.
        try:
            core.bridge_delete_node(cfg, project, child_path)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass

    assert out.get("ok") is True, (
        f"create_widget({vk_type!r}) returned ok:False: {out!r}"
    )
    assert out.get("created_path") is not None, (
        f"create_widget({vk_type!r}) missing created_path: {out!r}"
    )


# ---- 1.0.8 normalized-coordinate click round-trip (Tier-2, HMI gate) ------

def test_normalized_click_round_trip(live, tmp_path):
    """Tier-2 HMI-interact: click a known button with NORMALIZED coordinates and
    prove the click LANDED by diffing a screenshot region that contains nothing
    but a widget bound to the variable that button toggles.

    The assertion is a PIXEL CHANGE in that region, not state=="succeeded" —
    proving that:
      (a) the normalized-coordinate conversion is end-to-end correct (0.5 is
          multiplied by the CSS viewport width/height, not passed raw),
      (b) the click actually landed on the button (wrong coords -> miss -> the
          region stays byte-identical; measured as a negative control on
          2026-09-05, a click at 0.99/0.99 left the region unchanged),
      (c) the ToggleVariable command executed in the runtime and the binding
          repainted the indicator.

    WHY NOT describe_node (this test's original, unpassable form): a
    ToggleVariable command executes inside the EMULATOR process
    (FTOptixRuntime), which owns its own copy of the model. describe_node
    talks to the design-time bridge hosted in Studio, which only ever reports
    the CONFIGURED value. Measured 2026-09-05 on CI_204acdb: a click that
    demonstrably flipped the runtime variable (the bound Switch repainted
    on-canvas) left describe_node reporting `False (Bool)` both before and
    after. Runtime state is simply not readable from the design-time bridge —
    do not "fix" this test by putting the describe_node read back.

    WHY THE REGION MUST NOT TOUCH THE BUTTON: a click paints a focus ring on
    the button's border, so a region overlapping the button diffs even on a
    click that toggles nothing. FTX_LIVE_TOGGLE_REGION is deliberately below
    the seeded button (which ends at y=640) and over the seeded Switch only.

    1.0.8 root cause: cdp_click_runtime passed (x, y) straight to
    Input.dispatchMouseEvent without routing through _resolve_point, so a
    normalized 0.5 was dispatched at CSS pixel 0.5 (top-left corner) rather
    than at 640 (viewport mid-point on a 1280-wide canvas). The fix routes
    every click and fill through _resolve_point after set_viewport — the same
    path _cdp_capture_once and route replay already used.

    Requires FTX_LIVE_HMI=1 (emulator running, Chrome navigable to
    FTX_LIVE_HMI_URL). the live-CI setup script seeds the scaffolding:
      * Model/ClickToggle              — Boolean variable (initial value False)
      * UI/MainWindow                  — resized to 1280x720 so the rendered
                                         window fills the CSS viewport 1:1
      * UI/MainWindow/ClickToggleBtn   — Button at (40,40) 1200x600,
                                         MouseClickEvent -> ToggleVariable
      * UI/MainWindow/ToggleView       — Switch at (40,670) 120x28, Checked
                                         bound (Read) to Model/ClickToggle
      * emulator started via optix_emulator action=run, FTX_LIVE_HMI=1 set

    See the module-level block above this test for every env override.
    """
    if os.environ.get("FTX_LIVE_HMI") != "1":
        pytest.skip(
            "HMI-interact tests: set FTX_LIVE_HMI=1 with the emulator running "
            "on FTX_LIVE_PROJECT and Chrome accessible at FTX_LIVE_HMI_URL "
            "(the live-CI setup script seeds the scaffolding and sets this flag)"
        )

    cfg, project = live
    var_path = _LIVE_TOGGLE_VAR
    click_x = _LIVE_TOGGLE_X
    click_y = _LIVE_TOGGLE_Y
    hmi_url = _LIVE_HMI_URL
    region = _LIVE_TOGGLE_REGION

    # ---- confirm the seeded scaffolding exists (skip, don't fail) -----------
    if "/" not in var_path:
        pytest.skip(
            f"FTX_LIVE_TOGGLE_VAR={var_path!r} has no '/'; "
            "expected a path like 'Model/ClickToggle'"
        )
    var_parent, var_leaf = var_path.rsplit("/", 1)
    try:
        parent_node = core.describe_node(cfg, project, var_parent)
    except core.NodeNotFound:
        pytest.skip(
            f"no node at {var_parent!r} — toggle-variable parent not found. "
            "Run the live-CI setup script to seed the CI project or override "
            "FTX_LIVE_TOGGLE_VAR."
        )
    props = {p["name"]: p.get("value")
             for p in (parent_node.get("properties") or [])}
    if var_leaf not in props:
        pytest.skip(
            f"property {var_leaf!r} not found under {var_parent!r}; "
            f"available: {sorted(props)!r}. "
            "Seed Model/ClickToggle via the live-CI setup script or override "
            "FTX_LIVE_TOGGLE_VAR."
        )
    try:
        core.describe_node(cfg, project, _LIVE_TOGGLE_IND)
    except core.NodeNotFound:
        pytest.skip(
            f"no bound indicator widget at {_LIVE_TOGGLE_IND!r} — this test's "
            "only ground truth is a screenshot diff over a widget bound to "
            f"{var_path!r}. Seed it via the live-CI setup script or override "
            "FTX_LIVE_TOGGLE_IND / FTX_LIVE_TOGGLE_REGION."
        )

    def _capture(name: str, navigate: str | None, settle: float) -> bytes:
        path = str(tmp_path / name)
        shot = core.cdp_screenshot_runtime(
            cfg, save_path=path, quality=70, navigate_url=navigate,
            settle_seconds=settle, region=region, project=project)
        assert shot.get("state") == "succeeded", (
            f"region capture {name} failed: {shot!r}. Check that Chrome is "
            "running with CDP at OPTIX_CDP_URL and the emulator is serving at "
            f"{hmi_url}."
        )
        return pathlib.Path(path).read_bytes()

    # ---- two baselines: the instrument must be STABLE before we trust a diff -
    # A canvas that repaints on its own (an animation, a live clock) would make
    # any post-click diff meaningless, so prove it doesn't first.
    before_a = _capture("toggle-before-a.jpg", hmi_url, 2.0)
    before_b = _capture("toggle-before-b.jpg", "", 1.0)
    assert before_a == before_b, (
        f"the capture region {region!r} is not stable across two consecutive "
        f"captures ({len(before_a)} vs {len(before_b)} bytes) with no click in "
        "between — something in it repaints on its own, so a post-click diff "
        "proves nothing. Move FTX_LIVE_TOGGLE_REGION onto a static area that "
        "contains only the bound indicator widget."
    )

    # ---- click at normalized coordinates -------------------------------------
    # navigate_url="" keeps the already-navigated tab as-is: a reload here would
    # start a fresh web session between the baseline and the click.
    out = core.cdp_click_runtime(
        cfg, x=click_x, y=click_y, navigate_url="", settle_seconds=1.0)
    assert out.get("state") == "succeeded", (
        f"cdp_click_runtime failed before the read-back: {out!r}. Check that "
        "Chrome is running with CDP at OPTIX_CDP_URL and the emulator is "
        f"serving at {hmi_url}."
    )

    # Confirm the coordinate was treated as NORMALIZED (both values <= 1.0 ->
    # coords flag == "normalized"), and that it resolved to the exact viewport
    # fraction — this is the conversion formula itself, pinned on live metrics.
    assert out.get("coords") == "normalized", (
        f"click at ({click_x}, {click_y}) was not treated as normalized; "
        f"coords={out.get('coords')!r}, css_x={out.get('css_x')}, "
        f"css_y={out.get('css_y')}, viewport={out.get('viewport')!r}. "
        "Expected the 1.0.8 fix to route the click through _resolve_point."
    )
    vp = out.get("viewport") or {}
    assert out.get("css_x") == pytest.approx(click_x * vp.get("w", 0)), (
        f"css_x={out.get('css_x')} is not {click_x} x viewport width "
        f"{vp.get('w')}: {out!r}"
    )
    assert out.get("css_y") == pytest.approx(click_y * vp.get("h", 0)), (
        f"css_y={out.get('css_y')} is not {click_y} x viewport height "
        f"{vp.get('h')}: {out!r}"
    )

    # ---- the indicator must have repainted -----------------------------------
    after = _capture("toggle-after.jpg", "", 1.0)
    assert after != before_b, (
        f"normalized click at ({click_x}, {click_y}) -> CSS "
        f"({out.get('css_x')}, {out.get('css_y')}) on a {out.get('viewport')!r} "
        f"viewport did NOT change the canvas region {region!r} over "
        f"{_LIVE_TOGGLE_IND!r}, so {var_path!r} never flipped. Check, in order: "
        "(1) that the HMI actually RENDERED — an empty GridLayout anywhere in "
        "the start window crashes the Optix web client and leaves the page "
        "blank, which no error in the click path reveals (take a full-frame "
        "optix_observe screenshot and look at it); (2) that UI/MainWindow is "
        "1280x720, so the window fills the CSS viewport 1:1 and the centre of "
        "the viewport is inside ClickToggleBtn (40,40, 1200x600); (3) that the "
        "MouseClickEvent -> ToggleVariable handler and the indicator's binding "
        "survived the seed. If coords=='absolute' the normalize conversion "
        "regressed (1.0.8 fix not applied)."
    )


# ---- wire_event re-wire round-trip (Tier-2, in-place update gate) -----------
# Validates wire_event on an existing event handler performs an
# in-place update rather than creating a second (silent no-op) handler. The
# bridge must delete stale InputArguments, write the new args, return
# updated:True with InputArguments in the changed list, and the new value must
# survive a describe_node round-trip.
#
# No special CI seeding beyond Rectangle1 (already seeded by the live-CI setup script).
# The test creates a temp button ZzzWireRewireBtn and cleans it up in finally.
#
# Arg values "ZzzPanelA" / "ZzzPanelB" are intentionally non-existent paths so
# the bridge stores them as String variables (no model node lookup required).
# That keeps the assertions simple ("ZzzPanelB" in value) and independent of
# any particular project node layout.
#
# Env override (optional):
#   FTX_LIVE_REWIRE_METHOD_OBJ – path to any existing node used as the method
#                                 object in the wire_event call (the bridge
#                                 resolves the node to get its NodeId; the method
#                                 name "ChangePanel" is stored as-is). Defaults
#                                 to FTX_LIVE_SCRATCH_PANEL/Rectangle1.


def test_rewire_event_args_round_trip(live):
    """Tier-2: wire MouseClickEvent → ChangePanel with args=NewPanel=ZzzPanelA
    + AliasNode=ZzzAliasA, then re-wire the SAME event with args=NewPanel=ZzzPanelB
    (one arg only, no replace=True), then call describe_node on the handler's
    InputArguments and assert:

      * bridge returns ok:True, updated:True, "InputArguments" in changed
      * NewPanel reads back as ZzzPanelB (the second wire's value)
      * ZzzPanelA is absent (the first wire's value was replaced in-place)
      * AliasNode is absent (leftover from the first wire was purged)

    This is the live end-to-end contract for the 1.0.8 WireEventInline
    fix: the bridge must detect the existing EventHandler (by ListenEventType),
    wholesale-replace its InputArguments (delete all stale args first, then add
    the new set), and report the change via the updated/changed response fields.
    The describe_node read-back proves the model was actually mutated — not just
    that the bridge returned the right JSON.

    Prerequisites:
      * FTX_LIVE_SCRATCH_PANEL (or its default: parent of FTX_LIVE_NODE) must
        be an existing PanelType/Screen that accepts create_widget.
      * FTX_LIVE_REWIRE_METHOD_OBJ (default: SCRATCH_PANEL/Rectangle1) must be
        an existing node. Rectangle1 is seeded by the live-CI setup script — no extra
        CI setup is required when FTX_LIVE_NODE is under UI/MainWindow.
    """
    cfg, project = live
    scratch = _LIVE_SCRATCH_PANEL
    btn_name = "ZzzWireRewireBtn"
    btn_path = scratch + "/" + btn_name
    method_obj = _LIVE_REWIRE_METHOD_OBJ
    method_path = method_obj + "/ChangePanel"
    handler_name = f"EH_MouseClickEvent_{btn_name}"
    handler_path = btn_path + "/" + handler_name
    # InputArguments lives at EH.../MethodsToCall/MethodContainer1/InputArguments.
    # MethodsToCall is the ObjectsList backing node of EventHandler.MethodsToCall;
    # MethodContainer1 is added to it by WireEventInline; InputArguments is a
    # child object of MethodContainer1 that holds the per-argument variables.
    ia_path = handler_path + "/MethodsToCall/MethodContainer1/InputArguments"

    # --- prerequisites: scratch panel and method object must exist -------------
    try:
        core.describe_node(cfg, project, scratch)
    except core.NodeNotFound:
        pytest.skip(
            f"no node at {scratch!r} — set FTX_LIVE_SCRATCH_PANEL to an "
            "existing PanelType/Screen path, or ensure FTX_LIVE_NODE "
            "resolves to a node whose parent is a PanelType"
        )
    try:
        core.describe_node(cfg, project, method_obj)
    except core.NodeNotFound:
        pytest.skip(
            f"no method-object node at {method_obj!r} — set "
            "FTX_LIVE_REWIRE_METHOD_OBJ to an existing node path, or ensure "
            "the CI project creates Rectangle1 under FTX_LIVE_SCRATCH_PANEL "
            "(the live-CI setup script seeds UI/MainWindow/Rectangle1 by default)"
        )

    # --- best-effort pre-clean from any prior failed run ----------------------
    try:
        core.bridge_delete_node(cfg, project, btn_path)
    except (core.BridgeWriteFailed, core.NodeNotFound):
        pass

    try:
        # === Create a fresh temp button to wire on ============================
        try:
            core.bridge_create_widget(cfg, project, scratch, btn_name, "Button")
        except core.BridgeWriteFailed as exc:
            pytest.skip(
                f"could not create test button at {btn_path!r}: {exc}"
            )

        # === Wire A: two args so the "no leftover" assertion is non-trivial ===
        # AliasNode=ZzzAliasA is present in this wire but NOT in the second one,
        # so if the in-place update fails to purge stale args, it will leak.
        out_a = core.bridge_wire_event(
            cfg, project, btn_path, "MouseClickEvent",
            method_path=method_path,
            args="NewPanel=ZzzPanelA;AliasNode=ZzzAliasA",
        )
        assert out_a.get("ok") is True, (
            f"first wire_event (args A) returned ok:False; got: {out_a!r}. "
            f"Confirm {method_obj!r} is a reachable node."
        )

        # === Re-wire B: same event, different args, no replace=True ===========
        # This must take the in-place-update path (1.0.8 fix), NOT
        # fail with handler_exists (unsafe) or create a second handler silently.
        out_b = core.bridge_wire_event(
            cfg, project, btn_path, "MouseClickEvent",
            method_path=method_path,
            args="NewPanel=ZzzPanelB",  # only NewPanel — AliasNode must be purged
        )
        assert out_b.get("ok") is True, (
            f"re-wire (in-place update) returned ok:False; got: {out_b!r}. "
            "If the bridge returned handler_exists the WireEventInline in-place "
            "update fix is not in the build."
        )
        assert out_b.get("updated") is True, (
            "re-wire without replace=True must return updated:True to signal "
            "the in-place update path was taken; got: {out_b!r}. "
            "A missing updated:True means the bridge created a SECOND handler "
            "(silent no-op bug) or returned handler_exists (unsafe gate)."
        )
        assert "InputArguments" in (out_b.get("changed") or []), (
            "InputArguments must appear in the bridge's changed list when the "
            "args differ between the two wire_event calls; "
            f"got changed={out_b.get('changed')!r}"
        )

        # === Describe the InputArguments to verify the model was mutated =======
        try:
            ia_node = core.describe_node(cfg, project, ia_path)
        except core.NodeNotFound:
            pytest.fail(
                f"InputArguments not found at {ia_path!r} after re-wire. "
                "Check that WireEventInline's EventHandler structure matches "
                "EH_<evt>_<node>/MethodsToCall/MethodContainer1/InputArguments."
            )

        props = {
            p["name"]: (p.get("value") or "")
            for p in (ia_node.get("properties") or [])
        }

        # NewPanel must be present and carry the second wire's value.
        assert "NewPanel" in props, (
            f"NewPanel not found in InputArguments after re-wire to ZzzPanelB; "
            f"available props: {sorted(props)!r}. "
            "The in-place update may not have written the arg correctly."
        )
        new_panel_val = props["NewPanel"]
        assert "ZzzPanelB" in new_panel_val, (
            f"NewPanel should read back as ZzzPanelB after re-wire; "
            f"got: {new_panel_val!r}. "
            "The in-place update wrote the wrong value."
        )
        # Stale first-wire value must be replaced.
        assert "ZzzPanelA" not in new_panel_val, (
            f"NewPanel still contains ZzzPanelA after re-wire to ZzzPanelB; "
            f"got: {new_panel_val!r}. "
            "The in-place update wrote the wrong value (arg not replaced)."
        )

        # AliasNode must be purged — it was present in wire A, absent in wire B.
        # This is the "no leftover argument from the first call" assertion.
        assert "AliasNode" not in props, (
            f"AliasNode is still in InputArguments after re-wire "
            f"(props: {sorted(props)!r}). "
            "The in-place update did not purge stale args from the first wire — "
            "'no leftover argument from the first call' contract violated."
        )

    finally:
        try:
            core.bridge_delete_node(cfg, project, btn_path)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass


# ---- Expression literal rejection / runtime-log acceptance -----


def test_validate_expression_hash_literal_rejected(live):
    """1.0.8 Tier-2: validate_expression on an expression containing a
    '#RRGGBB' hex token returns valid:False + an invalid_literal dict whose fix
    value is the corrected 0xAARRGGBB form.

    Expression under test: ``if(true, #FFF809, 0xFFF0F0F0)``

    The ``#FFF809`` token is CSS-style RGB shorthand which the ExpressionEvaluator
    rejects silently at runtime — a bad expression silently no-ops instead of
    raising, so without this author-time gate an agent would wire an expression
    that does nothing and never find out why.  The validate_expression endpoint
    catches it in the lexer phase:

    * ``valid`` must be ``False``
    * ``invalid_literal`` dict must be present with ``reason`` and ``fix`` keys
    * ``fix`` must contain the corrected value ``0xFFFFF809`` (AARRGGBB with
      AA=FF prepended: #FFF809 → R=0xFF G=0xF8 B=0x09 → 0xFF_FF_F8_09)

    Read-only: validate_expression makes no model change regardless of the result.
    """
    cfg, project = live
    sources = _LIVE_EXPR_SOURCE if _LIVE_EXPR_SOURCE else None
    out = core.bridge_validate_expression(cfg, project, _LIVE_EXPR_BAD, sources=sources)

    assert out.get("valid") is False, (
        f"validate_expression on {_LIVE_EXPR_BAD!r} must return valid:False — "
        "the '#FFF809' token is not a valid ExpressionEvaluator literal; "
        f"got: {out!r}"
    )
    assert "invalid_literal" in out, (
        "validate_expression must include the 'invalid_literal' dict when valid:False "
        "and the rejection is caused by a bad literal token; "
        f"got keys: {sorted(out)!r}"
    )
    il = out["invalid_literal"]
    assert isinstance(il, dict), (
        f"invalid_literal must be a dict, got {type(il).__name__}: {il!r}"
    )
    fix = il.get("fix") or ""
    assert "0xFFFFF809" in fix, (
        f"the invalid_literal 'fix' must contain the corrected AABBGGRR value "
        f"'0xFFFFF809'; got fix={fix!r}. "
        "The validator should convert #FFF809 → AABBGGRR 0xFF_FF_F8_09 = 0xFFFFF809 "
        "(AA=FF alpha channel prepended to the RGB triplet R=FF G=F8 B=09)."
    )


def test_attach_expression_hash_literal_gate(live):
    """1.0.8 Tier-2: attach_expression with a '#hex' literal is refused
    by the pre-flight gate and no ExpressionEvaluator is created in the model.

    The C# ``/bridge/node/attach-expression`` endpoint runs the SAME literal
    check as ``/bridge/expr/validate`` before touching the model.  A ``#FFF809``
    token must cause the bridge to return an error body (``ok:False``) which
    ``_bridge_write`` surfaces as ``BridgeWriteFailed`` — the caller never sees
    an ``ok:True`` result that would suggest the ExpressionEvaluator was wired.

    The model-mutation guard is verified by comparing ``describe_node`` child
    lists BEFORE and AFTER the rejected call: no new child node (ExpressionEvaluator
    or otherwise) may appear after a gate-rejected attach.  This is the
    crash-safety gate counterpart to ``test_set_property_validity_gate``:
    a mis-wired ExpressionEvaluator silently no-ops at runtime and is near-
    impossible to diagnose after the fact, so the gate rejecting BEFORE touching
    the model is load-bearing.
    """
    cfg, project = live
    node_path = _LIVE_NODE
    prop = _LIVE_EXPR_PROP

    # Snapshot children BEFORE the attempted attach so we can compare after.
    node_before = core.describe_node(cfg, project, node_path)
    children_before = frozenset(
        c.get("browse_name") or c.get("name") or ""
        for c in (node_before.get("children") or [])
    )

    # The rejected attach must raise BridgeWriteFailed.
    sources = _LIVE_EXPR_SOURCE if _LIVE_EXPR_SOURCE else None
    with pytest.raises(core.BridgeWriteFailed) as excinfo:
        core.bridge_attach_expression(
            cfg, project, node_path, prop, _LIVE_EXPR_BAD, sources=sources,
        )

    msg = str(excinfo.value)
    assert msg, "BridgeWriteFailed must carry a non-empty reason"
    # The rejection message should name the invalid literal or the expression
    # error class; this is the "baked suggestion" an agent can act on.
    assert any(
        token in msg
        for token in ("invalid_literal", "#FFF809", "FFF809", "literal", "invalid")
    ), (
        f"BridgeWriteFailed message should mention the invalid literal or 'invalid_literal'; "
        f"got: {msg!r}. Confirm that /bridge/node/attach-expression runs the literal "
        "check before mutating the model."
    )

    # Compare children AFTER: no new child must have appeared.
    node_after = core.describe_node(cfg, project, node_path)
    children_after = frozenset(
        c.get("browse_name") or c.get("name") or ""
        for c in (node_after.get("children") or [])
    )
    new_children = children_after - children_before
    assert not new_children, (
        f"attach_expression with an invalid literal created new child node(s) on "
        f"{node_path!r}: {sorted(new_children)!r}. "
        "The pre-flight gate must refuse before any model mutation — no "
        "ExpressionEvaluator (or any other child) should appear after a rejected attach."
    )


def test_attach_expression_corrected_hex_runtime_log_clean(live):
    """1.0.8 Tier-2: the corrected 0xAARRGGBB expression attaches, the
    emulator restarts, and the runtime log contains no 'Invalid Expression
    parameter' line for the test widget — the runtime log is the acceptance
    criterion, not the attach result.

    A bad ExpressionEvaluator silently no-ops at runtime and logs
    'Invalid Expression parameter' against the node that owns the wired property.
    A valid expression produces no such log line.  This test pins the full path:
    valid expression → attach → runtime evaluates → log is clean.

    Steps:
      1. Create a temp Rectangle (ZzzExprTestRect) in FTX_LIVE_SCRATCH_PANEL.
      2. Attach the corrected expression 'if(true, 0xFFFFF809, 0xFFF0F0F0)'
         to its FillColor.  The attach must succeed (ok:True).
      3. Restart the emulator to get a fresh runtime log.
      4. Wait briefly for the expression evaluator to initialise.
      5. Tail the runtime log with contains='Invalid Expression parameter' and
         assert no matching line mentions the test widget name.
      6. Cleanup: delete the temp widget.

    Gated on FTX_LIVE_HMI=1 (emulator restartable).  the live-CI setup script starts
    the emulator and sets this flag before this suite runs.
    """
    if os.environ.get("FTX_LIVE_HMI") != "1":
        pytest.skip(
            "runtime-log acceptance test: set FTX_LIVE_HMI=1 with the emulator "
            "available for restart (the live-CI setup script arms the emulator and sets "
            "this flag before the live suite runs)"
        )

    import re
    import time

    cfg, project = live
    scratch = _LIVE_SCRATCH_PANEL
    widget_name = "ZzzExprTestRect"
    widget_path = scratch + "/" + widget_name
    prop = _LIVE_EXPR_PROP
    sources = _LIVE_EXPR_SOURCE if _LIVE_EXPR_SOURCE else None

    # --- confirm the scratch panel exists (skip, don't fail) ------------------
    try:
        core.describe_node(cfg, project, scratch)
    except core.NodeNotFound:
        pytest.skip(
            f"no node at {scratch!r} — set FTX_LIVE_SCRATCH_PANEL to an existing "
            "PanelType/Screen path so the temp widget can be created there"
        )

    # best-effort pre-clean from any prior failed run
    try:
        core.bridge_delete_node(cfg, project, widget_path)
    except (core.BridgeWriteFailed, core.NodeNotFound):
        pass

    try:
        # === (1) Create the temp Rectangle ====================================
        try:
            create_out = core.bridge_create_widget(
                cfg, project, scratch, widget_name, "Rectangle"
            )
        except core.BridgeWriteFailed as exc:
            pytest.skip(
                f"could not create temp Rectangle at {widget_path!r}: {exc} — "
                "check that FTX_LIVE_SCRATCH_PANEL is a PanelType/Screen"
            )
        assert create_out.get("ok") is True, (
            f"create_widget('Rectangle') returned ok:False: {create_out!r}"
        )

        # === (2) Attach the corrected expression to FillColor =================
        try:
            attach_out = core.bridge_attach_expression(
                cfg, project, widget_path, prop, _LIVE_EXPR_GOOD, sources=sources,
            )
        except core.BridgeWriteFailed as exc:
            pytest.fail(
                f"bridge_attach_expression({_LIVE_EXPR_GOOD!r}) raised "
                f"BridgeWriteFailed — the corrected AABBGGRR expression must be "
                f"accepted by the bridge: {exc}"
            )
        assert attach_out.get("ok") is True, (
            f"bridge_attach_expression returned ok:False for the corrected "
            f"expression {_LIVE_EXPR_GOOD!r}: {attach_out!r}. "
            "A valid AABBGGRR literal must be accepted."
        )

        # === (3) Restart the emulator to get a fresh runtime log ==============
        restart_out = core.restart_emulator(cfg, project)
        if not restart_out.get("serving"):
            pytest.skip(
                f"emulator did not come up after restart (state: "
                f"{restart_out.get('state')!r}): {restart_out!r}. "
                "Check that FTX_LIVE_PROJECT is open in Studio and the "
                "emulator can be restarted by the service."
            )

        # === (4) Brief settle — let the expression evaluator initialise =======
        time.sleep(3)

        # === (5) Tail the runtime log; assert no "Invalid Expression" for us ==
        log = core.runtime_log_tail(
            cfg, project,
            lines=500,
            contains="Invalid Expression parameter",
        )
        if "error" in log:
            pytest.skip(
                f"runtime log not available: {log.get('error')!r} — "
                f"{log.get('hint')!r}. "
                "The emulator may not have written a log yet after the restart."
            )

        # Unwrap the _untrusted envelope and check for our widget's name.
        raw_lines_str = str(log.get("lines") or "")
        # Strip the <untrusted source="runtime_log">...</untrusted> wrapper.
        inner = re.sub(r'^<untrusted[^>]*>|</untrusted>$', '', raw_lines_str,
                       flags=re.DOTALL)
        node_lines_for_widget = [
            ln for ln in inner.splitlines() if widget_name in ln
        ]
        assert not node_lines_for_widget, (
            f"runtime log contains 'Invalid Expression parameter' line(s) for "
            f"widget {widget_name!r} after attaching the corrected expression "
            f"{_LIVE_EXPR_GOOD!r}. This means the expression is STILL rejected "
            "at runtime — verify the 0xAARRGGBB literal format is correct and "
            "that the emulator reloaded after the attach. Matching log lines:\n"
            + "\n".join(node_lines_for_widget[:10])
        )

    finally:
        # === (6) Cleanup: delete the temp widget ==============================
        try:
            core.bridge_delete_node(cfg, project, widget_path)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass


# ---- issue #4: invoke runs under a root session ------------------------------

# The bridge's own NetLogic; CheckFormula(expression, sources) only logs.
_LIVE_BRIDGE_NETLOGIC = os.environ.get("FTX_LIVE_BRIDGE_NETLOGIC", "NetLogic/StudioMCPBridge")


def _assert_bridge_still_serves(cfg, project):
    bridges = core.list_bridges(cfg, force=True)
    mine = [b for b in bridges
            if (b.get("project") or "").strip().lower() == project.strip().lower()]
    assert mine and mine[0].get("available") is True, (
        f"bridge for {project!r} gone after invoke - Studio likely died: {bridges}")


def test_invoke_method_runs_under_root_session_and_studio_survives(live):
    """Before the fix, invoking ANY method from the bridge thread killed Studio
    (c0000374; no session on that thread). CheckFormula is the method that did
    it on 2026-09-24."""
    cfg, project = live
    out = core.bridge_invoke_method(cfg, project, _LIVE_BRIDGE_NETLOGIC,
                                    "CheckFormula", args="{0} + 1,Model/X")
    assert out.get("ok") is True and out.get("session") == "root", out
    _assert_bridge_still_serves(cfg, project)


def test_invoke_method_that_throws_is_survivable(live):
    """A method that throws (here: one argument for a two-parameter method ->
    TargetParameterCountException) is caught and logged by Optix; Studio and
    the bridge stay up."""
    cfg, project = live
    out = core.bridge_invoke_method(cfg, project, _LIVE_BRIDGE_NETLOGIC,
                                    "CheckFormula", args="only-one-arg")
    assert out.get("ok") is True, out
    _assert_bridge_still_serves(cfg, project)


@pytest.mark.parametrize("value", ["0", "Sideways"])
def test_spinbox_value_change_behaviour_is_refused_not_fatal(live, value):
    """1.0.8 battle test: ANY write to SpinBox.ValueChangeBehaviour (the valid
    ordinal 0 too) killed Studio - an AccessViolation inside GetOrCreateVariable,
    because the .NET proxy declares a property the UA type chain does not. The
    bridge must refuse it and keep serving."""
    cfg, project = live
    parent = "UI/MainWindow"
    name = "ZzzSpinVCB"
    try:
        core.bridge_delete_node(cfg, project, parent + "/" + name)
    except (core.BridgeWriteFailed, core.NodeNotFound):
        pass
    core.bridge_create_widget(cfg, project, parent, name, "SpinBox")
    try:
        out = core.bridge_edit(cfg, project, [
            {"op": "set_property", "path": parent + "/" + name,
             "name": "ValueChangeBehaviour", "value": value},
        ])
        assert not out.get("applied"), out
        assert "proxy_only_property" in json.dumps(out), out
        _assert_bridge_still_serves(cfg, project)
    finally:
        try:
            core.bridge_delete_node(cfg, project, parent + "/" + name)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass
