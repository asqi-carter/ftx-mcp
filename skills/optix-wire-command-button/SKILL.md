---
name: optix-wire-command-button
description: Add a Button (or wire an existing widget) to set or toggle a variable on click via the live bridge — the native-command action, no NetLogic. Use for "button that turns X on/off", "start/stop button", "toggle from a button".
user_invocable: true
---

# Command button (native SetVariable / ToggleVariable)

The most common authoring action: a Button whose click drives a variable, with
**no C#** — Optix's builtin `VariableCommands`. Studio open, bridge armed.

## One batch call (preferred)

Button creation, its Text, and the click wiring are 3 related ops on one
node — one `optix_bridge_edit` call instead of 3 sequential ones. The op
verb for wiring is `wire_event`, with fields `path`/`event_type` plus either
`command`+`variable`(+`value`) or `method_path`:
```
optix_bridge_edit(project, ops=[
  {"op": "create_widget", "screen": "UI/Screens/<Screen>", "name": "StartBtn", "widget_type": "Button"},
  {"op": "set_property", "path": "UI/Screens/<Screen>/StartBtn", "name": "Text", "value": "Start"},
  {"op": "wire_event", "path": "UI/Screens/<Screen>/StartBtn", "event_type": "MouseClickEvent",
   "command": "ToggleVariable", "variable": "Model/PowerOn"},
])
```
If `Model/PowerOn` doesn't exist yet, fold a `create_variable` op in before
the `wire_event` op — same batch, validated together (`bridge_edit` checks
the whole list against a model that accumulates the batch's own creates, so
"create the variable, then wire to it" validates clean even though the
variable isn't live until the batch applies).

1. **Create the button** (or reuse any clickable widget): `create_widget` +
   `set_property`(Text) — shown above.
2. **Wire the click** — pick the command:
   - **Toggle** a Boolean: `command="ToggleVariable"`, `variable="Model/PowerOn"`.
   - **Set** to a value: `command="SetVariable"`, `variable="Model/Mode"`, `value="2"`.
   (`variable` is a resolvable node path — `Model/<var>`; create it first with
   `create_variable` if needed.)

Just wiring one existing button, nothing to create? `optix_bridge_edit` with
a one-op `wire_event` list handles that fine — no need for a bigger batch.

## Custom logic instead of a native command

To run a NetLogic `[ExportMethod]` on click, pass `method_path` instead of
`command` (same `wire_event` op field):
`optix_bridge_edit(project, ops=[{"op": "wire_event", "path": ".../StartBtn", "event_type": "MouseClickEvent", "method_path": "NetLogic/MyLogic/DoThing"}])`.
(The ExportMethod is authored as an EventHandler.)

## Notes

- **Re-wiring an existing handler** — if the event node already has a handler
  for the event type, `wire_event` updates it **in place** (args and
  ObjectPointer are replaced and read back). `via` in the result reports the
  outcome as a suffix on the route token: `+existing` (updated in place),
  `+replaced` (delete-then-create), or none (created) — e.g.
  `"eventhandler+args+lateobj+existing"`. `changed: false`
  means the existing handler already matched exactly and no write was issued.
  When the bridge cannot safely identify the existing handler it returns
  `handler_exists` with the conflicting handler's details; pass `replace=true`
  to force delete-then-create instead.
- **One event, multiple actions is NOT yet supported** — chaining several
  actions in sequence (e.g. ChangeUser→Close) needs the future multi-command
  tool. For a single Set/Toggle this is complete.
- **Describe first** if unsure of the event name: `optix_describe_type("Button")`
  lists events/props; `MouseClickEvent` is the click.
- Verify at runtime: `optix_emulator(action="restart")` then
  `optix_interact(action="click", ...)`/`optix_observe(mode="screenshot", ...)` (trusted CDP events reach the Optix
  hit-tester).
- Wiring several buttons/components in one build? Don't restart per button —
  batch the wiring with the rest of the screen's edits and do ONE restart +
  verify pass at the end (see `optix-verify-loop`).
