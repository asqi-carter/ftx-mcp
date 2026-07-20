"""HTTP-surface tests for the v1.0 capability endpoints (doctor / save /
UpdateSvc deploy / serve / bridge writes). Black-box: the core layer is mocked,
verifying the route wiring + request models.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from service import core
from service.http_app import make_app
from service.tests.conftest import make_project


def _client(cfg):
    return TestClient(make_app(cfg))


def test_doctor_endpoint(cfg, monkeypatch):
    monkeypatch.setattr(core, "doctor", lambda c: {"ready": True, "checks": []})
    r = _client(cfg).get("/doctor")
    assert r.status_code == 200 and r.json()["ready"] is True


def test_save_endpoint(cfg, projects_root, monkeypatch):
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "save", lambda c, p: {"saved": True, "project": p})
    r = _client(cfg).post("/projects/Alpha/save")
    assert r.status_code == 200 and r.json()["saved"] is True


def test_deploy_updatesvc_endpoint(cfg, projects_root, monkeypatch):
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(
        core, "deploy_updatesvc",
        lambda c, p, run_after=False, disable_source_transfer=None: {
            "deployed": True, "run_after_deploy": run_after,
            "disable_source_transfer": disable_source_transfer})
    r = _client(cfg).post("/projects/Alpha/deploy/updatesvc?run_after=true")
    assert r.status_code == 200 and r.json()["run_after_deploy"] is True
    # default: endpoint passes None (core resolves to the cfg default)
    assert r.json()["disable_source_transfer"] is None
    r2 = _client(cfg).post(
        "/projects/Alpha/deploy/updatesvc?run_after=true&disable_source_transfer=false")
    assert r2.json()["disable_source_transfer"] is False


def test_bridge_widget_endpoint(cfg, projects_root, monkeypatch):
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "bridge_create_widget",
                        lambda c, p, s, n, t: {"ok": True, "created_path": f"{s}/{n}", "type": t})
    r = _client(cfg).post("/projects/Alpha/bridge/widget",
                          json={"screen": "UI/MainWindow", "name": "L1"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_bridge_set_property_endpoint(cfg, projects_root, monkeypatch):
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "bridge_set_property",
                        lambda c, p, np, n, v, locale="en-US": {"ok": True, "via": "materialized"})
    r = _client(cfg).post("/projects/Alpha/bridge/set-property",
                          json={"node_path": "UI/MainWindow/L1", "name": "Text", "value": "Hi"})
    assert r.status_code == 200 and r.json()["via"] == "materialized"


def test_bridge_bind_endpoint(cfg, projects_root, monkeypatch):
    make_project(projects_root, "Alpha")
    monkeypatch.setattr(core, "bridge_bind_property",
                        lambda c, p, np, n, src, mode="Read", raw=None: {"ok": True, "via": "dynamiclink", "mode": mode})
    r = _client(cfg).post("/projects/Alpha/bridge/bind",
                          json={"node_path": "UI/MainWindow/L1", "name": "Text",
                                "source_path": "Model/V1", "mode": "ReadWrite"})
    assert r.status_code == 200 and r.json()["mode"] == "ReadWrite"
