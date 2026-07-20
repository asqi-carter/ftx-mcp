"""deploy_preflight + standardized error envelope tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service import core
from service.http_app import make_app
from service.tests.conftest import FakeProc, make_fake_runner, make_project

# ---- preflight contract --------------------------------------------


def test_preflight_ready_when_all_preconditions_satisfied(
    cfg: core.Config, projects_root: Path
) -> None:
    make_project(projects_root, "Alpha")
    runner = make_fake_runner(lambda _c, _k: FakeProc(returncode=0))
    out = core.deploy_preflight(cfg, "Alpha", runner=runner)
    assert "ready" in out
    assert "blockers" in out
    assert "warnings" in out
    assert "checks" in out
    blocker_codes = [b["code"] for b in out["blockers"]]
    assert "runtime_dir_not_configured" not in blocker_codes
    assert "studio_exe_missing" not in blocker_codes
    assert "project_not_found" not in blocker_codes


def test_preflight_blocks_when_runtime_dir_missing(
    projects_root: Path, state_dir: Path, tmp_path: Path
) -> None:
    studio_exe = tmp_path / "FTOptixStudio.exe"
    studio_exe.write_text("fake")
    cfg = core.Config(
        projects_root=projects_root,
        studio_exe=studio_exe,
        state_dir=state_dir,
        runtime_dir=None,  # missing
    )
    make_project(projects_root, "Alpha")
    runner = make_fake_runner(lambda _c, _k: FakeProc(returncode=0))
    out = core.deploy_preflight(cfg, "Alpha", runner=runner)
    assert out["ready"] is False
    codes = [b["code"] for b in out["blockers"]]
    assert "runtime_dir_not_configured" in codes


def test_preflight_blocks_when_studio_missing(
    projects_root: Path, state_dir: Path, runtime_dir: Path, tmp_path: Path
) -> None:
    cfg = core.Config(
        projects_root=projects_root,
        studio_exe=tmp_path / "DoesNotExist.exe",
        state_dir=state_dir,
        runtime_dir=runtime_dir,
    )
    make_project(projects_root, "Alpha")
    runner = make_fake_runner(lambda _c, _k: FakeProc(returncode=0))
    out = core.deploy_preflight(cfg, "Alpha", runner=runner)
    assert out["ready"] is False
    codes = [b["code"] for b in out["blockers"]]
    assert "studio_exe_missing" in codes


def test_preflight_blocks_when_project_missing(cfg: core.Config) -> None:
    runner = make_fake_runner(lambda _c, _k: FakeProc(returncode=0))
    out = core.deploy_preflight(cfg, "DoesNotExist", runner=runner)
    assert out["ready"] is False
    codes = [b["code"] for b in out["blockers"]]
    assert "project_not_found" in codes


def test_preflight_runtime_port_check_is_informational(
    cfg: core.Config, projects_root: Path
) -> None:
    """A stopped runtime (no listener on the test port) is the normal
    pre-deploy state in v0.2.x — the deploy bounces it. Preflight reports
    the probe result but neither blocks nor warns on absence."""
    make_project(projects_root, "Alpha")
    runner = make_fake_runner(lambda _c, _k: FakeProc(returncode=0))
    out = core.deploy_preflight(cfg, "Alpha", runner=runner)
    assert "runtime" in out["checks"]
    assert out["checks"]["runtime"]["port"] == cfg.runtime_test_port
    assert "tcp_reachable" in out["checks"]["runtime"]
    blocker_codes = [b["code"] for b in out["blockers"]]
    warning_codes = [w["code"] for w in out["warnings"]]
    assert "target_unreachable" not in blocker_codes
    assert "target_unreachable" not in warning_codes


# ---- standardized error envelope -----------------------------------


def test_error_envelope_on_project_not_found(
    cfg: core.Config, projects_root: Path
) -> None:
    app = make_app(cfg)
    client = TestClient(app)
    r = client.get("/projects/Nonexistent/files/whatever.yaml")
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "project_not_found"
    assert "message" in body
    assert "hint" in body


def test_error_envelope_on_path_traversal(
    cfg: core.Config, projects_root: Path
) -> None:
    make_project(projects_root, "Alpha")
    app = make_app(cfg)
    client = TestClient(app)
    r = client.post(
        "/projects/Alpha/deploy",
        json={
            "edits": [{"path": "../../escape.txt", "content": "x"}],
            "commit_message": "smoke",
            "run_after_deploy": False,
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "path_traversal_rejected"
    assert "hint" in body


def test_error_envelope_on_runtime_dir_missing(
    projects_root: Path, state_dir: Path, tmp_path: Path
) -> None:
    studio_exe = tmp_path / "FTOptixStudio.exe"
    studio_exe.write_text("fake")
    cfg = core.Config(
        projects_root=projects_root,
        studio_exe=studio_exe,
        state_dir=state_dir,
        runtime_dir=None,
        enable_deploy=True,
    )
    make_project(projects_root, "Alpha")
    app = make_app(cfg)
    client = TestClient(app)
    r = client.post(
        "/projects/Alpha/deploy",
        json={"edits": [], "commit_message": "smoke", "run_after_deploy": False},
    )
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "runtime_dir_not_configured"
    assert "hint" in body


def test_preflight_endpoint_returns_envelope(
    cfg: core.Config, projects_root: Path
) -> None:
    make_project(projects_root, "Alpha")
    app = make_app(cfg)
    client = TestClient(app)
    r = client.post("/projects/Alpha/deploy/preflight")
    assert r.status_code == 200
    body = r.json()
    assert "ready" in body
    assert "blockers" in body
    assert "warnings" in body
    assert "checks" in body


@pytest.mark.parametrize("code,status_attr", [
    ("project_not_found", 404),
    ("path_traversal_rejected", 400),
    ("binary_file_unsupported", 415),
    ("runtime_dir_not_configured", 500),
    ("studio_exe_missing", 500),
    ("studio_open", 409),
    ("editor_project_open", 409),
])
def test_error_codes_have_consistent_http_status(code: str, status_attr: int) -> None:
    """Every CoreError subclass exports `code` + `http_status` as class
    attrs and they must match the documented mapping."""
    found = False
    for cls in core.CoreError.__subclasses__():
        if cls.code == code:
            assert cls.http_status == status_attr, (
                f"{cls.__name__}.http_status={cls.http_status} but expected {status_attr}"
            )
            found = True
    assert found, f"no CoreError subclass with code={code}"
