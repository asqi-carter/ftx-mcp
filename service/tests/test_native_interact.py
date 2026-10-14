"""optix_interact(target="native") — click / fill / type / key posted to the
emulator's native presentation engine window via service.native_input.

The Win32 layer is replaced by a recording fake through
core.native_interact's `input_mod` seam, and the window walk / process scan
through `windows_fn` / `pids_fn`, so the suite runs off-Windows and without
a live runtime.
"""
from __future__ import annotations

import pytest

from service import core, native_input
from service.mcp_app import make_mcp

_WIN = {"hwnd": 4242, "pid": 777, "title": "Chiller_HMI",
        "class_name": "Qt673QWindowIcon", "width": 1280, "height": 800}


class _FakeInput:
    """Stand-in for service.native_input that records every call."""

    NativeInputError = native_input.NativeInputError
    resolve_key = staticmethod(native_input.resolve_key)

    def __init__(self, fail: native_input.NativeInputError | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    def post_click(self, hwnd, x, y):
        if self.fail:
            raise self.fail
        self.calls.append(("click", hwnd, x, y))

    def post_text(self, hwnd, text):
        if self.fail:
            raise self.fail
        self.calls.append(("text", hwnd, text))
        return len(text)

    def post_key(self, hwnd, name):
        if self.fail:
            raise self.fail
        self.calls.append(("key", hwnd, name))
        return {"key": name, "vk": native_input.KEY_VKS.get(name.lower()), "char": None}


def _seams(pids=(777,), windows=(_WIN,), fake=None):
    fake = fake or _FakeInput()
    return fake, {
        "pids_fn": lambda project: list(pids),
        "windows_fn": lambda p: list(windows),
        "input_mod": fake,
    }


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda s: None)


def test_click_normalized_resolves_against_frame(cfg):
    fake, seams = _seams()
    r = core.native_interact(cfg, "click", x=0.5, y=0.25, project="Alpha", **seams)
    assert r["state"] == "succeeded" and r["source"] == "native"
    assert (r["px"], r["py"]) == (640, 200) and r["coords"] == "normalized"
    assert r["frame"] == {"w": 1280, "h": 800} and r["hwnd"] == 4242
    assert fake.calls == [("click", 4242, 640, 200)]
    assert "native_screenshot" in r["hint"]


def test_click_absolute_pixels(cfg):
    fake, seams = _seams()
    r = core.native_interact(cfg, "click", x=228, y=773, **seams)
    assert (r["px"], r["py"]) == (228, 773) and r["coords"] == "absolute"
    assert fake.calls == [("click", 4242, 228, 773)]


def test_click_outside_frame_is_bad_point(cfg):
    fake, seams = _seams()
    r = core.native_interact(cfg, "click", x=1280, y=10, **seams)
    assert r["state"] == "failed" and r["error"] == "bad_point"
    assert fake.calls == []


def test_fill_clicks_types_and_submits(cfg):
    fake, seams = _seams()
    r = core.native_interact(cfg, "fill", x=1085, y=466, text="12", **seams)
    assert r["state"] == "succeeded" and r["typed_chars"] == 2
    assert r["submitted_with"]["key"] == "Enter"
    assert fake.calls == [("click", 4242, 1085, 466), ("text", 4242, "12"),
                          ("key", 4242, "Enter")]


def test_fill_without_submit_leaves_edit_open(cfg):
    fake, seams = _seams()
    r = core.native_interact(cfg, "fill", x=10, y=10, text="7", submit=None, **seams)
    assert r["submitted_with"] is None
    assert [c[0] for c in fake.calls] == ["click", "text"]


def test_type_posts_text_only(cfg):
    fake, seams = _seams()
    r = core.native_interact(cfg, "type", text="abc", **seams)
    assert r["typed_chars"] == 3 and "px" not in r
    assert fake.calls == [("text", 4242, "abc")]


def test_key_named_and_single_char(cfg):
    fake, seams = _seams()
    r = core.native_interact(cfg, "key", key="Escape", **seams)
    assert r["pressed"]["key"] == "Escape" and fake.calls == [("key", 4242, "Escape")]


def test_key_unknown_fails_before_window_lookup(cfg):
    fake, seams = _seams(pids=())   # would fail on pids if it got that far
    r = core.native_interact(cfg, "key", key="ctrl+a", **seams)
    assert r["state"] == "failed" and r["error"] == "bad_key"


@pytest.mark.parametrize("action,kw,missing", [
    ("click", {}, "x, y"), ("fill", {"x": 1, "y": 1}, "text"),
    ("type", {}, "text"), ("key", {}, "key"),
])
def test_missing_params(cfg, action, kw, missing):
    _fake, seams = _seams()
    r = core.native_interact(cfg, action, **kw, **seams)
    assert r["state"] == "failed" and r["error"] == "missing_param"
    assert missing in r["message"]


def test_navigate_is_not_native(cfg):
    _fake, seams = _seams()
    r = core.native_interact(cfg, "navigate", **seams)
    assert r["error"] == "bad_action" and "nav buttons" in r["message"]


def test_no_runtime_and_no_window(cfg):
    _f, seams = _seams(pids=())
    assert core.native_interact(cfg, "click", x=10, y=10, **seams)["error"] == "native_runtime_not_found"
    _f, seams = _seams(windows=())
    assert core.native_interact(cfg, "click", x=10, y=10, **seams)["error"] == "native_window_not_found"


def test_post_failure_is_structured(cfg):
    fake, seams = _seams(fake=_FakeInput(fail=native_input.NativeInputError("post_failed", "boom")))
    r = core.native_interact(cfg, "click", x=10, y=10, **seams)
    assert r["state"] == "failed" and r["error"] == "post_failed" and r["message"] == "boom"


def test_settle_sleeps_after_post(cfg, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(core.time, "sleep", lambda s: slept.append(s))
    _fake, seams = _seams()
    r = core.native_interact(cfg, "click", x=10, y=10, settle_seconds=0.4, **seams)
    assert slept == [0.4] and r["settled_seconds"] == 0.4


# ---- native_input message shapes -------------------------------------------

def test_native_input_message_sequences(monkeypatch):
    posted: list[tuple] = []
    monkeypatch.setattr(native_input, "_post", lambda h, m, w, l: posted.append((m, w, l)))
    native_input.post_click(1, 228, 773)
    lp = (773 << 16) | 228
    assert posted == [(native_input.WM_MOUSEMOVE, 0, lp),
                      (native_input.WM_LBUTTONDOWN, native_input.MK_LBUTTON, lp),
                      (native_input.WM_LBUTTONUP, 0, lp)]
    posted.clear()
    assert native_input.post_text(1, "7a") == 2
    # WM_CHAR only — never WM_KEYDOWN, which would double every character on Qt
    assert posted == [(native_input.WM_CHAR, ord("7"), 1), (native_input.WM_CHAR, ord("a"), 1)]
    posted.clear()
    assert native_input.post_key(1, "enter") == {"key": "enter", "vk": 0x0D, "char": None}
    assert posted == [(native_input.WM_KEYDOWN, 0x0D, 1), (native_input.WM_KEYUP, 0x0D, 0xC0000001)]
    posted.clear()
    assert native_input.post_key(1, "x") == {"key": "x", "vk": None, "char": "x"}
    assert posted == [(native_input.WM_CHAR, ord("x"), 1)]


def test_resolve_key_aliases_and_rejections():
    assert native_input.resolve_key("ArrowUp") == (0x26, None)
    assert native_input.resolve_key("F5") == (0x74, None)
    with pytest.raises(native_input.NativeInputError) as ei:
        native_input.resolve_key("ctrl+a")
    assert ei.value.reason == "bad_key"


def test_post_off_windows_raises(monkeypatch):
    monkeypatch.setattr(native_input.os, "name", "posix")
    with pytest.raises(native_input.NativeInputError) as ei:
        native_input.post_click(1, 0, 0)
    assert ei.value.reason == "unsupported_platform"


# ---- tool surface -----------------------------------------------------------

def _tool_fn(mcp, name):
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == name)
    return getattr(tool, "_ftx_sync_fn", tool.fn)


def test_interact_native_target_forwards(cfg, monkeypatch):
    seen = {}

    def fake(cfg_, action, **kw):
        seen["action"] = action
        seen.update(kw)
        return {"state": "succeeded", "action": action}

    monkeypatch.setattr(core, "native_interact", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_interact")(
        action="fill", target="native", x=0.5, y=0.5, text="12",
        submit="Enter", settle_seconds=0.3)
    assert out["state"] == "succeeded"
    assert seen["action"] == "fill" and seen["text"] == "12"
    assert seen["x"] == 0.5 and seen["submit"] == "Enter" and seen["settle_seconds"] == 0.3


def test_interact_default_target_stays_web(cfg, monkeypatch):
    monkeypatch.setattr(core, "native_interact",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("native path used")))
    monkeypatch.setattr(core, "cdp_click_runtime",
                        lambda cfg_, **kw: {"state": "succeeded", "web": True})
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_interact")(action="click", x=1, y=1)
    assert out.get("web") is True
