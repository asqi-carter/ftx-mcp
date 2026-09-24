"""Bridge transport diagnostics: bridge.jsonl logging, verbosity policy,
rotation, last-seen tracking, and _bridge_http instrumentation + retry.

These are the forensic + resilience additions that make the design-time bridge
diagnosable over long-running sessions (a rebuild silently drops the :8768
listener; the log must show WHEN and WHY)."""
import http.client
import urllib.error

import pytest

from service import core


@pytest.fixture(autouse=True)
def _reset_bridge_log_state():
    """The last-seen / health-transition trackers are module globals — reset
    them so each case starts from a known state."""
    core._bridge_last_ok_at = None
    core._bridge_last_health_ok = None
    yield


def _events(cfg):
    return core.bridge_log_tail(cfg, lines=1000).get("events", [])


def test_tail_absent_reports_no_log(cfg):
    res = core.bridge_log_tail(cfg)
    assert res["error"] == "no_bridge_log"
    assert res["last_ok"] is None


def test_bridge_event_writes_a_parsed_line(cfg):
    core.bridge_event(cfg, path="/bridge/x", method="GET", latency_ms=3,
                      status=200, ok=True, error=None)
    res = core.bridge_log_tail(cfg)
    assert res["returned"] == 1
    ev = res["events"][0]
    assert ev["path"] == "/bridge/x" and ev["status"] == 200 and ev["ok"] is True
    assert "ts" in ev


def test_health_polls_dedupe_to_transitions_only(cfg):
    # Steady successful health polls -> only the first (None->up) transition logs.
    for _ in range(5):
        core._bridge_log_call(cfg, "/bridge/health", "GET", 2, 200, None)
    evs = _events(cfg)
    assert len(evs) == 1 and evs[0]["ok"] is True

    # First failure (up->down) logs; repeats do NOT spam.
    core._bridge_log_call(cfg, "/bridge/health", "GET", 5000, None, "timed out")
    core._bridge_log_call(cfg, "/bridge/health", "GET", 5000, None, "timed out")
    evs = _events(cfg)
    assert len(evs) == 2 and evs[1]["ok"] is False and evs[1]["error"] == "timed out"

    # Recovery (down->up) logs.
    core._bridge_log_call(cfg, "/bridge/health", "GET", 2, 200, None)
    evs = _events(cfg)
    assert len(evs) == 3 and evs[2]["ok"] is True


def test_non_health_ops_and_failures_always_log(cfg):
    core._bridge_log_call(cfg, "/bridge/node/set", "POST", 10, 200, None)
    core._bridge_log_call(cfg, "/bridge/node/set", "POST", 10, 200, None)
    core._bridge_log_call(cfg, "/bridge/node/get", "GET", 8000, None, "connection aborted")
    evs = _events(cfg)
    assert len(evs) == 3
    assert evs[-1]["ok"] is False and "aborted" in evs[-1]["error"]


def test_last_ok_advances_only_on_success(cfg):
    assert core._bridge_last_ok_at is None
    core._bridge_log_call(cfg, "/bridge/health", "GET", 2, 200, None)
    ts = core._bridge_last_ok_at
    assert ts is not None
    # A failure must NOT advance last_ok.
    core._bridge_log_call(cfg, "/bridge/health", "GET", 5000, None, "timed out")
    assert core._bridge_last_ok_at == ts
    assert core.bridge_log_tail(cfg)["last_ok"] == ts


def test_log_rotates_when_oversized(cfg, monkeypatch):
    monkeypatch.setattr(core, "_BRIDGE_LOG_MAX_BYTES", 200)
    for i in range(60):
        core.bridge_event(cfg, path=f"/bridge/op/{i}", method="GET",
                          latency_ms=i, status=200, ok=True, error=None)
    logs = cfg.state_dir / "logs"
    assert (logs / "bridge.jsonl").is_file()
    assert (logs / "bridge.jsonl.1").is_file()


def test_bridge_http_logs_and_retries_transport_failure(cfg, monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class _Resp:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("boom")
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    status, body = core._bridge_http(cfg, "/bridge/thing", retries=1)
    assert status == 200 and calls["n"] == 2
    evs = _events(cfg)
    assert any(e["ok"] is False for e in evs)                              # the retried failure
    assert any(e["ok"] is True and e["path"] == "/bridge/thing" for e in evs)  # the success


def test_bridge_http_no_retry_by_default(cfg, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(core.BridgeUnavailable):
        core._bridge_http(cfg, "/bridge/thing")  # retries=0 default
    # the single failed attempt was still logged
    assert any(e["ok"] is False for e in _events(cfg))


# --- post-rebuild bridge-drop recovery nudge --------------------------------

def test_drop_note_quiet_when_bridge_never_used(cfg):
    core._bridge_last_ok_at = None  # never used this session
    assert core._bridge_drop_note(cfg) is None


def test_drop_note_quiet_when_bridge_survived(cfg, monkeypatch):
    core._bridge_last_ok_at = "2026-01-01T00:00:00+00:00"
    monkeypatch.setattr(core, "bridge_state", lambda c, force=False: {"available": True})
    assert core._bridge_drop_note(cfg) is None


def test_drop_note_fires_when_bridge_dropped(cfg, monkeypatch):
    core._bridge_last_ok_at = "2026-01-01T00:00:00+00:00"
    monkeypatch.setattr(core, "bridge_state", lambda c, force=False: {"available": False})
    note = core._bridge_drop_note(cfg)
    assert note and "StartBridge" in note


# --- IncompleteRead / RemoteDisconnected retry and classification ------------

def test_incomplete_read_retries_and_succeeds(cfg, monkeypatch):
    """IncompleteRead on the first attempt retries and returns the success body."""
    monkeypatch.setattr(core.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class _Resp:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise http.client.IncompleteRead(b"partial")
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    status, body = core._bridge_http(cfg, "/bridge/thing", retries=1)
    assert status == 200 and calls["n"] == 2
    evs = _events(cfg)
    assert any(e["ok"] is False for e in evs)   # the failed first attempt
    assert any(e["ok"] is True for e in evs)     # the successful retry


def test_incomplete_read_exhausted_raises_bridge_loading(cfg, monkeypatch):
    """When all retries are exhausted on IncompleteRead, BridgeLoading is raised."""
    monkeypatch.setattr(core.time, "sleep", lambda *_: None)

    def fake_urlopen(req, timeout=None):
        raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(core.BridgeLoading) as exc_info:
        core._bridge_http(cfg, "/bridge/thing", retries=1)
    assert exc_info.value.code == "bridge_loading"
    assert any(e["ok"] is False for e in _events(cfg))


def test_remote_disconnected_exhausted_raises_bridge_loading(cfg, monkeypatch):
    """When all retries are exhausted on RemoteDisconnected, BridgeLoading is raised."""
    monkeypatch.setattr(core.time, "sleep", lambda *_: None)

    def fake_urlopen(req, timeout=None):
        raise http.client.RemoteDisconnected("bridge dropped the connection")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(core.BridgeLoading) as exc_info:
        core._bridge_http(cfg, "/bridge/thing", retries=1)
    assert exc_info.value.code == "bridge_loading"


def test_incomplete_read_no_retry_by_default_raises_bridge_loading(cfg, monkeypatch):
    """With retries=0 (default), IncompleteRead immediately raises BridgeLoading."""
    def fake_urlopen(req, timeout=None):
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(core.BridgeLoading):
        core._bridge_http(cfg, "/bridge/thing")  # retries=0 default
    assert any(e["ok"] is False for e in _events(cfg))
