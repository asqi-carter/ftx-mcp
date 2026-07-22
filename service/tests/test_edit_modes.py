"""W2 edit-foundation tests: optix_find, ranged read, anchored edit modes.

Anchored edits are exercised through the real deploy() path (with a fake
runner + Studio export handler) so the two-phase resolve-then-write and the
atomic-refusal semantics are covered end to end, not just in isolation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from service import core, studio_guard
from service.tests.conftest import (
    make_export_handler,
    make_fake_runner,
    make_project,
)


@pytest.fixture(autouse=True)
def _studio_closed(monkeypatch: pytest.MonkeyPatch):
    """All W2 tests run as if Studio is closed (guard covered separately)."""
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])
    studio_guard.reset_cache()
    yield
    studio_guard.reset_cache()


def _write(p: Path, rel: str, text: str, *, crlf: bool = False) -> Path:
    f = p / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    data = text.replace("\n", "\r\n") if crlf else text
    f.write_bytes(data.encode("utf-8"))
    return f


def _deploy(cfg: core.Config, project: str, edits: list[dict]) -> dict:
    runner = make_fake_runner(make_export_handler())
    req = core.DeployRequest(edits=edits, commit_message="w2", run_after_deploy=False)
    return core.deploy(cfg, project, req, runner=runner)


# ---- optix_find -------------------------------------------------------


def test_find_locates_node_with_context(cfg: core.Config, projects_root: Path) -> None:
    proj = make_project(projects_root, "Alpha")
    _write(proj, "Nodes/UI/Screens.yaml", "- Name: Screen1\n  Type: Screen\n- Name: Btn\n  Type: Button\n")
    out = core.find_in_project(cfg, "Alpha", "Type: Button")
    assert out["match_count"] == 1
    m = out["matches"][0]
    assert m["path"] == "Nodes/UI/Screens.yaml"
    assert m["line"] == 4
    assert "Name: Btn" in m["context_before"][-1]


def test_find_is_case_insensitive_by_default(cfg: core.Config, projects_root: Path) -> None:
    proj = make_project(projects_root, "Alpha")
    _write(proj, "f.yaml", "Type: LABEL\n")
    assert core.find_in_project(cfg, "Alpha", "type: label")["match_count"] == 1
    assert core.find_in_project(cfg, "Alpha", "type: label", case_sensitive=True)["match_count"] == 0


def test_find_skips_build_dirs(cfg: core.Config, projects_root: Path) -> None:
    proj = make_project(projects_root, "Alpha")
    _write(proj, "Nodes/UI.yaml", "marker_token\n")
    _write(proj, "ProjectFiles/NetSolution/obj/Debug/gen.cs", "marker_token\n")
    _write(proj, "ProjectFiles/NetSolution/bin/out.cs", "marker_token\n")
    out = core.find_in_project(cfg, "Alpha", "marker_token")
    assert out["match_count"] == 1
    assert out["matches"][0]["path"] == "Nodes/UI.yaml"


def test_find_truncates_at_max_results(cfg: core.Config, projects_root: Path) -> None:
    proj = make_project(projects_root, "Alpha")
    _write(proj, "big.yaml", "hit\n" * 50)
    out = core.find_in_project(cfg, "Alpha", "hit", max_results=10)
    assert out["match_count"] == 10
    assert out["truncated"] is True


def test_find_rejects_multiline_query(cfg: core.Config, projects_root: Path) -> None:
    make_project(projects_root, "Alpha")
    with pytest.raises(core.InvalidQuery):
        core.find_in_project(cfg, "Alpha", "a\nb")


# ---- ranged read ------------------------------------------------------


def test_ranged_read_returns_slice_but_whole_file_metadata(
    cfg: core.Config, projects_root: Path
) -> None:
    proj = make_project(projects_root, "Alpha")
    f = _write(proj, "f.yaml", "L1\nL2\nL3\nL4\nL5\n")
    full = core.read_file(cfg, "Alpha", "f.yaml")
    out = core.read_file(cfg, "Alpha", "f.yaml", start_line=2, end_line=3)
    assert out["content"] == "L2\nL3\n"
    assert out["start_line"] == 2 and out["end_line"] == 3
    assert out["total_lines"] == 5
    assert out["sha256"] == full["sha256"]  # fingerprint is whole-file
    assert out["size"] == f.stat().st_size


def test_ranged_read_clamps_end_to_eof(cfg: core.Config, projects_root: Path) -> None:
    proj = make_project(projects_root, "Alpha")
    _write(proj, "f.yaml", "L1\nL2\n")
    out = core.read_file(cfg, "Alpha", "f.yaml", start_line=2, end_line=99)
    assert out["content"] == "L2\n"
    assert out["end_line"] == 2


def test_ranged_read_rejects_bad_range(cfg: core.Config, projects_root: Path) -> None:
    proj = make_project(projects_root, "Alpha")
    _write(proj, "f.yaml", "L1\nL2\n")
    with pytest.raises(core.BadLineRange):
        core.read_file(cfg, "Alpha", "f.yaml", start_line=5)
    with pytest.raises(core.BadLineRange):
        core.read_file(cfg, "Alpha", "f.yaml", start_line=2, end_line=1)


# ---- find/replace mode ------------------------------------------------


def test_find_replace_changes_only_the_target_line(
    cfg: core.Config, projects_root: Path
) -> None:
    proj = make_project(projects_root, "Alpha")
    f = _write(proj, "Nodes/UI.yaml", "MaxConnections: 5\nOtherField: 5\n")
    out = _deploy(cfg, "Alpha", [{"path": "Nodes/UI.yaml", "find": "MaxConnections: 5", "replace": "MaxConnections: 6"}])
    assert out["state"] == "succeeded"
    assert f.read_text() == "MaxConnections: 6\nOtherField: 5\n"
    assert out["edit_summary"][0]["mode"] == "find_replace"
    assert out["edit_summary"][0]["occurrences"] == 1


def test_find_replace_count_mismatch_refuses_atomically(
    cfg: core.Config, projects_root: Path
) -> None:
    proj = make_project(projects_root, "Alpha")
    f1 = _write(proj, "a.yaml", "x: 1\nx: 1\n")
    f2 = _write(proj, "b.yaml", "y: 2\n")
    before1, before2 = f1.read_bytes(), f2.read_bytes()
    with pytest.raises(core.EditAnchorMismatch):
        _deploy(cfg, "Alpha", [
            {"path": "b.yaml", "find": "y: 2", "replace": "y: 3"},   # would succeed
            {"path": "a.yaml", "find": "x: 1", "replace": "x: 9"},   # 2 matches, expect 1
        ])
    # atomic: neither file written, even the valid one
    assert f1.read_bytes() == before1
    assert f2.read_bytes() == before2


def test_find_replace_honors_expect_count(cfg: core.Config, projects_root: Path) -> None:
    proj = make_project(projects_root, "Alpha")
    f = _write(proj, "a.yaml", "v\nv\nv\n")
    out = _deploy(cfg, "Alpha", [{"path": "a.yaml", "find": "v", "replace": "w", "expect_count": 3}])
    assert out["state"] == "succeeded"
    assert f.read_text() == "w\nw\nw\n"
    assert out["edit_summary"][0]["occurrences"] == 3


def test_find_replace_crlf_file_with_lf_anchor(cfg: core.Config, projects_root: Path) -> None:
    """Caller writes '\\n'; a CRLF file still matches and stays CRLF."""
    proj = make_project(projects_root, "Alpha")
    f = _write(proj, "win.yaml", "A: 1\nB: 2\n", crlf=True)
    out = _deploy(cfg, "Alpha", [{"path": "win.yaml", "find": "A: 1\nB: 2", "replace": "A: 1\nB: 9"}])
    assert out["state"] == "succeeded"
    raw = f.read_bytes()
    assert raw == b"A: 1\r\nB: 9\r\n"  # EOL preserved, no LF leakage


# ---- insert_after_anchor mode -----------------------------------------


def test_insert_after_anchor_adds_block(cfg: core.Config, projects_root: Path) -> None:
    proj = make_project(projects_root, "Alpha")
    f = _write(proj, "Model.yaml", "Children:\n- Name: Existing\n")
    out = _deploy(cfg, "Alpha", [{
        "path": "Model.yaml",
        "insert_after_anchor": "Children:",
        "block": "- Name: PowerOn\n  Type: Boolean",
    }])
    assert out["state"] == "succeeded"
    assert f.read_text() == "Children:\n- Name: PowerOn\n  Type: Boolean\n- Name: Existing\n"
    assert out["edit_summary"][0]["mode"] == "insert_after_anchor"


def test_insert_after_anchor_ambiguous_refuses(cfg: core.Config, projects_root: Path) -> None:
    proj = make_project(projects_root, "Alpha")
    f = _write(proj, "m.yaml", "Children:\nChildren:\n")
    before = f.read_bytes()
    with pytest.raises(core.EditAnchorMismatch):
        _deploy(cfg, "Alpha", [{"path": "m.yaml", "insert_after_anchor": "Children:", "block": "- x"}])
    assert f.read_bytes() == before


def test_insert_after_anchor_crlf_preserved(cfg: core.Config, projects_root: Path) -> None:
    proj = make_project(projects_root, "Alpha")
    f = _write(proj, "m.yaml", "Children:\n- Existing\n", crlf=True)
    out = _deploy(cfg, "Alpha", [{"path": "m.yaml", "insert_after_anchor": "Children:", "block": "- New"}])
    assert out["state"] == "succeeded"
    assert f.read_bytes() == b"Children:\r\n- New\r\n- Existing\r\n"


# ---- batch invariants -------------------------------------------------


def test_full_content_still_works_and_creates_files(
    cfg: core.Config, projects_root: Path
) -> None:
    make_project(projects_root, "Alpha")
    out = _deploy(cfg, "Alpha", [{"path": "Nodes/New.yaml", "content": "Name: New\n"}])
    assert out["state"] == "succeeded"
    assert (projects_root / "Alpha" / "Nodes" / "New.yaml").read_text() == "Name: New\n"
    assert out["edit_summary"][0]["mode"] == "content"


def test_duplicate_path_in_batch_refused(cfg: core.Config, projects_root: Path) -> None:
    proj = make_project(projects_root, "Alpha")
    f = _write(proj, "a.yaml", "x: 1\n")
    before = f.read_bytes()
    with pytest.raises(core.InvalidEdit):
        _deploy(cfg, "Alpha", [
            {"path": "a.yaml", "find": "x: 1", "replace": "x: 2"},
            {"path": "a.yaml", "find": "x: 2", "replace": "x: 3"},
        ])
    assert f.read_bytes() == before


def test_unknown_edit_shape_refused(cfg: core.Config, projects_root: Path) -> None:
    proj = make_project(projects_root, "Alpha")
    _write(proj, "a.yaml", "x: 1\n")
    with pytest.raises(core.InvalidEdit):
        _deploy(cfg, "Alpha", [{"path": "a.yaml", "replace": "x: 2"}])  # no find/content/insert


def test_multi_mode_edit_refused_without_touching_file(
    cfg: core.Config, projects_root: Path
) -> None:
    """A stray 'content' alongside find/replace must be rejected, not silently
    overwrite the whole file via mode precedence."""
    proj = make_project(projects_root, "Alpha")
    f = _write(proj, "a.yaml", "MaxConnections: 5\nOtherField: 5\n")
    before = f.read_bytes()
    with pytest.raises(core.InvalidEdit):
        _deploy(cfg, "Alpha", [{
            "path": "a.yaml",
            "content": "Name: New\n",            # would clobber the file
            "find": "MaxConnections: 5",         # caller's actual intent
            "replace": "MaxConnections: 6",
        }])
    assert f.read_bytes() == before  # atomic refusal: file untouched


def test_anchored_edit_on_missing_file_refused(cfg: core.Config, projects_root: Path) -> None:
    make_project(projects_root, "Alpha")
    with pytest.raises(core.FileNotFound):
        _deploy(cfg, "Alpha", [{"path": "nope.yaml", "find": "a", "replace": "b"}])
