"""/health is a fast liveness probe: it never probes bridges (ftx-mcp-h4a.1).

The bridge probe functions are mocked to sleep; /health must still answer in
well under 200 ms, with no bridge armed and with stale registry files present.
``bridge_source`` comes from the last completed scan only while it is fresher
than 30 s, otherwise ``unknown``; ``bridge_source_age_s`` reports the age.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from service import core
from service.tests.conftest import make_project
from service.tests.test_bridge_sync import SHIPPED, _armed_http, _put

SLEEP_S = 2.0


@pytest.fixture(autouse=True)
def _clean_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


@pytest.fixture
def sleepy_probes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every probe entry point sleeps; records any call."""
    calls: list[str] = []

    def slow(name):
        def _f(*a, **k):
            calls.append(name)
            time.sleep(SLEEP_S)
            raise core.BridgeUnavailable("slow")
        return _f

    for name in ("_bridge_http", "_bridge_get_json", "_bridge_health_at",
                 "_probe_ports", "_scan_bridge_ports", "list_bridges"):
        monkeypatch.setattr(core, name, slow(name))
    return calls


def _stale_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    local = tmp_path / "localappdata"
    reg = local / "ftx-mcp" / "bridges"
    reg.mkdir(parents=True)
    for port in (8768, 8769, 49152):
        (reg / f"{port}.json").write_text(json.dumps({"port": port}))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return reg


def test_health_fast_with_no_bridge_armed(cfg, sleepy_probes) -> None:
    t0 = time.perf_counter()
    out = core.health(cfg)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.2, elapsed
    assert sleepy_probes == []
    assert out["ok"] is True
    assert out["bridge_source"] == "unknown"
    assert out["bridge_source_age_s"] is None


def test_health_fast_with_stale_registry_files(
        cfg, tmp_path, monkeypatch, sleepy_probes) -> None:
    reg = _stale_registry(tmp_path, monkeypatch)
    t0 = time.perf_counter()
    out = core.health(cfg)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.2, elapsed
    assert sleepy_probes == []
    assert out["bridge_source"] == "unknown"
    # health neither probes nor cleans up registry entries
    assert len(list(reg.glob("*.json"))) == 3


def test_http_health_route_fast_with_stale_registry(
        cfg, tmp_path, monkeypatch, sleepy_probes) -> None:
    from fastapi.testclient import TestClient
    from service.http_app import make_app
    _stale_registry(tmp_path, monkeypatch)
    client = TestClient(make_app(cfg))
    t0 = time.perf_counter()
    r = client.get("/health")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 0.2, elapsed
    assert r.json()["bridge_source"] == "unknown"


def test_health_reports_cached_bridge_source_when_fresh(
        cfg, projects_root, monkeypatch) -> None:
    proj = make_project(projects_root)
    _put(proj, SHIPPED)
    monkeypatch.setattr(core, "_bridge_http", _armed_http(proj))
    assert len(core.list_bridges(cfg)) == 1  # a completed scan
    # From here on any probe would be a bug.
    def boom(*a, **k):
        raise AssertionError("health must not probe")
    monkeypatch.setattr(core, "_bridge_http", boom)
    monkeypatch.setattr(core, "_bridge_health_at", boom)
    out = core.health(cfg)
    assert out["bridge_source"] == "current"
    assert 0 <= out["bridge_source_age_s"] < 5


def test_health_bridge_source_unknown_when_cached_scan_older_than_30s(
        cfg, projects_root, monkeypatch) -> None:
    proj = make_project(projects_root)
    _put(proj, SHIPPED)
    monkeypatch.setattr(core, "_bridge_http", _armed_http(proj))
    core.list_bridges(cfg)
    assert core.health(cfg)["bridge_source"] == "current"
    real = time.time()
    monkeypatch.setattr(core.time, "time", lambda: real + 31.0)
    out = core.health(cfg)
    assert out["bridge_source"] == "unknown"
    assert 30 < out["bridge_source_age_s"] < 40


def test_optix_bridge_status_still_probes(cfg, projects_root, monkeypatch) -> None:
    from service.tests.test_bridge_sync import _status
    proj = make_project(projects_root)
    _put(proj, b"// old bridge\n")
    calls: list[str] = []
    inner = _armed_http(proj)

    def spy(cfg_, path, *a, **k):
        calls.append(path)
        return inner(cfg_, path, *a, **k)
    monkeypatch.setattr(core, "_bridge_http", spy)
    out = _status(cfg)
    assert calls, "optix_bridge_status must probe bridges"
    assert out["bridge_source"] == "stale"
    assert core.health(cfg)["service_path"]


def test_health_keeps_existing_fields(cfg) -> None:
    out = core.health(cfg)
    for key in ("ok", "service_path", "bridge_source", "version", "projects_root",
                "projects_root_exists", "studio_exe", "studio_exe_exists",
                "runtime_dir", "runtime_dir_exists", "runtime_launcher",
                "runtime_test_port", "interactive_session", "bind"):
        assert key in out
    assert "bridge_source_age_s" in out
