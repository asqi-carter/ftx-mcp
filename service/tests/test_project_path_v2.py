"""project_path_v2: GetProjectPath reports the real project location or null,
never Studio's working directory (typically C:\\WINDOWS\\system32)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from service import core

_CS = Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs"


def _src() -> str:
    return _CS.read_text(encoding="utf-8", errors="ignore")


def _get_project_path_body() -> str:
    text = _src()
    start = text.index("private static string GetProjectPath()")
    end = text.index("\n    private static string ProjectDirFromResourceUri", start)
    return text[start:end]


def test_get_project_path_does_not_use_current_directory_fallback():
    body = _get_project_path_body()
    assert "Environment.CurrentDirectory" not in body
    assert "return null;" in body
    # The only live use of the cwd must be gone from the whole bridge.
    code = "\n".join(l for l in _src().splitlines() if not l.strip().startswith("//"))
    assert "Environment.CurrentDirectory" not in code


def test_bridge_source_no_longer_uses_current_directory_as_get_project_path_fallback():
    test_get_project_path_does_not_use_current_directory_fallback()


def test_health_advertises_project_path_v2_and_version_stays_1_0_9():
    text = _src()
    assert re.search(r'BridgeVersion\s*=\s*"1\.0\.9"', text)
    assert '\\"project_path_v2\\"' in text


@pytest.fixture(autouse=True)
def _clear_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


@pytest.mark.parametrize("value", [
    r"C:\WINDOWS\system32", r"C:\Windows\System32", "c:/windows/system32",
    r"C:\WINDOWS\system32\\", r"C:\Windows", None, "", "  ",
])
def test_windows_directory_project_path_is_unknown(value):
    assert core._normalize_project_path(value) is None


def test_real_project_paths_are_kept():
    p = r"C:\Users\me\Desktop\Line4_HMI\HMI"
    assert core._normalize_project_path(p) == p
    q = r"C:\Windowsish\proj"
    assert core._normalize_project_path(q) == q


def test_health_reports_system32_project_path_as_unknown(cfg, monkeypatch):
    payload = {"project": "Alpha", "bridge_version": "1.0.8", "model_loaded": True,
               "port": 8768, "project_path": r"C:\WINDOWS\system32", "pid": 1}

    def fake(c, path, method="GET", timeout=5.0, **_kw):
        return 200, json.dumps(payload).encode()

    monkeypatch.setattr(core, "_bridge_http", fake)
    state = core._bridge_health_at(cfg, 8768, force=True)
    assert state["available"] is True
    assert state["project_path"] is None


def test_service_reports_system32_project_path_as_unknown(cfg, monkeypatch):
    test_health_reports_system32_project_path_as_unknown(cfg, monkeypatch)


# ---- project_path names the folder holding the .optix ----------------------

def _write_registry(monkeypatch, tmp_path, project, project_path):
    reg = tmp_path / "ftx-mcp" / "bridges"
    reg.mkdir(parents=True)
    (reg / "8768.json").write_text(
        json.dumps({"project": project, "project_path": str(project_path)}),
        encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))


def test_registry_open_candidates_accepts_project_folder_path(tmp_path, monkeypatch):
    proj = tmp_path / "Alpha"
    (proj / "ProjectFiles").mkdir(parents=True)
    (proj / "Alpha.optix").write_text("x")
    _write_registry(monkeypatch, tmp_path, "Alpha", proj)
    out = core._registry_open_candidates()
    assert [c["dir"] for c in out] == [proj.resolve()]
    assert out[0]["optix"].name == "Alpha.optix"


def test_registry_open_candidates_accepts_legacy_projectfiles_path(tmp_path, monkeypatch):
    proj = tmp_path / "Alpha"
    (proj / "ProjectFiles").mkdir(parents=True)
    (proj / "Alpha.optix").write_text("x")
    _write_registry(monkeypatch, tmp_path, "Alpha", proj / "ProjectFiles")
    out = core._registry_open_candidates()
    assert [c["dir"] for c in out] == [proj.resolve()]
    assert "alpha" in out[0]["names"]


def test_registry_open_candidates_reads_bom_prefixed_file(tmp_path, monkeypatch):
    """Bridges that wrote the registry with Encoding.UTF8 emit a BOM (#28)."""
    proj = tmp_path / "Alpha"
    (proj / "ProjectFiles").mkdir(parents=True)
    (proj / "Alpha.optix").write_text("x")
    reg = tmp_path / "ftx-mcp" / "bridges"
    reg.mkdir(parents=True)
    (reg / "8768.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps(
            {"project": "Alpha", "project_path": str(proj)}).encode("utf-8"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    out = core._registry_open_candidates()
    assert [c["project"] for c in out] == ["Alpha"]
    assert [c["dir"] for c in out] == [proj.resolve()]


def test_bridge_source_writes_registry_without_bom():
    src = (Path(__file__).resolve().parents[2] / "studio-bridge"
           / "StudioMCPBridge.cs").read_text(encoding="utf-8-sig")
    assert "WriteAllText(RegistryEntryPath(port), json, new UTF8Encoding(false))" in src


def test_projectfiles_path_with_ambiguous_parent_is_rejected(tmp_path, monkeypatch):
    proj = tmp_path / "Alpha"
    (proj / "ProjectFiles").mkdir(parents=True)
    (proj / "A.optix").write_text("x")
    (proj / "B.optix").write_text("x")
    _write_registry(monkeypatch, tmp_path, "Alpha", proj / "ProjectFiles")
    assert core._registry_open_candidates() == []


def test_bridge_source_normalizes_projectfiles_to_parent():
    text = _src()
    assert "NormalizeToProjectFolder(GetProjectPathRaw())" in text
    assert '"ProjectFiles"' in text
