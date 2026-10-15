# Tool reference

36 tools registered by default, grouped by where they sit in the loop
(gate env vars can add more — see the notes below each table). Every tool's
docstring carries "Use when / Do NOT use when" guidance for the model, and MCP
annotations (`readOnlyHint`/`destructiveHint`) so hosts can auto-run reads
and gate writes. `project` is optional everywhere — it defaults to the
project open in Studio.

## Discovery & health

| Tool | What it does |
|---|---|
| `optix_status` (`action="health"`/`"doctor"`/`"services"`/`"version"`) | Deploy-stack status family: fast preflight config (`health`), setup-fix checklist (`doctor`), live dashboard aggregate (`services`), raw Studio binary version (`version`) — heterogeneous, pick the action that matches the question |
| `optix_list_projects` | Projects under the projects root |
| `optix_project` (`action="open"`/`"new"`) | Open or create a Studio project from MCP — Studio's CLI verbs are GUI launches, so readiness is the window's UIA identity, not process exit |

**`optix_project(action="new")` name rule.** The project name must be a valid C# identifier: letters (any script), digits, underscore, not starting with a digit, no spaces, no trailing dot, not a C# keyword, and `len(projects_root / name)` must be at most 180 characters. Otherwise the call returns `{ok: false, error: "invalid_project_name", rule, name}` and Studio is not launched (e.g. `CI r18 sp`, `18CI_r18`, `Name.`, `class` are refused; `CI_r18x` and `CI_r20_ü` are fine). `created` is reported only when `<name>.optix` is non-empty and the Studio log since launch shows no `Error creating new project` for at least 5 s after the file appears (or until the log shows the project loaded); on that line the call returns `{ok: false, error: "create_failed", detail, path, partial_dir: <bool>, reason?}` and deletes nothing (`reason` is `IDEVersion.txt missing: the project was never fully created` when the directory has no usable `IDEVersion.txt`). `action="open"` on a directory whose `<name>.optix` is missing or empty (or a directory with no `.optix`) returns `error: "project_file_missing"`; an empty `.optix` or a Studio load-error log line (`is empty`, `Error creating new project`, `Cannot open`, `failed to load`, YAML parse error) returns `project_file_invalid` at once (the latter with `detail`, `pid`, `studio_left_open: {pid}` and a hint to close the launched Studio), all without waiting out the timeout; the version signal comes from the project files BEFORE launch: `<project>/IDEVersion.txt` (the `.optix` header is the fallback) is compared with the configured Studio's version. A project newer than the Studio returns `{ok: false, error: "studio_too_old", project_version, studio_version, hint}` (the hint names `FTOPTIX_STUDIO_EXE`) without launching; a `.optix` with a missing or empty `IDEVersion.txt` (a half-created project) returns `project_file_invalid` with `detail: "IDEVersion.txt missing: the project was never fully created"` and no launch. For an older project Studio is launched and, when its Project Upgrade dialog (`#32770`) appears, `open` returns `{ok: false, error: "upgrade_required", project_version, studio_version, dialog, hint}` in one poll, without clicking (upgrading is one-way). The dialog is checked before the Home screen; a Studio log version-mismatch line is only an additional signal (it adds `detail`). The Home-screen `not_opened` reason never blames the Studio version; `detail` carries the first Studio log error line.

| `optix_list_screens` | Screen/Panel/Dialog nodes in the project |
| `optix_get_project_map` | Whole-subtree component map in one call — overview with counts, then drill by path |
| `optix_find` / `optix_read_file` | Search / read project files |
| `optix_read_netlogic` | Read a NetLogic `.cs` source by class name from `ProjectFiles/NetSolution` — works while Studio is open (the NetSolution is on disk, not in Studio's model) |
| `optix_describe_node` | Live node: children, properties, values |
| `optix_list_ui_types` / `optix_describe_type` | Widget catalog + per-type property legend (consult before setting); `type_names=[...]` batches a survey into one call. `resolved_by:"browse_name"` on an entry means the compiled NodeId constant disagreed with the loaded module and the bridge found the type by walking the type tree — the entry is usable as named. `misaligned` / `catalog_misaligned` now flag only the leftovers the walk could not reconcile: a constant NAMED differently from the node it correctly points at (`UserValueChangedEvent` -> browse name `UserValueChanged`); address those by the `browse_name` |
| `optix_schema` (`action="dump"`/`"list"`/`"diff"`) | Cache the full type-schema dump per Studio version (offline), list cached versions, diff two versions (upgrade intelligence) |
| `optix_bridge_status` / `optix_active_target` | Bridge status (one entry per armed port); which deployment target Studio's dropdown has selected |
| `optix_bridge_log_tail` | The bridge's transport diagnostics — the forensic view when a bridge drops mid-edit |
| `optix_studio_log` (`lines`, `contains`) | Tail Studio's own `FTOptixStudio.0.log` (last N lines, optional case-insensitive filter) — where a rejected `optix_execute_method` is reported. Structured errors: `invalid_lines`, `invalid_contains`, `no_studio_log` |
| `optix_list_skills` / `optix_get_skill` | 19 bundled authoring playbooks — catalog + on-demand full content (served by the server itself, version-locked to the tools) |

## Authoring (live bridge — Studio open)

Writes go into Studio's in-memory model; an undeclared property is rejected
with the valid-property list rather than crashing Studio.

`optix_bridge_edit` is the primary entry point — batch one or more ops
(`set_property`, `bind`, `create_widget`, `create_variable`, `create_folder`,
`create_object`, `create_type`, `create_alias`, `delete`, `move`, `reorder`,
`wire_event`, `attach_expression`, `attach_formatter`, `attach_string_formatter`, `retype`, `add_translation`, `add_translations`, `remove_translation`, `set_text_key`, `set_modelling_rule`, `import_tags`, `create_netlogic`, `convert_to_type`, `rename`), validated as a whole
before anything is applied. **A single edit is just a one-op list** — there
is no separate "do one thing" tool for these verbs by default (see the gate
note below the table). `DisplayName` set_property routes to a dedicated
attribute endpoint (never the crash-capable variable path); `BrowseName`
changes only via the `rename` op, which lowers to a safe `move` (the node
gets a new NodeId). The hypothetical model used for validation now follows
in-batch `move` and `rename` ops — a reference to the old path after a
relocation is a `moved_earlier_in_batch` error with the new path in
`new_path` — and predicts where a `create_widget` lands when the parent
routes into a placeholder collection (`routed_into_collection` warning;
the actual created path is in `routed_path`), so creates need not be
ordered before moves and batches need not be split to work around ordering
constraints.

`wire_event` takes method ARGUMENTS via `args` — `"Name=Value;..."` pairs,
`';'`-separated because node paths contain commas; a value that resolves to a
node becomes a NodePointer argument, anything else a String — and late-binds
the call target via `object_raw`, a NodePath relative to the event node (e.g.
`"../Loader"`). `object_raw` is **required for every UI-hosted method**:
without it the ObjectPointer names the design-time node, while the UI is
instantiated per session, so `ChangePanel` resolves, throws nothing, and
silently does not navigate. Both are method-path only. `command` also accepts
`OpenDialog`/`CloseDialog` (resolved from the builtin `UICommands` object,
which lives outside the project root and so is unreachable as a `method_path`);
those take their arguments from `args` too —
`"Dialog=<Dialog type path>;AliasNode=<node or empty>;ParentItem="`, an empty
`ParentItem` being late-bound to the event node.

**`set_text_key` (translation keys).** `{op:"set_text_key", path, name, key[, locale]}`
sets a LocalizedText property (e.g. `Label.Text`) to a translation KEY: the value is a
LocalizedText carrying a TextId, resolved through the translation table per session
locale. `set_property` writes literal text instead. The key need not exist yet — pair
it with `add_translation`; the result's `translated` says whether a row exists (a key
with no row renders blank). `BrowseName`/`DisplayName`/`Description`/`NodeId`/`NodeClass`
are never written (`node_attribute_not_settable`). `dry_run` reports structured per-op
errors: `missing_field`, `node_attribute_not_settable`, `invalid_text_key`,
`invalid_locale` (service side), `text_key_missing`, `not_localized_text`,
`unresolved_reference`, `unknown_property` (bridge side); the single tool
`optix_bridge_set_text_key` returns `{ok:false, code:"invalid_text_key", errors}`.

**`set_modelling_rule` (type members).** `{op:"set_modelling_rule", path, rule}` sets the
ModellingRule of a member of an ObjectType/VariableType: `Mandatory` (every instance
carries it), `Optional` (instances may) or `None` (plain child). `rule` is
case-insensitive. Any other value is `invalid_modelling_rule` (with `allowed`), a
missing one `missing_field`; both are reported per op under `dry_run` and returned as
`{ok:false, code, errors}` by `optix_bridge_set_modelling_rule`. The bridge adds
`node_not_found`, `unresolved_reference` and `not_a_type_member` (the node is an
instance, not under a type). The tool takes `dry_run` too and reports
`previous`/`would_set` without changing anything.

**`retype` to `LocalizedText`.** `{op:"retype", path, datatype:"LocalizedText"[, dims]}`
(tool `optix_bridge_retype`, `POST /bridge/node/retype`) changes an existing variable's
DataType in place, keeping the node, children and inbound links. A String, number,
Boolean, DateTime or LocalizedText value carries over as text (an existing LocalizedText
keeps its TextId; the locale is `en-US`). A value with no text form (NodeId, ByteString,
...) or an array that would lose elements collapsing to a scalar is refused before any
change: `value_not_convertible`, returned as `{ok:false, code, errors:[{code, message,
field}]}` by the tool and reported per op under `dry_run`. An unsupported target is
`invalid_datatype` (with `allowed`), a bad `dims` `invalid_dims`. The bridge adds
`not_a_variable`, `node_not_found` and `unresolved_reference`. The tool takes `dry_run`
and reports `from`/`to`/`would_retype` without changing anything.

**`create_variable` NodePointer (`Kind`).** `{op:"create_variable", parent, name,
datatype:"NodePointer", target:"<node path>"[, kind]}` (tool `optix_bridge_create_variable`,
`POST /bridge/model/variable`) creates a typed NodePointer whose `Kind` is the pointed-to
node's type (an Object's ObjectType, a Variable's VariableType, a type node itself) and whose
value is that node - never left unset or BaseObjectType. `kind` (builtin UI type name or a
path to a type node) overrides the derived Kind and permits a target-less pointer. A
NodePointer with neither is refused `kind_required`; `target`/`kind` on another datatype is
`kind_not_applicable`; the bridge adds `node_not_found`, `type_not_found`, `kind_unresolved`.
`dry_run` lists the Kind it resolved under `report.resolved` (`[{op_index, path, datatype,
kind}]`), including for a target created earlier in the same batch.

**`create_variable` station tags and arrays.** `{op:"create_variable", parent, name, datatype,
variable_type:"RAEtherNetIPTag", array_dimensions:4, properties:{"SymbolName":"MyTags"}}`.
`variable_type` is any VariableType by name, `array_dimensions` one positive integer,
`properties` a `{name: scalar}` map of properties the type declares. Refusals are structured
(`unknown_variable_type`, `invalid_array_dimensions`, `invalid_properties`,
`invalid_property_name`, `invalid_property_value`, `unknown_property`); `dry_run` covers the
new fields (the tool takes `dry_run` too and the bridge resolves the type without creating).

**`add_translations` / `remove_translation` (translation rows).**
`{op:"add_translations", rows:[{key, value[, locale]}, ...][, locale]}` adds or overwrites
every row in one call (`POST /bridge/i18n/translations`, rows as a JSON body); a row
without `locale` takes the op-level one (default `en-US`). `{op:"remove_translation", key}`
is **not supported**: the Optix SDK cannot remove a translation row, so the op is always
refused (`POST /bridge/i18n/translation/remove` validates the key, then answers
`not_supported`; edit the key in Studio's Translations editor or overwrite its text with
`add_translation` / `add_translations` instead).
Both ops are checked as a whole before anything is written: `dry_run` reports structured
errors — service side `rows_not_a_list`, `rows_empty`, `too_many_rows`,
`row_not_an_object`, `missing_field`, `invalid_text_key`, `invalid_locale`,
`unknown_row_field`, `duplicate_row` (each with `row_index`/`field`); for
`remove_translation`: `missing_field` / `invalid_text_key` for a bad key, and otherwise
`not_supported` in `report.errors` with its `op_index` (dry_run included; a batch containing
it applies nothing); the single bridge route answers `translation_not_found` for an unknown
key and `not_supported` for an existing one. The single tools `optix_bridge_add_translations` and
`optix_bridge_remove_translation` (primitives, gated by `FTXMCP_BRIDGE_PRIMITIVES`) take
`dry_run` too and return `{ok:false, code:"invalid_translation_rows" | "invalid_translation_key"
| "translation_not_found" | "not_supported", ...}` instead of raising for these.

**`wire_event` multi-step handlers.** `calls` is a list of call objects
(`method_path` | `command` [+ `variable`/`value`], `args`, `object_raw` — the same
fields as a single call) that become MethodContainer1..N, in list order, under one
EventHandler's MethodsToCall; it replaces the single-call arguments (passing both is
`calls_conflict`). `append=true` adds the call(s) after the existing handler's calls
without touching them (a single call + `append=true` appends one); `append` and
`replace` together are refused. Without `append`, the first call updates the existing
handler's one call in place as above, and the rest follow it. The result carries
`steps`, `appended`, `created`, `call_count` and `containers`. A bad list is
`{ok:false, code:"invalid_wire_calls", errors:[{call_index, code, message}]}` with
nothing written, and `optix_bridge_edit` `dry_run` reports the same per op.

**`wire_event` result fields and idempotency.** When the target node already
has a handler for the same event type, the bridge updates it in place (replaces
`args` and `ObjectPointer`, reads back the result) rather than silently no-oping.
`via` in the result names the route and ends with the **outcome**: a route
token (`eventhandler`, plus `+args`/`+lateobj`/`+reltype` as used, or
`uicommand:<Name>` / `command:<Name>`) followed by `+existing` (existing handler
patched in place), `+replaced` (old handler deleted, new one written; the result
also carries `replaced: true`), or no suffix (newly created) — e.g.
`"eventhandler+args+lateobj+existing"`. When the bridge cannot
safely identify the existing handler — e.g. multiple handlers for the same event
or an incompatible handler shape — it returns `handler_exists` with the
conflicting handler's details instead of mutating. Pass **`replace=true`** to
force delete-then-create regardless: the old handler is removed first and a fresh
one written, guaranteeing the result matches the op. The result also carries
`updated` (bool), `replaced` (bool), and `changed` (bool — false when the
existing handler already matched exactly and no write was needed).

`reorder` `index` counts among **graphic children** only (variables and sub-objects are excluded from the count) and is direction-independent; `achieved` in the result reports the final position regardless of which direction the node moved.

**Batch validator report codes** (appear in `report.errors[*].code` /
`report.warnings[*].code` alongside `op_index`, `code`, `message`):

| Code | Severity | Meaning |
|---|---|---|
| `bad_op_verb` | error (tool level) | an op's `op` verb is not in the batch verb list; `optix_bridge_edit` refuses the whole call with `{state: "failed", error: "bad_op_verb", valid_ops}` before validating or applying anything |
| `unknown_op` | warning | the bridge validator does not know this verb, so the op is **applied unchecked** (not a refusal) |
| `unknown_op_field` | error (destructive verbs) / warning (others) | field not declared for the verb — destructive ops (`delete`/`move`/`reorder`) hard-fail unconditionally because there is no scoped form |
| `default_widget_type` | warning | `create_widget` carries no `widget_type`; bridge defaults to `Label` |
| `invalid_widget_type` / `invalid_object_type` | error (service, at validate time) | `create_widget` `widget_type` / `create_object` `object_type` names no builtin type (checked against the bridge's type catalog, fetched once per batch; a type path is checked against the live model and the batch's `create_type` ops). Carries `did_you_mean` (closest catalog name or `null`) and `op_index`. Skipped (fail open) when the bridge cannot answer; apply still refuses with `type_not_found` |
| `missing_field` | error | `widget_type` / `object_type` present but empty. `""` is **not** the same as absent: omit `widget_type` for the `Label` default (with the `default_widget_type` warning) or `object_type` for a plain `BaseObjectType` |
| `invalid_browse_name` | error | the `name` of any creating/renaming op (`create_variable`, `create_object`, `create_folder`, `create_widget`, `create_type`, `create_alias`, `create_netlogic`, `rename` `new_name`, `convert_to_type`, `copy`/`duplicate` targets) contains `/` or `\`, a control character, or leading/trailing whitespace (`.`, `[`, `]`, `@` stay allowed). Refused before anything is created, so a `D/E` variable can no longer be created and then reported `create_variable_not_created` |
| `bad_format_string` | error | `attach_string_formatter` / `attach_formatter` `format` has unbalanced braces or a placeholder that is not `{n}` / `{n:spec}` with an integer `n` (`attach_formatter` also accepts `{#name}` where `name` is bound by a `name=path` entry in `sources`). Carries the offending `token`. `unbound_placeholder` (a positional `{n}` with no source) is a separate, unchanged check |
| `duplicate_create` | error | two ops in the batch create the same path |
| `already_deleted` | error | op targets a path deleted earlier in the same batch |
| `modifies_deleted_node` | error | `set_property`/`bind`/etc. on a node deleted earlier in the batch |
| `unresolved_parent` | error | parent path does not exist live or hypothetically |
| `unresolved_reference` | error | target path not found and not created in this batch |
| `unverifiable_property` | warning | `ValidateOnNode` cannot check the property because the node has no declared type in the batch |
| `moved_earlier_in_batch` | error | path was relocated by an earlier `move`/`rename` op; the old path no longer resolves — use the new path reported in `new_path` |
| `routed_into_collection` | warning | `create_widget` auto-routed into a placeholder collection (e.g. `NavigationPanel.Panels`); `routed_path` carries the actual created path — use it for subsequent `set_property`/`bind` ops |
| `ambiguous_container` | error | `create_widget` matched more than one placeholder collection under the parent — pass the explicit sub-path as `screen` |
| `not_renderable` | error | `widget_type` is `Item` or `Container` (non-renderable abstract types); use a concrete widget type |
| `read_only_collection` | error (promoted from apply-time) | the only matching placeholder collection is runtime-managed and not authorable — target a different parent or use an explicit sub-path |

| Tool | What it does |
|---|---|
| `optix_bridge_edit` (`action="apply"`/`"continue"`/`"status"`, `batch_id`, `chunk_seconds`, `chunk_ops`) | Batch of live-model authoring ops (see verb list above), validated then applied as a whole. Large batches stop early and return `state="chunked"` when the time limit (`OPTIX_BATCH_CHUNK_SECONDS`, default 90 s) or op-count limit (`OPTIX_BATCH_CHUNK_OPS`, default 40) is reached — pass the returned `batch_id` to `action="continue"` to apply the next chunk (the `chunk_seconds`/`chunk_ops` used on the first call are persisted in the journal and honoured by `continue`); `action="status"` returns the journal for a `batch_id` or the most-recent N batches for the project when `batch_id` is omitted. MCP progress notifications are best-effort and **silently absent without a progressToken** — the journal is the authoritative record of what landed. **Apply failures:** the path is not atomic. If op N fails and nothing had been applied (`applied == 0`) the result is `state="failed"` with `failed_op` and the nudge "op N failed before anything was applied; the model is unchanged"; only when `applied > 0` is it `state="partial"` with the not-atomic wording (ops 0..N-1 stay in the model). Scope note: `action="status"` is an authoring-scope call (requires `author` token) because it shares the same tool route as the mutating actions. |
| `optix_bridge_add_bound_widget` | Create + position + bind in one call — the standard way to add a bound control |
| `optix_bridge_add_navigation_panel_item` | Add a nav tab in one call (create into `Panels` + Title + target screen) |
| `optix_bridge_add_label` | One-shot Label with text (+ optional position) |
| `optix_bridge_ensure_web_engine` | Ensure the web presentation engine exists so a deploy has a canvas to serve |
| `optix_bridge_convert_to_type` | Promote an existing instance to a reusable ObjectType (Studio's "Convert to Type", with a link audit) |
| `optix_bridge_validate_expression` | Syntax-check a formula before wiring it |
| `optix_bridge_arm` (`action="arm"`/`"stop"`) | Arm or stop the design-time bridge for a project with no human at the keyboard — walks a collapsed project tree and routes by BrowseName as well as folder name |
| `optix_execute_method` (`project`, `node`, `method`) | Right-click -> Execute ANY design-time `[ExportMethod]` on a NetLogic node — the generalisation of `optix_bridge_arm` for a project's own generators and one-off migration scripts. `ok: true` means the click landed and the Studio log showed no rejection, not that the method succeeded; a call Studio refused returns `ok: false, state: "rejected"` with the log line (`log_line`); confirm by `optix_describe_node` or a render |
| `optix_build_check` | Compile the project's NetSolution C# against an isolated temp copy and report errors (with a stale-references hint) — the working tree is never touched |
| `optix_bridge_invoke_method` | Execute an exported NetLogic method (`IUAObject.ExecuteMethod`) — the generic escape hatch for anything with no dedicated bridge verb. Runs under a root Optix session, so a method that throws is logged and Studio stays up (issue #4). Needs a bridge advertising `invoke_session` (else `invoke_unsupported_bridge`); `optix_execute_method` is the right-click alternative |
| `optix_bridge_import_tags` | Import PLC tags into an RA EtherNet/IP or TwinCAT station through its design-time Tag Importer (`station`, `mode` offline\|online, `tags`, `file` for offline, `timeout_s`, `replace`, `dry_run`). **`dry_run=true` runs the pre-flight (file parse, the station's current tags) and returns `{ok: true, dry_run: true, would_add, would_remove, would_skip}` without calling the bridge import (with `replace=false` a non-empty `would_remove` also sets `would_refuse: "would_remove_tags"`; online mode cannot be predicted).** **Merge by default; `replace=true` to drop tags not requested; the importer rewrites the station's tag tree from the file, so a file that lacks a tag removes it.** Before any bridge call an offline `file` must exist, be non-empty XML with root `RSLogix5000Content` (L5X) or `PlcProjectInfo` (TPY), else `{ok: false, error: "invalid_import_file", path, detail}`. With `replace=false`, a station tag missing from the file is refused with `{error: "would_remove_tags", removed_would_be, hint}` before importing. Result: `added`, `already_present`, `not_found`, `removed` (paths gone), `skipped` (`[{symbol, reason}]`), `elapsed_ms`. Gated primitive (see below); the `optix_bridge_edit` op `import_tags` takes the same fields |

**Per-noun bridge primitives (gated, off by default).** The 22 tools that are
1:1 with an `optix_bridge_edit` op verb — `optix_bridge_set_property`,
`_bind_property`, `_attach_expression`, `_attach_formatter`,
`_wire_event`, `_delete_node`,
`_move_node`, `_reorder`, `_create_variable`, `_create_folder`,
`_create_object`, `_create_type`, `_create_alias`, `_create_widget`,
`_add_translation`, `_add_translations`, `_remove_translation`,
`_set_text_key`, `_set_modelling_rule`, `_retype`, `_import_tags`,
`_create_netlogic` — are popped from the registry by default; they clutter
the surface for no capability `optix_bridge_edit` doesn't already cover
(including NetLogic creation: the `create_netlogic` op takes `parent`, `name`
and `kind` and shares one implementation with `optix_bridge_create_netlogic`,
so a default install can create a NetLogic).
Set `FTXMCP_BRIDGE_PRIMITIVES=1` to restore them (an opt-in escape hatch for
callers that prefer one-verb-per-tool). The composite wrappers above and
`optix_bridge_edit` itself are **never** gated.

## Preview & ship

| Tool | What it does |
|---|---|
| `optix_emulator` (`action="run"`/`"restart"`/`"stop"`/`"status"`/`"log"`) | Emulator lifecycle: start (`run`), stop-then-start-then-wait (`restart`), stop (`stop`), structured state (`status`), tail the runtime log (`log`, honors `lines`/`contains`). Readiness probes derive the web-engine port from the project's own `WebPresentationEngine` node (per-project resolver; there is no `port` parameter). **`status`** returns `state` (`stopped`/`starting`/`running`/`zombie`/`demo_expired`), `running` (bool, back-compat), `pids`, `port_reachable`, and `last_lifecycle_line`; when `project=` is supplied it also returns `project_resolved` (name extracted from the emulator cache path) and `matched_path` as attribution evidence (I23/I30). **Per-project (multi-Studio):** `status`/`stop` with `project=` act on that project's emulator only — its own `pids`, its own web `port`, and `external_change` scoped to it; without `project` they are host-wide. **`run`/`restart`** are not blocked by another project's running emulator; they refuse with `web_port_taken` (`runtime`: `none`, `zombie` or `running`; every result carries `owner: {pid, process}` when the listener table is readable, else `owner: null` with `owner_reason`; the retired `legacy_code` is gone) when two projects share a web port or a foreign listener holds it, and `status` reports the same code (#30, #87, #95); `restart` with nothing running returns `restarted: false, state: "stopped"`, and `restarted` is true only when a runtime was stopped and the new run did not fail; an http.sys squatter (pid 4 / System) gets a status hint naming http.sys / `netsh http show urlacl` |
| `optix_save` | Explicit Ctrl+S — rarely needed (the emulator saves as part of staging). Re-sends Ctrl+S every 5 s until the write lands (2nd keystroke = project-tree click + Ctrl+S), within `timeout` (default 30 s) plus a 15 s grace after the last keystroke. Returns `method` (`ctrl_s` \| `tree_focus_ctrl_s`), `keystrokes_sent`, `attempts`, `write_latency_s`. On `saved:false` the hint lists: Studio busy with the NetSolution build (`optix_studio_log(contains="solution")`), focus, then UIPI |

`optix_emulator(action="run")` is idempotent: if the project's emulator is
already running or starting, no F5 is sent and it returns
`state="already_running"` — no status check is needed first. Structural
edits (new widgets, bindings, layout) need `action="restart"` before they
render.

## Verify (rendered canvas)

The 10 read/interact CDP primitives are consolidated into two discriminator
tools — `optix_observe(mode=…)` for reads and `optix_interact(action=…)` for
actions. **The default surface is consolidated-only**: the 10 deprecated
`optix_cdp_*` aliases are OFF by default and are only registered when
`FTXMCP_LEGACY_TOOLS=1` (an opt-in escape hatch for existing configs; the
aliases delegate to the same functions and carry a deprecation marker).
`optix_cdp_sweep` / `optix_cdp_restart` are NOT aliases — they are
batch/lifecycle tools kept as-is and always registered.

| Tool | What it does |
|---|---|
| `optix_observe` | Read-side capture: `mode` in `screenshot` / `ocr` / `read_text` / `find_text` / `diff` / `dom_text` — consolidates the five read CDP tools; `dom_text` returns the WebUI's exact `document.body.innerText` via CDP. `target` is `"web"` (default) or `"native"`. **Web:** port resolved per-project from the `WebPresentationEngine` node (via armed bridge or offline `UI.yaml` parse); there is no `port` parameter; self-heals when the emulator is serving on a different port than expected (`port_followed` in the result). **Native:** reads the emulator's native presentation engine window (FTOptixRuntime.exe's own desktop window) via Win32 `PrintWindow` instead of the CDP canvas — no Chrome/CDP/web engine involved; every mode but `diff` honors it; the window may be covered but not minimized; the native frame's coordinates (and `find_text` centers) feed `optix_interact(target="native")` as-is. |
| `optix_interact` | Action: `action` in `click` / `fill` / `type` / `key` / `navigate` — consolidates the five interact CDP tools. `target` is `"web"` (default) or `"native"`. **Web:** port resolved per-project from the `WebPresentationEngine` node; there is no `port` parameter; self-heals on a port mismatch. **Native:** posts the input straight to the emulator's native presentation engine window instead (click/fill/type/key; no Chrome/CDP, window may be covered, focus never taken; coordinates are the `optix_observe(target="native")` frame, reported back as `px`/`py` + `frame`). `type` needs a prior `click` (focus) on the native engine; `fill` is click+type+submit. **Coordinate convention:** `x` and `y` are normalized viewport fractions when both values are ≤ 1.0 (e.g. `x=0.5, y=0.5` = screen centre); either value > 1.0 is treated as an absolute CSS pixel. Resolved CSS coordinates are reported back as `css_x` / `css_y`; the CSS viewport used for resolution is reported as `viewport: {"w": …, "h": …}`; `coords` is `"normalized"` or `"absolute"`. Out-of-viewport coordinates return `state: "failed", error: "bad_point"`. `settle_seconds` is applied after every action (not only after a navigation). |
| `optix_cdp_sweep` | Walk a route map in one session, capture per screen + OCR text manifest — baseline builder |
| `optix_cdp_restart` | Recover the verify browser |
| `optix_routes` (`action="save"`/`"get"`/`"list"`) | Bank/read/list navigation routes files server-side under `<project>/dev/` — the CREATE/read half of the routes-banking loop consumed by `optix_observe`/`optix_interact`(navigate) / `optix_cdp_sweep` |

The rows below are the **deprecated aliases** — absent by default, restored
only under `FTXMCP_LEGACY_TOOLS=1`. Prefer `optix_observe` / `optix_interact`.

| Tool (deprecated alias) | What it does |
|---|---|
| `optix_cdp_screenshot` | Screenshot the running HMI (auto-targets it); `fresh=true` forces a reload when a stale frame is suspected; `region=[x,y,w,h]` crops (<=1.0 = viewport fractions, >1 = pixels); `return_image=true` returns typed MCP image content inline |
| `optix_cdp_click` | Click at coordinates — reaches the Optix canvas where synthetic clicks don't |
| `optix_cdp_fill` | Set a field in one call: click + select-all + type + Enter |
| `optix_cdp_type` / `optix_cdp_key` | Keyboard primitives (mid-entry screenshots, arrow-stepping, Escape) |
| `optix_cdp_ocr` | Text read-back fallback when the client has no vision |
| `optix_cdp_read_text` | OCR a region (or the full frame) — the zero-vision-token "does it say X" check (needs tesseract) |
| `optix_cdp_find_text` | Locate rendered text: word boxes + clickable centers, feeds a click and route building (needs tesseract) |
| `optix_cdp_navigate` | Replay a banked route from a routes file — zero-screenshot navigation; `expect_text` steps OCR-verify arrival |
| `optix_cdp_diff` | Compare two sweep dirs: pixel gate + text-level delta per screen, pure text output |

## HTTP API

The same surface on `http://127.0.0.1:8765` for scripts and CI. No auth
header needed on a default loopback install.

```bash
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/projects
curl -X POST http://127.0.0.1:8765/projects/MyProject/run/emulator
curl -X POST "http://127.0.0.1:8765/runtime/cdp-screenshot?save_path=C:/Temp/shot.jpg"
```

See [`architecture.md`](architecture.md) for the request contract and error
envelope.

**Deploy & ship** — the deploy family (`optix_deploy`, `optix_deploy_updatesvc`,
`optix_deploy_preflight`) and the legacy v0.2.x single-shot authoring tools
(`optix_add_widget`, `optix_add_model_variable`, `optix_set_property`) are
deliberately not in the tables above — see [`architecture.md`](architecture.md#what-it-talks-to)'s
note under "What it talks to": this distribution authors, previews, and
verifies; shipping to hardware happens from Studio's own Deploy dialog.
