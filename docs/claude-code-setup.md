# Claude Code on Windows — setup playbook

**Audience.** An Optix developer bringing up Claude Code as the agent frontend for `ftx-mcp` on a stock Windows 11 box, all local, all loopback.

Canvas verification is Chrome DevTools Protocol (CDP) against headless chrome-cdp that `setup.ps1` installs by default (skip with `-NoCdp`) — not a separate MCP server; the service drives it over loopback `:9222`. Only `ftx-mcp` is registered as an MCP server.

## Working setup — Claude Code CLI + the local MCP server

End state: `claude mcp list` shows:

```
ftx-mcp: http://127.0.0.1:8766/mcp (HTTP) - ✓ Connected
```

(The canvas-verify Chrome runs as a background scheduled task on `:9222` — not an MCP server, doesn't appear in `claude mcp list`.)

### Prerequisites

- Windows 11, signed in as the user who runs deploys (interactive session — DPAPI keys bind to this account).
- `bootstrap/setup.ps1` already run (service installed, scheduled task registered, DPAPI password seeded, chrome-cdp verify task installed unless `-NoCdp`).
- Default browser is Edge or Chrome (the OAuth callback needs one of these).

### Steps

1. **Execution policy** — usually nothing to do: `setup.ps1` detects a
   blocking policy and prints the exact remedy. Only if scripts are refused:
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
   ```

2. **Install Claude Code:**
   ```powershell
   irm https://claude.ai/install.ps1 | iex
   ```
   Lands at `%USERPROFILE%\.local\bin\claude.exe`, not on `PATH`. For the current session: `$env:PATH += ";$env:USERPROFILE\.local\bin"`. For persistent PATH, add `%USERPROFILE%\.local\bin` via System Properties -> Environment Variables.

3. **First-run OAuth.** From the repo root:
   ```powershell
   cd C:\Users\<you>\ftx-mcp
   claude
   ```
   Browser pops; complete OAuth. Token caches under `%APPDATA%\Claude\`; subsequent runs need no re-auth. Exit the TUI (`/exit` or Ctrl-C) once authed.

4. **Issue a deploy-scope token:**
   ```powershell
   .\bootstrap\issue-token.ps1 -Label "claude-code" -Scope deploy -ExpiresInDays 30
   ```
   Prints the bearer once. Copy it.

5. **Register `ftx-mcp` (HTTP + bearer auth):**
   ```powershell
   claude mcp add --transport http ftx-mcp http://127.0.0.1:8766/mcp `
     --header "Authorization: Bearer <token-from-step-4>"
   ```

6. **Verify:**
   ```powershell
   claude mcp list
   ```
   Shows `✓ Connected`. Canvas verification needs no MCP registration (`optix_cdp_screenshot` / `optix_cdp_click`). Confirm the verify task with `netstat -ano | findstr :9222` (should show `LISTENING`).

## Cross-references

- `docs/runbook.md` — the pre-CC install sequence this builds on.
- `docs/troubleshooting.md` — failure-mode index.
- [`docs/cowork-quick-install.md`](cowork-quick-install.md) — the GUI (non-CLI) client path.
- `docs/vscode-quick-install.md` — wiring into VS Code / Copilot agent mode.
