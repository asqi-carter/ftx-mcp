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

## Setup (the part that's tedious by hand)

1. `optix_bridge_create_widget(screen=<parent>, name="Scroll", widget_type="ScrollView")`
2. Create the layout container INSIDE it (vertical for a top-to-bottom list).
3. On the ScrollView: `HorizontalAlignment="Stretch"`, `VerticalAlignment="Stretch"`
   — fill the parent component.
4. On the layout container: `HorizontalAlignment="Stretch"` (use the full
   width), `VerticalAlignment="Top"`, and **`Height="-1"`** — -1 means
   size-to-content, so the container grows with its children and the
   ScrollView's scrollbar appears exactly when content overflows.
5. Add children to the LAYOUT (not the ScrollView) — they stack in order;
   `optix_bridge_reorder` changes stacking position.

## Migrating existing content

Widgets already sitting on the screen? `optix_bridge_move_node(
node_path=<existing container or widget>, new_parent=<the layout's path>)`
per widget. Read the response: outbound bindings are re-created, but the
moved node's NodeId changes — anything elsewhere that bound INTO it needs
rebinding (the response's note + broken_links tell you).

## Verify

Restart the emulator (structural change), screenshot, then toggle a child's
Visible=false and re-screenshot — the siblings should close the gap. That
reflow is the whole point; if children overlap or don't move, the layout
container type is wrong (a plain Panel doesn't arrange).
