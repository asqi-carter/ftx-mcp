"""Tests for cdp_diff_runtime (S6): comparing two optix_cdp_sweep capture
directories screen-by-screen. Pure file comparison — no CDP session, no
fake WebSocket needed.

Pixel-path tests are skipped when Pillow isn't installed in this venv (the
`visual` optional dependency group); the degraded no-Pillow text-only path
is exercised unconditionally by monkeypatching service.core._load_pil so it
never depends on what's actually installed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from service import core

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def _write_manifest(d: Path, screens: dict, ocr: bool = False, viewport: dict | None = None) -> None:
    d.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": 1, "created_at": "2026-07-22T00:00:00Z",
        "viewport": viewport or {"w": 100, "h": 100},
        "ocr": ocr, "screens": screens,
    }
    (d / "manifest.json").write_text(json.dumps(manifest))


def _make_image(path: Path, size=(8, 8), color=(128, 128, 128)) -> None:
    """Write a solid-color image at `path`. Saved as PNG regardless of the
    .jpg extension in the filename — cdp_diff_runtime opens by content
    (PIL sniffs the format), and PNG avoids JPEG's lossy DCT rounding so
    pixel-diff percentages in these tests are exact, not approximate."""
    Image.new("RGB", size, color).save(path, format="PNG")


# ---- manifest_not_found -------------------------------------------------

def test_diff_manifest_not_found_dir_a(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _write_manifest(dir_b, {})
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    assert out["state"] == "failed" and out["error"] == "manifest_not_found"
    assert out["dir"] == str(dir_a)


def test_diff_manifest_not_found_dir_b(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _write_manifest(dir_a, {})
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    assert out["state"] == "failed" and out["error"] == "manifest_not_found"
    assert out["dir"] == str(dir_b)


def test_diff_manifest_invalid_json(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    (dir_a / "manifest.json").write_text("{not valid json")
    _write_manifest(dir_b, {})
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    assert out["state"] == "failed" and out["error"] == "manifest_invalid"


# ---- added / removed screens (union, not errors) ------------------------

def test_diff_added_and_removed_screens(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(core, "_load_pil", lambda: None)
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _write_manifest(dir_a, {
        "home": {"file": "home.jpg", "size_bytes": 10, "text": ["Hi"]},
        "old_screen": {"file": "old_screen.jpg", "size_bytes": 10, "text": ["Bye"]},
    }, ocr=True)
    _write_manifest(dir_b, {
        "home": {"file": "home.jpg", "size_bytes": 10, "text": ["Hi"]},
        "new_screen": {"file": "new_screen.jpg", "size_bytes": 10, "text": ["New"]},
    }, ocr=True)
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    assert out["state"] == "succeeded"
    assert out["added"] == ["new_screen"]
    assert out["removed"] == ["old_screen"]
    assert out["screens"]["home"]["status"] == "same"
    assert "new_screen" not in out["screens"]
    assert "old_screen" not in out["screens"]


# ---- degraded no-Pillow text-only mode -----------------------------------

def test_diff_no_pillow_no_ocr_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(core, "_load_pil", lambda: None)
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _write_manifest(dir_a, {"home": {"file": "home.jpg", "size_bytes": 10}}, ocr=False)
    _write_manifest(dir_b, {"home": {"file": "home.jpg", "size_bytes": 10}}, ocr=False)
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    assert out["state"] == "failed" and out["error"] == "no_pillow_no_ocr"
    assert "hint" in out


def test_diff_no_pillow_only_one_side_ocr_still_fails(tmp_path: Path, monkeypatch) -> None:
    """Both manifests must carry OCR text, not just one."""
    monkeypatch.setattr(core, "_load_pil", lambda: None)
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _write_manifest(dir_a, {"home": {"file": "home.jpg", "size_bytes": 10, "text": ["Hi"]}}, ocr=True)
    _write_manifest(dir_b, {"home": {"file": "home.jpg", "size_bytes": 10}}, ocr=False)
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    assert out["state"] == "failed" and out["error"] == "no_pillow_no_ocr"


def test_diff_degraded_text_only_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(core, "_load_pil", lambda: None)
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _write_manifest(dir_a, {
        "home": {"file": "home.jpg", "size_bytes": 10, "text": ["Hello", "World"]},
        "setup": {"file": "setup.jpg", "size_bytes": 10, "text": ["Setup Values"]},
    }, ocr=True)
    _write_manifest(dir_b, {
        "home": {"file": "home.jpg", "size_bytes": 10, "text": ["Hello", "World"]},
        "setup": {"file": "setup.jpg", "size_bytes": 10, "text": ["Setup Values", "New Line"]},
    }, ocr=True)
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    assert out["state"] == "succeeded"
    assert out["degraded"] == "no_pillow"
    assert out["screens"]["home"]["status"] == "same"
    assert out["screens"]["home"]["pixel_pct"] is None
    assert out["screens"]["home"]["text_added"] == []
    assert out["screens"]["home"]["text_changed"] is False
    assert out["screens"]["setup"]["status"] == "changed"
    assert out["screens"]["setup"]["text_added"] == ["New Line"]
    assert out["screens"]["setup"]["text_removed"] == []
    assert out["screens"]["setup"]["text_changed"] is True
    assert out["summary"] == {"same": 1, "changed": 1, "size_mismatch": 0,
                              "errors": 0, "text_changed": 1}


def test_diff_screen_with_sweep_error_reports_error_status(tmp_path: Path, monkeypatch) -> None:
    """A screen that failed to capture during the sweep (recorded as
    {"error": ...} in the manifest) degrades to a per-screen error, not a
    crash, and counts toward summary.errors."""
    monkeypatch.setattr(core, "_load_pil", lambda: None)
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _write_manifest(dir_a, {"home": {"error": "click x/y must be >= 0"}}, ocr=True)
    _write_manifest(dir_b, {"home": {"file": "home.jpg", "size_bytes": 1, "text": ["Hi"]}}, ocr=True)
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    assert out["state"] == "succeeded"
    assert out["screens"]["home"]["status"] == "error"
    assert out["summary"]["errors"] == 1


# ---- text explainer: 40-line cap -----------------------------------------

def test_diff_text_explainer_caps_at_40_lines(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(core, "_load_pil", lambda: None)
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    text_a = ["same line"]
    text_b = ["same line"] + [f"new line {i}" for i in range(50)]
    _write_manifest(dir_a, {"home": {"file": "home.jpg", "size_bytes": 10, "text": text_a}}, ocr=True)
    _write_manifest(dir_b, {"home": {"file": "home.jpg", "size_bytes": 10, "text": text_b}}, ocr=True)
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    added = out["screens"]["home"]["text_added"]
    assert len(added) == 41  # 40 lines + sentinel
    assert added[-1] == "+10 more"
    assert added[:40] == text_b[1:41]


def test_diff_state_and_threshold_echoed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(core, "_load_pil", lambda: None)
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _write_manifest(dir_a, {"home": {"file": "home.jpg", "size_bytes": 1, "text": ["Hi"]}}, ocr=True)
    _write_manifest(dir_b, {"home": {"file": "home.jpg", "size_bytes": 1, "text": ["Hi"]}}, ocr=True)
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b), threshold=5.0)
    assert out["state"] == "succeeded"
    assert out["threshold"] == 5.0
    assert "degraded" in out


# ---- Pillow pixel-gate path (skipped if Pillow isn't installed) ---------

@pytest.mark.skipif(not HAS_PIL, reason="Pillow not installed in this venv")
def test_diff_pixel_same_below_threshold(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    _make_image(dir_a / "home.jpg", color=(128, 128, 128))
    _make_image(dir_b / "home.jpg", color=(128, 128, 128))
    _write_manifest(dir_a, {"home": {"file": "home.jpg", "size_bytes": 1}})
    _write_manifest(dir_b, {"home": {"file": "home.jpg", "size_bytes": 1}})
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    assert out["state"] == "succeeded"
    assert "degraded" not in out
    assert out["screens"]["home"]["status"] == "same"
    assert out["screens"]["home"]["pixel_pct"] == 0.0
    assert out["screens"]["home"]["text_changed"] is False
    assert out["summary"] == {"same": 1, "changed": 0, "size_mismatch": 0,
                              "errors": 0, "text_changed": 0}


@pytest.mark.skipif(not HAS_PIL, reason="Pillow not installed in this venv")
def test_diff_pixel_changed_above_threshold_with_text_explainer(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    _make_image(dir_a / "home.jpg", color=(0, 0, 0))
    _make_image(dir_b / "home.jpg", color=(255, 255, 255))
    _write_manifest(dir_a, {"home": {"file": "home.jpg", "size_bytes": 1, "text": ["A"]}})
    _write_manifest(dir_b, {"home": {"file": "home.jpg", "size_bytes": 1, "text": ["B"]}})
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b), threshold=2.0)
    assert out["screens"]["home"]["status"] == "changed"
    assert out["screens"]["home"]["pixel_pct"] == 100.0
    assert out["screens"]["home"]["text_added"] == ["B"]
    assert out["screens"]["home"]["text_removed"] == ["A"]


@pytest.mark.skipif(not HAS_PIL, reason="Pillow not installed in this venv")
def test_diff_pixel_threshold_boundary_exact_equal_is_same(tmp_path: Path) -> None:
    # base=100, +51 -> uniform per-pixel diff of 51 -> pct = 51*100/255 = 20.0 exactly
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    _make_image(dir_a / "home.jpg", color=(100, 100, 100))
    _make_image(dir_b / "home.jpg", color=(151, 151, 151))
    _write_manifest(dir_a, {"home": {"file": "home.jpg", "size_bytes": 1}})
    _write_manifest(dir_b, {"home": {"file": "home.jpg", "size_bytes": 1}})
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b), threshold=20.0)
    assert out["screens"]["home"]["pixel_pct"] == 20.0
    assert out["screens"]["home"]["status"] == "same"  # changed requires STRICTLY > threshold


@pytest.mark.skipif(not HAS_PIL, reason="Pillow not installed in this venv")
def test_diff_pixel_threshold_boundary_just_over_is_changed(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    _make_image(dir_a / "home.jpg", color=(100, 100, 100))
    _make_image(dir_b / "home.jpg", color=(152, 152, 152))  # diff=52 -> pct ~20.39
    _write_manifest(dir_a, {"home": {"file": "home.jpg", "size_bytes": 1}})
    _write_manifest(dir_b, {"home": {"file": "home.jpg", "size_bytes": 1}})
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b), threshold=20.0)
    assert out["screens"]["home"]["status"] == "changed"


@pytest.mark.skipif(not HAS_PIL, reason="Pillow not installed in this venv")
def test_diff_pixel_size_mismatch_skips_compare(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    _make_image(dir_a / "home.jpg", size=(8, 8))
    _make_image(dir_b / "home.jpg", size=(16, 16))
    _write_manifest(dir_a, {"home": {"file": "home.jpg", "size_bytes": 1}})
    _write_manifest(dir_b, {"home": {"file": "home.jpg", "size_bytes": 1}})
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    assert out["screens"]["home"]["status"] == "size_mismatch"
    assert "pixel_pct" not in out["screens"]["home"]
    assert out["summary"]["size_mismatch"] == 1


@pytest.mark.skipif(not HAS_PIL, reason="Pillow not installed in this venv")
def test_diff_pixel_missing_file_degrades_to_error(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    # dir_a's home.jpg is never written -> Image.open raises
    _make_image(dir_b / "home.jpg")
    _write_manifest(dir_a, {"home": {"file": "home.jpg", "size_bytes": 1}})
    _write_manifest(dir_b, {"home": {"file": "home.jpg", "size_bytes": 1}})
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    assert out["screens"]["home"]["status"] == "error"
    assert out["summary"]["errors"] == 1


@pytest.mark.skipif(not HAS_PIL, reason="Pillow not installed in this venv")
def test_diff_small_text_edit_below_pixel_threshold_still_reports_text_delta(
    tmp_path: Path,
) -> None:
    """The 2026-07-23 field case: a single-label edit moved ~0.6% of pixels
    (under the 2% default), status stayed 'same', and the text delta was
    silently swallowed. Text deltas are now computed regardless of pixel
    status, with text_changed as their own channel."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    _make_image(dir_a / "pump.jpg", color=(128, 128, 128))
    _make_image(dir_b / "pump.jpg", color=(129, 128, 128))  # tiny pixel delta
    _write_manifest(dir_a, {"pump": {"file": "pump.jpg", "size_bytes": 1,
                                     "text": ["PUMP CONTROL", "START"]}}, ocr=True)
    _write_manifest(dir_b, {"pump": {"file": "pump.jpg", "size_bytes": 1,
                                     "text": ["PUMP CONTROL v2", "START"]}}, ocr=True)
    out = core.cdp_diff_runtime(str(dir_a), str(dir_b))
    scr = out["screens"]["pump"]
    assert scr["status"] == "same" and scr["pixel_pct"] < 2.0
    assert scr["text_changed"] is True
    assert scr["text_added"] == ["PUMP CONTROL v2"]
    assert scr["text_removed"] == ["PUMP CONTROL"]
    assert out["summary"]["text_changed"] == 1
