"""Offline test suite for core.read_netlogic + the HTTP /netlogic/{cls} surface.

All tests run without a live Studio or bridge — filesystem access is via
tmp_path, process detection is patched to the empty list by the autouse
``_no_host_processes`` fixture in conftest.py.

Test cases:
  1. Happy path — whole-file size/sha256/total_lines, source='disk', untrusted wrap.
  2. Ranged read — slice content, whole-file metadata, end_line clamping, start
     past EOF raises BadLineRange.
  3. Rejection matrix — PathTraversal and InvalidNetLogicClass; assert NOTHING
     was read in every rejection case.
  4. Missing class — FileNotFound names project-relative path + sibling list.
  5. Non-UTF-8 bytes — BinaryFile.
  6. Over-cap file — truncated=True, content <= cap, size unchanged, nudge present.
  7. Guard asymmetry — studio RUNNING → read_netlogic succeeds (studio_open=True),
     core.read_file on same project raises StudioOpen (deliberate asymmetry).
  8. Attributed VS Code holding project — EditorProjectOpen.
  9. Registry — tool in EXPECTED_TOOLS, count == 44, readOnlyHint=True,
     auth.TOOL_SCOPES['optix_read_netlogic'] == 'read'.
 10. Doc-count — parse tool count from README.md + docs/tool-reference.md;
     both must equal len(EXPECTED_TOOLS).
 11. HTTP — GET /projects/{p}/netlogic/{cls} returns correct payload shape;
     traversal attempt returns 400 with code='path_traversal_rejected'.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service import auth, core, studio_guard
from service.http_app import make_app
from service.mcp_app import make_mcp
from service.tests.conftest import make_project
from service.tests.test_mcp_app import EXPECTED_TOOLS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STUDIO_PROC = {
    "pid": 4242,
    "name": "ftoptixstudio.exe",
    "cmdline": [r"C:\FTOptixStudio.exe"],
}

_VSCODE_PROC_TEMPLATE = {
    "pid": 777,
    "name": "code.exe",
    "cmdline": ["code.exe", "PLACEHOLDER"],
}


def _make_netlogic(projects_root: Path, project: str, cls: str, content: bytes) -> Path:
    """Create a minimal Optix project with a NetSolution .cs file.

    Returns the project directory (not the .cs file).
    """
    p = make_project(projects_root, project)
    net_dir = p / "ProjectFiles" / "NetSolution"
    net_dir.mkdir(parents=True, exist_ok=True)
    cs_file = net_dir / f"{cls}.cs"
    cs_file.write_bytes(content)
    return p


def _tool_fn(tool):
    """Unwrap offloaded async tools to the original sync callable."""
    return getattr(tool, "_ftx_sync_fn", tool.fn)


def _list_tools(mcp) -> list:
    return mcp._tool_manager.list_tools()


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Successful whole-file read — result shape, keys, untrusted wrapper."""

    _CONTENT = b"using System;\nnamespace X { class Foo : BaseNetLogic { } }\n"

    def test_returns_expected_keys(self, cfg: core.Config, projects_root: Path) -> None:
        """Result must carry path, size, sha256, total_lines, source, studio_open, content."""
        _make_netlogic(projects_root, "Alpha", "Foo", self._CONTENT)
        out = core.read_netlogic(cfg, "Alpha", "Foo")

        assert out["path"] == "ProjectFiles/NetSolution/Foo.cs"
        assert out["size"] == len(self._CONTENT)
        assert out["sha256"] == hashlib.sha256(self._CONTENT).hexdigest()
        assert out["total_lines"] == 2
        assert out["source"] == "disk"
        assert isinstance(out["studio_open"], bool)
        assert "content" in out

    def test_content_is_untrusted_wrapped(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        """Content must be delimited by <untrusted source='read_netlogic'>…</untrusted>."""
        _make_netlogic(projects_root, "Alpha", "Foo", self._CONTENT)
        out = core.read_netlogic(cfg, "Alpha", "Foo")

        expected = core._untrusted(self._CONTENT.decode("utf-8"), "read_netlogic")
        assert out["content"] == expected
        # Explicit wrapper text assertions (per test_untrusted_wrap.py pattern)
        assert out["content"].startswith('<untrusted source="read_netlogic">')
        assert out["content"].endswith("</untrusted>")
        assert "BaseNetLogic" in out["content"]

    def test_no_truncated_key_for_normal_file(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        """Small files must NOT have a 'truncated' key."""
        _make_netlogic(projects_root, "Alpha", "Foo", self._CONTENT)
        out = core.read_netlogic(cfg, "Alpha", "Foo")
        assert "truncated" not in out

    def test_sha256_matches_whole_file_bytes(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        _make_netlogic(projects_root, "Alpha", "Foo", self._CONTENT)
        out = core.read_netlogic(cfg, "Alpha", "Foo")
        assert out["sha256"] == hashlib.sha256(self._CONTENT).hexdigest()


# ---------------------------------------------------------------------------
# 2. Ranged read
# ---------------------------------------------------------------------------


class TestRangedRead:
    """start_line / end_line slicing; whole-file metadata preserved."""

    _LINES = b"line1\nline2\nline3\nline4\nline5\n"  # 5 lines

    def _setup(self, projects_root: Path, cls: str = "FooLogic") -> None:
        _make_netlogic(projects_root, "Alpha", cls, self._LINES)

    def test_slice_returns_correct_lines(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        self._setup(projects_root)
        out = core.read_netlogic(cfg, "Alpha", "FooLogic", start_line=2, end_line=4)
        assert out["content"] == core._untrusted("line2\nline3\nline4\n", "read_netlogic")
        assert out["start_line"] == 2
        assert out["end_line"] == 4

    def test_whole_file_metadata_preserved_on_slice(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        """size, sha256, total_lines always describe the WHOLE file."""
        self._setup(projects_root)
        out = core.read_netlogic(cfg, "Alpha", "FooLogic", start_line=2, end_line=3)
        assert out["size"] == len(self._LINES)
        assert out["sha256"] == hashlib.sha256(self._LINES).hexdigest()
        assert out["total_lines"] == 5

    def test_end_line_past_eof_clamped(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        """end_line > total_lines is silently clamped to EOF, not an error."""
        self._setup(projects_root)
        out = core.read_netlogic(cfg, "Alpha", "FooLogic", start_line=4, end_line=999)
        assert out["end_line"] == 5  # clamped to total_lines
        assert out["content"] == core._untrusted("line4\nline5\n", "read_netlogic")

    def test_start_line_past_eof_raises_bad_range(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        """start_line beyond the last line must raise BadLineRange."""
        self._setup(projects_root)
        with pytest.raises(core.BadLineRange):
            core.read_netlogic(cfg, "Alpha", "FooLogic", start_line=100)

    def test_end_before_start_raises_bad_range(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        self._setup(projects_root)
        with pytest.raises(core.BadLineRange):
            core.read_netlogic(cfg, "Alpha", "FooLogic", start_line=4, end_line=2)

    def test_start_line_zero_raises_bad_range(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        self._setup(projects_root)
        with pytest.raises(core.BadLineRange):
            core.read_netlogic(cfg, "Alpha", "FooLogic", start_line=0)


# ---------------------------------------------------------------------------
# 3. Rejection matrix
# ---------------------------------------------------------------------------


class TestRejectionMatrix:
    """Every invalid cls must be rejected before any filesystem read.

    The test proves 'NOTHING was read' by asserting the error is raised even
    when the project directory does not exist — if the impl touched the disk
    first it would fail with a different error (FileNotFound / ProjectNotFound).
    """

    # PathTraversal cases — caught before InvalidNetLogicClass.
    @pytest.mark.parametrize("cls", [
        "../../../etc/passwd",
        "..\\..\\secrets",
        "/abs/Foo",
        "C:\\Foo",
    ])
    def test_path_traversal_cases(
        self, cls: str, cfg: core.Config, projects_root: Path
    ) -> None:
        """'/' '\\' '..' and drive-letter prefixes are PathTraversal."""
        # Do NOT create the project; proves no filesystem access occurs.
        with pytest.raises(core.PathTraversal):
            core.read_netlogic(cfg, "NonExistentProject", cls)

    # Sub/Foo — forward-slash → PathTraversal
    def test_subpath_forward_slash_is_traversal(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        with pytest.raises(core.PathTraversal):
            core.read_netlogic(cfg, "NonExistentProject", "Sub/Foo")

    # InvalidNetLogicClass cases — caught after PathTraversal, before filesystem.
    @pytest.mark.parametrize("cls", [
        "Foo.cs",   # extension included
        "Foo*",     # wildcard star
        "",         # empty
        "1Foo",     # leading digit
    ])
    def test_invalid_netlogic_class_cases(
        self, cls: str, cfg: core.Config, projects_root: Path
    ) -> None:
        """Malformed-but-safe names raise InvalidNetLogicClass."""
        with pytest.raises(core.InvalidNetLogicClass):
            core.read_netlogic(cfg, "NonExistentProject", cls)

    def test_nothing_read_on_traversal(
        self, cfg: core.Config, projects_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Traversal check fires before any Path.read_bytes / open call."""
        reads: list[str] = []
        original_read_bytes = Path.read_bytes

        def spy_read_bytes(self_: Path) -> bytes:
            reads.append(str(self_))
            return original_read_bytes(self_)

        monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
        with pytest.raises(core.PathTraversal):
            core.read_netlogic(cfg, "Alpha", "../../../etc/passwd")
        assert reads == [], f"read_bytes was called: {reads}"

    def test_nothing_read_on_invalid_class(
        self, cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """InvalidNetLogicClass check fires before any Path.read_bytes call."""
        reads: list[str] = []
        original_read_bytes = Path.read_bytes

        def spy_read_bytes(self_: Path) -> bytes:
            reads.append(str(self_))
            return original_read_bytes(self_)

        monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
        with pytest.raises(core.InvalidNetLogicClass):
            core.read_netlogic(cfg, "Alpha", "Foo.cs")
        assert reads == [], f"read_bytes was called: {reads}"


# ---------------------------------------------------------------------------
# 4. Missing class → FileNotFound with path + siblings
# ---------------------------------------------------------------------------


class TestMissingClass:
    """FileNotFound names the project-relative path and lists sibling .cs files."""

    def test_missing_class_raises_file_not_found(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.FileNotFound):
            core.read_netlogic(cfg, "Alpha", "DoesNotExist")

    def test_message_includes_project_relative_path(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        make_project(projects_root, "Alpha")
        with pytest.raises(core.FileNotFound) as exc_info:
            core.read_netlogic(cfg, "Alpha", "DoesNotExist")
        msg = str(exc_info.value)
        assert "ProjectFiles/NetSolution/DoesNotExist.cs" in msg

    def test_message_lists_sibling_classes(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        p = make_project(projects_root, "Alpha")
        net_dir = p / "ProjectFiles" / "NetSolution"
        net_dir.mkdir(parents=True, exist_ok=True)
        (net_dir / "SiblingA.cs").write_bytes(b"// a\n")
        (net_dir / "SiblingB.cs").write_bytes(b"// b\n")
        with pytest.raises(core.FileNotFound) as exc_info:
            core.read_netlogic(cfg, "Alpha", "DoesNotExist")
        msg = str(exc_info.value)
        assert "SiblingA.cs" in msg
        assert "SiblingB.cs" in msg


# ---------------------------------------------------------------------------
# 5. Non-UTF-8 bytes → BinaryFile
# ---------------------------------------------------------------------------


class TestBinaryFile:
    """Non-UTF-8 content raises BinaryFile."""

    def test_non_utf8_bytes_raise_binary_file(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        _make_netlogic(projects_root, "Alpha", "Foo", b"\xff\xfe\x00\x01binary")
        with pytest.raises(core.BinaryFile):
            core.read_netlogic(cfg, "Alpha", "Foo")


# ---------------------------------------------------------------------------
# 6. Over-cap file → truncated
# ---------------------------------------------------------------------------


class TestTruncation:
    """Files > _NETLOGIC_MAX_BYTES: truncated=True, content <= cap, size unchanged."""

    def test_large_file_truncated_flag(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        big = b"x" * (core._NETLOGIC_MAX_BYTES + 1)
        _make_netlogic(projects_root, "Alpha", "BigLogic", big)
        out = core.read_netlogic(cfg, "Alpha", "BigLogic")
        assert out.get("truncated") is True

    def test_content_length_within_cap(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        big = b"a" * (core._NETLOGIC_MAX_BYTES + 500)
        _make_netlogic(projects_root, "Alpha", "BigLogic", big)
        out = core.read_netlogic(cfg, "Alpha", "BigLogic")
        # Strip the untrusted wrapper before measuring raw content bytes.
        inner = re.sub(r'^<untrusted[^>]*>', '', out["content"])
        inner = re.sub(r'</untrusted>$', '', inner)
        assert len(inner.encode("utf-8")) <= core._NETLOGIC_MAX_BYTES

    def test_size_describes_whole_file_when_truncated(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        big = b"b" * (core._NETLOGIC_MAX_BYTES + 200)
        _make_netlogic(projects_root, "Alpha", "BigLogic", big)
        out = core.read_netlogic(cfg, "Alpha", "BigLogic")
        assert out["size"] == len(big)

    def test_nudge_present_when_truncated(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        """truncation_hint must mention start_line / end_line as the remedy."""
        big = b"c" * (core._NETLOGIC_MAX_BYTES + 1)
        _make_netlogic(projects_root, "Alpha", "BigLogic", big)
        out = core.read_netlogic(cfg, "Alpha", "BigLogic")
        hint = out.get("truncation_hint", "")
        assert "start_line" in hint or "end_line" in hint, (
            f"truncation_hint does not mention line ranges: {hint!r}"
        )

    def test_small_file_not_truncated(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        small = b"d" * (core._NETLOGIC_MAX_BYTES - 1)
        _make_netlogic(projects_root, "Alpha", "SmallLogic", small)
        out = core.read_netlogic(cfg, "Alpha", "SmallLogic")
        assert "truncated" not in out


# ---------------------------------------------------------------------------
# 7. Guard asymmetry — Studio running must NOT block read_netlogic
# ---------------------------------------------------------------------------


class TestGuardAsymmetry:
    """Pins the deliberate asymmetry: Studio running blocks read_file but NOT
    read_netlogic (C# source is on disk, not in Studio's in-memory model).

    Both assertions are in a single test so the asymmetry is indivisible.
    """

    def test_studio_running_blocks_read_file_but_not_read_netlogic(
        self, cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Studio open → read_netlogic succeeds (studio_open=True) AND
        core.read_file on the SAME project raises StudioOpen."""
        p = make_project(projects_root, "Alpha")

        # Write a regular project file for read_file to attempt.
        (p / "Nodes").mkdir(exist_ok=True)
        (p / "Nodes" / "UI.yaml").write_bytes(b"Name: UI\n")

        # Write a NetSolution .cs file for read_netlogic.
        net_dir = p / "ProjectFiles" / "NetSolution"
        net_dir.mkdir(parents=True, exist_ok=True)
        (net_dir / "Foo.cs").write_bytes(b"// foo\n")

        # Monkeypatch the process scanner to show Studio running.
        monkeypatch.setattr(studio_guard, "_scan", lambda: [_STUDIO_PROC])
        studio_guard.reset_cache()

        # --- Assertion A: read_netlogic SUCCEEDS (the asymmetry) ---
        out = core.read_netlogic(cfg, "Alpha", "Foo")
        assert out["studio_open"] is True, (
            "read_netlogic must report studio_open=True when Studio is running"
        )
        assert out["source"] == "disk"

        # --- Assertion B: read_file raises StudioOpen (the other side) ---
        with pytest.raises(core.StudioOpen, match="running"):
            core.read_file(cfg, "Alpha", "Nodes/UI.yaml")


# ---------------------------------------------------------------------------
# 8. VS Code attribution → EditorProjectOpen
# ---------------------------------------------------------------------------


class TestEditorAttribution:
    """VS Code with the project open attributably raises EditorProjectOpen."""

    def test_vscode_holding_project_raises_editor_project_open(
        self, cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = make_project(projects_root, "Alpha")
        net_dir = p / "ProjectFiles" / "NetSolution"
        net_dir.mkdir(parents=True, exist_ok=True)
        (net_dir / "Foo.cs").write_bytes(b"// foo\n")

        # VS Code with project path in its command line — attribution-detectable.
        vscode_proc = {
            "pid": 777,
            "name": "code.exe",
            "cmdline": ["code.exe", str(p)],
        }
        monkeypatch.setattr(studio_guard, "_scan", lambda: [vscode_proc])
        studio_guard.reset_cache()

        with pytest.raises(core.EditorProjectOpen):
            core.read_netlogic(cfg, "Alpha", "Foo")


# ---------------------------------------------------------------------------
# 9. Registry — EXPECTED_TOOLS membership, count, annotation, scope
# ---------------------------------------------------------------------------


class TestRegistry:
    """optix_read_netlogic is in EXPECTED_TOOLS, count == 44, readOnlyHint=True,
    TOOL_SCOPES == 'read'."""

    def test_tool_in_expected_tools(self) -> None:
        assert "optix_read_netlogic" in EXPECTED_TOOLS

    def test_expected_tools_count(self) -> None:
        assert len(EXPECTED_TOOLS) == 44, (
            f"Expected 44 tools, got {len(EXPECTED_TOOLS)}: {sorted(EXPECTED_TOOLS)}"
        )

    def test_read_netlogic_annotation_is_readonly(self, cfg: core.Config) -> None:
        mcp = make_mcp(cfg)
        tools_by_name = {t.name: t for t in _list_tools(mcp)}
        assert "optix_read_netlogic" in tools_by_name, "tool not registered"
        ann = tools_by_name["optix_read_netlogic"].annotations
        assert ann is not None, "optix_read_netlogic has no annotations"
        assert ann.readOnlyHint is True, (
            f"optix_read_netlogic.readOnlyHint should be True, got {ann.readOnlyHint}"
        )

    def test_tool_scope_is_read(self) -> None:
        assert auth.TOOL_SCOPES.get("optix_read_netlogic") == "read", (
            f"Expected scope 'read', got {auth.TOOL_SCOPES.get('optix_read_netlogic')!r}"
        )


# ---------------------------------------------------------------------------
# 10. Doc-count — README.md + docs/tool-reference.md match the DEFAULT surface
# ---------------------------------------------------------------------------


def _default_tool_count(cfg: core.Config) -> int:
    """Tools a stock install registers. The shared `cfg` fixture turns the
    deploy family on (enable_deploy=True) to exercise the full surface, so
    counting EXPECTED_TOOLS here documented 44 where users get 35."""
    import dataclasses
    lean = dataclasses.replace(cfg, enable_deploy=False)
    return len(make_mcp(lean)._tool_manager.list_tools())


class TestDocCount:
    """Both docs must advertise the tool count a default install registers,
    and the number of bundled playbooks that are actually tracked."""

    # Root of the project — two levels above service/tests/.
    _PROJECT_ROOT = Path(__file__).parent.parent.parent

    @staticmethod
    def _extract_count(path: Path, pattern: re.Pattern) -> int | None:
        """Return the first integer captured by *pattern* in *path*, or None."""
        text = path.read_text(encoding="utf-8")
        m = pattern.search(text)
        return int(m.group(1)) if m else None

    def test_readme_tool_count_matches_default_surface(self, cfg, monkeypatch) -> None:
        for var in ("FTXMCP_LEGACY_TOOLS", "FTXMCP_SKILLS", "FTXMCP_BRIDGE_PRIMITIVES"):
            monkeypatch.delenv(var, raising=False)
        readme = self._PROJECT_ROOT / "README.md"
        count = self._extract_count(readme, re.compile(r'(\d+)\s+tools?\s+by\s+default'))
        assert count is not None, f"Could not parse tool count from {readme}"
        expected = _default_tool_count(cfg)
        assert count == expected, f"README.md says {count} tools, default surface is {expected}"

    def test_tool_reference_count_matches_default_surface(self, cfg, monkeypatch) -> None:
        for var in ("FTXMCP_LEGACY_TOOLS", "FTXMCP_SKILLS", "FTXMCP_BRIDGE_PRIMITIVES"):
            monkeypatch.delenv(var, raising=False)
        ref = self._PROJECT_ROOT / "docs" / "tool-reference.md"
        pattern = re.compile(r'(\d+)\s+tools?\s+(?:registered\s+)?by\s+default')
        count = self._extract_count(ref, pattern)
        assert count is not None, f"Could not parse tool count from {ref}"
        expected = _default_tool_count(cfg)
        assert count == expected, f"docs/tool-reference.md says {count} tools, default surface is {expected}"

    def test_playbook_count_matches_tracked_skills(self) -> None:
        # Count from git, not the disk: a gitignored skill dir on a dev box
        # would otherwise inflate the number (it documented 24 where 23 ship).
        import subprocess
        out = subprocess.run(["git", "ls-files", "skills/*/SKILL.md"], cwd=self._PROJECT_ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
        pattern = re.compile(r'(\d+)\s+(?:bundled\s+)?authoring\s+playbooks', re.I)
        for doc in ("README.md", "docs/tool-reference.md"):
            text = (self._PROJECT_ROOT / doc).read_text(encoding="utf-8").replace("*", "")
            m = pattern.search(text)
            assert m, f"no playbook count in {doc}"
            assert int(m.group(1)) == len(out), f"{doc} says {m.group(1)} playbooks, {len(out)} tracked"


# ---------------------------------------------------------------------------
# 11. HTTP surface
# ---------------------------------------------------------------------------


class TestHttpSurface:
    """GET /projects/{project}/netlogic/{cls} — shape + traversal rejection."""

    def _make_netlogic(self, projects_root: Path, project: str, cls: str, content: bytes) -> Path:
        return _make_netlogic(projects_root, project, cls, content)

    def test_returns_payload_shape(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        """GET returns 200 with content, sha256, total_lines, source, studio_open."""
        self._make_netlogic(projects_root, "Alpha", "Foo", b"// hello\nclass Foo {}\n")
        client = TestClient(make_app(cfg))
        r = client.get("/projects/Alpha/netlogic/Foo")
        assert r.status_code == 200
        body = r.json()
        assert "content" in body
        assert "sha256" in body
        assert "total_lines" in body
        assert body["total_lines"] == 2
        assert body["source"] == "disk"
        assert "studio_open" in body
        assert body["content"].startswith('<untrusted source="read_netlogic">')

    def test_line_range_params_passed_through(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        content = b"L1\nL2\nL3\nL4\n"
        self._make_netlogic(projects_root, "Alpha", "Foo", content)
        client = TestClient(make_app(cfg))
        r = client.get("/projects/Alpha/netlogic/Foo?start_line=2&end_line=3")
        assert r.status_code == 200
        body = r.json()
        assert body["start_line"] == 2
        assert body["end_line"] == 3

    def test_traversal_attempt_returns_400_with_code(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        """A cls containing '..' triggers PathTraversal → 400 path_traversal_rejected."""
        make_project(projects_root, "Alpha")
        client = TestClient(make_app(cfg))
        # Use a cls with dotdot that passes through URL routing as one segment.
        r = client.get("/projects/Alpha/netlogic/..Foo")
        assert r.status_code == 400
        body = r.json()
        assert body.get("code") == "path_traversal_rejected", (
            f"Expected code='path_traversal_rejected', got {body!r}"
        )

    def test_unknown_project_returns_404(self, cfg: core.Config) -> None:
        client = TestClient(make_app(cfg))
        r = client.get("/projects/NoSuchProject/netlogic/Foo")
        assert r.status_code == 404
        assert r.json()["code"] == "project_not_found"

    def test_missing_class_returns_404(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        make_project(projects_root, "Alpha")
        client = TestClient(make_app(cfg))
        r = client.get("/projects/Alpha/netlogic/NoSuchClass")
        assert r.status_code == 404
        assert r.json()["code"] == "file_not_found"
