"""CDP page-target recovery: one reload + one retry, flagged cdp_recovered.

A fake CDP socket hangs (TimeoutError on recv) or errors for the first N
occurrences of a method, then answers normally. Seams are the same as
test_cdp.py (service._cdp._connect_ws / _discover_page_ws).
"""
from __future__ import annotations

import base64
import json

import pytest

from service import _cdp, core
from service.tests.test_cdp import FakeWS


@pytest.fixture(autouse=True)
def _f5_target_autouse(f5_target_studio) -> None:
    """A Studio is attributed to every project so F5 is reachable (#92 guard)."""


class FlakyWS(FakeWS):
    """FakeWS whose `method` fails `fail_times` times (hang or CDP error)."""

    def __init__(self, method, fail_times=1, mode="hang", message="x"):
        super().__init__(results={
            "Page.captureScreenshot": {"data": base64.b64encode(b"\xff\xd8jpg").decode()},
        })
        self.method, self.fail_left = method, fail_times
        self.mode, self.message = mode, message
        self._hang_next = False

    def send(self, text):
        msg = json.loads(text)
        if msg["method"] == self.method and self.fail_left > 0:
            self.fail_left -= 1
            self.sent.append((msg["method"], msg.get("params", {}), msg["id"]))
            if self.mode == "hang":
                self._hang_next = True
            else:
                self._outbox.append(json.dumps(
                    {"id": msg["id"], "error": {"message": self.message}}))
            return
        super().send(text)

    def recv(self):
        if self._hang_next:
            self._hang_next = False
            raise TimeoutError("timed out")
        return super().recv()

    def count(self, method):
        return sum(1 for m, _, _ in self.sent if m == method)


@pytest.fixture
def install(monkeypatch):
    def _install(ws):
        monkeypatch.setattr(_cdp, "_discover_page_ws",
                            lambda url, timeout=5.0: "ws://x/devtools/page/1")
        monkeypatch.setattr(_cdp, "_connect_ws", lambda url, timeout=30.0: ws)
        monkeypatch.setattr(core.time, "sleep", lambda s: None)
        return ws
    return _install


def test_click_hang_once_recovers_and_retries_once(cfg, install):
    ws = install(FlakyWS("Input.dispatchMouseEvent", 1, "hang"))
    out = core.cdp_click_runtime(cfg, x=10, y=20)
    assert out["state"] == "succeeded"
    assert out["cdp_recovered"] is True
    assert ws.count("Page.reload") == 1
    assert ws.count("Input.dispatchMouseEvent") >= 2  # failed try + retry


def test_observe_hang_once_recovers(cfg, install):
    ws = install(FlakyWS("Page.captureScreenshot", 1, "hang"))
    out = core.cdp_screenshot_runtime(cfg)
    assert out["state"] == "succeeded"
    assert out["cdp_recovered"] is True
    assert ws.count("Page.reload") == 1
    assert ws.count("Page.captureScreenshot") == 2


@pytest.mark.parametrize("msg", ["Target not attached", "No page for target"])
def test_not_attached_or_no_page_triggers_one_reload(cfg, install, msg):
    ws = install(FlakyWS("Page.captureScreenshot", 1, "error", msg))
    out = core.cdp_screenshot_runtime(cfg)
    assert out["state"] == "succeeded" and out["cdp_recovered"] is True
    assert ws.count("Page.reload") == 1


def test_first_try_success_does_not_reload_or_flag(cfg, install):
    ws = install(FlakyWS("Input.dispatchMouseEvent", 0))
    out = core.cdp_click_runtime(cfg, x=10, y=20)
    assert out["state"] == "succeeded"
    assert "cdp_recovered" not in out
    assert ws.count("Page.reload") == 0


def test_retry_failure_surfaces_error_without_further_reload(cfg, install):
    ws = install(FlakyWS("Page.captureScreenshot", 5, "error", "not attached"))
    out = core.cdp_screenshot_runtime(cfg)
    assert out["state"] == "failed"
    assert "not attached" in out["error"]
    assert out["cdp_recovered"] is False          # never None / absent
    assert out["cdp_recovery"]["retry_error"]
    assert ws.count("Page.reload") == 1
    assert ws.count("Page.captureScreenshot") == 2


def test_retry_hang_raises_without_further_reload(cfg, install):
    ws = install(FlakyWS("Input.dispatchMouseEvent", 5, "hang"))
    with pytest.raises(TimeoutError):
        core.cdp_click_runtime(cfg, x=10, y=20)
    assert ws.count("Page.reload") == 1
    assert ws.count("Input.dispatchMouseEvent") == 2


def test_unrelated_error_is_not_retried(cfg, install):
    ws = install(FlakyWS("Page.captureScreenshot", 5, "error", "boom"))
    out = core.cdp_screenshot_runtime(cfg)
    assert out["state"] == "failed"
    assert ws.count("Page.reload") == 0
    assert ws.count("Page.captureScreenshot") == 1


# ---- detached page after an emulator restart (ftx-mcp-xyc.3) -----------

class DetachedWS(FakeWS):
    """A page target Chrome still lists but that answers every command with
    'Not attached to an active page' (the post emulator-restart state)."""

    def send(self, text):
        msg = json.loads(text)
        self.sent.append((msg["method"], msg.get("params", {}), msg["id"]))
        self._outbox.append(json.dumps(
            {"id": msg["id"],
             "error": {"message": "Not attached to an active page"}}))


class FakeChrome:
    """Stateful fake DevTools endpoint: tabs listed by id, each with a WS."""

    def __init__(self, monkeypatch, detached_ids=("t1",)):
        self.tabs: dict[str, FakeWS] = {}
        self.closed: list[str] = []
        self.opened: list[str] = []
        self._n = 0
        for tid in detached_ids:
            self.tabs[tid] = DetachedWS()
        monkeypatch.setattr(_cdp, "list_pages", self._list_pages)
        monkeypatch.setattr(_cdp, "open_tab", self._open_tab)
        monkeypatch.setattr(_cdp, "close_tab", self._close_tab)
        monkeypatch.setattr(_cdp, "probe",
                            lambda url, timeout=2.0: {"alive": True,
                                                      "has_page": bool(self.tabs)})
        monkeypatch.setattr(_cdp, "_discover_page_ws",
                            lambda url, timeout=5.0: self._ws_url(next(iter(self.tabs))))
        monkeypatch.setattr(_cdp, "_connect_ws", self._connect_ws)
        monkeypatch.setattr(core.time, "sleep", lambda s: None)

    @staticmethod
    def _ws_url(tid):
        return f"ws://x/devtools/page/{tid}"

    def _list_pages(self, url, timeout=5.0):
        return [{"id": t, "type": "page", "url": "about:blank",
                 "webSocketDebuggerUrl": self._ws_url(t)} for t in self.tabs]

    def _healthy_ws(self):
        return FakeWS(results={
            "Runtime.evaluate": {"result": {"type": "number", "value": 2}},
            "Page.captureScreenshot": {
                "data": base64.b64encode(b"\xff\xd8jpg").decode()},
        })

    def _open_tab(self, url, page_url="about:blank", timeout=5.0):
        self._n += 1
        tid = f"new{self._n}"
        self.tabs[tid] = self._healthy_ws()
        self.opened.append(tid)
        return {"id": tid, "webSocketDebuggerUrl": self._ws_url(tid)}

    def _close_tab(self, url, tid, timeout=5.0):
        self.closed.append(tid)
        self.tabs.pop(tid, None)
        return True

    def _connect_ws(self, ws_url, timeout=30.0):
        return self.tabs[ws_url.rsplit("/", 1)[1]]


def test_page_probe_detects_detached_page_on_page_enable(monkeypatch):
    chrome = FakeChrome(monkeypatch)
    out = _cdp.page_probe("http://127.0.0.1:9222")
    assert out["healthy"] is False and out["not_attached"] is True


def test_cdp_restart_detects_detached_page_and_recreates(cfg, monkeypatch):
    chrome = FakeChrome(monkeypatch)
    out = core.ensure_chrome_cdp(cfg, allow_restart=False)
    assert out["detail"] != "already healthy"
    assert out["state"] == "recreated_page" and out["restarted"] is True
    assert chrome.closed == ["t1"] and len(chrome.opened) == 1
    # the new page is drivable
    assert _cdp.page_probe("http://127.0.0.1:9222")["healthy"] is True


def test_observe_fresh_screenshot_survives_detached_page(cfg, monkeypatch):
    """optix_observe(mode=screenshot, fresh=True) after an emulator restart
    left the CDP page detached: must recover (recreate) instead of failing
    with 'Page.reload: Not attached to an active page'."""
    core._LAST_FRESH_CAPTURE.clear()
    core.reset_cdp_tab_registry()
    chrome = FakeChrome(monkeypatch)
    out = core.cdp_screenshot_runtime(cfg, fresh=True)
    assert out["state"] == "succeeded", out
    assert chrome.closed == ["t1"] and chrome.opened


def test_observe_fresh_recovers_without_autoheal(cfg, monkeypatch):
    """Same detached page with session autoheal off: the _cdp_recoverable
    wrapper itself must recreate the page (reloading a detached page can't
    work) and retry once."""
    import dataclasses
    core._LAST_FRESH_CAPTURE.clear()
    core.reset_cdp_tab_registry()
    chrome = FakeChrome(monkeypatch)
    # sessions open fine on the detached tab; only Page.reload/capture fail
    chrome.tabs["t1"].send = lambda text, ws=chrome.tabs["t1"]: FakeWS.send(
        ws, text) if json.loads(text)["method"] in (
            "Page.enable", "Emulation.setDeviceMetricsOverride") else \
        DetachedWS.send(ws, text)
    cfg2 = dataclasses.replace(cfg, cdp_autoheal=False)
    out = core.cdp_screenshot_runtime(cfg2, fresh=True)
    assert out["state"] == "succeeded", out
    assert out.get("cdp_recovered") is True
    assert chrome.closed == ["t1"]


# ---- detached session that passes Page.enable + Runtime.evaluate (7hp.2) --

class PartiallyDetachedWS(FakeWS):
    """Session Chrome attaches for Page.enable / Runtime.evaluate (and the
    viewport override) but which answers 'Not attached to an active page' to
    the commands listed in ``detached_methods`` — the post emulator-restart
    state the 0031 probe could not see."""

    def __init__(self, detached_methods):
        super().__init__(results={
            "Runtime.evaluate": {"result": {"type": "number", "value": 2}},
        })
        self.detached_methods = set(detached_methods)

    def send(self, text):
        msg = json.loads(text)
        if msg["method"] in self.detached_methods:
            self.sent.append((msg["method"], msg.get("params", {}), msg["id"]))
            self._outbox.append(json.dumps(
                {"id": msg["id"],
                 "error": {"message": "Not attached to an active page"}}))
            return
        super().send(text)


_PROBE_BLIND = ("Page.reload", "Page.captureScreenshot")
_PROBE_VISIBLE = _PROBE_BLIND + ("Page.getNavigationHistory", "Page.getFrameTree")


def test_page_probe_detects_detached_session_passing_enable_and_runtime(monkeypatch):
    chrome = FakeChrome(monkeypatch, detached_ids=())
    chrome.tabs["t1"] = PartiallyDetachedWS(_PROBE_VISIBLE)
    out = _cdp.page_probe("http://127.0.0.1:9222")
    assert out["healthy"] is False and out["not_attached"] is True
    methods = [m for m, _, _ in chrome.tabs["t1"].sent]
    assert methods[:2] == ["Page.enable", "Page.getNavigationHistory"]


def test_cdp_restart_not_already_healthy_when_session_detached(cfg, monkeypatch):
    chrome = FakeChrome(monkeypatch, detached_ids=())
    chrome.tabs["t1"] = PartiallyDetachedWS(_PROBE_VISIBLE)
    out = core.ensure_chrome_cdp(cfg, allow_restart=False)
    assert out["detail"] != "already healthy"
    assert out["state"] == "recreated_page" and out["restarted"] is True
    assert chrome.closed == ["t1"] and len(chrome.opened) == 1


def test_observe_fresh_after_restart_recovers_when_probe_cannot_see_detach(
        cfg, monkeypatch):
    """Page.reload says 'Not attached' although every probe call passes:
    recovery must still replace that exact target (forced) and retry."""
    import dataclasses
    core._LAST_FRESH_CAPTURE.clear()
    core.reset_cdp_tab_registry()
    chrome = FakeChrome(monkeypatch, detached_ids=())
    chrome.tabs["t1"] = PartiallyDetachedWS(_PROBE_BLIND)
    assert _cdp.page_probe("http://127.0.0.1:9222")["healthy"] is True
    cfg2 = dataclasses.replace(cfg, cdp_autoheal=False)
    out = core.cdp_screenshot_runtime(cfg2, fresh=True)
    assert out["state"] == "succeeded", out
    assert out.get("cdp_recovered") is True
    assert chrome.closed == ["t1"] and len(chrome.opened) == 1


def test_recreate_page_force_ws_url_replaces_probe_healthy_target(monkeypatch):
    chrome = FakeChrome(monkeypatch, detached_ids=())
    chrome.tabs["t1"] = PartiallyDetachedWS(_PROBE_BLIND)
    url = "http://127.0.0.1:9222"
    assert _cdp.recreate_page(url) is None  # probe healthy -> untouched
    tab = _cdp.recreate_page(url, force_ws_url=FakeChrome._ws_url("t1"))
    assert tab is not None and chrome.closed == ["t1"]


# ---- #23 r5: detach between calls / target swap / restart gating --------

_RUNTIME_URL = "http://127.0.0.1:8081/"


class ProjectChrome(FakeChrome):
    """FakeChrome whose tabs carry URLs, so per-project (runtime port 8081)
    tab resolution from /json/list can be exercised."""

    def __init__(self, monkeypatch, tabs: dict):
        super().__init__(monkeypatch, detached_ids=())
        self.urls: dict[str, str] = {}
        self.ws_connected: list[str] = []
        for tid, (ws, url) in tabs.items():
            self.tabs[tid] = ws
            self.urls[tid] = url
        monkeypatch.setattr(core, "attach_mode", lambda c: False)
        monkeypatch.setattr(core, "_project_web_port", lambda c, p: 8081)
        monkeypatch.setattr(core, "runtime_base_url",
                            lambda c, p=None: _RUNTIME_URL)
        core.reset_cdp_tab_registry()
        core._LAST_FRESH_CAPTURE.clear()

    def _list_pages(self, url, timeout=5.0):
        return [{"id": t, "type": "page", "url": self.urls.get(t, "about:blank"),
                 "webSocketDebuggerUrl": self._ws_url(t)} for t in self.tabs]

    def _open_tab(self, url, page_url="about:blank", timeout=5.0):
        tab = super()._open_tab(url, page_url, timeout)
        self.urls[tab["id"]] = page_url
        return tab

    def _connect_ws(self, ws_url, timeout=30.0):
        self.ws_connected.append(ws_url.rsplit("/", 1)[1])
        return super()._connect_ws(ws_url, timeout)


def test_detach_between_calls_is_recovered_by_reresolving_target(cfg, monkeypatch):
    """Call 1 succeeds; the target detaches between calls (emulator restart);
    call 2 re-resolves the target from /json/list and reports cdp_recovered."""
    chrome = ProjectChrome(monkeypatch, {"t1": (FakeChrome._healthy_ws(None),
                                                _RUNTIME_URL)})
    first = core.cdp_screenshot_runtime(cfg, project="P")
    assert first["state"] == "succeeded" and "cdp_recovered" not in first
    assert core.cdp_tab_registry() == {"p:8081": "t1"}
    chrome.tabs["t1"] = DetachedWS()          # detached between the calls
    out = core.cdp_screenshot_runtime(cfg, project="P")
    assert out["state"] == "succeeded", out
    assert out["cdp_recovered"] is True
    assert chrome.closed == ["t1"] and len(chrome.opened) == 1
    new_id = chrome.opened[0]
    assert core.cdp_tab_registry() == {"p:8081": new_id}   # re-resolved
    assert chrome.urls[new_id] == _RUNTIME_URL             # created at runtime URL


def test_target_swap_after_restart_attaches_new_target_not_cached_ws(cfg, monkeypatch):
    """After a restart the old target stays listed but detached and the runtime
    has a NEW target (t2). Recovery attaches t2 and never reuses t1's ws_url."""
    chrome = ProjectChrome(monkeypatch, {"t1": (FakeChrome._healthy_ws(None),
                                                _RUNTIME_URL)})
    assert core.cdp_screenshot_runtime(cfg, project="P")["state"] == "succeeded"
    assert core.cdp_tab_registry() == {"p:8081": "t1"}
    # restart: t1 detached, runtime serves in a brand-new target t2
    chrome.tabs["t1"] = DetachedWS()
    chrome.tabs["t2"] = chrome._healthy_ws()
    chrome.urls["t2"] = _RUNTIME_URL
    chrome.ws_connected.clear()
    out = core.cdp_screenshot_runtime(cfg, project="P")
    assert out["state"] == "succeeded", out   # attached t2 on the first try
    assert core.cdp_tab_registry() == {"p:8081": "t2"}
    assert chrome.opened == []                 # adopted t2, did not create
    assert chrome.closed == ["t1"]
    # t1 is only touched by the health probe; the screenshot session is t2
    assert chrome.ws_connected[-1] == "t2"


def test_reresolve_tab_drops_cached_ws_and_matches_runtime_url(cfg, monkeypatch):
    chrome = ProjectChrome(monkeypatch, {
        "old": (FakeChrome._healthy_ws(None), _RUNTIME_URL),
        "other": (FakeChrome._healthy_ws(None), "http://127.0.0.1:8088/")})
    core._CDP_TABS[("p", 8081)] = "gone-target"
    r = core.cdp_reresolve_tab(cfg, "P")
    assert r["resolved"] and r["id"] == "old" and "created" not in r
    assert core.cdp_tab_registry() == {"p:8081": "old"}
    assert "other" not in chrome.closed


def test_emulator_restart_reports_serving_only_after_new_runtime_serves(
        cfg, monkeypatch):
    order: list[str] = []
    statuses = iter([[111],            # before the restart
                     [111], [111],     # old pid lingering: NOT a new runtime
                     [222]])           # the new runtime process
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, r=None, project=None:
                        {"pids": next(statuses), "state": "running"})
    monkeypatch.setattr(core, "stop_emulator",
                        lambda c, r=None, status=None, project=None:
                        {"stopped": True, "killed_pids": [111]})
    monkeypatch.setattr(core, "_wait_runtime_port", lambda *a, **k: True)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    monkeypatch.setattr(core, "attach_mode", lambda c: False)
    monkeypatch.setattr(core, "run_emulator",
                        lambda c, p, save_first=False, wait_ready=True, runner=None, wait_page=True:
                        order.append("run") or
                        {"launched": True, "serving": True, "waited_seconds": 0.0})
    monkeypatch.setattr(core, "_cdp_after_runtime_up",
                        lambda c, p: order.append("reresolve") or {"resolved": True})
    monkeypatch.setattr(core, "_wait_page_ready",
                        lambda c, p, timeout=20.0: {"ready": True, "waited": 0.0})
    out = core.restart_emulator(cfg, "P")
    assert out["serving"] is True
    assert out["new_runtime_pids"] == [222]
    assert order == ["run", "reresolve"]       # target re-resolved after serving
    assert out["cdp_target"] == {"resolved": True}


def test_emulator_restart_not_serving_while_only_old_process_answers(cfg, monkeypatch):
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, r=None, project=None:
                        {"pids": [111], "state": "running"})
    monkeypatch.setattr(core, "stop_emulator",
                        lambda c, r=None, status=None, project=None:
                        {"stopped": True, "killed_pids": [111]})
    monkeypatch.setattr(core, "_wait_runtime_port", lambda *a, **k: True)
    monkeypatch.setattr(core, "attach_mode", lambda c: False)
    monkeypatch.setattr(core, "_wait_new_runtime",
                        lambda c, p, old, runner=None, timeout=60.0:
                        {"ok": False, "pids": [], "waited": 0.0})
    monkeypatch.setattr(core, "run_emulator",
                        lambda c, p, save_first=False, wait_ready=True, runner=None, wait_page=True:
                        {"launched": True, "serving": True})
    monkeypatch.setattr(core, "_cdp_after_runtime_up",
                        lambda c, p: (_ for _ in ()).throw(AssertionError("no re-resolve")))
    out = core.restart_emulator(cfg, "P")
    assert out["serving"] is False and "hint" in out


def test_optix_cdp_restart_not_already_healthy_when_cached_session_detached(
        cfg, monkeypatch):
    """The first listed page is healthy, but the per-project cached session the
    tools use is detached: ensure_chrome_cdp (optix_cdp_restart) must not say
    'already healthy'."""
    chrome = ProjectChrome(monkeypatch, {
        "t0": (FakeChrome._healthy_ws(None), "about:blank"),
        "t1": (DetachedWS(), _RUNTIME_URL)})
    core._CDP_TABS[("p", 8081)] = "t1"
    assert _cdp.page_probe("http://127.0.0.1:9222")["healthy"] is True
    out = core.ensure_chrome_cdp(cfg, allow_restart=False)
    assert out["detail"] != "already healthy"
    assert out["state"] == "recreated_page" and out["restarted"] is True
    assert chrome.closed == ["t1"]
    assert core.cdp_tab_registry() == {}       # stale cached session dropped


# ---- ftx-mcp-phv.6: unchanged target id, detached session, all paths ------

def test_unchanged_target_id_with_detached_session_forces_fresh_attach(
        cfg, monkeypatch):
    """After a restart the runtime tab keeps the SAME target id but its session
    is detached in a way page_probe cannot see (Page.reload / capture fail).
    The re-resolve must not reuse that id: it forces a fresh attach."""
    chrome = ProjectChrome(monkeypatch, {
        "t1": (PartiallyDetachedWS(_PROBE_BLIND), _RUNTIME_URL)})
    core._CDP_TABS[("p", 8081)] = "t1"
    assert _cdp.page_probe("http://127.0.0.1:9222",
                           ws_url=FakeChrome._ws_url("t1"))["healthy"] is True
    out = core._cdp_after_runtime_up(cfg, "P")
    assert out["resolved"] is True and out["fresh_attach"] is True
    assert out["id"] != "t1" and chrome.closed == ["t1"]
    assert core.cdp_tab_registry() == {"p:8081": out["id"]}
    # the next call attaches the new target on the first try, no recovery
    res = core.cdp_screenshot_runtime(cfg, project="P", fresh=True)
    assert res["state"] == "succeeded" and "cdp_recovered" not in res


def test_reresolve_without_force_keeps_probe_healthy_unchanged_target(cfg, monkeypatch):
    chrome = ProjectChrome(monkeypatch, {
        "t1": (FakeChrome._healthy_ws(None), _RUNTIME_URL)})
    core._CDP_TABS[("p", 8081)] = "t1"
    r = core.cdp_reresolve_tab(cfg, "P")
    assert r["id"] == "t1" and "fresh_attach" not in r and chrome.closed == []


def test_cdp_restart_probes_cached_session_and_reports_restarted(cfg, monkeypatch):
    """optix_cdp_restart: cached session answers Not attached -> reattach and
    restarted: true (first listed page is healthy)."""
    chrome = ProjectChrome(monkeypatch, {
        "t0": (FakeChrome._healthy_ws(None), "about:blank"),
        "t1": (PartiallyDetachedWS(_PROBE_VISIBLE), _RUNTIME_URL)})
    core._CDP_TABS[("p", 8081)] = "t1"
    out = core.ensure_chrome_cdp(cfg, allow_restart=False)
    assert out["restarted"] is True and out["state"] == "recreated_page"
    assert chrome.closed == ["t1"]


class _NoMainFrameWS(FakeWS):
    """Passes the page probe but Page.getFrameTree returns no main frame."""

    def __init__(self):
        super().__init__(results={
            "Runtime.evaluate": {"result": {"type": "number", "value": 2}},
            "Page.getFrameTree": {}})


def test_cdp_restart_healthy_check_calls_getframetree_on_cached_target(
        cfg, monkeypatch):
    """Healthy cached tab: Page.getFrameTree is sent on that exact target and
    the result is 'already healthy' with no reattached key."""
    chrome = ProjectChrome(monkeypatch, {
        "t0": (FakeChrome._healthy_ws(None), "about:blank"),
        "t1": (FakeChrome._healthy_ws(None), _RUNTIME_URL)})
    core._CDP_TABS[("p", 8081)] = "t1"
    out = core.ensure_chrome_cdp(cfg, allow_restart=False)
    assert out["detail"] == "already healthy" and "reattached" not in out
    assert "Page.getFrameTree" in [m for m, _, _ in chrome.tabs["t1"].sent]
    assert chrome.closed == []


def test_cdp_restart_reports_reattached_when_cached_main_frame_missing(
        cfg, monkeypatch):
    """Cached tab answers the page probe but its main frame does not answer
    getFrameTree: not 'already healthy'; the tab is re-attached and the result
    says reattached: true."""
    chrome = ProjectChrome(monkeypatch, {
        "t0": (FakeChrome._healthy_ws(None), "about:blank"),
        "t1": (_NoMainFrameWS(), _RUNTIME_URL)})
    core._CDP_TABS[("p", 8081)] = "t1"
    out = core.ensure_chrome_cdp(cfg, allow_restart=False)
    assert out["detail"] != "already healthy"
    assert out["reattached"] is True and out["state"] == "recreated_page"
    assert chrome.closed == ["t1"]


def test_cdp_restart_reports_reattached_when_cached_session_detached(
        cfg, monkeypatch):
    chrome = ProjectChrome(monkeypatch, {
        "t0": (FakeChrome._healthy_ws(None), "about:blank"),
        "t1": (PartiallyDetachedWS(_PROBE_VISIBLE), _RUNTIME_URL)})
    core._CDP_TABS[("p", 8081)] = "t1"
    out = core.ensure_chrome_cdp(cfg, allow_restart=False)
    assert out["reattached"] is True and chrome.closed == ["t1"]


class _TextWS(FakeWS):
    """Healthy page: answers probes (1+1) and innerText; ``detached`` methods
    (and innerText when listed) answer 'Not attached to an active page'."""

    def __init__(self, detached=()):
        super().__init__(results={
            "Page.captureScreenshot": {
                "data": base64.b64encode(b"\xff\xd8jpg").decode()}})
        self.detached = set(detached)

    def send(self, text):
        msg = json.loads(text)
        expr = (msg.get("params") or {}).get("expression", "")
        if "innerText" in expr:
            self.sent.append((msg["method"], msg.get("params", {}), msg["id"]))
            if "innerText" in self.detached:
                self._outbox.append(json.dumps({"id": msg["id"], "error": {
                    "message": "Not attached to an active page"}}))
            else:
                self._outbox.append(json.dumps({"id": msg["id"], "result": {
                    "result": {"type": "string", "value": "Hello"}}}))
            return
        if msg["method"] == "Runtime.evaluate":
            self.sent.append((msg["method"], msg.get("params", {}), msg["id"]))
            self._outbox.append(json.dumps({"id": msg["id"], "result": {
                "result": {"type": "number", "value": 2}}}))
            return
        if msg["method"] in self.detached:
            self.sent.append((msg["method"], msg.get("params", {}), msg["id"]))
            self._outbox.append(json.dumps({"id": msg["id"], "error": {
                "message": "Not attached to an active page"}}))
            return
        super().send(text)


@pytest.mark.parametrize("call,detached", [
    ("observe_fresh", ("Page.reload", "Page.captureScreenshot")),
    ("click", ("Page.reload", "Input.dispatchMouseEvent")),
    ("dom_text", ("Page.reload", "innerText")),
])
def test_not_attached_recovers_once_on_every_path(cfg, monkeypatch, call, detached):
    """The tab the tools use answers 'Not attached to an active page' while the
    health probe passes: observe(fresh) (Page.reload), interact(click) and
    dom_text each recover with one retry and cdp_recovered: true."""
    import dataclasses
    chrome = ProjectChrome(monkeypatch, {"t1": (_TextWS(detached), _RUNTIME_URL)})
    chrome._healthy_ws = lambda: _TextWS()
    cfg2 = dataclasses.replace(cfg, cdp_autoheal=False)
    if call == "observe_fresh":
        out = core.cdp_screenshot_runtime(cfg2, project="P", fresh=True)
    elif call == "click":
        out = core.cdp_click_runtime(cfg2, 10, 10, project="P")
    else:
        out = core.cdp_dom_text_runtime(cfg2, project="P")
    assert out["state"] == "succeeded", out
    assert out.get("cdp_recovered") is True
    assert chrome.closed == ["t1"] and len(chrome.opened) == 1


# ---- restart / cold start wait for page readiness (ftx-mcp-czz.5) ----------

class _FakePage:
    """CDPClient stand-in: getFrameTree answers ``url``; readyState ``state``."""

    def __init__(self, script):
        self.script = script

    def cmd(self, method, **params):
        s = self.script
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"url": s["url"]()}}}
        if method == "Runtime.evaluate":
            return {"result": {"value": s["state"]()}}
        return {}

    def close(self):
        pass


def _patch_page(monkeypatch, cfg, url, state):
    from service import _cdp
    port = core._project_web_port(cfg, "P")
    page = {"id": "T1", "url": f"http://127.0.0.1:{port}/",
            "webSocketDebuggerUrl": "ws://x/T1"}
    monkeypatch.setattr(_cdp, "list_pages", lambda *a, **k: [page])
    monkeypatch.setattr(_cdp, "CDPClient",
                        lambda *a, **k: _FakePage({"url": url, "state": state}))
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    # The page check asks who owns the port: pin it to the runtime instead of
    # reading the host's listener table.
    monkeypatch.setattr(core, "_port_owner", lambda *a, **k:
                        {"owner": "runtime", "pid": 111, "name": None})
    return port


def test_wait_page_ready_returns_true_once_url_and_readystate_complete(cfg, monkeypatch):
    calls = {"n": 0}
    port = core._project_web_port(cfg, "P")
    want = f"http://127.0.0.1:{port}/"

    def url():
        calls["n"] += 1
        return "about:blank" if calls["n"] < 3 else want

    _patch_page(monkeypatch, cfg, url, lambda: "complete")
    res = core._wait_page_ready(cfg, "P", timeout=5.0)
    assert res["ready"] is True
    assert calls["n"] >= 3          # polled until the URL matched


def test_wait_page_ready_requires_readystate_complete(cfg, monkeypatch):
    port = core._project_web_port(cfg, "P")
    _patch_page(monkeypatch, cfg, lambda: f"http://127.0.0.1:{port}/",
                lambda: "loading")
    ticks = iter(range(0, 1000))
    monkeypatch.setattr(core.time, "monotonic", lambda: next(ticks) * 1.0)
    assert core._wait_page_ready(cfg, "P", timeout=3.0)["ready"] is False


def test_restart_reports_real_waited_seconds_and_page_ready(cfg, monkeypatch):
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, r=None, project=None: {"pids": [], "state": "stopped"})
    monkeypatch.setattr(core, "attach_mode", lambda c: False)
    monkeypatch.setattr(core, "run_emulator",
                        lambda c, p, save_first=False, wait_ready=True, runner=None, wait_page=True:
                        {"launched": True, "serving": True, "waited_seconds": 0.0})
    monkeypatch.setattr(core, "_cdp_after_runtime_up",
                        lambda c, p: {"resolved": True})
    monkeypatch.setattr(core, "_wait_page_ready",
                        lambda c, p, timeout=20.0: {"ready": True, "waited": 3.4})
    out = core.restart_emulator(cfg, "P")
    assert out["page_ready"] is True
    assert out["waited_seconds"] == 3.4
    assert out["serving"] is True


def test_restart_page_timeout_keeps_serving_and_hints(cfg, monkeypatch):
    monkeypatch.setattr(core, "emulator_status",
                        lambda c, r=None, project=None: {"pids": [], "state": "stopped"})
    monkeypatch.setattr(core, "attach_mode", lambda c: False)
    monkeypatch.setattr(core, "run_emulator",
                        lambda c, p, save_first=False, wait_ready=True, runner=None, wait_page=True:
                        {"launched": True, "serving": True, "waited_seconds": 1.0})
    monkeypatch.setattr(core, "_cdp_after_runtime_up",
                        lambda c, p: {"resolved": True})
    monkeypatch.setattr(core, "_wait_page_ready",
                        lambda c, p, timeout=20.0: {"ready": False, "waited": 20.0})
    out = core.restart_emulator(cfg, "P")
    assert out["serving"] is True
    assert out["page_ready"] is False
    assert out["hint"]
    assert out["waited_seconds"] == 21.0


def test_cold_start_run_emulator_uses_same_page_readiness_wait(cfg, monkeypatch):
    out = {"serving": True, "waited_seconds": 2.0,
           "cdp_target": {"resolved": True}}
    seen = []
    monkeypatch.setattr(core, "_wait_page_ready",
                        lambda c, p, timeout=20.0: seen.append(p) or
                        {"ready": True, "waited": 1.5})
    core._apply_page_ready(cfg, "P", out)
    assert seen == ["P"] and out["page_ready"] is True
    assert out["waited_seconds"] == 3.5


# ---- ftx-mcp-czz.7: transient detach polled before re-resolve -----------

def _probe_sequence(monkeypatch, healthy_after):
    """Patch page_probe: unhealthy/not-attached until call number
    ``healthy_after`` (None = never). Returns the call counter list."""
    calls: list[int] = []

    def fake(url, timeout=5.0, ws_url=None):
        calls.append(1)
        if healthy_after is not None and len(calls) >= healthy_after:
            return {"healthy": True, "not_attached": False, "detail": "ok"}
        return {"healthy": False, "not_attached": True,
                "detail": "Not attached to an active page"}
    monkeypatch.setattr(_cdp, "page_probe", fake)
    return calls


def test_transient_detach_polls_probe_then_retries_once_without_reload(
        cfg, install, monkeypatch):
    ws = install(FlakyWS("Page.captureScreenshot", 1, "error",
                         "Not attached to an active page"))
    calls = _probe_sequence(monkeypatch, healthy_after=3)
    sleeps: list[float] = []
    monkeypatch.setattr(core.time, "sleep", sleeps.append)
    reresolved = []
    monkeypatch.setattr(core, "_cdp_recover_page",
                        lambda c, p: reresolved.append(1) or True)
    out = core.cdp_screenshot_runtime(cfg)
    assert out["state"] == "succeeded"
    assert out["cdp_recovered"] is True
    assert len(calls) == 3 and not reresolved
    assert [d for d in sleeps if d in (0.25, 0.5)][:2] == [0.25, 0.5]  # backoff
    assert ws.count("Page.captureScreenshot") == 2


def test_persistent_detach_polls_up_to_10s_then_reports_cdp_recovered_false(
        cfg, install, monkeypatch):
    ws = install(FlakyWS("Page.captureScreenshot", 9, "error",
                         "Not attached to an active page"))
    _probe_sequence(monkeypatch, healthy_after=None)
    sleeps: list[float] = []
    monkeypatch.setattr(core.time, "sleep", sleeps.append)
    monkeypatch.setattr(core, "_cdp_recover_page", lambda c, p: False)
    out = core.cdp_screenshot_runtime(cfg)
    assert out["state"] == "failed"
    assert out["cdp_recovered"] is False
    rec = out["cdp_recovery"]
    assert rec["reresolve"] == "failed"
    assert rec["attach_poll"]["attached"] is False
    assert rec["attach_poll"]["attempts"] > 3
    assert 8 <= rec["attach_poll"]["waited_s"] <= 10
    assert ws.count("Page.captureScreenshot") == 1   # no retry w/o recovery


def test_poll_attach_healthy_first_try_is_not_transient(cfg, monkeypatch):
    _probe_sequence(monkeypatch, healthy_after=1)
    r = core._cdp_poll_attach(cfg, None)
    assert r["attached"] is True and r["transient"] is False


# ---- ftx-mcp-czz.6: observe(fresh) skips reload for just-attached pages ----

def _on_runtime_ws():
    """Healthy tab whose navigation history says it is already on the runtime."""
    ws = FakeChrome._healthy_ws(None)
    ws.results["Page.getNavigationHistory"] = {
        "currentIndex": 0, "entries": [{"url": _RUNTIME_URL}]}
    return ws


def _fresh_reloads(chrome, tid="t1"):
    return sum(1 for m, _, _ in chrome.tabs[tid].sent if m == "Page.reload")


def test_cached_tab_entry_records_attach_timestamp(cfg, monkeypatch):
    ProjectChrome(monkeypatch, {"t1": (FakeChrome._healthy_ws(None), _RUNTIME_URL)})
    assert core.cdp_tab_attach_age(cfg, "P") is None
    core.cdp_tab_for(cfg, "P", 8081)
    age = core.cdp_tab_attach_age(cfg, "P")
    assert age is not None and 0.0 <= age < 5.0


def test_observe_fresh_skips_reload_when_attached_under_5s(cfg, monkeypatch):
    chrome = ProjectChrome(monkeypatch, {
        "t1": (_on_runtime_ws(), _RUNTIME_URL)})
    core.cdp_tab_for(cfg, "P", 8081)          # attach now
    out = core.cdp_screenshot_runtime(cfg, fresh=True, project="P")
    assert out["state"] == "succeeded", out
    assert _fresh_reloads(chrome) == 0
    assert out["navigated"] is False


def test_observe_fresh_reloads_when_attached_over_5s(cfg, monkeypatch):
    chrome = ProjectChrome(monkeypatch, {
        "t1": (_on_runtime_ws(), _RUNTIME_URL)})
    core.cdp_tab_for(cfg, "P", 8081)
    tid, ts = core._CDP_TAB_ATTACHED[("p", 8081)]
    core._CDP_TAB_ATTACHED[("p", 8081)] = (tid, ts - 6.0)   # attached 6 s ago
    out = core.cdp_screenshot_runtime(cfg, fresh=True, project="P")
    assert out["state"] == "succeeded", out
    assert _fresh_reloads(chrome) == 1
    assert out["navigated"] is True
