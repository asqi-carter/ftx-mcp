"""Offline-testable helpers for the live click round-trip test (1.0.9 #17).

Kept out of ``test_bridge_live.py`` because that module is skipped wholesale
unless ``FTX_LIVE_BRIDGE=1``; the settle logic is pure and is unit-tested in
``test_click_settle.py`` without a bench.
"""
from __future__ import annotations

from typing import Callable

#: Upper bound on region captures while waiting for two identical frames.
SETTLE_MAX_ATTEMPTS = 5

PATH_PIXEL = "pixel"
PATH_BRIDGE_READ = "bridge-read fallback"


def settle_region(
    capture: Callable[[int], bytes],
    max_attempts: int = SETTLE_MAX_ATTEMPTS,
) -> tuple[bool, bytes, int]:
    """Capture a region repeatedly until two consecutive captures are identical.

    ``capture(i)`` takes the 0-based attempt index (so the caller can navigate
    on the first capture only) and returns the region's bytes. At most
    ``max_attempts`` captures are taken (minimum 2).

    Returns ``(stable, last_capture, attempts)``. ``stable`` is True when two
    consecutive captures matched; ``last_capture`` is then the settled frame.
    When the region never stabilises (e.g. Optix 1.8.0.555 repaints part of it
    on its own) ``stable`` is False and ``last_capture`` is the final frame.
    """
    max_attempts = max(2, int(max_attempts))
    prev = capture(0)
    for i in range(1, max_attempts):
        cur = capture(i)
        if cur == prev:
            return True, cur, i + 1
        prev = cur
    return False, prev, max_attempts
