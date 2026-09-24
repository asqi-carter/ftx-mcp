---
name: optix-scroll-layout
description: Add a scrollable, auto-arranging region (ScrollView + layout container) the right way — stretch alignments, Height=-1 auto-size, and how to migrate existing widgets into it with move_node. Use when content overflows a screen, the user asks for a scrollbar/scrollable list, or widgets should reflow when visibility toggles.
---

# Scrollable auto-arranging regions

Layout containers arrange children automatically: a horizontal layout flows
children left-to-right, a vertical layout top-to-bottom. Put one INSIDE a
ScrollView and you get a scrollable region where children resize and reflow
on visibility changes — conditionally hide a row and the gap closes itself,
no manual repositioning.

## The structure

```
Parent (screen or component)
└── ScrollView            ← owns the scrollbar
    └── <vertical layout> ← owns the arrangement
        ├── Row1 / Button1 / ...
        └── Row2 ...
```

Confirm exact type names live before scripting — `optix_list_ui_types`,
then `optix_describe_type` on the candidates (ScrollView and the layout
containers are builtins; Studio's UI labels "Vertical/Horizontal Layout"
don't always match the type's BrowseName).

## Setup — one batch call (the part that's tedious by hand)

The ScrollView, the layout container, and both their alignment/size props
are 7 related ops on 2 nodes — one `optix_bridge_edit` call instead of 7
sequential ones:
```
optix_bridge_edit(project, ops=[
  {"op": "create_widget", "screen": "<parent>", "name": "Scroll", "widget_type": "ScrollView"},
  {"op": "create_widget", "screen": "<parent>/Scroll", "name": "List", "widget_type": "<VerticalLayout type>"},

  {"op": "set_property", "path": "<parent>/Scroll", "name": "HorizontalAlignment", "value": "Stretch"},
  {"op": "set_property", "path": "<parent>/Scroll", "name": "VerticalAlignment", "value": "Stretch"},

  {"op": "set_property", "path": "<parent>/Scroll/List", "name": "HorizontalAlignment", "value": "Stretch"},
  {"op": "set_property", "path": "<parent>/Scroll/List", "name": "VerticalAlignment", "value": "Top"},
  {"op": "set_property", "path": "<parent>/Scroll/List", "name": "Height", "value": "-1"},
])
```
1. **ScrollView** owns the scrollbar; fill the parent
   (`HorizontalAlignment`/`VerticalAlignment` = `Stretch`).
2. **Layout container** goes INSIDE it (vertical for a top-to-bottom list) —
   confirm the exact type name live first (`optix_list_ui_types`, then
   `optix_describe_type`) since Studio's UI labels don't always match the
   type's BrowseName; a wrong `widget_type` in the batch fails validation
   before anything is written.
3. On the layout container: `HorizontalAlignment="Stretch"` (use the full
   width), `VerticalAlignment="Top"`, and **`Height="-1"`** — -1 means
   size-to-content, so the container grows with its children and the
   ScrollView's scrollbar appears exactly when content overflows.
4. Add children to the LAYOUT (not the ScrollView) — they stack in order;
   `reorder` (op verb, field `path` + `position`/`index`) changes stacking
   position. `index` counts among **graphic children** only (variables and
   sub-objects are excluded from the count) and is direction-independent —
   `achieved` in the report tells you where the node landed. Only the
   ScrollView/layout skeleton needs its own dry-run — new
   children can join the same batch as more `create_widget`+`set_property`
   ops once the skeleton is confirmed.

Building just the skeleton and nothing else? Two nodes is still a two-op
`optix_bridge_edit` list — no separate per-node tool needed.

## Migrating existing content

Widgets already sitting on the screen? `move` (op verb; fields `path` +
`new_parent`) per widget — several widgets moving into the same layout is
another batch: `{"op": "move", "path": <existing widget>, "new_parent": <the
layout's path>}` repeated, one call. Read the response: outbound bindings
are re-created, but the moved node's NodeId changes — anything elsewhere
that bound INTO it needs rebinding (the response's note + broken_links tell
you). Moving a single widget? `optix_bridge_edit` with a one-op `move` list
is just as simple.

## Verify

Restart the emulator (structural change), screenshot, then toggle a child's
Visible=false and re-screenshot — the siblings should close the gap. That
reflow is the whole point; if children overlap or don't move, the layout
container type is wrong (a plain Panel doesn't arrange).
