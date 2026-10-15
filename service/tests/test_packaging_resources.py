"""Wheel packaging: skills, bridge source and dashboard ship with the package.

Mock-free where possible: the "installed" layout is a real copy of the
``service`` package under a tmp ``site-packages`` (no sibling ``skills/`` or
``studio-bridge/``) plus a ``service/_bundled`` tree laid out the way
``setup.py`` produces it, resolved in a subprocess.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service import __version__, core, native_capture
from service.http_app import make_app

REPO = Path(__file__).resolve().parents[2]
SERVICE = REPO / "service"


def _load_setup_py():
    """Import setup.py as a module. If setuptools is not installed in the test
    interpreter, stub just enough of it for the module-level imports."""
    stubbed = False
    try:
        import setuptools  # noqa: F401
    except ImportError:
        stubbed = True
        import types
        st = types.ModuleType("setuptools")
        st.setup = lambda **kw: None
        cmd = types.ModuleType("setuptools.command")
        bp = types.ModuleType("setuptools.command.build_py")
        bp.build_py = type("build_py", (), {})
        sys.modules.update({"setuptools": st, "setuptools.command": cmd,
                            "setuptools.command.build_py": bp})
    spec = importlib.util.spec_from_file_location("ftx_setup_py", REPO / "setup.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # __main__ guard keeps setup() from running
    finally:
        if stubbed:
            for k in ("setuptools", "setuptools.command", "setuptools.command.build_py"):
                sys.modules.pop(k, None)
    return mod


def _make_site_packages(tmp_path: Path) -> Path:
    """tmp site-packages with the service package and a setup.py-style bundle."""
    sp = tmp_path / "site-packages"
    shutil.copytree(
        SERVICE, sp / "service",
        ignore=shutil.ignore_patterns("tests", "__pycache__", "_bundled"))
    setup_py = _load_setup_py()
    for src, rel in setup_py.bundle_files(REPO):
        dest = sp / "service" / "_bundled" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    return sp


def _resolve_in(sp: Path, expr: str, env_extra: dict | None = None) -> str:
    import os
    env = {**os.environ, "PYTHONPATH": str(sp)}
    env.pop("OPTIX_SKILLS_DIR", None)
    env.update(env_extra or {})
    out = subprocess.run(
        [sys.executable, "-c", f"from service import core; print({expr})"],
        capture_output=True, text=True, env=env, cwd=str(sp.parent), check=True)
    return out.stdout.strip()


def test_skills_dir_resolves_to_bundle_in_site_packages(tmp_path: Path) -> None:
    sp = _make_site_packages(tmp_path)
    assert not (sp / "skills").exists()
    got = Path(_resolve_in(sp, "core._skills_dir()"))
    assert got == (sp / "service" / "_bundled" / "skills").resolve()
    assert (got / "optix-verify-loop" / "SKILL.md").is_file()
    # the skill tools work off that directory
    listed = _resolve_in(sp, "len(core.list_skills(None)['skills'])")
    assert int(listed) == len(list((REPO / "skills").glob("*/SKILL.md")))


def test_skills_dir_env_override_wins_over_bundle(tmp_path: Path) -> None:
    sp = _make_site_packages(tmp_path)
    got = _resolve_in(sp, "core._skills_dir()", {"OPTIX_SKILLS_DIR": str(tmp_path / "x")})
    assert Path(got) == tmp_path / "x"


def test_skills_dir_prefers_repo_in_checkout() -> None:
    assert core._skills_dir() == SERVICE.parent / "skills"


def test_shipped_bridge_cs_resolves_to_bundle_in_site_packages(tmp_path: Path) -> None:
    sp = _make_site_packages(tmp_path)
    got = Path(_resolve_in(sp, "core._shipped_bridge_cs()"))
    assert got == (sp / "service" / "_bundled" / "studio-bridge" / "StudioMCPBridge.cs").resolve()
    assert got.read_bytes() == (REPO / "studio-bridge" / "StudioMCPBridge.cs").read_bytes()


def test_shipped_bridge_cs_returns_repo_copy_when_present() -> None:
    assert core._shipped_bridge_cs() == REPO / "studio-bridge" / "StudioMCPBridge.cs"


def test_shipped_bridge_cs_none_when_absent(tmp_path: Path) -> None:
    sp = tmp_path / "sp"
    shutil.copytree(SERVICE, sp / "service",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", "_bundled"))
    assert _resolve_in(sp, "core._shipped_bridge_cs()") == "None"


def test_manifest_in_lists_resources() -> None:
    text = (REPO / "MANIFEST.in").read_text(encoding="utf-8")
    assert "skills" in text
    assert "studio-bridge/StudioMCPBridge.cs" in text
    assert "service/static" in text


def test_pyproject_package_data_and_pillow() -> None:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    pkg = data["tool"]["setuptools"]["package-data"]["service"]
    assert "static/*" in pkg and "_bundled/**/*" in pkg
    deps = data["project"]["dependencies"]
    assert any(d.replace(" ", "").lower().startswith("pillow>=10") for d in deps)
    assert data["project"]["optional-dependencies"]["visual"] == []


def test_setup_py_copy_list_covers_every_skill_and_bridge() -> None:
    mod = _load_setup_py()
    pairs = mod.bundle_files(REPO)
    rels = {rel.as_posix() for _, rel in pairs}
    real_skills = {p.relative_to(REPO).as_posix() for p in (REPO / "skills").glob("*/SKILL.md")}
    assert real_skills, "checkout has no skills"
    assert real_skills <= rels
    assert "studio-bridge/StudioMCPBridge.cs" in rels
    assert all(src.is_file() for src, _ in pairs)


def test_build_py_copies_into_build_dir_not_source_tree(tmp_path: Path) -> None:
    """Run the real build_py in a copy of the repo inputs; the bundle must land
    in the build dir and the (copy of the) source tree must stay clean."""
    work = tmp_path / "work"
    work.mkdir()
    for name in ("setup.py", "pyproject.toml", "README.md", "LICENSE", "NOTICE", "MANIFEST.in"):
        shutil.copyfile(REPO / name, work / name)
    shutil.copytree(SERVICE, work / "service",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", "_bundled"))
    shutil.copytree(REPO / "skills", work / "skills")
    shutil.copytree(REPO / "studio-bridge", work / "studio-bridge")
    build = work / "build_out"
    r = subprocess.run(
        [sys.executable, "setup.py", "-q", "build_py", "--build-lib", str(build)],
        cwd=work, capture_output=True, text=True)
    if r.returncode != 0 and "No module named" in (r.stderr + r.stdout):
        pytest.skip("setuptools unavailable in this interpreter")
    assert r.returncode == 0, r.stderr + r.stdout
    assert (build / "service" / "_bundled" / "studio-bridge" / "StudioMCPBridge.cs").is_file()
    assert (build / "service" / "_bundled" / "skills" / "optix-verify-loop" / "SKILL.md").is_file()
    assert (build / "service" / "static" / "dashboard.html").is_file()
    assert not (work / "service" / "_bundled").exists()


def test_gitignore_and_checkout_have_no_bundle() -> None:
    assert "service/_bundled/" in (REPO / ".gitignore").read_text(encoding="utf-8")
    assert not (SERVICE / "_bundled").exists()


def test_bridge_source_endpoint_serves_cs_as_text_plain(cfg) -> None:
    client = TestClient(make_app(cfg))
    r = client.get("/bridge-source")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["x-ftx-mcp-version"] == __version__
    shipped = core._shipped_bridge_cs().read_bytes()
    assert core.bridge_source_hash(r.content) == core.bridge_source_hash(shipped)


def test_bridge_source_endpoint_404_when_not_packaged(cfg, monkeypatch) -> None:
    monkeypatch.setattr(core, "_shipped_bridge_cs", lambda: None)
    r = TestClient(make_app(cfg)).get("/bridge-source")
    assert r.status_code == 404
    assert r.json()["code"] == "bridge_source_missing"


def test_capture_window_pillow_missing_is_structured(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "PIL", None)  # makes `import PIL` raise ImportError
    monkeypatch.delitem(sys.modules, "PIL.Image", raising=False)
    with pytest.raises(native_capture.NativeCaptureError) as ei:
        native_capture.capture_window(1)
    assert ei.value.reason == "pillow_missing"
    assert ei.value.hint == "pip install Pillow"


def test_native_screenshot_reports_pillow_missing(cfg, monkeypatch) -> None:
    def no_pil(_hwnd):
        raise native_capture.NativeCaptureError(
            "pillow_missing", "Pillow is not installed", hint="pip install Pillow")
    out = core.native_screenshot_runtime(
        cfg, pids_fn=lambda p: [1], windows_fn=lambda pids: [{"hwnd": 1, "pid": 1, "title": "t"}],
        capture_fn=no_pil)
    assert out["state"] == "failed"
    assert out["error"] == "pillow_missing"
    assert out["hint"] == "pip install Pillow"


def test_pillow_missing_error_shape() -> None:
    e = core.pillow_missing_error()
    assert e["error"] == "pillow_missing" and e["hint"] == "pip install Pillow"
    json.dumps(e)
