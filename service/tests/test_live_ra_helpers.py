"""Offline tests for the RA driver/station helper used by the live bridge suite.

They live outside test_bridge_live.py because that module is skipped wholesale
without FTX_LIVE_BRIDGE=1; these need no bridge (ftx-mcp-jku.5).
"""
from __future__ import annotations

import os

import pytest

from service import core
from service.tests import test_bridge_live as live_mod


def test_ra_type_names_default_to_ethernetip_driver_and_station():
    """Without env overrides the RA types are the RAEtherNetIP driver and station."""
    if "FTX_LIVE_RA_DRIVER_TYPE" not in os.environ:
        assert live_mod._RA_DRIVER_TYPE == "FTOptix.RAEtherNetIP.RAEtherNetIPDriver"
    if "FTX_LIVE_RA_STATION_TYPE" not in os.environ:
        assert live_mod._RA_STATION_TYPE == "FTOptix.RAEtherNetIP.RAEtherNetIPStation"


def _stub_creation_failure(monkeypatch, version):
    def boom(*a, **k):
        raise core.BridgeWriteFailed("boom: type not found")
    monkeypatch.setattr(core, "bridge_create_object", boom)
    monkeypatch.setattr(core, "bridge_delete_node", lambda *a, **k: {})
    monkeypatch.setattr(live_mod, "_running_studio_version_or_none", lambda cfg: version)


def _create():
    live_mod._create_ra_driver_station(None, "p", "CommDrivers", "D", "S", "CommDrivers/D/S")


def test_ra_creation_error_fails_with_message_on_studio_1_8(monkeypatch):
    """A creation error on Studio >= 1.8 fails (not skips) and carries the error."""
    _stub_creation_failure(monkeypatch, (1, 8))
    with pytest.raises(pytest.fail.Exception, match="boom: type not found"):
        _create()


def test_ra_creation_error_fails_when_studio_version_unknown(monkeypatch):
    """An unknown Studio version is not evidence the module is absent: fail."""
    _stub_creation_failure(monkeypatch, None)
    with pytest.raises(pytest.fail.Exception, match="boom"):
        _create()


def test_ra_creation_error_skips_only_below_studio_1_8(monkeypatch):
    """Studio below 1.8 has no RA module: the only case that skips."""
    _stub_creation_failure(monkeypatch, (1, 7))
    with pytest.raises(pytest.skip.Exception, match="below 1.8"):
        _create()


# ---- live-suite hygiene helpers (ftx-mcp-h4a.3) ------------------------------

def test_color_value_matches_accepts_decimal_uint32_and_hex_forms():
    """A UInt32 describe value (decimal ARGB) and hex text both match #ff0000."""
    match = live_mod._color_value_matches
    assert match("4294901760", "#ff0000")          # 0xFFFF0000
    assert match("16711680", "#ff0000")            # 0x00FF0000
    assert match("#ffff0000", "#ff0000")
    assert match("0xFFFF0000", "#FF0000")
    assert match('<untrusted source="bridge">4294901760</untrusted>', "#ff0000")
    assert not match("4278255360", "#ff0000")      # 0xFF00FF00 (green)
    assert not match("", "#ff0000")


def test_color_value_matches_parses_typed_describe_values():
    """The three describe shapes all match #ff0000: typed, bare decimal, hex."""
    match = live_mod._color_value_matches
    assert match("4294901760 (UInt32)", "#ff0000")
    assert match("4294901760", "#ff0000")
    assert match("#ff0000", "#ff0000")
    assert match('<untrusted source="bridge">4294901760 (UInt32)</untrusted>', "#ff0000")
    assert not match("4278255360 (UInt32)", "#ff0000")


def test_unique_names_carry_run_suffix():
    """Created names go through _nm and end with this run's suffix."""
    assert live_mod._nm("ZzzX") == "ZzzX" + live_mod._RUN_SFX
    assert len(live_mod._RUN_SFX) >= 6


def test_remove_scratch_project_deletes_only_scratch_children(tmp_path):
    """The project folder is removed; foreign names and paths are refused."""
    cfg = type("Cfg", (), {"projects_root": tmp_path})()
    (tmp_path / "zzOpenTruth123abc" / "sub").mkdir(parents=True)
    (tmp_path / "zzOpenTruth123abc" / "sub" / "f.txt").write_text("x")
    (tmp_path / "RealProject").mkdir()
    assert live_mod._remove_scratch_project(cfg, "zzOpenTruth123abc") is True
    assert not (tmp_path / "zzOpenTruth123abc").exists()
    assert live_mod._remove_scratch_project(cfg, "RealProject") is False
    assert live_mod._remove_scratch_project(cfg, "zzBridgeSync../RealProject") is False
    assert (tmp_path / "RealProject").exists()
    assert live_mod._remove_scratch_project(cfg, "zzBridgeSyncGone") is True  # already absent


def test_seed_live_node_creates_missing_node_and_fails_on_error(monkeypatch):
    """A missing live node is created via create_widget; a creation error FAILS."""
    calls = []
    state = {"exists": False}

    def describe(cfg, project, path):
        if not state["exists"]:
            raise core.NodeNotFound(path)
        return {}

    def create(cfg, project, parent, name, wtype):
        calls.append((parent, name, wtype))
        state["exists"] = True
        return {"ok": True}

    monkeypatch.setattr(core, "describe_node", describe)
    monkeypatch.setattr(core, "bridge_create_widget", create)
    monkeypatch.setattr(live_mod, "_LIVE_NODE", "UI/MainWindow/Rectangle1")
    live_mod._seed_live_node(None, "p")
    assert calls == [("UI/MainWindow", "Rectangle1", live_mod._LIVE_TYPE)]
    live_mod._seed_live_node(None, "p")            # present now: nothing more created
    assert len(calls) == 1

    state["exists"] = False

    def boom(*a, **k):
        raise core.BridgeWriteFailed("no create")

    monkeypatch.setattr(core, "bridge_create_widget", boom)
    with pytest.raises(pytest.fail.Exception, match="cannot seed"):
        live_mod._seed_live_node(None, "p")
