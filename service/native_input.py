"""Input injection into the emulator's native presentation engine window.

Companion to service.native_capture: that module finds and screenshots the
FTOptixRuntime.exe Qt window, this one drives it. Everything goes through
``PostMessageW`` to the window handle, so the runtime does NOT need to be the
foreground window and the user's focus is never stolen — verified live
2026-10-13 on Studio 1.7.1 / Qt 6.7 against Chiller_HMI:

- WM_MOUSEMOVE + WM_LBUTTONDOWN + WM_LBUTTONUP at client coordinates press
  nav buttons and focus a SpinBox (which opened the project's on-screen
  keypad), exactly like a real click.
- Text: one WM_CHAR per character types exactly one character. Posting
  WM_KEYDOWN *and* WM_CHAR for the same key types it TWICE (Qt synthesizes
  the character from the key-down and then also honors the WM_CHAR), so
  printable text is WM_CHAR only and named keys are WM_KEYDOWN/WM_KEYUP only.
- Escape closed the keypad and restored the pre-edit value; Enter commits.

Coordinates are client-area pixels of the window, the same frame
native_capture.capture_window returns, so a point read off a native
screenshot can be clicked as-is. Windows-only; off-Windows every call raises
NativeInputError("unsupported_platform").
"""
from __future__ import annotations

import os

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
MK_LBUTTON = 0x0001

# Named keys -> virtual-key codes. Names are matched case-insensitively and
# the CDP-style aliases (ArrowUp, Return, Esc) are accepted so a caller can
# use the same key names on target="web" and target="native".
KEY_VKS: dict[str, int] = {
    "enter": 0x0D, "return": 0x0D,
    "escape": 0x1B, "esc": 0x1B,
    "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "space": 0x20, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "arrowup": 0x26, "arrowdown": 0x28, "arrowleft": 0x25, "arrowright": 0x27,
    "insert": 0x2D,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}


class NativeInputError(RuntimeError):
    """An injection step failed; `reason` is a stable error code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def _post(hwnd: int, msg: int, wparam: int, lparam: int) -> None:
    """PostMessageW, raising NativeInputError on failure. The single seam
    every public function goes through (tests patch this)."""
    if os.name != "nt":
        raise NativeInputError("unsupported_platform", "native input is Windows-only")
    import ctypes
    if not ctypes.windll.user32.PostMessageW(hwnd, msg, wparam, lparam):
        err = ctypes.get_last_error()
        raise NativeInputError(
            "post_failed", f"PostMessage(0x{msg:04X}) to hwnd {hwnd} failed (winerror {err})")


def _lparam_xy(x: int, y: int) -> int:
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def post_click(hwnd: int, x: int, y: int) -> None:
    """Left-click the client-area point (x, y): move, press, release."""
    lp = _lparam_xy(x, y)
    _post(hwnd, WM_MOUSEMOVE, 0, lp)
    _post(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    _post(hwnd, WM_LBUTTONUP, 0, lp)


def post_text(hwnd: int, text: str) -> int:
    """Type `text` into whatever has keyboard focus in the window, one
    WM_CHAR per character (never WM_KEYDOWN — see module docstring).
    Returns the number of characters posted."""
    n = 0
    for ch in text:
        _post(hwnd, WM_CHAR, ord(ch), 1)
        n += 1
    return n


def resolve_key(name: str) -> tuple[int | None, str | None]:
    """Map a key name to (vk, None) for a named key, or (None, char) for a
    single printable character that should go out as WM_CHAR. Raises
    NativeInputError("bad_key") for anything else (combos like "ctrl+a" are
    not supported: a posted modifier never reaches Qt's keyboard state)."""
    if not name:
        raise NativeInputError("bad_key", "key name is empty")
    vk = KEY_VKS.get(name.strip().lower())
    if vk is not None:
        return vk, None
    if len(name) == 1:
        return None, name
    raise NativeInputError(
        "bad_key",
        f"unknown key {name!r}; use one of {', '.join(sorted(KEY_VKS))} or a single character",
    )


def post_key(hwnd: int, name: str) -> dict:
    """Press one named key (WM_KEYDOWN + WM_KEYUP) or type one character
    (WM_CHAR). Returns {"key": name, "vk": int|None, "char": str|None}."""
    vk, ch = resolve_key(name)
    if vk is not None:
        _post(hwnd, WM_KEYDOWN, vk, 1)
        _post(hwnd, WM_KEYUP, vk, 0xC0000001)
    else:
        _post(hwnd, WM_CHAR, ord(ch), 1)
    return {"key": name, "vk": vk, "char": ch}
