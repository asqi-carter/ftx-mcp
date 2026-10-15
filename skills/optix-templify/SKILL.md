---
name: optix-templify
description: Make a reusable component (template/ObjectType) and stamp out instances — plan-ahead with create_type, or promote an existing widget assembly with convert_to_type. Use when the user says "make this reusable", "template", "component library", or the same widget cluster is being built more than once.
---

# Reusable components (types & templates)

An ObjectType is Optix's template: author it once, instantiate it everywhere,
edit the type and every instance follows. Two ways to get one:

## Plan-ahead (preferred): type first, author into it, instantiate

Steps 1-3 below — the folder, the type, and everything authored INTO the
type — are all `bridge_edit` op verbs (`create_folder`, `create_type`,
`create_widget`, `set_property`, `bind`), so compose them as one batch
rather than one call per step:
```
optix_bridge_edit(project, ops=[
  {"op": "create_folder", "parent": "UI", "name": "Templates"},
  {"op": "create_type", "name": "PumpCard", "parent": "UI/Templates", "base_type": "RowLayout"},

  {"op": "create_widget", "screen": "UI/Templates/PumpCard", "name": "SpeedLabel", "widget_type": "Label"},
  {"op": "set_property", "path": "UI/Templates/PumpCard/SpeedLabel", "name": "Text", "value": "0"},
])
```
(Skip the `create_folder` op if `UI/Templates` already exists —
`name_exists` on a redundant create tells you, it isn't a hard failure.)

1. Ensure a home exists: `create_folder`(parent="UI", name="Templates").
2. `create_type`(name="PumpCard", parent="UI/Templates", base_type="RowLayout")
   — base_type is a builtin UI type (optix_list_ui_types) so the type renders
   like its base; omit it only for model-side data types.
3. Author the template's content by targeting the TYPE's path with the normal
   ops — `create_widget`(screen="UI/Templates/PumpCard", ...), `set_property`,
   `bind` all write into a type exactly like into a screen; fold as many as
   the template needs into the same batch. Bind to properties/aliases you
   expect instances to override, not to absolute one-off variables.
4. Instantiate: `create_object` is an op verb — `optix_bridge_edit(project,
   ops=[{"op": "create_object", "parent": "UI/Screens/ScreenA", "name":
   "Pump1", "object_type": "UI/Templates/PumpCard"}])`. Multiple placements
   batch into the same call:
   `{"op": "create_object", "parent": "UI/Screens/ScreenA", "name": "Pump1", "object_type": "UI/Templates/PumpCard"}`,
   `{"op": "create_object", "parent": "UI/Screens/ScreenA", "name": "Pump2", "object_type": "UI/Templates/PumpCard"}`, ...
5. Verify per the optix-verify-loop skill (instances are structural changes —
   restart the emulator).

One-off folder or type, nothing else to author yet? `optix_bridge_edit` with
a one-op list handles that fine — no need for a bigger batch.

## Promote an existing assembly: convert_to_type

**Not a `bridge_edit` op verb** — `convert_to_type` is a standalone composite
call on the bridge side (it does its own multi-step re-author + rollback
internally) and is not in the batch op vocabulary, so it can't be folded into
an `optix_bridge_edit` ops list. Call it on its own.

Already built it as a one-off and want it reusable?
`optix_bridge_convert_to_type(node_path="UI/Screens/ScreenA/PumpPanel",
type_name="PumpCard", types_folder="UI/Templates")` reproduces Studio's
right-click "Convert to Type": new type subtyping the widget's own type, the
subtree RE-AUTHORED (copied) into it with values and bindings re-created,
original replaced by an instance (`replace=false` to keep the type only,
leaving the original untouched).

**Read `skipped` and the link audit in the response — do not assume.**
- `skipped` nonempty → those constructs (expression converters, exotic
  attachments) were NOT copied; re-attach them on the type
  (`optix_bridge_edit` with `attach_expression` ops etc.).
- `broken_links` nonempty → those bindings no longer resolve; re-bind them on
  the type.
- `optix_save` BEFORE converting anything you can't rebuild in a minute, and
  render-verify the replacement instance after (structural change — restart
  the emulator).

## Parameterize with aliases (the reuse mechanism)

A template that hardcodes `Model/Pump1/Speed` isn't reusable. Steps 1-2 are
both `bridge_edit` op verbs (`create_alias`, `bind`) and typically apply to
several widgets on the same type at once — one batch:
```
optix_bridge_edit(project, ops=[
  {"op": "create_alias", "parent_path": "UI/Templates/PumpCard", "name": "PumpAlias", "kind": "<type name or path>"},
  {"op": "bind", "path": "UI/Templates/PumpCard/SpeedLabel", "name": "Text", "raw_path": "{PumpAlias}/Speed"},
])
```
1. On the TYPE, add an alias slot — `create_alias`(parent_path="UI/Templates/PumpCard",
   name="PumpAlias", kind="<type name or path>"). NO target_path — the
   template leaves it unassigned; `kind` is the type constraint (what
   Studio's "+ Alias" sets) so binding/validation knows the alias's shape.
2. Bind the template's widgets THROUGH the alias with a LITERAL path — `bind`
   with `raw_path` (NOT `source_path`) set to `"{PumpAlias}/Speed"`. raw_path
   is resolved per instance at RUNTIME — a resolvable source_path through an
   alias is a contradiction and always fails source_not_variable.
3. Per instance, point the alias at real data — `set_property`, and pointing
   several instances' aliases batches into one call too:
   ```
   optix_bridge_edit(project, ops=[
     {"op": "set_property", "path": "UI/Screens/ScreenA/Pump1Card/PumpAlias", "name": "Value", "value": "Model/Pump1"},
     {"op": "set_property", "path": "UI/Screens/ScreenB/Pump2Card/PumpAlias", "name": "Value", "value": "Model/Pump2"},
   ])
   ```
   Single instance? `optix_bridge_edit` with a one-op `set_property` list is
   just as simple.
4. Render-verify — raw paths can't be validated at bind time by design, so
   the emulator is the only truth (restart it; structural change).

## Notes

- Studio auto-promotes a widget dropped at the Templates ROOT into a type;
  the bridge never does promote-by-location magic — say what you mean with
  create_type / convert_to_type.
- Model-side structured data uses the same machinery: bare
  `create_type("MotorType", parent="Model/Types")`, add variables into it,
  then `create_object(parent="Model", name="Motor1", object_type=
  "Model/Types/MotorType")`.
- A plain grouping node is `create_folder`; a plain container object (no type)
  is `create_object` without `object_type`.
- A member's ModellingRule decides whether instances of a type must carry it.
  Set it with the batch op `{"op": "set_modelling_rule", "path":
  "Model/Types/MotorType/Speed", "rule": "Mandatory"}` (`Mandatory`, `Optional`
  or `None`; anything else is `invalid_modelling_rule`, caught by `dry_run`).
  The bridge maps the name onto the SDK enum `UAManagedCore.NamingRuleType` (there is no
  `ModellingRule` type); the response returns the `previous` and new rule. Placeholder
  variants are not accepted.
  Only members under a type qualify — an instance path is `not_a_type_member`.
- A NodePointer variable needs a `Kind` (the pointed-to node's type), never unset or
  BaseObjectType. `{"op": "create_variable", "parent": "Model", "name": "Selected",
  "datatype": "NodePointer", "target": "Model/Motor1"}` derives it from the target;
  pass `kind` (builtin type name or type-node path) to override it or to make a
  target-less pointer. Neither `target` nor `kind` is `kind_required`; `dry_run`
  shows the resolved Kind under `report.resolved`.
