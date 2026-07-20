"""Tests for the service-side composites: restart_emulator,
bridge_add_bound_widget, bridge_add_navigation_panel_item."""
from __future__ import annotations

import pytest

from service import core


def test_restart_emulator_stops_then_runs(cfg: core.Config, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, r=None: {"pids": [111], "state": "running"})
    monkeypatch.setattr(core, "stop_emulator",
                        lambda c, r=None: calls.append("stop") or {"stopped": True, "killed_pids": [111]})
    monkeypatch.setattr(core, "run_emulator",
                        lambda c, p, save_first=False, wait_ready=True, runner=None:
                        calls.append("run") or {"launched": True, "serving": True})
    out = core.restart_emulator(cfg, "P")
    assert calls == ["stop", "run"]
    assert out["restarted"] is True and out["stopped_pids"] == [111]
    assert out["serving"] is True


def test_restart_emulator_skips_stop_when_idle(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, r=None: {"pids": [], "state": "not_running"})
    monkeypatch.setattr(core, "stop_emulator",
                        lambda c, r=None: (_ for _ in ()).throw(AssertionError("no stop when idle")))
    monkeypatch.setattr(core, "run_emulator",
                        lambda c, p, save_first=False, wait_ready=True, runner=None:
                        {"launched": True, "serving": True})
    out = core.restart_emulator(cfg, "P")
    assert out["restarted"] is False and "stopped_pids" not in out


def test_add_bound_widget_full_sequence(cfg: core.Config, monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(core, "bridge_create_widget",
                        lambda c, p, s, n, t: seen.append(("create", n, t)) or
                        {"ok": True, "created_path": f"{s}/{n}"})
    monkeypatch.setattr(core, "bridge_set_property",
                        lambda c, p, np, name, val, locale="en-US":
                        seen.append(("set", name, val)) or {"ok": True})
    monkeypatch.setattr(core, "bridge_bind_property",
                        lambda c, p, np, name, src, mode:
                        seen.append(("bind", name, src, mode)) or {"ok": True})
    out = core.bridge_add_bound_widget(
        cfg, "P", "UI/Screens/A", "Sw1", "Switch", left=40, top=60,
        bind_property="Checked", source_path="Model/Run", mode="ReadWrite")
    assert out["ok"] is True and out["created_path"] == "UI/Screens/A/Sw1"
    assert seen == [("create", "Sw1", "Switch"), ("set", "LeftMargin", "40"),
                    ("set", "TopMargin", "60"), ("bind", "Checked", "Model/Run", "ReadWrite")]


def test_add_bound_widget_rolls_back_on_step_failure(cfg: core.Config, monkeypatch) -> None:
    """The Cowork-found bug: bridge writes RAISE on failure; the composite
    must catch, DELETE the created node (no orphans), and report the step —
    so a retry with the same name is safe."""
    deleted = []
    monkeypatch.setattr(core, "bridge_create_widget",
                        lambda c, p, s, n, t: {"ok": True, "created_path": f"{s}/{n}"})

    def failing_set(c, p, np, name, val, locale="en-US"):
        if name == "TopMargin":
            raise core.BridgeWriteFailed("boom")
        return {"ok": True}
    monkeypatch.setattr(core, "bridge_set_property", failing_set)
    monkeypatch.setattr(core, "bridge_bind_property",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not bind after failure")))
    monkeypatch.setattr(core, "bridge_delete_node",
                        lambda c, p, np: deleted.append(np) or {"ok": True})
    out = core.bridge_add_bound_widget(
        cfg, "P", "UI/A", "W", "Label", left=1, top=2,
        bind_property="Text", source_path="Model/X")
    assert out["ok"] is False and out["failed_step"] == "set TopMargin"
    assert out["rolled_back"] is True and deleted == ["UI/A/W"]


def test_add_bound_widget_reports_orphan_when_rollback_fails(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "bridge_create_widget",
                        lambda c, p, s, n, t: {"ok": True, "created_path": f"{s}/{n}"})

    def failing_set(c, p, np, name, val, locale="en-US"):
        raise core.BridgeWriteFailed("boom")
    monkeypatch.setattr(core, "bridge_set_property", failing_set)
    monkeypatch.setattr(core, "bridge_delete_node",
                        lambda c, p, np: (_ for _ in ()).throw(core.BridgeWriteFailed("also boom")))
    out = core.bridge_add_bound_widget(cfg, "P", "UI/A", "W", "Label", left=1)
    assert out["ok"] is False and out["rolled_back"] is False
    assert out["orphaned_path"] == "UI/A/W" and "delete" in out["hint"]


def test_add_navigation_panel_item(cfg: core.Config, monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(core, "bridge_create_widget",
                        lambda c, p, s, n, t: seen.append(("create", s, n, t)) or
                        {"ok": True, "created_path": f"{s}/Panels/{n}"})
    monkeypatch.setattr(core, "bridge_set_property",
                        lambda c, p, np, name, val, locale="en-US":
                        seen.append(("set", np, name, val)) or {"ok": True})
    out = core.bridge_add_navigation_panel_item(
        cfg, "P", "UI/MainWindow/NavPanel", "Screen D",
        screen_path="UI/Screens/ScreenD")
    assert out["ok"] is True
    assert out["created_path"] == "UI/MainWindow/NavPanel/Panels/ScreenD"
    assert seen[0] == ("create", "UI/MainWindow/NavPanel", "ScreenD", "NavigationPanelItem")
    assert ("set", "UI/MainWindow/NavPanel/Panels/ScreenD", "Title", "Screen D") in seen
    assert ("set", "UI/MainWindow/NavPanel/Panels/ScreenD", "Panel", "UI/Screens/ScreenD") in seen
