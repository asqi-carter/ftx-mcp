"""Identity-keyed bridge resolution (core.resolve_bridge): a call for project X
goes to the bridge whose /bridge/health names X by name or directory - never
by port order or first-found. All offline (core._bridge_http faked per port)."""
from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from service import core


@pytest.fixture(autouse=True)
def _clean(monkeypatch) -> None:
    core.reset_bridge_cache()
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    yield
    core.reset_bridge_cache()


def _fake(by_port: dict[int, dict]):
    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        port = urlparse(cfg.bridge_url).port
        if port not in by_port:
            raise core.BridgeUnavailable(f"nothing on {port}") from ConnectionRefusedError()
        if not path.startswith("/bridge/health"):
            return 200, json.dumps({"served_by": port}).encode()
        return 200, json.dumps({"model_loaded": True, "bridge_version": "1.0.9",
                                "port": port, **by_port[port]}).encode()
    return fake


def _h(name: str, pid: int, directory: str | None = None) -> dict:
    d = {"project": name, "project_name": name, "studio_pid": pid}
    if directory:
        d["project_dir"] = directory
    return d


def test_resolve_bridge_routes_by_project_name_not_port_order(multi_bridge_cfg, monkeypatch) -> None:
    # Beta is on the LOWEST port; asking for Alpha must not land on it.
    monkeypatch.setattr(core, "_bridge_http", _fake({
        8768: _h("Beta", 200), 8769: _h("Alpha", 100), 8770: _h("Gamma", 300)}))
    assert core.resolve_bridge(multi_bridge_cfg, "Alpha")["port"] == 8769
    assert core.resolve_bridge(multi_bridge_cfg, "Beta")["port"] == 8768
    assert core.resolve_bridge(multi_bridge_cfg, "Gamma")["port"] == 8770


def test_resolve_bridge_name_is_normalised(multi_bridge_cfg, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_http", _fake({8768: _h("Alpha", 1), 8769: _h("Beta", 2)}))
    assert core.resolve_bridge(multi_bridge_cfg, " alpha/ ")["port"] == 8768
    assert core.resolve_bridge(multi_bridge_cfg, "BETA")["port"] == 8769
    # X and X/HMI are one project named two ways
    assert core.resolve_bridge(multi_bridge_cfg, "Alpha/HMI")["port"] == 8768


def test_resolve_bridge_matches_by_directory(multi_bridge_cfg, monkeypatch, tmp_path) -> None:
    """Two Studios serve same-named projects; the project directory decides."""
    a = tmp_path / "siteA" / "Line"
    b = tmp_path / "siteB" / "Line"
    for d in (a, b):
        d.mkdir(parents=True)
        (d / "Line.optix").write_text("x")
    cfg = core.dataclasses.replace(multi_bridge_cfg, projects_root=tmp_path)
    monkeypatch.setattr(core, "_bridge_http", _fake({
        8768: _h("Line", 1, str(a).upper().replace("/", "\\")),
        8769: _h("Line", 2, str(b))}))
    assert core.resolve_bridge(cfg, "siteA/Line")["port"] == 8768
    assert core.resolve_bridge(cfg, "siteB/Line")["port"] == 8769


def test_no_bridge_for_project_lists_bridges_found(multi_bridge_cfg, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_http", _fake({
        8768: _h("Alpha", 1, "C:/p/Alpha"), 8769: _h("Beta", 2, "C:/p/Beta")}))
    with pytest.raises(core.NoBridgeForProject) as ei:
        core.resolve_bridge(multi_bridge_cfg, "Delta")
    e = ei.value
    assert e.code == "no_bridge_for_project"
    assert isinstance(e, core.BridgeUnavailable)  # file-fallback paths keep working
    assert "no_bridge_for_project" in str(e)
    assert "Alpha" in str(e) and "Beta" in str(e) and "8768" in str(e)
    assert {b["project"] for b in e.bridges} == {"Alpha", "Beta"}


def test_no_bridge_for_project_when_nothing_armed(multi_bridge_cfg, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_http", _fake({}))
    with pytest.raises(core.NoBridgeForProject) as ei:
        core.resolve_bridge(multi_bridge_cfg, "Alpha")
    assert ei.value.bridges == []


def test_ambiguous_bridge_raises_clear_error(multi_bridge_cfg, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_http", _fake({
        8768: _h("Alpha", 1), 8769: _h("Alpha", 2), 8770: _h("Beta", 3)}))
    with pytest.raises(core.AmbiguousBridgeForProject) as ei:
        core.resolve_bridge(multi_bridge_cfg, "Alpha")
    e = ei.value
    assert e.code == "ambiguous_bridge_for_project"
    assert not isinstance(e, core.BridgeUnavailable)
    assert "8768" in str(e) and "8769" in str(e) and "8770" not in str(e)
    # the wrappers every tool uses surface it rather than guessing
    with pytest.raises(core.AmbiguousBridgeForProject):
        core._require_bridge_for(multi_bridge_cfg, "Alpha")
    with pytest.raises(core.AmbiguousBridgeForProject):
        core._bridge_cfg_for(multi_bridge_cfg, "Alpha")


def test_junk_project_names_never_match(multi_bridge_cfg, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_http", _fake({8768: _h("Alpha", 1)}))
    for junk in ("", "../Alpha", "C:\\x\\Alpha"):
        with pytest.raises(core.NoBridgeForProject):
            core.resolve_bridge(multi_bridge_cfg, junk)


def test_every_wrapper_routes_through_resolve_bridge(multi_bridge_cfg, monkeypatch) -> None:
    calls: list[str] = []
    real = core.resolve_bridge

    def spy(cfg, project):
        calls.append(project)
        return real(cfg, project)

    monkeypatch.setattr(core, "_bridge_http", _fake({8768: _h("Beta", 2), 8769: _h("Alpha", 1)}))
    monkeypatch.setattr(core, "resolve_bridge", spy)
    assert core._bridge_cfg_for(multi_bridge_cfg, "Alpha").bridge_url.endswith(":8769")
    assert core._require_bridge_for(multi_bridge_cfg, "Alpha").bridge_url.endswith(":8769")
    assert core._bridge_write_guard(multi_bridge_cfg, "Alpha").bridge_url.endswith(":8769")
    assert core._find_bridge_for(multi_bridge_cfg, "Alpha")["port"] == 8769
    assert core._use_bridge_for(multi_bridge_cfg, "Alpha") is True
    assert core._use_bridge_for(multi_bridge_cfg, "Nope") is False
    assert len(calls) == 6


def test_describe_node_goes_to_the_named_projects_bridge(multi_bridge_cfg, monkeypatch) -> None:
    seen: list[int] = []

    def fake(cfg, path, method="GET", timeout=5.0, **_kw):
        port = urlparse(cfg.bridge_url).port
        if port not in (8768, 8769):
            raise core.BridgeUnavailable("x") from ConnectionRefusedError()
        if path.startswith("/bridge/health"):
            return 200, json.dumps({"model_loaded": True, "port": port,
                                    **_h("Beta" if port == 8768 else "Alpha", port)}).encode()
        seen.append(port)
        return 200, json.dumps({"path": "UI", "children": []}).encode()

    monkeypatch.setattr(core, "_bridge_http", fake)
    core.describe_node(multi_bridge_cfg, "Alpha", "UI")
    assert seen == [8769]


# ---- Module.Type BrowseName fallback (ftx-mcp-jku.4) -------------------------
# The C# bridge cannot run offline; these guard the source shape. The behaviour
# itself (driver/station created under both spellings) is a live-bench check:
#   pytest service/tests/test_bridge_live.py -k "driver_tag" (needs Studio).

def _cs_body(start: str) -> str:
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs").read_text(
        encoding="utf-8-sig")
    body = src[src.index(start):]
    return body[:body.index("\n    }\n")]


def test_module_type_catalog_miss_falls_back_to_browsename_in_module_namespace():
    """A pinned Module.Type that misses the module catalog (RAEtherNetIPDriver vs
    catalog field Driver) retries as a BrowseName lookup, same namespace only."""
    body = _cs_body("private static NodeId ResolveModuleTypeId")
    # catalog hit still returns first; the fallback only runs after the loop
    assert body.index("return nid;") < body.index("ResolveModuleTypeByBrowseName(ns, field)")
    assert "if (ns != null) return ResolveModuleTypeByBrowseName" in body
    fb = _cs_body("private static NodeId ResolveModuleTypeByBrowseName")
    assert "ModuleNamespaceIndex(ns)" in fb
    assert "FindTypeByBrowseName(searchRoot, browseName, nsIdx)" in fb
    assert "nsIdx <= 0" in fb  # never the OPC UA base namespace


def test_module_type_driver_and_station_spellings_share_one_resolver():
    """create_object, the station-kind probe and the variable-type guard all go
    through ResolveUiTypeId -> ResolveModuleTypeId, so dry run and apply agree
    for FTOptix.RAEtherNetIP.RAEtherNetIPDriver/.Driver and ...Station spellings."""
    body = _cs_body("private NodeId ResolveUiTypeId")
    assert "ResolveModuleTypeId(name)" in body


# --- #63: root name vs same-named sub copy, routed by resolved directory ----

def _root_and_sub(cfg, tmp_path):
    """projects_root/Demo and projects_root/Site/Demo, both with a .optix."""
    root_dir = tmp_path / "Demo"
    sub_dir = tmp_path / "Site" / "Demo"
    for d in (root_dir, sub_dir):
        d.mkdir(parents=True)
        (d / "Demo.optix").write_text("x")
    return core.dataclasses.replace(cfg, projects_root=tmp_path), root_dir, sub_dir


def test_root_name_selects_root_bridge_never_the_sub_bridge(multi_bridge_cfg, monkeypatch, tmp_path) -> None:
    """Sub copy sits on the LOWEST port; arm/edit/save on the root name must
    still land on the root bridge."""
    cfg, root_dir, sub_dir = _root_and_sub(multi_bridge_cfg, tmp_path)
    monkeypatch.setattr(core, "_bridge_http", _fake({
        8768: _h("Demo", 1, str(sub_dir)), 8769: _h("Demo", 2, str(root_dir))}))
    assert core.resolve_bridge(cfg, "Demo")["port"] == 8769
    assert core.resolve_bridge(cfg, "Site/Demo")["port"] == 8768
    assert core._require_bridge_for(cfg, "Demo").bridge_url.endswith(":8769")
    assert core._bridge_cfg_for(cfg, "Demo").bridge_url.endswith(":8769")


def test_root_name_never_falls_back_to_sub_bridge_by_name(multi_bridge_cfg, monkeypatch, tmp_path) -> None:
    """Only the sub copy is armed: the root name has NO bridge (it used to be
    claimed through the name fallback)."""
    cfg, root_dir, sub_dir = _root_and_sub(multi_bridge_cfg, tmp_path)
    monkeypatch.setattr(core, "_bridge_http", _fake({8768: _h("Demo", 1, str(sub_dir))}))
    with pytest.raises(core.NoBridgeForProject):
        core.resolve_bridge(cfg, "Demo")
    assert core._find_bridge_for(cfg, "Demo") is None
    assert core.resolve_bridge(cfg, "Site/Demo")["port"] == 8768


def test_bridge_health_without_project_dir_on_collision_is_unproven(multi_bridge_cfg, monkeypatch, tmp_path) -> None:
    cfg, root_dir, sub_dir = _root_and_sub(multi_bridge_cfg, tmp_path)
    monkeypatch.setattr(core, "_bridge_http", _fake({8768: _h("Demo", 1)}))
    # no known second copy of the name -> nothing to confuse it with
    monkeypatch.setattr(core, "_same_name_other_dirs", lambda c, d: [])
    assert core.resolve_bridge(cfg, "Demo")["port"] == 8768
    # a second same-named directory exists -> the dirless health fits both
    core.reset_bridge_cache()
    monkeypatch.setattr(core, "_same_name_other_dirs", lambda c, d: [sub_dir])
    with pytest.raises(core.NoBridgeForProject) as ei:
        core.resolve_bridge(cfg, "Demo")
    assert [b["port"] for b in ei.value.bridges] == [8768]  # reported, not claimed


def test_dirless_bridge_loses_to_dir_bearing_one_on_collision(multi_bridge_cfg, monkeypatch, tmp_path) -> None:
    cfg, root_dir, sub_dir = _root_and_sub(multi_bridge_cfg, tmp_path)
    monkeypatch.setattr(core, "_bridge_http", _fake({
        8768: _h("Demo", 1), 8769: _h("Demo", 2, str(root_dir))}))
    monkeypatch.setattr(core, "_same_name_other_dirs", lambda c, d: [sub_dir])
    assert core.resolve_bridge(cfg, "Demo")["port"] == 8769
