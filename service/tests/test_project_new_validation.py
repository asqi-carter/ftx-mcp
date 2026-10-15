"""#67: `new` validates the project name up front, ends the create wait as soon
as Studio logs "Error creating new project", and `open` reports an empty or
missing project file instead of the misleading upgrade_required."""
from __future__ import annotations

import sys
import types

import pytest

from service import core, studio_arm, studio_uia


@pytest.mark.parametrize("name", [
    "CI r18 sp", "18CI_r18", "Trailing.", "class", "namespace", "a-b", "x" * 200, "a b\u00fc", "ü.x",
])
def test_validator_rejects_bad_names(cfg: core.Config, monkeypatch, name: str) -> None:
    monkeypatch.setattr(core, "_studio_launch",
                        lambda *a: pytest.fail("Studio must not be launched"))
    out = core.project_new(cfg, name)
    assert out["ok"] is False and out["error"] == "invalid_project_name"
    assert out["name"] == name and out["rule"]


@pytest.mark.parametrize("name", ["CI_r18x", "_x", "Proj1", "Line4_HMI", "CI_r20_ü", "Проект_1"])
def test_validator_accepts_good_names(cfg: core.Config, name: str) -> None:
    assert core.validate_project_name(cfg, name) is None


def test_validator_rejects_overlong_path(cfg: core.Config) -> None:
    room = 180 - len(str(cfg.projects_root / "x")) + 1
    assert core.validate_project_name(cfg, "x" * (room + 1)) is not None
    assert core.validate_project_name(cfg, "x" * room) is None


def test_create_failed_is_detected_from_the_studio_log(
        cfg: core.Config, monkeypatch, tmp_path) -> None:
    logdir = tmp_path / "studiolog"
    logdir.mkdir()
    log = logdir / "FTOptixStudio.0.log"
    log.write_text("old line\n", encoding="utf-8")
    monkeypatch.setenv("OPTIX_STUDIO_LOG_DIR", str(logdir))

    def fake_launch(_cfg, _args):
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("12:00 ERROR Error creating new project: bad template\n")
        return {"ok": True, "pid": 7}

    monkeypatch.setattr(core, "_studio_launch", fake_launch)
    out = core.project_new(cfg, "Doomed", wait_seconds=60)
    assert out["ok"] is False and out["error"] == "create_failed"
    assert "IDEVersion.txt missing" in out["reason"]
    assert "Error creating new project" in out["detail"]


def test_stale_create_error_before_launch_is_ignored(
        cfg: core.Config, monkeypatch, tmp_path) -> None:
    logdir = tmp_path / "studiolog"
    logdir.mkdir()
    (logdir / "FTOptixStudio.0.log").write_text(
        "Error creating new project: old\n", encoding="utf-8")
    monkeypatch.setenv("OPTIX_STUDIO_LOG_DIR", str(logdir))
    monkeypatch.setattr(core, "_studio_launch", lambda *a: {"ok": True, "pid": 1})
    out = core.project_new(cfg, "Slow", wait_seconds=0.2)
    assert out["error"] == "create_timeout"


@pytest.mark.parametrize("mode", ["missing"])
def test_open_with_missing_or_empty_optix_is_project_file_missing(
        cfg: core.Config, monkeypatch, tmp_path, mode: str) -> None:
    d = cfg.projects_root / "Broken"
    d.mkdir(parents=True)
    f = d / "Broken.optix"
    f.write_text("junk", encoding="utf-8")  # an empty file is refused before launch
    (d / "IDEVersion.txt").write_text("1.8.0.555", encoding="utf-8")
    monkeypatch.setenv("OPTIX_STUDIO_LOG_DIR", str(tmp_path / "nolog"))

    def launch(_cfg, _args):
        if mode == "missing":
            f.unlink()
        return {"ok": True, "pid": 9}

    mod = types.ModuleType("uiautomation")
    mod.GetRootControl = lambda: types.SimpleNamespace(
        ControlTypeName="PaneControl", Name="Desktop", ProcessId=0,
        GetChildren=lambda: [])
    monkeypatch.setitem(sys.modules, "uiautomation", mod)
    monkeypatch.setattr(core, "_studio_launch", launch)
    monkeypatch.setattr(core, "_studio_pids_now", lambda: [])
    monkeypatch.setattr(core, "_project_loaded_identity", lambda *a, **k: None)
    monkeypatch.setattr(studio_arm, "home_screen_pids", lambda auto: set())
    monkeypatch.setattr(studio_uia, "find_upgrade_modal",
                        lambda auto: {"title": "Project Upgrade"})
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    out = core.project_open(cfg, "Broken", wait_seconds=5)
    assert out["ok"] is False and out["error"] == "project_file_missing"
    assert out["path"].endswith("Broken.optix")


def test_unicode_name_is_accepted_by_validate_project_name(cfg: core.Config) -> None:
    assert core.validate_project_name(cfg, "CI_r20_ü") is None
    assert "any script" in core.PROJECT_NAME_RULE


def test_long_name_over_path_limit_is_refused_by_length(cfg: core.Config) -> None:
    name = "L" * 64
    if len(str(cfg.projects_root / name)) <= core._PROJECT_PATH_MAX:
        cfg = core.Config(**{**cfg.__dict__,
                             "projects_root": cfg.projects_root / ("d" * 130)})
    assert core.validate_project_name(cfg, name) is not None


def _fake_log(monkeypatch, tmp_path):
    logdir = tmp_path / "studiolog"
    logdir.mkdir()
    log = logdir / "FTOptixStudio.0.log"
    log.write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("OPTIX_STUDIO_LOG_DIR", str(logdir))
    return log


def test_create_waits_grace_after_file_appears_and_catches_late_error(
        cfg: core.Config, monkeypatch, tmp_path) -> None:
    """The file exists before Studio logs the error: must not report created."""
    log = _fake_log(monkeypatch, tmp_path)
    dest = cfg.projects_root / "Late"
    clock = {"t": 1000.0}
    monkeypatch.setattr(core.time, "time", lambda: clock["t"])
    ticks = {"n": 0}

    def sleep(s):
        clock["t"] += s
        ticks["n"] += 1
        if ticks["n"] == 3:  # 3 s after the file appeared (< 5 s grace)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write("ERROR Error creating new project: path too long\n")

    def launch(_cfg, _args):
        dest.mkdir(parents=True)
        (dest / "Late.optix").write_text("data", encoding="utf-8")
        return {"ok": True, "pid": 3}

    monkeypatch.setattr(core.time, "sleep", sleep)
    monkeypatch.setattr(core, "_studio_launch", launch)
    out = core.project_new(cfg, "Late", wait_seconds=60)
    assert out["ok"] is False and out["error"] == "create_failed"
    assert out["path"] == str(dest) and out["partial_dir"] is True
    assert "Error creating new project" in out["detail"]
    assert (dest / "Late.optix").exists()  # nothing deleted


def test_create_reports_created_only_after_grace_window(
        cfg: core.Config, monkeypatch, tmp_path) -> None:
    _fake_log(monkeypatch, tmp_path)
    dest = cfg.projects_root / "Fine"
    clock = {"t": 1000.0}
    monkeypatch.setattr(core.time, "time", lambda: clock["t"])
    monkeypatch.setattr(core.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + s))

    def launch(_cfg, _args):
        dest.mkdir(parents=True)
        (dest / "Fine.optix").write_text("data", encoding="utf-8")
        return {"ok": True, "pid": 3}

    monkeypatch.setattr(core, "_studio_launch", launch)
    t0 = clock["t"]
    out = core.project_new(cfg, "Fine", wait_seconds=60)
    assert out["ok"] is True and out["state"] == "created"
    assert clock["t"] - t0 >= 5.0


def test_create_ignores_empty_optix_and_ends_early_on_loaded_line(
        cfg: core.Config, monkeypatch, tmp_path) -> None:
    log = _fake_log(monkeypatch, tmp_path)
    dest = cfg.projects_root / "Quick"
    clock = {"t": 1000.0}
    monkeypatch.setattr(core.time, "time", lambda: clock["t"])
    n = {"i": 0}

    def sleep(s):
        clock["t"] += s
        n["i"] += 1
        if n["i"] == 2:
            (dest / "Quick.optix").write_text("data", encoding="utf-8")
        if n["i"] == 3:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write("INFO Project loaded: Quick\n")

    def launch(_cfg, _args):
        dest.mkdir(parents=True)
        (dest / "Quick.optix").write_text("", encoding="utf-8")  # 0 bytes
        return {"ok": True, "pid": 3}

    monkeypatch.setattr(core.time, "sleep", sleep)
    monkeypatch.setattr(core, "_studio_launch", launch)
    t0 = clock["t"]
    out = core.project_new(cfg, "Quick", wait_seconds=60)
    assert out["state"] == "created" and clock["t"] - t0 < 5.0


def test_open_with_create_error_in_log_is_project_file_invalid(
        cfg: core.Config, monkeypatch, tmp_path) -> None:
    log = _fake_log(monkeypatch, tmp_path)
    d = cfg.projects_root / "Bad"
    d.mkdir(parents=True)
    (d / "Bad.optix").write_text("junk", encoding="utf-8")
    (d / "IDEVersion.txt").write_text("1.8.0.555", encoding="utf-8")

    def launch(_cfg, _args):
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("ERROR Error creating new project: boom\n")
        return {"ok": True, "pid": 9}

    mod = types.ModuleType("uiautomation")
    monkeypatch.setitem(sys.modules, "uiautomation", mod)
    monkeypatch.setattr(core, "_studio_launch", launch)
    monkeypatch.setattr(core, "_studio_pids_now", lambda: [])
    monkeypatch.setattr(core, "_project_loaded_identity", lambda *a, **k: None)
    monkeypatch.setattr(studio_uia, "find_upgrade_modal",
                        lambda auto: {"title": "Project Upgrade"})
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    out = core.project_open(cfg, "Bad", wait_seconds=5)
    assert out["ok"] is False and out["error"] == "project_file_invalid"
    assert out["path"].endswith("Bad.optix") and out["error"] != "upgrade_required"
