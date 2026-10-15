"""Build hook: bundle skills/ and the Studio bridge source into the wheel.

A checkout reads ``skills/`` and ``studio-bridge/`` straight from the repo, but
a wheel only contains the ``service`` package. At build time ONLY, and into the
build directory (never the source tree), ``build_py`` copies:

    skills/<name>/SKILL.md                -> service/_bundled/skills/<name>/SKILL.md
    studio-bridge/StudioMCPBridge.cs      -> service/_bundled/studio-bridge/StudioMCPBridge.cs

``service.core._skills_dir()`` / ``_shipped_bridge_cs()`` fall back to that
``_bundled`` location when no checkout layout is present. Everything else
(metadata, dependencies, package-data) lives in pyproject.toml.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent
BUNDLE_SUBDIR = Path("service") / "_bundled"
BRIDGE_CS = Path("studio-bridge") / "StudioMCPBridge.cs"


def bundle_files(root: Path = ROOT) -> "list[tuple[Path, Path]]":
    """(source, destination-relative-to-service/_bundled) pairs to bundle."""
    pairs: "list[tuple[Path, Path]]" = []
    for skill_md in sorted(root.glob("skills/*/SKILL.md")):
        pairs.append((skill_md, skill_md.relative_to(root)))
    bridge = root / BRIDGE_CS
    if bridge.is_file():
        pairs.append((bridge, BRIDGE_CS))
    return pairs


class build_py_bundled(build_py):
    """build_py that also copies the bundled resources into the build dir."""

    def run(self) -> None:
        super().run()
        if self.dry_run:
            return
        dest_root = Path(self.build_lib) / BUNDLE_SUBDIR
        for src, rel in bundle_files():
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)


if __name__ == "__main__":
    setup(cmdclass={"build_py": build_py_bundled})
