---
name: optix-anchored-edit
description: Edit raw Optix project YAML files for operations the bridge cannot perform — the optix_find → ranged optix_read_file → targeted file-edit workflow. Use when optix_set_property returns structural_edit_unsupported, when a child-node property needs editing, or when a converter/binding node must be hand-authored in YAML.
user_invocable: true
---

# Anchored file edit — editing project YAML directly

The bridge (`optix_bridge_edit`) cannot reach every node. When it returns
`structural_edit_unsupported` or the node you need to change is a child-node
property (an expanded variable, a converter attachment, a DynamicLink attribute),
edit the raw project YAML directly using the three-step anchored-edit pattern.

**Studio must be CLOSED** for any direct file edit. `optix_find` and
`optix_read_file` both refuse with `studio_open` (409) while Studio holds the
project in memory — disk state would be stale. Close Studio, apply the edit,
reopen. No override exists.

---

## The pattern: find → ranged read → targeted edit

### Step 1 — locate the region with `optix_find`

```python
optix_find(project, query="Name: MyWidget", glob="Nodes/**/*.yaml")
```

`optix_find` returns file paths, line numbers, and surrounding context.
Use the line number to anchor the ranged read. Searches are case-insensitive
by default and literal only (no regex). If you already know which file holds
the node, skip to step 2.

### Step 2 — read the slice with `optix_read_file`

```python
optix_read_file(project, path="Nodes/UI/Screens/MyScreen.yaml",
                start_line=42, end_line=80)
```

The result includes a `sha256` field — the version fingerprint of the **whole
file** at the time of the read. Cite this fingerprint in the commit note or
task log so a reviewer can confirm the edit was applied to the correct version.
`start_line`/`end_line` are 1-based inclusive; `end_line` clamps to EOF.

### Step 3 — apply the targeted edit

Use a standard file-editing tool (the IDE's Edit tool, a script, or direct
file write) to modify **only the lines in the identified region**. Read back
with another `optix_read_file` call to confirm the result before reopening
Studio.

---

## When anchored edits are appropriate

| Situation | Anchored edit? |
|-----------|---------------|
| `optix_set_property` returns `structural_edit_unsupported` | Yes |
| Child-node property (DynamicLink, expanded variable, binding node) | Yes |
| The bridge doesn't know the node's type (`unverifiable_property`) | Yes |
| Converter-type annotation that `attach_expression` cannot reach | Yes |
| Any write while Studio is open | **No** — close Studio first |
| A property the bridge CAN set | No — use `optix_bridge_edit` |

---

## Promote an existing instance to a type (convert_to_type)

If the goal is to promote an existing widget assembly into a reusable type,
use the bridge composite rather than hand-editing YAML:

```python
optix_bridge_convert_to_type(
    node_path="UI/Screens/ScreenA/PumpPanel",
    type_name="PumpCard",
    types_folder="UI/Templates",
)
```

This is a dedicated composite operation (Studio's right-click "Convert to Type")
and handles the subtree re-author, link audit, and rollback internally. Check
`skipped` and `broken_links` in the result — converter attachments and exotic
binding nodes may be listed as skipped and need re-attachment via
`optix_bridge_edit` with `attach_expression` ops after the conversion.

---

## Add a bound widget (the bridge path, preferred)

Adding a bound control — a widget plus a binding to a model variable — is a
bridge operation, not a direct file edit:

```python
optix_bridge_add_bound_widget(
    project=project,
    screen="UI/Screens/MyScreen",
    name="SpeedGauge",
    widget_type="ProgressBar",
    source_path="Model/Speed",
    prop="Value",
    left=100, top=50,
)
```

Use `optix_bridge_edit` with `create_widget` + `bind` ops when you need to
combine the widget creation with other operations in a single validated batch.
Direct YAML edits for this case are unnecessary and bypass the bridge's
validation.

---

## Verify

After applying a file edit and reopening Studio:

```python
# Confirm the node reads as expected:
optix_describe_node(project, path="UI/Screens/MyScreen/MyWidget")

# Restart the emulator to load the edited model:
optix_emulator(action="restart", project=project)

# Check the runtime log for errors since 'Starting project':
optix_emulator(action="log", project=project)

# Screenshot to confirm the screen rendered:
optix_observe(mode="screenshot", project=project,
              save_path="<session dir>/verify.jpg")
```

A structural file edit is invisible to `optix_build_check` (it only compiles
C#) — the emulator log is the true verification channel.

---

## Notes

- `optix_read_file` is the only way to read raw YAML content; `optix_describe_node`
  reads the live model (which matches disk only when Studio is closed).
- Never hand-edit `FTOptixProject.optix` (the project manifest) — Studio
  regenerates it on save and will overwrite any change.
- For NetLogic `.cs` source, use `optix_read_netlogic` (works while Studio is
  open, since `.cs` files are not held in Studio's model).
