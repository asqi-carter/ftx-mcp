---
name: optix-verify-loop
description: Verify an Optix change the fast way — emulator preview + CDP screenshot, restart cycle for structural edits, deploy only as the deliberate ship step. Use after ANY bridge edit you want to see, or when a screenshot doesn't show an edit you just made.
---

# Verify a change (emulator-first)

The emulator is the default verify path; UpdateSvc deploy is the SHIP step.
Never deploy just to look at a change.

1. **Check state first.** `optix_emulator_status` → `not_running` / `starting`
   / `running`. F5 TOGGLES — a blind run stops a running emulator. F5 also
   runs Studio's SELECTED deployment target: if the run tool refuses with
   `active_target_not_emulator`, the user's dropdown points at hardware —
   ask them to switch it to Emulator; never work around the refusal.

2. **Structural edit?** (new widget, new binding, size/layout, anything
   authored via `optix_bridge_*`): a running emulator will NOT show it — it
   renders its own loaded snapshot. One call: `optix_restart_emulator`
   (stops if running, starts, waits until serving; no save needed).

3. **Interactive-only exercise?** (clicking a switch, typing a value into an
   already-rendered widget): no restart needed — drive it live with
   `optix_cdp_click` / `optix_cdp_screenshot`.

4. **Wait for `serving:true`** in the run result (or poll status to
   `running`), THEN `optix_cdp_screenshot`. `starting` means the port isn't
   up yet — a screenshot now hits nothing.

4b. **Run reported launched but the emulator never spawns?** If
   `optix_run_emulator` returns `runtime_identity: "not_running"` with
   `probable_cause: "target_or_modal"` (or repeated starts just never serve),
   hypothesize FIRST that Studio's toolbar target dropdown is set to another
   target, or a modal dialog (credentials, NetLogic security warning) is
   eating the keystroke. Neither is visible to any tool. Ask the user to set
   the dropdown to Emulator and dismiss dialogs. NEVER retry-loop the run
   call — each press fires at whatever target is selected.

5. **Edit not visible in the screenshot?** Do NOT conclude the edit failed.
   In order: (a) did you `optix_restart_emulator` after a structural edit?
   (b) is status `running`, not `starting`? (c) right screen navigated?
   (d) re-screenshot with `fresh=true` (rules out a stale frame);
   (e) `optix_runtime_log_tail(contains="error")` — NetLogic exceptions land
   there; (f) container renders blank with children configured? Check the
   **container's own** Width/Height via `optix_describe_node` — a layout
   container created without a size can be 0×0 and hides every child, no
   matter how correct the children are. Only then diagnose the edit itself.

   **Diagnose by READING, never by writing.** Do not "rule out" hidden-state
   by setting Visible/Enabled/Opacity to their presumed defaults —
   `optix_describe_node` already shows their effective values. Every
   set_property on a fresh instance MATERIALIZES the property
   (`via:"materialized"`), permanently baking your no-op diagnostic into the
   project file as noise. A write that returns the default value changed
   nothing and proved nothing.

6. **Ship.** Once the preview is right, deploy from Studio's own Deploy
   dialog — shipping to hardware is the user's step, not yours.

Refs: README §Your first loop; `docs/fast-verify-loop-strategy.md`.
