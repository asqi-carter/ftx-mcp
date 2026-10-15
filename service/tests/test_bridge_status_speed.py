"""bridge_status: scoped = one fast health call; unscoped = parallel probes (PRD 0052 F, #74)."""
from __future__ import annotations

import json
import time
from urllib.parse import urlparse

import pytest

from service import core
from service.tests.conftest import make_project


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


def _registry(tmp_path, entries: dict[int, dict]) -> None:
    d = tmp_path / "ftx-mcp" / "bridges"
    d.mkdir(parents=True)
    for port, payload in entries.items():
        (d / f"{port}.json").write_text(json.dumps({"port": port, **payload}))


def _health(name: str, port: int, pdir: str = "") -> dict:
    return {"project": name, "project_name": name, "bridge_version": "1.0.9",
            "model_loaded": True, "port": port, "project_dir": pdir}


def test_scoped_bridge_status_makes_one_health_call(
    multi_bridge_cfg, projects_root, monkeypatch, tmp_path
) -> None:
    proj = make_project(projects_root, "Alpha")
    make_project(projects_root, "Beta")
    _registry(tmp_path, {
        8769: {"project": "Alpha", "project_path": str(proj / "Alpha.optix")},
        8770: {"project": "Beta", "project_path": str(projects_root / "Beta" / "Beta.optix")},
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    calls: list[tuple[int, float]] = []

    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        calls.append((urlparse(cfg.bridge_url).port, timeout))
        return 200, json.dumps(_health("Alpha", 8769, str(proj))).encode()

    monkeypatch.setattr(core, "_bridge_http", fake)
    out = core.bridge_status(multi_bridge_cfg, "Alpha")
    assert calls == [(8769, 0.5)]
    assert out["count"] == 1 and out["project"] == "Alpha" and out["port"] == 8769


def test_scoped_bridge_status_unreachable_does_not_retry(
    multi_bridge_cfg, projects_root, monkeypatch, tmp_path
) -> None:
    proj = make_project(projects_root, "Alpha")
    _registry(tmp_path, {8769: {"project": "Alpha", "project_path": str(proj / "Alpha.optix")}})
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    calls: list[int] = []

    def fake(cfg, path, **_kw):
        calls.append(1)
        raise core.BridgeUnavailable("timed out")

    monkeypatch.setattr(core, "_bridge_http", fake)
    out = core.bridge_status(multi_bridge_cfg, "Alpha")
    assert len(calls) == 1 and out["count"] == 0 and out["available"] is False


def test_unscoped_bridge_status_probes_in_parallel(
    multi_bridge_cfg, monkeypatch, tmp_path
) -> None:
    ports = (8768, 8769, 8770)
    _registry(tmp_path, {p: {"project": f"P{p}"} for p in ports})
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    delay = 0.3

    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        time.sleep(delay)
        port = urlparse(cfg.bridge_url).port
        if port not in ports:
            raise core.BridgeUnavailable("nothing listening")
        return 200, json.dumps(_health(f"P{port}", port)).encode()

    monkeypatch.setattr(core, "_bridge_http", fake)
    t0 = time.perf_counter()
    out = core.bridge_status(multi_bridge_cfg)
    elapsed = time.perf_counter() - t0
    assert out["count"] == 3
    assert elapsed < 1.5 * delay, elapsed
