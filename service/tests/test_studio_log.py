"""1.0.9 5e: execute rejection detection (optix_execute_method reads
FTOptixStudio.0.log after the click) and the optix_studio_log read tool.
Offline: OPTIX_STUDIO_LOG_DIR points the resolver at tmp."""
from __future__ import annotations

from pathlib import Path

import pytest

from service import core, studio_arm
from service.mcp_app import make_mcp
from service.tests.conftest import make_project


@pytest.fixture()
def log_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "studiolog"
    d.mkdir()
    monkeypatch.setenv("OPTIX_STUDIO_LOG_DIR", str(d))
    return d


def _write(d: Path, lines: list[str]) -> Path:
    p = d / "FTOptixStudio.0.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _body(wrapped: str) -> list[str]:
    assert wrapped.startswith('<untrusted source="studio_log">'), wrapped
    return wrapped[wrapped.index(">") + 1: -len("</untrusted>")].splitlines()


# ---- studio_log ------------------------------------------------------------

def test_studio_log_returns_last_n_lines(cfg: core.Config, log_dir: Path) -> None:
    _write(log_dir, [f"line {i}" for i in range(50)])
    out = core.studio_log_tail(cfg, lines=10)
    assert out["ok"] is True and out["returned_lines"] == 10
    assert _body(out["lines"])[-1] == "line 49"
    assert _body(out["lines"])[0] == "line 40"


def test_studio_log_filter_is_case_insensitive(cfg: core.Config, log_dir: Path) -> None:
    _write(log_dir, ["INFO ok", "ERROR Boom", "info fine", "error again"])
    out = core.studio_log_tail(cfg, contains="error")
    assert _body(out["lines"]) == ["ERROR Boom", "error again"]
    assert out["filtered"] is True


def test_studio_log_missing_is_structured_error(cfg: core.Config, log_dir: Path) -> None:
    out = core.studio_log_tail(cfg)
    assert out["ok"] is False and out["code"] == "no_studio_log"
    assert out["errors"][0]["code"] == "no_studio_log"


@pytest.mark.parametrize("bad", [0, -1, 5001, "10", 2.5, True, None])
def test_studio_log_invalid_lines(cfg: core.Config, log_dir: Path, bad) -> None:
    _write(log_dir, ["x"])
    out = core.studio_log_tail(cfg, lines=bad)
    assert out["ok"] is False and out["code"] == "invalid_lines"
    assert out["errors"][0]["field"] == "lines"


@pytest.mark.parametrize("bad", ["", "   ", 5])
def test_studio_log_invalid_contains(cfg: core.Config, log_dir: Path, bad) -> None:
    _write(log_dir, ["x"])
    out = core.studio_log_tail(cfg, contains=bad)
    assert out["ok"] is False and out["code"] == "invalid_contains"


def test_studio_log_mcp_tool_delegates(cfg: core.Config, log_dir: Path) -> None:
    from service.tests.test_mcp_app import _list_tools, _tool_fn
    _write(log_dir, ["a", "b", "c"])
    tool = next(t for t in _list_tools(make_mcp(cfg)) if t.name == "optix_studio_log")
    assert tool.annotations.readOnlyHint is True
    out = _tool_fn(tool)(lines=2)
    assert _body(out["lines"]) == ["b", "c"]


# ---- execute rejection -----------------------------------------------------

@pytest.fixture()
def exec_env(cfg: core.Config, log_dir: Path, monkeypatch):
    make_project(cfg.projects_root, "MyProj")
    monkeypatch.setattr(core, "_EXECUTE_LOG_SETTLE_S", 0)
    return cfg, log_dir


def _fake_execute(log_dir: Path, appended: list[str], state: str = "executed"):
    def fake(project, project_dir, **kw):
        if appended:
            with open(log_dir / "FTOptixStudio.0.log", "a", encoding="utf-8") as fh:
                fh.write("\n".join(appended) + "\n")
        return {"ok": True, "state": state, "method": kw["method"]}
    return fake


def test_execute_method_reports_rejected_with_log_line(exec_env, monkeypatch) -> None:
    cfg, log_dir = exec_env
    _write(log_dir, ["old line: Execute rejected (stale, before the click)"])
    monkeypatch.setattr(studio_arm, "execute_method", _fake_execute(log_dir, [
        "10:00:01 INFO unrelated",
        "10:00:02 WARN Execute of Apply on FormattedLinkScript was rejected",
    ]))
    out = core.execute_design_method(cfg, "MyProj", "FormattedLinkScript", "Apply")
    assert out["state"] == "rejected" and out["ok"] is False
    assert "Apply on FormattedLinkScript was rejected" in out["log_line"]
    assert "stale" not in out["log_line"]  # only lines written after the click
    assert out["studio_log_checked"] is True


def test_execute_method_executed_when_log_clean(exec_env, monkeypatch) -> None:
    cfg, log_dir = exec_env
    _write(log_dir, ["old line: rejected long ago"])
    monkeypatch.setattr(studio_arm, "execute_method", _fake_execute(
        log_dir, ["10:00:01 INFO Apply ran"]))
    out = core.execute_design_method(cfg, "MyProj", "FormattedLinkScript", "Apply")
    assert out["ok"] is True and out["state"] == "executed"
    assert out["studio_log_checked"] is True and "log_line" not in out


def test_execute_method_executed_when_studio_log_missing(exec_env, monkeypatch) -> None:
    cfg, log_dir = exec_env
    monkeypatch.setattr(studio_arm, "execute_method", _fake_execute(log_dir, []))
    out = core.execute_design_method(cfg, "MyProj", "N", "Apply")
    assert out["ok"] is True and out["state"] == "executed"
    assert out["studio_log_checked"] is False


def test_execute_method_failure_is_not_rewritten(exec_env, monkeypatch) -> None:
    cfg, log_dir = exec_env
    monkeypatch.setattr(studio_arm, "execute_method", lambda *a, **k: {
        "ok": False, "error": "menu_item_not_found"})
    out = core.execute_design_method(cfg, "MyProj", "N", "Apply")
    assert out == {"ok": False, "error": "menu_item_not_found"}


def test_detect_rejection_prefers_line_naming_the_method() -> None:
    lines = ["x denied for something else", "Execute Apply rejected", "y refused"]
    assert core.detect_execute_rejection(lines, "Model/N", "Apply") == "Execute Apply rejected"
    assert core.detect_execute_rejection(["all fine"], "N", "Apply") is None


def test_log_rotation_reads_new_file_from_start(exec_env, monkeypatch) -> None:
    cfg, log_dir = exec_env
    _write(log_dir, ["padding " * 50])

    def fake(project, project_dir, **kw):
        _write(log_dir, ["Execute Apply rejected"])  # rotated: smaller than the mark
        return {"ok": True, "state": "executed"}

    monkeypatch.setattr(studio_arm, "execute_method", fake)
    out = core.execute_design_method(cfg, "MyProj", "N", "Apply")
    assert out["state"] == "rejected"


# ---- 1.0.9: chain derived from the live bridge when armed -------------------

def test_execute_method_chain_from_live_bridge_when_armed(exec_env, monkeypatch) -> None:
    """Bridge armed: the chain is the live node path, handed to studio_arm."""
    cfg, log_dir = exec_env
    seen: dict = {}

    def fake(project, project_dir, **kw):
        seen.update(kw)
        return {"ok": True, "state": "executed", "method": kw["method"]}

    monkeypatch.setattr(studio_arm, "execute_method", fake)
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda c, p: c)
    monkeypatch.setattr(core, "_bridge_get_json", lambda c, path, **k: (200, {
        "mode": "search", "matches": [
            {"path": "NetLogic/Gen/FormattedLinkScript", "type": "NetLogic"}]}))
    out = core.execute_design_method(cfg, "MyProj", "FormattedLinkScript", "Apply")
    assert out["ok"] is True
    assert seen["live_chain"] == ["NetLogic", "Gen", "FormattedLinkScript"]


def test_execute_method_chain_falls_back_to_yaml_when_no_bridge(exec_env, monkeypatch) -> None:
    """No bridge armed: no live chain, studio_arm derives it from saved YAML."""
    cfg, log_dir = exec_env
    seen: dict = {}

    def fake(project, project_dir, **kw):
        seen.update(kw)
        return {"ok": True, "state": "executed", "method": kw["method"]}

    def no_get(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("bridge queried with no bridge armed")

    monkeypatch.setattr(studio_arm, "execute_method", fake)
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda c, p: None)
    monkeypatch.setattr(core, "_bridge_get_json", no_get)
    core.execute_design_method(cfg, "MyProj", "FormattedLinkScript", "Apply")
    assert seen["live_chain"] is None


@pytest.mark.parametrize("reply", [
    (200, {"matches": []}),
    (200, {"matches": [{"path": "A/X", "type": "NetLogic"},
                       {"path": "B/X", "type": "NetLogic"}]}),
    (500, {}),
])
def test_live_netlogic_chain_unresolvable_is_none(cfg, monkeypatch, reply) -> None:
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda c, p: c)
    monkeypatch.setattr(core, "_bridge_get_json", lambda c, path, **k: reply)
    assert core.live_netlogic_chain(cfg, "P", "X") is None


def test_live_netlogic_chain_prefers_netlogic_among_same_name(cfg, monkeypatch) -> None:
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda c, p: c)
    monkeypatch.setattr(core, "_bridge_get_json", lambda c, path, **k: (200, {"matches": [
        {"path": "Model/X", "type": "UAVariable"},
        {"path": "NetLogic/X", "type": "NetLogic"}]}))
    assert core.live_netlogic_chain(cfg, "P", "X") == ["NetLogic", "X"]


def test_live_netlogic_chain_bridge_error_is_none(cfg, monkeypatch) -> None:
    def boom(c, p):
        raise core.AmbiguousBridgeForProject("x", project="P", bridges=[])
    monkeypatch.setattr(core, "_bridge_cfg_for", boom)
    assert core.live_netlogic_chain(cfg, "P", "X") is None


@pytest.mark.skipif(
    not (__import__("os").environ.get("FTX_LIVE_BRIDGE")
         and __import__("os").environ.get("FTX_LIVE_PROJECT")),
    reason="live-bridge test: set FTX_LIVE_BRIDGE=1 and FTX_LIVE_PROJECT with Studio open + bridge armed")
def test_live_netlogic_chain_against_live_bridge(cfg) -> None:
    import os
    chain = core.live_netlogic_chain(cfg, os.environ["FTX_LIVE_PROJECT"],
                                     "StudioMCPBridge")
    assert chain and chain[-1] == "StudioMCPBridge"


# ---- export_method_not_loaded ----------------------------------------------

def _live_node(monkeypatch, children, status=200):
    """Fake an armed bridge: /bridge/map finds Model/Gen, /bridge/nodes returns
    `children`."""
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda c, p: c)

    def fake_get(c, path, **k):
        if path.startswith("/bridge/map"):
            return 200, {"matches": [{"path": "Model/Gen", "type": "NetLogic"}]}
        return status, {"path": "Model/Gen", "children": children}
    monkeypatch.setattr(core, "_bridge_get_json", fake_get)


def test_execute_method_export_method_not_loaded(exec_env, monkeypatch) -> None:
    """Live node without a UAMethod child -> export_method_not_loaded (with a
    reopen nudge), NOT menu_item_not_found, and no GUI gesture is attempted."""
    cfg, _ = exec_env
    _live_node(monkeypatch, [{"browse_name": "BehaviourStartPriority",
                              "node_class": "Variable"}])
    called = []
    monkeypatch.setattr(studio_arm, "execute_method",
                        lambda *a, **k: called.append(1) or {"ok": True})
    out = core.execute_design_method(cfg, "MyProj", "Gen", "Apply")
    assert out["ok"] is False
    assert out["error"] == "export_method_not_loaded"
    assert out["error"] != "menu_item_not_found"
    assert "reopen" in out["nudge"].lower()
    assert out["reopen_required"] is True and out["path"] == "Model/Gen"
    assert called == []


def test_execute_method_proceeds_when_method_child_present(exec_env, monkeypatch) -> None:
    cfg, log_dir = exec_env
    _live_node(monkeypatch, [{"browse_name": "Apply", "node_class": "Method"}])
    monkeypatch.setattr(studio_arm, "execute_method", _fake_execute(log_dir, []))
    out = core.execute_design_method(cfg, "MyProj", "Gen", "Apply")
    assert out["ok"] is True and out["state"] == "executed"


def test_execute_method_non_method_child_of_same_name_is_not_loaded(
        exec_env, monkeypatch) -> None:
    cfg, _ = exec_env
    _live_node(monkeypatch, [{"browse_name": "Apply", "node_class": "Variable"}])
    out = core.execute_design_method(cfg, "MyProj", "Gen", "Apply")
    assert out["error"] == "export_method_not_loaded"


def test_execute_method_undecidable_live_state_falls_through(exec_env, monkeypatch) -> None:
    """Bridge error on the node read -> unchanged behaviour (GUI path runs)."""
    cfg, log_dir = exec_env
    _live_node(monkeypatch, [], status=500)
    monkeypatch.setattr(studio_arm, "execute_method", _fake_execute(log_dir, []))
    out = core.execute_design_method(cfg, "MyProj", "Gen", "Apply")
    assert out["ok"] is True and out["state"] == "executed"
