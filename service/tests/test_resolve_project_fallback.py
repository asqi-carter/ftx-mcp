"""resolve_project's out-of-root fallback: a project that is demonstrably
OPEN IN STUDIO resolves even though it does not live under projects_root.

Field case (1.0.8): Line4_HMI lives at
C:\\Users\\<u>\\Desktop\\Line4_HMI\\HMI and is open in Studio with its bridge
armed. Every bridge-routed tool worked with it — they route by the project the
bridge REPORTS — while every resolve_project caller (optix_bridge_arm,
optix_save, optix_build_check, optix_project, optix_routes, optix_read_file,
...) answered "project not found".

The security boundary these tests pin:
  - projects_root wins, and costs nothing extra (no process scan at all)
  - "..", absolute names and subpaths are rejected BEFORE any scan
  - the fallback directory comes ONLY from a running Studio's command line,
    never from the caller's `project` string
  - that directory must exist and hold EXACTLY ONE .optix file

psutil is mocked at studio_guard._scan — the boundary core reuses — not at the
module level, so the real cmdline-reading path in studio_guard runs.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from service import core, studio_guard

# ---- helpers ---------------------------------------------------------


def make_outside_project(tmp_path: Path, dir_name: str,
                         optix_stem: str | None = None) -> Path:
    """A project directory OUTSIDE projects_root, with one .optix file.

    `optix_stem` defaults to the directory name; passing a different one
    models the field shape (a project served under its .optix stem while its
    folder is named something else, e.g. Line4_HMI/HMI).
    """
    d = tmp_path / "outside" / dir_name
    d.mkdir(parents=True)
    (d / f"{optix_stem or dir_name}.optix").write_text("fake-optix-marker")
    return d


def fake_studio_scan(*cmdlines: list[str]):
    """A studio_guard._scan replacement: one FTOptixStudio.exe per cmdline."""
    def _scan() -> list[dict]:
        return [
            {"pid": 1000 + i, "name": "ftoptixstudio.exe", "cmdline": cmd}
            for i, cmd in enumerate(cmdlines)
        ]
    return _scan


def studio_open_cmdline(project_dir: Path) -> list[str]:
    """The command line core._studio_open actually produces:
    `FTOptixStudio.exe open <abs path to .optix> --silent`."""
    optix = next(project_dir.glob("*.optix"))
    return [r"C:\Program Files\Rockwell\FTOptixStudio.exe", "open",
            str(optix), "--silent"]


@pytest.fixture(autouse=True)
def _reset_guard_cache():
    studio_guard.reset_cache()
    yield
    studio_guard.reset_cache()


# ---- projects_root still wins, and is free ---------------------------


def test_under_projects_root_resolves_without_scanning(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal case must cost ZERO extra work. Proven two ways: the scanner
    records every call (must stay empty) AND raises if it is ever reached
    (studio_state swallows that into an `error` state, which is why the call
    log — not the exception — is the real assertion)."""
    from service.tests.conftest import make_project
    make_project(projects_root, "Alpha")

    calls: list[int] = []

    def _boom() -> list[dict]:
        calls.append(1)
        raise AssertionError("resolve_project scanned processes for an in-root project")

    monkeypatch.setattr(studio_guard, "_scan", _boom)

    assert core.resolve_project(cfg, "Alpha") == (projects_root / "Alpha").resolve()
    assert calls == []

    info = core.resolve_project_info(cfg, "Alpha")
    assert info["source"] == "projects_root"
    assert info["dir"] == (projects_root / "Alpha").resolve()
    assert calls == []


def test_nested_subpath_under_root_still_resolves(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested name ("Site/Cell 4/Proj") keeps working — the fallback gate
    rejects separators, so this must be answered by projects_root first."""
    nested = projects_root / "Site" / "Cell 4" / "Proj"
    nested.mkdir(parents=True)
    (nested / "Proj.optix").write_text("x")
    monkeypatch.setattr(studio_guard, "_scan",
                        Mock(side_effect=AssertionError("scanned")))

    info = core.resolve_project_info(cfg, "Site/Cell 4/Proj")
    assert info == {"dir": nested.resolve(), "source": "projects_root"}


# ---- the fallback ----------------------------------------------------


def test_fallback_resolves_project_open_in_studio(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = make_outside_project(tmp_path, "Line4_HMI")
    monkeypatch.setattr(studio_guard, "_scan",
                        fake_studio_scan(studio_open_cmdline(outside)))

    info = core.resolve_project_info(cfg, "Line4_HMI")
    assert info == {"dir": outside.resolve(), "source": "studio_process"}
    assert core.resolve_project(cfg, "Line4_HMI") == outside.resolve()


def test_fallback_matches_optix_stem_when_folder_is_named_differently(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field shape: ...\\Desktop\\Line4_HMI\\HMI serves as "Line4_HMI".
    A dir-basename-only match would miss it entirely."""
    outside = make_outside_project(tmp_path, "HMI", optix_stem="Line4_HMI")
    monkeypatch.setattr(studio_guard, "_scan",
                        fake_studio_scan(studio_open_cmdline(outside)))

    assert core.resolve_project(cfg, "Line4_HMI") == outside.resolve()
    # the folder name resolves too — both names are evidence-derived
    assert core.resolve_project(cfg, "HMI") == outside.resolve()


def test_fallback_matches_case_insensitively(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = make_outside_project(tmp_path, "Line4_HMI")
    monkeypatch.setattr(studio_guard, "_scan",
                        fake_studio_scan(studio_open_cmdline(outside)))

    for name in ("line4_hmi", "LINE4_HMI", "  Line4_HMI  "):
        assert core.resolve_project(cfg, name) == outside.resolve()


def test_fallback_accepts_a_bare_directory_argument(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Studio launched on the project FOLDER, not the .optix file."""
    outside = make_outside_project(tmp_path, "DeskProj")
    monkeypatch.setattr(studio_guard, "_scan", fake_studio_scan(
        [r"C:\FTOptixStudio.exe", "open", str(outside)]))

    assert core.resolve_project(cfg, "DeskProj") == outside.resolve()


# ---- the fallback's own rejections -----------------------------------


def test_rejected_when_directory_has_no_optix_file(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "outside" / "NotAProject"
    empty.mkdir(parents=True)
    monkeypatch.setattr(studio_guard, "_scan", fake_studio_scan(
        [r"C:\FTOptixStudio.exe", "open", str(empty)]))

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "NotAProject")


def test_rejected_when_directory_has_two_optix_files(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two .optix files means the directory cannot be attributed to ONE
    project — reject rather than guess which one the caller meant."""
    ambiguous = make_outside_project(tmp_path, "Ambiguous")
    (ambiguous / "Second.optix").write_text("x")
    monkeypatch.setattr(studio_guard, "_scan",
                        fake_studio_scan([r"C:\FTOptixStudio.exe", "open",
                                          str(ambiguous / "Ambiguous.optix")]))

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "Ambiguous")


def test_rejected_when_directory_does_not_exist(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale command line (project since deleted/moved) is not evidence."""
    gone = tmp_path / "outside" / "Gone"
    monkeypatch.setattr(studio_guard, "_scan", fake_studio_scan(
        [r"C:\FTOptixStudio.exe", "open", str(gone / "Gone.optix")]))

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "Gone")


def test_a_different_project_open_in_studio_does_not_resolve(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = make_outside_project(tmp_path, "SomethingElse")
    monkeypatch.setattr(studio_guard, "_scan",
                        fake_studio_scan(studio_open_cmdline(other)))

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "Line4_HMI")


def test_gui_opened_studio_contributes_no_candidate(
    cfg: core.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Studio opened from the GUI carries only its bare exe path (measured —
    see studio_guard's module docstring). That is normal, not an error."""
    monkeypatch.setattr(studio_guard, "_scan", fake_studio_scan(
        [r"C:\Program Files\Rockwell\FTOptixStudio.exe"]))

    with pytest.raises(core.ProjectNotFound) as exc:
        core.resolve_project(cfg, "Line4_HMI")
    assert "no running FTOptixStudio.exe names a project directory" in str(exc.value)


# ---- the caller's string is never a path -----------------------------


def test_traversal_rejected_before_any_scan(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = make_outside_project(tmp_path, "Line4_HMI")
    scan = Mock(side_effect=AssertionError("scanned a traversal name"))
    monkeypatch.setattr(studio_guard, "_scan", scan)

    for name in ("../etc", "..", "Line4_HMI/..", "..\\Line4_HMI"):
        with pytest.raises(core.ProjectNotFound):
            core.resolve_project(cfg, name)
    assert scan.call_count == 0
    assert outside.is_dir()  # the tempting target was there the whole time


def test_absolute_name_rejected_before_any_scan(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute name must never be a route to the fallback — the caller
    does not get to hand this function a path."""
    outside = make_outside_project(tmp_path, "Line4_HMI")
    scan = Mock(side_effect=AssertionError("scanned an absolute name"))
    monkeypatch.setattr(studio_guard, "_scan", scan)

    for name in (str(outside), str(outside.parent), "/etc", "//host/share"):
        with pytest.raises(core.ProjectNotFound):
            core.resolve_project(cfg, name)
    assert scan.call_count == 0


def test_fallback_not_attempted_for_a_name_with_a_separator(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subpath name is answered by projects_root or not at all — the
    fallback matches bare names only, so no scan runs."""
    make_outside_project(tmp_path, "Line4_HMI")
    scan = Mock(side_effect=AssertionError("scanned a subpath name"))
    monkeypatch.setattr(studio_guard, "_scan", scan)

    for name in ("Site/Line4_HMI", "Site\\Line4_HMI", "sub/Line4_HMI"):
        with pytest.raises(core.ProjectNotFound) as exc:
            core.resolve_project(cfg, name)
        assert "not a bare project name" in str(exc.value)
    assert scan.call_count == 0


def test_bare_name_predicate(cfg: core.Config) -> None:
    assert core._is_bare_project_name("Line4_HMI")
    assert core._is_bare_project_name("Line_HMI Cell 4")
    for bad in ("", "   ", ".", "..", "a/b", "a\\b", "C:", "C:\\x", "a..b"):
        assert not core._is_bare_project_name(bad), bad


# ---- the error explains itself ---------------------------------------


def test_error_lists_projects_root_and_the_projects_studio_has_open(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = make_outside_project(tmp_path, "HMI", optix_stem="Line4_HMI")
    b = make_outside_project(tmp_path, "FloatPrecisionLab")
    monkeypatch.setattr(studio_guard, "_scan", fake_studio_scan(
        studio_open_cmdline(a), studio_open_cmdline(b)))

    with pytest.raises(core.ProjectNotFound) as exc:
        core.resolve_project(cfg, "Nope")
    msg = str(exc.value)
    assert "resolved_from" in msg
    assert str(cfg.projects_root) in msg
    assert "Line4_HMI" in msg and "FloatPrecisionLab" in msg
    # project NAMES only — not a directory listing back to the caller
    assert str(a) not in msg and str(b) not in msg


def test_enumeration_failure_is_a_miss_not_a_crash(
    cfg: core.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A psutil fault means "unknown", never a traceback out of a resolve."""
    import psutil
    monkeypatch.setattr(studio_guard, "_scan",
                        Mock(side_effect=psutil.AccessDenied()))

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "Line4_HMI")


# ---- provenance is observable ----------------------------------------


def test_doctor_reports_the_resolution_source(
    cfg: core.Config, tmp_path: Path, projects_root: Path,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from service.tests.conftest import make_project
    make_project(projects_root, "Alpha")
    outside = make_outside_project(tmp_path, "Line4_HMI")
    monkeypatch.setattr(studio_guard, "_scan",
                        fake_studio_scan(studio_open_cmdline(outside)))
    monkeypatch.setattr(core, "_scan_bridge_ports", lambda _cfg, force=False: [])

    def row(rep: dict) -> dict:
        return next(c for c in rep["checks"] if c["name"] == "project")

    in_root = row(core.doctor(cfg, project="Alpha"))
    assert in_root["ok"] and "resolved_from=projects_root" in in_root["detail"]

    fallback = row(core.doctor(cfg, project="Line4_HMI"))
    assert fallback["ok"] and "resolved_from=studio_process" in fallback["detail"]

    missing = row(core.doctor(cfg, project="Nope"))
    assert not missing["ok"] and "resolved_from" in missing["detail"]
    # never required — an unresolvable project must not make the box "not ready"
    assert not missing["required"]

    # unchanged when no project is asked about
    assert not any(c["name"] == "project" for c in core.doctor(cfg)["checks"])


def test_studio_cmdlines_reads_the_guard_snapshot(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper core reuses: Studio cmdlines come out of the SAME cached
    snapshot the corruption guard takes, and an error state yields nothing."""
    monkeypatch.setattr(studio_guard, "_scan", fake_studio_scan(
        [r"C:\FTOptixStudio.exe", "open", r"C:\p\A.optix"]))
    state = studio_guard.studio_state()
    assert state["studio"]["running"] is True
    assert studio_guard.studio_cmdlines(state) == [
        [r"C:\FTOptixStudio.exe", "open", r"C:\p\A.optix"]]
    assert studio_guard.studio_cmdlines({"error": "boom"}) == []
    assert studio_guard.studio_cmdlines({"studio": {"running": False, "pids": []}}) == []


# ===========================================================================
# Registry evidence source (b)
# ===========================================================================

import json as _json


def make_registry(tmp_path: Path, entries: dict) -> Path:
    """Create a bridge registry directory under tmp_path.

    ``entries`` maps port -> dict (registry file payload).  Returns the
    registry directory path so the caller can set ``LOCALAPPDATA`` to
    ``tmp_path`` and have ``_bridge_registry_dir`` resolve it.
    """
    reg_dir = tmp_path / "ftx-mcp" / "bridges"
    reg_dir.mkdir(parents=True)
    for port, payload in entries.items():
        f = reg_dir / f"{port}.json"
        f.write_text(_json.dumps({"port": port, **payload}), encoding="utf-8")
    return reg_dir


def make_outside_project_for_registry(
    tmp_path: Path, dir_name: str, optix_stem: str | None = None
) -> Path:
    """A project directory OUTSIDE projects_root with one .optix file."""
    d = tmp_path / "outside" / dir_name
    d.mkdir(parents=True)
    (d / f"{optix_stem or dir_name}.optix").write_text("fake")
    return d


# ---- basic resolution via registry ------------------------------------


def test_registry_resolves_outside_project(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry entry with project + project_path resolves an out-of-root project."""
    outside = make_outside_project_for_registry(tmp_path, "Line4_HMI")
    make_registry(tmp_path, {
        8768: {
            "project": "Line4_HMI",
            "project_path": str(outside),
            "bridge_version": "1.0.8",
            "model_loaded": True,
        }
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # No Studio process running
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    info = core.resolve_project_info(cfg, "Line4_HMI")
    assert info["source"] == "registry"
    assert info["dir"] == outside.resolve()
    assert core.resolve_project(cfg, "Line4_HMI") == outside.resolve()


def test_registry_project_path_as_optix_file(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """project_path pointing at the .optix file (not the directory) is accepted."""
    outside = make_outside_project_for_registry(tmp_path, "Alpha")
    optix_file = outside / "Alpha.optix"
    make_registry(tmp_path, {
        8768: {
            "project": "Alpha",
            "project_path": str(optix_file),
            "bridge_version": "1.0.8",
            "model_loaded": True,
        }
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    info = core.resolve_project_info(cfg, "Alpha")
    assert info["source"] == "registry"
    assert info["dir"] == outside.resolve()


def test_registry_match_is_case_insensitive(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registry project-field comparison is case-insensitive."""
    outside = make_outside_project_for_registry(tmp_path, "Line4_HMI")
    make_registry(tmp_path, {
        8768: {
            "project": "Line4_HMI",
            "project_path": str(outside),
            "bridge_version": "1.0.8",
            "model_loaded": True,
        }
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    for name in ("line4_hmi", "LINE4_HMI", "  Line4_HMI  "):
        assert core.resolve_project(cfg, name) == outside.resolve()


def test_registry_optix_stem_also_matches(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dir-name and .optix stem are included in the matching set, not just project field."""
    # Directory is "HMI" but .optix stem is "Line4_HMI"
    outside = make_outside_project_for_registry(tmp_path, "HMI", optix_stem="Line4_HMI")
    make_registry(tmp_path, {
        8768: {
            "project": "Line4_HMI",
            "project_path": str(outside),
            "bridge_version": "1.0.8",
            "model_loaded": True,
        }
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    # Both names resolve
    assert core.resolve_project(cfg, "Line4_HMI") == outside.resolve()
    assert core.resolve_project(cfg, "HMI") == outside.resolve()


# ---- registry rejections ----------------------------------------------


def test_registry_rejected_when_project_path_missing(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registry entry without project_path (or null) is skipped."""
    make_registry(tmp_path, {
        8768: {"project": "Line4_HMI", "project_path": None,
               "bridge_version": "1.0.8", "model_loaded": True}
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "Line4_HMI")


def test_registry_rejected_when_project_path_does_not_exist(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale registry entry (project_path deleted) is not evidence."""
    gone = tmp_path / "outside" / "Gone"
    make_registry(tmp_path, {
        8768: {"project": "Gone", "project_path": str(gone),
               "bridge_version": "1.0.8", "model_loaded": True}
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "Gone")


def test_registry_rejected_when_no_optix_file(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project_path directory with no .optix file is rejected."""
    empty = tmp_path / "outside" / "NotAProject"
    empty.mkdir(parents=True)
    make_registry(tmp_path, {
        8768: {"project": "NotAProject", "project_path": str(empty),
               "bridge_version": "1.0.8", "model_loaded": True}
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "NotAProject")


def test_registry_rejected_when_two_optix_files(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory with two .optix files cannot be attributed to one project."""
    ambiguous = make_outside_project_for_registry(tmp_path, "Ambiguous")
    (ambiguous / "Second.optix").write_text("x")
    make_registry(tmp_path, {
        8768: {"project": "Ambiguous", "project_path": str(ambiguous),
               "bridge_version": "1.0.8", "model_loaded": True}
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "Ambiguous")


def test_registry_different_project_does_not_resolve(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry entry for a different project does not resolve the target."""
    other = make_outside_project_for_registry(tmp_path, "SomethingElse")
    make_registry(tmp_path, {
        8768: {"project": "SomethingElse", "project_path": str(other),
               "bridge_version": "1.0.8", "model_loaded": True}
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "Line4_HMI")


# ---- security rules apply to registry too ----------------------------


def test_registry_not_attempted_for_dotdot(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'..' is rejected before any fallback including registry."""
    outside = make_outside_project_for_registry(tmp_path, "Line4_HMI")
    make_registry(tmp_path, {
        8768: {"project": "Line4_HMI", "project_path": str(outside),
               "bridge_version": "1.0.8", "model_loaded": True}
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    scan = Mock(side_effect=AssertionError("scanned a traversal name"))
    monkeypatch.setattr(studio_guard, "_scan", scan)

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "../Line4_HMI")
    assert scan.call_count == 0


def test_registry_not_attempted_for_subpath(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subpath names are rejected before registry fallback."""
    outside = make_outside_project_for_registry(tmp_path, "Line4_HMI")
    make_registry(tmp_path, {
        8768: {"project": "Line4_HMI", "project_path": str(outside),
               "bridge_version": "1.0.8", "model_loaded": True}
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    with pytest.raises(core.ProjectNotFound) as exc:
        core.resolve_project(cfg, "sub/Line4_HMI")
    assert "not a bare project name" in str(exc.value)


# ---- ordering: projects_root > studio > registry ---------------------


def test_projects_root_beats_registry(
    cfg: core.Config, projects_root: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """projects_root answer always wins, even when a registry entry exists."""
    from service.tests.conftest import make_project
    make_project(projects_root, "Alpha")
    outside = make_outside_project_for_registry(tmp_path, "Alpha")
    make_registry(tmp_path, {
        8768: {"project": "Alpha", "project_path": str(outside),
               "bridge_version": "1.0.8", "model_loaded": True}
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    scan = Mock(side_effect=AssertionError("should not scan"))
    monkeypatch.setattr(studio_guard, "_scan", scan)

    info = core.resolve_project_info(cfg, "Alpha")
    assert info["source"] == "projects_root"
    assert info["dir"] == (projects_root / "Alpha").resolve()
    assert scan.call_count == 0


def test_studio_beats_registry(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Studio-process fallback is tried before registry."""
    studio_dir = make_outside_project_for_registry(tmp_path, "StudioProj")
    reg_dir = make_outside_project_for_registry(tmp_path, "RegProj", optix_stem="StudioProj")
    make_registry(tmp_path, {
        8768: {"project": "StudioProj", "project_path": str(reg_dir),
               "bridge_version": "1.0.8", "model_loaded": True}
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    optix_file = studio_dir / "StudioProj.optix"
    monkeypatch.setattr(studio_guard, "_scan", fake_studio_scan(
        [r"C:\FTOptixStudio.exe", "open", str(optix_file)]))

    info = core.resolve_project_info(cfg, "StudioProj")
    assert info["source"] == "studio_process"
    assert info["dir"] == studio_dir.resolve()


# ---- missing registry dir / corrupt files ----------------------------


def test_missing_registry_dir_is_fine(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent registry directory means no registry candidates, not an error."""
    # LOCALAPPDATA points at a dir with no ftx-mcp/bridges/ subtree
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "Line4_HMI")


def test_corrupt_registry_file_is_skipped(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt JSON file in the registry directory is silently skipped."""
    reg_dir = tmp_path / "ftx-mcp" / "bridges"
    reg_dir.mkdir(parents=True)
    (reg_dir / "8768.json").write_text("not-valid-json", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    with pytest.raises(core.ProjectNotFound):
        core.resolve_project(cfg, "Line4_HMI")


# ---- provenance is observable ----------------------------------------


def test_registry_source_in_doctor(
    cfg: core.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """doctor() surfaces resolved_from=registry when the registry answered."""
    outside = make_outside_project_for_registry(tmp_path, "Line4_HMI")
    make_registry(tmp_path, {
        8768: {"project": "Line4_HMI", "project_path": str(outside),
               "bridge_version": "1.0.8", "model_loaded": True}
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])
    monkeypatch.setattr(core, "_scan_bridge_ports", lambda _cfg, force=False: [])

    rep = core.doctor(cfg, project="Line4_HMI")
    row = next(c for c in rep["checks"] if c["name"] == "project")
    assert row["ok"]
    assert "resolved_from=registry" in row["detail"]


def test_same_named_container_folder_does_not_shadow_studio_project(
    cfg: core.Config, projects_root: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1.0.8 battle test: projects_root=Desktop, project at Desktop/Line4_HMI/HMI.
    The in-root folder Desktop/Line4_HMI exists but holds no .optix, and it won,
    so every tool got the container folder instead of the project."""
    container = projects_root / "Line4_HMI"
    real = container / "HMI"
    real.mkdir(parents=True)
    (real / "Line4_HMI.optix").write_text("fake-optix-marker")
    monkeypatch.setattr(studio_guard, "_scan", fake_studio_scan(studio_open_cmdline(real)))
    info = core.resolve_project_info(cfg, "Line4_HMI")
    assert info["dir"] == real.resolve() and info["source"] == "studio_process"


def test_container_folder_still_resolves_when_nothing_else_matches(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (projects_root / "Loose").mkdir()
    monkeypatch.setattr(studio_guard, "_scan", fake_studio_scan())
    info = core.resolve_project_info(cfg, "Loose")
    assert info == {"dir": (projects_root / "Loose").resolve(), "source": "projects_root"}
