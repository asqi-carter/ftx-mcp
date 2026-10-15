"""Offline tests for the click round-trip settle helper (1.0.9 #17)."""
from __future__ import annotations

from service.tests._click_settle import settle_region


def _seq(frames):
    calls = []

    def capture(i):
        calls.append(i)
        return frames[min(len(calls) - 1, len(frames) - 1)]

    return capture, calls


def test_settle_region_stable_on_second_capture():
    """1.7.4.32 shape: two identical frames -> exactly two captures."""
    capture, calls = _seq([b"aa", b"aa"])
    assert settle_region(capture) == (True, b"aa", 2)
    assert calls == [0, 1]


def test_settle_region_settles_after_a_repaint():
    capture, calls = _seq([b"a", b"b", b"c", b"c"])
    assert settle_region(capture) == (True, b"c", 4)


def test_settle_region_bounded_when_never_stable():
    """1.8.0.555 shape: alternating frames -> stops at the bound, unstable."""
    capture, calls = _seq([b"x" * 793, b"y" * 1196] * 10)
    stable, last, attempts = settle_region(capture, max_attempts=4)
    assert stable is False
    assert attempts == 4 and len(calls) == 4
    assert last in (b"x" * 793, b"y" * 1196)
