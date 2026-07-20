---
name: optix-bound-toggle
description: Add a switch that toggles a label (or any Visible-bound widget) on an Optix screen — the bound switch+label pattern, no NetLogic. Use for "add a toggle", "switch that shows/hides X", "on/off control".
user_invocable: true
---

# Bound switch + label (bridge-native)

A `Switch` whose `Checked` and a `Label` whose `Visible` both bind to one
Boolean Model variable. Flip the switch → the label shows/hides. No C#. Studio
**open**, bridge armed.

1. **Create the backing variable:**
   `optix_bridge_create_variable(project, name="PowerOn", parent="Model", datatype="Boolean")`
   → resolvable at `Model/PowerOn`.

2. **Switch, bound read/write** (so it can *write* the variable):
   `optix_bridge_create_widget(project, screen="UI/Screens/<Screen>", name="PowerSwitch", widget_type="Switch")`
   `optix_bridge_bind_property(project, "UI/Screens/<Screen>/PowerSwitch", "Checked", "Model/PowerOn", mode="ReadWrite")`

3. **Label, bound read-only** (display only):
   `optix_bridge_add_label(project, screen="UI/Screens/<Screen>", name="OnLabel", text="it's on!", left=300, top=250)`
   `optix_bridge_bind_property(project, "UI/Screens/<Screen>/OnLabel", "Visible", "Model/PowerOn", mode="Read")`

That's it — the model is live in the designer immediately.

## Alternative: a Button that toggles (no switch)

`optix_bridge_wire_event(project, "UI/Screens/<Screen>/<Button>", "MouseClickEvent", command="ToggleVariable", variable="Model/PowerOn")`
— a native command, no bind needed on the button.

## Verify at runtime

`optix_restart_emulator(project)` → `optix_cdp_screenshot(...)`;
drive it with `optix_cdp_click` (trusted CDP events reach Optix's hit-tester
where synthetic clicks no-op). Switch on → label appears; off → hides.

## Why the bridge fixes the old writability caveat

The legacy file-edit path emitted a **bare** model variable (no `AccessLevel`),
because an explicit `AccessLevel`/`Value` on a *file-added* variable hangs
Studio export on FactoryTalk-template projects — leaving the Switch's *write*
path unverified. The bridge sidesteps this: `create_variable` makes a proper
live-model variable, and the Switch writes through a **`ReadWrite` DynamicLink**
(step 2). Confirm the flip on a real runtime once per project shape, but the
write path is the sanctioned one, not a bare-variable workaround.

## Notes

- **Bridge = Studio OPEN** (no `studio_open` 409 guard — the bridge needs the
  project open).
- **Describe before you guess** — `optix_describe_type("Switch")` /
  `("Label")` list the settable properties; the bridge rejects unknowns with
  `unknown_property` + the valid set.
- Studio closed? Live authoring needs Studio open with the bridge armed —
  ask the user to open the project.
