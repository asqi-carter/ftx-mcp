"""optix_observe(mode="native_screenshot") — capture the emulator's native
presentation engine window via service.native_capture.

The Win32 layer (process scan, EnumWindows, PrintWindow) is injected through
core.native_screenshot_runtime's seams so the suite runs off-Windows and
without a live runtime; the real PrintWindow path is exercised only by hand.
"""
from __future__ import annotations

import base64
import json as _json

import pytest
from PIL import Image

from service import core, native_capture
from service.mcp_app import make_mcp


def _img(w: int = 120, h: int = 80, colour=(200, 30, 30)) -> Image.Image:
    im = Image.new("RGB", (w, h), colour)
    # a contrasting block so the frame is not "blank"
    im.paste((10, 10, 10), (0, 0, w // 2, h // 2))
    return im


_WIN = {"hwnd": 4242, "pid": 777, "title": "Chiller_HMI",
        "class_name": "Qt673QWindowIcon", "width": 120, "height": 80}


def _seams(pids=(777,), windows=(_WIN,), image=None, raise_=None):
    def pids_fn(project):
        return list(pids)

    def windows_fn(p):
        assert p == list(pids)
        return list(windows)

    def capture_fn(hwnd):
        assert hwnd == _WIN["hwnd"]
        if raise_ is not None:
            raise raise_
        return image or _img()
    return {"pids_fn": pids_fn, "windows_fn": windows_fn, "capture_fn": capture_fn}


def test_native_screenshot_saves_jpeg_and_reports_window(cfg, tmp_path):
    out = tmp_path / "n.jpg"
    r = core.native_screenshot_runtime(cfg, save_path=str(out), quality=80,
                                       project="Alpha", **_seams())
    assert r["state"] == "succeeded" and r["source"] == "native"
    assert r["path"] == str(out) and out.read_bytes()[:3] == b"\xff\xd8\xff"
    assert r["size_bytes"] == out.stat().st_size
    assert (r["width"], r["height"]) == (120, 80)
    assert r["pid"] == 777 and r["hwnd"] == 4242 and r["title"] == "Chiller_HMI"
    assert r["region"] is None and r["blank"] is False
    assert r["windows_seen"] == 1


def test_native_screenshot_b64_when_no_save_path(cfg):
    r = core.native_screenshot_runtime(cfg, **_seams())
    assert r["state"] == "succeeded" and r["path"] is None
    assert base64.b64decode(r["b64"])[:3] == b"\xff\xd8\xff"


def test_native_screenshot_picks_first_window_listed(cfg):
    """runtime_windows sorts largest-first; the runtime takes [0] as given."""
    small = dict(_WIN, hwnd=1, width=10, height=10)
    seams = _seams(windows=(_WIN, small))
    r = core.native_screenshot_runtime(cfg, **seams)
    assert r["hwnd"] == 4242 and r["windows_seen"] == 2


def test_native_screenshot_region_normalized_crops(cfg):
    r = core.native_screenshot_runtime(cfg, region=[0.5, 0.5, 0.5, 0.5], **_seams())
    assert r["state"] == "succeeded"
    assert (r["width"], r["height"]) == (60, 40)
    assert r["region"] == [60, 40, 60, 40]


def test_native_screenshot_region_absolute_clamps_to_frame(cfg):
    r = core.native_screenshot_runtime(cfg, region=[100, 60, 500, 500], **_seams())
    assert r["state"] == "succeeded"
    assert r["region"] == [100, 60, 20, 20]


@pytest.mark.parametrize("region", [[1, 2, 3], [0, 0, 0, 5], ["a", 0, 1, 1]])
def test_native_screenshot_bad_region_shape_fails_before_capture(cfg, region):
    def boom(hwnd):
        raise AssertionError("capture must not run for a malformed region")
    seams = _seams()
    seams["capture_fn"] = boom
    r = core.native_screenshot_runtime(cfg, region=region, **seams)
    assert r["state"] == "failed" and r["error"] == "bad_region"


def test_native_screenshot_region_outside_frame_fails(cfg):
    r = core.native_screenshot_runtime(cfg, region=[500, 500, 10, 10], **_seams())
    assert r["state"] == "failed" and r["error"] == "bad_region"
    assert r["width"] == 120 and r["height"] == 80


def test_native_screenshot_no_runtime(cfg):
    r = core.native_screenshot_runtime(cfg, project="Alpha", **_seams(pids=()))
    assert r["state"] == "failed" and r["error"] == "native_runtime_not_found"
    assert "Alpha" in r["message"] and "optix_emulator" in r["hint"]


def test_native_screenshot_no_window(cfg):
    r = core.native_screenshot_runtime(cfg, **_seams(windows=()))
    assert r["state"] == "failed" and r["error"] == "native_window_not_found"
    assert r["pids"] == [777] and "minimized" in r["hint"]


def test_native_screenshot_capture_error_is_structured(cfg):
    err = native_capture.NativeCaptureError("print_window_failed", "refused")
    r = core.native_screenshot_runtime(cfg, **_seams(raise_=err))
    assert r["state"] == "failed" and r["error"] == "print_window_failed"
    assert r["message"] == "refused" and r["hwnd"] == 4242


def test_native_screenshot_flags_blank_frame(cfg):
    flat = Image.new("RGB", (50, 50), (0, 0, 0))
    r = core.native_screenshot_runtime(cfg, **_seams(image=flat))
    assert r["state"] == "succeeded" and r["blank"] is True
    assert "settle_seconds" in r["hint"]


def test_native_screenshot_settle_sleeps_only_when_asked(cfg, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(core.time, "sleep", lambda s: slept.append(s))
    core.native_screenshot_runtime(cfg, **_seams())
    assert slept == []
    core.native_screenshot_runtime(cfg, settle_seconds=0.7, **_seams())
    assert slept == [0.7]


# ---- tool surface -----------------------------------------------------------

def _tool_fn(mcp, name):
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == name)
    return getattr(tool, "_ftx_sync_fn", tool.fn)


def test_observe_native_mode_forwards_and_synthesizes_path(cfg, monkeypatch, tmp_path):
    seen = {}

    def fake(cfg_, save_path=None, quality=65, region=None, project=None,
             settle_seconds=None, **_kw):
        seen.update(save_path=save_path, quality=quality, region=region,
                    settle_seconds=settle_seconds)
        return {"state": "succeeded", "path": save_path, "size_bytes": 1}

    monkeypatch.setattr(core, "native_screenshot_runtime", fake)
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(
        mode="native_screenshot", quality=70, region=[0, 0, 1, 1],
        settle_seconds=0.5)
    assert seen["quality"] == 70 and seen["region"] == [0, 0, 1, 1]
    assert seen["settle_seconds"] == 0.5
    assert seen["save_path"].endswith(".jpg") and "ftx-native-screenshots" in seen["save_path"]
    assert out["state"] == "succeeded" and "native runtime window" in out["hint"]


def test_observe_native_mode_return_image_list(cfg, monkeypatch, tmp_path):
    from mcp.server.fastmcp import Image as McpImage

    shot = tmp_path / "n.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xdbfakejpeg")
    monkeypatch.setattr(
        core, "native_screenshot_runtime",
        lambda cfg_, save_path=None, **kw: {"state": "succeeded", "path": str(shot)})
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(
        mode="native_screenshot", save_path=str(shot), return_image=True)
    assert isinstance(out, list) and len(out) == 2
    assert _json.loads(out[0])["path"] == str(shot)
    assert isinstance(out[1], McpImage)


def test_observe_native_mode_failure_passes_through(cfg, monkeypatch):
    monkeypatch.setattr(
        core, "native_screenshot_runtime",
        lambda cfg_, **kw: {"state": "failed", "error": "native_runtime_not_found"})
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(mode="native_screenshot", return_image=True)
    assert isinstance(out, dict) and out["error"] == "native_runtime_not_found"


# ---- native_capture off-Windows guards --------------------------------------

def test_runtime_windows_empty_for_no_pids():
    assert native_capture.runtime_windows([]) == []


def test_capture_window_off_windows_raises(monkeypatch):
    monkeypatch.setattr(native_capture.os, "name", "posix")
    with pytest.raises(native_capture.NativeCaptureError) as ei:
        native_capture.capture_window(1)
    assert ei.value.reason == "unsupported_platform"
