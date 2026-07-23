---
name: optix-visual-regression
description: Catch unintended UI changes across a whole project for near-zero vision tokens - bank routes once, sweep a baseline, sweep again after changes, diff to text. Only open an image when the diff says CHANGED and the text delta is ambiguous.
user_invocable: true
---

# Visual regression on a token budget

The loop: bank routes -> baseline sweep -> (make changes) -> compare sweep ->
diff -> read TEXT deltas. Images are only opened as a last resort.

## 1. Bank routes (once per project)

Build a routes file with `optix_routes_save` (one route per screen you
care about) -- the service owns the file; never ask for host folder access. Use `optix_cdp_find_text` on visible labels to get
clickable centers, then store NORMALIZED coords (0..1) so routes survive
window-size changes. Give important steps an `expect_text` so a broken route
fails loudly instead of capturing the wrong screen.

## 2. Baseline sweep

`optix_cdp_sweep(routes_path, out_dir="dev/baseline")` walks every route in
ONE session, captures each screen server-side, and OCRs them into
`manifest.json`. Building a baseline costs ~zero vision tokens - do NOT
read the images; the manifest text is the record.

Capture discipline (matters for diff quality):
- keep `warmup=true` (discards a settle frame per screen),
- baseline and comparison sweeps MUST use the same chrome-cdp window
  configuration - a resized window reads as a full-screen diff.

## 3. Compare + diff

After changes: `optix_cdp_sweep(routes_path, out_dir="dev/compare")`, then
`optix_cdp_diff("dev/baseline", "dev/compare")`.

Read the result as text, `text_changed` FIRST:
- `text_changed: true` screens: `text_added` / `text_removed` IS the answer
  for label, value, and state edits ("PUMP CONTROL" removed, "PUMP CONTROL
  v2" added) - even when pixel status says `same` (a one-label edit moves
  <1% of pixels, under the 2% default threshold). Expect benign churn from
  live process values.
- `changed` (pixel) screens with no text delta: a layout/color/graphic
  edit - screenshot it (`region` + `return_image`).
- `size_mismatch`: the window config drifted between sweeps - redo the
  comparison sweep with the same window, don't interpret it.

## Dependencies

Pixel diff needs Pillow (`pip install ftx-mcp[visual]`); without it, diff
degrades to text-only mode (still useful when OCR text exists). OCR
manifests need tesseract - without it sweep still captures images but the
cheap text loop is unavailable. `optix_doctor` reports both.
