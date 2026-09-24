# Troubleshooting

Symptom-indexed fixes. Search for your error text below.

## Studio crashes during deploy: `0xC0000005` / ACCESS_VIOLATION

**Symptom:** deploy returns `state: "failed"`, `stdout_tail` shows a crash
dump created right after "Opening project".

**Cause:** Studio reads per-user DPAPI-encrypted config, and DPAPI only
works in an *interactive* logon session. A service launched from SSH,
`LocalSystem`, or any non-interactive context crashes Studio at startup.

**Fix:** run the service from an interactive logon — the scheduled task
`setup.ps1` registers already does this. Confirm with
`curl http://127.0.0.1:8765/health`: `"interactive_session": true`.

## `Port conflicts detected: 8765` / `8766` (setup.ps1)

Another process holds the default ports. Kill it
(`Get-NetTCPConnection -LocalPort 8765 -State Listen` → `Stop-Process`)
or override with `OPTIX_HTTP_PORT` / `OPTIX_MCP_PORT` (User-scope env
vars), then re-run `setup.ps1`.


## `/projects` returns an empty list

`OPTIX_PROJECTS_ROOT` points somewhere without `.optix` files. Check
`/health` → `projects_root_exists`; default root is
`%USERPROFILE%\Documents\Rockwell Automation\FactoryTalk Optix\Projects`.

## `406 Not Acceptable` from `/mcp`

You hit the MCP endpoint with a plain browser/curl GET. That's correct
behavior — the endpoint speaks MCP's streamable-HTTP, not plain GET. Use
an MCP client (the [VS Code quickstart](vscode-quick-install.md) shows the registration shape).

## Deploy hangs >60s, Studio still alive in Task Manager

**Cause:** a project YAML has a UTF-8 BOM at byte 0 (`EF BB BF`) —
Studio's exporter hangs on it. PowerShell 5.1's
`Set-Content -Encoding UTF8` writes that BOM; never edit project files
that way.

**Recover:**

1. Kill Studio and the deploy worker:
   ```powershell
   Get-Process FTOptixStudio,FTOptixRuntime -ErrorAction SilentlyContinue | Stop-Process -Force
   type $env:LOCALAPPDATA\ftx-mcp\deploy.lock   # then Stop-Process -Id <pid>
   ```
   The lock is PID-aware and recovers on the next deploy — don't delete it.
2. Strip the BOM:
   `Path(f).write_bytes(Path(f).read_bytes().lstrip(b"\xef\xbb\xbf"))`
   (or `git checkout` the file).
3. Use the API for future edits — `edits[]` content is written UTF-8
   BOM-free by construction.

Known limitation: the deploy timeout does not kill Studio's process tree
on Windows; a BOM-hang needs the manual kill above.


## Canvas verify fails: `cdp_unavailable` / screenshot 503 / cert errors

The verify Chrome runs **headless** — no visible window is normal. The
CDP tools self-heal once (restart the task, reopen a page) on the next
call. If it persists:

1. `optix_cdp_restart` forces recovery and reports
   `{alive, has_page, restarted}`.
2. `bootstrap/services.ps1 status` — expect
   `ftx-mcp-chrome-cdp port:9222 LISTEN`. (Re)install with
   `bootstrap/install-chrome-cdp.ps1`.
3. `ERR_CERT_AUTHORITY_INVALID`: the Optix web engine defaults to
   self-signed HTTPS; `install-chrome-cdp.ps1` bakes in
   `--ignore-certificate-errors` — re-run it if the flag is missing.
4. Nothing to screenshot: no runtime/emulator is up — start one first.

<a id="studio-open"></a>
## Read or deploy refused with `409` / `studio_open`

The corruption guard. While Studio is running, its in-memory model is the
source of truth: file writes get stomped on Studio's next save, and file
reads return stale state. Any running `FTOptixStudio.exe` blocks all
file-level reads and deploys (Studio exposes no reliable way to detect *which* project it has
open — process args, window title, and lock files all fail on that — so the
guard is deliberately all-or-nothing).

**Fix:** use the live bridge tools while Studio is open (that's the
normal authoring path), or close Studio entirely for file-level work.
There is deliberately no *in-band* override — a bypass parameter would be
reachable by the model driving the tools.

**Attributed mode (operator opt-in, `OPTIX_STUDIO_GUARD_MODE=attributed`).**
An out-of-band env knob relaxes the blanket block for the narrow, safe
"Studio-open-on-A, file-op-on-B" case: when the design-time bridge proves a
single Studio instance is serving a *different* project, that project's model
is not held by Studio, so file ops on the target are safe and proceed. It is
NOT a tool parameter, so it does not reopen the "escape hatch reachable by the
model" hole the no-override design closed. The relaxation applies only when
*every* condition holds — mode is `attributed`, exactly one `FTOptixStudio.exe`
PID, the bridge is up and names a served project, and that name differs from
the target. Any ambiguity (bridge down — the common cold-start state —
multiple Studio instances, or the bridge serving *this* project) falls back to
the blanket block. Allowed reads carry `studio_guard: "attributed"` /
`studio_serving: "<other>"`; every downgrade is written to the audit trail.
`deploy_preflight` still reports `studio_open` under this mode (it runs its own
blanket check), so a preflight that blocks does not mean the real op will.

<a id="editor-project-open"></a>
## Deploy refused with `409` / `editor_project_open`

VS or VS Code has this project open; service edits would race unsaved
editor buffers. Close the project (or its NetSolution folder) in the
editor and retry. Editors not attributed to this project only produce a
warning, never a refusal.

## Claude Desktop / Cowork: tools stop working after a service restart

Claude Desktop reaches the service through an `mcp-remote` bridge that
gives up after two reconnect attempts when the service restarts. In
Cowork this reads "the device this session is bound to is not connected
to the bridge."

**Fix:** fully restart Claude Desktop (Quit from the tray), then start a
**new** Cowork conversation. Rule of thumb: service restarted → restart
Claude Desktop.

<a id="cowork-skip-all"></a>
## Cowork: "Skip all approvals" greyed out

Expected. Cowork runs in a VM sandbox, so host MCP servers are bridged in
as a *remote device*, and "Skip all approvals" only applies to tools
inside the VM. Use per-tool **"Always allow"** instead — it persists. (In
plain Claude Desktop Chat, no VM is involved and Skip-all applies.)

## `bridge not serving '<project>'` on one call — but the project is armed (stale-registry self-heal)

**Symptom (pre-1.0.8):** a single tool call — most commonly `optix_describe_node`
— returns `bridge not serving 'MyProject'` or a raw `BridgeUnavailable` message
while the bridge IS armed and running, and the very next call to the same or any
other bridge-routed tool succeeds.

**Cause:** a `bridges/<port>.json` registry file left behind by a killed Studio
— for example after a CI runner tears down mid-session — was the only entry in
the `LOCALAPPDATA\ftx-mcp\bridges\` directory. The resolver probed only that
dead port, found nothing, then unlinked the stale file and returned "no bridge
serving this project". On the next call the registry directory was empty so the
resolver fell through to the port-range scan and found the live bridge. The one
failing call was the window between "stale file deleted" and "range scan ran".

The refusal message also misdirected: it reported `serving='<other project>'`,
which was whatever project happened to be on the lowest configured port —
unrelated to the port that was actually probed for the requested project.

**Fix in 1.0.8:** when every registry entry is dead and has been deleted, the
resolver falls through to the port-range scan within the SAME call. A registry
with at least one live entry still skips the scan (unchanged). The refusal now
names `ports_probed` and the project served at each port, so the error message
reflects what was actually checked.

**Workaround on older versions:** retry the failing call once. The symptom
always self-heals on the next call because the stale file was already removed.
If the failure persists across multiple calls, check
`LOCALAPPDATA\ftx-mcp\bridges\` for leftover `*.json` files and remove any
whose port no longer has an armed Studio instance.

## The emulator serves, but `optix_observe` returns a blank white page

`optix_emulator action="run"` reports `serving: true`, port 8081 answers, the
runtime log shows `Starting project ...` with no ERROR — and every screenshot
comes back uniformly white. Clicks then "succeed" and change nothing, because
there is nothing on the canvas to hit.

The Optix **web** presentation engine is a React client that builds the whole
UI from the model it is handed. Some model shapes crash it *client-side*, where
neither the runtime log nor any MCP tool can see the failure: the page loads,
`#root` stays empty, and the only trace is a JS exception in the browser
console. The one measured on 2026-09-05 (Studio 1.7.4.32):

* **an empty `GridLayout` anywhere in the start window** →
  `TypeError: Cannot read properties of undefined (reading 'length')` in
  `assets/index-*.js`. `Rows` / `Columns` are `String[]` and are not settable
  over the bridge, and `Visible=false` does **not** help — the widget has to be
  out of the rendered window (move it to a folder such as `UI/Screens`, or give
  it rows and columns in Studio) before the page will render.

A second, non-fatal shape worth clearing while you are there: a
`NavigationPanel` with no items logs
`Navigation Panel Item index ... is out of range` as an ERROR in every session.
That one renders fine; it just makes the log untrustworthy.

**How to tell blank-page from bad-coordinates.** Take a full-frame
`optix_observe(mode="screenshot")` and *look at it* — a failed session is
uniformly white, not "empty-looking". The native presentation engine tolerates
several shapes the web client does not, so a working Studio preview proves
nothing about the web session.

**Related:** a runtime click cannot be verified with `optix_describe_node`.
`ToggleVariable` and friends write the **emulator's** copy of the model;
`describe_node` talks to the design-time bridge inside Studio, which only ever
reports the configured value. Verify a runtime write on the canvas — bind a
widget to the variable and diff a screenshot region over it.

## Batch call timed out — I don't know what landed

**Symptom:** an `optix_bridge_edit` call returned a network timeout or
`state="chunked"` and you lost track of which ops actually applied.

**Fix:** call `optix_bridge_edit(action="status", project=<project>)` with
**no `batch_id`** — it returns the most recent batch journals for the project.
Read `applied` (number of ops that landed) and `remaining_ops` (ops not yet
started). If the batch is still in progress, pass the `batch_id` to
`action="continue"` to pick up from where it stopped.

The journal persists under `%LOCALAPPDATA%\ftx-mcp\batches\<batch_id>.json`
for `OPTIX_BATCH_JOURNAL_DAYS` days (default 7), so recovery is available
even after a full service restart.

## 2026-09-05 — service dies under sustained emit load (thread-pool exhaustion)

**Symptom:** a large burst of MCP tool calls (in the field incident: 2988 template-build ops
followed by 462 screen-emit ops, reaching `optix_save`) caused the connection to drop
mid-call with `IncompleteRead(0 bytes read)` then `RemoteDisconnected`. Retries on
attempts 2–4 all failed. `bootstrap/services.ps1 status` reported `state=Running … port:8766`
with **no `LISTEN` mark** — the process was alive from the Task Scheduler's point of view
but the HTTP port was dead. `Get-ScheduledTaskInfo -TaskName ftx-mcp` showed
`LastRunTime` advancing at **05:10, 06:18, 06:21, 06:24 UTC 2026-09-05** — four automatic
restarts, roughly three minutes apart, with the service dying each time.

**Lifecycle log (`service.jsonl`) from a test box — the actual entries:**

```jsonl
{"ts":"2026-09-05T05:10:...+00:00","event":"start","version":"...","pid":...}
{"ts":"2026-09-05T06:18:...+00:00","event":"start","version":"...","pid":...}
{"ts":"2026-09-05T06:21:...+00:00","event":"start","version":"...","pid":...}
{"ts":"2026-09-05T06:24:...+00:00","event":"start","version":"...","pid":...}
```

No `stop` or `crash` entry follows any of the four `start` lines. That is the
**external-kill signature**: the process was not crashing (which would produce a `crash`
entry with a Python traceback) — it was being killed from outside. The Task Scheduler
fires its `ExecutionTimeLimit` action (result `0x00041306`) when a process stalls for
too long and fails to answer the port-health check.

`optix_status(action="doctor")` on that box confirmed this: the `service_lifecycle` block
returned four `external_kill` rows, each carrying the pid and version of the dangling
`start`, with `note: "start with no following stop or crash — process was killed
externally (SIGKILL, Task Scheduler ExecutionTimeLimit, or manual stop)"`. No `crash`
row, no `exc_type`, no `first_frame`.

**Root cause: thread-pool exhaustion (hypothesis A, confirmed).**

`service/mcp_app.py` offloads every synchronous tool via `anyio.to_thread.run_sync`.
Without an explicit `CapacityLimiter`, a large burst of concurrent tool calls spins an
unbounded number of threads. A 3450-op burst saturated the default anyio pool: the
process stalled, the scheduled task fired its `ExecutionTimeLimit`, and the client
received `RemoteDisconnected`. The scheduled task then restarted the service; a
short-lived new service startup produced the next `start` entry, repeated four times.

**Ruled-out hypotheses:**

- **B — unhandled exception killing uvicorn.** Predicts a `crash` entry in `service.jsonl`
  with `exc_type` and `first_frame`. The log contained only `external_kill` rows —
  no `crash` event. Hypothesis B is **ruled out**.
- **C — batch-journal memory growth** (`_write_batch_journal` at `core.py:3342` rewrites
  JSON after every op; 2988 ops = 2988 rewrites). Measured on a synthetic 3000-op batch
  (`service/tests/test_benchmark_batch_journal.py`): total
  wall time **1454.8 ms**, first-10 / last-10 call-time ratio **4.32×** (threshold for
  action: ~50×), peak RSS delta **+2816 KB** (+2.75 MB). RSS growth is negligible —
  no memory pressure at realistic batch sizes. The 4.32× timing ratio shows mild
  non-linear write-time growth but is far below the threshold that would indicate a
  real O(n²) signature; no throttle was added. Hypothesis C is **ruled out**.

**Fix (shipped):** an explicit `anyio.CapacityLimiter` wraps the offload loop in
`service/mcp_app.py`. Size is controlled by `OPTIX_THREAD_LIMITER` (default `40`).
The limiter bounds peak thread growth so a large burst queues rather than stalls.

**How to re-diagnose a repeat incident:**

1. `optix_status(action="doctor")` — the `service_lifecycle` block shows the last
   five lifecycle entries (start / stop / crash / derived external_kill) with timestamp,
   pid, version, and for crash entries: exception type + first traceback frame. An
   `external_kill` row with no preceding `crash` means thread-pool stall or manual
   termination, not a Python exception.
2. `bootstrap/services.ps1 status` — `state=Running` with `-----` in the LISTEN column
   is the running-but-dead-port fault; exit code is non-zero when this condition holds.
3. Collect evidence by hand on the affected box: copy
   `%LOCALAPPDATA%\ftx-mcp\logs\service.jsonl*` (every size-rotated copy) and
   `service-stderr.log`, export the Windows Application event log for the minutes
   around each crash (Event Viewer -> Filter Current Log), and capture
   `Get-ScheduledTaskInfo -TaskName ftx-mcp`. Zip them and attach them to the issue.

## `invoke_unsupported_bridge` from `optix_bridge_invoke_method`

The armed bridge is an older build that runs the method without an Optix
session, which crashes Studio (issue #4), so the service refuses before
calling it. Rebuild the NetSolution with the current
`studio-bridge/StudioMCPBridge.cs` and re-arm; `/bridge/health` should then
report `"invoke_session": "root"`. Until then, `optix_execute_method` runs
the method through Studio's own right-click Execute (the NetLogic node must
be saved to disk first with `optix_save`).

## Got a different error?

Open an issue at `https://github.com/asqi-carter/ftx-mcp/issues`
with: `/health` output, the deploy response's `state` + `stderr_tail`,
the last 50 lines of
`%LOCALAPPDATA%\ftx-mcp\logs\service-stderr.log`, and your Studio
version.
