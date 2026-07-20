---
name: optix-templify
description: Make a reusable component (template/ObjectType) and stamp out instances — plan-ahead with create_type, or promote an existing widget assembly with convert_to_type. Use when the user says "make this reusable", "template", "component library", or the same widget cluster is being built more than once.
---

# Reusable components (types & templates)

An ObjectType is Optix's template: author it once, instantiate it everywhere,
edit the type and every instance follows. Two ways to get one:

## Plan-ahead (preferred): type first, author into it, instantiate

1. Ensure a home exists: `optix_bridge_create_folder(parent="UI", name="Templates")`
   (skip if it exists — `name_exists` tells you).
2. `optix_bridge_create_type(name="PumpCard", parent="UI/Templates",
   base_type="RowLayout")` — base_type is a builtin UI type
   (optix_list_ui_types) so the type renders like its base; omit it only for
   model-side data types.
3. Author the template's content by targeting the TYPE's path with the normal
   tools — `optix_bridge_create_widget(screen="UI/Templates/PumpCard", ...)`,
   set_property, bind_property all write into a type exactly like into a
   screen. Bind to properties/aliases you expect instances to override, not to
   absolute one-off variables.
4. Instantiate: `optix_bridge_create_object(parent="UI/Screens/ScreenA",
   name="Pump1", object_type="UI/Templates/PumpCard")` — repeat per placement.
5. Verify per the optix-verify-loop skill (instances are structural changes —
   restart the emulator).

## Promote an existing assembly: convert_to_type

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
  (optix_bridge_attach_expression etc.).
- `broken_links` nonempty → those bindings no longer resolve; re-bind them on
  the type.
- `optix_save` BEFORE converting anything you can't rebuild in a minute, and
  render-verify the replacement instance after (structural change — restart
  the emulator).

## Parameterize with aliases (the reuse mechanism)

A template that hardcodes `Model/Pump1/Speed` isn't reusable. The pattern:

1. On the TYPE, add an alias slot — `optix_bridge_create_alias(
   parent_path="UI/Templates/PumpCard", name="PumpAlias",
   kind="<type name or path>")`. NO target_path — the template leaves it
   unassigned; `kind` is the type constraint (what Studio's "+ Alias" sets)
   so binding/validation knows the alias's shape.
2. Bind the template's widgets THROUGH the alias with a LITERAL path:
   `optix_bridge_bind_property(node_path="UI/Templates/PumpCard/SpeedLabel",
   name="Text", raw_path="{PumpAlias}/Speed")`. raw_path is resolved per
   instance at RUNTIME — a resolvable source_path through an alias is a
   contradiction and always fails source_not_variable.
3. Per instance, point the alias at real data:
   `optix_bridge_set_property(node_path="UI/Screens/ScreenA/Pump1Card/PumpAlias",
   name="Value", value="Model/Pump1")`.
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
