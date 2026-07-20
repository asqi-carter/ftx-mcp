---
name: optix-apply-style
description: Restyle a widget by pointing its Style property at a named StyleSheet variant (Accent, Emergency, Transparent, ...) via the live bridge — the canonical FT Optix theming mechanism, instead of hand-setting colors. Use for "make this button look like an emergency/accent button", "apply the X style".
user_invocable: true
---

# Apply a named style variant

Every Optix project ships a `UI/StyleSheets/DefaultStyleSheet` with per-family
variants (`ButtonStyles`, `SwitchStyles`, `GaugeStyles`, `InputBoxStyles`,
`NavigationPanelStyles`, …), each with stock variants like `Default`, `Accent`,
`Emergency`, `Transparent`, `BorderedRectangular`, `BorderedCircular`. Restyle a
widget by pointing its `Style` (a **NodeId** prop) at a variant node — don't
hand-set colors per widget.

1. **Find the variant** — browse the stylesheet:
   `optix_describe_node("UI/StyleSheets/DefaultStyleSheet/ButtonStyles")` to list
   the available variants for that widget family.

2. **Confirm the property** — `optix_describe_node("UI/Screens/<S>/<Widget>")`;
   the settable NodeId prop is usually `Style` (some families expose
   `ButtonStyle`/`SwitchStyle`/`GaugeStyle`).

3. **Point it** (NodeId props resolve by path):
   `optix_bridge_set_property(project, "UI/Screens/<S>/<Widget>", "Style", "UI/StyleSheets/DefaultStyleSheet/ButtonStyles/Emergency")`

## Reskin the whole app in one call

Point a PresentationEngine's style-sheet property at a different StyleSheet node
(e.g. an imported Rockwell **ISA Style Sheet**):
`optix_bridge_set_property(project, "UI/WebPresentationEngine", "<StyleSheet prop>", "UI/ISAStylesheet")`
(`optix_describe_node` the engine for the exact prop name). This assumes the
target StyleSheet already exists in the project — importing a library StyleSheet
asset is a Studio action, not a bridge one.

## Notes

- **One-off styling without a variant** → set `FillColor`/`BorderColor`/
  `TextColor`/`CornerRadius` directly on the widget (see `optix-shape-appearance`).
  Prefer a named variant when the look should be reused/consistent.
- Authoring a **brand-new** style variant needs a `create_object`-style tool for
  `ButtonStyle`/etc. (not yet in the bridge — roadmap tool C). This skill applies
  existing variants only.
