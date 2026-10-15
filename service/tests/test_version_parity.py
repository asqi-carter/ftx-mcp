"""Service version parity: ``service.__version__`` is the single source of truth.

pyproject.toml derives its version from it (dynamic), server.json must agree,
and it must equal the ``BridgeVersion`` constant in studio-bridge/StudioMCPBridge.cs.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from service import __version__

_ROOT = Path(__file__).resolve().parents[2]
_BRIDGE_CS = _ROOT / "studio-bridge" / "StudioMCPBridge.cs"


def _bridge_version() -> str:
    """Parse the ``BridgeVersion`` string literal from StudioMCPBridge.cs."""
    m = re.search(r'BridgeVersion\s*=\s*"([^"]+)"', _BRIDGE_CS.read_text(encoding="utf-8"))
    assert m, "BridgeVersion literal not found in StudioMCPBridge.cs"
    return m.group(1)


def test_service_version_matches_bridge_version() -> None:
    assert __version__ == _bridge_version()


def test_service_version_is_1_0_9() -> None:
    assert __version__ == "1.0.9"


def test_pyproject_version_derives_from_package() -> None:
    proj = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if "version" in proj["project"]:
        assert proj["project"]["version"] == __version__
    else:
        assert "version" in proj["project"].get("dynamic", [])
        assert proj["tool"]["setuptools"]["dynamic"]["version"] == {
            "attr": "service.__version__"
        }


def test_server_json_agrees() -> None:
    data = json.loads((_ROOT / "server.json").read_text(encoding="utf-8"))
    assert data["version"] == __version__
    assert all(p["version"] == __version__ for p in data["packages"])


def test_server_json_transport_matches_entry_point() -> None:
    """server.json declares the streamable-HTTP transport at the path main.py serves."""
    from urllib.parse import urlparse

    from service import main

    data = json.loads((_ROOT / "server.json").read_text(encoding="utf-8"))
    transport = data["packages"][0]["transport"]
    assert transport["type"] == "streamable-http"
    assert urlparse(transport["url"]).path == main.MCP_PATH
    assert transport == {"type": "streamable-http", "url": "http://127.0.0.1:8766/mcp"}
    assert "$schema" in data
    assert len(data["description"]) <= 100
