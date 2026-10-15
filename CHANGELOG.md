# Changelog

All notable changes to ftx-mcp. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/). Per-release detail lives in
`docs/release-notes-v<version>.md`.

## [1.0.9] - 2026-10-15

> Bench verification: every 1.0.9 entry was commissioned on a dedicated Studio bench (1.8.0.555 target, 1.7.4.32 regression smoke) in release rounds r1–r16 before tagging; a "pending bench verification" note inside an entry is the state at merge time, not at release (r16, 2026-10-14, verified PRD 0047: #53 #54 #55 #56).


- **Missing-project lookups are bounded, `studio_version` is cached, `upgrade_required` arrives fast, re-arm probes the target's port first (`service/core.py`, `service/studio_arm.py`, `service/tests/test_missing_project_bounded.py`, `service/tests/test_studio_arm.py`; PRD 0056 E, ftx-mcp-w3d.5, #95, #100).** (1) `resolve_project` / `resolve_project_info` run inside a per-call scope (`_resolve_scope`): `run`, `restart`, `status` and `describe_node` / `describe_type` skip the open-in-Studio fallback entirely, so a missing directory is `project_not_found` with no Studio or registry enumeration (`describe` reports it only when no bridge names the project, so a project open outside `projects_root` still works); `open`, `save` and `bridge_arm` keep the fallback with at most one Studio enumeration and one registry enumeration per call. (2) `studio_version` caches successful answers per `(studio_exe path, file mtime)` for the process lifetime, so a second `open` spawns no `--version`. (3) The `project_open` poll interval is `_OPEN_POLL_S = 0.4` (was 1.5 s). (4) `execute_method` probes the target project's registry port first and keeps the probes of its range walk (`serving_port_for(health_cache=)`), so the port-range-full check and the "before" snapshot no longer rescan the range. (5) The firewall hint names `coreservicehost.exe` (`DataAcquisitionCore.exe` is not known to prompt, so it is dropped). (6) `upgrade_required` and `create_failed` carry `pid` and `studio_left_open: {pid}`, as `project_file_invalid` does. (7) Nit: the `import_tags` dry-run comment typo `dry_runn=True` is fixed. The remaining #100 item 5 / item 7 entries could not be read from this sandbox (the issue is not reachable) and are NOT addressed here; the unit suite passes on Linux with no Windows-only failures. Tests: `test_run_restart_status_describe_skip_the_studio_fallback`, `test_open_and_save_keep_the_fallback_with_one_enumeration_per_call`, `test_studio_version_is_cached_per_exe_path_and_mtime`, `test_second_open_of_the_same_project_spawns_no_version_process`, `test_open_poll_interval_is_at_most_half_a_second`, `test_rearm_probes_the_target_registry_port_first`, `test_firewall_hint_names_coreservicehost`, `test_upgrade_required_carries_studio_left_open`, `test_create_failed_carries_studio_left_open`. Offline only: pending bench verification, on the bench with 5 Studios time `optix_emulator(action="status", project="Nope")` (expect < 1 s), open a 1.7 project in 1.8 (expect `upgrade_required` within 6 s of launch) and re-arm with 3 Studios armed.

- **Creating ops are never re-sent after a timeout; a shared verify-after-timeout settles them, and batch results are honest (`service/core.py`, `service/tests/test_create_verify_after_timeout.py`; PRD 0056, ftx-mcp-w3d.4).** (1) `_bridge_write` now defaults to `idempotent=False`: a timed-out or dropped POST is surfaced (`BridgeWriteTimeout` / `BridgeLoading`), never re-sent. Only repeat-safe ops opt in with `idempotent=True`: `set_property`, `set_displayname`, `set_text_key`, `set_modelling_rule`, `bind`, `retype`, `reorder`, `attach_*`, the translation writes, `validate_expression`, `ensure_web_engine`. `wire_event` and `invoke_method` are no longer re-sent either. (2) New `_create_with_verify`, the `create_netlogic` rule generalised, is used by `create_folder`, `create_object`, `create_variable`, `create_widget`, `create_type`, `create_alias`, `create_netlogic`, `move` and `convert_to_type`. On a timeout it GETs the would-be path: a node that matches the request (class, and the variable type when one was asked for) returns `succeeded` with `verified_after_timeout: true` and a `<op>_timeout_verified` warning; no node returns the original timeout error. A node that does not match returns `name_exists_other` with `existing_class` (`create_widget` raises it as `BridgeWriteFailed` with `op_result`). `move` also requires the source to be gone. (3) A timeout that could not be confirmed is remembered for 10 minutes; if the same op is then answered `name_exists`, the existing node is checked the same way: match -> `succeeded` with `verified_after_timeout: true`, otherwise `name_exists_other`. A `name_exists` with no earlier timeout stays a plain duplicate error. (4) `optix_bridge_edit`: `applied` counts verified ops, `verified_ops` lists their indexes and each `op_timings` entry carries `verified_after_timeout`. A failure never says "the model is unchanged" when an op was confirmed on the model (`partial`, naming the verified ops); a failing op whose own POST timed out unconfirmed says its outcome is UNKNOWN; `name_exists_other` names the existing class. Offline tests (stateful fake bridge that times out once, then answers `name_exists`): `test_timeout_with_node_present_is_verified_success_and_not_resent`, `test_timeout_with_node_absent_returns_the_timeout_error`, `test_timeout_once_then_name_exists_with_matching_node_is_verified`, `test_timeout_once_then_name_exists_with_other_node_is_name_exists_other`, `test_bridge_write_defaults_to_non_idempotent` and the `test_batch_*` cases. There is no `copy` op in this service; `move` and `convert_to_type` are the copy-based creators. Not run against a live Studio: pending bench verification, with Studio busy compiling, run `optix_bridge_edit` with a `create_object` and a `create_variable` and confirm a timed-out op reports `verified_after_timeout: true` once.

- **`run` fails closed when the pre-run status errors; `restart` reports its real state (`service/core.py`, `service/tests/test_run_emulator.py`, `service/tests/test_r24_result_shapes.py`; PRD 0056, ftx-mcp-w3d.3).** (1) `run_emulator` swallowed an `emulator_status` exception and went on to F5, which TOGGLES and could stop a running emulator. It now returns `{ok: false, error: "status_unavailable", detail, launched: false, hint: "optix_emulator status; fix the cause, then run"}` and sends no keystroke; `restart_emulator` returns the same result (no kill, no run) when its pre-stop status raises. (2) `restart` is stop (if running) plus run. With nothing running it returns `restarted: false, started: true` and `state` set to the run's real state (`launched`, `already_running`, or the refusal's code); the synthetic `state: "stopped"` is gone (this supersedes item 4 of the r24 result-shape nits). `started` is false when the run was refused or failed. Offline tests only: `test_run_emulator_status_unavailable_fails_closed_and_sends_no_f5`, `test_restart_status_unavailable_returns_same_result_and_does_not_run`, `test_restart_with_nothing_running_reports_started_and_real_state`.

- **Tests: the live suite survives the listener guard and runs in any order (`service/tests/conftest.py`, `service/tests/test_bridge_live.py`, `service/tests/test_run_emulator.py`, `service/tests/test_host_listener_isolation.py`; PRD 0056, ftx-mcp-w3d.2).** (1) `_no_host_listener_table` exempts tests through `_is_live_run(request)` (markers `live` / `slow_live` OR any non-empty `FTX_LIVE_*` env var), the same rule `_fast_netlogic_template_wait` uses, so a `FTX_LIVE_*` run no longer dies with `HostListenerTableAccess` on an unmarked test. (2) New session-scoped `warm_netlogic` fixture in `test_bridge_live.py`: it creates ONE NetLogic and polls (bounded, `FTX_LIVE_NETSOLUTION_WAIT`, default 120 s) until the NetSolution tree, the Studio log and the bridge health are quiet, so the `wire_event` tests no longer race the ~30 s NetSolution build of a cold Studio session; `wire_method_obj` now returns it (created once per session, deleted at teardown). Its docstring, and `wire_method_obj`'s, give the cold-session repro: `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<p> python -m pytest service/tests/test_bridge_live.py -k wire_event -q -rs -p no:cacheprovider`. (3) Every `run_emulator` launch / no-launch unit test in `test_run_emulator.py` (including `test_run_emulator_mismatch_guard_is_case_insensitive`) now calls `_mock_port`, which installs the `_port_owner` stub, so none depends on a host listener; the tests that exercise the real classifier restore it (`_stub_run_env`). `test_host_listener_isolation.py` gains `test_net_connections_guard_exempts_ftx_live_env_runs` and pins the case-insensitive test in the bound-port subprocess run. The live fixture was not run against a Studio here: pending bench verification, with `FTX_LIVE_BRIDGE=1` and `FTX_LIVE_PROJECT` set on a freshly opened project run `python -m pytest service/tests/test_bridge_live.py -k wire_event -q -rs -p no:cacheprovider`.

- **`add_translations` / `set_text_key` read the dictionary node's own `String[,]` value (`studio-bridge/StudioMCPBridge.cs`, `service/tests/test_translations_node_shape.py`; PRD 0056, ftx-mcp-w3d.1).** The r25 bench showed the dictionary is one variable node (`LocalizationDictionary1`, node_class Variable, datatype `String[]`, value `System.String[,]`) with no child `Value` variable, so `ReadValueMatrix` (which only looked for a child `Value`) found nothing: `unknown_locale` never fired, every call answered `locale_check_unavailable`, and `set_text_key` reported `translated: false` for existing keys. `ReadValueMatrix` now reads the node's own `IUAVariable` value and tries a child `Value` variable only when the node is not a variable; the rank-2 / jagged readers, `CellText`, `MatrixHeaderLocales`, `MatrixText` and `MatrixHasKey` are unchanged. The C# layout comment quotes the real node shape, and the `Locales` / `LocaleIds` path in `DictionaryLocales` stays as a fallback labelled "unobserved; the matrix variable is the known shape on 1.7.4 and 1.8.0". Reflection-free, so the same source compiles on Studio 1.7 and 1.8. Tests (fake bridge modelling the node tree): en-US rows written; de-DE refused as `unknown_locale` with `available_locales: ["en-US"]` at `dry_run` and apply (and in a batch); `locale_check_unavailable` only when no dictionary node exists or its value is not a matrix; `set_text_key` on a key in the matrix returns `translated: true` with `translated_locales`; plus C# source pins. The C# was not compiled here and the unit suite cannot verify it: pending bench verification, rebuild the bridge on 1.7 and 1.8, then on an en-US-only project run `optix_bridge_add_translations` with a `de-DE` row, `dry_run=true` (expect `unknown_locale`, `available_locales: ["en-US"]`) and `set_text_key` on a key present in the dictionary (expect `translated: true`).

- **Tests: the unit suite no longer reads the host's listener table; `psutil.net_connections` is guarded (`service/tests/conftest.py`, `service/tests/test_run_emulator.py`, `service/tests/test_host_listener_isolation.py`, `service/tests/test_cdp_recovery.py`, `service/tests/test_runtime_attach.py`, `service/tests/test_resolve_project_fallback.py`; PRD 0055 F, #91).** Five emulator tests passed or failed with whatever listened on the host's web port (a dev box serving 8081 turned `runtime` into `foreign`). (1) `_mock_port` now also patches `core._port_owner` to a deterministic table: `runtime` owned by the first fake pid when `reachable=True`, `none` otherwise; tests on the foreign / zombie branches patch `_port_owner` / `_port_listener_pids` explicitly (`test_emulator_status_reports_port_owner` restores the real classifier). (2) New autouse conftest guard: `psutil.net_connections` raises `HostListenerTableAccess` outside tests marked `live` / `slow_live`. It found six more unit tests that read the host table (the run/status/restart wait tests, `_wait_page_ready`, two `doctor` tests, the loopback-probe test); each now pins its seam (`_port_listener_pids`, `_pids_listening_on`, `_port_owner`, `_port_listener`). (3) `test_host_listener_isolation.py`: the guard raises, `live` tests are exempt, and the five affected tests (`test_emulator_status_running_needs_pid_and_port`, `..._running_state_with_log_fixture`, `..._project_excludes_other_project_pid`, `test_run_emulator_already_running_sends_no_f5[True-running]`, `..._port_attribution_when_name_differs_from_cache_folder`) pass in a subprocess while a throwaway socket listens on the test port. Unit tests only; no bench verification needed.

- **r24 result-shape nits (`service/core.py`, `service/studio_arm.py`, `docs/runbook.md`, `docs/tool-reference.md`, `service/tests/test_r24_result_shapes.py`; PRD 0055 E, #95).** (1) The `bridge_port_range_full` hint names `OPTIX_BRIDGE_PORT_RANGE` (not the nonexistent `FTX_BRIDGE_PORT_RANGE`) and says the bridge's own range is fixed at 4 (`PortRangeSize` in `StudioMCPBridge.cs`; widening needs a regenerated bridge); the runbook says the same. The bridge source is unchanged. (2) `project_file_invalid` detected from the Studio log after launch returns `studio_left_open: {pid}` (was `true`) and a hint to close the launched Studio. (3) `web_port_taken` results (run, restart, status, project clash) carry `owner: {pid, process}` (was `{pid, name}`) or `owner: null` with `owner_reason`; `legacy_code: web_port_clash` is dropped (nothing but tests read it). (4) `restart` reports `restarted: true` only when a runtime was stopped and the run did not fail, and `state: "stopped"` when nothing was running. (5) The status hint for an http.sys squatter (pid 4 / System) names http.sys and `netsh http show urlacl` instead of UpdateSvc. Offline tests only: pending bench verification of the http.sys hint against a real urlacl reservation.

- **The `netsolution_build` note is attached on every `create_netlogic` kind, not only `design_time` (`service/core.py`, `service/mcp_app.py`, `service/tests/test_save.py`; PRD 0055 D, #87).** `bridge_create_netlogic` set `netsolution_build: {state: "building", note}` only for `kind="design_time"`, so the default (runtime) create returned no note although Studio rebuilds the NetSolution after it too. The condition is gone: the tool, the `optix_bridge_edit` `create_netlogic` batch op (its `op_timings` entry, `detail` and top-level `netsolution_build`) and the primitive all delegate to the one function and now carry the note for either kind. The unit test no longer injects the note through the fake bridge response: it builds the result from a fake bridge response WITHOUT the note, for both kinds, and asserts the service adds it. Docstrings updated.
- **A Windows Firewall / consent prompt is reported as `blocking_window` with `kind: "firewall_prompt"`, whatever its size or position (`service/studio_arm.py`, `service/tests/test_studio_arm.py`; PRD 0055 C, #94).** The r24 arm failure `menu_item_not_found` (`menu_items: 0`) came from a firewall prompt hosted by PickerHost that was a zero-rect window: it overlapped nothing and was not in front, so `_win32_blocking_window` never listed it. (1) New `_is_consent_window`: PickerHost / consent / WindowsSecurity processes, and ShellExperienceHost only with a security-alert / Windows Security / firewall title, are blockers regardless of rect, overlap or foreground, and outrank every other candidate (the #32770 class, a foreground window, even a UIA foreground window); `blocking_window_for` checks them first. (2) `blocking_window` carries `{pid, process, title, class, source, kind: "firewall_prompt"}`; `kind` is present only for consent windows. This also applies to a PickerHost file picker, which is the same process. (3) `os_dialog_hint` (used by arm and by `run`'s no-spawn diagnosis) says "a Windows Firewall / consent prompt is open; answer it on the desktop (or pre-create the firewall rule: New-NetFirewallRule -Program <DataAcquisitionCore.exe> -Action Allow), then re-arm" for that kind. (4) `menu_item_not_found` always goes through `annotate_os_dialog`, so the search runs when the menu had 0 items. Tests (fake window table): `test_zero_rect_picker_host_window_is_reported_as_firewall_prompt`, `test_known_consent_processes_rank_above_other_overlap_candidates`, `test_menu_item_not_found_with_zero_menu_items_runs_blocking_window_search`. Offline only: pending bench verification, on the bench trigger a firewall prompt for DataAcquisitionCore.exe and run `optix_bridge_arm` (expect `blocking_window.kind == "firewall_prompt"` and the hint above).

- **`open` decides `upgrade_required` / `studio_too_old` / half-created from the project files; the Home-screen reason never says "newer Studio" (`service/core.py`, `service/tests/test_project_open_truth.py`, `service/tests/test_project_new_validation.py`, `service/tests/test_studio_arm.py`, `docs/tool-reference.md`; PRD 0055 B, #93, #66).** Studio 1.8 logs no version-mismatch line for its Project Upgrade dialog, so 0053's log-line-only `upgrade_required` could never fire, and the Home-screen check ran before the dialog check and blamed a "newer Studio" for an older project. (1) Before launching, `project_open` reads `<project>/IDEVersion.txt` (the `.optix` header is the fallback) and compares it with `studio_version(cfg)`: a newer project returns `{ok: false, error: "studio_too_old", project_version, studio_version, hint (FTOPTIX_STUDIO_EXE), launched: false}` without starting Studio. (2) A non-empty `.optix` with a missing or empty `IDEVersion.txt` (the half-created shape) returns `project_file_invalid` with `detail: "IDEVersion.txt missing: the project was never fully created"` and no launch; `project_new`'s `create_failed` carries the same text as `reason`. Empty / missing / garbage `.optix` behave as before. (3) For an older project the Project Upgrade dialog (checked FIRST, before the Home screen) returns `upgrade_required` with `project_version`, `studio_version`, `dialog` and the one-way "not clicked" hint on the first poll; the log line (`_VERSION_MISMATCH_RE`) is now a secondary signal that only adds `detail`, and the "UNCONFIRMED WORDING" claim is replaced by a note saying so. A dialog with no version signal still ends as `not_opened`. (4) The Home-screen `not_opened` reason no longer mentions a newer Studio; `detail` carries only the first log error line. Tests (fixtures for the three shapes): `test_old_project_with_upgrade_dialog_is_upgrade_required_in_one_poll`, `test_project_newer_than_studio_is_studio_too_old_without_launching`, `test_missing_ide_version_is_project_file_invalid_without_launching`, `test_home_screen_not_opened_reason_never_says_newer_studio`, plus the dialog-before-Home and first-error-line cases; existing open fixtures now write an `IDEVersion.txt`. The exact dialog title / class in the fixtures (`Project Upgrade`, `#32770`) come from the PRD text: `func/raw-r24/a-open-v17in18*` is not available in this sandbox, so pending bench verification: on the bench open a Studio 1.7.4.32 project in 1.8 (expect `upgrade_required` in under 10 s), a project with `IDEVersion.txt` removed (expect `project_file_invalid`, no Studio started) and one whose `IDEVersion.txt` names a version above the installed Studio (expect `studio_too_old`), and compare the captured dialog title with the fixture.

- **`run`, `restart` and `status` on a nonexistent project refuse with `project_not_found` before any scan, keystroke or wait; F5 is never sent without a target Studio (`service/core.py`, `service/tests/test_run_emulator.py`, `service/tests/conftest.py`, `pyproject.toml`; PRD 0055 A, #92).** `optix_emulator run project=<no such dir>` with several Studios open skipped the whole attribution block (it sat under `if _project_dir is not None`), fell through to the F5 guard with no target pid and sent F5 to an unrelated Studio (raising its NetLogic security warning, 279 s); `status` on the same name answered `stopped` after 44 s. (1) New `_resolve_emulator_project` resolves the project directory first in `run_emulator`, `restart_emulator` and `emulator_status(project=...)`; a miss returns `{ok: false, error: "project_not_found", project, projects_root, hint: "<projects_root> has no such project; optix_project list", detail}` before the process / command-line / log scan, the web-port check, the kill or the keystroke, and nothing else in the function runs. `run_emulator` resolves once and reuses the directory for attribution. (2) Last guard right before the F5 send: `target_pid` unset -> `{ok: false, error: "no_target_studio", launched: false}` and no keystroke (a no-Studio-at-all run now answers this instead of sending F5 to find `no_studio_window`; with exactly one Studio open and no usable window evidence that Studio's pid is pinned, which is what the old "first window" fallback picked). Tests: `test_nonexistent_project_is_refused_before_any_scan_or_keystroke` (run / restart / status; the fake keystroke runner is never called and the scan seams are never reached), `test_project_resolves_before_studio_commandline_and_log_scan`, `test_run_with_no_target_pid_returns_no_target_studio_and_sends_no_keystroke`. The emulator test modules get a conftest default that resolves any bare name and a pinned fake Studio pid (opt out with the `real_project_resolution` / `real_studio_attribution` markers). Offline only: pending bench verification, with several Studios open run `optix_emulator` `run`, `restart` and `status` with `project=CI_nothere` (expect `project_not_found` in under 1 s and no change in any Studio).

- **Tests: the live suite keeps the real NetLogic template wait; the invoke-throws live test asserts the post-0050 contract (`service/tests/conftest.py`, `service/tests/test_bridge_live.py`, `service/tests/test_conftest_fixture_live_skip.py`, `pyproject.toml`; PRD 0054 C).** (1) The autouse `_fast_netlogic_template_wait` fixture shortened `_NETLOGIC_TEMPLATE_MAX_WAIT_SECONDS` / settle constants for every test, including the live ones, so the three live `wire_event` tests timed out waiting for a real Studio to write the template. It now leaves the constants alone for tests marked `live` / `slow_live` (new `live` marker registered) or run with any non-empty `FTX_LIVE_*` env var. New `test_conftest_fixture_live_skip.py` pins that the fixture still applies to non-live tests (unit suite stays fast). (2) `test_invoke_method_that_throws_is_survivable` asserted `ok: True`, which the #73 bridge no longer returns; it now asserts `argument_count_mismatch` (`expected: 2, given: 1`) for a wrong argument count and `method_failed` for a method that fails at runtime (`CheckFormula` with an empty expression), with the bridge still serving after each. Not run against a live Studio: pending bench verification, with `FTX_LIVE_BRIDGE=1` and `FTX_LIVE_PROJECT` set run `python -m pytest service/tests/test_bridge_live.py -k "throws_is_survivable or rewire_event or wire_event_multistep" -q`.

- **`unknown_locale` fires on Studio 1.8: the locale list is read from the dictionary's `Value` matrix; an unreadable list refuses with `locale_check_unavailable`; `translated` sees rows written by Studio or the template (`studio-bridge/StudioMCPBridge.cs`, `service/core.py`, `service/tests/test_translations_truth.py`; PRD 0054 B, #83).** (1) `DictionaryLocales()` tried only `Locales` / `LocaleIds` variables, which Studio 1.8 does not have, so the list was empty and the locale check silently skipped (`add_translations` with a `de-DE` row against an en-US-only dictionary validated and wrote). It now reads those variables first (older Studio, kept) and otherwise the header row of the dictionary's `Value` matrix (row 0, columns 1..n; column 0 is the key column); the matrix layout and the 1.7 / 1.8 difference are documented in a C# comment above the helpers. (2) When no locale list can be read at all, the check is no longer skipped: `add_translations` (direct tool, `dry_run` included, and the batch validator) is refused with `locale_check_unavailable` carrying `dictionary_path` (and `op_index`; `row` / `locale` in the batch report), `written: 0`. This supersedes the "pre-check is skipped" sentence of the PRD 0052 C entry below. `service/core.py` maps the new code to a structured `{ok: false, code, dictionary_path, ...}` result like `unknown_locale`; there is no Python-side locale pre-check to mirror (the dictionary lives in the live model). (3) The `translated` / `translated_locales` / new-vs-update existence checks now also scan the matrix (key in column 0, locale in column j), so rows written in Studio or by the template read `translated: true`. Reflection-free, no 1.7 / 1.8 compile difference. Tests: `test_add_translations_de_de_against_en_us_only_is_unknown_locale_in_dry_run`, `test_add_translations_locale_check_unavailable_refused_with_dictionary_path`, `test_batch_refuses_locale_check_unavailable_before_writing`, plus source pins for the matrix header read, the matrix scan and the comment. The C# was not compiled here (no dotnet/Studio in this sandbox) and how the 1.8 SDK surfaces the `Value` matrix (rank-2 array vs array of rows; the dictionary node under `Translations`) is unconfirmed: pending bench verification, rebuild the bridge on 1.7 and 1.8, then on a project whose dictionary has only en-US run `optix_bridge_add_translations` with a `de-DE` row, `dry_run=true` (expect `unknown_locale`, `available_locales: ["en-US"]`), and `set_text_key` on a key added in Studio's Translations editor (expect `translated: true`); if the matrix cannot be read expect `locale_check_unavailable` with the dictionary path.

- **`import_tags` sees the station's Variable tags; the dry run reports the real plan (`service/core.py`, `studio-bridge/StudioMCPBridge.cs`, `service/tests/test_import_tags_station_walk.py`; PRD 0054 A, #88).** `import_tags_station_symbols` walked only non-Variable children, but RA EtherNet/IP tags are `Variable` nodes (`dotnet_type: TagStructure`) nested under `Tags/Controller Tags/...`, so on a station holding T0001/T0002 the dry run answered `would_remove: []`, listed present tags in `would_add`, the merge guard never refused and `replace: true` deleted the tags. (1) New bridge route `GET /bridge/station/tags?station=<path>` returns `[{path, symbol}]` from `ImportTagsSnapshot` (every descendant of `<station>/Tags` carrying a `SymbolName`, whatever its node class), so Python and C# agree on the set; the service uses it and, against a bridge build without it, falls back to a `GET /bridge/nodes` walk that now descends into every node class (Variables included). Reflection-free C#, no new 1.7/1.8 difference. (2) `import_tags dry_run` returns `would_add` without symbols already present, `would_remove` (present symbols absent from the source file in merge mode, or not requested / absent in replace mode) and a populated `already_present`; with `replace=false` a non-empty `would_remove` sets `would_refuse: "would_remove_tags"`. New tests use a fake describe tree with Variables nested two levels (with and without the new route) and show the merge guard refusing when the file lacks a present tag. The C# route was not compiled here (no dotnet/Studio in this sandbox): pending bench verification, rebuild the bridge on 1.7 and 1.8, then `curl "http://127.0.0.1:<port>/bridge/station/tags?station=<station>"` on an RA station holding tags (expect their `{path, symbol}` pairs) and run `optix_bridge_import_tags ... dry_run=true` from a file lacking them (expect `would_remove` and `would_refuse`).

- **Result-shape fixes from r22 (`service/core.py`, `service/mcp_app.py`, `studio-bridge/StudioMCPBridge.cs`, `examples/claude-code-settings.json`, `docs/tool-reference.md`, `skills/optix-project-lifecycle/SKILL.md`, `skills/optix-verify-loop/SKILL.md`, `service/tests/test_r22_result_shapes.py`, `service/tests/test_resolve_project_fallback.py`; PRD 0053 D, #87).** (1) An edit on a same-name project that is NOT armed (a sibling copy is) is classified `bridge_not_armed` with `hint: "optix_bridge_arm <project>"` instead of `bridge_transient` + "retry" (`classify_bridge_failure`; retrying could never succeed, arming can). (2) A foreign listener on the web port is one code, `web_port_taken`, in `run`, `restart` and `status`, with `runtime: "none" | "zombie" | "running"` (this project's own emulator process: absent, alive but not owning the port, or serving) and `owner: {pid, name}`; `status` previously only added a `hint` and `port_owner`. `web_port_clash` is retired: the two-projects-one-port refusal of `run` now answers `error` / `reason_code` `web_port_taken` and `detect_web_port_clash` returns `legacy_code: "web_port_clash"` for one release (the tests, `docs/tool-reference.md` and the two skills that named the old code are updated). (3) A batch `create_netlogic` carries the primitive tool's `netsolution_build` note (in its `op_timings` entry, its `detail`, and top-level `netsolution_build` of the batch result); the validator knows the op, so `unknown_op` ("applied unchecked") is gone: `StudioMCPBridge.cs` `validate_ops` has a `create_netlogic` case (duplicate create, deleted parent, `unresolved_reference` for a parent that exists neither live nor earlier in the batch), and the service drops the `unknown_op` warning / strict-mode error for verbs it validates itself (`_SERVICE_VALIDATED_VERBS`) so a bridge build without the new case behaves the same. A bad `parent` is refused at `dry_run` with `unresolved_reference` (`field: "parent"`, resolved against the live model and the batch's own creates like `bind` sources). (4) `examples/claude-code-settings.json` allows `optix_status` instead of the removed `optix_doctor`. (5) The "project not found" message lists only Studios whose project is actually LOADED (bridge health / window title via `_project_loaded_identity`, `_loaded_open_candidates`); a Studio launched on a project that never loaded is no longer named as "open" (with none loaded the message says so). New tests in `test_r22_result_shapes.py` (one or more per item) and `test_resolve_project_fallback.py`. The C# `create_netlogic` validator case was not compiled here (no dotnet/Studio in this sandbox): pending bench verification, rebuild the bridge and run `optix_bridge_edit` with `dry_run=true` and a `create_netlogic` op under a nonexistent parent (expect `unresolved_reference`, no `unknown_op`), then a real `kind="design_time"` batch op (expect `netsolution_build`); and occupy the web port with `python -m http.server <port>` and call `optix_emulator` `run`, `status` and `restart` (expect `web_port_taken` each time).

- **`project_new` and `execute_method` refuse with `no_interactive_session` too; one constant lists every Studio-launching tool (`service/core.py`, `service/tests/test_no_interactive_session.py`; PRD 0053, #86).** The PRD 0051 guard covered `open`, `arm`, `save`, `run`, `restart` and the native capture / click, but `optix_project(action="new")` (`core.project_new`) still started an invisible Studio from a session-0 service and `optix_execute_method` (`core.execute_design_method`, UI right-click -> Execute) still drove the UIA gesture. Both now call `_require_interactive_session()` before any validation, audit or launch and return `{ok: false, error: "no_interactive_session", hint}` when `_is_interactive_session()` is False (unknown / True behave as before). The guard list is the single constant `INTERACTIVE_SESSION_TOOLS` (open, new, arm, save, run, restart, execute_method, native observe, native interact), which the doctor row's `required_for` also uses. New tests: `test_project_new_refuses_in_session_zero` (also via the `optix_project` tool), session-0 refusals for `new` and `execute_method` in the parametrized suite, and `test_every_launching_mcp_tool_is_in_guard_constant`, which walks the MCP registry and the `core.py` call graph and fails when a registered tool reaches `_studio_launch`, the SendKeys script builder, `studio_arm.execute_method` / `focus_project_tree` or the native click without its core function being in the constant. Not run against a real session 0 (no Windows bench here): pending bench verification, start the service from a pre-logon S4U task and call `optix_project(action="new", project="X")` and `optix_execute_method` (expect `no_interactive_session`, no Studio process started).

- **A full bridge port range is reported as `bridge_port_range_full`, and shell windows are never the "blocking window" (`service/studio_arm.py`, `studio-bridge/StudioMCPBridge.cs`, `service/tests/test_studio_arm.py`; PRD 0053 B, #85).** (1) Before any Studio keystroke, `optix_bridge_arm` probes every port in the configured range; when all of them answer a health probe for OTHER projects it returns `{ok: false, error: "bridge_port_range_full", ports, owners: [{port, project, pid}], hint: "stop a bridge (optix_bridge_arm action=stop on one of them) or widen FTX_BRIDGE_PORT_RANGE"}` and sends nothing. (2) The verify path scans the Studio log written since the arm menu click for the bridge's bind-failure line and returns the same error (with the line as `detail`) instead of `verify_timeout`; the bridge's log line now starts with the stable marker `bridge_port_range_full:` (the old `cannot bind any port in` wording is still recognised). (3) `blocking_window_for` never returns the desktop / shell: classes `Progman`, `WorkerW`, `Shell_TrayWnd`, `Shell_SecondaryTrayWnd`, windows titled "Program Manager" / "Taskbar", on both the UIA and Win32 sources; the service's own windows (PRD 0049 / 0051 C) stay excluded. Pending bench verification: arm four projects, then a fifth, on the bench (`optix_bridge_arm <fifth>`) and confirm `bridge_port_range_full` with four owners and no keystrokes; with the range full and the pre-probe bypassed, confirm the verify path returns the same error.

- **`open` detects an unloadable project fast and reports why (`service/core.py`, `service/tests/test_project_open_truth.py`, `service/tests/test_studio_arm.py`, `service/tests/test_project_new_validation.py`; PRD 0053, #66).** (1) Before launching, an empty `.optix` returns `project_file_invalid` and a missing one (no `.optix` in the directory; replaces `no_optix_file`) returns `project_file_missing`, both with `path` and without starting Studio. (2) On every poll of the open wait the Studio log since launch is scanned for load failures (`... is empty`, `Error creating new project`, `Cannot open`, `failed to load`, YAML parse errors): the first match returns `{ok: false, error: "project_file_invalid", path, detail: <line>, pid}` at once with `studio_left_open: true` (the launched Studio is not closed) instead of waiting out the timeout. (3) `upgrade_required` is returned only when the Project Upgrade dialog is up AND the log carries the version-mismatch line (`_VERSION_MISMATCH_RE`); a dialog without that line (the half-created 64-character project) ends as `not_opened` with the log's first error line as `detail`. (4) `not_opened` no longer says "open in another Studio" unless a Studio pid was matched to the project (a refusal line from the log is quoted in `detail` only, and the Home-stuck reason uses the matched holder, not merely "a Studio was running"). Log-fixture tests cover each failure line. **Pending bench verification:** the real Studio wording of the version-mismatch log line was not available in this sandbox, so `_VERSION_MISMATCH_RE` and the test fixture `VERSION_MISMATCH_LINE` are a best guess; on the bench open a project saved by an older Studio, copy the exact line from `FTOptixStudio.*.log` into both, and check `optix_project(action="open")` returns `upgrade_required`.

- **`bridge_source` hash ignores Studio's rewrite; `optix_bridge_status` is fast and can be scoped to a project (`service/core.py`, `service/mcp_app.py`, `ci/check-template-drift.ps1`, `service/tests/test_bridge_sync.py`, `service/tests/test_bridge_status_speed.py`, `service/tests/test_template_drift_guard.py`; PRD 0052 F, #74).** (1) `normalize_bridge_source` (behind `bridge_source_hash` / `_compare_bridge_source`, used by arm, status and `/health`) now also drops `using ...;` directive lines and blank lines and strips leading/trailing whitespace on every line (rule in its docstring), so Studio inserting `using FTOptix.CommunicationDriver;` or re-indenting the file yields `current`, while any real code difference stays `stale`. `using` statements/declarations (`using (x) {`, `using var f = ...;`) are code and still count. The template drift guard (`ci/check-template-drift.ps1` and its Python mirror) uses the same rule. (2) `optix_bridge_status` takes an optional `project`: scoped, it makes exactly one health call at the port the registry lists for the project's directory, with a 0.5 s timeout and no retry (no registry entry: falls back to `resolve_bridge`). Unscoped, registry and range ports are probed in one parallel pass and a dead non-registry range port is tried once (it used to sleep through three attempts), so wall time is about one probe. New tests: hash equality across a `using` insertion, scoped status = one call, unscoped with 3 registry entries under 1.5x one probe. The PowerShell guard change was not run here (no PowerShell in this sandbox): pending bench verification, run `ci\check-template-drift.ps1` on the bench.

- **Docs say what the code does; `routed_into_collection` kept, now reported at apply time with `routed_path` (`docs/tool-reference.md`, `docs/runbook.md`, `README.md`, `bootstrap/setup.ps1`, `service/mcp_app.py`, `service/core.py`, `studio-bridge/StudioMCPBridge.cs`, `skills/optix-verify-loop/SKILL.md`, `service/tests/test_docs_tool_reference.py`; PRD 0052 E, #84).** `optix_project` actions are `open`/`new` (not `create`). An unknown batch verb is refused with `bad_op_verb`; `unknown_op` is only the validator warning "applied unchecked". There is no `port` parameter on `optix_emulator`/`optix_observe`/`optix_interact`. The "check status before run" advice is gone (`run` is idempotent: `already_running`). The `optix_find` docstring now says it searches the live model through the bridge and refuses with `studio_open` only when no bridge serves the project. `optix_read_file` keeps its `studio_open` docstring: the code does refuse in the default blanket guard mode (`test_read_netlogic` pins it), so the doc is right. `setup.ps1` no longer names the old `optix_cdp_*` aliases. The 1.0.9 `import_tags` bullet that described `tag_importer` on `create_object` and three tests that do not exist is removed (the later bullet already states `create_object` no longer reports it). **`routed_into_collection`: the routing was implemented, not removed.** `WriteWidgetInline` and the validator already route a widget whose type fits a placeholder collection (a `NavigationPanelItem` under a `NavigationPanel` lands in `Panels`); a widget type that does not fit any collection, such as a `Label`, stays a direct child by design. What was missing: the validator warning had no `routed_path` field (the doc promised one), and a parent created earlier in the same batch is not resolvable at validate time so nothing was reported. The validator warning now carries `routed_path`, and `bridge_create_widget` (direct and in a batch) adds a `routed_into_collection` warning with `routed_path` whenever the bridge says it routed. New test: every action/op name in the `docs/tool-reference.md` tables exists in the tool `action`/`mode` Literals or `BRIDGE_EDIT_VERBS`. **Pending bench verification:** the C# change was not compiled here (no dotnet/Studio in this sandbox); on the bench rebuild the bridge and run `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<p> pytest service/tests/test_bridge_live.py -k routed_path`.
- **Tests: the live `wire_event` tests wire a method that exists (`service/tests/test_bridge_live.py`; PRD 0052 D, #82).** Since PRD 0050 B the bridge refuses a method the target object does not expose (`method_not_found`), so `test_rewire_event_args_round_trip` and `test_wire_event_multistep_and_append_live`, which wired `<scratch>/Rectangle1/ChangePanel`, failed on every bench. Both now take a new `wire_method_obj` fixture that writes `NetSolution/<class>.cs` (a `BaseNetLogic` with `[ExportMethod]` `ChangePanel` and `ChangePanelByTabName`), creates the NetLogic with `bridge_create_netlogic` (the path the other live tests use) and deletes the node and the .cs afterwards; it skips if the NetLogic cannot be created, and `FTX_LIVE_REWIRE_METHOD_OBJ` (now optional) names an existing object with both methods to use instead. New `test_wire_event_nonexistent_method_returns_method_not_found_live` wires a nonexistent method on that NetLogic and asserts `method_not_found` (with `available`) and no handler created, then wires the real method. Existing skip markers are unchanged; the offline suite passes (2885 passed, 125 skipped). Not run against a live Studio: pending bench verification: with `FTX_LIVE_BRIDGE=1` and `FTX_LIVE_PROJECT` set on the bench, run `python -m pytest service/tests/test_bridge_live.py -k "rewire_event or wire_event_multistep or wire_event_nonexistent" -q`. Whether a freshly created runtime NetLogic exposes its exports to `wire_event` before a reopen (as a design-time one does not, see `export_method_not_loaded`) is unconfirmed; if the bench answers `export_method_not_loaded`, wire a `command` or reopen the project in the fixture.

- **`set_text_key` reports `translated` truthfully; `add_translations` refuses unknown locales before writing and reports partial writes (`studio-bridge/StudioMCPBridge.cs`, `service/core.py`, `service/tests/test_translations_truth.py`; PRD 0052 C, #83).** (1) `set_text_key` probed the table with a bare `LocalizedText(key)` that never matched a row written under the project namespace, so `translated` was always false. It now uses the same existence check `add_translation` uses to decide Add vs Set (`TranslationRowExists`, backed by a per-key/per-locale record of the rows the bridge wrote plus a namespaced lookup per dictionary locale): the row for `key` in `locale`, else any locale; the result gains `translated_locales: [...]`. (2) `add_translations` checks every row's locale against the dictionary's locale columns BEFORE the first write, on the direct route (`dry_run` included) and in the batch validator: `unknown_locale` with `op_index`, `row`, `locale`, `available_locales`, `written: 0`; no row is written when any locale is unknown. The direct tool returns it as `{ok: false, code: "unknown_locale", ...}`, a batch reports it in `report.errors` (state `validated`, nothing applied). (3) A write that fails mid-way cannot be rolled back (the Optix SDK has no remove-translation call, see `not_supported`), so the route answers `row_failed` with `partial: true, written: n`; `bridge_add_translations` returns it structured, and `bridge_edit` ends in state `partial` (the `failed_op` carries `partial`/`written`) instead of `failed` with "the model is unchanged", even when it was the first op (`_apply_failure_outcome(..., partial_written)`). A failure before any row landed is still `failed`/unchanged. If the dictionary's locale columns cannot be read, the locale pre-check is skipped rather than refusing every row. Tests use a fake en-US-only dictionary implementing the bridge contract; the C# is not compiled offline, and the locale-column read (`Project.Current.Get("Translations")` variable `Locales`/`LocaleIds`) is a best-effort guess at the SDK. Pending bench verification: on a project whose dictionary has only en-US run `optix_bridge_add_translations` and an `optix_bridge_edit` `add_translations` op (also `dry_run=true`) with a `de-DE` row (expect `unknown_locale`, nothing written), and `add_translation` then `set_text_key` of the same key (expect `translated: true`, `translated_locales: ["en-US"]`).

- **One shared browse-name guard covers every creating/renaming op (`service/core.py`, `service/tests/test_edit_op_validation.py`; PRD 0052 B, #80).** `create_widget`, `create_folder`, `create_type`, `create_alias`, `create_netlogic`, `rename` / `move` `new_name` and `convert_to_type` `type_name` accepted names such as `A/B`, creating nodes no path can address (0050 D had guarded only `create_variable` / `create_object`). `validate_op_browse_names` (table `_BROWSE_NAME_FIELDS`, built on `validate_browse_name`) now runs for every one of them in `bridge_edit` / `dry_run` (`invalid_browse_name` with `op_index` and `field`; `rename` is checked as the `move` it is lowered to) and at the top of each per-noun tool, which refuses before any bridge request (`bridge_create_widget` raises `BridgeWriteFailed: invalid_browse_name`, the others return `{ok: false, code: "invalid_browse_name", errors}`; a batch counts them as failed ops). The same characters as before are refused (`/`, `\\`, control characters, leading/trailing whitespace); `.`, `[`, `]`, `@` stay allowed. There is no `copy` / `duplicate` op or tool today; a test requires every batch verb to be classified as name-bearing or non-naming, so adding one forces its target name into the table. `StudioMCPBridge.cs` `validate_ops` does not validate names, so it is unchanged (no Studio 1.7 / 1.8 compile impact).

- **Unknown tool arguments are refused with `unknown_argument`; `import_tags` gains `dry_run` (`service/core.py`, `service/mcp_app.py`, `service/http_app.py`, `docs/tool-reference.md`, `service/tests/test_unknown_arguments.py`, `service/tests/test_import_tags.py`; PRD 0052 A, #81).** (1) FastMCP's argument model silently dropped a key that is not in a tool's signature, so a misspelled argument (`dry_runn`, `projct`) ran the tool with its default (for `optix_bridge_import_tags` that meant a real import). The MCP dispatch (`_measured_dispatch`) now compares the call's keys with the tool's input schema before anything runs and answers `{ok: false, error: "unknown_argument", code, unknown: [names], accepted: [names], hint}` (`core.unknown_argument_refusal`; the hint names the closest accepted spelling); the tool function is never called, so no bridge request is made. One mechanism for every tool. (2) The HTTP route layer does the same for JSON bodies: the request models derive from `StrictModel`, and an unknown key (nested `edits[]` items included) is answered with the same body (status 422) before the route function runs. Query parameters are unchanged. (3) `optix_bridge_import_tags` and the `import_tags` batch op take `dry_run: bool = false`: after the usual validation and file parse it reads the station's current tags (read-only `GET /bridge/nodes` walk of `<station>/Tags`, `SymbolName` of each tag node) and returns `{ok: true, dry_run: true, would_add, would_remove, would_skip}` without calling `/bridge/station/import-tags`. `would_remove` lists the root station tags the import would drop; with `replace=false` a non-empty list also sets `would_refuse: "would_remove_tags"` and `hint` (the real call is refused). `would_skip` is `[{symbol, reason}]` for requested tags the file lacks (the real result reports them as `not_found`); an unknown DataType is only found by the importer and is not predicted. Online mode reads the controller, so the lists are empty with a `note`. If the station's tags cannot be read, `would_remove` is empty and `station_read: false`. Not run against a live Studio: pending bench verification: on a bench station with tags run `optix_bridge_import_tags ... dry_run=true` (expect the three lists, the station unchanged) and a call with a misspelled argument (expect `unknown_argument`); the Tags-subtree read assumes `/bridge/nodes` lists children and a tag's `SymbolName` value.

- **`optix_bridge_edit` gains a `create_netlogic` op, so the default surface (primitives gated off) can create a NetLogic (`service/core.py`, `service/mcp_app.py`, `docs/tool-reference.md`, `docs/release-notes-v1.0.8.md`, `service/tests/test_bridge_edit_create_netlogic.py`; PRD 0051, #79).** `{"op": "create_netlogic", "parent", "name", "kind"?}` is in `valid_ops` and the tool docstring and dispatches to `core.bridge_create_netlogic`, the same function `optix_bridge_create_netlogic` calls (same parameters, validation, template-write wait, `netsolution_build` note and result: one implementation, two entry points). A bad parent/name/kind is refused at validation (dry_run included) and a refused create counts as a failed op. `bad_op_verb` no longer names it. The C# validator does not know the verb yet (like `convert_to_type` it reports `unknown_op`, applied unchecked), so `strict=true` promotes that warning to an error; pending bench verification: run `optix_bridge_edit` with a `create_netlogic` op on a default install (no `FTXMCP_BRIDGE_PRIMITIVES`). The tool-reference gate paragraph and the stale 1.0.8 "Known limitations" NetLogic entry are corrected.

- **Tools that need the interactive desktop refuse up front in session 0 with `no_interactive_session` (`service/core.py`, `bootstrap/setup.ps1`, `service/tests/test_no_interactive_session.py`; PRD 0051, #78).** A service started in session 0 (S4U task before logon) reported `interactive_session: false` in `/health` but `open` answered `opened` (an invisible Studio), `run` waited 20 s for `no_studio_window`, `save` said "Restore/un-minimize" and native capture / click said `native_runtime_not_found`. `project_open`, `bridge_arm`, `save`, `run_emulator`, `restart_emulator`, `native_screenshot_runtime` and `native_interact` now check `_is_interactive_session()` first (`_require_interactive_session`) and, only when it is False, return `{ok: false, error: "no_interactive_session", hint: "the service is running in session 0 (started before logon or by a non-interactive task); start it from the logged-on session: bootstrap\\services.ps1 start, or enable-autostart (runs at logon)"}` before any audit, launch or keystroke (the native tools add `state: "failed"`). An unknown session (`None`, non-Windows) and True behave as before. `optix_status action=doctor` marks the `interactive_session` row `required: true` (with `required_for` listing the tools), so `ready` is false in session 0. `bootstrap/setup.ps1` (both user-text lines and the header comment) now says `optix_status action=doctor` instead of the removed `optix_doctor`. Pending bench verification: start the service from a pre-logon S4U task and call `optix_project(action="open")`, `optix_save` and `optix_status(action="doctor")` (expect `no_interactive_session` and a red required `interactive_session` row), then start it with `bootstrap\services.ps1 start` from the logged-on session and confirm they work.

- **`optix_emulator(run)` checks the web port and detects a zombie runtime before F5 (`service/core.py`, `service/tests/test_run_emulator.py`; PRD 0051, #76).** (1) Before any keystroke `run_emulator` resolves the LISTEN owner of the project's web port (`_port_owner`); a foreign owner returns `{ok: false, launched: false, serving: false, error: "web_port_taken", port, owner: {pid, name}, hint}` at once and sends no F5 (previously the F5 was sent and the loop discovered it after polling). (2) A zombie runtime, meaning this project's emulator pid is alive but does not own the port (`emulator_status` state `zombie`, or `starting` with a `Bind to server address failed` line in the runtime log), returns `{ok: false, launched: false, error: "runtime_zombie", pid, pids, hint: "use action='restart'"}` (plus `detail` with the log line) without sending F5 (F5 toggles and would stop it) and without waiting. (3) The post-F5 readiness loop polls every 0.25 s (was 0.5 s) and returns `web_bind_failed` at the poll after the bind-failure line is logged, not after `ready_timeout`. The healthy run path, `already_running` for a `starting` runtime and the existing post-F5 `web_port_taken` are unchanged. Pending bench verification: occupy the web port with `python -m http.server <port>` and run `optix_emulator(action="run")` (expect `web_port_taken`, no F5); hang a runtime so it has a window but no listener and run again (expect `runtime_zombie`).

- **Hints name the right window, the destination path and the real cause when a project is not open (`service/studio_arm.py`, `service/core.py`, `service/tests/test_studio_arm.py`, `service/tests/test_save.py`; PRD 0051, #67).** (1) `blocking_window_for` also excludes the service's console window: `GetConsoleWindow()` and its root owner (`GetAncestor(hwnd, GA_ROOTOWNER)`) by handle, and any window titled with the service / `python.exe` path (`_own_window_filter`, computed once per call), even when the console host is not in the process tree. Candidates are ranked #32770 dialog class first, then PickerHost / consent / WindowsSecurity processes, then foreground, then topmost; `blocking_window` is `{pid, process, title, class, source}`. (2) `write_denied`: `_denied_path_from_log` parses every path on the Studio `copy_file` line and `denied_path` is the one under the project directory (the last path on the line when none is); the other path is reported as `denied_source`. (3) `optix_save` / `optix_emulator(run)` with no bridge and no command-line attribution decide by Studio window titles: exactly one Studio names the project -> it is targeted; none does (every Studio window readable) -> `project_not_open` with hint `optix_project open <project>`; `ambiguous_studio` only when 2 or more Studios name it. When the windows cannot be read the previous rule applies. Pending bench verification: on the bench, open a file picker over Studio and run `optix_bridge_arm`, make Studio's save fail on a read-only `Nodes/*.yaml`, and run `optix_save` for a project no Studio has open; confirm the blocking window, `denied_path` / `denied_source` and `project_not_open`.

- **`optix_project(new)` accepts Unicode names and reports `created` truthfully; `open` never mislabels a broken file as `upgrade_required` (`service/core.py`, `service/mcp_app.py`, `docs/tool-reference.md`, `service/tests/test_project_new_validation.py`; PRD 0051 B, #66).** `validate_project_name` now accepts any valid C# identifier via `str.isidentifier()` (letters of any script, digits, underscore) plus the keyword list, so `CI_r20_ü` works; no spaces, no trailing dot, path limit unchanged. `PROJECT_NAME_RULE`, the tool docstring and `docs/tool-reference.md` say "letters (any script), digits, underscore". The create wait no longer returns `created` as soon as the file appears: it needs a non-empty `<name>.optix` and a log with no `Error creating new project` for 5 s after the file appears (or a project-loaded line); on the error line it returns `{ok: false, error: "create_failed", detail, path, partial_dir}` and deletes nothing. `project_open` returns `project_file_missing` (missing/empty file, checked before launch and during the wait) or `project_file_invalid` (Studio create/load error line) with `path`, never `upgrade_required`. Pending bench verification: on the bench run `optix_project(action="new", project="CI_r20_ü")` and a 64-char name, and confirm `created` / `create_failed`.

- **Bridge selection is routed by the resolved project directory on every path (`service/core.py`, `service/studio_arm.py`, `service/tests/test_bridge_resolution.py`, `service/tests/test_project_open_truth.py`; PRD 0051, #63).** With a root copy `X` and a same-named sub copy `Site/X` open in two Studios, arm / edit / describe / status / save on `X` could land on the sub copy's bridge through a name-only fallback. `resolve_bridge` now matches by directory first; a name match counts only for a bridge that carries no directory or whose directory is the requested one, so a bridge naming another directory is never claimed under the same name, and a health WITHOUT `project_dir` on a same-name collision (another directory holds the name: projects root, Studio command lines, registry, or another armed bridge) is unproven and not claimed (it is listed in `bridges` of `no_bridge_for_project`; arm proves the Studio by pid / command line / launch record as in 0049 C). `studio_arm._bridge_matches` / `serving_port_for` take `name_collision` for the same rule, and `execute_method` takes `name_collision` / `proven_pids` (set by `bridge_arm` and `execute_design_method` from `_arm_identity`) for its `already_armed` probe, the arm / stop polls, the Studio-pid lookup and the window it clicks in; all of them already pass the RESOLVED `project_dir`. `_registry_port_for` keys the registry by directory: a same-name entry whose `project_path` differs is ignored (not offered as the preferred port) and `bridge_arm` reports it in `candidates` (`[{dir, port, ignored: "registry_dir_differs"}]`). `StudioMCPBridge.cs` already writes `project_dir` (from `GetProjectPath()`, null when unknown) in `/bridge/health` and the registry entry using no new SDK API; it is unchanged. Pending bench verification: with `<root>\X` and `<root>\Site\X` open in two Studios (sub armed first), run `optix_bridge_arm`, an edit and `optix_save` on `X` and confirm each hits the root Studio.

- **Edit ops are validated at validate time and refusals name the right rule (`service/core.py`, `service/mcp_app.py`, `docs/tool-reference.md`, `service/tests/test_edit_op_validation.py`, `service/tests/conftest.py`; PRD 0050 D, #72).** (1) `create_widget` `widget_type` and `create_object` `object_type` are checked in `bridge_edit` / `dry_run` against the bridge's builtin type catalog (`/bridge/types/ui`, fetched once per batch; a name outside it is confirmed with the bridge's type resolver, which also knows `Module.Type` names such as `AlarmGrid`; a type path is checked against the live model and the batch's own `create_type` ops) and refused with `invalid_widget_type` / `invalid_object_type`, `did_you_mean` and `op_index` instead of failing at apply with `type_not_found`. A present-but-empty `widget_type` / `object_type` is `missing_field` (it used to create a `BaseObject`; omit the field for the documented default, and the `default_widget_type` warning is unchanged for an absent `widget_type`); the per-noun `bridge_create_object` / `bridge_create_widget` refuse it too. The check fails open when the bridge cannot answer. (2) `create_variable` / `create_object` names containing `/`, `\\`, control characters or leading/trailing whitespace are refused with `invalid_browse_name` (`.`, `[`, `]`, `@` stay allowed), so `D/E` is no longer created and then reported `create_variable_not_created`; the post-create existence check now builds an escaped path (the name is one `quote(safe="")` segment). (3) `attach_string_formatter` / `attach_formatter` `format` is checked for balanced braces and `{n}` / `{n:spec}` placeholders with an integer `n` (`attach_formatter` also `{#name}` with `name` bound in `sources`): `{0:F1`, `{#}`, `{#y}` return `bad_format_string` with the offending `token`, at validate time and from the per-noun tools; `unbound_placeholder` is kept. (4) A batch whose failing op had `applied == 0` returns `state: "failed"` with "op N failed before anything was applied; the model is unchanged" (the journal records `failed`; `action="continue"` answers `batch_not_resumable` with a nudge); `state: "partial"` and the not-atomic wording only when `applied > 0`. Existing tests that used `{#k}` without a matching `sources` name, or asserted `partial` for a first-op failure, were updated. The type check is done in the service, so `StudioMCPBridge.cs` is unchanged.

- **`invoke_method` and `retype` report real outcomes (`studio-bridge/StudioMCPBridge.cs`, `service/core.py`, `service/mcp_app.py`, `service/tests/test_invoke_retype_outcomes.py`; PRD 0050 C, #73).** `invoke_method` used to answer `ok: true` for a method that does not exist, one called with the wrong number of arguments, and one that threw (Optix catches and logs the exception, so `ExecuteMethod` returns normally). The bridge now resolves the method first, with the same lookup as the `wire_event` check, and `ExecuteMethod` is not called when it fails: `{ok: false, error: "method_not_found", object, method, available: [...]}` (`export_method_not_loaded` + `reopen_required` for a NetLogic whose exports are not loaded) and `{ok: false, error: "argument_count_mismatch", expected, given}` (compared with the method's `InputArguments`; unreadable metadata skips the check). An `ExecuteMethod` exception, or an `error` / `fatal` / `exception` line Studio writes to `FTOptixStudio.0.log` during the call, is `{ok: false, error: "method_failed", detail}`. `bridge_invoke_method` returns all four as `{ok: false, error, code, ...}` results. `retype` tried each current value the way the route converts it only implicitly: a value that does not convert (e.g. `'true'` -> Int32) silently became the type's default while the call said `ok`. It is now refused before any write with `{ok: false, error: "value_unconvertible", code, count, samples: [...]}` unless the op carries `allow_value_loss: true` (tool parameter, batch op field, bridge query `allow_value_loss=true`); then the result has `values_reset: n` and a `warning`, and `dry_run` reports `would_reset: n`. `validate_ops` / `dry_run` on a batch report `value_unconvertible` with `op_index`. The C# was not compiled in this environment. Pending bench verification: on a Studio bench run `optix_bridge_invoke_method` with a nonexistent method name (expect `method_not_found` with `available`), with one argument too many (expect `argument_count_mismatch`), and on a method that throws (expect `method_failed`); then `retype` a String variable holding `true` to Int32 (expect `value_unconvertible`, nothing changed), with `allow_value_loss=true` (expect `values_reset: 1` and a warning) and with `dry_run=true` (expect `would_reset`).

- **`wire_event` validates its method and target node before it touches a handler (`studio-bridge/StudioMCPBridge.cs`, `service/core.py`, `service/tests/test_wire_event_validation.py`; PRD 0050 B, #70).** `replace=true` used to delete the existing handler first, so a wrong method name or a node that cannot raise the event left the button with no handler. The bridge now checks, before any handler is deleted or created (single call, each entry of `calls`, and `validate_ops`): (1) the method object exposes the method (a UAMethod child or one declared by its type), else `{ok: false, error: "method_not_found", object, method, available: [...]}`, and a NetLogic that exposes no method at all (exports not loaded) answers `export_method_not_loaded` with `reopen_required: true`, as `optix_execute_method` does; (2) the node's type declares the event (UI Items do; a Variable or Folder does not), else `{ok: false, error: "event_not_on_node", node, node_class, dotnet_type, event, hint}`. `validate_ops` reports both with `op_index`, so a batch is refused at validate time. With `replace=true` the new handler is built first (under a temporary `_replacing` suffix, renamed when the SDK allows) and the previous one deleted only once it is complete; a failure while building removes the half-built handler and leaves the previous one in place, and if deleting the previous handler fails the result is `replace_failed` with `previous_handler_kept: true` or `previous_handler_lost: true`. `bridge_wire_event` returns these refusals as `{ok: false, error, code, ...}` results (a batch counts them as failed ops); a valid wire behaves as before. The C# was not compiled in this environment. Pending bench verification: on a Studio bench run `wire_event` with `replace=true` and a nonexistent method on a button that already has a handler (expect `method_not_found` and the handler still present), on a Variable (expect `event_not_on_node`), and on a freshly created design-time NetLogic (expect `export_method_not_loaded`).

- **`import_tags` merges by default, validates its source file, refuses silent removals and reports what it did (`service/core.py`, `service/mcp_app.py`, `studio-bridge/StudioMCPBridge.cs`, `docs/tool-reference.md`, `skills/optix-project-lifecycle/SKILL.md`, `service/tests/test_import_tags.py`; PRD 0050 A, #69).** Every call used to leave the station with only that call's tags, and an empty / CSV / non-XML / latin-1 source gave `ok: true, added: []` with the station emptied. Now the service checks an offline `file` (absolute or project-relative, as the bridge resolves it) before any bridge call: it must exist, be non-empty and parse as XML with root `RSLogix5000Content` (L5X) or `PlcProjectInfo` (TPY), else `{ok: false, error: "invalid_import_file", path, detail}`; the symbol names (L5X `Tags/Tag[@Name]`, TPY `Symbols/Symbol/Name`) are sent as `source_symbols`. New `replace: bool = false` (tool, batch op, bridge query `replace=1`): the bridge filter now keeps a symbol when it matches a requested tag OR is already on the station (the `before` snapshot names); `replace=true` keeps only the requested tags. With `replace=false`, a station tag absent from `source_symbols` (the importer rewrites the tag tree from the file, so a file that lacks a tag removes it) is refused before importing with `{ok: false, error: "would_remove_tags", removed_would_be, hint: "pass replace=true to accept, or import from a file that still contains them"}`. The result adds `removed` (paths before minus after) and `skipped` (`[{symbol, reason}]` for symbols the filter matched that do not exist afterwards, e.g. an unknown DataType); `added` / `already_present` / `not_found` stay. The filter rule is mirrored in the C# comment above `ImportTagsKeepsSymbol` and in the `import_tags_symbol_matches` docstring; the C# stays reflection-only so it compiles on 1.7 and 1.8. Pending bench verification: on an RA station run `import_tags tags=[Motor1] file=A.L5X`, then `tags=[Motor10]` (expect `would_remove_tags` unless A.L5X holds both, and Motor1 kept when it does), then `replace=true` (expect `removed` listing Motor1), and `file=empty.L5X` (expect `invalid_import_file`).

- **`optix_project(new)` validates names and detects create failures; `open` reports a missing project file (`service/core.py`, `service/mcp_app.py`, `docs/tool-reference.md`, `service/tests/test_project_new_validation.py`; PRD 0049 F, #67).** `project_new` checks the name up front (`validate_project_name`): a C# identifier (letters, digits, underscore; no leading digit, spaces or trailing dot; not a C# keyword) with `len(projects_root / name) <= 180`, else `{ok: false, error: "invalid_project_name", rule, name}`. The create wait scans the Studio log written since launch for `Error creating new project` and returns `{ok: false, error: "create_failed", detail}` at once instead of waiting 180 s. `project_open` on a directory whose `<name>.optix` is missing or empty returns `error: "project_file_missing"` with `path` rather than `upgrade_required` when Studio shows its upgrade dialog. Pending bench verification: run `optix_project(action="new", project="CI r18 sp")` and a `new` with a bad template on the bench and confirm `invalid_project_name` / `create_failed`.

- **Save and arm failure hints name the real cause (`service/core.py`, `service/studio_arm.py`, `service/tests/test_save.py`, `service/tests/test_bridge_sync.py`, `service/tests/test_studio_arm.py`; PRD 0049 E, #66).** `optix_save` with `saved:false` now returns `cause`: `studio_exited` when the targeted Studio pid is gone (hint "Studio closed or crashed during the save", no busy/focus text), `write_denied` when the Studio log written since the first keystroke has `Access is denied` / `copy_file` (hint and `denied_path` carry the path from the log), else `busy_or_focus` with the unchanged 0047 busy > focus > UIPI hint. `blocking_window_for` never reports a window owned by the service's own process tree (service pid, its ancestors, the hosting conhost / OpenConsole / WindowsTerminal; `_own_process_tree_pids`, computed once per call), on both the UIA and Win32 sources. `optix_bridge_arm` that ends in `verify_timeout` runs the `optix_build_check` logic and, when the NetSolution has C# errors, returns `error: "netsolution_build_failed"` with `detail` = the first error line and a hint (a stale-.references result, a build that cannot run or a clean build keep `verify_timeout`). Pending bench verification: close Studio mid-save and arm a project with a broken NetLogic on the bench and confirm `studio_exited` / `netsolution_build_failed`.

- **Web observe/interact fail on Chrome error pages and the Optix "Server unreachable" overlay (`service/core.py`, `service/_cdp.py`, `service/tests/test_cdp_page_error.py`; PRD 0049 D, #65).** A screenshot of `chrome-error://chromewebdata/` used to return `succeeded` (and a click "dispatched" into the error page). Before a web screenshot / click / fill / type / key / dom_text / find_text / read_text / ocr returns success, `_page_error_check` inspects the page in the SAME CDP session with a single `Runtime.evaluate` (`CDPClient.page_state()`: URL, title, first 4000 chars of body text, visible `disconnect` banner). A `chrome-error://` main frame, or a title starting with the site host plus an `ERR_*` code in the body, returns `{state: "failed", error: "page_error", detail, url, hint}`; "Server unreachable" text or a visible disconnect banner returns `error: "runtime_unreachable"`; the hint is "the emulator is not serving this page: optix_emulator status / run". `navigate` skips the check (it is how you leave an error page), and an evaluation that fails or returns nothing usable fails open. Screenshot bytes are not attached to the failure. Pending bench verification: stop the emulator, run `optix_observe(mode="screenshot")` and `optix_interact(action="click", x=0.5, y=0.5)` on the bench and confirm `page_error` / `runtime_unreachable`.

- **Same-named projects are resolved by directory; `new`-launched Studios are attributed (`service/core.py`, `service/studio_arm.py`, `service/tests/test_project_open_truth.py`, `service/tests/test_studio_arm.py`, `service/tests/test_save.py`; PRD 0049 C, #63/#66).** When another directory (projects root, running Studio command lines, bridge registry) holds a project of the same name, a window title / tree-root NAME is no longer accepted as proof that a Studio has the requested copy open (`_studio_window_for(ambiguous=, proven_pids=)`, `window_title_names_project(name_only_ok=)`; a bridge without `project_dir` is skipped likewise). Proof must be directory-bearing: bridge health `project_dir`, the Studio command line, or the service's own launch record (`_STUDIO_LAUNCH_DIRS` `{pid: project_dir}`, filled by `project_open` / `project_new`, dropped when the pid is gone). `optix_project(open)` on the sub path therefore launches Studio with the full `.optix` path and reports `opened` with the new pid. New `project_name_ambiguity()` returns `{ok: false, error: "ambiguous_project_name", candidates: [{dir, pid|null}], hint}` with no proof; `save` and the emulator `run` use it before `ambiguous_studio`. `studio_arm.cmdline_project_paths` accepts `new <name> <root>` -> `<root>\<name>`, used by `_studio_pid_for_project` and `_studio_open_candidates`, so a multi-Studio save of a new-launched project no longer returns `ambiguous_studio`. Pending bench verification: with `<root>\X` open in one Studio and `<root>\Site\X` requested, run `optix_project(action="open", project="Site/X")` on the bench and confirm a second Studio opens and its pid is returned.

- **Emulator web port must be owned by this project's runtime (`service/core.py`, `service/tests/test_run_emulator.py`; PRD 0049, #64).** `reachable` only said something answered on the web port. New `_port_owner` resolves the LISTEN socket's pid (`psutil.net_connections`) and compares it with `_emulator_pids(project)` (plus port-attributed emulators). `optix_emulator(action="run")` returns `{launched, serving: false, error: "web_port_taken", owner: {pid, name}, hint}` when another process holds the port, and `run`/`restart` scan the runtime log written since the F5 (`_runtime_log_mark` / `_bind_failed_since`) for `Bind to server address failed` (any code) and return `error: "web_bind_failed"` with `port` and `detail` at once instead of polling for `ready_timeout`. `status` gains `port_owner` (`runtime`|`foreign`|`none`, plus `port_owner_process` when foreign) and keeps `port_reachable`; a foreign owner is no longer reported as `running`. `_wait_runtime_port(serving=True)` and `_page_ready_once` refuse a foreign-owned port, so `page_ready` is never reported for a foreign page. An unreadable connection table fails open (previous behaviour). Pending bench verification: occupy the project's web port with another process (e.g. `python -m http.server <port>`), run `optix_emulator(action="run")` on the bench and confirm `web_port_taken` with that pid.

- **Web capture activates background CDP tabs and bounds its timeouts (`service/_cdp.py`, `service/core.py`, `service/tests/test_cdp_activate.py`, `service/tests/conftest.py`; PRD 0049, #62).** Chrome throttles background tabs, so a page target for the runtime port could hang a websocket connect/read and be mistaken for a detached one (closed and replaced by a second tab). `_cdp.activate(cdp_url, target_id)` calls `GET /json/activate/<id>` (idempotent, never raises, non-200 ignored). `_cdp_session` (via the new `_cdp_attach`), `cdp_reresolve_tab` and `_page_ready_once` activate the tab before attaching; on a websocket connect/read timeout `_cdp_attach` activates again and retries exactly once, and a second timeout makes `cdp_screenshot_runtime` return `{state: "failed", error: "cdp_tab_timeout", target_id, url, hint}` instead of raw exception text (other tools raise `CDPTabTimeout`, a `CDPUnavailable` with the same code). `CDPClient` now uses `CDP_CONNECT_TIMEOUT_S = 5` for the handshake and `CDP_READ_TIMEOUT_S = 10` for every later read (was 30 s for both) and closes its socket if `Page.enable` fails. `cdp_reresolve_tab` prefers an existing tab for the runtime port: a tab whose probe only times out is kept rather than closed, only the OTHER tabs for the port are closed (`closed: [...]`), and `created: True` is present in the result only when a tab was actually opened. `page_probe` reports `timed_out`. Tests use a fake CDP transport; `conftest.py` stubs the activate HTTP seam. Pending bench verification: with the runtime tab in a background Chrome window, run `optix_observe(mode="screenshot")` on the bench and confirm one runtime tab remains.

- **Re-arm probes bridge ports in order and stops at the first match (`service/studio_arm.py`, `service/core.py`, `service/tests/test_studio_arm_ports.py`; PRD 0048 C, #61).** `serving_port_for` used to call `bound_ports()` (a health probe of every port in 8768-8771) and then probe each serving port a second time, with a 3 s timeout per dead port. It now takes `preferred: int | None`, probes the preferred (registry) port first and then the rest of the range ascending, makes exactly one `_bridge_health` call per port, returns at the first matching bridge and never calls `bound_ports()`. `_bridge_health` and `bound_ports` use a 0.5 s loopback timeout (`_LOOPBACK_TIMEOUT_S`; a serving bridge answers in ~20 ms, and a connect that takes 2 s to be refused on some Windows boxes counts as not serving). `execute_method` gains `preferred_port`; `bridge_arm` and the NetLogic execute path in `service/core.py` pass the registry port (new `_registry_port_for`) when an entry exists. `bound_ports()` stays for `list_bridges`.

- **`server.json` declares the real transport: streamable-HTTP (`server.json`, `service/main.py`, `service/tests/test_version_parity.py`, `README.md`, `docs/cowork-quick-install.md`; PRD 0048 B, #60).** The registry manifest said `stdio`, but the entry point serves streamable-HTTP. `packages[0].transport` is now `{"type": "streamable-http", "url": "http://127.0.0.1:8766/mcp"}`. `service/main.py` exposes `MCP_PATH` (`/mcp`), used by the startup banner; new `test_server_json_transport_matches_entry_point` checks type and URL path against it. README and the Cowork quick-install state streamable-HTTP at that URL.

- **Wheel packaging: skills, bridge source and dashboard ship in the wheel; Pillow is a hard dependency (`pyproject.toml`, `MANIFEST.in`, `setup.py`, `service/core.py`, `service/http_app.py`, `service/auth.py`, `service/native_capture.py`, `service/tests/test_packaging_resources.py`, `README.md`; PRD 0048 A).** A pip install had no `skills/` or `studio-bridge/` (the skill tools were empty and arm reported `bridge_source: unknown`) and no Pillow (`optix_observe target="native"` raised `No module named 'PIL'`). `setup.py`'s `build_py` copies `skills/*/SKILL.md` and `StudioMCPBridge.cs` into `service/_bundled/` in the build directory only (gitignored, never in the checkout); `service/static` and `_bundled` are package data and `MANIFEST.in` covers the sdist. `_skills_dir()` falls back to the bundle after the checkout locations, and `_SHIPPED_BRIDGE_CS` is replaced by `_shipped_bridge_cs() -> Path | None` used by every reader. New `GET /bridge-source` serves the `.cs` as `text/plain` with an `X-Ftx-Mcp-Version` header. `Pillow>=10` moves into the dependencies (`[visual]` stays as an empty extra) and a missing Pillow yields `error: "pillow_missing"`, hint `pip install Pillow`. Checkout installs keep reading the repo copies.

- **`optix_project(action="open")` names the real Studio knob (`service/core.py`, `service/tests/test_project_open_truth.py`; #57, r16).** The `not_opened` reason for a project saved by a newer Studio now says to point `FTOPTIX_STUDIO_EXE` at a Studio that can open it; the previous wording named `FTOPTIX_STUDIO_VERSION`, which the service never reads. Known issue #58 (aged-Studio saves credited to the tree-focus re-send) is documented in the release notes and left for 1.0.10.
- **Docs, hints and docstrings for the native engine and open/emulator wording (`docs/tool-reference.md`, `docs/release-notes-v1.0.9.md`, `service/core.py`, `service/mcp_app.py`, `service/tests/test_project_open_truth.py`; PRD 0047 D, #55).** `docs/tool-reference.md` has exactly one `optix_observe` and one `optix_interact` row, each describing `target="web"|"native"` once (the duplicated sentences are gone). The release notes gain a "Native presentation engine (PR #6, Jonathan Callahan)" highlight. The two `native_runtime_not_found` hints now say `optix_emulator(action='run')` (there is no `start` action). `optix_project(open)` `not_opened` no longer claims "most likely open in another Studio" when no Studio was running before the launch; it says the project may need a newer Studio (`FTOPTIX_STUDIO_VERSION`) and names the version the project file records when readable. The `optix_interact` docstring notes that native `type` needs a prior `click` (focus), `fill` is click+type+submit, and that whether Enter commits a TextBox to its bound variable is the project's binding mode. Known-issues wording for #45/#51/#54 reads "saves during Studio's NetSolution build are re-sent until they land".

- **Trend-pen live test collects names from children and properties (`service/tests/test_bridge_live.py`; PRD 0047 D).** `test_trend_pen_create_variable_flow_live` now gathers the pen name from both `children` and `properties` of `describe_node(Pens)`, as `test_trend_pen_set_property_creates_color_live` does. Test-only change. Pending bench verification: run `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<p> python -m pytest service/tests/test_bridge_live.py -k trend_pen_create_variable_flow -s` on the bench.

- **`optix_save` re-sends Ctrl+S until the write lands; `bridge_create_netlogic` reports the NetSolution build (`service/core.py`, `service/mcp_app.py`, `service/tests/test_save.py`, `service/tests/test_bridge_live.py`, `docs/`, `skills/optix-netlogic-and-bridge/SKILL.md`; PRD 0047 A, #54).** Studio drops Ctrl+S while it rebuilds the user .NET solution after a design-time NetLogic create, so the 0046 burst of three attempts within ~6 s reported `saved:false`. `save` now sends Ctrl+S, polls, and re-sends every `_SAVE_RESEND_S` (5 s) within `timeout` (30 s); the second keystroke is the tree-focus variant, later ones plain Ctrl+S. Polling continues `_SAVE_WRITE_GRACE_S` (15 s) after the last keystroke (wall time <= timeout + grace). The result adds `keystrokes_sent` and `write_latency_s`; `method` is `ctrl_s` only if the mtime advanced before the second keystroke, else the keystroke that preceded the write. The File-menu attempt and `title_clean` are removed. Hint order on `saved:false`: Studio busy with the NetSolution build, focus, UIPI (only when unfocused or integrity differs); the "nothing unsaved" sentence is dropped. `bridge_create_netlogic(kind="design_time")` returns `netsolution_build: {state: "building", note}` without waiting. Pending bench verification: run `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<p> python -m pytest service/tests/test_bridge_live.py -k save_after_design_time -s` on the bench.

- **`native_capture` / `native_input` use private `WinDLL` handles with explicit prototypes (`service/native_capture.py`, `service/native_input.py`, `service/tests/test_native_screenshot.py`, `service/tests/test_native_interact.py`; PRD 0047 B).** Both modules called the shared `ctypes.windll.user32`/`gdi32`, whose restypes the `uiautomation` package rewrites, so a 64-bit HWND/HDC could truncate or raise `OverflowError` on the capture path. Each module now lazily creates its own `WinDLL("user32"/"gdi32"/"shcore", use_last_error=True)` on first use (Windows only) and sets `argtypes`/`restype` for every function it calls, using the `c_void_p`-based `wintypes.HWND/HDC/HBITMAP/HGDIOBJ` and `WPARAM/LPARAM`. Behaviour is otherwise unchanged (blank, error and region handling as in PR #6). New seams `_is_windows()` / `_user32()` / `_gdi32()` / `_shcore()` let the tests drive the real Win32 call sequence off-Windows with fake DLLs built from the prototype tables, including `0x1_0000_0001` handles. Pending bench verification: run an `optix_observe(target="native")` screenshot and an `optix_interact(target="native")` click on the bench after Studio UIA has been used in the same service process.

- **PR #6 (Jonathan Callahan): native presentation engine support for `optix_observe` / `optix_interact`** — ported onto 1.0.9 by hand (the 1.0.9 `_do_observe` recovery wrapper and per-project `_cdp_session` kept); the two entries below are his.

- **`optix_observe(target="native")` — read the native presentation
  engine.** New `target` param (`"web"` default, `"native"`) on every
  `optix_observe` mode but `diff`. Native captures the FTOptixRuntime.exe
  desktop window itself (Win32 `PrintWindow` with `PW_CLIENTONLY |
  PW_RENDERFULLCONTENT`, new `service/native_capture.py`), so a project
  running the native Qt renderer can be verified without Chrome, CDP or a
  WebPresentationEngine node: `screenshot` returns the JPEG, and `ocr` /
  `read_text` / `find_text` run tesseract on that frame instead of the CDP
  canvas. Finds the emulator process for `project` (any emulator when
  omitted) and picks its largest visible top-level window. ~30 ms for a
  1280x800 client. The window may be behind others but must not be
  minimized. Structured failures: `native_runtime_not_found`,
  `native_window_not_found`, `print_window_failed`, `bad_region`; `blank:
  true` flags an unpainted frame. The native frame's coordinates (and
  `find_text`'s `center_px`) are the window's physical client pixels, the
  same frame `optix_interact(target="native")` clicks in, not the CDP
  viewport. `navigate_url` / `fresh` are ignored natively.

- **`optix_interact(target="native")` — drive the native presentation
  engine.** click / fill / type / key posted straight to the
  FTOptixRuntime.exe window (`PostMessageW`; new `service/native_input.py`):
  no Chrome or CDP, the window may be behind others, and the user's
  foreground focus is never taken. Coordinates are the
  `optix_observe(target="native")` frame (fractions when both <= 1, else client pixels), reported back as
  `px`/`py` with `frame: {w, h}`. Text goes out as one WM_CHAR per
  character (posting WM_KEYDOWN too types each character twice on Qt);
  named keys as WM_KEYDOWN/KEYUP. Verified live on Chiller_HMI: posted
  clicks switch screens and open a SpinBox's on-screen keypad, posted text
  edits the value, Escape cancels, all with the runtime in the background.
  `navigate` is web-only; `select_all` / `navigate_url` are ignored
  natively (an Optix field selects its content on focus).

- **Release documentation corrections for 1.0.9 (`README.md`, `docs/tool-reference.md`, `docs/release-notes-v1.0.9.md`, `CHANGELOG.md`, `service/mcp_app.py` comment, `skills/optix-netlogic-and-bridge/SKILL.md`; ftx-mcp-h4a.4).** Docs, plus the playbook-count guard test. README, tool reference and release notes now state the same bundled playbook count, 23, equal to the number of tracked `skills/*/SKILL.md` files (they said 19). `test_playbook_count_matches_tracked_skills` no longer excludes four skills as private-only, so it checks the docs against all tracked playbooks. The release notes say undeclared properties return `unknown_property` (service guard), describe the fast `/health` and the elapsed time `start` prints, and no public doc cites the internal template drift script any more (template refresh points to the runbook's manual copy steps; old-template projects are synced on their first arm). The `mcp_app.py` comment says 22 per-noun primitives. Known issues states #45 is fixed (superseded by PRD 0046 A: `optix_save` focuses the project tree and re-sends Ctrl+S). The performance wording is "seconds of fixed overhead per call on some machines (9–14 s on one box with endpoint protection, about 2 s on a plain VM)". "Pending bench verification" is removed from the `import_tags`, bridge sync (#47/#48) and PRD 0044 A-G entries, whose items are recorded as passed in rounds r11-r13 by PRD 0045 D.6 (`docs/prd/0045-r13-findings-batch.md`); this session did not re-run those bench rounds.

- **Live suite self-seeds and cleans up after itself; `ftx live` takes `-m` (`service/tests/test_bridge_live.py`, `service/tests/test_live_ra_helpers.py`, `service/tests/README.md`, `scripts/bench/ftx`, `scripts/bench/live-s1.sh`, `scripts/bench/live-s1.ps1`; ftx-mcp-h4a.3).** The `live` fixture creates `UI/MainWindow/Rectangle1` with `create_widget` when it is missing, and a seeding failure FAILS the suite instead of skipping. Every fixed node, driver, station and NetLogic class name the tests create (e.g. `ZzzVvk1Driver`/`ZzzVvk1Station`) now carries a per-run unique suffix (`_nm`) and is deleted in teardown; the design-time and reject NetLogic C# sources are templated on the class name. The trend-pen tests compare the Color value numerically (`_color_value_matches`: decimal or hex text parsed to an unsigned 32-bit value, alpha ignored) instead of asserting the hex string inside a UInt32 describe value. `test_open_after_new_reports_truthfully_live` and `test_template_project_arm_syncs_then_reports_current_live` delete their `zzOpenTruth*` / `zzBridgeSync*` project folder after closing the Studio they launched (`_remove_scratch_project`, refuses anything else). `scripts/bench/ftx live` accepts `-m '<expr>'` as well as `-k '<expr>'` (a bare fifth argument is still `-k`); `service/tests/README.md` documents `-m` and that `pip install -e ".[dev]"` is required again after the five-step upgrade. Offline unit tests cover the colour matcher, the folder cleanup guard and the seeding. Pending bench verification: the live tests and `ftx live -m` were not run in the executor sandbox (no Studio, no bench); run `scripts/bench/ftx live <rnd> <label> <proj> <port> -m 'not slow_live'` and `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<p> python -m pytest service/tests/test_bridge_live.py -q -rs` on the bench.

- **`optix_save` focuses the project tree and re-sends Ctrl+S; verification covers the full 30 s from the last keystroke (`service/core.py`, `service/studio_arm.py`, `service/mcp_app.py`, `service/tests/test_save.py`; PRD 0046 A, #51).** Studio 1.8 has no UIA File menu, so the 0045 fallback could never fire. When Ctrl+S has not advanced the project mtime within `_SAVE_CTRL_S_WINDOW_S`, `save` finds the project's Studio window (restricted to the bridge pid when known), left-clicks the project-tree row (`studio_arm.focus_project_tree`, built on `_candidate_rows`; prior cursor and foreground restored) and sends Ctrl+S again (`method: "tree_focus_ctrl_s"`). File > Save is a third attempt, only if a "File" menubar entry exists. Every try is listed under `attempts: [...]` (`tree_row_not_found`, `file_menu_not_found`, ...); the raw `menu_fallback` key appears only with `debug=True`. Default `timeout` is 30 s (`Config.save_timeout_seconds` and the `optix_save` parameter; was 12) and is measured from the LAST keystroke, so a slow fallback never consumes the poll budget. The result adds `title_clean` (window title lost its `*`; `null` if unreadable); `saved` stays mtime-based. Hint order on `saved: false`: nothing unsaved, focus/attempts, UIPI (only when `focused` is false or integrity levels differ). `optix_bridge_arm` is unchanged. Pending bench verification: the tree click and `title_clean` need a live Studio; run `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<p> python -m pytest service/tests/test_bridge_live.py -k save_after_design_time -s` on the bench.
- **Live pen-colour tests read the typed describe value (`service/tests/test_bridge_live.py`, `service/tests/test_live_ra_helpers.py`; PRD 0046 B, #52).** Test-side only. `_color_value_matches` strips a trailing ` (<Type>)` annotation (and the `<untrusted>` wrapper) before parsing, so `4294901760 (UInt32)`, `4294901760` and `#ff0000` all match opaque red; `test_trend_pen_create_variable_flow_live` and `test_trend_pen_set_property_creates_color_live` no longer fail on the typed value. No change to the service or the bridge. An offline unit test covers the three shapes.

- **Win32 blocking-window source for arm and emulator run/restart (`service/studio_arm.py`, `service/core.py`; PRD 0046 C, ftx-mcp-775.6).** `blocking_window_for` now has a second source after UIA: visible titled top-level Win32 windows (ctypes `EnumWindows`). A foreign-process window that is the foreground window or overlaps Studio's window is reported as `blocking_window {title, process, pid, source: "uia"|"win32"}` with `probable_cause: "os_dialog"`, and the hint names the window and says to dismiss it. Emulator `run`/`restart`, when UIA sees no dialog, use the same helper and add `blocking_window` and a naming hint; with nothing visible behaviour is unchanged. PRD 0046 D (`live-s1` bench wait) is specified but NOT yet implemented.

- **`optix_save` retries Ctrl+S when it does not take (`service/core.py`, `service/studio_arm.py`, `service/mcp_app.py`, `service/tests/test_save.py`, `service/tests/test_bridge_live.py`; ftx-mcp-h4a.2).** If Ctrl+S has not advanced the project mtime within 2 s (the active document, e.g. the Dashboard or a fresh code editor, swallowed it), `save` first (PRD 0046) clicks the project-tree row and re-sends Ctrl+S, and only then invokes Studio's File > Save (when a "File" menu entry exists) through the UIA menu path arm uses (`studio_arm.save_via_menu`, built on `_open_menu_items`; no second menu walker) and keeps polling for the rest of the timeout. The result reports `method: "ctrl_s" | "menu"` (plus `menu_fallback` when the menu was tried). A greyed-out Save item returns early as nothing-to-save. When neither works the result keeps `saved: false` and the hint lists, in order: (1) nothing unsaved, (2) the active document does not take Ctrl+S (click the project tree, or use `optix_bridge_arm`'s menu path), (3) UIPI integrity mismatch, only when `focused` is false or the Studio process elevation differs from the service's (`studio_arm.probe_elevation`). `ambiguous_studio`, `no_studio_window`, `bridge_studio_no_window` and pid targeting are unchanged. Unit tests mock the runner and the menu path. Pending bench verification: the UIA File menu walk was not run in the executor sandbox (no Studio); run `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<p> python -m pytest service/tests/test_bridge_live.py -k save_after_design_time -s` on the bench.

- **`/health` is a fast liveness probe; PowerShell health waiting is wall-clock bounded (`service/core.py`, `bootstrap/_common.ps1`, `bootstrap/services.ps1`, `service/tests/test_health_liveness.py`, `tests/bootstrap/services-status.Tests.ps1`; ftx-mcp-h4a.1).** `/health` no longer probes bridges (it used to run a full bridge scan, which stalled behind stale registry files and slow ports). `bridge_source` now comes from the last completed bridge scan's cached results only while they are fresher than 30 s, otherwise `"unknown"`, and the new `bridge_source_age_s` field reports how old that data is (`null` when no scan has run). `optix_bridge_status` still probes as before, and all other `/health` fields (including `service_path`) are unchanged. `Get-FtxHealth` now uses a 5 s timeout, `Wait-FtxHealth` is bounded by a Stopwatch on wall-clock time instead of counting sleeps (a probe slower than the interval no longer stretches the wait), `services.ps1 start` prints the elapsed time with `up` (and takes `-HealthWaitSec`, default 30), and `status` prints `up` as soon as `/health` answers (`starting`, `stop` and the setup own-service hint are unchanged). Pending bench verification: the Pester 5 tests and a PowerShell parse check were not run in the executor sandbox (no PowerShell); run `Invoke-Pester tests\bootstrap\services-status.Tests.ps1` on a Windows box.

- **Live-suite hygiene (`service/tests/test_bridge_live.py`, `service/tests/README.md`; ftx-mcp-wvk.7, closes #43).** Each NetLogic race / re-create live test now builds its class name with a per-run unique suffix (`_unique_suffix()`), so a leftover node or `.cs` from a crashed or parallel run cannot collide, and removes its node and `.cs` in `finally`. `test_observe_immediately_after_emulator_restart_live` runs `restart` and asserts `serving` on every cycle (no fixed sleep), so it works from a cold emulator. The slow tests stay `@pytest.mark.slow_live` (default Tier-2 run is `-m "not slow_live"`; the full set is a plain `pytest service/tests/test_bridge_live.py`, documented in `service/tests/README.md`). A failed NetLogic create in the race, re-create and design-time live tests is now a FAIL, not a skip, because the bridge is up at that point. No product code changed.

- **`optix_project` open attributes `already_open` by live window or command line, not a stale log line (`service/core.py`, `service/tests/test_project_open_truth.py`; ftx-mcp-wvk.6, closes #46).** The `studio_log` source of `already_open` is accepted only for a Studio pid whose live window title or command line names THIS project (`core._pids_naming_project`); a window-title or command-line match decides first, as for the `window_title` source. When the log names the project but the pid's window shows a different project, the project is treated as not open and the open proceeds. `already_open` with the same pid after `new` (r3 #25) and a clean open after close are unchanged. Unit tests use two mocked Studios (stale log line for A while the pid shows B).

- **`services.ps1 status` reports `starting` during startup, and `start` waits for `/health` (`bootstrap/services.ps1`, `bootstrap/_common.ps1`, `tests/bootstrap/services-status.Tests.ps1`; ftx-mcp-wvk.5).** While the service process is alive and both its ports (:8765, :8766) listen but `/health` does not answer yet, `status` prints `starting` instead of `/health down`; with no live process or ports it still prints `/health down`. `start` (and `restart`) now poll `/health` for up to 30 s and print `up` or `still starting`, exiting 0 in both cases. The process, port and `/health` checks are separate helpers in `_common.ps1` (`Test-FtxServiceProcessAlive`, `Test-FtxPortListening`, `Get-FtxHealth`, `Get-FtxServiceReadiness`, `Wait-FtxHealth`) so Pester 5 tests mock them.

- **Arm explains a blocking OS dialog (`service/studio_arm.py`, `service/tests/test_studio_arm.py`; ftx-mcp-wvk.4).** When arm's menu or click step fails (`menu_item_not_found`, `verify_timeout`, `uia_failed`) and the foreground top-level window does not belong to Studio (not the Studio window's pid, not an `FTOptixStudio` process), the result gains `probable_cause: "os_dialog"`, `blocking_window: {title, process}` and the hint "close or answer the dialog, then retry arm" (e.g. a Windows Firewall prompt over Studio). With no foreign foreground window the original error is unchanged. `bridge_arm` passes the result through, so it needed no change. Unit tests mock the window enumeration (`studio_arm._foreground_window`).

- **`create_object` accepts `Folder` and `FolderType` (`service/core.py`, `studio-bridge/StudioMCPBridge.cs`, `service/tests/test_bridge_writes.py`, `service/tests/test_bridge_live.py`; ftx-mcp-wvk.3).** `object_type` `Folder` or `FolderType` (any case) resolves to the standard OPC UA FolderType (ns=0 i=61), the same node `create_folder` makes, instead of failing type resolution. The service canonicalises the spelling (`core.resolve_create_object_type`) and the bridge resolves it ahead of the builtin catalog walk. When the bridge cannot create it on a Studio version it returns `type_not_found` with a hint naming `create_folder` (`optix_bridge_create_folder`, or op `create_folder` in `optix_bridge_edit`). Offline unit tests cover resolution and the hint; the live test (`test_create_object_folder_live`) creates a Folder under Model, checks describe shows it, and deletes it, and fails (not skips) when the bridge is up and setup fails.

- **`set_property` creates a type-declared optional child on demand (`studio-bridge/StudioMCPBridge.cs`, `skills/optix-verify-loop/SKILL.md`, `service/tests/test_bridge_writes.py`, `service/tests/test_bridge_live.py`; ftx-mcp-wvk.2).** When the named property is declared by the node's TYPE (VariableType chain for a variable, ObjectType chain for an object) but the instance lacks it, the bridge creates the child from the declaration, sets it, and returns `created_optional_child: "<name>"`. General, no TrendPen special case: a fresh TrendPen takes `set_property` `Color` in one call instead of `create_variable` first. A property the type does not declare still returns `property_not_found` (or `unknown_property`); the value is coerced before the child is attached, so a bad value creates nothing. `service/core.py` needed no change (its validator does not refuse the call). The verify-loop skill's Trend pens section documents the one-call route and keeps the two-step recipe as a fallback.

- **`optix_emulator(action="run")` is idempotent and scoped status fails safe (`service/core.py`, `service/mcp_app.py`, `skills/optix-verify-loop/SKILL.md`, `service/tests/test_run_emulator.py`; ftx-mcp-wvk.1).** `run` while this project's emulator is `running` or `starting` sends no F5 (F5 toggles, so it used to stop the emulator) and returns `state: "already_running"` with `serving` and the hint "use action='restart' to load structural edits"; while stopped it sends F5 as before. Project-scoped `status` now also attributes an emulator to a project whose name differs from its emulator cache folder when that emulator process serves the project's resolved web port: it reports `running` with `attribution: "port"` instead of `stopped` (cache-folder matches report `attribution: "path"`). An emulator whose cache folder names a different existing project is never attributed this way. The tool docstring and the verify-loop skill list the real states (stopped/starting/running/zombie/demo_expired) and say `run` is safe to repeat, `restart` reloads edits, status first is optional.

- **Every bridge-routed tool call lost seconds of fixed overhead per call on some machines (9–14 s on one box with endpoint protection, about 2 s on a plain VM).** Two
  causes, both on the path of every `optix_describe_node` /
  `optix_get_project_map` / `optix_bridge_edit` op (measured 2026-10-13 on a
  box with endpoint protection; the bridge itself answered in 10-50 ms):
  - `studio_guard._scan` asked psutil for `cmdline` on EVERY process before
    name-filtering, so it opened a handle on ~540 processes per scan (7-11 s
    where OpenProcess is hooked). It now name-filters first and reads the
    command line of the one or two Studio/editor hits only (~70 ms warm).
    This scan runs on every call whose project resolves through the
    open-in-Studio fallback (project outside `OPTIX_PROJECTS_ROOT`).
  - `_find_bridge_for` probed the whole bridge port range on every lookup,
    and a dead range port takes ~2 s to refuse where loopback TCP is
    intercepted; with the 2 s health-cache TTL nearly every call paid it.
    It now probes the registry-listed ports first and returns on a match;
    the full range scan still runs on a miss, so a pre-1.0.8 bridge with no
    registry file is found exactly as before. (PR #5, Jonathan Callahan; on 1.0.9 the registry fast path lives in `resolve_bridge` and short-circuits only on a unique match.)

- **One documented upgrade procedure for checkout installs (README, `docs/runbook.md`, `docs/release-notes-v1.0.9.md`; PRD 0042 #49; ftx-mcp-5iv.1).** Docs only. Five steps: `services.ps1 stop`, `git pull`, `uninstall.ps1 -PurgeVenv`, `setup.ps1`, `services.ps1 start`. The first `optix_bridge_arm` on each existing project syncs its bridge source and leaves a backup under `ProjectFiles\.ftx-mcp-backups\`; a BridgeTemplate is refreshed with the manual copy steps in `docs/runbook.md`. `pip install --upgrade` does not upgrade a checkout install and is not supported; the old "updates the service" claim is removed from current docs, and the 1.0.7/1.0.8 notes gain a "superseded" pointer. The README prerequisites now list the .NET SDK (8 or later), needed for `build_check`.

- **Skills document Trend pens and the arm bridge sync (`skills/optix-verify-loop/SKILL.md`, `skills/optix-netlogic-and-bridge/SKILL.md`; PRD 0041 #47/#48; ftx-mcp-5we.7).** Docs only. `optix-verify-loop` gains a "Trend pens" section: a pen is `create_variable` with `variable_type: "TrendPen"` under `<Trend>/Pens`, its colour is the `Color` child, the value is bound with `bind`, and `OPTIX_CDP_VIEWPORT` (e.g. `1920x940`) sets the screenshot viewport for screens larger than 1280x720. `optix-netlogic-and-bridge` documents the `bridge_source` values (`current` / `synced` / `stale` / `unknown`) and `FTX_BRIDGE_SYNC=0`.
- **`optix_bridge_status` and health report `bridge_source` (`service/core.py`, `service/mcp_app.py`, `service/tests/test_bridge_sync.py`; PRD 0041 #47; ftx-mcp-5we.2).** Read-only: each bridge entry in `optix_bridge_status` carries `bridge_source` (`current`, `stale` when the project's `StudioMCPBridge.cs` differs from the shipped one by normalized hash, or `unknown` when it cannot be compared), plus a top-level aggregate (any `stale` wins, then `unknown`, else `current`). `/health` carries the same aggregate over the armed bridges. Re-arm to sync a `stale` project. Nothing is written by status or health.
- **The internal template drift check compares normalized content hashes and gains `-Fix` (`ci/` script, not in the public tree, `service/tests/test_template_drift_guard.py`; PRD 0041 #47; ftx-mcp-5we.3).** The Studio project template's `StudioMCPBridge.cs` is compared with the repo copy by the same normalized sha256 as `arm` (BOM, CRLF/CR and trailing spaces/tabs ignored) instead of the `BridgeVersion` string, so drift with an identical version is caught and line-ending-only differences are not. A mismatch names both hashes and line counts. `-Fix` (opt-in) copies the repo's file over the template's and re-checks; without it the template is never modified.
- **`create_object` / `create_variable` never report success without a node (`service/core.py`, `service/tests/test_bridge_writes.py`; PRD 0041 #48; ftx-mcp-5we.4).** After the bridge answers ok, `bridge_create_object` (direct, UI-namespace-child and batch, which dispatches through it) and `bridge_create_variable` (not on `dry_run`) make one `GET /bridge/nodes?path=<parent>/<name>`. An explicit `node_not_found` returns `create_object_not_created` / `create_variable_not_created` with `parent`, `type` (`variable_type`) and a message saying nothing was created; in a batch the op fails. An unreachable bridge or any other answer is not treated as absent, so successful creates of containers, project types, drivers and stations still return ok. `TrendPen` is added to `_KNOWN_VARIABLE_TYPE_NAMES` (refused up front as `variable_type_not_object`, hint: `create_variable`).
- **`optix_bridge_arm` syncs the shipped bridge source into the project (`service/core.py`, `service/tests/test_bridge_sync.py`; PRD 0041 #47; ftx-mcp-5we.1).** On `arm`, the project's `ProjectFiles/NetSolution/StudioMCPBridge.cs` is compared with `studio-bridge/StudioMCPBridge.cs` by a sha256 over the normalized content (BOM, CRLF/CR and trailing spaces/tabs ignored). If they differ, the project's file is copied to `ProjectFiles/.ftx-mcp-backups/StudioMCPBridge.<ts>.cs.bak`, the shipped copy is written, and arming proceeds (StartBridge rebuilds from disk). The result carries `bridge_source` (`synced` with `bridge_backup`, or `current`) plus `bridge_project_hash` / `bridge_shipped_hash` (12 hex chars; the project hash is the pre-sync one). `FTX_BRIDGE_SYNC=0` reports `bridge_source: "stale"` with a `warning` and writes nothing. A project with no `StudioMCPBridge.cs` still gets today's `bridge_netlogic_absent` error. `stop` never syncs. Status-side `bridge_source`, the drift script and the live test (PRD 0041 reqs 2-4) are separate tasks.
- **`import_tags` uses the station's own TagImporter, else a temporary one it deletes afterwards (`studio-bridge/StudioMCPBridge.cs`, `service/core.py`; PRD 0040 #44, #19; ftx-mcp-jku.1).** If `station.TagImporter` already works (a station created in Studio's UI) it is used as is and never modified or deleted; the result reports `tag_importer: "existing"`. Otherwise the bridge resolves the importer type by exact BrowseName among the subtypes of the TagImporter base type (`RAEtherNetIPTagImporter` for RA, `TwinCATTagImporter` for TwinCAT; no substring match, no hard-coded NodeId), adds one `FtxMcpTagImporter` child to the station and reports `tag_importer: "temporary"`. The temporary importer is deleted when the task finishes: success, fault, cancel, or a validation error after it was created. Imported tags survive the delete. On `import_timeout` the task may still be running, so the importer is kept and the error carries `tag_importer_left: "<path>"` (the service returns it at top level and in the error entry); if deleting it fails the reply carries `tag_importer_left` plus `tag_importer_cleanup_error`. If the type cannot be resolved or `station.TagImporter` still fails after the add, whatever the call created is deleted and `import_failed` is returned with the diagnostics. Reflection only; the 1.7 build and the `requires_optix_1_8` dry-run refusal are unchanged.
- **Dead importer routes and the `create_object` station hook removed (`studio-bridge/StudioMCPBridge.cs`; PRD 0040 req. 3; ftx-mcp-jku.2).** The SDK-helper search (0038 route A) and the `DesignTimeNodes` tree (route B) never worked: the design project is not reachable from the bridge. They and their rollback/owner helpers are gone, `create_object` of a station is exactly what `MakeObject` gives and no longer reports `tag_importer`, and the diagnostics and hint no longer point at `DesignTimeNodes`.
- **`optix_bridge_edit` batch entries for `import_tags` carry the import result (`service/core.py`, `service/mcp_app.py`; PRD 0040 req. 4; ftx-mcp-jku.3).** The op's `op_timings` entry now includes `added`, `already_present`, `not_found`, `elapsed_ms`, `tag_importer` and `tag_importer_left` when present, and on failure the error `code` and `detail` (plus `hint`), matching the direct tool instead of just `ok: true`.
- **`Module.Type` spellings agree between dry run and apply (`studio-bridge/StudioMCPBridge.cs`; PRD 0040 req. 5; ftx-mcp-jku.4).** `create_object` with `FTOptix.RAEtherNetIP.RAEtherNetIPDriver` used to validate at dry run but fail apply with `type_not_found` (the catalog field is `Driver`, the BrowseName `RAEtherNetIPDriver`). When a name misses the module's ObjectTypes catalog, apply now also looks up the part after the module prefix by BrowseName in the type tree, restricted to that module's namespace. Both `...RAEtherNetIPDriver` and `...Driver` create the driver; same for the station.
- **Live tests no longer skip silently when the RA driver or station cannot be created (`service/tests/test_bridge_live.py`, `service/tests/test_live_ra_helpers.py`; PRD 0040 req. 6; ftx-mcp-jku.5).** `_RA_DRIVER_TYPE` / `_RA_STATION_TYPE` default to `RAEtherNetIPDriver` / `RAEtherNetIPStation`. With a live bridge, a creation failure FAILS the test with the error; a skip is allowed only when the RA module is absent (Studio below 1.8).
- **Tests for the station-owned importer flow (`service/tests/test_import_tags.py`, `service/tests/test_bridge_live.py`, `service/tests/test_bridge_resolution.py`; PRD 0040 req. 7; ftx-mcp-jku.6).** Source-level tests for exact-BrowseName type resolution, an existing importer never deleted, a temporary importer deleted on success/fault/validation error and kept and reported on timeout, no ensure call from `create_object` of a station, batch result fields, and the `Module.Type` fallback; live tests for the 1.8 import flow.
- **Skills: "Adding PLC tags" explains existing-versus-temporary importer and `tag_importer_left` (`skills/optix-project-lifecycle/SKILL.md`; PRD 0040; ftx-mcp-jku.7).** Replaces the 0038 text about creating a design-time importer.
- **Stale-capture self-heal never breaks a valid capture (`service/core.py`; PRD 0039 #23; ftx-mcp-e1i.1).** In `optix_observe(fresh=true)`, when a fresh capture is byte-identical to the previous one the recovery step (session reconnect, reload/navigate, re-capture) is now failure-safe: any exception, `CDPError` or otherwise, leaves the original capture intact and is reported as `stale_recovery: {attempted: true, resolved: false, error}` plus a `next_step` hint instead of failing the call. An identical fresh capture is reported, not failed.
- **Recovery does one reload-or-navigate, not navigate then reload, and waits for the load (`service/_cdp.py`, `service/core.py`; PRD 0039 #23; ftx-mcp-e1i.2).** The old navigate-then-reload raced the in-flight load. Recovery now reads the main-frame URL (`CDPClient.main_frame_url`) and reloads (ignoring cache) when already on the target, else navigates, exactly once, then calls the new bounded `CDPClient.wait_for_load` (`Page.loadEventFired` or `document.readyState == 'complete'`, at most settle + 5 s) before the settle sleep and re-capture.
- **Self-heal unit tests (`service/tests/test_cdp.py`; PRD 0039 #23; ftx-mcp-e1i.3).** Test-only, with an in-flight-aware fake CDP session: `test_cdp_screenshot_fresh_recovery_cdp_error_keeps_original_capture`, `test_cdp_screenshot_fresh_recovery_generic_exception_keeps_original_capture`, `test_cdp_stale_recovery_reloads_once_when_already_on_target`, `test_cdp_stale_recovery_navigates_once_when_url_differs`, `test_cdp_stale_recovery_load_wait_is_bounded_by_settle_plus_slack`, `test_cdp_client_wait_for_load_event_and_ready_state`.
- **Live tests for repeated fresh observes and restart cycles (`service/tests/test_bridge_live.py`; PRD 0039 #23; ftx-mcp-e1i.4).** `test_observe_fresh_repeated_no_restart_live` and `test_observe_fresh_after_each_of_5_restarts_live` skip without a live bridge; the bench run is pending (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k observe_fresh`).
- **Skills: observe guidance for identical fresh captures (`skills/optix-verify-loop/SKILL.md` rule 5c; PRD 0039 #23; ftx-mcp-e1i.5).** States that an identical fresh capture is reported (`stale_recovery.resolved: false` with a `next_step` hint), not failed.
- **Batch validator knows a station type creates its `Tags` and `Types` children (`studio-bridge/StudioMCPBridge.cs`; PRD 0038 req. 8; ftx-mcp-aaw.8).** A single batch that `create_object`s an RA EtherNet/IP or TwinCAT station and then creates a tag under `<station>/Tags` no longer fails with `unresolved_parent`. The hypothetical model adds the type's declared children when the type resolves (always including `Tags` and `Types`); unknown parents still report `unresolved_parent`.
- **`import_tags` API-mismatch `import_failed` carries importer and design-time-node diagnostics (`studio-bridge/StudioMCPBridge.cs`; PRD 0038 req. 1; ftx-mcp-aaw.1).** The error detail now names the importer's runtime type full name, its public property names and every public `Import*` method with parameter types, and says which of `Mode` / `FilePath` / `Import(Func<string,string,bool>)` is missing. The station-side error (getter throws, no `TagImporter` property, null importer) states whether a design-time TagImporter node exists and lists the paths searched (`DesignTimeNodes/<station>/TagImporter`, its parents, the station's own child). Reflection only (no static 1.8 type references). Tests: `test_csharp_api_mismatch_error_carries_importer_diagnostics`, `test_csharp_station_side_error_says_whether_design_time_node_exists_and_where_it_looked`, `test_csharp_diagnostics_have_no_static_1_8_references` (source-level). The C# needs a bridge rebuild and `build_check` on Studio 1.7 and 1.8 (no dotnet here).
- **`import_tags` binds any `(string, string) -> bool` `Import` overload; parameterless `Import()` is used only when `tags` is empty (`studio-bridge/StudioMCPBridge.cs`; PRD 0038 req. 4; ftx-mcp-aaw.4).** The bridge accepts any public `Import` overload with one delegate parameter of that shape (the stock `Func<string,string,bool>` is preferred) and builds a delegate of the importer's exact type with `Delegate.CreateDelegate` over the filter's closure. The parameterless `Import()` is used ONLY when `tags` is empty (meaning import everything); with a tag list it is never called, since it would silently import every symbol. `Mode` and `FilePath` are read by name from the runtime type or its base types. Test: `test_import_delegate_binds_custom_delegate_type_with_same_signature`. Needs a bridge rebuild.
- **`import_tags` on an RA EtherNet/IP station is refused at dry run on Studio < 1.8 (`studio-bridge/StudioMCPBridge.cs`; PRD 0038 req. 5; ftx-mcp-aaw.5).** The `validate_ops` case applies the same gate as the apply route and reports `requires_optix_1_8` (field `station`); TwinCAT stations still import from 1.7. A station created earlier in the same batch has no CLR type yet and is gated at apply. Tests: `test_import_tags_dry_run_refusal_requires_optix_1_8`, `test_dry_run_surfaces_requires_optix_1_8_from_bridge`. Pending bench verification.
- **`create_object` of a VariableType reports `variable_type_not_object` once, not twice (`studio-bridge/StudioMCPBridge.cs`; PRD 0038 req. 6; ftx-mcp-aaw.6).** The bridge dry-run validator no longer repeats the error; the service validator `validate_create_object_op` is the single source. The direct `/bridge/model/object` route still refuses it. Test: `test_create_object_variable_type_error_reported_once`.
- **Live test parent fixed: `CommDrivers`, not `CommunicationDrivers` (`service/tests/test_bridge_live.py`; PRD 0038 req. 7; ftx-mcp-aaw.7).** `test_create_typed_driver_tag_with_symbolname_live` now creates its driver under the real folder so it runs. Test-only.
- **Tests for the TagImporter work (`service/tests/test_import_tags.py`, `service/tests/test_bridge_live.py`; PRD 0038 req. 9; ftx-mcp-aaw.9).** Unit: `test_import_failure_diagnostics_shape`, `test_import_delegate_binds_custom_delegate_type_with_same_signature`, `test_import_tags_dry_run_refusal_requires_optix_1_8`, `test_import_tags_dry_run_reports_single_error`, `test_batch_validator_accepts_station_then_tags_path`. Live (skip without a live bridge; station created through the MCP): `test_import_tags_mcp_station_added_holds_member_and_parents_live`, `..._not_found_live`, `..._already_present_live`, `..._prefix_motor1_vs_motor10_live`, `..._save_and_build_check_zero_errors_live`, `..._dry_run_requires_optix_1_8_live`.
- **The NetLogic late guard restores only the corruption signature, never a legitimate edit (`service/core.py`; PRD 0037 #42; ftx-mcp-czz.1/.2/.3).** `_netlogic_late_guard` (the 60 s re-check after a delete restore) now restores a `.cs` only when it contains NUL bytes or starts with the snapshot bytes and continues past them (appended tail). Any other difference, including a complete valid file, is a deliberate edit: the entry is dropped silently with no restore and no `.drifted` copy. Deliberate class-source changes (`create_netlogic` with the same class, NetLogic source write/edit, `delete_node` / batch `delete`, project close / open) clear the registry entry before they run. `create_netlogic X` over an existing `X.cs` backs it up to `.ftx-mcp-backups/` first and adds a `netlogic_source_existed` warning (with the backup path) if the content changed, so create never silently replaces a source. Unit tests cover each case.
- **`optix_emulator(action="restart")` returns only when its page is usable; `page_ready` (`service/core.py`, `service/_cdp.py`; PRD 0037 #23; ftx-mcp-czz.5/.6/.7/.8).** After the web port serves, restart waits (bounded, default 20 s) until the project's page target reports `Page.getFrameTree` main-frame URL == runtime URL and `document.readyState == "complete"`; `waited_seconds` is the real elapsed time. On timeout it returns `serving: true`, `page_ready: false` and a hint. `optix_observe(fresh)` skips the reload for a page attached within the last 5 s. Recovery from a transient "Not attached" polls `page_probe` on the current target (up to 10 s with backoff) before re-resolving or recreating, retries once, and reports `cdp_recovered: true` or `false` (never null). `optix_cdp_restart`'s "already healthy" also checks the cached per-project tab's main frame via `Page.getFrameTree` on that exact target and reports `reattached` when it had to re-attach.
- **Live suite and CI fixes (`service/tests/test_bridge_live.py`, `service/tests/README.md`, `ci/`, `pyproject.toml`; PRD 0037 #43; ftx-mcp-czz.4/.9-.12).** The NetLogic race and re-create live tests are `@pytest.mark.slow_live` and excluded from the default live invocation (`-m "not slow_live"`; separate invocation documented in `service/tests/README.md` and `ci/README.md`). Race tests use a fresh class name per iteration; a live test encodes the #42 sequence. `test_observe_immediately_after_emulator_restart_live` builds its config after restoring the real LOCALAPPDATA and passes from a cold emulator; `test_execute_rejected_call_reported_rejected_live` waits (bounded) for the save's mtime change. New live tests for {restart -> observe(fresh)} and {restart -> cdp_restart -> observe(fresh)} cycles.
- **`import_tags` design-time op and `optix_bridge_import_tags` tool: add PLC driver tags through the station's Tag Importer (`studio-bridge/StudioMCPBridge.cs`, `service/core.py`, `service/mcp_app.py`; PRD 0036 #19; ftx-mcp-vvk.3).** `{station, mode: offline|online, file, tags[], timeout_s}` (also an `optix_bridge_edit` op; the direct tool is a `FTXMCP_BRIDGE_PRIMITIVES` primitive like its siblings). The bridge route `POST /bridge/station/import-tags` sets `station.TagImporter` `FilePath`/`Mode` and calls `Import(filter)`, keeping a symbol that equals a requested name or is its parent/child on `.` boundaries (`Motor1` never matches `Motor10`). The Task wait is bounded (default 120 s, `timeout_s` up to 3600) on the listener thread under a root session, never Studio's UI thread; faulted/cancelled tasks come back as `import_failed`/`import_cancelled` with the base exception message, a timeout as `import_timeout`. Result: `added`, `already_present`, `not_found`, `elapsed_ms` from before/after snapshots of the station's Tags subtree. An RA EtherNet/IP station on Studio below 1.8 is refused with `requires_optix_1_8` (same `studio_version` source as the pow gate); TwinCAT works from 1.7. The importer is reached by reflection only (no static `using` of the 1.8-only types), so the same source should build on both Studio versions. Tests: `service/tests/test_import_tags.py` (filter, version gate, argument validation, request/error mapping, source-level C# guards, L5X fixture `service/tests/fixtures/import_tags_sample.L5X`); live `test_import_tags_offline_nested_member_live` (1.8) and `test_import_tags_requires_optix_1_8_live` (1.7) skip without a bridge and `FTX_LIVE_RA_STATION`. Online mode has no live test (the bench has no controller). The C# needs a bridge rebuild and `build_check` on Studio 1.7 and 1.8 (no dotnet here).
- **`create_object` refuses a VariableType as `type` with `variable_type_not_object` (`service/core.py`, `studio-bridge/StudioMCPBridge.cs`; ftx-mcp-vvk.2).** Both the batch dry run and the direct `create_object` route refuse a `type`/`object_type` that resolves to a VariableType (e.g. `RAEtherNetIPTag`) before anything is created, with the hint `use create_variable with variable_type: "<type>"`; previously dry_run validated OK and apply failed with `not_a_type`. Python validator `validate_create_object_op` (well-known VariableTypes offline, plus a live-tree resolver hook); the bridge repeats the check on `/bridge/model/object` and `validate_ops` for any module's VariableType. Tests: `test_create_object_*` (`service/tests/test_bridge_writes.py`); live `test_create_object_refuses_variable_type_live` skips without a bridge. The C# needs a bridge rebuild (no dotnet here); live run pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k refuses_variable_type`).
- **`create_variable` resolves typed driver tag VariableTypes by short name, `Module.Type` or type path, and refuses ambiguity (`studio-bridge/StudioMCPBridge.cs`, `service/core.py`; ftx-mcp-vvk.1).** `variable_type` accepts `RAEtherNetIPTag`, `FTOptix.RAEtherNetIP.RAEtherNetIPTag` (pinned through the module's generated `VariableTypes` catalog) or a slash type path; a short name defined by several modules is refused as `ambiguous_variable_type` listing the `candidates` (also in the `validate_ops`/dry_run batch path). `properties` such as `SymbolName` are set before `parent.Add`, the node is made with `MakeVariable(name, dataTypeId, variableTypeId)`, and `describe_node` on a Variable now also reports `variable_type`. Tests: `test_variable_type_*`, `test_ambiguous_variable_type_*` (`service/tests/test_bridge_writes.py`, source-level for the C#; no dotnet here); live `test_create_typed_driver_tag_with_symbolname_live` skips without a bridge or when the RA driver/station cannot be created. The C# needs a bridge rebuild; SDK names (`FTOptix.RAEtherNetIP.VariableTypes`, driver/station types) are pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k typed_driver_tag`).
- **NetLogic delete restore is backup-first, byte-verified and guarded after return (`service/core.py`, `service/mcp_app.py`; PRD 0035 #37; ftx-mcp-phv.2).** Before the delete POST the `.cs` bytes are also written to `ProjectFiles/.ftx-mcp-backups/<Class>.<ts>.cs.bak` (removed again once the restore is verified, kept and named in the warning when it is not). The restore waits for the file and the NetSolution build tree to stop changing (and for the bridge to stop reporting busy), writes via same-dir temp + `os.replace`, and re-verifies byte-for-byte after a second stability window; NUL-extended or prefix-only rewrites count as modified. If Studio still rewrites the file after the window the result is `preserved: false` with `netlogic_source_restore_failed` naming the backup. A late guard (`_netlogic_late_guard`, wired into the MCP bridge-call wrapper) re-checks files restored in the last 60 s on the NEXT bridge/NetLogic call for that project: a drifted or deleted file is restored again (the drifted bytes are first saved as `<Class>.<ts>.drifted.cs.bak`) and that call's result carries a `netlogic_source_restored_late` warning (or `netlogic_source_restore_failed` if the restore fails). Tests: `service/tests/test_netlogic_delete_protection.py` (`test_backup_is_written_before_delete_post_and_removed_after_verify`, `test_rewrite_after_return_is_caught_by_next_call_guard`, `test_late_guard_restores_deleted_file_and_attaches_on_bridge_write`, `test_late_guard_ignores_restores_older_than_60s`, `test_late_guard_failure_names_backup`, ...). Not verified against a live Studio - pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k netlogic`).
- **Live tests for NetLogic delete right after create, and live-suite test defects fixed (`service/tests/test_bridge_live.py`; PRD 0035 #37/#23; ftx-mcp-phv.3, ftx-mcp-phv.5).** Test-only. New race variants `test_netlogic_edit_delete_race_after_create_live` and `test_netlogic_delete_node_race_after_create_live` (runtime and design_time; delete via an `optix_bridge_edit` batch and via `delete_node` immediately after `create_netlogic`). Fixes: `_save_retrying_once` and `_with_bridge_recovery` wait for the bridge to come back after Studio recompiles an edited NetLogic (`no_bridge_for_project`) before retrying; the emulator-restart observe tests restore the real `LOCALAPPDATA` (conftest points it at an empty dir, hiding the emulator/bridge registry); the execute-method assertion follows the #5 contract (`export_method_not_loaded` until the project is reopened); leftover nodes are deleted best-effort so a rerun does not hit `name_exists`. Skipped without a live bridge; bench run pending (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py`).
- **`remove_translation` inline and batch paths share one existence check (`studio-bridge/StudioMCPBridge.cs`; PRD 0035 #40; ftx-mcp-phv.4).** `RemoveTranslationInline` did its own `LookupTranslation`, while the batch `validate_ops` case used `TranslationRowExists`, so a key written by `add_translations` could answer `translation_not_found` on the primitive route. Both now call `TranslationRowExists` (raw SDK lookup, or a key `UpsertTranslation` wrote this session): an added/updated key answers `not_supported`, an unknown key `translation_not_found`. `UpsertTranslation` keeps the raw `TranslationLookupHit` for Add-vs-Set. Tests: `test_remove_translation_inline_and_batch_share_one_existence_check`, `test_remove_translation_after_primitive_add_and_batch_add_is_not_supported` (`service/tests/test_bridge_writes.py`); live `test_translations_bulk_and_remove_live` extended with remove after a primitive and a batch add. Needs a bridge rebuild (no dotnet here); live run pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k translations_bulk_and_remove`).
- **CDP health probe is session-aware and Not-attached recovery covers every path (`service/core.py`; PRD 0035 #23; ftx-mcp-phv.6).** `optix_cdp_restart` (`ensure_chrome_cdp`) probes the cached per-project session and, on `Not attached`, re-attaches (`state: recreated_page`) and now reports `restarted: true`. `Page.reload` answering `Not attached to an active page` is recovered once (`cdp_recovered: true`) for observe (incl. `fresh`), interact and dom_text; a probe-blind adopted target is replaced instead of reloaded again (no orphan tab). After an emulator restart `cdp_reresolve_tab(force_fresh=True)` never reuses an unchanged target id (`fresh_attach: true`). Tests: `test_unchanged_target_id_with_detached_session_forces_fresh_attach`, `test_not_attached_recovers_once_on_every_path`, `test_cdp_restart_probes_cached_session_and_reports_restarted` (`service/tests/test_cdp_recovery.py`). Not verified against a live Studio - pending bench verification.
- **`create_netlogic` returns only after Studio's template `.cs` write has finished (`service/core.py`; PRD 0035 #37 root cause; ftx-mcp-phv.1).** `bridge_create_netlogic` (both kinds; the one implementation behind the direct tool and any batch surface) now waits until `ProjectFiles/NetSolution/<Class>.cs` exists, is non-empty and is unchanged (size + mtime + sha1) for `_NETLOGIC_SETTLE_SECONDS`, bounded by `_NETLOGIC_TEMPLATE_MAX_WAIT_SECONDS` (15 s), before the #33 snapshot restore. The result carries `netlogic_template: {file, ready, waited_seconds[, reason]}`; on timeout it also carries a `netlogic_template_write_pending` warning and `ready: false`. Tests: `test_netlogic_delete_protection.py` (`test_create_netlogic_waits_for_late_positioned_template_write`, `..._template_write_pending_on_timeout`, ...). Not verified against a live Studio - pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k netlogic`).
- **`optix_describe_node` on a Variable returns its own `datatype`, `value` and `target_path` (`studio-bridge/StudioMCPBridge.cs`, `service/core.py`; PRD 0034 #39; ftx-mcp-aqs.4).** Describing a Variable (`Model/X` Int32, `Model/<DesignTimeNetLogic>/BehaviourStartPriority`) used to return `properties: []` and nothing about the variable itself. `NodeJson` now adds top-level `datatype` and `value` (plus `target_path` / `target_missing` for a link) when the node is a Variable; `describe_node` wraps `value` in the `<untrusted>` envelope, prints the link target path, and resolves relative links / dangling diagnostics like property rows. Tests: `test_describe_variable_int32_value_wrapped`, `test_describe_variable_link_value_includes_target_path` (`service/tests/test_bridge.py`); live `test_design_time_netlogic_export_method_after_save_live` asserts `BehaviourStartPriority` shows 180 - pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k design_time_netlogic_export`). The C# change needs a bridge rebuild (no dotnet here).
- **`add_translations` updates an existing key in place (`studio-bridge/StudioMCPBridge.cs`; PRD 0034 #38; ftx-mcp-aqs.3).** `optix_bridge_add_translations`, the `add_translations` op of `optix_bridge_edit` and `optix_bridge_add_translation` share `UpsertTranslation`: a `LookupTranslation` hit calls `SetTranslation` (counted in `updated`), a new key calls `AddTranslation` (`added`), and an `AddTranslation` that throws "already contains a translation" (lookup miss) falls back to `SetTranslation` instead of failing the call with `row_failed`. A real row error still stops the list and reports how many rows were already applied. Tests: `test_add_translations_*` in `service/tests/test_bridge_writes.py` (source-level for the C#; no dotnet here); live `test_add_translations_repeat_add_updates_live` is pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k repeat_add_updates`).
- **NetLogic `.cs` snapshot restore after delete is stable and atomic (`service/core.py`; PRD 0034 #37; ftx-mcp-aqs.2).** `_restore_netlogic_source` now waits until the file is unchanged (size + mtime + hash across polls) for a stability window so Studio's delayed write-back is finished, restores the whole snapshot via a temp file in the same directory + `os.replace` (never NUL bytes or mixed content), re-verifies after a second stability window and restores again if Studio rewrote it (bounded by `_NETLOGIC_RESTORE_ATTEMPTS`). If the file still differs, `.ftx-mcp-backups/<Class>.<ts>.cs.bak` is written and a `netlogic_source_restore_failed` warning (renamed from `netlogic_source_lost`) names the backup. Tests: `test_netlogic_delete_protection.py` (late truncate + offset rewrite, persistent mismatch). Live `test_netlogic_edit_after_modify_preserves_source_live` / `test_netlogic_delete_node_after_modify_preserves_source_live` (byte equality) are pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k after_modify`).
- **`delete` of a NetLogic after the user edited its `.cs` keeps the source and no longer reports a false failure (`service/core.py`; PRD 0033 #37/#33; ftx-mcp-7hp.1).** Once the user had edited the class, Studio recompiled on delete and `POST /bridge/node/delete` outlasted the 8 s default; the service re-sent it, the retry answered `node_not_found`, and the call failed (`state: partial` / `write_failed`) although the node was deleted. It also raised before the #33 snapshot restore ran, so the edited `.cs` was lost. The POST now uses a 90 s timeout (`DELETE_POST_TIMEOUT`) and is never blindly re-sent: on a timeout or dropped response the node is looked up via `GET /bridge/nodes`; if gone the delete is `ok` with `verified_after_timeout: true` and a `delete_node_timeout_verified` warning, if still present it is re-sent once (`node_not_found` on that re-send also counts as success). A `node_not_found` from the first response stays an error. The source snapshot is restored even when the delete raises (the error carries `netlogic_source`). Applies to `optix_bridge_delete_node` and the `delete` op of `optix_bridge_edit`. Tests: `test_netlogic_delete_protection.py`; live `test_netlogic_edit_after_modify_preserves_source_live` and `test_netlogic_delete_node_after_modify_preserves_source_live` (runtime and design_time; `service/tests/test_bridge_live.py`) are pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k after_modify`). Docs: `skills/optix-netlogic-and-bridge/SKILL.md`.
- **First observe / click / dom_text after `optix_emulator` restart recovers on every cycle (`service/core.py`; PRD 0034 #23; ftx-mcp-aqs.1).** (a) `restart_emulator` reports `serving` only once a NEW emulator pid (not one of the stopped pids) exists and the port serves (`_wait_new_runtime`; it first waits for the old listener to release the port), otherwise `serving: false` with a hint; once serving, the project's CDP tab is re-resolved from `/json/list` by runtime-URL port (`cdp_reresolve_tab`, result `cdp_target`), dropping the cached tab/ws_url, probing every candidate, closing detached ones and creating a tab at the runtime URL if none is healthy. `run_emulator` re-resolves too. (b) `_cdp_recoverable` re-resolves the project target the same way (then reloads) before the old recreate-page fallback, and also treats target-closed / crashed / session-not-found errors as recoverable; one retry, `cdp_recovered: true`. `cdp_tab_for` no longer keeps a detached cached tab and closes the healthy new one when both show the runtime port. (c) `optix_cdp_restart` (`ensure_chrome_cdp`) also probes the cached per-project session targets and no longer answers `already healthy` while one is detached (`recreated_page`). Tests: `test_cdp_recovery.py` (`test_detach_between_calls_is_recovered_by_reresolving_target`, `test_target_swap_after_restart_attaches_new_target_not_cached_ws`, `test_emulator_restart_reports_serving_only_after_new_runtime_serves`, `test_optix_cdp_restart_not_already_healthy_when_cached_session_detached`, ...). Live `test_observe_immediately_after_emulator_restart_live` now runs 3 restart cycles; pending bench verification on Studio 1.7.4.32 and 1.8.0.555 (`FTX_LIVE_BRIDGE=1 FTX_LIVE_HMI=1 pytest service/tests/test_bridge_live.py -k immediately_after_emulator_restart`). Docs: `skills/optix-verify-loop/SKILL.md`.
- **`optix_observe` / `optix_cdp_restart` recover a detached page whose probe passes (`service/_cdp.py`, `service/core.py`; PRD 0033 #23; ftx-mcp-7hp.2).** On Studio 1.7.4.32 after `optix_emulator(action="restart")` the page target answered `Page.enable` and `Runtime.evaluate` but failed `Page.reload` with `Not attached to an active page`, so `page_probe` passed, `optix_cdp_restart` said `already healthy` and `recreate_page` never ran. `page_probe` now also calls `Page.getNavigationHistory` and `Page.getFrameTree` (side-effect free, need an attached page), and when a real call fails with the not-attached error the runtime recovery passes that exact target as `force_ws_url` to `recreate_page`, which replaces it (new tab first, then closes the detached one) even if the probe cannot tell. Tests: `test_cdp_recovery.py`; live `test_observe_immediately_after_emulator_restart_live` is pending bench verification on Studio 1.7.4.32 and 1.8.0.555 (`FTX_LIVE_BRIDGE=1 FTX_LIVE_HMI=1 pytest service/tests/test_bridge_live.py -k immediately_after_emulator_restart`). Docs: `skills/optix-verify-loop/SKILL.md`.
- **Tier-2 live suite test defects fixed (`service/tests/test_bridge_live.py`; PRD 0033 #31; ftx-mcp-7hp.3).** Test-only. Assertions compare the inner text of the `<untrusted source="bridge">` envelope (`_inner`), no longer expect a `translated` key or a printed type definition (`BehaviourStartPriorityVariableType`, `RAEtherNetIPTag`), and accept the bridge's `0/61 (NodeId)` kind; the rejected-call test accepts the correct `export_method_not_loaded` (with `reopen_required`) as well as a real `rejected`; `test_translations_bulk_and_remove_live` is idempotent (per-run key suffix, since the SDK cannot remove a row; legacy fixed keys cleaned best-effort); `test_health_project_path_is_folder_holding_optix` restores the real `LOCALAPPDATA` the conftest redirects; and the suite stops the CI project's emulator at module teardown (`_stop_emulator_at_teardown`) and before `test_r3_35_*`, so no orphan emulator is left and those tests no longer skip. A live run is pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py`).
- **Live tests for every r4 symptom (`service/tests/test_bridge_live.py`; PRD 0033; ftx-mcp-7hp.4).** NetLogic delete after the `.cs` was modified (edit batch and `delete_node`, runtime and design_time) and observe immediately after an emulator restart. They skip without a live bridge; the bench run is pending (see the entries above).
- **Bridge `studio_version` is the Studio product version, so the `pow` gate fires on Studio 1.7 (`studio-bridge/StudioMCPBridge.cs`; PRD 0032 #36/#14; ftx-mcp-mnk.1).** `StudioVersionString()` read the FTOptix.UI assembly version (`21.2.0.98`), so `VersionAtLeast(studio_version, "1.8")` was always true and `pow` validated on 1.7 but failed at runtime. It now reads the Studio process's main-module version resource (`Process.GetCurrentProcess().MainModule.FileVersionInfo.ProductVersion`, falling back to `FileVersion`, trimmed to its leading numeric part by `CleanProductVersion`), e.g. `1.7.4.32` / `1.8.0.555`; an unreadable version stays `"unknown"` (gate does not refuse). It flows into `/bridge/health`, the schema dump and the expression gate. Tests: `test_bridge_writes.py` (`test_bridge_studio_version_is_the_process_product_version_not_the_ui_assembly`, `test_bridge_clean_product_version_keeps_leading_numeric_version`). Live check on Studio 1.7.4.32 / 1.8.0.555 (`test_validate_expression_pow_live`) is pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k pow_live`); the C# is compiled only by the bench `build_check` (`ci/w11-validate.sh <sha>`), pending.
- **Live tests for the Studio product version and the `pow` gate (`service/tests/test_bridge_live.py`; PRD 0032 #36/#14; ftx-mcp-mnk.2).** `test_health_studio_version_matches_running_studio_major_minor_live` checks `/bridge/health` `studio_version` starts with the running Studio's major.minor; `test_pow_version_gate_follows_running_studio_live` checks `pow` is refused with `function_requires_version` below 1.8 and accepted on 1.8+. They skip without a live bridge; the bench run is pending (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k "studio_version or pow"`). Docs: `skills/optix-expression-converter/SKILL.md` states that `studio_version` is the Studio product version and that `pow` (1.8+) is refused with `function_requires_version` on older Studios (ftx-mcp-mnk.3).
- **`describe_node` resolves relative NodePath links; no false `dangling_link` (`service/core.py`, `studio-bridge/StudioMCPBridge.cs`; PRD 0031 #32/#29/#2; ftx-mcp-xyc.1).** A DynamicLink holding a relative NodePath such as `../../../../../Model/F01Val` was reported `target_path: null, target_missing: true` plus a `dangling_link` diagnostic even when the target existed, so every relative link looked deleted (a fresh, correct Switch bind was flagged and re-bound for nothing). The bridge now anchors relative forms (`../X`, `./X`) at the node that HOLDS the link (the DynamicLink's owner, or the `Source*` variable itself for formatter rows) and walks the segments by hand (`ResolveRelativeFrom`: `..` = Owner, other segments = child by BrowseName). The service re-resolves any remaining `target_missing` relative link lexically (`_resolve_relative_nodepath`, `_resolve_relative_link`) against the project tree. A link whose target really is deleted is still `target_missing: true` with the `dangling_link` diagnostic; formatter `Source*` rows now get a checked `target_path` too. SDK members used are already used in the bridge (`Owner`, `Children`, `BrowseName`); only the bench `build_check` compiles the C#, pending (`ci/w11-validate.sh <sha>`). Tests: `test_bridge.py` (`test_describe_relative_nodepath_link_resolves`, `test_describe_relative_nodepath_link_to_deleted_target_still_dangling`, `test_describe_formatter_source_row_not_falsely_dangling`, `test_resolve_relative_nodepath`); live `test_r3_32_relative_link_not_dangling_unless_target_deleted_live` in `test_bridge_live.py` is pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k r3_32`). Docs: `skills/optix-formatted-dynamic-link/SKILL.md`.
- **Deleting a NetLogic no longer destroys the user's C# source (`service/core.py`; PRD 0031 #33; ftx-mcp-xyc.2).** Studio removes `ProjectFiles/NetSolution/<Class>.cs` (not into the Recycle Bin) when the LAST node bound to the class is deleted, and the next `create_netlogic` of that name made Studio regenerate a default `Method1` template over the user's code. `delete` now snapshots the file first and restores it if Studio removed or rewrote it; the result carries `netlogic_source` (`file`, `class`, `last_instance`, `preserved`, `backup`) and a `netlogic_source_preserved` warning (surfaced in the top-level `warnings` of an `optix_bridge_edit` batch, with `op_index`). `create_netlogic` does the same for an existing `<Class>.cs`, restoring it if Studio overwrote it with the template (`netlogic_source_preserved`). If restoring fails a `netlogic_source_lost` warning names a backup under `ProjectFiles/.ftx-mcp-backups/`. A class still used by other nodes keeps its file and the result is unchanged. The preserved file stays on disk as an unused class; delete it by hand if you want it gone. Tests: `test_netlogic_delete_protection.py` (`test_delete_last_netlogic_instance_preserves_source_and_warns`, `test_delete_netlogic_still_used_elsewhere_keeps_file_no_warning`, `test_delete_plain_node_without_matching_source_unchanged`, `test_create_netlogic_does_not_overwrite_user_source_with_template`, `test_create_netlogic_with_existing_source_untouched_is_quiet`, `test_batch_delete_surfaces_warning`); live `test_r3_33_netlogic_delete_keeps_user_source_live` is pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k r3_33`). Docs: `skills/optix-netlogic-and-bridge/SKILL.md`.
- **`optix_observe` survives an emulator restart (`service/_cdp.py`, `service/core.py`; PRD 0031 #23; ftx-mcp-xyc.3).** After `optix_emulator(action="restart")` the CDP page target stayed detached: `observe(fresh=true)` failed with `Page.reload: Not attached to an active page` and `optix_cdp_restart` answered `already healthy`. `page_probe` now exercises `Page.enable` before `Runtime.evaluate` (what reload/screenshot depend on), so a detached target is unhealthy; new `recreate_page` opens a fresh tab FIRST and then closes the detached targets. `optix_cdp_restart` tries this (`state: "recreated_page"`, `restarted: false`) before restarting Chrome, and the recoverable runtime calls recreate the page instead of reloading a detached one. Tests: `test_cdp_recovery.py` (`test_page_probe_detects_detached_page_on_page_enable`, `test_cdp_restart_detects_detached_page_and_recreates`, `test_observe_fresh_screenshot_survives_detached_page`, `test_observe_fresh_recovers_without_autoheal`); live `test_r3_23_observe_after_emulator_restart_live` is pending bench verification (`FTX_LIVE_BRIDGE=1 FTX_LIVE_HMI=1 pytest service/tests/test_bridge_live.py -k r3_23`). Docs: `skills/optix-verify-loop/SKILL.md`.
- **`create_netlogic` no longer retries the non-idempotent POST on a timeout; it verifies instead (`service/core.py`; PRD 0031 #34; ftx-mcp-xyc.4).** When the class is not compiled yet Studio compiles during `POST /bridge/model/netlogic` (more than 8 s); the service timed out, re-sent the POST, got `name_exists` and reported `write_failed` although the first request had created the node. The POST now uses a 90 s timeout (`NETLOGIC_POST_TIMEOUT`) and is never re-sent after it was sent (`_bridge_http(retry_ambiguous=False)`; a timeout raises `BridgeWriteTimeout`, plain connect failures still retry). On a timeout or dropped response the node is looked up via `GET /bridge/nodes`; if it exists the create is reported `ok` with `verified_after_timeout: true` and a `create_netlogic_timeout_verified` warning, otherwise the original error is raised. A `name_exists` from the single POST is a genuine duplicate and stays an error. Tests: `test_bridge_writes.py` (`test_create_netlogic_timeout_verifies_node_and_succeeds`, `test_create_netlogic_timeout_node_missing_raises`, `test_create_netlogic_genuine_name_exists_still_error`, `test_bridge_http_non_idempotent_post_timeout_not_retried`, `test_bridge_http_default_post_still_retries_timeouts`); live `test_r3_34_create_netlogic_slow_compile_live` is pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k r3_34`). Docs: `skills/optix-netlogic-and-bridge/SKILL.md`.
- **The emulator `web_port_clash` pre-check ignores ports nobody is listening on (`service/core.py`; PRD 0031 #35; ftx-mcp-xyc.5).** Two open Studios both configured for WebPresentationEngine port 8081 with no emulator running made `optix_emulator(action="run")` refuse with `web_port_clash` telling the user to stop a project that was not running. `detect_web_port_clash` now needs the port to be IN USE: another project configured for it has a running emulator (`other_project` names that project), or a live listener holds the port and the project itself is not the one running (`other_project: null`, nudge says to stop whatever listens on it). A merely configured equal port is not a clash. Tests: `test_web_port_discovery.py` (`test_configured_same_port_but_not_running_is_not_a_clash`, `test_live_listener_on_port_is_a_clash_without_naming_idle_project`, `test_clash_names_only_the_running_project`); live `test_r3_35_duplicate_configured_port_no_emulator_not_a_clash_live` and `test_r3_35_run_emulator_not_refused_for_idle_duplicate_port_live` are pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k r3_35`). Docs: `skills/optix-project-lifecycle/SKILL.md`.
- **Live bench tests for every r3 symptom (`service/tests/test_bridge_live.py`; PRD 0031; ftx-mcp-xyc.6).** `test_r3_32_*`, `test_r3_33_*`, `test_r3_23_*`, `test_r3_34_*`, `test_r3_35_*` skip without a live bridge; the bench run is pending (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k r3_`).
- **`optix_execute_method` returns `export_method_not_loaded` when the live node has no `UAMethod` child of that name (`service/core.py`, `service/mcp_app.py`; PRD 0030 #5; ftx-mcp-ib4.5).** With a bridge armed, `execute_design_method` reads the live node's children (`/bridge/nodes`) before the GUI gesture; no `Method` child named `method` -> `{ok: false, error: "export_method_not_loaded", reopen_required: true, nudge}` instead of the misleading `menu_item_not_found`. Finding: no supported refresh/rebuild trigger is exposed by the bridge (optix_save does not load the `[ExportMethod]`; re-adding the node is not a trigger), so tools and docs now say plainly that a project REOPEN may be required; the earlier "no reopen needed" claims for `kind="design_time"` were softened. An undecidable live state (no bridge, bridge error) falls through to the old path. Tests: `test_studio_log.py` (`test_execute_method_export_method_not_loaded`, `test_execute_method_proceeds_when_method_child_present`, `test_execute_method_non_method_child_of_same_name_is_not_loaded`, `test_execute_method_undecidable_live_state_falls_through`). Whether a reopen actually loads a bridge-created method on a live Studio is pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k export_method_after_save`).

- **Live-test hygiene for the design-time NetLogic tests (`service/tests/test_bridge_live.py`; PRD 0030 #31; ftx-mcp-ib4.6).** `_DT_CS` no longer has `using FTOptix.Core.Log;` (Log is in `UAManagedCore`, so the class did not compile), and `test_design_time_netlogic_export_method_after_save_live` / `test_execute_rejected_call_reported_rejected_live` pass the NetLogic NAME (`_DT_CLASS`, `_REJ_CLASS`), not the node path, to `execute_design_method`. Test-only; the live tests skip without a bridge and a live run is pending bench verification (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k "export_method_after_save or rejected_call"`).
- **`target_path` is emitted for DynamicLink NodePath strings (`studio-bridge/StudioMCPBridge.cs`; PRD 0030 #29; ftx-mcp-ib4.3).** `LinkTargetJson` only handled a `NodeId` value, but a DynamicLink value is a NodePath STRING such as `{NodeId:ns=103;g=...}`, so `target_path` / `target_missing` were never emitted on `Source*` rows or DynamicLink children. It now parses the `{NodeId:ns=..;g=|i=|s=..}` form (`TryParseNodeIdString`), else resolves a browse path (absolute `/Objects/<project>/`, project-relative or owner-relative `../X`); `Source*` holders follow their DynamicLink child; attribute refs (`@`) get none; unresolvable gives `target_path: null, target_missing: true`. SDK members used are already used in the bridge (`InformationModel.Get`, `NodeId` constructors, `Refs.GetVariable`); only the bench `build_check` compiles the C#, pending (`ci/w11-validate.sh <sha>`). Tests: `test_attach_sources.py` (`test_nodeid_string_parser_grammar`, `test_bridge_link_target_resolves_string_values_and_falls_back_to_path`). Docs: `skills/optix-formatted-dynamic-link/SKILL.md`.
- **Registry file BOM no longer hides a bridge (`studio-bridge/StudioMCPBridge.cs`, `service/core.py`; PRD 0030 #28; ftx-mcp-ib4.2).** The bridge wrote the registry entry with `Encoding.UTF8` (BOM) and `_registry_open_candidates()` read it as plain `utf-8`, so `json.loads` failed and the entry was silently skipped (empty candidate list). The bridge now writes `new UTF8Encoding(false)` and the service reads `utf-8-sig`, so entries from older bridges still load. Tests: `test_project_path_v2.py` (`test_registry_open_candidates_reads_bom_prefixed_file`, `test_bridge_source_writes_registry_without_bom`). Docs: `skills/optix-netlogic-and-bridge/SKILL.md`.
- **Per-project emulator lifecycle for multiple Studios (`service/core.py`, `service/mcp_app.py`, `service/http_app.py`; PRD 0030 #30; ftx-mcp-ib4.4).** `run`/`restart` no longer refuse with `runtime_project_mismatch` just because another project's emulator is running: each Studio's emulator runs side by side, and the only refusal is a shared web port (`web_port_clash`, `detect_web_port_clash`). `restart` stops only its own project's emulator (cache-path attribution). `status(project=X)` returns X's own pids and probes/returns X's own web port; `stop(project=X)` kills only X's pids; `external_change` detection and recording are scoped per project on the project's status/run/stop/restart, so one project's status never swallows another's change. Without `project`, `status`/`stop` stay host-wide. Tests: `test_run_emulator.py` (`test_run_emulator_proceeds_when_foreign_emulator_runs_on_other_port`, `test_run_emulator_refuses_foreign_emulator_on_same_web_port`, `test_emulator_status_project_reports_its_own_web_port`, `test_status_reports_external_change_on_the_per_project_status`, `test_stop_emulator_with_project_status_leaves_other_project_running`), `test_composites.py`, `test_web_port_discovery.py`; live `test_live_both_emulators_up_on_their_own_web_ports` (`test_multi_studio_live.py`) is pending bench verification (`FTX_LIVE_MULTI_STUDIO=1 FTX_LIVE_PROJECT_A=<a> FTX_LIVE_PROJECT_B=<b> pytest service/tests/test_multi_studio_live.py -k both_emulators`).
- **`create_netlogic` and other instantiating bridge writes run under `Sessions.ImpersonateRootTemporary()` (`studio-bridge/StudioMCPBridge.cs`; PRD 0030 #27; ftx-mcp-ib4.1).** `CreateNetLogicInline` ran `MakeObject` / `parentNode.Add(o)` on the bridge HTTP thread with no Optix session, so `kind="design_time"` for a class already compiled in the NetSolution killed Studio (c0000374), the same fault as `invoke_method`. The create now runs inside `using (parentNode.Context.Sessions.ImpersonateRootTemporary())`. Audited paths (all `MakeObject` + `Add` that instantiate an Object into the live model): **wrapped** - `CreateNetLogicInline` (`/bridge/model/netlogic`), `CreateObjectInline` (`/bridge/model/object`, the type may be a NetLogic), `ConvertToTypeInline` (the `replace` re-instantiation), `MoveNodeInline` (the copy `MakeObject` + `Add`); **already wrapped** - `InvokeMethodInline` (`ExecuteMethod`); **audited, not wrapped** - folder / variable / alias / widget / ObjectType creation, `CopySubtreeInto` children, `WireEvent*` (EventHandler graph), translations, `DeleteNodeInline` (no NetLogic or behaviour instantiation; subtrees containing a NetLogic are refused before any copy). SDK members used are already used in the bridge (`Context`, `Sessions.ImpersonateRootTemporary`, `InformationModel.MakeObject`, `IUAObject`); only the bench `build_check` compiles the C#, pending (`ci/w11-validate.sh <sha>`). Tests: `test_bridge_writes.py` (`test_create_netlogic_add_inside_root_impersonation`, `test_session_affine_instantiation_paths_are_impersonated`); live `test_create_design_time_netlogic_for_compiled_class_live` in `test_bridge_live.py` skips without a bridge (and without `FTX_LIVE_COMPILED_NETLOGIC_CLASS`); a live run is pending bench verification (`FTX_LIVE_BRIDGE=1 FTX_LIVE_COMPILED_NETLOGIC_CLASS=<class> pytest service/tests/test_bridge_live.py -k compiled_class`).
- **Docs: the bench `build_check` is the ONLY compile gate for the bridge C# (`ci/README.md`, `CONTRIBUTING.md`, `skills/optix-netlogic-and-bridge/SKILL.md`; PRD 0029 #26; ftx-mcp-b7v.5).** The executor sandbox has no dotnet, so `ci/w11-validate.sh <sha>` (`build_check`) is the only thing that compiles `studio-bridge/StudioMCPBridge.cs`; every PRD touching `studio-bridge/` must name the SDK members it uses. Docs only.
- **Source-level regression guard for the bridge C# (`service/tests/test_bridge_source_guard.py`; PRD 0029 #26; ftx-mcp-b7v.3).** Reads `studio-bridge/StudioMCPBridge.cs` (no dotnet) and fails on `RemoveTranslation(`, `typeof(ModellingRule)` or a `(ModellingRule)` cast; the message names #26 and points at the SDK facts in `docs/prd/0029-bridge-compile-fix.md`. Plain `node.ModellingRule` access is not flagged. Tests: `test_bridge_source_has_no_forbidden_tokens`, `test_modelling_rule_cast_goes_through_naming_rule_type`, `test_guard_does_not_flag_property_access`.
- **`remove_translation` is `not_supported` (`studio-bridge/StudioMCPBridge.cs`, `service/core.py`, `service/mcp_app.py`; ftx-mcp-b7v.2).** `RemoveTranslationInline` called `InformationModel.RemoveTranslation`, which the Optix SDK does not have, so the bridge did not build. The call is gone: the route still validates `key` and returns `translation_not_found` for an unknown key; for an existing key (`dry_run` included) it returns `{"ok":false,"error":{"code":"not_supported","message":"removing a translation row is not supported by the Optix SDK; edit the key in Studio's Translations editor or overwrite its text with add/set"}}`. The bridge `validate_ops` case, the `optix_bridge_edit` `remove_translation` op (reported in `report.errors` with `op_index`, `dry_run` included, nothing applied) and the `optix_bridge_remove_translation` primitive (`{ok:false, code:"not_supported"}`) surface the same refusal. Earlier entries, `docs/tool-reference.md` and `skills/optix-add-label/SKILL.md` no longer claim removal works. Tests: `service/tests/test_bridge_writes.py`. Compile check on the bench is pending (`ci/w11-validate.sh <sha>`).
- **`set_modelling_rule` compiles again: cast through `NamingRuleType` (`studio-bridge/StudioMCPBridge.cs`; PRD 0029 #26; ftx-mcp-b7v.1).** `SetModellingRuleInline` used `(ModellingRule)Enum.Parse(typeof(ModellingRule), rule)`, but `IUANode.ModellingRule` is a `UAManagedCore.NamingRuleType` and no `ModellingRule` type exists (CS0246), so the bridge did not build. It now uses `(NamingRuleType)Enum.Parse(typeof(NamingRuleType), rule)`; accepted names stay `Mandatory`, `Optional`, `None` and the response still carries `previous` and the new rule. Test: `test_bridge_source_guard.py` (`test_modelling_rule_cast_goes_through_naming_rule_type`). Compile check on the bench is pending (`ci/w11-validate.sh <sha>`). Docs: `skills/optix-templify/SKILL.md`.
- **Runtime CDP calls recover a hung or detached page target (`service/core.py`; PRD 0028 #23; ftx-mcp-un6.3).**
  `cdp_click_runtime`, `cdp_screenshot_runtime` and the other `_cdp_recoverable` runtime calls used to hang or fail
  with "not attached" / "no page" after an emulator restart. They now reload the page target once, retry once, and
  mark a successful retry with `cdp_recovered: true`; a first-try success is untouched and a failing retry surfaces
  its error with no second reload. Tests: `test_cdp_recovery.py` (`test_observe_hang_once_recovers`,
  `test_click_hang_once_recovers_and_retries_once`, `test_retry_failure_surfaces_error_without_further_reload`);
  live `test_observe_recovers_after_emulator_restart_live` and `test_open_after_new_reports_truthfully_live`
  (#25) in `test_bridge_live.py` skip without a live Studio, and a live run is pending bench verification
  (`FTX_LIVE_BRIDGE=1 FTX_LIVE_HMI=1 pytest service/tests/test_bridge_live.py -k "open_after_new or observe_recovers"`).
  Docs: `skills/optix-project-lifecycle/SKILL.md`, `skills/optix-verify-loop/SKILL.md`.
- **`optix_bridge_arm` names an elevated Studio (`service/studio_arm.py`; PRD 0028 #24; ftx-mcp-un6.2).** When no Studio window is visible to the service but a Studio process runs elevated while the service does not, arm returns `studio_elevated_service_not` with the hint "Studio is running as administrator; start it without elevation, or run the service elevated" instead of the misleading `studio_window_not_found`. A PowerShell helper reports token elevation of the service and each Studio process; `studio_window_not_found` is kept for the real case. Elevation probe behaviour on a live bench is pending bench verification. Tests: `test_studio_arm.py` (`test_studio_elevated_service_not_decision`, `test_studio_window_not_found_without_elevation_mismatch`, `test_elevation_powershell_helper_reports_elevation`).
- **`optix_project open` reports a project open only when a Studio really has it loaded (`service/core.py`,
  `service/studio_arm.py`; PRD 0028 #25; ftx-mcp-un6.1).** The Home screen's Recent-projects entry used to read as
  a loaded project, so a refused second launch ("same project is open in another") still answered `opened` /
  `already_open`. Identity now comes from the bridge health project name/dir, else the main-window title (or project
  tree labels), else the Studio log line - and a Home page never counts. A project already open returns that Studio
  (`already_open`, with `pid`) and starts no new launch. A refused second launch, or one that stays on Home, returns
  `state: "not_opened"` with `reason` and `holder_pid`, and the second Studio the service launched is closed (never
  a Studio it did not launch). Studio log wording for "loaded"/"refused" is an assumption pending bench verification
  on the bench. Tests: `test_project_open_truth.py` (`test_home_screen_with_recent_entries_is_not_open`,
  `test_refused_second_launch_reports_not_opened_with_holder_pid`,
  `test_project_already_open_in_window_returns_it_and_launches_nothing`,
  `test_project_already_open_by_bridge_health_launches_nothing`, `test_project_already_open_by_log_line_launches_nothing`).
- **Service version bumped to 1.0.9, single source of truth (`service/__init__.py`, `pyproject.toml`, `server.json`;
  ftx-mcp-ver.4).** `service.__version__` is `1.0.9` and equals the bridge's `BridgeVersion`; `pyproject.toml` now
  derives its version from it (`dynamic`, `attr = "service.__version__"`) and `server.json` (top-level and package
  entry) says 1.0.9. Tests: `test_version_parity.py` (`test_service_version_matches_bridge_version`,
  `test_service_version_is_1_0_9`, `test_pyproject_version_derives_from_package`, `test_server_json_agrees`).
- **`project_path` names the folder holding the `.optix` (`service/core.py`, `studio-bridge/StudioMCPBridge.cs`;
  1.0.9 slate #8c, PRD 0027 #21; ftx-mcp-ver.2).** `/bridge/health` and the registry file used to report whatever
  the SDK handed back, which could be the `ProjectFiles` subfolder (or, earlier, Studio's cwd, shown as unknown).
  The bridge now normalizes it (`NormalizeToProjectFolder`): a `.optix` file path or a folder that has its own
  `.optix` is unchanged; `ProjectFiles` or a folder with no `.optix` resolves to its parent when that holds exactly
  one `.optix`. The service accepts the legacy `ProjectFiles` shape from an older bridge the same way
  (`_project_dir_from_path`), and rejects it when the parent is ambiguous (several `.optix`). Tests:
  `test_registry_open_candidates_accepts_project_folder_path`,
  `test_registry_open_candidates_accepts_legacy_projectfiles_path`,
  `test_projectfiles_path_with_ambiguous_parent_is_rejected`, `test_bridge_source_normalizes_projectfiles_to_parent`
  in `test_project_path_v2.py`; live check in `test_bridge_live.py` pending bench verification
  (`FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k project_path`). Docs:
  `skills/optix-netlogic-and-bridge/SKILL.md`.
- **Live suite runs in the interactive session; GUI live tests skip in session 0 (`ci/validate-w11.ps1`,
  `service/tests/_live_session.py`; PRD 0027 #22; ftx-mcp-ver.3).** Gate 7 now launches the live pytest through a
  scheduled task with an Interactive/Highest principal, output to `live_<sha>.log`, bounded by `-LiveTimeoutSec`
  (default 1800). GUI-touching live tests skip with `session_0_no_desktop` when there is no input desktop instead of
  failing with `bridge_studio_no_window`. Documented in `ci/README.md`. Tests: `test_live_session.py`. Live run
  pending bench verification: `ci/w11-validate.sh <sha>` on the bench and check `live_<sha>.log` has no `session_0_no_desktop` skips.
- **Batch subtree refusals are per-op `refused_ops`, not a whole-batch validation failure (`service/core.py`;
  1.0.9 slate #6/8a/8c, PRD 0027 #20; ftx-mcp-ver.1).** The C# validate stage reports `contains_netlogic` /
  `contains_event_handler` / `refused_netlogic_move` as `report.errors`, which refused the whole batch
  (`applied: 0`, `state: validated`, no `refused_ops`). `bridge_edit` now lifts those errors out of the report
  (`_split_subtree_refusals`): each op is recorded in `refused_ops` as {index, op, path, code, error, hint}
  (the same hint the direct routes return), the other ops validate and apply, `applied` counts only what
  landed (journal and progress events too), and the nudge names the refused indices. Any other validation
  error (`unresolved_reference`, bad args, ...) still refuses the whole batch with `applied: 0`. `dry_run`
  reports the refusals without failing the batch. `convert_to_type` is now also a batch verb (service-side
  dispatch; the bridge validator does not pre-check it, so its refusal arrives at apply time and is recorded
  the same way). Tests: `test_bridge_subtree_refusals.py` (`test_mixed_batch_rename_lowered_to_move_refused_at_validation`,
  `test_mixed_batch_move_refused_at_validation`, `test_mixed_batch_convert_to_type_refused`,
  `test_create_rename_set_property_applies_two_refuses_one`, `test_other_validation_errors_still_refuse_whole_batch`);
  live `test_subtree_refusals_live_and_bridge_survives` asserts `len(refused) == 1` through the batch (live run
  pending bench verification: `FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k subtree_refusals`).
- **`services.ps1 enable-autostart` starts only the ftx-mcp service (`bootstrap/services.ps1`; 1.0.9 slate #13/9d,
  PRD 0026; ftx-mcp-xk7.6).** The verb looped over every registered task, so it also put an at-logon trigger on
  `ftx-mcp-chrome-cdp`. It now adds the trigger to the `ftx-mcp` task only and reports the CDP task as skipped;
  Studio and the emulator were never autostarted and still are not. `disable-autostart` still clears triggers on
  all tasks, so a CDP trigger left by an older version can be removed. Tests (Pester-free, parse the script's
  autostart branch): `service/tests/test_services_autostart.py` (`test_autostart_guards_to_ftx_mcp_task_only`,
  `test_autostart_branch_does_not_start_studio_emulator_or_cdp`, ...). The Windows task behaviour itself is
  pending bench verification: run `.\bootstrap\services.ps1 enable-autostart` then
  `(Get-ScheduledTask ftx-mcp-chrome-cdp).Triggers` (expect none). Docs: `skills/optix-netlogic-and-bridge/SKILL.md`.
- **Emulator web-port default follows the project, with a warned 8081 fallback (`service/core.py`;
  1.0.9 slate #4e, PRD 0026; ftx-mcp-xk7.5).** The port read from the project (live bridge
  `UI/WebPresentationEngine`, else `Nodes/UI/UI.yaml`) is used when present; no 8088 default exists. When
  no project port is found, `resolve_web_port` still falls back to 8081 (Optix's shipped default) but the
  result (`source` `default` or `no_engine`) now carries `searched` and a `warning` naming the locations
  looked in (bridge node, UI.yaml path, `OPTIX_RUNTIME_TEST_PORT`), and the warning is logged. Tests:
  `TestWebPortFallbackWarning` in `test_web_port_resolver.py`; live
  `test_resolve_web_port_against_live_bridge` (skipped unless `FTX_LIVE_BRIDGE=1` and `FTX_LIVE_PROJECT`;
  not run here). Docs: `skills/optix-verify-loop/SKILL.md`.
- **`optix_execute_method` derives its chain from the live bridge when armed (`service/core.py`,
  `service/studio_arm.py`; 1.0.9 slate #4, PRD 0026; ftx-mcp-xk7.4).** The right-click chain used to come
  only from the saved NetLogic YAML, which is stale or absent for a node created over the bridge. New
  `core.live_netlogic_chain()` asks the serving bridge (`/bridge/map?match=<node>`) for the node's path - the
  Project-view rows - and passes it as `studio_arm.execute_method(live_chain=...)`; results carry
  `chain_source: "bridge"`. With no bridge armed, or no single matching live node, it falls back to the saved
  YAML (`chain_source: "yaml"`) exactly as before. Design-time NetLogic creation stays out of scope (PRD 0025).
  Tests: `test_execute_method_chain_from_live_bridge_when_armed`,
  `test_execute_method_chain_falls_back_to_yaml_when_no_bridge`, `test_live_netlogic_chain_*`
  in `test_studio_log.py`; `test_execute_method_uses_live_chain_without_yaml`,
  `test_execute_method_without_live_chain_uses_yaml` in `test_studio_arm.py`; live
  `test_live_netlogic_chain_against_live_bridge` (skipped unless `FTX_LIVE_BRIDGE=1` and `FTX_LIVE_PROJECT`;
  not run here). Docs: `skills/optix-project-lifecycle/SKILL.md`.
- **`dry_run` validates `create_variable` datatypes against the bridge's list (`service/core.py`;
  1.0.9 slate #4, PRD 0026 #4c; ftx-mcp-xk7.3).** The bridge's `ResolveDataType` refuses an unsupported
  datatype at apply, but `dry_run` / `validate_create_variable_op` accepted anything. New
  `core.CREATE_VARIABLE_DATATYPES` (the bridge's 18 names, exact case as the bridge matches) plus
  `NodePointer`; anything else returns `invalid_datatype` (`field: datatype`, with an `allowed` list) per op,
  strict or not. The C# cannot import Python, so a drift test parses `ResolveDataType` in
  `StudioMCPBridge.cs` and fails if the lists differ. Tests: `test_create_variable_datatypes_match_bridge_source`,
  `test_create_variable_datatypes_match_bridge_error_message`,
  `test_validate_create_variable_accepts_every_bridge_datatype`,
  `test_validate_create_variable_rejects_unsupported_datatype`,
  `test_dry_run_rejects_create_variable_unsupported_datatype` in `test_bridge_writes.py`; live
  `test_dry_run_create_variable_datatype_matches_bridge_live` (skipped unless `FTX_LIVE_BRIDGE=1`; not run
  here). Docs: `skills/optix-project-lifecycle/SKILL.md`.
- **`optix_cdp_restart` health probe now exercises a page-level call (`service/_cdp.py`,
  `service/core.py`; 1.0.9 slate #4, PRD 0026; ftx-mcp-xk7.2).** `ensure_chrome_cdp` used to
  report "already healthy" whenever the DevTools endpoint answered and a page target was listed,
  even while every page command returned `Not attached to an active page`. New
  `_cdp.page_probe()` runs `Runtime.evaluate` of `1+1` on the page target; a failure (notably
  "Not attached to an active page") is unhealthy and falls through to the restart tier
  (`allow_restart=False` returns `failed` naming the cause). A page that answers still reports
  `already healthy`. Tests: `test_page_probe_sends_runtime_evaluate_1_plus_1`,
  `test_page_probe_not_attached_is_unhealthy`, `test_ensure_chrome_cdp_not_attached_triggers_restart`,
  `test_ensure_chrome_cdp_not_attached_no_restart_when_disabled`, `test_ensure_chrome_cdp_already_healthy`
  in `test_cdp.py`; live `test_page_probe_live` (skipped unless `FTX_LIVE_CDP=1`; not run here).
  Docs: `skills/optix-verify-loop/SKILL.md`, `docs/troubleshooting.md`.
- **`optix_bridge_edit` `action="continue"` honours the original `chunk_ops` / `chunk_seconds`
  (`service/core.py`, `service/mcp_app.py`; 1.0.9 slate #4a, PRD 0026; ftx-mcp-xk7.1).**
  Previously the per-call overrides lived only in a temporary env-var swap, so a batch started
  with `chunk_ops=20` applied all remaining ops on continue. `bridge_edit` now takes
  `chunk_seconds`/`chunk_ops` directly and writes the per-call overrides (null when not given) into every batch
  journal record; `bridge_edit_continue` reads them back (an override passed to continue wins and is
  re-persisted; pre-1.0.9 journals fall back to the env/default thresholds). The env-var swap in
  `mcp_app.py` is removed. Tests: `test_chunk_overrides_persisted_in_journal`,
  `test_continue_honours_persisted_chunk_overrides`,
  `test_continue_explicit_override_wins_and_is_persisted` in `test_batch_chunking.py`; live
  `test_batch_continue_honours_chunk_ops_override_live` (skipped without a bridge; not run here).
  Docs: `skills/optix-blind-authoring/SKILL.md`, `docs/tool-reference.md`.
- **Guarded typed `string[]` write for GridLayout `Rows`/`Columns` (`service/core.py`,
  `studio-bridge/StudioMCPBridge.cs`; 1.0.9 slate 3, PRD 0026 #3; ftx-mcp-xk7.7).**
  `set_property name=Columns value=["1*","1*"]` (also `Rows`) on a GridLayout takes a
  separate typed `string[]` path (`typed=string[]`; node-model `UAValue(string[])`, never
  the scalar path). **Behind a flag, default OFF:** `FTX_GRID_STRING_ARRAY=1`
  (`Config.enable_grid_string_array`). Eligible only for names exactly `Rows`/`Columns`,
  non-empty all-string arrays, GridLayout nodes (bridge refuses others with
  `typed_array_not_supported`). The generic array guard is NOT loosened: every other array
  property/node/value, and everything with the flag off, still gets `unsupported_array_write`
  (flag-off error now names the flag). `bridge_validate_ops` marks eligible ops so the C#
  validator waives its array error for exactly that case. `gridlayout_without_columns` is
  still emitted for grids lacking Rows/Columns; with the flag on, a batch that sets both
  on the grid it creates no longer warns. **Bench finding pending:** this has not been run
  against a live Studio (no bridge here); if the live test kills Studio, keep the flag off
  and record the WER stack here and in `docs/troubleshooting.md`. Run: `FTX_LIVE_BRIDGE=1
  FTX_LIVE_PROJECT=<CI_ project> FTX_GRID_STRING_ARRAY=1 FTX_LIVE_HMI=1
  /opt/venv/bin/python -m pytest service/tests/test_bridge_live.py -k gridlayout_rows_columns -q`.
  Tests: `test_grid_string_array_*`, `test_grid_validate_ops_annotates_only_eligible_ops`,
  `test_gridlayout_warning_retired_only_when_both_set_and_flag_on`,
  `test_bridge_source_typed_path_is_isolated_from_generic_guard` in `test_bridge_writes.py`;
  live `test_gridlayout_rows_columns_typed_write_live` (skipped without a bridge). Docs:
  `skills/optix-custom-keyboard/SKILL.md`.
- **`optix_observe(mode="dom_text")` reads exact WebUI strings via CDP (`service/core.py`,
  `service/_cdp.py`, `service/mcp_app.py`; 1.0.9 slate 5g, PRD 0025; ftx-mcp-090.7).**
  The WebUI renders to the DOM, so the new mode returns `document.body.innerText`
  (CDP `Runtime.evaluate`) as `{state, mode, text, length, navigated, captured_at}`;
  `text` is `<untrusted>`-delimited like the OCR modes. It honors `navigate_url`,
  `settle_seconds`, `project`. New `core.cdp_dom_text_runtime`,
  `core.validate_dom_text_args`, `CDPClient.body_inner_text`. Structured errors, never
  raised: `invalid_dom_text_args` (+ `errors[{code, field, message}]`: `invalid_navigate_url`,
  `invalid_settle_seconds`; no CDP session opened), `cdp_unavailable` (+ `hint`),
  `dom_text_failed` (evaluation threw / no `document.body`). `bad_mode`'s `valid_modes` now
  includes `dom_text`. Docs: `skills/optix-verify-loop/SKILL.md`, `docs/tool-reference.md`.
  Tests: `test_dom_text_*`, `test_validate_dom_text_args_accepts_valid` in `test_cdp.py`,
  `test_observe_dom_text_forwards_to_core` in `test_cdp_consolidated.py`; live
  `test_observe_dom_text_returns_known_label_live` in `test_bridge_live.py` (pending bench
  verification, not run here: `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<running project>
  FTX_LIVE_DOM_TEXT_LABEL=<label shown by the UI> /opt/venv/bin/python -m pytest
  service/tests/test_bridge_live.py -k dom_text -q`).
- **Skill notes: `GetVariable` null, RAEtherNet polling, `wire_event` vs rebuild
  generators (1.0.9 slate 5f, PRD 0025; ftx-mcp-090.6).** Docs only, in
  `skills/optix-netlogic-and-bridge/SKILL.md` (and a pointer in
  `skills/optix-wire-command-button/SKILL.md`): `GetVariable(name)` is null for an
  uninstantiated optional property (use `VisibleVariable`/`TextVariable`/...); an
  RAEtherNet tag is polled only while a widget binds it (NetLogic-only tags need a
  `RemoteVariableSynchronizer`); `wire_event` output does not survive a wipe-and-rebuild
  generator (author EventHandlers in C#).
- **Batch `dry_run` resolves `bind` `source_path` (`service/core.py`; 1.0.9 slate 5d,
  PRD 0025; ftx-mcp-090.4).** `optix_bridge_edit(dry_run=True)` now resolves each `bind`
  op's `source_path` against the live model (`GET /bridge/nodes`) and against nodes created
  earlier in the same batch (new `resolve_bind_sources`), so a `create_widget` + `bind`
  batch with a bad source is caught at dry_run instead of ending `partial` and stranding the
  widget. Structured per-op errors `{op_index, code, field: "source_path", source_path,
  message}`: `source_not_found` (also a source created only after the bind, or deleted
  earlier in the batch) and `source_not_variable`; a failed live lookup yields a
  `source_unverified` warning, never a false claim. `raw_path` binds are never resolved;
  non-dry-run batches and single-bind dry_run are unchanged. Python-side only (no C#
  change). Docs: `skills/optix-project-lifecycle/SKILL.md`. Tests:
  `test_bridge_edit_dry_run_rejects_unresolvable_bind_source`,
  `test_bridge_edit_dry_run_resolves_in_batch_bind_source` in `test_bridge_writes.py`; live
  `test_dry_run_catches_bad_bind_source_live` in `test_bridge_live.py` (pending bench
  verification, not run here: `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<throwaway>
  /opt/venv/bin/python -m pytest service/tests/test_bridge_live.py -k
  bad_bind_source -q`).
- **`bind` targets an array element via `raw_path` `[i]` (`service/core.py`,
  `service/mcp_app.py`, `service/http_app.py`; 1.0.9 slate 5c, PRD 0025; ftx-mcp-090.3).**
  The bridge's literal-NodePath escape (`raw_path`) now accepts one `[i]` index on a
  segment, so `optix_bridge_bind_property(raw_path="Model/Tags[1]")` / the batch
  `bind` op binds to element 1 of an array variable; the path reaches the bridge
  verbatim (no C# change). New validator `validate_raw_path` / `validate_bind_op`
  returns structured `{ok: False, code, errors: [{code, message, field}]}` with nothing
  written: `invalid_raw_path` (blank, whitespace, empty segment, unbalanced `[ ]` /
  `{ }`, index with no name or after `..`) and `invalid_raw_path_index` (empty,
  non-numeric, negative, > 2**31-1, more than one `[...]`). `optix_bridge_bind_property`
  and `POST /projects/{project}/bridge/bind` gain `dry_run` (validates, returns the plan,
  no bridge call; a raw_path is never resolved by design, and single-bind dry_run does
  not resolve `source_path`); `optix_bridge_edit` dry_run applies the same raw_path
  check to `bind` ops. Docs: `skills/optix-project-lifecycle/SKILL.md`. Tests:
  `test_bind_raw_path_*`, `test_validate_raw_path_*`,
  `test_bridge_edit_dry_run_rejects_bad_bind_raw_path` in `test_bridge_writes.py`; live
  `test_bind_raw_path_array_element_live` in `test_bridge_live.py` (pending bench
  verification, not run here: `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<throwaway>
  /opt/venv/bin/python -m pytest service/tests/test_bridge_live.py -k
  bind_raw_path_array -q`).
- **`optix_execute_method` reports `rejected`; new `optix_studio_log` tool
  (`service/core.py`, `service/mcp_app.py`, `service/auth.py`; 1.0.9 slate 5e, PRD 0025;
  ftx-mcp-090.5).** Studio reports a refused Execute only in `FTOptixStudio.0.log`, so
  seven identical no-ops used to read as `state: "executed"`. After the click,
  `execute_design_method` now reads the lines appended to that log and, when one looks
  like a rejection (reject / refused / denied / not allowed / `Condition '...' failed`;
  one naming the method or node wins), returns `{ok: False, state: "rejected",
  log_line, nudge}`. A clean log keeps `state: "executed"` plus
  `studio_log_checked: true` (`false` when the log is unreadable). The new read-scope
  tool `optix_studio_log(lines=100, contains=None)` returns the last N lines (1..5000)
  of `FTOptixStudio.0.log` (`%LOCALAPPDATA%\Rockwell Automation\FactoryTalk Optix\
  FTOptixStudio\Log`, override dir with `OPTIX_STUDIO_LOG_DIR`) with an optional
  case-insensitive filter, `<untrusted>`-wrapped. Structured errors: `invalid_lines`,
  `invalid_contains`, `no_studio_log`, `studio_log_unreadable`. Default tool count
  35 -> 36 (README, `docs/tool-reference.md`). Tests: `service/tests/test_studio_log.py`
  (rejected / executed / log-missing / rotation / studio_log paths); live test
  `test_execute_rejected_call_reported_rejected_live` in `test_bridge_live.py`.
  Pending bench verification: the exact wording Studio logs for a rejection was not
  captured, so the match is a keyword net, and the live test was not run:
  `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<throwaway> /opt/venv/bin/python -m pytest
  service/tests/test_bridge_live.py -k rejected_call -q`.
- **`create_variable` takes `variable_type`, `array_dimensions` and `properties`
  (`service/core.py`, `service/mcp_app.py`, `studio-bridge/StudioMCPBridge.cs`; 1.0.9
  slate 5b, PRD 0025; ftx-mcp-090.2).** `optix_bridge_create_variable` and the batch
  `create_variable` op can now create driver station tags and arrays:
  `variable_type` is any VariableType by name (e.g. `RAEtherNetIPTag`; default
  `BaseDataVariableType`), `array_dimensions` is one positive integer (`4`, `"4"` or
  `[4]`; one-dimensional only, like `retype`) and `properties` is a `{name: scalar}` map
  of child properties the type declares (e.g. `{"SymbolName": "MyTags"}`). Errors are
  structured `{ok: False, code, errors: [{code, message, field}]}` with nothing written:
  `invalid_variable_type`, `unknown_variable_type` (a malformed name, or a type the live
  type tree does not have), `invalid_array_dimensions`, `invalid_properties`,
  `invalid_property_name` (incl. reserved `Value`/`DataType`/`BrowseName`/...),
  `invalid_property_value`, `unknown_property` (the type does not declare it) and
  `variable_type_not_applicable` / `array_dimensions_not_applicable` on a NodePointer.
  The batch validator checks the new fields, so `optix_bridge_edit(dry_run=True)` catches
  them, and the single tool takes `dry_run=True` (the bridge resolves the type and
  property names, creates nothing). Existing calls are unchanged. Tests:
  `test_create_variable_*` / `test_validate_create_variable_*` in
  `service/tests/test_bridge_writes.py`; live test `test_create_variable_station_tag_array_live`
  in `test_bridge_live.py` (skipped without a live bridge, and skips if the throwaway
  project has no RAEtherNetIP module). Pending bench verification: the C# was not
  compiled (it walks the type tree for the VariableType and its `SuperType` chain for
  declared properties) and the live test was not run:
  `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<throwaway with RAEtherNetIP> /opt/venv/bin/python
  -m pytest service/tests/test_bridge_live.py -k station_tag_array -q`.
- **`optix_bridge_create_netlogic` takes `kind="design_time"` (`service/core.py`,
  `service/mcp_app.py`, `studio-bridge/StudioMCPBridge.cs`; 1.0.9 slate 5a, PRD 0025).**
  `kind="design_time"` also mints the `BehaviourStartPriority` child with VariableType
  `BehaviourStartPriorityVariableType` (Value 180) instead of the `BaseDataVariableType`
  `create_variable` always produces, so the class's `[ExportMethod]`s materialize after
  `optix_save` with no Studio reopen and `optix_execute_method` can run them. Default
  (`runtime`) is unchanged. An invalid `kind` returns `{ok: False, code: "invalid_kind",
  errors: [{code, message, field: "kind", allowed}]}` with nothing written (a missing
  `parent`/`name` is `missing_field`); `dry_run=True` validates and reports the plan
  (path, kind, the priority child) without touching the bridge. A bridge too old to know
  `kind` is detected (response lacks `kind: design_time`) and raised as
  `BridgeWriteFailed` rather than reported as success. Tests: unit, fake-bridge and
  source-level C# checks in `service/tests/test_bridge_writes.py`
  (`test_create_netlogic_design_time_*`, `test_create_netlogic_invalid_kind_*`); live test
  `test_design_time_netlogic_export_method_after_save_live` in `test_bridge_live.py`
  (skipped without a live bridge). Pending bench verification: the C# was not compiled
  (it resolves the VariableType by walking the type tree) and the live test was not run:
  `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<throwaway> /opt/venv/bin/python -m pytest
  service/tests/test_bridge_live.py -k design_time_netlogic -q`.
- **Skills docs cover the 1.0.9 #9e bridge editing tools (`skills/optix-add-label`,
  `skills/optix-templify`; ftx-mcp-8fy.8).** `optix-add-label` gains a translated-text
  section (`add_translations`, `set_text_key`, `retype` to
  `LocalizedText`); `optix-templify` gains the NodePointer `Kind` rule for
  `create_variable`. `set_modelling_rule`, `pow` and multi-step `wire_event` were already
  documented in `optix-templify`, `optix-expression-converter` and
  `optix-wire-command-button`. Full suite re-run: 1939 passed, 63 skipped (live tests skip
  without a live bridge); no code changes.
- **Expression evaluator / validation accept `pow` on Studio 1.8
  (`studio-bridge/StudioMCPBridge.cs`, `service/core.py`, `service/mcp_app.py`;
  1.0.9 #9e, PRD 0024).** `ValidateExpressionSyntax` (shared by `validate_expression`,
  `attach_expression` and the `CheckFormula` method) treats `pow` as a version-gated
  function: valid on Studio 1.8+, and on 1.7 it returns
  `function_requires_version: 'pow' requires Optix Studio 1.8 or later (this Studio is
  <ver>)` plus a structured `requires_version: {function, min_version, studio_version}`
  field (attach_expression refuses with code `bad_expression`, model untouched).
  `/bridge/health` now reports `studio_version`. Tests: unit, fake-bridge and source-level
  checks in `service/tests/test_bridge_writes.py`; live tests
  `test_validate_expression_pow_live` and `test_attach_expression_pow_live` in
  `test_bridge_live.py`. Pending bench verification: the C# was not compiled and the live
  tests were not run (they branch on the Studio version, so run them on both 1.7 and 1.8):
  `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<throwaway> /opt/venv/bin/python -m pytest
  service/tests/test_bridge_live.py -k pow -q`.
- **`create_variable` makes a NodePointer with the correct `Kind`
  (`service/core.py`, `service/mcp_app.py`, `studio-bridge/StudioMCPBridge.cs`;
  1.0.9 #9e, PRD 0024).** `create_variable` accepts `datatype: NodePointer` with
  `target` (the node to point at) and/or `kind`; the pointer's `Kind` is the pointed-to
  node's type, never unset or BaseObjectType. A NodePointer with neither is refused
  (`kind_required`). `optix_bridge_edit` `dry_run` refuses the same cases and reports the
  resolved Kind under `report.resolved` (also for a target created earlier in the batch).
  Tests: unit, fake-bridge and source-level C# checks in
  `service/tests/test_bridge_writes.py`; live test `test_create_variable_nodepointer_kind_live`
  in `test_bridge_live.py`. Pending bench verification: the C# was not compiled and the
  live test was not run; run `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<throwaway>
  /opt/venv/bin/python -m pytest service/tests/test_bridge_live.py -k nodepointer_kind -q`
  on the Studio VM.
- **Retype a variable to `LocalizedText`
  (`service/core.py`, `service/mcp_app.py`, `studio-bridge/StudioMCPBridge.cs`;
  1.0.9 #9e, PRD 0024).** The existing `retype` op / `POST /bridge/node/retype` now
  accepts `datatype: LocalizedText`: a String / number / Boolean / DateTime /
  LocalizedText value is preserved as text (an existing LocalizedText keeps its
  TextId); a value with no text form, or an array that would lose elements when
  collapsed to a scalar, is refused before any change with a structured
  `value_not_convertible` (`errors: [{code, message, field}]`). New gated tool
  `optix_bridge_retype` (primitive, `FTXMCP_BRIDGE_PRIMITIVES`) with `dry_run`; the
  bridge route takes `dry_run=true`. `retype` ops are validated in `optix_bridge_edit`
  `dry_run` (`invalid_datatype`, `invalid_dims`, and the bridge's `value_not_convertible`
  against the live value). Tests: unit, fake-bridge and source-level C# checks in
  `service/tests/test_bridge_writes.py`; live test `test_retype_to_localized_text_live`
  in `test_bridge_live.py`. Pending bench verification: the C# was not compiled and the
  live test was not run; run `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<throwaway>
  /opt/venv/bin/python -m pytest service/tests/test_bridge_live.py -k retype_to_localized -q`
  on the Studio VM.

- **ModellingRule on type members: `set_modelling_rule`
  (`service/core.py`, `service/mcp_app.py`, `studio-bridge/StudioMCPBridge.cs`;
  1.0.9 #9e, PRD 0024).** Sets the ModellingRule of a member of an
  ObjectType/VariableType to `Mandatory`, `Optional` or `None` (case-insensitive
  input, canonical on the wire). New bridge route
  `POST /bridge/node/modelling-rule?path=..&rule=..[&dry_run=true]`, service tool
  `optix_bridge_set_modelling_rule` (primitive, gated by `FTXMCP_BRIDGE_PRIMITIVES`)
  and batch verb `set_modelling_rule {path, rule}`. Any other rule is refused
  before the bridge is touched with a structured `invalid_modelling_rule` error
  (`errors: [{code, message, field, allowed}]`), including under `dry_run` of
  `optix_bridge_edit`; a node that is not under a type is `not_a_type_member`.
  Tests: unit, fake-bridge and source-level C# checks in
  `service/tests/test_bridge_writes.py`; live test `test_set_modelling_rule_live`
  in `test_bridge_live.py`. Pending bench verification: the C# was not compiled
  (the `IUANode.ModellingRule` setter is unconfirmed against the Optix API) and the
  live test was not run; run `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<throwaway>
  /opt/venv/bin/python -m pytest service/tests/test_bridge_live.py -k modelling_rule -q`
  on the Studio bench.
- **Translation rows in bulk: `add_translations` (and `remove_translation`, later found unsupported -- see the `not_supported` entry above)
  (`service/core.py`, `service/mcp_app.py`, `studio-bridge/StudioMCPBridge.cs`;
  1.0.9 #9e, PRD 0024).** `add_translations` takes a list of `{key, value[, locale]}`
  rows and adds them in one call (`POST /bridge/i18n/translations`, JSON body);
  `remove_translation` (`POST /bridge/i18n/translation/remove`) was added alongside it but
  cannot remove anything (see above).
  Service tools `optix_bridge_add_translations` / `optix_bridge_remove_translation`
  (primitives, gated by `FTXMCP_BRIDGE_PRIMITIVES`) and batch verbs of the same
  names. `dry_run` (tool and `optix_bridge_edit`) validates both without touching
  the translation table, with per-row structured errors. Tests: unit, fake-bridge
  and source-level C# checks in `service/tests/test_bridge_writes.py`; live test
  `test_translations_bulk_and_remove_live` in `test_bridge_live.py`. Pending bench
  verification: the C# was not compiled (`InformationModel.RemoveTranslation` in
  particular is unconfirmed against the Optix API) and the live test was not run;
  run `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<throwaway> /opt/venv/bin/python -m pytest
  service/tests/test_bridge_live.py -k translations_bulk -q` on the Studio bench.
- **Text properties can be set to a translation key: `set_text_key`
  (`service/core.py`, `service/mcp_app.py`, `studio-bridge/StudioMCPBridge.cs`;
  1.0.9 #9e, PRD 0024).** New bridge route `POST /bridge/node/text-key`
  (`path`, `name`, `key`, `locale`), service tool `optix_bridge_set_text_key`
  (primitive, gated by `FTXMCP_BRIDGE_PRIMITIVES`) and batch verb `set_text_key`.
  The property gets a LocalizedText carrying a TextId (the key) — not a literal
  string; `translated` in the result says whether the key has a table row yet.
  `dry_run` returns structured per-op errors (`missing_field`, `invalid_text_key`,
  `invalid_locale`, `text_key_missing`, `not_localized_text`, ...). The
  `_reject_node_attribute` guard is unchanged and applies first: BrowseName /
  DisplayName / Description / NodeId / NodeClass are never written as attributes.
  Tests: unit + source-level C# checks in `service/tests/test_bridge_writes.py`;
  live test `test_set_text_key_live` in `test_bridge_live.py`. Pending bench
  verification: the C# was not compiled or run in this change; run
  `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<throwaway> /opt/venv/bin/python -m pytest
  service/tests/test_bridge_live.py -k set_text_key -q` on the Studio bench.
- **Multi-step event handlers: `wire_event` takes a list of calls and can append
  (`service/core.py`, `service/mcp_app.py`, `studio-bridge/StudioMCPBridge.cs`;
  1.0.9 #9e, PRD 0024).** `calls=[{method_path|command[+variable/value], args,
  object_raw}, ...]` creates the calls IN ORDER as MethodContainer1..N under ONE
  EventHandler's MethodsToCall (`WireEventStepsInline`; the list rides as a JSON body
  on `POST /bridge/node/event`). `append=true` adds the call(s) after the ones an
  existing handler already has and leaves those untouched; without it the first call
  updates the existing handler exactly as a single `wire_event` does. Every call is
  resolved before anything is written, a failing append is undone, and the result
  carries `steps`, `appended`, `created`, `call_count`, `containers`. `dry_run`
  (`bridge_edit`) validates the list form with structured per-call errors
  (`call_index` + `calls_empty` / `call_missing_action` / `call_ambiguous` /
  `call_unknown_command` / `call_missing_variable` / `call_missing_value` /
  `call_bad_args` / `calls_conflict` / `append_with_replace` / `unresolved_reference`,
  ...); `bridge_wire_event` returns the same as `{ok:false, code:"invalid_wire_calls"}`
  before anything reaches the bridge. The `_reject_node_attribute` guard is unchanged
  (BrowseName/DisplayName are still never written as attributes). Unit tests
  (validator + fake bridge, source-level C# checks) in `service/tests/test_bridge_writes.py`;
  live test `test_wire_event_multistep_and_append_live` in `test_bridge_live.py`.
  Pending bench verification: the C# was not compiled or run in this change; run
  `FTX_LIVE_BRIDGE=1 FTX_LIVE_PROJECT=<throwaway> /opt/venv/bin/python -m pytest
  service/tests/test_bridge_live.py -k wire_event_multistep -q` on the Studio bench
  (1.7.4.32 and 1.8.0.555).
- **Project-relative formatted-link `format` paths are refused
  (`service/core.py`, `service/mcp_app.py`; 1.0.9 #1, PRD 0023).** `attach_formatter`
  with `format: "Model/Demo/{#pick}"` (static prefix starting with a project-top
  segment) now fails `format_path_not_runtime_resolvable` in the `bridge_edit`
  pre-check and in `bridge_attach_formatter`, strict or not; the message carries the
  exact rewrite `/Objects/<Project>/Model/Demo/{#pick}`. `/Objects/`, `../`, `./` and a
  leading `{#` pass. `fix_format_paths=true` (tool arg / op field) applies the rewrite
  using the bridge-reported project name.

- **Bridge backstop for non-runtime-resolvable format paths
  (`studio-bridge/StudioMCPBridge.cs` `AttachFormatterInline`; 1.0.9 #1, PRD 0023).**
  Direct bridge callers that bypass the service get the same refusal:
  `IsProjectRelativeFormatPath` (mirrors `core.classify_format_path`) returns
  `format_path_not_runtime_resolvable` BEFORE any model write, with the absolute
  `/Objects/<Project>/...` rewrite in the message.

- **Service project map and describe print `target_path` and surface dangling links
  (`service/core.py`; 1.0.9 #2, PRD 0023).** `describe` / `project_map` print the
  bridge's `target_path` in place of the raw NodeId GUID (map label `-> <path>`), and a
  `target_missing` source is flagged `[DANGLING LINK: target missing]` and collected into
  a top-level `diagnostics` list (`code: "dangling_link"`, `path`, `message`). Tests:
  `service/tests/test_project_map.py`. Skills docs updated:
  `optix-formatted-dynamic-link`.

- **Bridge resolves NodeId dynamic-link sources to `target_path`
  (`studio-bridge/StudioMCPBridge.cs` `LinkTargetJson`; 1.0.9 #2, PRD 0023).** A
  DynamicLink / `Source*` value that is a NodeId reference is resolved with
  `InformationModel.Get(nodeId)` and serialized with `target_path` (browse path from the
  project root) beside the raw value in describe, the set_property echo and the project
  map. An unresolvable NodeId yields `target_path: null` + `target_missing: true` (the
  dangling-link diagnostic). Each source of a multi-source formatter reports its own.
  Tests: `service/tests/test_attach_sources.py` (source-level), live
  `test_formatter_two_sources_target_path_live` in `test_bridge_live.py` (pending bench
  verification: `FTX_LIVE_BRIDGE=1 python -m pytest service/tests/test_bridge_live.py -k target_path`).

- **Live bridge tests for format path forms and target_path / target_missing
  (`service/tests/test_bridge_live.py`; 1.0.9 #1/#2, PRD 0023).**
  `test_format_path_forms_ab_render_live` (relative form refused, absolute form renders
  the target value; render half needs `FTX_LIVE_HMI=1` + tesseract),
  `test_formatter_two_sources_both_target_paths_live`,
  `test_formatter_target_deleted_reports_target_missing_live`. Skipped without a live
  bridge. Pending bench verification:
  `FTX_LIVE_BRIDGE=1 FTX_LIVE_HMI=1 python -m pytest service/tests/test_bridge_live.py -k "format_path_forms or both_target_paths or target_missing"`.

- **Per-project CDP tab registry and external emulator change detection
  (`service/core.py`, `service/_cdp.py`; 1.0.9 #12, PRD 0022 9c).** Emulator start stays on
  Studio's own run path (F5 via the targeted-window helper for the bridge-resolved
  Studio pid), never the runtime exe. CDP sessions that carry a `project` now use
  `cdp_tab_for`: exactly one tab per (project, web port), reused across calls,
  with any other tab on that port closed as leaked. Every emulator / `optix_status`
  services / `optix_observe` call (MCP and HTTP) compares the live emulator
  processes and their projects with the last recorded state, updates the record
  and returns `external_change: true` with `external_changes` (started / stopped /
  project_changed). Call-driven only; no daemon or thread. Tests:
  `service/tests/test_emulator_cdp_registry.py`. Skills docs updated:
  `optix-netlogic-and-bridge`, `optix-project-lifecycle`, `optix-verify-loop`.

- **Web-port discovery, project-name normalisation and port clash detection
  (`service/core.py`; 1.0.9 #11, PRD 0022 9b).** `parse_web_port` reads serialized
  values such as `8081 (UInt16)` (bridge and `Nodes/UI/UI.yaml` readers use it);
  `discover_web_port` returns only what the live bridge or the project names
  (no env override, no hard-coded default; `port=None` when unknown).
  `normalise_project_name` makes `X`, `X/HMI`, `x` and `X/` one project; the
  `run_emulator` and `_emulator_entries` project comparisons use it, fixing the
  false `runtime_project_mismatch` when one project is named two ways.
  `detect_web_port_clash` makes `run_emulator` refuse with `web_port_clash`
  (naming both projects and the port) BEFORE F5 when another open / emulator-running
  project is configured for the same port. Tests:
  `service/tests/test_web_port_discovery.py`.
- **One identity-keyed bridge resolver; port-order / first-found selection removed
  (`service/core.py` `resolve_bridge`; 1.0.9 #10, PRD 0022 9a).** Every
  bridge-calling tool now reaches its bridge through `resolve_bridge(cfg, project)`
  (the `_bridge_cfg_for` / `_require_bridge_for` / `_bridge_write_guard` /
  `_use_bridge_for` helpers are thin wrappers). It picks the bridge whose
  `/bridge/health` names the project by name (`project_name`/`project`, case and
  separators normalised, `X` = `X/HMI`) or directory (`project_dir`/`project_path`;
  a directory match decides first). No match raises `no_bridge_for_project`
  (still a `BridgeUnavailable`, so file fallbacks work) listing every bridge found
  with its port, project, directory and Studio pid; more than one match raises
  `ambiguous_bridge_for_project` (HTTP 409). `bridge_state()` and the
  `optix_bridge_status` / `/ui` top-level fields no longer single out the lowest
  port when several bridges are armed (read `bridges`). Tests:
  `service/tests/test_bridge_resolution.py`.
- **Bridge names its Studio; arm is idempotent per Studio pid
  (`studio-bridge/StudioMCPBridge.cs`, `service/studio_arm.py`; 1.0.9 #10, PRD 0022 9a).**
  `/bridge/health` (and the registry file) now also return `studio_pid`,
  `project_name` and `project_dir` next to the existing fields. `StartBridge`
  first looks for a live bridge of the SAME Studio process (ports from the
  process-scoped bound-port list, each confirmed by its own health
  `studio_pid`) and, if found, returns it instead of binding a second port.
  The service's arm fast-exit resolves the Studio pid from the project's window
  and reports `already_armed` only for the bridge whose health names that pid
  (or, with no pid, the same project name AND `project_dir`); another Studio's
  bridge is never reported. Tests: `service/tests/test_bridge_studio_identity.py`.
  Live check pending bench verification: arm two Studios with the same project
  name and compare `/bridge/health` `studio_pid` on each port.
- **Focus helper falls back to EnumWindows when `MainWindowHandle` is 0
  (`service/core.py` `_build_save_ps`; 1.0.9 #18).** On the bench the bridge's
  Studio reported `MainWindowHandle` 0 although its window was visible, so
  save and the emulator restart ended in `NO_TARGET_WINDOW` /
  `bridge_studio_no_window`. The generated PowerShell now enumerates top-level
  windows with `EnumWindows` + `GetWindowThreadProcessId` + `IsWindowVisible`,
  keeps only windows of the targeted pid that are visible and unowned, and
  prefers one whose title contains `Optix`, otherwise the largest. That handle
  is used for restore/foreground/keystroke. The targeted pid is always honoured
  (never another Studio's window), and `NO_TARGET_WINDOW` is emitted only when
  enumeration also finds nothing. `_build_save_ps` also builds the F5 /
  emulator start script, so both paths share the fallback. Offline tests:
  `service/tests/test_save.py`. Live tests `test_save_live_targets_bridge_studio`
  and `test_emulator_restart_live_focuses_studio` in `test_bridge_live.py` are
  skipped without a live bridge; live run pending bench verification:
  `FTX_LIVE_BRIDGE=1 FTX_LIVE_HMI=1 pytest service/tests/test_bridge_live.py -k "save_live or emulator_restart_live"`.
  Documented in `skills/optix-project-lifecycle/SKILL.md`.
- **Click round-trip test tolerates an unstable capture region on Optix 1.8
  (`service/tests/test_bridge_live.py`, `service/tests/_click_settle.py`;
  1.0.9 #17).** `test_normalized_click_round_trip` failed on 1.8.0.555 with
  "capture region [36.0, 664.0, 128.0, 40.0] is not stable (793 vs 1196
  bytes)". It now captures the region until two consecutive captures are
  identical (bounded at 5). If it never settles it asserts the bound
  variable's value change via the bridge read instead of a pixel diff, and logs
  `path=pixel` or `path=bridge-read fallback`. With a stable region (1.7.4.32)
  the captures and pixel assertion are unchanged. Offline tests:
  `service/tests/test_click_settle.py`. Live run pending bench verification:
  `FTX_LIVE_BRIDGE=1 FTX_LIVE_HMI=1 pytest service/tests/test_bridge_live.py -k normalized_click -s`.
  Documented in `skills/optix-web-click-surface/SKILL.md`.
- **Subtree-refusal live test creates and cleans up its own nodes
  (`service/tests/test_bridge_live.py`; 1.0.9 #16).**
  `test_subtree_refusals_live_and_bridge_survives` failed in setup with
  `node_not_found: no variable at: Model/ZzzUnused`, so the subtree guard
  (#6/#8) was never exercised live. The new `subtree_nodes` fixture creates the
  `Model/ZzzUnused` target variable and the Panels/Button before
  `bridge_wire_event`, and deletes everything it created on teardown, also when
  the test fails. The test asserts move, rename and convert_to_type are refused
  with `contains_event_handler` / `contains_netlogic` and that `/bridge/health`
  still answers. Live run pending bench verification:
  `FTX_LIVE_BRIDGE=1 pytest service/tests/test_bridge_live.py -k subtree_refusals`.
- **ftx-agent per-item batch accounting (`ci/ftx-agent.ps1 -Batch`; 1.0.9 #7).**
  `ci/batch-prompt-template.txt` tells the agent to end with an `ACCOUNTING`
  list keyed by item id (applied / failed with reason / skipped with reason)
  and states that Verify is the ground truth. With `-Batch`, the wrapper calls
  `ci/ftx_agent_accounting.py` and exits 4 naming the missing ids when
  applied + failed + skipped differs from the item count.
- **Health reports the real `project_path`; `project_path_v2` flag
  (`studio-bridge/StudioMCPBridge.cs`, `service/core.py`; 1.0.9 #9).**
  `GetProjectPath` no longer falls back to Studio's working directory (usually
  `C:\WINDOWS\system32`); it probes the SDK path properties and the project
  `ResourceUri`, and reports `null` when nothing reliable is found.
  `/bridge/health` advertises `project_path_v2`. The service
  (`_normalize_project_path`) treats any path at or under the Windows
  directory from an older bridge as unknown, in both health and the registry
  open candidates.
- **Arm expands collapsed ancestors of a nested bridge node
  (`service/studio_arm.py`; 1.0.9 #9).** When the filtered tree hides an
  ancestor of the bridge leaf, or the leaf's menu has 0 items, each collapsed
  ancestor is opened outermost-first through ExpandCollapsePattern (state-aware;
  chevron click is the fallback) and the leaf is retried. If expansion is
  impossible the filtered leaf row is right-clicked directly (`direct: true` in
  `tried`). On `verify_timeout` the arm now looks for a NetLogic
  security-warning top-level window owned by the Studio pid and presses Proceed
  through the same UIA-invoke helper (`security_warning` in the result).
- **Consent is pressed by UIA InvokePattern and its dismissal is reported
  (`service/studio_arm.py`; 1.0.9 #7).** `_press` invokes the security-warning
  button via InvokePattern (works when Proceed sits below the work area) and
  falls back to a coordinate click only when the element has no InvokePattern.
  The 'do not show again' toggle is still ticked first. The popup is re-queried
  after the press: results carry `consent_dismissed` next to `consent_clicked`,
  and a surviving dialog makes the arm / `clear_consent` return
  `error: "consent_not_dismissed"` with the dialog title instead of a later
  `verify_timeout`.
- **Service surfaces the subtree refusals (`service/core.py`).** `contains_netlogic`,
  `contains_event_handler` (and `refused_netlogic_move`) come back unchanged from
  `bridge_move_node`, `bridge_convert_to_type` and the batch `rename` (lowered to
  move) as `BridgeWriteFailed` with `bridge_code`, an actionable `hint`, and the
  same in `classify_bridge_failure`. A batch records a refused item in
  `refused_ops` and continues with the other items. Codes are listed in
  `BRIDGE_SUBTREE_REFUSALS` / `KNOWN_BRIDGE_ERRORS`; see troubleshooting.md.
- **Bridge refuses move / rename / convert_to_type on NetLogic and EventHandler
  subtrees (bridge 1.0.9; closes #6, #8).** `StudioMCPBridge.cs` walks the source subtree
  before any mutation (`FindUnsafeSubtree`, depth-bounded, top node included)
  and returns `refused_netlogic_move` (top-level NetLogic on move),
  `contains_netlogic`, or `contains_event_handler`, changing nothing. The batch
  dry run reports the same per-item refusals. `/bridge/health` now advertises
  `subtree_guard`; `BridgeVersion` is 1.0.9. Pending bench verification: move,
  rename and convert a Panel holding a NetLogic / a MouseClick handler on a live
  Studio and confirm the refusal and that `/bridge/health` still answers.
- **Skill docs explain the subtree refusal.** `optix-custom-keyboard` (move
  section) and `optix-anchored-edit` (convert_to_type) now state what is
  refused (`refused_netlogic_move`, `contains_netlogic`, `contains_event_handler`
  on move / rename / convert_to_type), why (re-authoring via `CopySubtreeInto`
  crashes Studio) and the workaround (move the NetLogic out first, re-wire the
  handler afterwards, or use Studio's own rename/edit).
- **Optix Studio 1.8.0.555 support for arm, consent clear and project open.**
  Studio 1.8 changed the shape of its UI Automation tree and added a project
  upgrade prompt. Four behaviours, all in `service/` (covered by unit tests with
  faked UIA; pending bench verification against a live Studio 1.8.0.555 — run
  `optix_project open` / `optix_bridge_arm` on a 1.8 box to confirm):
  1. **Row labels at any depth.** `studio_arm` matches Project-tree row labels
     as `TextControl` descendants down to a bounded depth (4 levels, node-capped),
     not only direct children of the window. 1.7 exposes the label as a direct
     child; 1.8 nests it in a grouping container. Both shapes yield the same flat
     row list, so arm works on either.
  2. **Focus-confirmed key sending.** Ctrl+A / Delete / typed text go to the
     Search box only after the box itself is confirmed to hold keyboard focus
     (bounded retries). If focus cannot be confirmed, the value is set through
     `ValuePattern` when available; otherwise arm returns
     `{ok: false, error: "focus_unconfirmed"}` and sends no keystroke. Keys are
     never sent blind, since with focus elsewhere they edit whatever Studio
     element has it.
  3. **Consent clear resolves the project directory.** `clear_consent` (used by
     `optix_emulator run/restart` on the NetLogic security warning) now resolves
     the project directory and served-name aliases itself via `resolve_project`
     when none is passed. An unresolvable name returns
     `{ok: false, error: "project_not_found"}` instead of silently doing nothing.
  4. **`optix_project open` returns `upgrade_required`.** Opening a project saved
     by an older Studio shows a modal "Project Upgrade" dialog. `open` now detects
     it and returns promptly with
     `{ok: false, error: "upgrade_required", project, studio_version, dialog, hint}`
     instead of waiting out the 90 s `open_timeout`. **The upgrade dialog is not
     clicked automatically**: upgrading is one-way, so accepting or declining it
     is an operator decision. Accept it in Studio, then retry `open`.

## [1.0.8]

- **`optix_bridge_invoke_method` / `POST /bridge/node/invoke` no longer kill
  Studio — fixes #4, reported by @Rempelino.** Root cause: `ExecuteMethod`
  ran on the bridge's HTTP thread with no Optix session, and native code
  dereferencing the missing session corrupted the heap (`c0000374`) on ANY
  method, not only one that throws mid-mutation; even reading
  `Sessions.CurrentSessionInfo` on that thread is fatal. The bridge now wraps
  the call in `Sessions.ImpersonateRootTemporary()` (the pattern in the
  FactoryTalk-Optix NetLogic_CheatSheet `sessions.md`). `DelayedTask`,
  `LongRunningTask` and `IContext.Dispatch` never fire at design time, and
  `Sessions.CurrentSessionHandler` is not implemented there, so root
  impersonation is the design-time option. `/bridge/health` advertises
  `invoke_session`; the service refuses invoke against a bridge without it
  (`invoke_unsupported_bridge`). `unsafe` is accepted for compatibility and has no effect.

- **Bridge: `attach_string_formatter` — format a display value.** New op / route
  (`POST /bridge/node/attach-string-formatter`) attaches a `StringFormatter` as a
  converter ON THE PROPERTY (HasConverter on the property itself, where
  `attach_expression` puts its ExpressionEvaluator), so a Float renders `67.7`
  instead of `67.701805`. This is the shape a hand-built Studio label uses and is
  distinct from `attach_formatter`, which builds a formatted dynamic LINK (the
  StringFormatter formats a NodePath) and renders EMPTY as a value formatter
  (field finding 2026-09-01, re-confirmed 2026-09-16). `Format` is written via
  the HasParameter child, never the `.Format` property setter: that setter reads
  `Context.Sessions.CurrentSessionInfo`, which is null on the bridge's HTTP
  thread, so it AVs and kills Studio with nothing logged (decompiled
  `FTOptix.CoreBase.Net` 2.1.0.89). Sources are positional (`Source0..N`, so
  `{0}`/`{1}` in the format); `raw=1` attaches each source as a literal NodePath
  so an alias/template path like `{data}/value` attaches on a PanelType and
  resolves per instance. Proven live on `string_formatter` 2026-09-16: a
  bridge-authored formatter on `Label.Text` rendered `3.14` from `{0:F2}` over a
  Float 3.14159, clean runtime log, no crash. Every `{N}` in `format` must have a
  bound source: a missing or misspelled `sources` is refused with
  `unbound_placeholder` before anything is written (previously the formatter
  attached with no `Source0`, reported `succeeded`, and rendered the bare format
  text). The response carries `placeholders` and `bound` counts.

- **Batch ops: `sources` accepts a list, and `source` is an alias.** On
  `attach_expression`, `attach_formatter` and `attach_string_formatter` a JSON
  list is joined into the delimited string the bridge reads (`,` positional,
  `;` for `attach_formatter`'s `name=path` pairs; a mapping is accepted there
  too). Lists used to be stringified to `"['Model/X']"` and fail
  `source_not_variable`. `source` (singular) is folded into `sources` instead of
  being dropped as an unknown field; both present and different is an error.

- **Bridge: non-UI builtin types are creatable.** `create_object` (`object_type`),
  `create_type` (`base_type`) and `create_widget` now resolve a bare or
  `Module.Type` name against EVERY loaded `FTOptix.<Module>.ObjectTypes` catalog
  (step (a2) in `ResolveUiTypeId`) and, failing that, walk the whole builtin type
  tree without the UI-namespace filter (step (b2), project namespace excluded);
  `create_object` tries the builtin catalogs before a project type path.
  Measured 2026-09-13 on a live project: `OffNormalAlarmController`,
  `ExclusiveLevelAlarmController`, `NonExclusiveLevelAlarmController`,
  `ExclusiveRateOfChangeAlarmController`, `AlarmController`, `DataLogger`,
  `User`, `Group`, `SQLiteStore`, `ODBCStore`, `Recipe`, `RecipeSchema`,
  `Report` all create and read back. Still `type_not_found` on that project:
  `DigitalAlarm`, `EmbeddedDatabase`, and the library widgets `AlarmGrid`,
  `LoginForm`, `LoginButton`, `LogoutButton`, `AlarmBanner` — not in any loaded
  catalog, consistent with Studio library templates that are COPIED into a
  project on drag rather than builtin ObjectTypes; add them from the Library
  panel once, then address them by project path.

### Added
- **`optix_read_netlogic(project, cls)`** — read a NetLogic `.cs` source by
  class name from `ProjectFiles/NetSolution`, path-traversal safe, size-capped,
  `<untrusted>`-wrapped, read-only. Unlike `optix_read_file` it works while
  Studio is open, because the NetSolution lives on disk and not in Studio's
  model (the VS / VS Code attributed-editor guard still applies). Default tool
  count 33 → 35 (with `optix_execute_method`); the documented counts in
  `README.md` and `docs/tool-reference.md` are checked by a test against the
  surface a default install registers.
- **Per-project web-engine port resolution (I28; supersedes I24).** `optix_observe`,
  `optix_interact`, and the emulator readiness probes now derive the CDP/runtime
  port from the project's own `WebPresentationEngine` node — via the armed bridge
  or an offline parse of `Nodes/UI/UI.yaml` — rather than the machine-global
  `OPTIX_RUNTIME_TEST_PORT`. Resolver priority: explicit `port=` parameter →
  project node → `OPTIX_RUNTIME_TEST_PORT` (operator override only) → default 8081.
  A project with no engine node returns `source="no_engine"` with a nudge naming
  `optix_bridge_ensure_web_engine`. **Self-heal (I28 rule 2):** when the expected
  port is dead but the project's emulator is alive, the resolver re-derives and
  follows — one loud log line and a `port_followed` field in the tool result, so
  the 2026-08-25 "web presentation engine is down" false-positive on a multi-port
  box (a second runtime serving on 8088, service expecting 8081) recovers without operator
  intervention. **Doctor row (I28 rule 3):** `optix_status(action="doctor",
  project=…)` gains a `web_port` row: `project_says` / `service_expects` /
  `listening` (pid + process name, so an Ignition-owned squatter is named), plus
  the runtime log's bind-fail signature surfaced as
  `web_engine_bind_failed {port, holder}` with remedies.
- **`wire_event` native UI commands by name — `OpenDialog`, `CloseDialog`,
  `OpenKeyboard`.** The bridge keeps a table of the builtin `UICommands` it
  resolves from the `/Objects` root (they live outside the project tree, so
  neither `method_path` nor a NodeId write could reach them), and `args`
  carries their InputArguments by name. `OpenKeyboard` takes THREE —
  `KeyboardType`, `TargetVariable`, `ParentItem` (per
  `Modules/FTOptix.UI/.../Module.xml`; a wrong count is a runtime "invalid
  number of arguments") — and the bridge refuses a wrong set BEFORE it
  creates the handler. Command names are matched case-insensitively on the
  service side, like event names. The standalone `optix_bridge_wire_event`
  tool now exposes `args` and `object_raw` too (they were reachable only
  through the batch op).
- **Links authored INSIDE an ObjectType are now relative.** `attach_expression`
  and `attach_formatter` wrote their `Source{n}` dynamic links as absolute
  NodeIds, and `wire_event` (method_path mode) wrote an absolute
  `ObjectPointer`, so every INSTANCE read the TYPE's variable (an `Enabled`
  that never changed) or the runtime answered "No behaviour or observer
  found". `bind` was already right because its property is parented before
  `SetDynamicLink` runs. The three sites now re-point the link relative to
  the nearest common type ancestor (`relative_sources` on attach results,
  `via: …+reltype` on wire_event); nothing changes outside a type.
- **`wire_event` gains `args` and `object_raw` — the two things a UI-hosted
  method needs before it does anything.** Found while authoring navigation handlers over HTTP: without
  arguments only argument-LESS methods were reachable, which excludes every
  method a converted navigation action calls (`ChangePanel(NewPanel)`,
  `ChangePanelByTabName(TabName)`, `OpenDialog(Dialog, AliasNode)`). `args`
  takes `"Name=Value;..."` pairs — `';'`-separated because node paths routinely
  contain commas — and a value that resolves to a node becomes a `NodePointer`
  argument (a bare NodeId wires but never resolves at invoke time); anything
  else becomes a String. `object_raw` late-binds the call TARGET: a literal
  NodePath RELATIVE TO THE EVENT NODE (e.g. `"../Loader"`), written as a
  DynamicLink on `ObjectPointer` with the levels up from `ObjectPointer`
  prepended for the caller. It is required for every UI-hosted method, because
  an absolute NodeId names the DESIGN-TIME node while the UI is instantiated
  per session under `UI/<engine>/Sessions/<id>/UIRoot/...` — `ChangePanel`
  wired without it resolves, throws nothing, and silently does not navigate.
  Both are method-path only; `args` alongside a variable `command` is a hard
  error rather than a silent drop. `via` reports `eventhandler[+args][+lateobj]`.
- **`wire_event` `command="OpenDialog"` / `"CloseDialog"`.** UI commands live on
  `/Objects/Commands/UICommands`, OUTSIDE the project root, so `ResolveNode`
  (`Project.Current.Get`) cannot reach them and a `method_path` of
  `"Commands/UICommands/OpenDialog"` answered `node_not_found`. They are now
  resolved by path from `Project.Current.Owner` — the same absolute pointer a
  Studio-authored handler carries — and exposed as a `command`, the way
  `SetVariable`/`ToggleVariable` are. Unlike those, a UI command takes its
  arguments from `args`: `"Dialog=<Dialog type path>;AliasNode=<node or
  empty>;ParentItem="`, where an empty `ParentItem` is late-bound to the event
  node, as Studio does.
- **A project that is OPEN IN STUDIO now resolves even when it lives outside
  `projects_root`.** `resolve_project` only ever tried
  `projects_root / <project>`, so a customer project on the Desktop
  (`C:\Users\<u>\Desktop\Line4\HMI`) answered `project not found` from
  every one of the ~29 call sites — `optix_bridge_arm`, `optix_save`,
  `optix_build_check`, `optix_project`, `optix_routes`, `optix_read_file`, ...
  — while every bridge-routed tool worked with it perfectly, because those
  route by the project the bridge REPORTS rather than by a path. The fallback
  and its boundary, in order: `..` is rejected first; the name is joined onto
  `projects_root` and canonicalized, and anything not under the root is
  rejected there (that is what catches an absolute name, since joining an
  absolute path replaces the root entirely) — an absolute name is therefore
  never eligible for the fallback; an existing in-root directory wins and
  costs ZERO extra work (no process scan runs at all); only on a miss, and
  only for a BARE name (no separator, no drive colon — a subpath never takes
  this path), the directory may come from a running `FTOptixStudio.exe`
  command line, and must exist and hold exactly one `.optix` file whose stem
  OR whose folder basename equals the name case-insensitively. The caller's
  string is only ever COMPARED against that evidence — no path is ever taken
  from it. Reuses `studio_guard`'s existing cached process snapshot (one
  name-filtered psutil pass, 2s TTL) rather than adding a second scanner, and
  caches nothing of its own.
  The armed bridge's `/bridge/health` is deliberately NOT used as a second
  evidence source: `HealthJson()` returns `{bridge_version, project,
  model_loaded, port}`, where `project` is `Project.Current.BrowseName` — a
  NAME, not a path — so a bridge can confirm a project is open but cannot say
  where it lives. Giving it a path field is a future bridge-version change.
- **`resolve_project_info(cfg, project) -> {"dir", "source"}`** — the sibling
  of `resolve_project` that reports WHICH source answered (`projects_root` or
  `studio_process`). Surfaced by `optix_status(action="doctor", project=...)`
  (and `GET /doctor?project=`) as one non-required `project` row, and by
  `optix_bridge_arm`'s result as `resolved_from`, so "it resolved, but only
  because Studio is holding it" is visible instead of silent. The
  `project not found` error now carries a `resolved_from` breadcrumb naming
  `projects_root` and, when the fallback ran, the project NAMES Studio has
  open.
- **`optix_bridge_edit` batch chunking and timeout recovery (I18).** A batch that would otherwise stall an MCP
  client past its idle timeout now stops early and returns
  `state="chunked"` with a `batch_id`. Thresholds: `OPTIX_BATCH_CHUNK_SECONDS`
  (default 90 s) and `OPTIX_BATCH_CHUNK_OPS` (default 40 ops); both
  overridable per call via `chunk_seconds` / `chunk_ops` parameters.
  Pass the returned `batch_id` to `action="continue"` to apply the next chunk,
  or to `action="status"` to inspect the journal at any time (omit `batch_id`
  for the most recent N batches on the project). `applied` counts ops landed
  across chunks, readable the same way as `state="partial"`. Per-op timings
  are recorded as `op_timings` (`{index, op, ms, ok}` per applied op) in the
  journal at `%LOCALAPPDATA%\ftx-mcp\batches\<batch_id>.json`, pruned after
  `OPTIX_BATCH_JOURNAL_DAYS` days (default 7). MCP progress notifications are
  best-effort and silently absent without a `progressToken` — the journal is
  the authoritative record.

### Fixed
- **`SpinBox.ValueChangeBehaviour` (and any property only the .NET proxy
  declares) is refused, not fatal.** Any write to it — a valid ordinal too —
  killed Studio with an AccessViolation inside `GetOrCreateVariable`: the proxy
  declares the property, the loaded UA type chain does not. The bridge now
  materializes a property only when an ObjectType in the node's supertype chain
  declares it, and answers `proxy_only_property` otherwise. (The I31 entry
  below fixed the invalid-member path; this was a second, earlier crash site.)
- **A same-named non-project folder no longer shadows the open project.** With
  `projects_root` at the Desktop and a project at `Desktop\Line4_HMI\HMI`,
  the folder `Desktop\Line4_HMI` (no `.optix`) won resolution and every tool
  got the container. A bare name whose in-root folder holds no `.optix` now
  lets the open-in-Studio / registry match answer first.
- **`optix_bridge_edit` results carry per-op `detail`** — `via` (wire_event),
  `relative_sources`, `achieved` (reorder), `prior binding cleared` (bind).
  They were computed by the bridge and dropped by the batch loop.
- **`gridlayout_without_columns` warning.** A GridLayout's `Rows`/`Columns`
  cannot be set over the bridge, and one in a rendered window blanks the
  whole web page; batches creating one now say so.
- **Integer `Value` writes keep their declared width.** `set_property
  name="Value"` cast every integer DataType to 32 bits, so `Int64`/`UInt64`
  values wrapped silently and reported success (`123456789012345` stored as
  `-2045911175`). Values are now parsed at the variable's width, and an
  out-of-range value (`70000` on `Int16`, `-1` on `UInt64`) is refused as
  `bad_value` at validation, before anything is written.
- **`wire_event` silently no-oped when the event node already had a handler.**
  The bridge wrote the op and returned `ok: true`, but the existing handler was
  left unchanged — args and object pointer from the original wiring stayed in
  place and the new ones were discarded. The fix has three modes: (1) **update
  in place** — the handler is found by `EventTypeName` match, its `args` and
  `ObjectPointer` fields are replaced from the op, and the updated handler is
  read back; `via` in the result now ends with the outcome (`+existing` for an
  in-place update, `+replaced`, or no suffix when created). (2) `handler_exists`
  is returned (with the conflicting handler's details) when in-place mutation is
  unsafe — e.g. the existing handler is not the one the op expects or the bridge
  cannot identify it unambiguously. (3) Pass `replace=true` to force a
  delete-then-create regardless: the old handler is removed first and a fresh
  one written, guaranteeing the result matches the op exactly.
- **I30 / I23 — `optix_emulator` project attribution and structured status.**
  On a multi-Studio box, `emulator_status` previously matched any
  `FTOptixRuntime.exe --application-name=Emulator` process, so
  `action="status" project="Alpha"` could report Beta's runtime as Alpha's —
  and a subsequent `action="run"` or `action="restart"` could send F5 to the
  wrong Studio window. The process scan is now narrowed by the
  `\Emulator\Projects\<Project>\` cache-path component of the runtime command
  line (exact, case-insensitive, not a substring so `Line4` never matches
  `Line4_HMI`). When `project=` is given the status result gains
  `project_resolved` (the name extracted from the path) and `matched_path` as
  attribution evidence. `action="run"` and `action="restart"` refuse with
  `{"error": "runtime_project_mismatch", "requested": …, "resolved": …,
  "pid": …, "nudge": …}` when any running emulator's path identifies a
  different project (I30 — correct project attribution via cache path).
  `action="status"` also gains a structured `state` field
  (`stopped`/`starting`/`running`/`zombie`/`demo_expired`) alongside the
  existing `running` bool (kept for back-compat). `demo_expired` is derived
  from the emulator's per-project lifecycle log and wins over all other
  conditions; `zombie` distinguishes a process that is up with a visible window
  but no port listener from one that is still initialising. `last_lifecycle_line`
  surfaces the last runtime log entry for diagnostics (I23 — structured state
  field on emulator_status).
- **I31 — a bad enum value on `SpinBox.ValueChangeBehaviour` took Studio
  down.** Two things, both in the bridge: (1) the listener's `accept` and the
  per-request handler shared one try, so a client hanging up mid-response
  ended the listener for the rest of the session — split, with a catch-all
  that logs and continues; (2) the crash itself was the last arm of the enum
  coercion: when the datatype's browse name did not match a loaded enum type,
  an invalid member fell through to a bare-string assign whose failure is a
  NATIVE assert (uncatchable). The enum is now read off the owner's CLR proxy
  (`DeclaredEnumType`), so invalid members return the same `bad_value` +
  member list the other 21 enum properties already did. Handler exceptions
  report `type: message` in the 500 payload.
- **`move` of a NetLogic node closed the project in Studio** — window back on
  Home, bridge gone, unsaved edits lost, no exception (field finding
  2026-09-04). The bridge now refuses it before any mutation
  (`refused_netlogic_move`), in the batch validator's dry run too; move
  NetLogic nodes in Studio's Project view.
- **I35 — `describe_node` reported a bare ObjectType as `ScreenType` (its
  instance as `Screen`).** `dotnet_type` is now corroborated against the
  node's real base chain (SuperType for a type, ObjectType for an instance)
  and says `BaseObjectType` / `BaseObject` when that is what it is; Screen,
  Panel, Folder and Window types are unchanged.
- **`BridgeVersion` is 1.0.8** — the bridge's behaviour changed; re-propagate
  `studio-bridge/StudioMCPBridge.cs` to every project copy and the template.
- **`optix_build_check` path rebasing was Windows-only.** MSBuild diagnostic
  paths are always Windows paths; comparing them with the host's `Path` on a
  POSIX box never matched the throwaway-copy prefix (the one test the Linux
  suite had to deselect). Compared as `PureWindowsPath` on every host now.
- **`optix_bridge_arm` could not find an UNSAVED project's window.** Studio
  appends `*` to the project name and path it shows in its own in-scene chrome
  while there are unsaved changes (`C:\Users\<u>\Desktop\NavArgDemo*`), and
  `_studio_window_for` — which identifies a Studio window by exactly that text,
  the Win32 caption being the same for every instance — compared it verbatim.
  So `arm` answered `studio_window_not_found` on any project with pending
  edits, which reads exactly like "Studio is not open" and cost a session three
  wrong theories before one Ctrl+S made the same call succeed. Every
  bridge-edited project is unsaved by definition until someone saves, so this
  hit constantly. The marker is now tolerated on both ends of the text, for the
  root-row name match and the path-prefix match alike (I34).
- **`type` is now accepted as an alias for `create_widget`'s `widget_type`
  and `create_object`'s `object_type` in a `bridge_edit` batch (I32).** The
  raw HTTP routes spell the node type `type`
  (`/bridge/ui/widget?...&type=PanelLoader`), and HTTP-first
  callers follow them; the batch op-spec
  spelled it per-noun only. Through `bridge_edit`, `type` was an unknown
  field — a WARNING under the default non-strict mode — so it was DROPPED
  and the op's own default applied: a `Label` (or a bare `UAObject`) under
  the right name in the right parent, with the batch reporting
  `state: succeeded`. A wrong-typed node passes every subsequent model read;
  only a render reveals it. `_normalize_edit_op` now folds `type` into the
  canonical field and removes the key (so the unknown-field guard stays
  quiet about a field the service itself consumed), and RAISES when both
  spellings are present and name different types rather than guessing.
  Prior art: gap-register **B4** (2026-08-06) tabulated the same field-name
  table, but recorded the wrong spelling as *rejected* — which holds only
  for REQUIRED fields; an OPTIONAL field with a default succeeds instead,
  which is the half worth fixing. Relatedly, a `create_widget` with no type
  at all now carries a `default_widget_type` warning in the report naming
  the `Label` default, so the remaining silent default is visible without a
  render.
- **Destructive ops (`delete`/`move`/`reorder`) now hard-fail on an unknown
  field, regardless of `strict`.** These verbs have no scoped/partial form —
  there is no "delete just this property" op — so an unrecognized field
  (e.g. a `name` meant to scope a `delete` down to one property) is very
  likely a caller trying to narrow the blast radius, not a harmless extra.
  Previously `unknown_op_field` was only a warning under the (default)
  non-strict mode, and the op still applied against the whole node at
  `path`. Live incident: a `delete` op on a `NavigationPanel` carrying
  `name: "AttachedPanelLoader"` deleted the entire node instead of clearing
  the one property, recovered only via the caller's own Ctrl+Z in Studio —
  the bridge has no undo. `bridge_edit` also no longer mutates the report
  dict returned by `bridge_validate_ops` in place (it was a shared/caller
  object in some paths — surfaced as cross-test pollution while adding
  regression coverage for this fix).
- **`StudioMCPBridge.cs` stopped compiling in any project with an EtherNet/IP
  driver.** Adding a comm driver makes Studio inject
  `using FTOptix.RAEtherNetIP;` into EVERY NetLogic `.cs` in the solution, and
  that namespace exports its own `Encoding` — colliding with `System.Text` on the
  bridge's four `Encoding.ASCII/UTF8` call sites
  (`CS0104: 'Encoding' is an ambiguous reference`). Since a NetLogic that fails to
  compile takes the in-Studio bridge and the emulator down with it, this presents
  as "the bridge died", not as a build error. Fixed with a using alias
  (`using Encoding = System.Text.Encoding;`) rather than qualifying the call
  sites, so it survives Studio re-injecting its usings and covers usages added
  later.
- **`bind` left the previous binding's converter attached.** `SetDynamicLink`
  replaces a link's *value* but not a converter already hanging off it, so
  binding a property that carried a formatted dynamic link produced a property
  with BOTH a new target and a stale `DynamicLinkFormatter` fighting over it —
  reported as a clean `applied: 1`. `bind` now calls `ResetDynamicLink()` first,
  which is Studio's own right-click -> *Remove binding* step and what
  `attach_formatter` already did. Measured on a Label's `Text` (2026-09-01).
- **`bind` destroyed property-level converters while reporting it cleared
  nothing.** `ResetDynamicLink()` clears BOTH converter positions — link-level
  (`HasConverter` on the DynamicLink, where a `StringFormatter` lives) and
  property-level (`HasConverter` on the property, where `attach_expression` puts
  an `ExpressionEvaluator` via `SetConverter`). Detection walked only the link,
  so binding over a formula destroyed it and returned
  `"prior binding cleared": {"converter": false}` — an affirmative all-clear over
  a real loss. Both positions are now inspected and the response names the
  `converter kind` that was removed. Both the source and raw branches report it
  (the raw branch initially reported nothing at all).
- **`optix_project(action="new", template=...)` could never create a project
  from a template.** The Studio CLI invocation omitted `-u`, so Studio ran
  `NewProject` during `IDEContext::Initialize` with the *user* template library
  not yet indexed and the lookup failed
  `Cannot create a new project with invalid template: <name>` out of
  `TemplateLibrary::GetModelNodeFromPath` — for a template name the Studio GUI
  resolves fine. Verified by running identical argv with and without the flag.
- **Studio's NetLogic security warning was raised again on every trigger.**
  `_click_consent` clicked `Proceed` but never ticked *"Do not show this warning
  again"* (there was no checkbox/toggle handling anywhere in the service), so
  arming consented and then the very next F5 hit the identical modal. The
  clicker now ticks the box first. It also walks the whole popup **before**
  clicking anything: the consent button is frequently reached before the
  checkbox in traversal order, and the previous early-return would have
  dismissed the dialog with the box still unticked.
- **`optix_emulator(action="run"/"restart")` detected that modal but never
  cleared it**, burning the whole `ready_timeout` and reporting
  `probable_cause: target_or_modal` with nothing spawned. It now clears the
  dialog in flight. The focus-stealing click is gated behind
  `studio_uia.pending_dialog` (a read-only walk that never takes the
  foreground), so a normal build is never interrupted; bounded to the first 25 s
  and at most one clear. **F5 is not resent** — answering the dialog lets the
  already-sent keystroke continue, and F5 toggles. Reports `consent_cleared`.
- **I27: `services.ps1 start -Silent` raw-failed on an elevation-mismatched
  task.** If `setup.ps1` had been run elevated, the scheduled task was
  Admins-owned, so a later `-Silent` from a regular shell died inside
  `Set-ServiceConsoleMode` at `Set-ScheduledTask` with a raw CIM `Access is
  denied` (0x80070005) — plain `start` still worked (no write needed), so
  the service just ran console-visible and the failure read as "-Silent is
  broken." `Set-ServiceConsoleMode` now catches the permission-denied case
  (by HRESULT and by message, to survive both a bare `CimException` and an
  `UnauthorizedAccessException`) and fails with the real cause plus both
  remedies (re-run `-Silent` elevated once, or uninstall elevated + re-run
  `setup.ps1` from a regular shell to re-register non-elevated).
  `setup.ps1` also gained `-AllowElevated`: it now refuses to run elevated
  at all unless that switch is passed, instead of only warning.
- **The batch validator's hypothetical model follows move and predicts
  where a create lands.** The validator now tracks `move` and `rename` ops
  applied earlier in the batch and resolves subsequent references against
  the relocated path (`moved_earlier_in_batch` error names the `new_path`;
  the old path no longer resolves). `create_widget` into a screen with a
  placeholder collection is resolved the same way `WriteWidgetInline`
  routes it at apply time: the actual created path is reported in the
  `routed_into_collection` warning (`routed_path`), so subsequent
  `set_property`/`bind`/`reorder` ops in the same batch can target the
  real path without splitting the batch. Three validator checks previously
  deferred to apply time are now reported as validation errors:
  `read_only_collection` (runtime-managed collection), `not_renderable`
  (`Item`/`Container` abstract types), and `ambiguous_container` (multiple
  matching placeholder collections under the parent).
- **`reorder` indexes graphic children and verifies where it landed**. `index` counts among graphic children only
  (variables and sub-objects are excluded from the count) and is
  direction-independent; the `achieved` field in the result reports the
  final position regardless of which direction the node moved.
- **`GridLayoutProperties` authored through the bridge carries the
  `FTOptix.UI` browse-name qualifier, the only one the engine honours**. Previously the node's browse name was qualified
  with the project namespace; `RowStart`/`ColumnStart` were silently ignored
  by the grid engine and `reorder` was the only positioning workaround. What
  is namespaced is the QUALIFIED BROWSE NAME (Studio writes
  `Name: ns=<FTOptix.UI>;GridLayoutProperties`), not the NodeId — every
  instance node lives in the project namespace, so a NodeId-namespace check
  can only ever fail. The bridge re-reads `QualifiedBrowseName` after the
  write and returns `wrong_namespace` rather than `ok: true` if it did not
  take.
- **The builtin UI type catalog resolves by browse name when the compiled
  NodeId constant disagrees with the loaded module — the VirtualKeyboard*
  block was unusable and `describe_type` answered with another type's
  browse name**. The resolver walks the builtin
  TYPE TREE (a type node's `Owner` is its SUPERTYPE, so subtypes are found by
  descending from the topmost type root, and the walk is widened to the
  `Types` folder so event types under `BaseEventType` are reachable too);
  `list_ui_types` / `describe_type` / `optix_schema` all report
  `resolved_by:"browse_name"` on a corrected entry. `misaligned` /
  `catalog_misaligned` now flag only what the walk could NOT reconcile — a
  constant NAMED differently from the node it correctly points at, such as
  `UserValueChangedEvent` for the ObjectType browse-named `UserValueChanged`.
- **A stale `bridges/<port>.json` from a killed Studio suppressed the
  range-scan fallback for one call, making an armed project appear unserved.**
  A registry file left behind after a killed Studio — for example from the CI
  teardown race — was the sole entry in the `bridges/` directory;
  `_scan_bridge_ports` probed only that (dead) port, returned empty, and
  unlinked the stale file, so a project armed on any other port read as
  unserved for that call. The stale file was gone by the time the next call
  ran, the empty registry fell through to the range scan, and the bridge
  resolved — which is why the failure appeared intermittent. Registry ports
  and the configured range are now probed together in one concurrent pass:
  the registry adds out-of-range ports and stale-entry cleanup but is never
  authoritative, because a pre-1.0.8 bridge writes no registry file and a
  registry-only answer hid it. The suite also pins `%LOCALAPPDATA%` to a temp
  dir, so running it on a box with armed bridges no longer reads (or deletes)
  the real registry. The `bridge not serving '<project>'` refusal
  previously named the serving project of the FIRST bridge in port order
  (from `bridge_state()`, unrelated to what was actually probed); it now
  lists `ports_probed` and each port's served project.

### Added
- **`set_property` can now CLEAR a NodeId** — pass `"null"` (or `""` when calling
  the bridge directly; the MCP layer rejects an empty string as a missing field).
  The NodeId arm previously required the value to resolve to a node, so there was
  no way to express "no target". A `NodePointer` driven by a dynamic link keeps
  whatever static `Value` the designer last set: invisible on screen (the link
  wins) but misleading in the property grid, and the **fallback if the link ever
  resolves to nothing** — a confidently wrong panel instead of an empty one.
  A navigation loader in a field project read as pointing at a recipe page. Verified that
  clearing it does not disturb the link-driven render.
- **`optix_execute_method(project, node, method)`** — right-click -> Execute any
  design-time `[ExportMethod]`, not just the bridge's `StartBridge`/`StopBridge`.
  The generalisation of `optix_bridge_arm`, which was this hard-wired to one
  node and two methods. Lets a project's own generators/builders and one-off
  migration scripts be fired from the agent instead of by hand. Uses the same
  UI Automation gesture — which is the ONLY safe route, since invoking a
  design-time method in-process on the bridge's HTTP thread runs it off the UI
  thread and crashes Studio. `ok` means the click landed, NOT that the method
  succeeded: arbitrary methods expose no signal to verify against, so the
  result carries a nudge to confirm by `describe_node` or a render.
- `studio_arm.execute_method(..., verify=...)` — `"none"` skips the port-based
  pre-checks and verify loop. Without it, any method other than
  `StartBridge`/`StopBridge` was judged by a bridge-port signal it never
  produces and reported `verify_timeout` on a successful run. `verify=None`
  (the default) derives `arm`/`stop` exactly as before, so the arm path is
  bit-for-bit unchanged.
- `studio_arm.clear_consent(project, ...)` — public entry point for answering
  Studio's security warning outside the arm path, with the same cursor and
  foreground restore as `execute_method`.
- **`optix-project-lifecycle` skill** — cold-starting a project hands-off
  (new-from-template -> open -> arm), making and verifying a project template,
  what the security warning actually is, running a project's own design-time
  methods, multi-instance port hygiene, and reading demo/reference projects
  safely.
- **`retype` op + `POST /bridge/node/retype`** — change a VARIABLE's DataType
  in place (optionally its array shape) while keeping the node: NodeId,
  children (DynamicLink, Mode, converters) and every inbound link survive,
  which delete+create cannot offer and `create_variable` cannot do at all for
  arrays. The current value is converted element-wise, float→text through
  `ToString("R")` so 22.3f becomes "22.3", never 22.299999237060547. Works on
  StoreColumns too; columns behind a recipe array have browse names that
  start with "/", which Studio's `Get` cannot spell, so the route resolves
  parent + child name as a fallback (spell `.../Columns//Setpoints_0`).
  Studio survives every call; no reopen is needed to load the new bridge code
  on a project whose NetSolution rebuilds cleanly.
- **`attach_formatter` op + `POST /bridge/node/attach-formatter`** — attach a
  **formatted dynamic link** to any property: a DynamicLink whose path is built
  at runtime by a StringFormatter from **NAMED** sources (`{#name}` binds to a
  `Source<name>` child), as opposed to `attach_expression`'s positional
  `Source0..N` on an ExpressionEvaluator. This is the "indirect address"
  pattern — a `PanelLoader.Panel` selected by an integer with the candidates as
  readable named variables on the loader instead of opaque NodeId GUIDs inside
  a `ValueMapConverter` — and equally the single-widget `"{#value} {#eu}"`
  unit-label form, which `optix-expression-converter` previously (wrongly)
  documented as not bridge-authorable. Replaces whatever converter or link was
  on the property, so migrating off a `ValueMapConverter` needs no teardown.
  Exposed as a batch op verb; the per-noun `optix_bridge_attach_formatter`
  primitive follows the usual `FTXMCP_BRIDGE_PRIMITIVES` gate.
- **`optix-formatted-dynamic-link` skill** — the pattern, the rules
  (`Source<name>` naming, index suffixes matching the driver's values, the path
  living in `Format` and never in the link's own value, `@NodeId` vs
  `@Pointer`), how to verify it (discriminating render, not `describe_node`),
  and two traps for anyone editing the C#:
  **(a)** assigning `StringFormatter.Format` via its property setter from the
  bridge kills Studio — a corrupted-state exception .NET Core cannot catch, so
  the process vanishes with nothing logged; write the underlying `HasParameter`
  child instead. **(b)** `SetModellingRuleRecursive()` is mandatory: without it
  nodes serialize `ModellingRule: None`, and a `None` member is not
  instantiated into instances of its owning ObjectType — so on a `ScreenType`
  the link is present at design time and absent at runtime, which reads exactly
  like a mis-wired converter. No thread marshaling is needed; `DelayedTask`
  does not help, as a design-time NetLogic's behaviour context is not running.
- **New skill for a silent-failure pattern: `optix-web-click-surface`.** It
  covers the Button-only click surface for web CDP tests — Label, Rectangle,
  Image, and ScreenType root do not fire `MouseClickEvent` in any
  configuration measured; an `Opacity=0` Button is invisible and fires.

### Fixed (continued)
- **`unknown_op_field: name` warned on every `attach_expression` call.**
  `_normalize_edit_op` injects `name` alongside `prop_name` to reconcile the
  C#-validator/Python-applier seam, and the field check runs after it — so the
  service warned about a field it had just added itself. Now accepted for both
  `attach_expression` and `attach_formatter`; genuine typos are still caught.
- **`set_property name="Value"` on a variable node validates, not just applies**. The bridge applier (`SetPropertyInline`) has
  recognised `name="Value"` as the variable's own OPC-UA Value attribute since
  1.0.5, but the batch validator did not — so a correct op was refused as
  `UAVariable has no settable property 'Value'` and the whole batch with it,
  while a variable created in the same batch was waved through as
  `unverifiable_property`. Both halves now agree: `set_property` on a variable
  node with `name="Value"` is accepted and applied cleanly. The equivalent
  spelling from the parent — `path=<parent>, name=<VariableName>` — remains
  supported and is unchanged.
- **Nudge added for pre-1.0.8 bridge compatibility on `name="Value"` errors.**
  A bridge older than 1.0.8 does not support the `name="Value"` self-assign
  spelling and returns `unknown_property` / `UAVariable has no settable property
  'Value'`. The service now detects this response (both on the direct
  `bridge_set_property` path and when augmenting batch-validator error reports)
  and appends a correction pointing the caller at the parent-node spelling —
  `path=<parent>, name=<VariableName>` — so the LLM caller sees the fix without
  a separate `describe_type` round-trip.
- **`optix_interact` normalized-coordinate conversion applied to all
  click/fill actions, not just route replay.** The `<= 1.0` fraction
  convention was documented for `optix_interact` but was only wired into the
  route-replay (`navigate`) path; direct `click` and `fill` calls passed raw
  floats straight to CDP, so `(0.5, 0.25)` landed at pixel (0, 0) instead of
  the centre of the first quadrant. All click and fill paths now call
  `_resolve_point` against the session's `Page.getLayoutMetrics` CSS viewport.
  The success result gains four fields: `css_x` and `css_y` (the resolved CSS
  pixel coordinates actually dispatched), `viewport: {"w": …, "h": …}` (the
  CSS viewport against which they were resolved), and `coords`
  (`"normalized"` when both inputs were ≤ 1.0, `"absolute"` otherwise).
  Out-of-viewport coordinates return `state: "failed", error: "bad_point"`.
- **`settle_seconds` now honoured unconditionally on every interact action.**
  Previously the post-action settle sleep was triggered only when a
  `navigate_url` was also supplied; a plain `click`, `fill`, `type`, `key`,
  or `screenshot` call with an explicit `settle_seconds` silently ignored it.
  `_navigate_if_given` no longer sleeps; each action's caller applies the
  settle unconditionally after dispatching its input event, so the wait fires
  exactly once per call regardless of whether navigation preceded it.
- **Stale tool names corrected in `optix-netlogic-and-bridge` and
  `optix-anchored-edit`.** `optix-netlogic-and-bridge` referenced
  `optix_cdp_screenshot` (now `optix_observe(mode="screenshot")`) and
  `optix_restart_emulator` (now `optix_emulator(action="restart")`), both
  renamed in the 1.0.4 CDP consolidation. `optix-anchored-edit` referenced
  `optix_add_converter` and `optix_add_panel_widget`, which have no successor
  tools; the sentences were rewritten to describe the operations via tools that
  exist. A new tree-wide tool-name lint in `service/tests/test_skills_content.py`
  asserts that every `optix_[a-z_0-9]+` token in every `skills/*/SKILL.md` body
  is a member of `EXPECTED_TOOLS ∪ _BRIDGE_PRIMITIVE_TOOLS`, keeping these
  correct going forward.

- **`ValidateExpressionSyntax` now rejects literals the runtime ExpressionEvaluator cannot parse.**
  `attach_expression` returned `ok:true` and `validate_expression` returned `valid:true` for expressions
  containing `#RRGGBB` colour literals, while the runtime logged
  `Invalid 'Expression' parameter, Syntax error at 13` on every affected node — for example a generated
  `if({0} == 2, #FFF809, 0xFFF0F0F0)` on a button's `BackgroundColor`. A literal-lint table is added to the existing scanner in
  `ValidateExpressionSyntax`: (1) a `#`-prefixed hex token outside a string literal → `invalid_literal` with
  the `0xAARRGGBB` rewrite and the converted value in the message (`#FFF809` → `0xFFFFF809`); (2) a
  capitalised `True`/`False` (Python f-string shape) → lowercase fix. All three callers share the validator —
  `ValidateExpJson` (`:5706`), the `ValidateExpression` ExportMethod (`:162-175`), and
  `AttachExpressionInline`'s gate (`:1387`) — so the batch validator, standalone `validate_expression` tool,
  and `attach_expression` are all corrected in the same change. `invalid_literal` surfaces unmodified through
  `bridge_validate_expression` and through `bridge_edit` report entries.

### Known limitations
- **`optix_bridge_create_netlogic` cannot create a DESIGN-TIME NetLogic.** The
  node it mints compiles, binds its class, and exposes its `[ExportMethod]`s to
  `optix_describe_node` — but Studio offers **no `Execute` entries** for it, so
  `optix_execute_method` can never fire them. The distinguishing marker is
  `BehaviourStartPriority`. Design-time scripts still need one manual Studio
  step (`NetLogic -> Add -> DesignTime NetLogic`), which is the second
  unscriptable step after making a project template.
- `optix_execute_method` derives its tree chain from the on-disk NetLogic YAML,
  so a newly created node needs an `optix_save` before it can be found; the node
  lookup prefix-matches, so a stale `FooScript` shadows `FooScript2`.

## [1.0.7]

The largest release since 1.0.0, consolidating three development streams.
1.0.6 was never published; internal work-in-progress labels up to "1.0.11"
existed during development — everything below ships as **1.0.7**. Full
notes: `docs/release-notes-v1.0.7.md`.

### Added
- **Multi-instance design-time bridge.** Up to 4 Studio instances armed
  simultaneously — `StudioMCPBridge.cs` self-binds the first free port in
  `8768..8771`; every bridge-routed tool resolves the specific bridge
  serving the `project` you name. Fixes "emulator started for the wrong
  (non-bridged) project": with a bridge armed per project, the "first
  focus-able Studio window" fallback is no longer reachable.
- **`DisplayName` is settable** via a dedicated attribute route, and a new
  **`rename` op** lowers to the proven-safe `move` machinery. The crash
  class behind them (node attributes materialized as orphan UA variables →
  Studio access violation) is refused at both bridge and service.
- **`optix_bridge_arm`** (arm/stop the bridge with no human at the
  keyboard, collapsed-tree + BrowseName aware), **`optix_project`**
  (open/create from MCP), **`optix_bridge_log_tail`** (transport
  forensics), **`optix_build_check`** (isolated NetSolution compile check).
- **`optix_bridge_invoke_method`** — generic `ExecuteMethod` wrapper for
  exported NetLogic methods. Confirmed hazard: some built-in methods (e.g.
  `SearchBrokenDynamicLinks`) can crash Studio — treat as crash-capable
  until the bridge marshals `ExecuteMethod` to the main thread.
- `/ui` dashboard: socket chip per configured port with per-port version +
  last-saved age, a distinct "loading" state, per-port Doctor rows with
  hover detail/fix tooltips.

### Fixed
- **Emulator lifecycle is now in-process psutil** — status 2-4s → ~0.15s,
  restart overhead 10-20s → sub-second, `/health` no longer starves under
  slow scans. The `--application-name=Emulator` discrimination is
  unchanged; deployed runtimes are never touched.
- **Batch validation refuses unknown op FIELDS** (`unknown_op_field`)
  instead of applying the op and reporting success.
- **Deploy verification** no longer loses a succeeded deploy to filesystem
  clock granularity.
- **Port-range scan hardening:** no raw-socket pre-probe against live
  listeners (was aborting the C# bridge's accept loop and spamming
  Studio's Output panel), definitive refusals skip the retry sleeps,
  per-port checks run concurrently, and the widget-type catalog is not
  requested when no bridge is armed.
- **`/ui` tool catalog no longer duplicates under concurrent polls**
  (double-checked locking around the lazy build).
- **Nested project directories resolve and list** — a project moved into a
  subfolder is discoverable (recursive `list_projects`, capped depth) and
  addressable; the security boundary remains the post-resolve
  `is_relative_to` check.
- **`describe_node` no longer reports populated dynamic-link/alias paths
  as empty** — `ValueString` unwraps `UAValue.Value` before falling back
  to `ToString()`.
- **Console model finalized:** one launcher (`python.exe`) with
  `--hide-console` / `OPTIX_HIDE_CONSOLE`, `services.ps1 start -Silent`
  rewrites the task action (and recycles a running service on a mode
  change), and every Windows child spawn defaults to `CREATE_NO_WINDOW` —
  no console flashes, real stdout/stderr kept. (An interim windowless-
  launcher approach from the 1.0.6-era stream was replaced by this.)
- Bridge transport: transient write retries with backoff, HTTP-only
  liveness probes, distinct missing-NetLogic diagnosis, post-rebuild drop
  detection with a recovery nudge; the 72-hour Task Scheduler execution
  limit is removed and service crashes land in a lifecycle log.

### Changed
- `optix_active_target()` (and the omit-`project` convenience everywhere)
  refuses to guess with several bridges armed — explicit `project=` is
  required once more than one is up.

## [1.0.5]

Theme: an installer that installs. Full notes: `docs/release-notes-v1.0.5.md`.

### Fixed
- **Fresh installs were broken.** `mcp>=1.2` had no upper bound, so a clean
  `pip install ftx-mcp` resolved MCP Python SDK 2.0.0 (published 2026-07-28),
  which removes `mcp.server.fastmcp`; the server failed at import. Pinned to
  `mcp>=1.2,<2`. Existing installs were unaffected — an already-resolved
  environment still satisfied the old range, so neither `setup.ps1` (which
  reuses `.venv`) nor a plain reinstall re-resolved it.
- **`services.ps1 restart` crashed under `Set-StrictMode`** when the CDP
  chrome had just been killed (#1, reported and diagnosed by @Jraa01). A
  listening socket outlives its process, so `OwningProcess` can name a dead
  pid; reading `.CommandLine` off a `$null` CIM result is a terminating
  error. Guarded at all three call sites (`services.ps1`, `uninstall.ps1`,
  `setup.ps1`); `setup.ps1` deliberately still fails on an unidentifiable
  port holder rather than skipping it.
- **Documented tool counts were wrong.** The default surface is 28 tools;
  `README.md` and `docs/tool-reference.md` both said 37, predating the 1.0.4
  CDP consolidation.

## [1.0.4]

Theme: authoring you can trust, and an agent that spends fewer tokens getting
there. Full notes: `docs/release-notes-v1.0.4.md`.

### Added
- `optix_bridge_edit` — batched, validate-then-apply authoring (U16). A whole
  op list is validated against a hypothetical model before a single node is
  written; `dry_run=true` pre-flights. Not atomic by design (`state="partial"`
  on mid-batch failure).
- Generic enum coercion in the Studio bridge: friendly enum values
  (`FontWeight="Bold"`, `VerticalAlignment="Bottom"`) resolve by reflection
  with an `"Enum"`-suffix strip, falling back to known ordinals.
- Screenshot device-metrics override — `OPTIX_CDP_VIEWPORT` / `OPTIX_CDP_SCALE`
  (fit-to-content capture; supersedes Chrome `--window-size` for capture size).
- Env gates for surface trimming: `FTXMCP_LEGACY_TOOLS=1` (restore 10 CDP
  aliases), `FTXMCP_SKILLS=0` (drop skill tools), `FTXMCP_BRIDGE_PRIMITIVES=1`
  (restore per-noun bridge primitives).

### Changed
- CDP surface consolidated to `optix_observe` / `optix_interact`; the legacy
  `optix_cdp_*` aliases are OFF by default (gated behind `FTXMCP_LEGACY_TOOLS=1`).
- Emulator/status/schema/routes tools consolidated behind action dispatch:
  `optix_emulator(action=...)`, `optix_status(action=...)`,
  `optix_schema(action=...)`, `optix_routes(action=...)`. The old
  `optix_run_emulator` / `_restart_emulator` / `_stop_emulator` /
  `_emulator_status` / `_runtime_log_tail` names were replaced with NO
  deprecated alias — update any scripts to the `action=` form.
- Authoring skills lead with a single `optix_bridge_edit` batch instead of one
  call per property. Folded the standalone `optix-known-pitfalls` skill into
  `optix-expression-converter` and `optix-verify-loop`.

### Fixed
- HorizontalAlignment / VerticalAlignment ordinals corrected
  (Bottom/Right=1, Center=2, Stretch=3) — non-WPF order, live-verified.
- `attach_expression` in a batch reconciles its property-name field
  (`name` ↔ `prop_name`).
- Connect-time `instructions` no longer point at a tool absent from the default
  surface; in-tool hints reference the consolidated tool names.

### Security
- Per-tool scope hardening and installer hardening — see release notes.

## [1.0.3]
See `docs/release-notes-v1.0.3.md`.

## [1.0.2]
See `docs/release-notes-v1.0.2.md`.
