---
name: optix-web-click-surface
description: Make any region of an Optix web screen clickable — the Button-only rule, the Opacity=0 overlay recipe, and how to diagnose a silent inert click. Use for "invisible click area", "clickable overlay", "transparent button", "click region over an image", "nothing happens when I click".
user_invocable: true
---

# Web click surface (Button-only rule)

In the Optix **web presentation** only a `Button` widget fires `MouseClickEvent`.
Label, Rectangle, and Image are visually complete but permanently inert on the
web — they accept `wire_event` handlers without error and the handler saves
cleanly, but **the click never fires and there is no runtime log entry**.
A ScreenType ROOT does not fire `MouseClickEvent` either, and neither does a
Rectangle at any z-order or alpha — measured six ways.

Measured 2026-09-04 on `PopupApp2` via CDP web presentation: identical
`wire_event … ChangePanel` handlers at the same path depth — Button navigated
(3 of 3 cases: page-level and inside a template instance); Label → nothing;
Rectangle → nothing; Image (inherited, both type-level and instance-level) →
nothing. No runtime error in any inert case.

> **Note:** the native runtime is more permissive (some non-Button types respond
> to click events there). This skill covers the **web** path only — do not use a
> screenshot from a native run as evidence that the web path works.

## The recipe: Opacity=0 Button overlay

An `Opacity=0` Button is **invisible AND fires** — that is the web click surface.

### Full-screen root surface

```
optix_bridge_edit(project, ops=[
  # --- content goes here first ---

  # Then the overlay — created LAST so reorder sends it to the front:
  {"op": "create_widget", "screen": "UI/Screens/<Screen>", "name": "ClickSurface",
   "widget_type": "Button"},
  {"op": "set_property",  "path": "UI/Screens/<Screen>/ClickSurface",
   "name": "Opacity",     "value": "0"},
  {"op": "set_property",  "path": "UI/Screens/<Screen>/ClickSurface",
   "name": "HorizontalAlignment", "value": "Stretch"},
  {"op": "set_property",  "path": "UI/Screens/<Screen>/ClickSurface",
   "name": "VerticalAlignment",   "value": "Stretch"},
  {"op": "wire_event",    "path": "UI/Screens/<Screen>/ClickSurface",
   "event_type": "MouseClickEvent",
   "command": "ChangePanel", "variable": "UI/Screens/TargetScreen"},
  {"op": "reorder",       "path": "UI/Screens/<Screen>/ClickSurface",
   "position": "front"},
])
```

Key points:
- **Create AFTER the content, reorder to the front.** Child order is render
  order; the overlay must be on top or it cannot intercept clicks.
- **Stretch fills the parent.** `HorizontalAlignment=Stretch` +
  `VerticalAlignment=Stretch` fills the parent screen root. For a fixed-size
  overlay, set `Width`/`Height` explicitly and omit the Alignment sets.
- **Opacity=0** (integer 0–100, NOT 0.0–1.0). `Opacity=0` is invisible.
  `Opacity=0.9` is nearly invisible to the eye but evaluates as ~0 after
  integer truncation — use `0` explicitly.

### Inside a flow layout

Flow layouts (VerticalLayout, HorizontalLayout) do not support absolute
z-ordering between unrelated siblings. Wrap the widget AND the overlay together
in a coordinate `Panel`:

```
optix_bridge_edit(project, ops=[
  # Coordinate Panel wrapper
  {"op": "create_widget", "screen": "UI/Screens/<Screen>", "name": "CardWrapper",
   "widget_type": "Panel"},
  {"op": "set_property",  "path": "UI/Screens/<Screen>/CardWrapper",
   "name": "Width",  "value": "200"},
  {"op": "set_property",  "path": "UI/Screens/<Screen>/CardWrapper",
   "name": "Height", "value": "120"},

  # The content widget (e.g. an Image) — created first, therefore behind
  {"op": "create_widget", "screen": "UI/Screens/<Screen>/CardWrapper",
   "name": "CardImage", "widget_type": "Image"},
  {"op": "set_property",  "path": "UI/Screens/<Screen>/CardWrapper/CardImage",
   "name": "HorizontalAlignment", "value": "Stretch"},
  {"op": "set_property",  "path": "UI/Screens/<Screen>/CardWrapper/CardImage",
   "name": "VerticalAlignment",   "value": "Stretch"},

  # Overlay Button — created AFTER the content
  {"op": "create_widget", "screen": "UI/Screens/<Screen>/CardWrapper",
   "name": "ClickSurface", "widget_type": "Button"},
  {"op": "set_property",  "path": "UI/Screens/<Screen>/CardWrapper/ClickSurface",
   "name": "Opacity", "value": "0"},
  {"op": "set_property",  "path": "UI/Screens/<Screen>/CardWrapper/ClickSurface",
   "name": "HorizontalAlignment", "value": "Stretch"},
  {"op": "set_property",  "path": "UI/Screens/<Screen>/CardWrapper/ClickSurface",
   "name": "VerticalAlignment",   "value": "Stretch"},
  {"op": "wire_event",    "path": "UI/Screens/<Screen>/CardWrapper/ClickSurface",
   "event_type": "MouseClickEvent",
   "command": "ChangePanel", "variable": "UI/Screens/TargetScreen"},
  {"op": "reorder",       "path": "UI/Screens/<Screen>/CardWrapper/ClickSurface",
   "position": "front"},
])
```

The `Panel` gives a coordinate-layout context inside the flow cell, so
`reorder` can push the overlay in front of the image without escaping the
flow container.

## Diagnostic rule

**Do not diagnose an inert click as a depth or AliasNode problem before
checking the widget TYPE.**

`ChangePanel`'s `ObjectPointer` has a depth rule too:
off-by-one depth is a real failure mode, but it is **silent in a different
way** — the handler wires correctly and the Button fires, the ChangePanel
just navigates nowhere. An inert click from a Label, Rectangle, or Image
never fires at all; the handler depth is irrelevant. Check widget type first,
depth second.

If you replaced a non-Button widget and the click still does nothing after a
restart: then check depth.

## Verify

After every structural change, restart the emulator once:
`optix_emulator(action="restart")`.

Then exercise the click surface directly:

1. **Send the click:** `optix_interact(action="click", path="UI/Screens/<Screen>/ClickSurface")`
   (trusted CDP events reach the Optix hit-tester).
2. **Read the effect:** `optix_describe_node(project, path="<target variable or screen node>")`
   — confirm the navigation or state change happened — OR
   `optix_observe(mode="screenshot")` to confirm the screen changed visually.
3. **Check the runtime log:** `optix_emulator(action="log")` — filter lines
   since the last `Starting project`.

**An inert click leaves no trace.** A widget that does not fire `MouseClickEvent`
on the web produces no log entry, no error, and no change. **Absence of error
is not evidence the click fired.** Only a confirmed state change or navigation
in step 2 proves the surface works. See `optix-verify-loop` for the full
verify cadence — navigation, popup, click, and keyboard changes all require
a real click in the verify step because these four classes fail silently.

## Enum wire names (applies when setting enum properties on Button)

FT Optix has two name sets for the same enum and only one lands on the
bridge. **15 of the 22 enums probed differ** between `Module.xml` and what
the bridge accepts (usually a dropped prefix/suffix, but member counts
differ too, so it is not a rule to derive). **Numeric values are accepted
by every enum and both sources agree on them** — emit the number as the safe
default. Harvest the real member set with a deliberately invalid `set_property`:
the bridge answers `bad_value` with the valid member list and writes nothing.
See `docs/optix-patterns/enum-wire-names.md` for the full 22-enum table.
