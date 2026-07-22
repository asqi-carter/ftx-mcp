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


# ---- read_text (core.cdp_read_text_runtime — region-clipped OCR, S4 feature 2) --

def test_read_text_returns_recognized_text(cfg, monkeypatch) -> None:
    _stub_shot(monkeypatch, region=[10.0, 20.0, 30.0, 40.0])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "SP-101"))
    out = core.cdp_read_text_runtime(cfg, region=[0.1, 0.1, 0.2, 0.2], runner=runner)
    assert out["state"] == "succeeded"
    assert out["text"] == "SP-101"
    assert out["region"] == [10.0, 20.0, 30.0, 40.0]
    cmd = runner.calls[0][0]
    assert cmd[0] == "/usr/bin/tesseract" and "stdout" in cmd and "--psm" in cmd


def test_read_text_forwards_region_to_screenshot(cfg, monkeypatch) -> None:
    seen = {}

    def fake_shot(cfg_, save_path=None, navigate_url=None, settle_seconds=None,
                  region=None, **kw):
        seen["region"] = region
        return {"state": "succeeded", "size_bytes": 1, "navigated": False,
                "region": region}

    monkeypatch.setattr(core, "cdp_screenshot_runtime", fake_shot)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: FakeProc(0, "x"))
    core.cdp_read_text_runtime(cfg, region=[0.0, 0.0, 0.5, 0.5], runner=runner)
    assert seen["region"] == [0.0, 0.0, 0.5, 0.5]


def test_read_text_missing_binary_is_soft_failure(cfg, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    # screenshot must NOT even be attempted when tesseract is absent
    monkeypatch.setattr(core, "cdp_screenshot_runtime",
                        lambda *a, **k: pytest.fail("should not capture"))
    out = core.cdp_read_text_runtime(cfg)
    assert out["state"] == "failed"
    assert out["error"] == "tesseract_not_installed"
    assert "PATH" in out["hint"]


def test_read_text_propagates_screenshot_failure(cfg, monkeypatch) -> None:
    _stub_shot(monkeypatch, state="failed", error="bad_region", region=None)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    runner = make_fake_runner(lambda cmd, kw: pytest.fail("tesseract should not run"))
    out = core.cdp_read_text_runtime(cfg, runner=runner)
    assert out["state"] == "failed" and out["error"] == "bad_region"
    assert out["region"] is None


# ---- find_text tesseract TSV behaviors (S4 feature 3) --------------------

# A realistic tesseract `--psm 6 tsv` fixture:
#  - "Start Button" on line 1, two adjacent words (word_num 1, 2) -> joins
#  - "Foo Button" on line 2 -> a second, unrelated "Button" that must NOT
#    join with line 1's "Start" (different line_num)
#  - "Exit Now Confirm" on line 3, where "Now" is low-confidence (< 40) and
#    must be dropped, which breaks the Exit/Confirm adjacency too
_TSV_FIXTURE = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "1\t1\t0\t0\t0\t0\t0\t0\t1000\t800\t-1\t\n"
    "4\t1\t1\t1\t1\t0\t10\t10\t300\t30\t-1\t\n"
    "5\t1\t1\t1\t1\t1\t10\t10\t80\t30\t95.5\tStart\n"
    "5\t1\t1\t1\t1\t2\t95\t10\t100\t30\t92.0\tButton\n"
    "4\t1\t1\t1\t2\t0\t10\t50\t300\t30\t-1\t\n"
    "5\t1\t1\t1\t2\t1\t10\t50\t60\t30\t88.0\tFoo\n"
    "5\t1\t1\t1\t2\t2\t75\t50\t100\t30\t90.0\tButton\n"
    "4\t1\t1\t1\t3\t0\t10\t90\t300\t30\t-1\t\n"
    "5\t1\t1\t1\t3\t1\t10\t90\t60\t30\t91.0\tExit\n"
    "5\t1\t1\t1\t3\t2\t75\t90\t70\t30\t15.0\tNow\n"
    "5\t1\t1\t1\t3\t3\t150\t90\t90\t30\t93.0\tConfirm\n"
)


def test_parse_tsv_keeps_only_word_level_rows() -> None:
    words = core._parse_tesseract_tsv(_TSV_FIXTURE)
    # 7 word-level (level==5) rows; the level 1/4 aggregate rows are dropped
    assert len(words) == 7
    assert all(w["text"] for w in words)
    assert {w["text"] for w in words} == {
        "Start", "Button", "Foo", "Exit", "Now", "Confirm"}


def test_match_multiword_joins_adjacent_words_same_line() -> None:
    words = core._parse_tesseract_tsv(_TSV_FIXTURE)
    matches = core._match_tsv_words(words, "Start Button")
    assert len(matches) == 1
    assert matches[0]["text"] == "Start Button"
    assert matches[0]["bbox_px"] == [10.0, 10.0, 185.0, 30.0]
    assert matches[0]["confidence"] == 92.0  # min() of the two joined words


def test_match_is_case_insensitive_and_finds_all_occurrences() -> None:
    words = core._parse_tesseract_tsv(_TSV_FIXTURE)
    matches = core._match_tsv_words(words, "button")
    assert len(matches) == 2  # line 1's "Button" and line 2's "Button"


def test_match_does_not_join_words_across_lines() -> None:
    words = core._parse_tesseract_tsv(_TSV_FIXTURE)
    # "Button" (line 1, word 2) followed by "Foo" (line 2, word 1) are
    # adjacent in reading order but on different lines -> must not join
    assert core._match_tsv_words(words, "Button Foo") == []


def test_match_skips_low_confidence_word_breaking_multiword_join() -> None:
    words = core._parse_tesseract_tsv(_TSV_FIXTURE)
    # "Now" has conf 15 (< 40) -> filtered out entirely, which breaks the
    # word_num adjacency between Exit (1) and Confirm (3)
    assert core._match_tsv_words(words, "Exit Now Confirm") == []
    assert core._match_tsv_words(words, "Now") == []  # low-conf word never matches on its own
    assert len(core._match_tsv_words(words, "Exit")) == 1  # neighboring high-conf words still match


def test_match_no_match_returns_empty_list() -> None:
    words = core._parse_tesseract_tsv(_TSV_FIXTURE)
    assert core._match_tsv_words(words, "Nonexistent Label") == []


def test_find_text_missing_binary_is_soft_failure(cfg, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    out = core.cdp_find_text_runtime(cfg, "Start")
    assert out["state"] == "failed"
    assert out["error"] == "tesseract_not_installed"
    assert out["found"] is False and out["matches"] == []
    assert "PATH" in out["hint"]
