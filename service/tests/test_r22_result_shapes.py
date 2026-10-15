"""PRD 0053 D (#87): result-shape fixes from r22.

1. bridge_not_armed (not bridge_transient) for an unarmed same-name project
2. web_port_taken everywhere (run / status / restart) with runtime
   none | zombie | running; web_port_clash retired (legacy_code alias)
3. batch create_netlogic: netsolution_build note, no unknown_op, bad parent
   refused at dry_run with unresolved_reference
4. examples/claude-code-settings.json allow-list: optix_status, not optix_doctor
(5, "project not found" lists only loaded projects, lives in
test_resolve_project_fallback.py.)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from service import core
from service.tests.conftest import make_project
from service.tests.test_run_emulator import (  # noqa: F401 - fixtures
    _counting_runner, _emulator_target_by_default, _f5_runner, _hermetic_web_port,
    _no_bridge_by_default, _no_host_runtime, _proj, _stub_run_env,
)

_RUN_ENV = pytest.mark.usefixtures(
    "_hermetic_web_port", "_no_bridge_by_default", "_no_host_runtime",
    "_emulator_target_by_default")


@pytest.fixture
def alpha(cfg, projects_root):
    make_project(projects_root, "Alpha")
    return cfg


# ---- 1. bridge_not_armed ------------------------------------------------

def test_edit_on_unarmed_same_name_project_is_bridge_not_armed(alpha, monkeypatch):
    # A bridge answers to the name "Alpha" (the sibling copy) and is healthy,
    # but resolve_bridge finds none that serves THIS project's directory.
    monkeypatch.setattr(core, "_bridge_health_at", lambda cfg, port, force=False: {
        "responded": port == alpha.bridge_port_base, "port": port, "project": "Alpha",
        "available": True})
    monkeypatch.setattr(
        core, "resolve_bridge",
        lambda cfg, project: (_ for _ in ()).throw(
            core.NoBridgeForProject("no_bridge_for_project: sibling only", project)))
    out = core.classify_bridge_failure(alpha, "Alpha", core.BridgeUnavailable("x"))
    assert out["reason_code"] == "bridge_not_armed"
    assert out["hint"] == "optix_bridge_arm Alpha"
    assert "retry" not in out["nudge"].lower()
    assert out["state"] == "failed"


def test_healthy_resolved_bridge_stays_bridge_transient(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_health_at", lambda cfg, port, force=False: {
        "responded": port == alpha.bridge_port_base, "port": port, "project": "Alpha",
        "available": True})
    monkeypatch.setattr(core, "resolve_bridge",
                        lambda cfg, project: {"port": alpha.bridge_port_base})
    out = core.classify_bridge_failure(alpha, "Alpha", core.BridgeUnavailable("race"))
    assert out["reason_code"] == "bridge_transient"


# ---- 2. web_port_taken ---------------------------------------------------

class _Proc:
    def __init__(self, pid): pass
    def name(self): return "nginx.exe"


@_RUN_ENV
@pytest.mark.parametrize("status,ours,runtime", [
    ({"state": "stopped", "pids": []}, [], "none"),
    ({"state": "zombie", "pids": [777]}, [777], "zombie"),
    ({"state": "running", "pids": [777]}, [777], "running"),
])
def test_run_foreign_listener_is_web_port_taken_with_runtime(
        cfg, projects_root, monkeypatch, status, ours, runtime):
    _proj(projects_root)
    _stub_run_env(monkeypatch, listeners=[4242], ours=ours)
    monkeypatch.setattr(core.psutil, "Process", _Proc)
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, runner=None, project=None: dict(status))
    calls: list = []
    out = core.run_emulator(cfg, "Alpha", runner=_counting_runner(calls))
    assert out["error"] == "web_port_taken"
    assert out["runtime"] == runtime
    assert calls == []


@_RUN_ENV
def test_restart_foreign_listener_is_web_port_taken(cfg, projects_root, monkeypatch):
    _proj(projects_root)
    _stub_run_env(monkeypatch, listeners=[4242], ours=[])
    monkeypatch.setattr(core.psutil, "Process", _Proc)
    out = core.restart_emulator(cfg, "Alpha", runner=_f5_runner())
    assert out["error"] == "web_port_taken"
    assert out["runtime"] == "none"
    assert out["owner"] == {"pid": 4242, "process": "nginx.exe"}


def _status_env(monkeypatch, *, pids, owner, windowed):
    monkeypatch.setattr(core, "_emulator_entries", lambda project=None: [
        {"pid": p, "project_resolved": "Alpha", "matched_path": "x"} for p in pids])
    monkeypatch.setattr(core, "_project_web_port", lambda cfg, project: 8081)
    monkeypatch.setattr(core, "_tcp_probe", lambda host, port, *a, **k: True)
    monkeypatch.setattr(core, "_port_owner", lambda *a, **k: {
        "owner": owner, "pid": 4242 if owner == "foreign" else None, "name": "nginx.exe"})
    monkeypatch.setattr(core, "_any_pid_has_window", lambda pids: windowed)
    monkeypatch.setattr(core, "_tail_last_lifecycle_line", lambda project: None)


@pytest.mark.parametrize("pids,windowed,runtime", [
    ([], False, "none"),
    ([777], True, "zombie"),
])
def test_status_foreign_listener_is_web_port_taken(
        cfg, projects_root, monkeypatch, pids, windowed, runtime):
    _proj(projects_root)
    _status_env(monkeypatch, pids=pids, owner="foreign", windowed=windowed)
    out = core.emulator_status(cfg, project="Alpha")
    assert out["error"] == "web_port_taken"
    assert out["runtime"] == runtime
    assert out["owner"] == {"pid": 4242, "process": "nginx.exe"}


def test_status_runtime_owned_port_has_no_web_port_taken(cfg, projects_root, monkeypatch):
    _proj(projects_root)
    _status_env(monkeypatch, pids=[777], owner="runtime", windowed=True)
    out = core.emulator_status(cfg, project="Alpha")
    assert out["state"] == "running"
    assert "error" not in out and "runtime" not in out


def test_runtime_kind_values():
    assert core._runtime_kind([], "stopped") == "none"
    assert core._runtime_kind([1], "zombie") == "zombie"
    assert core._runtime_kind([1], "starting") == "zombie"
    assert core._runtime_kind([1], "running") == "running"


def test_web_port_clash_is_retired_without_legacy_alias(monkeypatch):
    monkeypatch.setattr(core, "discover_web_port",
                        lambda c, p: {"port": 8081, "source": "project", "detail": ""})
    monkeypatch.setattr(core, "_running_emulator_projects", lambda: {"b"})
    clash = core.detect_web_port_clash(None, "A", others=["B"])
    assert clash["error"] == "web_port_taken"
    assert "legacy_code" not in clash
    src = Path(core.__file__).read_text(encoding="utf-8")
    assert '"error": "web_port_clash"' not in src
    assert '"reason_code": "web_port_clash"' not in src


# ---- 3. create_netlogic in a batch ---------------------------------------

@pytest.fixture
def batch_seams(alpha, monkeypatch):
    # The bridge validator reports unknown_op for create_netlogic (older build).
    def validate(cfg_, project, ops, strict=False):
        warns = [{"op_index": i, "code": "unknown_op",
                  "message": "op 'create_netlogic' is not validated by this bridge; "
                             "it will be applied unchecked"}
                 for i, o in enumerate(ops) if o["op"] == "create_netlogic"]
        if strict and warns:
            return {"ok": False, "errors": warns, "warnings": [],
                    "op_count": len(ops), "strict": True}
        return {"ok": True, "errors": [], "warnings": warns,
                "op_count": len(ops), "strict": strict}

    monkeypatch.setattr(core, "bridge_validate_ops", validate)
    monkeypatch.setattr(core, "validate_edit_op_types", lambda *a, **k: [])
    monkeypatch.setattr(core, "bridge_create_netlogic",
                        lambda cfg_, project, parent, name, kind=None, dry_run=False: {
                            "ok": True, "created_path": f"{parent}/{name}",
                            "type": "NetLogic", "kind": kind or "runtime",
                            "netsolution_build": {"state": "building",
                                                  "note": core._NETSOLUTION_BUILD_NOTE}}
                        if kind == "design_time" else
                        {"ok": True, "created_path": f"{parent}/{name}", "type": "NetLogic"})

    def get_json(cfg_, path, *a, **k):
        # live model: only Model exists
        if path.startswith("/bridge/nodes?path=Model") and "Missing" not in path:
            return 200, {"node_class": "Object"}
        return 404, {"error": {"code": "node_not_found"}}

    monkeypatch.setattr(core, "_bridge_get_json", get_json)
    return alpha


def test_batch_create_netlogic_carries_netsolution_build_note(batch_seams):
    out = core.bridge_edit(batch_seams, "Alpha", [
        {"op": "create_netlogic", "parent": "Model", "name": "L", "kind": "design_time"}])
    assert out["state"] == "succeeded", out
    timing = out["op_timings"][0]
    assert timing["netsolution_build"]["state"] == "building"
    assert timing["netsolution_build"]["note"] == core._NETSOLUTION_BUILD_NOTE
    assert out["netsolution_build"]["state"] == "building"


@pytest.mark.parametrize("strict", [False, True])
def test_batch_create_netlogic_raises_no_unknown_op(batch_seams, strict):
    out = core.bridge_edit(batch_seams, "Alpha", [
        {"op": "create_netlogic", "parent": "Model", "name": "L"}],
        dry_run=True, strict=strict)
    report = out["report"]
    codes = {e.get("code") for e in report["errors"] + report["warnings"]}
    assert "unknown_op" not in codes
    assert report["ok"] is True


def test_batch_create_netlogic_bad_parent_refused_at_dry_run(batch_seams):
    out = core.bridge_edit(batch_seams, "Alpha", [
        {"op": "create_netlogic", "parent": "Model/Missing", "name": "L"}], dry_run=True)
    assert out["state"] == "validated" and out["applied"] == 0
    errs = [e for e in out["report"]["errors"] if e["code"] == "unresolved_reference"]
    assert errs and errs[0]["op_index"] == 0 and errs[0]["field"] == "parent"
    assert out["report"]["ok"] is False


def test_batch_create_netlogic_parent_created_earlier_in_batch_resolves(batch_seams):
    out = core.bridge_edit(batch_seams, "Alpha", [
        {"op": "create_folder", "parent": "Model", "name": "Fresh"},
        {"op": "create_netlogic", "parent": "Model/Fresh", "name": "L"}], dry_run=True)
    assert out["report"]["ok"] is True, out["report"]


def test_bridge_validator_source_knows_create_netlogic():
    cs = (Path(core.__file__).parent.parent / "studio-bridge" / "StudioMCPBridge.cs"
          ).read_text(encoding="utf-8")
    assert 'case "create_netlogic":' in cs


# ---- 4. allow-list -------------------------------------------------------

def test_example_allow_list_has_optix_status_not_optix_doctor():
    p = Path(core.__file__).parent.parent / "examples" / "claude-code-settings.json"
    allow = json.loads(p.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "mcp__ftx-mcp__optix_status" in allow
    assert "mcp__ftx-mcp__optix_doctor" not in allow
