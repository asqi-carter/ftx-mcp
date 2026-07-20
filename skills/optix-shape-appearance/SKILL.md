---
name: optix-shape-appearance
description: Style a Rectangle/shape directly (fill, border, corner radius, opacity) via the live bridge — for one-off panels, backgrounds, and status indicators. Use for "add a colored box", "rounded panel", "background rectangle", "status indicator shape".
user_invocable: true
---

# Direct shape styling (Rectangle)

For one-off backgrounds, panels, and indicators that don't warrant a reusable
style object, set the appearance properties directly. Studio open, bridge armed.

1. **Create:** `optix_bridge_create_widget(project, screen="UI/Screens/<S>", name="Card", widget_type="Rectangle")`
2. **Style** via `optix_bridge_set_property` (Color props take `#RRGGBB` /
   `#AARRGGBB` / uint — the bridge coerces hex → UInt32 ARGB):
   - `FillColor` → `"#ffffff"`
   - `BorderColor` → `"#b3b3b3"`, `BorderThickness` → `"1"`
   - `CornerRadius` → `"8"`
   - `Opacity` → `"0.9"` (0–1)
   - `Width`/`Height`, and `HorizontalAlignment`/`VerticalAlignment` for placement
     (see `optix-anchor-fill`).

## Panel background (the trap)

A **`Panel` has no fill/border** — it's a pure layout container. To give a panel a
background, add a **Rectangle child** sized to fill it (`…Alignment=Stretch`),
placed **behind** the other children. Render order = child order and the bridge
appends (last = on top):
- **Fresh panel:** add the Rectangle first, then the content.
- **Already-populated panel:** add the Rectangle, then send it behind:
  `optix_bridge_reorder(project, "UI/Screens/<S>/<Panel>/<Rect>", position="back")`.
  (Reorder only bites on graphic objects inside a TYPE — the normal screen case;
  reload the runtime page to see it.)

## Status indicator (color reacts to state)

- **Simple 1:1** — bind `FillColor` to a color source variable:
  `optix_bridge_bind_property(project, "<rect>", "FillColor", "Model/StatusColor", mode="Read")`.
  Blinking: `bind_property("<rect>", "Blink", "Model/AlarmActive", mode="Read")`.
- **Conditional** (fault=red / ok=green from a Boolean or value) — use
  `optix_bridge_attach_expression` on `FillColor`:
  `expression="if({0}, 0xFFFF0000, 0xFF00FF00)", sources="Model/Fault"`. See the
  `optix-expression-converter` skill. (Verify at runtime — converters no-op silently.)

## Notes
- `optix_describe_type("Rectangle")` lists the settable props (FillColor,
  BorderColor, BorderThickness, CornerRadius, Blink, Opacity, …) — consult it
  rather than guessing; the bridge rejects unknowns with the valid list.
