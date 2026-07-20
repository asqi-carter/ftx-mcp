---
name: optix-anchor-fill
description: Make a widget fill/stretch/anchor within its container in Optix — there is NO dock-panel widget; docking is Alignment=Stretch + margins. Use for "make it fill the panel", "dock this to the top", "anchor to the edges", "responsive layout".
user_invocable: true
---

# Fill / anchor a widget (Optix has no "docking")

**Key fact:** FT Optix has **no dock-panel concept.** "Docking"/"fill parent"/
"anchor to edges" is expressed as `HorizontalAlignment` / `VerticalAlignment` =
`Stretch` plus margins. Don't hunt for a DockPanel — it doesn't exist.

## Fill the whole container
```
optix_bridge_set_property(project, "<widget>", "HorizontalAlignment", "Stretch")
optix_bridge_set_property(project, "<widget>", "VerticalAlignment",   "Stretch")
```
(Alignment props are enums — pass the friendly name; the bridge coerces.)

## Dock to an edge
Stretch on the cross axis, align on the main axis, and use margins to inset:
- **Top bar:** `HorizontalAlignment=Stretch`, `VerticalAlignment=Top`, set `Height`.
- **Left rail:** `VerticalAlignment=Stretch`, `HorizontalAlignment=Left`, set `Width`.
- Inset from the edge with `LeftMargin`/`TopMargin`/`RightMargin`/`BottomMargin`.

## Auto-arranging containers (instead of manual margins)
For rows/columns/grids that lay children out automatically, create a layout
container and drop children into it:
`optix_bridge_create_widget(project, screen="UI/Screens/<S>", name="Row1", widget_type="RowLayout")`
(also `ColumnLayout`, `GridLayout`). **Verify the type live first** with
`optix_describe_type("RowLayout")` — these aren't in the create tool's example
list, so confirm the exact type name and its child-arrangement props before
scripting a batch.

## Notes
- A background behind a container's children is a **Rectangle child** (Panels have
  no fill) — see the panel-background pattern; render order = child order, and the
  bridge appends (last = on top) so add the background first on a fresh panel.
- `optix_describe_type` before guessing alignment/margin property names.
