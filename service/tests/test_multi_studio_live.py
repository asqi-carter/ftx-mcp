"""Live MULTI-Studio tests — bench-only, gated, INERT on Linux/CI.

Companion to ``test_bridge_live.py`` (one Studio). These drive TWO real
FactoryTalk Optix Studio instances at once, each with its own throwaway
project, and check the multi-Studio guarantees of epic ftx-mcp-21b:

* each Studio can be armed and gets its OWN bridge (distinct port; distinct
  Studio pid when the bridge reports one);
* an edit addressed to project A lands on A and never on B (and vice versa);
* both emulators come up, each on its OWN web port.

HOW TO RUN (on the bench, NOT CI; Studio 1.7.4.32 and 1.8.0.555 are the
target versions for the post-merge run):
    1. Open two throwaway projects, one per Studio window. Give them DIFFERENT
       WebPresentationEngine ports (e.g. 8081 and 8082).
    2. Set the gate and both project names, then run pytest:
           set FTX_LIVE_MULTI_STUDIO=1
           set FTX_LIVE_PROJECT_A=<project open in Studio 1>
           set FTX_LIVE_PROJECT_B=<project open in Studio 2>
           python -m pytest service/tests/test_multi_studio_live.py -q

Without FTX_LIVE_MULTI_STUDIO=1 the module is skipped. With it set, each test
still SKIPS (never fails) when two live Studios are not available: a project
name is unset, the two names are equal, or a project has no reachable bridge
and cannot be armed.

Optional env: FTX_LIVE_MODEL_PARENT (folder the marker nodes are created
under, default ``Model``), FTX_LIVE_EMULATOR_TIMEOUT (seconds, default 90).
Not run in the authoring sandbox: bench verification is pending.
"""
from __future__ import annotations

import os
import time
import uuid

import pytest

from service import core

pytestmark = pytest.mark.skipif(
    os.environ.get("FTX_LIVE_MULTI_STUDIO") != "1",
    reason="live multi-Studio tests: set FTX_LIVE_MULTI_STUDIO=1 with two "
           "Studios open (FTX_LIVE_PROJECT_A / FTX_LIVE_PROJECT_B)",
)

_MODEL_PARENT = os.environ.get("FTX_LIVE_MODEL_PARENT", "Model")
_EMULATOR_TIMEOUT = float(os.environ.get("FTX_LIVE_EMULATOR_TIMEOUT", "90"))


def _norm(name: object) -> str:
    return str(name or "").strip().lower()


def _bridge_for(cfg: "core.Config", project: str) -> "dict | None":
    """The list_bridges() entry serving ``project``, or None."""
    for b in core.list_bridges(cfg, force=True):
        if _norm(b.get("project")) == _norm(project) or \
                _norm(b.get("project_name")) == _norm(project):
            return b
    return None


def _child_names(node: dict) -> set[str]:
    out: set[str] = set()
    for c in node.get("children", []) or []:
        if isinstance(c, dict):
            out.add(str(c.get("browse_name") or c.get("name") or ""))
        else:
            out.add(str(c))
    return out


@pytest.fixture(scope="module")
def studios():
    """(cfg, project_a, project_b) with BOTH projects armed on separate bridges.

    Skips cleanly when two live Studios are not available.
    """
    a = os.environ.get("FTX_LIVE_PROJECT_A")
    b = os.environ.get("FTX_LIVE_PROJECT_B")
    if not a or not b:
        pytest.skip("FTX_LIVE_PROJECT_A and FTX_LIVE_PROJECT_B must name the "
                    "two projects open in two Studios")
    if _norm(a) == _norm(b):
        pytest.skip("FTX_LIVE_PROJECT_A and _B must be two different projects")
    core.reset_bridge_cache()
    cfg = core.Config.from_env()
    for proj in (a, b):
        if _bridge_for(cfg, proj) is None:
            try:
                core.bridge_arm(cfg, proj)
            except Exception as exc:  # no Studio / not openable here
                pytest.skip(f"cannot arm {proj!r}: {exc}")
            core.reset_bridge_cache()
        if _bridge_for(cfg, proj) is None:
            pytest.skip(f"no live Studio serving {proj!r} after arming")
    return cfg, a, b


def test_live_two_studios_armed_on_distinct_bridges(studios):
    """Both projects are armed, each on its own bridge (port, and Studio pid
    when reported), and each bridge answers as available."""
    cfg, a, b = studios
    ba, bb = _bridge_for(cfg, a), _bridge_for(cfg, b)
    assert ba and bb
    assert ba.get("available") is True and bb.get("available") is True
    assert ba.get("port") != bb.get("port"), "both projects share one bridge"
    pa, pb = ba.get("studio_pid"), bb.get("studio_pid")
    if pa and pb:
        assert pa != pb, "both bridges report the same Studio process"
    assert core.resolve_bridge(cfg, a)["port"] == ba["port"]
    assert core.resolve_bridge(cfg, b)["port"] == bb["port"]


def test_live_edit_in_each_studio_lands_on_the_right_project(studios):
    """Create a uniquely named marker folder in each project; each must appear
    in its own project's model and NOT in the other's."""
    cfg, a, b = studios
    tag = uuid.uuid4().hex[:8]
    marker = {a: f"LiveMultiA_{tag}", b: f"LiveMultiB_{tag}"}
    created: list[tuple[str, str]] = []
    try:
        for proj in (a, b):
            core.bridge_create_folder(cfg, proj, _MODEL_PARENT, marker[proj])
            created.append((proj, marker[proj]))
        for proj, other in ((a, b), (b, a)):
            here = _child_names(core.describe_node(cfg, proj, _MODEL_PARENT))
            there = _child_names(core.describe_node(cfg, other, _MODEL_PARENT))
            assert marker[proj] in here, f"edit for {proj!r} did not land on it"
            assert marker[proj] not in there, (
                f"edit for {proj!r} leaked into {other!r}")
    finally:
        for proj, name in created:
            try:
                core.bridge_delete_node(cfg, proj, f"{_MODEL_PARENT}/{name}")
            except Exception:
                pass


def test_live_both_emulators_up_on_their_own_web_ports(studios):
    """Start each project's emulator; both must be running and each project's
    own web port must accept connections, with the two ports different."""
    cfg, a, b = studios
    if core.attach_mode(cfg):
        pytest.skip("OPTIX_RUNTIME_URL set: runtime is external, no emulator")
    ports: dict[str, int] = {}
    for proj in (a, b):
        found = core.discover_web_port(cfg, proj)
        if not found.get("port"):
            pytest.skip(f"{proj!r} has no WebPresentationEngine port "
                        f"({found.get('source')}); add one on the bench")
        ports[proj] = int(found["port"])
    assert ports[a] != ports[b], (
        f"both projects use web port {ports[a]}; give them different ports")

    try:
        for proj in (a, b):
            core.run_emulator(cfg, proj, wait_ready=False)
        host = core.runtime_probe_host(cfg)
        deadline = time.monotonic() + _EMULATOR_TIMEOUT
        up: dict[str, bool] = {a: False, b: False}
        while time.monotonic() < deadline and not all(up.values()):
            for proj in (a, b):
                up[proj] = up[proj] or core._tcp_probe(host, ports[proj])
            if not all(up.values()):
                time.sleep(2)
        assert up[a], f"{a!r} emulator not serving on :{ports[a]}"
        assert up[b], f"{b!r} emulator not serving on :{ports[b]}"
        for proj in (a, b):
            st = core.emulator_status(cfg, project=proj)
            assert st["pids"], f"no emulator process attributed to {proj!r}"
            assert st["port"] == ports[proj], (
                f"status({proj!r}) reports port {st['port']}, not its own {ports[proj]}")
    finally:
        for proj in (a, b):
            try:
                st = core.emulator_status(cfg, project=proj)
                if st["pids"]:
                    core.stop_emulator(cfg, status=st)
            except Exception:
                pass
