---
name: optix-wire-command-button
description: Add a Button (or wire an existing widget) to set or toggle a variable on click via the live bridge — the native-command action, no NetLogic. Use for "button that turns X on/off", "start/stop button", "toggle from a button".
user_invocable: true
---

# Command button (native SetVariable / ToggleVariable)

The most common authoring action: a Button whose click drives a variable, with
**no C#** — Optix's builtin `VariableCommands`. Studio open, bridge armed.

1. **Create the button** (or reuse any clickable widget):
   `optix_bridge_create_widget(project, screen="UI/Screens/<Screen>", name="StartBtn", widget_type="Button")`
   `optix_bridge_set_property(project, "UI/Screens/<Screen>/StartBtn", "Text", "Start")`

2. **Wire the click** — pick the command:
   - **Toggle** a Boolean: `optix_bridge_wire_event(project, "UI/Screens/<Screen>/StartBtn", "MouseClickEvent", command="ToggleVariable", variable="Model/PowerOn")`
   - **Set** to a value: `optix_bridge_wire_event(project, ".../StartBtn", "MouseClickEvent", command="SetVariable", variable="Model/Mode", value="2")`
   (`variable` is a resolvable node path — `Model/<var>`; create it first with
   `optix_bridge_create_variable` if needed.)

## Custom logic instead of a native command

To run a NetLogic `[ExportMethod]` on click, pass `method_path` instead of
`command`: `optix_bridge_wire_event(project, ".../StartBtn", "MouseClickEvent", method_path="NetLogic/MyLogic/DoThing")`.
(The ExportMethod is authored as an EventHandler.)

## Notes

- **One event, multiple actions is NOT yet supported** — a second `wire_event`
  on the same node+event appends another command handler; chaining several
  actions in sequence (e.g. ChangeUser→Close) needs the future multi-command
  tool. For a single Set/Toggle this is complete.
- **Describe first** if unsure of the event name: `optix_describe_type("Button")`
  lists events/props; `MouseClickEvent` is the click.
- Verify at runtime: `optix_restart_emulator` then
  `optix_cdp_click`/`optix_cdp_screenshot` (trusted CDP events reach the Optix
  hit-tester).
