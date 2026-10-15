# ftx-mcp runbook

Operational procedures for the current build. See `docs/troubleshooting.md` for failure-mode lookup.

## First-session walkthrough

Fresh Windows 11 box, FactoryTalk Optix Studio already installed. ~15 min.

**Prerequisites:** Windows 11, signed in as the user who will own the service (not Administrator — DPAPI state binds to this account); Studio installed; the repo (or release tarball) at a path of your choosing.

1. **Install.**
   ```powershell
   cd C:\Users\<you>\ftx-mcp
   PowerShell -ExecutionPolicy Bypass -File .\bootstrap\setup.ps1
   ```
   `setup.ps1` probes ports, verifies Studio/Chrome, creates state dirs under `%LOCALAPPDATA%\ftx-mcp\`, sets up a Python venv, prompts once for bearer auth (default no — `docs/security.md`), registers a manual-start scheduled task. Safe to re-run.

2. **Start the service.**
   ```powershell
   .\bootstrap\services.ps1 start
   ```
   Manual start by default; `services.ps1 enable-autostart` adds an at-logon trigger.

3. **Run the doctor.**
   ```powershell
   curl http://127.0.0.1:8765/health
   ```
   Or call `optix_status(action="doctor")` for the full dependency checklist (Studio, projects folder, bridge, chrome-cdp, deploy account/cert/password, interactive session) — each red item carries a fix. `ready: true` means the Studio binary and projects folder are present.

4. **Open the project in Studio**, on the same box running the service (session 1 — the service needs an interactive logon to send keystrokes and launch processes).

5. **Arm the bridge.** The design-time bridge is a NetLogic HTTP listener inside Studio; it does not auto-start (the service never starts or restarts Studio — `docs/architecture.md`). Add a DesignTime NetLogic node named `StudioMCPBridge` and paste in `studio-bridge/StudioMCPBridge.cs` (the shipped class name already matches; if you name the node differently, rename the class too — Studio ties an ExportMethod to a node by class-name match), then: right-click the StudioBridge node -> **Execute** -> `StartBridge` -> **Proceed** on Studio's one-time security prompt. Armed for the rest of the session. Confirm with `optix_bridge_status` (`available`, serving project, bridge version).

6. **First authoring loop:**
   - **Author** via the bridge (`optix_bridge_add_label`, or `optix_bridge_edit` with a one-op list for anything else — e.g. `[{"op": "set_property", ...}]` — see `docs/tool-reference.md` for the full op-verb list and the `FTXMCP_BRIDGE_PRIMITIVES` gate) — writes the live in-memory model, no file races.
   - **Preview** with `optix_emulator(action="run")` — sends F5; stages the model (saves as part of staging) and boots a local FTOptixRuntime. Polls the runtime port until it answers (`wait_ready`, default on).
   - **Verify** with `optix_observe(mode="screenshot", ...)` — headless Chrome/CDP captures the rendered canvas; no URL needed, it auto-targets the runtime.
   - **Iterate.** A running emulator doesn't pick up further Studio edits (separate process, its own snapshot). Interactive elements (switches, fields) can be exercised live; structural changes need `optix_emulator(action="restart")`.
   - **Ship** from Studio's own Deploy dialog once the preview looks right — this distribution has no MCP deploy path to hardware.

**Emulator status.** `optix_emulator(action="run")` is idempotent: if the project's emulator is already running or starting, no F5 is sent and it returns `state="already_running"`, so there is no need to check status before running. Use `optix_emulator(action="status")` to read the state:

| State | Meaning |
|---|---|
| `not_running` | No emulator process. |
| `starting` | Process up, runtime port not serving yet. |
| `running` | Process up and port serving — safe to screenshot. |

Only counts processes launched with `--application-name=Emulator` — an UpdateSvc-deployed runtime (same exe, same port) doesn't count as "running" here.

**Debugging a bad preview:**
```
optix_emulator(action="log", lines=100, contains="error")
```
Tails the newest `FTOptixRuntime.*.log` under the emulator's per-project log directory. Non-blocking, one-shot read — safe to call repeatedly.

## Service state inspection

| What | How |
|---|---|
| Is the service up? | `curl http://127.0.0.1:8765/health` |
| Every dependency, plain-English | `optix_status(action="doctor")` / `curl http://127.0.0.1:8765/doctor` |
| Studio + Chrome-CDP reachability | `curl http://127.0.0.1:8765/services/status` |
| Bridge armed? Which project? | `optix_bridge_status` |
| Emulator state | `optix_emulator(action="status")` |
| Emulator debug log | `optix_emulator(action="log")` |
| Scheduled task status | `.\bootstrap\services.ps1 status` |
| Deploy preflight (no Studio launch) | `curl -X POST http://127.0.0.1:8765/projects/<name>/deploy/preflight` |

## Advanced env vars

Operator escape hatches not covered by `setup.ps1` prompts — set with
`setx <name> <value>` (or before `services.ps1 start`) and restart the
service to pick them up:

| Var | Default | What it does |
|---|---|---|
| `OPTIX_BRIDGE_ENABLED` | `true` | Disable the design-time bridge integration entirely (`false`/`0`) — for a pure emulator+CDP workflow with no live-model authoring. |
| `OPTIX_BRIDGE_URL` | unset | (v1.0.7) pins the service to exactly ONE bridge URL — the legacy pre-1.0.7 single-bridge behavior — and disables port-range scanning entirely. Set this only if you need to force a single specific bridge (e.g. it's been rebound to a nonstandard port outside the scanned range). Leave unset for the default: up to `OPTIX_BRIDGE_PORT_RANGE` Studio instances can each have an armed bridge simultaneously, one per self-assigned port starting at `OPTIX_BRIDGE_PORT_BASE`. |
| `OPTIX_BRIDGE_PORT_BASE` | `8768` | (v1.0.7) first port the service scans for an armed bridge — must match `StudioMCPBridge.cs`'s `BasePort`. Ignored when `OPTIX_BRIDGE_URL` is set. |
| `OPTIX_BRIDGE_PORT_RANGE` | `4` | (v1.0.7) how many ports starting at `OPTIX_BRIDGE_PORT_BASE` the service scans — i.e. how many simultaneous Studio instances it can discover bridges for. The bridge's own range is fixed at 4 (`PortRangeSize` in `StudioMCPBridge.cs`) and is NOT read from this variable: raising it only widens what the service scans. To run more than 4 Studio instances at once, raise `PortRangeSize` and re-arm with a regenerated bridge. The `bridge_port_range_full` hint says the same. Ignored when `OPTIX_BRIDGE_URL` is set. |
| `OPTIX_HIDE_CONSOLE` | unset | Set to `1` to start the service with its console window hidden, without re-registering the task. Equivalent to passing `--hide-console`; the flag wins if both are set. The task always runs console-subsystem `python.exe` — there is no second, windowless launcher — so the service hides the console it already owns, keeping real stdout/stderr and a console for child processes to inherit. `setup.ps1 -HideConsole` bakes the flag into the task instead. |
| `OPTIX_BRIDGE_TOKEN` | unset | Shared secret for the bridge listener, if you've configured one on the NetLogic side. |
| `OPTIX_CDP_URL` | `http://127.0.0.1:9222` | Chrome DevTools Protocol endpoint the CDP verify tools drive. |
| `OPTIX_CDP_SETTLE_SECONDS` | `1.0` | Post-navigate settle before a CDP screenshot/click. Measured range 0.3s-3.5s produces byte-identical captures; raise it if a site's runtime renders slower than the default headroom covers. |
| `OPTIX_CDP_AUTOHEAL` | `true` | Silent one-shot self-heal when a CDP tool can't connect or finds no page target — restarts the `ftx-mcp-chrome-cdp` task once, then retries. `0`/`false` disables it and surfaces the raw `cdp_unavailable` error instead, useful when diagnosing a flaky Chrome install. |
| `FTX_SAVE_GENTLE_FOCUS` | `1` (on) | `optix_save`'s focus-preserving Ctrl+S path. `0`/`false` reverts to the legacy always-refocus behavior — the escape hatch if gentle-focus misbehaves on an unusual window-manager setup. |

## Common situations

**"The bridge won't respond / `bridge_unreachable`"** — never an auto-restart (the service does not own Studio's process):
- `bridge_unreachable_studio_closed` — Studio isn't running. Open the project.
- `bridge_unreachable_studio_open` — Studio is open but StartBridge hasn't run this session. Right-click -> Execute -> StartBridge.

A per-operation `write_failed` (bridge up, one call failed) is different — don't restart Studio; read the `detail` field.

**"F5 didn't seem to do anything"** — check `optix_emulator(action="status")` (`run` on an already-running emulator sends no F5 and returns `already_running`). If Studio runs elevated while the service does not (or vice versa), Windows UIPI silently blocks the keystroke; run both at the same integrity level (`optix_emulator(action="run")` reports `focused: false`).

**"My edit isn't showing up in the screenshot"** — the running-emulator staleness trap: it renders its own loaded snapshot, not further Studio edits. Restart it (`optix_emulator(action="restart")`) before concluding the edit failed.

**"I lost the bearer token" / "I want to enable auth"** — see `docs/security.md`. Tokens are issued via `bootstrap/issue-token.ps1`, DPAPI-encrypted. Auth is off by default on loopback.

**"I want to upgrade without losing config"** — follow [Upgrading](#upgrading) below. `%LOCALAPPDATA%\ftx-mcp\` (state, secrets, logs) is independent of the repo/install location, so encrypted tokens survive.

## Upgrading

To upgrade a checkout install (e.g. 1.0.8 → 1.0.9), run these five steps, in order, from a regular PowerShell window in the checkout:

1. `bootstrap\services.ps1 stop`
2. `git pull` (or replace the checkout)
3. `bootstrap\uninstall.ps1 -PurgeVenv`
4. `bootstrap\setup.ps1`
5. `bootstrap\services.ps1 start`

Your state, secrets and logs live in `%LOCALAPPDATA%\ftx-mcp\` and survive
the procedure (`-PurgeVenv` removes only the virtualenv).

- **Existing projects:** the first `optix_bridge_arm` on each existing project
  syncs its bridge source to the shipped version and leaves a backup of the
  previous one under `ProjectFiles\.ftx-mcp-backups\`.
- **BridgeTemplate:** to refresh a Studio project template, close Studio, then
  copy `studio-bridge\StudioMCPBridge.cs` over
  `<Documents>\Rockwell Automation\FactoryTalk Optix\ProjectTemplates\FTOptix_template_<TemplateName>\ProjectFiles\NetSolution\StudioMCPBridge.cs`.
  New projects copy the template folder. Projects already made from an old
  template are synced on their first arm anyway.
- **`pip install --upgrade` is not a supported route.** It does not upgrade a
  checkout install: it can bump the pip-reported version while the service
  keeps running the checkout's code. Use the five steps above.

## LAN / remote access

Supported model is **single Windows machine, loopback only** — service, Studio, and MCP client all on the same box. Bearer auth (`FTX_AUTH_REQUIRED`) is required before a non-loopback bind is even allowed to start, but LAN/multi-operator exposure is **not supported**. Do not bind non-loopback or open firewall ports. See `docs/security.md`.

## Test discipline

```powershell
cd C:\Users\<you>\ftx-mcp
.\.venv\Scripts\python -m pytest service/tests/ --tb=short
.\.venv\Scripts\python -m ruff check service/
```
Do not commit if either fails.

## When in doubt

- `docs/troubleshooting.md` — searchable by symptom keyword.
- `docs/security.md` — auth contract, LAN-bind matrix.
- `docs/architecture.md` — component overview.
