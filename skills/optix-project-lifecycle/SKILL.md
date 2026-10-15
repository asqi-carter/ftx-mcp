---
name: optix-project-lifecycle
description: Cold-start a project with no human at the keyboard — create one from a Studio project template, open it, arm its bridge, and get past the NetLogic security warning. Use for "new project", "new from template", "open a project", "arm the bridge", "set up a scratch/demo project to test against", or when a tool answers bridge_unavailable / studio_window_not_found.
---

# Project lifecycle (nothing -> authorable, hands-off)

Three calls take you from no project to a live bridge. Everything below is
verified against Studio 1.7.4.32 / ftx-mcp 1.0.7.

```
optix_project(action="new", project="Probe01", template="MCPBridgeTemplate")
optix_project(action="open", project="Probe01")    # skip: "new" leaves Studio open on it
optix_bridge_arm(action="arm", project="Probe01")
```

`new` returns `studio_left_open: true` — go straight to `arm`. `arm` returns
`{state: "armed", port, consent_clicked, consent_dismissed}`; the port is where that project's
bridge now answers.

## `open` answers truthfully: `opened`, `already_open`, `not_opened`

`opened` / `already_open` mean a Studio really has the project **loaded** (bridge health name/dir, else the
main-window title, else the Studio log line). The Home screen's Recent list never counts. If a Studio already has
it, `open` returns that Studio (`pid`) and launches nothing. If Studio refuses a second editor ("same project is
open in another") or stays on Home, you get `{ok: false, state: "not_opened", reason, holder_pid}` and the
second Studio the service launched is closed. Work in the Studio `holder_pid` names (arm that one); do not retry
`open` in a loop.

The same honesty applies right after `new`: `new` leaves Studio open on the project, so a following `open` returns
`already_open` and launches nothing - go straight to `arm`.

## `upgrade_required` — an operator decision (Studio 1.8.0.555)

Opening a project saved by an older Studio makes 1.8 raise a modal
**Project Upgrade** dialog. `optix_project(action="open")` detects it and returns
at once instead of timing out:

```
{ok: false, error: "upgrade_required", project, studio_version, dialog: {title, text}, hint}
```

**The upgrade dialog is not clicked automatically**, and you must not try to
click it either. Upgrading is one-way, so accepting or declining is an
**operator decision**. Stop, tell the user, and retry `open` once they have
answered it in Studio. Do not loop on `open` and do not run `arm` against it: no
project window exists until the dialog is answered.

Other 1.8.0.555 behaviours that touch arming (unit-tested; pending bench
verification on a live 1.8.0.555 Studio):

- Tree row labels are matched at any bounded depth, so arm works on both the 1.7
  and 1.8 UIA shapes.
- Keys are sent to the Search box only once it is confirmed focused. Otherwise
  arm returns `focus_unconfirmed` (no keystroke sent); refocus Studio and retry
  once, as in the foreground-contention notes below.
- `optix_emulator` consent clearing resolves the project directory itself; an
  unknown name gives `project_not_found`.

## Always test on a throwaway project

`create_variable` is **not idempotent** and emit teardown does not touch
`Model/`, so re-running a build into a used project silently duplicates seeded
variables while every op still reports success. A fresh project per experiment
is the cheap defence — that is what `new` is for. **Never experiment against a
production HMI project.**

## Driver station tags and arrays: `create_variable` extras (1.0.9+)

`optix_bridge_create_variable` (and the batch `create_variable` op) takes three extra
fields beyond parent/name/datatype:

- `variable_type` - any VariableType by name, e.g. `RAEtherNetIPTag` (needs the driver
  module in the project); default `BaseDataVariableType`.
- `array_dimensions` - one positive integer (`4` / `[4]`) for a one-dimensional array.
- `properties` - `{name: scalar}` of child properties the type declares, e.g.
  `{"SymbolName": "MyTags"}`.

```json
{"op": "create_variable", "parent": "Model", "name": "Tags", "datatype": "Int32",
 "variable_type": "RAEtherNetIPTag", "array_dimensions": 4,
 "properties": {"SymbolName": "MyTags"}}
```

`datatype` must be one of `Boolean, Byte, SByte, Int16, Int32, Int64, UInt16, UInt32, UInt64,
Float, Double, String, NodeId, DateTime, LocalizedText, Range, Color, ResourceUri` (exact case) or
`NodePointer`; anything else is `invalid_datatype` (with the `allowed` list) in `dry_run`, the same
list the bridge enforces at apply.

Bad input is refused with structured errors and nothing written (`unknown_variable_type`,
`invalid_array_dimensions`, `invalid_property_name`, `unknown_property`, ...);
`dry_run=True` (single tool) or `optix_bridge_edit(dry_run=True)` catches them first.
Remember a tag is polled only while something binds it (see the NetLogic skill).
Array elements are then bound with `raw_path` (slate 5c), not `source_path`.

## Adding PLC tags (1.0.9+)

Driver tags (RA EtherNet/IP, TwinCAT) can be added without the Studio GUI. Pick by how
many members you need:

- **One or two members: `create_variable` + `variable_type`.** Create the tag directly
  under the station's Tags node, e.g. `variable_type: "RAEtherNetIPTag"` with
  `properties: {"SymbolName": "..."}` (see the section above). Run `dry_run=True` first.
- **Several members: `import_tags`.** `optix_bridge_import_tags` (or the `import_tags` op of
  `optix_bridge_edit`) runs the station's Tag Importer once for the whole list:

```json
{"op": "import_tags", "station": "CommDrivers/RAEtherNet_IPDriver1/Station1",
 "mode": "offline", "file": "C:/plc/line1.L5X",
 "tags": ["Motor1.Speed", "Motor1.Running", "Pump2"]}
```

  A requested symbol brings in its parent path and its children, nothing else (`Motor1`
  never matches `Motor10`). **Merge by default; `replace=true` to drop tags not requested;
  the importer rewrites the station's tag tree from the file, so a file that lacks a tag
  removes it.** With `replace` off, tags already on the station are kept, and a call whose
  file lacks one of them is refused with `would_remove_tags` (`removed_would_be`, `hint`)
  before anything is imported: pass `replace: true` to accept, or import from a file that
  still contains them. An offline `file` that is missing, empty, not XML or not an L5X/TPY
  (root `RSLogix5000Content` / `PlcProjectInfo`) is refused with `invalid_import_file`
  (`path`, `detail`) and the station is untouched. The result lists `added`,
  `already_present`, `not_found`, `removed` (node paths gone after the import), `skipped`
  (`[{symbol, reason}]`: matched but not created, e.g. an unknown DataType) and
  `elapsed_ms`; check `not_found` and `skipped` for typos. `file` is required for `offline` (L5X for RA,
  TPY for TwinCAT). An RA EtherNet/IP station on Studio below 1.8 is refused with
  a "requires Optix 1.8" error (CHANGELOG 1.0.9 has the exact code; TwinCAT works from 1.7). Failures: `import_failed`,
  `import_cancelled`, `import_timeout` (`timeout_s`, default 120).
- **Which importer `import_tags` uses.** It uses the station's own TagImporter when it has
  one (a station created in Studio's UI does); that importer is never modified or deleted and
  the result says `tag_importer: "existing"`. A station made through the MCP (`create_object`)
  has none, so `import_tags` adds a temporary `FtxMcpTagImporter` under the station
  (`tag_importer: "temporary"`), runs the import and deletes it when the task finishes,
  whether it succeeded, faulted, was cancelled or failed validation. The imported tags stay.
  `create_object` of a station no longer creates an importer. If the importer cannot be
  obtained, `import_failed` lists what was tried and a `hint`; nothing half-created is left.
- **`tag_importer_left`.** Present only when a temporary importer was **not** removed: the
  path of the leftover `FtxMcpTagImporter` node. On `import_timeout` the import task may still
  be running, so the importer is kept and `tag_importer_left` names it (top level of the result
  and in the error entry); if deleting it fails, `tag_importer_cleanup_error` says why. Wait
  and check the station's Tags (`added` is not reported on a timeout), then delete the
  node at that path yourself (`delete`) before retrying. Absent means nothing was left behind.
- **Reading an `import_failed` diagnostic.** The detail names: the importer's runtime type
  full name (a plain model object instead of the importer class shows here); its
  public property names (is `Mode` / `FilePath` there?); every public `Import*` method with
  parameter types (is there a `(string, string) -> bool` delegate overload?); and which of
  `Mode` / `FilePath` / `Import(Func<string,string,bool>)` is missing. The station-side form
  says whether a station-owned TagImporter child exists and where it was looked for
  (`<station>/TagImporter`), plus what the temporary-importer attempt tried (type not
  resolved, add failed, `station.TagImporter` still failing). "NOT FOUND" with a failed
  temporary attempt means the importer type is missing on this Studio; "exists but a member is
  missing" means a Studio API mismatch - report the type and method list.
- **`Import` binding.** Any public `Import` overload taking one `(string, string) -> bool`
  delegate is accepted, whatever the delegate type is called. The parameterless `Import()` is
  used only when `tags` is empty (import everything); with a `tags` list it is never used, so
  a list can never silently import the whole controller.
- **One batch can create the station and its tags.** The batch validator knows a station type
  creates its `Tags` and `Types` children, so `create_object` of the station followed by
  `create_variable` under `<station>/Tags` validates without `unresolved_parent`.
- **Do not use `create_object` for a tag type.** A VariableType passed as `type` is refused
  with `variable_type_not_object`; the hint points back to `create_variable`.

**Online mode has no live test on the ftx bench.** `mode: "online"` reads the symbols from a
live controller; the bench has no controller, so only offline mode is covered (by a live test
against an L5X fixture). Treat online imports as unverified and check `added`/`not_found`
yourself.

### Bind to one array element: `raw_path` with `[i]` (1.0.9+)

`source_path` resolves to a whole variable, so it cannot name an element. Use
`raw_path` - the literal-NodePath escape - with ONE `[i]` index on a segment:

```json
{"op": "bind", "path": "UI/MainWindow/Label1", "name": "Text",
 "raw_path": "Model/Tags[1]"}
```

Also `{Alias1}/Tags[1]` and `../../Alias1/Tags[1]` inside a template. `i` is a
non-negative integer (zero-based), one-dimensional arrays only; the path reaches the
bridge verbatim and its target is not resolved at bind time (render-verify). Malformed
input is refused with structured errors and nothing written: `invalid_raw_path`
(blank, empty segment, unbalanced `[ ]`/`{ }`, `[1]` with no name or after `..`) and
`invalid_raw_path_index` (`[]`, `[x]`, `[-1]`, `[1][2]`, > 2**31-1). `dry_run=True`
(on `optix_bridge_bind_property`, or `optix_bridge_edit(dry_run=True)`) reports the
same errors / plan without touching the model; a single-bind `dry_run` does not resolve
`source_path` (batch `dry_run` does).

**Batch `dry_run` resolves `source_path`** (1.0.9). In `optix_bridge_edit(dry_run=True)`
every `bind` op's `source_path` is looked up in the live model and in the nodes created
earlier in the same batch (`create_variable` / `create_widget` / ... before the bind), so a
`create_widget` + `bind` batch with a bad source fails at dry_run instead of ending
`partial` with the widget stranded. Per-op errors (`op_index`, `field: "source_path"`):
`source_not_found` (nothing there, a source created only *after* the bind, or one deleted
earlier in the batch) and `source_not_variable` (resolves to a non-variable node). If the
live lookup itself fails, a `source_unverified` warning is returned instead of a claim
either way. `raw_path` binds are never resolved. Run `dry_run=True` first; a real (non
dry-run) batch does not re-check the source.

### Batch refusals: `refused_ops` (1.0.9+)

`optix_bridge_edit` does not fail a whole batch because one op touches a protected subtree.
Ops the bridge refuses for a subtree reason - `refused_netlogic_move`, `contains_netlogic`,
`contains_event_handler` (a `move`, a `rename` lowered to a move, or `convert_to_type` on a node
that is, or holds, a NetLogic / EventHandler) - are listed in `refused_ops`, one record each:
`{index, op, path, code, error, hint}`. The other ops validate and apply; `applied` counts only
what landed, and the `nudge` names the refused indices. `dry_run` reports the same refusals
without failing the batch. Read `refused_ops` after every batch: a non-empty list with
`state: applied` means the rest of the batch DID run. Any other validation error
(`unresolved_reference`, bad args, ...) still refuses the whole batch with `applied: 0`.

## The template

A project template is an ordinary project that already contains the
`StudioMCPBridge` NetLogic, so anything created from it can be armed with zero
manual Studio work.

**Making one is the single step that cannot be scripted.** In Studio: open the
project that has the bridge NetLogic -> **Save ▾ -> Save As -> tick "Template"**.
Studio writes
`Documents\Rockwell Automation\FactoryTalk Optix\ProjectTemplates\FTOptix_template_<Name>\`.

- **Pass the PLAIN name** — `template="MCPBridgeTemplate"`, never the
  `FTOptix_template_` folder prefix. The prefix appears on the folder AND on the
  internal `.optixt` `Name:`, which is normal and is not what the CLI matches.
- **Do not hand-copy a project folder** into `ProjectTemplates`. Studio rejects
  it (`Cannot create a new project with invalid template`) because there is no
  `.optixt`. Copying is also wrong for a different reason: a project carries its
  name and GUID in the `.optix` manifest, the `.optix.design`,
  `Nodes\<Name>.yaml` and every NodePointer value, and a folder copy leaves a
  project that believes it is the original. `new` rewrites all of that.
- **Verify a template before trusting it.** The `.cs` file alone is not enough —
  the arm invokes the NODE's `StartBridge`. Check
  `Nodes\NetLogic\NetLogic.yaml` contains a `StudioMCPBridge` node with methods
  `StartBridge`, `StopBridge`, `SetupProject`, `CheckFormula`.

After creating, confirm `0` stale references to the template name survived the
rename, and that the `.optix` `Name:`/`GUID:` are the new project's.

## The NetLogic security warning — handled, but know what it is

Studio raises a modal when a project's NetLogics reference security-critical
assemblies (`System.Net.Sockets` etc). The bridge is an HTTP listener, so every
bridge-bearing project raises it.

**This is handled automatically now** — both `optix_bridge_arm` and
`optix_emulator(action="run"/"restart")` tick *"Do not show this warning again"*
and click **Proceed**. `arm` reports `consent_clicked: true` and `consent_dismissed` (a dialog that survives the press fails the arm as `consent_not_dismissed` with the dialog title); the emulator
reports `consent_cleared: true` on the run that hit it, and omits the key
entirely once suppressed.

What matters if you ever touch this code:

- It is raised once per **TRIGGER**, not once per project. Before the checkbox
  was ticked, arm consented and then the very next F5 hit the identical dialog.
  Ticking it is what makes one dismissal cover the project's life.
- **`Cancel` is the focused default.** Never send keys to this dialog — a blind
  Enter cancels, leaves the emulator down, and leaves no trace of why.
- **Answering it lets the ALREADY-SENT F5 continue.** Do NOT resend F5 after
  clearing: F5 toggles, and a resend stops the emulator that just started.
- **UI Automation CAN see it.** `FindWindow`/Win32 enumeration cannot (Studio is
  native Qt and the modal is not a top-level window), but Qt draws modals into
  the main window's scene graph and `studio_uia.pending_dialog(pid)` walks
  exactly that. Screenshots and coordinate-clicking are NOT needed.

## Running a project's own design-time methods

`optix_execute_method(project, node, method)` right-click -> Executes any
`[ExportMethod]`. Three things it depends on, each of which cost a debugging
cycle:

* **The node must be a DESIGN-TIME NetLogic.** Create it with
  `optix_bridge_create_netlogic(parent, name, kind="design_time")` (1.0.9+; needs a
  bridge that supports `kind`). A plain `kind="runtime"` node (the default) is treated
  by Studio as RUNTIME: the class compiles and binds, but the context menu carries **no
  `Execute` entries**. The marker is the `BehaviourStartPriority` child with VariableType
  `BehaviourStartPriorityVariableType` (Value 180); `kind="design_time"` mints it
  (`create_variable` cannot - it always makes `BaseDataVariableType`). Use `dry_run=True`
  to preview. Fallback for an older bridge: right-click **NetLogic -> Add -> DesignTime
  NetLogic** in Studio, named EXACTLY the C# class name.
* **Where the tree chain comes from (1.0.9+).** With a bridge ARMED for the project,
  `optix_execute_method` derives the chain from the LIVE node path (the bridge's
  `/bridge/map?match=<node>`; the result reports `chain_source: "bridge"`), so a node you
  just created over the bridge works without an `optix_save`. With NO bridge armed (or
  when the live model has no single node of that name) it falls back to the ON-DISK
  NetLogic YAML (`chain_source: "yaml"`); then `optix_save` after creating or renaming the
  node, because Studio holds new method declarations in memory until a save. Before saving
  you get `menu_item_not_found` with a chain missing its folder ancestor. The YAML node
  lookup also PREFIX-matches, so a stale `FooScript` shadows `FooScript2` - delete the
  dead one.
* **Stop the emulator first.** A running runtime window is foreground
  contention: every right-click returns `menu_items: 0`, the same signature as
  the just-opened-and-still-indexing flake but a different cause. Killing the
  runtime turned `0` into `19` on the identical call. Do not run these gestures
  concurrently with anything else driving the desktop.
* **An accidental Start menu does the same.** It happens on some boxes without
  anyone touching the keyboard. Recovery: send `{ESC}`, `AppActivate` the Studio
  pid, and CONFIRM it is the foreground window before retrying — the arm drives
  the Project tree by UI Automation and needs Studio genuinely in front.
* **`verify_timeout` alone does NOT mean the arm failed.** It has been reported
  while the bridge started anyway (the port appeared a moment late). The
  authoritative check is Studio's own log:
  `%LOCALAPPDATA%\...\FTOptixStudio\Log\FTOptixStudio.0.log` contains
  `INFO;StudioBridge;;listening on http://127.0.0.1:<port> (started via StartBridge())`
  on a real arm. No such line = it never fired.
* **Retries do not converge.** Tree row positions drift between reopens; the
  same call failed three times with `menu_items: 0` on the first row, then
  succeeded unchanged. Try the Escape/refocus recovery once, then ask the user
  to right-click `StudioMCPBridge` → Execute StartBridge rather than looping.

**Rejected calls (1.0.9+).** Studio can refuse an Execute, and says so ONLY in
`FTOptixStudio.0.log`. After the click `optix_execute_method` reads what was appended
to that log: a rejection-looking line (reject / refused / denied / not allowed /
`Condition '...' failed`) returns `ok: false, state: "rejected"` with the line in
`log_line` (wrapped in `<untrusted>`), and the method did NOT run. Read more with
`optix_studio_log(lines=100, contains="Execute")` (last N lines, 1..5000, optional
case-insensitive filter). Errors are structured: `invalid_lines`, `invalid_contains`,
`no_studio_log`. `studio_log_checked: false` on an `executed` result means the log was
unreadable, so a rejection could not be ruled out.

`ok: true` (state `executed`) means the CLICK landed and no rejection was logged, never that the method worked — arbitrary
methods have no signal to verify against. Confirm with `describe_node` or a
render, and read Studio's Output pane for the method's own logging.

## Multi-instance hygiene

- Each Studio gets exactly one bridge (self-binds the first free port in
  **8768..8771**, so **four armed projects maximum**); re-arming a Studio returns
  its existing bridge. `/bridge/health` names its `studio_pid`, `project_name`
  and `project_dir`, and `optix_bridge_status` lists every bridge with the project
  it serves.
- With more than one bridge armed, **pass `project=` on every call**. Calls go to
  the bridge whose health names that project; if none does the call fails with
  `no_bridge_for_project` (listing the bridges found), if several do,
  `ambiguous_bridge_for_project`. `optix_active_target` still returns
  `{known: false, reason: "ambiguous_bridge"}` rather than guessing.
- `optix_bridge_arm(action="stop", project=...)` frees a port for another
  instance.
- Every open project can run its own emulator side by side, each on ITS OWN web
  port, read from the project (`WebPresentationEngine.Port`, serialized values like
  `8081 (UInt16)` parse to 8081) or the live bridge — not a fixed default. Project
  names are normalised (`X`, `X/HMI`, `x`, `X/` are one project). If two projects
  are configured for the same web port, `optix_emulator(action="run")` refuses
  BEFORE F5 with `web_port_taken` (`legacy_code: web_port_clash`, `runtime: none|zombie|running`) naming both projects and the port; change one
  project's port. A clash needs the port to be IN USE: two projects merely
  *configured* for the same port (nothing running) are not a clash and `run`
  proceeds. The refusal names only a project whose emulator is actually running;
  if a foreign listener holds the port it says so (`other_project: null`) - stop
  that listener or change the port.
- The emulator is started through Studio's own run path (F5 via the targeted-window
  helper for that Studio's pid), never by launching the runtime exe.
- Starts/stops done outside MCP (e.g. F5 in Studio) are noticed on the next
  emulator / `optix_status` / `optix_observe` call, which return
  `external_change: true` plus `external_changes` (started / stopped /
  project_changed). It is call-driven; there is no background daemon.
- Arming can fail transiently right after an open, while Studio indexes the
  tree: the NetLogic row shows 0 menu items instead of 14. Retry once before
  diagnosing anything. Going through `optix_project(action="open")` largely
  avoids this, because it waits on the window's project identity rather than on
  process existence or a sleep.

## Demo and reference projects

`optix_list_projects()` enumerates what is under the projects root. Reading a
**closed** project's YAML on disk is safe, free, and often the fastest way to
learn a construct the bridge cannot introspect — `AdvDynLink` carries working
StringFormatter dynamic links, for instance.

**Never edit project YAML on disk while that project is open in Studio.** Studio
owns the in-memory model and writes it back on save, so a concurrent on-disk
edit is clobbered or corrupts the project. That includes `git checkout` of a
tracked model file. To revert: undo through the bridge, or close Studio first
and only then restore the file.

Note the projects root is not the only place projects live — one may sit on the
Desktop or elsewhere, in which case `optix_list_projects` will not show it while
`optix_bridge_status` still names it. Resolve by the name the bridge reports.

## Traps

| symptom | cause | fix |
|---|---|---|
| `Cannot create a new project with invalid template: <T>` | the `-u` flag was missing from the CLI invocation, so Studio ran NewProject during `IDEContext::Initialize` with the user template library not yet indexed | fixed in 1.0.8; if it recurs, confirm the argv still carries `-u` alongside `--template` |
| same error, and argv looks right | template name passed WITH the `FTOptix_template_` prefix, or the template was hand-copied | pass the plain name; re-make via Save As -> tick Template |
| `FTOptixStudio new` "fails" with `0xC0000005` | the CLI always returns that as it tears down, successful runs included | judge by the `.optix` artefact, never the exit code |
| `studio_elevated_service_not` from arm | a Studio is running as administrator but the service is not, so its window is invisible to the service | start Studio without elevation, or run the service elevated (the hint says so); do not retry arm until one side changes. Elevation detection is pending bench verification |
| `studio_window_not_found` from arm | project not open (and no elevated Studio is hiding it - that is `studio_elevated_service_not`) | `optix_project(action="open")` first — arming cannot conjure a window |
| arm: NetLogic row has 0 menu items | Studio still indexing after open | wait ~30 s, retry once; then check the template actually has the `StartBridge` NODE |
| emulator `serving: false`, `probable_cause: target_or_modal` | a modal ate the F5, or the toolbar target is not Emulator | check `consent_cleared` in the result; then `optix_active_target` — the live UIA read, not the config file, which lags |
| `optix_save` / emulator start: `bridge_studio_no_window` (`NO_TARGET_WINDOW PID=<n>`) | the targeted Studio has no window the helper can use: minimized to tray, or closed | since 1.0.9 the focus helper no longer trusts `Get-Process` `MainWindowHandle` alone (it can be 0 while the window is visible); it falls back to `EnumWindows` for that exact pid, preferring the `Optix`-titled or largest unowned visible window, and never touches another Studio. `NO_TARGET_WINDOW` now means enumeration found nothing too: restore that Studio window and retry |
| emulator never serves, second project | 8088 already held | stop the other project's emulator |
| emulator `serving: false` forever, but the runtime IS up | the project's `UI/WebPresentationEngine` `Port` differs from the service's probe port | make them agree — read the engine's `Port` with `describe_node`, or point the probe at it |
| tools edit the wrong project | `project=` omitted with several bridges armed | always name the project |
| `execute_method`: `menu_items: 0` on every row | foreground contention (a running emulator window, another UIA driver) | stop the emulator; never run two gestures at once |
| `execute_method`: `menu_item_not_found`, chain missing the folder | node's methods not yet in the on-disk YAML | `optix_save`, then retry |
| `execute_method`: `error: "export_method_not_loaded"`, `reopen_required: true` | the live node has no `UAMethod` child of that name: a freshly bridge-created design-time NetLogic's `[ExportMethod]` is not loaded by `optix_save` alone and the bridge has no refresh trigger | reopen the project (then re-arm), retry; pass the NetLogic NAME, not a path, as `node` |
| `execute_method`: `state: "rejected"` | Studio refused the call; the reason is in `log_line` | read `optix_studio_log`, fix the cause, retry |
| a design-time method has no `Execute` entry | node was created by the bridge, not Studio's Add -> DesignTime NetLogic | recreate it via the GUI |
| a screen area renders blank though the model looks right | `UI/MainWindow` is 400x400 in a fresh project and clips everything below | set the window size before debugging the binding |
| file-path tools refuse a project the bridge happily serves | it lives outside `OPTIX_PROJECTS_ROOT`; `resolve_project` refuses | set the env var, or use bridge-only tools for it |
