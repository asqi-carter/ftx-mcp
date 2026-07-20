"""Tests for core.cdp_ocr_runtime — the opt-in tesseract read-back fallback.

Offline: the screenshot capture (cdp_screenshot_runtime) is stubbed, shutil.which
and the tesseract subprocess are faked. The real tesseract is validated on the
Windows box; these cover the wrapper's routing + failure interpretation."""
from __future__ import annotations

import shutil

import pytest

from service import core
from service.tests.conftest import FakeProc, make_fake_runner


def _stub_shot(monkeypatch, **over) -> None:
    base = {"state": "succeeded", "size_bytes": 42, "navigated": True}
    base.update(over)
    monkeypatch.setattr(core, "cdp_screenshot_runtime",
                        lambda *a, **k: base)


def test_ocr_returns_recognized_text(cfg, monkeypatch) -> None:
    _stub_shot(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "Hello Optix\nStart"))
    out = core.cdp_ocr_runtime(cfg, runner=runner)
    assert out["state"] == "succeeded"
    assert "Hello Optix" in out["text"]
    assert out["size_bytes"] == 42 and out["navigated"] is True
    # invoked tesseract with a psm and stdout target
    cmd = runner.calls[0][0]
    assert cmd[0] == "/usr/bin/tesseract" and "stdout" in cmd and "--psm" in cmd


def test_ocr_missing_binary_is_soft_failure(cfg, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    # screenshot must NOT even be attempted when tesseract is absent
    monkeypatch.setattr(core, "cdp_screenshot_runtime",
                        lambda *a, **k: pytest.fail("should not capture"))
    out = core.cdp_ocr_runtime(cfg)
    assert out["state"] == "failed"
    assert out["error"] == "tesseract_not_installed"
    assert "PATH" in out["hint"]


def test_ocr_propagates_screenshot_failure(cfg, monkeypatch) -> None:
    _stub_shot(monkeypatch, state="failed", error="cdp_unavailable")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: pytest.fail("tesseract should not run"))
    out = core.cdp_ocr_runtime(cfg, runner=runner)
    assert out["state"] == "failed" and out["error"] == "cdp_unavailable"


def test_ocr_reports_tesseract_nonzero(cfg, monkeypatch) -> None:
    _stub_shot(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(1, "", "leptonica error"))
    out = core.cdp_ocr_runtime(cfg, runner=runner)
    assert out["state"] == "failed" and "leptonica" in out["error"]
