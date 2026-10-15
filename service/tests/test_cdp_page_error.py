"""Web observe/interact fail loud on Chrome error pages and the Optix
"Server unreachable" overlay (PRD 0049 D, #65)."""
from __future__ import annotations

import base64

import pytest

from service import core

from .test_cdp import FakeWS, fake_cdp  # noqa: F401  (fixture re-export)

HINT = "optix_emulator status / run"

NORMAL = {"url": "http://localhost:8081/", "title": "Site",
          "text": "Welcome", "overlay": False}
CHROME_ERROR = {"url": "chrome-error://chromewebdata/", "title": "localhost",
                "text": "This site can't be reached ERR_CONNECTION_REFUSED",
                "overlay": False}
ERR_BODY = {"url": "http://localhost:8081/", "title": "localhost",
            "text": "localhost refused to connect. ERR_CONNECTION_REFUSED",
            "overlay": False}
OVERLAY_TEXT = {"url": "http://localhost:8081/", "title": "Site",
                "text": "Server unreachable", "overlay": False}
OVERLAY_BANNER = {"url": "http://localhost:8081/", "title": "Site",
                  "text": "", "overlay": True}

JPEG = base64.b64encode(b"\xff\xd8x\xff\xd9").decode()


def _results(state, **extra):
    return {"Runtime.evaluate": {"result": {"value": state}},
            "Page.captureScreenshot": {"data": JPEG}, **extra}


def _methods(ws):
    return [m for (m, _p, _i) in ws.sent]


def _assert_failed(out, error):
    assert out["state"] == "failed", out
    assert out["error"] == error
    assert out["detail"]
    assert HINT in out["hint"]


@pytest.mark.parametrize("state,error", [
    (CHROME_ERROR, "page_error"),
    (ERR_BODY, "page_error"),
    (OVERLAY_TEXT, "runtime_unreachable"),
    (OVERLAY_BANNER, "runtime_unreachable"),
])
def test_screenshot_fails_on_error_pages(cfg, fake_cdp, state, error):  # noqa: F811
    ws = fake_cdp(results=_results(state))
    out = core.cdp_screenshot_runtime(cfg)
    _assert_failed(out, error)
    assert "Page.captureScreenshot" not in _methods(ws)


@pytest.mark.parametrize("state,error", [
    (CHROME_ERROR, "page_error"),
    (OVERLAY_TEXT, "runtime_unreachable"),
])
def test_click_type_fill_key_fail_on_error_pages(cfg, fake_cdp, state, error):  # noqa: F811
    ws = fake_cdp(results=_results(state))
    _assert_failed(core.cdp_click_runtime(cfg, x=10, y=20), error)
    _assert_failed(core.cdp_type_runtime(cfg, text="hi"), error)
    _assert_failed(core.cdp_fill_runtime(cfg, x=10, y=20, text="hi"), error)
    _assert_failed(core.cdp_key_runtime(cfg, key="Enter"), error)
    assert not any(m.startswith("Input.") for m in _methods(ws))


def test_dom_text_and_find_text_fail_on_error_pages(cfg, fake_cdp, monkeypatch):  # noqa: F811
    fake_cdp(results=_results(CHROME_ERROR))
    _assert_failed(core.cdp_dom_text_runtime(cfg), "page_error")
    monkeypatch.setattr(core, "_find_tesseract", lambda: "tesseract")
    out = core.cdp_find_text_runtime(cfg, "Start")
    _assert_failed(out, "page_error")
    assert out["found"] is False


def test_read_text_propagates_page_error(cfg, fake_cdp, monkeypatch):  # noqa: F811
    fake_cdp(results=_results(OVERLAY_TEXT))
    monkeypatch.setattr(core, "_find_tesseract", lambda: "tesseract")
    out = core.cdp_read_text_runtime(cfg)
    _assert_failed(out, "runtime_unreachable")


def test_normal_page_unchanged(cfg, fake_cdp):  # noqa: F811
    ws = fake_cdp(results=_results(NORMAL))
    shot = core.cdp_screenshot_runtime(cfg)
    assert shot["state"] == "succeeded"
    click = core.cdp_click_runtime(cfg, x=10, y=20)
    assert click["state"] == "succeeded"
    assert "Input.dispatchMouseEvent" in _methods(ws)


def test_title_matching_host_without_err_body_is_not_an_error(cfg, fake_cdp):  # noqa: F811
    fake_cdp(results=_results({**NORMAL, "title": "localhost",
                               "text": "all good"}))
    assert core.cdp_click_runtime(cfg, x=1, y=2)["state"] == "succeeded"


def test_check_is_single_evaluate_in_same_session(cfg, fake_cdp):  # noqa: F811
    ws = fake_cdp(results=_results(NORMAL))
    core.cdp_click_runtime(cfg, x=10, y=20)
    evals = [p for (m, p, _) in ws.sent if m == "Runtime.evaluate"]
    assert len(evals) == 1
    assert "location.href" in evals[0]["expression"]


def test_navigate_skips_check(cfg, fake_cdp, tmp_path):  # noqa: F811
    import json
    ws = fake_cdp(results=_results(CHROME_ERROR))
    routes = tmp_path / "routes.json"
    routes.write_text(json.dumps({"version": 1, "routes": {"home": {"steps": [{"click": [0.5, 0.5]}]}}}))
    core.cdp_navigate_runtime(cfg, route="home", routes_path=str(routes),
                              expect=False)
    assert ws.sent, "navigate must have opened a CDP session"
    assert "Runtime.evaluate" not in _methods(ws)


def test_unknown_page_state_fails_open(cfg, fake_cdp):  # noqa: F811
    fake_cdp(results={"Runtime.evaluate": {"result": {"value": None}},
                      "Page.captureScreenshot": {"data": JPEG}})
    assert core.cdp_screenshot_runtime(cfg)["state"] == "succeeded"
