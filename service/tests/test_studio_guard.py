"""Studio-open corruption guard tests (v0.2.3 W1).

Detection itself was probed on real hardware (Studio 1.7.1.46, Windows 11
25H2); these tests cover the guard's
wiring: gate placement on reads and deploys, the TTL cache, the post-lock
TOCTOU re-check, editor attribution matching, and the HTTP error envelope.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service import core, studio_guard
from service.http_app import make_app
from service.tests.conftest import FakeProc, make_fake_runner, make_project

STUDIO = {
    "pid": 4242,
    "name": "ftoptixstudio.exe",
    "cmdline": [r"C:\Program Files\Rockwell Automation\FactoryTalk Optix\Studio 1.7.1.46\FTOptixStudio.exe"],
}


def _code(cmdline: list[str]) -> dict:
    return {"pid": 777, "name": "code.exe", "cmdline": cmdline}


@pytest.fixture(autouse=True)
def _fresh_guard_cache():
    studio_guard.reset_cache()
    yield
    studio_guard.reset_cache()


def set_procs(monkeypatch: pytest.MonkeyPatch, procs: list[dict]) -> None:
    """Point the guard's scanner at a fixed process list."""
    monkeypatch.setattr(studio_guard, "_scan", lambda: list(procs))
    studio_guard.reset_cache()


# ---- studio_state unit behavior --------------------------------------


def test_studio_state_reports_running(monkeypatch: pytest.MonkeyPatch) -> None:
    set_procs(monkeypatch, [STUDIO])
    state = studio_guard.studio_state()
    assert state["studio"]["running"] is True
    assert state["studio"]["pids"] == [4242]
    assert state["editors"] == []


def test_studio_state_ttl_cache_and_force(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def counting_scan() -> list[dict]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(studio_guard, "_scan", counting_scan)
    studio_guard.reset_cache()
    studio_guard.studio_state()
    studio_guard.studio_state()  # within TTL -> served from cache
    assert calls["n"] == 1
    studio_guard.studio_state(force=True)  # bypasses cache
    assert calls["n"] == 2


def test_studio_state_enumeration_failure_is_error_not_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> list[dict]:
        raise OSError("proc table unavailable")

    monkeypatch.setattr(studio_guard, "_scan", boom)
    studio_guard.reset_cache()
    state = studio_guard.studio_state()
    assert "error" in state
    assert "studio" not in state


def test_attributed_editors_is_case_and_slash_insensitive(tmp_path: Path) -> None:
    project_dir = tmp_path / "Alpha"
    state = {
        "editors": [
            _code([str(project_dir)]),
            _code([str(project_dir).upper().replace("/", "\\")]),
            _code(["--folder-uri", "file:///somewhere/else"]),
        ],
    }
    hits = studio_guard.attributed_editors(state, project_dir)
    assert len(hits) == 2


# ---- read gate --------------------------------------------------------


def test_read_file_refused_while_studio_running(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = make_project(projects_root, "Alpha")
    (p / "Nodes").mkdir()
    (p / "Nodes" / "UI.yaml").write_text("Name: UI\n", encoding="utf-8")
    set_procs(monkeypatch, [STUDIO])
    with pytest.raises(core.StudioOpen):
        core.read_file(cfg, "Alpha", "Nodes/UI.yaml")


def test_read_file_ok_when_studio_closed(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = make_project(projects_root, "Alpha")
    (p / "Nodes").mkdir()
    # Pin LF on disk: Windows text-mode write translates \n -> \r\n, which
    # would break the exact-EOL assertion below (read_file preserves EOL).
    (p / "Nodes" / "UI.yaml").write_text("Name: UI\n", encoding="utf-8", newline="\n")
    set_procs(monkeypatch, [])
    out = core.read_file(cfg, "Alpha", "Nodes/UI.yaml")
    assert out["content"] == "Name: UI\n"


def test_read_file_proceeds_on_detection_error(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enumeration fault is an infra problem, not evidence of Studio —
    reads proceed (preflight carries the warning)."""
    p = make_project(projects_root, "Alpha")
    # Pin LF on disk (see note above): keeps the exact-EOL assertion portable.
    (p / "f.yaml").write_text("x: 1\n", encoding="utf-8", newline="\n")

    def boom() -> list[dict]:
        raise OSError("no proc table")

    monkeypatch.setattr(studio_guard, "_scan", boom)
    studio_guard.reset_cache()
    assert core.read_file(cfg, "Alpha", "f.yaml")["content"] == "x: 1\n"


def test_read_file_http_409_envelope(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = make_project(projects_root, "Alpha")
    (p / "f.yaml").write_text("x: 1\n", encoding="utf-8")
    set_procs(monkeypatch, [STUDIO])
    client = TestClient(make_app(cfg))
    r = client.get("/projects/Alpha/files/f.yaml")
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "studio_open"
    assert "hint" in body
    assert "docs_url" in body


# ---- deploy gates ------------------------------------------------------


def test_deploy_refused_at_entry_writes_nothing(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = make_project(projects_root, "Alpha")
    set_procs(monkeypatch, [STUDIO])
    runner = make_fake_runner(lambda _c, _k: FakeProc(returncode=0))
    req = core.DeployRequest(
        edits=[{"path": "Nodes/New.yaml", "content": "Name: New\n"}],
        commit_message="guard test",
        run_after_deploy=False,
    )
    with pytest.raises(core.StudioOpen):
        core.deploy(cfg, "Alpha", req, runner=runner)
    assert not (project_dir / "Nodes" / "New.yaml").exists()
    # refused at entry: nothing started, so no outcome buffer entry
    assert core.last_deploy_tail(cfg) is None
    # and the deploy lock is not left held
    assert not (cfg.state_dir / "deploy.lock").exists()


def test_deploy_toctou_recheck_refuses_inside_lock(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Studio opens between the entry check and the first write: the forced
    post-lock re-check must refuse before bytes land, and the refusal is
    recorded in the outcome buffer (exception path)."""
    project_dir = make_project(projects_root, "Alpha")
    responses: list[list[dict]] = [[], [STUDIO]]  # entry-check, post-lock recheck

    def sequenced_scan() -> list[dict]:
        return responses.pop(0) if responses else [STUDIO]

    monkeypatch.setattr(studio_guard, "_scan", sequenced_scan)
    studio_guard.reset_cache()
    runner = make_fake_runner(lambda _c, _k: FakeProc(returncode=0))
    req = core.DeployRequest(
        edits=[{"path": "Nodes/New.yaml", "content": "Name: New\n"}],
        commit_message="toctou test",
        run_after_deploy=False,
    )
    with pytest.raises(core.StudioOpen):
        core.deploy(cfg, "Alpha", req, runner=runner)
    assert not (project_dir / "Nodes" / "New.yaml").exists()
    entry = core.last_deploy_tail(cfg)
    assert entry is not None
    assert entry["state"] == "failed"
    assert "StudioOpen" in entry["stderr_tail"]
    assert not (cfg.state_dir / "deploy.lock").exists()


# ---- preflight check #8 ------------------------------------------------


def test_preflight_blocks_when_studio_running(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_project(projects_root, "Alpha")
    set_procs(monkeypatch, [STUDIO])
    runner = make_fake_runner(lambda _c, _k: FakeProc(returncode=0))
    out = core.deploy_preflight(cfg, "Alpha", runner=runner)
    assert out["ready"] is False
    codes = [b["code"] for b in out["blockers"]]
    assert "studio_open" in codes
    assert out["checks"]["studio_guard"]["studio_running"] is True
    assert out["checks"]["studio_guard"]["studio_pids"] == [4242]


def test_preflight_blocks_attributed_editor(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = make_project(projects_root, "Alpha")
    set_procs(monkeypatch, [_code([str(project_dir / "ProjectFiles" / "NetSolution")])])
    runner = make_fake_runner(lambda _c, _k: FakeProc(returncode=0))
    out = core.deploy_preflight(cfg, "Alpha", runner=runner)
    assert out["ready"] is False
    codes = [b["code"] for b in out["blockers"]]
    assert "editor_project_open" in codes


def test_preflight_warns_unattributed_editor(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_project(projects_root, "Alpha")
    set_procs(monkeypatch, [_code(["/some/other/workspace"])])
    runner = make_fake_runner(lambda _c, _k: FakeProc(returncode=0))
    out = core.deploy_preflight(cfg, "Alpha", runner=runner)
    codes = [b["code"] for b in out["blockers"]]
    assert "editor_project_open" not in codes
    assert "studio_open" not in codes
    warning_codes = [w["code"] for w in out["warnings"]]
    assert "editor_processes_detected" in warning_codes


def test_preflight_warns_when_detection_unavailable(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_project(projects_root, "Alpha")

    def boom() -> list[dict]:
        raise OSError("no proc table")

    monkeypatch.setattr(studio_guard, "_scan", boom)
    studio_guard.reset_cache()
    runner = make_fake_runner(lambda _c, _k: FakeProc(returncode=0))
    out = core.deploy_preflight(cfg, "Alpha", runner=runner)
    warning_codes = [w["code"] for w in out["warnings"]]
    assert "studio_guard_unavailable" in warning_codes
    # detection failure alone must not block
    codes = [b["code"] for b in out["blockers"]]
    assert "studio_open" not in codes
