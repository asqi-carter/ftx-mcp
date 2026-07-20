"""Tests for core.runtime_log_tail — non-blocking emulator log tail (v1.1
backlog 1.6). Offline: OPTIX_EMULATOR_LOG_ROOT points the resolver at tmp."""
from __future__ import annotations

from pathlib import Path

import pytest

from service import core


@pytest.fixture()
def log_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "emulog"
    root.mkdir()
    monkeypatch.setenv("OPTIX_EMULATOR_LOG_ROOT", str(root))
    return root


def _write_log(root: Path, project: str, name: str, lines: list[str]) -> Path:
    d = root / project
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_tail_returns_last_n_lines(cfg: core.Config, log_root: Path) -> None:
    _write_log(log_root, "Alpha", "FTOptixRuntime.0.log",
               [f"line {i}" for i in range(50)])
    out = core.runtime_log_tail(cfg, "Alpha", lines=10)
    assert out["returned_lines"] == 10
    assert out["lines"][-1] == "line 49" and out["lines"][0] == "line 40"
    assert out["truncated"] is False and out["filtered"] is False


def test_tail_picks_newest_rotation_file(cfg: core.Config, log_root: Path) -> None:
    import os
    old = _write_log(log_root, "Alpha", "FTOptixRuntime.1.log", ["OLD"])
    cur = _write_log(log_root, "Alpha", "FTOptixRuntime.0.log", ["CURRENT"])
    past = cur.stat().st_mtime - 100
    os.utime(old, (past, past))
    out = core.runtime_log_tail(cfg, "Alpha")
    assert out["file"].endswith("FTOptixRuntime.0.log")
    assert out["lines"] == ["CURRENT"]


def test_tail_contains_filter_case_insensitive(cfg: core.Config, log_root: Path) -> None:
    _write_log(log_root, "Alpha", "FTOptixRuntime.0.log",
               ["INFO ok", "ERROR boom", "info fine", "Error again"])
    out = core.runtime_log_tail(cfg, "Alpha", contains="error")
    assert out["lines"] == ["ERROR boom", "Error again"]
    assert out["filtered"] is True


def test_tail_windows_large_file_and_drops_partial_line(cfg: core.Config, log_root: Path) -> None:
    _write_log(log_root, "Alpha", "FTOptixRuntime.0.log",
               [f"padline {i} " + "x" * 100 for i in range(200)])
    out = core.runtime_log_tail(cfg, "Alpha", lines=5, max_bytes=2048)
    assert out["truncated"] is True
    assert out["returned_lines"] == 5
    # the seek lands mid-line; the partial first line must have been dropped
    assert all(ln.startswith("padline") for ln in out["lines"])


def test_tail_no_log_dir(cfg: core.Config, log_root: Path) -> None:
    out = core.runtime_log_tail(cfg, "Ghost")
    assert out["error"] == "no_log_dir" and "optix_run_emulator" in out["hint"]


def test_tail_no_log_file(cfg: core.Config, log_root: Path) -> None:
    (log_root / "Alpha").mkdir()
    out = core.runtime_log_tail(cfg, "Alpha")
    assert out["error"] == "no_log_file"


def test_tail_does_not_hold_the_handle(cfg: core.Config, log_root: Path, monkeypatch) -> None:
    """The spec constraint: one brief open, closed before return."""
    _write_log(log_root, "Alpha", "FTOptixRuntime.0.log", ["a", "b"])
    opened = []
    real_open = open

    def spy_open(*a, **k):
        fh = real_open(*a, **k)
        opened.append(fh)
        return fh

    # module-scoped patch (never builtins.open — xdist hazard)
    monkeypatch.setattr(core, "open", spy_open, raising=False)
    core.runtime_log_tail(cfg, "Alpha")
    monkeypatch.undo()
    assert opened, "log was never opened"
    assert all(fh.closed for fh in opened)
