# ftx-mcp v1.0.3 — draft release notes

Theme: token economy + install UX. The efficiency tools implement a
field-validated workflow (thanks to Isaac's field reports from production
use); the install fixes close out the 2026-07-22 field session.

## Install / lifecycle UX

- **Setup no longer prompts about auth.** The interactive y/N question led
  fresh installers to enable auth by accident and lock themselves out of the
  UI. Auth stays OFF silently (loopback-only default); LAN users opt in
  explicitly with `setup.ps1 -EnableAuth` (persists the choice + issues a
  bootstrap token). `-NoAuthPrompt` is now a deprecated no-op.
- **Scheduled tasks survive the week:** restart-on-failure on both tasks and
  the Task Scheduler's silent 72-hour execution limit removed — the service
  and the CDP chrome no longer die quietly mid-week.
- README: new Auth and Uninstall notes in the install section.
- **Setup installs the OCR/visual deps**: Tesseract via winget (step 3.5,
  `-NoOcr` to skip, warn-and-continue if winget can't) and Pillow via the
  venv `[visual]` extra - `optix_doctor` still reports both.

## Token-economy tools

- `optix_cdp_screenshot`: `return_image=true` returns the capture as typed
  MCP image content (model sees it same-turn, no file round-trip) — opt-in;
  the default file-path flow is unchanged. Responses now include a `hint`
  telling the model exactly how to access the file.
- `region=[x,y,w,h]` cropping on `optix_cdp_screenshot` (CDP-native clip;
  values <=1.0 are viewport fractions, >1 are pixels).
- `optix_cdp_read_text(region?)` — OCR a widget or the frame: the
  zero-vision-token "does it say X" check.
- `optix_cdp_find_text(text)` — word boxes + clickable centers; feeds
  `optix_cdp_click` and route building.
- `optix_cdp_navigate(route, routes_path)` — replay banked routes (JSON,
  normalized coords) with optional OCR `expect_text` verification that
  fails loud instead of drifting blind.
- `optix_cdp_sweep` / `optix_cdp_diff` — walk a route map in one session
  into per-screen captures + OCR text manifests, then diff two sweeps:
  pixel gate (Pillow, `pip install ftx-mcp[visual]`) + text-level deltas;
  degrades to text-only without Pillow.
- `optix_routes_save` / `get` / `list` — the service owns routes files
  end-to-end: schema-validated before write, atomic, traversal-guarded.
  Sandboxed clients (Cowork) never need host folder access to bank routes.
- `optix_doctor` now reports tesseract and Pillow with install hints.
- New skill `optix-visual-regression` documents the bank -> baseline ->
  compare -> text-first-diff loop.

## Robustness

- **Shell-out tools no longer stall the server** (@PlantwideIntegration).
  FastMCP runs sync tools directly on the shared event loop; a slow
  subprocess (a 15s process scan) could hang every connected client and
  drop the MCP transport. Slow tools are now offloaded to worker threads,
  with a lock-in test; the v1.0.3 CDP/OCR tools are covered.
- `run_emulator` no longer reports a false failure while the runtime is
  still identifying itself (@PlantwideIntegration).
- CDP recovery clears a wedged Chrome and its profile lock before relaunch,
  scoped strictly to ftx-mcp's own Chrome (@PlantwideIntegration).
- Tesseract output is decoded as UTF-8 on all platforms (fixes a Windows
  cp1252 reader-thread crash on multi-byte OCR output).
- Setup no longer auto-starts the service - one explicit
  `services.ps1 start` brings up both tasks together.

## Skills

- `optix-blind-authoring` — bank UI knowledge once, author blind, verify
  with cheap text reads; at most one screenshot per change.
- `optix-known-pitfalls` — four field-diagnosed failure modes, so the next
  session doesn't re-pay the debugging cost.

## Build

- License metadata modernized (SPDX + license-files); builds are
  deprecation-warning-free on current setuptools.
