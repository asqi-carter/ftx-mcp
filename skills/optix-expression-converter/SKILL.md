---
name: optix-expression-converter
description: Make an Optix property COMPUTED from one or more sources via an ExpressionEvaluator converter (conditional color, computed visibility, scaling, formatted text). Use for "turn X red when Y", "show only if A and B", "color reacts to a value", "scale/convert a value".
user_invocable: true
---

# Expression converter (the "dumb Excel" of Optix)

When a property needs a **formula** over one or more sources — not a 1:1 bind — attach
an `ExpressionEvaluator`. It subsumes ConditionalConverter, LinearConverter, and most
transforms with one uniform tool. Studio open, bridge armed.

```
optix_bridge_edit(project, ops=[
  {"op": "attach_expression", "path": "UI/Screens/<S>/<Widget>", "prop_name": "FillColor",
   "expression": "if({0} > 40, 0xFFFF0000, 0xFF00FF00)", "sources": "Model/Speed"},
])
```
- `{0}`,`{1}`,… placeholders bind **in order** to the comma-separated `sources`
  (model/node paths). `{#name}` named placeholders also work.
- Colors are `0xAARRGGBB` (opaque = `0xFF……`). Booleans lowercase `true`/`false`.

**A widget usually needs a converter on more than one property** (FillColor
AND Visible AND Enabled, say) — that's N `attach_expression` calls, batch
them into one `optix_bridge_edit` instead. The op verb is `attach_expression`
with fields `path`/`prop_name`/`expression`/`sources` (note: `prop_name`, not
`name`, unlike `set_property`):
```
optix_bridge_edit(project, ops=[
  {"op": "attach_expression", "path": "UI/Screens/<S>/<Widget>", "prop_name": "FillColor",
   "expression": "if({0} > 40, 0xFFFF0000, 0xFF00FF00)", "sources": "Model/Speed"},
  {"op": "attach_expression", "path": "UI/Screens/<S>/<Widget>", "prop_name": "Enabled",
   "expression": "{0} >= 100", "sources": "Model/Level"},
])
```
If the source variable doesn't exist yet, fold a `create_variable` op in
before the `attach_expression` ops that reference it — same batch, validated
together. One property, one converter? `optix_bridge_edit` handles a
one-op list fine — no need to grow it beyond what's shown above.

## Canonical recipes
- **Conditional color** (fault red / ok green): `FillColor` ←
  `if({0}, 0xFFFF0000, 0xFF00FF00)`, sources `Model/Alarm`.
- **Computed visibility**: `Visible` ← `{0} && {1}`, sources `Model/Running,Model/Enabled`.
- **Threshold enable**: `Enabled` ← `{0} >= 100`, sources `Model/Level`.
- **Scale/convert** (replaces LinearConverter): `Value` ← `{0} * 0.1 + 32`, source `Model/Raw`.
- **Composed text**: a String prop ← `left_of({0}, "-")`, etc.

## The function set (all 15)
`max min avg abs trunc ceil floor round sqrt sign like isempty` **`if(cond,a,b)`**
`left_of right_of`. Operators: arithmetic, `<< >>`, relational, `== !=`, `& ^ |`,
`&& ||`, unary `- ~ (cast)`. Full reference: `docs/expression-evaluator-reference.md`.
Beyond these needs a custom C# converter (out of bridge scope).
- `round`/`abs`/`ceil`/`floor`/`trunc`/`sqrt`/`sign`/`isempty` take **ONE** arg —
  `round({0})`, NOT `round({0}, 1)` (a 2nd arg fails). `if` takes 3, `like`/`left_of`/
  `right_of` take 2.

## Numbers + text — you CANNOT concatenate them in an expression
`+` is **numeric-only**. `round({0}*10) + " L"` (or any `<number> + "text"`) **silently
no-ops at runtime, even fully parenthesized** — FTOptix's ExpressionEvaluator has no
number→string coercion. `optix_bridge_edit` now REJECTS this at author-time
(`ExpressionEvaluator '+' is numeric-only ... use a StringFormatter`). To show a value
WITH a unit/label:
- **Formatted dynamic link — DISPUTED, do not rely on it for display text.**
  This skill used to recommend `attach_formatter` on a Label's `Text`
  (`"format": "{#val} {#eu}"`) as a native single-widget value+unit label.
  Measured 2026-09-01: a StringFormatter on `Label.Text` with a numeric source
  produced NO text at all — the label kept its static value, with no log line.
  A formatted dynamic link appears to format a **NodePath** that the link then
  resolves to a node (every verified working example — field-project nav loaders,
  `../NavPanel{#idx}@NodeId` — builds a path), not display text.
  Tested with a single `{#v}` token, not the literal-plus-token form above, so
  the exact boundary is unconfirmed; treat the value+unit claim as unproven and
  render-verify before using it. See `optix-formatted-dynamic-link`.
- **Two widgets (the old workaround):** the numeric value via `attach_expression` on one
  Label plus a **separate static Label** for the unit. Still fine, but no longer the only
  option — this skill previously said the single-widget form was not bridge-authorable,
  which was wrong.
- Either way, do NOT try to build it via `attach_expression` — that verb is hard-wired to
  ExpressionEvaluator and its `{#name}` placeholders yield positional `Source0`.
The `left_of`/`right_of` string funcs compose text from a **string** source (e.g. split a
string tag), not from a computed number.

## Cannot return a NodeId — hard limit
`ERROR;160007;Result data type 'NodeId' unsupported` (measured 2026-09-01). An
ExpressionEvaluator CANNOT produce a NodeId, so it can never pick a panel/screen
directly. `if({0}, <panelA>, <panelB>)` attaches fine, reports `ok:true`, and
fails at runtime. To choose a panel from a condition, compute an **Int32 index**
and feed it to a formatted dynamic link (`../NavPanel{#idx}@NodeId`) — see
`optix-formatted-dynamic-link`. A `ConditionalConverter` IS NodeId-native, but no
bridge op attaches one.

## Verify — the runtime LOG is the real channel
The bridge does **not** validate formula syntax at author-time, so `{ok:true}`
means "attached", not "correct", and a mis-wired converter renders
nothing/transparent on screen.

But it is **not silent** — it logs, with the offending node path:
`%LOCALAPPDATA%\Rockwell Automation\FactoryTalk Optix\Emulator\Log\<Project>\FTOptixRuntime.0.log`
Nothing in the bridge or MCP surfaces this, so a screenshot alone cannot separate
"wrong value" from "converter refused to run". **Read the log before theorising
about a bad render** — this file names the cause in one line, and guessing
instead cost two wrong diagnoses in one session.

Record the line count before `optix_emulator(action="restart")`, then read only
the new lines; a real project has standing noise (offline-PLC tag reads, USB/FTP,
SQLite limits) to filter out. Then also confirm the property reacts (toggle the
source and re-shoot).

Mid a multi-component build, don't restart per converter — attach all of
them (and the rest of the screen's edits) first, then do ONE restart +
verify pass at the end (see `optix-verify-loop`).

## Two silent traps (field-verified — each cost a slow debug once)
- **An expression feeding a ResourceUri (e.g. an Image path) fails SILENTLY** —
  blank image, no log line, no error. Don't fight it: use stacked Image widgets
  with Boolean `Visible` expressions (one image per state, expressions toggle
  visibility) instead.
- **`{Session}/...` sources are rejected at design time.** Attach the expression
  with a placeholder variable as the source, then rebind `Source0` to the
  session path afterward (a `{"op": "bind", ...}` with a raw_path in the same or
  a follow-up batch).

## Notes
- `sources` must be resolvable **variable** paths (model vars, other props). Create a
  model variable first (`{"op": "create_variable", ...}`) if needed.
- Reading an existing converter works via `optix_describe_node` on the property
  (its `ExpressionEvaluator` child shows the Expression + SourceN).
- For a straight 1:1 bind (no formula), use the `bind` op instead.
