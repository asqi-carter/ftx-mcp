---
name: optix-add-label
description: Add a text label to an Optix screen via the live design-time bridge (Studio open). Use when the user says "add a label", "put text on the screen", "add a caption/title".
user_invocable: true
---

# Add a label to an Optix screen (bridge-native)

Studio **open**, bridge armed. One call authors the label directly in the live
model — no YAML, no deploy needed to see it in the designer.

1. **One-shot add:**
   `optix_bridge_add_label(project, screen="UI/Screens/<Screen>", name="<UniqueName>", text="<the text>", left=<x>, top=<y>)`
   — creates the Label and sets Text (+ LeftMargin/TopMargin) in one call.
   `name` must be unique under that screen; pick something descriptive
   (`HeaderTitleLabel`).

   Longhand if you need more control:
   `optix_bridge_create_widget(project, screen="UI/Screens/<Screen>", name="<Name>", widget_type="Label")`
   then `optix_bridge_set_property(...,"Text","<the text>")`,
   `...,"LeftMargin","<x>"`, `...,"TopMargin","<y>"`.

2. **Style it** (optional) via `optix_bridge_set_property`:
   - `TextColor` → a Color: `"#1F3A93"` / `"#AARRGGBB"` (opaque; the bridge
     coerces hex → UInt32 ARGB).
   - font size, alignment, etc. — call `optix_describe_type("Label")` first to
     see the exact settable property names rather than guessing.

3. **Runtime (only if the user wants it live):**
   `optix_restart_emulator(project)` →
   `optix_cdp_screenshot(project, save_path="<session dir>/label.jpg")`.

## Show/hide on a condition

Bind the label's `Visible` to a Boolean model variable:
`optix_bridge_bind_property(project, "UI/Screens/<Screen>/<Name>", "Visible", "Model/<BoolVar>", mode="Read")`.
Create the variable first (`optix_bridge_create_variable`) — see
[`optix-bound-toggle`](../optix-bound-toggle/SKILL.md).

## Notes

- **Bridge = Studio OPEN.** Opposite of the old file-edit path; the bridge
  authors the in-memory model directly (no `studio_open` 409 guard).
- **Describe before you guess.** `optix_describe_node` / `optix_describe_type`
  give the authoritative settable-property list — the bridge rejects an unknown
  property with `unknown_property` + the valid set rather than silently failing.
- Studio closed? Live authoring needs Studio open with the bridge armed —
  ask the user to open the project.
