"""Session detection for the live suite (ftx-mcp-ver.3, PRD 0027 #22).

Kept out of ``test_bridge_live.py`` because that module is skipped wholesale
unless ``FTX_LIVE_BRIDGE=1``; the detection logic is unit-tested in
``test_live_session.py`` without a bench.

GUI-touching live tests (save, emulator run/restart, anything that sends keys
or focuses Studio) need an input desktop. Over plain ssh the suite runs in
session 0, which has none: the focus helper then reports
``bridge_studio_no_window``, which is the environment talking, not a product
failure. Those tests SKIP with ``SKIP_REASON`` instead.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable

#: Skip reason used by every GUI-touching live test in a non-interactive session.
SKIP_REASON = "session_0_no_desktop"

#: Test override: "0"/"session0" force non-interactive, "1"/"interactive" force interactive.
ENV_OVERRIDE = "FTX_LIVE_SESSION"


def _windows_probe() -> bool:
    """True iff this process is outside session 0 AND can open the input desktop."""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    session_id = ctypes.c_ulong(0)
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session_id)):
        return False
    if session_id.value == 0:
        return False
    # OpenInputDesktop fails (NULL) when the process has no interactive desktop
    # (service window station, disconnected session, locked secure desktop).
    desk = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if not desk:
        return False
    user32.CloseDesktop(desk)
    return True


def has_interactive_desktop(
    probe: Callable[[], bool] | None = None,
    env: dict[str, str] | None = None,
    platform: str | None = None,
) -> bool:
    """Return True when GUI-touching live tests can run in this session.

    ``probe``/``env``/``platform`` are injectable for tests. Non-Windows hosts
    have no Studio window to focus, so they count as having no desktop.
    """
    environ = os.environ if env is None else env
    override = environ.get(ENV_OVERRIDE, "").strip().lower()
    if override in ("0", "session0", "none"):
        return False
    if override in ("1", "interactive"):
        return True
    if (platform or sys.platform) != "win32":
        return False
    try:
        return bool((probe or _windows_probe)())
    except Exception:
        return False


def skip_reason_if_no_desktop(**kwargs) -> str | None:
    """Return ``SKIP_REASON`` in a non-interactive session, else None."""
    return None if has_interactive_desktop(**kwargs) else SKIP_REASON
