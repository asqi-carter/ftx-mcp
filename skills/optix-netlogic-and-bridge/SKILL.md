---
name: optix-netlogic-and-bridge
description: Register NetLogic (C#) into the live model, compile-check it, operate the design-time bridge lifecycle (start it, when new bridge code needs a Studio reopen, run design-time [ExportMethod]s SAFELY), restart the MCP service, and deploy. Use when adding code-behind, running generators/builders, when the bridge is down/stale after a rebuild, or when restarting the service or shipping to a panel.
---

# NetLogic registration + bridge operation

Operational playbook for the parts of Optix that are not pure model authoring:
adding running C#, compiling it, and driving the in-Studio bridge lifecycle.
Everything here is verified behavior, not guesswork.

## Registering a NetLogic (adding running C#)

A NetLogic node has **no generated proxy and no code-reference property**. The
runtime binds a NetLogic node to the C# class whose name **equals the node
BrowseName**, and the SDK-style NetSolution `.csproj` **auto-globs every `.cs`**.
So the whole recipe is:

1. Write the class into the project's `ProjectFiles/NetSolution/` with the normal
   file tools: `public class MyLogic : BaseNetLogic { public override void Start() {...} }`.
2. `optix_bridge_create_netlogic(parent="<path>", name="MyLogic")` — `name` MUST
   equal the class name exactly. This mints a real `NetLogicObject` node.
3. `optix_emulator(action="restart")` — the rebuild instantiates it; `Start()` runs.

**Placement matters.** A runtime NetLogic that reads sibling nodes through `Owner`
must be created UNDER the object that owns them (pass that object as `parent`,
e.g. a screen). A self-contained one can live under any loaded container; a
NetLogic category folder runs at app start.

There is NO Studio "New -> NetLogic" menu step required. (This was long believed
to be a wall; it is not — the wall was creating a plain `BaseObjectType` named
like a class, which never binds.)

### `[ExportMethod]`s need a Studio REOPEN before anything can call them

A bridge-created NetLogic node starts with **no method children**. The class
compiles, `Start()`/`Stop()` run fine, but the `[ExportMethod]`s do not exist as
`UAMethod` nodes in the model yet — Studio materialises them only when it reopens
the project and parses the class. Until then any
`wire_event(..., method_path="Model/MyLogic/MyMethod")` wires cleanly, reports
`ok`, and fails at runtime with:

```
ERROR;urn:FTOptix:CoreBase;02;Exception caught: Method 'Key2' not found on
  target object Root/Objects/<Proj>/Model/NumKeypad
```

**Sequence that works:** (`optix_read_netlogic` to inspect the current `.cs`
source if the class already exists →) author the `.cs` → `create_netlogic` →
`optix_build_check` → **save, close Studio, reopen, re-arm** → confirm with
`optix_describe_node` that the method children now exist → THEN wire buttons to
them. Measured 2026-09-01: 15 `[ExportMethod]`s went from zero children to all
15 `UAMethod` nodes across one reopen, with no other change.

Verify with `describe_node`, not by assuming — and note this failure is silent
everywhere except the runtime log (see `optix-verify-loop`).

### Session-based NetLogic: methods are NOT callable by absolute path

A NetLogic that uses `Session` (it must sit inside a UI object — `IdleTimeoutLogic`,
anything closing dialogs, anything per-operator) is instantiated **per UI session**.
The node at `UI/MainWindow/MyLogic` is only the TEMPLATE. A button wired to
`method_path: "UI/MainWindow/MyLogic/DoThing"` wires cleanly, the `UAMethod` node
exists, the click fires — and the runtime answers:

```
ERROR;CoreBase;31;No behaviour or observer found that handles the method
  'DoThing' on node .../UI/MainWindow/MyLogic
```

**Call it directly in C#** from code already running inside the session instance
(an event handler on that same object) instead of routing through the node. For
an idle-timeout/close-dialogs job that is also simply less machinery.

Watch for this whenever a method "exists" but nothing happens: the node being
present says nothing about a behaviour being attached to *that* node.

## Compile pre-flight — ALWAYS before restart_emulator

`optix_build_check(project=...)` copies the NetSolution to a throwaway temp dir
and builds the copy there (Studio's own bin/obj untouched, so it can't race a
Studio build) and returns `{ok, error_count, errors:[{file,line,col,code,
message}], hint?, ...}`. Run it after editing any `.cs`. A NetLogic that does not
compile fails the build **silently** and takes the in-Studio bridge AND the
emulator down with it — build_check turns that into an instant file:line report.
It works whether or not Studio is open (it reads the `.cs` on disk).
`optix_read_netlogic` offers the same offline-read reach — it retrieves a
class's source by name without needing the bridge.

**`CS0104: 'X' is an ambiguous reference` after adding a comm driver.** Studio
injects `using FTOptix.RAEtherNetIP;` (and `FTOptix.CommunicationDriver`) into
**every** NetLogic `.cs` in the solution when a driver is added — including files
you wrote and files copied in. `FTOptix.RAEtherNetIP` exports its own `Encoding`,
which collides with `System.Text.Encoding`:

```
error CS0104: 'Encoding' is an ambiguous reference between
  'FTOptix.RAEtherNetIP.Encoding' and 'System.Text.Encoding'
```

Fix with a **using alias**, not by fully qualifying the call sites:
`using Encoding = System.Text.Encoding;`. One line covers every usage and
survives Studio re-injecting its usings later; qualifying the current sites
leaves the next `Encoding.` you write broken. This bit `StudioMCPBridge.cs` on
2026-09-02 — and remember a NetLogic that does not compile takes the in-Studio
bridge AND the emulator down with it, so it presents as "the bridge died", not as
a compile error.

**Read the `hint`.** If every error is `CS0246` on FTOptix/UAManagedCore types,
it is almost always **stale `.references` HintPaths** (pinned to a Studio version
not installed here, or the project moved between machines) — the project builds
fine in Studio, which regenerates its references. Do NOT treat that as a code
error; open/rebuild in Studio first. (References must resolve from within the
NetSolution, which is the case for a standard Optix project.)

## Bridge lifecycle — start, staleness, reopen

The bridge is a design-time NetLogic listening on loopback `127.0.0.1:8768`. Its
`Start()` does NOT auto-fire at design time, so **it must be started by hand** and
**it cannot start itself** (no channel is up when it is down).

### Start the bridge manually (search -> right-click -> Execute)

When `optix_bridge_status` says unavailable but Studio is open, re-arm it:

1. In Studio's **Project view Search box**, type `StudioMCPBridge` and press
   Enter — the tree filters to just that node at a stable position.
2. **Right-click** the node — the context menu lists its `[ExportMethod]`s as
   **`Execute <MethodName>`**.
3. Click **`Execute StartBridge`**. `optix_bridge_status` should now be available.

This search -> right-click -> `Execute <method>` sequence is the general way to run
ANY design-time method by hand (see below).

### A C# rebuild unloads the bridge; NEW BRIDGE CODE needs a Studio REOPEN

- Any NetLogic (C#) recompile unloads the bridge listener — after
  `optix_emulator(action="restart")` the bridge is down until you re-run StartBridge.
- **StartBridge after an emulator-only rebuild was OBSERVED to run the OLD bridge
  code.** The Optix docs say a DesignTime NetLogic's assembly is *reloaded from
  disk on every `[ExportMethod]` call* — but the bridge is a long-lived listener
  started from a prior load (a case the docs do not cover), and in practice a new
  build of `StudioMCPBridge.cs` did not take effect until the project was
  reopened. So, to be safe: if you edited `StudioMCPBridge.cs` itself, **close and
  reopen the Studio project** (which recompiles and reloads the assembly), then
  StartBridge. (Editing OTHER NetLogics is fine — only changes to the bridge's own
  code need the reopen; a normal generator/screen NetLogic edit does not.)

## Running design-time [ExportMethod]s (generators, card builders)

Design-time methods (a generator's `BuildFromDatabase`, a card builder's
`BuildCards`, etc.) **must run on Studio's UI thread**. Run them by hand: search
the node -> right-click -> `Execute <MethodName>` (same sequence as StartBridge).

**Do NOT invoke a design-time method in-process on the bridge's HTTP thread.**
It is off the UI thread and **crashes Studio** (observed: the whole process
died). A safe programmatic path requires main-thread marshaling in the bridge,
which is not shipped — until it is, use the Studio UI for these.

## Multi-project note

The bridge binds a single fixed port (8768, exclusive). With two projects open in
two Studio instances, only ONE bridge can hold the port — the other's StartBridge
fails to bind. `optix_bridge_status` reports which project the live bridge serves;
`project=` on the tools does not re-route bridge calls to a different Studio (a
mismatch surfaces as a wrong-project failure, not a silent write into the wrong
project).

## Restarting the MCP service (loading new tools/code)

The MCP tools are served by the ftx-mcp service (a scheduled task), separate from
Studio and the bridge. It is a long-lived process, so **edits to the service's
Python (new/changed tools) do not take effect until the service restarts** — a
running client keeps the old tool list.

- Restart: from the repo root, `.\bootstrap\services.ps1 stop` then
  `.\bootstrap\services.ps1 start` (`status` reports task state + the health
  probe). This bounces both `ftx-mcp` (the MCP/HTTP server) and
  `ftx-mcp-chrome-cdp` (the CDP task).
- A restart **drops the current MCP connection**; the client reconnects and
  re-lists tools. Verify with the dashboard health at `http://127.0.0.1:8765/health`
  and that `:8766` is listening.
- This is unrelated to the bridge — restarting the service does NOT start the
  bridge (that is still StartBridge in Studio, above), and restarting the bridge
  does not need a service restart.

## Deploying (shipping to a panel / production)

Deploy is **not** done through this MCP distribution — the deploy tool family is
disabled here on purpose; the standard loop is author -> `optix_emulator(action="restart")`
preview -> `optix_observe(mode="screenshot")` verify. When you are ready to ship:

- Use **Studio's own Deploy dialog** (UI) to push to a connected panel / runtime.
- Or the **Studio CLI export** for a USB panel image:
  `export "<projectPath>" --platform="Yocto_arm64" --location="<outputDir>"`.
- `optix_build_check` is the right **pre-deploy gate**: never ship a NetSolution
  that does not compile.

Promotion between environments (dev -> prod) is a per-screen manual step tracked
outside the MCP — check each screen's NetLogic config source and any hardcoded
host/path before promoting.
