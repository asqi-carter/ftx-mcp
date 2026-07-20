# Architecture

One Python service makes a FactoryTalk Optix installation scriptable:
author a change, preview it, verify it on the rendered canvas, ship it —
from an MCP tool call or a plain HTTP POST. It embeds no model and no API
key; the intelligence is whichever client connects.

## Process shape

A single Python process runs two servers in one event loop:

- **`:8765`** — HTTP/REST (curl, scripts, CI)
- **`:8766`** — MCP, streamable HTTP (Claude Desktop/Code, Cursor, any MCP host)

Both thin-wrap the same core functions — one behavior, two protocols. The
process runs as a **Windows scheduled task in your interactive logon
session**; that anchor is required because DPAPI token encryption is
per-user-per-session and the emulator/save paths drive Studio with real
keystrokes. Default bind is loopback with auth off; a non-loopback bind
refuses to start without bearer auth ([security.md](security.md)).

## What it talks to

```
              MCP client (Claude Desktop / Code / any)     curl / CI
                          MCP :8766          HTTP :8765
                               \                /
+---------------------------------------------------------------+
|  Windows box            ftx-mcp service                        |
|                                |                                |
|     +--------------+----------+                                |
|     v              v                                           |
|  design-time    Studio emulator                                |
|  bridge :8768   (F5; boots its own                             |
|  inside the     FTOptixRuntime                                 |
|  running Studio on :8081)                                      |
|                       |                                        |
|                       v                                        |
|            headless Chrome, CDP :9222                          |
|            (screenshot / click / type on the canvas)           |
|                                                                 |
+---------------------------------------------------------------+
```

1. **Design-time bridge** (`:8768`) — a NetLogic HTTP listener inside the
   running Studio (source: `studio-bridge/StudioMCPBridge.cs`). Authors
   the live in-memory model: widgets, properties, variables, bindings,
   expressions, events, aliases, translations, z-order, delete. The
   service can't start it — Studio is your application; arming the bridge
   is a right-click ([runbook.md](runbook.md)).

2. **Studio's emulator** — the default preview/verify path. F5 stages the
   current in-Studio model (saving as part of staging) and boots a local
   runtime on `:8081`. It is a separate process with its own snapshot:
   interactive elements can be exercised live, but structural edits show
   up only after a stop → start cycle.

3. **Headless Chrome via CDP** (`:9222`) — canvas verify for emulator and
   deployed runtime alike. Uses trusted CDP input (mouse, text, keys)
   because the Optix canvas ignores synthetic DOM events. It's the
   service's own disposable process, so it self-heals once per call —
   whereas the bridge, which you own, never gets auto-restarted: it fails
   with an actionable message instead.

Shipping to hardware is deliberately absent: when the preview looks right,
you deploy from Studio's own Deploy dialog. (Deploy plumbing exists in the
source for possible future reintegration but has no runtime activation
path in this distribution.)

## Error envelope

Every domain error carries `http_status`, a snake_case `code`, and
optionally a `hint` and docs anchor; both surfaces render it the same way:

```json
{ "code": "studio_open", "message": "...", "hint": "...",
  "docs_url": "docs/troubleshooting.md#studio-open" }
```

## Security posture

Loopback + auth off is the default. Bearer-token hashes are stored
DPAPI-encrypted at `%LOCALAPPDATA%\ftx-mcp\secrets\tokens.json.dpapi`,
bound to your Windows user; the secret is shown once at issue time and
never persisted. Details: [security.md](security.md).
