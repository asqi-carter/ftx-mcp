# Cowork quick install

The fastest path from a fresh Windows box to running `ftx-mcp` tools inside
Claude Desktop / Cowork. If anything fails along the way,
[`troubleshooting.md`](troubleshooting.md) is searchable by symptom.

1. **Install the service.**

   ```powershell
   .\bootstrap\setup.ps1
   ```

2. **Wire the client config:**

   ```powershell
   .\bootstrap\setup-mcp-client.ps1 -WriteConfig
   ```

   This writes the `ftx-mcp` entry into Claude Desktop's config
   (auto-detecting the Microsoft Store/MSIX package path). No token is
   involved on a default install — auth is off on loopback; the script only
   issues and embeds one if you've enabled `FTX_AUTH_REQUIRED`.

3. **Restart Claude Desktop.**

4. **Verify:** Settings → **Developer → Local MCP servers** should show
   `ftx-mcp` with a `running` badge. (It also appears in the Connectors list
   with a "Local dev" tag — that listing is informational; local servers are
   configured here, never added through Connectors → Add.) In Cowork its
   tools are namespaced `ftx-mcp__optix_*`.

5. **Try it.** Ask Cowork to "run `optix_doctor`" — it should come back with
   a plain-English health report.

The bundled authoring playbooks come along automatically — the server
announces them at connect time and Claude fetches one on demand
(`optix_list_skills` / `optix_get_skill`). No skill upload needed. (In
Claude Code they additionally load as native skills from `skills/`.)

## Two gotchas that actually bite

- **After any SERVICE restart, restart Claude Desktop and start a NEW Cowork
  conversation.** The `mcp-remote` bridge gives up after 2 reconnect attempts
  and does not self-heal. An old Cowork conversation stays bound to the dead
  bridge — restarting Desktop respawns a fresh one, but you still need a new
  conversation.
- **If Windows Firewall prompts to "allow Node.js," Cancel is fine.** The
  prompt is for `mcp-remote`'s loopback callback port. Loopback works without
  the allow rule; nothing gets exposed either way.

The connector config lives in Claude Desktop's settings (Settings →
Connectors); `setup-mcp-client.ps1 -WriteConfig` writes it for you.
