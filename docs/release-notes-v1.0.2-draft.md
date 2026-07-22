# ftx-mcp v1.0.2 — draft release notes

Security and field-report fixes. Thanks to first-time contributor
**@PlantwideIntegration** for five reviewed, tested fixes.

## Security

- **Per-tool auth scope is now enforced at the MCP dispatch site**
  (@PlantwideIntegration). With `FTX_AUTH_REQUIRED=true`, a read-scoped
  token could previously invoke write/destructive MCP tools — the MCP
  transport only required "read" while the HTTP twins correctly required
  "deploy". MCP tool dispatch now mirrors the HTTP route scopes; unknown
  tools fail closed to "deploy". Loopback auth-off default unchanged.
  If you run LAN mode with issued tokens, upgrade.

## Fixes

- **Live-model tools now work for projects opened outside `projects_root`**
  (@PlantwideIntegration). Studio can open a project from anywhere; the
  bridge served it, but every live-model tool refused with "bridge not
  serving <project>". Name-based fallback keeps both guards.
- **Deploy-lock acquire is atomic** (@PlantwideIntegration) — O_EXCL
  create closes a TOCTOU where two concurrent deploys could both pass the
  lock. (Deploy is dormant in this distribution; fix is forward-looking.)
- **Edit engine rejects multi-mode edit requests; runtime-stop matching is
  anchored to a directory boundary** (@PlantwideIntegration). Both
  forward-looking (deploy dormant), both regression-tested.
- **services.ps1 now reaps orphaned CDP chrome.** Reinstalling re-registers
  the chrome-cdp task, orphaning the old task's chrome — `stop` then
  appeared to do nothing while :9222 stayed held. Stop/start now identify
  our chrome by its dedicated profile dir (never by port alone) and handle
  already-running / foreign-holder cases explicitly.
- **Windows test portability** (@PlantwideIntegration): studio-guard
  fixtures pin LF so the suite passes on Windows checkouts.

## Improvements

- **Boot banner now prints the UI dashboard URL and CDP status**, e.g.
  `cdp ok (http://127.0.0.1:9222, drivable page)` or a clear
  "not running — canvas verify unavailable" line with the start command.
  Answers "will verify work?" without a support round-trip.

## Notes

- Suite: 522 passed (Linux), 2 Windows-only skips.
- No breaking changes; default loopback/no-auth behavior unchanged.
