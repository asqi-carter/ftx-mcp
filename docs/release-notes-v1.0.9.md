# ftx-mcp v1.0.9 — release notes

See `CHANGELOG.md` (1.0.9) for the full list of changes in this release.

## Summary

1.0.9 adds support for FactoryTalk Optix Studio 1.8.0.555 (arm, consent
clear, project open, `import_tags`) while keeping Studio 1.7 working, and
hardens the service for day-to-day use: several projects and several Studios
at once, guards against bridge crashes and lost NetLogic sources, typed PLC
tags, more reliable screenshots after an emulator restart, an arm that keeps
the project's bridge source current, and a large cut in per-call overhead. The
final finishing batch makes `optix_emulator(action="run")` safe to repeat and
closes a handful of smaller gaps (trend pen `Color`, `Folder`, arm dialog
hint, `services.ps1 status`, `open` attribution, live-suite hygiene).

Studio 1.7: regression-tested only. Studio 1.8.0.555 is the target version;
1.7 is covered by the regression suite but not by new feature work.

## Highlights

The groups below follow the 1.0.9 section of `CHANGELOG.md`.

### Optix 1.8 support

- Arm finds Project-tree row labels at any depth, sends keys only once the
  Search box is confirmed focused, and reports `focus_unconfirmed` otherwise.
- Consent clear resolves the project directory itself; an unknown project
  returns `project_not_found`.
- `optix_project(open)` returns `upgrade_required` for a project that needs a
  Studio upgrade. The upgrade dialog is never clicked automatically.
- `import_tags` works through the station's own Tag Importer on 1.8 and is
  refused at dry run on earlier Studio versions.
- The bridge reports the Studio product version, so version gates such as
  `pow` behave correctly on both 1.7 and 1.8.

### Several projects at once

- Emulator lifecycle (`run`, `restart`, status) is per project across several
  open Studios; `stop` stays host-wide.
- One identity-keyed bridge resolver replaces port-order selection, the bridge
  names its Studio, and arm is idempotent per Studio process.
- Per-project CDP tab registry, web-port discovery and port-clash detection.
- `optix_project(open)` reports a project open only when a Studio really has it
  loaded, and (see below) attributes it by live window or command line.

### Crash guards

- Writes that instantiate nodes run under a root session, so a failing method
  or create is logged and Studio stays up.
- `set_modelling_rule` compiles again and `remove_translation` is reported as
  `not_supported`; a source-level guard test protects the bridge C#.
- `create_object` / `create_variable` never report success without a node.
- Subtree refusals (move, rename, convert on NetLogic and event handler
  subtrees) are per-op `refused_ops`.

### NetLogic safety

- Deleting a NetLogic no longer destroys the user's C# source: the restore is
  backup-first, byte-verified, atomic and guarded after return.
- `create_netlogic` returns only after Studio's template `.cs` write has
  finished, and no longer retries its non-idempotent POST on a timeout.
- The late guard restores only the corruption signature, never a legitimate
  edit. Design-time NetLogics are supported (`kind="design_time"`).

### PLC tags

- `create_variable` resolves typed driver tag VariableTypes by short name,
  `Module.Type` or type path, and refuses ambiguity.
- `import_tags` (and the `optix_bridge_edit` op) uses the station's own Tag
  Importer, else a temporary one it deletes afterwards. On Studio 1.8 only.
- `Module.Type` spellings agree between dry run and apply.

### Screenshot reliability

- `optix_observe` and `optix_cdp_restart` recover a hung or detached page
  target; `optix_emulator(action="restart")` returns only when its page is
  usable (`page_ready`).
- A fresh capture identical to the previous one is reported, and the
  self-heal never replaces a valid capture.
- `optix_observe(mode="dom_text")` reads exact WebUI strings.

### Bridge sync on arm

- The first `optix_bridge_arm` on a project syncs the shipped
  `StudioMCPBridge.cs` into it and leaves a backup under
  `ProjectFiles\.ftx-mcp-backups\`.
- `optix_bridge_status` and health report `bridge_source`
  (`current` / `stale` / `unknown`).
- Projects made from an old template are synced on their first arm; to refresh
  a BridgeTemplate itself, follow the manual copy steps in `docs/runbook.md`.

### Performance

- Every bridge-routed tool call used to lose seconds of fixed overhead per call
  on some machines (9–14 s on one box with endpoint protection, about 2 s on a
  plain VM): the Studio process scan read the command line of every process, and the bridge
  lookup probed the whole port range. Both are fixed; the registry-listed ports
  are probed first (PR #5, credit Jonathan Callahan).

### Finishing batch (PRD 0044, items A-G)

- **A. Emulator `run` is idempotent.** If this project's emulator is already
  `running` or `starting`, `run` sends no F5 and returns
  `already_running` with a hint to use `restart`. When the project name differs
  from the cache folder but the emulator serves this project's web port, scoped
  status reports `running` with `attribution: "port"` instead of `stopped`. The
  docstring and skill name the real states. `run` is safe to repeat; `restart`
  reloads edits; status first is optional.
- **B. Trend pen `Color`.** `set_property` of a property the node's type
  declares as an optional child, but the instance lacks, creates that child and
  sets it (`created_optional_child`). A fresh pen from `create_variable` takes
  `Color` in one call. Undeclared properties still return `unknown_property` (the service guard).
- **C. `create_object` `Folder`.** `Folder` and `FolderType` create a standard
  OPC UA folder; if a Studio version cannot, `type_not_found` names the working
  route.
- **D. Arm explains a blocking OS dialog.** When arm's menu or click step fails
  and a foreground window not owned by Studio is present, the error carries
  `probable_cause: "os_dialog"`, `blocking_window` and a hint to close or answer
  the dialog and retry.
- **E. `services.ps1 status` during startup.** Reports `starting` while the
  process lives and its ports listen but `/health` does not answer yet; `start`
  waits up to 30 s for `/health`, prints `up` with the elapsed time or
  `still starting`, and exits 0. `/health` itself answers fast: it no longer
  probes bridges, so a stale registry file or slow port cannot stall it.
- **F. `optix_project(open)` attribution (#46).** `already_open` from the
  Studio log is reported only when that pid's live window title or command line
  names this project, so a second Studio holding another project is no longer
  named.
- **G. Live-suite hygiene (#43).** NetLogic race tests use unique names and
  clean up; the observe-immediately test restarts the emulator and waits on
  `serving`; the slowest tests are marked `slow_live` so the default Tier-2 run
  fits its time limit. Live tests still fail, not skip, when the bridge is up
  and setup fails.

### Native presentation engine (PR #6, Jonathan Callahan)

- `optix_observe` and `optix_interact` take `target="native"` (default
  `"web"`) to verify a project running the native Qt renderer, with no Chrome,
  CDP or `WebPresentationEngine` node. Every observe mode but `diff` honors it.
- Capture reads the FTOptixRuntime.exe window with Win32 `PrintWindow`
  (`PW_CLIENTONLY | PW_RENDERFULLCONTENT`), so the window may be covered by
  others.
- Input is posted straight to that window (`click` / `fill` / `type` / `key`);
  focus is never taken. `type` needs a prior `click` to focus the field, and
  `fill` is click + type + submit.
- Coordinate-frame rule: the native frame is the window's client pixels.
  `x` / `y` are fractions of the frame when both are <= 1, otherwise absolute
  client pixels. Results report `px` / `py` and `frame`; `find_text` centers
  from observe feed interact as-is.
- A capture that is entirely black is flagged `blank: true` instead of being
  passed off as a valid frame.
- Failures are structured (for example `native_runtime_not_found`), not
  exceptions.
- The window must not be minimized; covered is fine.
- OCR on small UI text is unreliable on either engine. On web use
  `optix_observe(mode="dom_text")` for exact strings; on either engine fall
  back to screenshots plus vision.

### Packaging

- The wheel now bundles the authoring skills (`skills/*/SKILL.md`), the Studio
  bridge source (`StudioMCPBridge.cs`) and the `/ui` dashboard. `setup.py`
  copies the skills and bridge source into `service/_bundled/` in the build
  directory only; `service/_bundled/` is gitignored and absent from the
  checkout. `MANIFEST.in` keeps the sdist complete.
- `_skills_dir()` resolves `OPTIX_SKILLS_DIR`, `<repo>/skills`,
  `<repo>/.claude/skills`, then the bundle; `_shipped_bridge_cs()` replaces the
  `_SHIPPED_BRIDGE_CS` constant (`<repo>/studio-bridge/...`, then the bundle) and
  is used by bridge sync, arm and the "not readable" warning. Checkout installs
  behave as before.
- New `GET /bridge-source` returns the shipped `.cs` as `text/plain` with an
  `X-Ftx-Mcp-Version` header, so a pip user can fetch it.
- Pillow is a hard dependency (`Pillow>=10`); `[visual]` is an empty extra.
  Without Pillow, native screenshots return `error: "pillow_missing"` with hint
  `pip install Pillow` instead of a raw `ImportError`.

### Docs

- README and `docs/tool-reference.md` match the default registered surface
  (36 tools; 19 bundled playbooks; 22 gated per-noun bridge primitives).
- One documented upgrade procedure for checkout installs (below).

## Known issues

- **#45/#51/#54 fixed:** saves during Studio's NetSolution build are re-sent until
  they land. `optix_save` sends Ctrl+S and re-sends it every 5 s until the project
  mtime advances (the second keystroke clicks the project tree first; Studio 1.8
  has no File menu), within `timeout` (30 s), then keeps polling 15 s after the last
  keystroke because Studio can write the YAML ~10 s after it. The result reports
  `method`, `keystrokes_sent` and `write_latency_s`; `title_clean` and the File-menu
  attempt are gone. `bridge_create_netlogic(kind="design_time")` returns
  `netsolution_build: {state: "building", note}`. Bench-verified on Studio
  1.8.0.555 and 1.7.4.32 (an immediate save after a NetLogic create lands in
  about 27 s, 4-5 keystrokes).
- **`optix_save` on a long-running Studio reports `method: tree_focus_ctrl_s`
  with 2 keystrokes** when the first Ctrl+S was in fact taken: Studio can take
  ~11 s to write the YAML, longer than the 5 s re-send cadence, so the second
  keystroke goes out before the first one's write lands and `write_latency_s`
  is measured from that second key. The save itself lands (~12 s). Cosmetic;
  tracked for 1.0.10.
- **Open at release (tracked for 1.0.10, none blocks normal use):**
  - A second `optix_project(action="open")` of a project whose Studio is still
    showing the Project Upgrade dialog launches another Studio instead of
    pointing at the first one (`studio_left_open` names each). Answer or close
    the dialog before retrying (#102).
  - On Studio 1.7 the standalone `optix_bridge_import_tags(dry_run=true)` on
    a Rockwell EtherNet/IP station reports what it would add although the real
    call refuses `requires_optix_1_8`; the batch op's dry run refuses correctly
    (#101).
  - Lookups of a project that does not exist answer in milliseconds for
    `run`/`restart`/`status`, but `describe` still takes about 4 s and
    `open`/`save` several seconds per unarmed Studio; `upgrade_required` is
    reported 8-10 s after the call (#95).
  - Result-shape nits: a create confirmed after a bridge timeout omits
    `variable_type`; `add_translations` apply has no `written` count;
    `restart` while running has no `state`; `studio_left_open` is `true` on a
    successful `new` and `{pid}` elsewhere. A design-time NetLogic
    (`kind="design_time"`) leaves the project DLL locked by Studio, so later
    builds of that project fail until Studio is restarted (#103).
  - Two runtimes of the same project are reported without saying which is
    which (#71); a Chrome tab that stops answering CDP is not recovered
    automatically (#77); cosmetic items #58, #68, #75, #89.
- **Studio 1.7: regression-tested only.** New 1.8 behaviour (for example
  `import_tags`) is not available on 1.7.

## Upgrading

To upgrade a checkout install (e.g. 1.0.8 → 1.0.9), run these five steps, in order, from a regular PowerShell window in the checkout:

1. `bootstrap\services.ps1 stop`
2. `git pull` (or replace the checkout)
3. `bootstrap\uninstall.ps1 -PurgeVenv`
4. `bootstrap\setup.ps1`
5. `bootstrap\services.ps1 start`

Your state, secrets and logs live in `%LOCALAPPDATA%\ftx-mcp\` and survive
the procedure (`-PurgeVenv` removes only the virtualenv).

- **Existing projects:** the first `optix_bridge_arm` on each existing project
  syncs its bridge source to the shipped version and leaves a backup of the
  previous one under `ProjectFiles\.ftx-mcp-backups\`.
- **BridgeTemplate:** to refresh a Studio project template, close Studio, then
  copy `studio-bridge\StudioMCPBridge.cs` over
  `<Documents>\Rockwell Automation\FactoryTalk Optix\ProjectTemplates\FTOptix_template_<TemplateName>\ProjectFiles\NetSolution\StudioMCPBridge.cs`.
  New projects copy the template folder. Projects already made from an old
  template are synced on their first arm anyway.
- **`pip install --upgrade` is not a supported route.** It does not upgrade a
  checkout install: it can bump the pip-reported version while the service
  keeps running the checkout's code. Use the five steps above.
