"""Arm syncs the shipped StudioMCPBridge.cs into the project (PRD 0041 #47)."""
from __future__ import annotations

from pathlib import Path

import pytest

from service import core, studio_arm
from service.tests.conftest import make_project

SHIPPED = core._shipped_bridge_cs().read_bytes()


def _bridge_file(proj: Path) -> Path:
    return proj.joinpath(*core._BRIDGE_CS_REL)


def _put(proj: Path, data: bytes) -> Path:
    f = _bridge_file(proj)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


@pytest.fixture
def armed_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def fake(project, project_dir, **kw):
        calls.append({"project": project, "dir": project_dir, **kw})
        return {"ok": True, "state": "armed"}

    monkeypatch.setattr(studio_arm, "execute_method", fake)
    monkeypatch.delenv("FTX_BRIDGE_SYNC", raising=False)
    return calls


def test_bridge_hash_normalization_ignores_eol_bom_and_trailing_spaces() -> None:
    base = b"class A\n{\n    int x;\n}\n"
    variants = [
        base.replace(b"\n", b"\r\n"),
        b"\xef\xbb\xbf" + base,
        b"class A  \n{\t\n    int x;   \n}\n",
        b"\xef\xbb\xbf" + base.replace(b"\n", b"\r\n").replace(b"A", b"A "),
    ]
    h = core.bridge_source_hash(base)
    for v in variants:
        assert core.bridge_source_hash(v) == h
    assert core.bridge_source_hash(base.replace(b"int", b"long")) != h
    # Per-line leading/trailing whitespace is not content (0052 F).
    assert core.bridge_source_hash(base.replace(b"    int", b"  int")) == h


def test_bridge_source_hash_ignores_using_and_blank_lines() -> None:
    base = b"using System;\nclass A\n{\n    int x;\n}\n"
    studio = (b"using System;\r\nusing FTOptix.CommunicationDriver;\r\n\r\n"
              b"class A\r\n{\r\n\r\n    int x;\r\n}\r\n")
    assert core.bridge_source_hash(studio) == core.bridge_source_hash(base)
    # a real code change is still different (stale)
    assert core.bridge_source_hash(studio.replace(b"int x", b"long x")) != core.bridge_source_hash(base)
    # a using STATEMENT is code, not a directive
    with_stmt = base.replace(b"int x;", b"using var f = Open();")
    assert core.bridge_source_hash(with_stmt) != core.bridge_source_hash(
        with_stmt.replace(b"using var f = Open();", b"var f = Open();"))


def test_compare_bridge_source_current_after_studio_rewrite(cfg, projects_root) -> None:
    proj = make_project(projects_root)
    body = SHIPPED.removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    rewritten = b"\xef\xbb\xbfusing FTOptix.CommunicationDriver;\r\n\r\n" + body
    _put(proj, rewritten)
    assert core._compare_bridge_source(proj)[0]["bridge_source"] == "current"
    _put(proj, SHIPPED + b"\n// real change\nint y;\n")
    assert core._compare_bridge_source(proj)[0]["bridge_source"] == "stale"


def test_arm_stale_bridge_backs_up_writes_and_reports_synced(cfg, projects_root, armed_calls) -> None:
    proj = make_project(projects_root)
    stale = b"// old bridge\n"
    f = _put(proj, stale)
    out = core.bridge_arm(cfg, "TestProj")
    assert out["ok"] and out["bridge_source"] == "synced"
    assert f.read_bytes() == SHIPPED
    backup = Path(out["bridge_backup"])
    assert backup.parent.name == ".ftx-mcp-backups"
    assert backup.read_bytes() == stale
    assert out["bridge_project_hash"] == core.bridge_source_hash(stale)[:12]
    assert out["bridge_shipped_hash"] == core.bridge_source_hash(SHIPPED)[:12]
    assert len(armed_calls) == 1
    # No temp files left in the NetSolution glob.
    assert [p.name for p in f.parent.iterdir()] == ["StudioMCPBridge.cs"]


def test_arm_current_bridge_is_not_written(cfg, projects_root, armed_calls) -> None:
    proj = make_project(projects_root)
    # CRLF + BOM variant counts as current and must stay byte-for-byte.
    variant = b"\xef\xbb\xbf" + SHIPPED.removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    f = _put(proj, variant)
    mtime = f.stat().st_mtime_ns
    out = core.bridge_arm(cfg, "TestProj")
    assert out["bridge_source"] == "current"
    assert out["bridge_project_hash"] == out["bridge_shipped_hash"]
    assert "bridge_backup" not in out
    assert f.read_bytes() == variant and f.stat().st_mtime_ns == mtime
    assert not (proj / "ProjectFiles" / ".ftx-mcp-backups").exists()


def test_arm_sync_opt_out_reports_stale_and_does_not_write(
        cfg, projects_root, armed_calls, monkeypatch) -> None:
    monkeypatch.setenv("FTX_BRIDGE_SYNC", "0")
    proj = make_project(projects_root)
    f = _put(proj, b"// old bridge\n")
    out = core.bridge_arm(cfg, "TestProj")
    assert out["bridge_source"] == "stale"
    assert "FTX_BRIDGE_SYNC=0" in out["warning"]
    assert f.read_bytes() == b"// old bridge\n"
    assert not (proj / "ProjectFiles" / ".ftx-mcp-backups").exists()
    assert len(armed_calls) == 1


def test_arm_without_project_bridge_file_keeps_todays_error(cfg, projects_root) -> None:
    proj = make_project(projects_root)
    assert not _bridge_file(proj).exists()
    out = core.bridge_arm(cfg, "TestProj")
    assert out["ok"] is False and out["error"] == "bridge_netlogic_absent"
    assert "bridge_source" not in out
    assert not _bridge_file(proj).exists()


def test_stop_does_not_sync(cfg, projects_root, armed_calls) -> None:
    proj = make_project(projects_root)
    f = _put(proj, b"// old bridge\n")
    out = core.bridge_arm(cfg, "TestProj", action="stop")
    assert "bridge_source" not in out
    assert f.read_bytes() == b"// old bridge\n"


# ---- bridge_source in optix_bridge_status / health (PRD 0041 #47) -----------

def _armed_http(project_dir: Path | None):
    import json

    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        if not path.startswith("/bridge/health"):
            return 404, b"{}"
        body = {"project": "TestProj", "bridge_version": "1.0.9",
                "model_loaded": True, "port": 8768}
        if project_dir is not None:
            body["project_dir"] = str(project_dir)
        return 200, json.dumps(body).encode()
    return fake


def _status(cfg) -> dict:
    from service.mcp_app import make_mcp
    tool = {t.name: t for t in make_mcp(cfg)._tool_manager.list_tools()}["optix_bridge_status"]
    return getattr(tool, "_ftx_sync_fn", tool.fn)()


@pytest.fixture(autouse=False)
def _fresh_bridge_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


def test_bridge_status_reports_current_when_hashes_match(
        cfg, projects_root, monkeypatch, _fresh_bridge_cache) -> None:
    proj = make_project(projects_root)
    _put(proj, SHIPPED.replace(b"\n", b"\r\n"))  # normalization-equal
    monkeypatch.setattr(core, "_bridge_http", _armed_http(proj))
    out = _status(cfg)
    assert out["bridge_source"] == "current"
    assert out["bridges"][0]["bridge_source"] == "current"
    assert core.health(cfg)["bridge_source"] == "current"


def test_bridge_status_reports_stale_when_hashes_differ(
        cfg, projects_root, monkeypatch, _fresh_bridge_cache) -> None:
    proj = make_project(projects_root)
    f = _put(proj, b"// old bridge\n")
    monkeypatch.setattr(core, "_bridge_http", _armed_http(proj))
    out = _status(cfg)
    assert out["bridge_source"] == "stale"
    assert out["bridges"][0]["bridge_source"] == "stale"
    assert core.health(cfg)["bridge_source"] == "stale"
    assert f.read_bytes() == b"// old bridge\n"  # status never writes


def test_bridge_status_reports_unknown_when_comparison_impossible(
        cfg, projects_root, monkeypatch, _fresh_bridge_cache) -> None:
    proj = make_project(projects_root)  # no StudioMCPBridge.cs in the project
    monkeypatch.setattr(core, "_bridge_http", _armed_http(proj))
    assert _status(cfg)["bridge_source"] == "unknown"
    # bridge reports no project dir at all
    core.reset_bridge_cache()
    monkeypatch.setattr(core, "_bridge_http", _armed_http(None))
    assert _status(cfg)["bridge_source"] == "unknown"
    # shipped file unreadable
    _put(proj, SHIPPED)
    core.reset_bridge_cache()
    monkeypatch.setattr(core, "_bridge_http", _armed_http(proj))
    monkeypatch.setattr(core, "_shipped_bridge_cs", lambda: None)
    assert _status(cfg)["bridge_source"] == "unknown"


def test_health_includes_bridge_source_when_nothing_armed(cfg, monkeypatch, _fresh_bridge_cache) -> None:
    def refuse(*a, **k):
        raise core.BridgeUnavailable("nothing listening")
    monkeypatch.setattr(core, "_bridge_http", refuse)
    assert core.health(cfg)["bridge_source"] == "unknown"


# ---- PRD 0049 E (#66): a failing NetSolution build is not a verify_timeout ----

def _timeout_arm(monkeypatch) -> None:
    monkeypatch.setattr(studio_arm, "execute_method", lambda *a, **k: {
        "ok": False, "error": "verify_timeout",
        "detail": "clicked 'Execute StartBridge' but no bridge is serving"})
    monkeypatch.delenv("FTX_BRIDGE_SYNC", raising=False)


def test_arm_unanswered_bridge_with_failing_build_is_netsolution_build_failed(
        cfg, projects_root, monkeypatch) -> None:
    make_project(projects_root)
    _timeout_arm(monkeypatch)
    err = {"file": "ProjectFiles/NetSolution/Foo.cs", "line": 12, "col": 5,
           "code": "CS1002", "message": "; expected"}
    monkeypatch.setattr(core, "build_check", lambda *a, **k: {
        "ok": False, "returncode": 1, "error_count": 1, "errors": [err]})
    out = core.bridge_arm(cfg, "TestProj")
    assert out["ok"] is False
    assert out["error"] == "netsolution_build_failed"
    assert "Foo.cs(12,5)" in out["detail"] and "CS1002" in out["detail"]
    assert "; expected" in out["detail"] and "\n" not in out["detail"]
    assert out["hint"] and out["error"] != "verify_timeout"


def test_arm_unanswered_bridge_with_clean_build_stays_verify_timeout(
        cfg, projects_root, monkeypatch) -> None:
    make_project(projects_root)
    _timeout_arm(monkeypatch)
    monkeypatch.setattr(core, "build_check", lambda *a, **k: {
        "ok": True, "returncode": 0, "error_count": 0, "errors": []})
    assert core.bridge_arm(cfg, "TestProj")["error"] == "verify_timeout"


def test_arm_unanswered_bridge_build_not_runnable_stays_verify_timeout(
        cfg, projects_root, monkeypatch) -> None:
    make_project(projects_root)
    _timeout_arm(monkeypatch)
    monkeypatch.setattr(core, "build_check", lambda *a, **k: {
        "ok": False, "error": "no_dotnet", "message": "dotnet not found"})
    assert core.bridge_arm(cfg, "TestProj")["error"] == "verify_timeout"
