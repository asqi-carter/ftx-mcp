"""Tests for v0.2.0 §3.2 — `git_log`, deploy outcome buffer, last_deploy_tail.

Covers the pure-function layer in service.core. HTTP-wrapper tests live in
test_http_endpoints.py.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from service import core
from service.tests.conftest import FakeProc, make_fake_runner, make_project

# ---- git_log --------------------------------------------------------

class TestGitLog:
    def test_returns_empty_when_not_a_git_repo(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        make_project(projects_root, "Alpha")
        # No git init — _git rev-parse will fail.
        out = core.git_log(cfg, "Alpha")
        assert out == []

    def test_parses_git_log_output(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        sep = "\x1f"
        sample = (
            f"abc123{sep}Alice{sep}2026-05-06T10:00:00+00:00{sep}initial commit\n"
            f"def456{sep}Bob{sep}2026-05-07T11:00:00+00:00{sep}second commit"
        )

        def handler(_cmd: list[str], _kwargs: dict) -> FakeProc:
            return FakeProc(returncode=0, stdout=sample, stderr="")

        runner = make_fake_runner(handler)
        out = core.git_log(cfg, "Alpha", runner=runner)

        assert len(out) == 2
        assert out[0] == {
            "sha": "abc123",
            "author": "Alice",
            "date": "2026-05-06T10:00:00+00:00",
            "message": "initial commit",
        }
        assert out[1]["author"] == "Bob"
        assert out[1]["sha"] == "def456"

    def test_limit_is_clamped_to_100(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        captured: list[list[str]] = []

        def handler(cmd: list[str], _kwargs: dict) -> FakeProc:
            captured.append(cmd)
            return FakeProc(returncode=0, stdout="", stderr="")

        runner = make_fake_runner(handler)
        core.git_log(cfg, "Alpha", limit=10_000, runner=runner)

        # Find the git log call and confirm -n100 (the cap)
        log_calls = [c for c in captured if c[3:5] == ["log", "-n100"]]
        assert log_calls, f"no clamped -n100 call found in {captured}"

    def test_limit_is_floored_at_1(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        captured: list[list[str]] = []

        def handler(cmd: list[str], _kwargs: dict) -> FakeProc:
            captured.append(cmd)
            return FakeProc(returncode=0, stdout="", stderr="")

        runner = make_fake_runner(handler)
        core.git_log(cfg, "Alpha", limit=0, runner=runner)
        core.git_log(cfg, "Alpha", limit=-5, runner=runner)

        log_calls = [c for c in captured if "log" in c]
        # All log calls should have used -n1
        for c in log_calls:
            assert "-n1" in c, f"unexpected limit in {c}"

    def test_skips_malformed_lines(self, cfg: core.Config, projects_root: Path) -> None:
        make_project(projects_root, "Alpha")
        sep = "\x1f"
        # Three lines, middle one is truncated (only 2 fields).
        sample = (
            f"sha1{sep}A{sep}date1{sep}msg1\n"
            f"sha2{sep}truncated\n"
            f"sha3{sep}B{sep}date3{sep}msg3"
        )

        def handler(_cmd: list[str], _kwargs: dict) -> FakeProc:
            return FakeProc(returncode=0, stdout=sample, stderr="")

        runner = make_fake_runner(handler)
        out = core.git_log(cfg, "Alpha", runner=runner)
        assert len(out) == 2
        assert [e["sha"] for e in out] == ["sha1", "sha3"]

    def test_returns_empty_when_git_log_fails(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        make_project(projects_root, "Alpha")

        def handler(_cmd: list[str], _kwargs: dict) -> FakeProc:
            return FakeProc(returncode=128, stdout="", stderr="fatal: not a git repository")

        runner = make_fake_runner(handler)
        out = core.git_log(cfg, "Alpha", runner=runner)
        assert out == []

    def test_message_with_field_separator_is_safe(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        # The \x1f field separator is rejected by `git commit-tree`, but
        # belt-and-suspenders: even if it appeared, our `split(_, 3)`
        # would put the trailing \x1f into the message field intact
        # rather than splitting the row mid-message.
        make_project(projects_root, "Alpha")
        sep = "\x1f"
        sample = f"sha1{sep}A{sep}date1{sep}message{sep}with separator"

        def handler(_cmd: list[str], _kwargs: dict) -> FakeProc:
            return FakeProc(returncode=0, stdout=sample, stderr="")

        runner = make_fake_runner(handler)
        out = core.git_log(cfg, "Alpha", runner=runner)
        assert len(out) == 1
        assert out[0]["message"] == f"message{sep}with separator"


# ---- deploy buffer --------------------------------------------------

def _sample_result(state: str = "succeeded", project_label: str = "Alpha") -> dict:
    return {
        "state": state,
        "studio_exit": 0 if state != "failed" else 1,
        "started_at": "2026-05-06T10:00:00Z",
        "completed_at": "2026-05-06T10:01:00Z",
        "git_sha": "abc123",
        "files_written": [],
        "verification": {"method": "mtime", "confirmed_at": "2026-05-06T10:00:30Z",
                         "timeout_seconds": 30},
        "stdout_tail": f"deploy {project_label} stdout",
        "stderr_tail": f"deploy {project_label} stderr",
    }


class TestDeployBuffer:
    def test_record_creates_jsonl_file(self, cfg: core.Config) -> None:
        core.record_deploy_outcome(cfg, "Alpha", _sample_result())
        path = cfg.state_dir / core.DEPLOY_BUFFER_FILENAME
        assert path.exists()
        contents = path.read_text(encoding="utf-8")
        entry = json.loads(contents.splitlines()[0])
        assert entry["project"] == "Alpha"
        assert entry["state"] == "succeeded"

    def test_record_appends_multiple_entries(self, cfg: core.Config) -> None:
        core.record_deploy_outcome(cfg, "Alpha", _sample_result(state="succeeded"))
        core.record_deploy_outcome(cfg, "Bravo", _sample_result(state="failed"))
        path = cfg.state_dir / core.DEPLOY_BUFFER_FILENAME
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["project"] == "Alpha"
        assert json.loads(lines[1])["project"] == "Bravo"

    def test_buffer_caps_at_100_entries(self, cfg: core.Config) -> None:
        for i in range(150):
            core.record_deploy_outcome(cfg, f"P{i}", _sample_result())
        path = cfg.state_dir / core.DEPLOY_BUFFER_FILENAME
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 100
        # Oldest dropped: first kept is P50.
        assert json.loads(lines[0])["project"] == "P50"
        assert json.loads(lines[-1])["project"] == "P149"

    def test_buffer_caps_at_1mb(self, cfg: core.Config) -> None:
        # Synthesize a single oversized result to test byte-cap. We
        # write a giant stderr_tail to push one entry over the byte
        # threshold quickly.
        big = _sample_result()
        big["stderr_tail"] = "x" * (200 * 1024)  # 200 KB per entry

        # 6 entries × 200 KB ≈ 1.2 MB → should trim to fit under 1 MB.
        for i in range(6):
            big["project"] = f"P{i}"
            core.record_deploy_outcome(cfg, f"P{i}", big)

        path = cfg.state_dir / core.DEPLOY_BUFFER_FILENAME
        size = path.stat().st_size
        assert size <= core.DEPLOY_BUFFER_MAX_BYTES, f"file is {size} bytes, cap is 1 MB"

    def test_record_swallows_io_errors(
        self, cfg: core.Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate a write failure — record_deploy_outcome must not raise.
        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(Path, "open", boom)
        # Should not raise.
        core.record_deploy_outcome(cfg, "Alpha", _sample_result())


class TestLastDeployTail:
    def test_returns_none_for_missing_buffer(self, cfg: core.Config) -> None:
        assert core.last_deploy_tail(cfg) is None

    def test_returns_none_for_empty_buffer(self, cfg: core.Config) -> None:
        path = cfg.state_dir / core.DEPLOY_BUFFER_FILENAME
        path.write_text("", encoding="utf-8")
        assert core.last_deploy_tail(cfg) is None

    def test_returns_last_entry(self, cfg: core.Config) -> None:
        core.record_deploy_outcome(cfg, "Alpha", _sample_result(state="succeeded"))
        core.record_deploy_outcome(cfg, "Bravo", _sample_result(state="failed"))
        entry = core.last_deploy_tail(cfg)
        assert entry is not None
        assert entry["project"] == "Bravo"
        assert entry["state"] == "failed"

    def test_skips_malformed_trailing_lines(self, cfg: core.Config) -> None:
        path = cfg.state_dir / core.DEPLOY_BUFFER_FILENAME
        good = json.dumps({"project": "Alpha", "state": "succeeded"})
        path.write_text(good + "\n" + "{not json\n", encoding="utf-8")
        entry = core.last_deploy_tail(cfg)
        # Walks backwards past the malformed line to the last good record.
        assert entry == {"project": "Alpha", "state": "succeeded"}


# ---- deploy() integration -----------------------------------------

def _stub_studio_runner(returncode: int = 0, stderr: str = "") -> core.Runner:
    """Studio export handler that writes the staging tree on exit 0 (so
    atomic_swap has something to move) and returns the configured stderr
    on non-zero. Git calls pass through as success/no-op."""
    from .conftest import make_export_handler  # noqa: PLC0415
    export_h = make_export_handler()

    def handler(cmd: list[str], kwargs: dict) -> FakeProc:
        if "FTOptixStudio.exe" in cmd[0]:
            if returncode != 0:
                return FakeProc(returncode=returncode, stdout="", stderr=stderr)
            return export_h(cmd, kwargs)
        return FakeProc(returncode=0, stdout="", stderr="")

    return make_fake_runner(handler)


class _NoopRuntime:
    def stop(self, _cfg, _pdir): pass
    def start(self, _cfg, _pdir): pass


class TestDeployRecordsOutcome:
    def test_succeeded_deploy_records_to_buffer(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        make_project(projects_root, "Alpha")
        runner = _stub_studio_runner(returncode=0)

        def verify_ok(_cfg: core.Config, _pdir: Path, _started: float) -> dict:
            return {"method": "runtime_probe",
                    "confirmed_at": "2026-05-06T10:00:30Z",
                    "timeout_seconds": 30}

        result = core.deploy(
            cfg, "Alpha", core.DeployRequest(),
            runner=runner, runtime=_NoopRuntime(), verify=verify_ok,
        )
        assert result["state"] == "succeeded"

        entry = core.last_deploy_tail(cfg)
        assert entry is not None
        assert entry["project"] == "Alpha"
        assert entry["state"] == "succeeded"

    def test_failed_deploy_records_to_buffer(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        make_project(projects_root, "Alpha")
        runner = _stub_studio_runner(returncode=1, stderr="boom")

        result = core.deploy(
            cfg, "Alpha", core.DeployRequest(),
            runner=runner, runtime=_NoopRuntime(),
        )
        assert result["state"] == "failed"

        entry = core.last_deploy_tail(cfg)
        assert entry is not None
        assert entry["project"] == "Alpha"
        assert entry["state"] == "failed"
        assert "boom" in entry["stderr_tail"]


# ---- helper smoke ---------------------------------------------------

class TestRunnerHelper:
    """Smoke check that subprocess.CompletedProcess construction in
    make_fake_runner survives the new dispatch paths in core."""

    def test_runner_returns_completed_process(self) -> None:
        runner = make_fake_runner(lambda _c, _k: FakeProc(returncode=0))
        proc = runner.run(["echo"])
        assert isinstance(proc, subprocess.CompletedProcess)
        assert proc.returncode == 0
