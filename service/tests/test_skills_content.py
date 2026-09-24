"""Tree-wide skill registration, frontmatter, tool-name, and op-verb lints.

These tests are purely static — no bridge, no Studio, no network — and target
the skills/ tree directly.  They serve as CI gates that catch:

  * Missing or mis-named skills after the tree grows
  * Frontmatter regressions (name/description contract)
  * Skill bodies that reference non-existent tool names
  * Op-verb strings that would fail the bridge validator
  * Missing silent-failure documentation in the silent-failure skills
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from service import core
from service.tests.test_mcp_app import EXPECTED_TOOLS, _BRIDGE_PRIMITIVE_TOOLS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SKILLS_ROOT: Path = Path(__file__).resolve().parent.parent.parent / "skills"

#: Skills documenting silent-failure patterns (the failure mode is silence).
_SILENT_FAILURE_SKILLS: list[str] = [
    "optix-web-click-surface",
]

#: Every skill present in the tree (sorted for deterministic parametrisation).
_ALL_SKILLS: list[str] = sorted(
    p.parent.name for p in _SKILLS_ROOT.glob("*/SKILL.md")
)

#: Pattern for optix_* tool name tokens.  Three exclusion rules:
#:  1. Tokens immediately followed by ``*`` are prose wildcards (e.g.
#:     ``optix_bridge_*``); the negative lookahead ``(?!\*)`` drops them.
#:  2. Requiring at least one char of ``[a-z_0-9]`` after the ``optix_``
#:     prefix and using ``\b`` for a word boundary together ensure we only
#:     capture complete identifiers, never bare prefixes.
#:  3. Tokens immediately followed by ``.md`` are memory-file citations
#:     (e.g. ``reference_optix_web_session_non_ui_object.md``), not tool
#:     names; the negative lookahead ``(?!\.md\b)`` drops them. A prior
#:     fix mutated the citation instead (9605741), corrupting a real
#:     pointer to a maintainer note -- fix the lint, not
#:     the citation.
_TOOL_TOKEN: re.Pattern[str] = re.compile(r"optix_[a-z_0-9]+\b(?!\*)(?!\.md\b)")

#: All valid tool names across the default surface + gated bridge primitives.
_VALID_TOOLS: frozenset[str] = EXPECTED_TOOLS | _BRIDGE_PRIMITIVE_TOOLS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _skill_body(name: str) -> str:
    """Return the SKILL.md content after stripping the YAML frontmatter block.

    The frontmatter is the ``---``-fenced block at the very top of the file.
    If no frontmatter is present, the full file is returned unchanged.
    """
    text = (_SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i, ln in enumerate(lines[1:], 1):
            if ln.strip() == "---":
                return "\n".join(lines[i + 1:])
    return text


def _frontmatter_lines(name: str) -> list[str]:
    """Return only the lines inside the YAML frontmatter block (no delimiters)."""
    text = (_SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    fm: list[str] = []
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        fm.append(ln)
    return fm


# ---------------------------------------------------------------------------
# (1) Registration
# ---------------------------------------------------------------------------

def test_registration_includes_silent_failure_skills(
    cfg: core.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration: the real bundled tree exposes the silent-failure skills and
    has a healthy total count.

    Uses the same monkeypatch.delenv idiom as test_real_bundled_skills_parse
    in test_skills_tools.py so that OPTIX_SKILLS_DIR overrides set in CI do
    not interfere.
    """
    monkeypatch.delenv("OPTIX_SKILLS_DIR", raising=False)
    out = core.list_skills(cfg)
    names = {s["name"] for s in out["skills"]}
    for expected in _SILENT_FAILURE_SKILLS:
        assert expected in names, (
            f"skill {expected!r} missing from catalog; present: {sorted(names)}"
        )
    assert out["count"] == len(_ALL_SKILLS), (
        f"catalog lists {out['count']} skills, tree has {len(_ALL_SKILLS)}"
    )


# ---------------------------------------------------------------------------
# (2) Fetch
# ---------------------------------------------------------------------------

def test_fetch_returns_heading_for_each_new_skill(
    cfg: core.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fetch: get_skill for each silent-failure skill returns content that
    includes the skill's own ``# `` top-level heading.

    Proves that SKILL.md is discoverable at the path the glob in core.py
    expects (``skills/<name>/SKILL.md``) and that the file has a heading.
    """
    monkeypatch.delenv("OPTIX_SKILLS_DIR", raising=False)
    for name in _SILENT_FAILURE_SKILLS:
        out = core.get_skill(cfg, name)
        assert "# " in out["content"], (
            f"{name}: get_skill returned content with no '# ' heading"
        )


# ---------------------------------------------------------------------------
# (3) Frontmatter parse — parametrised over every skill in the tree
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", _ALL_SKILLS)
def test_frontmatter_parse(name: str) -> None:
    """Frontmatter: parsed name equals directory name, description is
    non-empty, and the raw frontmatter block contains exactly one line
    beginning ``description:``.

    Generalises test_real_bundled_skills_parse from test_skills_tools.py,
    which only asserts truthiness; this pins the schema per skill.
    """
    path = _SKILLS_ROOT / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    fm = core._skill_frontmatter(text)  # type: ignore[attr-defined]

    assert fm.get("name") == name, (
        f"{name}: frontmatter 'name' field is {fm.get('name')!r}, "
        f"expected {name!r}"
    )
    assert fm.get("description"), (
        f"{name}: frontmatter 'description' is missing or empty"
    )

    fm_lines = _frontmatter_lines(name)
    desc_lines = [ln for ln in fm_lines if ln.startswith("description:")]
    assert len(desc_lines) == 1, (
        f"{name}: expected exactly 1 'description:' line in frontmatter, "
        f"found {len(desc_lines)}: {desc_lines!r}"
    )


# ---------------------------------------------------------------------------
# (4) Tool-name lint — tree-wide
# ---------------------------------------------------------------------------

def test_tool_name_lint() -> None:
    """Tool-name lint: every ``optix_*`` identifier in every skill body must
    be a member of EXPECTED_TOOLS ∪ _BRIDGE_PRIMITIVE_TOOLS.

    The regex excludes prose wildcards (``optix_bridge_*``) via a negative
    lookahead for ``*`` and uses a word boundary to require complete tokens.

    **This test FAILS on the unmodified tree** — skills that reference a
    filename or prose token that looks like a tool name (e.g.
    ``optix_web_session_non_ui_object``) will appear in the failure list.
    Fix by renaming the prose token in the skill body or by adding the
    name to EXPECTED_TOOLS if it is a real, registered tool.
    """
    failures: list[str] = []
    for name in _ALL_SKILLS:
        body = _skill_body(name)
        for token in _TOOL_TOKEN.findall(body):
            if token not in _VALID_TOOLS:
                failures.append(f"{name}: unrecognised tool name {token!r}")

    assert not failures, (
        "Tool-name lint failures — add token to EXPECTED_TOOLS / "
        "_BRIDGE_PRIMITIVE_TOOLS in test_mcp_app.py, or fix the skill body:\n  "
        + "\n  ".join(failures)
    )


# ---------------------------------------------------------------------------
# (5) Op-verb lint — tree-wide
# ---------------------------------------------------------------------------

def test_op_verb_lint() -> None:
    """Op-verb lint: every ``"op": "<verb>"`` occurrence in every skill body
    must be a member of ``core.BRIDGE_EDIT_VERBS``.

    This is a regression guard — it passes on the current tree and should
    continue to pass unless a skill body is edited to use a non-existent verb.
    """
    _OP_VERB: re.Pattern[str] = re.compile(r'"op"\s*:\s*"([^"]+)"')
    failures: list[str] = []
    for name in _ALL_SKILLS:
        body = _skill_body(name)
        for verb in _OP_VERB.findall(body):
            if verb not in core.BRIDGE_EDIT_VERBS:
                failures.append(f"{name}: unknown op verb {verb!r}")

    assert not failures, (
        "Op-verb lint failures — verb must be in core.BRIDGE_EDIT_VERBS "
        f"(service/core.py:{core.BRIDGE_EDIT_VERBS!r}):\n  "
        + "\n  ".join(failures)
    )


# ---------------------------------------------------------------------------
# (6) Silent-failure section — parametrised over the silent-failure skills
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", _SILENT_FAILURE_SKILLS)
def test_silent_failure_section(name: str) -> None:
    """Each silent-failure skill must document silent-failure patterns and
    include a ``## Verify`` (or similar) heading.

    ``silent`` is checked against the full file (frontmatter + body) because
    some skills name the pattern in their description line.  The ``Verify``
    heading is checked against the body only (headings live in the markdown
    body, not in YAML frontmatter).

    Parametrised so a missing section reports the specific skill name.
    """
    content = (_SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    body = _skill_body(name)
    assert re.search(r"silent", content, re.IGNORECASE), (
        f"{name}: no mention of 'silent' failures anywhere in the skill file"
    )
    assert re.search(r"^#+\s*verify\b", body, re.IGNORECASE | re.MULTILINE), (
        f"{name}: no 'Verify' heading (``## Verify`` or similar) found in body"
    )


# ---------------------------------------------------------------------------
# (8) Enum note — parametrised over the silent-failure skills
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", _SILENT_FAILURE_SKILLS)
def test_enum_note(name: str) -> None:
    """Each silent-failure skill must include a reference to Module.xml and
    a statement that numeric enum values are always accepted.

    These notes help agents avoid enum-name failures without needing an
    up-to-date external table.
    """
    content = (_SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    assert "Module.xml" in content, (
        f"{name}: no 'Module.xml' reference in skill body"
    )
    assert re.search(
        r"[Nn]umeric values are.*accepted",
        content,
        re.DOTALL | re.IGNORECASE,
    ), (
        f"{name}: no statement that numeric enum values are accepted "
        f"(expected pattern 'Numeric values are ... accepted')"
    )
