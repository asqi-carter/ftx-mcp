"""Tests for core.doctor — the layman dependency checklist."""
from __future__ import annotations

import dataclasses

import pytest

from service import core


@pytest.fixture(autouse=True)
def _no_bridge(monkeypatch):
    # deterministic: bridge unreachable (no live Studio in CI)
    core.reset_bridge_cache()

    def _down(cfg, path, method="GET", timeout=5.0):
        raise core.BridgeUnavailable("no bridge in test")

    monkeypatch.setattr(core, "_bridge_http", _down)
    yield
    core.reset_bridge_cache()


def test_ready_when_required_present(cfg):
    # cfg fixture gives a real studio_exe file + projects_root dir
    out = core.doctor(cfg)
    assert out["ready"] is True
    names = {c["name"] for c in out["checks"]}
    assert {"studio_exe", "projects_root", "bridge", "cdp", "deploy_username",
            "deploy_password", "deploy_thumbprint", "interactive_session"} <= names
    # every check carries a plain-english fix
    assert all(c["fix"] for c in out["checks"])


def test_not_ready_when_studio_missing(cfg):
    c = dataclasses.replace(cfg, studio_exe=cfg.studio_exe.parent / "nope.exe")
    out = core.doctor(c)
    assert out["ready"] is False
    studio = next(x for x in out["checks"] if x["name"] == "studio_exe")
    assert studio["ok"] is False and studio["required"] is True


def test_deploy_checks_reflect_config(cfg, monkeypatch):
    monkeypatch.delenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", raising=False)
    c = dataclasses.replace(cfg, deploy_username="admin", deploy_thumbprint="ABC")
    out = core.doctor(c)
    by = {x["name"]: x for x in out["checks"]}
    assert by["deploy_username"]["ok"] is True
    assert by["deploy_thumbprint"]["ok"] is True
    assert by["deploy_password"]["ok"] is False
    # deploy checks aren't required -> ready still True
    assert out["ready"] is True
