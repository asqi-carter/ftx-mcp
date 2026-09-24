"""Offline regression tests for five field-reported bridge/registry scenarios.

All tests are fully offline: no live Studio, no network, no real processes.
``core._bridge_http`` and ``LOCALAPPDATA`` are monkeypatched throughout.

Scenarios
---------
1. Stale 8768.json + live 8769 not in registry: resolves in one
   ``_scan_bridge_ports`` call, deletes the stale registry file, and returns
   the live bridge via the range-scan fallback — demonstrating the
   all-stale → fallback path introduced in 1.0.8.

2. Registry-only live entry serving a DIFFERENT project: ``_require_bridge_for``
   raises ``BridgeUnavailable`` whose message contains per-port detail
   (``port=… serving=…``) but no bare ``serving=`` without a port prefix.

3. Timed-out registry entry is NOT unlinked: a health probe that raises
   ``BridgeUnavailable`` without a ``ConnectionRefusedError`` cause (i.e. a
   timeout, not a definitive "nothing listening") must leave the registry file
   intact so an arming bridge is not evicted prematurely.

4. ``optix_describe_node`` against an unresolvable project returns the
   ``_bridge_guarded`` structured error shape (a dict, never a raised
   exception).  ``reset_bridge_cache`` is called between cases.

5. ``IncompleteRead`` is retried once then classified as ``BridgeLoading``:
   with ``retries=1`` the transport-retry loop fires once; after both attempts
   fail, ``_bridge_http`` raises ``BridgeLoading`` (not ``BridgeUnavailable``).
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from service import core


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_registry(tmp_path: Path, entries: dict[int, dict]) -> Path:
    """Create a bridge registry directory under tmp_path.

    ``entries`` maps port -> health-response payload dict.  Each entry writes
    ``<port>.json`` under ``<tmp_path>/ftx-mcp/bridges/``.  Returns the
    registry directory path.
    """
    registry_dir = tmp_path / "ftx-mcp" / "bridges"
    registry_dir.mkdir(parents=True)
    for port, payload in entries.items():
        f = registry_dir / f"{port}.json"
        f.write_text(json.dumps({"port": port, **payload}), encoding="utf-8")
    return registry_dir


def _get_describe_node_fn(cfg: core.Config):
    """Return the direct (sync) callable for optix_describe_node from the MCP app."""
    from service.mcp_app import make_mcp
    mcp = make_mcp(cfg)
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    tool = tools["optix_describe_node"]
    # Offloaded tools store the original sync fn at _ftx_sync_fn; fast tools
    # expose it directly as .fn.
    return getattr(tool, "_ftx_sync_fn", tool.fn)


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_bridge_cache(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[return]
    """Wipe the bridge-state TTL cache before and after every test."""
    core.reset_bridge_cache()
    # Suppress the retry-loop sleep so these offline tests run fast.
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    yield
    core.reset_bridge_cache()


# ===========================================================================
# Scenario 1 — stale 8768.json + live 8769 not in registry
# ===========================================================================

def test_stale_registry_plus_live_port_not_in_registry(
    multi_bridge_cfg: core.Config,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Registry has 8768.json (stale) only; 8769 is live but not in the registry.

    Expected behaviour (1.0.8 all-stale fallback):
    * ``_scan_bridge_ports`` probes the single registry entry (port 8768).
    * Port 8768 is connection-refused → stale file is deleted.
    * All registry entries were stale → execution falls through to the
      port-range scan within the SAME call.
    * Port 8769 answers → returned with ``source='scan'``.
    * Result: one call, stale file gone, live bridge found.
    """
    registry_dir = _make_registry(tmp_path, {
        8768: {"project": "StaleProj", "bridge_version": "1.0.8", "model_loaded": True},
    })
    stale_file = registry_dir / "8768.json"
    assert stale_file.exists()

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def fake_http(cfg_: core.Config, path: str, **_kw: object) -> tuple[int, bytes]:
        port = urlparse(cfg_.bridge_url).port
        if port == 8769 and path.startswith("/bridge/health"):
            return 200, json.dumps({
                "project": "LiveProj", "bridge_version": "1.0.8",
                "model_loaded": True, "port": 8769,
            }).encode()
        # Port 8768 (and any other port) — connection refused (stale entry).
        raise core.BridgeUnavailable(f"nothing on {port}") from ConnectionRefusedError()

    monkeypatch.setattr(core, "_bridge_http", fake_http)

    results = core._scan_bridge_ports(multi_bridge_cfg, force=True)

    # Stale registry file must have been deleted.
    assert not stale_file.exists(), (
        "stale 8768.json was not deleted after a connection-refused probe"
    )

    # The live bridge on 8769 must appear via the range-scan fallback.
    available = [r for r in results if r.get("available")]
    assert len(available) == 1, f"expected 1 available bridge, got {available}"
    live = available[0]
    assert live["port"] == 8769
    assert live["project"] == "LiveProj"
    assert live["source"] == "scan"


# ===========================================================================
# Scenario 2 — registry-only live entry serving a different project
# ===========================================================================

def test_require_bridge_for_different_project_error_format(
    multi_bridge_cfg: core.Config,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A registry entry that is live but serves the WRONG project triggers
    ``BridgeUnavailable`` from ``_require_bridge_for``.

    The error message must contain per-port detail strings like
    ``port=8768 serving='OtherProj'`` (with a port prefix) and must NOT
    contain a bare ``serving=`` without a preceding ``port=`` qualifier.
    """
    _make_registry(tmp_path, {
        8768: {"project": "OtherProj", "bridge_version": "1.0.8", "model_loaded": True},
    })
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def fake_http(cfg_: core.Config, path: str, **_kw: object) -> tuple[int, bytes]:
        port = urlparse(cfg_.bridge_url).port
        if port == 8768 and path.startswith("/bridge/health"):
            return 200, json.dumps({
                "project": "OtherProj", "bridge_version": "1.0.8",
                "model_loaded": True, "port": 8768,
            }).encode()
        raise core.BridgeUnavailable(f"nothing on {port}") from ConnectionRefusedError()

    monkeypatch.setattr(core, "_bridge_http", fake_http)

    with pytest.raises(core.BridgeUnavailable) as exc_info:
        core._require_bridge_for(multi_bridge_cfg, "WantedProj")

    msg = str(exc_info.value)

    # Must contain per-port project information.
    assert "ports_probed=" in msg, f"ports_probed missing from: {msg!r}"
    assert "port=8768" in msg, f"port= qualifier missing from: {msg!r}"
    assert "serving='OtherProj'" in msg or 'serving="OtherProj"' in msg, (
        f"per-port project name missing from: {msg!r}"
    )

    # Must NOT contain a bare "serving=" that is not preceded by "port=".
    import re
    bare_serving = re.search(r"(?<!port=\d{4} )(?<!port=\d )serving=", msg)
    # More robust: verify every "serving=" occurrence is preceded by "port=<digits>"
    # within the same entry string by checking the ports_probed section structure.
    # The format is: port=N serving='X' source='Y' available=B
    # So "serving=" should always come after "port=…" in the entry.
    entries_section = re.search(r"ports_probed=\[(.+)\]", msg)
    if entries_section:
        entries_text = entries_section.group(1)
        # Each "serving=" must be preceded by "port=" in its entry.
        for m in re.finditer(r"serving=", entries_text):
            preceding = entries_text[max(0, m.start() - 30):m.start()]
            assert "port=" in preceding, (
                f"bare 'serving=' found without preceding 'port=': {entries_text!r}"
            )


# ===========================================================================
# Scenario 3 — timed-out registry entry is NOT unlinked
# ===========================================================================

def test_timed_out_registry_entry_not_deleted(
    multi_bridge_cfg: core.Config,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A health-probe timeout must leave the registry file intact.

    A Studio process still loading its model (arming) can cause the health probe
    to time out even though the process is alive.  Only a definitive
    connection-refused answer justifies deleting the file.  A
    ``BridgeUnavailable`` raised WITHOUT a ``ConnectionRefusedError`` as its
    cause (the timeout signature) must therefore NOT trigger file deletion.
    """
    registry_dir = _make_registry(tmp_path, {
        9001: {"project": "Arming", "bridge_version": "1.0.8", "model_loaded": False},
    })
    arming_file = registry_dir / "9001.json"
    assert arming_file.exists()

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def fake_http(cfg_: core.Config, path: str, **_kw: object) -> tuple[int, bytes]:
        # Timeout — BridgeUnavailable WITHOUT a ConnectionRefusedError cause.
        raise core.BridgeUnavailable("timed out waiting for port 9001")

    monkeypatch.setattr(core, "_bridge_http", fake_http)

    core._scan_bridge_ports(multi_bridge_cfg, force=True)

    assert arming_file.exists(), (
        "registry file was incorrectly deleted after a timeout probe; "
        "only a connection-refused failure should remove it"
    )


# ===========================================================================
# Scenario 4 — optix_describe_node unresolvable project → _bridge_guarded shape
# ===========================================================================

def test_describe_node_unresolvable_project_returns_guarded_shape(
    multi_bridge_cfg: core.Config,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``optix_describe_node`` against a project no bridge is serving must
    return the ``_bridge_guarded`` structured error dict — never raise.

    The shape contract: the returned dict must have ``state='failed'`` and a
    ``reason_code`` key (set by ``classify_bridge_failure``).

    ``reset_bridge_cache`` is called between the two sub-cases to ensure each
    probe is fresh.
    """
    # No registry dir → no registry entries → range scan → nothing listening.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def fake_http(cfg_: core.Config, path: str, **_kw: object) -> tuple[int, bytes]:
        port = urlparse(cfg_.bridge_url).port
        raise core.BridgeUnavailable(f"nothing on {port}") from ConnectionRefusedError()

    monkeypatch.setattr(core, "_bridge_http", fake_http)
    # Also suppress studio_guard so classify_bridge_failure doesn't need real processes.
    from service import studio_guard
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])

    describe_node = _get_describe_node_fn(multi_bridge_cfg)

    # --- sub-case A: bridge completely absent ---
    core.reset_bridge_cache()
    result_a = describe_node(path="UI/MainWindow", project="NoSuchProject")

    assert isinstance(result_a, dict), (
        f"expected a dict from _bridge_guarded, got {type(result_a).__name__}"
    )
    assert result_a.get("state") == "failed", (
        f"expected state='failed', got {result_a!r}"
    )
    assert "reason_code" in result_a, (
        f"reason_code missing from guarded response: {result_a!r}"
    )

    # --- sub-case B: bridge up but serving a different project ---
    core.reset_bridge_cache()

    def fake_http_wrong(cfg_: core.Config, path: str, **_kw: object) -> tuple[int, bytes]:
        port = urlparse(cfg_.bridge_url).port
        if path.startswith("/bridge/health"):
            return 200, json.dumps({
                "project": "DifferentProject",
                "bridge_version": "1.0.8",
                "model_loaded": True,
                "port": port,
            }).encode()
        raise core.BridgeUnavailable("wrong project path")

    monkeypatch.setattr(core, "_bridge_http", fake_http_wrong)

    result_b = describe_node(path="UI/MainWindow", project="NoSuchProject")

    assert isinstance(result_b, dict), (
        f"expected a dict from _bridge_guarded, got {type(result_b).__name__}"
    )
    assert result_b.get("state") == "failed", (
        f"expected state='failed', got {result_b!r}"
    )
    assert "reason_code" in result_b, (
        f"reason_code missing from guarded response: {result_b!r}"
    )


# ===========================================================================
# Scenario 5 — IncompleteRead retried once then classified as BridgeLoading
# ===========================================================================

def test_incomplete_read_retried_once_then_bridge_loading(
    cfg: core.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``retries=1``, ``_bridge_http`` retries the transport-failure path
    once on ``http.client.IncompleteRead``.  After two failed attempts (the
    initial one plus the one retry) it raises ``BridgeLoading`` — not
    ``BridgeUnavailable`` — because a truncated response means the bridge
    accepted the connection and is mid-rebuild, not absent.
    """
    attempt_log: list[int] = []

    original_bridge_http = core._bridge_http  # noqa: F841

    def fake_http(cfg_: core.Config, path: str, timeout: float = 5.0,
                  retries: int = 0, **_kw: object) -> tuple[int, bytes]:
        """Simulate _bridge_http internals: raise IncompleteRead on every attempt,
        honoring the retries budget so the retry logic is exercised as-written."""
        import random
        attempt = 0
        while True:
            attempt_log.append(attempt)
            if attempt < retries:
                attempt += 1
                # (sleep is already patched to a no-op by the autouse fixture)
                continue
            raise core.BridgeLoading(
                f"bridge at {cfg_.bridge_url}{path} returned a partial response "
                f"(mid-rebuild?): IncompleteRead(0 bytes read)"
            )

    # We want to test _bridge_http's own IncompleteRead handling directly,
    # so we patch at the urllib.request level instead of _bridge_http itself,
    # to exercise the real retry loop in core._bridge_http.
    import urllib.request

    call_count: list[int] = []

    class FakeOpener:
        def open(self, req: object, timeout: object = None) -> object:
            raise http.client.IncompleteRead(b"")

    original_build_opener = urllib.request.build_opener

    def fake_build_opener(*handlers: object) -> FakeOpener:
        return FakeOpener()  # type: ignore[return-value]

    # Patch urlopen to raise IncompleteRead every time.
    original_urlopen = urllib.request.urlopen

    def fake_urlopen(req: object, timeout: object = None) -> object:
        call_count.append(1)
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    # _bridge_http with retries=1: should attempt twice then raise BridgeLoading.
    with pytest.raises(core.BridgeLoading) as exc_info:
        core._bridge_http(cfg, "/bridge/health", retries=1)

    # Two total attempts: the initial one + one retry.
    assert len(call_count) == 2, (
        f"expected 2 urlopen calls (1 initial + 1 retry), got {len(call_count)}"
    )

    # The exception must be BridgeLoading, not BridgeUnavailable.
    assert isinstance(exc_info.value, core.BridgeLoading)
    assert "partial response" in str(exc_info.value) or "IncompleteRead" in str(exc_info.value), (
        f"unexpected BridgeLoading message: {exc_info.value}"
    )
