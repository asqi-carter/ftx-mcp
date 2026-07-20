"""Tests for the MCP-served skill playbooks (list_skills / get_skill)."""
from __future__ import annotations

from pathlib import Path

import pytest

from service import core


@pytest.fixture()
def skills_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "skills"
    (d / "optix-nav-layout").mkdir(parents=True)
    (d / "optix-nav-layout" / "SKILL.md").write_text(
        "---\nname: optix-nav-layout\ndescription: Build navigation.\n---\n\n# Steps\n1. do it\n",
        encoding="utf-8")
    (d / "optix-add-label").mkdir()
    (d / "optix-add-label" / "SKILL.md").write_text(
        "---\nname: optix-add-label\ndescription: One-shot label.\n---\nbody",
        encoding="utf-8")
    monkeypatch.setenv("OPTIX_SKILLS_DIR", str(d))
    return d


def test_list_skills(cfg: core.Config, skills_dir: Path) -> None:
    out = core.list_skills(cfg)
    assert out["count"] == 2
    names = {s["name"]: s["description"] for s in out["skills"]}
    assert names["optix-nav-layout"] == "Build navigation."


def test_get_skill(cfg: core.Config, skills_dir: Path) -> None:
    out = core.get_skill(cfg, "optix-nav-layout")
    assert "# Steps" in out["content"]


def test_get_skill_unknown_names_available(cfg: core.Config, skills_dir: Path) -> None:
    with pytest.raises(core.NodeNotFound) as e:
        core.get_skill(cfg, "optix-ghost")
    assert "optix-nav-layout" in str(e.value)


def test_real_bundled_skills_parse(cfg: core.Config, monkeypatch) -> None:
    """The actual skills/ tree must parse: every skill has a name and
    a description (the catalog line)."""
    monkeypatch.delenv("OPTIX_SKILLS_DIR", raising=False)
    out = core.list_skills(cfg)
    assert out["count"] >= 10
    for s in out["skills"]:
        assert s["name"] and s["description"], s
