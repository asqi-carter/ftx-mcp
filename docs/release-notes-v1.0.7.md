# ftx-mcp v1.0.7 — release notes

The largest release since 1.0.0, consolidating three development streams.
Version note: **1.0.6 was never published** — its change (blocking the
DisplayName crash) ships here. Internal work-in-progress labels up to
"1.0.11" existed during development; the released version is **1.0.7**
everywhere (service, bridge, package, registry).

Theme: **many Studios, one service — and the bridge grows up.** The
design-time bridge goes multi-instance, gets armed and pointed at projects
from MCP alone, refuses a whole class of crash- and corruption-shaped
requests, and the emulator loop gets an order of magnitude faster.

## Multi-instance design-time bridge

The bridge no longer exclusively owns `:8768`. `StartBridge` binds the first
free port in `8768..8771`, so up to 4 Studio instances can be armed at once,
each on its own port (`OPTIX_BRIDGE_PORT_BASE` / `OPTIX_BRIDGE_PORT_RANGE`
configure the range; `OPTIX_BRIDGE_URL` still pins a single explicit bridge
and skips scanning).

- Every bridge-routed tool (`optix_describe_node`, `optix_bridge_edit`,
  `optix_find`, `optix_save`, `optix_emulator`, …) resolves the SPECIFIC
  bridge serving the `project` you name, instead of assuming there is one.
- `optix_bridge_status` returns `bridges` (one entry per answering port,
  with project, version, reason) plus `count`.
- F5/Ctrl+S targeting: with a bridge armed per project, every emulator/save
  keystroke resolves the exact Studio PID that owns that project's bridge —
  the old "first focus-able Studio window" fallback is no longer reachable
  for armed projects. (This was the root cause of "started the emulator for
  the wrong project.")
- `optix_active_target` takes an optional `project`; with several bridges
  armed and no project given it refuses with `ambiguous_bridge` +
  `armed_projects` instead of silently reading the lowest port. The
  "default project" convenience does the same — explicit `project=` is
  required once more than one bridge is up.
- The port-range scan is concurrent, goes straight to the real
  `/bridge/health` HTTP check (a raw-socket pre-probe was aborting live
  listeners' sockets and spamming Studio's Output panel — removed), and
  short-circuits definitively-refused ports so an empty range scans fast.
- `/ui` dashboard: a socket chip per configured port — armed chips show
  `:port vVERSION project` with last-saved age on hover; a distinct amber
  "loading…" state for a bridge that answered but hasn't finished loading
  the model; Doctor emits one `bridge :PORT` row per configured port (the
  single-port `OPTIX_BRIDGE_URL` mode keeps the legacy `bridge` row name);
  every Doctor row now carries its detail + fix as a hover tooltip; a
  thread-safety fix stops duplicated tool chips under concurrent polls.
- Known limitation: `OPTIX_STUDIO_GUARD_MODE=attributed` (opt-in) still
  requires exactly one running Studio; the default `blanket` guard is
  unaffected and protects every project regardless of instance count.

## DisplayName: crash class closed, then made to work

Setting `DisplayName` via `set_property` used to terminate Studio
(`0xC0000005`, unsaved edits lost): UA node **attributes** (`DisplayName`,
`BrowseName`, `Description`, …) passed the property guard's CLR check, got
materialized as orphan UA variables, and Studio's renderer dereferenced the
orphan off-thread.

- **Guard fixed at both ends:** the bridge's `DeclaredPropertyGuard` now
  requires an `FTOptix.*`-declared property (the same test the legend uses),
  across `set_property`, `bind`, `attach_expression`, and batch validation;
  attribute names get a targeted `node_attribute_not_settable` error. The
  service refuses the same shapes before dispatch, so a stale (older)
  bridge can never receive a crash-capable request.
- **`DisplayName` is now settable** through a dedicated attribute route
  (`/bridge/node/displayname`): a direct `LocalizedText(value, locale)`
  attribute write, never the variable-materialization path. Works standalone
  and inside `optix_bridge_edit` batches.
- **New `rename` op:** `{"op":"rename","path":…,"new_name":…}` — sugar that
  lowers to `move` with the node's own parent, the only mutation pattern
  proven safe against the re-parenting crash class. The renamed node gets a
  new NodeId (outbound links re-created; inbound references elsewhere are
  not rewritten). `BrowseName` remains rename-only; binding or expressing
  an attribute is still refused.
- Studio labels a tree node `BrowseName (DisplayName)` when the two differ
  (verified live and documented).

## Arm the bridge and manage projects from MCP

- `optix_bridge_arm`: arms (or stops) the design-time bridge for a project
  with no human at the keyboard — it drives Studio's own UI to execute
  StartBridge, walking a fully collapsed project tree (chevron expansion via
  the Project-view search; indent measured from tree rows only) and routing
  by the project's **BrowseName** as well as its folder name, so projects
  whose window title differs from the directory arm correctly.
- `optix_project`: open/create Studio projects from MCP. Studio's CLI verbs
  are GUI launches, so readiness is detected by the window's UIA identity
  rather than process exit.
- `optix_bridge_log_tail`: the bridge's transport diagnostics as a tool —
  the forensic view when a bridge drops mid-edit (paired with post-rebuild
  drop detection and a recovery nudge server-side).
- `optix_build_check`: compile the project's NetSolution C# against an
  isolated temp copy (with a stale-references hint), without touching the
  working tree.
- NetLogic registration via the bridge, and sync tools moved off the event
  loop into worker threads so bursts don't stall the service.

## Emulator lifecycle: seconds, not tens of seconds

Every emulator status/stop/restart used to spawn PowerShell/CIM processes
(up to 7 per restart, 2–4 s each; slow scans could starve the HTTP pool and
hang `/health`). All process discovery is now in-process psutil:

- `emulator_status`: 2–4 s → ~0.15 s warm; restart overhead 10–20 s →
  sub-second; `/health` hang → 7 ms.
- The command-line discrimination is unchanged: only
  `--application-name=Emulator` processes count; UpdateSvc-deployed
  runtimes (same exe) are never touched.
- A psutil warm-up runs at service boot so the first scan doesn't pay the
  cold process-map cost.

## Also in this release

- **`optix_bridge_invoke_method`** — a generic `ExecuteMethod` wrapper for
  exported NetLogic methods (its own `/bridge/node/invoke` endpoint).
  Confirmed hazard: some Studio built-ins (e.g. `SearchBrokenDynamicLinks`)
  can crash Studio when invoked off the UI thread — treat calls as
  crash-capable until main-thread marshaling lands.
- **Nested project directories** are discoverable and addressable —
  `list_projects` walks recursively (capped depth) and `resolve_project`
  accepts subfolder paths; escape protection stays with the post-resolve
  `is_relative_to` boundary.
- **`describe_node` no longer shows populated dynamic-link/alias paths as
  empty strings** (`ValueString` unwraps `UAValue.Value` first) — these
  read as false "broken link" reports before.

## Correctness and resilience

- **Batch validation hardening:** an op carrying an unknown FIELD (e.g. a
  typo'd key) now refuses the whole batch with `unknown_op_field` naming
  the field and op index — previously it applied the op anyway and
  reported success.
- **Deploy verification** no longer loses a genuinely-succeeded deploy to
  filesystem clock granularity.
- **Bridge transport:** transient write failures retry with backoff;
  liveness is probed over HTTP (never a bare TCP connect — see the socket
  storm above); a missing bridge NetLogic is named distinctly and checked
  early.
- **Windows service UX:** one launcher with `--hide-console`;
  `services.ps1 start -Silent`; a console-mode change recycles a running
  service; `__main__.py` no longer drops CLI flags; the 72-hour Task
  Scheduler execution limit is removed and service crashes land in a
  lifecycle log.

## Upgrading

> Superseded, see [1.0.9 Upgrading](release-notes-v1.0.9.md#upgrading).

`pip install --upgrade ftx-mcp` updates the service. The bridge changes
require re-arming: paste the new `studio-bridge/StudioMCPBridge.cs` into the
`StudioMCPBridge` NetLogic node, then StopBridge/StartBridge (once per
Studio instance you want bridged). `optix_bridge_status` should report
bridge version **1.0.7** on every armed port.

If you run more than one project's **emulator** simultaneously (not just
bridging them), pair each project with its own Web-presentation-engine port
via `optix_bridge_ensure_web_engine(port=…)` — the web port is a project
setting, and two projects both on the default will collide. The service's
own expectation of the runtime port is `OPTIX_RUNTIME_TEST_PORT`
(default 8081).

## Credits

The multi-instance bridge and the dashboard's multi-port evolution are
Chuck's work, hardened here through a live multi-project field arc. The
DisplayName crash investigation and fix, the `rename` op, and the psutil
emulator-lifecycle speedup came from Jonathan Callahan (@nahallac) via PRs
#2 and #3. Thanks also to the contributors whose fixes and field reports
shaped this release.

The default tool surface is now **33 tools** (`optix_bridge_arm`,
`optix_project`, `optix_bridge_log_tail`, `optix_build_check` are new);
gate env vars can add more. See `docs/tool-reference.md`.
