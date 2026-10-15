---
name: optix-add-label
description: Add a text label to an Optix screen via the live design-time bridge (Studio open). Use when the user says "add a label", "put text on the screen", "add a caption/title".
user_invocable: true
---

# Add a label to an Optix screen (bridge-native)

Studio **open**, bridge armed. A bare label is a single call; a styled or
conditionally-visible label is several related edits — batch those.

1. **Bare label, one-shot add:**
   `optix_bridge_add_label(project, screen="UI/Screens/<Screen>", name="<UniqueName>", text="<the text>", left=<x>, top=<y>)`
   — creates the Label and sets Text (+ LeftMargin/TopMargin) in one call.
   `name` must be unique under that screen; pick something descriptive
   (`HeaderTitleLabel`). This IS already the batched shape (the wrapper
   collapses create_widget + up to 3 set_property into one round trip) — no
   need to reach for `optix_bridge_edit` for a bare label.

2. **Label + style + conditional visibility → one `optix_bridge_edit` batch.**
   `add_label` is a convenience wrapper, not a batch op verb, so decompose it
   to `create_widget` + `set_property` when combining with other ops in one
   call:
   ```
   optix_bridge_edit(project, ops=[
     {"op": "create_widget", "screen": "UI/Screens/<Screen>", "name": "HeaderTitleLabel", "widget_type": "Label"},
     {"op": "set_property", "path": "UI/Screens/<Screen>/HeaderTitleLabel", "name": "Text", "value": "<the text>"},
     {"op": "set_property", "path": "UI/Screens/<Screen>/HeaderTitleLabel", "name": "LeftMargin", "value": "<x>"},
     {"op": "set_property", "path": "UI/Screens/<Screen>/HeaderTitleLabel", "name": "TopMargin", "value": "<y>"},
     {"op": "set_property", "path": "UI/Screens/<Screen>/HeaderTitleLabel", "name": "TextColor", "value": "#1F3A93"},
     {"op": "bind", "path": "UI/Screens/<Screen>/HeaderTitleLabel", "name": "Visible",
      "source_path": "Model/<BoolVar>", "mode": "Read"},
   ])
   ```
   One round trip, validated as a whole, instead of add_label + 2 set_property
   + 1 bind as four separate calls. Call it directly — it validates before it
   applies, so no dry_run pre-flight is needed. Create
   the bound variable first if it doesn't exist yet
   (`{"op": "create_variable", ...}` — fold it into the same batch, ordered
   before the `bind`).
   - `TextColor` takes a Color: `"#1F3A93"` / `"#AARRGGBB"` (opaque; the
     bridge coerces hex → UInt32 ARGB).
   - font size, alignment, etc. — call `optix_describe_type("Label")` first to
     see the exact settable property names rather than guessing.

3. **Runtime (only if the user wants it live):**
   `optix_emulator(action="restart", project=project)` →
   `optix_observe(mode="screenshot", project=project, save_path="<session dir>/label.jpg")`.
   Adding several labels/components in one build? Don't restart per label —
   author them all first and do ONE restart + screenshot at the end (see
   `optix-verify-loop`).

## Show/hide on a condition

Bind the label's `Visible` to a Boolean model variable (op verb `bind`, field
`source_path` — see the batch above). Single edit? Call `optix_bridge_edit`
with a one-op list:
`optix_bridge_edit(project, ops=[{"op": "bind", "path": "UI/Screens/<Screen>/<Name>", "name": "Visible", "source_path": "Model/<BoolVar>", "mode": "Read"}])`.
Create the variable first (`{"op": "create_variable", ...}`)
— see [`optix-bound-toggle`](../optix-bound-toggle/SKILL.md).

## Notes

- **Bridge = Studio OPEN.** Opposite of the old file-edit path; the bridge
  authors the in-memory model directly (no `studio_open` 409 guard).
- **Describe before you guess.** `optix_describe_node` / `optix_describe_type`
  give the authoritative settable-property list — the bridge rejects an unknown
  property with `unknown_property` + the valid set rather than silently failing.
- Studio closed? Live authoring needs Studio open with the bridge armed —
  ask the user to open the project.

## Translated text (translation keys)

A label that must follow the session locale takes a translation KEY, not literal
text (`set_property` writes literal text).

1. Add the rows in one call (primitives, gated by `FTXMCP_BRIDGE_PRIMITIVES`):
   `optix_bridge_add_translations(project, rows=[{"key": "Start", "value": "Start", "locale": "en-US"}, {"key": "Start", "value": "Marche", "locale": "fr-FR"}])`
   — or fold `{"op": "add_translations", "rows": [...]}` into an `optix_bridge_edit` batch.
   Removing a row is **not supported** (the Optix SDK has no API for it):
   `optix_bridge_remove_translation` and the batch verb `remove_translation` answer
   `not_supported` (`translation_not_found` for an unknown key; both routes use the same
   existence check, so a key just added by a primitive or batch `add_translations` answers
   `not_supported`, never `translation_not_found`). To change a row,
   overwrite its text with `add_translations`, or edit the key in Studio's Translations editor.
   Repeating a key is safe: `add_translations` updates an existing key in place and reports it
   under `updated` (new keys under `added`) instead of failing with `row_failed` / "already contains a translation".
2. Point the label at the key:
   `{"op": "set_text_key", "path": "UI/Screens/<Screen>/<Name>", "name": "Text", "key": "Start"}`
   (tool `optix_bridge_set_text_key`). The key need not exist yet, but a key with no
   row renders blank — the result's `translated` says whether a row exists.
   `BrowseName`/`DisplayName`/`Description` are never writable this way.
3. A model variable that must hold translated text is retyped in place:
   `{"op": "retype", "path": "Model/<Var>", "datatype": "LocalizedText"}`
   (`optix_bridge_retype`; value carries over as text; a value with no text form is
   `value_not_convertible`).

Every one of these takes `dry_run` and reports structured per-op errors
(`invalid_text_key`, `invalid_locale`, `text_key_missing`, `not_localized_text`,
`invalid_translation_rows`, ...) before anything is written.
