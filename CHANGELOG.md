# Changelog

All notable changes to ftx-mcp. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/). Per-release detail lives in
`docs/release-notes-v<version>.md`.

## [Unreleased]

### Fixed
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
