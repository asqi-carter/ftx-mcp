# Tool reference

62 tools, grouped by where they sit in the loop. Every tool's docstring
carries "Use when / Do NOT use when" guidance for the model, and MCP
annotations (`readOnlyHint`/`destructiveHint`) so hosts can auto-run reads
and gate writes. `project` is optional everywhere — it defaults to the
project open in Studio.

## Discovery & health

| Tool | What it does |
|---|---|
| `optix_doctor` | Dependency checklist with a plain-English fix per item — run first |
| `optix_health` | Service config + liveness |
| `optix_list_projects` | Projects under the projects root |
| `optix_list_screens` | Screen/Panel/Dialog nodes in the project |
| `optix_get_project_map` | Whole-subtree component map in one call — overview with counts, then drill by path |
| `optix_find` / `optix_read_file` | Search / read project files |
| `optix_describe_node` | Live node: children, properties, values |
| `optix_list_ui_types` / `optix_describe_type` | Widget catalog + per-type property legend (consult before setting); `type_names=[...]` batches a survey into one call |
| `optix_bridge_status` / `optix_studio_version` / `optix_services_status` | Component status |
| `optix_list_skills` / `optix_get_skill` | Bundled authoring playbooks — catalog + on-demand full content (served by the server itself, version-locked to the tools) |

## Authoring (live bridge — Studio open)

Writes go into Studio's in-memory model; an undeclared property is rejected
with the valid-property list rather than crashing Studio.

| Tool | What it does |
|---|---|
| `optix_bridge_create_widget` | Create a widget; collection-aware (a `NavigationPanelItem` aimed at the panel lands in `Panels` automatically) |
| `optix_bridge_add_bound_widget` | Create + position + bind in one call — the standard way to add a bound control |
| `optix_bridge_add_navigation_panel_item` | Add a nav tab in one call (create into `Panels` + Title + target screen) |
| `optix_bridge_set_property` | Set any property (colors as `#RRGGBB`/`#AARRGGBB`) |
| `optix_bridge_bind_property` | DynamicLink a property to a model variable |
| `optix_bridge_attach_expression` | Computed color/visibility/scaling/text from one or more sources |
| `optix_bridge_validate_expression` | Syntax-check a formula before wiring it |
| `optix_bridge_wire_event` | Click/change events + native Set/Toggle commands |
| `optix_bridge_create_variable` / `_create_alias` / `_add_translation` | Model variables, aliases, i18n |
| `optix_bridge_create_folder` / `_create_object` | Structural nodes: folders; plain Object containers or instances of custom types |
| `optix_bridge_create_type` / `_convert_to_type` | Reusable templates: create an ObjectType and author into it, or promote an existing instance (Studio's "Convert to Type", with a link audit) |
| `optix_bridge_move_node` | Reparent an instance (re-authoring move: copy + link fixups + delete original; NodeId changes) |
| `optix_bridge_reorder` | Z-order (send to back/front) |
| `optix_bridge_delete_node` / `_add_label` / `_ensure_web_engine` | Delete; one-shot label; ensure the web presentation engine |

## Preview & ship

| Tool | What it does |
|---|---|
| `optix_run_emulator` | Start Studio's emulator — the default verify step |
| `optix_restart_emulator` | Stop-if-running → start → wait serving: THE call after a structural edit |
| `optix_emulator_status` | `not_running` / `starting` / `running` (check first — the emulator toggle can stop a running one) |
| `optix_stop_emulator` | Stop it (structural edits need a stop → start to show) |
| `optix_runtime_log_tail` | Tail the emulator/NetLogic log when a preview misbehaves |
| `optix_save` | Explicit Ctrl+S — rarely needed (the emulator saves as part of staging) |

## Verify (rendered canvas)

| Tool | What it does |
|---|---|
| `optix_cdp_screenshot` | Screenshot the running HMI (auto-targets it); `fresh=true` forces a reload when a stale frame is suspected; `region=[x,y,w,h]` crops (<=1.0 = viewport fractions, >1 = pixels); `return_image=true` returns typed MCP image content inline |
| `optix_cdp_click` | Click at coordinates — reaches the Optix canvas where synthetic clicks don't |
| `optix_cdp_fill` | Set a field in one call: click + select-all + type + Enter |
| `optix_cdp_type` / `optix_cdp_key` | Keyboard primitives (mid-entry screenshots, arrow-stepping, Escape) |
| `optix_cdp_ocr` | Text read-back fallback when the client has no vision |
| `optix_cdp_read_text` | OCR a region (or the full frame) — the zero-vision-token "does it say X" check (needs tesseract) |
| `optix_cdp_find_text` | Locate rendered text: word boxes + clickable centers, feeds `optix_cdp_click` and route building (needs tesseract) |
| `optix_cdp_navigate` | Replay a banked route from a routes file — zero-screenshot navigation; `expect_text` steps OCR-verify arrival |
| `optix_cdp_sweep` | Walk a route map in one session, capture per screen + OCR text manifest — baseline builder |
| `optix_cdp_diff` | Compare two sweep dirs: pixel gate + text-level delta per screen, pure text output |
| `optix_cdp_restart` | Recover the verify browser |

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
