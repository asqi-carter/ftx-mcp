"""PRD 0055 E (#95): r24 result-shape nits.

1. bridge_port_range_full hint names OPTIX_BRIDGE_PORT_RANGE and the fixed 4
2. project_file_invalid after launch: studio_left_open {pid} + close hint
3. web_port_taken: owner {pid, process} | null + reason; no legacy_code
4. restart with nothing running: restarted false, state = the run's real state
5. status hint for an http.sys squatter names http.sys / netsh http show urlacl
"""
from __future__ import annotations

from pathlib import Path

import pytest

from service import core, studio_arm
from service.tests.conftest import make_project


def test_port_range_full_hint_names_real_env_var_and_fixed_range():
    hint = studio_arm.PORT_RANGE_FULL_HINT
    assert "OPTIX_BRIDGE_PORT_RANGE" in hint
    assert "FTX_BRIDGE_PORT_RANGE" not in hint
    assert "fixed at 4" in hint and "PortRangeSize" in hint
    health = {p: {"project": f"Other{p}", "studio_pid": p} for p in range(8768, 8772)}
    out = studio_arm.port_range_full_result(health, "Mine")
    assert out["hint"] == hint
    # The bridge source really is fixed at 4, as the hint says.
    cs = (Path(core.__file__).resolve().parents[1] / "studio-bridge"
          / "StudioMCPBridge.cs").read_text(encoding="utf-8")
    assert "PortRangeSize = 4;" in cs


def test_runbook_documents_fixed_bridge_range():
    text = (Path(core.__file__).resolve().parents[1] / "docs" / "runbook.md"
            ).read_text(encoding="utf-8")
    assert "fixed at 4" in text and "regenerated bridge" in text


def test_owner_fields_pid_and_process_or_null_with_reason():
    assert core._web_port_owner_fields(
        {"owner": "foreign", "pid": 4242, "name": "nginx.exe"}
    ) == {"owner": {"pid": 4242, "process": "nginx.exe"}}
    unreadable = core._web_port_owner_fields({"owner": "unknown", "pid": None})
    assert unreadable["owner"] is None and unreadable["owner_reason"]
    nopid = core._web_port_owner_fields({"owner": "foreign", "pid": None})
    assert nopid["owner"] is None and nopid["owner_reason"]


def _clash_env(monkeypatch, listeners):
    monkeypatch.setattr(core, "discover_web_port",
                        lambda c, p: {"port": 8081, "source": "project", "detail": ""})
    monkeypatch.setattr(core, "_port_listener_pids", lambda port: listeners)

    class _P:
        def __init__(self, pid): pass
        def name(self): return "FTOptixRuntime.exe"

    monkeypatch.setattr(core.psutil, "Process", _P)


def test_web_port_taken_clash_carries_owner_and_no_legacy_code(monkeypatch):
    _clash_env(monkeypatch, [321])
    monkeypatch.setattr(core, "_running_emulator_projects", lambda: {"b"})
    clash = core.detect_web_port_clash(None, "A", others=["B"])
    assert clash["error"] == "web_port_taken"
    assert "legacy_code" not in clash
    assert clash["owner"] == {"pid": 321, "process": "FTOptixRuntime.exe"}


def test_web_port_taken_listener_clash_owner_null_when_table_unreadable(monkeypatch):
    _clash_env(monkeypatch, None)
    monkeypatch.setattr(core, "_running_emulator_projects", lambda: set())
    monkeypatch.setattr(core, "_port_has_listener", lambda cfg, port: True)
    clash = core.detect_web_port_clash(None, "A", others=["B"])
    assert clash["error"] == "web_port_taken"
    assert clash["owner"] is None and clash["owner_reason"]


def test_nothing_in_service_reads_legacy_code():
    root = Path(core.__file__).resolve().parent
    for py in root.glob("*.py"):
        assert "legacy_code" not in py.read_text(encoding="utf-8"), py.name


def _status_env(monkeypatch, owner):
    monkeypatch.setattr(core, "_emulator_entries", lambda project=None: [])
    monkeypatch.setattr(core, "_project_web_port", lambda cfg, project: 8081)
    monkeypatch.setattr(core, "_tcp_probe", lambda host, port, *a, **k: True)
    monkeypatch.setattr(core, "_port_owner", lambda *a, **k: owner)
    monkeypatch.setattr(core, "_tail_last_lifecycle_line", lambda project: None)


def test_status_httpsys_squatter_hint_names_httpsys_and_urlacl(cfg, projects_root, monkeypatch):
    make_project(projects_root, "Alpha")
    _status_env(monkeypatch, {"owner": "foreign", "pid": 4, "name": "System"})
    out = core.emulator_status(cfg, project="Alpha")
    assert out["error"] == "web_port_taken"
    assert out["owner"] == {"pid": 4, "process": "System"}
    assert "http.sys" in out["hint"] and "netsh http show urlacl" in out["hint"]
    assert "UpdateSvc" not in out["hint"]


def test_status_foreign_listener_without_pid_has_null_owner_and_reason(cfg, projects_root, monkeypatch):
    make_project(projects_root, "Alpha")
    _status_env(monkeypatch, {"owner": "foreign", "pid": None, "name": None})
    out = core.emulator_status(cfg, project="Alpha")
    assert out["error"] == "web_port_taken"
    assert out["owner"] is None and out["owner_reason"]


def test_restart_with_nothing_running_reports_the_runs_refusal_not_stopped(cfg, monkeypatch):
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, r=None, project=None: {"pids": [], "state": "stopped"})
    monkeypatch.setattr(core, "run_emulator",
                        lambda *a, **k: {"ok": False, "launched": False, "serving": False,
                                         "error": "web_port_taken"})
    out = core.restart_emulator(cfg, "P")
    assert out["restarted"] is False and out["started"] is False
    assert out["state"] == "web_port_taken"


def test_restart_is_not_restarted_when_run_fails_after_the_kill(cfg, monkeypatch):
    monkeypatch.setattr(core, "_wait_runtime_port", lambda *a, **k: True)
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, r=None, project=None: {"pids": [111], "state": "running"})
    monkeypatch.setattr(core, "stop_emulator",
                        lambda c, r=None, status=None, project=None:
                        {"stopped": True, "killed_pids": [111]})
    monkeypatch.setattr(core, "run_emulator",
                        lambda *a, **k: {"ok": False, "launched": False, "serving": False,
                                         "error": "web_port_taken"})
    out = core.restart_emulator(cfg, "P")
    assert out["restarted"] is False and out["stopped_pids"] == [111]
