"""Studio identity on /bridge/health and arm idempotency per Studio pid
(PRD 0022 9a, ftx-mcp-21b.1)."""
from __future__ import annotations

from pathlib import Path

from service import studio_arm

_CS = Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs"


def _src() -> str:
    return _CS.read_text(encoding="utf-8", errors="ignore")


def _health_body() -> str:
    text = _src()
    start = text.index("private string HealthJson()")
    return text[start:text.index("private string NodeJson", start)]


def _start_listener_body() -> str:
    text = _src()
    start = text.index("private void StartListener(string via)")
    return text[start:text.index("private void StopListener()", start)]


def test_health_reports_studio_identity_and_keeps_existing_fields() -> None:
    body = _health_body()
    for field in ("studio_pid", "project_name", "project_dir",
                  "bridge_version", "project_path", "project_path_v2",
                  "invoke_session", "subtree_guard", "model_loaded"):
        assert f'\\"{field}\\"' in body, field


def test_registry_entry_carries_studio_identity() -> None:
    text = _src()
    start = text.index("private static void WriteRegistryEntry")
    body = text[start:text.index("private static void DeleteRegistryEntry", start)]
    for field in ("studio_pid", "project_name", "project_dir"):
        assert f'\\"{field}\\"' in body, field


def test_start_listener_is_idempotent_per_studio_pid() -> None:
    """The live-own-bridge check must run before any port is bound."""
    body = _start_listener_body()
    assert "FindLiveOwnBridgePort()" in body
    assert body.index("FindLiveOwnBridgePort()") < body.index("TryBindWithRetry")


def _fake_bridges(monkeypatch, bridges: dict[int, dict]) -> None:
    monkeypatch.setattr(studio_arm, "bound_ports", lambda *a, **k: set(bridges))
    monkeypatch.setattr(studio_arm, "_bridge_health",
                        lambda p, timeout=3.0: bridges.get(p))


TWO = {
    8768: {"project": "Plant", "studio_pid": 111, "pid": 111,
           "project_name": "Plant", "project_dir": r"C:\work\A\Plant"},
    8769: {"project": "Plant", "studio_pid": 222, "pid": 222,
           "project_name": "Plant", "project_dir": r"C:\work\B\Plant"},
}


def test_two_bridges_are_routed_by_studio_pid(monkeypatch) -> None:
    _fake_bridges(monkeypatch, TWO)
    assert studio_arm.serving_port_for("Plant", studio_pid=111) == 8768
    assert studio_arm.serving_port_for("Plant", studio_pid=222) == 8769


def test_already_armed_never_reported_for_another_studio_pid(monkeypatch) -> None:
    _fake_bridges(monkeypatch, {8768: TWO[8768]})
    # Studio 222 has the same project name but no bridge of its own.
    assert studio_arm.serving_port_for("Plant", studio_pid=222) is None
    monkeypatch.setattr(studio_arm, "studio_pid_for", lambda *a, **k: 222)
    out = studio_arm.execute_method("Plant", str(Path("/nonexistent")),
                                    method="StartBridge")
    assert out.get("state") != "already_armed"


def test_same_name_different_directory_is_another_studio(monkeypatch) -> None:
    _fake_bridges(monkeypatch, {8768: TWO[8768]})
    assert studio_arm.serving_port_for(
        "Plant", project_dir=r"C:\work\B\Plant") is None
    assert studio_arm.serving_port_for(
        "Plant", project_dir="C:/work/A/Plant/") == 8768


def test_arm_of_studio_with_live_bridge_returns_existing(monkeypatch) -> None:
    _fake_bridges(monkeypatch, TWO)
    monkeypatch.setattr(studio_arm, "studio_pid_for", lambda *a, **k: 222)
    out = studio_arm.execute_method("Plant", "/nope", method="StartBridge")
    assert out["ok"] and out["state"] == "already_armed"
    assert out["port"] == 8769 and out["studio_pid"] == 222


def test_older_bridge_pid_is_accepted_as_studio_pid(monkeypatch) -> None:
    _fake_bridges(monkeypatch, {8768: {"project": "Plant", "pid": 111}})
    assert studio_arm.serving_port_for("Plant", studio_pid=111) == 8768
    assert studio_arm.serving_port_for("Plant", studio_pid=222) is None
