"""#25: `open` reports a project as open only when a Studio really has it
loaded (bridge health identity, window title / tree, Studio log line) and never
because the Home screen lists it under Recent. A refused / Home-stuck second
Studio is closed and reported as not_opened with the holder's pid; a project
already open launches nothing."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from service import core, studio_arm

HOLDER_PID = 4000
LAUNCH_PID = 5000
STUDIO_VERSION = "1.8.0.555"
# The Studio 1.7.4.32 project opened in 1.8 (r24, func/raw-r24/a-open-v17in18*).
PROJECT_VERSION_17 = "1.7.4.32"
# Title/class of the dialog Studio 1.8 shows over its Home screen for such a project.
UPGRADE_DIALOG = {"title": "Project Upgrade", "class": "#32770", "text": ""}


class Ctrl:
    def __init__(self, ctype: str, name: str, children=None, pid: int = 0) -> None:
        self.ControlTypeName, self.Name, self.ProcessId = ctype, name, pid
        self._c = children or []

    def GetChildren(self):  # noqa: N802 -- mirrors uiautomation
        return self._c


def _home(pid: int, project: str, project_dir: Path) -> Ctrl:
    """Studio Home page: Recent-projects entries naming the project + its path."""
    return Ctrl("WindowControl", "FactoryTalk Optix Studio", [
        Ctrl("TextControl", "Recent projects"),
        Ctrl("TextControl", project),
        Ctrl("TextControl", str(project_dir)),
    ], pid)


def _loaded(pid: int, project: str, title: str | None = None) -> Ctrl:
    return Ctrl("WindowControl", title or "FactoryTalk Optix Studio", [
        Ctrl("TextControl", project),
    ], pid)


@pytest.fixture()
def world(cfg: core.Config, monkeypatch, tmp_path):
    """Fake desktop + process table + launcher. Tests mutate `w`."""
    d = cfg.projects_root / "Demo"
    d.mkdir(parents=True)
    (d / "Demo.optix").write_text("Name: Demo\n", encoding="utf-8")
    (d / "IDEVersion.txt").write_text(STUDIO_VERSION, encoding="utf-8")
    monkeypatch.setattr(core, "studio_version",
                        lambda *a, **k: {"ok": True, "stdout": STUDIO_VERSION})
    monkeypatch.setenv("OPTIX_STUDIO_LOG_DIR", str(tmp_path / "log"))
    (tmp_path / "log").mkdir()
    w = types.SimpleNamespace(
        dir=d, windows=[], pids=[], launches=[], closed=[], health={},
        on_launch=None, log=tmp_path / "log" / "FTOptixStudio.0.log")
    mod = types.ModuleType("uiautomation")
    mod.GetRootControl = lambda: Ctrl("PaneControl", "Desktop", w.windows)
    monkeypatch.setitem(sys.modules, "uiautomation", mod)
    monkeypatch.setattr(core, "_studio_pids_now", lambda: list(w.pids))
    monkeypatch.setattr(core, "_studio_pid_for_project", lambda *a, **k: 0)
    monkeypatch.setattr(studio_arm, "_bridge_health",
                        lambda port, timeout=3.0: w.health.get(port))

    def launch(_cfg, args):
        w.launches.append(args)
        w.pids.append(LAUNCH_PID)
        if w.on_launch:
            w.on_launch()
        return {"ok": True, "pid": LAUNCH_PID}

    def close(pid):
        w.closed.append(pid)
        w.pids[:] = [p for p in w.pids if p != pid]
        w.windows[:] = [x for x in w.windows if x.ProcessId != pid]
        return True

    monkeypatch.setattr(core, "_studio_launch", launch)
    monkeypatch.setattr(core, "_close_launched_studio", close)
    monkeypatch.setattr(core, "_OPEN_HOME_GRACE_S", 0.0)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    return w


def test_home_screen_with_recent_entries_is_not_open(cfg, world) -> None:
    """A lone Studio on Home listing the project under Recent: never opened /
    already_open. The launched Studio also stays on Home -> not_opened."""
    world.pids.append(HOLDER_PID)
    world.windows.append(_home(HOLDER_PID, "Demo", world.dir))
    world.on_launch = lambda: world.windows.append(_home(LAUNCH_PID, "Demo", world.dir))
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["state"] == "not_opened" and out["ok"] is False
    assert out["state"] not in ("opened", "already_open")
    assert world.launches, "Home is not a loaded project, so a launch is expected"
    assert world.closed == [LAUNCH_PID]
    assert out["reason"]


def test_home_screen_is_not_a_window_identity() -> None:
    home = Ctrl("WindowControl", "FactoryTalk Optix Studio", [
        Ctrl("TextControl", "Recent projects"), Ctrl("TextControl", "Demo")])
    root = Ctrl("PaneControl", "Desktop", [home])
    auto = types.SimpleNamespace(GetRootControl=lambda: root)
    assert studio_arm._studio_window_for(auto, "Demo") is None
    loaded = Ctrl("WindowControl", "FactoryTalk Optix Studio", [Ctrl("TextControl", "Demo")])
    auto2 = types.SimpleNamespace(GetRootControl=lambda: Ctrl("P", "Desktop", [loaded]))
    assert studio_arm._studio_window_for(auto2, "Demo") is loaded


def test_refused_second_launch_reports_not_opened_with_holder_pid(cfg, world) -> None:
    """Project loaded in Studio A (no bridge). A is not recognisable on the
    desktop, the second launch is refused in the log and sits on Home."""
    world.pids.append(HOLDER_PID)
    world.windows.append(_loaded(HOLDER_PID, "SomethingElse"))

    def on_launch():
        world.windows.append(_home(LAUNCH_PID, "Demo", world.dir))
        world.log.write_text(
            "INFO Opening project\nERROR Cannot open: same project is open in another "
            "instance of Studio\n", encoding="utf-8")

    world.on_launch = on_launch
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["ok"] is False and out["state"] == "not_opened"
    assert out["reason"] and "refused" in out["reason"]
    assert out["holder_pid"] == HOLDER_PID
    assert out["launched_pid"] == LAUNCH_PID
    assert world.closed == [LAUNCH_PID] and out["closed_launched_studio"] is True
    assert HOLDER_PID in world.pids, "a Studio the service did not launch is never closed"


def test_project_already_open_in_window_returns_it_and_launches_nothing(cfg, world) -> None:
    world.pids.append(HOLDER_PID)
    world.windows.append(_loaded(HOLDER_PID, "Demo"))
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["ok"] is True and out["state"] == "already_open"
    assert out["pid"] == HOLDER_PID and out["source"] == "window_title"
    assert world.launches == [] and world.closed == []


def test_project_already_open_by_bridge_health_launches_nothing(cfg, world) -> None:
    world.pids.append(HOLDER_PID)
    world.windows.append(_home(HOLDER_PID, "Demo", world.dir))  # window says nothing
    world.health[cfg.bridge_port_base] = {
        "project": "Demo", "project_dir": str(world.dir), "studio_pid": HOLDER_PID}
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["state"] == "already_open" and out["source"] == "bridge_health"
    assert out["pid"] == HOLDER_PID
    assert world.launches == []


def test_bridge_for_a_different_dir_does_not_count(cfg, world) -> None:
    world.health[cfg.bridge_port_base] = {
        "project": "Demo", "project_dir": r"C:\elsewhere\Demo", "studio_pid": HOLDER_PID}
    world.pids.append(HOLDER_PID)
    world.on_launch = lambda: world.windows.append(_loaded(LAUNCH_PID, "Demo"))
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["state"] == "opened" and out["pid"] == LAUNCH_PID


def test_project_already_open_by_log_line_launches_nothing(cfg, world, monkeypatch) -> None:
    world.pids.append(HOLDER_PID)
    monkeypatch.setattr(core, "_studio_pid_for_project", lambda *a, **k: HOLDER_PID)
    world.log.write_text("INFO Project loaded: Demo\n", encoding="utf-8")
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["state"] == "already_open" and out["source"] == "studio_log"
    assert out["pid"] == HOLDER_PID and world.launches == []


def test_stale_log_for_a_while_pid_shows_b_proceeds_with_open(cfg, world) -> None:
    """Two mocked Studios: the log still says Demo (A) loaded, but the only
    live Studio (pid 4000) shows project B and its command line names B. The
    log match is rejected and the open proceeds (#46)."""
    other = cfg.projects_root / "Other"
    other.mkdir()
    world.pids.append(HOLDER_PID)
    world.windows.append(_loaded(HOLDER_PID, "Other", title="Other - FactoryTalk Optix Studio"))
    world.log.write_text("INFO Project loaded: Demo\n", encoding="utf-8")
    world.on_launch = lambda: world.windows.append(_loaded(LAUNCH_PID, "Demo"))
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert len(world.launches) == 1, "stale log for A must not block the open"
    assert out["state"] != "already_open"
    assert out["state"] == "opened" and out["pid"] == LAUNCH_PID


def test_log_match_accepted_when_pid_window_names_this_project(cfg, world, monkeypatch) -> None:
    """Two Studios (A=Demo by command line, B=Other by window): the log match
    is attributed to the pid that names Demo, not to B."""
    world.pids.extend([HOLDER_PID, 4001])
    world.windows.append(_loaded(4001, "Other", title="Other - FactoryTalk Optix Studio"))
    monkeypatch.setattr(core, "_studio_pid_for_project", lambda *a, **k: HOLDER_PID)
    world.log.write_text("INFO Project loaded: Demo\n", encoding="utf-8")
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["state"] == "already_open" and out["source"] == "studio_log"
    assert out["pid"] == HOLDER_PID and world.launches == []


def test_pids_naming_project_uses_live_window_title(cfg, world) -> None:
    world.windows.append(_loaded(4001, "x", title="Demo - FactoryTalk Optix Studio"))
    world.windows.append(_loaded(4002, "x", title="Other - FactoryTalk Optix Studio"))
    got = core._pids_naming_project("Demo", world.dir, {"Demo"}, [4001, 4002])
    assert got == [4001]


def test_stale_log_without_a_running_studio_proves_nothing(cfg, world) -> None:
    world.log.write_text("INFO Project loaded: Demo\n", encoding="utf-8")
    world.on_launch = lambda: world.windows.append(_loaded(LAUNCH_PID, "Demo"))
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["state"] == "opened" and len(world.launches) == 1


def test_successful_launch_reports_opened(cfg, world) -> None:
    world.on_launch = lambda: world.windows.append(
        _loaded(LAUNCH_PID, "x", title="Demo - FactoryTalk Optix Studio"))
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["ok"] is True and out["state"] == "opened"
    assert out["pid"] == LAUNCH_PID and out["source"] == "window_title"
    assert world.closed == []


def test_window_title_names_project() -> None:
    f = studio_arm.window_title_names_project
    assert f("Demo - FactoryTalk Optix Studio", "Demo")
    assert f("FactoryTalk Optix Studio - Demo*", "Demo")
    assert not f("FactoryTalk Optix Studio", "Demo")
    assert not f("Other - FactoryTalk Optix Studio", "Demo")


def test_close_launched_studio_never_touches_non_studio(monkeypatch) -> None:
    killed: list[int] = []

    class P:
        def __init__(self, pid): self.pid = pid
        def name(self): return "notepad.exe"
        def terminate(self): killed.append(self.pid)
        def kill(self): killed.append(self.pid)
        def children(self, recursive=False): return []

    monkeypatch.setattr(core.psutil, "Process", P)
    assert core._close_launched_studio(123) is False
    assert killed == []


def test_home_stuck_reason_no_studio_running_never_says_newer_studio(tmp_path) -> None:
    """No other Studio running: don't blame 'another Studio' and never claim the
    project needs a 'newer Studio' (that comes from the files, before launch)."""
    f = tmp_path / "Demo.optix"
    f.write_text("Name: Demo\nVersion: 1.9.0.12\n", encoding="utf-8")
    reason = core._home_stuck_reason(f, other_studio_running=False)
    assert "most likely open in another Studio" not in reason
    assert "newer" not in reason.lower() and "1.9.0.12" in reason


def test_home_stuck_reason_other_studio_running_keeps_attribution(tmp_path) -> None:
    f = tmp_path / "Demo.optix"
    f.write_text("", encoding="utf-8")
    reason = core._home_stuck_reason(f, other_studio_running=True)
    assert "most likely open in another Studio" in reason
    assert "newer" not in reason.lower()


def test_home_stuck_reason_unreadable_version(tmp_path) -> None:
    reason = core._home_stuck_reason(tmp_path / "missing.optix", False)
    assert "newer" not in reason.lower() and "Home screen" in reason


# --- #63/#66: same-named projects are told apart by directory ---------------

def _sub_copy(world, cfg) -> Path:
    """A second project of the same name nested under projects_root."""
    d = cfg.projects_root / "Site" / "Demo"
    d.mkdir(parents=True)
    (d / "Demo.optix").write_text("Name: Demo\n", encoding="utf-8")
    (d / "IDEVersion.txt").write_text(STUDIO_VERSION, encoding="utf-8")
    return d


def test_root_copy_open_sub_path_requested_without_proof_is_ambiguous(cfg, world) -> None:
    sub = _sub_copy(world, cfg)
    world.pids.append(HOLDER_PID)
    world.windows.append(_loaded(HOLDER_PID, "Demo", title="Demo - FactoryTalk Optix Studio"))
    out = core.project_name_ambiguity(cfg, "Site/Demo")
    assert out is not None and out["ok"] is False
    assert out["error"] == "ambiguous_project_name"
    dirs = {c["dir"] for c in out["candidates"]}
    assert dirs == {str(sub.resolve()), str(world.dir.resolve())}
    assert all(c["pid"] is None for c in out["candidates"])
    assert out["hint"]


def test_ambiguous_title_is_not_already_open_and_open_launches_full_path(cfg, world) -> None:
    sub = _sub_copy(world, cfg)
    world.pids.append(HOLDER_PID)
    world.windows.append(_loaded(HOLDER_PID, "Demo", title="Demo - FactoryTalk Optix Studio"))
    world.on_launch = lambda: world.windows.append(
        _loaded(LAUNCH_PID, "x", title="Demo - FactoryTalk Optix Studio"))
    out = core.project_open(cfg, "Site/Demo", wait_seconds=5)
    assert out["ok"] is True and out["state"] == "opened"
    assert out["pid"] == LAUNCH_PID and out["launched"] is True
    assert world.launches == [["open", str((sub / "Demo.optix")), "--silent"]]
    # the launch record is now the proof: no ambiguity for the launched copy
    assert core.project_name_ambiguity(cfg, "Site/Demo") is None


def test_unique_name_window_title_still_counts(cfg, world) -> None:
    world.pids.append(HOLDER_PID)
    world.windows.append(_loaded(HOLDER_PID, "Demo", title="Demo - FactoryTalk Optix Studio"))
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["state"] == "already_open" and out["pid"] == HOLDER_PID
    assert core.project_name_ambiguity(cfg, "Demo") is None


def test_title_carrying_the_dir_is_proof_even_when_ambiguous(cfg, world) -> None:
    sub = _sub_copy(world, cfg)
    world.pids.append(HOLDER_PID)
    world.windows.append(_loaded(HOLDER_PID, "Demo", title=f"{sub} - FactoryTalk Optix Studio"))
    out = core.project_open(cfg, "Site/Demo", wait_seconds=5)
    assert out["state"] == "already_open" and out["pid"] == HOLDER_PID


# --- #63: every bridge lookup passes the resolved dir; registry keyed by dir -

def test_serving_port_for_dirless_health_unproven_on_name_collision(world) -> None:
    world.health[8768] = {"project": "Demo", "studio_pid": 7}
    assert studio_arm.serving_port_for("Demo", project_dir="C:/p/Demo") == 8768
    assert studio_arm.serving_port_for(
        "Demo", project_dir="C:/p/Demo", name_collision=True) is None
    world.health[8769] = {"project": "Demo", "studio_pid": 8, "project_dir": "C:\\p\\Demo"}
    assert studio_arm.serving_port_for(
        "Demo", project_dir="C:/p/Demo", name_collision=True) == 8769


def test_serving_port_for_ignores_other_dir_bridge_before_ours(world) -> None:
    world.health[8768] = {"project": "Demo", "project_dir": "C:\\p\\Site\\Demo"}
    world.health[8769] = {"project": "Demo", "project_dir": "C:\\p\\Demo"}
    assert studio_arm.serving_port_for(
        "Demo", project_dir="C:/p/Demo", preferred=8768) == 8769


def _write_registry(monkeypatch, tmp_path, entries) -> None:
    import json
    reg = tmp_path / "ftx-mcp" / "bridges"
    reg.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    for port, data in entries.items():
        (reg / f"{port}.json").write_text(json.dumps(data), encoding="utf-8")


def test_registry_hit_for_other_directory_is_ignored_and_reported(cfg, world, monkeypatch, tmp_path) -> None:
    sub = _sub_copy(world, cfg)
    _write_registry(monkeypatch, tmp_path / "la", {
        8768: {"project": "Demo", "project_path": str(sub)}})
    ignored: list = []
    assert core._registry_port_for("Demo", world.dir, {"demo"}, ignored=ignored) is None
    assert [(i["port"], i["dir"]) for i in ignored] == [(8768, str(sub.resolve()))] \
        or [(i["port"], Path(i["dir"]).resolve()) for i in ignored] == [(8768, sub.resolve())]
    # the sub directory's own lookup does take it
    assert core._registry_port_for("Demo", sub, {"demo"}) == 8768


def test_registry_directory_match_beats_earlier_same_name_entry(cfg, world, monkeypatch, tmp_path) -> None:
    sub = _sub_copy(world, cfg)
    _write_registry(monkeypatch, tmp_path / "la", {
        8768: {"project": "Demo", "project_path": str(sub)},
        8769: {"project": "Demo", "project_path": str(world.dir)}})
    assert core._registry_port_for("Demo", world.dir, {"demo"}) == 8769


def test_bridge_arm_passes_resolved_dir_and_collision_to_execute_method(cfg, world, monkeypatch, tmp_path) -> None:
    sub = _sub_copy(world, cfg)
    _write_registry(monkeypatch, tmp_path / "la", {
        8768: {"project": "Demo", "project_path": str(sub)}})
    seen: dict = {}

    def fake_exec(project, project_dir, **kw):
        seen.update(project_dir=project_dir, **kw)
        return {"ok": True, "state": "armed", "project": project}

    monkeypatch.setattr(studio_arm, "execute_method", fake_exec)
    monkeypatch.setattr(core, "_sync_bridge_source", lambda d: {})
    out = core.bridge_arm(cfg, "Demo")
    assert seen["project_dir"] == str(world.dir.resolve())
    assert seen["name_collision"] is True
    assert seen["preferred_port"] is None
    assert [c["port"] for c in out["candidates"]] == [8768]


# --- #66 (PRD 0053): unloadable projects are detected fast and reported truthfully

# SECONDARY signal only: Studio 1.8 logs no version line for the Project Upgrade
# dialog (r24); the wording is a guess kept in sync with core._VERSION_MISMATCH_RE.
VERSION_MISMATCH_LINE = (
    "ERROR The project was created with an older version of FactoryTalk Optix Studio")


def _log_on_launch(world, line: str) -> None:
    def on_launch():
        with open(world.log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    world.on_launch = on_launch


def test_empty_optix_is_project_file_invalid_without_launching(cfg, world) -> None:
    (world.dir / "Demo.optix").write_text("", encoding="utf-8")
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["ok"] is False and out["error"] == "project_file_invalid"
    assert out["path"].endswith("Demo.optix")
    assert world.launches == []


def test_missing_optix_is_project_file_missing_without_launching(cfg, world) -> None:
    (world.dir / "Demo.optix").unlink()
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["ok"] is False and out["error"] == "project_file_missing"
    assert out["path"].endswith("Demo.optix")
    assert world.launches == []


@pytest.mark.parametrize("line", [
    r"ERROR YAML file C:\p\Demo\Demo.optix is empty",
    "ERROR Error creating new project: boom",
    r"ERROR Cannot open project C:\p\Demo\Demo.optix",
    r"ERROR Project failed to load: C:\p\Demo\Demo.optix",
    "ERROR YamlDotNet.Core.SyntaxErrorException: (Line: 3, Col: 1) while parsing a block mapping",
    "ERROR YAML parse error at line 3: mapping values are not allowed here",
], ids=["is_empty", "error_creating", "cannot_open", "failed_to_load",
        "yaml_syntax_exception", "yaml_parse_error"])
def test_log_load_failure_line_is_project_file_invalid_fast(cfg, world, line) -> None:
    _log_on_launch(world, line)
    out = core.project_open(cfg, "Demo", wait_seconds=60)
    assert out["ok"] is False and out["error"] == "project_file_invalid"
    assert line.split("ERROR ", 1)[1] in out["detail"]
    assert out["pid"] == LAUNCH_PID and out["studio_left_open"] == {"pid": LAUNCH_PID}
    assert "close" in out["hint"].lower()
    assert out["path"].endswith("Demo.optix")
    assert world.closed == []  # the launched Studio is left as it is


def test_version_mismatch_line_with_upgrade_modal_is_upgrade_required(
        cfg, world, monkeypatch) -> None:
    """The log line stays an additional signal (it adds `detail`)."""
    _log_on_launch(world, VERSION_MISMATCH_LINE)
    monkeypatch.setattr(core.studio_uia, "find_upgrade_modal",
                        lambda auto: {"title": "Project Upgrade", "text": ""})
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["ok"] is False and out["error"] == "upgrade_required"
    assert "older version" in out["detail"]


def test_old_project_with_upgrade_dialog_is_upgrade_required_in_one_poll(
        cfg, world, monkeypatch) -> None:
    """A 1.7 project in 1.8: Studio logs nothing about a version, shows the
    Project Upgrade dialog over the Home screen. Files say older -> one poll."""
    (world.dir / "IDEVersion.txt").write_text(PROJECT_VERSION_17, encoding="utf-8")
    world.on_launch = lambda: world.windows.append(_home(LAUNCH_PID, "Demo", world.dir))
    monkeypatch.setattr(core.studio_uia, "find_upgrade_modal",
                        lambda auto: dict(UPGRADE_DIALOG))
    monkeypatch.setattr(core, "_OPEN_HOME_GRACE_S", 0.0)  # Home would win if checked first
    sleeps: list[float] = []
    monkeypatch.setattr(core.time, "sleep", lambda s: sleeps.append(s))
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["ok"] is False and out["error"] == "upgrade_required"
    assert out["project_version"] == PROJECT_VERSION_17
    assert out["studio_version"] == STUDIO_VERSION
    assert out["dialog"]["title"] == "Project Upgrade" and out["dialog"]["class"] == "#32770"
    assert "one-way" in out["hint"] and "NOT clicked" in out["hint"]
    assert sleeps == [], "returned on the first poll"
    assert world.closed == [], "the dialog is never clicked and Studio is left for the operator"


def test_upgrade_required_checks_dialog_before_home_screen(cfg, world, monkeypatch) -> None:
    (world.dir / "IDEVersion.txt").write_text(PROJECT_VERSION_17, encoding="utf-8")
    world.on_launch = lambda: world.windows.append(_home(LAUNCH_PID, "Demo", world.dir))
    monkeypatch.setattr(core.studio_uia, "find_upgrade_modal",
                        lambda auto: dict(UPGRADE_DIALOG))
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["error"] == "upgrade_required"
    assert "state" not in out


def test_project_newer_than_studio_is_studio_too_old_without_launching(
        cfg, world) -> None:
    (world.dir / "IDEVersion.txt").write_text("1.9.2.7\n", encoding="utf-8")
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["ok"] is False and out["error"] == "studio_too_old"
    assert out["project_version"] == "1.9.2.7"
    assert out["studio_version"] == STUDIO_VERSION
    assert "FTOPTIX_STUDIO_EXE" in out["hint"]
    assert world.launches == []


@pytest.mark.parametrize("content", [None, "", "  \r\n"], ids=["missing", "empty", "blank"])
def test_missing_ide_version_is_project_file_invalid_without_launching(
        cfg, world, content) -> None:
    """The half-created project: .optix exists, IDEVersion.txt does not."""
    f = world.dir / "IDEVersion.txt"
    if content is None:
        f.unlink()
    else:
        f.write_text(content, encoding="utf-8")
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["ok"] is False and out["error"] == "project_file_invalid"
    assert out["detail"] == "IDEVersion.txt missing: the project was never fully created"
    assert world.launches == []


def test_optix_header_version_is_used_when_ide_version_has_none(cfg, world) -> None:
    (world.dir / "IDEVersion.txt").write_text("unknown", encoding="utf-8")
    (world.dir / "Demo.optix").write_text("Name: Demo\nVersion: 2.0.0.1\n", encoding="utf-8")
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["error"] == "studio_too_old" and out["project_version"] == "2.0.0.1"


def test_upgrade_modal_without_version_signal_is_not_upgrade_required(
        cfg, world, monkeypatch) -> None:
    """Project files not older, no log line: a dialog alone proves nothing."""
    monkeypatch.setattr(core.studio_uia, "find_upgrade_modal",
                        lambda auto: {"title": "Project Upgrade", "text": ""})
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out.get("error") != "upgrade_required"
    assert out["state"] == "not_opened"


def test_home_screen_not_opened_reason_never_says_newer_studio(
        cfg, world) -> None:
    world.on_launch = lambda: world.windows.append(_home(LAUNCH_PID, "Demo", world.dir))
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["state"] == "not_opened"
    assert "newer studio" not in out["reason"].lower()


def test_not_opened_carries_only_the_first_log_error_line(cfg, world, monkeypatch) -> None:
    monkeypatch.setattr(core, "_find_project_holder",
                        lambda *a, **k: {"pid": None, "source": None})
    world.on_launch = lambda: (
        world.windows.append(_home(LAUNCH_PID, "Demo", world.dir)),
        open(world.log, "a", encoding="utf-8").write(
            "ERROR first thing\nERROR second thing\n"))
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["state"] == "not_opened"
    assert "first thing" in out["detail"] and "second thing" not in out["detail"]
    assert "second thing" not in out["reason"]


def test_not_opened_never_blames_another_studio_without_a_matched_pid(
        cfg, world, monkeypatch) -> None:
    world.pids.append(HOLDER_PID)  # a Studio runs, but nothing ties it to Demo
    monkeypatch.setattr(core, "_find_project_holder",
                        lambda *a, **k: {"pid": None, "source": None})
    world.on_launch = lambda: world.windows.append(_home(LAUNCH_PID, "Demo", world.dir))
    _log_on_launch(world, "ERROR something odd happened")
    prev = world.on_launch

    def both():
        prev()
        world.windows.append(_home(LAUNCH_PID, "Demo", world.dir))
    world.on_launch = both
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["state"] == "not_opened"
    assert "open in another Studio" not in out["reason"]
    assert "something odd happened" in out["detail"]


def test_refusal_line_without_matched_pid_does_not_repeat_another_studio(
        cfg, world, monkeypatch) -> None:
    monkeypatch.setattr(core, "_find_project_holder",
                        lambda *a, **k: {"pid": None, "source": None})
    _log_on_launch(world, "ERROR Cannot open: same project is open in another Studio")
    out = core.project_open(cfg, "Demo", wait_seconds=5)
    assert out["state"] == "not_opened"
    assert "open in another Studio" not in out["reason"]
