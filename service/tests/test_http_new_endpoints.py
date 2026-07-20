"""HTTP-surface tests for v0.2.0 §3.2 — git/log + last-deploy-tail.

Black-box: hit the FastAPI app via TestClient and verify response shape.
The pure-function layer is covered in test_git_log_and_deploy_buffer.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from service import core
from service.http_app import make_app
from service.tests.conftest import FakeProc, make_fake_runner, make_project


def _result(state: str = "succeeded") -> dict:
    return {
        "state": state,
        "studio_exit": 0,
        "started_at": "2026-05-06T10:00:00Z",
        "completed_at": "2026-05-06T10:01:00Z",
        "git_sha": "abc123",
        "files_written": [],
        "verification": {"method": "mtime", "confirmed_at": "2026-05-06T10:00:30Z",
                         "timeout_seconds": 30},
        "stdout_tail": "stdout snippet",
        "stderr_tail": "stderr snippet",
    }


class TestGitLogEndpoint:
    def test_returns_commits_array(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        sep = "\x1f"
        sample = (
            f"abc123{sep}Alice{sep}2026-05-06T10:00:00+00:00{sep}initial commit\n"
            f"def456{sep}Bob{sep}2026-05-07T11:00:00+00:00{sep}second commit"
        )

        def handler(_cmd: list[str], _kwargs: dict) -> FakeProc:
            return FakeProc(returncode=0, stdout=sample, stderr="")

        # The endpoint resolves _DEFAULT_RUNNER as the function default, so
        # rebinding the module attr is too late. Patch the live Runner's fn.
        original_fn = core._DEFAULT_RUNNER.fn
        core._DEFAULT_RUNNER.fn = make_fake_runner(handler).fn
        try:
            client = TestClient(make_app(cfg))
            r = client.get("/projects/Alpha/git/log")
        finally:
            core._DEFAULT_RUNNER.fn = original_fn

        assert r.status_code == 200
        data: dict[str, Any] = r.json()
        assert "commits" in data
        assert len(data["commits"]) == 2
        assert data["commits"][0]["sha"] == "abc123"

    def test_unknown_project_404(self, cfg: core.Config) -> None:
        client = TestClient(make_app(cfg))
        r = client.get("/projects/Nonexistent/git/log")
        assert r.status_code == 404
        assert r.json()["code"] == "project_not_found"

    def test_limit_query_param_passes_through(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        make_project(projects_root, "Alpha")
        captured: list[list[str]] = []

        def handler(cmd: list[str], _kwargs: dict) -> FakeProc:
            captured.append(cmd)
            return FakeProc(returncode=0, stdout="", stderr="")

        original_fn = core._DEFAULT_RUNNER.fn
        core._DEFAULT_RUNNER.fn = make_fake_runner(handler).fn
        try:
            client = TestClient(make_app(cfg))
            r = client.get("/projects/Alpha/git/log?limit=5")
        finally:
            core._DEFAULT_RUNNER.fn = original_fn

        assert r.status_code == 200
        log_calls = [c for c in captured if "log" in c]
        assert any("-n5" in c for c in log_calls)


class TestLastDeployTailEndpoint:
    def test_returns_null_when_buffer_empty(self, cfg: core.Config) -> None:
        client = TestClient(make_app(cfg))
        r = client.get("/services/last-deploy-tail")
        assert r.status_code == 200
        assert r.json() == {"deploy": None}

    def test_returns_last_recorded_outcome(self, cfg: core.Config) -> None:
        core.record_deploy_outcome(cfg, "Alpha", _result(state="succeeded"))
        core.record_deploy_outcome(cfg, "Bravo", _result(state="failed"))

        client = TestClient(make_app(cfg))
        r = client.get("/services/last-deploy-tail")
        assert r.status_code == 200
        body = r.json()
        assert body["deploy"]["project"] == "Bravo"
        assert body["deploy"]["state"] == "failed"
        assert body["deploy"]["stderr_tail"] == "stderr snippet"
