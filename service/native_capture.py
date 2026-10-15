"""Native-window screenshot of FTOptixRuntime.exe (Windows only).

The CDP path (core.cdp_screenshot_runtime) sees the HMI only through the
WebPresentationEngine in Chrome. A project whose UI runs on the NATIVE
presentation engine (the Qt window FTOptixRuntime.exe opens on the desktop)
has no web canvas to point Chrome at, and even when both engines are on, the
two renderers do not agree pixel-for-pixel. This module captures the native
window itself.

Mechanism: ``PrintWindow(hwnd, dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)``
into a 32-bit DIB of the window's CLIENT size, then Pillow. PW_RENDERFULLCONTENT
asks DWM for the composed surface, so a GPU-rendered Qt window comes back
with real pixels instead of black (BitBlt from the screen DC would also
need the window unobscured). Measured 2026-10-13 on Studio 1.7.1 / Qt 6.7:
~30 ms for a 1280x800 client at flag 3, content verified by eye.
PW_CLIENTONLY drops the title bar and borders so (0, 0) in the capture is
the HMI's own top-left — the frame `region` is resolved against.

Requires the service to run in the same interactive session as the runtime
window (the install's at-logon scheduled task does). Everything here is
best-effort and raises NativeCaptureError with a reason the caller turns
into a structured result; nothing is cached.
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Any

PW_CLIENTONLY = 0x1
PW_RENDERFULLCONTENT = 0x2


class NativeCaptureError(RuntimeError):
    """A capture step failed; `reason` is a stable error code."""

    def __init__(self, reason: str, detail: str = "", hint: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        self.hint = hint


# --- private Win32 handles ------------------------------------------------
# The shared ``ctypes.windll.user32`` / ``gdi32`` objects are process-wide and
# the ``uiautomation`` package (loaded by studio_uia) rewrites their function
# restypes/argtypes (e.g. GetDC/GetForegroundWindow -> c_void_p, others left
# at the c_int default). A 64-bit HWND/HDC then truncates or raises
# OverflowError here. So this module owns private ``WinDLL`` instances, built
# lazily on first use (Windows only), with explicit argtypes/restype on every
# function it calls. HWND/HDC/HBITMAP/HGDIOBJ are c_void_p-based, so full
# 64-bit handle values survive both directions.

# name -> (restype, argtypes)
_USER32_PROTOTYPES: dict[str, tuple[Any, tuple]] = {
    "GetDC": (wintypes.HDC, (wintypes.HWND,)),
    "ReleaseDC": (ctypes.c_int, (wintypes.HWND, wintypes.HDC)),
    "PrintWindow": (wintypes.BOOL, (wintypes.HWND, wintypes.HDC, wintypes.UINT)),
    "GetClientRect": (wintypes.BOOL, (wintypes.HWND, ctypes.POINTER(wintypes.RECT))),
    "IsWindow": (wintypes.BOOL, (wintypes.HWND,)),
    "IsWindowVisible": (wintypes.BOOL, (wintypes.HWND,)),
    "IsIconic": (wintypes.BOOL, (wintypes.HWND,)),
    "EnumWindows": (wintypes.BOOL, (ctypes.c_void_p, wintypes.LPARAM)),
    "GetWindowThreadProcessId": (
        wintypes.DWORD, (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))),
    "GetWindowTextW": (ctypes.c_int, (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)),
    "GetWindowTextLengthW": (ctypes.c_int, (wintypes.HWND,)),
    "GetClassNameW": (ctypes.c_int, (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)),
    "SetProcessDPIAware": (wintypes.BOOL, ()),
}
_GDI32_PROTOTYPES: dict[str, tuple[Any, tuple]] = {
    "CreateCompatibleDC": (wintypes.HDC, (wintypes.HDC,)),
    "CreateCompatibleBitmap": (
        wintypes.HBITMAP, (wintypes.HDC, ctypes.c_int, ctypes.c_int)),
    "SelectObject": (wintypes.HGDIOBJ, (wintypes.HDC, wintypes.HGDIOBJ)),
    "DeleteObject": (wintypes.BOOL, (wintypes.HGDIOBJ,)),
    "DeleteDC": (wintypes.BOOL, (wintypes.HDC,)),
    "GetDIBits": (ctypes.c_int, (
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT)),
}
_SHCORE_PROTOTYPES: dict[str, tuple[Any, tuple]] = {
    "SetProcessDpiAwareness": (ctypes.c_long, (ctypes.c_int,)),
}

_WNDENUMPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_dll_lock = threading.Lock()
_user32_dll: Any = None
_gdi32_dll: Any = None
_shcore_dll: Any = None


def _is_windows() -> bool:
    """Seam: tests flip this (not the global os.name) to run the Win32 path."""
    return os.name == "nt"


def _load_dll(name: str, prototypes: dict[str, tuple[Any, tuple]]) -> Any:
    """Create a private WinDLL and declare restype/argtypes for `prototypes`."""
    dll = ctypes.WinDLL(name, use_last_error=True)  # type: ignore[attr-defined]
    for fn_name, (restype, argtypes) in prototypes.items():
        fn = getattr(dll, fn_name)
        fn.restype = restype
        fn.argtypes = list(argtypes)
    return dll


def _user32() -> Any:
    """Lazily created private user32 with explicit prototypes (seam)."""
    global _user32_dll
    with _dll_lock:
        if _user32_dll is None:
            _user32_dll = _load_dll("user32", _USER32_PROTOTYPES)
        return _user32_dll


def _gdi32() -> Any:
    """Lazily created private gdi32 with explicit prototypes (seam)."""
    global _gdi32_dll
    with _dll_lock:
        if _gdi32_dll is None:
            _gdi32_dll = _load_dll("gdi32", _GDI32_PROTOTYPES)
        return _gdi32_dll


def _shcore() -> Any:
    """Lazily created private shcore with explicit prototypes (seam)."""
    global _shcore_dll
    with _dll_lock:
        if _shcore_dll is None:
            _shcore_dll = _load_dll("shcore", _SHCORE_PROTOTYPES)
        return _shcore_dll


_dpi_aware_set = False


def _ensure_dpi_aware() -> None:
    """Make this process per-monitor DPI aware once, so GetClientRect reports
    physical pixels and the DIB is captured at native resolution instead of a
    scaled-down logical size. A refusal (already set by the host, or an old
    Windows) is fine — the capture still works at whatever awareness is in
    effect."""
    global _dpi_aware_set
    if _dpi_aware_set:
        return
    _dpi_aware_set = True
    try:
        try:
            _shcore().SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        except Exception:
            _user32().SetProcessDPIAware()
    except Exception:
        pass


def runtime_windows(pids: list[int]) -> list[dict]:
    """Visible top-level windows owned by any of `pids`, largest client area
    first: [{hwnd, pid, title, class_name, width, height}, ...].

    Zero-size windows (Qt's hidden helper windows, a minimized main window)
    are skipped. Empty list off-Windows or when nothing qualifies.
    """
    if not _is_windows() or not pids:
        return []

    _ensure_dpi_aware()
    user32 = _user32()
    pid_set = set(pids)
    out: list[dict] = []

    def _collect(hwnd, _lparam) -> bool:
        try:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value not in pid_set or not user32.IsWindowVisible(hwnd):
                return True
            if user32.IsIconic(hwnd):
                return True
            rc = wintypes.RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
                return True
            w, h = rc.right - rc.left, rc.bottom - rc.top
            if w <= 0 or h <= 0:
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            out.append({
                "hwnd": int(hwnd), "pid": int(owner.value), "title": buf.value,
                "class_name": cls.value, "width": int(w), "height": int(h),
            })
        except Exception:
            pass
        return True

    user32.EnumWindows(_WNDENUMPROC(_collect), 0)
    out.sort(key=lambda d: d["width"] * d["height"], reverse=True)
    return out


def capture_window(hwnd: int) -> Any:
    """PrintWindow the CLIENT area of `hwnd` into a Pillow RGB Image.

    Raises NativeCaptureError(reason) for: a window that vanished or has a
    zero-size client (`window_gone`), a GDI allocation failure (`gdi_failed`),
    or PrintWindow itself declining (`print_window_failed` — the process may
    be hung, or the window belongs to another session).
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise NativeCaptureError(
            "pillow_missing", "Pillow is not installed", hint="pip install Pillow") from e
    if not _is_windows():
        raise NativeCaptureError("unsupported_platform", "native capture is Windows-only")

    _ensure_dpi_aware()
    user32 = _user32()
    gdi32 = _gdi32()

    rc = wintypes.RECT()
    if not user32.IsWindow(hwnd) or not user32.GetClientRect(hwnd, ctypes.byref(rc)):
        raise NativeCaptureError("window_gone", f"hwnd {hwnd} is no longer a window")
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        raise NativeCaptureError("window_gone", f"hwnd {hwnd} has a {w}x{h} client area")

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    hdc = user32.GetDC(hwnd)
    if not hdc:
        raise NativeCaptureError("gdi_failed", "GetDC returned NULL")
    mdc = bmp = old = None
    try:
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        if not mdc or not bmp:
            raise NativeCaptureError("gdi_failed", "CreateCompatibleDC/Bitmap failed")
        old = gdi32.SelectObject(mdc, bmp)
        if not user32.PrintWindow(hwnd, mdc, PW_CLIENTONLY | PW_RENDERFULLCONTENT):
            raise NativeCaptureError(
                "print_window_failed",
                f"PrintWindow refused hwnd {hwnd} (process hung, or window in another session?)",
            )
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight = w, -h   # negative height: top-down rows
        bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(w * h * 4)
        if gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0) != h:
            raise NativeCaptureError("gdi_failed", "GetDIBits returned short")
        return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        try:
            if old and mdc:
                gdi32.SelectObject(mdc, old)
            if bmp:
                gdi32.DeleteObject(bmp)
            if mdc:
                gdi32.DeleteDC(mdc)
            user32.ReleaseDC(hwnd, hdc)
        except Exception:
            pass
