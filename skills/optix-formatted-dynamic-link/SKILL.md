---
name: optix-formatted-dynamic-link
description: Drive a property from an integer or key through an INDIRECT ADDRESS — a dynamic link whose path is built at runtime from named custom variables, so the candidate targets are readable named properties instead of opaque GUIDs inside a converter. Use for "tab index picks a panel", "nav panel without a KeyValueConverter", "indirect address", "formatted dynamic link", "make the panel targets visible in Studio", or a "{#value} {#eu}" unit label on a Label.
---

# Formatted dynamic link (the "indirect address" pattern)

A `PanelLoader` whose `Panel` is chosen by an integer, WITHOUT a
`ValueMapConverter` full of opaque NodeId GUIDs. The candidates become ordinary
named variables on the loader, visible and editable in Studio's property grid.

```
Loader (PanelLoader)
  Panel (NodePointer)
    DynamicLink                                       (NodePath)
      DynamicLinkFormatter (StringFormatter)          <- HasConverter
        Format = "../NavPanel{#navIdx}@NodeId"        <- HasParameter
        Source<navIdx> (BaseDataType)                 <- HasSource
          DynamicLink -> Model/NavTab                 (the driver)
  NavPanel1 (NodeId) -> UI/Screens/PageA
  NavPanel2 (NodeId) -> UI/Screens/PageB
```

`NavTab = 2` formats `../NavPanel2@NodeId`, which resolves to that variable's
NodeId. In Studio this is ONE control: type `{#token}` into a dynamic link's
path and it becomes a **"Formatted dynamic link"** with a child link per token.
The StringFormatter is how it SERIALIZES — not a thing to assemble by hand.

## Authoring it — one op

```
optix_bridge_edit(ops=[
  {"op": "create_variable", "parent": "<loader>", "name": "NavPanel1", "datatype": "NodeId"},
  {"op": "create_variable", "parent": "<loader>", "name": "NavPanel2", "datatype": "NodeId"},
])
optix_bridge_edit(ops=[
  {"op": "set_property", "path": "<loader>", "name": "NavPanel1", "value": "UI/Screens/PageA"},
  {"op": "set_property", "path": "<loader>", "name": "NavPanel2", "value": "UI/Screens/PageB"},
  {"op": "attach_formatter", "path": "<loader>", "prop_name": "Panel",
   "format": "../NavPanel{#navIdx}@NodeId",
   "sources": "navIdx=Model/NavTab", "mode": "Read"},
])
```

Two calls because a custom variable is only settable once it EXISTS — the
property guard validates against the live node, and a `create` in the same
batch has not landed yet.

## The rules that make or break it

1. **`{#name}` binds to a HasSource child called `Source<name>`.** The source
   NAME is load-bearing. This is the opposite of `attach_expression`, whose
   sources are POSITIONAL (`Source0..N`) — and `{#name}` in an *expression*
   does NOT create a named source (measured: it yields `Source0`).
2. **Index suffixes must match the driver's values.** `NavTab = 2` looks for
   `NavPanel2`. Read the existing `Pairs` keys before converting a
   `ValueMapConverter` rather than assuming 1,2,3 — a real recipe editor can map
   3→SettingsPage and 4→AlarmsPage, which is not the folder order.
3. **The path lives in the formatter's `Format`, never as the DynamicLink's own
   value.** A raw link value containing `{#…}` fails Studio's NodePath
   translation outright: `Unable to translate namespace indexes ... Syntax
   error at <n>`, where n is the closing brace.
4. **`@NodeId` reads a NodeId-typed variable's VALUE.** `@Pointer` dereferences
   an actual NodePointer. For `NavPanel<N>` variables created as `datatype:
   NodeId`, `@NodeId` is the right suffix.
5. **The `Format` path is resolved by the RUNTIME, not the bridge — so it must
   be node-relative (`../NavPanel{#idx}@NodeId`) or absolute
   (`/Objects/<Project>/Model/Folder/{#key}`).** The project-relative form every
   other bridge op accepts (`Model/Folder/{#key}`, as in `sources`) is read
   relative to the property itself, resolves to nothing, and renders BLANK with
   `ok:true`, a correct-looking `describe_node`, and **no runtime log line at
   all** (1.0.8 battle test). The bridge does not check or rewrite it yet
   (slated for 1.0.9). Copy the absolute prefix from a stock link that already
   renders — e.g. MainWindow's `Caption` shows `/Objects/<Project>@BrowseName`.

## A CONDITIONAL target — compute the index, don't branch on NodeIds

To make one slot conditional (tab 1 shows Previews or History depending on a
flag), you cannot put the condition on `NavPanel1`: an ExpressionEvaluator
**cannot return a NodeId** (`ERROR;160007;Result data type 'NodeId'
unsupported`). Give the alternative its own numbered slot and compute the index
instead — Int32 results are fine:

```
NavPanel1 = Previews          NavPanel5 = History      (the alternative)
TabIndex (Int32) <- attach_expression
    "if({0} == 1, if({1}, 1, 5), {0})"   sources: <Tab>,<Flag>
Panel <- attach_formatter "../NavPanel{#idx}@NodeId"  sources: idx=<loader>/TabIndex
```

Nested `if` works; validate first with `optix_bridge_validate_expression`. Cost:
one extra slot per conditional, and the alternative's number is arbitrary — say
so in a DisplayName. A `ConditionalConverter` would be NodeId-native and avoid
this, but no bridge op attaches one. Field-verified 2026-09-01.

**Drive the format DIRECTLY from the tab variable when no conditional is needed**
— `sources: "idx=Model/…/ScreenTab"`, no intermediate variable at all. Fewer
moving parts, and it is the form that has never failed.

**The computed-index variable is not reliable.** Measured 2026-09-01 on a real project:
one screen's `TabIndex` fed its formatter correctly, while a second screen's —
structurally IDENTICAL in the saved YAML, rebuilt from scratch, single Model
source, expression `{0}` — evaluated to 0 forever, so the loader never switched.
Swapping the format's source to the tab variable directly fixed it instantly.
The cause is UNKNOWN; do not assume the indirection works because it worked once.
So: **prove the loader switches before adding a conditional on top**, and if a
computed index misbehaves, re-point the format at the driver rather than
debugging the expression — that took an hour to learn the other way round.

Instrumenting it: a Label whose `Text` gets `attach_expression "{0} * 1000"` over
the variable renders its runtime value, which is how the 0 was caught. Place the
probe on a screen whose layout actually draws screen-root children — on the first screen
the label never rendered (the layout covers it), making the probe useless there.

## Verifying — structure is NOT proof

A mis-wired converter renders NOTHING **on screen**, so `{ok:true}` and a
correct-looking `describe_node` both mean "attached", never "correct".

It is usually NOT silent, though (the exception is rule 5's wrong-base path,
which logs nothing): the runtime logs the failure with the node path in
`%LOCALAPPDATA%\Rockwell Automation\FactoryTalk Optix\Emulator\Log\<Project>\FTOptixRuntime.0.log`.
Read it before theorising about a bad render — it is the only channel that
distinguishes "wrong value" from "converter refused to run", and neither the
bridge nor the MCP surfaces it.

* **Render-verify, and make the render DISCRIMINATING.** One loader showing the
  right page proves little. Two loaders on the same driver with INVERTED
  mappings must show DIFFERENT pages — nothing but real per-loader resolution
  produces that.
* **If it looks perfect in Studio and renders nothing, check `ModellingRule`.**
  See the trap below. This is the single most misleading failure here: every
  design-time read is correct and the runtime instance simply has no link.
* **Check the window size before blaming the link.** `UI/MainWindow` is 400x400
  in a fresh project and clips everything below it.
* The project map prints a source's link as a raw `{NodeId:ns=…;g=…}` — before
  AND after a save (re-measured on 1.0.8; an earlier note here said a save
  resolves it, which no longer holds). To identify the target, match the GUID
  against `describe_node` of the candidate variables, or compare GUIDs across
  sources — two links with the same GUID point at the same node. (Printing the
  resolved path is slated for 1.0.9.)

## Working on the C# — two traps that cost a full session

Only relevant if you are editing `AttachFormatterInline` in `StudioMCPBridge.cs`.
Both are invisible in `describe_node`, and each was diagnosed the hard way.

**1. NEVER assign `StringFormatter.Format` through its property setter from the
bridge.** It takes Studio down outright — a corrupted-state exception, which
.NET Core cannot catch, so the process vanishes with NOTHING logged and
`catch (Exception)` never runs. Write the underlying `HasParameter` child
directly instead. Step-tracing (a log line before every call) is what found
this; the crash is otherwise completely silent.

**2. `SetModellingRuleRecursive()` is MANDATORY.** Without it every node
serializes `ModellingRule: None`, and a `None` member is **not instantiated
into instances of its owning ObjectType**. On a `ScreenType` that means the link
exists at design time and is absent at runtime — the exact "looks right,
renders nothing" signature. Do not remove it as a suspect: in the documented
recipe it comes AFTER the `Format` write, so a crash at `Format` never reaches
it and it is never the culprit.

**Do not misread the YAML when diagnosing this.** A converter OBJECT
(`DynamicLinkFormatter`, `ExpressionEvaluator`) and its `Source*` children
legitimately carry NO `ModellingRule:` line even when the link works perfectly —
only parameter children like `Format` show `ModellingRule: Optional`. Absence on
the object is therefore NOT evidence of the bug. Always diff against a
known-working loader in the SAME project before blaming modelling rules; reading
one file in isolation produced a confident wrong diagnosis (2026-09-01), and the
runtime log named the real cause immediately.

Otherwise follow the `NetLogic_CheatSheet` "formatted dynamic link" recipe
(`pages/dynamic-links.md`) exactly. Substituting `SetConverter` for
`AddReference(HasConverter)`, or `SetDynamicLink(null)` for `ResetDynamicLink`,
each produced a structure that read correctly and resolved to nothing.

**No thread marshaling is needed.** Everything except the `Format` setter runs
fine on the bridge's HTTP thread. `DelayedTask(LogicObject)` does NOT help: a
design-time NetLogic's behaviour context is not running, so scheduled work never
fires.

## Converting an existing ValueMapConverter

Read the pairs first (`describe_node` each `Pairs/Pair*` for `Key` and `Value`),
create `NavPanel<Key>` per pair pointing at that `Value`, then `attach_formatter`
— which REPLACES the converter on that property, so no separate teardown.

Worth doing at N<=4 targets. Past roughly six the property grid is no more
scannable than the `Pairs` collection was and the readability argument inverts.

Leave alone: converters driven by an ExpressionEvaluator over several sources
(machine-option selection), and any shared named `ValueMapConverterType` in
`Data/Converters` — inlining those into per-loader variables forks a single
source of truth.
