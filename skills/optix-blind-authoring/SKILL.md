---
name: optix-blind-authoring
description: Cut vision-token cost by banking UI knowledge once and authoring blind — cache navigation routes and screen structure, batch every multi-op edit through optix_bridge_edit instead of one tool call per property, verify with describe_node (text) instead of pixels, spend at most one screenshot per change. Use on any project you will touch more than once.
user_invocable: true
---

# Author blind, verify cheap

Two independent cost drivers, two independent fixes. Screenshots dominate
*vision*-token cost (~1-2k tokens each); bank knowledge once and work blind
against it (below). Round-trips dominate *text*-token cost — each bridge
tool call is a full model turn that re-sends the accumulating conversation,
so a widget built one property at a time grows roughly QUADRATICALLY in
call count. An 8-segment tank indicator authored one op per call runs to
~60 tool calls (8× create_widget, then per-segment 5× set_property + 1×
attach_expression). Batched by COMPONENT — one `optix_bridge_edit` per
segment (~7 ops) — that collapses to ~8 calls, each quick to generate and
apply. Do NOT swing the other way and cram all 60 ops into one call:
generating a huge op list is slow (output tokens), it applies as one long
blocking call, and the whole indicator rides on a single partial-failure
point. Component-sized batches are the sweet spot (see SIZE below).

## Batch-first is the default authoring doctrine

If an edit touches more than one property, one binding, or one widget —
**do not fan out into N sequential `optix_bridge_*` calls.** Compose the
whole thing as an ops list and send it through `optix_bridge_edit` in one
call:

```
optix_bridge_edit(project=<p>, ops=[
  {"op": "create_widget", "screen": "UI/MainWindow", "name": "Gauge1", "widget_type": "Rectangle"},
  {"op": "set_property",  "path": "UI/MainWindow/Gauge1", "name": "Width", "value": "120"},
  {"op": "set_property",  "path": "UI/MainWindow/Gauge1", "name": "FillColor", "value": "#FF00FF00"},
  {"op": "bind",           "path": "UI/MainWindow/Gauge1", "name": "Visible", "source_path": "Model/Running", "mode": "Read"},
])
```

Why this wins beyond just "fewer calls": `bridge_edit` validates the WHOLE
batch first against a hypothetical model that accumulates the batch's own
creates and follows its moves and renames — "create Gauge1 then set
Gauge1.Width" validates clean even though Gauge1 doesn't exist yet, and a
"move Gauge1 to Panel/Gauge1 then set Panel/Gauge1.Width" validates clean
without splitting the batch — and only applies if that report is clean. A
`create_widget` routed into a placeholder collection reports its actual
landed path in the `routed_into_collection` warning (`routed_path`); use
it for subsequent `set_property`/`bind`/`reorder` ops in the same batch.
One round trip, one validation pass, no ordering constraint between creates
and moves, instead of discovering a constraint three calls deep into a
fan-out.

## Batch SIZE: by component, not by screen

Batch ONE component's related ops — a widget plus its properties plus its
binding/expression (typically ~5-20 ops). Do NOT batch a whole screen into
one giant call. Beyond a component's worth of ops the costs invert: the model
spends real time *generating* the giant JSON (output tokens are the slow,
expensive ones), it applies as one multi-minute blocking call with no
progress, and a single failed op strands the entire screen at `state=partial`.
A dozen ~10-op component batches beat one 120-op batch on both token cost and
wall-clock, and each is independently debuggable. If a batch does exceed the
chunk threshold it returns `state="chunked"` instead of dying — recover with
`optix_bridge_edit(action="status", project=...)` to see what landed, then
`action="continue"` with the returned `batch_id` to apply the remainder.

## Call it once — do NOT dry_run first

`optix_bridge_edit` validates the whole batch BEFORE writing anything and
applies nothing if validation fails (`state="validated"` with the errors). So
just call it once and let it apply — a separate `dry_run=True` pass buys no
safety and regenerates the entire op list a second time, which on a large
batch is the single most wasteful thing you can do. Reserve `dry_run=True`
for a genuinely risky batch you want to eyeball before committing; property
names you're unsure of are better checked with `optix_describe_type` than a
throwaway dry_run.

**Valid op verbs** (the ONLY ones `bridge_edit` accepts): `set_property`,
`bind`, `create_widget`, `create_variable`, `create_folder`, `create_object`,
`create_type`, `create_alias`, `delete`, `move`, `reorder`, `wire_event`,
`attach_expression`, `add_translation`. Convenience wrappers like
`optix_bridge_add_label` are NOT op verbs — decompose them to their
underlying primitives (`add_label` → `create_widget` + `set_property`×1-3)
when putting them in a batch. See each per-noun skill (`optix-add-label`,
`optix-bound-toggle`, etc.) for the exact op shape.

**One edit, and only one?** Call `optix_bridge_edit` with a one-op list — the
batch tool handles a single op fine, there's no separate "do one thing" tool
by default (the per-noun primitives are gated behind
`FTXMCP_BRIDGE_PRIMITIVES=1`; see `docs/tool-reference.md`). Batching pays
off from 2 related ops up to a component's worth (~5-20); past that, split
into another component batch rather than growing one call unbounded.

## The per-project UI cache

Bank the cache with `optix_routes(action="save")` (project, routes payload) -- the
service writes `dev/ftx_ui_map.json` itself; read it back with
`optix_routes(action="get")`. NEVER ask for host folder access or write the file with
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
   If nothing visual changed by design, spend zero. "A change" means the
   whole batch of structural edits for the build, not each component in it —
   restart the emulator ONCE at the end and spend the one screenshot then,
   not per widget (see `optix-verify-loop`).
5. **Bank anything newly discovered** back via `optix_routes(action="save")` before
   moving on — extra top-level keys (structure maps, notes) are preserved
   alongside `routes`, so one file carries the whole cache. The next
   session starts ahead.

## Capture discipline (when you do shoot)

- Let the Chrome window SETTLE before any baseline capture — take a warm-up
  shot first and discard it.
- Comparison captures must use the same chrome-cdp window configuration as
  their baseline; a size mismatch reads as a diff.
