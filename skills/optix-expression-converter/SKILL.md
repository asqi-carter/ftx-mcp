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
optix_bridge_attach_expression(project,
  node_path="UI/Screens/<S>/<Widget>", prop_name="FillColor",
  expression="if({0} > 40, 0xFFFF0000, 0xFF00FF00)",
  sources="Model/Speed")
```
- `{0}`,`{1}`,… placeholders bind **in order** to the comma-separated `sources`
  (model/node paths). `{#name}` named placeholders also work.
- Colors are `0xAARRGGBB` (opaque = `0xFF……`). Booleans lowercase `true`/`false`.

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

## Verify — converters no-op SILENTLY
The bridge also does **not** validate the formula syntax at author-time (Optix
does, at runtime — a malformed expression silently no-ops). So `{ok:true}` means
"attached", not "correct". A mis-wired converter renders **nothing/transparent with no error** — the classic
Optix trap. So `{ok:true}` from the tool is NOT proof. **Always runtime-verify**:
`optix_restart_emulator` → screenshot, and confirm
the property actually reacts (e.g. toggle the source and re-shoot).

## Notes
- `sources` must be resolvable **variable** paths (model vars, other props). Create a
  model variable first with `optix_bridge_create_variable` if needed.
- Reading an existing converter works via `optix_describe_node` on the property
  (its `ExpressionEvaluator` child shows the Expression + SourceN).
- For a straight 1:1 bind (no formula), use `optix_bridge_bind_property` instead.
