# VS Code quick install

`ftx-mcp` speaks streamable-HTTP MCP on `127.0.0.1:8766/mcp`. VS Code's
native MCP support (consumed by GitHub Copilot's agent mode) can point at it
directly — no bridge process needed, unlike the Claude Desktop Store build.

## VS Code native MCP (GitHub Copilot agent mode)

VS Code reads MCP server definitions from an `mcp.json` file — workspace-level
at `.vscode/mcp.json`, or a user-level equivalent reachable from the Command
Palette (**MCP: Open User Configuration**). The current shape is a top-level
`servers` map, with each entry given a `type`, `url`, and optional `headers`:

```json
{
  "servers": {
    "ftx-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:8766/mcp",
      "headers": {
        "Authorization": "Bearer <paste-your-token>"
      }
    }
  }
}
```

Drop the `headers` block entirely on a default (auth-off, loopback) install —
the JSON above is the whole template
this is adapted from.

> **Key names may have moved on.** VS Code's MCP config format has changed
> shape more than once since it shipped. If `servers` doesn't work, check
> VS Code's own MCP documentation (Command Palette → **MCP: Open User
> Configuration**, or the *Use MCP servers in VS Code* doc) for the current
> key names before assuming the server itself is broken.

Once added, open the Command Palette → **MCP: List Servers** to confirm
`ftx-mcp` connects, then enable it for a Copilot Chat agent-mode session.
Tools surface as `optix_*` (health, discovery, author, deploy, verify — see
the main [`README.md`](../README.md) for the full tool list).

## Claude Code inside the VS Code terminal/extension

If you're driving `ftx-mcp` via the Claude Code CLI (either in VS
Code's integrated terminal or the Claude Code extension), the config is
identical to the standalone CLI setup: a `.mcp.json` in the workspace root.
See [`claude-code-setup.md`](claude-code-setup.md) for the full walkthrough
for details.

## Cursor / Cline

Both consume the same HTTP MCP shape as VS Code's native support — an entry
with `type: "http"`, `url: "http://127.0.0.1:8766/mcp"`, and an optional
`Authorization` header. Check each client's own MCP settings UI or config
file location (Cursor: Settings → MCP; Cline: its extension settings) for
where to paste the block, then reuse the same JSON body shown above.
