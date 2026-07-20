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
   Or call `optix_doctor` for the full dependency checklist (Studio, projects folder, bridge, chrome-cdp, deploy account/cert/password, interactive session) — each red item carries a fix. `ready: true` means the Studio binary and projects folder are present.

4. **Open the project in Studio**, on the same box running the service (session 1 — the service needs an interactive logon to send keystrokes and launch processes).

5. **Arm the bridge.** The design-time bridge is a NetLogic HTTP listener inside Studio; it does not auto-start (the service never starts or restarts Studio — `docs/architecture.md`). Add a DesignTime NetLogic node named `StudioMCPBridge` and paste in `studio-bridge/StudioMCPBridge.cs` (the shipped class name already matches; if you name the node differently, rename the class too — Studio ties an ExportMethod to a node by class-name match), then: right-click the StudioBridge node -> **Execute** -> `StartBridge` -> **Proceed** on Studio's one-time security prompt. Armed for the rest of the session. Confirm with `optix_bridge_status` (`available`, serving project, bridge version).

6. **First authoring loop:**
   - **Author** via the bridge (`optix_bridge_add_label`, `optix_bridge_create_widget`, `optix_bridge_set_property`, ...) — writes the live in-memory model, no file races.
   - **Preview** with `optix_run_emulator` — sends F5; stages the model (saves as part of staging) and boots a local FTOptixRuntime. Polls the runtime port until it answers (`wait_ready`, default on).
   - **Verify** with `optix_cdp_screenshot` — headless Chrome/CDP captures the rendered canvas; no URL needed, it auto-targets the runtime.
   - **Iterate.** A running emulator doesn't pick up further Studio edits (separate process, its own snapshot). Interactive elements (switches, fields) can be exercised live; structural changes need `optix_stop_emulator` -> `optix_run_emulator`.
   - **Ship** from Studio's own Deploy dialog once the preview looks right — this distribution has no MCP deploy path to hardware.

**Emulator status — check before you F5.** F5 toggles; calling "run" on an already-running emulator stops it. Check `optix_emulator_status` first:

| State | Meaning |
|---|---|
| `not_running` | No emulator process. |
| `starting` | Process up, runtime port not serving yet. |
| `running` | Process up and port serving — safe to screenshot. |

Only counts processes launched with `--application-name=Emulator` — an UpdateSvc-deployed runtime (same exe, same port) doesn't count as "running" here.

**Debugging a bad preview:**
```
optix_runtime_log_tail(lines=100, contains="error")
```
Tails the newest `FTOptixRuntime.*.log` under the emulator's per-project log directory. Non-blocking, one-shot read — safe to call repeatedly.

## Service state inspection

| What | How |
|---|---|
| Is the service up? | `curl http://127.0.0.1:8765/health` |
| Every dependency, plain-English | `optix_doctor` / `curl http://127.0.0.1:8765/doctor` |
| Studio + Chrome-CDP reachability | `curl http://127.0.0.1:8765/services/status` |
| Bridge armed? Which project? | `optix_bridge_status` |
| Emulator state | `optix_emulator_status` |
| Emulator debug log | `optix_runtime_log_tail` |
| Scheduled task status | `.\bootstrap\services.ps1 status` |
| Deploy preflight (no Studio launch) | `curl -X POST http://127.0.0.1:8765/projects/<name>/deploy/preflight` |

## Common situations

**"The bridge won't respond / `bridge_unreachable`"** — never an auto-restart (the service does not own Studio's process):
- `bridge_unreachable_studio_closed` — Studio isn't running. Open the project.
- `bridge_unreachable_studio_open` — Studio is open but StartBridge hasn't run this session. Right-click -> Execute -> StartBridge.

A per-operation `write_failed` (bridge up, one call failed) is different — don't restart Studio; read the `detail` field.

**"F5 didn't seem to do anything"** — check `optix_emulator_status` first (F5 toggles). If Studio runs elevated while the service does not (or vice versa), Windows UIPI silently blocks the keystroke; run both at the same integrity level (`run_emulator` reports `focused: false`).

**"My edit isn't showing up in the screenshot"** — the running-emulator staleness trap: it renders its own loaded snapshot, not further Studio edits. Restart it (`optix_stop_emulator` -> `optix_run_emulator`) before concluding the edit failed.

**"I lost the bearer token" / "I want to enable auth"** — see `docs/security.md`. Tokens are issued via `bootstrap/issue-token.ps1`, DPAPI-encrypted. Auth is off by default on loopback.

**"I want to upgrade without losing config"** — `%LOCALAPPDATA%\ftx-mcp\` (state, secrets, logs) is independent of the repo/install location. Replace the checkout, re-run `setup.ps1` — encrypted tokens and the scheduled task survive.

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
