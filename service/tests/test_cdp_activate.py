"""Background CDP tabs are activated before attach; timeouts are bounded (#62).

Fake CDP transport only (service._cdp seams), no live Chrome.
"""
from __future__ import annotations

from service import _cdp, core
from service.tests.test_cdp import FakeWS
from service.tests.test_cdp_recovery import _RUNTIME_URL, FakeChrome, ProjectChrome


def _record_http(monkeypatch, events: list, status: int = 200):
    def fake(url, timeout):
        events.append(("http", url.rsplit("/json/", 1)[-1]))
        return status
    monkeypatch.setattr(_cdp, "_http_get_status", fake)


class _Recording(ProjectChrome):
    """ProjectChrome that logs websocket connects into a shared event list and
    can time out the first N connects."""

    def __init__(self, monkeypatch, tabs, events, timeouts=0):
        super().__init__(monkeypatch, tabs)
        self.events, self.timeouts = events, timeouts
        self.connects = 0

    def _connect_ws(self, ws_url, timeout=30.0):
        self.connects += 1
        self.events.append(("connect", ws_url.rsplit("/", 1)[1]))
        if self.timeouts > 0:
            self.timeouts -= 1
            raise TimeoutError("timed out")
        return super()._connect_ws(ws_url, timeout)


def _chrome(monkeypatch, events, timeouts=0):
    return _Recording(monkeypatch,
                      {"t1": (FakeChrome._healthy_ws(None), _RUNTIME_URL)},
                      events, timeouts)


def test_background_tab_is_activated_before_attach(cfg, monkeypatch):
    events: list = []
    _record_http(monkeypatch, events)
    _chrome(monkeypatch, events)
    out = core.cdp_screenshot_runtime(cfg, project="P")
    assert out["state"] == "succeeded", out
    assert ("http", "activate/t1") in events
    assert events.index(("http", "activate/t1")) < events.index(("connect", "t1"))


def test_activate_ignores_non_200_and_transport_errors(monkeypatch):
    _record_http(monkeypatch, [], status=404)
    assert _cdp.activate("http://127.0.0.1:9222", "t1") is False

    def boom(url, timeout):
        raise OSError("down")
    monkeypatch.setattr(_cdp, "_http_get_status", boom)
    assert _cdp.activate("http://127.0.0.1:9222", "t1") is False


def test_websocket_timeout_then_success_retries_exactly_once(cfg, monkeypatch):
    events: list = []
    _record_http(monkeypatch, events)
    chrome = _chrome(monkeypatch, events, timeouts=1)
    out = core.cdp_screenshot_runtime(cfg, project="P")
    assert out["state"] == "succeeded", out
    assert chrome.connects == 2                      # first try + exactly one retry
    assert events.count(("http", "activate/t1")) == 2   # before attach + before retry
    assert "cdp_recovered" not in out                # no reload-recovery involved


def test_existing_tab_for_runtime_port_is_reused_without_created_flag(cfg, monkeypatch):
    chrome = _chrome(monkeypatch, [])
    r = core.cdp_reresolve_tab(cfg, "P")
    assert r["resolved"] and r["id"] == "t1"
    assert "created" not in r
    assert chrome.opened == []


def test_throttled_tab_whose_probe_times_out_is_kept_not_replaced(cfg, monkeypatch):
    chrome = _chrome(monkeypatch, [], timeouts=1)    # page_probe connect times out
    chrome.tabs["dup"] = FakeChrome._healthy_ws(None)
    chrome.urls["dup"] = _RUNTIME_URL
    r = core.cdp_reresolve_tab(cfg, "P")
    assert r["resolved"] and "created" not in r
    assert chrome.opened == []                       # never a second/new tab
    assert r["id"] == "dup" and r["closed"] == ["t1"]   # only the OTHER tab closed
    assert len(chrome.tabs) == 1


def test_persistent_timeout_returns_structured_cdp_tab_timeout(cfg, monkeypatch):
    events: list = []
    _record_http(monkeypatch, events)
    chrome = _chrome(monkeypatch, events, timeouts=99)
    out = core.cdp_screenshot_runtime(cfg, project="P")
    assert out["state"] == "failed"
    assert out["error"] == "cdp_tab_timeout"
    assert out["target_id"] == "t1"
    assert out["url"] == _RUNTIME_URL
    assert out["hint"]
    assert chrome.connects == 2                      # one attempt + one retry, no more


def test_cdp_timeouts_are_5s_connect_10s_read():
    assert _cdp.CDP_CONNECT_TIMEOUT_S == 5.0
    assert _cdp.CDP_READ_TIMEOUT_S == 10.0


def test_client_applies_read_timeout_after_connect(monkeypatch):
    seen: dict = {}

    class WS(FakeWS):
        def settimeout(self, t):
            seen["read"] = t

    monkeypatch.setattr(_cdp, "_connect_ws",
                        lambda url, timeout=30.0: seen.update(connect=timeout) or WS())
    _cdp.CDPClient("http://127.0.0.1:9222", ws_url="ws://x/devtools/page/1")
    assert seen == {"connect": 5.0, "read": 10.0}
