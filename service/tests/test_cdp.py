"""Tests for the CDP coordinate-click path (service/_cdp.py + core wrappers).

A fake WebSocket auto-answers each command by id, so these run without a live
Chrome. The seams are service._cdp._connect_ws (the socket) and
_discover_page_ws (target discovery).
"""
from __future__ import annotations

import base64
import dataclasses
import json
import urllib.request
from pathlib import Path

import importlib.util
import pytest

from service import core, _cdp
from service.tests.conftest import FakeProc, make_fake_runner, make_project


# Default Page.getLayoutMetrics response used by FakeWS when the test does not
# override it.  Matches cfg.cdp_viewport_width/height (1280x720) so that
# _resolve_point succeeds for any reasonable absolute-pixel coordinate without
# every click/fill test needing to stub this method explicitly.
_FAKE_WS_DEFAULTS: dict = {
    "Page.getLayoutMetrics": {
        "cssVisualViewport": {"clientWidth": 1280, "clientHeight": 720},
    },
}


class FakeWS:
    """Echoes a result (or error/event) for every CDP command sent."""

    def __init__(self, results=None, pre_events=None):
        # Merge caller overrides on top of the shared defaults so that tests
        # providing explicit Page.getLayoutMetrics values always win.
        self.results = {**_FAKE_WS_DEFAULTS, **(results or {})}
        self.pre_events = list(pre_events or [])  # event dicts emitted before first reply
        self.sent: list[tuple[str, dict, int]] = []
        self._outbox: list[str] = []

    def send(self, text):
        msg = json.loads(text)
        self.sent.append((msg["method"], msg.get("params", {}), msg["id"]))
        # emit any queued protocol events first (no id) to exercise the skip loop
        for ev in self.pre_events:
            self._outbox.append(json.dumps(ev))
        self.pre_events = []
        r = self.results.get(msg["method"], {})
        if isinstance(r, Exception):
            self._outbox.append(json.dumps(
                {"id": msg["id"], "error": {"message": str(r)}}))
        else:
            self._outbox.append(json.dumps({"id": msg["id"], "result": r}))

    def recv(self):
        return self._outbox.pop(0)

    def close(self):
        pass


@pytest.fixture
def fake_cdp(monkeypatch):
    """Install a FakeWS; return a setter so each test scripts its results."""
    holder = {}

    def install(results=None, pre_events=None):
        ws = FakeWS(results=results, pre_events=pre_events)
        holder["ws"] = ws
        monkeypatch.setattr(_cdp, "_discover_page_ws",
                            lambda url, timeout=5.0: "ws://x/devtools/page/1")
        monkeypatch.setattr(_cdp, "_connect_ws",
                            lambda url, timeout=30.0: ws)
        # don't actually sleep the navigate-settle in tests
        monkeypatch.setattr(core.time, "sleep", lambda s: None)
        return ws

    return install


def test_cdp_click_sends_trusted_mouse_sequence(cfg, fake_cdp):
    ws = fake_cdp()
    out = core.cdp_click_runtime(cfg, x=10, y=20)
    assert out["state"] == "succeeded" and out["navigated"] is False
    kinds = [(m, p.get("type")) for (m, p, _) in ws.sent if m == "Input.dispatchMouseEvent"]
    assert ("Input.dispatchMouseEvent", "mousePressed") in kinds
    assert ("Input.dispatchMouseEvent", "mouseReleased") in kinds
    press = next(p for (m, p, _) in ws.sent
                 if m == "Input.dispatchMouseEvent" and p.get("type") == "mousePressed")
    assert press["x"] == 10 and press["y"] == 20 and press["button"] == "left"


def test_cdp_click_navigates_first_when_url_given(cfg, fake_cdp):
    ws = fake_cdp()
    out = core.cdp_click_runtime(cfg, x=1, y=2, navigate_url="http://localhost:8081/")
    assert out["navigated"] is True
    navs = [p["url"] for (m, p, _) in ws.sent if m == "Page.navigate"]
    assert navs == ["http://localhost:8081/"]


def test_cdp_click_skips_protocol_events(cfg, fake_cdp):
    # an out-of-band event arrives before our reply; cmd() must skip it
    ws = fake_cdp(pre_events=[{"method": "Page.frameNavigated", "params": {}}])
    out = core.cdp_click_runtime(cfg, x=5, y=5)
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))


def test_cdp_screenshot_saves_file(cfg, fake_cdp, tmp_path: Path):
    jpeg = b"\xff\xd8\xff\xe0jpeg-bytes\xff\xd9"
    fake_cdp(results={"Page.captureScreenshot":
                      {"data": base64.b64encode(jpeg).decode()}})
    out_file = tmp_path / "sub" / "shot.jpg"
    out = core.cdp_screenshot_runtime(cfg, save_path=str(out_file))
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["path"] == str(out_file)
    assert out_file.read_bytes() == jpeg
    assert out["size_bytes"] == len(jpeg)


def test_cdp_screenshot_b64_when_no_path(cfg, fake_cdp):
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    fake_cdp(results={"Page.captureScreenshot":
                      {"data": base64.b64encode(jpeg).decode()}})
    out = core.cdp_screenshot_runtime(cfg)
    assert out["path"] is None
    assert base64.b64decode(out["b64"]) == jpeg


def _shot_results(jpeg: bytes, current_url: str | None = None) -> dict:
    r = {"Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()}}
    if current_url is not None:
        r["Page.getNavigationHistory"] = {
            "currentIndex": 0, "entries": [{"url": current_url}]}
    return r


def test_cdp_screenshot_auto_navigates_to_runtime_when_off_runtime(cfg, fake_cdp):
    # No navigate_url + tab is on about:blank → auto-target the runtime.
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results=_shot_results(jpeg, current_url="about:blank"))
    out = core.cdp_screenshot_runtime(cfg)
    assert out["state"] == "succeeded" and out["navigated"] is True
    navs = [p["url"] for (m, p, _) in ws.sent if m == "Page.navigate"]
    assert navs == [f"http://127.0.0.1:{cfg.runtime_test_port}/"]


def test_cdp_screenshot_skips_nav_when_already_on_runtime(cfg, fake_cdp):
    # No navigate_url + tab already on the runtime → do NOT reload (preserve state).
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    on = f"http://127.0.0.1:{cfg.runtime_test_port}/#/Screen1"
    ws = fake_cdp(results=_shot_results(jpeg, current_url=on))
    out = core.cdp_screenshot_runtime(cfg)
    assert out["state"] == "succeeded" and out["navigated"] is False
    assert [m for (m, _, _) in ws.sent if m == "Page.navigate"] == []


def test_cdp_screenshot_empty_url_never_navigates(cfg, fake_cdp):
    # navigate_url="" is the explicit opt-out: screenshot the current tab as-is.
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results=_shot_results(jpeg))
    out = core.cdp_screenshot_runtime(cfg, navigate_url="")
    assert out["navigated"] is False
    assert [m for (m, _, _) in ws.sent if m == "Page.navigate"] == []


def test_cdp_settle_uses_cfg_default(cfg, fake_cdp, monkeypatch):
    # When settle_seconds is not passed, the post-navigate wait comes from
    # cfg.cdp_settle_seconds (default 1.0, down from the old fixed 3.5).
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    fake_cdp(results={"Page.captureScreenshot":
                      {"data": base64.b64encode(jpeg).decode()}})
    recorded: list[float] = []
    monkeypatch.setattr(core.time, "sleep", lambda s: recorded.append(s))
    core.cdp_screenshot_runtime(cfg, navigate_url="http://x/")
    assert recorded == [cfg.cdp_settle_seconds]


def test_cdp_settle_explicit_override_wins(cfg, fake_cdp, monkeypatch):
    fake_cdp()
    recorded: list[float] = []
    monkeypatch.setattr(core.time, "sleep", lambda s: recorded.append(s))
    core.cdp_click_runtime(cfg, x=1, y=2, navigate_url="http://x/", settle_seconds=0.25)
    assert recorded == [0.25]


# ---- Problem #2: stale-tab self-heal after optix_restart_emulator ----------
#
# Root cause: after a restart the runtime process gets a new PID but keeps the
# SAME URL, so _point_screenshot_at_runtime's "already there" check sees the
# tab already pointed at the runtime and skips navigating. fresh=True forces a
# reload in that case, but a PLAIN reload can still be served from Chrome's
# cache instead of re-fetching from (and reconnecting to) the restarted
# process -- the observed "identical byte size across three restarts" bug.
# The fix has two parts, tested separately below:
#   1. the forced reload now passes ignoreCache=True (ignore_cache wiring).
#   2. a fresh=True capture that comes back byte-identical to the previous
#      fresh capture from the same endpoint triggers ONE recovery cycle
#      (session reconnect + hard reload) and a single re-capture.

def test_cdp_screenshot_fresh_reload_bypasses_cache_when_already_on_runtime(cfg, fake_cdp, monkeypatch):
    monkeypatch.setattr(core, "_LAST_FRESH_CAPTURE", {})
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    on = f"http://127.0.0.1:{cfg.runtime_test_port}/#/Screen1"
    ws = fake_cdp(results=_shot_results(jpeg, current_url=on))
    out = core.cdp_screenshot_runtime(cfg, fresh=True)
    assert out["state"] == "succeeded" and out["navigated"] is True
    reloads = [p for (m, p, _) in ws.sent if m == "Page.reload"]
    # Page.reload with ignoreCache=True -- a plain reload (ignoreCache=False,
    # the pre-fix behavior) can still be served from cache and never actually
    # re-points at the restarted runtime process.
    assert reloads == [{"ignoreCache": True}]


class _StaleFakeSess:
    """Session double for the stale-capture recovery path: records
    navigate/reload/close so the test can assert the recovery actually
    reconnects + hard-reloads rather than silently reusing the old tab."""

    def __init__(self):
        self.navigated_to: list[str] = []
        self.reloaded: list[bool] = []
        self.closed = 0

    def navigate(self, url):
        self.navigated_to.append(url)

    def reload(self, ignore_cache: bool = False):
        self.reloaded.append(ignore_cache)

    def close(self):
        self.closed += 1

    def set_viewport(self, *a, **k):
        return True


def _script_stale_capture_test(monkeypatch, responses: list[tuple]):
    """Shared rig for the stale-detection tests: fake _cdp_session (records
    each session opened) + fake _point_screenshot_at_runtime (pretend the tab
    is already on the runtime, navigated=True, so the initial forced-reload
    branch is out of scope here) + a scripted _cdp_capture_once returning
    `responses` in call order. Returns the list of sessions opened, in order."""
    monkeypatch.setattr(core, "_LAST_FRESH_CAPTURE", {})
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    monkeypatch.setattr(core, "_point_screenshot_at_runtime",
                         lambda c, sess, url, settle, project=None: True)
    sessions: list[_StaleFakeSess] = []

    def fake_session(c, **k):
        s = _StaleFakeSess()
        sessions.append(s)
        return s
    monkeypatch.setattr(core, "_cdp_session", fake_session)

    calls = {"n": 0}
    def fake_capture_once(sess, cfg_, quality, region):
        i = calls["n"]
        calls["n"] += 1
        return responses[i]
    monkeypatch.setattr(core, "_cdp_capture_once", fake_capture_once)
    return sessions


def test_cdp_screenshot_fresh_first_call_has_nothing_to_compare(cfg, monkeypatch):
    _script_stale_capture_test(monkeypatch, [(b"frame-A", None, None)])
    out = core.cdp_screenshot_runtime(cfg, fresh=True)
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert "stale_recovery" not in out


def test_cdp_screenshot_fresh_recovers_from_stale_capture(cfg, monkeypatch):
    sessions = _script_stale_capture_test(monkeypatch, [
        (b"frame-A", None, None),  # call #1 -> seeds the cache
        (b"frame-A", None, None),  # call #2, first attempt -> identical -> stale
        (b"frame-B", None, None),  # call #2, recovery attempt -> different
    ])
    out1 = core.cdp_screenshot_runtime(cfg, fresh=True)
    assert "stale_recovery" not in out1

    out2 = core.cdp_screenshot_runtime(cfg, fresh=True)
    assert out2["state"] == "succeeded"
    assert out2["stale_recovery"] == {"attempted": True, "resolved": True}
    assert out2["size_bytes"] == len(b"frame-B")
    assert "next_step" not in out2
    # call #1 session, call #2's initial (stale) session, call #2's recovery session
    assert len(sessions) == 3
    assert sessions[1].closed == 1  # the stale session was torn down, not reused
    assert sessions[2].navigated_to  # recovery re-navigated the fresh session
    assert sessions[2].reloaded == [True]  # ...and hard-reloaded (ignore_cache=True)


def test_cdp_screenshot_fresh_reports_unresolved_when_still_stale_after_recovery(cfg, monkeypatch):
    _script_stale_capture_test(monkeypatch, [
        (b"frame-A", None, None),
        (b"frame-A", None, None),
        (b"frame-A", None, None),  # recovery ALSO byte-identical
    ])
    out1 = core.cdp_screenshot_runtime(cfg, fresh=True)
    assert "stale_recovery" not in out1

    out2 = core.cdp_screenshot_runtime(cfg, fresh=True)
    assert out2["state"] == "succeeded"  # never raises -- surfaces the info instead
    assert out2["stale_recovery"] == {"attempted": True, "resolved": False}
    assert "next_step" in out2


def test_cdp_screenshot_no_stale_check_when_not_fresh(cfg, fake_cdp, monkeypatch):
    # fresh=False must NEVER consult/populate the stale-capture cache --
    # repeatedly confirming "nothing changed" is a legitimate, expected outcome.
    monkeypatch.setattr(core, "_LAST_FRESH_CAPTURE", {})
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    on = f"http://127.0.0.1:{cfg.runtime_test_port}/#/Screen1"
    fake_cdp(results=_shot_results(jpeg, current_url=on))
    out1 = core.cdp_screenshot_runtime(cfg)
    out2 = core.cdp_screenshot_runtime(cfg)
    assert "stale_recovery" not in out1
    assert "stale_recovery" not in out2
    assert core._LAST_FRESH_CAPTURE == {}


def test_cdp_unavailable_when_connect_fails(cfg, monkeypatch):
    monkeypatch.setattr(_cdp, "_discover_page_ws",
                        lambda url, timeout=5.0: "ws://x/1")

    def boom(url, timeout=30.0):
        raise OSError("connection refused")

    monkeypatch.setattr(_cdp, "_connect_ws", boom)
    with pytest.raises(core.CDPUnavailable):
        core.cdp_click_runtime(cfg, x=1, y=1)


def test_cdp_unavailable_when_no_page_target(cfg, monkeypatch):
    def no_target(url, timeout=5.0):
        raise _cdp.CDPError("no CDP page target")

    monkeypatch.setattr(_cdp, "_discover_page_ws", no_target)
    with pytest.raises(core.CDPUnavailable):
        core.cdp_screenshot_runtime(cfg)


def test_discover_page_ws_picks_page_target(monkeypatch):
    import urllib.request, io
    targets = [
        {"type": "background_page", "webSocketDebuggerUrl": "ws://x/bg"},
        {"type": "page", "webSocketDebuggerUrl": "ws://x/page/abc"},
    ]

    class FakeResp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): self.close()

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=5.0: FakeResp(json.dumps(targets).encode()))
    assert _cdp._discover_page_ws("http://127.0.0.1:9222") == "ws://x/page/abc"


def test_cdp_error_is_caught_and_returned(cfg, fake_cdp):
    # a CDP command error during click → failed result, not a raise.
    # Use absolute-pixel coordinates (both > 1.0) so they are not treated as
    # normalized fractions; (10, 10) is well within the default 1280x720 viewport.
    fake_cdp(results={"Input.dispatchMouseEvent": _cdp.CDPError("bad coords")})
    out = core.cdp_click_runtime(cfg, x=10, y=10)
    assert out["state"] == "failed" and "bad coords" in out["error"]


# ---- chrome-cdp health / ensure / self-heal ----------------------------

class _FakeHTTP:
    def __init__(self, body: bytes = b""):
        self._body = body
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _fake_urlopen(monkeypatch, routes: dict):
    """Route urlopen by URL substring (checked in insertion order). A value may
    be bytes (200 body) or an Exception (raised). Records (url, method)."""
    calls: list[tuple[str, str]] = []
    def fake(req, timeout=5.0):
        if hasattr(req, "full_url"):
            url, method = req.full_url, req.get_method()
        else:
            url, method = req, "GET"
        calls.append((url, method))
        for key, val in routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return _FakeHTTP(val)
        raise OSError(f"no route: {url}")
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


_PAGE = json.dumps([{"type": "page", "webSocketDebuggerUrl": "ws://x/1"}]).encode()
_NOPAGE = json.dumps([{"type": "background_page"}]).encode()


def test_probe_alive_with_page(monkeypatch):
    _fake_urlopen(monkeypatch, {"/json/version": b"{}", "/json": _PAGE})
    assert _cdp.probe("http://127.0.0.1:9222") == {"alive": True, "has_page": True}


def test_probe_alive_but_tabless(monkeypatch):
    _fake_urlopen(monkeypatch, {"/json/version": b"{}", "/json": _NOPAGE})
    assert _cdp.probe("http://127.0.0.1:9222") == {"alive": True, "has_page": False}


def test_probe_dead_endpoint(monkeypatch):
    _fake_urlopen(monkeypatch, {"/json/version": OSError("refused")})
    assert _cdp.probe("http://127.0.0.1:9222") == {"alive": False, "has_page": False}


def test_ensure_page_noop_when_page_exists(monkeypatch):
    calls = _fake_urlopen(monkeypatch, {"/json/version": b"{}", "/json": _PAGE})
    _cdp.ensure_page("http://127.0.0.1:9222")
    assert not any("/json/new" in u for (u, _) in calls)


def test_ensure_page_opens_via_put_when_tabless(monkeypatch):
    calls = _fake_urlopen(monkeypatch, {
        "/json/version": b"{}", "/json/new": b"{}", "/json": _NOPAGE})
    _cdp.ensure_page("http://127.0.0.1:9222")
    puts = [(u, m) for (u, m) in calls if "/json/new" in u]
    assert puts and puts[0][1] == "PUT"


def test_ensure_chrome_cdp_already_healthy(cfg, monkeypatch):
    monkeypatch.setattr(_cdp, "probe", lambda url, timeout=2.0: {"alive": True, "has_page": True})
    runner = make_fake_runner()
    out = core.ensure_chrome_cdp(cfg, runner=runner)
    assert out["state"] == "ok" and out["restarted"] is False
    assert runner.calls == []  # no process work when healthy


def test_ensure_chrome_cdp_opens_page_when_tabless(cfg, monkeypatch):
    monkeypatch.setattr(_cdp, "probe", lambda url, timeout=2.0: {"alive": True, "has_page": False})
    ep = {"n": 0}
    monkeypatch.setattr(_cdp, "ensure_page", lambda url, **k: ep.__setitem__("n", ep["n"] + 1))
    runner = make_fake_runner()
    out = core.ensure_chrome_cdp(cfg, runner=runner)
    assert out["state"] == "opened_page" and ep["n"] == 1
    assert runner.calls == []  # no task restart for a tab-less Chrome


def test_ensure_chrome_cdp_restarts_task_when_down(cfg, monkeypatch):
    seq = iter([{"alive": False, "has_page": False}, {"alive": True, "has_page": False}])
    monkeypatch.setattr(_cdp, "probe", lambda url, timeout=2.0: next(seq))
    monkeypatch.setattr(core, "_tcp_probe", lambda h, p, timeout=0.5: True)
    monkeypatch.setattr(_cdp, "ensure_page", lambda url, **k: None)
    runner = make_fake_runner()
    out = core.ensure_chrome_cdp(cfg, runner=runner, wait_seconds=1.0)
    assert out["state"] == "restarted" and out["restarted"] is True
    ran = [" ".join(c[0]) for c in runner.calls]
    assert any("schtasks" in r and core._CHROME_CDP_TASK in r for r in ran)


def test_ensure_chrome_cdp_no_restart_when_disabled(cfg, monkeypatch):
    monkeypatch.setattr(_cdp, "probe", lambda url, timeout=2.0: {"alive": False, "has_page": False})
    runner = make_fake_runner()
    out = core.ensure_chrome_cdp(cfg, runner=runner, allow_restart=False)
    assert out["state"] == "failed" and runner.calls == []


def test_ensure_chrome_cdp_failed_when_port_never_up(cfg, monkeypatch):
    monkeypatch.setattr(_cdp, "probe", lambda url, timeout=2.0: {"alive": False, "has_page": False})
    monkeypatch.setattr(core, "_tcp_probe", lambda h, p, timeout=0.5: False)
    runner = make_fake_runner()
    out = core.ensure_chrome_cdp(cfg, runner=runner, wait_seconds=0)
    assert out["state"] == "failed" and out["restarted"] is True


def test_ensure_chrome_cdp_failed_when_task_missing(cfg, monkeypatch):
    monkeypatch.setattr(_cdp, "probe", lambda url, timeout=2.0: {"alive": False, "has_page": False})
    def boom(cmd, **k): raise FileNotFoundError("schtasks")
    out = core.ensure_chrome_cdp(cfg, runner=core.Runner(fn=boom))
    assert out["state"] == "failed" and out["restarted"] is False
    assert "could not start" in out["detail"]


def test_cdp_session_selfheals_and_retries(cfg, monkeypatch):
    healed = dataclasses.replace(cfg, cdp_autoheal=True)
    n = {"c": 0}
    class OneShotFail:
        def __init__(self, url):
            n["c"] += 1
            if n["c"] == 1:
                raise _cdp.CDPError("no page target")

        def set_viewport(self, width, height, scale=1.0):
            # _cdp_session applies the viewport override to every session
            # (real CDPClient.set_viewport) — this fake stands in for it.
            return True
    monkeypatch.setattr(_cdp, "CDPClient", OneShotFail)
    monkeypatch.setattr(core, "ensure_chrome_cdp", lambda c, **k: {"state": "restarted"})
    core._cdp_session(healed)
    assert n["c"] == 2  # failed once → healed → retried once


def test_cdp_session_no_heal_when_disabled(cfg, monkeypatch):
    # cfg fixture has cdp_autoheal=False → no heal, raises straight through
    monkeypatch.setattr(_cdp, "CDPClient",
                        lambda url: (_ for _ in ()).throw(OSError("refused")))
    seen = {"n": 0}
    monkeypatch.setattr(core, "ensure_chrome_cdp",
                        lambda c, **k: seen.__setitem__("n", seen["n"] + 1) or {"state": "ok"})
    with pytest.raises(core.CDPUnavailable):
        core._cdp_session(cfg)
    assert seen["n"] == 0  # heal never invoked


# ---- keyboard input (v1.1 backlog 1.8, cdp-keyboard-input-spec.md) -----------

def test_cdp_type_inserts_text_when_input_focused(cfg, fake_cdp):
    ws = fake_cdp(results={"Runtime.evaluate": {"result": {"value": "CANVAS"}}})
    out = core.cdp_type_runtime(cfg, "hello")
    assert out["state"] == "succeeded" and out["typed_chars"] == 5
    assert out["active_element"] == "CANVAS"
    inserts = [p for (m, p, _) in ws.sent if m == "Input.insertText"]
    assert inserts == [{"text": "hello"}]


def test_cdp_type_input_overlay_counts_as_focused(cfg, fake_cdp):
    fake_cdp(results={"Runtime.evaluate": {"result": {"value": "INPUT"}}})
    out = core.cdp_type_runtime(cfg, "42")
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))


def test_cdp_type_fails_loud_without_focus(cfg, fake_cdp):
    """Acceptance §5.3: no focused editable -> no_focused_input, not a silent no-op."""
    ws = fake_cdp(results={"Runtime.evaluate": {"result": {"value": "BODY"}}})
    out = core.cdp_type_runtime(cfg, "hello")
    assert out["state"] == "failed" and out["error"] == "no_focused_input"
    assert "optix_interact(action='click')" in out["hint"]
    assert not [p for (m, p, _) in ws.sent if m == "Input.insertText"]


def test_cdp_type_navigates_first_when_url_given(cfg, fake_cdp):
    ws = fake_cdp(results={"Runtime.evaluate": {"result": {"value": "CANVAS"}}})
    out = core.cdp_type_runtime(cfg, "x", navigate_url="http://localhost:8081/")
    assert out["navigated"] is True
    assert [p["url"] for (m, p, _) in ws.sent if m == "Page.navigate"] == ["http://localhost:8081/"]


def test_cdp_key_navigates_first_when_url_given(cfg, fake_cdp):
    """cdp_key_runtime shares the _navigate_if_given block — an explicit URL
    navigates first and reports navigated=True."""
    ws = fake_cdp()
    out = core.cdp_key_runtime(cfg, "Enter", navigate_url="http://localhost:8081/")
    assert out["navigated"] is True
    assert [p["url"] for (m, p, _) in ws.sent if m == "Page.navigate"] == ["http://localhost:8081/"]


def test_cdp_key_no_url_does_not_navigate(cfg, fake_cdp):
    """No navigate_url -> _navigate_if_given is a no-op (navigated False); the
    key press acts on whatever the tab already shows. This is the regression
    guard for NOT routing key/click/type through _point_screenshot_at_runtime
    (which would auto-navigate to the runtime URL here)."""
    ws = fake_cdp()
    out = core.cdp_key_runtime(cfg, "Enter")
    assert out["navigated"] is False
    assert [m for (m, _, _) in ws.sent if m == "Page.navigate"] == []


class _SpySess:
    """Minimal sess double for _navigate_if_given: records navigate() calls."""

    def __init__(self):
        self.navigated_to: list[str] = []

    def navigate(self, url):
        self.navigated_to.append(url)


def test_navigate_if_given_no_url_is_noop(monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    sess = _SpySess()
    assert core._navigate_if_given(sess, None, 1.0) is False
    assert core._navigate_if_given(sess, "", 1.0) is False
    assert sess.navigated_to == []


def test_navigate_if_given_url_navigates_but_does_not_sleep(monkeypatch):
    # _navigate_if_given navigates but no longer sleeps; the settle is applied
    # unconditionally by the caller (cdp_click/type/key_runtime) after dispatch
    # so the total settle fires exactly once regardless of navigation.
    slept: list[float] = []
    monkeypatch.setattr(core.time, "sleep", lambda s: slept.append(s))
    sess = _SpySess()
    assert core._navigate_if_given(sess, "http://x/", 0.25) is True
    assert sess.navigated_to == ["http://x/"]
    assert slept == []


def test_cdp_key_enter_sends_down_up_with_commit_char(cfg, fake_cdp):
    ws = fake_cdp()
    out = core.cdp_key_runtime(cfg, "Enter")
    assert out["state"] == "succeeded" and out["key"] == "Enter"
    keys = [p for (m, p, _) in ws.sent if m == "Input.dispatchKeyEvent"]
    assert len(keys) == 2
    down, up = keys
    assert down["type"] == "keyDown" and down["text"] == "\r"
    assert down["windowsVirtualKeyCode"] == 13
    assert up["type"] == "keyUp" and up["key"] == "Enter"


def test_cdp_key_escape_is_raw_no_text(cfg, fake_cdp):
    ws = fake_cdp()
    core.cdp_key_runtime(cfg, "Escape")
    down = next(p for (m, p, _) in ws.sent if m == "Input.dispatchKeyEvent")
    assert down["type"] == "rawKeyDown" and "text" not in down
    assert down["windowsVirtualKeyCode"] == 27


def test_cdp_key_invalid_key_fails_loud(cfg, fake_cdp):
    """Unknown key -> invalid_key + the valid list, before any CDP traffic."""
    ws = fake_cdp()
    out = core.cdp_key_runtime(cfg, "F13")
    assert out["state"] == "failed" and out["error"] == "invalid_key"
    assert "Enter" in out["valid_keys"]
    assert not ws.sent  # rejected before opening traffic... (session opens lazily)


def test_cdp_type_reports_cdp_error(cfg, fake_cdp):
    fake_cdp(results={"Runtime.evaluate": _cdp.CDPError("boom")})
    out = core.cdp_type_runtime(cfg, "x")
    assert out["state"] == "failed" and "boom" in out["error"]


# ---- one-call fill (click + select-all + type + commit) ----------------------

def test_cdp_fill_full_sequence(cfg, fake_cdp):
    ws = fake_cdp(results={"Runtime.evaluate": {"result": {"value": "INPUT"}}})
    import service.core as core_mod
    out = core_mod.cdp_fill_runtime(cfg, x=119, y=187, text="hello")
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["steps"] == {"clicked": True, "focused_element": "INPUT",
                            "typed_chars": 5, "committed": "Enter"}
    methods = [m for (m, p, _) in ws.sent]
    # click (3 mouse events) -> ctrl+a (2 key events) -> insertText -> enter (2 key events)
    assert methods.count("Input.dispatchMouseEvent") == 3
    assert methods.count("Input.insertText") == 1
    keys = [p for (m, p, _) in ws.sent if m == "Input.dispatchKeyEvent"]
    assert len(keys) == 4
    assert keys[0]["key"] == "a" and keys[0]["modifiers"] == 2   # select-all
    assert keys[2]["key"] == "Enter"                              # commit
    # ordering: insertText comes after the ctrl+a and before the enter
    it = methods.index("Input.insertText")
    assert methods[:it].count("Input.dispatchKeyEvent") == 2


def test_cdp_fill_no_focus_reports_steps(cfg, fake_cdp):
    ws = fake_cdp(results={"Runtime.evaluate": {"result": {"value": "BODY"}}})
    from service import core as core_mod
    out = core_mod.cdp_fill_runtime(cfg, x=5, y=5, text="x")
    assert out["state"] == "failed" and out["error"] == "no_focused_input"
    assert out["steps"]["clicked"] is True and out["steps"]["typed_chars"] == 0
    assert not [p for (m, p, _) in ws.sent if m == "Input.insertText"]


def test_cdp_fill_submit_none_and_no_select_all(cfg, fake_cdp):
    ws = fake_cdp(results={"Runtime.evaluate": {"result": {"value": "INPUT"}}})
    from service import core as core_mod
    out = core_mod.cdp_fill_runtime(cfg, x=1, y=2, text="abc",
                                    submit=None, select_all=False)
    assert out["state"] == "succeeded" and out["steps"]["committed"] is None
    assert not [p for (m, p, _) in ws.sent if m == "Input.dispatchKeyEvent"]


def test_cdp_fill_invalid_submit_key(cfg, fake_cdp):
    fake_cdp()
    from service import core as core_mod
    out = core_mod.cdp_fill_runtime(cfg, x=1, y=2, text="x", submit="F13")
    assert out["state"] == "failed" and out["error"] == "invalid_key"


# ---- CDP viewport override (clipped-HMI fix: Emulation.setDeviceMetricsOverride) --
#
# Problem: chrome-cdp launches with --window-size=800,600, but the Optix
# runtime's cssVisualViewport measures 769x434 (scrollbar/chrome overhead) —
# the visible slice a raw screenshot captures, clipping the bottom/right of
# the HMI. The fix overrides the emulated device metrics to a larger,
# configurable size before every screenshot (default 1280x720 @ scale 1),
# and applies the SAME override at every CDP session's open (core._cdp_session)
# so a later, separate click call's coordinate space still matches.

def test_screenshot_applies_viewport_override_before_capture(cfg, fake_cdp):
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results={"Page.captureScreenshot":
                           {"data": base64.b64encode(jpeg).decode()}})
    out = core.cdp_screenshot_runtime(cfg, navigate_url="")
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    methods = [m for (m, _, _) in ws.sent]
    assert "Emulation.setDeviceMetricsOverride" in methods
    override_idx = methods.index("Emulation.setDeviceMetricsOverride")
    capture_idx = methods.index("Page.captureScreenshot")
    assert override_idx < capture_idx
    override_params = next(
        p for (m, p, _) in ws.sent if m == "Emulation.setDeviceMetricsOverride")
    assert override_params == {
        "width": cfg.cdp_viewport_width, "height": cfg.cdp_viewport_height,
        "deviceScaleFactor": cfg.cdp_viewport_scale, "mobile": False,
    }


def test_screenshot_viewport_override_applied_after_navigate(cfg, fake_cdp):
    # Auto-navigate case (navigate_url omitted, tab starts on about:blank):
    # the override must land AFTER Page.navigate, not before — a region
    # resolved against the pre-override viewport would be wrong.
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results=_shot_results(jpeg, current_url="about:blank"))
    out = core.cdp_screenshot_runtime(cfg)
    assert out["state"] == "succeeded" and out["navigated"] is True
    methods = [m for (m, _, _) in ws.sent]
    nav_idx = methods.index("Page.navigate")
    capture_idx = methods.index("Page.captureScreenshot")
    override_after_nav = [i for i, m in enumerate(methods)
                          if m == "Emulation.setDeviceMetricsOverride" and i > nav_idx]
    assert override_after_nav, "expected an override call after Page.navigate"
    assert override_after_nav[0] < capture_idx


def test_config_cdp_viewport_defaults_1280x720_scale_1(cfg):
    assert cfg.cdp_viewport_width == 1280
    assert cfg.cdp_viewport_height == 720
    assert cfg.cdp_viewport_scale == 1.0


def test_screenshot_uses_configured_viewport_values(cfg, fake_cdp):
    custom = dataclasses.replace(
        cfg, cdp_viewport_width=1920, cdp_viewport_height=1080,
        cdp_viewport_scale=2.0)
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results={"Page.captureScreenshot":
                           {"data": base64.b64encode(jpeg).decode()}})
    out = core.cdp_screenshot_runtime(custom, navigate_url="")
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    override_params = next(
        p for (m, p, _) in ws.sent if m == "Emulation.setDeviceMetricsOverride")
    assert override_params == {
        "width": 1920, "height": 1080, "deviceScaleFactor": 2.0, "mobile": False,
    }


def test_screenshot_survives_viewport_override_rejected(cfg, fake_cdp):
    # Older Chrome/CDP (or any transport hiccup) rejects the override command
    # — set_viewport swallows it and returns False; the screenshot must still
    # succeed at whatever size Chrome already has, never crash.
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results={
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
        "Emulation.setDeviceMetricsOverride": Exception("Not supported"),
    })
    out = core.cdp_screenshot_runtime(cfg, navigate_url="")
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert base64.b64decode(out["b64"]) == jpeg
    # the (failed) override was still attempted
    assert any(m == "Emulation.setDeviceMetricsOverride" for (m, _, _) in ws.sent)


def test_click_session_also_gets_viewport_override(cfg, fake_cdp):
    # The hard invariant: a LATER, separate click call (its own CDP session)
    # must see the same override a screenshot call applied, or coordinates
    # read off a screenshot land in the wrong place. core._cdp_session
    # applies it to every session, so a bare click call gets it too.
    ws = fake_cdp()
    out = core.cdp_click_runtime(cfg, x=10, y=20)
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    override_params = next(
        p for (m, p, _) in ws.sent if m == "Emulation.setDeviceMetricsOverride")
    assert override_params["width"] == cfg.cdp_viewport_width
    assert override_params["height"] == cfg.cdp_viewport_height


def test_set_viewport_returns_false_on_cdp_error(cfg, fake_cdp):
    fake_cdp(results={"Emulation.setDeviceMetricsOverride": _cdp.CDPError("nope")})
    sess = core._cdp_session(cfg)
    try:
        assert sess.set_viewport(1280, 720, 1.0) is False
    finally:
        sess.close()


# ---- coordinate resolution in cdp_click_runtime / cdp_fill_runtime ----------
#
# cdp_click_runtime and cdp_fill_runtime must:
#   1. Re-apply the viewport override (set_viewport) after navigation so the
#      coordinate space matches a screenshot taken under the same override.
#   2. Route (x, y) through _resolve_point: normalized fractions (both <= 1.0)
#      are multiplied by the CSS viewport; absolute pixels (either > 1.0) pass
#      through unchanged.
#   3. Return state:"failed", error:"bad_point" for out-of-frame coordinates.
#   4. Include css_x, css_y, viewport:{w,h}, coords in the success result.

def test_click_resolves_normalized_coords_to_css_pixels(cfg, fake_cdp):
    """(x, y) both <= 1.0 are treated as normalized fractions and multiplied
    by the CSS viewport size reported by Page.getLayoutMetrics."""
    ws = fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(800, 600),
    })
    out = core.cdp_click_runtime(cfg, x=0.5, y=0.25)
    assert out["state"] == "succeeded", out
    assert out["css_x"] == 400.0
    assert out["css_y"] == 150.0
    assert out["coords"] == "normalized"
    assert out["viewport"] == {"w": 800.0, "h": 600.0}
    press = next(p for (m, p, _) in ws.sent
                 if m == "Input.dispatchMouseEvent" and p.get("type") == "mousePressed")
    assert press["x"] == 400.0 and press["y"] == 150.0


def test_click_absolute_coords_pass_through_unchanged(cfg, fake_cdp):
    """(x, y) with either value > 1.0 are treated as absolute CSS pixels."""
    fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(1280, 720),
    })
    out = core.cdp_click_runtime(cfg, x=100.0, y=200.0)
    assert out["state"] == "succeeded", out
    assert out["css_x"] == 100.0
    assert out["css_y"] == 200.0
    assert out["coords"] == "absolute"
    assert out["viewport"] == {"w": 1280.0, "h": 720.0}


def test_click_bad_point_out_of_frame_returns_failed(cfg, fake_cdp):
    """A coordinate that lies outside the CSS viewport returns bad_point."""
    fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(800, 600),
    })
    out = core.cdp_click_runtime(cfg, x=900.0, y=300.0)
    assert out["state"] == "failed"
    assert out["error"] == "bad_point"
    assert "outside viewport" in out["detail"]
    # nothing dispatched
    assert not [m for (m, _, _) in core._cdp_session.__module__ and [] or []
                if m == "Input.dispatchMouseEvent"]


def test_click_applies_viewport_override_after_navigate_before_dispatch(cfg, fake_cdp):
    """The viewport re-apply (set_viewport) must come AFTER Page.navigate but
    BEFORE Input.dispatchMouseEvent, identical to _cdp_capture_once."""
    ws = fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(1280, 720),
    })
    out = core.cdp_click_runtime(cfg, x=10, y=20,
                                  navigate_url="http://localhost:8081/")
    assert out["state"] == "succeeded", out
    methods = [m for (m, _, _) in ws.sent]
    nav_idx = methods.index("Page.navigate")
    dispatch_idx = methods.index("Input.dispatchMouseEvent")
    override_after_nav = [i for i, m in enumerate(methods)
                          if m == "Emulation.setDeviceMetricsOverride" and i > nav_idx]
    assert override_after_nav, "expected set_viewport call after navigate"
    assert override_after_nav[0] < dispatch_idx, (
        "viewport override must precede mouse dispatch")


def test_fill_resolves_normalized_coords_to_css_pixels(cfg, fake_cdp):
    """cdp_fill_runtime applies the same normalized-coordinate resolution."""
    ws = fake_cdp(results={
        "Runtime.evaluate": {"result": {"value": "INPUT"}},
        "Page.getLayoutMetrics": _layout_metrics(1000, 500),
    })
    import service.core as core_mod
    out = core_mod.cdp_fill_runtime(cfg, x=0.1, y=0.4, text="42")
    assert out["state"] == "succeeded", out
    assert out["css_x"] == 100.0
    assert out["css_y"] == 200.0
    assert out["coords"] == "normalized"
    assert out["viewport"] == {"w": 1000.0, "h": 500.0}
    press = next(p for (m, p, _) in ws.sent
                 if m == "Input.dispatchMouseEvent" and p.get("type") == "mousePressed")
    assert press["x"] == 100.0 and press["y"] == 200.0


def test_fill_bad_point_out_of_frame_returns_failed(cfg, fake_cdp):
    """cdp_fill_runtime returns bad_point when the coordinate is outside the
    viewport — nothing is clicked or typed."""
    fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(800, 600),
    })
    import service.core as core_mod
    out = core_mod.cdp_fill_runtime(cfg, x=900.0, y=300.0, text="x")
    assert out["state"] == "failed"
    assert out["error"] == "bad_point"
    assert "outside viewport" in out["detail"]
    assert out["steps"]["clicked"] is False


# ---- screenshot region clipping (S4 feature 1: optix_cdp_screenshot region) --

def _layout_metrics(vp_w: float, vp_h: float) -> dict:
    return {"cssVisualViewport": {"clientWidth": vp_w, "clientHeight": vp_h}}


def test_screenshot_region_normalized_resolves_against_viewport(cfg, fake_cdp):
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results={
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
    })
    out = core.cdp_screenshot_runtime(cfg, navigate_url="", region=[0.1, 0.2, 0.5, 0.5])
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["region"] == [100.0, 160.0, 500.0, 400.0]
    clip = next(p for (m, p, _) in ws.sent if m == "Page.captureScreenshot")["clip"]
    assert clip == {"x": 100.0, "y": 160.0, "width": 500.0, "height": 400.0, "scale": 1}


def test_screenshot_region_pixel_passthrough(cfg, fake_cdp):
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results={
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
    })
    out = core.cdp_screenshot_runtime(cfg, navigate_url="", region=[100, 50, 200, 150])
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["region"] == [100.0, 50.0, 200.0, 150.0]
    clip = next(p for (m, p, _) in ws.sent if m == "Page.captureScreenshot")["clip"]
    assert clip == {"x": 100.0, "y": 50.0, "width": 200.0, "height": 150.0, "scale": 1}


def test_screenshot_region_none_skips_clip(cfg, fake_cdp):
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results={"Page.captureScreenshot":
                           {"data": base64.b64encode(jpeg).decode()}})
    out = core.cdp_screenshot_runtime(cfg, navigate_url="")
    assert out["region"] is None
    call = next(p for (m, p, _) in ws.sent if m == "Page.captureScreenshot")
    assert "clip" not in call


@pytest.mark.parametrize("bad_region", [
    [1, 2, 3],           # wrong length
    [-1, 0, 10, 10],      # negative x
    [0, 0, 0, 10],        # zero width
    [0, 0, 10, -5],       # negative height
    ["a", 0, 10, 10],     # non-numeric
])
def test_screenshot_region_malformed_never_touches_viewport(cfg, fake_cdp, bad_region):
    # shape/value errors are rejected before any CDP round trip - no
    # Page.getLayoutMetrics result is even stubbed here.
    ws = fake_cdp()
    out = core.cdp_screenshot_runtime(cfg, navigate_url="", region=bad_region)
    assert out["state"] == "failed" and out["error"] == "bad_region"
    assert out["region"] == bad_region
    assert [m for (m, _, _) in ws.sent if m == "Page.getLayoutMetrics"] == []


def test_screenshot_region_outside_frame_is_bad_region(cfg, fake_cdp):
    fake_cdp(results={"Page.getLayoutMetrics": _layout_metrics(1000, 800)})
    out = core.cdp_screenshot_runtime(cfg, navigate_url="", region=[2000, 0, 10, 10])
    assert out["state"] == "failed" and out["error"] == "bad_region"
    assert "outside viewport" in out["detail"]


def test_screenshot_region_composes_with_save_path(cfg, fake_cdp, tmp_path: Path):
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    fake_cdp(results={
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
        "Page.getLayoutMetrics": _layout_metrics(400, 300),
    })
    out_file = tmp_path / "clip.jpg"
    out = core.cdp_screenshot_runtime(
        cfg, navigate_url="", save_path=str(out_file), region=[0.0, 0.0, 1.0, 1.0])
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["region"] == [0.0, 0.0, 400.0, 300.0]
    assert out_file.read_bytes() == jpeg


# ---- find_text (S4 feature 3): CDP session assembly ---------------------

_FIND_TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "5\t1\t1\t1\t1\t1\t10\t10\t80\t30\t95.5\tStart\n"
    "5\t1\t1\t1\t1\t2\t95\t10\t100\t30\t92.0\tButton\n"
)


def test_find_text_captures_full_frame_and_runs_tesseract_tsv(cfg, fake_cdp, monkeypatch):
    import shutil
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results={
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
    })
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, _FIND_TSV))
    out = core.cdp_find_text_runtime(cfg, "Start Button", runner=runner)
    assert out["state"] == "succeeded" and out["found"] is True
    assert len(out["matches"]) == 1
    match = out["matches"][0]
    assert match["text"] == "Start Button"
    assert match["bbox_px"] == [10.0, 10.0, 185.0, 30.0]
    assert match["bbox_norm"] == [0.01, 0.0125, 0.185, 0.0375]
    assert match["center_px"] == [102.5, 25.0]
    assert out["viewport"] == {"w": 1000.0, "h": 800.0}
    # no `clip` on the capture — find_text is always full-frame
    shot_call = next(p for (m, p, _) in ws.sent if m == "Page.captureScreenshot")
    assert "clip" not in shot_call
    # tesseract invoked with tsv output, not --psm/stdout text mode
    cmd = runner.calls[0][0]
    assert cmd[0] == "/usr/bin/tesseract" and cmd[-1] == "tsv"


def test_find_text_no_match_is_not_an_error(cfg, fake_cdp, monkeypatch):
    import shutil
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    fake_cdp(results={
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
    })
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, _FIND_TSV))
    out = core.cdp_find_text_runtime(cfg, "Nonexistent Label", runner=runner)
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["found"] is False and out["matches"] == []


def test_find_text_reports_tesseract_nonzero(cfg, fake_cdp, monkeypatch):
    import shutil
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    fake_cdp(results={
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
    })
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(1, "", "leptonica error"))
    out = core.cdp_find_text_runtime(cfg, "Start", runner=runner)
    assert out["state"] == "failed" and "leptonica" in out["error"]
    assert out["found"] is False and out["matches"] == []


_FIND_TSV_SCALE = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    # box in the 2000x1600 IMAGE space (a 2x render of a 1000x800 viewport)
    "5\t1\t1\t1\t1\t1\t200\t100\t160\t60\t95.0\tFan\n"
)


@pytest.mark.skipif(importlib.util.find_spec("PIL") is None, reason="Pillow not installed in this venv")
def test_find_text_rescales_ocr_coords_to_css_under_devicescale(cfg, fake_cdp, monkeypatch):
    """OPTIX_CDP_SCALE>1 renders the capture LARGER than the CSS viewport, so
    tesseract's image-pixel boxes must be rescaled to CSS px — center_px feeds a
    click that dispatches in CSS px. A real 2000x1600 JPEG over a 1000x800
    viewport must halve every coordinate. Measured from the image, so it holds
    regardless of cfg.cdp_viewport_scale (self-correcting)."""
    import io
    import shutil

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2000, 1600), (0, 0, 0)).save(buf, format="JPEG")
    jpeg = buf.getvalue()
    fake_cdp(results={
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),  # CSS viewport
    })
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, _FIND_TSV_SCALE))
    out = core.cdp_find_text_runtime(cfg, "Fan", runner=runner)
    assert out["state"] == "succeeded" and out["found"] is True
    m = out["matches"][0]
    # image px [200,100,160,60] * 0.5 -> CSS px [100,50,80,30]
    assert m["bbox_px"] == [100.0, 50.0, 80.0, 30.0]
    assert m["center_px"] == [140.0, 65.0]  # (100+80/2, 50+30/2), in CSS px
    assert m["bbox_norm"] == [0.1, 0.0625, 0.08, 0.0375]  # normed to CSS vp
    assert out["viewport"] == {"w": 1000.0, "h": 800.0}


def test_find_text_scale_fallback_uses_cfg_when_pil_absent(cfg, fake_cdp, monkeypatch):
    """No Pillow to measure the image -> fall back to 1/OPTIX_CDP_SCALE. cfg
    scale=2 with an unmeasurable JPEG must still halve the coordinates."""
    import shutil
    monkeypatch.setattr(core, "_load_pil", lambda: None)
    cfg2 = dataclasses.replace(cfg, cdp_viewport_scale=2.0)
    jpeg = b"\xff\xd8jpeg\xff\xd9"  # unmeasurable; PIL is stubbed out regardless
    fake_cdp(results={
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
    })
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, _FIND_TSV_SCALE))
    out = core.cdp_find_text_runtime(cfg2, "Fan", runner=runner)
    m = out["matches"][0]
    assert m["bbox_px"] == [100.0, 50.0, 80.0, 30.0]
    assert m["center_px"] == [140.0, 65.0]


# ---- cdp_navigate (S5): blind navigation via banked routes files --------

def _write_routes(tmp_path: Path, routes: dict) -> Path:
    p = tmp_path / "routes.json"
    p.write_text(json.dumps({"version": 1, "routes": routes}))
    return p


def test_navigate_happy_path_resolves_coords_and_honors_settle_and_expect(
    cfg, fake_cdp, tmp_path: Path, monkeypatch,
):
    import shutil
    routes_file = _write_routes(tmp_path, {
        "setup-values": {"steps": [
            {"click": [0.5, 0.5], "settle_seconds": 0.25, "expect_text": "Setup Values"},
            {"click": [100, 50]},  # pixel-coord step (both > 1)
        ]}
    })
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
    })
    recorded_sleep: list[float] = []
    monkeypatch.setattr(core.time, "sleep", lambda s: recorded_sleep.append(s))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "Setup Values panel loaded"))

    out = core.cdp_navigate_runtime(
        cfg, route="setup-values", routes_path=str(routes_file),
        navigate_url="", runner=runner)

    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["route"] == "setup-values"
    assert out["steps_run"] == 2
    assert out["verified_steps"] == 1
    assert "ocr_unavailable" not in out
    assert out["navigated"] is False

    # step 1 normalized against the fake 1000x800 viewport; step 2 pixel passthrough
    presses = [(p["x"], p["y"]) for (m, p, _) in ws.sent
               if m == "Input.dispatchMouseEvent" and p.get("type") == "mousePressed"]
    assert presses == [(500.0, 400.0), (100.0, 50.0)]

    # step 1's explicit settle_seconds honored; step 2 falls back to cfg default
    assert recorded_sleep == [0.25, cfg.cdp_settle_seconds]

    # tesseract ran exactly once (only step 1 carries expect_text)
    assert len(runner.calls) == 1


def test_navigate_expectation_failure_stops_route_and_reports_readback(
    cfg, fake_cdp, tmp_path: Path, monkeypatch,
):
    import shutil
    routes_file = _write_routes(tmp_path, {
        "r1": {"steps": [
            {"click": [0.1, 0.1], "expect_text": "Never Shown"},
            {"click": [0.9, 0.9]},
        ]}
    })
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
    })
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "Something else entirely"))

    out = core.cdp_navigate_runtime(
        cfg, route="r1", routes_path=str(routes_file), navigate_url="", runner=runner)

    assert out["state"] == "failed"
    assert out["error"] == "expectation_failed"
    assert out["step"] == 0
    assert out["expected"] == "Never Shown"
    assert "Something else entirely" in out["read_back"]

    # only step 0's click happened; step 1 never ran
    presses = [p for (m, p, _) in ws.sent
               if m == "Input.dispatchMouseEvent" and p.get("type") == "mousePressed"]
    assert len(presses) == 1


def test_navigate_tesseract_absent_skips_checks_but_runs_all_clicks(
    cfg, fake_cdp, tmp_path: Path, monkeypatch,
):
    import shutil
    routes_file = _write_routes(tmp_path, {
        "r1": {"steps": [
            {"click": [0.1, 0.1], "expect_text": "Anything"},
            {"click": [0.2, 0.2]},
        ]}
    })
    ws = fake_cdp(results={"Page.getLayoutMetrics": _layout_metrics(1000, 800)})
    monkeypatch.setattr(core, "_find_tesseract", lambda: None)

    out = core.cdp_navigate_runtime(
        cfg, route="r1", routes_path=str(routes_file), navigate_url="")

    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["ocr_unavailable"] is True
    assert out["steps_run"] == 2
    assert out["verified_steps"] == 0
    presses = [p for (m, p, _) in ws.sent
               if m == "Input.dispatchMouseEvent" and p.get("type") == "mousePressed"]
    assert len(presses) == 2
    # never even captured a screenshot for OCR since tesseract is absent
    assert not [m for (m, _, _) in ws.sent if m == "Page.captureScreenshot"]


def test_navigate_missing_routes_file(cfg, fake_cdp, tmp_path: Path):
    ws = fake_cdp()
    out = core.cdp_navigate_runtime(
        cfg, route="r1", routes_path=str(tmp_path / "missing.json"))
    assert out["state"] == "failed" and out["error"] == "routes_file_not_found"
    assert not ws.sent  # never opened a CDP session for a file error


def test_navigate_invalid_json(cfg, fake_cdp, tmp_path: Path):
    routes_file = tmp_path / "routes.json"
    routes_file.write_text("{not valid json")
    ws = fake_cdp()
    out = core.cdp_navigate_runtime(cfg, route="r1", routes_path=str(routes_file))
    assert out["state"] == "failed" and out["error"] == "routes_file_invalid"
    assert not ws.sent


def test_navigate_unknown_route_lists_available(cfg, fake_cdp, tmp_path: Path):
    routes_file = _write_routes(tmp_path, {
        "alpha": {"steps": [{"click": [0, 0]}]},
        "beta": {"steps": [{"click": [0, 0]}]},
    })
    ws = fake_cdp()
    out = core.cdp_navigate_runtime(
        cfg, route="missing-route", routes_path=str(routes_file))
    assert out["state"] == "failed" and out["error"] == "route_not_found"
    assert out["available"] == ["alpha", "beta"]
    assert not ws.sent


def test_navigate_malformed_step_missing_click(cfg, fake_cdp, tmp_path: Path):
    routes_file = _write_routes(tmp_path, {
        "r1": {"steps": [{"settle_seconds": 0.1}]}
    })
    ws = fake_cdp()
    out = core.cdp_navigate_runtime(cfg, route="r1", routes_path=str(routes_file))
    assert out["state"] == "failed" and out["error"] == "route_invalid"
    assert out["step"] == 0
    assert not ws.sent


def test_navigate_malformed_step_wrong_shape_click(cfg, fake_cdp, tmp_path: Path):
    routes_file = _write_routes(tmp_path, {
        "r1": {"steps": [{"click": [0.1, 0.2, 0.3]}]}
    })
    ws = fake_cdp()
    out = core.cdp_navigate_runtime(cfg, route="r1", routes_path=str(routes_file))
    assert out["state"] == "failed" and out["error"] == "route_invalid"
    assert out["step"] == 0
    assert not ws.sent


def test_navigate_malformed_step_reported_at_correct_index(cfg, fake_cdp, tmp_path: Path):
    routes_file = _write_routes(tmp_path, {
        "r1": {"steps": [
            {"click": [0.1, 0.1]},
            {"click": "not-a-list"},
        ]}
    })
    fake_cdp()
    out = core.cdp_navigate_runtime(cfg, route="r1", routes_path=str(routes_file))
    assert out["state"] == "failed" and out["error"] == "route_invalid"
    assert out["step"] == 1


# ---- cdp_sweep (S6): visual baseline capture over banked routes ---------

def test_sanitize_route_filename_replaces_unsafe_chars():
    assert core._sanitize_route_filename("Setup Values") == "Setup-Values"
    assert core._sanitize_route_filename("a/b\\c") == "a-b-c"
    assert core._sanitize_route_filename("valid_Name-1.2") == "valid_Name-1.2"
    assert core._sanitize_route_filename("Setup / Values #1") == "Setup---Values--1"


def test_sweep_happy_path_writes_files_and_manifest(
    cfg, fake_cdp, tmp_path: Path, monkeypatch,
):
    import shutil
    routes_file = _write_routes(tmp_path, {
        "home": {"steps": [{"click": [0.5, 0.5]}]},
        "setup": {"steps": [{"click": [0.2, 0.2]}, {"click": [0.8, 0.8]}]},
    })
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
    })
    monkeypatch.setattr(core, "_find_tesseract", lambda: None)  # no tesseract here
    out_dir = tmp_path / "out"

    out = core.cdp_sweep_runtime(cfg, routes_path=str(routes_file), out_dir=str(out_dir))

    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert "errors" not in out
    assert out["version"] == 1
    assert out["ocr"] is False
    assert out["viewport"] == {"w": 1000.0, "h": 800.0}
    assert set(out["screens"].keys()) == {"home", "setup"}
    for route in ("home", "setup"):
        entry = out["screens"][route]
        assert entry["file"] == f"{route}.jpg"
        assert entry["size_bytes"] == len(jpeg)
        assert "text" not in entry
        assert (out_dir / f"{route}.jpg").read_bytes() == jpeg

    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["screens"]["home"]["file"] == "home.jpg"
    assert manifest["ocr"] is False

    # initial auto-target navigate + one reload between "home" and "setup"
    navs = [p["url"] for (m, p, _) in ws.sent if m == "Page.navigate"]
    assert len(navs) == 2


def test_sweep_route_subset_in_given_order(cfg, fake_cdp, tmp_path: Path, monkeypatch):
    import shutil
    routes_file = _write_routes(tmp_path, {
        "a": {"steps": [{"click": [0.1, 0.1]}]},
        "b": {"steps": [{"click": [0.2, 0.2]}]},
        "c": {"steps": [{"click": [0.3, 0.3]}]},
    })
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
    })
    monkeypatch.setattr(core, "_find_tesseract", lambda: None)
    out_dir = tmp_path / "out"
    out = core.cdp_sweep_runtime(
        cfg, routes_path=str(routes_file), out_dir=str(out_dir), routes=["c", "a"])
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert list(out["screens"].keys()) == ["c", "a"]
    assert (out_dir / "c.jpg").exists()
    assert (out_dir / "a.jpg").exists()
    assert not (out_dir / "b.jpg").exists()


def test_sweep_unknown_subset_route(cfg, fake_cdp, tmp_path: Path):
    routes_file = _write_routes(tmp_path, {
        "a": {"steps": [{"click": [0.1, 0.1]}]},
    })
    ws = fake_cdp()
    out = core.cdp_sweep_runtime(
        cfg, routes_path=str(routes_file), out_dir=str(tmp_path / "out"),
        routes=["missing"])
    assert out["state"] == "failed" and out["error"] == "route_not_found"
    assert out["available"] == ["a"]
    assert not ws.sent  # never opened a CDP session for a route-name error


def test_sweep_missing_routes_file(cfg, fake_cdp, tmp_path: Path):
    ws = fake_cdp()
    out = core.cdp_sweep_runtime(
        cfg, routes_path=str(tmp_path / "missing.json"), out_dir=str(tmp_path / "out"))
    assert out["state"] == "failed" and out["error"] == "routes_file_not_found"
    assert not ws.sent


def test_sweep_invalid_routes_json(cfg, fake_cdp, tmp_path: Path):
    routes_file = tmp_path / "routes.json"
    routes_file.write_text("{not valid json")
    ws = fake_cdp()
    out = core.cdp_sweep_runtime(
        cfg, routes_path=str(routes_file), out_dir=str(tmp_path / "out"))
    assert out["state"] == "failed" and out["error"] == "routes_file_invalid"
    assert not ws.sent


def test_sweep_warmup_discards_extra_capture(cfg, fake_cdp, tmp_path: Path, monkeypatch):
    import shutil
    routes_file = _write_routes(tmp_path, {
        "home": {"steps": [{"click": [0.5, 0.5]}]},
    })
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
    })
    monkeypatch.setattr(core, "_find_tesseract", lambda: None)
    out = core.cdp_sweep_runtime(
        cfg, routes_path=str(routes_file), out_dir=str(tmp_path / "out_warm"),
        warmup=True)
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    caps = [m for (m, _, _) in ws.sent if m == "Page.captureScreenshot"]
    assert len(caps) == 2  # 1 discard + 1 saved


def test_sweep_no_warmup_single_capture(cfg, fake_cdp, tmp_path: Path, monkeypatch):
    import shutil
    routes_file = _write_routes(tmp_path, {
        "home": {"steps": [{"click": [0.5, 0.5]}]},
    })
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    ws = fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
    })
    monkeypatch.setattr(core, "_find_tesseract", lambda: None)
    out = core.cdp_sweep_runtime(
        cfg, routes_path=str(routes_file), out_dir=str(tmp_path / "out_nowarm"),
        warmup=False)
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    caps = [m for (m, _, _) in ws.sent if m == "Page.captureScreenshot"]
    assert len(caps) == 1


def test_sweep_ocr_when_tesseract_available(cfg, fake_cdp, tmp_path: Path, monkeypatch):
    import shutil
    routes_file = _write_routes(tmp_path, {
        "home": {"steps": [{"click": [0.5, 0.5]}]},
    })
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
    })
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "Line One\n\nLine Two  \n"))
    out_dir = tmp_path / "out_ocr"
    out = core.cdp_sweep_runtime(
        cfg, routes_path=str(routes_file), out_dir=str(out_dir), runner=runner)
    assert out["ocr"] is True
    assert out["screens"]["home"]["text"] == ["Line One", "Line Two"]
    cmd = runner.calls[0][0]
    assert cmd[0] == "/usr/bin/tesseract"
    assert cmd[1] == str(out_dir / "home.jpg")
    assert cmd[2:] == ["stdout", "--psm", "6"]


def test_sweep_per_route_capture_error_continues(cfg, fake_cdp, tmp_path: Path, monkeypatch):
    import shutil
    routes_file = _write_routes(tmp_path, {
        "good": {"steps": [{"click": [0.1, 0.1]}]},
        "bad": {"steps": [{"click": [-1, 0.1]}]},  # invalid: negative coord
        "good2": {"steps": [{"click": [0.2, 0.2]}]},
    })
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    fake_cdp(results={
        "Page.getLayoutMetrics": _layout_metrics(1000, 800),
        "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
    })
    monkeypatch.setattr(core, "_find_tesseract", lambda: None)
    out_dir = tmp_path / "out_err"
    out = core.cdp_sweep_runtime(cfg, routes_path=str(routes_file), out_dir=str(out_dir))
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["errors"] == 1
    assert "error" in out["screens"]["bad"]
    assert out["screens"]["good"]["file"] == "good.jpg"
    assert out["screens"]["good2"]["file"] == "good2.jpg"
    assert (out_dir / "good.jpg").exists()
    assert (out_dir / "good2.jpg").exists()
    assert not (out_dir / "bad.jpg").exists()


def test_sweep_cdp_transport_error_mid_sweep_continues(cfg, tmp_path: Path, monkeypatch):
    """A genuine CDP transport error (not a step-validation error) on one
    route's capture is caught and recorded, and the sweep continues."""
    import shutil
    from service import _cdp as _cdp_mod

    routes_file = _write_routes(tmp_path, {
        "a": {"steps": [{"click": [0.1, 0.1]}]},
        "b": {"steps": [{"click": [0.2, 0.2]}]},
        "c": {"steps": [{"click": [0.3, 0.3]}]},
    })
    jpeg = b"\xff\xd8jpeg\xff\xd9"

    class FlakyWS(FakeWS):
        def __init__(self):
            super().__init__(results={
                "Page.getLayoutMetrics": _layout_metrics(1000, 800),
                "Page.captureScreenshot": {"data": base64.b64encode(jpeg).decode()},
            })
            self._shot_calls = 0

        def send(self, text):
            msg = json.loads(text)
            if msg["method"] == "Page.captureScreenshot":
                self._shot_calls += 1
                if self._shot_calls == 3:  # route "b"'s warmup-discard capture
                    self.sent.append((msg["method"], msg.get("params", {}), msg["id"]))
                    self._outbox.append(json.dumps(
                        {"id": msg["id"], "error": {"message": "transport boom"}}))
                    return
            super().send(text)

    ws = FlakyWS()
    monkeypatch.setattr(_cdp_mod, "_discover_page_ws",
                        lambda url, timeout=5.0: "ws://x/devtools/page/1")
    monkeypatch.setattr(_cdp_mod, "_connect_ws", lambda url, timeout=30.0: ws)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    monkeypatch.setattr(core, "_find_tesseract", lambda: None)

    out = core.cdp_sweep_runtime(
        cfg, routes_path=str(routes_file), out_dir=str(tmp_path / "out_flaky"))

    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["errors"] == 1
    assert "transport boom" in out["screens"]["b"]["error"]
    assert out["screens"]["a"]["file"] == "a.jpg"
    assert out["screens"]["c"]["file"] == "c.jpg"


# ---- routes file management (S7): service-owned routes CRUD ------------
#
# Motivation (see core.py's S7 comment): a field test needed to CREATE a
# routes file and had no MCP tool for it, so the model reached for host
# folder access instead. These tests pin routes_save/get/list as the
# service-owned replacement for that gap.

def test_routes_save_happy_path_round_trips_into_navigate(
    cfg, fake_cdp, projects_root: Path,
):
    """save's returned path loads via _load_routes_file AND drives
    cdp_navigate_runtime end-to-end — proving the save->navigate loop a
    caller is meant to use never needs local file access."""
    make_project(projects_root, "Alpha")
    ws = fake_cdp(results={"Page.getLayoutMetrics": _layout_metrics(1000, 800)})

    out = core.routes_save(
        cfg, "Alpha",
        {"version": 1, "routes": {"home": {"steps": [{"click": [0.5, 0.5]}]}}},
    )
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["routes"] == ["home"]
    assert out["bytes"] > 0
    path = out["path"]
    assert path.endswith(str(Path("dev") / "ftx_ui_map.json"))

    data, err = core._load_routes_file(path)
    assert err is None
    assert data["routes"]["home"]["steps"][0]["click"] == [0.5, 0.5]

    nav = core.cdp_navigate_runtime(cfg, route="home", routes_path=path, navigate_url="")
    assert nav["state"] == "succeeded"
    assert nav["steps_run"] == 1
    presses = [(p["x"], p["y"]) for (m, p, _) in ws.sent
               if m == "Input.dispatchMouseEvent" and p.get("type") == "mousePressed"]
    assert presses == [(500.0, 400.0)]


def test_routes_save_accepts_bare_inner_mapping_and_normalizes(
    cfg, projects_root: Path,
):
    """Passing just the {name: {steps: [...]}} mapping (no version/routes
    wrapper) is accepted and normalized to the versioned shape on disk."""
    make_project(projects_root, "Alpha")
    out = core.routes_save(
        cfg, "Alpha", {"home": {"steps": [{"click": [0.1, 0.1]}]}}, name="bare",
    )
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["routes"] == ["home"]
    data, err = core._load_routes_file(out["path"])
    assert err is None
    assert data == {"version": 1, "routes": {"home": {"steps": [{"click": [0.1, 0.1]}]}}}


@pytest.mark.parametrize("bad_name", [
    "../escape", "..", "a/b", "a\\b", ".hidden", ".", "",
])
def test_routes_save_rejects_bad_names(cfg, projects_root: Path, bad_name):
    make_project(projects_root, "Alpha")
    out = core.routes_save(cfg, "Alpha", {"home": {"steps": [{"click": [0, 0]}]}},
                           name=bad_name)
    assert out["state"] == "failed"
    assert out["error"] == "bad_name"


def test_routes_save_rejects_invalid_step_names_route_and_index(
    cfg, projects_root: Path,
):
    make_project(projects_root, "Alpha")
    out = core.routes_save(cfg, "Alpha", {
        "good": {"steps": [{"click": [0.1, 0.1]}]},
        "bad": {"steps": [{"click": [0.1, 0.1]}, {"no_click": True}]},
    })
    assert out["state"] == "failed"
    assert out["error"] == "routes_invalid"
    assert out["route"] == "bad"
    assert out["step"] == 1
    # nothing written on a rejected save
    assert not (projects_root / "Alpha" / "dev").exists()


def test_routes_save_overwrite_replaces_content_wholesale(cfg, projects_root: Path):
    make_project(projects_root, "Alpha")
    first = core.routes_save(cfg, "Alpha", {"a": {"steps": [{"click": [0, 0]}]}})
    assert first["state"] == "succeeded"
    second = core.routes_save(cfg, "Alpha", {"b": {"steps": [{"click": [1, 1]}]}})
    assert second["state"] == "succeeded"
    assert second["path"] == first["path"]

    data, err = core._load_routes_file(second["path"])
    assert err is None
    # "a" is gone — overwrite replaces, it does not merge
    assert list(data["routes"].keys()) == ["b"]


def test_routes_save_unknown_project_raises(cfg):
    with pytest.raises(core.ProjectNotFound):
        core.routes_save(cfg, "NoSuchProject", {"a": {"steps": [{"click": [0, 0]}]}})


def test_routes_get_happy_path(cfg, projects_root: Path):
    make_project(projects_root, "Alpha")
    saved = core.routes_save(cfg, "Alpha", {"home": {"steps": [{"click": [0.2, 0.3]}]}})
    out = core.routes_get(cfg, "Alpha")
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["path"] == saved["path"]
    assert out["routes"]["routes"]["home"]["steps"][0]["click"] == [0.2, 0.3]


def test_routes_get_not_found_names_the_path(cfg, projects_root: Path):
    make_project(projects_root, "Alpha")
    out = core.routes_get(cfg, "Alpha", name="missing")
    assert out["state"] == "failed"
    assert out["error"] == "routes_file_not_found"
    assert out["path"].endswith(str(Path("dev") / "missing.json"))


def test_routes_get_unknown_project_raises(cfg):
    with pytest.raises(core.ProjectNotFound):
        core.routes_get(cfg, "NoSuchProject")


def test_routes_list_counts_valid_and_skips_junk(cfg, projects_root: Path):
    project_dir = make_project(projects_root, "Alpha")
    core.routes_save(cfg, "Alpha", {"home": {"steps": [{"click": [0, 0]}]}}, name="one")
    core.routes_save(cfg, "Alpha", {"setup": {"steps": [{"click": [0, 0]}]}}, name="two")
    (project_dir / "dev" / "junk.json").write_text("not valid json")

    out = core.routes_list(cfg, "Alpha")
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    assert out["count"] == 2
    assert out["skipped"] == 1
    names = {f["name"] for f in out["files"]}
    assert names == {"one", "two"}
    one = next(f for f in out["files"] if f["name"] == "one")
    assert one["routes"] == ["home"]
    assert "mtime" in one


def test_routes_list_no_dev_dir_is_empty_not_an_error(cfg, projects_root: Path):
    make_project(projects_root, "Alpha")
    out = core.routes_list(cfg, "Alpha")
    assert out == {"state": "succeeded", "files": [], "count": 0, "skipped": 0}


def test_routes_list_unknown_project_raises(cfg):
    with pytest.raises(core.ProjectNotFound):
        core.routes_list(cfg, "NoSuchProject")


def test_routes_save_preserves_extra_top_level_keys(cfg, projects_root):
    """A combined cache (routes + screen-structure notes in ONE dev/ file -
    the blind-authoring workflow) saves cleanly: extras ride through
    verbatim, and the file still loads for navigate."""
    from service import core
    make_project(projects_root, "Alpha")
    payload = {
        "version": 1,
        "routes": {"home": {"steps": [{"click": [0.5, 0.5]}]}},
        "structure": {"SetupValuesPage": {"row_container": "UI/Rows", "index_base": 1}},
        "notes": ["description auto-fills from row 2"],
    }
    out = core.routes_save(cfg, "Alpha", payload)
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    import json as _json
    from pathlib import Path
    on_disk = _json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert on_disk["structure"]["SetupValuesPage"]["index_base"] == 1
    assert on_disk["notes"] == ["description auto-fills from row 2"]
    data, err = core._load_routes_file(out["path"])
    assert err is None and "home" in data["routes"]


def test_routes_roundtrip_preserves_non_ascii(cfg, projects_root):
    """2026-07-23 field bug: routes_save wrote UTF-8 but the loader read with
    the platform default codec, mojibaking an em dash on Windows. The full
    save -> get round-trip must preserve non-ASCII exactly."""
    from service import core
    make_project(projects_root, "Alpha")
    payload = {
        "version": 1,
        "routes": {"overview": {"steps": [
            {"click": [0.5, 0.5], "expect_text": "LINE 4 — OVERVIEW"}]}},
        "structure": {"MainWindow": {"title": "LINE 4 — OVERVIEW"}},
    }
    out = core.routes_save(cfg, "Alpha", payload)
    assert out["state"] == "succeeded", (out.get("reason_code"), out.get("detail"))
    back = core.routes_get(cfg, "Alpha")
    assert back["state"] == "succeeded"
    assert back["routes"]["structure"]["MainWindow"]["title"] == "LINE 4 — OVERVIEW"
    assert back["routes"]["routes"]["overview"]["steps"][0]["expect_text"] == "LINE 4 — OVERVIEW"


# ---- Bug-exact coordinate resolution: normalized vs absolute on 1280x720 ----
#
# The bug: normalized fractions (both <= 1.0) were dispatched raw as pixel
# coordinates, so x=0.606 landed at pixel 0.606 instead of 775.68.
# After the fix, _resolve_point multiplies by the CSS viewport dimensions.
# These two tests pin the exact arithmetic using the default 1280x720
# layout-metrics fixture and together reflect the actual bug shape: an agent
# might pass normalized (0.606, 0.193) or the approximate pixel (775, 139)
# intending the same target — both must resolve through their correct path.

def test_normalized_coords_1280x720_dispatch_at_775_68_138_96(cfg, fake_cdp):
    """cdp_click_runtime(x=0.606, y=0.193) on the default 1280x720 fake
    viewport must dispatch at exactly 775.68 / 138.96 (= 0.606*1280,
    0.193*720) — the arithmetic from the bug description."""
    # FakeWS default already returns 1280x720 for Page.getLayoutMetrics.
    ws = fake_cdp()
    out = core.cdp_click_runtime(cfg, x=0.606, y=0.193)
    assert out["state"] == "succeeded", out
    assert out["coords"] == "normalized"
    assert out["css_x"] == pytest.approx(775.68)
    assert out["css_y"] == pytest.approx(138.96)
    assert out["viewport"] == {"w": 1280.0, "h": 720.0}
    press = next(p for (m, p, _) in ws.sent
                 if m == "Input.dispatchMouseEvent" and p.get("type") == "mousePressed")
    assert press["x"] == pytest.approx(775.68)
    assert press["y"] == pytest.approx(138.96)


def test_absolute_coords_775_139_dispatch_unchanged_on_1280x720(cfg, fake_cdp):
    """cdp_click_runtime(x=775, y=139) has x > 1.0 so is treated as absolute
    CSS pixels and passes through unchanged — the absolute-pixel half of the
    same bug shape."""
    ws = fake_cdp()
    out = core.cdp_click_runtime(cfg, x=775, y=139)
    assert out["state"] == "succeeded", out
    assert out["coords"] == "absolute"
    assert out["css_x"] == 775.0
    assert out["css_y"] == 139.0
    press = next(p for (m, p, _) in ws.sent
                 if m == "Input.dispatchMouseEvent" and p.get("type") == "mousePressed")
    assert press["x"] == 775.0 and press["y"] == 139.0


# ---- cdp_viewport_scale=2: dispatched point stays in CSS px, not device px --
#
# OPTIX_CDP_SCALE=2 renders screenshots at 2× device pixels but
# Input.dispatchMouseEvent still operates in CSS pixels (the same coordinate
# space as the browser's pointer model and Page.captureScreenshot's clip).
# A click at absolute (400, 200) must dispatch at exactly (400, 200) under
# scale=2 — NOT at (800, 400). Mirrors the OCR rescaling tested by
# test_find_text_rescales_ocr_coords_to_css_under_devicescale.

def test_click_viewport_scale2_dispatches_in_css_px_not_device_px(cfg, fake_cdp):
    """scale=2 changes the captured pixel density but NOT the click coordinate
    space: dispatched (400, 200) must arrive at (400, 200), not (800, 400)."""
    cfg2 = dataclasses.replace(cfg, cdp_viewport_scale=2.0)
    ws = fake_cdp()  # returns 1280x720 CSS viewport via Page.getLayoutMetrics
    out = core.cdp_click_runtime(cfg2, x=400, y=200)
    assert out["state"] == "succeeded", out
    assert out["css_x"] == 400.0
    assert out["css_y"] == 200.0
    press = next(p for (m, p, _) in ws.sent
                 if m == "Input.dispatchMouseEvent" and p.get("type") == "mousePressed")
    assert press["x"] == 400.0 and press["y"] == 200.0
    # The override is sent with the configured scale factor
    override = next(p for (m, p, _) in ws.sent
                    if m == "Emulation.setDeviceMetricsOverride")
    assert override["deviceScaleFactor"] == 2.0


# ---- Out-of-bounds bad_point + fill resolves identically to click -----------

def test_click_x2000_returns_bad_point_and_dispatches_nothing_on_1280_viewport(
        cfg, fake_cdp):
    """x=2000 on a 1280-wide viewport: state='failed', error='bad_point', and
    no Input.dispatchMouseEvent is sent (the point is rejected before click)."""
    ws = fake_cdp()  # default 1280x720
    out = core.cdp_click_runtime(cfg, x=2000, y=300)
    assert out["state"] == "failed"
    assert out["error"] == "bad_point"
    assert "outside viewport" in out["detail"]
    dispatched = [m for (m, _, _) in ws.sent if m == "Input.dispatchMouseEvent"]
    assert dispatched == [], "bad_point must dispatch nothing"


def test_fill_x775_y139_resolves_identically_to_click(cfg, fake_cdp):
    """cdp_fill_runtime(x=775, y=139) must dispatch mousePressed at (775, 139)
    — the same resolved point as cdp_click_runtime — confirming that fill
    routes through the same _resolve_point logic (absolute-pixel branch)."""
    ws = fake_cdp(results={"Runtime.evaluate": {"result": {"value": "INPUT"}}})
    out = core.cdp_fill_runtime(cfg, x=775, y=139, text="42")
    assert out["state"] == "succeeded", out
    assert out["coords"] == "absolute"
    assert out["css_x"] == 775.0
    assert out["css_y"] == 139.0
    press = next(p for (m, p, _) in ws.sent
                 if m == "Input.dispatchMouseEvent" and p.get("type") == "mousePressed")
    assert press["x"] == 775.0 and press["y"] == 139.0


# ---- settle_seconds: monkeypatched time.sleep assertions --------------------
#
# settle_seconds=0.25 with no navigate_url: click, fill, and screenshot each
# call time.sleep with the settle value.  With navigate_url, the settle fires
# ONCE (after dispatch/capture) — _navigate_if_given and
# _point_screenshot_at_runtime no longer sleep themselves, so the total settle
# is applied exactly once regardless of whether a navigation occurred.

def test_click_no_url_settle_seconds_calls_sleep_once(cfg, fake_cdp, monkeypatch):
    """settle_seconds=0.25 with no navigate_url: exactly one sleep(0.25)
    fires after the mouse dispatch."""
    fake_cdp()
    recorded: list[float] = []
    monkeypatch.setattr(core.time, "sleep", lambda s: recorded.append(s))
    out = core.cdp_click_runtime(cfg, x=10, y=20, settle_seconds=0.25)
    assert out["state"] == "succeeded", out
    assert recorded == [0.25]


def test_fill_settle_seconds_0_25_fires_after_type(cfg, fake_cdp, monkeypatch):
    """settle_seconds=0.25 on fill: the 0.25 settle sleep fires after
    insert_text.  The internal 0.3 s focus-wait and the 0.25 settle are the
    only two sleeps (no extra sleep for navigation)."""
    fake_cdp(results={"Runtime.evaluate": {"result": {"value": "INPUT"}}})
    recorded: list[float] = []
    monkeypatch.setattr(core.time, "sleep", lambda s: recorded.append(s))
    out = core.cdp_fill_runtime(cfg, x=10, y=20, text="x", settle_seconds=0.25)
    assert out["state"] == "succeeded", out
    assert 0.25 in recorded          # settle fires
    assert len(recorded) == 2        # focus-wait (0.3) + settle (0.25)


def test_screenshot_settle_seconds_0_25_with_navigate_url_calls_sleep_once(
        cfg, fake_cdp, monkeypatch):
    """settle_seconds=0.25 with navigate_url: exactly one sleep(0.25)
    fires before capture (_point_screenshot_at_runtime does not sleep)."""
    jpeg = b"\xff\xd8jpeg\xff\xd9"
    fake_cdp(results={"Page.captureScreenshot":
                      {"data": base64.b64encode(jpeg).decode()}})
    recorded: list[float] = []
    monkeypatch.setattr(core.time, "sleep", lambda s: recorded.append(s))
    out = core.cdp_screenshot_runtime(
        cfg, navigate_url="http://localhost:8081/", settle_seconds=0.25)
    assert out["state"] == "succeeded", out
    assert recorded == [0.25]


def test_click_navigate_url_settle_fires_once_not_twice(cfg, fake_cdp, monkeypatch):
    """With navigate_url, the settle sleep fires ONCE after dispatch — not
    twice. _navigate_if_given navigates but never sleeps, so there is no
    double-settle when a URL is provided."""
    fake_cdp()
    recorded: list[float] = []
    monkeypatch.setattr(core.time, "sleep", lambda s: recorded.append(s))
    out = core.cdp_click_runtime(cfg, x=10, y=20,
                                  navigate_url="http://localhost:8081/",
                                  settle_seconds=0.25)
    assert out["state"] == "succeeded", out
    assert recorded == [0.25], (
        f"expected exactly one settle sleep, got: {recorded}"
    )
