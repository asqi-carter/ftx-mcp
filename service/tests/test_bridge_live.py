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
           python -m pytest service/tests/test_bridge_live.py -q -m "not slow_live"

    The race / re-create NetLogic tests (57-81 s each) carry
    ``@pytest.mark.slow_live`` and are excluded from the default invocation
    above so the Tier-2 suite fits in 30 min. Run them separately with
    ``-m slow_live`` (see service/tests/README.md and ci/README.md).

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
import logging
import re
import os
import pathlib
import time
import uuid

import pytest

from service import core, optix_schema
from service.tests import _click_settle
from service.tests import _live_session as ls

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

# One short random suffix per test-module run. Every node / driver / station /
# class name a test CREATES goes through ``_nm`` so it never collides with a
# leftover from a crashed or parallel run; each test deletes what it made.
_RUN_SFX = uuid.uuid4().hex[:6]


def _nm(base: str) -> str:
    """``base`` + this run's unique suffix (the name a live test creates)."""
    return f"{base}{_RUN_SFX}"


def _require_desktop() -> None:
    """Skip a GUI-touching test (save, F5, focus/SendKeys on Studio) when the
    suite runs in session 0 / without an input desktop. There the focus helper
    can only answer bridge_studio_no_window, which is the environment, not the
    product (ftx-mcp-ver.3). ci/validate-w11.ps1 runs the suite in the
    interactive session so these tests run there."""
    if not ls.has_interactive_desktop():
        pytest.skip(ls.SKIP_REASON)


_REAL_LOCALAPPDATA = os.environ.get("LOCALAPPDATA")   # before conftest redirects it
_UNTRUSTED_RE = re.compile(r"<untrusted[^>]*>(.*?)</untrusted>", re.DOTALL)


def _inner(value) -> str:
    """The text inside the bridge's ``<untrusted source="bridge">…</untrusted>``
    envelope (or the plain string when there is none)."""
    text = "" if value is None else str(value)
    m = _UNTRUSTED_RE.search(text)
    return (m.group(1) if m else text).strip()


def _stop_project_emulator(cfg, project) -> None:
    """Best-effort stop of ``project``'s emulator (never raises)."""
    try:
        core.stop_emulator(cfg, project=project)
    except Exception:
        pass


@pytest.fixture(scope="module", autouse=True)
def _stop_emulator_at_teardown():
    """Stop the CI project's emulator when this module's tests finish so no
    orphan emulator survives the live suite."""
    yield
    project = os.environ.get("FTX_LIVE_PROJECT")
    if os.environ.get("FTX_LIVE_BRIDGE") != "1" or not project:
        return
    try:
        _stop_project_emulator(core.Config.from_env(), project)
    except Exception:
        pass


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
    _seed_live_node(bcfg, project)
    return bcfg, project


def _seed_live_node(cfg, project) -> None:
    """Make sure ``_LIVE_NODE`` (default UI/MainWindow/Rectangle1) exists, creating
    it with create_widget (``_LIVE_TYPE``, a Rectangle, under its parent) when it
    is missing.

    The module gate says a bridge is up, so a seeding failure is a setup
    failure: it FAILS (``pytest.fail``), never skips."""
    try:
        core.describe_node(cfg, project, _LIVE_NODE)
        return
    except core.NodeNotFound:
        pass
    parent, _, name = _LIVE_NODE.rpartition("/")
    try:
        out = core.bridge_create_widget(cfg, project, parent, name, _LIVE_TYPE)
        if out.get("ok") is False:
            raise AssertionError(out)
        core.describe_node(cfg, project, _LIVE_NODE)
    except (core.BridgeWriteFailed, core.NodeNotFound, AssertionError) as exc:
        pytest.fail(f"live setup: cannot seed {_LIVE_NODE!r} via create_widget "
                    f"({_LIVE_TYPE} under {parent!r}): {exc}", pytrace=False)


@pytest.fixture
def live_real_localappdata(monkeypatch, request):
    """``live`` resolved AFTER the real LOCALAPPDATA is restored.

    conftest redirects LOCALAPPDATA to an empty dir; the bridge/emulator
    registry lives under the real one. ``live`` builds its config (and finds the
    bridge through that registry) at setup, so tests that need the real
    registry must restore it *before* the config is built, not in the body."""
    if _REAL_LOCALAPPDATA is not None:
        monkeypatch.setenv("LOCALAPPDATA", _REAL_LOCALAPPDATA)
    else:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
    return request.getfixturevalue("live")


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
_HYPO = _LIVE_NODE.rsplit("/", 1)[0] + "/" + _nm("ZzzU16Hypothetical")


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
    orig_name = _nm("ZzzU16Original")
    orig_path = parent + "/" + orig_name
    new_name = _nm("ZzzU16Moved")
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
    orig_name = _nm("ZzzU16Original")
    orig_path = parent + "/" + orig_name
    new_name = _nm("ZzzU16Moved")
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
    item_name = _nm("ZzzU16NavItem")
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
         "name": _nm("ZzzU16Container"), "widget_type": "Container"},
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
    var_name = _nm("ZzzU16HypoVar")
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
    var_name = _nm("ZzzU16ValueValidateVar")
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
    var_name = _nm("ZzzU16ValueWriteVar")
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
    var_name = _nm("Zzz64Bit") + dtype
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
    var_name = _nm("ZzzRange") + dtype
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
    child_name = _nm("ZzzU16SpinBoxValueGuard")
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
# ---- wire_event method object (PRD 0052 D, #82) ----------------
# Since PRD 0050 B the bridge refuses a wire_event whose method the target object
# does not expose (``method_not_found``), so wiring to Rectangle1/ChangePanel (a
# Rectangle has no such method) fails on every bench.  The wire_event live tests
# take the ``wire_method_obj`` fixture instead: a NetLogic whose class declares
# real [ExportMethod]s, created through the same create_netlogic path the other
# live tests use.
# FTX_LIVE_REWIRE_METHOD_OBJ (optional) names an existing object that already
# exposes ChangePanel and ChangePanelByTabName; the fixture then uses it as is
# and creates nothing.
_LIVE_REWIRE_METHOD_OBJ = os.environ.get("FTX_LIVE_REWIRE_METHOD_OBJ", "")
_WIRE_NL_CLASS = _nm("ZzzWireMethods")
_WIRE_NL_METHODS = ("ChangePanel", "ChangePanelByTabName")
_WIRE_NL_CS_TEMPLATE = """using FTOptix.NetLogic;
using UAManagedCore;

public class __CLS__ : BaseNetLogic
{
    [ExportMethod]
    public void ChangePanel(string NewPanel)
    {
        Log.Info("__CLS__", "ChangePanel " + NewPanel);
    }

    [ExportMethod]
    public void ChangePanelByTabName(string TabName)
    {
        Log.Info("__CLS__", "ChangePanelByTabName " + TabName);
    }
}
"""


# Bounded wait for the NetSolution build to settle after the warm NetLogic is
# created (seconds).  Studio rebuilds the user .NET solution after a NetLogic
# create (~30 s on the bench), and a wire_event issued mid-build fails.
_NETSOLUTION_SETTLE_TIMEOUT = float(os.environ.get("FTX_LIVE_NETSOLUTION_WAIT", "120"))
_NETSOLUTION_QUIET_SECONDS = 3.0


def _wait_netsolution_settled(bcfg, project, timeout: float = _NETSOLUTION_SETTLE_TIMEOUT,
                              quiet: float = _NETSOLUTION_QUIET_SECONDS,
                              poll: float = 0.5) -> bool:
    """Poll until Studio's NetSolution build has settled; bounded by ``timeout``.

    Settled means, for ``quiet`` seconds in a row: the NetSolution tree (sources
    and build output) did not change, the Studio log did not grow, and the
    bridge, if it answers, does not report it is loading/compiling.  Returns
    True when that was observed, False on timeout (the caller proceeds: the
    test then fails with its own error rather than hanging the session)."""
    net_dir = core.resolve_project(bcfg, project) / "ProjectFiles" / "NetSolution"
    deadline = time.monotonic() + timeout
    state = (core._netlogic_tree_sig(net_dir), core._studio_log_mark())
    since = time.monotonic()
    while True:
        now = time.monotonic()
        if now - since >= quiet and not core._netlogic_bridge_busy(bcfg):
            return True
        if now >= deadline:
            return False
        time.sleep(poll)
        new = (core._netlogic_tree_sig(net_dir), core._studio_log_mark())
        if new != state:
            state, since = new, time.monotonic()


@pytest.fixture(scope="session")
def warm_netlogic():
    """Session-scoped, WARM NetLogic exposing ``ChangePanel`` and
    ``ChangePanelByTabName``; yields its path (``Model/<class>``).

    A cold Studio session rebuilds the NetSolution after the first NetLogic
    create, and a wire_event fired during that build fails (the wire_event
    tests passed in a full run only because earlier tests had already paid for
    the build).  This fixture creates ONE NetLogic for the whole session, then
    waits (bounded, ``_wait_netsolution_settled``: ``FTX_LIVE_NETSOLUTION_WAIT``
    seconds, default 120) for the build to settle before any test uses it, so
    the wire_event tests pass in any order and alone.  Teardown deletes the node
    and the .cs.  Skips when the NetLogic cannot be created.  When
    FTX_LIVE_REWIRE_METHOD_OBJ is set it is yielded unchanged.

    Reproduce the cold-session run (Studio freshly opened on the project, bridge
    armed, no NetLogic created yet)::

        FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<p> python -m pytest \\
            service/tests/test_bridge_live.py -k wire_event -q -rs -p no:cacheprovider
    """
    if _LIVE_REWIRE_METHOD_OBJ:
        yield _LIVE_REWIRE_METHOD_OBJ
        return
    project = os.environ.get("FTX_LIVE_PROJECT")
    if os.environ.get("FTX_LIVE_BRIDGE") != "1" or not project:
        pytest.skip("warm NetLogic needs FTX_LIVE_BRIDGE=1 and FTX_LIVE_PROJECT")
    core.reset_bridge_cache()
    cfg = core._bridge_cfg_for(core.Config.from_env(), project)
    if cfg is None:
        pytest.skip(f"no armed bridge serves {project!r}")
    node_path = f"Model/{_WIRE_NL_CLASS}"
    cs_path = (core.resolve_project(cfg, project) / "ProjectFiles" / "NetSolution"
               / f"{_WIRE_NL_CLASS}.cs")
    _best_effort_delete(cfg, project, node_path)  # leftover of an aborted run
    cs_path.parent.mkdir(parents=True, exist_ok=True)
    cs_path.write_text(_WIRE_NL_CS_TEMPLATE.replace("__CLS__", _WIRE_NL_CLASS),
                       encoding="utf-8")
    created = False
    try:
        try:
            out = core.bridge_create_netlogic(cfg, project, "Model", _WIRE_NL_CLASS)
        except core.BridgeWriteFailed as exc:
            pytest.skip(f"could not create the wire_event NetLogic: {exc}")
        if out.get("ok") is False:
            pytest.skip(f"could not create the wire_event NetLogic: {out!r}")
        created = True
        if not _wait_netsolution_settled(cfg, project):
            logging.getLogger(__name__).warning(
                "NetSolution build had not settled after %.0fs; continuing",
                _NETSOLUTION_SETTLE_TIMEOUT)
        yield node_path
    finally:
        if created:
            _best_effort_delete(cfg, project, node_path)
        try:
            cs_path.unlink()
        except OSError:
            pass


@pytest.fixture
def wire_method_obj(live, warm_netlogic):
    """Path of a NetLogic exposing ``ChangePanel`` and ``ChangePanelByTabName``.

    The wire_event live tests take this fixture.  It is the session-scoped
    ``warm_netlogic`` (created once, NetSolution build awaited), so no test pays
    for or races the build and the tests run in any order, including alone.

    Reproduce the cold-session run (fresh Studio session, bridge armed)::

        FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<p> python -m pytest \\
            service/tests/test_bridge_live.py -k wire_event -q -rs -p no:cacheprovider

    When FTX_LIVE_REWIRE_METHOD_OBJ is set it is yielded unchanged."""
    return warm_netlogic

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
    child_name = _nm("ZzzVKTestWidget")
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

    # ---- settle: the instrument must be STABLE before we trust a pixel diff --
    # A canvas that repaints on its own (an animation, a live clock) would make
    # any post-click pixel diff meaningless, so capture until two consecutive
    # frames are identical (bounded). Optix 1.7.4.32 settles on the second
    # capture (same captures as before). Optix 1.8.0.555 repaints part of the
    # default region by itself (793 vs 1196 bytes), so it may never settle; in
    # that case the test falls back to the bridge-read value change instead of
    # failing on pixels.
    stable, before_b, attempts = _click_settle.settle_region(
        lambda i: _capture(
            f"toggle-before-{i}.jpg", hmi_url if i == 0 else "",
            2.0 if i == 0 else 1.0))
    path_used = (_click_settle.PATH_PIXEL if stable
                 else _click_settle.PATH_BRIDGE_READ)
    _msg = (f"test_normalized_click_round_trip: path={path_used} "
            f"region={region!r} settle_attempts={attempts} stable={stable}")
    logging.getLogger(__name__).info(_msg)
    print(_msg)

    def _bridge_value():
        node = core.describe_node(cfg, project, var_parent)
        return {p["name"]: p.get("value")
                for p in (node.get("properties") or [])}.get(var_leaf)

    bridge_before = None if stable else _bridge_value()

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

    # ---- bridge-read fallback: the region never stabilised --------------------
    # Pixels are unusable here; assert the bound variable's value changed via
    # the bridge read. Caveat (see docstring): describe_node reports the
    # design-time model, so this read may not move for a runtime-only toggle;
    # the failure message says so. Pending bench verification on 1.8.0.555.
    if not stable:
        bridge_after = _bridge_value()
        assert bridge_after != bridge_before, (
            f"path={path_used}: region {region!r} never stabilised in "
            f"{attempts} captures, and the bridge read of {var_path!r} did not "
            f"change after the click ({bridge_before!r} -> {bridge_after!r}). "
            "Either the click missed, or the bridge only reports the "
            "design-time value, which a runtime ToggleVariable does not "
            "change; point FTX_LIVE_TOGGLE_REGION at a static area so the "
            "pixel path can be used."
        )
        return

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
# The method object is the ``wire_method_obj`` fixture: a NetLogic with real
# [ExportMethod]s ChangePanel / ChangePanelByTabName (PRD 0052 D, #82; the bridge
# refuses a method the object does not expose since PRD 0050 B).
# Env override (optional):
#   FTX_LIVE_REWIRE_METHOD_OBJ – path to an existing object that already exposes
#                                 both methods; the fixture then creates nothing.


def test_rewire_event_args_round_trip(live, wire_method_obj):
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
      * ``wire_method_obj`` supplies the method object: a NetLogic with a real
        exported ChangePanel (created and removed by the fixture).
    """
    cfg, project = live
    scratch = _LIVE_SCRATCH_PANEL
    btn_name = _nm("ZzzWireRewireBtn")
    btn_path = scratch + "/" + btn_name
    method_obj = wire_method_obj
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


# ---- wire_event multi-step handlers + append (PRD 0024, 1.0.9 9e) -----------
# One EventHandler, several calls, created in order under its MethodsToCall; a
# later wire_event(append=True) adds to the SAME handler without replacing the
# calls it already has; dry_run refuses a malformed list with structured errors.
# Same prerequisites as test_rewire_event_args_round_trip (a scratch panel and
# any existing node as the method object). Arg values are non-existent paths so
# they land as Strings. Skipped without a live bridge (the `live` fixture).


def _live_method_containers(cfg, project, handler_path):
    """{container browse name: Method value} for the handler, in bridge order."""
    mtc = core.describe_node(cfg, project, handler_path + "/MethodsToCall")
    names = [c.get("name") or c.get("browse_name") if isinstance(c, dict) else c
             for c in (mtc.get("children") or [])]
    out = {}
    for name in names:
        node = core.describe_node(cfg, project, f"{handler_path}/MethodsToCall/{name}")
        props = {p["name"]: _inner(p.get("value")) for p in (node.get("properties") or [])}
        out[name] = props.get("Method", "")
    return out


def test_wire_event_multistep_and_append_live(live, wire_method_obj):
    cfg, project = live
    scratch = _LIVE_SCRATCH_PANEL
    btn_name = _nm("ZzzWireStepsBtn")
    btn_path = scratch + "/" + btn_name
    method_obj = wire_method_obj
    handler_path = f"{btn_path}/EH_MouseClickEvent_{btn_name}"
    for needed in (scratch, method_obj):
        try:
            core.describe_node(cfg, project, needed)
        except core.NodeNotFound:
            pytest.skip(f"no node at {needed!r} — set FTX_LIVE_SCRATCH_PANEL / "
                        "FTX_LIVE_REWIRE_METHOD_OBJ to existing nodes")
    try:
        core.bridge_delete_node(cfg, project, btn_path)
    except (core.BridgeWriteFailed, core.NodeNotFound):
        pass

    try:
        try:
            core.bridge_create_widget(cfg, project, scratch, btn_name, "Button")
        except core.BridgeWriteFailed as exc:
            pytest.skip(f"could not create test button at {btn_path!r}: {exc}")

        # --- 1. dry_run refuses a bad list (structured) and mutates nothing ----
        bad = core.bridge_edit(cfg, project, [{
            "op": "wire_event", "path": btn_path, "event_type": "MouseClickEvent",
            "calls": [{"method_path": method_obj + "/ChangePanel"}, {"command": "ToggleVariable"}],
        }], dry_run=True)
        assert bad["report"]["ok"] is False and bad["applied"] == 0
        assert any(e.get("code") == "call_missing_variable" and e.get("call_index") == 1
                   for e in bad["report"]["errors"]), bad["report"]
        with pytest.raises(core.NodeNotFound):
            core.describe_node(cfg, project, handler_path)

        good_ops = [{
            "op": "wire_event", "path": btn_path, "event_type": "MouseClickEvent",
            "calls": [{"method_path": method_obj + "/ChangePanel", "args": "NewPanel=ZzzPanelA"},
                      {"method_path": method_obj + "/ChangePanelByTabName", "args": "TabName=ZzzTab"}],
        }]
        ok = core.bridge_edit(cfg, project, good_ops, dry_run=True)
        assert ok["report"]["ok"] is True, ok["report"]

        # --- 2. create: two calls, in order, under ONE handler ------------------
        out = core.bridge_wire_event(
            cfg, project, btn_path, "MouseClickEvent",
            calls=good_ops[0]["calls"])
        assert out.get("ok") is True and out.get("steps") == 2, out
        containers = _live_method_containers(cfg, project, handler_path)
        assert list(containers.values()) == ["ChangePanel", "ChangePanelByTabName"], containers

        # --- 3. append: existing calls kept, new one added after them ----------
        out2 = core.bridge_wire_event(
            cfg, project, btn_path, "MouseClickEvent",
            calls=[{"method_path": method_obj + "/ChangePanel", "args": "NewPanel=ZzzPanelC"}],
            append=True)
        assert out2.get("ok") is True and out2.get("appended") is True, out2
        containers = _live_method_containers(cfg, project, handler_path)
        assert list(containers.values()) == [
            "ChangePanel", "ChangePanelByTabName", "ChangePanel"], containers
        # still ONE handler for the event on the node
        node = core.describe_node(cfg, project, btn_path)
        handlers = [c for c in (node.get("children") or [])
                    if "EH_MouseClickEvent" in str(c)]
        assert len(handlers) == 1, handlers
    finally:
        try:
            core.bridge_delete_node(cfg, project, btn_path)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass


def test_wire_event_nonexistent_method_returns_method_not_found_live(live, wire_method_obj):
    """PRD 0050 B / 0052 D: wiring to a method the object does not expose is
    refused with ``method_not_found`` (naming the object, the method and the
    methods it does have) and creates no handler.  The object is the real
    NetLogic from ``wire_method_obj``, so the refusal is about the method name
    only, not a missing object."""
    cfg, project = live
    scratch = _LIVE_SCRATCH_PANEL
    btn_name = _nm("ZzzWireNoMethodBtn")
    btn_path = scratch + "/" + btn_name
    handler_path = f"{btn_path}/EH_MouseClickEvent_{btn_name}"
    try:
        core.describe_node(cfg, project, scratch)
    except core.NodeNotFound:
        pytest.skip(f"no node at {scratch!r} — set FTX_LIVE_SCRATCH_PANEL to an "
                    "existing PanelType/Screen path")
    _best_effort_delete(cfg, project, btn_path)
    try:
        try:
            core.bridge_create_widget(cfg, project, scratch, btn_name, "Button")
        except core.BridgeWriteFailed as exc:
            pytest.skip(f"could not create test button at {btn_path!r}: {exc}")

        out = core.bridge_wire_event(
            cfg, project, btn_path, "MouseClickEvent",
            method_path=wire_method_obj + "/ZzzNoSuchMethod")
        assert out.get("ok") is False, out
        assert out.get("error") == "method_not_found" == out.get("code"), out
        assert out.get("method") == "ZzzNoSuchMethod", out
        assert set(_WIRE_NL_METHODS) <= set(out.get("available") or []) \
            or _LIVE_REWIRE_METHOD_OBJ, out
        with pytest.raises(core.NodeNotFound):
            core.describe_node(cfg, project, handler_path)

        # The real method on the same object still wires: the refusal above was
        # about the name, not the object.
        good = core.bridge_wire_event(
            cfg, project, btn_path, "MouseClickEvent",
            method_path=wire_method_obj + "/ChangePanel", args="NewPanel=ZzzPanelA")
        assert good.get("ok") is True, good
    finally:
        _best_effort_delete(cfg, project, btn_path)


def test_set_text_key_live(live):
    """set_text_key writes a LocalizedText TextId (a key), not literal text.

    Creates a throwaway Label under the scratch panel, adds a translation row,
    sets Label.Text to the key, and checks the bridge's read-back (`text_id`)
    equals the key and that a literal set_property is a different thing
    (no `text_id`). Also checks dry_run/the write path refuse attributes and that
    BrowseName is untouched. Deletes the label afterwards.
    """
    cfg, project = live
    scratch = _LIVE_SCRATCH_PANEL
    name = _nm("ZzzTextKeyLabel")
    path = scratch + "/" + name
    key = _nm("ZzzTextKeyLive")
    try:
        core.describe_node(cfg, project, scratch)
    except core.NodeNotFound:
        pytest.skip(f"no node at {scratch!r} — set FTX_LIVE_SCRATCH_PANEL to an existing node")
    try:
        core.bridge_delete_node(cfg, project, path)
    except (core.BridgeWriteFailed, core.NodeNotFound):
        pass
    try:
        try:
            core.bridge_create_widget(cfg, project, scratch, name, "Label")
        except core.BridgeWriteFailed as exc:
            pytest.skip(f"could not create test label at {path!r}: {exc}")

        # dry_run: structured errors, nothing written
        for attr in ("DisplayName", "BrowseName"):
            bad = core.bridge_edit(cfg, project, [
                {"op": "set_text_key", "path": path, "name": attr, "key": key}], dry_run=True)
            assert bad["report"]["ok"] is False and bad["applied"] == 0, bad["report"]
            assert any(e.get("code") == "node_attribute_not_settable"
                       for e in bad["report"]["errors"]), bad["report"]
        bad = core.bridge_edit(cfg, project, [
            {"op": "set_text_key", "path": path, "name": "Text", "key": " padded "}], dry_run=True)
        assert any(e.get("code") == "invalid_text_key" for e in bad["report"]["errors"])
        good = core.bridge_edit(cfg, project, [
            {"op": "set_text_key", "path": path, "name": "Text", "key": key}], dry_run=True)
        assert good["report"]["ok"] is True, good["report"]

        # the write path refuses attributes before reaching the bridge
        with pytest.raises(core.BridgeWriteFailed, match="node_attribute_not_settable"):
            core.bridge_set_text_key(cfg, project, path, "BrowseName", key)

        core.bridge_add_translation(cfg, project, key, "Live key text", "en-US")
        out = core.bridge_set_text_key(cfg, project, path, "Text", key)
        assert out.get("ok") is True, out
        assert out.get("text_id") == key, out

        # a literal set is the other form: the key must not be reported back
        lit = core.bridge_set_property(cfg, project, path, "Text", "literal words")
        assert lit.get("ok") is True and "text_id" not in lit

        # a non-LocalizedText property is refused by the bridge, not coerced
        with pytest.raises(core.BridgeWriteFailed, match="not_localized_text"):
            core.bridge_set_text_key(cfg, project, path, "Width", key)
        assert core.describe_node(cfg, project, path).get("name", name) == name
    finally:
        try:
            core.bridge_delete_node(cfg, project, path)
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

    _require_desktop()
    cfg, project = live
    scratch = _LIVE_SCRATCH_PANEL
    widget_name = _nm("ZzzExprTestRect")
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
    """Post-0050 C contract (#73): a wrong argument count is refused with
    ``argument_count_mismatch`` WITHOUT calling the method, and a method that
    fails at runtime (CheckFormula logs an error for an empty expression; Optix
    catches and logs it) is ``method_failed``. Studio and the bridge stay up
    after both."""
    cfg, project = live
    mismatch = core.bridge_invoke_method(cfg, project, _LIVE_BRIDGE_NETLOGIC,
                                         "CheckFormula", args="only-one-arg")
    assert mismatch.get("ok") is False, mismatch
    assert mismatch.get("error") == "argument_count_mismatch", mismatch
    assert mismatch.get("expected") == 2 and mismatch.get("given") == 1, mismatch
    _assert_bridge_still_serves(cfg, project)

    failed = core.bridge_invoke_method(cfg, project, _LIVE_BRIDGE_NETLOGIC,
                                       "CheckFormula", args=",Model/X")
    assert failed.get("ok") is False, failed
    assert failed.get("error") == "method_failed", failed
    _assert_bridge_still_serves(cfg, project)


@pytest.mark.parametrize("value", ["0", "Sideways"])
def test_spinbox_value_change_behaviour_is_refused_not_fatal(live, value):
    """1.0.8 battle test: ANY write to SpinBox.ValueChangeBehaviour (the valid
    ordinal 0 too) killed Studio - an AccessViolation inside GetOrCreateVariable,
    because the .NET proxy declares a property the UA type chain does not. The
    bridge must refuse it and keep serving."""
    cfg, project = live
    parent = "UI/MainWindow"
    name = _nm("ZzzSpinVCB")
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


# ---- PRD 0019: subtree refusal (move / rename / convert) ---------------------
# Throwaway-project test: needs a bridge >= 1.0.9 (``subtree_guard``). Gated by
# the module-level FTX_LIVE_BRIDGE=1 skip, so it only ever runs against a live
# bridge. NetLogic creation needs the project's NetSolution to accept a class
# of the same name; if it cannot be created the netlogic half skips.

_SUBTREE_CODES = ("contains_netlogic", "contains_event_handler")


_SCRATCH_PROJECT_PREFIXES = ("zzOpenTruth", "zzBridgeSync")


def _remove_scratch_project(cfg, name: str, attempts: int = 10) -> bool:
    """Delete the throw-away project folder ``<projects_root>/<name>`` a test made
    with ``project_new``. Call it AFTER the Studio the test launched is closed
    (Studio holds file handles; on Windows the delete is retried for a while).

    Refuses anything that is not a direct child of the projects root named like
    one of this suite's scratch projects. Never raises; True when the folder is
    gone."""
    import shutil

    if not name.startswith(_SCRATCH_PROJECT_PREFIXES) or "/" in name or "\\" in name \
            or ".." in name:
        return False
    root = pathlib.Path(cfg.projects_root).resolve()
    target = (root / name).resolve()
    if target.parent != root:
        return False
    for i in range(attempts):
        if not target.exists():
            return True
        try:
            shutil.rmtree(target)
        except OSError:
            time.sleep(1.0)
    return not target.exists()


def _color_value_matches(value, expected_hex: str) -> bool:
    """True when a describe_node Color value equals ``expected_hex`` (``#rrggbb``).

    A Color variable is a UInt32 (0xAARRGGBB), so describe reports it as a decimal
    number, not the hex string that was set. Both are accepted: a decimal or
    ``0x``/``#`` hex text (with any trailing `` (<Type>)`` annotation removed) is parsed to an unsigned 32-bit value and its low 24
    bits (RGB, alpha ignored) are compared with the expected colour."""
    want = int(expected_hex.lstrip("#"), 16) & 0xFFFFFF
    text = _inner(value).strip().lower()
    # describe_node annotates typed values as "<value> (<Type>)", e.g. "4294901760 (UInt32)".
    text = re.sub(r"\s*\([A-Za-z_][\w.]*\)\s*$", "", text)
    try:
        if re.fullmatch(r"\d+", text):
            got = int(text) & 0xFFFFFFFF
        elif re.fullmatch(r"(#|0x)[0-9a-f]+", text):
            got = int(text.lstrip("#").removeprefix("0x"), 16) & 0xFFFFFFFF
        else:
            return expected_hex.lstrip("#").lower() in text
    except ValueError:
        return False
    return got & 0xFFFFFF == want


def _best_effort_delete(cfg, project, path):
    try:
        core.bridge_delete_node(cfg, project, path)
    except (core.BridgeWriteFailed, core.NodeNotFound):
        pass


def _save_retrying_once(cfg, project) -> dict:
    """``core.save`` and, when Studio left the mtime unchanged (saved False,
    mtime_after == mtime_before: Ctrl+S raced a still-settling create), save
    once more. Returns the last result."""
    out = core.save(cfg, project)
    if out.get("saved") is not True and out.get("mtime_after") == out.get("mtime_before"):
        out = core.save(cfg, project)
    return out


def _wait_for_mtime_change(cfg, project, before: float, *, timeout: float = 15.0,
                           interval: float = 0.5) -> bool:
    """Poll the project's newest node-YAML mtime until it exceeds ``before``.
    Bounded by ``timeout`` seconds; returns True once it advanced, else False."""
    project_dir = core.resolve_project(cfg, project)
    deadline = time.monotonic() + timeout
    while True:
        if core._project_max_mtime(project_dir) > before:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _with_bridge_recovery(cfg, project, fn, *, wait: float = 60.0, interval: float = 2.0):
    """Run ``fn()``; if it raises ``no_bridge_for_project`` (the bridge drops
    for a moment while Studio recompiles a NetLogic), wait for the bridge to
    re-arm - dropping the registry cache and re-resolving each poll - then run
    ``fn()`` once more. Any other error, or a bridge that never returns, fails."""
    try:
        return fn()
    except core.NoBridgeForProject:
        pass
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        core.reset_bridge_cache()
        if core._find_bridge_for(cfg, project) is not None:
            break
        time.sleep(interval)
    else:
        raise AssertionError(
            f"no_bridge_for_project: bridge for {project!r} did not return within {wait}s")
    return fn()


def _assert_subtree_refused(cfg, project, src, dest_parent, code, types_folder):
    """move, rename (via batch, lowered to move) and convert each refuse `src`
    with `code`, and the source node is still present afterwards."""
    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_move_node(cfg, project, src, dest_parent)
    assert e.value.bridge_code == code, e.value
    assert e.value.hint and "Do not retry as-is" in e.value.hint

    out = core.bridge_edit(cfg, project, [
        {"op": "rename", "path": src, "new_name": _nm("ZzzRenamed")}])
    assert not out.get("applied"), out
    refused = out.get("refused_ops") or []
    assert len(refused) == 1 and refused[0]["code"] == code, out
    assert refused[0]["hint"]

    with pytest.raises(core.BridgeWriteFailed) as e:
        core.bridge_convert_to_type(cfg, project, src, _nm("ZzzSubtreeType"), types_folder)
    assert e.value.bridge_code == code, e.value

    # Refusal happens before any mutation: the node is untouched.
    assert core.describe_node(cfg, project, src)


_SUBTREE_VAR_PARENT, _SUBTREE_VAR_NAME = "Model", _nm("ZzzUnused")


@pytest.fixture
def subtree_nodes(live):
    """Create every node the subtree-refusal test needs; delete them afterwards.

    Creates the SetVariable target ``Model/ZzzUnused`` (#16: the throwaway
    project does not have it, so wire_event failed with node_not_found), a
    destination Panel, and a Panel holding a Button wired with a MouseClick
    EventHandler. Yields a dict of paths plus a ``created`` list; the test
    appends anything else it creates. Teardown runs even when setup or the test
    fails and deletes (deepest first) only what this fixture created.
    """
    cfg, project = live
    scratch = _LIVE_SCRATCH_PANEL
    try:
        core.describe_node(cfg, project, scratch)
    except core.NodeNotFound:
        pytest.skip(f"no scratch node at {scratch!r} — set FTX_LIVE_SCRATCH_PANEL")
    health = core._bridge_http(cfg, "/bridge/health")
    if health[0] != 200:
        pytest.skip(f"/bridge/health returned {health[0]}")
    ver = tuple(int(p) for p in str(json.loads(health[1]).get("bridge_version", "0"))
                .split(".")[:3] if p.isdigit())
    if ver < (1, 0, 9):
        pytest.skip(f"bridge {ver} predates the subtree guard (1.0.9)")

    var_path = f"{_SUBTREE_VAR_PARENT}/{_SUBTREE_VAR_NAME}"
    eh_panel, eh_child = _nm("ZzzSubtreeEH"), _nm("ZzzBtn")
    nl_panel, dest = _nm("ZzzSubtreeNL"), _nm("ZzzSubtreeDest")
    created: list = []  # paths created here, in creation order
    # Stale leftovers from an aborted earlier run (our own scratch names only).
    for n in (eh_panel, nl_panel, dest):
        _best_effort_delete(cfg, project, f"{scratch}/{n}")
    var_preexisting = True
    try:
        core.describe_node(cfg, project, var_path)
    except core.NodeNotFound:
        var_preexisting = False
    try:
        if not var_preexisting:
            core.bridge_create_variable(
                cfg, project, _SUBTREE_VAR_NAME, _SUBTREE_VAR_PARENT, "Int32")
            created.append(var_path)
        for parent, name, wtype in (
                (scratch, dest, "Panel"),
                (scratch, eh_panel, "Panel"),
                (f"{scratch}/{eh_panel}", eh_child, "Button")):
            core.bridge_create_widget(cfg, project, parent, name, wtype)
            created.append(f"{parent}/{name}")
        core.bridge_wire_event(
            cfg, project, f"{scratch}/{eh_panel}/{eh_child}", "MouseClickEvent",
            command="SetVariable", variable=var_path, value="1")
        yield {"cfg": cfg, "project": project, "scratch": scratch,
               "eh_panel": f"{scratch}/{eh_panel}", "dest": f"{scratch}/{dest}",
               "nl_panel": nl_panel, "created": created}
    finally:
        for path in reversed(created):
            _best_effort_delete(cfg, project, path)


def test_subtree_refusals_live_and_bridge_survives(subtree_nodes):
    """Panel holding a child with a MouseClick EventHandler, and a Panel holding
    a NetLogic: move, rename and convert_to_type are each refused with the
    structured code + hint, and /bridge/health still answers afterwards (the
    unguarded operations crashed Studio)."""
    f = subtree_nodes
    cfg, project, scratch = f["cfg"], f["project"], f["scratch"]
    types_folder = "UI/Templates"
    _assert_subtree_refused(cfg, project, f["eh_panel"], f["dest"],
                            "contains_event_handler", types_folder)
    _assert_bridge_still_serves(cfg, project)
    health = core._bridge_http(cfg, "/bridge/health")
    assert health[0] == 200, health

    nl_path = f"{scratch}/{f['nl_panel']}"
    core.bridge_create_widget(cfg, project, scratch, f["nl_panel"], "Panel")
    f["created"].append(nl_path)
    try:
        core.bridge_create_netlogic(cfg, project, nl_path, _nm("ZzzSubtreeNetLogic"))
    except core.BridgeWriteFailed as exc:
        pytest.skip(f"could not create a NetLogic under {f['nl_panel']}: {exc}")
    _assert_subtree_refused(cfg, project, nl_path, f["dest"],
                            "contains_netlogic", types_folder)
    _assert_bridge_still_serves(cfg, project)
    health = core._bridge_http(cfg, "/bridge/health")
    assert health[0] == 200, health


# ---- 1.0.9 #18: focus helper EnumWindows fallback (save + emulator restart) --
# The bench's Studio reports MainWindowHandle 0 with a visible window; the
# focus helper must still find it via EnumWindows instead of NO_TARGET_WINDOW.

def test_save_live_targets_bridge_studio(live):
    """core.save must reach the bridge's Studio window (no
    bridge_studio_no_window / NO_TARGET_WINDOW) even when MainWindowHandle is 0.
    Skipped without a live bridge (module gate)."""
    _require_desktop()
    cfg, project = live
    out = core.save(cfg, project)
    assert out.get("reason") not in ("bridge_studio_no_window", "no_studio_window"), out
    assert "NO_TARGET_WINDOW" not in (out.get("stdout") or ""), out
    assert out.get("focused") is True, out
    if out.get("bridge_pid") is not None:
        assert out.get("save_target_pid") == out["bridge_pid"], out


def test_emulator_restart_live_focuses_studio(live):
    """restart_emulator sends F5 through the same focus helper; it must not
    end in bridge_studio_no_window. Needs FTX_LIVE_HMI=1 (emulator restartable)."""
    if os.environ.get("FTX_LIVE_HMI") != "1":
        pytest.skip("emulator restart test: set FTX_LIVE_HMI=1 with the emulator "
                    "restartable")
    _require_desktop()
    cfg, project = live
    out = core.restart_emulator(cfg, project)
    assert out.get("reason") != "bridge_studio_no_window", out
    assert "NO_TARGET_WINDOW" not in str(out.get("stdout") or ""), out
    assert out.get("serving"), out


# ---- 1.0.9 #2: NodeId dynamic-link sources report target_path ----------------

def test_formatter_two_sources_target_path_live(live):
    """A StringFormatter with two sources reports a target_path for each; deleting
    one target flips that source to target_path:null + target_missing:true."""
    cfg, project = live
    scratch = _LIVE_SCRATCH_PANEL
    try:
        core.describe_node(cfg, project, scratch)
    except core.NodeNotFound:
        pytest.skip(f"no scratch node at {scratch!r} — set FTX_LIVE_SCRATCH_PANEL")
    names = (_nm("ZzzLinkA"), _nm("ZzzLinkB"))
    label = _nm("ZzzLinkLabel")
    label_path = f"{scratch}/{label}"
    for n in names:
        _best_effort_delete(cfg, project, f"Model/{n}")
    _best_effort_delete(cfg, project, label_path)
    try:
        for n in names:
            core.bridge_create_variable(cfg, project, n, "Model", "Int32")
        core.bridge_create_widget(cfg, project, scratch, label, "Label")
        core.bridge_attach_string_formatter(
            cfg, project, label_path, "Text", "{0} / {1}",
            sources=[f"Model/{n}" for n in names])

        def sources():
            found = {}
            conv = core.describe_node(cfg, project, f"{label_path}/Text")
            for c in conv.get("children", []):
                if "Formatter" not in c.get("dotnet_type", ""):
                    continue
                fmt = core.describe_node(cfg, project, f"{label_path}/Text/{c['browse_name']}")
                for p in fmt.get("properties", []):
                    if p["name"].startswith("Source"):
                        found[p["name"]] = p
            return found

        found = sources()
        got = {k: v.get("target_path") for k, v in found.items()}
        if not got:
            pytest.skip("bridge describe did not surface Source* properties")
        assert got == {"Source0": f"Model/{names[0]}",
                       "Source1": f"Model/{names[1]}"}, got

        core.bridge_delete_node(cfg, project, f"Model/{names[1]}")
        after = sources()
        assert after["Source0"].get("target_path") == f"Model/{names[0]}"
        assert after["Source1"].get("target_path") is None
        assert after["Source1"].get("target_missing") is True
    finally:
        _best_effort_delete(cfg, project, label_path)
        for n in names:
            _best_effort_delete(cfg, project, f"Model/{n}")


# ---- 1.0.9 #1/#2 (PRD 0023): format path forms + target_missing --------------

def _live_formatter_sources(cfg, project, label_path):
    """{Source<name>: property dict} for the formatter hung on ``label_path``/Text,
    read through describe_node so ``target_path`` / ``target_missing`` are the
    bridge's own serialization."""
    found = {}
    conv = core.describe_node(cfg, project, f"{label_path}/Text")
    for c in conv.get("children", []):
        if "Formatter" not in c.get("dotnet_type", ""):
            continue
        fmt = core.describe_node(cfg, project, f"{label_path}/Text/{c['browse_name']}")
        for p in fmt.get("properties", []):
            if p["name"].startswith("Source"):
                found[p["name"]] = p
    return found


def _live_scratch_or_skip(cfg, project):
    """The scratch panel path, or skip when it does not exist in the live project."""
    scratch = _LIVE_SCRATCH_PANEL
    try:
        core.describe_node(cfg, project, scratch)
    except core.NodeNotFound:
        pytest.skip(f"no scratch node at {scratch!r} — set FTX_LIVE_SCRATCH_PANEL")
    return scratch


def test_format_path_forms_ab_render_live(live):
    """Both format forms on two Labels: the project-relative form is REFUSED
    (format_path_not_runtime_resolvable, nothing written), the absolute form
    attaches and RENDERS the target value (A/B render).

    Label A gets the relative form (refused -> keeps its static text), Label B
    the absolute `/Objects/<Project>/...` form. The design-time half (refusal,
    absolute attach, nothing on A) always runs. The render half needs
    FTX_LIVE_HMI=1 (emulator restartable) and tesseract: it restarts the
    emulator and OCRs the runtime canvas, asserting the target value is on
    screen below A's static text (B is placed under A) — i.e. only the
    absolute-form Label shows the value."""
    _require_desktop()
    cfg, project = live
    scratch = _live_scratch_or_skip(cfg, project)
    pname = core._bridge_reported_project(cfg, project)
    if not pname:
        pytest.skip("bridge did not report a project name on /bridge/health")
    sel, item = _nm("ZzzFmtSel"), _nm("ZzzFmtItem1")
    target_value, a_static = "QUARTZ", "UNBOUND"
    label_a, label_b = _nm("ZzzFmtLabelA"), _nm("ZzzFmtLabelB")
    pa, pb = f"{scratch}/{label_a}", f"{scratch}/{label_b}"
    relative = f"Model/{item}{{#k}}"
    absolute = f"/Objects/{pname}/Model/{item}{{#k}}"
    assert core.classify_format_path(relative) == "project_relative"
    assert core.classify_format_path(absolute) == "runtime_resolvable"

    def cleanup():
        for p in (pa, pb, f"Model/{sel}", f"Model/{item}"):
            _best_effort_delete(cfg, project, p)

    cleanup()
    try:
        core.bridge_create_variable(cfg, project, sel, "Model", "Int32")
        core.bridge_set_property(cfg, project, f"Model/{sel}", "Value", "1")
        core.bridge_create_variable(cfg, project, item, "Model", "String")
        core.bridge_set_property(cfg, project, f"Model/{item}", "Value", target_value)
        for name, top in ((label_a, "40"), (label_b, "140")):
            core.bridge_create_widget(cfg, project, scratch, name, "Label")
            core.bridge_set_property(cfg, project, f"{scratch}/{name}", "LeftMargin", "40")
            core.bridge_set_property(cfg, project, f"{scratch}/{name}", "TopMargin", top)
        core.bridge_set_property(cfg, project, pa, "Text", a_static)

        # A: project-relative form is refused, and nothing is written.
        with pytest.raises(core.BridgeWriteFailed) as exc:
            core.bridge_attach_formatter(
                cfg, project, pa, "Text", relative, sources=f"k=Model/{sel}")
        assert "format_path_not_runtime_resolvable" in str(exc.value), exc.value
        assert f"/Objects/{pname}/Model/{item}{{#k}}" in str(exc.value), (
            "the refusal must carry the exact absolute rewrite")
        a_children = core.describe_node(cfg, project, f"{pa}/Text").get("children", [])
        assert not [c for c in a_children if "Formatter" in c.get("dotnet_type", "")], (
            f"refused attach left a formatter on Label A: {a_children!r}")

        # B: absolute form attaches.
        out = core.bridge_attach_formatter(
            cfg, project, pb, "Text", absolute, sources=f"k=Model/{sel}")
        assert out.get("ok") is True, out
        src = _live_formatter_sources(cfg, project, pb)
        if src:
            assert src["Sourcek"].get("target_path") == f"Model/{sel}", src

        # A/B render (runtime half).
        if os.environ.get("FTX_LIVE_HMI") != "1":
            pytest.skip("render half: set FTX_LIVE_HMI=1 with the emulator "
                        "restartable (design-time half passed)")
        restart = core.restart_emulator(cfg, project)
        if not restart.get("serving"):
            pytest.skip(f"emulator did not come up after restart: {restart!r}")
        hits = {}
        for token in (target_value, a_static):
            res = core.cdp_find_text_runtime(
                cfg, token, navigate_url=_LIVE_HMI_URL, settle_seconds=3.0,
                project=project)
            if res.get("error") == "tesseract_not_installed":
                pytest.skip("tesseract not installed — cannot A/B the render by OCR")
            hits[token] = res.get("matches") or []
        assert hits[a_static], f"Label A's static text {a_static!r} not on screen: {hits!r}"
        assert hits[target_value], (
            f"Label B (absolute form) did not render the target value "
            f"{target_value!r}: {hits!r}. The absolute format path "
            f"{absolute!r} should resolve at runtime.")
        assert hits[target_value][0]["center_px"][1] > hits[a_static][0]["center_px"][1], (
            f"{target_value!r} rendered at/above Label A ({a_static!r}) — expected only "
            f"Label B, placed below A, to show it: {hits!r}")
    finally:
        cleanup()


def test_formatter_two_sources_both_target_paths_live(live):
    """A formatter (attach_formatter, named sources) with two sources reports a
    target_path for EACH source, equal to the variables' project-root paths."""
    cfg, project = live
    scratch = _live_scratch_or_skip(cfg, project)
    pname = core._bridge_reported_project(cfg, project)
    if not pname:
        pytest.skip("bridge did not report a project name on /bridge/health")
    names = (_nm("ZzzTpA"), _nm("ZzzTpB"))
    label_name = _nm("ZzzTpLabel")
    label_path = f"{scratch}/{label_name}"
    for p in [label_path] + [f"Model/{n}" for n in names]:
        _best_effort_delete(cfg, project, p)
    try:
        for n in names:
            core.bridge_create_variable(cfg, project, n, "Model", "Int32")
        core.bridge_create_widget(cfg, project, scratch, label_name, "Label")
        core.bridge_attach_formatter(
            cfg, project, label_path, "Text", f"/Objects/{pname}/Model/{{#a}}{{#b}}",
            sources=f"a=Model/{names[0]};b=Model/{names[1]}")
        found = _live_formatter_sources(cfg, project, label_path)
        if not found:
            pytest.skip("bridge describe did not surface Source* properties")
        got = {k: v.get("target_path") for k, v in found.items()}
        assert got == {"Sourcea": f"Model/{names[0]}",
                       "Sourceb": f"Model/{names[1]}"}, got
        assert not any(v.get("target_missing") for v in found.values()), found
    finally:
        for p in [label_path] + [f"Model/{n}" for n in names]:
            _best_effort_delete(cfg, project, p)


def test_formatter_target_deleted_reports_target_missing_live(live):
    """Deleting a source's target flips ONLY that source to target_path:null +
    target_missing:true; the surviving source keeps its target_path."""
    cfg, project = live
    scratch = _live_scratch_or_skip(cfg, project)
    names = (_nm("ZzzTmA"), _nm("ZzzTmB"))
    label_name = _nm("ZzzTmLabel")
    label_path = f"{scratch}/{label_name}"
    for p in [label_path] + [f"Model/{n}" for n in names]:
        _best_effort_delete(cfg, project, p)
    try:
        for n in names:
            core.bridge_create_variable(cfg, project, n, "Model", "Int32")
        core.bridge_create_widget(cfg, project, scratch, label_name, "Label")
        core.bridge_attach_string_formatter(
            cfg, project, label_path, "Text", "{0} / {1}",
            sources=[f"Model/{n}" for n in names])
        before = _live_formatter_sources(cfg, project, label_path)
        if not before:
            pytest.skip("bridge describe did not surface Source* properties")
        assert not any(v.get("target_missing") for v in before.values()), before

        core.bridge_delete_node(cfg, project, f"Model/{names[1]}")
        after = _live_formatter_sources(cfg, project, label_path)
        assert after["Source0"].get("target_path") == f"Model/{names[0]}", after
        assert not after["Source0"].get("target_missing"), after
        assert after["Source1"].get("target_path") is None, after
        assert after["Source1"].get("target_missing") is True, after
    finally:
        for p in [label_path] + [f"Model/{n}" for n in names]:
            _best_effort_delete(cfg, project, p)


def test_translations_bulk_and_remove_live(live):
    """add_translations adds rows in one call; remove_translation is not_supported.

    Uses throwaway keys. The SDK cannot remove a row, so they are left behind
    (overwritten on every run). Checks dry_run does not add, the bulk add reports
    `added` then `updated` on a repeat, an unknown key is translation_not_found,
    and an existing key is not_supported (dry_run included), in the primitive and
    in a bridge_edit batch (report.errors with op_index).
    """
    cfg, project = live
    # The SDK cannot remove a row, and re-adding a key a previous run left
    # behind fails ("Localization dictionary ... already contains"). So the
    # keys carry a per-run suffix; the legacy fixed keys are cleaned up
    # best-effort (a no-op where removal is unsupported) before and after.
    import uuid
    suffix = uuid.uuid4().hex[:8]
    legacy = [_nm("ZzzBulkTx1"), _nm("ZzzBulkTx2"), _nm("ZzzBulkTx3")]
    keys = [f"{k}{suffix}" for k in legacy]
    rows = [{"key": k, "value": f"Live {k}"} for k in keys]

    def _cleanup():
        for k in legacy + keys:
            try:
                core.bridge_remove_translation(cfg, project, k)
            except core.BridgeWriteFailed:
                pass

    _cleanup()
    try:
        _translations_bulk_body(cfg, project, keys, rows)
    finally:
        _cleanup()


def _translations_bulk_body(cfg, project, keys, rows):
    # dry_run validates, mutates nothing
    dry = core.bridge_add_translations(cfg, project, rows, dry_run=True)
    assert dry.get("ok") is True and dry.get("dry_run") is True, dry

    # structured row errors, nothing written
    invalid = core.bridge_add_translations(
        cfg, project, [rows[0], {"key": keys[1], "value": "x"}, {"key": keys[1], "value": "y"}])
    assert invalid["ok"] is False and invalid["code"] == "invalid_translation_rows"

    # the real bulk add: all rows new (or updated if left by a prior run), then updated
    out = core.bridge_add_translations(cfg, project, rows)
    assert out.get("ok") is True and out.get("count") == 3, out
    assert out.get("added") == 3, out
    again = core.bridge_add_translations(cfg, project, rows)
    assert again.get("updated") == 3 and again.get("added") == 0, again

    # remove: existing key (dry_run or not) is not_supported, unknown is not_found
    for dry_run in (True, False):
        res = core.bridge_remove_translation(cfg, project, keys[0], dry_run=dry_run)
        assert res["ok"] is False and res["code"] == "not_supported", res
    unknown = core.bridge_remove_translation(cfg, project, "ZzzNoSuchKey")
    assert unknown["ok"] is False and unknown["code"] == "translation_not_found", unknown

    # remove after a primitive add and after a batch add (ftx-mcp-phv.4 / #40): both
    # answer not_supported through the one existence check, never translation_not_found
    import uuid
    sfx = uuid.uuid4().hex[:8]
    prim_key, batch_key = f"ZzzPrimTx{sfx}", f"ZzzBatchTx{sfx}"
    prim = core.bridge_add_translations(cfg, project, [{"key": prim_key, "value": "prim"}])
    assert prim.get("ok") is True and prim.get("added") == 1, prim
    res = core.bridge_remove_translation(cfg, project, prim_key)
    assert res["ok"] is False and res["code"] == "not_supported", res
    applied = core.bridge_edit(cfg, project, [
        {"op": "add_translations", "rows": [{"key": batch_key, "value": "batch"}]}])
    assert applied["report"]["ok"] is True and applied["applied"] == 1, applied["report"]
    res = core.bridge_remove_translation(cfg, project, batch_key)
    assert res["ok"] is False and res["code"] == "not_supported", res
    after_batch = core.bridge_edit(cfg, project, [
        {"op": "remove_translation", "key": batch_key}], dry_run=True)
    assert any(e.get("code") == "not_supported" for e in after_batch["report"]["errors"]), after_batch["report"]

    # in a batch it is reported with its op_index and nothing is applied
    batch = core.bridge_edit(cfg, project, [
        {"op": "add_translations", "rows": rows},
        {"op": "remove_translation", "key": keys[0]}], dry_run=True)
    assert batch["report"]["ok"] is False and batch["applied"] == 0, batch["report"]
    assert any(e.get("code") == "not_supported" and e.get("op_index") == 1
               for e in batch["report"]["errors"])


def test_add_translations_repeat_add_updates_live(live):
    """Adding the same key twice returns `updated` the second time (dedicated tool
    and the bridge_edit op), instead of failing with 'already contains'."""
    import uuid
    cfg, project = live
    key = f"ZzzRepeatTx{uuid.uuid4().hex[:8]}"   # the SDK cannot remove rows
    first = core.bridge_add_translations(cfg, project, [{"key": key, "value": "one"}])
    assert first.get("ok") is True and first.get("added") == 1, first
    again = core.bridge_add_translations(cfg, project, [{"key": key, "value": "two"}])
    assert again.get("ok") is True and again.get("updated") == 1 and again.get("added") == 0, again
    batch = core.bridge_edit(cfg, project, [
        {"op": "add_translations", "rows": [{"key": key, "value": "three"}]}])
    assert batch["report"]["ok"] is True and batch["applied"] == 1, batch["report"]


def test_set_modelling_rule_live(live):
    """ModellingRule is settable to Mandatory / Optional / None on a type member.

    Creates a throwaway ObjectType under FTX_LIVE_TYPES_FOLDER (default
    ``Model``) with one Int32 member, then: dry_run rejects a bad rule with a
    structured error, each of the three rules applies (and is read back as
    `modelling_rule`), and an instance (non-type) node is refused with
    `not_a_type_member`. Deletes the type afterwards.
    """
    cfg, project = live
    folder = os.environ.get("FTX_LIVE_TYPES_FOLDER", "Model")
    type_name = _nm("ZzzModRuleType")
    type_path = folder + "/" + type_name
    member = type_path + "/Speed"
    try:
        core.bridge_delete_node(cfg, project, type_path)
    except (core.BridgeWriteFailed, core.NodeNotFound):
        pass
    try:
        try:
            core.bridge_create_type(cfg, project, type_name, folder)
            core.bridge_create_variable(cfg, project, "Speed", type_path, "Int32")
        except core.BridgeWriteFailed as exc:
            pytest.skip(f"could not create test type member at {member!r}: {exc}")

        # dry_run: a bad rule is a structured error, nothing changes
        bad = core.bridge_edit(cfg, project, [
            {"op": "set_modelling_rule", "path": member, "rule": "Required"}], dry_run=True)
        assert bad["report"]["ok"] is False and bad["applied"] == 0, bad["report"]
        assert any(e.get("code") == "invalid_modelling_rule"
                   for e in bad["report"]["errors"]), bad["report"]
        single = core.bridge_set_modelling_rule(cfg, project, member, "Required", dry_run=True)
        assert single["ok"] is False and single["code"] == "invalid_modelling_rule", single
        good = core.bridge_edit(cfg, project, [
            {"op": "set_modelling_rule", "path": member, "rule": "Mandatory"}], dry_run=True)
        assert good["report"]["ok"] is True, good["report"]

        # the bridge's dry_run reports without changing
        would = core.bridge_set_modelling_rule(cfg, project, member, "Mandatory", dry_run=True)
        assert would.get("ok") is True and would.get("would_set") is True, would
        previous = would.get("previous")

        for rule in ("Mandatory", "Optional", "None"):
            out = core.bridge_set_modelling_rule(cfg, project, member, rule)
            assert out.get("ok") is True, out
            assert out.get("modelling_rule") == rule, out

        # same through the batch surface
        batch = core.bridge_edit(cfg, project, [
            {"op": "set_modelling_rule", "path": member, "rule": "optional"}])
        assert batch.get("applied") == 1, batch
        assert core.bridge_set_modelling_rule(
            cfg, project, member, "Optional", dry_run=True).get("previous") == "Optional"
        assert previous is not None

        # an instance node is not a type member
        scratch = _LIVE_SCRATCH_PANEL
        with pytest.raises(core.BridgeWriteFailed, match="not_a_type_member"):
            core.bridge_set_modelling_rule(cfg, project, scratch, "Mandatory")
    finally:
        try:
            core.bridge_delete_node(cfg, project, type_path)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass


def test_retype_to_localized_text_live(live):
    """An existing variable is retyped to LocalizedText in place.

    A String variable holding text keeps that text across the retype
    (`values_converted` 1, reported `to` LocalizedText); dry_run validates and
    leaves the DataType alone; a bad datatype is refused with a structured
    error; a value with no text form (a NodeId variable pointing at a node) is
    refused with `value_not_convertible` and the node keeps its DataType.
    """
    cfg, project = live
    parent = _LIVE_MODEL_PARENT
    txt_name, ref_name = _nm("ZzzRetypeLtText"), _nm("ZzzRetypeLtRef")
    txt, ref = parent + "/" + txt_name, parent + "/" + ref_name

    def _datatype_of(name: str) -> str:
        props = {p["name"]: p.get("value")
                 for p in (core.describe_node(cfg, project, parent).get("properties") or [])}
        return re.sub(r"</?untrusted[^>]*>", "", props.get(name) or "")

    for p in (txt, ref):
        _best_effort_delete(cfg, project, p)
    try:
        core.bridge_create_variable(cfg, project, txt_name, parent, "String")
        core.bridge_create_variable(cfg, project, ref_name, parent, "NodeId")
        core.bridge_set_property(cfg, project, txt, "Value", "Hello retype")
        core.bridge_set_property(cfg, project, ref, "Value", parent)

        # dry_run: validated, nothing changes; bad datatype is structured
        dry = core.bridge_retype(cfg, project, txt, "LocalizedText", dry_run=True)
        assert dry.get("ok") is True and dry.get("would_retype") is True, dry
        bad = core.bridge_edit(cfg, project, [
            {"op": "retype", "path": txt, "datatype": "Color"}], dry_run=True)
        assert bad["report"]["ok"] is False and bad["applied"] == 0, bad["report"]
        assert any(e.get("code") == "invalid_datatype" for e in bad["report"]["errors"])
        good = core.bridge_edit(cfg, project, [
            {"op": "retype", "path": txt, "datatype": "LocalizedText"}], dry_run=True)
        assert good["report"]["ok"] is True, good["report"]
        assert "LocalizedText" not in _datatype_of(txt_name)

        # non-convertible value: refused, structured, node unchanged
        refused = core.bridge_retype(cfg, project, ref, "LocalizedText")
        assert refused["ok"] is False and refused["code"] == "value_not_convertible", refused
        dry_refused = core.bridge_edit(cfg, project, [
            {"op": "retype", "path": ref, "datatype": "LocalizedText"}], dry_run=True)
        assert any(e.get("code") == "value_not_convertible"
                   for e in dry_refused["report"]["errors"]), dry_refused["report"]
        assert "LocalizedText" not in _datatype_of(ref_name)

        # convertible value: retyped in place, text preserved
        out = core.bridge_retype(cfg, project, txt, "LocalizedText")
        assert out.get("ok") is True, out
        assert out["from"]["datatype"] == "String", out
        assert out["to"]["datatype"] == "LocalizedText", out
        assert out["sample"][0] == "Hello retype", out
        assert "LocalizedText" in _datatype_of(txt_name)
        assert "Hello retype" in _datatype_of(txt_name)
    finally:
        for p in (txt, ref):
            _best_effort_delete(cfg, project, p)


def test_create_variable_nodepointer_kind_live(live):
    """create_variable(NodePointer) sets Kind to the pointed-to node's type.

    A NodePointer aimed at a Folder gets Kind FolderType (not unset, not
    BaseObjectType); dry_run reports the resolved Kind and refuses a pointer
    with no target/kind and an unresolvable target; the live node's Kind child
    reads back as the target's type.
    """
    cfg, project = live
    parent = _LIVE_MODEL_PARENT
    folder_name, ptr_name, bad_name = _nm("ZzzNpKindFolder"), _nm("ZzzNpKindPtr"), _nm("ZzzNpKindBad")
    folder, ptr, bad = (parent + "/" + n for n in (folder_name, ptr_name, bad_name))

    for p in (ptr, bad, folder):
        _best_effort_delete(cfg, project, p)
    try:
        core.bridge_create_folder(cfg, project, parent, folder_name)

        # dry_run: Kind resolved from the target, nothing created
        dry = core.bridge_edit(cfg, project, [
            {"op": "create_variable", "parent": parent, "name": ptr_name,
             "datatype": "NodePointer", "target": folder}], dry_run=True)
        assert dry["report"]["ok"] is True and dry["applied"] == 0, dry["report"]
        kinds = [r["kind"] for r in dry["report"].get("resolved") or []]
        assert kinds == ["FolderType"], dry["report"]

        # target created earlier in the same batch resolves through the hypothetical model
        hypo = core.bridge_edit(cfg, project, [
            {"op": "create_folder", "parent": parent, "name": bad_name},
            {"op": "create_variable", "parent": parent, "name": ptr_name + "H",
             "datatype": "NodePointer", "target": bad}], dry_run=True)
        assert hypo["report"]["ok"] is True, hypo["report"]
        assert [r["kind"] for r in hypo["report"]["resolved"]] == ["FolderType"], hypo["report"]

        # no target / kind, or a target that does not resolve: refused, structured
        none = core.bridge_edit(cfg, project, [
            {"op": "create_variable", "parent": parent, "name": ptr_name,
             "datatype": "NodePointer"}], dry_run=True)
        assert any(e.get("code") == "kind_required" for e in none["report"]["errors"])
        missing = core.bridge_edit(cfg, project, [
            {"op": "create_variable", "parent": parent, "name": ptr_name,
             "datatype": "NodePointer", "target": parent + "/ZzzNoSuchNode"}], dry_run=True)
        assert missing["report"]["ok"] is False, missing["report"]
        assert any(e.get("code") == "node_not_found" for e in missing["report"]["errors"])

        # apply: the pointer exists, aimed at the folder, Kind == the folder's type
        out = core.bridge_create_variable(
            cfg, project, ptr_name, parent, "NodePointer", target=folder)
        assert out.get("ok") is True and out.get("kind") == "FolderType", out
        assert out.get("kind_node_id") not in (None, "", "ns=0;i=58"), out
        kind_child = core.describe_node(cfg, project, ptr)
        text = re.sub(r"</?untrusted[^>]*>", "", json.dumps(kind_child))
        # the bridge prints the Kind NodeId as "0/61 (NodeId)"
        assert "FolderType" in text or "i=61" in text or "0/61" in text, text
    finally:
        for p in (ptr, bad, folder):
            _best_effort_delete(cfg, project, p)


# ---- 1.0.9 5c: bind raw_path with an [i] array-element index ----------------
# Needs a live bridge and the scratch panel (FTX_LIVE_SCRATCH_PANEL). Creates one
# Int32 array variable under Model and one Label, and deletes both afterwards.
# NOT yet run on the bench (pending bench verification).

def test_bind_raw_path_array_element_live(live):
    """bind(raw_path="Model/<arr>[1]") links a Label's Text to element [1] of an
    array variable: the DynamicLink literal keeps the `[1]`; malformed paths and
    indexes are refused with structured errors and nothing is bound."""
    cfg, project = live
    scratch = _live_scratch_or_skip(cfg, project)
    arr_name, label = _nm("ZzzBindArr"), _nm("ZzzBindArrLabel")
    arr_path = f"{_LIVE_MODEL_PARENT}/{arr_name}"
    label_path = f"{scratch}/{label}"
    for p in (label_path, arr_path):
        _best_effort_delete(cfg, project, p)
    try:
        core.bridge_create_variable(
            cfg, project, arr_name, _LIVE_MODEL_PARENT, "Int32", array_dimensions=3)
        core.bridge_create_widget(cfg, project, scratch, label, "Label")

        for bad_path, code in ((f"{arr_path}[x]", "invalid_raw_path_index"),
                               (f"{arr_path}[-1]", "invalid_raw_path_index"),
                               (f"{arr_path}[1", "invalid_raw_path")):
            bad = core.bridge_bind_property(
                cfg, project, label_path, "Text", raw_path=bad_path)
            assert bad.get("ok") is False and bad.get("code") == code, bad

        dry = core.bridge_bind_property(
            cfg, project, label_path, "Text", raw_path=f"{arr_path}[1]", dry_run=True)
        assert dry.get("ok") is True and dry.get("dry_run") is True, dry

        out = core.bridge_bind_property(
            cfg, project, label_path, "Text", raw_path=f"{arr_path}[1]")
        assert out.get("ok") is True and out.get("via") == "dynamiclink-raw", out
        assert out.get("raw") == f"{arr_path}[1]", out

        text = re.sub(r"</?untrusted[^>]*>", "", json.dumps(
            core.describe_node(cfg, project, f"{label_path}/Text")))
        assert f"{arr_name}[1]" in text, text
    finally:
        for p in (label_path, arr_path):
            _best_effort_delete(cfg, project, p)


# ---- 1.0.9 5d: dry_run resolves bind source_path ----------------------------
# Needs a live bridge and the scratch panel (FTX_LIVE_SCRATCH_PANEL).
# NOT yet run on the bench (pending bench verification).

def test_dry_run_catches_bad_bind_source_live(live):
    """dry_run on create_widget + bind with a nonexistent source_path returns a
    structured source_not_found error and creates nothing; the same batch with a
    source created earlier in the batch validates clean (still nothing created)."""
    cfg, project = live
    scratch = _live_scratch_or_skip(cfg, project)
    label = _nm("ZzzBindSrcLabel")
    label_path = f"{scratch}/{label}"
    var_name = _nm("ZzzBindSrcVar")
    _best_effort_delete(cfg, project, label_path)
    try:
        ops = [
            {"op": "create_widget", "screen": scratch, "name": label, "widget_type": "Label"},
            {"op": "bind", "path": label_path, "name": "Text",
             "source_path": f"{_LIVE_MODEL_PARENT}/ZzzNoSuchSource"},
        ]
        bad = core.bridge_edit(cfg, project, ops, dry_run=True)
        assert bad["state"] == "validated" and bad["applied"] == 0, bad
        errs = [e for e in bad["report"]["errors"] if e.get("code") == "source_not_found"]
        assert errs and errs[0]["op_index"] == 1, bad
        good = core.bridge_edit(cfg, project, [
            {"op": "create_variable", "parent": _LIVE_MODEL_PARENT, "name": var_name,
             "datatype": "String"},
            ops[0],
            {**ops[1], "source_path": f"{_LIVE_MODEL_PARENT}/{var_name}"},
        ], dry_run=True)
        assert good["report"]["errors"] == [], good
        # dry_run wrote nothing: the widget does not exist.
        try:
            core.describe_node(cfg, project, label_path)
            raise AssertionError("dry_run must not create the widget")
        except core.NodeNotFound:
            pass
    finally:
        _best_effort_delete(cfg, project, label_path)


# ---- expression pow (Studio 1.8) — ftx-mcp-8fy.7 ----------------------------------
# Runs on BOTH Studio versions: the expectation is picked from the version the bridge
# reports in /bridge/health.studio_version, so run it once against 1.7 and once on 1.8.

_LIVE_POW_EXPR = "pow(2, 3) + 1"


def _live_studio_minor(cfg):
    """(major, minor) of the Studio serving the bridge, or skip."""
    status, raw = core._bridge_http(cfg, "/bridge/health")
    if status != 200:
        pytest.skip(f"/bridge/health returned {status}")
    ver = str(json.loads(raw).get("studio_version") or "")
    parts = [int(p) for p in re.findall(r"\d+", ver)[:2]]
    if len(parts) < 2:
        pytest.skip(f"bridge does not report a parseable studio_version ({ver!r}) — rebuild it")
    return tuple(parts), ver


def test_health_studio_version_is_product_version_live(live):
    """health studio_version is the Studio product version (1.x), not FTOptix.UI's 21.2.0.98."""
    cfg, _project = live
    (major_minor, ver) = _live_studio_minor(cfg)
    assert major_minor[0] == 1, f"studio_version {ver!r} is not a Studio product version"
    assert not ver.startswith("21."), ver


def _running_studio_version_or_none(cfg):
    """(major, minor) of the running Studio, or None when it cannot be determined.

    ``FTX_LIVE_STUDIO_VERSION`` (e.g. ``1.7.4.32``) wins; otherwise the version
    the Studio binary prints for ``--version``. Never skips.
    """
    raw = os.environ.get("FTX_LIVE_STUDIO_VERSION", "").strip()
    if not raw:
        raw = str(core.studio_version(cfg).get("stdout") or "")
    parts = [int(p) for p in re.findall(r"\d+", raw)[:2]]
    return tuple(parts) if len(parts) >= 2 else None


def _live_running_studio_version(cfg):
    """The running Studio's version from a source INDEPENDENT of the bridge.

    Skips when no parseable ``major.minor`` is available so the bridge is never
    compared against itself.
    """
    ver = _running_studio_version_or_none(cfg)
    if ver is None:
        raw = os.environ.get("FTX_LIVE_STUDIO_VERSION", "").strip()
        pytest.skip("running Studio version unknown — set FTX_LIVE_STUDIO_VERSION "
                    f"(e.g. 1.7.4.32); Studio --version gave {raw!r}")
    return ver


def test_health_studio_version_matches_running_studio_major_minor_live(live):
    """health studio_version starts with the running Studio's major.minor (ftx-mcp-mnk.2)."""
    cfg, _project = live
    running = _live_running_studio_version(cfg)
    _reported, ver = _live_studio_minor(cfg)
    prefix = f"{running[0]}.{running[1]}"
    assert ver == prefix or ver.startswith(prefix + "."), (
        f"bridge reports studio_version {ver!r}, running Studio is {prefix}")


def test_pow_version_gate_follows_running_studio_live(live):
    """pow is refused with function_requires_version below 1.8 and accepted on 1.8+.

    The expectation comes from the independently-known Studio version, not from
    the bridge's own report, so a wrong studio_version cannot make it pass.
    """
    cfg, project = live
    running = _live_running_studio_version(cfg)
    out = core.bridge_validate_expression(cfg, project, _LIVE_POW_EXPR)
    if running >= (1, 8):
        assert out.get("valid") is True, (running, out)
        assert "function_requires_version" not in str(out.get("error", "")), out
    else:
        assert out.get("valid") is False, (running, out)
        assert str(out.get("error", "")).startswith("function_requires_version:"), out
        assert (out.get("requires_version") or {}).get("function") == "pow", out


def test_validate_expression_pow_live(live):
    """validate_expression: pow valid on Studio 1.8, structured error on 1.7."""
    cfg, project = live
    (major_minor, ver) = _live_studio_minor(cfg)
    out = core.bridge_validate_expression(cfg, project, _LIVE_POW_EXPR)
    if major_minor >= (1, 8):
        assert out.get("valid") is True, (ver, out)
    else:
        assert out.get("valid") is False, (ver, out)
        assert str(out.get("error", "")).startswith("function_requires_version:"), out
        assert "1.8" in out["error"], out
        rv = out.get("requires_version") or {}
        assert rv.get("function") == "pow" and rv.get("min_version") == "1.8", out


def test_attach_expression_pow_live(live):
    """attach_expression: pow attaches on 1.8; on 1.7 it is refused and nothing is created."""
    cfg, project = live
    (major_minor, ver) = _live_studio_minor(cfg)
    node_path, prop = _LIVE_NODE, _LIVE_EXPR_PROP

    def children():
        node = core.describe_node(cfg, project, node_path)
        return frozenset(c.get("browse_name") or c.get("name") or ""
                         for c in (node.get("children") or []))

    before = children()
    if major_minor >= (1, 8):
        try:
            out = core.bridge_attach_expression(
                cfg, project, node_path, prop, "pow(2, 3) + 0xFF000000")
            assert out.get("ok") is True, (ver, out)
        finally:
            # leave the shared node as found (best effort)
            for extra in children() - before:
                _best_effort_delete(cfg, project, f"{node_path}/{extra}")
    else:
        with pytest.raises(core.BridgeWriteFailed) as exc:
            core.bridge_attach_expression(cfg, project, node_path, prop, _LIVE_POW_EXPR)
        msg = str(exc.value)
        assert "pow" in msg and "1.8" in msg, msg
        assert children() == before, "refused attach must not create an ExpressionEvaluator"


# ---- 1.0.9 5a: design-time NetLogic (kind="design_time") ---------------------
# Needs a live bridge (module gate) AND a Studio window that can take Ctrl+S /
# a right-click Execute (session 1). Writes one scratch .cs into the throwaway
# project's NetSolution and removes the node + file afterwards.

_DT_CLASS = _nm("ZzzDesignTimeLogic")
_DT_CS_TEMPLATE = """using FTOptix.NetLogic;
using UAManagedCore;

public class __CLS__ : BaseNetLogic
{
    [ExportMethod]
    public void ZzzPing()
    {
        Log.Info("__CLS__", "ZzzPing ran");
    }
}
"""


def _dt_cs(cls: str) -> str:
    """The design-time NetLogic C# source for the (suffixed) class ``cls``."""
    return _DT_CS_TEMPLATE.replace("__CLS__", cls)


def test_design_time_netlogic_export_method_after_save_live(live):
    """kind="design_time" mints BehaviourStartPriority (BehaviourStartPriorityVariableType,
    180); after optix_save, with NO reopen, execute answers
    export_method_not_loaded (reopen_required) per the #5 contract - the
    [ExportMethod] only materializes after a reopen."""
    _require_desktop()
    cfg, project = live
    node_path = f"Model/{_DT_CLASS}"
    cs_path = (core.resolve_project(cfg, project) / "ProjectFiles" / "NetSolution"
               / f"{_DT_CLASS}.cs")
    cs_path.write_text(_dt_cs(_DT_CLASS), encoding="utf-8")
    created = False
    try:
        try:
            out = core.bridge_create_netlogic(
                cfg, project, "Model", _DT_CLASS, kind="design_time")
        except core.BridgeWriteFailed as exc:
            pytest.fail(f"could not create a design-time NetLogic: {exc}", pytrace=False)
        created = True
        assert out.get("kind") == "design_time", out

        prio = core.describe_node(cfg, project, f"{node_path}/BehaviourStartPriority")
        # describe does not print the VariableType definition, only the value;
        # the Variable's own datatype/value are top-level fields (#39)
        assert "180" in _inner(prio.get("value")), prio
        assert prio.get("datatype"), prio

        saved = core.save(cfg, project)
        assert saved.get("saved") is True, saved

        # #5 contract: the [ExportMethod] is NOT materialized by save; it
        # appears only after the project is reopened. Until then execute
        # answers export_method_not_loaded (with a reopen nudge) and clicks
        # nothing. (A Studio that did load it runs it instead.)
        ran = core.execute_design_method(cfg, project, _DT_CLASS, "ZzzPing")
        if ran.get("error") == "export_method_not_loaded":
            assert ran.get("ok") is False, ran
            assert ran.get("reopen_required") is True, ran
            assert ran.get("nudge"), ran
        else:
            assert ran.get("ok") is True, ran
    finally:
        if created:
            _best_effort_delete(cfg, project, node_path)
        try:
            cs_path.unlink()
        except OSError:
            pass


_SAVE_CLASS = _nm("ZzzSaveAfterCreateLogic")


def test_save_after_design_time_netlogic_create_live(live):
    """ftx-mcp-h4a.2: a design-time NetLogic create, then save -> saved true.
    Any method counts (Ctrl+S or tree-click + Ctrl+S); the method
    used is logged so the bench run shows which path worked."""
    _require_desktop()
    cfg, project = live
    node_path = f"Model/{_SAVE_CLASS}"
    cs_path = (core.resolve_project(cfg, project) / "ProjectFiles" / "NetSolution"
               / f"{_SAVE_CLASS}.cs")
    cs_path.write_text(_dt_cs(_SAVE_CLASS), encoding="utf-8")
    created = False
    try:
        try:
            core.bridge_create_netlogic(
                cfg, project, "Model", _SAVE_CLASS, kind="design_time")
        except core.BridgeWriteFailed as exc:
            pytest.fail(f"could not create a design-time NetLogic: {exc}", pytrace=False)
        created = True
        saved = core.save(cfg, project)
        print(f"optix_save method={saved.get('method')!r} saved={saved.get('saved')!r} "
              f"attempts={saved.get('attempts')!r}")
        assert saved.get("saved") is True, saved
        assert saved.get("method") in ("ctrl_s", "tree_focus_ctrl_s"), saved
    finally:
        if created:
            _best_effort_delete(cfg, project, node_path)
        try:
            cs_path.unlink()
        except OSError:
            pass


_SAVE18_CLASS = _nm("ZzzSave18Logic")


@pytest.mark.slow_live
def test_save_studio18_immediate_save_after_netlogic_create_live(live):
    """PRD 0047 A (#54): on Studio 1.8, a design-time NetLogic create, then an
    IMMEDIATE save -> saved true within timeout + grace. Studio drops Ctrl+S
    during the NetSolution build that follows the create; optix_save re-sends
    until one lands. keystrokes_sent and write_latency_s are logged. Marked
    slow_live: the build can take ~30 s."""
    _require_desktop()
    cfg, project = live
    node_path = f"Model/{_SAVE18_CLASS}"
    cs_path = (core.resolve_project(cfg, project) / "ProjectFiles" / "NetSolution"
               / f"{_SAVE18_CLASS}.cs")
    cs_path.write_text(_dt_cs(_SAVE18_CLASS), encoding="utf-8")
    created = False
    try:
        try:
            created_out = core.bridge_create_netlogic(
                cfg, project, "Model", _SAVE18_CLASS, kind="design_time")
        except core.BridgeWriteFailed as exc:
            pytest.fail(f"could not create a design-time NetLogic: {exc}", pytrace=False)
        created = True
        assert created_out["netsolution_build"]["state"] == "building", created_out

        t0 = time.monotonic()
        saved = core.save(cfg, project)
        wall = time.monotonic() - t0
        print(f"optix_save saved={saved.get('saved')!r} method={saved.get('method')!r} "
              f"keystrokes_sent={saved.get('keystrokes_sent')!r} "
              f"write_latency_s={saved.get('write_latency_s')!r} wall={wall:.1f}s "
              f"attempts={saved.get('attempts')!r}")
        assert saved.get("saved") is True, saved
        assert saved.get("method") in ("ctrl_s", "tree_focus_ctrl_s"), saved
        # wall time <= timeout + grace (+ overhead for the PowerShell/UIA calls)
        budget = (float(cfg.save_timeout_seconds) + core._SAVE_WRITE_GRACE_S + 15.0)
        assert wall < budget, (wall, budget, saved)
    finally:
        if created:
            _best_effort_delete(cfg, project, node_path)
        try:
            cs_path.unlink()
        except OSError:
            pass


# ---- 1.0.9 5e: a rejected Execute is reported as rejected -------------------
# Needs the same live setup as the design-time test above. ZzzReject throws, which
# Studio logs and refuses; the exact log wording is unverified on a bench, so this
# test is the thing to run to confirm (or tune core._EXECUTE_REJECT_RE).

_REJ_CLASS = _nm("ZzzRejectLogic")
_REJ_CS_TEMPLATE = """using FTOptix.NetLogic;
using UAManagedCore;
using System;

public class __CLS__ : BaseNetLogic
{
    [ExportMethod]
    public void ZzzReject()
    {
        throw new InvalidOperationException("ZzzReject rejected on purpose");
    }
}
"""
_REJ_CS = _REJ_CS_TEMPLATE.replace("__CLS__", _REJ_CLASS)


def test_execute_rejected_call_reported_rejected_live(live):
    """Execute a design-time method Studio rejects: the result is state
    "rejected" carrying the Studio log line, and optix_studio_log can find it."""
    _require_desktop()
    cfg, project = live
    node_path = f"Model/{_REJ_CLASS}"
    cs_path = (core.resolve_project(cfg, project) / "ProjectFiles" / "NetSolution"
               / f"{_REJ_CLASS}.cs")
    cs_path.write_text(_REJ_CS, encoding="utf-8")
    created = False
    try:
        mtime_before = core._project_max_mtime(core.resolve_project(cfg, project))
        try:
            core.bridge_create_netlogic(cfg, project, "Model", _REJ_CLASS,
                                        kind="design_time")
        except core.BridgeWriteFailed as exc:
            pytest.fail(f"could not create a design-time NetLogic: {exc}", pytrace=False)
        created = True
        saved = _save_retrying_once(cfg, project)
        if saved.get("saved") is not True:
            # The save can land after core.save's own poll gave up: wait
            # (bounded) for the mtime change before asserting.
            assert _wait_for_mtime_change(cfg, project, mtime_before), saved
        else:
            assert saved.get("saved") is True, saved
        ran = core.execute_design_method(cfg, project, _REJ_CLASS, "ZzzReject")
        assert ran.get("ok") is False, ran
        if ran.get("error") == "export_method_not_loaded":
            # Studio has not loaded the new [ExportMethod] (no reopen): the
            # product correctly refuses before clicking anything.
            assert ran.get("reopen_required") is True, ran
        else:
            assert ran.get("state") == "rejected", ran
            assert ran.get("log_line"), ran
            tail = core.studio_log_tail(cfg, lines=200, contains="ZzzReject")
            assert tail.get("ok") is True and tail["returned_lines"] >= 1, tail
    finally:
        if created:
            _best_effort_delete(cfg, project, node_path)
        try:
            cs_path.unlink()
        except OSError:
            pass


# ---- 1.0.9 5b: create_variable variable_type / array_dimensions / properties --
# Needs a live bridge (module gate) and a throwaway project that has the
# RAEtherNetIP driver module (so the RAEtherNetIPTag VariableType exists);
# skips otherwise. Creates one node under Model and deletes it afterwards.

def test_create_variable_station_tag_array_live(live):
    """An RAEtherNetIPTag-typed ARRAY variable with a SymbolName property is
    created; dry_run resolves without writing; an unknown variable_type and bad
    array_dimensions are refused with structured errors."""
    cfg, project = live
    parent = _LIVE_MODEL_PARENT
    name = _nm("ZzzStationTagArr")
    path = parent + "/" + name
    _best_effort_delete(cfg, project, path)
    try:
        bad = core.bridge_create_variable(
            cfg, project, name, parent, "Int32", variable_type="ZzzNoSuchTagType")
        assert bad.get("ok") is False and bad.get("code") == "unknown_variable_type", bad
        bad = core.bridge_create_variable(
            cfg, project, name, parent, "Int32", array_dimensions=0)
        assert bad.get("ok") is False and bad.get("code") == "invalid_array_dimensions", bad

        dry = core.bridge_create_variable(
            cfg, project, name, parent, "Int32", variable_type="RAEtherNetIPTag",
            array_dimensions=4, properties={"SymbolName": "ZzzTags"}, dry_run=True)
        if dry.get("ok") is False and dry.get("code") == "unknown_variable_type":
            pytest.skip("throwaway project has no RAEtherNetIPTag VariableType "
                        "(RAEtherNetIP driver module not added)")
        assert dry.get("ok") is True and dry.get("dry_run") is True, dry
        with pytest.raises(core.NodeNotFound):
            core.describe_node(cfg, project, path)     # dry_run wrote nothing

        _best_effort_delete(cfg, project, path)    # a leftover would answer name_exists
        out = core.bridge_create_variable(
            cfg, project, name, parent, "Int32", variable_type="RAEtherNetIPTag",
            array_dimensions=4, properties={"SymbolName": "ZzzTags"})
        assert out.get("ok") is True, out
        assert out.get("variable_type") == "RAEtherNetIPTag", out
        assert out.get("array_dimensions") == [4], out

        desc = json.dumps(core.describe_node(cfg, project, path))
        # describe does not print the VariableType definition name
        assert "SymbolName" in desc and "ZzzTags" in desc, desc

        # an undeclared property is refused and nothing is created
        _best_effort_delete(cfg, project, path)
        bad = core.bridge_create_variable(
            cfg, project, name, parent, "Int32", variable_type="RAEtherNetIPTag",
            properties={"ZzzNotAProperty": "x"})
        assert bad.get("ok") is False and bad.get("code") == "unknown_property", bad
        with pytest.raises(core.NodeNotFound):
            core.describe_node(cfg, project, path)
    finally:
        _best_effort_delete(cfg, project, path)


# ---- 1.0.9 import_tags: design-time Tag Importer (ftx-mcp-vvk.3) -----------------
# Needs a CI project with an RA EtherNet/IP driver + station: name the station with
# FTX_LIVE_RA_STATION (e.g. CommDrivers/RAEtherNet_IPDriver1/Station1); skips when
# unset. Run once against Studio 1.8 (offline import) and once against 1.7 (the
# requires_optix_1_8 refusal); the running Studio version comes from
# FTX_LIVE_STUDIO_VERSION or Studio --version, NOT from the bridge's own report.
# The offline fixture is service/tests/fixtures/import_tags_sample.L5X.
# ONLINE mode has no live test: it reads a real controller and the ftx bench has
# none. It shares the importer path above and differs only in Mode=Online.
# NOT yet run on the bench (pending bench verification): on a Windows bench with
# Studio open and the bridge armed,
#   FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<proj> FTX_LIVE_RA_STATION=<station> \
#   python -m pytest service/tests/test_bridge_live.py -k import_tags -q

_IMPORT_TAGS_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "import_tags_sample.L5X"


def _live_ra_station_or_skip(cfg, project) -> str:
    station = os.environ.get("FTX_LIVE_RA_STATION", "").strip()
    if not station:
        pytest.skip("FTX_LIVE_RA_STATION unset — name an RA EtherNet/IP station in the "
                    "live project (e.g. CommDrivers/RAEtherNet_IPDriver1/Station1)")
    try:
        core.describe_node(cfg, project, station)
    except core.NodeNotFound:
        pytest.skip(f"FTX_LIVE_RA_STATION {station!r} does not exist in the live project")
    return station


def test_import_tags_offline_nested_member_live(live):
    """Studio 1.8: importing ONE nested UDT member from the L5X fixture adds it with
    its parents, and nothing for the false-prefix tag Motor10; a name absent from the
    file is reported in not_found; a repeat import adds nothing."""
    cfg, project = live
    if _live_running_studio_version(cfg) < (1, 8):
        pytest.skip("RA tag importer exists from Studio 1.8 (the 1.7 refusal is "
                    "test_import_tags_requires_optix_1_8_live)")
    station = _live_ra_station_or_skip(cfg, project)
    assert _IMPORT_TAGS_FIXTURE.is_file(), _IMPORT_TAGS_FIXTURE
    added: list = []
    try:
        out = core.bridge_import_tags(
            cfg, project, station, "offline", ["Motor1.Limits.High", "ZzzNoSuchTag"],
            file=str(_IMPORT_TAGS_FIXTURE))
        assert out.get("ok") is True, out
        added = list(out["added"])
        if not added:
            pytest.skip(f"station {station!r} already holds the fixture tags: {out}")
        assert out["not_found"] == ["ZzzNoSuchTag"], out
        assert isinstance(out["elapsed_ms"], int) and out["elapsed_ms"] >= 0, out
        joined = " ".join(added)
        assert "High" in joined, out                       # the requested nested member
        assert "Motor10" not in joined, out                # no false prefix
        assert all(a.startswith(station + "/") for a in added), out

        again = core.bridge_import_tags(
            cfg, project, station, "offline", ["Motor1.Limits.High"],
            file=str(_IMPORT_TAGS_FIXTURE))
        assert again.get("ok") is True and again["added"] == [], again
        assert again["already_present"], again
    finally:
        # delete the outermost added nodes (children go with them)
        for path in sorted(added, key=len):
            if not any(path.startswith(o + "/") for o in added if o != path):
                _best_effort_delete(cfg, project, path)


def test_import_tags_requires_optix_1_8_live(live):
    """Studio 1.7: an RA station is refused with requires_optix_1_8 and nothing is
    imported (single tool and batch op)."""
    cfg, project = live
    if _live_running_studio_version(cfg) >= (1, 8):
        pytest.skip("Studio is 1.8+: the RA importer is available (see the offline test)")
    station = _live_ra_station_or_skip(cfg, project)
    out = core.bridge_import_tags(
        cfg, project, station, "offline", ["Motor1.Limits.High"],
        file=str(_IMPORT_TAGS_FIXTURE))
    assert out.get("ok") is False and out.get("code") == "requires_optix_1_8", out
    assert (out.get("requires_version") or {}).get("min_version") == "1.8", out
    with pytest.raises(core.BridgeWriteFailed, match="requires_optix_1_8"):
        core._apply_one_edit(cfg, project, {
            "op": "import_tags", "station": station, "mode": "offline",
            "tags": ["Motor1.Limits.High"], "file": str(_IMPORT_TAGS_FIXTURE)})


# ---- ftx-mcp-aaw.9: import_tags on a station created THROUGH the MCP ----------------
# The station is made with bridge_create_object (driver + station), so it has no
# UI-created TagImporter: these prove the find-or-create path end to end. Skipped
# without a live bridge (the `live` fixture) or on Studio below 1.8 (no RA module);
# a driver/station creation error with a live bridge FAILS. NOT yet run on the bench (pending bench
# verification): on a Windows bench with Studio open and the bridge armed,
#   FTX_LIVE_PROJECT=<proj> python -m pytest service/tests/test_bridge_live.py -k mcp_station -q
# (set FTX_LIVE_STUDIO_VERSION when Studio --version is unavailable).

_MCP_STATION_DRIVER = _nm("ZzzAaw9Driver")
_MCP_STATION_NAME = _nm("ZzzAaw9Station")
# nodes the fixture expands for Motor1.Limits.High: nothing may come from these
_MCP_STATION_OTHER_SYMBOLS = ("Motor10", "Speed", "Low", "Mode")


@pytest.fixture
def mcp_station(live):
    """(cfg, project, station_path): a fresh RA EtherNet/IP driver+station created
    through bridge_create_object; deleted afterwards. FAILS if it cannot be built
    (skips only when the RA module is absent, i.e. Studio below 1.8)."""
    cfg, project = live
    driver_path = "CommDrivers/" + _MCP_STATION_DRIVER
    station_path = driver_path + "/" + _MCP_STATION_NAME
    _best_effort_delete(cfg, project, driver_path)
    _create_ra_driver_station(cfg, project, "CommDrivers", _MCP_STATION_DRIVER,
                              _MCP_STATION_NAME, station_path)
    try:
        yield cfg, project, station_path
    finally:
        _best_effort_delete(cfg, project, driver_path)


def _mcp_station_import(cfg, project, station, tags):
    return core.bridge_import_tags(cfg, project, station, "offline", tags,
                                   file=str(_IMPORT_TAGS_FIXTURE))


def _require_studio_1_8(cfg) -> None:
    if _live_running_studio_version(cfg) < (1, 8):
        pytest.skip("RA tag importer exists from Studio 1.8")


def _station_importer_children(cfg, project, station) -> list:
    """Browse names of any importer-like child directly under the station."""
    kids = core.describe_node(cfg, project, station).get("children") or []
    return [c.get("browse_name") for c in kids
            if "importer" in str(c.get("browse_name") or "").lower()]


def test_import_tags_mcp_station_added_holds_member_and_parents_live(mcp_station):
    """Importing Motor1.Limits.High from the fixture adds EXACTLY the Controller
    Tags/Motor1, Motor1/Limits and Motor1/Limits/High nodes, through a temporary
    importer (a station made by the MCP has none), and the station has no importer
    child afterwards."""
    cfg, project, station = mcp_station
    _require_studio_1_8(cfg)
    assert _station_importer_children(cfg, project, station) == []     # none to begin with
    out = _mcp_station_import(cfg, project, station, ["Motor1.Limits.High"])
    assert out.get("ok") is True, out
    base = station + "/Tags/Controller Tags"
    expected = {base + "/Motor1", base + "/Motor1/Limits", base + "/Motor1/Limits/High"}
    assert set(out["added"]) == expected and len(out["added"]) == 3, out
    assert out.get("tag_importer") == "temporary", out
    assert "tag_importer_left" not in out, out
    for a in out["added"]:
        core.describe_node(cfg, project, a)                           # each one exists
    assert _station_importer_children(cfg, project, station) == [], out   # temporary one deleted


def test_import_tags_mcp_station_not_found_live(mcp_station):
    """A name absent from the file is reported in not_found and adds nothing."""
    cfg, project, station = mcp_station
    _require_studio_1_8(cfg)
    out = _mcp_station_import(cfg, project, station, ["ZzzNoSuchTag"])
    assert out.get("ok") is True, out
    assert out["not_found"] == ["ZzzNoSuchTag"], out
    assert out["added"] == [], out


def test_import_tags_mcp_station_already_present_live(mcp_station):
    """Repeating an import adds nothing and reports the nodes as already_present."""
    cfg, project, station = mcp_station
    _require_studio_1_8(cfg)
    first = _mcp_station_import(cfg, project, station, ["Motor1.Limits.High"])
    assert first.get("ok") is True and first["added"], first
    again = _mcp_station_import(cfg, project, station, ["Motor1.Limits.High"])
    assert again.get("ok") is True, again
    assert again["added"] == [], again
    assert set(first["added"]) <= set(again["already_present"]), (first, again)


def test_import_tags_mcp_station_prefix_motor1_vs_motor10_live(mcp_station):
    """Requesting Motor1 never imports Motor10 (no false prefix match)."""
    cfg, project, station = mcp_station
    _require_studio_1_8(cfg)
    assert "Motor10" in _IMPORT_TAGS_FIXTURE.read_text(encoding="utf-8")   # fixture has the pair
    out = _mcp_station_import(cfg, project, station, ["Motor1"])
    assert out.get("ok") is True and out["added"], out
    assert any(a.endswith("/Motor1") for a in out["added"]), out
    assert not any("Motor10" in a for a in out["added"]), out
    names = [a.rsplit("/", 1)[-1] for a in out["added"]]
    assert "Motor10" not in names, out


def test_import_tags_mcp_station_save_and_build_check_zero_errors_live(mcp_station):
    """After an import, save succeeds and build_check reports 0 errors."""
    cfg, project, station = mcp_station
    _require_studio_1_8(cfg)
    out = _mcp_station_import(cfg, project, station, ["Motor1.Limits.High"])
    assert out.get("ok") is True and out["added"], out
    saved = _save_retrying_once(cfg, project)
    assert saved.get("saved") is True, saved
    bc = core.build_check(cfg, project)
    assert bc.get("error_count") == 0, bc


def test_import_tags_mcp_station_batch_entry_fields_live(mcp_station):
    """The same import through optix_bridge_edit reports added / already_present /
    not_found / elapsed_ms / tag_importer in its op_timings entry."""
    cfg, project, station = mcp_station
    _require_studio_1_8(cfg)
    ops = [{"op": "import_tags", "station": station, "mode": "offline",
            "tags": ["Motor1.Limits.High", "ZzzNoSuchTag"],
            "file": str(_IMPORT_TAGS_FIXTURE)}]
    out = core.bridge_edit(cfg, project, ops)
    entry = next(t for t in out["op_timings"] if t["op"] == "import_tags")
    assert entry["ok"] is True, out
    base = station + "/Tags/Controller Tags"
    assert set(entry["added"]) == {base + "/Motor1", base + "/Motor1/Limits",
                                   base + "/Motor1/Limits/High"}, entry
    assert entry["already_present"] == [], entry
    assert entry["not_found"] == ["ZzzNoSuchTag"], entry
    assert isinstance(entry["elapsed_ms"], int) and entry["elapsed_ms"] >= 0, entry
    assert entry["tag_importer"] == "temporary", entry
    assert _station_importer_children(cfg, project, station) == [], out


def test_import_tags_mcp_station_emulator_runs_live(mcp_station):
    """After an import + save, the project builds clean and the emulator comes up.
    Needs FTX_LIVE_HMI=1 and an interactive desktop (F5 goes to Studio)."""
    cfg, project, station = mcp_station
    _require_studio_1_8(cfg)
    if os.environ.get("FTX_LIVE_HMI") != "1":
        pytest.skip("emulator run: set FTX_LIVE_HMI=1 (emulator startable)")
    _require_desktop()
    out = _mcp_station_import(cfg, project, station, ["Motor1.Limits.High"])
    assert out.get("ok") is True and out["added"], out
    saved = _save_retrying_once(cfg, project)
    assert saved.get("saved") is True, saved
    assert core.build_check(cfg, project).get("error_count") == 0
    _stop_project_emulator(cfg, project)
    try:
        run = core.run_emulator(cfg, project)
        assert run.get("launched") is True and run.get("serving"), run
    finally:
        _stop_project_emulator(cfg, project)


def test_import_tags_mcp_station_dry_run_requires_optix_1_8_live(mcp_station):
    """Studio 1.7: the dry run refuses the RA station with requires_optix_1_8."""
    cfg, project, station = mcp_station
    if _live_running_studio_version(cfg) >= (1, 8):
        pytest.skip("Studio is 1.8+: the RA importer is available")
    ops = [{"op": "import_tags", "station": station, "mode": "offline",
            "tags": ["Motor1.Limits.High"], "file": str(_IMPORT_TAGS_FIXTURE)}]
    dry = core.bridge_edit(cfg, project, ops, dry_run=True)
    codes = [e.get("code") for e in dry["report"]["errors"]]
    assert codes == ["requires_optix_1_8"], dry
    assert dry.get("applied") == 0, dry


def test_import_tags_dry_run_requires_optix_1_8_on_1_7_live(live):
    """Studio 1.7: the import_tags DRY RUN refuses an RA station with exactly one
    requires_optix_1_8 error and applies nothing; the apply route agrees. The
    station is made through the MCP when the 1.7 RA module allows it, else it is
    FTX_LIVE_RA_STATION; with neither the test FAILS (it cannot prove the refusal)."""
    cfg, project = live
    ver = _live_running_studio_version(cfg)
    if ver >= (1, 8):
        pytest.skip("Studio is 1.8+: the RA importer is available")
    jku_driver, jku_station = _nm("ZzzJku6Driver"), _nm("ZzzJku6Station")
    driver_path = "CommDrivers/" + jku_driver
    station = driver_path + "/" + jku_station
    made = False
    _best_effort_delete(cfg, project, driver_path)
    try:
        try:
            core.bridge_create_object(cfg, project, "CommDrivers", jku_driver, _RA_DRIVER_TYPE)
            core.bridge_create_object(cfg, project, driver_path, jku_station, _RA_STATION_TYPE)
            core.describe_node(cfg, project, station)
            made = True
        except (core.BridgeWriteFailed, core.NodeNotFound):
            _best_effort_delete(cfg, project, driver_path)
            station = os.environ.get("FTX_LIVE_RA_STATION", "").strip()
            if not station:
                pytest.fail("Studio 1.7 cannot create an RA station through the MCP and "
                            "FTX_LIVE_RA_STATION is unset: no station to refuse", pytrace=False)
        ops = [{"op": "import_tags", "station": station, "mode": "offline",
                "tags": ["Motor1.Limits.High"], "file": str(_IMPORT_TAGS_FIXTURE)}]
        dry = core.bridge_edit(cfg, project, ops, dry_run=True)
        assert [e.get("code") for e in dry["report"]["errors"]] == ["requires_optix_1_8"], dry
        assert dry.get("applied") == 0, dry
        out = core.bridge_import_tags(cfg, project, station, "offline", ["Motor1.Limits.High"],
                                      file=str(_IMPORT_TAGS_FIXTURE))
        assert out.get("ok") is False and out.get("code") == "requires_optix_1_8", out
    finally:
        if made:
            _best_effort_delete(cfg, project, driver_path)


# ---- 1.0.9 5g: optix_observe(mode="dom_text") --------------------------------
# Needs the project running in the emulator/WebUI AND chrome-cdp up. Set
# FTX_LIVE_DOM_TEXT_LABEL to a label string the running UI is known to show.
# NOT yet run on the bench (pending bench verification).

def test_observe_dom_text_returns_known_label_live(live):
    """dom_text returns document.body.innerText containing a known label."""
    cfg, project = live
    label = os.environ.get("FTX_LIVE_DOM_TEXT_LABEL")
    if not label:
        pytest.skip("FTX_LIVE_DOM_TEXT_LABEL unset - name a label the running UI shows")
    out = core.cdp_dom_text_runtime(cfg, project=project)
    assert out["state"] == "succeeded", out
    assert label in out["text"], out


# ---- 1.0.9 #3: GridLayout Rows/Columns typed string[] write -------------------
# Needs FTX_GRID_STRING_ARRAY=1 in the service env AND a THROWAWAY project (CI_*):
# if the typed write kills Studio the bench run must leave the flag off and record
# the WER stack in docs/field-findings (see CHANGELOG 1.0.9). NOT yet run on the
# bench (pending bench verification):
#   FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<CI_ project> FTX_GRID_STRING_ARRAY=1 \
#   FTX_LIVE_HMI=1 /opt/venv/bin/python -m pytest \
#   service/tests/test_bridge_live.py -k gridlayout_rows_columns -q

def test_gridlayout_rows_columns_typed_write_live(live, tmp_path):
    """Create a GridLayout, set Rows and Columns via the typed path, save, render
    non-blank, and confirm Studio is still alive."""
    _require_desktop()
    cfg, project = live
    if not core.Config.from_env().enable_grid_string_array:
        pytest.skip("FTX_GRID_STRING_ARRAY not enabled - typed string[] path is off")
    import dataclasses
    cfg = dataclasses.replace(cfg, enable_grid_string_array=True)
    parent = os.environ.get("FTX_LIVE_GRID_PARENT", "UI/MainWindow")
    name = _nm("CIGridTyped")
    path = f"{parent}/{name}"
    pid_before = core.bridge_state(cfg, force=True).get("studio_pid")
    try:
        core.bridge_delete_node(cfg, project, path)
    except (core.BridgeWriteFailed, core.NodeNotFound):
        pass
    try:
        out = core.bridge_create_widget(cfg, project, parent, name, "GridLayout")
        assert out.get("ok") is True, out
        for prop in ("Rows", "Columns"):
            res = core.bridge_set_property(cfg, project, path, prop, ["1*", "1*"])
            assert res.get("ok") is True, res
            assert res.get("via") == "typed-string-array", res
        # a non-GridLayout and a non-Rows/Columns array stay refused
        with pytest.raises(core.BridgeWriteFailed):
            core.bridge_set_property(cfg, project, parent, "Columns", ["1*"])
        assert core.save(cfg, project).get("saved") is True
        # Studio survived the write + save
        st = core.bridge_state(cfg, force=True)
        assert st.get("available") is True, f"Studio/bridge not alive after typed write: {st}"
        assert st.get("studio_pid") == pid_before, (
            f"Studio restarted/died during the typed write: {pid_before} -> {st}")
        assert core.describe_node(cfg, project, path)
        if os.environ.get("FTX_LIVE_HMI") == "1":
            shot_path = str(tmp_path / "grid.jpg")
            shot = core.cdp_screenshot_runtime(
                cfg, save_path=shot_path, quality=70, settle_seconds=2.0,
                project=project)
            assert shot.get("state") == "succeeded", shot
            # a blanked page is a tiny near-uniform JPEG
            assert pathlib.Path(shot_path).stat().st_size > 3000, "page rendered blank"
    finally:
        try:
            core.bridge_delete_node(cfg, project, path)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass


# ---- chunk overrides survive action="continue" (ftx-mcp-xk7.1) --------------

def test_batch_continue_honours_chunk_ops_override_live(live):
    """A batch started with chunk_ops=2 must not apply all remaining ops on
    continue. Uses harmless set_property writes of the node's current Width."""
    cfg, project = live
    node = core.describe_node(cfg, project, _LIVE_NODE)
    width = next((p.get("value") for p in node.get("properties") or []
                  if p.get("name") == "Width"), None)
    if width is None:
        pytest.skip(f"{_LIVE_NODE} exposes no Width to rewrite")
    ops = [{"op": "set_property", "path": _LIVE_NODE, "name": "Width",
            "value": str(width)} for _ in range(6)]

    first = core.bridge_edit(cfg, project, ops, chunk_ops=2)
    assert first["state"] == "chunked" and first["applied"] == 2, first
    second = core.bridge_edit_continue(cfg, project, first["batch_id"])
    assert second["state"] == "chunked" and second["applied"] == 4, second
    third = core.bridge_edit_continue(cfg, project, first["batch_id"])
    assert third["state"] == "succeeded" and third["applied"] == 6, third


# ---- create_variable datatype validation (ftx-mcp-xk7.3) --------------------

def test_dry_run_create_variable_datatype_matches_bridge_live(live):
    """dry_run must refuse an unsupported datatype and accept every supported
    one (nothing is written: dry_run only)."""
    cfg, project = live
    bad = core.bridge_edit(cfg, project, [
        {"op": "create_variable", "parent": "Model", "name": "XkBad",
         "datatype": "NotAType"}], dry_run=True)
    assert bad["report"]["ok"] is False, bad
    assert any(e.get("code") == "invalid_datatype" for e in bad["report"]["errors"]), bad

    good = core.bridge_edit(cfg, project, [
        {"op": "create_variable", "parent": "Model", "name": f"XkOk{i}", "datatype": dt}
        for i, dt in enumerate(core.CREATE_VARIABLE_DATATYPES)], dry_run=True)
    assert not any(e.get("code") == "invalid_datatype"
                   for e in good["report"].get("errors", [])), good


def test_health_project_path_is_folder_holding_optix(live, monkeypatch):
    """project_path from health is the folder containing <project>.optix (never
    the ProjectFiles subfolder), and the registry lists the armed project."""
    cfg, project = live
    bridges = core.list_bridges(cfg, force=True)
    mine = [b for b in bridges
            if (b.get("project") or "").strip().lower() == project.strip().lower()]
    assert mine, f"no bridge serves {project!r}"
    pp = mine[0].get("project_path")
    assert pp, "bridge reported no project_path"
    d = pathlib.Path(pp)
    if str(pp).lower().endswith(".optix"):
        d = d.parent
    assert d.name.lower() != "projectfiles", f"project_path ends in ProjectFiles: {pp}"
    assert d.is_dir(), f"project_path dir missing: {d}"
    assert (d / f"{project}.optix").is_file() or len(list(d.glob("*.optix"))) == 1, (
        f"{d} does not contain {project}.optix")
    # conftest redirects LOCALAPPDATA to an empty dir; the registry the bridge
    # wrote lives under the real one, so restore it for this call.
    if _REAL_LOCALAPPDATA is not None:
        monkeypatch.setenv("LOCALAPPDATA", _REAL_LOCALAPPDATA)
    else:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
    cands = core._registry_open_candidates()
    assert any(project.strip().lower() in c["names"] for c in cands), (
        f"_registry_open_candidates() does not list {project!r}: "
        f"{[c['project'] for c in cands]}")


# ---- 1.0.9 #25 / #23: honest open-after-new, observe after emulator restart --

def test_open_after_new_reports_truthfully_live(live):
    """`project_new` leaves Studio open on the new project; `project_open` on it
    must then answer truthfully (PRD 0028 #25): `already_open` with that
    Studio's pid and NO second Studio launched - never a bare `opened` for a
    Home-screen Recent entry. If it cannot say so it must be `not_opened` with a
    `holder_pid`, never ok:true.

    Creates a throw-away project under the projects root (named
    zzOpenTruth<ts><suffix>), closes only the Studio this test's `new` launched
    and then deletes the project folder.
    Skipped without a live Studio (module gate) or without an input desktop.
    """
    import time

    _require_desktop()
    cfg, _project = live
    name = f"zzOpenTruth{int(time.time())}{_RUN_SFX}"
    created = core.project_new(cfg, name)
    if not created.get("ok"):
        pytest.skip(f"project_new could not create a scratch project: {created!r}")
    new_pid = created.get("pid")
    try:
        before = set(core._studio_pids_now())
        out = core.project_open(cfg, name, wait_seconds=30.0)
        after = set(core._studio_pids_now())
        if out.get("ok"):
            assert out.get("state") in ("already_open", "opened"), out
            # The project is already loaded by the Studio `new` left open, so
            # a truthful answer launches nothing.
            assert out.get("state") == "already_open", out
            assert out.get("launched") is False, out
            assert after <= before, (
                f"open launched a second Studio: new pids {sorted(after - before)}")
        else:
            assert out.get("state") == "not_opened", out
            assert out.get("holder_pid"), out
            assert out.get("reason"), out
    finally:
        if new_pid:
            core._close_launched_studio(new_pid)
        _remove_scratch_project(cfg, name)


def test_observe_recovers_after_emulator_restart_live(live):
    """After `restart_emulator` the CDP page target can be stale/hung; observe
    must recover (one reload + one retry, `cdp_recovered: True` when it had to)
    and return a real screenshot instead of hanging or erroring (PRD 0028 #23).
    Needs FTX_LIVE_HMI=1 (emulator restartable, Chrome at OPTIX_CDP_URL)."""
    if os.environ.get("FTX_LIVE_HMI") != "1":
        pytest.skip("observe-after-restart test: set FTX_LIVE_HMI=1 with the "
                    "emulator restartable and Chrome at OPTIX_CDP_URL")
    _require_desktop()
    cfg, project = live
    first = core.cdp_screenshot_runtime(cfg, project=project)
    if first.get("state") != "succeeded":
        pytest.skip(f"no working CDP screenshot before the restart: {first!r}")
    restarted = core.restart_emulator(cfg, project)
    assert restarted.get("serving"), restarted
    out = core.cdp_screenshot_runtime(cfg, project=project)
    assert out.get("state") == "succeeded", out
    # cdp_recovered is present (True) only when a recovery was needed; it must
    # never be present-and-false.
    assert out.get("cdp_recovered", True) is True, out


# ---- #27: design-time NetLogic for an ALREADY-COMPILED class ----------------
# Before the fix, parentNode.Add(o) ran on the bridge thread with no session and
# Studio died (c0000374) as soon as it instantiated the design-time behaviour of
# a class that already existed compiled in the NetSolution.
_LIVE_COMPILED_NETLOGIC_CLASS = os.environ.get("FTX_LIVE_COMPILED_NETLOGIC_CLASS", "")


def test_create_design_time_netlogic_for_compiled_class_live(live):
    """Needs FTX_LIVE_COMPILED_NETLOGIC_CLASS = a NetLogic class already compiled
    into the open project's NetSolution (not already a node under the scratch
    parent). Creates a kind=design_time node of that name and asserts Studio is
    still alive (bridge health answers) afterwards."""
    cfg, project = live
    cls = _LIVE_COMPILED_NETLOGIC_CLASS
    if not cls:
        pytest.skip("set FTX_LIVE_COMPILED_NETLOGIC_CLASS to a class already "
                    "compiled in the NetSolution")
    parent = _LIVE_SCRATCH_PANEL
    try:
        core.describe_node(cfg, project, parent)
    except core.NodeNotFound:
        pytest.skip(f"no scratch node at {parent!r} - set FTX_LIVE_SCRATCH_PANEL")
    path = f"{parent}/{cls}"
    _best_effort_delete(cfg, project, path)
    created = False
    try:
        out = core.bridge_create_netlogic(cfg, project, parent, cls, kind="design_time")
        created = True
        assert out.get("ok") is not False, out
        assert out.get("kind") == "design_time", out
        health = core._bridge_http(cfg, "/bridge/health")
        assert health[0] == 200, health
        _assert_bridge_still_serves(cfg, project)
    finally:
        if created:
            _best_effort_delete(cfg, project, path)


# ---- PRD 0031 (functional round r3): #32 #33 #23 #34 #35 -------------------
# Post-merge bench run on Studio 1.7.4.32 and 1.8.0.555. Same module gate as the
# rest of this file (FTX_LIVE_BRIDGE=1), so these are skipped cleanly on CI/Linux.

def _r3_write_netlogic_source(cfg, project, cls: str, marker: str):
    """Write a user-authored NetSolution/<cls>.cs carrying ``marker``; returns the
    (path, text) pair so a test can assert the user's code survived."""
    text = f"""using FTOptix.NetLogic;
using UAManagedCore;

public class {cls} : BaseNetLogic
{{
    // {marker}
    [ExportMethod]
    public void UserMethod()
    {{
        Log.Info("{cls}", "{marker}");
    }}
}}
"""
    cs_path = (core.resolve_project(cfg, project) / "ProjectFiles" / "NetSolution"
               / f"{cls}.cs")
    cs_path.parent.mkdir(parents=True, exist_ok=True)
    cs_path.write_text(text, encoding="utf-8")
    return cs_path, text


def _r3_link_property(cfg, project, label_path):
    """The DynamicLink property row of ``<label>/Text`` as describe_node reports it."""
    out = core.describe_node(cfg, project, f"{label_path}/Text")
    for p in out.get("properties") or []:
        if p.get("name") == "DynamicLink":
            return p, out
    return None, out


def test_r3_32_relative_link_not_dangling_unless_target_deleted_live(live):
    """#32/#29/#2: a good RELATIVE NodePath link must NOT be reported as a
    dangling_link by describe_node; after the target is really deleted the same
    link MUST be reported dangling (target_missing True + a dangling_link
    diagnostic)."""
    cfg, project = live
    scratch = _LIVE_SCRATCH_PANEL
    try:
        core.describe_node(cfg, project, scratch)
    except core.NodeNotFound:
        pytest.skip(f"no scratch node at {scratch!r} - set FTX_LIVE_SCRATCH_PANEL")
    tgt_name, lbl_name = _nm("ZzzR3RelTarget"), _nm("ZzzR3RelLabel")
    tgt_path, lbl_path = f"Model/{tgt_name}", f"{scratch}/{lbl_name}"
    text_path = f"{lbl_path}/Text"
    for p in (lbl_path, tgt_path):
        _best_effort_delete(cfg, project, p)
    try:
        core.bridge_create_variable(cfg, project, tgt_name, "Model", "String")
        core.bridge_create_widget(cfg, project, scratch, lbl_name, "Label")
        # The link is anchored at the described node (<label>/Text): one ".."
        # per path segment climbs to the project root.
        ups = len([s for s in text_path.split("/") if s])
        rel = "../" * ups + tgt_path
        core.bridge_bind_property(cfg, project, lbl_path, "Text", raw_path=rel)

        prop, out = _r3_link_property(cfg, project, lbl_path)
        if prop is None:
            pytest.skip("bridge describe did not surface a DynamicLink row on Text")
        assert prop.get("target_missing") is not True, (prop, out.get("diagnostics"))
        codes = [d.get("code") for d in out.get("diagnostics") or []]
        assert "dangling_link" not in codes, (rel, out.get("diagnostics"))

        core.bridge_delete_node(cfg, project, tgt_path)
        prop, out = _r3_link_property(cfg, project, lbl_path)
        assert prop is not None, out
        assert prop.get("target_missing") is True, prop
        codes = [d.get("code") for d in out.get("diagnostics") or []]
        assert "dangling_link" in codes, out
    finally:
        for p in (lbl_path, tgt_path):
            _best_effort_delete(cfg, project, p)


def test_r3_33_netlogic_delete_keeps_user_source_live(live):
    """#33: deleting the last NetLogic node bound to a class must leave the user's
    NetSolution/<Class>.cs byte-identical on disk, and re-creating the node must
    not replace it with Studio's default Method1 template."""
    cfg, project = live
    cls = _nm("ZzzR3KeepSrcLogic")
    node_path = f"Model/{cls}"
    _best_effort_delete(cfg, project, node_path)
    cs_path, text = _r3_write_netlogic_source(cfg, project, cls, "r3-33-user-code")
    created = False
    try:
        try:
            core.bridge_create_netlogic(cfg, project, "Model", cls)
        except core.BridgeWriteFailed as exc:
            pytest.skip(f"could not create a NetLogic: {exc}")
        created = True
        assert cs_path.read_text(encoding="utf-8") == text, (
            "create_netlogic replaced the user's source with a template")

        out = core.bridge_delete_node(cfg, project, node_path)
        created = False
        assert out.get("ok") is not False, out
        assert cs_path.is_file(), f"{cs_path} was removed by the NetLogic delete: {out}"
        assert cs_path.read_text(encoding="utf-8") == text, out
        if out.get("netlogic_source"):  # Studio did remove it; we put it back
            assert out["netlogic_source"].get("preserved") is True, out
            assert any(w.get("code") == "netlogic_source_preserved"
                       for w in out.get("warnings") or []), out

        # A later create binds to the preserved class instead of regenerating.
        core.bridge_create_netlogic(cfg, project, "Model", cls)
        created = True
        assert cs_path.read_text(encoding="utf-8") == text
    finally:
        if created:
            _best_effort_delete(cfg, project, node_path)
        try:
            cs_path.unlink()
        except OSError:
            pass


def test_r3_23_observe_after_emulator_restart_live(live, monkeypatch):
    """#23: after the emulator is restarted, optix_observe's CDP path recovers
    (detached page target replaced) on EVERY restart, not just the first, and
    returns a real screenshot. Needs FTX_LIVE_HMI=1 (emulator restartable,
    Chrome at OPTIX_CDP_URL)."""
    if os.environ.get("FTX_LIVE_HMI") != "1":
        pytest.skip("observe-after-restart test: set FTX_LIVE_HMI=1 with the "
                    "emulator restartable and Chrome at OPTIX_CDP_URL")
    _require_desktop()
    # conftest redirects LOCALAPPDATA to an empty dir; the emulator/bridge
    # registry lives under the real one, so restore it for the restart.
    if _REAL_LOCALAPPDATA is not None:
        monkeypatch.setenv("LOCALAPPDATA", _REAL_LOCALAPPDATA)
    else:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
    cfg, project = live
    first = core.cdp_screenshot_runtime(cfg, project=project)
    if first.get("state") != "succeeded":
        pytest.skip(f"no working CDP screenshot before the restart: {first!r}")
    for cycle in range(2):
        restarted = core.restart_emulator(cfg, project)
        assert restarted.get("serving"), (cycle, restarted)
        out = core.cdp_screenshot_runtime(cfg, project=project)
        assert out.get("state") == "succeeded", out
        assert out.get("cdp_recovered", True) is True, out
    from service import _cdp
    probe = _cdp.page_probe(cfg.cdp_url)
    assert probe["healthy"] is True, probe


_R3_SLOW_CLASS = _nm("ZzzR3SlowCompileLogic")


def _r3_slow_cs(cls: str) -> str:
    """A NetLogic class with many methods so Studio's compile is noticeably slow."""
    body = "\n".join(
        f"    private int Work{i}(int x) {{ var s = 0; for (int k = 0; k < x; k++) "
        f"{{ s += k * {i + 1}; }} return s; }}" for i in range(400))
    return f"""using FTOptix.NetLogic;
using UAManagedCore;

public class {cls} : BaseNetLogic
{{
    [ExportMethod]
    public void Ping() {{ Log.Info("{cls}", "ping " + Work1(3)); }}
{body}
}}
"""


@pytest.mark.parametrize("post_timeout", [None, 0.3], ids=["default_timeout", "forced_timeout"])
def test_r3_34_create_netlogic_slow_compile_live(live, monkeypatch, post_timeout):
    """#34: create_netlogic for a not-yet-compiled class whose compile outlasts
    the POST must end with exactly ONE node and ok:true - never a spurious
    name_exists from a blind re-send. ``forced_timeout`` shrinks the POST timeout
    so the timeout -> verify-by-GET path runs against the real bridge."""
    cfg, project = live
    cls = _R3_SLOW_CLASS + ("Forced" if post_timeout else "")
    node_path = f"Model/{cls}"
    _best_effort_delete(cfg, project, node_path)
    cs_path = (core.resolve_project(cfg, project) / "ProjectFiles" / "NetSolution"
               / f"{cls}.cs")
    cs_path.parent.mkdir(parents=True, exist_ok=True)
    cs_path.write_text(_r3_slow_cs(cls), encoding="utf-8")
    if post_timeout is not None:
        monkeypatch.setattr(core, "NETLOGIC_POST_TIMEOUT", post_timeout)
    created = False
    try:
        try:
            out = core.bridge_create_netlogic(cfg, project, "Model", cls)
        except core.BridgeWriteFailed as exc:
            assert "name_exists" not in str(exc), (
                f"create_netlogic was re-sent after a timeout: {exc}")
            raise
        created = True
        assert out.get("ok") is not False, out
        assert out.get("created_path") == node_path, out
        if post_timeout is not None and out.get("verified_after_timeout"):
            assert any(w.get("code") == "create_netlogic_timeout_verified"
                       for w in out.get("warnings") or []), out
        node = core.describe_node(cfg, project, node_path)
        assert node.get("path") == node_path, node
        _assert_bridge_still_serves(cfg, project)
    finally:
        if created:
            _best_effort_delete(cfg, project, node_path)
        try:
            cs_path.unlink()
        except OSError:
            pass


def test_r3_35_duplicate_configured_port_no_emulator_not_a_clash_live(live, monkeypatch):
    """#35: two projects merely CONFIGURED for the same web port, with no
    emulator running and nothing listening, must not be reported as a clash.
    Uses FTX_LIVE_PORT_TWIN_PROJECT as the second project when given; otherwise
    a synthetic twin that reports this project's own port."""
    cfg, project = live
    port = core.discover_web_port(cfg, project).get("port")
    if port is None:
        pytest.skip(f"cannot discover the web port of {project!r}")
    _stop_project_emulator(cfg, project)     # earlier live tests leave it running
    if core._emulator_entries():
        pytest.skip("an emulator is running - stop it so the port is truly idle")
    if core._port_has_listener(cfg, port):
        pytest.skip(f"something is listening on web port {port}")
    twin = os.environ.get("FTX_LIVE_PORT_TWIN_PROJECT")
    if twin:
        if core.discover_web_port(cfg, twin).get("port") != port:
            pytest.skip(f"{twin!r} is not configured for web port {port}")
    else:
        twin = _nm("ZzzR3PortTwin")
        real = core.discover_web_port
        monkeypatch.setattr(
            core, "discover_web_port",
            lambda c, p: {"port": port} if p == twin else real(c, p))
    assert core.detect_web_port_clash(cfg, project, others=[twin]) is None


def test_r3_35_run_emulator_not_refused_for_idle_duplicate_port_live(live):
    """#35: with a duplicate configured port but no running emulator, run_emulator
    must start the emulator (no reason_code web_port_clash). Needs FTX_LIVE_HMI=1
    and a project pair sharing the port (FTX_LIVE_PORT_TWIN_PROJECT); stops the
    emulator it started."""
    if os.environ.get("FTX_LIVE_HMI") != "1":
        pytest.skip("run_emulator test: set FTX_LIVE_HMI=1 (emulator startable)")
    twin = os.environ.get("FTX_LIVE_PORT_TWIN_PROJECT")
    if not twin:
        pytest.skip("set FTX_LIVE_PORT_TWIN_PROJECT to a second project "
                    "configured for the same web port")
    _require_desktop()
    cfg, project = live
    port = core.discover_web_port(cfg, project).get("port")
    if port is None or core.discover_web_port(cfg, twin).get("port") != port:
        pytest.skip(f"{project!r} and {twin!r} do not share a configured web port")
    _stop_project_emulator(cfg, project)     # earlier live tests leave it running
    if core._emulator_entries():
        pytest.skip("an emulator is already running")
    try:
        out = core.run_emulator(cfg, project)
        assert out.get("reason_code") != "web_port_taken", out
        assert out.get("serving"), out
    finally:
        try:
            core.stop_emulator(cfg, project=project)
        except Exception:
            pass


# ---- PRD 0033 (ftx-mcp-7hp.4): NetLogic delete after edit; observe after restart --
# Same module gate as the rest of this file (FTX_LIVE_BRIDGE=1): skipped with no
# live bridge. The restart test additionally needs FTX_LIVE_HMI=1.

_R4_EDIT_MARKER = "r4-delete-after-edit"


def _r4_netlogic_delete_after_edit(live, via: str, kind: str) -> None:
    """Create a NetLogic, modify its generated .cs, delete the node (``via`` =
    ``edit`` -> optix_bridge_edit delete op, ``delete_node`` ->
    optix_bridge_delete_node) and assert the node is gone, the modified source
    is preserved and the delete is not reported as a failure."""
    cfg, project = live
    cls = (f"ZzzR4Del{via.title().replace('_', '')}"
           f"{kind.title().replace('_', '')}{_nm('')}Logic")
    node_path = f"Model/{cls}"
    _best_effort_delete(cfg, project, node_path)
    cs_path = (core.resolve_project(cfg, project) / "ProjectFiles" / "NetSolution"
               / f"{cls}.cs")
    cs_path.parent.mkdir(parents=True, exist_ok=True)
    cs_path.unlink(missing_ok=True)
    created = False
    try:
        try:
            _with_bridge_recovery(
                cfg, project,
                lambda: core.bridge_create_netlogic(cfg, project, "Model", cls, kind=kind))
        except core.BridgeWriteFailed as exc:
            pytest.skip(f"could not create a {kind} NetLogic: {exc}")
        created = True
        # The "edit": replace the generated template with user code.
        text = f"""using FTOptix.NetLogic;
using UAManagedCore;

public class {cls} : BaseNetLogic
{{
    // {_R4_EDIT_MARKER}
    [ExportMethod]
    public void EditedMethod()
    {{
        Log.Info("{cls}", "{_R4_EDIT_MARKER}");
    }}
}}
"""
        cs_path.write_text(text, encoding="utf-8")

        if via == "edit":
            # Studio recompiles the edited NetLogic and the bridge can drop
            # for a moment (no_bridge_for_project): wait for it, then retry.
            out = _with_bridge_recovery(
                cfg, project,
                lambda: core.bridge_edit(cfg, project, [{"op": "delete", "path": node_path}]))
            created = False
            assert out.get("state") == "succeeded", out
            assert out.get("applied") == 1, out
            assert not out.get("failed_op"), out
        else:
            out = _with_bridge_recovery(
                cfg, project, lambda: core.bridge_delete_node(cfg, project, node_path))
            created = False
            assert out.get("ok") is not False, out
        with pytest.raises(core.NodeNotFound):
            _with_bridge_recovery(
                cfg, project, lambda: core.describe_node(cfg, project, node_path))
        assert cs_path.is_file(), f"{cs_path} was removed by the NetLogic delete: {out}"
        assert cs_path.read_text(encoding="utf-8") == text, out
        _assert_bridge_still_serves(cfg, project)
    finally:
        if created:
            _best_effort_delete(cfg, project, node_path)
        try:
            cs_path.unlink()
        except OSError:
            pass


@pytest.mark.parametrize("kind", ["runtime", "design_time"])
def test_netlogic_edit_after_modify_preserves_source_live(live, kind):
    """optix_bridge_edit delete of a NetLogic whose .cs was modified: node gone,
    source preserved, batch reported succeeded (no false failure)."""
    _r4_netlogic_delete_after_edit(live, "edit", kind)


@pytest.mark.parametrize("kind", ["runtime", "design_time"])
def test_netlogic_delete_node_after_modify_preserves_source_live(live, kind):
    """optix_bridge_delete_node of a NetLogic whose .cs was modified: node gone,
    source preserved, delete reported successful (no false failure)."""
    _r4_netlogic_delete_after_edit(live, "delete_node", kind)


_RACE_ITERATIONS = 3


def _unique_suffix() -> str:
    """Short per-call suffix so a race test never collides with a leftover node
    or .cs from an earlier (crashed or parallel) run."""
    return uuid.uuid4().hex[:8]


def _netlogic_delete_race_after_create(live, via: str, kind: str) -> None:
    """Race variant: write the NetLogic .cs *immediately* after create_netlogic
    returns (no settle, no wait for Studio's compile/refresh), then delete the
    node (``via`` = ``edit`` -> batch delete op, ``delete_node`` ->
    optix_bridge_delete_node). Repeated ``_RACE_ITERATIONS`` times, each with a
    FRESH class name (re-creating the same name inside the late-guard window is
    a deliberate source change, not a race); each time the node must be gone
    and the on-disk bytes must equal the pre-delete bytes."""
    cfg, project = live
    prefix = (f"ZzzRaceDel{via.title().replace('_', '')}"
              f"{kind.title().replace('_', '')}{_unique_suffix()}x")
    ns_dir = core.resolve_project(cfg, project) / "ProjectFiles" / "NetSolution"
    ns_dir.mkdir(parents=True, exist_ok=True)
    touched: list[tuple[str, pathlib.Path]] = []  # (node_path, cs_path) to clean up
    live_node: str | None = None  # node currently created and not yet deleted
    try:
        for i in range(_RACE_ITERATIONS):
            cls = f"{prefix}{i}Logic"
            node_path = f"Model/{cls}"
            cs_path = ns_dir / f"{cls}.cs"
            _best_effort_delete(cfg, project, node_path)
            cs_path.unlink(missing_ok=True)
            touched.append((node_path, cs_path))
            marker = f"race-delete-after-create-{via}-{i}"
            data = f"""using FTOptix.NetLogic;
using UAManagedCore;

public class {cls} : BaseNetLogic
{{
    // {marker}
    [ExportMethod]
    public void RaceMethod()
    {{
        Log.Info("{cls}", "{marker}");
    }}
}}
""".encode("utf-8")
            try:
                _with_bridge_recovery(
                    cfg, project,
                    lambda: core.bridge_create_netlogic(
                        cfg, project, "Model", cls, kind=kind))
            except core.BridgeWriteFailed as exc:
                # The bridge is up (the ``live`` fixture bound to it), so a failed
                # setup is a real failure, not a skip.
                pytest.fail(f"could not create a {kind} NetLogic: {exc}",
                            pytrace=False)
            live_node = node_path
            # No settle: write straight after the create returns.
            cs_path.write_bytes(data)
            pre_delete = cs_path.read_bytes()
            assert pre_delete == data, (i, "pre-delete write did not stick")

            if via == "edit":
                out = _with_bridge_recovery(
                    cfg, project,
                    lambda: core.bridge_edit(
                        cfg, project, [{"op": "delete", "path": node_path}]))
                live_node = None
                assert out.get("state") == "succeeded", (i, out)
                assert out.get("applied") == 1, (i, out)
                assert not out.get("failed_op"), (i, out)
            else:
                out = _with_bridge_recovery(
                    cfg, project,
                    lambda: core.bridge_delete_node(cfg, project, node_path))
                live_node = None
                assert out.get("ok") is not False, (i, out)
            with pytest.raises(core.NodeNotFound):
                _with_bridge_recovery(
                    cfg, project, lambda: core.describe_node(cfg, project, node_path))
            assert cs_path.is_file(), (
                f"iteration {i}: {cs_path} was removed by the NetLogic delete: {out}")
            assert cs_path.read_bytes() == pre_delete, (
                f"iteration {i}: source bytes changed across the delete: {out}")
        _assert_bridge_still_serves(cfg, project)
    finally:
        if live_node:
            _best_effort_delete(cfg, project, live_node)
        for _node, cs in touched:
            try:
                cs.unlink()
            except OSError:
                pass


@pytest.mark.slow_live
@pytest.mark.parametrize("kind", ["runtime", "design_time"])
def test_netlogic_edit_delete_race_after_create_live(live, kind):
    """Race: .cs written right after create_netlogic, then the batch delete op;
    x3. Node gone each time and the source is byte-identical to pre-delete."""
    _netlogic_delete_race_after_create(live, "edit", kind)


@pytest.mark.slow_live
@pytest.mark.parametrize("kind", ["runtime", "design_time"])
def test_netlogic_delete_node_race_after_create_live(live, kind):
    """Race: .cs written right after create_netlogic, then
    optix_bridge_delete_node; x3. Node gone each time and the source is
    byte-identical to pre-delete."""
    _netlogic_delete_race_after_create(live, "delete_node", kind)


@pytest.mark.slow_live
@pytest.mark.parametrize("kind", ["runtime", "design_time"])
def test_netlogic_recreate_after_delete_does_not_revert_edit_live(live, kind):
    """#42 sequence: create a NetLogic, write source A, delete the node (the
    source is preserved), write source B on disk, re-create the SAME class
    within the 60 s late-guard window. B must survive the re-create and the
    next bridge call's late guard (no revert to A, no NUL padding, no
    ``netlogic_source_restored_late`` warning). Skipped without a live bridge
    (the ``live`` fixture)."""
    cfg, project = live
    cls = f"ZzzIssue42{kind.title().replace('_', '')}{_unique_suffix()}Logic"
    node_path = f"Model/{cls}"
    cs_path = (core.resolve_project(cfg, project) / "ProjectFiles" / "NetSolution"
               / f"{cls}.cs")

    def source(tag: str) -> bytes:
        return f"""using FTOptix.NetLogic;
using UAManagedCore;

public class {cls} : BaseNetLogic
{{
    // {tag}
    [ExportMethod]
    public void Issue42Method()
    {{
        Log.Info("{cls}", "{tag}");
    }}
}}
""".encode("utf-8")

    a, b = source("issue42-A"), source("issue42-B-longer-than-A-" + "x" * 40)
    _best_effort_delete(cfg, project, node_path)
    cs_path.parent.mkdir(parents=True, exist_ok=True)
    cs_path.unlink(missing_ok=True)
    created = False
    try:
        try:
            _with_bridge_recovery(
                cfg, project,
                lambda: core.bridge_create_netlogic(
                    cfg, project, "Model", cls, kind=kind))
        except core.BridgeWriteFailed as exc:
            pytest.fail(f"could not create a {kind} NetLogic: {exc}", pytrace=False)
        created = True
        cs_path.write_bytes(a)
        _with_bridge_recovery(
            cfg, project, lambda: core.bridge_delete_node(cfg, project, node_path))
        created = False
        assert cs_path.read_bytes() == a, "delete must preserve source A"
        # B is written to disk after the delete, then the class is re-created.
        cs_path.write_bytes(b)
        out = _with_bridge_recovery(
            cfg, project,
            lambda: core.bridge_create_netlogic(
                cfg, project, "Model", cls, kind=kind))
        created = True
        assert cs_path.read_bytes() == b, ("re-create reverted/altered B", out)
        # The next bridge call runs the late guard; it must not touch B.
        nxt = _with_bridge_recovery(
            cfg, project, lambda: core.describe_node(cfg, project, node_path))
        assert cs_path.read_bytes() == b, ("late guard reverted B", nxt)
        codes = [w.get("code") for w in (out.get("warnings") or [])
                 + ((nxt or {}).get("warnings") or []) if isinstance(w, dict)]
        assert "netlogic_source_restored_late" not in codes, codes
    finally:
        if created:
            _best_effort_delete(cfg, project, node_path)
        try:
            cs_path.unlink()
        except OSError:
            pass


def test_observe_immediately_after_emulator_restart_live(live_real_localappdata):
    """optix_observe(mode='screenshot') right after optix_emulator(action=
    'restart') - no settle, no warm-up capture in between - succeeds on each
    of 3 restart cycles. Needs
    FTX_LIVE_HMI=1 (emulator restartable, Chrome at OPTIX_CDP_URL)."""
    if os.environ.get("FTX_LIVE_HMI") != "1":
        pytest.skip("observe-after-restart test: set FTX_LIVE_HMI=1 with the "
                    "emulator restartable and Chrome at OPTIX_CDP_URL")
    _require_desktop()
    # cfg is built by the fixture after the real LOCALAPPDATA is restored; the
    # emulator may start out stopped (cold) - restart_emulator just starts it.
    cfg, project = live_real_localappdata
    for cycle in range(3):
        restarted = core.restart_emulator(cfg, project)
        assert restarted.get("serving"), (cycle, restarted)
        out = core.cdp_screenshot_runtime(cfg, project=project)
        assert out.get("state") == "succeeded", (cycle, out)
        assert out.get("cdp_recovered", True) is True, (cycle, out)
        # the call after the first one must work too (no detach in between)
        again = core.cdp_screenshot_runtime(cfg, project=project)
        assert again.get("state") == "succeeded", (cycle, again)


@pytest.mark.slow_live
def test_observe_fresh_after_emulator_restart_cycles_live(live_real_localappdata):
    """#23: the first observe(fresh) after a restart succeeds, every time.

    10 cycles of {restart, observe(fresh)} and 5 cycles of {restart,
    cdp_restart, observe(fresh)}; no warm-up call in between, and the FIRST
    observe of each cycle must succeed (no retry). Needs FTX_LIVE_HMI=1
    (emulator restartable, Chrome at OPTIX_CDP_URL). Skipped without a live
    bridge (module-level FTX_LIVE_BRIDGE gate / the ``live`` fixture)."""
    if os.environ.get("FTX_LIVE_HMI") != "1":
        pytest.skip("observe(fresh)-after-restart cycles: set FTX_LIVE_HMI=1 "
                    "with the emulator restartable and Chrome at OPTIX_CDP_URL")
    _require_desktop()
    # cfg is built by the fixture after the real LOCALAPPDATA is restored.
    cfg, project = live_real_localappdata

    def first_observe(label):
        out = core.cdp_screenshot_runtime(cfg, fresh=True, project=project)
        assert out.get("state") == "succeeded", (label, out)

    for cycle in range(10):
        restarted = core.restart_emulator(cfg, project)
        assert restarted.get("serving"), ("restart", cycle, restarted)
        first_observe(("restart+observe", cycle))
    for cycle in range(5):
        restarted = core.restart_emulator(cfg, project)
        assert restarted.get("serving"), ("restart+cdp_restart", cycle, restarted)
        healed = core.ensure_chrome_cdp(cfg)
        assert healed.get("state") != "failed", ("cdp_restart", cycle, healed)
        first_observe(("restart+cdp_restart+observe", cycle))


def test_observe_fresh_repeated_no_restart_live(live_real_localappdata):
    """#23 (ftx-mcp-e1i.4): 6 consecutive fresh=true screenshot observes on a
    static screen, with NO restart in between, all succeed. Guards against the
    stale-capture self-heal breaking a valid capture on repeated calls. Needs
    FTX_LIVE_HMI=1 (emulator running, Chrome at OPTIX_CDP_URL). Skipped without
    a live bridge (module-level FTX_LIVE_BRIDGE gate)."""
    if os.environ.get("FTX_LIVE_HMI") != "1":
        pytest.skip("repeated observe(fresh) test: set FTX_LIVE_HMI=1 with the "
                    "emulator running and Chrome at OPTIX_CDP_URL")
    _require_desktop()
    cfg, project = live_real_localappdata
    for call in range(6):
        out = core.cdp_screenshot_runtime(cfg, fresh=True, project=project)
        assert out.get("state") == "succeeded", (call, out)


def test_observe_fresh_after_each_of_5_restarts_live(live_real_localappdata):
    """#23 (ftx-mcp-e1i.4): 5 cycles of {emulator restart, fresh=true screenshot
    observe}; the FIRST observe after each restart must succeed (no retry, no
    warm-up). Needs FTX_LIVE_HMI=1 (emulator restartable, Chrome at
    OPTIX_CDP_URL). Skipped without a live bridge."""
    if os.environ.get("FTX_LIVE_HMI") != "1":
        pytest.skip("observe(fresh)-after-restart test: set FTX_LIVE_HMI=1 with "
                    "the emulator restartable and Chrome at OPTIX_CDP_URL")
    _require_desktop()
    cfg, project = live_real_localappdata
    for cycle in range(5):
        restarted = core.restart_emulator(cfg, project)
        assert restarted.get("serving"), ("restart", cycle, restarted)
        out = core.cdp_screenshot_runtime(cfg, fresh=True, project=project)
        assert out.get("state") == "succeeded", ("first observe", cycle, out)


# ---- typed driver tag via create_variable (ftx-mcp-vvk.1) ------------------
#
# SDK names are overridable because they could not be confirmed offline
# (pending bench verification): FTX_LIVE_RA_DRIVER_TYPE / _STATION_TYPE are the
# ObjectTypes create_object instantiates, FTX_LIVE_RA_TAG_TYPE the VariableType.
_RA_DRIVER_TYPE = os.environ.get("FTX_LIVE_RA_DRIVER_TYPE", "FTOptix.RAEtherNetIP.RAEtherNetIPDriver")
_RA_STATION_TYPE = os.environ.get("FTX_LIVE_RA_STATION_TYPE", "FTOptix.RAEtherNetIP.RAEtherNetIPStation")
_RA_TAG_TYPE = os.environ.get("FTX_LIVE_RA_TAG_TYPE", "RAEtherNetIPTag")


def _create_ra_driver_station(cfg, project, drivers, driver, station, verify_path) -> None:
    """Create an RA EtherNet/IP driver+station through the MCP and confirm it exists.

    A creation error with a live bridge FAILS the test with the error message
    (and the half-built driver is removed). The only skip is when the RA module
    is absent, i.e. the running Studio is below 1.8.
    """
    driver_path = drivers + "/" + driver
    try:
        core.bridge_create_object(cfg, project, drivers, driver, _RA_DRIVER_TYPE)
        core.bridge_create_object(cfg, project, driver_path, station, _RA_STATION_TYPE)
        core.describe_node(cfg, project, verify_path)
    except (core.BridgeWriteFailed, core.NodeNotFound) as exc:
        _best_effort_delete(cfg, project, driver_path)
        ver = _running_studio_version_or_none(cfg)
        if ver is not None and ver < (1, 8):
            pytest.skip(f"RA EtherNet/IP module absent: Studio {ver[0]}.{ver[1]} is below 1.8")
        pytest.fail(f"cannot create an RA EtherNet/IP driver+station through the MCP "
                    f"({_RA_DRIVER_TYPE} / {_RA_STATION_TYPE}): {exc}", pytrace=False)


def test_create_typed_driver_tag_with_symbolname_live(live):
    """create_variable variable_type=RAEtherNetIPTag + properties SymbolName makes
    a typed Boolean tag under an RA EtherNet/IP station's Tags; describe_node shows
    the type, datatype and SymbolName. FAILS with the error when the driver or
    station cannot be created; skips only on Studio below 1.8 (no RA module)."""
    cfg, project = live
    drivers = "CommDrivers"
    driver, station = _nm("ZzzVvk1Driver"), _nm("ZzzVvk1Station")
    driver_path = drivers + "/" + driver
    station_path = driver_path + "/" + station
    tags_path = station_path + "/Tags"
    tag_name = _nm("ZzzVvk1Tag")
    tag_path = tags_path + "/" + tag_name
    _create_ra_driver_station(cfg, project, drivers, driver, station, tags_path)
    try:
        out = core.bridge_create_variable(
            cfg, project, tag_name, tags_path, "Boolean",
            variable_type=_RA_TAG_TYPE, properties={"SymbolName": "ZzzVvk1Sym"})
        assert out.get("ok") is not False, out
        node = core.describe_node(cfg, project, tag_path)
        assert node.get("datatype") == "Boolean", node
        assert _RA_TAG_TYPE in (node.get("variable_type") or "") \
            or _RA_TAG_TYPE in (node.get("dotnet_type") or ""), node
        props = {p["name"]: p.get("value") for p in (node.get("properties") or [])}
        assert "ZzzVvk1Sym" in (props.get("SymbolName") or ""), node
    finally:
        try:
            core.bridge_delete_node(cfg, project, driver_path)
        except (core.BridgeWriteFailed, core.NodeNotFound):
            pass


def test_create_object_refuses_variable_type_live(live):
    """create_object type=<VariableType> is refused in the batch dry run and on the
    direct route with variable_type_not_object, and nothing is created. Skipped
    without a live bridge (the `live` fixture)."""
    cfg, project = live
    name = _nm("ZzzVvk2Obj")
    ops = [{"op": "create_object", "parent": "Model", "name": name,
            "object_type": _RA_TAG_TYPE}]
    dry = core.bridge_edit(cfg, project, ops, dry_run=True)
    codes = [e.get("code") for e in dry["report"]["errors"]]
    assert "variable_type_not_object" in codes, dry
    out = core.bridge_create_object(cfg, project, "Model", name, _RA_TAG_TYPE)
    assert out.get("ok") is False and out.get("code") == "variable_type_not_object", out
    assert f'create_variable with variable_type: "{_RA_TAG_TYPE}"' in (out.get("hint") or ""), out
    with pytest.raises(core.NodeNotFound):
        core.describe_node(cfg, project, "Model/" + name)


# ---- PRD 0041: arm syncs the shipped bridge (#47); create verifies (#48) ----
#
# Both tests below are SETUP-STRICT: the module gate (FTX_LIVE_BRIDGE=1) says a
# bridge is up, so a setup step that fails (project_new, arm, widget create, pen
# create) is a product failure and FAILS the test with the error. They never
# skip on a setup error. The only skips are the environment ones the rest of
# this module already has (no interactive desktop, Studio below 1.8).
#
# Names that could not be confirmed offline are env-overridable (pending bench
# verification on Studio 1.8): FTX_LIVE_BRIDGE_TEMPLATE, FTX_LIVE_TREND_TYPE,
# FTX_LIVE_PEN_DATATYPE, FTX_LIVE_PEN_COLOR.
_LIVE_BRIDGE_TEMPLATE = os.environ.get("FTX_LIVE_BRIDGE_TEMPLATE", "BridgeTemplate")
_LIVE_TREND_TYPE = os.environ.get("FTX_LIVE_TREND_TYPE", "Trend")
_LIVE_PEN_DATATYPE = os.environ.get("FTX_LIVE_PEN_DATATYPE", "Float")
_LIVE_PEN_COLOR = os.environ.get("FTX_LIVE_PEN_COLOR", "#ff0000")


def _require_studio_1_8_or_fail_setup(cfg) -> None:
    """Skip only when the running Studio is known to be below 1.8."""
    ver = _running_studio_version_or_none(cfg)
    if ver is not None and ver < (1, 8):
        pytest.skip(f"needs Studio 1.8 (running {ver[0]}.{ver[1]})")


def test_template_project_arm_syncs_then_reports_current_live(live_real_localappdata):
    """optix_project(new, template=BridgeTemplate) -> arm says ``synced`` (the
    template's bridge is older than the shipped one), the armed bridge has the
    current code (create_object with a VariableType such as RAEtherNetIPTag is
    refused with variable_type_not_object), and a second arm says ``current``.

    If the template already holds the shipped bridge (its drift was fixed) the
    first arm legitimately says ``current`` instead; the test then asserts that.
    FAILS (never skips) when project_new, arm or the bridge coming up fails.
    """
    _require_desktop()
    _, _live_project = live_real_localappdata
    cfg = core.Config.from_env()
    _require_studio_1_8_or_fail_setup(cfg)
    name = f"zzBridgeSync{int(time.time())}{_RUN_SFX}"
    created = core.project_new(cfg, name, template=_LIVE_BRIDGE_TEMPLATE)
    if not created.get("ok"):
        pytest.fail(f"project_new(template={_LIVE_BRIDGE_TEMPLATE!r}) failed: {created!r}",
                    pytrace=False)
    new_pid = created.get("pid")
    try:
        project_dir = pathlib.Path(created["path"])
        bridge_cs = project_dir.joinpath(*core._BRIDGE_CS_REL)
        assert bridge_cs.is_file(), f"template project has no {bridge_cs}"
        template_is_current = (
            core.bridge_source_hash(bridge_cs.read_bytes())
            == core.bridge_source_hash(core._shipped_bridge_cs().read_bytes()))
        expected_first = "current" if template_is_current else "synced"

        armed = core.bridge_arm(cfg, name)
        assert armed.get("ok") is not False and armed.get("state") != "failed", armed
        assert armed.get("bridge_source") == expected_first, armed
        if expected_first == "synced":
            assert armed.get("bridge_backup"), armed
        # The file on disk is now the shipped bridge.
        assert core.bridge_source_hash(bridge_cs.read_bytes()) \
            == core.bridge_source_hash(core._shipped_bridge_cs().read_bytes())

        core.reset_bridge_cache()
        deadline = time.time() + 60.0
        bcfg = None
        while bcfg is None and time.time() < deadline:
            bcfg = core._bridge_cfg_for(cfg, name)
            if bcfg is None:
                time.sleep(2.0)
                core.reset_bridge_cache()
        assert bcfg is not None, f"no bridge serving {name!r} within 60s of arm: {armed!r}"

        # The bridge runs the current code: a VariableType is refused as an object.
        sync_obj = _nm("ZzzSyncObj")
        out = core.bridge_create_object(bcfg, name, "Model", sync_obj, _RA_TAG_TYPE)
        assert out.get("ok") is False and out.get("code") == "variable_type_not_object", out
        with pytest.raises(core.NodeNotFound):
            core.describe_node(bcfg, name, "Model/" + sync_obj)

        again = core.bridge_arm(cfg, name)
        assert again.get("bridge_source") == "current", again
        assert "bridge_backup" not in again, again
    finally:
        try:
            core.bridge_arm(cfg, name, action="stop")
        except Exception:
            pass
        if new_pid:
            core._close_launched_studio(new_pid)
        _remove_scratch_project(cfg, name)


def test_trend_pen_create_variable_flow_live(live):
    """Create a Trend, add a pen with create_variable + variable_type=TrendPen
    under <Trend>/Pens, set the pen's Color child, and describe_node shows the
    pen and its colour. create_object with TrendPen is refused
    (variable_type_not_object) and creates nothing. FAILS with the error when the
    Trend or the pen cannot be created; skips only on Studio below 1.8."""
    cfg, project = live
    _require_studio_1_8_or_fail_setup(cfg)
    trend_name, pen_name = _nm("ZzzTrend48"), _nm("ZzzPen48")
    pen_obj = _nm("ZzzPenObj48")
    trend_path = _LIVE_SCRATCH_PANEL + "/" + trend_name
    pens_path = trend_path + "/Pens"
    pen_path = pens_path + "/" + pen_name
    _best_effort_delete(cfg, project, trend_path)
    try:
        try:
            out = core.bridge_create_widget(
                cfg, project, _LIVE_SCRATCH_PANEL, trend_name, _LIVE_TREND_TYPE)
            assert out.get("ok") is not False, out
            core.describe_node(cfg, project, pens_path)
        except (core.BridgeWriteFailed, core.NodeNotFound, AssertionError) as exc:
            pytest.fail(f"cannot create a {_LIVE_TREND_TYPE} with a Pens folder at "
                        f"{trend_path}: {exc}", pytrace=False)
        try:
            made = core.bridge_create_variable(
                cfg, project, pen_name, pens_path, _LIVE_PEN_DATATYPE,
                variable_type="TrendPen")
            assert made.get("ok") is not False, made
        except core.BridgeWriteFailed as exc:
            pytest.fail(f"create_variable TrendPen under {pens_path} failed: {exc}",
                        pytrace=False)
        pen = core.describe_node(cfg, project, pen_path)
        assert "TrendPen" in ((pen.get("variable_type") or "") + (pen.get("dotnet_type") or "")), pen
        color_path = pen_path + "/Color"
        # A fresh TrendPen has no Color child (set_property -> property_not_found);
        # create it first (r12 / w11 rehearsal, skills Trend pens section).
        core.bridge_create_variable(cfg, project, "Color", pen_path, "Color")
        core.bridge_set_property(cfg, project, color_path, "Value", _LIVE_PEN_COLOR)
        color = core.describe_node(cfg, project, color_path)
        assert _color_value_matches(color.get("value"), _LIVE_PEN_COLOR), color
        pens = core.describe_node(cfg, project, pens_path)
        kids = {c.get("browse_name") or c.get("name")
                for c in (pens.get("children") or []) + (pens.get("properties") or [])
                if isinstance(c, dict)}
        assert pen_name in kids, kids

        refused = core.bridge_create_object(
            cfg, project, pens_path, pen_obj, "TrendPen")
        assert refused.get("ok") is False \
            and refused.get("code") == "variable_type_not_object", refused
        assert "create_variable" in (refused.get("hint") or ""), refused
        with pytest.raises(core.NodeNotFound):
            core.describe_node(cfg, project, pens_path + "/" + pen_obj)
    finally:
        _best_effort_delete(cfg, project, trend_path)


def test_trend_pen_set_property_creates_color_live(live):
    """One-call route (ftx-mcp-wvk.2): create a TrendPen via create_variable, then
    set_property Color on the PEN (no create_variable for Color first). The bridge
    creates the type-declared optional child, reports created_optional_child, and
    describe_node then shows Color with the value. An undeclared name on the same
    pen still returns property_not_found. FAILS with the error when the Trend or
    the pen cannot be created; skips only on Studio below 1.8."""
    cfg, project = live
    _require_studio_1_8_or_fail_setup(cfg)
    trend_name, pen_name = _nm("ZzzTrend82"), _nm("ZzzPen82")
    trend_path = _LIVE_SCRATCH_PANEL + "/" + trend_name
    pens_path = trend_path + "/Pens"
    pen_path = pens_path + "/" + pen_name
    _best_effort_delete(cfg, project, trend_path)
    try:
        try:
            out = core.bridge_create_widget(
                cfg, project, _LIVE_SCRATCH_PANEL, trend_name, _LIVE_TREND_TYPE)
            assert out.get("ok") is not False, out
            core.describe_node(cfg, project, pens_path)
            made = core.bridge_create_variable(
                cfg, project, pen_name, pens_path, _LIVE_PEN_DATATYPE,
                variable_type="TrendPen")
            assert made.get("ok") is not False, made
        except (core.BridgeWriteFailed, core.NodeNotFound, AssertionError) as exc:
            pytest.fail(f"setup failed: cannot create a {_LIVE_TREND_TYPE} with a "
                        f"TrendPen at {pen_path}: {exc}", pytrace=False)

        res = core.bridge_set_property(cfg, project, pen_path, "Color", _LIVE_PEN_COLOR)
        assert res.get("ok") is True, res
        assert res.get("created_optional_child") == "Color", res

        pen = core.describe_node(cfg, project, pen_path)
        names = {c.get("browse_name") or c.get("name")
                 for c in (pen.get("children") or []) + (pen.get("properties") or [])
                 if isinstance(c, dict)}
        assert "Color" in names, pen
        color = core.describe_node(cfg, project, pen_path + "/Color")
        assert _color_value_matches(color.get("value"), _LIVE_PEN_COLOR), color

        with pytest.raises(core.BridgeWriteFailed) as ei:
            core.bridge_set_property(cfg, project, pen_path, "ZzzNotDeclared82", "1")
        assert "property_not_found" in str(ei.value) \
            or "unknown_property" in str(ei.value), str(ei.value)
    finally:
        _best_effort_delete(cfg, project, trend_path)


def test_create_object_folder_live(live):
    """create_object(object_type='Folder') under Model makes a standard FolderType
    node, describe_node shows it (as a child of Model and by path), and it is
    deleted again. FAILS with the error when the bridge is up and the create or the
    describe fails; ``type_not_found`` counts as a failure here, so the working
    route (create_folder) is exercised by the other live tests, not by this one.
    (ftx-mcp-wvk.3)"""
    cfg, project = live
    parent = _LIVE_MODEL_PARENT
    name = _nm("ZzzFolder13")
    path = parent + "/" + name
    _best_effort_delete(cfg, project, path)
    try:
        try:
            out = core.bridge_create_object(cfg, project, parent, name, "Folder")
        except core.BridgeWriteFailed as exc:
            pytest.fail(f"create_object(Folder) under {parent} failed: {exc}",
                        pytrace=False)
        assert out.get("ok") is not False, out
        try:
            node = core.describe_node(cfg, project, path)
            siblings = core.describe_node(cfg, project, parent).get("children", [])
        except core.NodeNotFound as exc:
            pytest.fail(f"describe_node cannot see the created Folder at {path}: {exc}",
                        pytrace=False)
        assert "FolderType" in json.dumps(node) or "Folder" in json.dumps(node), node
        kids = {c.get("browse_name") or c.get("name") for c in siblings
                if isinstance(c, dict)}
        assert name in kids, kids
        # FolderType spelling resolves to the same type
        out2 = core.bridge_create_object(cfg, project, path, "Inner", "FolderType")
        assert out2.get("ok") is not False, out2
        core.describe_node(cfg, project, path + "/Inner")
    finally:
        _best_effort_delete(cfg, project, path)
    with pytest.raises(core.NodeNotFound):
        core.describe_node(cfg, project, path)
