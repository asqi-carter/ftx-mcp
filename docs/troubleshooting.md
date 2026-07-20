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
There is deliberately no override — a bypass parameter would be reachable
by the model driving the tools.

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

## Got a different error?

Open an issue at `https://github.com/asqi-carter/ftx-mcp/issues`
with: `/health` output, the deploy response's `state` + `stderr_tail`,
the last 50 lines of
`%LOCALAPPDATA%\ftx-mcp\logs\service-stderr.log`, and your Studio
version.
