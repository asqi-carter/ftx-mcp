"""PRD 0056 E (#95, #100): missing-project lookups are bounded, `studio_version`
is cached, the upgrade-dialog poll is fast, re-arm probes the target's registry
port first, the firewall hint names coreservicehost.exe, and `upgrade_required`
/ `create_failed` name the Studio they leave open."""
from __future__ import annotations

import types

import pytest

from service import core, studio_arm
from service.tests.test_project_open_truth import (  # noqa: F401 -- fixture + helpers
    LAUNCH_PID,
    PROJECT_VERSION_17,
    UPGRADE_DIALOG,
    _home,
    world,
)

_REAL_STUDIO_VERSION = core.studio_version  # `world` replaces it with a stub


@pytest.fixture()
def enum_counts(monkeypatch):
    """Count Studio / registry enumerations; both report one unrelated Studio."""
    counts = {"studio": 0, "registry": 0}

    def studio(force: bool = False):
        counts["studio"] += 1
        return []

    def registry():
        counts["registry"] += 1
        return []

    monkeypatch.setattr(core, "_studio_open_candidates", studio)
    monkeypatch.setattr(core, "_registry_open_candidates", registry)
    monkeypatch.setattr(core, "_require_interactive_session", lambda: None)
    return counts


# ---- 1. bounded fallback ----------------------------------------------------


@pytest.mark.real_project_resolution
def test_run_restart_status_describe_skip_the_studio_fallback(cfg, enum_counts) -> None:
    for out in (core.run_emulator(cfg, "Nope"),
                core.restart_emulator(cfg, "Nope"),
                core.emulator_status(cfg, project="Nope")):
        assert out["error"] == "project_not_found"
    assert enum_counts == {"studio": 0, "registry": 0}


@pytest.mark.real_project_resolution
def test_describe_on_a_missing_project_is_project_not_found_without_a_scan(
        cfg, enum_counts, monkeypatch) -> None:
    def no_bridge(_cfg, project):
        raise core.NoBridgeForProject("none", project=project)
    monkeypatch.setattr(core, "_require_bridge_for", no_bridge)
    assert core.describe_node(cfg, "Nope", "UI")["error"] == "project_not_found"
    assert core.describe_type(cfg, "Nope", "Label")["error"] == "project_not_found"
    assert enum_counts == {"studio": 0, "registry": 0}


def test_describe_keeps_the_bridge_error_for_a_project_that_exists(
        cfg, projects_root, enum_counts, monkeypatch) -> None:
    (projects_root / "Real").mkdir()
    (projects_root / "Real" / "Real.optix").write_text("x")

    def no_bridge(_cfg, project):
        raise core.NoBridgeForProject("none", project=project)
    monkeypatch.setattr(core, "_require_bridge_for", no_bridge)
    with pytest.raises(core.NoBridgeForProject):
        core.describe_node(cfg, "Real", "UI")


def test_open_and_save_keep_the_fallback_with_one_enumeration_per_call(
        cfg, enum_counts) -> None:
    for fn in (lambda: core.project_open(cfg, "Nope"),
               lambda: core.save(cfg, "Nope")):
        before = dict(enum_counts)
        try:
            fn()
        except core.ProjectNotFound:
            pass
        assert enum_counts["studio"] - before["studio"] <= 1
        assert enum_counts["registry"] - before["registry"] <= 1
    assert enum_counts["studio"] >= 1, "open/save still consult Studio"


def test_one_scope_shares_a_single_enumeration_across_lookups(cfg, enum_counts) -> None:
    with core._resolve_scope(True):
        for _ in range(4):
            with pytest.raises(core.ProjectNotFound):
                core.resolve_project(cfg, "Nope")
    assert enum_counts == {"studio": 1, "registry": 1}


def test_outside_a_scope_nothing_is_cached(cfg, enum_counts) -> None:
    for _ in range(2):
        with pytest.raises(core.ProjectNotFound):
            core.resolve_project(cfg, "Nope")
    assert enum_counts["studio"] == 2


def test_no_fallback_scope_still_resolves_projects_under_root(
        cfg, projects_root, enum_counts) -> None:
    (projects_root / "Real").mkdir()
    (projects_root / "Real" / "Real.optix").write_text("x")
    with core._resolve_scope(False):
        assert core.resolve_project(cfg, "Real") == (projects_root / "Real").resolve()
    assert enum_counts == {"studio": 0, "registry": 0}


# ---- 2. studio_version cache ------------------------------------------------


class _CountingRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, cmd, timeout=None):
        self.calls += 1
        return types.SimpleNamespace(returncode=0, stdout="1.8.0.555\n", stderr="")


def test_studio_version_is_cached_per_exe_path_and_mtime(cfg) -> None:
    runner = _CountingRunner()
    first = _REAL_STUDIO_VERSION(cfg, runner)
    second = _REAL_STUDIO_VERSION(cfg, runner)
    assert runner.calls == 1 and first == second and first["stdout"] == "1.8.0.555"
    # A different mtime (Studio upgraded in place) is a different key.
    st = cfg.studio_exe.stat()
    import os
    os.utime(cfg.studio_exe, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    _REAL_STUDIO_VERSION(cfg, runner)
    assert runner.calls == 2


def test_studio_version_does_not_cache_a_failure(cfg) -> None:
    class Failing(_CountingRunner):
        def run(self, cmd, timeout=None):
            self.calls += 1
            return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
    runner = Failing()
    _REAL_STUDIO_VERSION(cfg, runner)
    _REAL_STUDIO_VERSION(cfg, runner)
    assert runner.calls == 2


def test_second_open_of_the_same_project_spawns_no_version_process(
        cfg, world, monkeypatch) -> None:
    monkeypatch.setattr(core, "studio_version", _REAL_STUDIO_VERSION)
    runner = _CountingRunner()
    for _ in range(2):
        out = core.project_open(cfg, "Demo", wait_seconds=0.05, runner=runner)
        assert out["error"] == "open_timeout"
    assert runner.calls == 1


# ---- 3. poll interval -------------------------------------------------------


def test_open_poll_interval_is_at_most_half_a_second(cfg, world, monkeypatch) -> None:
    assert core._OPEN_POLL_S <= 0.5
    sleeps: list[float] = []
    monkeypatch.setattr(core.time, "sleep", lambda s: sleeps.append(s))
    core.project_open(cfg, "Demo", wait_seconds=0.05)
    assert sleeps and max(sleeps) <= 0.5


# ---- 4. re-arm probes the target's registry port first ----------------------


def _health(project: str) -> dict:
    return {"project": project, "project_name": project, "available": True}


def test_rearm_probes_the_target_registry_port_first(monkeypatch) -> None:
    probed: list[int] = []
    healths = {8768: _health("A"), 8769: _health("B"), 8770: _health("Target"),
               8771: _health("C")}

    def fake(port, timeout=0.5):
        probed.append(port)
        return healths.get(port)

    monkeypatch.setattr(studio_arm, "_bridge_health", fake)
    monkeypatch.setattr(studio_arm, "studio_pid_for", lambda *a, **k: None)
    out = studio_arm.execute_method(
        "Target", "C:/p/Target", base_port=8768, port_range=4, preferred_port=8770)
    assert out["state"] == "already_armed" and out["port"] == 8770
    assert probed == [8770], "other armed Studios are never probed"


def test_serving_port_for_records_each_probe_once(monkeypatch) -> None:
    calls: list[int] = []

    def fake(port, timeout=0.5):
        calls.append(port)
        return _health("Other") if port != 8769 else None

    monkeypatch.setattr(studio_arm, "_bridge_health", fake)
    cache: dict = {}
    assert studio_arm.serving_port_for("Target", 8768, 4, preferred=8770,
                                       health_cache=cache) is None
    assert calls[0] == 8770 and sorted(calls) == [8768, 8769, 8770, 8771]
    assert set(cache) == {8768, 8769, 8770, 8771}


def test_arm_with_a_full_range_probes_each_port_once(monkeypatch) -> None:
    calls: list[int] = []

    def fake(port, timeout=0.5):
        calls.append(port)
        return _health(f"Other{port}")

    monkeypatch.setattr(studio_arm, "_bridge_health", fake)
    monkeypatch.setattr(studio_arm, "studio_pid_for", lambda *a, **k: None)
    out = studio_arm.execute_method(
        "Target", "C:/p/Target", base_port=8768, port_range=4, preferred_port=8770)
    assert out["error"] == "bridge_port_range_full"
    assert sorted(calls) == [8768, 8769, 8770, 8771], "no second scan of the range"


# ---- 5. firewall hint -------------------------------------------------------


def test_firewall_hint_names_coreservicehost() -> None:
    hint = studio_arm.os_dialog_hint({"kind": studio_arm.FIREWALL_PROMPT_KIND})
    assert "coreservicehost.exe" in hint
    assert "DataAcquisitionCore" not in hint


# ---- 6. studio_left_open ----------------------------------------------------


def test_upgrade_required_carries_studio_left_open(cfg, world, monkeypatch) -> None:
    (world.dir / "IDEVersion.txt").write_text(PROJECT_VERSION_17, encoding="utf-8")
    world.on_launch = lambda: world.windows.append(_home(LAUNCH_PID, "Demo", world.dir))
    monkeypatch.setattr(core.studio_uia, "find_upgrade_modal",
                        lambda auto: dict(UPGRADE_DIALOG))
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["error"] == "upgrade_required"
    assert out["studio_left_open"] == {"pid": LAUNCH_PID}


def test_create_failed_carries_studio_left_open(cfg, monkeypatch, tmp_path) -> None:
    logdir = tmp_path / "studiolog"
    logdir.mkdir()
    log = logdir / "FTOptixStudio.0.log"
    log.write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("OPTIX_STUDIO_LOG_DIR", str(logdir))

    def fake_launch(_cfg, _args):
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("12:00 ERROR Error creating new project: bad template\n")
        return {"ok": True, "pid": 77}

    monkeypatch.setattr(core, "_studio_launch", fake_launch)
    monkeypatch.setattr(core, "_require_interactive_session", lambda: None)
    out = core.project_new(cfg, "Doomed", wait_seconds=30)
    assert out["error"] == "create_failed"
    assert out["studio_left_open"] == {"pid": 77}
