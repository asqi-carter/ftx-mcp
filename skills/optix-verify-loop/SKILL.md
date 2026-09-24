---
name: optix-verify-loop
description: Verify an Optix change the fast way — emulator preview + CDP screenshot, ONE restart after ALL structural edits land, deploy only as the deliberate ship step. Use once you've finished authoring a batch of bridge edits, or when a screenshot doesn't show an edit you just made.
---

# Verify a change (emulator-first, restart ONCE)

The emulator is the default verify path; UpdateSvc deploy is the SHIP step.
Never deploy just to look at a change.

**Default cadence: author ALL structural edits for the screen/build first,
restart the emulator ONCE, verify ONCE.** A build with N components does not
get N restarts — it gets one, after the last component lands. Each
`optix_emulator(action="restart")` is ~10s (stop → start → wait-until-serving) plus a
re-render; restarting per-component on an 8-widget screen turns a ~10s check
into minutes of stacked restarts, for no extra signal — a mid-build
screenshot only confirms components you already validated by reading the
model (step 2 below). **A mid-build restart is a DEBUG action** for isolating
one specific edit you suspect is broken (see step 6) — it is not the normal
rhythm of authoring, and "restart to check" after every widget is exactly
the pattern this skill exists to stop.

1. **Check state first.** `optix_emulator(action="status")` → `not_running` / `starting`
   / `running`. F5 TOGGLES — a blind run stops a running emulator. F5 also
   runs Studio's SELECTED deployment target: if the run tool refuses with
   `active_target_not_emulator`, the user's dropdown points at hardware —
   ask them to switch it to Emulator; never work around the refusal.

2. **Author everything before you touch the emulator.** Land ALL structural
   edits for the batch (new widgets, bindings, layout, converters — anything
   authored via `optix_bridge_*`) first. Confirm each one landed by reading
   the model (`optix_describe_node`), not by restarting to look — a running
   emulator won't show structural edits anyway (it renders its own loaded
   snapshot), so restarting before the build is finished just re-shows you
   an incomplete screen and burns a cycle.

3. **One restart, at the end of the batch.** Once every structural edit is
   in: one call, `optix_emulator(action="restart")` (stops if running, starts, waits
   until serving; no manual save needed). Note F5 PERSISTS the live model to
   disk — a "temporary" bridge edit becomes permanent the moment you restart to
   look at it. If an edit was exploratory, revert it in the model BEFORE the
   restart, or be ready to revert on disk afterward.

4. **Interactive-only exercise?** (clicking a switch, typing a value into an
   already-rendered widget — no new bridge edit involved): no restart
   needed — drive it live with `optix_interact(action="click", ...)` /
   `optix_observe(mode="screenshot", ...)`.

5. **Wait for `serving:true`** in the run result (or poll status to
   `running`), THEN `optix_observe(mode="screenshot", ...)`. `starting` means
   the port isn't up yet — a screenshot now hits nothing. Omit `navigate_url`
   to shoot the CURRENT tab state; passing a URL navigates first, which RESETS
   transient UI state (open dropdowns, dialog overlays, unsaved form fields)
   before the capture.

5b. **Port mismatch or "connection refused" in the screenshot?** The web-engine
   port is now resolved per-project: `optix_observe` / `optix_interact` read the
   project's own `WebPresentationEngine.Port` node — via the armed bridge, or
   from `Nodes/UI/UI.yaml` when no bridge is up — rather than the machine-global
   `OPTIX_RUNTIME_TEST_PORT` (which is an operator override only). When the
   emulator is alive but serving on a different port than expected the tools
   self-heal and follow it automatically (`port_followed` appears in the result).
   For a persistent failure: `optix_status(action="doctor", project=...)` has a
   `web_port` row — `project_says` / `service_expects` / `listening` (pid + process
   name, so an Ignition-owned squatter is named) — and surfaces the runtime log's
   bind-fail signature with remedies. Pass `port=N` to override the resolver
   explicitly. A project with no engine node returns `source="no_engine"` with a
   nudge naming `optix_bridge_ensure_web_engine`.

5c. **Run reported launched but the emulator never spawns?** If
   `optix_emulator(action="run")` returns `runtime_identity: "not_running"` with
   `probable_cause: "target_or_modal"` (or repeated starts just never serve),
   hypothesize FIRST that Studio's toolbar target dropdown is set to another
   target, or a modal dialog (credentials, NetLogic security warning) is
   eating the keystroke. Neither is visible to any tool. Ask the user to set
   the dropdown to Emulator and dismiss dialogs. NEVER retry-loop the run
   call — each press fires at whatever target is selected.

6. **Edit not visible in the screenshot?** Do NOT conclude the edit failed.
   In order: (a) did you `optix_emulator(action="restart")` after the FULL batch of
   structural edits (not just the one you're eyeing)? (b) is status
   `running`, not `starting`? (c) right screen navigated? (d) re-screenshot
   with `fresh=true` (rules out a stale frame); (e)
   `optix_emulator(action="log", contains="error")` — NetLogic exceptions land
   there; (f) container renders blank with children configured? Check the
   **container's own** Width/Height via `optix_describe_node` — a layout
   container created without a size can be 0×0 and hides every child, no
   matter how correct the children are. Only then diagnose the edit itself.

   **This is the one place a mid-build restart is warranted:** once you've
   narrowed the suspect to a specific edit, isolate it (revert it or drop it
   from the plan), restart once to confirm the rest of the screen is clean,
   then reapply and restart again to confirm the fix. That's a deliberate
   two-restart debug detour for ONE stuck edit — not a template to repeat
   for every component going forward.

   **(g) READ THE RUNTIME LOG — do this BEFORE theorising about a cause.**
   `%LOCALAPPDATA%\Rockwell Automation\FactoryTalk Optix\Emulator\Log\<Project>\FTOptixRuntime.0.log`
   Converters and dynamic links are NOT silent: they log the failure with the
   offending node path (e.g. `ERROR;160007;Result data type 'NodeId'
   unsupported`). Neither the bridge nor the MCP surfaces this, so a screenshot
   alone cannot separate "wrong value" from "converter refused to run". Record
   `wc -l` before the restart and read only the new lines; a real project has
   standing noise (offline-PLC tag reads, USB/FTP, SQLite limits) to filter out.
   Measured 2026-09-01: two confident wrong root causes were asserted in one
   session that one look at this file would have ruled out.

   **`optix_save` FIRST if you intend to believe the result.** F5 staging does
   NOT reliably persist edits — an explicit save after several restarts still
   advanced the project mtime, meaning earlier "verifications" had run against
   stale state. A verify you plan to act on is worth an explicit save.

   **Diagnose by READING, never by writing.** Do not "rule out" hidden-state
   by setting Visible/Enabled/Opacity to their presumed defaults —
   `optix_describe_node` already shows their effective values. Every
   set_property on a fresh instance MATERIALIZES the property
   (`via:"materialized"`), permanently baking your no-op diagnostic into the
   project file as noise. A write that returns the default value changed
   nothing and proved nothing.

7. **Ship.** Once the preview is right, deploy from Studio's own Deploy
   dialog — shipping to hardware is the user's step, not yours.

**Navigation, popup, keyboard, and click-surface changes require a real click in the verify step** — these pattern classes fail silently, and a screenshot of a correctly-rendered screen proves nothing about whether the handlers fire. See `optix-web-click-surface`.

Refs: README §Your first loop; `docs/fast-verify-loop-strategy.md`.
