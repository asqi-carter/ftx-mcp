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


def test_observe_screenshot_native_target_forwards_and_synthesizes_path(cfg, monkeypatch):
    seen = {}

    def fake(cfg_, save_path=None, quality=65, region=None, project=None,
             settle_seconds=None, **_kw):
        seen.update(save_path=save_path, quality=quality, region=region,
                    settle_seconds=settle_seconds)
        return {"state": "succeeded", "path": save_path, "size_bytes": 1}

    monkeypatch.setattr(core, "native_screenshot_runtime", fake)
    monkeypatch.setattr(core, "cdp_screenshot_runtime",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("web path used")))
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(
        mode="screenshot", target="native", quality=70, region=[0, 0, 1, 1],
        settle_seconds=0.5)
    assert seen["quality"] == 70 and seen["region"] == [0, 0, 1, 1]
    assert seen["settle_seconds"] == 0.5
    assert seen["save_path"].endswith(".jpg") and "ftx-native-screenshots" in seen["save_path"]
    assert out["state"] == "succeeded" and "file tool" in out["hint"]


def test_observe_screenshot_default_target_stays_web(cfg, monkeypatch):
    monkeypatch.setattr(core, "native_screenshot_runtime",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("native path used")))
    monkeypatch.setattr(core, "cdp_screenshot_runtime",
                        lambda cfg_, save_path=None, **kw: {"state": "succeeded", "path": save_path})
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(mode="screenshot")
    assert out["state"] == "succeeded" and "ftx-cdp-screenshots" in out["path"]


def test_observe_native_target_return_image_list(cfg, monkeypatch, tmp_path):
    from mcp.server.fastmcp import Image as McpImage

    shot = tmp_path / "n.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xdbfakejpeg")
    monkeypatch.setattr(
        core, "native_screenshot_runtime",
        lambda cfg_, save_path=None, **kw: {"state": "succeeded", "path": str(shot)})
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(
        mode="screenshot", target="native", save_path=str(shot), return_image=True)
    assert isinstance(out, list) and len(out) == 2
    assert _json.loads(out[0])["path"] == str(shot)
    assert isinstance(out[1], McpImage)


def test_observe_native_target_failure_passes_through(cfg, monkeypatch):
    monkeypatch.setattr(
        core, "native_screenshot_runtime",
        lambda cfg_, **kw: {"state": "failed", "error": "native_runtime_not_found"})
    mcp = make_mcp(cfg)
    out = _tool_fn(mcp, "optix_observe")(mode="screenshot", target="native", return_image=True)
    assert isinstance(out, dict) and out["error"] == "native_runtime_not_found"


@pytest.mark.parametrize("mode,core_fn,extra", [
    ("ocr", "cdp_ocr_runtime", {}),
    ("read_text", "cdp_read_text_runtime", {"region": [0, 0, 0.5, 0.5]}),
    ("find_text", "cdp_find_text_runtime", {"text": "Start"}),
])
def test_observe_ocr_family_passes_target(cfg, monkeypatch, mode, core_fn, extra):
    seen = {}

    def fake(cfg_, *a, **kw):
        seen.update(kw)
        return {"state": "succeeded"}

    monkeypatch.setattr(core, core_fn, fake)
    mcp = make_mcp(cfg)
    _tool_fn(mcp, "optix_observe")(mode=mode, target="native", **extra)
    assert seen["target"] == "native"
    _tool_fn(mcp, "optix_observe")(mode=mode, **extra)
    assert seen["target"] == "web"


# ---- core: OCR family on the native frame -----------------------------------

_TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "5\t1\t1\t1\t1\t1\t10\t10\t80\t30\t95.5\tStart\n"
    "5\t1\t1\t1\t1\t2\t95\t10\t100\t30\t92.0\tButton\n"
)


class _Proc:
    def __init__(self, stdout: str) -> None:
        self.returncode, self.stdout, self.stderr = 0, stdout, ""


class _Runner:
    def __init__(self) -> None:
        self.cmds: list[list[str]] = []

    def run(self, cmd, **_kw):
        self.cmds.append(list(cmd))
        return _Proc(_TSV)


def _native_jpeg_writer(width=120, height=80):
    """Fake core.native_screenshot_runtime: writes a real JPEG to save_path,
    or returns it as b64 when no save_path (find_text's path)."""
    import base64
    import io

    def fake(cfg_, save_path=None, region=None, project=None, settle_seconds=None,
             quality=65, **_kw):
        buf = io.BytesIO()
        _img(width, height).save(buf, format="JPEG")
        out = {"state": "succeeded", "size_bytes": buf.tell(), "width": width,
               "height": height, "region": region, "navigated": False}
        if save_path:
            with open(save_path, "wb") as fh:
                fh.write(buf.getvalue())
            out["path"] = save_path
        else:
            out["b64"] = base64.b64encode(buf.getvalue()).decode("ascii")
        return out
    return fake


def test_ocr_native_target_captures_native_frame(cfg, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(core, "native_screenshot_runtime", _native_jpeg_writer())
    monkeypatch.setattr(core, "cdp_screenshot_runtime",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("web path used")))
    runner = _Runner()
    out = core.cdp_ocr_runtime(cfg, runner=runner, target="native", project="Alpha")
    assert out["state"] == "succeeded" and out["target"] == "native"
    assert "Start Button" in out["text"] and out["navigated"] is False
    assert runner.cmds and runner.cmds[0][-1] == "tsv"


def test_read_text_native_target_forwards_region(cfg, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(core, "native_screenshot_runtime", _native_jpeg_writer())
    out = core.cdp_read_text_runtime(cfg, region=[0, 0, 0.5, 0.5], runner=_Runner(),
                                     target="native")
    assert out["state"] == "succeeded" and out["region"] == [0, 0, 0.5, 0.5]


def test_ocr_native_target_failure_is_structured(cfg, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(core, "native_screenshot_runtime",
                        lambda cfg_, **kw: {"state": "failed", "error": "native_runtime_not_found",
                                            "message": "no emulator"})
    out = core.cdp_ocr_runtime(cfg, runner=_Runner(), target="native")
    assert out["state"] == "failed" and out["error"] == "native_runtime_not_found"
    assert out["message"] == "no emulator" and out["target"] == "native"


def test_find_text_native_target_boxes_are_frame_pixels(cfg, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(core, "native_screenshot_runtime", _native_jpeg_writer(1000, 800))
    monkeypatch.setattr(core, "_cdp_session",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("CDP session opened")))
    out = core.cdp_find_text_runtime(cfg, "Start Button", runner=_Runner(), target="native")
    assert out["state"] == "succeeded" and out["found"] is True and out["target"] == "native"
    m = out["matches"][0]
    # native frame == click frame: no CSS rescale, boxes are raw frame pixels
    assert m["bbox_px"] == [10.0, 10.0, 185.0, 30.0]
    assert m["center_px"] == [102.5, 25.0]
    assert out["viewport"] == {"w": 1000.0, "h": 800.0} and out["navigated"] is False


# ---- native_capture off-Windows guards --------------------------------------

def test_runtime_windows_empty_for_no_pids():
    assert native_capture.runtime_windows([]) == []


def test_capture_window_off_windows_raises(monkeypatch):
    monkeypatch.setattr(native_capture.os, "name", "posix")
    with pytest.raises(native_capture.NativeCaptureError) as ei:
        native_capture.capture_window(1)
    assert ei.value.reason == "unsupported_platform"


# ---- private Win32 handles (PRD 0047) ---------------------------------------
# The fake DLLs are built from native_capture's own prototype tables as real
# ctypes callbacks, so every call from capture_window is converted through the
# declared argtypes/restype exactly like a WinDLL function would be. A table
# entry that truncated a handle to c_int would hand the callback 0x1 instead
# of 0x1_0000_0001, and an undeclared function would fail the table test.

import ast
import ctypes
import inspect
import types

_BIG_HWND = 0x1_0000_0001
_BIG_WDC = 0x2_0000_0002      # window DC
_BIG_MDC = 0x3_0000_0003      # memory DC
_BIG_BMP = 0x4_0000_0004
_BIG_OLD = 0x5_0000_0005


def _fake_dll(protos, impls):
    ns = types.SimpleNamespace()
    for name, (restype, argtypes) in protos.items():
        cb_t = ctypes.CFUNCTYPE(restype, *argtypes)
        # keep the callback alive for the lifetime of the namespace
        setattr(ns, name, cb_t(impls[name]))
    return ns


def _fake_win32(w=4, h=3, calls=None, fail_print=False):
    calls = calls if calls is not None else []

    def rec(name, ret=1):
        def f(*a):
            calls.append((name, a))
            return ret
        return f

    def get_client_rect(hwnd, prc):
        calls.append(("GetClientRect", (hwnd,)))
        prc.contents.left, prc.contents.top = 0, 0
        prc.contents.right, prc.contents.bottom = w, h
        return 1

    def get_dibits(hdc, bmp, start, lines, bits, bi, usage):
        calls.append(("GetDIBits", (hdc, bmp, start, lines)))
        ctypes.memmove(bits, bytes([0, 0, 255, 255]) * (w * h), w * h * 4)  # BGRA red
        return lines

    u_impl = {
        "GetDC": rec("GetDC", _BIG_WDC), "ReleaseDC": rec("ReleaseDC"),
        "PrintWindow": rec("PrintWindow", 0 if fail_print else 1),
        "GetClientRect": get_client_rect, "IsWindow": rec("IsWindow"),
        "IsWindowVisible": rec("IsWindowVisible"), "IsIconic": rec("IsIconic", 0),
        "EnumWindows": rec("EnumWindows"),
        "GetWindowThreadProcessId": rec("GetWindowThreadProcessId", 0),
        "GetWindowTextW": rec("GetWindowTextW", 0),
        "GetWindowTextLengthW": rec("GetWindowTextLengthW", 0),
        "GetClassNameW": rec("GetClassNameW", 0), "SetProcessDPIAware": rec("SetProcessDPIAware"),
    }
    g_impl = {
        "CreateCompatibleDC": rec("CreateCompatibleDC", _BIG_MDC),
        "CreateCompatibleBitmap": rec("CreateCompatibleBitmap", _BIG_BMP),
        "SelectObject": rec("SelectObject", _BIG_OLD),
        "DeleteObject": rec("DeleteObject"), "DeleteDC": rec("DeleteDC"),
        "GetDIBits": get_dibits,
    }
    s_impl = {"SetProcessDpiAwareness": rec("SetProcessDpiAwareness", 0)}
    return (_fake_dll(native_capture._USER32_PROTOTYPES, u_impl),
            _fake_dll(native_capture._GDI32_PROTOTYPES, g_impl),
            _fake_dll(native_capture._SHCORE_PROTOTYPES, s_impl), calls)


def _use_fake_win32(monkeypatch, **kw):
    user32, gdi32, shcore, calls = _fake_win32(**kw)
    monkeypatch.setattr(native_capture, "_is_windows", lambda: True)
    monkeypatch.setattr(native_capture, "_user32", lambda: user32)
    monkeypatch.setattr(native_capture, "_gdi32", lambda: gdi32)
    monkeypatch.setattr(native_capture, "_shcore", lambda: shcore)
    monkeypatch.setattr(native_capture, "_dpi_aware_set", True)
    return calls


def test_capture_window_64bit_handles_no_overflow(monkeypatch):
    calls = _use_fake_win32(monkeypatch)
    im = native_capture.capture_window(_BIG_HWND)   # must not raise OverflowError
    assert im.size == (4, 3) and im.getpixel((0, 0)) == (255, 0, 0)
    by_name = {n: a for n, a in calls}
    # every handle crosses the argtypes/restype boundary untruncated
    assert by_name["IsWindow"] == (_BIG_HWND,)
    assert by_name["GetClientRect"] == (_BIG_HWND,)
    assert by_name["CreateCompatibleDC"] == (_BIG_WDC,)
    assert by_name["CreateCompatibleBitmap"][0] == _BIG_WDC
    assert [a for n, a in calls if n == "SelectObject"] == [
        (_BIG_MDC, _BIG_BMP), (_BIG_MDC, _BIG_OLD)]
    assert by_name["PrintWindow"] == (_BIG_HWND, _BIG_MDC, 3)
    assert by_name["GetDIBits"] == (_BIG_MDC, _BIG_BMP, 0, 3)
    assert by_name["DeleteObject"] == (_BIG_BMP,) and by_name["DeleteDC"] == (_BIG_MDC,)
    assert by_name["ReleaseDC"] == (_BIG_HWND, _BIG_WDC)


def test_capture_window_print_failure_still_releases_64bit_handles(monkeypatch):
    calls = _use_fake_win32(monkeypatch, fail_print=True)
    with pytest.raises(native_capture.NativeCaptureError) as ei:
        native_capture.capture_window(_BIG_HWND)
    assert ei.value.reason == "print_window_failed"
    names = [n for n, _a in calls]
    assert {"DeleteObject", "DeleteDC", "ReleaseDC"} <= set(names)


def test_capture_window_ignores_mutated_shared_windll(monkeypatch):
    """uiautomation rewrites restypes on the shared ctypes.windll objects. The
    capture path must not consult them, whatever state they are in."""
    class _Mutated:
        def __getattr__(self, name):
            raise AssertionError(f"shared windll.{name} must not be used")

    shared = types.SimpleNamespace(user32=_Mutated(), gdi32=_Mutated(), shcore=_Mutated())
    monkeypatch.setattr(ctypes, "windll", shared, raising=False)
    _use_fake_win32(monkeypatch)
    assert native_capture.capture_window(_BIG_HWND).size == (4, 3)
    tree = ast.parse(inspect.getsource(native_capture))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "windll"]


def test_runtime_windows_uses_private_user32_with_64bit_hwnd(monkeypatch):
    user32, _gdi32, _shcore, _calls = _fake_win32(w=10, h=5)
    seen_pid = {}
    protos = native_capture._USER32_PROTOTYPES

    def override(name, fn):
        restype, argtypes = protos[name]
        setattr(user32, name, ctypes.CFUNCTYPE(restype, *argtypes)(fn))

    def enum_windows(proc_addr, lparam):
        # the callback arrives as a raw address; call it back like Windows would
        cb = ctypes.cast(proc_addr, native_capture._WNDENUMPROC)
        return cb(_BIG_HWND, 0)

    def owner(hwnd, pid_p):
        seen_pid["hwnd"] = hwnd
        pid_p.contents.value = 777
        return 1

    override("EnumWindows", enum_windows)
    override("GetWindowThreadProcessId", owner)
    monkeypatch.setattr(native_capture, "_is_windows", lambda: True)
    monkeypatch.setattr(native_capture, "_user32", lambda: user32)
    monkeypatch.setattr(native_capture, "_dpi_aware_set", True)
    out = native_capture.runtime_windows([777])
    assert seen_pid["hwnd"] == _BIG_HWND
    assert [d["hwnd"] for d in out] == [_BIG_HWND]
    assert (out[0]["pid"], out[0]["width"], out[0]["height"]) == (777, 10, 5)


def test_every_win32_call_in_native_capture_has_declared_prototype():
    tables = {"user32": native_capture._USER32_PROTOTYPES,
              "_user32": native_capture._USER32_PROTOTYPES,
              "gdi32": native_capture._GDI32_PROTOTYPES,
              "_gdi32": native_capture._GDI32_PROTOTYPES,
              "shcore": native_capture._SHCORE_PROTOTYPES,
              "_shcore": native_capture._SHCORE_PROTOTYPES}
    used = 0
    for node in ast.walk(ast.parse(inspect.getsource(native_capture))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        base = node.func.value
        if isinstance(base, ast.Call) and isinstance(base.func, ast.Name):
            base = base.func
        if isinstance(base, ast.Name) and base.id in tables:
            assert node.func.attr in tables[base.id], f"{base.id}.{node.func.attr} undeclared"
            used += 1
    assert used >= 20
    for table in tables.values():
        for restype, argtypes in table.values():
            assert restype is not None and isinstance(argtypes, tuple)


def test_private_dlls_are_lazy_cached_and_declare_prototypes(monkeypatch):
    created: list[tuple] = []

    class _Fn:
        restype = argtypes = None

    class _Dll:
        def __init__(self, name, **kw):
            created.append((name, kw))
            self._fns: dict = {}

        def __getattr__(self, n):
            return self._fns.setdefault(n, _Fn())

    monkeypatch.setattr(ctypes, "WinDLL", _Dll, raising=False)
    for attr in ("_user32_dll", "_gdi32_dll", "_shcore_dll"):
        monkeypatch.setattr(native_capture, attr, None)
    assert created == []                       # nothing at import time
    u = native_capture._user32()
    assert native_capture._user32() is u and [c[0] for c in created] == ["user32"]
    g = native_capture._gdi32()
    native_capture._shcore()
    assert [c[0] for c in created] == ["user32", "gdi32", "shcore"]
    for dll, table in ((u, native_capture._USER32_PROTOTYPES),
                       (g, native_capture._GDI32_PROTOTYPES)):
        for name, (restype, argtypes) in table.items():
            fn = getattr(dll, name)
            assert fn.restype is restype and fn.argtypes == list(argtypes)
