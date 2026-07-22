# ftx-mcp

<!-- mcp-name: io.github.asqi-carter/ftx-mcp -->

**Talk to your FactoryTalk Optix project.** ftx-mcp connects AI tools
(Desktop, Cowork, Code — or any MCP client) to FactoryTalk Optix Studio on
your Windows machine, so you can build and change HMI screens by describing
what you want:

> *"Add a header that says 'Hello Optix' to Screen 1 and show me."*
>
> *"Bind that label's visibility to Model/PumpRunning."*
>
> *"Looks right. Launch the emulator and validate it."*

Your LLM authors the change directly into your open Studio project, runs the
emulator, and looks at the rendered runtime to confirm it worked. It is a
development and testing companion: shipping to hardware stays in Studio's own
Deploy dialog, in your hands. Everything besides the LLM calls runs locally
on your machine. No cloud service, no API key; your LLM of choice provides
the intelligence.

[**Blog Post**](https://asqi.org/resources/ftx-mcp-v1/)

## Install (10 minutes)

Requirements: Windows 11, FactoryTalk Optix Studio 1.7.x, and an MCP client
like Claude Cowork. Also: **Google Chrome** (the CDP verify loop; skip with
`setup.ps1 -NoCdp`) and — for the Claude Desktop **Microsoft Store build**
connector path — **Node.js** (`winget install OpenJS.NodeJS.LTS`; the config
uses `npx mcp-remote`).

Run `setup.ps1` from a **regular PowerShell window** — not a shell hosted
inside a packaged app like the Store build of Claude Desktop, whose
`%LOCALAPPDATA%` writes are virtualized (setup now detects this and refuses).
If you downloaded a ZIP instead of cloning, run
`Get-ChildItem -Recurse | Unblock-File` first.

```powershell
git clone https://github.com/asqi-carter/ftx-mcp.git
cd ftx-mcp
.\bootstrap\setup.ps1                                  # install + start the service
.\bootstrap\services.ps1 start                         # start the mcp
.\bootstrap\services.ps1 status                        # verify health
http://127.0.0.1:8765/ui                               # health dashboard
```

To remove an install (or reset before a clean reinstall):
`.\bootstrap\uninstall.ps1` stops and unregisters the scheduled tasks and
reaps any leftover CDP chrome; add `-All` to also delete state (issued
tokens, chrome profile, persisted auth choice) and the venv.

## Start the Studio Bridge (5 minutes)

1. **One-time bridge setup** (per project): in the Studio project tree,
   right-click **NetLogic** → add a new **DesignTime NetLogic** named
   `StudioMCPBridge`, double-click it to open the C# editor, and paste in
   [`studio-bridge/StudioMCPBridge.cs`](studio-bridge/StudioMCPBridge.cs)
   (make sure to rebuild in your code editor or save in Studio to trigger a rebuild)
2. **Setup the Project** once per Studio session: right-click
   **StudioMCPBridge** → **SetupProject** → This just adds a webui for validation access at localhost:8081
3. **Start the bridge** once per Studio session: right-click
   **StudioMCPBridge** → **StartBridge** → accept Studio's
   one-time security prompt.
4. **Verify bridge health**: Studio Output will show `listening on http://127.0.0.1:8768` (the bridge). The service dashboard at http://127.0.0.1:8765/ui shows bridge status too.

## Cowork (5 minutes)

Requirements: MCP server and bridge running. Claude desktop app downloaded.

```powershell
cd ftx-mcp                   # install + start the service
.\bootstrap\setup-mcp-client.ps1 -WriteConfig          # adds as a connector to desktop app
```

Restart Claude Desktop (You might have to end task in task manager to fully restart), then ask Claude to **"run optix_doctor"** — it
reports anything missing, with a plain-English fix for each item.
In settings > connectors you can adjust the permissions for each of the tools

## Claude Code (5 minutes)

Requirements: MCP server healthy and claude accessible in cli

```powershell
claude mcp add --transport http ftx-mcp http://127.0.0.1:8766/mcp
```

Then in Claude Code, run `/mcp` and confirm `ftx-mcp` shows as connected.

## Visual Studio Code (5 minutes)

Create or open `.vscode/mcp.json` and add:

```json
{
  "servers": {
    "ftx-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

## Your first change

1. Ask for a change, e.g. *"Using the ftx mcp, Add a Start and stop button that toggles
   Model/MotorRun on MainWindow, and verify it works with a label with the text of 'MOTOR RUNNING' that has visibility tied to MotorRun."*
2. Watch it work: author → emulator preview → screenshot → and when it looks
   right, you deploy it to your hardware from Studio as usual.

## What it's capable of

- **Author** widgets, properties, bindings, computed expressions, events,
  translations, and multi-screen navigation — live in the open Studio project.
- **Run** Studio's built-in emulator via F5 key and read the runtime log.
- **Verify** by looking at the webui: screenshot, click
  buttons and tabs, type into fields.
- **Hand back to you to ship** when the preview looks right, you deploy
  from Studio as usual. This distribution only runs the emulator.

The full tool list (48 tools, plus the same surface over plain HTTP for
scripts and CI) is in [docs/tool-reference.md](docs/tool-reference.md).
Bundled **authoring playbooks** (navigation, bound controls, styles,
expressions) ship with the server itself — Claude discovers them via
`optix_list_skills` / `optix_get_skill`, so every connected client gets
them with zero setup. (In Claude Code they also load natively as
[skills](skills/).)

## Security & safety posture

- **Local only.** The service binds `127.0.0.1` and talks to nothing off the
  machine. Optional bearer-token auth, enforced before any LAN bind.
- **Read-only by default.** Every tool carries MCP `readOnlyHint` /
  `destructiveHint` annotations (contract-pinned by tests) so your MCP host
  can auto-approve reads and gate writes.
- **Write gates, not hope.** Undeclared properties, array writes, duplicate
  names, and unsafe re-parents are refused with typed errors; composite
  operations roll back on failure. File-level edits are refused while Studio
  has the project open.
- **Audited.** Every model mutation appends a JSON line to a local audit
  trail (`%LOCALAPPDATA%\ftx-mcp\logs\audit.jsonl`) what, when,
  outcome.
- **Shipping stays in your hands.** Previewing never touches your runtime;
  deploying to hardware happens from Studio, full stop. This distribution
  does design time edits and testing via the emulator.

The full posture including the prompt-injection surface analysis is in
[SECURITY.md](SECURITY.md).

## Documentation

| | |
|---|---|
| [Runbook](docs/runbook.md) | First session, step by step |
| [Tool reference](docs/tool-reference.md) | Every MCP tool + the HTTP API |
| [Architecture](docs/architecture.md) | How the pieces fit together |
| [Troubleshooting](docs/troubleshooting.md) | Symptom-indexed fixes |
| [Security](docs/security.md) | Auth, ports, what talks to what |

## Compatibility

Tested with FactoryTalk Optix Studio 1.7.x on Windows 11, Python 3.12.
Optix CLI behavior is not contract-stable across major versions — pin your
Studio version in production.

## License

[MIT](LICENSE) · © 2026 ASQI · Not affiliated with or endorsed by Rockwell
Automation. FactoryTalk Optix is a trademark of Rockwell Automation, Inc.;
this project orchestrates locally installed Optix binaries without
redistributing them. See [NOTICE](NOTICE).
