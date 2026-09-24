"""Tests for core.doctor — the layman dependency checklist."""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

from service import core

# Preserve real implementations before any monkeypatching runs (used by
# the _scan_bind_fail unit tests to bypass the autouse isolation stub).
_REAL_SCAN_BIND_FAIL = core._scan_bind_fail


@pytest.fixture(autouse=True)
def _no_bridge(monkeypatch):
    # deterministic: bridge unreachable (no live Studio in CI)
    core.reset_bridge_cache()

    def _down(cfg, path, method="GET", timeout=5.0, **_kwargs):
        raise core.BridgeUnavailable("no bridge in test")

    monkeypatch.setattr(core, "_bridge_http", _down)
    yield
    core.reset_bridge_cache()


@pytest.fixture(autouse=True)
def _no_psutil_ports(monkeypatch):
    """Isolate tests from the host's real TCP connections."""
    monkeypatch.setattr(core, "_port_listener", lambda port: None)
    yield


@pytest.fixture(autouse=True)
def _no_bind_fail_scan(monkeypatch):
    """Isolate tests from any real runtime log on the host."""
    monkeypatch.setattr(core, "_scan_bind_fail", lambda project: None)
    yield


def test_ready_when_required_present(cfg):
    # cfg fixture gives a real studio_exe file + projects_root dir
    out = core.doctor(cfg)
    assert out["ready"] is True
    names = {c["name"] for c in out["checks"]}
    assert {"studio_exe", "projects_root", "bridge", "cdp", "deploy_username",
            "deploy_password", "deploy_thumbprint", "interactive_session"} <= names
    # every check carries a plain-english fix
    assert all(c["fix"] for c in out["checks"])


def test_not_ready_when_studio_missing(cfg):
    c = dataclasses.replace(cfg, studio_exe=cfg.studio_exe.parent / "nope.exe")
    out = core.doctor(c)
    assert out["ready"] is False
    studio = next(x for x in out["checks"] if x["name"] == "studio_exe")
    assert studio["ok"] is False and studio["required"] is True


def test_deploy_checks_reflect_config(cfg, monkeypatch):
    monkeypatch.delenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", raising=False)
    c = dataclasses.replace(cfg, deploy_username="admin", deploy_thumbprint="ABC")
    out = core.doctor(c)
    by = {x["name"]: x for x in out["checks"]}
    assert by["deploy_username"]["ok"] is True
    assert by["deploy_thumbprint"]["ok"] is True
    assert by["deploy_password"]["ok"] is False
    # deploy checks aren't required -> ready still True
    assert out["ready"] is True


def test_deploy_checks_do_not_disclose_credential_values(cfg, monkeypatch):
    """U1: doctor sits at the `read` scope, which is the whole 27-tool
    introspection tier — so any agent that can list a project can read these
    rows. `ok` answers "is it configured"; the literal username and thumbprint
    are not disclosed. deploy_password was always handled this way; username
    and thumbprint now match it.
    """
    monkeypatch.setenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", "hunter2")
    c = dataclasses.replace(
        cfg, deploy_username="svc_deploy", deploy_thumbprint="A1B2C3D4E5F6")
    out = core.doctor(c)
    blob = json.dumps(out)

    assert "svc_deploy" not in blob
    assert "A1B2C3D4E5F6" not in blob
    assert "hunter2" not in blob

    by = {x["name"]: x for x in out["checks"]}
    assert by["deploy_username"]["detail"] == "set"
    assert by["deploy_password"]["detail"] == "set"
    # last-4 tail survives so two certs stay tellable apart
    assert by["deploy_thumbprint"]["detail"] == "set (...E5F6)"


# ---------------------------------------------------------------------------
# web_port three-way diagnosis (I28 rule 3)
# ---------------------------------------------------------------------------

def _make_project_with_engine(projects_root: Path, name: str = "MyApp",
                               port: int = 8088) -> Path:
    """Create a project directory with a UI.yaml containing a WebPresentationEngine."""
    proj = projects_root / name
    proj.mkdir()
    (proj / f"{name}.optix").write_text("fake-optix-marker")
    ui_dir = proj / "Nodes" / "UI"
    ui_dir.mkdir(parents=True)
    (ui_dir / "UI.yaml").write_text(
        f"- Name: WebPresentationEngine\n"
        f"  Type: FTOptix.WebPresentationEngine.WebPresentationEngine\n"
        f"  Port: {port}\n",
        encoding="utf-8",
    )
    return proj


def test_web_port_row_present_when_project_given(cfg, projects_root, monkeypatch):
    """doctor(project=…) always includes a 'web_port' row."""
    _make_project_with_engine(projects_root, "MyApp", port=8090)
    monkeypatch.delenv("OPTIX_RUNTIME_TEST_PORT", raising=False)
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    names = {c["name"] for c in out["checks"]}
    assert "web_port" in names


def test_web_port_row_not_present_without_project(cfg):
    """doctor() without a project MUST NOT include 'web_port'."""
    out = core.doctor(cfg)
    assert all(c["name"] != "web_port" for c in out["checks"])


def test_web_port_three_fields_present(cfg, projects_root, monkeypatch):
    """The web_port row carries project_says, service_expects, listening."""
    _make_project_with_engine(projects_root, "MyApp", port=8088)
    monkeypatch.delenv("OPTIX_RUNTIME_TEST_PORT", raising=False)
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    by = {c["name"]: c for c in out["checks"]}
    wp = by["web_port"]
    assert "project_says" in wp
    assert "service_expects" in wp
    assert "listening" in wp


def test_web_port_project_says_from_yaml(cfg, projects_root, monkeypatch):
    """project_says reflects the port in Nodes/UI/UI.yaml."""
    _make_project_with_engine(projects_root, "MyApp", port=8088)
    monkeypatch.delenv("OPTIX_RUNTIME_TEST_PORT", raising=False)
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    by = {c["name"]: c for c in out["checks"]}
    assert by["web_port"]["project_says"] == 8088


def test_web_port_service_expects_uses_env_override(cfg, projects_root, monkeypatch):
    """service_expects reflects OPTIX_RUNTIME_TEST_PORT when the project has
    no Nodes/UI/UI.yaml (no configured WebPresentationEngine port)."""
    # Project with NO UI.yaml — env override takes effect as the fallback
    proj = projects_root / "MyApp"
    proj.mkdir()
    (proj / "MyApp.optix").write_text("fake-optix-marker")
    monkeypatch.setenv("OPTIX_RUNTIME_TEST_PORT", "9999")
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    by = {c["name"]: c for c in out["checks"]}
    assert by["web_port"]["service_expects"] == 9999


def test_web_port_ok_when_port_free(cfg, projects_root, monkeypatch):
    """web_port.ok is True when nothing holds the expected port."""
    _make_project_with_engine(projects_root, "MyApp", port=8088)
    monkeypatch.delenv("OPTIX_RUNTIME_TEST_PORT", raising=False)
    monkeypatch.setattr(core, "_port_listener", lambda port: None)
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    by = {c["name"]: c for c in out["checks"]}
    assert by["web_port"]["ok"] is True
    assert by["web_port"]["listening"] is None


def test_web_port_ok_when_optix_runtime_holds_port(cfg, projects_root, monkeypatch):
    """web_port.ok is True when FTOptixRuntime.exe holds the port (expected)."""
    _make_project_with_engine(projects_root, "MyApp", port=8088)
    monkeypatch.delenv("OPTIX_RUNTIME_TEST_PORT", raising=False)
    monkeypatch.setattr(core, "_port_listener",
                        lambda port: {"pid": 1234, "name": "FTOptixRuntime.exe"})
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    by = {c["name"]: c for c in out["checks"]}
    assert by["web_port"]["ok"] is True


def test_web_port_not_ok_when_squatter_holds_port(cfg, projects_root, monkeypatch):
    """web_port.ok is False when an unrelated process holds the expected port."""
    _make_project_with_engine(projects_root, "MyApp", port=8088)
    monkeypatch.delenv("OPTIX_RUNTIME_TEST_PORT", raising=False)
    monkeypatch.setattr(core, "_port_listener",
                        lambda port: {"pid": 9001, "name": "Ignition.exe"})
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    by = {c["name"]: c for c in out["checks"]}
    assert by["web_port"]["ok"] is False
    assert by["web_port"]["listening"] == {"pid": 9001, "name": "Ignition.exe"}


def test_web_port_squatter_fix_names_process(cfg, projects_root, monkeypatch):
    """The fix string names the squatter process when one is detected."""
    _make_project_with_engine(projects_root, "MyApp", port=8088)
    monkeypatch.delenv("OPTIX_RUNTIME_TEST_PORT", raising=False)
    monkeypatch.setattr(core, "_port_listener",
                        lambda port: {"pid": 9001, "name": "Ignition.exe"})
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    by = {c["name"]: c for c in out["checks"]}
    fix = by["web_port"]["fix"]
    assert "Ignition.exe" in fix
    assert "9001" in fix


def test_web_port_fix_offers_to_unset_env_var_when_conflicting(
        cfg, projects_root, monkeypatch):
    """When OPTIX_RUNTIME_TEST_PORT overrides the project port, the fix OFFERS
    to unset it (doctor never acts autonomously — I28 rule 3)."""
    _make_project_with_engine(projects_root, "MyApp", port=8088)
    monkeypatch.setenv("OPTIX_RUNTIME_TEST_PORT", "9999")
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    by = {c["name"]: c for c in out["checks"]}
    fix = by["web_port"]["fix"]
    # Fix must MENTION the env var (offer) but not say it was already unset
    assert "OPTIX_RUNTIME_TEST_PORT" in fix
    assert "unset" in fix.lower() or "remove" in fix.lower()


def test_web_port_is_not_required(cfg, projects_root, monkeypatch):
    """web_port is informational: required=False so a missing engine
    doesn't cause doctor to report ready=False."""
    _make_project_with_engine(projects_root, "MyApp", port=8088)
    monkeypatch.delenv("OPTIX_RUNTIME_TEST_PORT", raising=False)
    monkeypatch.setattr(core, "_port_listener",
                        lambda port: {"pid": 9001, "name": "Squatter.exe"})
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    wp = next(c for c in out["checks"] if c["name"] == "web_port")
    assert wp["required"] is False
    # ready is driven only by required checks
    assert out["ready"] is True


# ---------------------------------------------------------------------------
# web_engine_bind_failed row
# ---------------------------------------------------------------------------

def test_web_engine_bind_failed_row_absent_when_no_log(cfg, projects_root, monkeypatch):
    """No web_engine_bind_failed row when _scan_bind_fail returns None."""
    _make_project_with_engine(projects_root, "MyApp", port=8088)
    monkeypatch.delenv("OPTIX_RUNTIME_TEST_PORT", raising=False)
    monkeypatch.setattr(core, "_scan_bind_fail", lambda project: None)
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    assert all(c["name"] != "web_engine_bind_failed" for c in out["checks"])


def test_web_engine_bind_failed_row_present_when_log_has_fail(
        cfg, projects_root, monkeypatch):
    """web_engine_bind_failed row appears when a bind-fail is detected."""
    _make_project_with_engine(projects_root, "MyApp", port=8088)
    monkeypatch.delenv("OPTIX_RUNTIME_TEST_PORT", raising=False)
    monkeypatch.setattr(core, "_scan_bind_fail",
                        lambda project: {"port": 8088, "holder": "Address already in use"})
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    by = {c["name"]: c for c in out["checks"]}
    assert "web_engine_bind_failed" in by
    row = by["web_engine_bind_failed"]
    assert row["ok"] is False
    assert row["port"] == 8088
    assert "already in use" in row["holder"]


def test_web_engine_bind_failed_fix_mentions_env_var_when_set(
        cfg, projects_root, monkeypatch):
    """When OPTIX_RUNTIME_TEST_PORT is set, the bind-fail fix OFFERS to unset it."""
    _make_project_with_engine(projects_root, "MyApp", port=8088)
    monkeypatch.setenv("OPTIX_RUNTIME_TEST_PORT", "8088")
    monkeypatch.setattr(core, "_scan_bind_fail",
                        lambda project: {"port": 8088, "holder": "EADDRINUSE"})
    core.reset_web_port_cache()

    out = core.doctor(cfg, project="MyApp")
    by = {c["name"]: c for c in out["checks"]}
    fix = by["web_engine_bind_failed"]["fix"]
    assert "OPTIX_RUNTIME_TEST_PORT" in fix
    assert "unset" in fix.lower() or "remove" in fix.lower()


# ---------------------------------------------------------------------------
# _scan_bind_fail unit tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def log_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "emulog"
    root.mkdir()
    monkeypatch.setenv("OPTIX_EMULATOR_LOG_ROOT", str(root))
    return root


def _write_runtime_log(root: Path, project: str, lines: list[str]) -> Path:
    d = root / project
    d.mkdir(exist_ok=True)
    p = d / "FTOptixRuntime.0.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_scan_bind_fail_returns_none_when_no_log(log_root):
    result = _REAL_SCAN_BIND_FAIL("Ghost")
    assert result is None


def test_scan_bind_fail_returns_none_when_no_fail_in_log(log_root):
    _write_runtime_log(log_root, "Alpha", ["INFO startup ok", "INFO binding 8088"])
    result = _REAL_SCAN_BIND_FAIL("Alpha")
    assert result is None


def test_scan_bind_fail_detects_eaddrinuse(log_root):
    _write_runtime_log(log_root, "Alpha", [
        "INFO startup",
        "ERROR EADDRINUSE :8088 - port already bound",
    ])
    result = _REAL_SCAN_BIND_FAIL("Alpha")
    assert result is not None
    assert result["port"] == 8088
    assert "EADDRINUSE" in result["holder"]


def test_scan_bind_fail_detects_address_already_in_use(log_root):
    _write_runtime_log(log_root, "Alpha", [
        "INFO startup",
        "Failed to bind to :8081 — Address already in use",
    ])
    result = _REAL_SCAN_BIND_FAIL("Alpha")
    assert result is not None
    assert result["port"] == 8081


def test_scan_bind_fail_returns_most_recent_occurrence(log_root):
    _write_runtime_log(log_root, "Alpha", [
        "ERROR EADDRINUSE :8080",
        "INFO restart",
        "ERROR EADDRINUSE :8082",
    ])
    result = _REAL_SCAN_BIND_FAIL("Alpha")
    # scan is reversed — should find the last (most recent) bind-fail line first
    assert result is not None
    assert result["port"] == 8082


def test_scan_bind_fail_detects_failed_to_bind(log_root):
    _write_runtime_log(log_root, "Alpha", [
        "ERROR Failed to bind socket on port :9090",
    ])
    result = _REAL_SCAN_BIND_FAIL("Alpha")
    assert result is not None
    assert result["port"] == 9090


# ---------------------------------------------------------------------------
# _read_service_lifecycle unit tests
# ---------------------------------------------------------------------------

def _write_service_log(state_dir: Path, lines: list[str], suffix: str = "") -> Path:
    """Write JSONL lines to state_dir/logs/service.jsonl (or a rotated backup)."""
    d = state_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    name = f"service.jsonl{suffix}"
    p = d / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _sl(cfg) -> dict:
    """Call _read_service_lifecycle with default n."""
    return core._read_service_lifecycle(cfg)


def test_service_lifecycle_no_log_returns_empty(cfg):
    """Missing log → entries=[], note='no_log'."""
    result = _sl(cfg)
    assert result["entries"] == []
    assert result.get("note") == "no_log"


def test_service_lifecycle_single_start_stop(cfg, state_dir):
    """A start followed by a stop → no external_kill row."""
    _write_service_log(state_dir, [
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "event": "start",
                    "version": "1.0.0", "pid": 100}),
        json.dumps({"ts": "2026-01-01T01:00:00+00:00", "event": "stop",
                    "reason": "keyboard_interrupt"}),
    ])
    result = _sl(cfg)
    events = [e["event"] for e in result["entries"]]
    assert "external_kill" not in events
    assert "start" in events
    assert "stop" in events


def test_service_lifecycle_external_kill_detected(cfg, state_dir):
    """A start with no following stop or crash → external_kill row appended."""
    _write_service_log(state_dir, [
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "event": "start",
                    "version": "1.0.0", "pid": 200}),
    ])
    result = _sl(cfg)
    events = [e["event"] for e in result["entries"]]
    assert "external_kill" in events
    kill_row = next(e for e in result["entries"] if e["event"] == "external_kill")
    assert kill_row["pid"] == 200
    assert kill_row["version"] == "1.0.0"
    assert "externally" in kill_row["note"]


def test_service_lifecycle_crash_fields(cfg, state_dir):
    """Crash entry includes exc_type and first_frame."""
    tb = (
        "Traceback (most recent call last):\n"
        '  File "main.py", line 10, in run\n'
        "    raise RuntimeError(\"boom\")\n"
        "RuntimeError: boom"
    )
    _write_service_log(state_dir, [
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "event": "start",
                    "version": "1.2.3", "pid": 300}),
        json.dumps({"ts": "2026-01-01T01:00:00+00:00", "event": "crash",
                    "error": "RuntimeError: boom", "traceback": tb}),
    ])
    result = _sl(cfg)
    crash_row = next(e for e in result["entries"] if e["event"] == "crash")
    assert crash_row["exc_type"] == "RuntimeError"
    assert crash_row["first_frame"] is not None
    assert 'File "main.py"' in crash_row["first_frame"]


def test_service_lifecycle_last_n_entries(cfg, state_dir):
    """With more than n entries, only the last n are returned."""
    lines = []
    for i in range(10):
        lines.append(json.dumps({"ts": f"2026-01-01T0{i}:00:00+00:00",
                                  "event": "stop", "reason": f"r{i}"}))
    _write_service_log(state_dir, lines)
    result = core._read_service_lifecycle(cfg, n=3)
    assert len(result["entries"]) == 3
    assert result["n"] == 3


def test_service_lifecycle_canonical_fields(cfg, state_dir):
    """Every entry carries event, ts, version, pid (even when None)."""
    _write_service_log(state_dir, [
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "event": "start",
                    "version": "2.0.0", "pid": 42, "host": "0.0.0.0"}),
        json.dumps({"ts": "2026-01-01T01:00:00+00:00", "event": "stop",
                    "reason": "serve_returned"}),
    ])
    result = _sl(cfg)
    for entry in result["entries"]:
        assert "event" in entry
        assert "ts" in entry
        assert "version" in entry
        assert "pid" in entry
    # Extra fields from the raw log (host, reason) must NOT be present
    start_row = next(e for e in result["entries"] if e["event"] == "start")
    assert "host" not in start_row


def test_service_lifecycle_rotated_files_read(cfg, state_dir):
    """Rotated backups (.1, .2) are merged in chronological order."""
    # .2 is older, .1 is next, primary is newest
    _write_service_log(state_dir, [
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "event": "start",
                    "version": "0.9.0", "pid": 10}),
        json.dumps({"ts": "2026-01-01T01:00:00+00:00", "event": "stop",
                    "reason": "r1"}),
    ], suffix=".2")
    _write_service_log(state_dir, [
        json.dumps({"ts": "2026-01-02T00:00:00+00:00", "event": "start",
                    "version": "1.0.0", "pid": 20}),
        json.dumps({"ts": "2026-01-02T01:00:00+00:00", "event": "stop",
                    "reason": "r2"}),
    ], suffix=".1")
    _write_service_log(state_dir, [
        json.dumps({"ts": "2026-01-03T00:00:00+00:00", "event": "start",
                    "version": "1.1.0", "pid": 30}),
        json.dumps({"ts": "2026-01-03T01:00:00+00:00", "event": "stop",
                    "reason": "r3"}),
    ])
    result = core._read_service_lifecycle(cfg, n=10)
    pids = [e.get("pid") for e in result["entries"] if e.get("pid")]
    # All three starts must be present, oldest first
    assert pids == [10, 20, 30]


def test_doctor_includes_service_lifecycle_key(cfg):
    """doctor() always includes a 'service_lifecycle' key."""
    out = core.doctor(cfg)
    assert "service_lifecycle" in out
    sl = out["service_lifecycle"]
    assert "entries" in sl
    assert "n" in sl


def test_doctor_service_lifecycle_no_log_when_empty_state(cfg):
    """When no service.jsonl exists, service_lifecycle.note == 'no_log'."""
    out = core.doctor(cfg)
    assert out["service_lifecycle"].get("note") == "no_log"
    assert out["service_lifecycle"]["entries"] == []


def test_doctor_service_lifecycle_populated_after_start_stop(cfg, state_dir):
    """After writing start+stop events, doctor returns lifecycle entries."""
    core.service_event(cfg, "start", version="1.0.0", pid=999)
    core.service_event(cfg, "stop", reason="test")
    out = core.doctor(cfg)
    sl = out["service_lifecycle"]
    events = [e["event"] for e in sl["entries"]]
    assert "start" in events
    assert "stop" in events
    assert "external_kill" not in events


# ---------------------------------------------------------------------------
# optix_status(action="doctor") — service_lifecycle offline scenarios
#
# ---------------------------------------------------------------------------

def test_doctor_start_crash_dangling_start_lifecycle(cfg, state_dir):
    """service.jsonl with start→crash→start (no stop) via core.doctor():

    • service_lifecycle entries appear in chronological order:
      [start, crash, start, external_kill]
    • The crash row carries exc_type (parsed from 'error') and first_frame
      (first 'File …' line of the traceback)
    • The dangling second start is labelled external_kill with the pid and
      version of that start
    """
    tb = (
        "Traceback (most recent call last):\n"
        '  File "runner.py", line 42, in _emit_loop\n'
        "    raise ValueError(\"emit buffer overflow\")\n"
        "ValueError: emit buffer overflow"
    )
    _write_service_log(state_dir, [
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "event": "start",
                    "version": "1.0.0", "pid": 100}),
        json.dumps({"ts": "2026-01-01T01:00:00+00:00", "event": "crash",
                    "error": "ValueError: emit buffer overflow", "traceback": tb}),
        json.dumps({"ts": "2026-01-01T02:00:00+00:00", "event": "start",
                    "version": "1.0.1", "pid": 200}),
    ])
    out = core.doctor(cfg)
    sl = out["service_lifecycle"]
    entries = sl["entries"]

    # Entries are in chronological order; external_kill is appended after the
    # dangling start, so the sequence must be exactly four rows.
    events = [e["event"] for e in entries]
    assert events == ["start", "crash", "start", "external_kill"], (
        f"unexpected event order: {events}"
    )

    # Crash row must carry exc_type and first_frame.
    crash_row = next(e for e in entries if e["event"] == "crash")
    assert crash_row["exc_type"] == "ValueError"
    assert crash_row["first_frame"] is not None
    assert 'File "runner.py"' in crash_row["first_frame"]

    # Synthetic external_kill carries the metadata of the dangling start.
    kill_row = next(e for e in entries if e["event"] == "external_kill")
    assert kill_row["pid"] == 200
    assert kill_row["version"] == "1.0.1"
    assert "externally" in kill_row["note"]


def test_doctor_missing_service_log_no_exception(cfg):
    """Missing service.jsonl via core.doctor(): no exception propagates and
    service_lifecycle carries an explanatory note ('no_log') with empty entries.
    (Best-effort guarantee: core.py:2484.)
    """
    # No service.jsonl is created — the state_dir logs dir doesn't exist yet.
    out = core.doctor(cfg)  # must not raise
    sl = out["service_lifecycle"]
    assert isinstance(sl, dict), "service_lifecycle must be a dict even with no log"
    assert sl["entries"] == []
    assert sl.get("note") == "no_log", (
        f"expected note='no_log' when log is absent, got: {sl.get('note')!r}"
    )


@pytest.mark.skipif(sys.platform == "win32",
                    reason="chmod 0o000 does not revoke read access on Windows")
def test_doctor_unreadable_service_log_no_exception(cfg, state_dir):
    """Unreadable service.jsonl via core.doctor(): no exception propagates and
    service_lifecycle is a valid dict with an empty entries list.
    (Best-effort guarantee: core.py:2484; OSError path in _read_service_lifecycle.)
    """
    import os

    log_path = _write_service_log(state_dir, [
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "event": "start",
                    "version": "1.0.0", "pid": 100}),
    ])
    os.chmod(log_path, 0o000)  # remove all read permissions
    try:
        out = core.doctor(cfg)  # must not raise
    finally:
        os.chmod(log_path, 0o644)  # restore so tmp_path cleanup works

    sl = out["service_lifecycle"]
    assert isinstance(sl, dict), "service_lifecycle must be a dict even when log is unreadable"
    assert "entries" in sl
    # The unreadable file is silently skipped; no partial data or stale rows.
    assert sl["entries"] == []
