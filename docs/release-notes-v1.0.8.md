# ftx-mcp v1.0.8 — release notes

Theme: **execute anything, link anything, reach anywhere — and never lose
the session doing it.** Design-time methods fire from MCP alone; formatted
dynamic links are bridge-authorable for the first time; projects outside
`projects_root` resolve; and a wave of reliability fixes closes the
silent-failure class that made a successful bridge call look like a no-op.

## Execute any design-time ExportMethod from MCP

**`optix_execute_method(project, node, method)`** generalises
`optix_bridge_arm` — where `arm` was hard-wired to one node and two verbs,
`execute_method` fires any `[ExportMethod]` on any design-time NetLogic
(generators, migration scripts, one-off builders, and the bridge's own
`StartBridge`/`StopBridge`). The gesture is UI Automation — the only safe
route, since dispatching off the bridge's HTTP thread crashes Studio.

- `ok` means the click landed; it does NOT mean the method succeeded.
  Arbitrary methods expose no port signal, so the result carries a nudge to
  verify by `describe_node` or a render.
- `studio_arm.execute_method(..., verify="none")` skips the port-based
  pre-checks so a method that produces no port signal doesn't time out in
  `verify_loop`.
- `studio_arm.clear_consent(project, ...)` — public entry point for
  answering Studio's security-warning modal outside the arm path.
- `optix_execute_method` was missing from `auth.TOOL_SCOPES` and fell
  through to the deploy default; it is now `"author"`, matching
  `optix_bridge_arm`.

**Known limitation:** `optix_execute_method` derives its tree chain from the
on-disk NetLogic YAML, so a newly created node needs an `optix_save` before
it can be found; the node lookup prefix-matches, so a stale `FooScript`
shadows `FooScript2`.

## Formatted dynamic links — the "indirect address" pattern

**`attach_formatter` op + `POST /bridge/node/attach-formatter`** attaches a
`DynamicLink` whose path is built at runtime by a `StringFormatter` from
NAMED sources (`{#name}` binds to a `Source<name>` child). This is the
indirect-address pattern — a `PanelLoader.Panel` selected by an integer with
the candidates as readable named variables on the loader, rather than opaque
NodeId GUIDs inside a `ValueMapConverter` — and equally the single-widget
`"{#value} {#eu}"` unit-label form (which `optix-expression-converter`
previously, incorrectly, documented as not bridge-authorable).

- Replaces whatever converter or link was already on the property;
  migrating off a `ValueMapConverter` needs no teardown step.
- Exposed as a batch op verb on `optix_bridge_edit`; the per-noun
  `optix_bridge_attach_formatter` primitive follows the usual
  `FTXMCP_BRIDGE_PRIMITIVES` gate.
- The false `unknown_op_field: name` warning that appeared on every
  `attach_expression` / `attach_formatter` call is fixed (`_normalize_edit_op`
  injected `name` internally and then the field check ran — now both ops
  accept it).

See the **`optix-formatted-dynamic-link`** skill for the rules
(`Source<name>` naming, the path living in `Format` not the link value,
`@NodeId` vs `@Pointer`), how to verify (a discriminating render, not
`describe_node`), and two bridge-authoring traps:
*(a)* assigning `StringFormatter.Format` via its property setter kills Studio
(corrupted-state exception, process vanishes, nothing logged) — write the
underlying `HasParameter` child instead;
*(b)* `SetModellingRuleRecursive()` is mandatory inside an ObjectType — omitting
it serialises `ModellingRule: None`, so the link exists at design time and is
absent at runtime.

## Invoke no longer takes Studio down (fixes #4)

Thanks to **[@Rempelino](https://github.com/Rempelino)** for issue #4, a
precise report with a stack trace and three suggested directions.

`POST /bridge/node/invoke` (`optix_bridge_invoke_method`) called
`ExecuteMethod` on the bridge's HTTP thread. When the invoked method threw
half-way through a node mutation (`panel.Remove(child); other.Add(child)`),
the bridge caught and logged the exception and Studio died anyway, losing
unsaved work. Studio's own right-click Execute survives the same throw.

The throw turned out to be a red herring. Invoking **any** method from the
bridge thread killed Studio, including one whose body does nothing, and
every crash was the same native heap corruption. The bridge thread had no
Optix session. Studio's right-click Execute survives because its GUI thread
has one; the managed code path is otherwise identical.

- The bridge now runs the call inside `Sessions.ImpersonateRootTemporary()`.
  The issue's throw-mid-mutation shape, UI creation from inside the method,
  and repeated calls all keep Studio up. The same calls without the session
  crash it every time.
- Exceptions the method throws are caught and logged by Optix, as with
  right-click Execute.
- `/bridge/health` advertises `invoke_session`. The service refuses invoke
  (`invoke_unsupported_bridge`) against a bridge that doesn't, because an
  older build still runs the call without a session. Rebuild the NetSolution
  with this release's `StudioMCPBridge.cs` and re-arm.
- `optix_bridge_invoke_method` accepts an `unsafe` parameter for compatibility;
  it has no effect.
- `optix_execute_method` (the UI Automation right-click) still works when
  you'd rather use Studio's own gesture.

## attach_string_formatter — format a display value

**`attach_string_formatter` op + `POST /bridge/node/attach-string-formatter`**
puts a `StringFormatter` on the PROPERTY itself (where `attach_expression` puts
its ExpressionEvaluator), so a Float renders `67.7` instead of `67.701805`.
Use it for decimal precision on labels; `attach_formatter` (above) builds a
formatted *link* and renders empty when used as a value formatter.

- `format` is a .NET composite string (`{0:F1}`); `sources` are positional
  (`Source0..N`), as a comma-separated string or a list.
- Every `{N}` needs a source. A missing or misspelled `sources` is refused with
  `unbound_placeholder` before anything is written, instead of attaching a
  formatter that renders the bare format text. `source` (singular) is accepted
  as an alias.
- `raw=1` attaches each source as a literal NodePath, so an alias path such as
  `{data}/value` attaches on a PanelType and resolves per instance.
- The `Format` parameter is written as a child node, never through the
  `.Format` property setter, which dereferences a null session on the bridge's
  HTTP thread and takes Studio down.

## Bridge C# reliability — bind, converters, Encoding, ObjectType statics

- **`bind` now calls `ResetDynamicLink()` first.** `SetDynamicLink` replaces
  a link's value but not a converter already hanging off it; binding a
  property that carried a formatted link left BOTH a new target and a stale
  `DynamicLinkFormatter` fighting over it — reported as a clean `applied: 1`.
  `ResetDynamicLink()` is Studio's own *Remove binding* step.
- **`bind` now reports both converter positions.** `ResetDynamicLink()` clears
  the link-level converter (where a `StringFormatter` lives) AND the
  property-level converter (where `attach_expression` puts an
  `ExpressionEvaluator`). Detection walked only the link, so binding over a
  formula destroyed it silently. Both positions are now inspected;
  `"prior binding cleared": {"converter": <kind>}` names what was removed.
- **`set_property` with `"null"` (or `""` on the raw HTTP route) clears a
  NodeId pointer.** The NodeId arm previously required the value to resolve
  to a node; there was no way to express "no target". A `NodePointer` driven
  by a dynamic link keeps the static `Value` as a fallback — invisible on
  screen but confidently wrong when the link resolves to nothing.
- **`set_property` materialises statics on ObjectType members** — consistent
  with how Studio writes them.
- **`StudioMCPBridge.cs` now uses `using Encoding = System.Text.Encoding;`**
  to resolve the `CS0104` ambiguity that appeared the moment a project added
  an EtherNet/IP driver (which injects `using FTOptix.RAEtherNetIP;` into
  every NetLogic, importing its own `Encoding` type). Without the alias the
  bridge failed to compile and took the in-process arm and emulator down
  with it — presenting as "bridge died", not a build error.
- **`BridgeVersion` is 1.0.8** — the bridge's behaviour has changed.
  Re-propagate `studio-bridge/StudioMCPBridge.cs` to every project copy and
  the bridge template; reload with StopBridge / StartBridge.

## Security-warning consent: tick "do not show again"

**`_click_consent` now ticks "Do not show this warning again" before
clicking Proceed.** Previously it clicked `Proceed` but never ticked the
checkbox, so arming consented and then the very next F5 raised the same
modal. The clicker walks the WHOLE popup before clicking anything (the
consent button is frequently reached before the checkbox in traversal order;
an early return would have dismissed the dialog with the box still unticked).

**`optix_emulator(action="run"/"restart")` now clears the consent modal
in-flight.** It detected the modal but never cleared it — burning the whole
`ready_timeout` and reporting `probable_cause: target_or_modal` with nothing
spawned. The focus-stealing click is gated behind `studio_uia.pending_dialog`
(a read-only walk that never takes the foreground), bounded to the first 25 s
and at most one clear. F5 is not resent — answering the dialog lets the
already-sent keystroke continue, and F5 toggles. The result carries
`consent_cleared`.

## Reach any project — out-of-root fallback

**A project open in Studio now resolves even when it lives outside
`projects_root`.** `resolve_project` previously only tried
`projects_root / <project>`, so a customer project on the Desktop answered
`project not found` from every path-dependent tool
(`optix_bridge_arm`, `optix_save`, `optix_build_check`, `optix_project`,
`optix_routes`, `optix_read_file`, …) while bridge-routed tools worked
because they route by the project the bridge REPORTS.

The fallback and its boundary, in order:

1. `..` is rejected before anything else.
2. The name is joined onto `projects_root` and canonicalized; anything not
   under the root is rejected (absolute names replace the root entirely and
   are never eligible for the fallback).
3. An existing in-root directory wins and costs zero extra work.
4. Only on a miss, and only for a BARE name (no path separator, no drive
   colon), the directory may come from a running `FTOptixStudio.exe` command
   line, and must exist and hold exactly one `.optix` file whose stem OR
   whose folder basename equals the name case-insensitively.
5. The caller's string is only ever COMPARED against that evidence — no path
   is ever taken from it.

Reuses `studio_guard`'s existing cached process snapshot (one
name-filtered psutil pass, 2 s TTL). The armed bridge's `/bridge/health` is
deliberately NOT used as a second source: `project` in the health response
is a name, not a path.

**`resolve_project_info(cfg, project) -> {"dir", "source"}`** surfaces which
source answered (`projects_root` or `studio_process`): visible in
`optix_status(action="doctor", project=…)`, in `optix_bridge_arm`'s result as
`resolved_from`, and in the `project not found` error as a breadcrumb naming
what Studio has open.

## retype — change a variable's DataType in place

**`retype` op + `POST /bridge/node/retype?path=&datatype=&dims=`** changes a
variable's DataType (and optionally its array shape) while keeping the node —
NodeId, children (DynamicLink, Mode, converters), and every inbound link
survive, which a delete+create cannot offer and `create_variable` cannot do
at all for arrays.

- The current value is converted element-wise: float→text via
  `ToString("R")` so `22.3f` becomes `"22.3"`, never `22.299999237060547`.
- Works on `StoreColumn` nodes too; columns behind a recipe array have
  browse names starting with `/`, which Studio's `Get` cannot spell — the
  route resolves parent + child name as a fallback (spell
  `.../Columns//Setpoints_0`).
- Tested in both directions; Studio stays up across calls with no reopen
  needed.

## wire_event — native UI commands, ObjectType-relative links, args

- **Native UI commands by name.** The bridge keeps a lookup of
  `UICommands` (`OpenDialog`, `CloseDialog`, `OpenKeyboard`), which live
  outside the project tree and were previously unreachable via `method_path`
  or a NodeId write. `args` carries their `InputArguments` by name; the
  bridge refuses a wrong argument set before creating the handler.
  `OpenKeyboard` takes THREE — `KeyboardType`, `TargetVariable`,
  `ParentItem` (per `Modules/FTOptix.UI/.../Module.xml`) — and a wrong
  count is a runtime "invalid number of arguments". Command names are matched
  case-insensitively, like event names.
- **Links authored INSIDE an ObjectType are now relative.** `attach_expression`
  and `attach_formatter` wrote their `Source{n}` dynamic links as absolute
  NodeIds; `wire_event` (method_path mode) wrote an absolute `ObjectPointer`.
  Every INSTANCE therefore read the TYPE's variable (an `Enabled` that never
  changed) or the runtime answered "No behaviour or observer found". `bind`
  was already right. The three sites now re-point the link relative to the
  nearest common type ancestor (`relative_sources` on attach results,
  `via: …+reltype` on wire_event); nothing changes outside a type.
- **`args` and `object_raw`** — `wire_event` now accepts `args`
  (`"Name=Value;…"` pairs, `;`-separated because node paths contain commas)
  and `object_raw` (a node path RELATIVE TO THE EVENT NODE, late-bound as
  a DynamicLink on `ObjectPointer`). Both are method-path only; `args`
  alongside a variable `command` is a hard error rather than a silent drop.
  Required for every UI-hosted method (`ChangePanel`, `ChangePanelByTabName`,
  `OpenDialog`): without `object_raw`, an absolute NodeId names the
  design-time node while the UI is instantiated per session under
  `UI/<engine>/Sessions/<id>/UIRoot/…` — `ChangePanel` wires without it,
  throws nothing, and silently does not navigate.
- **`wire_event` no longer silently no-ops when a handler already exists.**
  The bridge now has three modes: (1) **update in place** — the handler is
  found by `EventTypeName` match, its `args` and `ObjectPointer` fields are
  replaced; (2) `handler_exists` is returned when in-place mutation is unsafe;
  (3) pass `replace=true` to force delete-then-create regardless. `via` in the
  result ends with the outcome: `+existing` (updated in place), `+replaced`, or
  no suffix (created) — e.g. `eventhandler+args+lateobj+existing`.

## Batch editing — chunking, validator improvements, type alias

- **Batch chunking and timeout recovery (I18).** A batch that would stall
  an MCP client past its idle timeout now stops early and returns
  `state="chunked"` with a `batch_id`. Thresholds: `OPTIX_BATCH_CHUNK_SECONDS`
  (default 90 s) and `OPTIX_BATCH_CHUNK_OPS` (default 40 ops), overridable
  per call. Pass the returned `batch_id` to `action="continue"` to apply the
  next chunk, or `action="status"` to inspect the journal at any time.
  Per-op timings recorded as `op_timings` in
  `%LOCALAPPDATA%\ftx-mcp\batches\<batch_id>.json`, pruned after
  `OPTIX_BATCH_JOURNAL_DAYS` days (default 7).
- **The validator's hypothetical model follows `move` and predicts where a
  `create` lands.** Subsequent ops in the same batch can now target a moved
  or newly created node by its real path; `moved_earlier_in_batch` and
  `routed_into_collection` are reported with the actual path so the batch
  doesn't need to be split.  Three validator checks previously deferred to
  apply time now fail at validation: `read_only_collection`,
  `not_renderable`, and `ambiguous_container`.
- **`type` is now accepted as an alias for `create_widget`'s `widget_type`
  and `create_object`'s `object_type` (I32).** The raw HTTP routes spell the
  node type `type`; the batch op-spec required the per-noun spelling, so
  `type` was an unknown field, DROPPED, and the op applied the default
  (`Label` / bare `UAObject`) while reporting success. A `create_widget` with
  no type at all now carries a `default_widget_type` warning naming `Label`.
- **Destructive ops (`delete`/`move`/`reorder`) now hard-fail on an unknown
  field, regardless of `strict`.** An unrecognized field on a destructive
  op is very likely the caller trying to narrow the blast radius (e.g. `name:
  "AttachedPanelLoader"` on a `delete` was meant to clear one property, not
  the entire node). Previously `unknown_op_field` was only a warning and the
  op applied against the whole node.
- **`reorder` indexes graphic children only** (variables and sub-objects
  excluded) and is direction-independent; `achieved` in the result reports the
  final position.
- **`GridLayoutProperties` authored through the bridge now carries the
  `FTOptix.UI` browse-name qualifier**, the only one the engine honours.
  Previously the node was qualified with the project namespace and
  `RowStart`/`ColumnStart` were silently ignored.
- **The builtin UI type catalog resolves by browse name** when the compiled
  NodeId constant disagrees with the loaded module — fixing the
  `VirtualKeyboard*` block that was entirely unusable and causing
  `describe_type` to report another type's browse name.
- **`set_property name="Value"` on a variable node now validates, not just
  applies.** The bridge applier has recognised this form since 1.0.5; the
  batch validator did not, refusing it as `UAVariable has no settable property
  'Value'` while a variable created in the same batch was waved through.
- **Nudge added for pre-1.0.8 bridge compatibility** on `name="Value"` errors
  — a stale bridge returns `unknown_property`, the service appends the
  parent-node spelling as a correction.
- **`ValidateExpressionSyntax` rejects literals the runtime cannot parse.** `#RRGGBB` colour literals and capitalised `True`/`False` (Python
  f-string shape) now return `invalid_literal` with the correct rewrite
  (`0xAARRGGBB`; `true`/`false`) — the runtime logged a syntax error on every
  affected node while the validator returned `valid: true`.

## Per-project web-engine port resolution (I28)

`optix_observe`, `optix_interact`, and the emulator readiness probes now
derive the CDP/runtime port from the project's own `WebPresentationEngine`
node (via the armed bridge or an offline YAML parse) rather than the
machine-global `OPTIX_RUNTIME_TEST_PORT`.

Resolver priority: explicit `port=` → project node → `OPTIX_RUNTIME_TEST_PORT`
(operator override only) → default 8081. **Self-heal:** when the expected port
is dead but the project's emulator is alive on a different port, the resolver
re-derives and follows — one loud log line and a `port_followed` field. The
2026-08-25 "web presentation engine is down" false-positive on a multi-port
box (a second runtime serving on 8088, service expecting 8081) recovers without operator
intervention.

`optix_status(action="doctor", project=…)` gains a `web_port` row:
`project_says` / `service_expects` / `listening` (pid + process name, so
an Ignition-owned squatter is named).

## Emulator and studio-guard reliability

- **`optix_emulator` project attribution (I30) and structured status (I23).**
  `emulator_status` previously matched any `FTOptixRuntime.exe
  --application-name=Emulator` process; `action="status" project="Alpha"`
  could report Beta's runtime as Alpha's — and a subsequent `action="run"`
  could send F5 to the wrong Studio window. The process scan is narrowed by
  the `\Emulator\Projects\<Project>\` cache-path component (exact,
  case-insensitive; `Line4` never matches `Line4_HMI`). `action="run"` and
  `action="restart"` refuse with `runtime_project_mismatch` when any running
  emulator's path identifies a different project. `action="status"` gains a
  structured `state` field (`stopped`/`starting`/`running`/`zombie`/
  `demo_expired`); `last_lifecycle_line` surfaces the last runtime log entry.
- **`move` of a NetLogic node is now refused.** Moving a NetLogic closed the
  project in Studio (window back on Home, bridge gone, unsaved edits lost, no
  exception). The bridge refuses it before any mutation
  (`refused_netlogic_move`) and the batch validator's dry run catches it too.
- **`optix_project(action="new", template=…)` can now create from a template.**
  The Studio CLI invocation omitted `-u`, so the user template library was
  not indexed during `IDEContext::Initialize` and every template name failed
  `Cannot create a new project with invalid template`.
- **`optix_bridge_arm` no longer fails on unsaved projects (I34).** Studio
  appends `*` to its window title while there are unsaved edits; `_studio_window_for`
  compared the caption verbatim and answered `studio_window_not_found` on any
  project with pending edits (every bridge-edited project is unsaved by
  definition until saved). The marker is now tolerated on both ends of the
  text for both the root-row name match and the path-prefix match.
- **`optix_build_check` path rebasing is no longer Windows-only.** MSBuild
  diagnostic paths are always Windows paths; comparing them with the host's
  `Path` on a POSIX box never matched the throwaway-copy prefix. Compared as
  `PureWindowsPath` on every host now.
- **Stale `bridges/<port>.json` no longer suppresses the range-scan fallback.**
  A registry file left from a killed Studio was the sole entry; the scan
  probed only that dead port, returned empty, and unlinked it — making an
  armed project appear unserved for that single call. Registry ports and
  the configured range are now probed together in one concurrent pass, so
  a dead registry entry can't hide a live bridge, and neither can a live
  one: a project still running a pre-1.0.8 bridge (which writes no registry
  file) stays visible alongside upgraded ones.
- **I31 — a bad enum value on `SpinBox.ValueChangeBehaviour` took Studio down.**
  The bridge listener's `accept` and per-request handler shared one try, so a
  client hanging up mid-response ended the listener for the rest of the session.
  That is split. The crash itself was the last arm of the enum coercion: an
  invalid member fell through to a bare-string assign whose failure is a native
  assert (uncatchable). The enum is now read off the owner's CLR proxy so
  invalid members return the `bad_value` + member list every other enum already
  did.
- **`I35` — `describe_node` reported a bare ObjectType as `ScreenType`.**
  `dotnet_type` is now corroborated against the node's real base chain.
- **`services.ps1 start -Silent` raw-failed on an elevation-mismatched
  scheduled task (I27).** `Set-ScheduledTask` returned CIM `Access is denied`
  when the task was Admins-owned but `-Silent` ran from a regular shell.
  `Set-ServiceConsoleMode` now catches the permission-denied case and reports
  both remedies. `setup.ps1` also gained `-AllowElevated` and refuses to run
  elevated without it.

## Also in this release

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
- **`create_variable` datatypes: six more, and no silent Boolean.**
  `Byte`, `SByte`, `LocalizedText`, `Range`, `Color` and `ResourceUri` are now
  creatable; an unsupported datatype name fails loud with the list of 18
  supported names instead of silently creating a `Boolean`.
- **`optix_read_netlogic(project, cls)`** — read a NetLogic `.cs` source by
  class name from `ProjectFiles/NetSolution`, path-traversal safe, size-capped,
  `<untrusted>`-wrapped. Unlike `optix_read_file` it works while Studio is open
  (the NetSolution lives on disk). Default tool count 33 → 35 (this and
  `optix_execute_method`).
- **`optix_interact` normalized-coordinate conversion applied to all actions.**
  The ≤ 1.0 fraction convention was documented but wired only into the
  route-replay path; direct `click` and `fill` passed raw floats to CDP,
  so `(0.5, 0.25)` landed at pixel (0, 0). All click and fill paths now call
  `_resolve_point`; the result gains `css_x`, `css_y`, `viewport`, and
  `coords` fields. Out-of-viewport coordinates return `bad_point`.
- **`settle_seconds` now honoured unconditionally on every interact action.**
  Previously it fired only when `navigate_url` was also supplied.
- **New skills:** `optix-project-lifecycle` (cold-starting a project
  hands-off, security warning, design-time methods, multi-instance port
  hygiene), `optix-formatted-dynamic-link`, `optix-anchored-edit`,
  `optix-web-click-surface` (Button-only click surface in the web engine).
- **Stale tool names corrected in `optix-netlogic-and-bridge` and
  `optix-anchored-edit`** and a new tree-wide tool-name lint in
  `test_skills_content.py` asserts that every `optix_*` token in every
  `skills/*/SKILL.md` names a real tool.

## Known limitations

- **`optix_bridge_create_netlogic` cannot create a DESIGN-TIME NetLogic.**
  The node it mints compiles and exposes its `[ExportMethod]`s, but Studio
  offers no `Execute` entries for it — `optix_execute_method` can never fire
  them. Design-time scripts still require one manual Studio step
  (`NetLogic -> Add -> DesignTime NetLogic`).
- `optix_execute_method` derives its tree chain from on-disk YAML (needs
  `optix_save` first) and prefix-matches node names.

## Contributors

Thanks to **[@Rempelino](https://github.com/Rempelino)** for issue #4 (above).

Thanks to **[@insightfool](https://github.com/insightfool)** for three fixes in
this release: destructive batch ops hard-failing on unknown fields (found the
hard way, on a live project), the expanded `create_variable` datatype set, and
isolating the F5-guard tests from a developer box's real Studio configuration.

## Upgrading

`pip install --upgrade ftx-mcp` updates the service.

**The bridge changes require re-deploying `StudioMCPBridge.cs`** — paste the
new `studio-bridge/StudioMCPBridge.cs` into the `StudioMCPBridge` NetLogic node,
then StopBridge / StartBridge. `optix_bridge_status` should report bridge
version **1.0.8** on every armed port. Re-deploy to every project copy and the
`BridgeTemplate` project; otherwise the `attach_formatter` endpoint will 404,
the Encoding ambiguity will reappear on EtherNet/IP projects, and `bind` will
leave stale converters.

`BridgeVersion` moves to 1.0.8. A project you have already deployed keeps
reporting the old value on its `:8765/ui` dashboard until its
`StudioMCPBridge.cs` is recompiled and StopBridge/StartBridge'd.
