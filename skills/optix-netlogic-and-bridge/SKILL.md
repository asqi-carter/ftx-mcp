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
   equal the class name exactly. This mints a real `NetLogicObject` node. Without
   `FTXMCP_BRIDGE_PRIMITIVES` use the batch op instead:
   `optix_bridge_edit(ops=[{"op": "create_netlogic", "parent": "<path>", "name": "MyLogic"}])`
   (same implementation and result).
3. `optix_emulator(action="restart")` — the rebuild instantiates it; `Start()` runs.

**Placement matters.** A runtime NetLogic that reads sibling nodes through `Owner`
must be created UNDER the object that owns them (pass that object as `parent`,
e.g. a screen). A self-contained one can live under any loaded container; a
NetLogic category folder runs at app start.

**`create_netlogic` waits for Studio's template write (`netlogic_template`).** Studio writes a default
`Method1` template to `ProjectFiles/NetSolution/<Class>.cs` shortly after the node is created. The
create returns only once that file exists, is non-empty and unchanged for a settle window (max 15 s),
and the result carries `netlogic_template: {file, ready, waited_seconds[, reason]}`. If the wait times
out the result has `ready: false` and a `netlogic_template_write_pending` warning (also on the
`optix_bridge_edit` batch surface): the file is NOT ready - do not write your `.cs` yet; re-read it
(`optix_read_netlogic`) until it stops changing, otherwise a late Studio write can overwrite your content.

**A slow first create is not a failure (`verified_after_timeout`).** When the class
is not compiled yet, Studio compiles during the create and it can take well over 8 s.
The service waits up to 90 s and NEVER re-sends the create after it was sent (a re-send
only yields `name_exists`). If the bridge still times out or drops the response, it looks
the node up; if it exists the result is `ok: true` with `verified_after_timeout: true` and
a `create_netlogic_timeout_verified` warning. A `name_exists` from the single create is a
real duplicate. If the node cannot be confirmed the original error is returned: check the
model (`optix_describe_node`) before trying again, do not blind-retry.

**Deleting a NetLogic never destroys your C# (`netlogic_source`).** Studio drops
`ProjectFiles/NetSolution/<Class>.cs` when the LAST node bound to the class is deleted
(it is not in the Recycle Bin), and a later `create_netlogic` of that name regenerates a
default `Method1` template over it. The service snapshots the file around `delete` (also
inside an `optix_bridge_edit` batch) and `create_netlogic` and restores it. The result
then carries `netlogic_source` (`file`, `class`, `preserved`, `backup`) and a
`netlogic_source_preserved` warning (batch: top-level `warnings` with `op_index`). The
`.cs` stays on disk as an unused class - delete the file by hand if you want it gone; a
later `create_netlogic` with that name binds to the preserved class. A
`netlogic_source_restore_failed` warning means the file still differed after bounded
restore attempts (the service waits for Studio's write-back to settle, restores via temp
file + `os.replace`, and re-verifies after a second stability window); the copy is at the
`backup` path (`ProjectFiles/.ftx-mcp-backups/<Class>.<ts>.cs.bak`).
A class still used by other nodes keeps its file and nothing is reported.

**The restore is backup-first and guarded after the call returns.** The `.cs` bytes are copied to
`ProjectFiles/.ftx-mcp-backups/<Class>.<ts>.cs.bak` BEFORE the delete is sent and the copy is removed
once the restored file is verified byte-for-byte (an untouched file leaves no backup). If Studio keeps
rewriting the file past the stability window the result says `preserved: false` with a
`netlogic_source_restore_failed` warning that names the backup - recover from that copy.
For 60 s after a restore, the NEXT bridge/NetLogic call for the same project re-checks the file; if
Studio rewrote or deleted it late, it is restored again and that call's result carries a
`netlogic_source_restored_late` warning (`file`, `class`, `drifted_copy`). The rewritten bytes are
first saved as `<Class>.<ts>.drifted.cs.bak`, so a deliberate edit of yours is never lost. Treat
`netlogic_source_restored_late` as "your source is back, check it"; do not re-run the delete.

**The late guard only undoes corruption, never your edit.** It restores the snapshot only when the file
has NUL bytes or is the snapshot plus an appended tail. Any other difference (a complete, valid C# file
you wrote yourself) is treated as a deliberate edit and left alone, with no restore and no `.drifted`
copy. A deliberate change of that class's source (`create_netlogic` with the same name, a NetLogic
source write/edit, `delete_node`, project close/open) clears the guard for that file first.

**`create_netlogic X` when `X.cs` already exists** (a leftover kept by an earlier delete): the existing
file is copied to `ProjectFiles/.ftx-mcp-backups/` before create runs. If its content changed during
create, the result carries a `netlogic_source_existed` warning with the `backup` path; create never
silently replaces an existing `.cs`. Recover your earlier source from that backup, or use a fresh
class name.

**Delete after you edited the class is safe too, and a slow delete is not a failure.**
Deleting a NetLogic whose `.cs` you changed makes Studio recompile, which can take longer
than the 8 s default. `delete` (`optix_bridge_delete_node`, and the `delete` op in an
`optix_bridge_edit` batch) waits up to 90 s and does not blindly re-send. After a timeout it
checks the model: node gone -> `ok` with `verified_after_timeout: true` and a
`delete_node_timeout_verified` warning (never `node_not_found` / `write_failed` for a delete
that happened); node still there -> one re-send. The source snapshot is restored even when the
call errors (`netlogic_source` on the error). Do not retry a delete by hand after a timeout;
check `optix_describe_node` first.

There is NO Studio "New -> NetLogic" menu step required. (This was long believed
to be a wall; it is not — the wall was creating a plain `BaseObjectType` named
like a class, which never binds.)

### Design-time NetLogic: `kind="design_time"` (reopen may be required)

`optix_bridge_create_netlogic(parent, name, kind="design_time")` also mints the
`BehaviourStartPriority` child with VariableType `BehaviourStartPriorityVariableType`
(Value 180). That is what makes Studio treat the node as a design-time NetLogic, so its
`[ExportMethod]`s become `UAMethod` children once Studio loads them. **`optix_save`
alone does not guarantee that and there is no refresh trigger: reopen the project
if the children are missing.** Until then `optix_execute_method` returns
`export_method_not_loaded` (with a reopen nudge), not `menu_item_not_found`.
Instantiating bridge writes (`create_netlogic`, `create_object`, `convert_to_type` replace, `move_node`
copy, `invoke_method`) run under `Sessions.ImpersonateRootTemporary()`; without that session Optix
kills Studio (c0000374) when the class is already compiled in the NetSolution (#27). A live test
class must not `using FTOptix.Core.Log;` (Log lives in `UAManagedCore`), and
`execute_design_method` takes the NetLogic NAME, not a path.
`dry_run=True` reports the plan; an unknown `kind` returns `invalid_kind` (allowed:
`runtime`, `design_time`) and writes nothing. Sequence: author the `.cs` →
`create_netlogic(kind="design_time")` → `optix_build_check` → `optix_save` →
`optix_describe_node` to confirm the method children → wire / execute.
Right after the create, Studio rebuilds the user .NET solution (about 30 s; the create result
carries `netsolution_build: {state: "building", note}`) and drops Ctrl+S meanwhile.
`optix_save` therefore re-sends Ctrl+S every 5 s until the write lands (the second keystroke
clicks the project tree first, `method: "tree_focus_ctrl_s"`; Studio 1.8 has no File menu) and
keeps polling 15 s after the last keystroke. It reports `keystrokes_sent` and `write_latency_s`;
on `saved: false` check `optix_studio_log(contains="solution")` and retry.

### Runtime-kind NetLogic: `[ExportMethod]`s need a Studio REOPEN before anything can call them

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

`optix_describe_node` on a Variable (e.g. `Model/X`, or a NetLogic's `BehaviourStartPriority`) returns its own
`datatype` and `value` (the value inside the `<untrusted>` envelope), plus `target_path` for a link
(`target_missing` when dangling); its `properties` list stays empty.

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

**Bridge source (`studio-bridge/StudioMCPBridge.cs`): the bench `build_check` is the
ONLY compile gate.** The executor sandbox has no dotnet, so unit tests cannot prove the
bridge compiles; only `ci/w11-validate.sh <sha>` (its `build_check` gate) does. Every PRD
touching `studio-bridge/` must name the SDK members it uses; never call an SDK member
that is not listed there or already used in the bridge (e.g. there is no `ModellingRule`
type - `IUANode.ModellingRule` is a `NamingRuleType` - and no `RemoveTranslation`).

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

### `optix_bridge_arm` syncs the shipped bridge source (`bridge_source`)

`arm` always runs the bridge this service ships, not whatever copy the project holds. It
compares the project's `ProjectFiles/NetSolution/StudioMCPBridge.cs` with
`studio-bridge/StudioMCPBridge.cs` by a normalized content hash (BOM, CRLF/CR and trailing
spaces/tabs ignored) and reports the verdict as **`bridge_source`**:

| `bridge_source` | Meaning |
|---|---|
| `current` | Hashes match; nothing written. |
| `synced` | Hashes differed. The project file was backed up to `ProjectFiles/.ftx-mcp-backups/StudioMCPBridge.<ts>.cs.bak` (`bridge_backup`), the shipped copy was written, and arming proceeded (StartBridge rebuilds from disk). |
| `stale` | Hashes differ and nothing was synced: `arm` with `FTX_BRIDGE_SYNC=0` (the result carries a `warning`), or `optix_bridge_status` / health reporting that the project copy is out of date (re-arm to sync). |
| `unknown` | The comparison could not be made (no/unreadable file, project dir not found, or nothing armed). |

`bridge_project_hash` / `bridge_shipped_hash` (12 hex chars) accompany the verdict. A project with
no `StudioMCPBridge.cs` still fails with `bridge_netlogic_absent`; `stop` never syncs.

- **`FTX_BRIDGE_SYNC=0`** (service environment) turns the sync off: `arm` leaves the project
  file alone and reports `stale` with a warning. Use it only to deliberately run a hand-edited bridge.
- `optix_bridge_status` returns `bridge_source` per bridge entry and as a top-level aggregate
  (any `stale` wins, then `unknown`, else `current`); health reports the same aggregate.
- A synced bridge is new bridge code: see the REOPEN note above if Studio already has the old one loaded.
- For the Studio project **template**, follow the manual copy steps in `docs/runbook.md`; projects
  made from an old template are synced on their first arm.

## Running design-time [ExportMethod]s (generators, card builders)

Design-time methods (a generator's `BuildFromDatabase`, a card builder's
`BuildCards`, etc.) **must run on Studio's UI thread**. Run them by hand: search
the node -> right-click -> `Execute <MethodName>` (same sequence as StartBridge).

**Do NOT invoke a design-time method in-process on the bridge's HTTP thread.**
It is off the UI thread and **crashes Studio** (observed: the whole process
died). A safe programmatic path requires main-thread marshaling in the bridge,
which is not shipped — until it is, use the Studio UI for these.

## Gotchas when authoring NetLogic and generators

- **`GetVariable(name)` is null for an uninstantiated optional property.** An optional
  property that was never instantiated has no variable node, so
  `node.GetVariable("Visible")` returns `null` and a `.Value` access throws a
  `NullReferenceException`. Use the typed accessors instead, which create/return the
  property: `VisibleVariable`, `TextVariable`, etc. (e.g. `label.VisibleVariable.Value`,
  `label.TextVariable`). Null-check any remaining `GetVariable` result.
- **An RAEtherNet tag is polled only while a widget binds it.** The driver reads a tag
  only while something on screen is bound to it. A tag used solely by a NetLogic (no
  widget binding) is never polled and its value goes stale. For NetLogic-only tags add a
  `RemoteVariableSynchronizer` (in the NetLogic or on the model) that lists the tags to
  keep them polled.
- **`wire_event` output does not survive a wipe-and-rebuild generator.** A generator that
  deletes and recreates the screen/nodes (wipe-and-rebuild) discards any EventHandlers
  added earlier with `wire_event`. Author the EventHandlers in C# inside the generator
  so they are recreated on each rebuild, and use `wire_event` only for nodes that persist.

## Multi-project note (one bridge per Studio, identified by project)

Since 1.0.9 each running Studio has exactly ONE bridge, and the bridge says which
Studio it is. `/bridge/health` (and the registry file) return `studio_pid`,
`project_name` and `project_dir` next to the older fields. Arming a Studio that
already has a live bridge is idempotent: `StartBridge` returns that bridge instead
of binding a second port, and the service's `already_armed` is only ever reported
for the bridge whose health names that Studio pid (never another Studio's).

Every bridge-calling tool resolves its bridge through one function,
`resolve_bridge(cfg, project)`, keyed by identity — never by port order or
first-found. It matches the project by name (case/separators normalised, `X` =
`X/HMI`) or by directory (a directory match decides first).

**`project_path` semantics.** `project_path` in `/bridge/health` and the registry is the folder
that contains `<project>.optix` (a `.optix` file path is also accepted). It is never Studio's working
directory (`C:\Windows\System32` is reported as unknown) and, since 1.0.9, never the `ProjectFiles`
subfolder: the bridge reports the parent when it holds exactly one `.optix`. The service applies the same
rule to an older bridge's `ProjectFiles` path, and refuses to guess when the parent holds several
`.optix` files. Prefer `project_dir` / `project_name` to identify a bridge.

The registry file is written as UTF-8 WITHOUT a BOM (`new UTF8Encoding(false)`), and the service
reads it with `utf-8-sig`, so an entry with a BOM from an older bridge is still listed (#28; before,
it was silently skipped and the candidate list came back empty).

- **No bridge names the project** -> `no_bridge_for_project`, listing every bridge
  found with its port, project, directory and Studio pid. Fix: arm that project's
  Studio (`optix_bridge_arm`) or pass the right `project=`. File-based fallbacks
  still work, since it is a `BridgeUnavailable`.
- **More than one bridge names it** -> `ambiguous_bridge_for_project` (HTTP 409).
- With several bridges armed, `optix_bridge_status` / `/ui` no longer single out
  the lowest port at the top level; read the `bridges` list.
- Pass `project=` on every call when more than one Studio is open.

## Restarting the MCP service (loading new tools/code)

The MCP tools are served by the ftx-mcp service (a scheduled task), separate from
Studio and the bridge. It is a long-lived process, so **edits to the service's
Python (new/changed tools) do not take effect until the service restarts** — a
running client keeps the old tool list.

- Restart: from the repo root, `.\bootstrap\services.ps1 stop` then
  `.\bootstrap\services.ps1 start` (`status` reports task state + the health
  probe). This bounces both `ftx-mcp` (the MCP/HTTP server) and
  `ftx-mcp-chrome-cdp` (the CDP task).
- Autostart is opt-in: `.\bootstrap\services.ps1 enable-autostart` adds an at-logon trigger to the
  `ftx-mcp` service task **only** (never Studio, the emulator or the CDP task);
  `disable-autostart` returns to manual start. Guarded by
  `service/tests/test_services_autostart.py`.
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

## Live test runs: `session_0_no_desktop`

GUI-touching live tests (anything that calls `core.save` / `core.restart_emulator`, which need Studio's
window) SKIP with reason `session_0_no_desktop` when the pytest process has no input desktop (session 0,
a service, or an SSH shell). A skip is not a pass: run the live suite in the interactive session
(`ci/w11-validate.sh <sha>` does, via an Interactive scheduled task) and check `live_<sha>.log` for
`session_0_no_desktop` skips. `FTX_LIVE_SESSION=0|1` overrides detection. See `ci/README.md`.
