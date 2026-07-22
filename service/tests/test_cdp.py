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

import pytest

from service import core, _cdp
from service.tests.conftest import FakeProc, make_fake_runner


class FakeWS:
    """Echoes a result (or error/event) for every CDP command sent."""

    def __init__(self, results=None, pre_events=None):
        self.results = results or {}          # method -> result dict | Exception
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
    assert out["state"] == "succeeded"


def test_cdp_screenshot_saves_file(cfg, fake_cdp, tmp_path: Path):
    jpeg = b"\xff\xd8\xff\xe0jpeg-bytes\xff\xd9"
    fake_cdp(results={"Page.captureScreenshot":
                      {"data": base64.b64encode(jpeg).decode()}})
    out_file = tmp_path / "sub" / "shot.jpg"
    out = core.cdp_screenshot_runtime(cfg, save_path=str(out_file))
    assert out["state"] == "succeeded"
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
    # a CDP command error during click → failed result, not a raise
    fake_cdp(results={"Input.dispatchMouseEvent": _cdp.CDPError("bad coords")})
    out = core.cdp_click_runtime(cfg, x=1, y=1)
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
    assert out["state"] == "succeeded"


def test_cdp_type_fails_loud_without_focus(cfg, fake_cdp):
    """Acceptance §5.3: no focused editable -> no_focused_input, not a silent no-op."""
    ws = fake_cdp(results={"Runtime.evaluate": {"result": {"value": "BODY"}}})
    out = core.cdp_type_runtime(cfg, "hello")
    assert out["state"] == "failed" and out["error"] == "no_focused_input"
    assert "optix_cdp_click" in out["hint"]
    assert not [p for (m, p, _) in ws.sent if m == "Input.insertText"]


def test_cdp_type_navigates_first_when_url_given(cfg, fake_cdp):
    ws = fake_cdp(results={"Runtime.evaluate": {"result": {"value": "CANVAS"}}})
    out = core.cdp_type_runtime(cfg, "x", navigate_url="http://localhost:8081/")
    assert out["navigated"] is True
    assert [p["url"] for (m, p, _) in ws.sent if m == "Page.navigate"] == ["http://localhost:8081/"]


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
    assert out["state"] == "succeeded"
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
    assert out["state"] == "succeeded"
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
    assert out["state"] == "succeeded"
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
    assert out["state"] == "succeeded"
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
    assert out["state"] == "succeeded"
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
