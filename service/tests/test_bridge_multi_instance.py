"""Tests for v1.0.7 multi-instance bridge support: several Studio instances,
each with its own armed bridge on its own port, discovered/routed by
service.core's port-range scan.

All offline: core._bridge_http is monkeypatched per-port (keyed off
cfg.bridge_url), so these validate registry/routing behavior without a live
Studio. Uses the `multi_bridge_cfg` fixture (bridge_url_pinned=False,
range=8768..8771) — the plain `cfg` fixture stays pinned to the single legacy
bridge path so the rest of the suite is unaffected (see conftest.py).
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from service import core


@pytest.fixture(autouse=True)
def _clear_bridge_cache(monkeypatch) -> None:
    core.reset_bridge_cache()
    # _bridge_health_at used to do a fast raw-socket
    # pre-check (_tcp_probe) before the mockable HTTP layer, to skip
    # obviously-dead ports in production. That pre-check was removed —
    # against a real listener it could race the C# bridge's accept/read/write
    # cycle and cause "connection aborted" errors on the live, armed bridge
    # (see core.py's _bridge_health_at comment). Every port now goes straight
    # through the mockable HTTP layer, so there's nothing left to force
    # through here; keep the sleep patch so the retry loop (still present for
    # transient transport failures) doesn't slow these tests down.
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    yield
    core.reset_bridge_cache()


def _multi_bridge(by_port: dict[int, dict]):
    """Fake core._bridge_http: routes /bridge/health by the PORT cfg.bridge_url
    points at, so different simulated Studio instances answer differently. A
    port with no entry raises BridgeUnavailable (nothing listening there)."""
    def fake(cfg: core.Config, path: str, method: str = "GET", timeout: float = 5.0, **_kwargs):
        port = urlparse(cfg.bridge_url).port
        if port not in by_port:
            raise core.BridgeUnavailable(f"nothing listening on {port}")
        if not path.startswith("/bridge/health"):
            return 404, b'{"error":{"code":"not_found"}}'
        return 200, json.dumps(by_port[port]).encode()
    return fake


_THREE_ARMED = {
    8768: {"project": "Alpha", "bridge_version": "1.0.7", "model_loaded": True, "port": 8768},
    8769: {"project": "Beta", "bridge_version": "1.0.7", "model_loaded": True, "port": 8769},
    8770: {"project": "Gamma", "bridge_version": "1.0.7", "model_loaded": True, "port": 8770},
}


def test_list_bridges_finds_every_armed_port(multi_bridge_cfg, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(_THREE_ARMED))
    bridges = core.list_bridges(multi_bridge_cfg)
    assert {b["project"] for b in bridges} == {"Alpha", "Beta", "Gamma"}
    assert {b["port"] for b in bridges} == {8768, 8769, 8770}
    assert all(b["available"] for b in bridges)


def test_list_bridges_empty_when_nothing_armed(multi_bridge_cfg, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge({}))
    assert core.list_bridges(multi_bridge_cfg) == []


def test_use_bridge_for_routes_to_the_right_port_among_several(
    multi_bridge_cfg, monkeypatch
) -> None:
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(_THREE_ARMED))
    assert core._use_bridge_for(multi_bridge_cfg, "Alpha") is True
    assert core._use_bridge_for(multi_bridge_cfg, "Beta") is True
    assert core._use_bridge_for(multi_bridge_cfg, "Gamma") is True
    assert core._use_bridge_for(multi_bridge_cfg, "Delta") is False


def test_bridge_cfg_for_rebinds_to_the_serving_projects_own_port(
    multi_bridge_cfg, monkeypatch
) -> None:
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(_THREE_ARMED))
    bcfg = core._bridge_cfg_for(multi_bridge_cfg, "Beta")
    assert bcfg is not None
    assert urlparse(bcfg.bridge_url).port == 8769
    # A downstream _bridge_get_json(bcfg, ...) call now targets Beta's own
    # bridge, not whichever port happens to be first in the range.
    status, data = core._bridge_get_json(bcfg, "/bridge/health")
    assert data["project"] == "Beta"


def test_bridge_cfg_for_none_when_project_not_served_by_any_bridge(
    multi_bridge_cfg, monkeypatch
) -> None:
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(_THREE_ARMED))
    assert core._bridge_cfg_for(multi_bridge_cfg, "Delta") is None


def test_default_project_resolves_when_exactly_one_bridge_armed(
    multi_bridge_cfg, monkeypatch
) -> None:
    only = {8768: _THREE_ARMED[8768]}
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(only))
    assert core.default_project(multi_bridge_cfg) == "Alpha"


def test_default_project_none_when_several_bridges_armed(
    multi_bridge_cfg, monkeypatch
) -> None:
    """v1.0.7 behavior change — with more than one bridge armed
    there's no longer a single 'the' project to default to. A caller MUST pass
    project= explicitly rather than get a silent guess."""
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(_THREE_ARMED))
    assert core.default_project(multi_bridge_cfg) is None


def test_default_project_none_when_none_armed(multi_bridge_cfg, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge({}))
    assert core.default_project(multi_bridge_cfg) is None


def test_bridge_state_returns_first_bridge_as_primary(
    multi_bridge_cfg, monkeypatch
) -> None:
    """Back-compat single-bridge view: bridge_state() still returns ONE
    answer (the first in port order) for callers that only care 'is anything
    up' — list_bridges() is what exposes the full multi-instance picture."""
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(_THREE_ARMED))
    st = core.bridge_state(multi_bridge_cfg)
    assert st["available"] is True
    assert st["project"] == "Alpha"
    assert st["port"] == 8768


def test_active_target_ambiguous_when_several_bridges_armed(
    multi_bridge_cfg, monkeypatch
) -> None:
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(_THREE_ARMED))
    out = core.active_target(multi_bridge_cfg)
    assert out["known"] is False
    assert out["reason"] == "ambiguous_bridge"
    assert set(out["armed_projects"]) == {"Alpha", "Beta", "Gamma"}


def test_active_target_with_explicit_project_resolves_that_bridges_pid(
    multi_bridge_cfg, monkeypatch
) -> None:
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(_THREE_ARMED))
    seen_pid_cfg = {}

    def fake_owner_pid(cfg, runner=None):
        seen_pid_cfg["port"] = urlparse(cfg.bridge_url).port
        return 4242

    def fake_resolve(cfg, bridge_pid=None):
        return {"known": True, "is_emulator": True, "name": "Emulator",
                "source": "uia_live", "bridge_pid": bridge_pid}

    monkeypatch.setattr(core, "_bridge_owner_pid", fake_owner_pid)
    monkeypatch.setattr(core, "resolve_active_target", fake_resolve)
    out = core.active_target(multi_bridge_cfg, project="Gamma")
    assert seen_pid_cfg["port"] == 8770  # Gamma's own bridge, not the base port
    assert out["bridge_pid"] == 4242


def test_bridge_url_pinned_skips_range_scan_legacy_path(cfg, monkeypatch) -> None:
    """The plain `cfg` fixture (bridge_url_pinned=True) only ever probes its
    one pinned port — the pre-1.0.7 single-bridge behavior, unaffected by
    multi-instance support existing in the codebase."""
    probed_ports = []

    def fake(cfg_, path, timeout=5.0, **_kwargs):
        probed_ports.append(urlparse(cfg_.bridge_url).port)
        return 200, json.dumps({"project": "Alpha", "bridge_version": "1.0.7",
                                 "model_loaded": True}).encode()

    monkeypatch.setattr(core, "_bridge_http", fake)
    core.list_bridges(cfg)
    assert probed_ports == [8768]


def test_list_bridges_health_payload_includes_project_path_and_pid(
    multi_bridge_cfg, monkeypatch
) -> None:
    """Each bridge entry from list_bridges() must carry project_path (str) and
    pid (int) at the expected keys when the /bridge/health payload includes them.

    Shape contract for the multi-instance path: project_path and pid must be
    present and of the correct type in every entry list_bridges() returns.
    """
    by_port = {
        8768: {"project": "Alpha", "bridge_version": "1.1.0", "model_loaded": True,
               "port": 8768,
               "project_path": r"C:\Projects\Alpha\Alpha.optix",
               "pid": 1001},
        8769: {"project": "Beta", "bridge_version": "1.1.0", "model_loaded": True,
               "port": 8769,
               "project_path": None,   # Studio open but path unknown
               "pid": 2002},
    }
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(by_port))
    bridges = core.list_bridges(multi_bridge_cfg)
    assert len(bridges) == 2
    by_project = {b["project"]: b for b in bridges}

    # Alpha — both fields populated; verify key presence and type
    alpha = by_project["Alpha"]
    assert "project_path" in alpha
    assert "pid" in alpha
    assert alpha["project_path"] == r"C:\Projects\Alpha\Alpha.optix"
    assert isinstance(alpha["project_path"], str)
    assert alpha["pid"] == 1001
    assert isinstance(alpha["pid"], int)

    # Beta — project_path is None (null case), pid still present as int
    beta = by_project["Beta"]
    assert "project_path" in beta
    assert "pid" in beta
    assert beta["project_path"] is None
    assert beta["pid"] == 2002
    assert isinstance(beta["pid"], int)


# ---- bridge registry tests (1.0.8) -------------------------------------------

def _make_registry(tmp_path, entries: dict[int, dict | None]) -> "Path":
    """Create a bridge registry directory under tmp_path.

    `entries` maps port -> health-response dict (None = no file for that port,
    i.e. simulate a port that exists in the range but not in the registry).
    Each entry creates ``<port>.json`` under ``<tmp_path>/ftx-mcp/bridges/``.
    Returns the registry directory path.
    """
    registry_dir = tmp_path / "ftx-mcp" / "bridges"
    registry_dir.mkdir(parents=True)
    for port, payload in entries.items():
        if payload is not None:
            f = registry_dir / f"{port}.json"
            f.write_text(json.dumps({"port": port, **payload}))
    return registry_dir


def test_registry_live_and_stale_entry(
    multi_bridge_cfg, monkeypatch, tmp_path
) -> None:
    """Registry with one live and one stale entry: list_bridges returns the live
    bridge, deletes the stale file, and never falls through to the range scan
    (ports outside the registry are NOT probed).

    The stale port is 9000 — outside the default 8768..8771 range — so any
    probe against it via the range-scan fallback would be plainly visible.
    """
    registry_dir = _make_registry(tmp_path, {
        8768: {"project": "Alpha", "bridge_version": "1.0.8", "model_loaded": True},
        9000: {"project": "Stale",  "bridge_version": "1.0.8", "model_loaded": True},
    })
    stale_file = registry_dir / "9000.json"
    assert stale_file.exists()

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def fake_http(cfg_, path, **_kw):
        port = urlparse(cfg_.bridge_url).port
        if port == 8768 and path.startswith("/bridge/health"):
            return 200, json.dumps(
                {"project": "Alpha", "bridge_version": "1.0.8",
                 "model_loaded": True, "port": 8768}
            ).encode()
        # port 9000 — bridge crashed, OS gives connection-refused (stale entry)
        raise core.BridgeUnavailable(
            f"nothing on {port}",
        ) from ConnectionRefusedError()

    monkeypatch.setattr(core, "_bridge_http", fake_http)

    bridges = core.list_bridges(multi_bridge_cfg, force=True)

    # only the live entry is returned
    assert len(bridges) == 1
    assert bridges[0]["project"] == "Alpha"
    assert bridges[0]["port"] == 8768
    assert bridges[0]["source"] == "registry"

    # stale file was deleted
    assert not stale_file.exists()


def test_registry_live_entry_source_is_registry(
    multi_bridge_cfg, monkeypatch, tmp_path
) -> None:
    """Each bridge result from a registry-sourced scan carries source='registry'."""
    _make_registry(tmp_path, {
        8768: {"project": "Alpha", "bridge_version": "1.0.8", "model_loaded": True},
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge({
        8768: {"project": "Alpha", "bridge_version": "1.0.8",
               "model_loaded": True, "port": 8768},
    }))
    bridges = core.list_bridges(multi_bridge_cfg, force=True)
    assert bridges[0]["source"] == "registry"


def test_empty_registry_falls_back_to_scan(
    multi_bridge_cfg, monkeypatch, tmp_path
) -> None:
    """An empty registry directory (no *.json files) is treated as 'absent':
    list_bridges falls back to the port-range scan unchanged."""
    # create the dir but leave it empty
    registry_dir = tmp_path / "ftx-mcp" / "bridges"
    registry_dir.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(_THREE_ARMED))
    bridges = core.list_bridges(multi_bridge_cfg, force=True)
    # falls back to port-range scan: 3 armed bridges found, source="scan"
    assert {b["project"] for b in bridges} == {"Alpha", "Beta", "Gamma"}
    assert all(b["source"] == "scan" for b in bridges)


def test_missing_registry_dir_falls_back_to_scan(
    multi_bridge_cfg, monkeypatch, tmp_path
) -> None:
    """When the registry directory does not exist, the range scan runs as normal."""
    # point LOCALAPPDATA at a dir that has no ftx-mcp/bridges/ subtree
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(_THREE_ARMED))
    bridges = core.list_bridges(multi_bridge_cfg, force=True)
    assert {b["project"] for b in bridges} == {"Alpha", "Beta", "Gamma"}
    assert all(b["source"] == "scan" for b in bridges)


def test_all_stale_registry_falls_back_to_scan(
    multi_bridge_cfg, monkeypatch, tmp_path
) -> None:
    """When the registry has entries but ALL are stale (none respond), execution
    falls through to the legacy port-range scan within the same call so that
    live bridges on the standard range are still discovered.

    Port 9000 is outside the configured range (8768..8771) so it can only be
    probed via the registry path — any appearance of it in results would prove
    the registry path ran.  Port 8768 is inside the range and will respond via
    the fake HTTP layer once the range scan runs.
    """
    registry_dir = _make_registry(tmp_path, {
        9000: {"project": "Ghost", "bridge_version": "1.0.8", "model_loaded": True},
    })
    stale_file = registry_dir / "9000.json"
    assert stale_file.exists()

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    # port 9000 is unreachable (stale); port 8768 is live via range scan
    def fake_http(cfg_, path, **_kw):
        from urllib.parse import urlparse as _urlparse
        port = _urlparse(cfg_.bridge_url).port
        if port == 8768 and path.startswith("/bridge/health"):
            return 200, json.dumps(
                {"project": "Alpha", "bridge_version": "1.0.8",
                 "model_loaded": True, "port": 8768}
            ).encode()
        raise core.BridgeUnavailable(f"nothing on {port}") from ConnectionRefusedError()

    monkeypatch.setattr(core, "_bridge_http", fake_http)

    bridges = core.list_bridges(multi_bridge_cfg, force=True)

    # the stale registry was fully dead → range scan kicked in → found Alpha
    assert len(bridges) == 1
    assert bridges[0]["project"] == "Alpha"
    assert bridges[0]["port"] == 8768
    assert bridges[0]["source"] == "scan"

    # stale file was cleaned up during the (failed) registry probe
    assert not stale_file.exists()


def test_range_scan_source_field_is_scan(multi_bridge_cfg, monkeypatch, tmp_path) -> None:
    """Port-range scan results carry source='scan'."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # no registry dir
    only = {8768: _THREE_ARMED[8768]}
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge(only))
    bridges = core.list_bridges(multi_bridge_cfg, force=True)
    assert bridges[0]["source"] == "scan"


def test_registry_find_bridge_for_routes_correctly(
    multi_bridge_cfg, monkeypatch, tmp_path
) -> None:
    """_find_bridge_for routes to the correct port using registry entries."""
    _make_registry(tmp_path, {
        8768: {"project": "Alpha", "bridge_version": "1.0.8", "model_loaded": True},
        8769: {"project": "Beta",  "bridge_version": "1.0.8", "model_loaded": True},
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge({
        8768: {"project": "Alpha", "bridge_version": "1.0.8",
               "model_loaded": True, "port": 8768},
        8769: {"project": "Beta",  "bridge_version": "1.0.8",
               "model_loaded": True, "port": 8769},
    }))
    b = core._find_bridge_for(multi_bridge_cfg, "Beta")
    assert b is not None
    assert b["port"] == 8769
    assert b["source"] == "registry"


def test_find_bridge_for_stale_registry_resolved(
    multi_bridge_cfg, monkeypatch, tmp_path
) -> None:
    """_find_bridge_for with a live and a stale registry entry: returns the live
    bridge and deletes the stale file. Port 9000 is outside the configured
    range, so it is probed only because the registry names it; the range
    ports are probed in the same pass, once each.
    """
    registry_dir = _make_registry(tmp_path, {
        8768: {"project": "Alpha", "bridge_version": "1.0.8", "model_loaded": True},
        9000: {"project": "Stale",  "bridge_version": "1.0.8", "model_loaded": True},
    })
    stale_file = registry_dir / "9000.json"
    assert stale_file.exists()

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    probed_ports: list[int] = []

    def fake_http(cfg_, path, **_kw):
        port = urlparse(cfg_.bridge_url).port
        probed_ports.append(port)
        if port == 8768 and path.startswith("/bridge/health"):
            return 200, json.dumps(
                {"project": "Alpha", "bridge_version": "1.0.8",
                 "model_loaded": True, "port": 8768}
            ).encode()
        # port 9000 — bridge crashed, OS gives connection-refused (stale entry)
        raise core.BridgeUnavailable(f"nothing on {port}") from ConnectionRefusedError()

    monkeypatch.setattr(core, "_bridge_http", fake_http)

    result = core._find_bridge_for(multi_bridge_cfg, "Alpha")

    # live bridge is returned with registry metadata
    assert result is not None
    assert result["port"] == 8768
    assert result["project"] == "Alpha"
    assert result["source"] == "registry"

    # stale file was cleaned up by the registry probe
    assert not stale_file.exists()

    # registry + range are probed together, each port exactly once
    assert sorted(probed_ports) == [8768, 8769, 8770, 8771, 9000]


def test_registry_timeout_does_not_delete_entry(
    multi_bridge_cfg, monkeypatch, tmp_path
) -> None:
    """A health-probe timeout must NOT delete the registry file.

    A Studio process that is still loading its model (arming) can cause the
    health probe to time out even though the process is alive.  The registry
    entry must be preserved so the bridge is not evicted before it finishes
    arming.  Only a definitive connection-refused answer (bridge crashed or
    never started) justifies deleting the file.
    """
    registry_dir = _make_registry(tmp_path, {
        9001: {"project": "Arming", "bridge_version": "1.0.8", "model_loaded": False},
    })
    arming_file = registry_dir / "9001.json"
    assert arming_file.exists()

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def fake_http(cfg_, path, **_kw):
        # Simulate a timeout: raise BridgeUnavailable WITHOUT a
        # ConnectionRefusedError cause.  This is the "still loading" case.
        raise core.BridgeUnavailable("timed out connecting to 9001")

    monkeypatch.setattr(core, "_bridge_http", fake_http)

    core._scan_bridge_ports(multi_bridge_cfg, force=True)

    # The registry file must still exist — a timeout is not a definitive "dead"
    assert arming_file.exists(), (
        "registry file was incorrectly deleted after a probe timeout; "
        "only a connection-refused failure should remove it"
    )


# ---- optix_bridge_status source field -------------------------------------------

def _get_bridge_status_fn(cfg: core.Config):
    """Return the direct (sync) callable for optix_bridge_status from the MCP app."""
    from service.mcp_app import make_mcp
    mcp = make_mcp(cfg)
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    tool = tools["optix_bridge_status"]
    # Offloaded tools store the original sync fn at _ftx_sync_fn; fast tools
    # expose it directly as .fn.
    return getattr(tool, "_ftx_sync_fn", tool.fn)


def test_optix_bridge_status_source_registry(
    multi_bridge_cfg, monkeypatch, tmp_path
) -> None:
    """optix_bridge_status reports source='registry' for each bridge entry when
    discovered via the per-port JSON registry files."""
    _make_registry(tmp_path, {
        8768: {"project": "Alpha", "bridge_version": "1.0.8", "model_loaded": True},
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge({
        8768: {"project": "Alpha", "bridge_version": "1.0.8",
               "model_loaded": True, "port": 8768},
    }))

    result = _get_bridge_status_fn(multi_bridge_cfg)()

    assert result["count"] == 1
    assert result["bridges"][0]["source"] == "registry"
    # top-level echoes bridges[0] — source must be there too
    assert result["source"] == "registry"


def test_optix_bridge_status_source_scan(
    multi_bridge_cfg, monkeypatch, tmp_path
) -> None:
    """optix_bridge_status reports source='scan' for each bridge entry when the
    registry directory is absent and the port-range scan is used instead."""
    # No registry dir under tmp_path — _bridge_registry_entries() returns None.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge({
        8768: {"project": "Alpha", "bridge_version": "1.0.7",
               "model_loaded": True, "port": 8768},
    }))

    result = _get_bridge_status_fn(multi_bridge_cfg)()

    assert result["count"] == 1
    assert result["bridges"][0]["source"] == "scan"
    assert result["source"] == "scan"


def test_registry_does_not_hide_a_bridge_that_writes_no_registry_file(
    multi_bridge_cfg, monkeypatch, tmp_path
) -> None:
    """Mixed fleet after an upgrade: Alpha runs a 1.0.8 bridge (registry file
    on 8768), Beta still runs an older bridge on 8769 that writes none. Both
    must be listed; Beta comes from the range scan."""
    _make_registry(tmp_path, {
        8768: {"project": "Alpha", "bridge_version": "1.0.8", "model_loaded": True},
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(core, "_bridge_http", _multi_bridge({
        8768: {"project": "Alpha", "bridge_version": "1.0.8",
               "model_loaded": True, "port": 8768},
        8769: {"project": "Beta", "bridge_version": "1.0.7",
               "model_loaded": True, "port": 8769},
    }))
    bridges = {b["project"]: b for b in core.list_bridges(multi_bridge_cfg, force=True)}
    assert set(bridges) == {"Alpha", "Beta"}
    assert bridges["Alpha"]["source"] == "registry"
    assert bridges["Beta"]["source"] == "scan"
    assert core._find_bridge_for(multi_bridge_cfg, "Beta")["port"] == 8769
