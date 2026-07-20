"""Tests for core.deploy_updatesvc — the UpdateSvc CLI 'deploy' verb path.

Offline: the subprocess runner is faked. Validates command construction, the
password/username config guards, and success/failure parsing. The real deploy
is validated against a live UpdateSvc.
"""
from __future__ import annotations

import dataclasses

import pytest

from service import core
from service.tests.conftest import FakeProc, make_fake_runner, make_project


@pytest.fixture(autouse=True)
def _fast_save(monkeypatch):
    """deploy_updatesvc now save_first by default; stub save so these command-
    construction tests stay fast and inspect the DEPLOY call, not the save."""
    monkeypatch.setattr(core, "save", lambda *a, **k: {"saved": True})


def _ready(cfg, projects_root, **over):
    make_project(projects_root, "Alpha")
    return dataclasses.replace(
        cfg, deploy_username="admin", deploy_ip_address="203.0.113.20",
        deploy_thumbprint="ABC", **over,
    )


def test_deploy_saves_first_by_default(cfg, projects_root, monkeypatch):
    c = _ready(cfg, projects_root)
    monkeypatch.setenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", "pw")
    calls = {"save": 0}
    monkeypatch.setattr(core, "save", lambda *a, **k: calls.__setitem__("save", calls["save"] + 1) or {"saved": True})
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "Deployment successfully completed"))
    out = core.deploy_updatesvc(c, "Alpha", runner=runner)
    assert calls["save"] == 1 and out["saved"] is True and out["deployed"] is True
    # opt-out skips the save
    calls["save"] = 0
    out2 = core.deploy_updatesvc(c, "Alpha", save_first=False, runner=runner)
    assert calls["save"] == 0 and out2["saved"] is None


def test_builds_command_and_parses_success(cfg, projects_root, monkeypatch):
    c = _ready(cfg, projects_root)
    monkeypatch.setenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", "pw")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "Deployment successfully completed"))
    out = core.deploy_updatesvc(c, "Alpha", run_after=True, runner=runner)
    assert out["deployed"] is True and out["run_after_deploy"] is True
    cmd = runner.calls[0][0]
    assert cmd[1] == "deploy"
    assert "--ip-address=203.0.113.20" in cmd
    assert "--username=admin" in cmd
    assert "--thumbprint=ABC" in cmd
    assert "--run-after-deploy" in cmd


def test_no_run_after_omits_flag(cfg, projects_root, monkeypatch):
    c = _ready(cfg, projects_root)
    monkeypatch.setenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", "pw")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "Deployment successfully completed"))
    core.deploy_updatesvc(c, "Alpha", runner=runner)
    assert "--run-after-deploy" not in runner.calls[0][0]


def test_disable_source_transfer_on_by_default(cfg, projects_root, monkeypatch):
    # cfg default is deploy_disable_source_transfer=True -> flag appended.
    c = _ready(cfg, projects_root)
    monkeypatch.setenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", "pw")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "Deployment successfully completed"))
    out = core.deploy_updatesvc(c, "Alpha", runner=runner)
    assert "--disable-source-project-transfer" in runner.calls[0][0]
    assert out["source_transfer_disabled"] is True


def test_disable_source_transfer_per_call_override(cfg, projects_root, monkeypatch):
    c = _ready(cfg, projects_root)
    monkeypatch.setenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", "pw")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "Deployment successfully completed"))
    out = core.deploy_updatesvc(
        c, "Alpha", disable_source_transfer=False, runner=runner)
    assert "--disable-source-project-transfer" not in runner.calls[0][0]
    assert out["source_transfer_disabled"] is False


def test_disable_source_transfer_cfg_off(cfg, projects_root, monkeypatch):
    c = _ready(cfg, projects_root, deploy_disable_source_transfer=False)
    monkeypatch.setenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", "pw")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "Deployment successfully completed"))
    core.deploy_updatesvc(c, "Alpha", runner=runner)
    assert "--disable-source-project-transfer" not in runner.calls[0][0]


def test_missing_username_raises(cfg, projects_root, monkeypatch):
    make_project(projects_root, "Alpha")
    monkeypatch.setenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", "pw")
    with pytest.raises(core.DeployConfigError):
        core.deploy_updatesvc(cfg, "Alpha")  # cfg.deploy_username is None by default


def test_missing_password_raises(cfg, projects_root, monkeypatch):
    c = _ready(cfg, projects_root)
    monkeypatch.delenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", raising=False)
    with pytest.raises(core.DeployConfigError):
        core.deploy_updatesvc(c, "Alpha")


def test_failure_parsed(cfg, projects_root, monkeypatch):
    c = _ready(cfg, projects_root)
    monkeypatch.setenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", "pw")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(1, "", "ERROR [a011b] destination"))
    out = core.deploy_updatesvc(c, "Alpha", runner=runner)
    assert out["deployed"] is False


# ---- build-race awareness ----

def test_deploy_build_race_warning_when_studio_open(cfg, projects_root, monkeypatch):
    c = _ready(cfg, projects_root)
    monkeypatch.setenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", "pw")
    monkeypatch.setattr(core.studio_guard, "studio_state",
                        lambda *a, **k: {"studio": {"running": True}})
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "Deployment successfully completed"))
    out = core.deploy_updatesvc(c, "Alpha", runner=runner)
    assert out["studio_running_locally"] is True
    assert out["build_race_warning"] and "race" in out["build_race_warning"]


def test_deploy_no_warning_when_studio_closed(cfg, projects_root, monkeypatch):
    c = _ready(cfg, projects_root)
    monkeypatch.setenv("OPTIX_STUDIO_DEPLOYMENT_PASSWORD", "pw")
    monkeypatch.setattr(core.studio_guard, "studio_state",
                        lambda *a, **k: {"studio": {"running": False}})
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "Deployment successfully completed"))
    out = core.deploy_updatesvc(c, "Alpha", runner=runner)
    assert out["studio_running_locally"] is False
    assert out["build_race_warning"] is None
