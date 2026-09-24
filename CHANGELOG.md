# Changelog

All notable changes to ftx-mcp. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/). Per-release detail lives in
`docs/release-notes-v<version>.md`.

## [1.0.8]

- **`optix_bridge_invoke_method` / `POST /bridge/node/invoke` no longer kill
  Studio — fixes #4, reported by @Rempelino.** Root cause: `ExecuteMethod`
  ran on the bridge's HTTP thread with no Optix session, and native code
  dereferencing the missing session corrupted the heap (`c0000374`) on ANY
  method, not only one that throws mid-mutation; even reading
  `Sessions.CurrentSessionInfo` on that thread is fatal. The bridge now wraps
  the call in `Sessions.ImpersonateRootTemporary()` (the pattern in the
  FactoryTalk-Optix NetLogic_CheatSheet `sessions.md`). `DelayedTask`,
  `LongRunningTask` and `IContext.Dispatch` never fire at design time, and
  `Sessions.CurrentSessionHandler` is not implemented there, so root
  impersonation is the design-time option. `/bridge/health` advertises
  `invoke_session`; the service refuses invoke against a bridge without it
  (`invoke_unsupported_bridge`). `unsafe` is accepted for compatibility and has no effect.

- **Bridge: `attach_string_formatter` — format a display value.** New op / route
  (`POST /bridge/node/attach-string-formatter`) attaches a `StringFormatter` as a
  converter ON THE PROPERTY (HasConverter on the property itself, where
  `attach_expression` puts its ExpressionEvaluator), so a Float renders `67.7`
  instead of `67.701805`. This is the shape a hand-built Studio label uses and is
  distinct from `attach_formatter`, which builds a formatted dynamic LINK (the
  StringFormatter formats a NodePath) and renders EMPTY as a value formatter
  (field finding 2026-09-01, re-confirmed 2026-09-16). `Format` is written via
  the HasParameter child, never the `.Format` property setter: that setter reads
  `Context.Sessions.CurrentSessionInfo`, which is null on the bridge's HTTP
  thread, so it AVs and kills Studio with nothing logged (decompiled
  `FTOptix.CoreBase.Net` 2.1.0.89). Sources are positional (`Source0..N`, so
  `{0}`/`{1}` in the format); `raw=1` attaches each source as a literal NodePath
  so an alias/template path like `{data}/value` attaches on a PanelType and
  resolves per instance. Proven live on `string_formatter` 2026-09-16: a
  bridge-authored formatter on `Label.Text` rendered `3.14` from `{0:F2}` over a
  Float 3.14159, clean runtime log, no crash. Every `{N}` in `format` must have a
  bound source: a missing or misspelled `sources` is refused with
  `unbound_placeholder` before anything is written (previously the formatter
  attached with no `Source0`, reported `succeeded`, and rendered the bare format
  text). The response carries `placeholders` and `bound` counts.

- **Batch ops: `sources` accepts a list, and `source` is an alias.** On
  `attach_expression`, `attach_formatter` and `attach_string_formatter` a JSON
  list is joined into the delimited string the bridge reads (`,` positional,
  `;` for `attach_formatter`'s `name=path` pairs; a mapping is accepted there
  too). Lists used to be stringified to `"['Model/X']"` and fail
  `source_not_variable`. `source` (singular) is folded into `sources` instead of
  being dropped as an unknown field; both present and different is an error.

- **Bridge: non-UI builtin types are creatable.** `create_object` (`object_type`),
  `create_type` (`base_type`) and `create_widget` now resolve a bare or
  `Module.Type` name against EVERY loaded `FTOptix.<Module>.ObjectTypes` catalog
  (step (a2) in `ResolveUiTypeId`) and, failing that, walk the whole builtin type
  tree without the UI-namespace filter (step (b2), project namespace excluded);
  `create_object` tries the builtin catalogs before a project type path.
  Measured 2026-09-13 on a live project: `OffNormalAlarmController`,
  `ExclusiveLevelAlarmController`, `NonExclusiveLevelAlarmController`,
  `ExclusiveRateOfChangeAlarmController`, `AlarmController`, `DataLogger`,
  `User`, `Group`, `SQLiteStore`, `ODBCStore`, `Recipe`, `RecipeSchema`,
  `Report` all create and read back. Still `type_not_found` on that project:
  `DigitalAlarm`, `EmbeddedDatabase`, and the library widgets `AlarmGrid`,
  `LoginForm`, `LoginButton`, `LogoutButton`, `AlarmBanner` — not in any loaded
  catalog, consistent with Studio library templates that are COPIED into a
  project on drag rather than builtin ObjectTypes; add them from the Library
  panel once, then address them by project path.

### Added
- **`optix_read_netlogic(project, cls)`** — read a NetLogic `.cs` source by
  class name from `ProjectFiles/NetSolution`, path-traversal safe, size-capped,
  `<untrusted>`-wrapped, read-only. Unlike `optix_read_file` it works while
  Studio is open, because the NetSolution lives on disk and not in Studio's
  model (the VS / VS Code attributed-editor guard still applies). Default tool
  count 33 → 35 (with `optix_execute_method`); the documented counts in
  `README.md` and `docs/tool-reference.md` are checked by a test against the
  surface a default install registers.
- **Per-project web-engine port resolution (I28; supersedes I24).** `optix_observe`,
  `optix_interact`, and the emulator readiness probes now derive the CDP/runtime
  port from the project's own `WebPresentationEngine` node — via the armed bridge
  or an offline parse of `Nodes/UI/UI.yaml` — rather than the machine-global
  `OPTIX_RUNTIME_TEST_PORT`. Resolver priority: explicit `port=` parameter →
  project node → `OPTIX_RUNTIME_TEST_PORT` (operator override only) → default 8081.
  A project with no engine node returns `source="no_engine"` with a nudge naming
  `optix_bridge_ensure_web_engine`. **Self-heal (I28 rule 2):** when the expected
  port is dead but the project's emulator is alive, the resolver re-derives and
  follows — one loud log line and a `port_followed` field in the tool result, so
  the 2026-08-25 "web presentation engine is down" false-positive on a multi-port
  box (a second runtime serving on 8088, service expecting 8081) recovers without operator
  intervention. **Doctor row (I28 rule 3):** `optix_status(action="doctor",
  project=…)` gains a `web_port` row: `project_says` / `service_expects` /
  `listening` (pid + process name, so an Ignition-owned squatter is named), plus
  the runtime log's bind-fail signature surfaced as
  `web_engine_bind_failed {port, holder}` with remedies.
- **`wire_event` native UI commands by name — `OpenDialog`, `CloseDialog`,
  `OpenKeyboard`.** The bridge keeps a table of the builtin `UICommands` it
  resolves from the `/Objects` root (they live outside the project tree, so
  neither `method_path` nor a NodeId write could reach them), and `args`
  carries their InputArguments by name. `OpenKeyboard` takes THREE —
  `KeyboardType`, `TargetVariable`, `ParentItem` (per
  `Modules/FTOptix.UI/.../Module.xml`; a wrong count is a runtime "invalid
  number of arguments") — and the bridge refuses a wrong set BEFORE it
  creates the handler. Command names are matched case-insensitively on the
  service side, like event names. The standalone `optix_bridge_wire_event`
  tool now exposes `args` and `object_raw` too (they were reachable only
  through the batch op).
- **Links authored INSIDE an ObjectType are now relative.** `attach_expression`
  and `attach_formatter` wrote their `Source{n}` dynamic links as absolute
  NodeIds, and `wire_event` (method_path mode) wrote an absolute
  `ObjectPointer`, so every INSTANCE read the TYPE's variable (an `Enabled`
  that never changed) or the runtime answered "No behaviour or observer
  found". `bind` was already right because its property is parented before
  `SetDynamicLink` runs. The three sites now re-point the link relative to
  the nearest common type ancestor (`relative_sources` on attach results,
  `via: …+reltype` on wire_event); nothing changes outside a type.
- **`wire_event` gains `args` and `object_raw` — the two things a UI-hosted
  method needs before it does anything.** Found while authoring navigation handlers over HTTP: without
  arguments only argument-LESS methods were reachable, which excludes every
  method a converted navigation action calls (`ChangePanel(NewPanel)`,
  `ChangePanelByTabName(TabName)`, `OpenDialog(Dialog, AliasNode)`). `args`
  takes `"Name=Value;..."` pairs — `';'`-separated because node paths routinely
  contain commas — and a value that resolves to a node becomes a `NodePointer`
  argument (a bare NodeId wires but never resolves at invoke time); anything
  else becomes a String. `object_raw` late-binds the call TARGET: a literal
  NodePath RELATIVE TO THE EVENT NODE (e.g. `"../Loader"`), written as a
  DynamicLink on `ObjectPointer` with the levels up from `ObjectPointer`
  prepended for the caller. It is required for every UI-hosted method, because
  an absolute NodeId names the DESIGN-TIME node while the UI is instantiated
  per session under `UI/<engine>/Sessions/<id>/UIRoot/...` — `ChangePanel`
  wired without it resolves, throws nothing, and silently does not navigate.
  Both are method-path only; `args` alongside a variable `command` is a hard
  error rather than a silent drop. `via` reports `eventhandler[+args][+lateobj]`.
- **`wire_event` `command="OpenDialog"` / `"CloseDialog"`.** UI commands live on
  `/Objects/Commands/UICommands`, OUTSIDE the project root, so `ResolveNode`
  (`Project.Current.Get`) cannot reach them and a `method_path` of
  `"Commands/UICommands/OpenDialog"` answered `node_not_found`. They are now
  resolved by path from `Project.Current.Owner` — the same absolute pointer a
  Studio-authored handler carries — and exposed as a `command`, the way
  `SetVariable`/`ToggleVariable` are. Unlike those, a UI command takes its
  arguments from `args`: `"Dialog=<Dialog type path>;AliasNode=<node or
  empty>;ParentItem="`, where an empty `ParentItem` is late-bound to the event
  node, as Studio does.
- **A project that is OPEN IN STUDIO now resolves even when it lives outside
  `projects_root`.** `resolve_project` only ever tried
  `projects_root / <project>`, so a customer project on the Desktop
  (`C:\Users\<u>\Desktop\Line4\HMI`) answered `project not found` from
  every one of the ~29 call sites — `optix_bridge_arm`, `optix_save`,
  `optix_build_check`, `optix_project`, `optix_routes`, `optix_read_file`, ...
  — while every bridge-routed tool worked with it perfectly, because those
  route by the project the bridge REPORTS rather than by a path. The fallback
  and its boundary, in order: `..` is rejected first; the name is joined onto
  `projects_root` and canonicalized, and anything not under the root is
  rejected there (that is what catches an absolute name, since joining an
  absolute path replaces the root entirely) — an absolute name is therefore
  never eligible for the fallback; an existing in-root directory wins and
  costs ZERO extra work (no process scan runs at all); only on a miss, and
  only for a BARE name (no separator, no drive colon — a subpath never takes
  this path), the directory may come from a running `FTOptixStudio.exe`
  command line, and must exist and hold exactly one `.optix` file whose stem
  OR whose folder basename equals the name case-insensitively. The caller's
  string is only ever COMPARED against that evidence — no path is ever taken
  from it. Reuses `studio_guard`'s existing cached process snapshot (one
  name-filtered psutil pass, 2s TTL) rather than adding a second scanner, and
  caches nothing of its own.
  The armed bridge's `/bridge/health` is deliberately NOT used as a second
  evidence source: `HealthJson()` returns `{bridge_version, project,
  model_loaded, port}`, where `project` is `Project.Current.BrowseName` — a
  NAME, not a path — so a bridge can confirm a project is open but cannot say
  where it lives. Giving it a path field is a future bridge-version change.
- **`resolve_project_info(cfg, project) -> {"dir", "source"}`** — the sibling
  of `resolve_project` that reports WHICH source answered (`projects_root` or
  `studio_process`). Surfaced by `optix_status(action="doctor", project=...)`
  (and `GET /doctor?project=`) as one non-required `project` row, and by
  `optix_bridge_arm`'s result as `resolved_from`, so "it resolved, but only
  because Studio is holding it" is visible instead of silent. The
  `project not found` error now carries a `resolved_from` breadcrumb naming
  `projects_root` and, when the fallback ran, the project NAMES Studio has
  open.
- **`optix_bridge_edit` batch chunking and timeout recovery (I18).** A batch that would otherwise stall an MCP
  client past its idle timeout now stops early and returns
  `state="chunked"` with a `batch_id`. Thresholds: `OPTIX_BATCH_CHUNK_SECONDS`
  (default 90 s) and `OPTIX_BATCH_CHUNK_OPS` (default 40 ops); both
  overridable per call via `chunk_seconds` / `chunk_ops` parameters.
  Pass the returned `batch_id` to `action="continue"` to apply the next chunk,
  or to `action="status"` to inspect the journal at any time (omit `batch_id`
  for the most recent N batches on the project). `applied` counts ops landed
  across chunks, readable the same way as `state="partial"`. Per-op timings
  are recorded as `op_timings` (`{index, op, ms, ok}` per applied op) in the
  journal at `%LOCALAPPDATA%\ftx-mcp\batches\<batch_id>.json`, pruned after
  `OPTIX_BATCH_JOURNAL_DAYS` days (default 7). MCP progress notifications are
  best-effort and silently absent without a `progressToken` — the journal is
  the authoritative record.

### Fixed
- **`SpinBox.ValueChangeBehaviour` (and any property only the .NET proxy
  declares) is refused, not fatal.** Any write to it — a valid ordinal too —
  killed Studio with an AccessViolation inside `GetOrCreateVariable`: the proxy
  declares the property, the loaded UA type chain does not. The bridge now
  materializes a property only when an ObjectType in the node's supertype chain
  declares it, and answers `proxy_only_property` otherwise. (The I31 entry
  below fixed the invalid-member path; this was a second, earlier crash site.)
- **A same-named non-project folder no longer shadows the open project.** With
  `projects_root` at the Desktop and a project at `Desktop\Line4_HMI\HMI`,
  the folder `Desktop\Line4_HMI` (no `.optix`) won resolution and every tool
  got the container. A bare name whose in-root folder holds no `.optix` now
  lets the open-in-Studio / registry match answer first.
- **`optix_bridge_edit` results carry per-op `detail`** — `via` (wire_event),
  `relative_sources`, `achieved` (reorder), `prior binding cleared` (bind).
  They were computed by the bridge and dropped by the batch loop.
- **`gridlayout_without_columns` warning.** A GridLayout's `Rows`/`Columns`
  cannot be set over the bridge, and one in a rendered window blanks the
  whole web page; batches creating one now say so.
- **Integer `Value` writes keep their declared width.** `set_property
  name="Value"` cast every integer DataType to 32 bits, so `Int64`/`UInt64`
  values wrapped silently and reported success (`123456789012345` stored as
  `-2045911175`). Values are now parsed at the variable's width, and an
  out-of-range value (`70000` on `Int16`, `-1` on `UInt64`) is refused as
  `bad_value` at validation, before anything is written.
- **`wire_event` silently no-oped when the event node already had a handler.**
  The bridge wrote the op and returned `ok: true`, but the existing handler was
  left unchanged — args and object pointer from the original wiring stayed in
  place and the new ones were discarded. The fix has three modes: (1) **update
  in place** — the handler is found by `EventTypeName` match, its `args` and
  `ObjectPointer` fields are replaced from the op, and the updated handler is
  read back; `via` in the result now ends with the outcome (`+existing` for an
  in-place update, `+replaced`, or no suffix when created). (2) `handler_exists`
  is returned (with the conflicting handler's details) when in-place mutation is
  unsafe — e.g. the existing handler is not the one the op expects or the bridge
  cannot identify it unambiguously. (3) Pass `replace=true` to force a
  delete-then-create regardless: the old handler is removed first and a fresh
  one written, guaranteeing the result matches the op exactly.
- **I30 / I23 — `optix_emulator` project attribution and structured status.**
  On a multi-Studio box, `emulator_status` previously matched any
  `FTOptixRuntime.exe --application-name=Emulator` process, so
  `action="status" project="Alpha"` could report Beta's runtime as Alpha's —
  and a subsequent `action="run"` or `action="restart"` could send F5 to the
  wrong Studio window. The process scan is now narrowed by the
  `\Emulator\Projects\<Project>\` cache-path component of the runtime command
  line (exact, case-insensitive, not a substring so `Line4` never matches
  `Line4_HMI`). When `project=` is given the status result gains
  `project_resolved` (the name extracted from the path) and `matched_path` as
  attribution evidence. `action="run"` and `action="restart"` refuse with
  `{"error": "runtime_project_mismatch", "requested": …, "resolved": …,
  "pid": …, "nudge": …}` when any running emulator's path identifies a
  different project (I30 — correct project attribution via cache path).
  `action="status"` also gains a structured `state` field
  (`stopped`/`starting`/`running`/`zombie`/`demo_expired`) alongside the
  existing `running` bool (kept for back-compat). `demo_expired` is derived
  from the emulator's per-project lifecycle log and wins over all other
  conditions; `zombie` distinguishes a process that is up with a visible window
  but no port listener from one that is still initialising. `last_lifecycle_line`
  surfaces the last runtime log entry for diagnostics (I23 — structured state
  field on emulator_status).
- **I31 — a bad enum value on `SpinBox.ValueChangeBehaviour` took Studio
  down.** Two things, both in the bridge: (1) the listener's `accept` and the
  per-request handler shared one try, so a client hanging up mid-response
  ended the listener for the rest of the session — split, with a catch-all
  that logs and continues; (2) the crash itself was the last arm of the enum
  coercion: when the datatype's browse name did not match a loaded enum type,
  an invalid member fell through to a bare-string assign whose failure is a
  NATIVE assert (uncatchable). The enum is now read off the owner's CLR proxy
  (`DeclaredEnumType`), so invalid members return the same `bad_value` +
  member list the other 21 enum properties already did. Handler exceptions
  report `type: message` in the 500 payload.
- **`move` of a NetLogic node closed the project in Studio** — window back on
  Home, bridge gone, unsaved edits lost, no exception (field finding
  2026-09-04). The bridge now refuses it before any mutation
  (`refused_netlogic_move`), in the batch validator's dry run too; move
  NetLogic nodes in Studio's Project view.
- **I35 — `describe_node` reported a bare ObjectType as `ScreenType` (its
  instance as `Screen`).** `dotnet_type` is now corroborated against the
  node's real base chain (SuperType for a type, ObjectType for an instance)
  and says `BaseObjectType` / `BaseObject` when that is what it is; Screen,
  Panel, Folder and Window types are unchanged.
- **`BridgeVersion` is 1.0.8** — the bridge's behaviour changed; re-propagate
  `studio-bridge/StudioMCPBridge.cs` to every project copy and the template.
- **`optix_build_check` path rebasing was Windows-only.** MSBuild diagnostic
  paths are always Windows paths; comparing them with the host's `Path` on a
  POSIX box never matched the throwaway-copy prefix (the one test the Linux
  suite had to deselect). Compared as `PureWindowsPath` on every host now.
- **`optix_bridge_arm` could not find an UNSAVED project's window.** Studio
  appends `*` to the project name and path it shows in its own in-scene chrome
  while there are unsaved changes (`C:\Users\<u>\Desktop\NavArgDemo*`), and
  `_studio_window_for` — which identifies a Studio window by exactly that text,
  the Win32 caption being the same for every instance — compared it verbatim.
  So `arm` answered `studio_window_not_found` on any project with pending
  edits, which reads exactly like "Studio is not open" and cost a session three
  wrong theories before one Ctrl+S made the same call succeed. Every
  bridge-edited project is unsaved by definition until someone saves, so this
  hit constantly. The marker is now tolerated on both ends of the text, for the
  root-row name match and the path-prefix match alike (I34).
- **`type` is now accepted as an alias for `create_widget`'s `widget_type`
  and `create_object`'s `object_type` in a `bridge_edit` batch (I32).** The
  raw HTTP routes spell the node type `type`
  (`/bridge/ui/widget?...&type=PanelLoader`), and HTTP-first
  callers follow them; the batch op-spec
  spelled it per-noun only. Through `bridge_edit`, `type` was an unknown
  field — a WARNING under the default non-strict mode — so it was DROPPED
  and the op's own default applied: a `Label` (or a bare `UAObject`) under
  the right name in the right parent, with the batch reporting
  `state: succeeded`. A wrong-typed node passes every subsequent model read;
  only a render reveals it. `_normalize_edit_op` now folds `type` into the
  canonical field and removes the key (so the unknown-field guard stays
  quiet about a field the service itself consumed), and RAISES when both
  spellings are present and name different types rather than guessing.
  Prior art: gap-register **B4** (2026-08-06) tabulated the same field-name
  table, but recorded the wrong spelling as *rejected* — which holds only
  for REQUIRED fields; an OPTIONAL field with a default succeeds instead,
  which is the half worth fixing. Relatedly, a `create_widget` with no type
  at all now carries a `default_widget_type` warning in the report naming
  the `Label` default, so the remaining silent default is visible without a
  render.
- **Destructive ops (`delete`/`move`/`reorder`) now hard-fail on an unknown
  field, regardless of `strict`.** These verbs have no scoped/partial form —
  there is no "delete just this property" op — so an unrecognized field
  (e.g. a `name` meant to scope a `delete` down to one property) is very
  likely a caller trying to narrow the blast radius, not a harmless extra.
  Previously `unknown_op_field` was only a warning under the (default)
  non-strict mode, and the op still applied against the whole node at
  `path`. Live incident: a `delete` op on a `NavigationPanel` carrying
  `name: "AttachedPanelLoader"` deleted the entire node instead of clearing
  the one property, recovered only via the caller's own Ctrl+Z in Studio —
  the bridge has no undo. `bridge_edit` also no longer mutates the report
  dict returned by `bridge_validate_ops` in place (it was a shared/caller
  object in some paths — surfaced as cross-test pollution while adding
  regression coverage for this fix).
- **`StudioMCPBridge.cs` stopped compiling in any project with an EtherNet/IP
  driver.** Adding a comm driver makes Studio inject
  `using FTOptix.RAEtherNetIP;` into EVERY NetLogic `.cs` in the solution, and
  that namespace exports its own `Encoding` — colliding with `System.Text` on the
  bridge's four `Encoding.ASCII/UTF8` call sites
  (`CS0104: 'Encoding' is an ambiguous reference`). Since a NetLogic that fails to
  compile takes the in-Studio bridge and the emulator down with it, this presents
  as "the bridge died", not as a build error. Fixed with a using alias
  (`using Encoding = System.Text.Encoding;`) rather than qualifying the call
  sites, so it survives Studio re-injecting its usings and covers usages added
  later.
- **`bind` left the previous binding's converter attached.** `SetDynamicLink`
  replaces a link's *value* but not a converter already hanging off it, so
  binding a property that carried a formatted dynamic link produced a property
  with BOTH a new target and a stale `DynamicLinkFormatter` fighting over it —
  reported as a clean `applied: 1`. `bind` now calls `ResetDynamicLink()` first,
  which is Studio's own right-click -> *Remove binding* step and what
  `attach_formatter` already did. Measured on a Label's `Text` (2026-09-01).
- **`bind` destroyed property-level converters while reporting it cleared
  nothing.** `ResetDynamicLink()` clears BOTH converter positions — link-level
  (`HasConverter` on the DynamicLink, where a `StringFormatter` lives) and
  property-level (`HasConverter` on the property, where `attach_expression` puts
  an `ExpressionEvaluator` via `SetConverter`). Detection walked only the link,
  so binding over a formula destroyed it and returned
  `"prior binding cleared": {"converter": false}` — an affirmative all-clear over
  a real loss. Both positions are now inspected and the response names the
  `converter kind` that was removed. Both the source and raw branches report it
  (the raw branch initially reported nothing at all).
- **`optix_project(action="new", template=...)` could never create a project
  from a template.** The Studio CLI invocation omitted `-u`, so Studio ran
  `NewProject` during `IDEContext::Initialize` with the *user* template library
  not yet indexed and the lookup failed
  `Cannot create a new project with invalid template: <name>` out of
  `TemplateLibrary::GetModelNodeFromPath` — for a template name the Studio GUI
  resolves fine. Verified by running identical argv with and without the flag.
- **Studio's NetLogic security warning was raised again on every trigger.**
  `_click_consent` clicked `Proceed` but never ticked *"Do not show this warning
  again"* (there was no checkbox/toggle handling anywhere in the service), so
  arming consented and then the very next F5 hit the identical modal. The
  clicker now ticks the box first. It also walks the whole popup **before**
  clicking anything: the consent button is frequently reached before the
  checkbox in traversal order, and the previous early-return would have
  dismissed the dialog with the box still unticked.
- **`optix_emulator(action="run"/"restart")` detected that modal but never
  cleared it**, burning the whole `ready_timeout` and reporting
  `probable_cause: target_or_modal` with nothing spawned. It now clears the
  dialog in flight. The focus-stealing click is gated behind
  `studio_uia.pending_dialog` (a read-only walk that never takes the
  foreground), so a normal build is never interrupted; bounded to the first 25 s
  and at most one clear. **F5 is not resent** — answering the dialog lets the
  already-sent keystroke continue, and F5 toggles. Reports `consent_cleared`.
- **I27: `services.ps1 start -Silent` raw-failed on an elevation-mismatched
  task.** If `setup.ps1` had been run elevated, the scheduled task was
  Admins-owned, so a later `-Silent` from a regular shell died inside
  `Set-ServiceConsoleMode` at `Set-ScheduledTask` with a raw CIM `Access is
  denied` (0x80070005) — plain `start` still worked (no write needed), so
  the service just ran console-visible and the failure read as "-Silent is
  broken." `Set-ServiceConsoleMode` now catches the permission-denied case
  (by HRESULT and by message, to survive both a bare `CimException` and an
  `UnauthorizedAccessException`) and fails with the real cause plus both
  remedies (re-run `-Silent` elevated once, or uninstall elevated + re-run
  `setup.ps1` from a regular shell to re-register non-elevated).
  `setup.ps1` also gained `-AllowElevated`: it now refuses to run elevated
  at all unless that switch is passed, instead of only warning.
- **The batch validator's hypothetical model follows move and predicts
  where a create lands.** The validator now tracks `move` and `rename` ops
  applied earlier in the batch and resolves subsequent references against
  the relocated path (`moved_earlier_in_batch` error names the `new_path`;
  the old path no longer resolves). `create_widget` into a screen with a
  placeholder collection is resolved the same way `WriteWidgetInline`
  routes it at apply time: the actual created path is reported in the
  `routed_into_collection` warning (`routed_path`), so subsequent
  `set_property`/`bind`/`reorder` ops in the same batch can target the
  real path without splitting the batch. Three validator checks previously
  deferred to apply time are now reported as validation errors:
  `read_only_collection` (runtime-managed collection), `not_renderable`
  (`Item`/`Container` abstract types), and `ambiguous_container` (multiple
  matching placeholder collections under the parent).
- **`reorder` indexes graphic children and verifies where it landed**. `index` counts among graphic children only
  (variables and sub-objects are excluded from the count) and is
  direction-independent; the `achieved` field in the result reports the
  final position regardless of which direction the node moved.
- **`GridLayoutProperties` authored through the bridge carries the
  `FTOptix.UI` browse-name qualifier, the only one the engine honours**. Previously the node's browse name was qualified
  with the project namespace; `RowStart`/`ColumnStart` were silently ignored
  by the grid engine and `reorder` was the only positioning workaround. What
  is namespaced is the QUALIFIED BROWSE NAME (Studio writes
  `Name: ns=<FTOptix.UI>;GridLayoutProperties`), not the NodeId — every
  instance node lives in the project namespace, so a NodeId-namespace check
  can only ever fail. The bridge re-reads `QualifiedBrowseName` after the
  write and returns `wrong_namespace` rather than `ok: true` if it did not
  take.
- **The builtin UI type catalog resolves by browse name when the compiled
  NodeId constant disagrees with the loaded module — the VirtualKeyboard*
  block was unusable and `describe_type` answered with another type's
  browse name**. The resolver walks the builtin
  TYPE TREE (a type node's `Owner` is its SUPERTYPE, so subtypes are found by
  descending from the topmost type root, and the walk is widened to the
  `Types` folder so event types under `BaseEventType` are reachable too);
  `list_ui_types` / `describe_type` / `optix_schema` all report
  `resolved_by:"browse_name"` on a corrected entry. `misaligned` /
  `catalog_misaligned` now flag only what the walk could NOT reconcile — a
  constant NAMED differently from the node it correctly points at, such as
  `UserValueChangedEvent` for the ObjectType browse-named `UserValueChanged`.
- **A stale `bridges/<port>.json` from a killed Studio suppressed the
  range-scan fallback for one call, making an armed project appear unserved.**
  A registry file left behind after a killed Studio — for example from the CI
  teardown race — was the sole entry in the `bridges/` directory;
  `_scan_bridge_ports` probed only that (dead) port, returned empty, and
  unlinked the stale file, so a project armed on any other port read as
  unserved for that call. The stale file was gone by the time the next call
  ran, the empty registry fell through to the range scan, and the bridge
  resolved — which is why the failure appeared intermittent. Registry ports
  and the configured range are now probed together in one concurrent pass:
  the registry adds out-of-range ports and stale-entry cleanup but is never
  authoritative, because a pre-1.0.8 bridge writes no registry file and a
  registry-only answer hid it. The suite also pins `%LOCALAPPDATA%` to a temp
  dir, so running it on a box with armed bridges no longer reads (or deletes)
  the real registry. The `bridge not serving '<project>'` refusal
  previously named the serving project of the FIRST bridge in port order
  (from `bridge_state()`, unrelated to what was actually probed); it now
  lists `ports_probed` and each port's served project.

### Added
- **`set_property` can now CLEAR a NodeId** — pass `"null"` (or `""` when calling
  the bridge directly; the MCP layer rejects an empty string as a missing field).
  The NodeId arm previously required the value to resolve to a node, so there was
  no way to express "no target". A `NodePointer` driven by a dynamic link keeps
  whatever static `Value` the designer last set: invisible on screen (the link
  wins) but misleading in the property grid, and the **fallback if the link ever
  resolves to nothing** — a confidently wrong panel instead of an empty one.
  A navigation loader in a field project read as pointing at a recipe page. Verified that
  clearing it does not disturb the link-driven render.
- **`optix_execute_method(project, node, method)`** — right-click -> Execute any
  design-time `[ExportMethod]`, not just the bridge's `StartBridge`/`StopBridge`.
  The generalisation of `optix_bridge_arm`, which was this hard-wired to one
  node and two methods. Lets a project's own generators/builders and one-off
  migration scripts be fired from the agent instead of by hand. Uses the same
  UI Automation gesture — which is the ONLY safe route, since invoking a
  design-time method in-process on the bridge's HTTP thread runs it off the UI
  thread and crashes Studio. `ok` means the click landed, NOT that the method
  succeeded: arbitrary methods expose no signal to verify against, so the
  result carries a nudge to confirm by `describe_node` or a render.
- `studio_arm.execute_method(..., verify=...)` — `"none"` skips the port-based
  pre-checks and verify loop. Without it, any method other than
  `StartBridge`/`StopBridge` was judged by a bridge-port signal it never
  produces and reported `verify_timeout` on a successful run. `verify=None`
  (the default) derives `arm`/`stop` exactly as before, so the arm path is
  bit-for-bit unchanged.
- `studio_arm.clear_consent(project, ...)` — public entry point for answering
  Studio's security warning outside the arm path, with the same cursor and
  foreground restore as `execute_method`.
- **`optix-project-lifecycle` skill** — cold-starting a project hands-off
  (new-from-template -> open -> arm), making and verifying a project template,
  what the security warning actually is, running a project's own design-time
  methods, multi-instance port hygiene, and reading demo/reference projects
  safely.
- **`retype` op + `POST /bridge/node/retype`** — change a VARIABLE's DataType
  in place (optionally its array shape) while keeping the node: NodeId,
  children (DynamicLink, Mode, converters) and every inbound link survive,
  which delete+create cannot offer and `create_variable` cannot do at all for
  arrays. The current value is converted element-wise, float→text through
  `ToString("R")` so 22.3f becomes "22.3", never 22.299999237060547. Works on
  StoreColumns too; columns behind a recipe array have browse names that
  start with "/", which Studio's `Get` cannot spell, so the route resolves
  parent + child name as a fallback (spell `.../Columns//Setpoints_0`).
  Studio survives every call; no reopen is needed to load the new bridge code
  on a project whose NetSolution rebuilds cleanly.
- **`attach_formatter` op + `POST /bridge/node/attach-formatter`** — attach a
  **formatted dynamic link** to any property: a DynamicLink whose path is built
  at runtime by a StringFormatter from **NAMED** sources (`{#name}` binds to a
  `Source<name>` child), as opposed to `attach_expression`'s positional
  `Source0..N` on an ExpressionEvaluator. This is the "indirect address"
  pattern — a `PanelLoader.Panel` selected by an integer with the candidates as
  readable named variables on the loader instead of opaque NodeId GUIDs inside
  a `ValueMapConverter` — and equally the single-widget `"{#value} {#eu}"`
  unit-label form, which `optix-expression-converter` previously (wrongly)
  documented as not bridge-authorable. Replaces whatever converter or link was
  on the property, so migrating off a `ValueMapConverter` needs no teardown.
  Exposed as a batch op verb; the per-noun `optix_bridge_attach_formatter`
  primitive follows the usual `FTXMCP_BRIDGE_PRIMITIVES` gate.
- **`optix-formatted-dynamic-link` skill** — the pattern, the rules
  (`Source<name>` naming, index suffixes matching the driver's values, the path
  living in `Format` and never in the link's own value, `@NodeId` vs
  `@Pointer`), how to verify it (discriminating render, not `describe_node`),
  and two traps for anyone editing the C#:
  **(a)** assigning `StringFormatter.Format` via its property setter from the
  bridge kills Studio — a corrupted-state exception .NET Core cannot catch, so
  the process vanishes with nothing logged; write the underlying `HasParameter`
  child instead. **(b)** `SetModellingRuleRecursive()` is mandatory: without it
  nodes serialize `ModellingRule: None`, and a `None` member is not
  instantiated into instances of its owning ObjectType — so on a `ScreenType`
  the link is present at design time and absent at runtime, which reads exactly
  like a mis-wired converter. No thread marshaling is needed; `DelayedTask`
  does not help, as a design-time NetLogic's behaviour context is not running.
- **New skill for a silent-failure pattern: `optix-web-click-surface`.** It
  covers the Button-only click surface for web CDP tests — Label, Rectangle,
  Image, and ScreenType root do not fire `MouseClickEvent` in any
  configuration measured; an `Opacity=0` Button is invisible and fires.

### Fixed (continued)
- **`unknown_op_field: name` warned on every `attach_expression` call.**
  `_normalize_edit_op` injects `name` alongside `prop_name` to reconcile the
  C#-validator/Python-applier seam, and the field check runs after it — so the
  service warned about a field it had just added itself. Now accepted for both
  `attach_expression` and `attach_formatter`; genuine typos are still caught.
- **`set_property name="Value"` on a variable node validates, not just applies**. The bridge applier (`SetPropertyInline`) has
  recognised `name="Value"` as the variable's own OPC-UA Value attribute since
  1.0.5, but the batch validator did not — so a correct op was refused as
  `UAVariable has no settable property 'Value'` and the whole batch with it,
  while a variable created in the same batch was waved through as
  `unverifiable_property`. Both halves now agree: `set_property` on a variable
  node with `name="Value"` is accepted and applied cleanly. The equivalent
  spelling from the parent — `path=<parent>, name=<VariableName>` — remains
  supported and is unchanged.
- **Nudge added for pre-1.0.8 bridge compatibility on `name="Value"` errors.**
  A bridge older than 1.0.8 does not support the `name="Value"` self-assign
  spelling and returns `unknown_property` / `UAVariable has no settable property
  'Value'`. The service now detects this response (both on the direct
  `bridge_set_property` path and when augmenting batch-validator error reports)
  and appends a correction pointing the caller at the parent-node spelling —
  `path=<parent>, name=<VariableName>` — so the LLM caller sees the fix without
  a separate `describe_type` round-trip.
- **`optix_interact` normalized-coordinate conversion applied to all
  click/fill actions, not just route replay.** The `<= 1.0` fraction
  convention was documented for `optix_interact` but was only wired into the
  route-replay (`navigate`) path; direct `click` and `fill` calls passed raw
  floats straight to CDP, so `(0.5, 0.25)` landed at pixel (0, 0) instead of
  the centre of the first quadrant. All click and fill paths now call
  `_resolve_point` against the session's `Page.getLayoutMetrics` CSS viewport.
  The success result gains four fields: `css_x` and `css_y` (the resolved CSS
  pixel coordinates actually dispatched), `viewport: {"w": …, "h": …}` (the
  CSS viewport against which they were resolved), and `coords`
  (`"normalized"` when both inputs were ≤ 1.0, `"absolute"` otherwise).
  Out-of-viewport coordinates return `state: "failed", error: "bad_point"`.
- **`settle_seconds` now honoured unconditionally on every interact action.**
  Previously the post-action settle sleep was triggered only when a
  `navigate_url` was also supplied; a plain `click`, `fill`, `type`, `key`,
  or `screenshot` call with an explicit `settle_seconds` silently ignored it.
  `_navigate_if_given` no longer sleeps; each action's caller applies the
  settle unconditionally after dispatching its input event, so the wait fires
  exactly once per call regardless of whether navigation preceded it.
- **Stale tool names corrected in `optix-netlogic-and-bridge` and
  `optix-anchored-edit`.** `optix-netlogic-and-bridge` referenced
  `optix_cdp_screenshot` (now `optix_observe(mode="screenshot")`) and
  `optix_restart_emulator` (now `optix_emulator(action="restart")`), both
  renamed in the 1.0.4 CDP consolidation. `optix-anchored-edit` referenced
  `optix_add_converter` and `optix_add_panel_widget`, which have no successor
  tools; the sentences were rewritten to describe the operations via tools that
  exist. A new tree-wide tool-name lint in `service/tests/test_skills_content.py`
  asserts that every `optix_[a-z_0-9]+` token in every `skills/*/SKILL.md` body
  is a member of `EXPECTED_TOOLS ∪ _BRIDGE_PRIMITIVE_TOOLS`, keeping these
  correct going forward.

- **`ValidateExpressionSyntax` now rejects literals the runtime ExpressionEvaluator cannot parse.**
  `attach_expression` returned `ok:true` and `validate_expression` returned `valid:true` for expressions
  containing `#RRGGBB` colour literals, while the runtime logged
  `Invalid 'Expression' parameter, Syntax error at 13` on every affected node — for example a generated
  `if({0} == 2, #FFF809, 0xFFF0F0F0)` on a button's `BackgroundColor`. A literal-lint table is added to the existing scanner in
  `ValidateExpressionSyntax`: (1) a `#`-prefixed hex token outside a string literal → `invalid_literal` with
  the `0xAARRGGBB` rewrite and the converted value in the message (`#FFF809` → `0xFFFFF809`); (2) a
  capitalised `True`/`False` (Python f-string shape) → lowercase fix. All three callers share the validator —
  `ValidateExpJson` (`:5706`), the `ValidateExpression` ExportMethod (`:162-175`), and
  `AttachExpressionInline`'s gate (`:1387`) — so the batch validator, standalone `validate_expression` tool,
  and `attach_expression` are all corrected in the same change. `invalid_literal` surfaces unmodified through
  `bridge_validate_expression` and through `bridge_edit` report entries.

### Known limitations
- **`optix_bridge_create_netlogic` cannot create a DESIGN-TIME NetLogic.** The
  node it mints compiles, binds its class, and exposes its `[ExportMethod]`s to
  `optix_describe_node` — but Studio offers **no `Execute` entries** for it, so
  `optix_execute_method` can never fire them. The distinguishing marker is
  `BehaviourStartPriority`. Design-time scripts still need one manual Studio
  step (`NetLogic -> Add -> DesignTime NetLogic`), which is the second
  unscriptable step after making a project template.
- `optix_execute_method` derives its tree chain from the on-disk NetLogic YAML,
  so a newly created node needs an `optix_save` before it can be found; the node
  lookup prefix-matches, so a stale `FooScript` shadows `FooScript2`.

## [1.0.7]

The largest release since 1.0.0, consolidating three development streams.
1.0.6 was never published; internal work-in-progress labels up to "1.0.11"
existed during development — everything below ships as **1.0.7**. Full
notes: `docs/release-notes-v1.0.7.md`.

### Added
- **Multi-instance design-time bridge.** Up to 4 Studio instances armed
  simultaneously — `StudioMCPBridge.cs` self-binds the first free port in
  `8768..8771`; every bridge-routed tool resolves the specific bridge
  serving the `project` you name. Fixes "emulator started for the wrong
  (non-bridged) project": with a bridge armed per project, the "first
  focus-able Studio window" fallback is no longer reachable.
- **`DisplayName` is settable** via a dedicated attribute route, and a new
  **`rename` op** lowers to the proven-safe `move` machinery. The crash
  class behind them (node attributes materialized as orphan UA variables →
  Studio access violation) is refused at both bridge and service.
- **`optix_bridge_arm`** (arm/stop the bridge with no human at the
  keyboard, collapsed-tree + BrowseName aware), **`optix_project`**
  (open/create from MCP), **`optix_bridge_log_tail`** (transport
  forensics), **`optix_build_check`** (isolated NetSolution compile check).
- **`optix_bridge_invoke_method`** — generic `ExecuteMethod` wrapper for
  exported NetLogic methods. Confirmed hazard: some built-in methods (e.g.
  `SearchBrokenDynamicLinks`) can crash Studio — treat as crash-capable
  until the bridge marshals `ExecuteMethod` to the main thread.
- `/ui` dashboard: socket chip per configured port with per-port version +
  last-saved age, a distinct "loading" state, per-port Doctor rows with
  hover detail/fix tooltips.

### Fixed
- **Emulator lifecycle is now in-process psutil** — status 2-4s → ~0.15s,
  restart overhead 10-20s → sub-second, `/health` no longer starves under
  slow scans. The `--application-name=Emulator` discrimination is
  unchanged; deployed runtimes are never touched.
- **Batch validation refuses unknown op FIELDS** (`unknown_op_field`)
  instead of applying the op and reporting success.
- **Deploy verification** no longer loses a succeeded deploy to filesystem
  clock granularity.
- **Port-range scan hardening:** no raw-socket pre-probe against live
  listeners (was aborting the C# bridge's accept loop and spamming
  Studio's Output panel), definitive refusals skip the retry sleeps,
  per-port checks run concurrently, and the widget-type catalog is not
  requested when no bridge is armed.
- **`/ui` tool catalog no longer duplicates under concurrent polls**
  (double-checked locking around the lazy build).
- **Nested project directories resolve and list** — a project moved into a
  subfolder is discoverable (recursive `list_projects`, capped depth) and
  addressable; the security boundary remains the post-resolve
  `is_relative_to` check.
- **`describe_node` no longer reports populated dynamic-link/alias paths
  as empty** — `ValueString` unwraps `UAValue.Value` before falling back
  to `ToString()`.
- **Console model finalized:** one launcher (`python.exe`) with
  `--hide-console` / `OPTIX_HIDE_CONSOLE`, `services.ps1 start -Silent`
  rewrites the task action (and recycles a running service on a mode
  change), and every Windows child spawn defaults to `CREATE_NO_WINDOW` —
  no console flashes, real stdout/stderr kept. (An interim windowless-
  launcher approach from the 1.0.6-era stream was replaced by this.)
- Bridge transport: transient write retries with backoff, HTTP-only
  liveness probes, distinct missing-NetLogic diagnosis, post-rebuild drop
  detection with a recovery nudge; the 72-hour Task Scheduler execution
  limit is removed and service crashes land in a lifecycle log.

### Changed
- `optix_active_target()` (and the omit-`project` convenience everywhere)
  refuses to guess with several bridges armed — explicit `project=` is
  required once more than one is up.

## [1.0.5]

Theme: an installer that installs. Full notes: `docs/release-notes-v1.0.5.md`.

### Fixed
- **Fresh installs were broken.** `mcp>=1.2` had no upper bound, so a clean
  `pip install ftx-mcp` resolved MCP Python SDK 2.0.0 (published 2026-07-28),
  which removes `mcp.server.fastmcp`; the server failed at import. Pinned to
  `mcp>=1.2,<2`. Existing installs were unaffected — an already-resolved
  environment still satisfied the old range, so neither `setup.ps1` (which
  reuses `.venv`) nor a plain reinstall re-resolved it.
- **`services.ps1 restart` crashed under `Set-StrictMode`** when the CDP
  chrome had just been killed (#1, reported and diagnosed by @Jraa01). A
  listening socket outlives its process, so `OwningProcess` can name a dead
  pid; reading `.CommandLine` off a `$null` CIM result is a terminating
  error. Guarded at all three call sites (`services.ps1`, `uninstall.ps1`,
  `setup.ps1`); `setup.ps1` deliberately still fails on an unidentifiable
  port holder rather than skipping it.
- **Documented tool counts were wrong.** The default surface is 28 tools;
  `README.md` and `docs/tool-reference.md` both said 37, predating the 1.0.4
  CDP consolidation.

## [1.0.4]

Theme: authoring you can trust, and an agent that spends fewer tokens getting
there. Full notes: `docs/release-notes-v1.0.4.md`.

### Added
- `optix_bridge_edit` — batched, validate-then-apply authoring (U16). A whole
  op list is validated against a hypothetical model before a single node is
  written; `dry_run=true` pre-flights. Not atomic by design (`state="partial"`
  on mid-batch failure).
- Generic enum coercion in the Studio bridge: friendly enum values
  (`FontWeight="Bold"`, `VerticalAlignment="Bottom"`) resolve by reflection
  with an `"Enum"`-suffix strip, falling back to known ordinals.
- Screenshot device-metrics override — `OPTIX_CDP_VIEWPORT` / `OPTIX_CDP_SCALE`
  (fit-to-content capture; supersedes Chrome `--window-size` for capture size).
- Env gates for surface trimming: `FTXMCP_LEGACY_TOOLS=1` (restore 10 CDP
  aliases), `FTXMCP_SKILLS=0` (drop skill tools), `FTXMCP_BRIDGE_PRIMITIVES=1`
  (restore per-noun bridge primitives).

### Changed
- CDP surface consolidated to `optix_observe` / `optix_interact`; the legacy
  `optix_cdp_*` aliases are OFF by default (gated behind `FTXMCP_LEGACY_TOOLS=1`).
- Emulator/status/schema/routes tools consolidated behind action dispatch:
  `optix_emulator(action=...)`, `optix_status(action=...)`,
  `optix_schema(action=...)`, `optix_routes(action=...)`. The old
  `optix_run_emulator` / `_restart_emulator` / `_stop_emulator` /
  `_emulator_status` / `_runtime_log_tail` names were replaced with NO
  deprecated alias — update any scripts to the `action=` form.
- Authoring skills lead with a single `optix_bridge_edit` batch instead of one
  call per property. Folded the standalone `optix-known-pitfalls` skill into
  `optix-expression-converter` and `optix-verify-loop`.

### Fixed
- HorizontalAlignment / VerticalAlignment ordinals corrected
  (Bottom/Right=1, Center=2, Stretch=3) — non-WPF order, live-verified.
- `attach_expression` in a batch reconciles its property-name field
  (`name` ↔ `prop_name`).
- Connect-time `instructions` no longer point at a tool absent from the default
  surface; in-tool hints reference the consolidated tool names.

### Security
- Per-tool scope hardening and installer hardening — see release notes.

## [1.0.3]
See `docs/release-notes-v1.0.3.md`.

## [1.0.2]
See `docs/release-notes-v1.0.2.md`.
