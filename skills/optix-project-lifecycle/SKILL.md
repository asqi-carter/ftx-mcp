---
name: optix-project-lifecycle
description: Cold-start a project with no human at the keyboard — create one from a Studio project template, open it, arm its bridge, and get past the NetLogic security warning. Use for "new project", "new from template", "open a project", "arm the bridge", "set up a scratch/demo project to test against", or when a tool answers bridge_unavailable / studio_window_not_found.
---

# Project lifecycle (nothing -> authorable, hands-off)

Three calls take you from no project to a live bridge. Everything below is
verified against Studio 1.7.4.32 / ftx-mcp 1.0.7.

```
optix_project(action="new", project="Probe01", template="MCPBridgeTemplate")
optix_project(action="open", project="Probe01")    # skip: "new" leaves Studio open on it
optix_bridge_arm(action="arm", project="Probe01")
```

`new` returns `studio_left_open: true` — go straight to `arm`. `arm` returns
`{state: "armed", port, consent_clicked}`; the port is where that project's
bridge now answers.

## Always test on a throwaway project

`create_variable` is **not idempotent** and emit teardown does not touch
`Model/`, so re-running a build into a used project silently duplicates seeded
variables while every op still reports success. A fresh project per experiment
is the cheap defence — that is what `new` is for. **Never experiment against a
production HMI project.**

## The template

A project template is an ordinary project that already contains the
`StudioMCPBridge` NetLogic, so anything created from it can be armed with zero
manual Studio work.

**Making one is the single step that cannot be scripted.** In Studio: open the
project that has the bridge NetLogic -> **Save ▾ -> Save As -> tick "Template"**.
Studio writes
`Documents\Rockwell Automation\FactoryTalk Optix\ProjectTemplates\FTOptix_template_<Name>\`.

- **Pass the PLAIN name** — `template="MCPBridgeTemplate"`, never the
  `FTOptix_template_` folder prefix. The prefix appears on the folder AND on the
  internal `.optixt` `Name:`, which is normal and is not what the CLI matches.
- **Do not hand-copy a project folder** into `ProjectTemplates`. Studio rejects
  it (`Cannot create a new project with invalid template`) because there is no
  `.optixt`. Copying is also wrong for a different reason: a project carries its
  name and GUID in the `.optix` manifest, the `.optix.design`,
  `Nodes\<Name>.yaml` and every NodePointer value, and a folder copy leaves a
  project that believes it is the original. `new` rewrites all of that.
- **Verify a template before trusting it.** The `.cs` file alone is not enough —
  the arm invokes the NODE's `StartBridge`. Check
  `Nodes\NetLogic\NetLogic.yaml` contains a `StudioMCPBridge` node with methods
  `StartBridge`, `StopBridge`, `SetupProject`, `CheckFormula`.

After creating, confirm `0` stale references to the template name survived the
rename, and that the `.optix` `Name:`/`GUID:` are the new project's.

## The NetLogic security warning — handled, but know what it is

Studio raises a modal when a project's NetLogics reference security-critical
assemblies (`System.Net.Sockets` etc). The bridge is an HTTP listener, so every
bridge-bearing project raises it.

**This is handled automatically now** — both `optix_bridge_arm` and
`optix_emulator(action="run"/"restart")` tick *"Do not show this warning again"*
and click **Proceed**. `arm` reports `consent_clicked: true`; the emulator
reports `consent_cleared: true` on the run that hit it, and omits the key
entirely once suppressed.

What matters if you ever touch this code:

- It is raised once per **TRIGGER**, not once per project. Before the checkbox
  was ticked, arm consented and then the very next F5 hit the identical dialog.
  Ticking it is what makes one dismissal cover the project's life.
- **`Cancel` is the focused default.** Never send keys to this dialog — a blind
  Enter cancels, leaves the emulator down, and leaves no trace of why.
- **Answering it lets the ALREADY-SENT F5 continue.** Do NOT resend F5 after
  clearing: F5 toggles, and a resend stops the emulator that just started.
- **UI Automation CAN see it.** `FindWindow`/Win32 enumeration cannot (Studio is
  native Qt and the modal is not a top-level window), but Qt draws modals into
  the main window's scene graph and `studio_uia.pending_dialog(pid)` walks
  exactly that. Screenshots and coordinate-clicking are NOT needed.

## Running a project's own design-time methods

`optix_execute_method(project, node, method)` right-click -> Executes any
`[ExportMethod]`. Three things it depends on, each of which cost a debugging
cycle:

* **The node must be a DESIGN-TIME NetLogic, and only Studio's GUI can make
  one.** `optix_bridge_create_netlogic` mints a node Studio treats as RUNTIME:
  the class compiles, the class binds, and the method nodes even show up in
  `optix_describe_node` — but the context menu carries **no `Execute` entries at
  all**, so nothing can fire them. The marker is `BehaviourStartPriority`, which
  a GUI-created design-time node has and a bridge-created one lacks (measured
  2026-09-01: 15 menu items on a design-time node vs 10, none of them Execute).
  Add it by hand: right-click **NetLogic -> Add -> DesignTime NetLogic**, named
  EXACTLY the C# class name. This is the second thing in this file that cannot
  be scripted, alongside making a template.
* **`optix_save` after creating or renaming the node.** The Execute gesture
  derives its tree chain from the ON-DISK NetLogic YAML, and Studio holds new
  method declarations in memory until a save. Before saving you get
  `menu_item_not_found` with a chain missing its folder ancestor. The node
  lookup also PREFIX-matches, so a stale `FooScript` shadows `FooScript2` —
  delete the dead one.
* **Stop the emulator first.** A running runtime window is foreground
  contention: every right-click returns `menu_items: 0`, the same signature as
  the just-opened-and-still-indexing flake but a different cause. Killing the
  runtime turned `0` into `19` on the identical call. Do not run these gestures
  concurrently with anything else driving the desktop.
* **An accidental Start menu does the same.** It happens on some boxes without
  anyone touching the keyboard. Recovery: send `{ESC}`, `AppActivate` the Studio
  pid, and CONFIRM it is the foreground window before retrying — the arm drives
  the Project tree by UI Automation and needs Studio genuinely in front.
* **`verify_timeout` alone does NOT mean the arm failed.** It has been reported
  while the bridge started anyway (the port appeared a moment late). The
  authoritative check is Studio's own log:
  `%LOCALAPPDATA%\...\FTOptixStudio\Log\FTOptixStudio.0.log` contains
  `INFO;StudioBridge;;listening on http://127.0.0.1:<port> (started via StartBridge())`
  on a real arm. No such line = it never fired.
* **Retries do not converge.** Tree row positions drift between reopens; the
  same call failed three times with `menu_items: 0` on the first row, then
  succeeded unchanged. Try the Escape/refocus recovery once, then ask the user
  to right-click `StudioMCPBridge` → Execute StartBridge rather than looping.

`ok: true` means the CLICK landed, never that the method worked — arbitrary
methods have no signal to verify against. Confirm with `describe_node` or a
render, and read Studio's Output pane for the method's own logging.

## Multi-instance hygiene

- Bridge 1.0.7 self-binds the first free port in **8768..8771**, in whatever
  order instances arm — so **four armed projects maximum**, and the port is
  never assumed from last time. `optix_bridge_status` lists every bridge with
  the project it serves.
- With more than one bridge armed, **pass `project=` on every call**.
  `optix_active_target` refuses to guess and returns
  `{known: false, reason: "ambiguous_bridge"}` rather than silently reading the
  lowest port.
- `optix_bridge_arm(action="stop", project=...)` frees a port for another
  instance.
- The **emulator port (8088) is fixed and global** — stop one project's emulator
  before running another's, or the second never serves.
- Arming can fail transiently right after an open, while Studio indexes the
  tree: the NetLogic row shows 0 menu items instead of 14. Retry once before
  diagnosing anything. Going through `optix_project(action="open")` largely
  avoids this, because it waits on the window's project identity rather than on
  process existence or a sleep.

## Demo and reference projects

`optix_list_projects()` enumerates what is under the projects root. Reading a
**closed** project's YAML on disk is safe, free, and often the fastest way to
learn a construct the bridge cannot introspect — `AdvDynLink` carries working
StringFormatter dynamic links, for instance.

**Never edit project YAML on disk while that project is open in Studio.** Studio
owns the in-memory model and writes it back on save, so a concurrent on-disk
edit is clobbered or corrupts the project. That includes `git checkout` of a
tracked model file. To revert: undo through the bridge, or close Studio first
and only then restore the file.

Note the projects root is not the only place projects live — one may sit on the
Desktop or elsewhere, in which case `optix_list_projects` will not show it while
`optix_bridge_status` still names it. Resolve by the name the bridge reports.

## Traps

| symptom | cause | fix |
|---|---|---|
| `Cannot create a new project with invalid template: <T>` | the `-u` flag was missing from the CLI invocation, so Studio ran NewProject during `IDEContext::Initialize` with the user template library not yet indexed | fixed in 1.0.8; if it recurs, confirm the argv still carries `-u` alongside `--template` |
| same error, and argv looks right | template name passed WITH the `FTOptix_template_` prefix, or the template was hand-copied | pass the plain name; re-make via Save As -> tick Template |
| `FTOptixStudio new` "fails" with `0xC0000005` | the CLI always returns that as it tears down, successful runs included | judge by the `.optix` artefact, never the exit code |
| `studio_window_not_found` from arm | project not open | `optix_project(action="open")` first — arming cannot conjure a window |
| arm: NetLogic row has 0 menu items | Studio still indexing after open | wait ~30 s, retry once; then check the template actually has the `StartBridge` NODE |
| emulator `serving: false`, `probable_cause: target_or_modal` | a modal ate the F5, or the toolbar target is not Emulator | check `consent_cleared` in the result; then `optix_active_target` — the live UIA read, not the config file, which lags |
| emulator never serves, second project | 8088 already held | stop the other project's emulator |
| emulator `serving: false` forever, but the runtime IS up | the project's `UI/WebPresentationEngine` `Port` differs from the service's probe port | make them agree — read the engine's `Port` with `describe_node`, or point the probe at it |
| tools edit the wrong project | `project=` omitted with several bridges armed | always name the project |
| `execute_method`: `menu_items: 0` on every row | foreground contention (a running emulator window, another UIA driver) | stop the emulator; never run two gestures at once |
| `execute_method`: `menu_item_not_found`, chain missing the folder | node's methods not yet in the on-disk YAML | `optix_save`, then retry |
| a design-time method has no `Execute` entry | node was created by the bridge, not Studio's Add -> DesignTime NetLogic | recreate it via the GUI |
| a screen area renders blank though the model looks right | `UI/MainWindow` is 400x400 in a fresh project and clips everything below | set the window size before debugging the binding |
| file-path tools refuse a project the bridge happily serves | it lives outside `OPTIX_PROJECTS_ROOT`; `resolve_project` refuses | set the env var, or use bridge-only tools for it |
