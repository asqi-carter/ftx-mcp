# Tool reference

35 tools registered by default, grouped by where they sit in the loop
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
| `optix_project` (`action="open"`/`"create"`) | Open or create a Studio project from MCP — Studio's CLI verbs are GUI launches, so readiness is the window's UIA identity, not process exit |
| `optix_list_screens` | Screen/Panel/Dialog nodes in the project |
| `optix_get_project_map` | Whole-subtree component map in one call — overview with counts, then drill by path |
| `optix_find` / `optix_read_file` | Search / read project files |
| `optix_read_netlogic` | Read a NetLogic `.cs` source by class name from `ProjectFiles/NetSolution` — works while Studio is open (the NetSolution is on disk, not in Studio's model) |
| `optix_describe_node` | Live node: children, properties, values |
| `optix_list_ui_types` / `optix_describe_type` | Widget catalog + per-type property legend (consult before setting); `type_names=[...]` batches a survey into one call. `resolved_by:"browse_name"` on an entry means the compiled NodeId constant disagreed with the loaded module and the bridge found the type by walking the type tree — the entry is usable as named. `misaligned` / `catalog_misaligned` now flag only the leftovers the walk could not reconcile: a constant NAMED differently from the node it correctly points at (`UserValueChangedEvent` -> browse name `UserValueChanged`); address those by the `browse_name` |
| `optix_schema` (`action="dump"`/`"list"`/`"diff"`) | Cache the full type-schema dump per Studio version (offline), list cached versions, diff two versions (upgrade intelligence) |
| `optix_bridge_status` / `optix_active_target` | Bridge status (one entry per armed port); which deployment target Studio's dropdown has selected |
| `optix_bridge_log_tail` | The bridge's transport diagnostics — the forensic view when a bridge drops mid-edit |
| `optix_list_skills` / `optix_get_skill` | 19 bundled authoring playbooks — catalog + on-demand full content (served by the server itself, version-locked to the tools) |

## Authoring (live bridge — Studio open)

Writes go into Studio's in-memory model; an undeclared property is rejected
with the valid-property list rather than crashing Studio.

`optix_bridge_edit` is the primary entry point — batch one or more ops
(`set_property`, `bind`, `create_widget`, `create_variable`, `create_folder`,
`create_object`, `create_type`, `create_alias`, `delete`, `move`, `reorder`,
`wire_event`, `attach_expression`, `attach_formatter`, `attach_string_formatter`, `retype`, `add_translation`, `rename`), validated as a whole
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
| `unknown_op` | error | op verb not recognised by the bridge |
| `unknown_op_field` | error (destructive verbs) / warning (others) | field not declared for the verb — destructive ops (`delete`/`move`/`reorder`) hard-fail unconditionally because there is no scoped form |
| `default_widget_type` | warning | `create_widget` carries no `widget_type`; bridge defaults to `Label` |
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
| `optix_bridge_edit` (`action="apply"`/`"continue"`/`"status"`, `batch_id`, `chunk_seconds`, `chunk_ops`) | Batch of live-model authoring ops (see verb list above), validated then applied as a whole. Large batches stop early and return `state="chunked"` when the time limit (`OPTIX_BATCH_CHUNK_SECONDS`, default 90 s) or op-count limit (`OPTIX_BATCH_CHUNK_OPS`, default 40) is reached — pass the returned `batch_id` to `action="continue"` to apply the next chunk; `action="status"` returns the journal for a `batch_id` or the most-recent N batches for the project when `batch_id` is omitted. MCP progress notifications are best-effort and **silently absent without a progressToken** — the journal is the authoritative record of what landed. Scope note: `action="status"` is an authoring-scope call (requires `author` token) because it shares the same tool route as the mutating actions. |
| `optix_bridge_add_bound_widget` | Create + position + bind in one call — the standard way to add a bound control |
| `optix_bridge_add_navigation_panel_item` | Add a nav tab in one call (create into `Panels` + Title + target screen) |
| `optix_bridge_add_label` | One-shot Label with text (+ optional position) |
| `optix_bridge_ensure_web_engine` | Ensure the web presentation engine exists so a deploy has a canvas to serve |
| `optix_bridge_convert_to_type` | Promote an existing instance to a reusable ObjectType (Studio's "Convert to Type", with a link audit) |
| `optix_bridge_validate_expression` | Syntax-check a formula before wiring it |
| `optix_bridge_arm` (`action="arm"`/`"stop"`) | Arm or stop the design-time bridge for a project with no human at the keyboard — walks a collapsed project tree and routes by BrowseName as well as folder name |
| `optix_execute_method` (`project`, `node`, `method`) | Right-click -> Execute ANY design-time `[ExportMethod]` on a NetLogic node — the generalisation of `optix_bridge_arm` for a project's own generators and one-off migration scripts. `ok` means the click landed, not that the method succeeded; confirm by `optix_describe_node` or a render |
| `optix_build_check` | Compile the project's NetSolution C# against an isolated temp copy and report errors (with a stale-references hint) — the working tree is never touched |
| `optix_bridge_invoke_method` | Execute an exported NetLogic method (`IUAObject.ExecuteMethod`) — the generic escape hatch for anything with no dedicated bridge verb. Runs under a root Optix session, so a method that throws is logged and Studio stays up (issue #4). Needs a bridge advertising `invoke_session` (else `invoke_unsupported_bridge`); `optix_execute_method` is the right-click alternative |

**Per-noun bridge primitives (gated, off by default).** The 16 tools that are
1:1 with an `optix_bridge_edit` op verb — `optix_bridge_set_property`,
`_bind_property`, `_attach_expression`, `_attach_formatter`,
`_wire_event`, `_delete_node`,
`_move_node`, `_reorder`, `_create_variable`, `_create_folder`,
`_create_object`, `_create_type`, `_create_alias`, `_create_widget`,
`_add_translation`, `_create_netlogic` — are popped from the registry by default; they clutter
the surface for no capability `optix_bridge_edit` doesn't already cover.
Set `FTXMCP_BRIDGE_PRIMITIVES=1` to restore them (an opt-in escape hatch for
callers that prefer one-verb-per-tool). The composite wrappers above and
`optix_bridge_edit` itself are **never** gated.

## Preview & ship

| Tool | What it does |
|---|---|
| `optix_emulator` (`action="run"`/`"restart"`/`"stop"`/`"status"`/`"log"`) | Emulator lifecycle: start (`run`), stop-then-start-then-wait (`restart`), stop (`stop`), structured state (`status`), tail the runtime log (`log`, honors `lines`/`contains`). Readiness probes derive the web-engine port from the project's own `WebPresentationEngine` node (per-project resolver; `port` parameter overrides). **`status`** returns `state` (`stopped`/`starting`/`running`/`zombie`/`demo_expired`), `running` (bool, back-compat), `pids`, `port_reachable`, and `last_lifecycle_line`; when `project=` is supplied it also returns `project_resolved` (name extracted from the emulator cache path) and `matched_path` as attribution evidence (I23/I30). **`run`/`restart`** refuse with `{"ok": false, "error": "runtime_project_mismatch", "requested": …, "resolved": …, "pid": …, "nudge": …}` when a running emulator's cache path identifies a different project — guarding against stopping the wrong emulator on a multi-Studio box (I30) |
| `optix_save` | Explicit Ctrl+S — rarely needed (the emulator saves as part of staging) |

`optix_emulator` toggles on F5 (`action="run"`) — check `action="status"`
first so a blind "run" doesn't stop a running emulator. Structural edits
(new widgets, bindings, layout) need `action="restart"` before they render.

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
| `optix_observe` | Read-side capture: `mode` in `screenshot` / `ocr` / `read_text` / `find_text` / `diff` — consolidates the five read CDP tools. Web-engine port resolved per-project from the `WebPresentationEngine` node (via armed bridge or offline `UI.yaml` parse); `port` parameter overrides. Self-heals when the emulator is serving on a different port than expected (`port_followed` in the result) |
| `optix_interact` | Action: `action` in `click` / `fill` / `type` / `key` / `navigate` — consolidates the five interact CDP tools. Web-engine port resolved per-project from the `WebPresentationEngine` node; `port` parameter overrides; self-heals on a port mismatch. **Coordinate convention:** `x` and `y` are normalized viewport fractions when both values are ≤ 1.0 (e.g. `x=0.5, y=0.5` = screen centre); either value > 1.0 is treated as an absolute CSS pixel. Resolved CSS coordinates are reported back as `css_x` / `css_y`; the CSS viewport used for resolution is reported as `viewport: {"w": …, "h": …}`; `coords` is `"normalized"` or `"absolute"`. Out-of-viewport coordinates return `state: "failed", error: "bad_point"`. `settle_seconds` is applied after every action (not only after a navigation). |
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
