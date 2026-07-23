---
name: optix-blind-authoring
description: Cut vision-token cost by banking UI knowledge once and authoring blind — cache navigation routes and screen structure, verify with describe_node (text) instead of pixels, spend at most one screenshot per change. Use on any project you will touch more than once.
user_invocable: true
---

# Author blind, verify cheap

Screenshots dominate token cost in the CDP loop (~1-2k vision tokens each);
bridge authoring calls are nearly free. So: bank knowledge once, then work
blind against the banked structure.

## The per-project UI cache

Bank the cache with `optix_routes_save` (project, routes payload) -- the
service writes `dev/ftx_ui_map.json` itself; read it back with
`optix_routes_get`. NEVER ask for host folder access or write the file with
client-side tools (the service filesystem is not reachable from sandboxed
clients). The cache holds:

- **Navigation routes** as normalized (0..1) click coordinates — portable
  across window sizes (headless and visible windows differ).
- **Screen structure maps**: container paths, row/item template types, index
  conventions (note whether 0- or 1-based), auto-fill sources — whatever you
  had to discover to author against that screen.

## Workflow discipline

1. **Check the cache first.** If the screen is banked, skip rediscovery
   entirely.
2. **Author against banked structure paths** — no screenshots to author.
3. **Verify the MODEL, not pixels:** `optix_describe_node` on what you just
   wrote is cheap text and catches most mistakes.
4. **Spend at most ONE screenshot** on final visual confirmation of a change.
   If nothing visual changed by design, spend zero.
5. **Bank anything newly discovered** back via `optix_routes_save` before
   moving on — extra top-level keys (structure maps, notes) are preserved
   alongside `routes`, so one file carries the whole cache. The next
   session starts ahead.

## Capture discipline (when you do shoot)

- Let the Chrome window SETTLE before any baseline capture — take a warm-up
  shot first and discard it.
- Comparison captures must use the same chrome-cdp window configuration as
  their baseline; a size mismatch reads as a diff.
