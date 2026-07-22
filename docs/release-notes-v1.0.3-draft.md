# ftx-mcp v1.0.3 — draft release notes (in progress)

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

## Token-economy tools (in progress)

- `optix_cdp_screenshot`: `return_image=true` returns the capture as typed
  MCP image content (model sees it same-turn, no file round-trip) — opt-in;
  the default file-path flow is unchanged. Responses now include a `hint`
  telling the model exactly how to access the file.
- (planned this release) `region` cropping, `optix_cdp_read_text`,
  `optix_cdp_find_text`, `optix_cdp_navigate` + routes files,
  `optix_cdp_sweep` / `optix_cdp_diff` visual-regression loop.

## Skills

- `optix-blind-authoring` — bank UI knowledge once, author blind, verify
  with cheap text reads; at most one screenshot per change.
- `optix-known-pitfalls` — four field-diagnosed failure modes, so the next
  session doesn't re-pay the debugging cost.

## Build

- License metadata modernized (SPDX + license-files); builds are
  deprecation-warning-free on current setuptools.
